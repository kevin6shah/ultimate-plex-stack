from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from pathlib import Path
from typing import Any

import httpx

from app.agent_core import PauseForInputRequested, run_agent
from app.artifacts import is_browser_step_screenshot, query_requests_browser_images
from app.jobs import ArtifactUploadRequest, CheckpointPayload, ControlCommand, WorkerCheckpointRequest, WorkerClaimResponse, WorkerCompleteRequest, WorkerFailureRequest, WorkerHeartbeat, WorkerPauseRequest
from app.settings import Settings
from app.workspace import Workspace


class LocalBudgetStore:
    def __init__(self) -> None:
        self.last_cost = "0"

    def budget_available(self) -> bool:
        return True

    def add_spend(self, amount) -> None:
        self.last_cost = str(amount)


class WorkerApiClient:
    def __init__(self, base_url: str, worker_key: str):
        self.base_url = base_url.rstrip("/")
        self.worker_key = worker_key

    async def _post(self, path: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base_url}{path}",
                json=payload,
                headers={"x-friday-worker-key": self.worker_key},
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return {}

    async def get_control(self, job_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.base_url}/internal/worker/control/{job_id}",
                headers={"x-friday-worker-key": self.worker_key},
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return {}

    async def heartbeat(self, job_id: str, current_step: str, summary: str = "", *, notify: bool = False) -> None:
        await self._post(
            "/internal/worker/heartbeat",
            WorkerHeartbeat(job_id=job_id, current_step=current_step, summary=summary, notify=notify).model_dump(),
        )

    async def checkpoint(self, job_id: str, summary: str, current_step: str, workspace_files: list[str]) -> None:
        payload = WorkerCheckpointRequest(
            job_id=job_id,
            checkpoint={
                "summary": summary,
                "current_step": current_step,
                "workspace_files": workspace_files,
            },
        )
        await self._post("/internal/worker/checkpoint", payload.model_dump())

    async def pause_for_input(self, job_id: str, *, question: str, details: str, checkpoint: CheckpointPayload) -> None:
        payload = WorkerPauseRequest(
            job_id=job_id,
            question=question,
            details=details,
            checkpoint=checkpoint,
        )
        await self._post("/internal/worker/pause", payload.model_dump())

    async def artifact_url(self, job_id: str, file_name: str, content_type: str) -> dict:
        payload = ArtifactUploadRequest(job_id=job_id, file_name=file_name, content_type=content_type)
        return await self._post("/internal/worker/artifact-url", payload.model_dump())

    async def complete(self, job_id: str, result_text: str, output_files: list[str], artifact_keys: list[str]) -> None:
        payload = WorkerCompleteRequest(job_id=job_id, result_text=result_text, output_files=output_files, artifact_keys=artifact_keys)
        await self._post("/internal/worker/complete", payload.model_dump())

    async def fail(self, job_id: str, error_message: str, *, interrupted: bool = False, timed_out: bool = False) -> None:
        payload = WorkerFailureRequest(job_id=job_id, error_message=error_message, interrupted=interrupted, timed_out=timed_out)
        await self._post("/internal/worker/fail", payload.model_dump())


def _normalize_worker_error_message(query: str, message: str) -> str:
    normalized = (message or "").strip()
    lowered = normalized.lower()
    query_lowered = query.lower()
    if "request_limit of 50" in lowered or "would exceed the request_limit" in lowered:
        if any(token in query_lowered for token in ("flight", "flights", "hotel", "hotels", "rental car", "rental cars", "google flights", "google travel")):
            return (
                "I hit an internal browser/tool step limit before finishing the travel search. "
                "This usually means the task drifted into a browser loop instead of staying on direct travel tools. "
                "The task did not complete."
            )
        if any(token in query_lowered for token in ("restaurant", "reservation", "resy", "opentable", "booking")):
            return (
                "I hit an internal browser/tool step limit before finishing the reservation task. "
                "This usually means the task drifted into a browser loop instead of staying on structured reservation tools. "
                "The task did not complete."
            )
        return (
            "I hit an internal step limit before finishing the task. "
            "The task did not complete."
        )
    return normalized


def _status_summary_for_query(query: str, *, attachments: bool) -> str:
    lowered = query.lower()
    if any(token in lowered for token in ("flight", "flights", "airline", "airport")):
        return "checking live flight options and comparing fares"
    if any(token in lowered for token in ("hotel", "hotels", "stay", "airbnb", "accommodation")):
        return "comparing hotel options, locations, and prices"
    if any(token in lowered for token in ("rental car", "car rental", "rent a car", "avis", "hertz", "enterprise")):
        return "checking rental car availability and comparing prices"
    if any(token in lowered for token in ("restaurant", "reservation", "resy", "opentable", "book me", "dinner")):
        return "checking reservation sources and matching real venues"
    if any(token in lowered for token in ("research", "compare", "review", "reddit", "google")):
        return "researching sources and comparing findings"
    if any(token in lowered for token in ("browser", "website", "site", "search")):
        return "working through website steps"
    if any(token in lowered for token in ("pdf", "report", "summary", "table")):
        return "preparing a report and output files"
    if attachments:
        return "working through the uploaded files"
    return "working through the task"


