from __future__ import annotations

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import TemporalError, WorkflowAlreadyStartedError

from .jobs import AgentJob, JobStatus
from .settings import Settings
from .storage import StateStore
from .temporal_runtime import get_temporal_client, heavy_workflow_id
from .temporal_workflows import FridayGmailWatchRenewalWorkflow, FridayHeavyJobWorkflow
from .worker_lifecycle import ensure_dedicated_worker_running


async def start_heavy_job_workflow(settings: Settings, state: StateStore, job: AgentJob) -> str:
    client = await get_temporal_client(settings)
    workflow_id = heavy_workflow_id(settings, job.job_id)
    state.update_job_status(job.job_id, status=JobStatus.WAITING_WORKER, current_step="waiting_worker")
    ensure_dedicated_worker_running(settings)
    try:
        await client.start_workflow(
            FridayHeavyJobWorkflow.run,
            {
                "job_id": job.job_id,
                "heavy_task_queue": settings.temporal_heavy_activity_task_queue,
                "heavy_activity_max_attempts": max(1, settings.temporal_activity_max_attempts),
            },
            id=workflow_id,
            task_queue=settings.temporal_workflow_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except WorkflowAlreadyStartedError:
        pass
    return workflow_id


async def signal_stop_heavy_job(settings: Settings, job_id: str, note: str = "") -> bool:
    client = await get_temporal_client(settings)
    try:
        handle = client.get_workflow_handle(heavy_workflow_id(settings, job_id))
        await handle.signal(FridayHeavyJobWorkflow.stop, note)
        return True
    except TemporalError:
        return False


async def signal_answer_heavy_job(settings: Settings, job_id: str, text: str) -> bool:
    client = await get_temporal_client(settings)
    try:
        handle = client.get_workflow_handle(heavy_workflow_id(settings, job_id))
        await handle.signal(FridayHeavyJobWorkflow.answer, text)
        return True
    except TemporalError:
        return False


async def signal_submit_verification_code(settings: Settings, job_id: str, code: str) -> bool:
    client = await get_temporal_client(settings)
    try:
        handle = client.get_workflow_handle(heavy_workflow_id(settings, job_id))
        await handle.signal(FridayHeavyJobWorkflow.submit_verification_code, code)
        return True
    except TemporalError:
        return False


def gmail_watch_workflow_id(settings: Settings) -> str:
    prefix = settings.temporal_workflow_id_prefix or "friday"
    return f"{prefix}-gmail-watch-renewal"


async def ensure_gmail_watch_renewal_workflow(settings: Settings) -> str:
    client = await get_temporal_client(settings)
    workflow_id = gmail_watch_workflow_id(settings)
    try:
        await client.start_workflow(
            FridayGmailWatchRenewalWorkflow.run,
            {"renewal_days": max(1, settings.gmail_watch_renewal_days)},
            id=workflow_id,
            task_queue=settings.temporal_workflow_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        pass
    return workflow_id
