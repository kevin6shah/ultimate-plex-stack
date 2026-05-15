from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote
from uuid import uuid4

import boto3
from fastapi import FastAPI, Header, HTTPException, Request
from mangum import Mangum
from pydantic import BaseModel

from .agent_core import run_agent
from .jobs import (
    AgentConfig,
    AgentJob,
    ArtifactUploadRequest,
    AttachmentRef,
    CheckpointPayload,
    ControlCommand,
    JobSource,
    JobStatus,
    SaveConfigRequest,
    TaskClass,
    ThreadTurnRole,
    WorkerCheckpointRequest,
    WorkerClaimResponse,
    WorkerCompleteRequest,
    WorkerFailureRequest,
    WorkerHeartbeat,
)
from .routing import classify_task, is_long_task
from .settings import settings
from .storage import StateStore
from .telegram import TelegramClient, parse_telegram_update

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app = FastAPI(title="Friday Personal Agent")
api_handler = Mangum(app)


class SiriRequest(BaseModel):
    query: str


class SiriResponse(BaseModel):
    response: str
    queued: bool = False
    job_id: Optional[str] = None


class ApprovalRequest(BaseModel):
    approved: bool
    note: str = ""


def configure_observability() -> None:
    if not settings.logfire_enabled:
        return
    import logfire

    token = settings.secret(settings.logfire_token_param)
    logfire.configure(token=token, send_to_logfire=True)
    logfire.instrument_fastapi(app)
    try:
        logfire.instrument_pydantic_ai()
    except AttributeError:
        pass


configure_observability()


def store() -> StateStore:
    return StateStore(settings)


def queue_client():
    return boto3.client("sqs", region_name=settings.aws_region)


def s3_client():
    return settings.s3


def _dedicated_worker_enabled() -> bool:
    return settings.hands_worker_mode == "dedicated_ec2" and bool(settings.hands_worker_instance_id)


def _worker_instance_state() -> str:
    if not _dedicated_worker_enabled():
        return ""
    response = settings.ec2.describe_instances(InstanceIds=[settings.hands_worker_instance_id])
    reservations = response.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        return ""
    return str(reservations[0]["Instances"][0]["State"]["Name"])


def _ensure_dedicated_worker_running() -> None:
    if not _dedicated_worker_enabled():
        return
    state = _worker_instance_state()
    logger.info("dedicated worker instance state=%s instance_id=%s", state, settings.hands_worker_instance_id)
    if state == "stopped":
        settings.ec2.start_instances(InstanceIds=[settings.hands_worker_instance_id])
    elif state in {"stopping", "pending", "running"}:
        return


def _maybe_stop_dedicated_worker_if_idle(state: StateStore) -> None:
    if not _dedicated_worker_enabled():
        return
    active = state.list_jobs(
        statuses=(
            JobStatus.QUEUED,
            JobStatus.RUNNING,
        ),
        limit=100,
    )
    heavy_active = [job for job in active if job.task_class == TaskClass.HEAVY]
    if heavy_active:
        logger.info("dedicated worker remains running; active heavy jobs=%s", len(heavy_active))
        return
    instance_state = _worker_instance_state()
    if instance_state == "running":
        logger.info("stopping dedicated worker instance_id=%s after queue drain", settings.hands_worker_instance_id)
        settings.ec2.stop_instances(InstanceIds=[settings.hands_worker_instance_id])


def _normalize_query(text: str) -> str:
    return text.strip()


def _is_resume_request(query: str) -> bool:
    lowered = query.lower()
    return any(token in lowered for token in ("continue that task", "resume that task", "resume the task", "continue the task"))


