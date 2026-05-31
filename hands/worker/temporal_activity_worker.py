from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

AGENT_ROOT = Path(__file__).resolve().parents[2] / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from app.agent_core import PauseForInputRequested, run_agent
from app.artifacts import is_browser_step_screenshot, query_requests_browser_images
from app.heavy_job_runtime import (
    artifact_key,
    extract_partial_findings_block,
    progress_notification_text,
    progress_summary_for_step,
    status_summary_for_query,
    summarize_recoverable_failure,
)
from app.jobs import AgentConfig, AgentJob, CheckpointPayload, ThreadTurn
from app.schemas.execution_state import ExecutionProgressMatrix, JobContext
from app.settings import Settings
from app.storage import StateStore
from app.strategy_runtime import (
    advance_strategy_state,
    default_strategy_state,
    is_retryable_interaction_failure,
    normalize_execution_progress_matrix,
    normalize_strategy_state,
    strategy_threshold_for_error,
)
from app.telegram import TelegramClient
from app.workspace import Workspace


class LocalBudgetStore:
    def __init__(self) -> None:
        self.last_cost = "0"

    def budget_available(self) -> bool:
        return True

    def add_spend(self, amount) -> None:
        self.last_cost = str(amount)


def _workspace_root() -> Path:
    return Path(os.environ.get("FRIDAY_WORKSPACE_ROOT", "/srv/friday-hands/workspaces"))


