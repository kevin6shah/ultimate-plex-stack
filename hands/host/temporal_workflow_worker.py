from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

AGENT_ROOT = Path(__file__).resolve().parents[2] / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from app.settings import Settings
from app.temporal_control_activities import (
    finalize_heavy_job_completed,
    finalize_heavy_job_failed,
    finalize_heavy_job_paused,
    finalize_heavy_job_stop,
    prepare_heavy_job_claim,
    prepare_heavy_job_resume_claim,
    renew_gmail_watch_activity,
)
from app.temporal_workflows import FridayGmailWatchRenewalWorkflow, FridayHeavyJobWorkflow


async def main() -> None:
    settings = Settings()
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
        identity=settings.temporal_worker_identity,
    )
    worker = Worker(
        client,
        task_queue=settings.temporal_workflow_task_queue,
        workflows=[FridayHeavyJobWorkflow, FridayGmailWatchRenewalWorkflow],
        activities=[
            prepare_heavy_job_claim,
            prepare_heavy_job_resume_claim,
            finalize_heavy_job_completed,
            finalize_heavy_job_paused,
            finalize_heavy_job_failed,
            finalize_heavy_job_stop,
            renew_gmail_watch_activity,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