def _is_status_request(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return False
    patterns = (
        r"\b(status|progress|update|updates)\b",
        r"\b(how'?s it going|how is it going|where is it at|where's it at|how far along)\b",
        r"\b(is it done|did it finish|did that finish|still working)\b",
        r"\b(check on that|check that task|check the task)\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _is_list_tasks_request(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return False
    patterns = (
        r"\b(list|show|see)\b.{0,20}\b(tasks|jobs)\b",
        r"\bwhat tasks are\b",
        r"\bwhat jobs are\b",
        r"\bactive tasks\b",
        r"\brunning tasks\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _is_stop_request(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return False
    return any(re.search(pattern, normalized) for pattern in (r"^\s*(stop|cancel|abort)\b", r"\b(stop|cancel|abort)\b.{0,20}\b(task|job)\b"))


def _resume_query_text(query: str, resumable: Optional[AgentJob]) -> str:
    if resumable is None:
        return query
    normalized = query.strip().lower()
    exact_resume_requests = {
        "resume that task",
        "resume the task",
        "continue that task",
        "continue the task",
    }
    if normalized in exact_resume_requests and resumable.query.strip():
        return resumable.query
    return query


def _remember_if_tagged(state: StateStore, *, user_id: str, query: str) -> bool:
    normalized = query.strip()
    if not normalized.startswith("#"):
        return False
    state.remember_fact(owner=user_id, text=normalized[1:].strip())
    return True


def _looks_like_internal_tool_markup(text: str) -> bool:
    normalized = (text or "").strip()
    if not normalized:
        return False
    markers = (
        "<| DSML |",
        "<|DSML|",
        "tool_calls>",
        "invoke name=\"",
        "parameter name=\"query\"",
    )
    return any(marker in normalized for marker in markers)


def _latest_status_job_for_user(state: StateStore, *, source: JobSource, user_id: str) -> Optional[AgentJob]:
    active_statuses = (
        JobStatus.RUNNING,
        JobStatus.WAITING_WORKER,
        JobStatus.QUEUED,
        JobStatus.WAITING_APPROVAL,
        JobStatus.CHECKPOINTED,
        JobStatus.INTERRUPTED,
        JobStatus.PAUSED_BUDGET,
        JobStatus.TIMED_OUT,
    )
    active = state.get_latest_job_for_user(source=source.value, user_id=user_id, statuses=active_statuses)
    if active is not None:
        return active
    recent_statuses = (
        JobStatus.COMPLETED,
        JobStatus.FAILED,
    )
    return state.get_latest_job_for_user(source=source.value, user_id=user_id, statuses=recent_statuses)


def _active_jobs_for_user(state: StateStore, *, source: JobSource, user_id: str) -> list[AgentJob]:
    return state.list_jobs_for_user(
        source=source.value,
        user_id=user_id,
        statuses=(
            JobStatus.RUNNING,
            JobStatus.WAITING_WORKER,
            JobStatus.QUEUED,
            JobStatus.WAITING_APPROVAL,
        ),
        limit=10,
    )


def _format_tasks_list(state: StateStore, jobs: list[AgentJob]) -> str:
    if not jobs:
        return "I do not see any active long-running tasks right now."
    lines = ["Here are your current long-running tasks:"]
    for index, job in enumerate(jobs, start=1):
        summary = (job.latest_checkpoint_summary or "").strip()
        step = (job.current_step or "").strip()
        line = f"{index}. {job.job_id[:8]} - {job.status.value}"
        if step:
            line += f" - {step}"
        elif summary:
            line += f" - {summary[:120]}"
        lines.append(line)
    lines.append("Reply with 'stop 1', 'stop 2', or 'stop <job id>' to stop one.")
    return "\n".join(lines)


def _resolve_stop_target(query: str, jobs: list[AgentJob]) -> Optional[AgentJob]:
    if not jobs:
        return None
    lowered = query.strip().lower()
    partial_match = re.search(r"\b([0-9a-f]{8,36})\b", lowered)
    if partial_match:
        token = partial_match.group(1)
        for job in jobs:
            if job.job_id.lower().startswith(token):
                return job
    ordinal_match = re.search(r"\b(?:task|job)?\s*([1-9])\b", lowered)
    if ordinal_match:
        index = int(ordinal_match.group(1)) - 1
        if 0 <= index < len(jobs):
            return jobs[index]
    if any(token in lowered for token in ("latest", "last", "that")):
        return jobs[0]
    return jobs[0] if len(jobs) == 1 else None


def _format_status_message(state: StateStore, job: Optional[AgentJob]) -> str:
    if job is None:
        return "I do not see a recent long-running task to report on."
    checkpoint = state.get_latest_checkpoint(job.job_id)
    summary = (job.latest_checkpoint_summary or (checkpoint.summary if checkpoint else "") or "").strip()
    step = (job.current_step or (checkpoint.current_step if checkpoint else "") or "").strip()
    step_line = f"\nCurrent step: {step}" if step else ""
    summary_line = f"\nLatest update: {summary}" if summary else ""
    if job.status in {JobStatus.QUEUED, JobStatus.WAITING_WORKER}:
        return f"Your latest task is queued.{step_line}{summary_line}".strip()
    if job.status == JobStatus.RUNNING:
        return f"Still working on your latest task.{step_line}{summary_line}".strip()
    if job.status == JobStatus.WAITING_APPROVAL:
        return f"Your latest task is waiting for approval.{step_line}{summary_line}".strip()
    if job.status in {JobStatus.CHECKPOINTED, JobStatus.INTERRUPTED, JobStatus.TIMED_OUT}:
        tail = " Say 'resume that task' when you want me to continue."
        return f"Your latest task is {job.status.value}.{step_line}{summary_line}{tail}".strip()
    if job.status == JobStatus.PAUSED_BUDGET:
        return f"Your latest task is paused because of budget limits.{step_line}{summary_line}".strip()
    if job.status == JobStatus.COMPLETED:
        result = (job.result_preview or "").strip()
        result_line = f"\nResult: {result[:800]}" if result else ""
        files_line = f"\nFiles: {', '.join(job.output_files[:5])}" if job.output_files else ""
        return f"Your latest task completed.{result_line}{files_line}".strip()
    if job.status == JobStatus.FAILED:
        error = (job.error_message or "no error details were recorded").strip()
        return f"Your latest task failed. Error: {error[:800]}".strip()
    return f"Your latest task is {job.status.value}.{step_line}{summary_line}".strip()


def _artifacts_prefix(job_id: str) -> str:
    return f"jobs/{job_id}"


def _artifact_key(job_id: str, kind: str, file_name: str) -> str:
    safe_name = quote(file_name, safe="._-() ").replace("%20", "_")
    return f"{_artifacts_prefix(job_id)}/{kind}/{safe_name}"


def _auth_owner_key_from_siri() -> str:
    return "owner"


def _auth_or_401(expected_key: str, provided_key: Optional[str]) -> None:
    if expected_key and provided_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid siri key")


async def enqueue_light_job(job: AgentJob) -> None:
    logger.info("queueing light job source=%s job_id=%s", job.source, job.job_id)
    queue_client().send_message(QueueUrl=settings.job_queue_url, MessageBody=job.model_dump_json())


async def upload_telegram_document(job_id: str, document, telegram: TelegramClient) -> AttachmentRef:
    file_info = await telegram.get_file(document.file_id)
    if not file_info.file_path:
        raise RuntimeError("telegram file path missing")
    size_bytes = int(document.file_size or file_info.file_size or 0)
    if size_bytes > settings.max_attachment_bytes:
        raise HTTPException(status_code=413, detail="attachment too large")
    payload = await telegram.download_file_bytes(file_info.file_path)
    object_key = _artifact_key(job_id, "inputs", document.file_name or f"{document.file_unique_id}.bin")
    s3_client().put_object(
        Bucket=settings.artifacts_bucket,
        Key=object_key,
        Body=payload,
        ContentType=document.mime_type or "application/octet-stream",
    )
    return AttachmentRef(
        object_key=object_key,
        file_name=document.file_name or object_key.rsplit("/", 1)[-1],
        mime_type=document.mime_type or "application/octet-stream",
        size_bytes=len(payload),
    )


def _build_light_context(state: StateStore, *, channel: str, user_id: str, conversation_id: str) -> tuple[str, list[Any], list[str], AgentConfig]:
    context, turns, config = state.get_context_bundle(channel=channel, user_id=user_id, conversation_id=conversation_id)
    memories = state.list_memories(owner=user_id)
    return context, turns, memories, config


async def run_and_notify(job: AgentJob) -> None:
    state = store()
    telegram = TelegramClient(settings)
    context_summary, recent_turns, memories, config = _build_light_context(
        state,
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
    )
    logger.info("starting light job source=%s job_id=%s", job.source, job.job_id)
    try:
        result = await asyncio.wait_for(
            run_agent(
                job.query,
                settings=settings,
                store=state,
                mode="light",
                context_summary=context_summary,
                recent_turns=recent_turns,
                durable_memories=memories,
                config=config,
            ),
            timeout=settings.light_task_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning("light job timed out before lambda limit job_id=%s", job.job_id)
        timeout_message = (
            "That task is taking too long for the serverless path, so I stopped it before the Lambda timeout. "
            "Try again after the hands worker is deployed, or break it into a smaller request."
        )
        state.record_turn(
            channel=job.source.value,
            user_id=job.user_id or "unknown",
            conversation_id=job.conversation_id or "default",
            role=ThreadTurnRole.ASSISTANT,
            text=timeout_message,
            task_class=TaskClass.LIGHT,
        )
        if job.chat_id:
            await telegram.send_message(job.chat_id, timeout_message)
        return
    except Exception as exc:
        logger.exception("light job failed job_id=%s", job.job_id)
        failure_message = f"That task failed before completion: {exc}"
        state.record_turn(
            channel=job.source.value,
            user_id=job.user_id or "unknown",
            conversation_id=job.conversation_id or "default",
            role=ThreadTurnRole.ASSISTANT,
            text=failure_message,
            task_class=TaskClass.LIGHT,
        )
        if job.chat_id:
            await telegram.send_message(job.chat_id, failure_message[:4000])
        return

    if _looks_like_internal_tool_markup(result.text):
        logger.warning("light job leaked internal tool markup; upgrading to heavy job_id=%s", job.job_id)
        heavy_job = AgentJob(
            source=job.source,
            query=job.query,
            task_class=TaskClass.HEAVY,
            chat_id=job.chat_id,
            user_id=job.user_id,
            conversation_id=job.conversation_id,
            long_task=True,
            attachments=job.attachments,
            resume_from_job_id=job.resume_from_job_id,
        )
        state.create_job(heavy_job)
        _ensure_dedicated_worker_running()
        if job.chat_id:
            await telegram.send_message(job.chat_id, "I started that and will notify you in Telegram.")
        return

    state.record_turn(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
        role=ThreadTurnRole.ASSISTANT,
        text=result.text,
        task_class=TaskClass.LIGHT,
    )
    if job.chat_id:
        await telegram.send_message(job.chat_id, result.text)
    logger.info("finished light job budget_blocked=%s job_id=%s", result.budget_blocked, job.job_id)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/jobs/{job_id}")
async def job_status(job_id: str, x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    job = store().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    checkpoint = store().get_latest_checkpoint(job_id)
    return {
        "job": job.model_dump(),
        "latest_checkpoint": checkpoint.model_dump() if checkpoint else None,
    }


@app.post("/jobs/{job_id}/approve")
async def approve_job(job_id: str, body: ApprovalRequest, x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, str]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    state = store()
    if state.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    state.record_approval(job_id, approved=body.approved, note=body.note)
    state.update_job_status(job_id, status=JobStatus.RUNNING if body.approved else JobStatus.FAILED, current_step="approval received")
    return {"status": "ok"}


@app.get("/memory")
async def list_memory(x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    memories = store().list_memories(owner=_auth_owner_key_from_siri())
    return {"memories": memories}


@app.delete("/memory")
async def delete_memory(fact: str, x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, str]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    store().forget_fact(owner=_auth_owner_key_from_siri(), text=fact)
    return {"status": "ok"}


@app.get("/config")
async def get_config(x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    config = store().get_config()
    return {"config": config.model_dump()}


@app.put("/config")
async def save_config(body: SaveConfigRequest, x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    config = store().save_config(body.config)
    return {"config": config.model_dump()}


@app.get("/threads/{conversation_id}")
async def get_thread_turns(
    conversation_id: str,
    channel: str = "telegram",
    user_id: Optional[str] = None,
    x_friday_siri_key: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    owner = user_id or ("siri" if channel == "siri" else settings.secret(settings.telegram_allowed_chat_id_param) or "unknown")
    turns = store().get_recent_turns(channel=channel, user_id=owner, conversation_id=conversation_id, limit=50)
    return {"turns": [turn.model_dump() for turn in turns]}


@app.delete("/threads/{conversation_id}")
async def delete_thread(
    conversation_id: str,
    channel: str = "telegram",
    user_id: Optional[str] = None,
    x_friday_siri_key: Optional[str] = Header(default=None),
) -> dict[str, str]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    owner = user_id or ("siri" if channel == "siri" else settings.secret(settings.telegram_allowed_chat_id_param) or "unknown")
    store().clear_thread(channel=channel, user_id=owner, conversation_id=conversation_id)
    return {"status": "ok"}


@app.get("/context/recent")
async def recent_context(x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    return {"contexts": store().list_recent_contexts()}


@app.get("/worker/health")
async def worker_health(x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    active = store().list_jobs(statuses=(JobStatus.RUNNING, JobStatus.WAITING_WORKER, JobStatus.CHECKPOINTED, JobStatus.INTERRUPTED), limit=10)
    return {"active_jobs": [job.model_dump() for job in active]}


@app.get("/spend/status")
async def spend_status(x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    daily, monthly = store().spend_totals()
    config = store().get_config()
    return {
        "daily_usd": str(daily),
        "monthly_usd": str(monthly),
        "daily_budget_usd": config.daily_budget_usd if config.daily_budget_usd is not None else settings.daily_budget_usd,
        "monthly_budget_usd": config.monthly_budget_usd if config.monthly_budget_usd is not None else settings.monthly_budget_usd,
    }


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str, x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, str]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    store().delete_job(job_id)
    return {"status": "ok"}


@app.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str, x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, str]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    state = store()
    job = state.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in {JobStatus.QUEUED, JobStatus.WAITING_WORKER}:
        state.update_job_status(job_id, status=JobStatus.INTERRUPTED, current_step="stopped by user", error_message="stopped by user")
        return {"status": "ok"}
    state.record_control_signal(job_id, command=ControlCommand.STOP, note="stopped by user")
    return {"status": "ok"}


@app.post("/telegram")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(default=None)) -> dict[str, str]:
    expected_secret = settings.secret(settings.telegram_webhook_secret_param)
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=401, detail="invalid telegram secret")

    payload = await request.json()
    update = parse_telegram_update(payload)
    message = update.message
    if message is None:
        logger.info("ignoring telegram update without message")
        return {"status": "ignored"}

    query = message.effective_text
    if not query and message.document is None:
        logger.info("ignoring telegram update without text or document")
        return {"status": "ignored"}

    chat_id = str(message.chat.id)
    allowed_chat_id = settings.secret(settings.telegram_allowed_chat_id_param)
    if allowed_chat_id and chat_id != allowed_chat_id:
        logger.warning("blocked telegram chat_id=%s", chat_id)
        raise HTTPException(status_code=403, detail="chat not allowed")

    user_id = str(message.from_.id if message.from_ else message.chat.id)
    conversation_id = chat_id
    state = store()
    state.put_session(channel="telegram", user_id=user_id, metadata={"chat_id": chat_id})

    if query and _remember_if_tagged(state, user_id=user_id, query=query):
        await TelegramClient(settings).send_message(chat_id, "I will remember that.")
        return {"status": "remembered"}

    if query and _is_list_tasks_request(query):
        state.record_turn(
            channel="telegram",
            user_id=user_id,
            conversation_id=conversation_id,
            role=ThreadTurnRole.USER,
            text=query,
            task_class=TaskClass.LIGHT,
        )
        jobs = _active_jobs_for_user(state, source=JobSource.TELEGRAM, user_id=user_id)
        status_text = _format_tasks_list(state, jobs)
        state.record_turn(
            channel="telegram",
            user_id=user_id,
            conversation_id=conversation_id,
            role=ThreadTurnRole.ASSISTANT,
            text=status_text,
            task_class=TaskClass.LIGHT,
        )
        await TelegramClient(settings).send_message(chat_id, status_text)
        return {"status": "tasks_listed"}

    if query and _is_status_request(query):
        state.record_turn(
            channel="telegram",
            user_id=user_id,
            conversation_id=conversation_id,
            role=ThreadTurnRole.USER,
            text=query,
            task_class=TaskClass.LIGHT,
        )
        status_text = _format_status_message(
            state,
            _latest_status_job_for_user(state, source=JobSource.TELEGRAM, user_id=user_id),
        )
        state.record_turn(
            channel="telegram",
            user_id=user_id,
            conversation_id=conversation_id,
            role=ThreadTurnRole.ASSISTANT,
            text=status_text,
            task_class=TaskClass.LIGHT,
        )
        await TelegramClient(settings).send_message(chat_id, status_text)
        return {"status": "status_reported"}

    if query and _is_stop_request(query):
        state.record_turn(
            channel="telegram",
            user_id=user_id,
            conversation_id=conversation_id,
            role=ThreadTurnRole.USER,
            text=query,
            task_class=TaskClass.LIGHT,
        )
        jobs = _active_jobs_for_user(state, source=JobSource.TELEGRAM, user_id=user_id)
        target = _resolve_stop_target(query, jobs)
        if target is None:
            reply = "I could not tell which running task to stop. Ask me to list your tasks, then say something like 'stop 1' or 'stop <job id>'."
        elif target.status in {JobStatus.QUEUED, JobStatus.WAITING_WORKER}:
            state.update_job_status(target.job_id, status=JobStatus.INTERRUPTED, current_step="stopped by user", error_message="stopped by user")
            reply = f"Stopped task {target.job_id[:8]} before it started running."
        else:
            state.record_control_signal(target.job_id, command=ControlCommand.STOP, note="stopped by user")
            reply = f"I asked the worker to stop task {target.job_id[:8]}. I will update you when it is interrupted."
        state.record_turn(
            channel="telegram",
            user_id=user_id,
            conversation_id=conversation_id,
            role=ThreadTurnRole.ASSISTANT,
            text=reply,
            task_class=TaskClass.LIGHT,
        )
        await TelegramClient(settings).send_message(chat_id, reply)
        return {"status": "stop_requested"}

    task_class = classify_task(query or "file task", has_attachment=message.document is not None)
    if query:
        state.record_turn(
            channel="telegram",
            user_id=user_id,
            conversation_id=conversation_id,
            role=ThreadTurnRole.USER,
            text=query,
            task_class=task_class,
        )
    resume_from_job_id = None
    effective_query = query
    if query and _is_resume_request(query):
        resumable = state.get_latest_job_for_user(
            source=JobSource.TELEGRAM.value,
            user_id=user_id,
            statuses=(JobStatus.CHECKPOINTED, JobStatus.INTERRUPTED, JobStatus.TIMED_OUT, JobStatus.FAILED),
        )
        if resumable is not None:
            resume_from_job_id = resumable.job_id
            effective_query = _resume_query_text(query, resumable)

    attachments: list[AttachmentRef] = []
    if message.document is not None:
        job_id = str(uuid4())
        attachments.append(await upload_telegram_document(job_id, message.document, TelegramClient(settings)))
        created_job = AgentJob(
            job_id=job_id,
            source=JobSource.TELEGRAM,
            query=effective_query or f"Process attachment {attachments[0].file_name}",
            task_class=TaskClass.HEAVY,
            chat_id=chat_id,
            user_id=user_id,
            conversation_id=conversation_id,
            long_task=True,
            attachments=attachments,
            resume_from_job_id=resume_from_job_id,
        )
        state.create_job(created_job)
        _ensure_dedicated_worker_running()
        await TelegramClient(settings).send_message(chat_id, "I started that and will notify you in Telegram.")
        return {"status": "queued", "job_id": created_job.job_id}

    if task_class == TaskClass.HEAVY:
        job = AgentJob(
            source=JobSource.TELEGRAM,
            query=effective_query,
            task_class=TaskClass.HEAVY,
            chat_id=chat_id,
            user_id=user_id,
            conversation_id=conversation_id,
            long_task=True,
            attachments=attachments,
            resume_from_job_id=resume_from_job_id,
        )
        state.create_job(job)
        _ensure_dedicated_worker_running()
        await TelegramClient(settings).send_message(chat_id, "I started that and will notify you in Telegram.")
        return {"status": "queued", "job_id": job.job_id}

    job = AgentJob(
        source=JobSource.TELEGRAM,
        query=query,
        task_class=TaskClass.LIGHT,
        chat_id=chat_id,
        user_id=user_id,
        conversation_id=conversation_id,
        long_task=is_long_task(query),
    )
    await enqueue_light_job(job)
    return {"status": "queued", "job_id": job.job_id}


@app.post("/siri", response_model=SiriResponse)
async def siri(request: SiriRequest, x_friday_siri_key: Optional[str] = Header(default=None)) -> SiriResponse:
    expected_key = settings.secret(settings.siri_api_key_param)
    if expected_key and x_friday_siri_key != expected_key:
        logger.warning("blocked siri request with invalid key")
        raise HTTPException(status_code=401, detail="invalid siri key")

    query = _normalize_query(request.query)
    state = store()
    if _remember_if_tagged(state, user_id=_auth_owner_key_from_siri(), query=query):
        return SiriResponse(response="I will remember that.")

    if _is_list_tasks_request(query):
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id="siri",
            role=ThreadTurnRole.USER,
            text=query,
            task_class=TaskClass.LIGHT,
        )
        jobs = _active_jobs_for_user(state, source=JobSource.SIRI, user_id="siri")
        status_text = _format_tasks_list(state, jobs)
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id="siri",
            role=ThreadTurnRole.ASSISTANT,
            text=status_text,
            task_class=TaskClass.LIGHT,
        )
        return SiriResponse(response=status_text)

    if _is_status_request(query):
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id="siri",
            role=ThreadTurnRole.USER,
            text=query,
            task_class=TaskClass.LIGHT,
        )
        status_text = _format_status_message(
            state,
            _latest_status_job_for_user(state, source=JobSource.SIRI, user_id="siri"),
        )
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id="siri",
            role=ThreadTurnRole.ASSISTANT,
            text=status_text,
            task_class=TaskClass.LIGHT,
        )
        return SiriResponse(response=status_text)

    if _is_stop_request(query):
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id="siri",
            role=ThreadTurnRole.USER,
            text=query,
            task_class=TaskClass.LIGHT,
        )
        jobs = _active_jobs_for_user(state, source=JobSource.SIRI, user_id="siri")
        target = _resolve_stop_target(query, jobs)
        if target is None:
            reply = "I could not tell which running task to stop. Ask me to list your tasks, then say something like stop 1."
        elif target.status in {JobStatus.QUEUED, JobStatus.WAITING_WORKER}:
            state.update_job_status(target.job_id, status=JobStatus.INTERRUPTED, current_step="stopped by user", error_message="stopped by user")
            reply = f"Stopped task {target.job_id[:8]} before it started running."
        else:
            state.record_control_signal(target.job_id, command=ControlCommand.STOP, note="stopped by user")
            reply = f"I asked the worker to stop task {target.job_id[:8]}."
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id="siri",
            role=ThreadTurnRole.ASSISTANT,
            text=reply,
            task_class=TaskClass.LIGHT,
        )
        return SiriResponse(response=reply)

    allowed_chat_id = settings.secret(settings.telegram_allowed_chat_id_param)
    conversation_id = "siri"
    task_class = classify_task(query)
    resume_from_job_id = None
    effective_query = query
    if _is_resume_request(query):
        resumable = state.get_latest_job_for_user(
            source=JobSource.SIRI.value,
            user_id="siri",
            statuses=(JobStatus.CHECKPOINTED, JobStatus.INTERRUPTED, JobStatus.TIMED_OUT, JobStatus.FAILED),
        )
        if resumable is not None:
            resume_from_job_id = resumable.job_id
            task_class = TaskClass.HEAVY
            effective_query = _resume_query_text(query, resumable)

    if task_class == TaskClass.HEAVY:
        logger.info("siri request queued as heavy task")
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id=conversation_id,
            role=ThreadTurnRole.USER,
            text=query,
            task_class=TaskClass.HEAVY,
        )
        job = AgentJob(
            source=JobSource.SIRI,
            query=effective_query,
            task_class=TaskClass.HEAVY,
            status=JobStatus.QUEUED,
            chat_id=allowed_chat_id or None,
            user_id="siri",
            conversation_id=conversation_id,
            long_task=True,
            resume_from_job_id=resume_from_job_id,
        )
        state.create_job(job)
        _ensure_dedicated_worker_running()
        return SiriResponse(response="I started that and will notify you in Telegram.", queued=True, job_id=job.job_id)

    state.record_turn(
        channel="siri",
        user_id="siri",
        conversation_id=conversation_id,
        role=ThreadTurnRole.USER,
        text=query,
        task_class=task_class,
    )

    context_summary, recent_turns, memories, config = _build_light_context(state, channel="siri", user_id="siri", conversation_id=conversation_id)
    try:
        logger.info("running synchronous siri request")
        result = await asyncio.wait_for(
            run_agent(
                query,
                settings=settings,
                store=state,
                mode="light",
                context_summary=context_summary,
                recent_turns=recent_turns,
                durable_memories=memories,
                config=config,
            ),
            timeout=settings.siri_short_timeout_seconds,
        )
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id=conversation_id,
            role=ThreadTurnRole.ASSISTANT,
            text=result.text,
            task_class=TaskClass.LIGHT,
        )
        if _looks_like_internal_tool_markup(result.text):
            logger.warning("siri light path leaked internal tool markup; re-queuing as heavy")
            job = AgentJob(
                source=JobSource.SIRI,
                query=query,
                task_class=TaskClass.HEAVY,
                status=JobStatus.QUEUED,
                chat_id=allowed_chat_id or None,
                user_id="siri",
                conversation_id=conversation_id,
                long_task=True,
            )
            state.create_job(job)
            _ensure_dedicated_worker_running()
            return SiriResponse(response="I started that and will notify you in Telegram.", queued=True, job_id=job.job_id)
        return SiriResponse(response=result.text)
    except TimeoutError:
        logger.info("siri request timed out and was re-queued as heavy task")
        job = AgentJob(
            source=JobSource.SIRI,
            query=query,
            task_class=TaskClass.HEAVY,
            chat_id=allowed_chat_id or None,
            user_id="siri",
            conversation_id=conversation_id,
            long_task=True,
        )
        state.create_job(job)
        _ensure_dedicated_worker_running()
        return SiriResponse(response="I started that and will notify you in Telegram.", queued=True, job_id=job.job_id)