def _prepare_workspace(job_id: str, *, resume_checkpoint: dict[str, Any] | None) -> Workspace:
    root = _workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    workspace_dir = root / job_id
    is_resume = resume_checkpoint is not None and workspace_dir.exists()
    if workspace_dir.exists() and not is_resume:
        shutil.rmtree(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return Workspace(str(workspace_dir))


async def _download_attachments(settings: Settings, claim: dict[str, Any], workspace: Workspace) -> None:
    bucket = claim["artifacts_bucket"]
    for attachment in claim.get("attachments", []):
        target = workspace.resolve(attachment["file_name"])
        target.parent.mkdir(parents=True, exist_ok=True)
        response = settings.s3.get_object(Bucket=bucket, Key=attachment["object_key"])
        target.write_bytes(response["Body"].read())


async def _upload_outputs(settings: Settings, claim: dict[str, Any], job: AgentJob, workspace: Workspace) -> tuple[list[str], list[str]]:
    include_browser_step_screenshots = query_requests_browser_images(job.query)
    output_files: list[str] = []
    artifact_keys: list[str] = []
    for relative_path in workspace.list_files():
        if relative_path == "claim.json" or relative_path.startswith(".pki/"):
            continue
        if not include_browser_step_screenshots and is_browser_step_screenshot(relative_path):
            continue
        file_path = workspace.resolve(relative_path)
        key = artifact_key(job.job_id, "outputs", relative_path)
        settings.s3.put_object(
            Bucket=claim["artifacts_bucket"],
            Key=key,
            Body=file_path.read_bytes(),
            ContentType="application/octet-stream",
        )
        output_files.append(relative_path)
        artifact_keys.append(key)
    return output_files, artifact_keys


@activity.defn(name="execute_heavy_job_activity")
async def execute_heavy_job_activity(claim: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    state = StateStore(settings)
    job = AgentJob.model_validate(claim["job"])
    resume_checkpoint = CheckpointPayload.model_validate(claim["resume_checkpoint"]) if claim.get("resume_checkpoint") else None
    previous_checkpoint = state.get_latest_checkpoint(job.job_id)
    recent_turns = [ThreadTurn.model_validate(turn) for turn in claim.get("recent_turns") or []]
    config = AgentConfig.model_validate(claim.get("config") or {})
    job_context = JobContext.model_validate(claim.get("job_context") or (job.metadata or {}).get("job_context") or {})
    workspace = _prepare_workspace(job.job_id, resume_checkpoint=claim.get("resume_checkpoint"))
    attachment_names = [attachment["file_name"] for attachment in claim.get("attachments", [])]
    execution_progress_matrix = normalize_execution_progress_matrix(
        claim.get("execution_progress_matrix")
        or (job.metadata or {}).get("execution_progress_matrix")
        or claim.get("strategy_state")
        or (job.metadata or {}).get("strategy_state")
        or default_strategy_state()
    )
    strategy_state = normalize_strategy_state(execution_progress_matrix.model_dump(mode="json"))
    state.merge_job_metadata(
        job.job_id,
        {
            "strategy_state": strategy_state,
            "execution_progress_matrix": execution_progress_matrix.model_dump(mode="json"),
            "job_context": job_context.model_dump(mode="json"),
        },
    )
    status_interval = max(10, settings.temporal_activity_heartbeat_seconds)
    current_step = "starting worker"
    current_step_started_at = time.monotonic()
    current_summary = progress_summary_for_step(job.query, current_step=current_step, attachments=bool(attachment_names), summary="worker picked up job")
    task_summary = status_summary_for_query(job.query, attachments=bool(attachment_names))
    interrupted = asyncio.Event()
    main_task = asyncio.current_task()
    strategy_retry_result: dict[str, Any] | None = None
    strategy_exhausted_pause: dict[str, Any] | None = None

    def _persist_runtime_state(*, heartbeat_summary: str = "", findings_summary: str = "") -> None:
        nonlocal execution_progress_matrix, job_context
        if heartbeat_summary:
            execution_progress_matrix.last_operator_visible_summary = heartbeat_summary[:1200]
            job_context.latest_checkpoint_summary = heartbeat_summary[:1200]
        if findings_summary:
            execution_progress_matrix.last_meaningful_artifact = findings_summary[:1200]
            job_context.latest_findings_summary = findings_summary[:1200]
        state.merge_job_metadata(
            job.job_id,
            {
                "strategy_state": strategy_state,
                "execution_progress_matrix": execution_progress_matrix.model_dump(mode="json"),
                "job_context": job_context.model_dump(mode="json"),
            },
        )

    def _signal_handler(*_args) -> None:
        interrupted.set()
        if main_task is not None:
            main_task.cancel()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, _signal_handler)

    async def report_status(*, notify: bool = False) -> None:
        nonlocal strategy_state, strategy_retry_result, strategy_exhausted_pause
        elapsed_seconds = max(0.0, time.monotonic() - current_step_started_at)
        heartbeat_summary = progress_summary_for_step(
            job.query,
            current_step=current_step,
            attachments=bool(attachment_names),
            summary=current_summary,
            elapsed_seconds=elapsed_seconds,
        )
        try:
            updated_job = state.update_job_heartbeat(job.job_id, current_step=current_step, summary=heartbeat_summary)
        except KeyError:
            interrupted.set()
            if main_task is not None:
                main_task.cancel()
            return
        _persist_runtime_state(heartbeat_summary=heartbeat_summary)
        config = state.get_config()
        if state.should_stop_for_stall(job.job_id, interval_seconds=config.status_update_interval_seconds):
            strategy_state = advance_strategy_state(
                strategy_state,
                "stalled without meaningful progress",
                threshold=max(1, int(settings.strategy_consecutive_failure_threshold)),
            )
            execution_progress_matrix = ExecutionProgressMatrix.from_legacy(strategy_state)
            findings_summary = summarize_recoverable_failure(
                job.query,
                "stalled without meaningful progress",
                str(strategy_state.get("failed_strategy") or strategy_state.get("current_strategy") or ""),
            )
            _persist_runtime_state(heartbeat_summary=heartbeat_summary, findings_summary=findings_summary)
            state.save_checkpoint(
                job.job_id,
                CheckpointPayload(
                    summary=f"strategy retry pending: {strategy_state.get('current_strategy', '')}",
                    current_step="strategy_retry_pending",
                    workspace_files=workspace.list_files(),
                    resume_instructions="Continue from the latest workspace state using the next internal strategy automatically.",
                    metadata={
                        "pause_kind": "strategy_retry_pending",
                        "strategy_state": strategy_state,
                        "last_error": "stalled without meaningful progress",
                    },
                ),
            )
            if bool(strategy_state.get("retry_requested")):
                strategy_retry_result = {
                    "kind": "retry_strategy",
                    "error_message": "stalled without meaningful progress",
                    "strategy_state": strategy_state,
                }
            else:
                strategy_exhausted_pause = {
                    "kind": "paused",
                    "question": "I exhausted the available internal strategies for this run.",
                    "details": (
                        "I already rotated through direct tools, Stagehand, and visual browser fallback. "
                        "I only need your input if you want to change the constraints, provider, or destination."
                    ),
                    "checkpoint": CheckpointPayload(
                        summary="strategy exhausted after automatic retries",
                        current_step="waiting_for_user_input",
                        workspace_files=workspace.list_files(),
                        resume_instructions="Use the user's next reply to change the constraints, provider, or venue and continue from the saved workspace state.",
                        metadata={
                            "pause_kind": "strategy_exhausted",
                            "strategy_state": strategy_state,
                            "last_error": "stalled without meaningful progress",
                        },
                    ).model_dump(),
                }
            if updated_job.chat_id and bool(strategy_state.get("retry_requested")):
                await TelegramClient(settings).send_message(
                    updated_job.chat_id,
                    "Still working on it. I hit a dead end and switched to another approach.",
                )
            elif updated_job.chat_id:
                await TelegramClient(settings).send_message(
                    updated_job.chat_id,
                    "I tried the available approaches and need one change from you before I continue.",
                )
            interrupted.set()
            if main_task is not None:
                main_task.cancel()
            return
        if notify and job.chat_id:
            live_job = state.get_job(job.job_id) or job
            message = progress_notification_text(live_job, current_step=current_step, summary=heartbeat_summary)
            if state.should_send_status_update(job.job_id, interval_seconds=config.status_update_interval_seconds, text=message):
                await TelegramClient(settings).send_message(job.chat_id, message)
                state.mark_status_update_sent(job.job_id, text=message)
        activity.heartbeat({"job_id": job.job_id, "current_step": current_step, "summary": heartbeat_summary})

    async def periodic_status() -> None:
        while True:
            await asyncio.sleep(status_interval)
            await report_status(notify=True)

    status_task = asyncio.create_task(periodic_status())
    local_store = LocalBudgetStore()
    try:
        if activity.info().attempt > 1 and previous_checkpoint is not None:
            prior_summary = (previous_checkpoint.summary or "").strip().lower()
            if prior_summary.startswith("interrupted:"):
                return {
                    "kind": "failed",
                    "error_message": "The task was interrupted before it finished, and the worker restarted on a retry attempt.",
                    "interrupted": True,
                    "timed_out": False,
                }
        await report_status()
        await _download_attachments(settings, claim, workspace)
        current_step = "attachments_ready"
        current_step_started_at = time.monotonic()
        current_summary = progress_summary_for_step(job.query, current_step=current_step, attachments=bool(attachment_names), summary="workspace prepared")
        state.save_checkpoint(
            job.job_id,
            CheckpointPayload(summary="attachments downloaded", current_step="attachments_ready", workspace_files=workspace.list_files()),
        )
        await report_status()
        if interrupted.is_set():
            raise asyncio.CancelledError("worker interrupted after attachment download")
        current_step = "running_agent"
        current_step_started_at = time.monotonic()
        current_summary = progress_summary_for_step(job.query, current_step=current_step, attachments=bool(attachment_names), summary=task_summary)
        result = await run_agent(
            job.query,
            settings=settings,
            store=state,
            spend_store=local_store,
            mode="heavy",
            context_summary=str(claim.get("context_summary") or ""),
            recent_turns=recent_turns,
            durable_memories=claim.get("durable_memories") or [],
            workspace=workspace,
            attachment_names=attachment_names,
            config=config,
            resume_checkpoint=resume_checkpoint,
            current_job=job,
            strategy_mode=str(strategy_state.get("current_strategy") or ""),
        )
        current_step = "uploading_outputs"
        current_step_started_at = time.monotonic()
        current_summary = progress_summary_for_step(job.query, current_step=current_step, attachments=bool(attachment_names), summary="preparing final files and upload")
        state.save_checkpoint(
            job.job_id,
            CheckpointPayload(summary="agent completed", current_step="uploading_outputs", workspace_files=workspace.list_files()),
        )
        await report_status()
        if interrupted.is_set():
            raise asyncio.CancelledError("worker interrupted before output upload")
        output_files, artifact_keys = await _upload_outputs(settings, claim, job, workspace)
        return {
            "kind": "completed",
            "result_text": result.text,
            "output_files": output_files,
            "artifact_keys": artifact_keys,
        }
    except PauseForInputRequested as exc:
        return {
            "kind": "paused",
            "question": exc.question,
            "details": exc.details,
            "checkpoint": CheckpointPayload(
                summary=exc.summary,
                current_step=exc.current_step,
                workspace_files=workspace.list_files(),
                resume_instructions=exc.resume_instructions,
                metadata={
                    "input_question": exc.question,
                    "input_details": exc.details,
                    "pause_kind": "user_input",
                },
            ).model_dump(),
        }
    except asyncio.CancelledError as exc:
        if strategy_retry_result is not None:
            if main_task is not None:
                main_task.uncancel()
            return strategy_retry_result
        if strategy_exhausted_pause is not None:
            if main_task is not None:
                main_task.uncancel()
            return strategy_exhausted_pause
        state.save_checkpoint(
            job.job_id,
            CheckpointPayload(summary=f"interrupted: {current_summary}", current_step=current_step, workspace_files=workspace.list_files()),
        )
        return {"kind": "failed", "error_message": str(exc) or "stopped by user", "interrupted": True, "timed_out": False}
    except Exception as exc:
        error_message = str(exc)
        if is_retryable_interaction_failure(error_message):
            retry_threshold = strategy_threshold_for_error(
                str(strategy_state.get("current_strategy") or ""),
                error_message,
                default_threshold=max(1, int(settings.strategy_consecutive_failure_threshold)),
            )
            updated_strategy_state = advance_strategy_state(
                strategy_state,
                error_message,
                threshold=retry_threshold,
            )
            updated_execution_progress_matrix = ExecutionProgressMatrix.from_legacy(updated_strategy_state)
            strategy_state = updated_strategy_state
            execution_progress_matrix = updated_execution_progress_matrix
            findings_summary = extract_partial_findings_block(error_message) or summarize_recoverable_failure(
                job.query,
                error_message,
                str(updated_strategy_state.get("failed_strategy") or updated_strategy_state.get("current_strategy") or ""),
            )
            _persist_runtime_state(heartbeat_summary=current_summary, findings_summary=findings_summary)
            state.save_checkpoint(
                job.job_id,
                CheckpointPayload(
                    summary=f"strategy retry pending: {updated_strategy_state.get('current_strategy', '')}",
                    current_step="strategy_retry_pending",
                    workspace_files=workspace.list_files(),
                    resume_instructions="Continue from the latest workspace state using the next internal strategy automatically.",
                    metadata={
                        "pause_kind": "strategy_retry_pending",
                        "strategy_state": updated_strategy_state,
                    },
                ),
            )
            if bool(updated_strategy_state.get("retry_requested")):
                return {
                    "kind": "retry_strategy",
                    "error_message": error_message,
                    "strategy_state": updated_strategy_state,
                }
            return {
                "kind": "paused",
                "question": "I exhausted the available internal strategies for this run.",
                "details": (
                    "I already rotated through direct tools, Stagehand, and visual browser fallback. "
                    "I only need your input if you want to change the constraints, provider, or destination."
                ),
                "checkpoint": CheckpointPayload(
                    summary="strategy exhausted after automatic retries",
                    current_step="waiting_for_user_input",
                    workspace_files=workspace.list_files(),
                    resume_instructions="Use the user's next reply to change the constraints, provider, or venue and continue from the saved workspace state.",
                    metadata={
                        "pause_kind": "strategy_exhausted",
                        "strategy_state": updated_strategy_state,
                        "last_error": error_message[:1000],
                    },
                ).model_dump(),
            }
        return {"kind": "failed", "error_message": error_message, "interrupted": False, "timed_out": False}
    finally:
        status_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await status_task


async def _run_worker() -> None:
    settings = Settings()
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
        identity=settings.temporal_activity_worker_identity,
    )
    worker = Worker(
        client,
        task_queue=settings.temporal_heavy_activity_task_queue,
        activities=[execute_heavy_job_activity],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(_run_worker())
