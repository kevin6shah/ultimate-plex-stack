from __future__ import annotations

import re

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .heavy_job_runtime import (
    build_contextual_heavy_followup_query,
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
from .strategy_runtime import default_strategy_state, normalize_strategy_state
from .worker_lifecycle import ensure_dedicated_worker_running, maybe_stop_dedicated_worker_if_idle


def _store() -> StateStore:
    return StateStore(settings)


def _missing_job(job_id: str) -> ApplicationError:
    return ApplicationError(f"job not found: {job_id}", non_retryable=True)


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


def _stop_reason_from_control_signal(state: StateStore, job_id: str) -> tuple[str, str]:
    signal = state.get_latest_control_signal(job_id)
    note = (signal.note if signal else "").strip()
    lowered = note.lower()
    if "stopped by user" in lowered:
        return ("stopped by user", "stopped by user")
    if "auto-stopped" in lowered or "no meaningful progress" in lowered or "stuck" in lowered:
        return (
            "auto-stopped after repeated identical steps",
            "I stopped this run because it appeared stuck on the same step without meaningful progress. I kept the latest checkpoint.",
        )
    return (
        "interrupted",
        "I hit an interruption before that task finished. I kept the latest checkpoint.",
    )


def _result_looks_like_booking_clarification(query: str, result_text: str) -> bool:
    normalized = clean_user_facing_result(result_text or "")
    if not normalized:
        return False
    lowered_query = (query or "").lower()
    if not any(token in lowered_query for token in ("book", "reservation", "table", "restaurant", "resy", "opentable")):
        return False
    lowered = normalized.lower()
    triggers = (
        "which would you prefer",
        "which do you prefer",
        "which would you like",
        "which seating preference",
        "let me confirm with you first",
        "would you like me to",
    )
    if not any(trigger in lowered for trigger in triggers):
        return False
    return bool(re.search(r"^\s*(?:[-*•]|\d+\.)", normalized, flags=re.MULTILINE)) or normalized.endswith("?")


def _booking_clarification_prompt_from_result(result_text: str) -> tuple[str, str]:
    normalized = clean_user_facing_result(result_text or "")
    if not normalized:
        return "", ""
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    question = ""
    for line in lines:
        lowered = line.lower()
        if "would you like me to" in lowered or "which would you" in lowered or "which do you" in lowered:
            question = line
            break
    for line in reversed(lines):
        if question:
            break
        if line.endswith("?"):
            question = line
            break
    detail_lines = [line for line in lines if re.match(r"^\s*(?:[-*•]|\d+\.)", line)]
    if not question:
        question = "I need your choice between the available booking options before I can continue."
    return question[:500], "\n".join(detail_lines[:12])[:1500]


@activity.defn
async def prepare_heavy_job_claim(job_id: str) -> dict:
    state = _store()
    job = state.get_job(job_id)
    if job is None:
        raise _missing_job(job_id)
    ensure_dedicated_worker_running(settings)
    state.update_job_status(job_id, status=JobStatus.RUNNING, current_step="starting worker")
    return build_heavy_claim(state, settings, job)


@activity.defn
async def prepare_heavy_job_resume_claim(job_id: str, reply_text: str) -> dict:
    state = _store()
    job = state.get_job(job_id)
    if job is None:
        raise _missing_job(job_id)
    checkpoint = state.get_latest_checkpoint(job_id)
    resumed_query = build_paused_input_resume_query(job, checkpoint, reply_text)
    state.merge_job_metadata(job_id, {"strategy_state": default_strategy_state(), "last_strategy_error": None})
    state.update_job_status(job_id, status=JobStatus.RUNNING, current_step="resuming task")
    refreshed_job = state.get_job(job_id) or job
    return build_heavy_claim(state, settings, refreshed_job, query_override=resumed_query)


@activity.defn
async def prepare_heavy_job_followup_claim(job_id: str, followup_text: str) -> dict:
    state = _store()
    job = state.get_job(job_id)
    if job is None:
        raise _missing_job(job_id)
    checkpoint = state.get_latest_checkpoint(job_id)
    followup_query = build_contextual_heavy_followup_query(job, checkpoint, followup_text)
    state.merge_job_metadata(job_id, {"strategy_state": default_strategy_state(), "last_strategy_error": None})
    state.update_job_status(job_id, status=JobStatus.RUNNING, current_step="updating task")
    refreshed_job = state.get_job(job_id) or job
    return build_heavy_claim(state, settings, refreshed_job, query_override=followup_query)


@activity.defn
async def prepare_heavy_job_strategy_retry_claim(job_id: str, strategy_state: dict, error_message: str = "") -> dict:
    state = _store()
    job = state.get_job(job_id)
    if job is None:
        raise _missing_job(job_id)
    normalized_strategy_state = normalize_strategy_state(strategy_state)
    state.merge_job_metadata(
        job_id,
        {
            "strategy_state": normalized_strategy_state,
            "last_strategy_error": str(error_message or "")[:1000],
        },
    )
    state.update_job_status(job_id, status=JobStatus.RUNNING, current_step="switching strategy")
    refreshed_job = state.get_job(job_id) or job
    return build_heavy_claim(state, settings, refreshed_job)


@activity.defn
async def finalize_heavy_job_completed(job_id: str, result_text: str, output_files: list[str], artifact_keys: list[str]) -> None:
    state = _store()
    job = state.get_job(job_id)
    if job is None:
        return
    cleaned_result = clean_user_facing_result(result_text)
    if _result_looks_like_booking_clarification(job.query, cleaned_result):
        input_question, input_details = _booking_clarification_prompt_from_result(cleaned_result)
        payload = CheckpointPayload(
            summary="waiting for your choice between the available booking options",
            current_step="waiting_for_user_input",
            resume_instructions="Use the user's selected booking option to continue the same reservation flow without asking again for the same choice.",
            metadata={
                "input_question": input_question,
                "input_details": input_details,
            },
        )
        state.save_checkpoint(job_id, payload)
        state.update_job_status(
            job_id,
            status=JobStatus.PAUSED_FOR_INPUT,
            current_step="waiting_for_user_input",
            result_preview=cleaned_result,
            output_files=output_files,
            artifact_keys=artifact_keys,
        )
        paused_job = state.get_job(job_id) or job
        message = paused_input_reply_text(state, paused_job)
        record_job_assistant_turn(state, job, message)
        if job.chat_id:
            from .telegram import TelegramClient

            await TelegramClient(settings).send_message(job.chat_id, message)
        maybe_stop_dedicated_worker_if_idle(settings, state)
        return
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
        return
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
        return
    status = JobStatus.FAILED
    if interrupted or _checkpoint_indicates_interruption(state, job_id) or _message_indicates_interruption(error_message):
        status = JobStatus.INTERRUPTED
    elif timed_out:
        status = JobStatus.TIMED_OUT
    if status == JobStatus.INTERRUPTED:
        current_step, user_error = _stop_reason_from_control_signal(state, job_id)
    else:
        user_error = humanize_worker_failure(job.query, error_message, status)
        current_step = "failed"
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
    current_step, error_message = _stop_reason_from_control_signal(state, job_id)
    state.update_job_status(job_id, status=JobStatus.INTERRUPTED, current_step=current_step, error_message=error_message)
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