@app.post("/internal/worker/claim", response_model=WorkerClaimResponse)
async def worker_claim(request: Request, x_friday_worker_key: Optional[str] = Header(default=None)) -> WorkerClaimResponse:
    expected_key = settings.secret(settings.worker_api_key_param)
    if expected_key and x_friday_worker_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid worker key")

    state = store()
    if not state.budget_available():
        return WorkerClaimResponse(ok=False)
    job = state.claim_next_heavy_job()
    if job is None:
        return WorkerClaimResponse(ok=False)
    context_summary, recent_turns, config = state.get_context_bundle(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
    )
    memories = state.list_memories(owner=job.user_id or _auth_owner_key_from_siri())
    resume_checkpoint = state.get_latest_checkpoint(job.resume_from_job_id) if job.resume_from_job_id else None
    attachments = []
    for attachment in job.attachments:
        attachments.append(
            {
                "file_name": attachment.file_name,
                "mime_type": attachment.mime_type,
                "size_bytes": attachment.size_bytes,
                "object_key": attachment.object_key,
                "download_url": s3_client().generate_presigned_url(
                    "get_object",
                    Params={"Bucket": settings.artifacts_bucket, "Key": attachment.object_key},
                    ExpiresIn=settings.artifact_url_ttl_seconds,
                ),
            }
        )
    base_url = str(request.base_url).rstrip("/")
    return WorkerClaimResponse(
        ok=True,
        job=job,
        attachments=attachments,
        context_summary=context_summary,
        recent_turns=recent_turns,
        durable_memories=memories,
        resume_checkpoint=resume_checkpoint,
        config=config,
        api_base_url=base_url,
        artifacts_prefix=_artifacts_prefix(job.job_id),
    )