async def download_attachments(claim: WorkerClaimResponse, workspace: Workspace) -> None:
    async with httpx.AsyncClient(timeout=120) as client:
        for attachment in claim.attachments:
            target = workspace.resolve(attachment["file_name"])
            target.parent.mkdir(parents=True, exist_ok=True)
            response = await client.get(attachment["download_url"])
            response.raise_for_status()
            target.write_bytes(response.content)


async def upload_outputs(
    api: WorkerApiClient,
    job_id: str,
    workspace: Workspace,
    *,
    include_browser_step_screenshots: bool,
) -> tuple[list[str], list[str]]:
    output_files: list[str] = []
    artifact_keys: list[str] = []
    async with httpx.AsyncClient(timeout=120) as client:
        for relative_path in workspace.list_files():
            if relative_path == "claim.json" or relative_path.startswith(".pki/"):
                continue
            if not include_browser_step_screenshots and is_browser_step_screenshot(relative_path):
                continue
            file_path = workspace.resolve(relative_path)
            response = await api.artifact_url(job_id, relative_path, "application/octet-stream")
            put = await client.put(response["upload_url"], content=file_path.read_bytes(), headers={"content-type": "application/octet-stream"})
            put.raise_for_status()
            output_files.append(relative_path)
            artifact_keys.append(response["object_key"])
    return output_files, artifact_keys


async def main() -> None:
    claim_path = Path(os.environ["FRIDAY_CLAIM_PATH"])
    worker_key = os.environ["FRIDAY_WORKER_KEY"]
    base_url = os.environ["FRIDAY_API_BASE_URL"]
    api = WorkerApiClient(base_url, worker_key)
    claim = WorkerClaimResponse.model_validate_json(claim_path.read_text(encoding="utf-8"))
    if claim.job is None:
        raise RuntimeError("claim does not include a job")

    workspace = Workspace("/workspace")
    local_store = LocalBudgetStore()
    settings = Settings()
    attachment_names = [attachment["file_name"] for attachment in claim.attachments]
    include_browser_step_screenshots = query_requests_browser_images(claim.job.query)
    status_interval = max(60, int(claim.config.status_update_interval_seconds or 300))
    current_step = "starting worker"
    current_summary = "worker picked up job"
    task_summary = _status_summary_for_query(claim.job.query, attachments=bool(attachment_names))
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

    async def periodic_status() -> None:
        while True:
            await asyncio.sleep(status_interval)
            await api.heartbeat(claim.job.job_id, current_step, current_summary, notify=True)

    status_task = asyncio.create_task(periodic_status())

    async def poll_control() -> None:
        while True:
            await asyncio.sleep(10)
            payload = await api.get_control(claim.job.job_id)
            control = payload.get("control")
            if not control:
                continue
            if control.get("command") == ControlCommand.STOP.value:
                interrupted.set()
                if main_task is not None:
                    main_task.cancel()
                return

    control_task = asyncio.create_task(poll_control())

    try:
        await api.heartbeat(claim.job.job_id, current_step, current_summary)
        await download_attachments(claim, workspace)
        current_step = "attachments_ready"
        current_summary = "workspace prepared"
        await api.checkpoint(claim.job.job_id, "attachments downloaded", "attachments_ready", workspace.list_files())
        if interrupted.is_set():
            raise KeyboardInterrupt("worker interrupted after attachment download")
        current_step = "running_agent"
        current_summary = task_summary
        result = await run_agent(
            claim.job.query,
            settings=settings,
            store=local_store,
            mode="heavy",
            context_summary=claim.context_summary,
            recent_turns=claim.recent_turns,
            durable_memories=claim.durable_memories,
            workspace=workspace,
            attachment_names=attachment_names,
            config=claim.config,
            resume_checkpoint=claim.resume_checkpoint,
        )
        current_step = "uploading_outputs"
        current_summary = "preparing final files and upload"
        await api.checkpoint(claim.job.job_id, "agent completed", "uploading_outputs", workspace.list_files())
        if interrupted.is_set():
            raise KeyboardInterrupt("worker interrupted before output upload")
        output_files, artifact_keys = await upload_outputs(
            api,
            claim.job.job_id,
            workspace,
            include_browser_step_screenshots=include_browser_step_screenshots,
        )
        await api.complete(claim.job.job_id, result.text, output_files, artifact_keys)
    except PauseForInputRequested as exc:
        current_step = exc.current_step
        current_summary = exc.summary
        await api.pause_for_input(
            claim.job.job_id,
            question=exc.question,
            details=exc.details,
            checkpoint=CheckpointPayload(
                summary=exc.summary,
                current_step=exc.current_step,
                workspace_files=workspace.list_files(),
                resume_instructions=exc.resume_instructions,
                metadata={
                    "input_question": exc.question,
                    "input_details": exc.details,
                    "pause_kind": "user_input",
                },
            ),
        )
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        await api.checkpoint(
            claim.job.job_id,
            f"interrupted: {current_summary}",
            current_step,
            workspace.list_files(),
        )
        await api.fail(claim.job.job_id, str(exc), interrupted=True)
    except Exception as exc:
        await api.fail(claim.job.job_id, _normalize_worker_error_message(claim.job.query, str(exc)))
        raise
    finally:
        status_task.cancel()
        control_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await status_task
        with contextlib.suppress(asyncio.CancelledError):
            await control_task


if __name__ == "__main__":
    asyncio.run(main())
