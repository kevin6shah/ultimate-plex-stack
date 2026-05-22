from __future__ import annotations

from temporalio import activity

from .heavy_job_runtime import (
    build_heavy_claim,
    build_paused_input_resume_query,
    clean_user_facing_result,
    humanize_worker_failure,
    paused_input_reply_text,
    record_job_assistant_turn,
    send_final_job_message,
)
from .gmail_oauth import renew_gmail_watch
from .jobs import CheckpointPayload, JobStatus, MailboxWatchState, utc_now_iso
from .settings import Settings, settings
from .storage import StateStore
from .worker_lifecycle import ensure_dedicated_worker_running, maybe_stop_dedicated_worker_if_idle


def _store() -> StateStore:
    return StateStore(settings)


def _checkpoint_indicates_interruption(state: StateStore, job_id: str) -> bool:
    checkpoint = state.get_latest_checkpoint(job_id)
    if checkpoint is None:
        return False
    return (checkpoint.summary or "").strip().lower().startswith("interrupted:")


def _message_indicates_interruption(message: str) -> bool:
    normalized = (message or "").strip().lower()
    if not normalized:
        return False
    return normalized in {
        "stopped by user",
        "activity cancelled",
        "activity canceled",
    } or "cancelled" in normalized or "canceled" in normalized


@activity.defn
async def prepare_heavy_job_claim(job_id: str) -> dict:
    state = _store()
    job = state.get_job(job_id)
    if job is None:
        raise RuntimeError(f"job not found: {job_id}")
    ensure_dedicated_worker_running(settings)
    state.update_job_status(job_id, status=JobStatus.RUNNING, current_step="starting worker")
    return build_heavy_claim(state, settings, job)


@activity.defn
async def prepare_heavy_job_resume_claim(job_id: str, reply_text: str) -> dict:
    state = _store()
    job = state.get_job(job_id)
    if job is None:
        raise RuntimeError(f"job not found: {job_id}")
    checkpoint = state.get_latest_checkpoint(job_id)
    resumed_query = build_paused_input_resume_query(job, checkpoint, reply_text)
    state.update_job_status(job_id, status=JobStatus.RUNNING, current_step="resuming task")
    return build_heavy_claim(state, settings, job, query_override=resumed_query)


@activity.defn
async def finalize_heavy_job_completed(job_id: str, result_text: str, output_files: list[str], artifact_keys: list[str]) -> None:
    state = _store()
    job = state.get_job(job_id)
    if job is None:
        raise RuntimeError(f"job not found: {job_id}")
    cleaned_result = clean_user_facing_result(result_text)
    state.update_job_status(
        job_id,
        status=JobStatus.COMPLETED,
        current_step="completed",
        result_preview=cleaned_result,
        output_files=output_files,
        artifact_keys=artifact_keys,
    )
    record_job_assistant_turn(state, job, cleaned_result)
    await send_final_job_message(settings, state, state.get_job(job_id) or job, cleaned_result=cleaned_result, output_files=output_files, artifact_keys=artifact_keys)
    maybe_stop_dedicated_worker_if_idle(settings, state)


@activity.defn
async def finalize_heavy_job_paused(job_id: str, question: str, details: str, checkpoint: dict) -> None:
    state = _store()
    job = state.get_job(job_id)
    if job is None:
        raise RuntimeError(f"job not found: {job_id}")
    payload = CheckpointPayload.model_validate(checkpoint)
    state.save_checkpoint(job_id, payload)
    state.update_job_status(job_id, status=JobStatus.PAUSED_FOR_INPUT, current_step=payload.current_step or "waiting_for_user_input")
    message = paused_input_reply_text(state, state.get_job(job_id) or job)
    record_job_assistant_turn(state, job, message)
    if job.chat_id:
        from .telegram import TelegramClient

        await TelegramClient(settings).send_message(job.chat_id, message)
    maybe_stop_dedicated_worker_if_idle(settings, state)


@activity.defn
async def finalize_heavy_job_failed(
    job_id: str,
    error_message: str,
    interrupted: bool = False,
    timed_out: bool = False,
) -> None:
    state = _store()
    job = state.get_job(job_id)
    if job is None:
        raise RuntimeError(f"job not found: {job_id}")
    status = JobStatus.FAILED
    if interrupted or _checkpoint_indicates_interruption(state, job_id) or _message_indicates_interruption(error_message):
        status = JobStatus.INTERRUPTED
    elif timed_out:
        status = JobStatus.TIMED_OUT
    user_error = humanize_worker_failure(job.query, error_message, status)
    current_step = "stopped by user" if status == JobStatus.INTERRUPTED else "failed"
    state.update_job_status(job_id, status=status, current_step=current_step, error_message=user_error)
    record_job_assistant_turn(state, job, user_error)
    if job.chat_id:
        from .telegram import TelegramClient

        await TelegramClient(settings).send_message(job.chat_id, user_error)
    maybe_stop_dedicated_worker_if_idle(settings, state)


@activity.defn
async def finalize_heavy_job_stop(job_id: str) -> None:
    state = _store()
    job = state.get_job(job_id)
    if job is None:
        return
    state.update_job_status(job_id, status=JobStatus.INTERRUPTED, current_step="stopped by user", error_message="stopped by user")
    maybe_stop_dedicated_worker_if_idle(settings, state)


@activity.defn
async def renew_gmail_watch_activity() -> dict:
    state = _store()
    mailbox_email = settings.secret(settings.gmail_account_email_param).strip()
    watch_state = state.get_mailbox_watch_state(mailbox_email) if mailbox_email else None
    now = utc_now_iso()
    try:
        payload = await renew_gmail_watch(settings)
        state.put_mailbox_watch_state(
            (watch_state or MailboxWatchState(mailbox_email=mailbox_email)).model_copy(
                update={
                    "mailbox_email": mailbox_email,
                    "history_id": str(payload.get("historyId", "")),
                    "expiration": str(payload.get("expiration", "")),
                    "topic_name": settings.gmail_pubsub_topic_name,
                    "watch_status": "active",
                    "last_watch_renewed_at": now,
                    "last_oauth_tested_at": now,
                    "last_oauth_error": "",
                    "updated_at": now,
                }
            )
        )
        return {"ok": True, "history_id": str(payload.get("historyId", "")), "expiration": str(payload.get("expiration", ""))}
    except Exception as exc:
        state.put_mailbox_watch_state(
            (watch_state or MailboxWatchState(mailbox_email=mailbox_email)).model_copy(
                update={
                    "mailbox_email": mailbox_email,
                    "topic_name": settings.gmail_pubsub_topic_name,
                    "watch_status": "oauth_error",
                    "last_oauth_tested_at": now,
                    "last_oauth_error": str(exc)[:500],
                    "updated_at": now,
                }
            )
        )
        raise