@app.post("/internal/worker/claim/{job_id}", response_model=WorkerClaimResponse)
async def worker_claim_specific(job_id: str, request: Request, x_friday_worker_key: Optional[str] = Header(default=None)) -> WorkerClaimResponse:
    expected_key = settings.secret(settings.worker_api_key_param)
    if expected_key and x_friday_worker_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid worker key")

    state = store()
    job = state.claim_job(job_id)
    if job is None:
        return WorkerClaimResponse(ok=False)
    context_summary, recent_turns, config = state.get_context_bundle(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
    )
    memories = state.list_memories(owner=job.user_id or _auth_owner_key_from_siri())
    resume_checkpoint = state.get_latest_checkpoint(job.resume_from_job_id) if job.resume_from_job_id else None
    attachments = []
    for attachment in job.attachments:
        attachments.append(
            {
                "file_name": attachment.file_name,
                "mime_type": attachment.mime_type,
                "size_bytes": attachment.size_bytes,
                "object_key": attachment.object_key,
                "download_url": s3_client().generate_presigned_url(
                    "get_object",
                    Params={"Bucket": settings.artifacts_bucket, "Key": attachment.object_key},
                    ExpiresIn=settings.artifact_url_ttl_seconds,
                ),
            }
        )
    base_url = str(request.base_url).rstrip("/")
    return WorkerClaimResponse(
        ok=True,
        job=job,
        attachments=attachments,
        context_summary=context_summary,
        recent_turns=recent_turns,
        durable_memories=memories,
        resume_checkpoint=resume_checkpoint,
        config=config,
        api_base_url=base_url,
        artifacts_prefix=_artifacts_prefix(job.job_id),
    )


