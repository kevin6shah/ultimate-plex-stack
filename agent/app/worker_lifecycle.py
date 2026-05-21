from __future__ import annotations

import logging
from datetime import datetime, timezone

from .jobs import JobStatus
from .settings import Settings
from .storage import StateStore


logger = logging.getLogger(__name__)


def dedicated_worker_enabled(settings: Settings) -> bool:
    return settings.hands_worker_mode == "dedicated_ec2" and bool(settings.hands_worker_instance_id)


def worker_instance_state(settings: Settings) -> str:
    if not dedicated_worker_enabled(settings):
        return ""
    response = settings.ec2.describe_instances(InstanceIds=[settings.hands_worker_instance_id])
    reservations = response.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        return ""
    return str(reservations[0]["Instances"][0]["State"]["Name"])


def ensure_dedicated_worker_running(settings: Settings) -> None:
    if not dedicated_worker_enabled(settings):
        return
    state = worker_instance_state(settings)
    logger.info("dedicated worker instance state=%s instance_id=%s", state, settings.hands_worker_instance_id)
    if state == "stopped":
        settings.ec2.start_instances(InstanceIds=[settings.hands_worker_instance_id])


def maybe_stop_dedicated_worker_if_idle(settings: Settings, state: StateStore) -> None:
    if not dedicated_worker_enabled(settings):
        return
    if not settings.hands_worker_stop_enabled:
        logger.info("dedicated worker stop disabled; leaving instance running")
        return
    active = state.list_jobs(statuses=(JobStatus.QUEUED, JobStatus.RUNNING), limit=100)
    heavy_active = [job for job in active if job.task_class.value == "heavy"]
    if heavy_active:
        logger.info("dedicated worker remains running; active heavy jobs=%s", len(heavy_active))
        return
    grace_seconds = max(0, settings.hands_worker_idle_grace_seconds)
    if grace_seconds:
        recent_statuses = (
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.INTERRUPTED,
            JobStatus.TIMED_OUT,
            JobStatus.PAUSED_FOR_INPUT,
            JobStatus.WAITING_APPROVAL,
            JobStatus.CHECKPOINTED,
            JobStatus.PAUSED_BUDGET,
        )
        recent_jobs = state.list_jobs(statuses=recent_statuses, limit=20)
        now = datetime.now(timezone.utc)
        for job in recent_jobs:
            if job.task_class.value != "heavy":
                continue
            last_activity = _parse_job_timestamp(job.last_heartbeat_at or job.created_at)
            if last_activity is None:
                continue
            idle_seconds = (now - last_activity).total_seconds()
            if idle_seconds < grace_seconds:
                logger.info(
                    "dedicated worker remains running; last heavy activity was %.1fs ago (grace=%ss)",
                    idle_seconds,
                    grace_seconds,
                )
                return
    if worker_instance_state(settings) == "running":
        logger.info("stopping dedicated worker instance_id=%s after queue drain", settings.hands_worker_instance_id)
        settings.ec2.stop_instances(InstanceIds=[settings.hands_worker_instance_id])


def _parse_job_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
