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
from app.heavy_job_runtime import artifact_key, progress_notification_text, progress_summary_for_step, status_summary_for_query
from app.jobs import AgentConfig, AgentJob, CheckpointPayload, ThreadTurn
from app.settings import Settings
from app.storage import StateStore
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
    workspace = _prepare_workspace(job.job_id, resume_checkpoint=claim.get("resume_checkpoint"))
    attachment_names = [attachment["file_name"] for attachment in claim.get("attachments", [])]
    status_interval = max(10, settings.temporal_activity_heartbeat_seconds)
    current_step = "starting worker"
    current_step_started_at = time.monotonic()
    current_summary = progress_summary_for_step(job.query, current_step=current_step, attachments=bool(attachment_names), summary="worker picked up job")
    task_summary = status_summary_for_query(job.query, attachments=bool(attachment_names))
    interrupted = asyncio.Event()
    main_task = asyncio.current_task()

    def _signal_handler(*_args) -> None:
        interrupted.set()
        if main_task is not None:
            main_task.cancel()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, _signal_handler)

    async def report_status(*, notify: bool = False) -> None:
        elapsed_seconds = max(0.0, time.monotonic() - current_step_started_at)
        heartbeat_summary = progress_summary_for_step(
            job.query,
            current_step=current_step,
            attachments=bool(attachment_names),
            summary=current_summary,
            elapsed_seconds=elapsed_seconds,
        )
        state.update_job_heartbeat(job.job_id, current_step=current_step, summary=heartbeat_summary)
        if notify and job.chat_id:
            config = state.get_config()
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
            store=local_store,
            mode="heavy",
            context_summary=str(claim.get("context_summary") or ""),
            recent_turns=recent_turns,
            durable_memories=claim.get("durable_memories") or [],
            workspace=workspace,
            attachment_names=attachment_names,
            config=config,
            resume_checkpoint=resume_checkpoint,
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
        state.save_checkpoint(
            job.job_id,
            CheckpointPayload(summary=f"interrupted: {current_summary}", current_step=current_step, workspace_files=workspace.list_files()),
        )
        return {"kind": "failed", "error_message": str(exc) or "stopped by user", "interrupted": True, "timed_out": False}
    except Exception as exc:
        return {"kind": "failed", "error_message": str(exc), "interrupted": False, "timed_out": False}
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