@app.post("/internal/worker/heartbeat")
async def worker_heartbeat(body: WorkerHeartbeat, x_friday_worker_key: Optional[str] = Header(default=None)) -> dict[str, str]:
    expected_key = settings.secret(settings.worker_api_key_param)
    if expected_key and x_friday_worker_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid worker key")
    state = store()
    job = state.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    state.update_job_heartbeat(body.job_id, current_step=body.current_step, summary=body.summary)
    if body.notify and job.chat_id:
        config = state.get_config()
        if state.should_send_status_update(body.job_id, interval_seconds=config.status_update_interval_seconds):
            summary = body.summary.strip() or body.current_step.strip() or "still working"
            await TelegramClient(settings).send_message(job.chat_id, f"Still working: {summary[:1000]}")
            state.mark_status_update_sent(body.job_id)
    return {"status": "ok"}


@app.get("/internal/worker/control/{job_id}")
async def worker_control(job_id: str, x_friday_worker_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.worker_api_key_param)
    if expected_key and x_friday_worker_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid worker key")
    signal = store().get_latest_control_signal(job_id)
    if signal is None:
        return {"control": None}
    return {"control": signal.model_dump()}


@app.post("/internal/worker/checkpoint")
async def worker_checkpoint(body: WorkerCheckpointRequest, x_friday_worker_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.worker_api_key_param)
    if expected_key and x_friday_worker_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid worker key")
    state = store()
    if state.get_job(body.job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    seq = state.save_checkpoint(body.job_id, body.checkpoint)
    return {"status": "ok", "checkpoint_seq": seq}


@app.post("/internal/worker/artifact-url")
async def worker_artifact_url(body: ArtifactUploadRequest, x_friday_worker_key: Optional[str] = Header(default=None)) -> dict[str, str]:
    expected_key = settings.secret(settings.worker_api_key_param)
    if expected_key and x_friday_worker_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid worker key")
    if store().get_job(body.job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    key = _artifact_key(body.job_id, "outputs", body.file_name)
    url = s3_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.artifacts_bucket, "Key": key, "ContentType": body.content_type},
        ExpiresIn=settings.artifact_url_ttl_seconds,
    )
    return {"upload_url": url, "object_key": key}


@app.post("/internal/worker/complete")
async def worker_complete(body: WorkerCompleteRequest, x_friday_worker_key: Optional[str] = Header(default=None)) -> dict[str, str]:
    expected_key = settings.secret(settings.worker_api_key_param)
    if expected_key and x_friday_worker_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid worker key")
    state = store()
    job = state.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    state.update_job_status(
        body.job_id,
        status=JobStatus.COMPLETED,
        current_step="completed",
        result_preview=body.result_text,
        output_files=body.output_files,
        artifact_keys=body.artifact_keys,
    )
    state.record_turn(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
        role=ThreadTurnRole.ASSISTANT,
        text=body.result_text,
        task_class=TaskClass.HEAVY,
    )
    if job.chat_id:
        message = body.result_text
        if body.output_files:
            message += "\n\nOutput files:\n" + "\n".join(f"- {name}" for name in body.output_files[:20])
        telegram = TelegramClient(settings)
        await telegram.send_message(job.chat_id, message)
        for file_name, object_key in zip(body.output_files[:10], body.artifact_keys[:10]):
            response = s3_client().get_object(Bucket=settings.artifacts_bucket, Key=object_key)
            content = response["Body"].read()
            await telegram.send_document_bytes(job.chat_id, file_name, content, caption=file_name)
    _maybe_stop_dedicated_worker_if_idle(state)
    return {"status": "ok"}


@app.post("/internal/worker/fail")
async def worker_fail(body: WorkerFailureRequest, x_friday_worker_key: Optional[str] = Header(default=None)) -> dict[str, str]:
    expected_key = settings.secret(settings.worker_api_key_param)
    if expected_key and x_friday_worker_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid worker key")
    state = store()
    job = state.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    status = JobStatus.FAILED
    if body.interrupted:
        status = JobStatus.INTERRUPTED
    elif body.timed_out:
        status = JobStatus.TIMED_OUT
    state.update_job_status(body.job_id, status=status, current_step="failed", error_message=body.error_message)
    state.record_turn(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
        role=ThreadTurnRole.ASSISTANT,
        text=f"Task {status.value}: {body.error_message}",
        task_class=TaskClass.HEAVY,
    )
    if job.chat_id:
        await TelegramClient(settings).send_message(job.chat_id, f"Task {status.value}: {body.error_message}")
    _maybe_stop_dedicated_worker_if_idle(state)
    return {"status": "ok"}


async def handle_sqs_event(event: dict[str, Any]) -> dict[str, Any]:
    for record in event.get("Records", []):
        job = AgentJob.model_validate_json(record["body"])
        try:
            await run_and_notify(job)
        except Exception:
            logger.exception("job failed job_id=%s", job.job_id)
            raise
    return {"status": "ok"}


def handler(event: dict[str, Any], context: Any) -> Any:
    if event.get("Records") and event["Records"][0].get("eventSource") == "aws:sqs":
        return asyncio.run(handle_sqs_event(event))
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    return api_handler(event, context)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8080)
