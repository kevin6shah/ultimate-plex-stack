from __future__ import annotations

import asyncio
import io
import json
import hashlib
import hmac
import logging
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Optional
from urllib.parse import parse_qs, quote
from uuid import uuid4

import boto3
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from mangum import Mangum
from pydantic_ai import Agent
from pydantic import BaseModel
from starlette.responses import HTMLResponse, RedirectResponse

from .agent_core import run_agent
from .artifacts import (
    is_browser_step_screenshot,
    query_requests_browser_images,
    query_requests_output_files,
    visible_output_files,
)
from .jobs import (
    AgentConfig,
    AgentJob,
    AutomationPolicyRecord,
    ArtifactUploadRequest,
    AttachmentRef,
    BrowserSessionRecord,
    CheckpointPayload,
    ControlCommand,
    DashboardSessionRecord,
    IdentityRecord,
    IdentitySecretPointer,
    JobSource,
    JobStatus,
    MailboxWatchState,
    PaymentProfileRecord,
    SaveConfigRequest,
    SecretKind,
    TaskClass,
    ThreadTurnRole,
    WorkerCheckpointRequest,
    WorkerClaimResponse,
    WorkerCompleteRequest,
    WorkerFailureRequest,
    WorkerHeartbeat,
    WorkerPauseRequest,
)
from .gmail_oauth import (
    decode_pubsub_push_body,
    extract_otp_codes,
    first_matching_code,
    gmail_api_get,
    gmail_message_headers,
    gmail_message_text,
    list_history_message_ids,
    mint_gmail_access_token,
    renew_gmail_watch,
)
from .routing import classify_task, is_long_task, query_domains_compatible, task_routing_profile
from .settings import settings
from .storage import StateStore
from .temporal_runtime import temporal_backend_enabled
from .telegram import TelegramClient, parse_telegram_update
from .worker_lifecycle import ensure_dedicated_worker_running as shared_ensure_dedicated_worker_running
from .worker_lifecycle import maybe_stop_dedicated_worker_if_idle as shared_maybe_stop_dedicated_worker_if_idle
from .heavy_job_runtime import (
    has_useful_partial_findings as _shared_has_useful_partial_findings,
    interrupted_reply_text as _shared_interrupted_reply_text,
    partial_findings_text as _shared_partial_findings_text,
    progress_snapshot_text as _shared_progress_snapshot_text,
    progress_notification_text,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RUNNING_JOB_STALE_AFTER = timedelta(minutes=10)
WAITING_JOB_STALE_AFTER = timedelta(minutes=15)
ATTACHMENTS_READY_STALE_AFTER = timedelta(minutes=3)
DASHBOARD_SESSION_COOKIE = "friday_dashboard_session"


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


class FollowupDecision(BaseModel):
    is_followup: bool
    confidence: str = ""
    reason: str = ""


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


@app.on_event("startup")
async def startup_housekeeping() -> None:
    if not temporal_backend_enabled(settings):
        return
    if not settings.gmail_pubsub_topic_name.strip():
        return
    try:
        from .temporal_client import ensure_gmail_watch_renewal_workflow

        await ensure_gmail_watch_renewal_workflow(settings)
    except Exception as exc:
        logger.warning("gmail watch renewal workflow ensure failed: %s", exc)


def store() -> StateStore:
    return StateStore(settings)


def queue_client():
    return boto3.client("sqs", region_name=settings.aws_region)


def s3_client():
    return settings.s3


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    shared_ensure_dedicated_worker_running(settings)


def _maybe_stop_dedicated_worker_if_idle(state: StateStore) -> None:
    shared_maybe_stop_dedicated_worker_if_idle(settings, state)


async def _start_heavy_job(state: StateStore, job: AgentJob) -> None:
    state.create_job(job)
    if job.user_id and job.conversation_id:
        state.set_active_heavy_job(
            channel=job.source.value,
            user_id=job.user_id,
            conversation_id=job.conversation_id,
            job_id=job.job_id,
        )
        _sync_siri_heavy_job_to_telegram_thread(state, job_id=job.job_id, job=job)
    if temporal_backend_enabled(settings):
        from .temporal_client import start_heavy_job_workflow

        await start_heavy_job_workflow(settings, state, job)
        return
    _ensure_dedicated_worker_running()


def _normalize_query(text: str) -> str:
    return text.strip()


def _dashboard_session_signature(session_id: str) -> str:
    signing_secret = _resolved_optional_secret(settings.dashboard_session_secret_param)
    if not signing_secret:
        raise HTTPException(status_code=500, detail="dashboard session secret not configured")
    return hmac.new(signing_secret.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _encode_dashboard_session_cookie(session_id: str) -> str:
    return f"{session_id}.{_dashboard_session_signature(session_id)}"


def _decode_dashboard_session_cookie(raw_cookie: str) -> Optional[str]:
    if not raw_cookie or "." not in raw_cookie:
        return None
    session_id, supplied_signature = raw_cookie.split(".", 1)
    expected_signature = _dashboard_session_signature(session_id)
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None
    return session_id


def _telegram_login_is_valid(payload: dict[str, str]) -> bool:
    supplied_hash = (payload.get("hash") or "").strip()
    if not supplied_hash:
        return False
    bot_token = _resolved_optional_secret(settings.telegram_bot_token_param)
    if not bot_token:
        return False
    data_check = "\n".join(
        f"{key}={value}"
        for key, value in sorted((key, value) for key, value in payload.items() if key != "hash" and value is not None)
    )
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected_hash = hmac.new(secret_key, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_hash, expected_hash):
        return False
    try:
        auth_date = int(payload.get("auth_date") or "0")
    except ValueError:
        return False
    age_seconds = int((_utc_now() - datetime.fromtimestamp(auth_date, tz=timezone.utc)).total_seconds())
    return 0 <= age_seconds <= max(300, settings.dashboard_session_ttl_seconds)


def _dashboard_login_widget_html() -> str:
    bot_username = settings.telegram_bot_username.strip()
    if not bot_username:
        return "<p>Telegram dashboard login is not configured yet.</p>"
    return (
        '<script async src="https://telegram.org/js/telegram-widget.js?22" '
        f'data-telegram-login="{escape(bot_username)}" '
        'data-size="large" '
        'data-userpic="false" '
        'data-auth-url="/dashboard/auth/telegram" '
        'data-request-access="write"></script>'
    )


def _dashboard_shell(*, title: str, active_view: str, body_html: str, session_name: str = "") -> str:
    def nav_link(view: str, label: str) -> str:
        current = " class='active'" if view == active_view else ""
        return f"<a{current} href='/dashboard/{escape(view)}'>{escape(label)}</a>"

    nav = " ".join(
        [
            nav_link("jobs", "Jobs"),
            nav_link("mailbox", "Mailbox"),
            nav_link("identities", "Identities"),
            nav_link("policies", "Policies"),
            nav_link("sessions", "Sessions"),
            nav_link("payments", "Payments"),
        ]
    )
    signed_in = f"<div class='whoami'>Signed in as {escape(session_name)}</div>" if session_name else ""
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{escape(title)}</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f6f4ef;
        --ink: #1e1b17;
        --muted: #6e655a;
        --panel: #fffdfa;
        --line: #d8d0c3;
        --accent: #166534;
        --accent-2: #f59e0b;
      }}
      body {{ margin: 0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, sans-serif; background: linear-gradient(180deg, #f4efe5, #fbfaf7); color: var(--ink); }}
      .page {{ max-width: 980px; margin: 0 auto; padding: 24px 16px 48px; }}
      .mast {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 18px; }}
      .mast h1 {{ margin: 0; font-size: 1.5rem; }}
      .whoami {{ color: var(--muted); font-size: 0.95rem; }}
      nav {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }}
      nav a {{ text-decoration: none; padding: 8px 12px; border-radius: 999px; border: 1px solid var(--line); color: var(--ink); background: rgba(255,255,255,0.7); }}
      nav a.active {{ background: var(--ink); color: white; border-color: var(--ink); }}
      .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 18px; box-shadow: 0 10px 30px rgba(0,0,0,0.05); }}
      .panel + .panel {{ margin-top: 14px; }}
      table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
      th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid #eee5d8; vertical-align: top; }}
      th {{ color: var(--muted); font-weight: 600; font-size: 0.9rem; }}
      .kpi {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin-top: 12px; }}
      .kpi div {{ border: 1px solid var(--line); border-radius: 14px; padding: 12px; background: #fff; }}
      .muted {{ color: var(--muted); }}
      .pill {{ display: inline-block; padding: 3px 8px; border-radius: 999px; background: #eef4eb; color: var(--accent); font-size: 0.82rem; font-weight: 600; }}
      .warn {{ color: #9a3412; }}
      form.inline {{ display: inline; }}
      form.stack, .stack {{ display: grid; gap: 10px; }}
      .grid {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }}
      label {{ display: grid; gap: 6px; font-size: 0.92rem; color: var(--muted); }}
      input, select, textarea {{ width: 100%; box-sizing: border-box; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; background: white; color: var(--ink); }}
      textarea {{ min-height: 88px; resize: vertical; }}
      details summary {{ cursor: pointer; color: var(--accent); }}
      button {{ cursor: pointer; border: none; border-radius: 10px; padding: 10px 14px; background: var(--ink); color: white; }}
      button.secondary {{ background: var(--accent-2); color: #241b07; }}
      pre {{ white-space: pre-wrap; word-break: break-word; background: #faf6ee; border-radius: 12px; padding: 12px; border: 1px solid var(--line); }}
    </style>
  </head>
  <body>
    <main class="page">
      <div class="mast">
        <div>
          <h1>{escape(title)}</h1>
          <div class="muted">Friday operator board for booking, mailbox, and workflow control.</div>
        </div>
        {signed_in}
      </div>
      <nav>{nav}</nav>
      {body_html}
    </main>
  </body>
</html>"""


def _html_table(headers: list[str], rows: list[list[str]]) -> str:
    head_html = "".join(f"<th>{escape(header)}</th>" for header in headers)
    if not rows:
        return "<p class='muted'>Nothing recorded yet.</p>"
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    return f"<table><thead><tr>{head_html}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _dashboard_record_rows(records: list[dict[str, Any]], *, fields: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for record in records:
        row: list[str] = []
        for field in fields:
            value = record.get(field, "")
            rendered = escape(str(value)) if value not in (None, "") else "<span class='muted'>-</span>"
            row.append(rendered)
        rows.append(row)
    return rows


def _require_dashboard_session(request: Request):
    raw_cookie = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    session_id = _decode_dashboard_session_cookie(raw_cookie)
    if not session_id:
        raise HTTPException(status_code=401, detail="dashboard session required")
    record = store().get_dashboard_session(session_id)
    if record is None:
        raise HTTPException(status_code=401, detail="dashboard session expired")
    return record


def _resolved_optional_secret(parameter_name: str) -> str:
    if not parameter_name:
        return ""
    value = settings.secret(parameter_name).strip()
    if not value:
        return ""
    if not parameter_name.startswith("/") and value == parameter_name and re.fullmatch(r"[A-Z0-9_]+", parameter_name):
        return ""
    return value


def _mailbox_watch_state(state: StateStore) -> MailboxWatchState:
    mailbox_email = _resolved_optional_secret(settings.gmail_account_email_param)
    if not mailbox_email:
        return MailboxWatchState(mailbox_email="", watch_status="missing_mailbox_email")
    return state.get_mailbox_watch_state(mailbox_email) or MailboxWatchState(mailbox_email=mailbox_email)


def _dashboard_form_str(form_data: Any, field: str, *, limit: int = 1000) -> str:
    return str(form_data.get(field, "") or "").strip()[:limit]


def _dashboard_form_bool(form_data: Any, field: str) -> bool:
    return str(form_data.get(field, "") or "").strip().lower() in {"1", "true", "on", "yes"}


def _dashboard_form_int(form_data: Any, field: str) -> int:
    raw = _dashboard_form_str(form_data, field, limit=50)
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _find_identity_record(state: StateStore, identity_id: str) -> Optional[IdentityRecord]:
    for record in state.list_identities(limit=200):
        if record.identity_id == identity_id:
            return record
    return None


def _find_policy_record(state: StateStore, policy_id: str) -> Optional[AutomationPolicyRecord]:
    for record in state.list_automation_policies(limit=200):
        if record.policy_id == policy_id:
            return record
    return None


def _find_payment_profile(state: StateStore, payment_profile_id: str) -> Optional[PaymentProfileRecord]:
    for record in state.list_payment_profiles(limit=200):
        if record.payment_profile_id == payment_profile_id:
            return record
    return None


def _find_browser_session(state: StateStore, session_id: str) -> Optional[BrowserSessionRecord]:
    for record in state.list_browser_sessions(limit=200):
        if record.session_id == session_id:
            return record
    return None


async def _dashboard_request_data(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {
        key: values[-1] if values else ""
        for key, values in parsed.items()
    }


def _mailbox_wait_matches(wait, *, sender: str, subject: str, text: str, message_internal_date: datetime) -> Optional[str]:
    if wait.status != "waiting":
        return None
    created_after = _parse_job_timestamp(wait.created_after) or datetime.fromtimestamp(0, tz=timezone.utc)
    expires_at = _parse_job_timestamp(wait.expires_at) or (_utc_now() + timedelta(minutes=5))
    if message_internal_date < created_after or _utc_now() > expires_at:
        return None
    if wait.expected_sender_patterns and not any(re.search(pattern, sender, flags=re.IGNORECASE) for pattern in wait.expected_sender_patterns):
        return None
    if wait.expected_subject_patterns and not any(re.search(pattern, subject, flags=re.IGNORECASE) for pattern in wait.expected_subject_patterns):
        return None
    patterns = wait.otp_regex or []
    code = first_matching_code(text, patterns)
    if code:
        return code
    fallback_codes = extract_otp_codes(text)
    return fallback_codes[0] if fallback_codes else None


def _is_resume_request(query: str) -> bool:
    lowered = query.lower()
    return any(token in lowered for token in ("continue that task", "resume that task", "resume the task", "continue the task"))


def _is_input_reply(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return False
    return normalized.startswith(("answer:", "answer ", "continue with ", "resume with "))


def _strip_input_reply_prefix(query: str) -> str:
    normalized = query.strip()
    if not normalized:
        return ""
    return re.sub(r"^(answer\s*:?\s*|continue with\s+|resume with\s+)", "", normalized, flags=re.IGNORECASE).strip()


def _plain_text_message(text: str) -> str:
    normalized = (text or "").strip()
    if not normalized:
        return ""
    normalized = re.sub(r"\*\*(.*?)\*\*", r"\1", normalized)
    normalized = re.sub(r"__(.*?)__", r"\1", normalized)
    normalized = re.sub(r"`([^`]*)`", r"\1", normalized)
    return normalized.strip()


def _clean_user_facing_result(text: str) -> str:
    normalized = (text or "").strip()
    if not normalized:
        return ""
    cleaned = normalized
    cleaned = re.sub(
        r"(?is)^\s*(?:the page is dynamic/js rendered.*?|google flights requires javascript.*?|the site requires javascript.*?|the live price search tools and web sources aren't returning results right now.*?|ok,\s*search is currently unavailable.*?|let me be upfront about what i've found and present the best option from the structured data i already have\..*?)(?:\n\s*\n|$)",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?is)^\s*let me (?:work with what i already have.*?|compile the best info from what i have.*?|pull together what i know.*?|use what i've already gathered.*?)(?:\n\s*\n|$)",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?im)^\s*(?:based on my research,\s*)?(?:here(?:'s| is)\s+)(?:a clear summary(?: of)?|what i can tell you(?: so far)?|my summary(?: of)?|the full analysis(?: based on the data i've gathered)?|the analysis)[:\s-]*",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?im)^\s*now i have a clear picture\.\s*let me summarize the findings against your criteria\.?\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(r"(?im)^\s*ok,\s*", "", cleaned)
    cleaned = re.sub(r"(?im)^\s*>?\s*file saved:.*$", "", cleaned)
    cleaned = re.sub(r"(?m)^\s*---\s*$", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return _plain_text_message(cleaned or normalized)


def _format_siri_telegram_mirror(query: str, *, reply: Optional[str] = None) -> str:
    parts: list[str] = []
    normalized_query = _plain_text_message(query)
    normalized_reply = _clean_user_facing_result(reply or "")
    if normalized_query:
        parts.append(f"Siri: {normalized_query}")
    if normalized_reply:
        parts.append(normalized_reply)
    return "\n\n".join(part for part in parts if part).strip()[:4000]


async def _mirror_siri_to_telegram(
    query: str,
    *,
    reply: Optional[str] = None,
    state: Optional[StateStore] = None,
    task_class: TaskClass = TaskClass.LIGHT,
) -> None:
    chat_id = settings.secret(settings.telegram_allowed_chat_id_param)
    if not chat_id:
        return
    cleaned_query = _plain_text_message(query)
    cleaned_reply = _clean_user_facing_result(reply or "")
    if state is not None and cleaned_query:
        state.record_turn(
            channel="telegram",
            user_id=chat_id,
            conversation_id=chat_id,
            role=ThreadTurnRole.USER,
            text=cleaned_query,
            task_class=task_class,
        )
    if state is not None and cleaned_reply:
        state.record_turn(
            channel="telegram",
            user_id=chat_id,
            conversation_id=chat_id,
            role=ThreadTurnRole.ASSISTANT,
            text=cleaned_reply,
            task_class=task_class,
        )
    message = _format_siri_telegram_mirror(query, reply=reply)
    if not message:
        return
    try:
        await TelegramClient(settings).send_message(chat_id, message)
    except Exception:
        logger.exception("failed to mirror siri exchange to telegram")


def _parse_job_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _stale_active_job_error(job: AgentJob) -> str:
    if job.status == JobStatus.RUNNING:
        return "This task stopped making progress before it finished."
    return "This task never started cleanly and was cleaned up after it stopped making progress."


def _active_job_is_stale(job: AgentJob, *, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if job.status == JobStatus.RUNNING:
        reference = _parse_job_timestamp(job.last_heartbeat_at) or _parse_job_timestamp(job.created_at)
        if reference is None:
            return False
        if (job.current_step or "") == "attachments_ready":
            return now - reference > ATTACHMENTS_READY_STALE_AFTER
        return now - reference > RUNNING_JOB_STALE_AFTER
    if job.status in {JobStatus.QUEUED, JobStatus.WAITING_WORKER}:
        reference = _parse_job_timestamp(job.created_at)
        if reference is None:
            return False
        return now - reference > WAITING_JOB_STALE_AFTER
    return False


def _refresh_stale_active_jobs(state: StateStore, jobs: list[AgentJob]) -> list[AgentJob]:
    if not jobs:
        return jobs
    now = datetime.now(timezone.utc)
    refreshed: list[AgentJob] = []
    for job in jobs:
        if _active_job_is_stale(job, now=now):
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step="stopped after losing progress",
                error_message=_stale_active_job_error(job),
            )
            _clear_thread_active_heavy_job_if_matches(state, job)
            refreshed_job = state.get_job(job.job_id)
            if refreshed_job is not None:
                job = refreshed_job
        refreshed.append(job)
    return refreshed


def _refresh_superseded_paused_jobs(state: StateStore, jobs: list[AgentJob]) -> list[AgentJob]:
    if not jobs:
        return jobs
    paused_jobs = [job for job in jobs if job.status == JobStatus.PAUSED_FOR_INPUT]
    if len(paused_jobs) <= 1:
        return jobs
    keep_job = sorted(paused_jobs, key=_job_sort_timestamp, reverse=True)[0]
    refreshed: list[AgentJob] = []
    for job in jobs:
        if job.status == JobStatus.PAUSED_FOR_INPUT and job.job_id != keep_job.job_id:
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step="superseded by newer task",
                error_message="This older paused task was cleaned up after newer work took priority.",
            )
            _clear_thread_active_heavy_job_if_matches(state, job)
            refreshed_job = state.get_job(job.job_id)
            if refreshed_job is not None:
                job = refreshed_job
        refreshed.append(job)
    return refreshed


def _humanize_worker_failure(query: str, error_message: str, status: JobStatus) -> str:
    normalized = _plain_text_message(error_message)
    lowered = normalized.lower()
    query_lowered = query.lower()

    if status == JobStatus.INTERRUPTED:
        if "stopped by user" in lowered or "activity cancelled" in lowered or "activity canceled" in lowered:
            return "I stopped that task."
        return "This task was interrupted before it finished. I kept the latest checkpoint for the next follow-up."
    if status == JobStatus.TIMED_OUT:
        return "This task took too long and timed out before it finished."
    if "request_limit of 50" in lowered or "would exceed the request_limit" in lowered:
        if any(token in query_lowered for token in ("flight", "flights", "hotel", "hotels", "rental car", "rental cars", "google flights", "google travel")):
            return "I hit an internal step limit while working through the travel search, so this run did not finish cleanly."
        if any(token in query_lowered for token in ("restaurant", "reservation", "resy", "opentable", "booking")):
            return "I hit an internal step limit while working through the reservation search, so this run did not finish cleanly."
        return "I hit an internal step limit, so this run did not finish cleanly."
    if "worker exited without reporting a terminal state" in lowered:
        return "I hit an internal worker problem before the task finished."
    if any(
        token in lowered
        for token in (
            "status_code: 500",
            "status_code: 502",
            "status_code: 503",
            "status_code: 504",
            "internal_error",
            "internal server error",
            "bad gateway",
            "gateway timeout",
            "upstream connect error",
            "model_name: deepseek-chat",
            "model_name: deepseek/deepseek-chat",
        )
    ):
        if any(token in query_lowered for token in ("restaurant", "reservation", "resy", "opentable", "booking", "book ")):
            return "The model provider had a transient failure while I was working through the reservation flow."
        return "The model provider had a transient failure before the task finished."
    if any(token in lowered for token in ("429", "1015", "rate limit", "rate-limited", "rate limited", "captcha", "access denied", "forbidden", "403")):
        if any(token in query_lowered for token in ("flight", "flights", "hotel", "hotels", "rental car", "rental cars", "google flights", "google travel")):
            return "Some live travel sources temporarily blocked or rate-limited access during this run, and the fallback paths still did not fully complete."
        if any(token in query_lowered for token in ("restaurant", "reservation", "resy", "opentable", "booking")):
            return "Some live booking sources temporarily blocked or rate-limited access during this run, and the fallback paths still did not fully complete."
        return "Live web sources temporarily blocked or rate-limited access during this run."
    if "timed out" in lowered:
        return "This task timed out before it finished."
    if "restaurant-cli timed out" in lowered:
        return "The restaurant source took too long to respond, so this run did not finish cleanly."
    if normalized:
        return normalized
    return "I hit an internal error before the task finished."


def _humanize_step(step: str) -> str:
    normalized = (step or "").strip()
    if not normalized:
        return ""
    known = {
        "waiting_for_user_input": "waiting for your reply",
        "waiting_for_confirmation": "waiting for your booking confirmation",
        "stopped by user": "stopped by you",
        "approval received": "approval received",
        "waiting_worker": "waiting for the worker",
        "card_entry_required": "waiting for your approval on a card-on-file step",
        "payment_blocked": "blocked on a payment step",
    }
    if normalized in known:
        return known[normalized]
    return normalized.replace("_", " ").strip()


def _humanize_status(status: JobStatus) -> str:
    known = {
        JobStatus.QUEUED: "queued",
        JobStatus.WAITING_WORKER: "waiting for the worker",
        JobStatus.RUNNING: "running",
        JobStatus.WAITING_APPROVAL: "waiting for approval",
        JobStatus.PAUSED_FOR_INPUT: "paused for your input",
        JobStatus.CHECKPOINTED: "checkpointed",
        JobStatus.INTERRUPTED: "interrupted",
        JobStatus.TIMED_OUT: "timed out",
        JobStatus.PAUSED_BUDGET: "paused for budget limits",
        JobStatus.COMPLETED: "completed",
        JobStatus.FAILED: "failed",
    }
    return known.get(status, status.value.replace("_", " "))


def _looks_like_natural_input_reply(query: str) -> bool:
    normalized = query.strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    if _is_status_request(normalized) or _is_list_tasks_request(normalized) or _is_stop_request(normalized) or _is_resume_request(normalized):
        return False
    if any(lowered.startswith(prefix) for prefix in ("what ", "why ", "how ", "when ", "where ", "who ")):
        return False
    if re.match(
        r"^(?:friday[\s,:-]+)?(?:find|book|search|look(?:\s+for)?|show|get|plan|research|compare|track|check|cancel|stop|start|help|tell me|give me)\b",
        lowered,
    ):
        return False
    if normalized.endswith("?"):
        return False
    if classify_task(normalized) == TaskClass.HEAVY:
        return False
    if task_routing_profile(normalized).name != "general":
        return False
    return len(normalized) <= 220


def _natural_reply_can_resume(
    query: str,
    *,
    resume_job: Optional[AgentJob],
    latest_status_job: Optional[AgentJob],
) -> bool:
    if resume_job is None or latest_status_job is None:
        return False
    if latest_status_job.job_id != resume_job.job_id:
        return False
    return _looks_like_natural_input_reply(query)


def _query_changes_active_heavy_domain(
    query: str,
    *,
    latest_job: Optional[AgentJob],
) -> bool:
    if latest_job is None or latest_job.task_class != TaskClass.HEAVY:
        return False
    normalized = query.strip()
    if not normalized:
        return False
    return not query_domains_compatible(latest_job.query, normalized)


def _should_supersede_for_new_heavy_task(
    query: str,
    *,
    latest_job: Optional[AgentJob],
    task_class: TaskClass,
) -> bool:
    if task_class != TaskClass.HEAVY:
        return False
    if not _job_can_be_superseded_by_followup(latest_job):
        return False
    return _query_changes_active_heavy_domain(query, latest_job=latest_job)


def _followup_matcher_decision(
    query: str,
    *,
    latest_job: Optional[AgentJob],
) -> Optional[bool]:
    if latest_job is None or latest_job.task_class != TaskClass.HEAVY:
        return False
    normalized = query.strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    if (
        _is_status_request(normalized)
        or _is_list_tasks_request(normalized)
        or _is_stop_request(normalized)
        or _is_resume_request(normalized)
        or _is_input_reply(normalized)
    ):
        return False
    if _query_changes_active_heavy_domain(normalized, latest_job=latest_job):
        return False
    if len(normalized) > 220:
        return False
    if re.match(r"^\s*(find|book|show|look|search|reserve|get|plan)\b", lowered):
        return False
    contextual_phrases = (
        "what other",
        "what about",
        "what cuisines",
        "what option",
        "go with",
        "resy only",
        "only give me",
        "none of these",
        "this doesn't",
        "that doesn't",
        "doesn't work",
        "doesnt work",
        "try a different approach",
        "switch gears",
        "my budget",
        "too expensive",
        "not on resy",
        "preferably",
        "free cancellation",
        "free cancelation",
        "free cancel",
        "ones with",
        "that have",
    )
    if any(phrase in lowered for phrase in contextual_phrases):
        return True
    if re.match(r"^\s*(what(?:'s| is)?\s+the?\s*time|who(?:'s| is)?|when(?:'s| is)?|where(?:'s| is)?|why(?:'s| is)?|how(?:\s+do|\s+does|\s+can|\s+would)|tell me|explain)\b", lowered):
        return False
    if len(normalized) <= 180 and re.search(
        r"\b(preferably|prefer|only|under|near|around|earlier|later|cheaper|closer|indoor|outdoor|free)\b",
        lowered,
    ):
        return True
    if task_routing_profile(normalized).name != "general":
        return False
    if len(normalized) <= 160 and re.search(
        r"\b(this|that|these|those|other|another|instead|same|only|them|ones)\b",
        lowered,
    ):
        return True
    if task_routing_profile(normalized).name != "general":
        return False
    if len(normalized) <= 120:
        return None
    return False


def _should_continue_contextual_heavy_followup(
    query: str,
    *,
    latest_job: Optional[AgentJob],
) -> bool:
    return _followup_matcher_decision(query, latest_job=latest_job) is True


async def _llm_followup_decision(
    query: str,
    *,
    latest_job: Optional[AgentJob],
) -> bool:
    if latest_job is None or latest_job.task_class != TaskClass.HEAVY:
        return False
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not deepseek_key:
        try:
            deepseek_key = settings.secret(settings.deepseek_api_key_param).strip()
        except Exception as exc:
            logger.warning("followup classifier secret unavailable: %s", exc)
            deepseek_key = ""
    if deepseek_key:
        os.environ["DEEPSEEK_API_KEY"] = deepseek_key
    agent = Agent(
        settings.agent_model,
        output_type=FollowupDecision,
        retries=1,
        output_retries=1,
        system_prompt=(
            "Decide whether the user's new message should continue the same in-progress task. "
            "Return is_followup=true only when the new message clearly refines, constrains, corrects, or chooses within the active task. "
            "Return is_followup=false when the message is a new request, a separate question, a status request, or unrelated small talk. "
            "Be conservative about attaching unrelated requests to the active task."
        ),
    )
    latest_summary = (latest_job.latest_checkpoint_summary or latest_job.result_preview or "").strip()
    prompt = (
        f"Active task query:\n{latest_job.query.strip()[:2000]}\n\n"
        f"Active task status: {latest_job.status.value}\n"
        f"Active task step: {(latest_job.current_step or '').strip()[:500]}\n"
        f"Active task update:\n{latest_summary[:1200]}\n\n"
        f"New user message:\n{query.strip()[:1000]}\n\n"
        "Return a structured decision."
    )
    try:
        result = await agent.run(prompt)
    except Exception as exc:
        logger.warning("followup classifier failed query=%r latest_job_id=%s error=%s", query, latest_job.job_id, exc)
        return False
    decision = bool(result.output.is_followup)
    logger.info(
        "followup_classifier latest_job_id=%s is_followup=%s confidence=%s reason=%s query=%r",
        latest_job.job_id,
        decision,
        getattr(result.output, "confidence", ""),
        getattr(result.output, "reason", ""),
        query,
    )
    return decision


async def _resolve_contextual_heavy_followup(
    query: str,
    *,
    latest_job: Optional[AgentJob],
) -> bool:
    matcher_decision = _followup_matcher_decision(query, latest_job=latest_job)
    if matcher_decision is not None:
        return matcher_decision
    return await _llm_followup_decision(query, latest_job=latest_job)


def _build_contextual_heavy_followup_query(
    prior_job: AgentJob,
    checkpoint: Optional[CheckpointPayload],
    new_query: str,
) -> str:
    parts = [
        prior_job.query.strip(),
        "Continue the same task using the user's new follow-up.",
    ]
    if checkpoint is not None and checkpoint.summary.strip():
        parts.append("Latest saved checkpoint:\n" + checkpoint.summary[:2000])
    if checkpoint is not None and checkpoint.resume_instructions.strip():
        parts.append("Resume instructions:\n" + checkpoint.resume_instructions[:3000])
    normalized_query = new_query.strip()
    if normalized_query:
        parts.append("New user direction:\n" + normalized_query[:3000])
    parts.append(
        "Use the existing thread context and any saved workspace state. "
        "Do not repeat stale results or repeat an older answer as if it were new. "
        "If the user changed provider, cuisine, neighborhood, budget, or venue constraints, rerun the live search with the new constraints."
    )
    return "\n\n".join(part for part in parts if part)


def _job_can_be_superseded_by_followup(job: Optional[AgentJob]) -> bool:
    if job is None or job.task_class != TaskClass.HEAVY:
        return False
    return job.status in {
        JobStatus.RUNNING,
        JobStatus.WAITING_WORKER,
        JobStatus.QUEUED,
        JobStatus.WAITING_APPROVAL,
    }


def _log_followup_resolution(
    *,
    channel: str,
    query: str,
    latest_job: Optional[AgentJob],
    action: str,
    replacement_job_id: Optional[str] = None,
) -> None:
    logger.info(
        "followup_resolution channel=%s action=%s latest_job_id=%s latest_status=%s replacement_job_id=%s query=%r",
        channel,
        action,
        latest_job.job_id if latest_job is not None else "",
        latest_job.status.value if latest_job is not None else "",
        replacement_job_id or "",
        query[:400],
    )


async def _supersede_running_heavy_job(
    state: StateStore,
    job: AgentJob,
    *,
    note: str = "superseded by newer follow-up",
) -> None:
    uses_temporal = str((job.metadata or {}).get("execution_backend", "")).strip().lower() == "temporal"
    state.update_job_status(
        job.job_id,
        status=JobStatus.INTERRUPTED,
        current_step=note,
        error_message="This earlier task was replaced by your newer follow-up.",
    )
    if uses_temporal:
        from .temporal_client import signal_stop_heavy_job

        state.record_control_signal(job.job_id, command=ControlCommand.STOP, note=note)
        await signal_stop_heavy_job(settings, job.job_id, note)
        return
    state.record_control_signal(job.job_id, command=ControlCommand.STOP, note=note)


async def _signal_running_heavy_followup(job: Optional[AgentJob], query: str) -> bool:
    if job is None or job.task_class != TaskClass.HEAVY:
        return False
    uses_temporal = str((job.metadata or {}).get("execution_backend", "")).strip().lower() == "temporal"
    if not uses_temporal:
        return False
    state = store()
    normalized = (query or "").strip()
    if not normalized:
        return False
    existing_followup = str((job.metadata or {}).get("pending_followup_text") or "").strip()
    combined_followup = f"{existing_followup}\n{normalized}".strip() if existing_followup else normalized
    state.merge_job_metadata(
        job.job_id,
        {
            "pending_followup_text": combined_followup,
        },
    )
    from .temporal_client import signal_update_heavy_job

    signaled = await signal_update_heavy_job(settings, job.job_id, normalized)
    if signaled:
        return True
    state.merge_job_metadata(job.job_id, {"pending_followup_text": None})
    return False


def _job_result_looks_like_booking_clarification(job: AgentJob) -> bool:
    if task_routing_profile(job.query).name != "booking_commerce":
        return False
    normalized = _plain_text_message(job.result_preview or "")
    if not normalized:
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
    has_list = bool(re.search(r"^\s*(?:[-*•]|\d+\.)", normalized, flags=re.MULTILINE))
    return has_list or normalized.endswith("?")


def _booking_clarification_prompt_from_result(result_text: str) -> tuple[str, str]:
    normalized = _plain_text_message(result_text or "")
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


def _is_status_request(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return False
    patterns = (
        r"^\s*(status|stats|stat|start|starts|started|still)\s*\??\s*$",
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
        r"^\s*/(?:show-?tasks|tasks|jobs)\s*$",
        r"\b(list|show|see)\b.{0,20}\b(tasks|jobs)\b",
        r"\bwhat tasks are\b",
        r"\bwhat jobs are\b",
        r"\bactive tasks\b",
        r"\brunning tasks\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _looks_like_booking_cancel_request(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized or "cancel" not in normalized:
        return False
    if re.search(r"\b(task|job|agent)\b", normalized):
        return False
    booking_markers = (
        "booking",
        "reservation",
        "restaurant",
        "resy",
        "opentable",
        "table",
        "seating",
        "venue",
    )
    return any(marker in normalized for marker in booking_markers)


def _is_stop_request(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return False
    if _looks_like_booking_cancel_request(normalized):
        return False
    patterns = (
        r"^\s*(stop|cancel|abort)\b",
        r"\b(stop|cancel|abort)\b.{0,20}\b(task|job|agent)\b",
        r"\b(stop|cancel|abort)\b.{0,20}\b(this|that|it)\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _is_stop_all_request(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return False
    patterns = (
        r"\b(stop|cancel|abort)\b.{0,20}\b(all|everything)\b",
        r"\b(stop|cancel|abort)\s+all\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _wants_findings_after_stop(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return False
    patterns = (
        r"\b(any findings|what did it find|what did you find|show (?:me )?(?:the )?findings)\b",
        r"\breveal\b.{0,30}\b(findings|results|what it found)\b",
        r"\bstop\b.{0,30}\b(show|share|reveal)\b.{0,30}\b(findings|results|what it found)\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _is_findings_request(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return False
    patterns = (
        r"\bpresent\b.{0,30}\b(findings|results|what you found|what it found)\b",
        r"\bwhat have you found\b",
        r"\bwhat do you have so far\b",
        r"\bcurrent findings\b",
        r"\bshow\b.{0,30}\bwhat you found\b",
        r"\bfindings so far\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _has_useful_partial_findings(summary: str) -> bool:
    return _shared_has_useful_partial_findings(summary)


def _partial_findings_text(state: StateStore, job: AgentJob) -> str:
    checkpoint = state.get_latest_checkpoint(job.job_id)
    return _shared_partial_findings_text(job, checkpoint)


def _progress_snapshot_text(state: StateStore, job: AgentJob) -> str:
    get_checkpoint = getattr(state, "get_latest_checkpoint", None)
    checkpoint = get_checkpoint(job.job_id) if callable(get_checkpoint) else None
    return _plain_text_message(_shared_progress_snapshot_text(job, checkpoint))


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


def _job_accepts_live_worker_updates(job: AgentJob) -> bool:
    return job.status == JobStatus.RUNNING


def _checkpoint_indicates_interruption(summary: str) -> bool:
    return (summary or "").strip().lower().startswith("interrupted:")


def _job_indicates_user_stop(job: AgentJob, latest_summary: str) -> bool:
    current_step = (job.current_step or "").strip().lower()
    error_message = (job.error_message or "").strip().lower()
    return (
        job.status == JobStatus.INTERRUPTED
        or current_step == "stopped by user"
        or "stopped by user" in error_message
        or _checkpoint_indicates_interruption(latest_summary)
    )


def _job_thread_matches(job: AgentJob, *, channel: str, user_id: str, conversation_id: str) -> bool:
    if (
        channel == "telegram"
        and job.source == JobSource.SIRI
        and settings.secret(settings.telegram_allowed_chat_id_param) == user_id
        and conversation_id == user_id
    ):
        return True
    return (
        job.source.value == channel
        and (job.user_id or "unknown") == user_id
        and (job.conversation_id or "default") == conversation_id
    )


def _clear_thread_active_heavy_job_if_matches(state: StateStore, job: Optional[AgentJob]) -> None:
    if job is None or not job.user_id or not job.conversation_id:
        return
    state.clear_active_heavy_job(
        channel=job.source.value,
        user_id=job.user_id,
        conversation_id=job.conversation_id,
        only_if_job_id=job.job_id,
    )
    _sync_siri_heavy_job_to_telegram_thread(state, job_id=None, job=job)


def _sync_siri_heavy_job_to_telegram_thread(
    state: StateStore,
    *,
    job_id: Optional[str],
    job: Optional[AgentJob],
) -> None:
    if job is None or job.source != JobSource.SIRI:
        return
    chat_id = settings.secret(settings.telegram_allowed_chat_id_param)
    if not chat_id:
        return
    if job_id:
        state.set_active_heavy_job(
            channel="telegram",
            user_id=chat_id,
            conversation_id=chat_id,
            job_id=job_id,
        )
        return
    state.clear_active_heavy_job(
        channel="telegram",
        user_id=chat_id,
        conversation_id=chat_id,
        only_if_job_id=job.job_id,
    )


def _record_visible_assistant_turn(state: StateStore, job: AgentJob, text: str, *, task_class: TaskClass) -> None:
    cleaned_text = _clean_user_facing_result(text)
    state.record_turn(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
        role=ThreadTurnRole.ASSISTANT,
        text=cleaned_text,
        task_class=task_class,
    )
    if job.source == JobSource.SIRI:
        chat_id = settings.secret(settings.telegram_allowed_chat_id_param)
        if chat_id:
            state.record_turn(
                channel="telegram",
                user_id=chat_id,
                conversation_id=chat_id,
                role=ThreadTurnRole.ASSISTANT,
                text=cleaned_text,
                task_class=task_class,
            )


def _sync_thread_active_heavy_job(
    state: StateStore,
    *,
    channel: str,
    user_id: str,
    conversation_id: str,
) -> Optional[AgentJob]:
    active_job_id = state.get_active_heavy_job_id(channel=channel, user_id=user_id, conversation_id=conversation_id)
    if not active_job_id:
        return None
    job = state.get_job(active_job_id)
    if job is None or job.task_class != TaskClass.HEAVY or not _job_thread_matches(job, channel=channel, user_id=user_id, conversation_id=conversation_id):
        state.clear_active_heavy_job(
            channel=channel,
            user_id=user_id,
            conversation_id=conversation_id,
            only_if_job_id=active_job_id,
        )
        return None
    if _active_job_is_stale(job):
        state.update_job_status(
            job.job_id,
            status=JobStatus.INTERRUPTED,
            current_step="stopped after losing progress",
            error_message=_stale_active_job_error(job),
        )
        _clear_thread_active_heavy_job_if_matches(state, job)
        return state.get_job(job.job_id)
    if job.status in {JobStatus.RUNNING, JobStatus.WAITING_WORKER, JobStatus.QUEUED, JobStatus.WAITING_APPROVAL, JobStatus.PAUSED_FOR_INPUT}:
        return job
    _clear_thread_active_heavy_job_if_matches(state, job)
    return None


def _parse_context_pk(value: str) -> Optional[tuple[str, str, str]]:
    parts = str(value or "").split("#", 3)
    if len(parts) != 4 or parts[0] != "CTX":
        return None
    return parts[1], parts[2], parts[3]


def _recent_contexts_with_synced_active_jobs(state: StateStore, limit: int = 20) -> list[dict[str, Any]]:
    contexts = state.list_recent_contexts(limit=limit)
    refreshed: list[dict[str, Any]] = []
    for context in contexts:
        item = dict(context)
        active_job_id = str(item.get("active_heavy_job_id", "")).strip()
        parsed_pk = _parse_context_pk(str(item.get("pk", "")))
        if active_job_id and parsed_pk is not None:
            channel, user_id, conversation_id = parsed_pk
            thread_job = _sync_thread_active_heavy_job(
                state,
                channel=channel,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            item["active_heavy_job_id"] = thread_job.job_id if thread_job is not None else ""
        refreshed.append(item)
    return refreshed


def _thread_owner(channel: str, user_id: Optional[str]) -> str:
    if user_id:
        return user_id
    if channel == "siri":
        return "siri"
    return settings.secret(settings.telegram_allowed_chat_id_param) or "unknown"


def _normalized_thread_conversation_id(channel: str, conversation_id: str, owner: str) -> str:
    normalized = str(conversation_id or "").strip()
    if channel == "telegram" and normalized == "telegram-owner":
        return owner
    return normalized


async def _preferred_latest_status_job_for_thread_async(
    state: StateStore,
    *,
    channel: str,
    user_id: str,
    conversation_id: str,
    owner_pairs: list[tuple[JobSource, str]],
) -> Optional[AgentJob]:
    thread_job = _sync_thread_active_heavy_job(
        state,
        channel=channel,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if thread_job is not None and thread_job.status in {
        JobStatus.RUNNING,
        JobStatus.WAITING_WORKER,
        JobStatus.QUEUED,
        JobStatus.WAITING_APPROVAL,
        JobStatus.PAUSED_FOR_INPUT,
    }:
        return thread_job
    return await _latest_status_job_for_pairs_async(state, owner_pairs)


def _extract_explicit_memory_fact(query: str) -> str:
    normalized = query.strip()
    if not normalized or normalized.endswith("?"):
        return ""
    tagged_match = re.match(r"^#(?:memory|remember)\s+(.+)$", normalized, flags=re.IGNORECASE)
    if tagged_match:
        return tagged_match.group(1).strip()
    natural_patterns = (
        r"^(?:friday[\s,:-]+)?remember(?:\s+(?:this|that))?[:\s-]+(.+)$",
        r"^(?:friday[\s,:-]+)?add(?:\s+this)?\s+to\s+(?:your\s+)?memory[:\s-]+(.+)$",
        r"^(?:friday[\s,:-]+)?add\s+(.+?)\s+to\s+(?:your\s+)?memory$",
    )
    for pattern in natural_patterns:
        match = re.match(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _remember_if_tagged(state: StateStore, *, user_id: str, query: str) -> bool:
    memory_fact = _extract_explicit_memory_fact(query)
    if not memory_fact:
        return False
    state.remember_fact(owner=user_id, text=memory_fact)
    return True


def _stall_stop_user_message() -> str:
    return (
        "I stopped this run because it appeared stuck on the same step without meaningful progress. "
        "I kept the latest checkpoint and will use it if you continue the task."
    )


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
    raise RuntimeError("use async variant")


async def _reconcile_temporal_jobs(state: StateStore, jobs: list[AgentJob]) -> list[AgentJob]:
    refreshed = _refresh_stale_active_jobs(state, jobs)
    if not refreshed or not temporal_backend_enabled(settings):
        return refreshed
    temporal_jobs = [
        job for job in refreshed if str((job.metadata or {}).get("execution_backend", "")).strip().lower() == "temporal"
    ]
    if not temporal_jobs:
        return refreshed
    try:
        from .temporal_runtime import get_temporal_client, heavy_workflow_id

        client = await get_temporal_client(settings)
    except Exception:
        return refreshed

    status_updates: dict[str, AgentJob] = {}
    for job in temporal_jobs:
        checkpoint = state.get_latest_checkpoint(job.job_id)
        latest_summary = job.latest_checkpoint_summary or (checkpoint.summary if checkpoint else "") or ""
        try:
            handle = client.get_workflow_handle(heavy_workflow_id(settings, job.job_id))
            description = await handle.describe()
            raw_status = getattr(description, "status", "")
            status_name = getattr(raw_status, "name", str(raw_status)).upper()
        except Exception:
            continue
        if status_name in {"RUNNING", "CONTINUED_AS_NEW"}:
            continue
        if status_name in {"TERMINATED", "CANCELED", "CANCELLED"}:
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step="stopped by user",
                error_message="stopped by user",
            )
            _clear_thread_active_heavy_job_if_matches(state, job)
        elif status_name == "TIMED_OUT":
            state.update_job_status(
                job.job_id,
                status=JobStatus.TIMED_OUT,
                current_step="workflow timed out",
                error_message="The workflow timed out before the task finished.",
            )
            _clear_thread_active_heavy_job_if_matches(state, job)
        elif status_name == "FAILED":
            if _checkpoint_indicates_interruption(latest_summary):
                state.update_job_status(
                    job.job_id,
                    status=JobStatus.INTERRUPTED,
                    current_step=job.current_step or (checkpoint.current_step if checkpoint else "") or "interrupted",
                    error_message="This task was interrupted before it finished.",
                )
                _clear_thread_active_heavy_job_if_matches(state, job)
            else:
                state.update_job_status(
                    job.job_id,
                    status=JobStatus.FAILED,
                    current_step="workflow failed",
                    error_message="The workflow failed before the task finished.",
                )
                _clear_thread_active_heavy_job_if_matches(state, job)
        elif status_name == "COMPLETED":
            if (job.result_preview or "").strip() or job.output_files or job.artifact_keys:
                state.update_job_status(
                    job.job_id,
                    status=JobStatus.COMPLETED,
                    current_step="completed",
                    error_message="",
                )
                _clear_thread_active_heavy_job_if_matches(state, job)
            elif _job_indicates_user_stop(job, latest_summary):
                state.update_job_status(
                    job.job_id,
                    status=JobStatus.INTERRUPTED,
                    current_step=job.current_step or (checkpoint.current_step if checkpoint else "") or "stopped by user",
                    error_message=job.error_message or "stopped by user",
                )
                _clear_thread_active_heavy_job_if_matches(state, job)
            else:
                state.update_job_status(
                    job.job_id,
                    status=JobStatus.INTERRUPTED,
                    current_step="workflow finished without persisting a final result",
                    error_message="The workflow finished internally, but its final result was not persisted cleanly.",
                )
                _clear_thread_active_heavy_job_if_matches(state, job)
        refreshed_job = state.get_job(job.job_id)
        if refreshed_job is not None:
            status_updates[job.job_id] = refreshed_job
    return [status_updates.get(job.job_id, job) for job in refreshed]


def _job_sort_timestamp(job: AgentJob) -> str:
    return job.created_at or job.updated_at or ""


async def _latest_status_job_for_user_async(state: StateStore, *, source: JobSource, user_id: str) -> Optional[AgentJob]:
    return await _latest_status_job_for_pairs_async(state, [(source, user_id)])


async def _latest_status_job_for_pairs_async(
    state: StateStore,
    source_user_pairs: list[tuple[JobSource, str]],
) -> Optional[AgentJob]:
    active_statuses = (
        JobStatus.RUNNING,
        JobStatus.WAITING_WORKER,
        JobStatus.QUEUED,
        JobStatus.WAITING_APPROVAL,
        JobStatus.PAUSED_FOR_INPUT,
        JobStatus.CHECKPOINTED,
        JobStatus.INTERRUPTED,
        JobStatus.PAUSED_BUDGET,
        JobStatus.TIMED_OUT,
    )
    active_jobs: list[AgentJob] = []
    recent_jobs: list[AgentJob] = []
    for source, user_id in source_user_pairs:
        active_jobs.extend(
            job
            for job in state.list_jobs_for_user(source=source.value, user_id=user_id, statuses=active_statuses, limit=10)
            if job.task_class == TaskClass.HEAVY
        )
        recent_jobs.extend(
            job
            for job in state.list_jobs_for_user(
                source=source.value,
                user_id=user_id,
                statuses=(JobStatus.COMPLETED, JobStatus.FAILED),
                limit=10,
            )
            if job.task_class == TaskClass.HEAVY
        )
    active_jobs = await _reconcile_temporal_jobs(state, active_jobs)
    active_jobs = _refresh_superseded_paused_jobs(state, active_jobs)
    recent_statuses = (
        JobStatus.COMPLETED,
        JobStatus.FAILED,
    )
    if not recent_statuses:
        recent_jobs = []
    candidates = active_jobs + recent_jobs
    if not candidates:
        return None
    ordered = sorted(candidates, key=_job_sort_timestamp, reverse=True)
    return ordered[0]


def _active_jobs_for_user(state: StateStore, *, source: JobSource, user_id: str) -> list[AgentJob]:
    raise RuntimeError("use async variant")


async def _active_jobs_for_user_async(state: StateStore, *, source: JobSource, user_id: str) -> list[AgentJob]:
    return await _active_jobs_for_pairs_async(state, [(source, user_id)])


async def _active_jobs_for_pairs_async(
    state: StateStore,
    source_user_pairs: list[tuple[JobSource, str]],
) -> list[AgentJob]:
    jobs: list[AgentJob] = []
    for source, user_id in source_user_pairs:
        jobs.extend(
            job
            for job in state.list_jobs_for_user(
                source=source.value,
                user_id=user_id,
                statuses=(
                    JobStatus.RUNNING,
                    JobStatus.WAITING_WORKER,
                    JobStatus.QUEUED,
                    JobStatus.WAITING_APPROVAL,
                    JobStatus.PAUSED_FOR_INPUT,
                ),
                limit=10,
            )
            if job.task_class == TaskClass.HEAVY
        )
    refreshed = await _reconcile_temporal_jobs(state, jobs)
    refreshed = _refresh_superseded_paused_jobs(state, refreshed)
    return [
        job
        for job in refreshed
        if job.status in {JobStatus.RUNNING, JobStatus.WAITING_WORKER, JobStatus.QUEUED, JobStatus.WAITING_APPROVAL, JobStatus.PAUSED_FOR_INPUT}
    ]


def _latest_paused_input_job_for_user(state: StateStore, *, source: JobSource, user_id: str) -> Optional[AgentJob]:
    return state.get_latest_job_for_user(
        source=source.value,
        user_id=user_id,
        statuses=(JobStatus.PAUSED_FOR_INPUT,),
    )


def _latest_paused_input_job_for_pairs(state: StateStore, source_user_pairs: list[tuple[JobSource, str]]) -> Optional[AgentJob]:
    candidates: list[AgentJob] = []
    for source, user_id in source_user_pairs:
        job = _latest_paused_input_job_for_user(state, source=source, user_id=user_id)
        if job is not None:
            candidates.append(job)
    if not candidates:
        return None
    return sorted(candidates, key=_job_sort_timestamp, reverse=True)[0]


def _latest_completed_clarification_job_for_user(state: StateStore, *, source: JobSource, user_id: str) -> Optional[AgentJob]:
    candidates = state.list_jobs_for_user(
        source=source.value,
        user_id=user_id,
        statuses=(JobStatus.COMPLETED,),
        limit=10,
    )
    now = datetime.now(timezone.utc)
    for job in candidates:
        if str((job.metadata or {}).get("execution_backend", "")).strip().lower() != "temporal":
            continue
        created_at = _parse_job_timestamp(job.created_at)
        if created_at is None or now - created_at > timedelta(hours=1):
            continue
        if _job_result_looks_like_booking_clarification(job):
            return job
    return None


def _latest_completed_clarification_job_for_pairs(
    state: StateStore,
    source_user_pairs: list[tuple[JobSource, str]],
) -> Optional[AgentJob]:
    candidates: list[AgentJob] = []
    for source, user_id in source_user_pairs:
        job = _latest_completed_clarification_job_for_user(state, source=source, user_id=user_id)
        if job is not None:
            candidates.append(job)
    if not candidates:
        return None
    return sorted(candidates, key=_job_sort_timestamp, reverse=True)[0]


def _siri_owner_source_pairs() -> list[tuple[JobSource, str]]:
    pairs: list[tuple[JobSource, str]] = [(JobSource.SIRI, "siri")]
    allowed_chat_id = settings.secret(settings.telegram_allowed_chat_id_param)
    if allowed_chat_id:
        pairs.append((JobSource.TELEGRAM, str(allowed_chat_id)))
    return pairs


def _telegram_owner_source_pairs(user_id: str) -> list[tuple[JobSource, str]]:
    pairs: list[tuple[JobSource, str]] = [(JobSource.TELEGRAM, str(user_id))]
    pairs.append((JobSource.SIRI, "siri"))
    return pairs


def _checkpoint_input_prompt(checkpoint: Optional[CheckpointPayload]) -> tuple[str, str]:
    if checkpoint is None:
        return "", ""
    metadata = checkpoint.metadata or {}
    question = str(metadata.get("input_question", "")).strip()
    details = str(metadata.get("input_details", "")).strip()
    return question, details


def _paused_input_reply_text(state: StateStore, job: AgentJob) -> str:
    checkpoint = state.get_latest_checkpoint(job.job_id)
    question, details = _checkpoint_input_prompt(checkpoint)
    progress = _progress_snapshot_text(state, job)
    raw_summary = _plain_text_message(job.latest_checkpoint_summary or (checkpoint.summary if checkpoint else "") or "")
    question = _plain_text_message(question)
    details = _plain_text_message(details)

    lines = ["I need one thing before I continue."]
    if question:
        lines.extend(["", question])
    if details:
        lines.extend(["", details])
    if raw_summary and raw_summary not in {question, details, progress}:
        lines.extend(["", raw_summary])
    if progress:
        lines.extend(["", progress])
    lines.extend(
        [
            "",
            "Reply normally with the missing detail.",
            "If you want something else instead, just ask.",
        ]
    )
    return "\n".join(lines)


def _build_paused_input_resume_query(
    paused_job: AgentJob,
    checkpoint: Optional[CheckpointPayload],
    reply_text: str,
    *,
    has_attachment: bool = False,
) -> str:
    parts = [paused_job.query.strip(), "Resume the task from the prior paused-for-input checkpoint."]
    if checkpoint is not None and checkpoint.resume_instructions.strip():
        parts.append("Previous resume instructions:\n" + checkpoint.resume_instructions[:3000])
    normalized_reply = reply_text.strip()
    if normalized_reply:
        parts.append("New user input:\n" + normalized_reply[:3000])
    if has_attachment:
        parts.append("The user also provided the requested attachment in this resumed run.")
    parts.append("Continue from the saved workspace state. Do not ask the same question again unless the new input is still insufficient.")
    return "\n\n".join(part for part in parts if part)


def _format_tasks_list(state: StateStore, jobs: list[AgentJob]) -> str:
    if not jobs:
        return "I do not see any active long-running tasks right now."
    lines = ["Active tasks:"]
    for index, job in enumerate(jobs, start=1):
        progress = _progress_snapshot_text(state, job)
        checkpoint = state.get_latest_checkpoint(job.job_id) if hasattr(state, "get_latest_checkpoint") else None
        raw_summary = _plain_text_message(job.latest_checkpoint_summary or (checkpoint.summary if checkpoint else "") or "")
        source = job.source.value
        query_preview = _plain_text_message(job.query or "")
        query_preview = re.sub(r"\s+", " ", query_preview).strip()
        if len(query_preview) > 90:
            query_preview = query_preview[:87] + "..."
        line = f"{index}. {_humanize_status(job.status)}: {query_preview or job.job_id[:8]}"
        if query_preview:
            line += f" ({source})"
        if raw_summary and raw_summary != progress:
            line += f"\n   {raw_summary[:160]}"
        if progress:
            line += f"\n   {progress[:160]}"
        line += f"\n   ID: {job.job_id[:8]}"
        lines.append(line)
    lines.append("Say 'stop 1' or 'stop <job id>' to stop one.")
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
    running_or_pending = [
        job
        for job in jobs
        if job.status in {JobStatus.RUNNING, JobStatus.WAITING_WORKER, JobStatus.QUEUED, JobStatus.WAITING_APPROVAL}
    ]
    running_jobs = [job for job in jobs if job.status == JobStatus.RUNNING]
    non_paused = [job for job in jobs if job.status != JobStatus.PAUSED_FOR_INPUT]
    if any(
        phrase in lowered
        for phrase in (
            "latest",
            "last",
            "that",
            "this task",
            "this job",
            "cancel this",
            "stop this",
            "abort this",
        )
    ):
        if running_or_pending:
            return running_or_pending[0]
        return jobs[0]
    if len(running_jobs) == 1:
        return running_jobs[0]
    if len(running_or_pending) == 1:
        return running_or_pending[0]
    if len(non_paused) == 1:
        return non_paused[0]
    return jobs[0] if len(jobs) == 1 else None


def _stop_jobs(state: StateStore, jobs: list[AgentJob]) -> tuple[int, int]:
    stopped_now = 0
    signaled = 0
    for job in jobs:
        if _active_job_is_stale(job):
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step="stopped after losing progress",
                error_message=_stale_active_job_error(job),
            )
            _clear_thread_active_heavy_job_if_matches(state, job)
            stopped_now += 1
            continue
        if job.status in {JobStatus.QUEUED, JobStatus.WAITING_WORKER, JobStatus.WAITING_APPROVAL, JobStatus.PAUSED_FOR_INPUT}:
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step="stopped by user",
                error_message="stopped by user",
            )
            _clear_thread_active_heavy_job_if_matches(state, job)
            stopped_now += 1
        elif job.status == JobStatus.RUNNING:
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step="stopped by user",
                error_message="stopped by user",
            )
            _clear_thread_active_heavy_job_if_matches(state, job)
            state.record_control_signal(job.job_id, command=ControlCommand.STOP, note="stopped by user")
            stopped_now += 1
            signaled += 1
    return stopped_now, signaled


async def _stop_jobs_async(
    state: StateStore,
    jobs: list[AgentJob],
    *,
    note: str = "stopped by user",
) -> tuple[int, int]:
    stopped_now = 0
    signaled = 0
    for job in jobs:
        uses_temporal = str((job.metadata or {}).get("execution_backend", "")).strip().lower() == "temporal"
        if _active_job_is_stale(job):
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step="stopped after losing progress",
                error_message=_stale_active_job_error(job),
            )
            _clear_thread_active_heavy_job_if_matches(state, job)
            stopped_now += 1
            continue
        if job.status in {JobStatus.QUEUED, JobStatus.WAITING_WORKER, JobStatus.WAITING_APPROVAL, JobStatus.PAUSED_FOR_INPUT}:
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step=note,
                error_message=note,
            )
            _clear_thread_active_heavy_job_if_matches(state, job)
            if uses_temporal:
                from .temporal_client import signal_stop_heavy_job

                state.record_control_signal(job.job_id, command=ControlCommand.STOP, note=note)
                signaled += 1 if await signal_stop_heavy_job(settings, job.job_id, note) else 0
            stopped_now += 1
        elif job.status == JobStatus.RUNNING:
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step=note,
                error_message=note,
            )
            _clear_thread_active_heavy_job_if_matches(state, job)
            if uses_temporal:
                from .temporal_client import signal_stop_heavy_job

                state.record_control_signal(job.job_id, command=ControlCommand.STOP, note=note)
                signaled += 1 if await signal_stop_heavy_job(settings, job.job_id, note) else 0
            else:
                state.record_control_signal(job.job_id, command=ControlCommand.STOP, note=note)
                signaled += 1
            stopped_now += 1
    return stopped_now, signaled


def _format_status_message(state: StateStore, job: Optional[AgentJob]) -> str:
    if job is None:
        return "I do not see a recent long-running task to report on."
    checkpoint = state.get_latest_checkpoint(job.job_id)
    raw_summary = _plain_text_message(job.latest_checkpoint_summary or (checkpoint.summary if checkpoint else "") or "")
    progress = _progress_snapshot_text(state, job)
    if job.status in {JobStatus.QUEUED, JobStatus.WAITING_WORKER}:
        if progress:
            return f"I queued that task. {progress}".strip()
        return "I queued that task."
    if job.status == JobStatus.RUNNING:
        if _checkpoint_indicates_interruption(raw_summary):
            return _shared_interrupted_reply_text(
                job,
                checkpoint,
                lead="I hit an interruption while finishing that task.",
            )
        if progress:
            return progress
        return "I’m still working on that."
    if job.status == JobStatus.WAITING_APPROVAL:
        if progress:
            return f"I’m waiting for approval on that task. {progress}".strip()
        return "I’m waiting for approval on that task."
    if job.status == JobStatus.PAUSED_FOR_INPUT:
        return _paused_input_reply_text(state, job)
    if job.status in {JobStatus.CHECKPOINTED, JobStatus.INTERRUPTED, JobStatus.TIMED_OUT}:
        return _shared_interrupted_reply_text(
            job,
            checkpoint,
            lead=f"That task is {_humanize_status(job.status)}.",
        )
    if job.status == JobStatus.PAUSED_BUDGET:
        if progress:
            return f"That task is paused because of budget limits. {progress}".strip()
        return "That task is paused because of budget limits."
    if job.status == JobStatus.COMPLETED:
        if _job_result_looks_like_booking_clarification(job):
            prompt = (job.result_preview or "").strip()
            return f"I need your choice before I continue.\n{prompt[:1200]}".strip()
        result = (job.result_preview or "").strip()
        result_line = f"\nResult: {result[:800]}" if result else ""
        files_line = f"\nFiles: {', '.join(job.output_files[:5])}" if job.output_files else ""
        return f"That task is done.{result_line}{files_line}".strip()
    if job.status == JobStatus.FAILED:
        error = (job.error_message or "no error details were recorded").strip()
        return f"That task failed. {error[:800]}".strip()
    return f"That task is {_humanize_status(job.status)}.{step_line}{summary_line}".strip()


def _artifacts_prefix(job_id: str) -> str:
    return f"jobs/{job_id}"


def _artifact_key(job_id: str, kind: str, file_name: str) -> str:
    safe_name = quote(file_name, safe="._-() ").replace("%20", "_")
    return f"{_artifacts_prefix(job_id)}/{kind}/{safe_name}"


def _zip_artifact_bundle_name(job: AgentJob) -> str:
    return f"{job.job_id[:8]}-browser-screenshots.zip"


def _build_zip_bytes(files: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_name, content in files:
            archive.writestr(file_name, content)
    return buffer.getvalue()


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


def _build_light_context_for_query(
    state: StateStore,
    *,
    channel: str,
    user_id: str,
    conversation_id: str,
    query: str,
) -> tuple[str, list[Any], list[str], AgentConfig]:
    context, turns, memories, config = _build_light_context(
        state,
        channel=channel,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    active_heavy_job = _sync_thread_active_heavy_job(
        state,
        channel=channel,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if active_heavy_job is None:
        return context, turns, memories, config
    if (
        _is_status_request(query)
        or _is_list_tasks_request(query)
        or _is_stop_request(query)
        or _is_resume_request(query)
        or _is_input_reply(query)
        or _should_continue_contextual_heavy_followup(query, latest_job=active_heavy_job)
    ):
        return context, turns, memories, config
    light_turns = [turn for turn in turns if getattr(turn, "task_class", TaskClass.LIGHT) != TaskClass.HEAVY]
    return "", light_turns, memories, config


async def run_and_notify(job: AgentJob) -> None:
    state = store()
    telegram = TelegramClient(settings)
    context_summary, recent_turns, memories, config = _build_light_context_for_query(
        state,
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
        query=job.query,
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
        failure_message = _humanize_worker_failure(job.query, str(exc), JobStatus.FAILED)
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
            metadata={"execution_backend": "temporal" if temporal_backend_enabled(settings) else "legacy"},
        )
        await _start_heavy_job(state, heavy_job)
        if job.chat_id:
            await telegram.send_message(job.chat_id, "On it. I'll message you here when I have something useful.")
        return

    cleaned_result = _clean_user_facing_result(result.text)
    state.record_turn(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
        role=ThreadTurnRole.ASSISTANT,
        text=cleaned_result,
        task_class=TaskClass.LIGHT,
    )
    if job.chat_id:
        await telegram.send_message(job.chat_id, cleaned_result)
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
    owner = _thread_owner(channel, user_id)
    normalized_conversation_id = _normalized_thread_conversation_id(channel, conversation_id, owner)
    turns = store().get_recent_turns(channel=channel, user_id=owner, conversation_id=normalized_conversation_id, limit=50)
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
    owner = _thread_owner(channel, user_id)
    normalized_conversation_id = _normalized_thread_conversation_id(channel, conversation_id, owner)
    store().clear_thread(channel=channel, user_id=owner, conversation_id=normalized_conversation_id)
    return {"status": "ok"}


@app.get("/context/recent")
async def recent_context(x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    state = store()
    return {"contexts": _recent_contexts_with_synced_active_jobs(state)}


@app.get("/worker/health")
async def worker_health(x_friday_siri_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.siri_api_key_param)
    _auth_or_401(expected_key, x_friday_siri_key)
    state = store()
    active = _refresh_stale_active_jobs(
        state,
        state.list_jobs(
            statuses=(
                JobStatus.RUNNING,
                JobStatus.WAITING_WORKER,
                JobStatus.QUEUED,
                JobStatus.WAITING_APPROVAL,
                JobStatus.PAUSED_FOR_INPUT,
            ),
            limit=10,
        ),
    )
    active = [
        job
        for job in active
        if job.status
        in {
            JobStatus.RUNNING,
            JobStatus.WAITING_WORKER,
            JobStatus.QUEUED,
            JobStatus.WAITING_APPROVAL,
            JobStatus.PAUSED_FOR_INPUT,
        }
    ]
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
    await _stop_jobs_async(state, [job])
    return {"status": "ok"}


@app.get("/dashboard")
async def dashboard_home(request: Request):
    try:
        _require_dashboard_session(request)
    except HTTPException:
        body = f"""
        <section class="panel">
          <p>Sign in with Telegram to manage identities, mailbox health, policies, jobs, sessions, and payment metadata.</p>
          {_dashboard_login_widget_html()}
        </section>
        """
        return HTMLResponse(_dashboard_shell(title="Friday Operator Board", active_view="jobs", body_html=body))
    return RedirectResponse(url="/dashboard/jobs", status_code=302)


@app.get("/dashboard/auth/telegram")
async def dashboard_auth_telegram(request: Request):
    params = {key: value for key, value in request.query_params.items()}
    if not _telegram_login_is_valid(params):
        raise HTTPException(status_code=401, detail="invalid telegram login payload")
    expires_at = (_utc_now() + timedelta(seconds=settings.dashboard_session_ttl_seconds)).isoformat()
    record = DashboardSessionRecord(
        telegram_user_id=params.get("id", ""),
        telegram_auth_date=params.get("auth_date", ""),
        first_name=params.get("first_name", ""),
        username=params.get("username", ""),
        expires_at=expires_at,
    )
    store().create_dashboard_session(record)
    response = RedirectResponse(url="/dashboard/jobs", status_code=302)
    response.set_cookie(
        DASHBOARD_SESSION_COOKIE,
        _encode_dashboard_session_cookie(record.session_id),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.dashboard_session_ttl_seconds,
        path="/",
    )
    return response


@app.post("/dashboard/logout")
async def dashboard_logout(request: Request):
    raw_cookie = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    session_id = _decode_dashboard_session_cookie(raw_cookie)
    if session_id:
        store().delete_dashboard_session(session_id)
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.delete_cookie(DASHBOARD_SESSION_COOKIE, path="/")
    return response


@app.post("/dashboard/mailbox/refresh-watch")
async def dashboard_refresh_mailbox_watch(request: Request):
    _require_dashboard_session(request)
    state = store()
    mailbox_email = _resolved_optional_secret(settings.gmail_account_email_param)
    watch_state = _mailbox_watch_state(state)
    try:
        payload = await renew_gmail_watch(settings)
        watch_state = MailboxWatchState(
            mailbox_email=mailbox_email,
            history_id=str(payload.get("historyId", "")),
            expiration=str(payload.get("expiration", "")),
            topic_name=settings.gmail_pubsub_topic_name,
            watch_status="active",
            last_watch_renewed_at=_utc_now().isoformat(),
            last_push_received_at=watch_state.last_push_received_at,
            last_oauth_tested_at=_utc_now().isoformat(),
            last_oauth_error="",
        )
    except Exception as exc:
        watch_state = watch_state.model_copy(
            update={
                "mailbox_email": mailbox_email,
                "watch_status": "oauth_error",
                "last_oauth_tested_at": _utc_now().isoformat(),
                "last_oauth_error": str(exc)[:500],
                "updated_at": _utc_now().isoformat(),
            }
        )
    state.put_mailbox_watch_state(watch_state)
    return RedirectResponse(url="/dashboard/mailbox", status_code=302)


@app.post("/dashboard/jobs/{job_id}/stop")
async def dashboard_stop_job(job_id: str, request: Request):
    _require_dashboard_session(request)
    state = store()
    job = state.get_job(job_id)
    if job is not None:
        await _stop_jobs_async(state, [job])
    return RedirectResponse(url="/dashboard/jobs", status_code=302)


@app.post("/dashboard/identities/save")
async def dashboard_save_identity(request: Request):
    _require_dashboard_session(request)
    form = await _dashboard_request_data(request)
    state = store()
    identity_id = _dashboard_form_str(form, "identity_id")
    existing = _find_identity_record(state, identity_id) if identity_id else None
    record_kwargs = {
        "identity_id": existing.identity_id if existing else identity_id,
        "label": _dashboard_form_str(form, "label", limit=200) or (existing.label if existing else "Untitled identity"),
        "email": _dashboard_form_str(form, "email", limit=320) or (existing.email if existing else ""),
        "provider": _dashboard_form_str(form, "provider", limit=100) or (existing.provider if existing else "gmail"),
        "category": _dashboard_form_str(form, "category", limit=100) or (existing.category if existing else "general"),
        "site_scope": _dashboard_form_str(form, "site_scope", limit=200) or (existing.site_scope if existing else ""),
        "is_default": _dashboard_form_bool(form, "is_default"),
        "notes": _dashboard_form_str(form, "notes", limit=2000),
        "status": _dashboard_form_str(form, "status", limit=100) or (existing.status if existing else "active"),
    }
    if existing:
        record_kwargs["created_at"] = existing.created_at
    record = IdentityRecord(**record_kwargs)
    if record.is_default:
        for other in state.list_identities(limit=200):
            if other.identity_id != record.identity_id and other.is_default:
                state.put_identity(other.model_copy(update={"is_default": False}))
    saved = state.put_identity(record)
    parameter_name = _dashboard_form_str(form, "secret_parameter_name", limit=500)
    if parameter_name:
        secret_kind_value = _dashboard_form_str(form, "secret_kind", limit=100) or SecretKind.PASSWORD.value
        try:
            secret_kind = SecretKind(secret_kind_value)
        except ValueError:
            secret_kind = SecretKind.PASSWORD
        state.put_identity_secret_pointer(
            IdentitySecretPointer(
                identity_id=saved.identity_id,
                parameter_name=parameter_name,
                secret_kind=secret_kind,
            )
        )
    return RedirectResponse(url="/dashboard/identities", status_code=302)


@app.post("/dashboard/policies/save")
async def dashboard_save_policy(request: Request):
    _require_dashboard_session(request)
    form = await _dashboard_request_data(request)
    state = store()
    policy_id = _dashboard_form_str(form, "policy_id")
    existing = _find_policy_record(state, policy_id) if policy_id else None
    record_kwargs = {
        "policy_id": existing.policy_id if existing else policy_id,
        "label": _dashboard_form_str(form, "label", limit=200) or (existing.label if existing else "Unnamed policy"),
        "site_scope": _dashboard_form_str(form, "site_scope", limit=200) or (existing.site_scope if existing else ""),
        "category": _dashboard_form_str(form, "category", limit=100) or (existing.category if existing else "general"),
        "allow_account_creation": _dashboard_form_bool(form, "allow_account_creation"),
        "allow_login_reuse": _dashboard_form_bool(form, "allow_login_reuse"),
        "allow_zero_dollar_booking": _dashboard_form_bool(form, "allow_zero_dollar_booking"),
        "pause_on_sms_or_captcha": _dashboard_form_bool(form, "pause_on_sms_or_captcha") or not any(
            key in form for key in ("pause_on_sms_or_captcha",)
        ),
        "default_identity_id": _dashboard_form_str(form, "default_identity_id", limit=200),
    }
    if existing:
        record_kwargs["created_at"] = existing.created_at
    record = AutomationPolicyRecord(**record_kwargs)
    state.put_automation_policy(record)
    return RedirectResponse(url="/dashboard/policies", status_code=302)


@app.post("/dashboard/payments/save")
async def dashboard_save_payment_profile(request: Request):
    _require_dashboard_session(request)
    form = await _dashboard_request_data(request)
    state = store()
    payment_profile_id = _dashboard_form_str(form, "payment_profile_id")
    existing = _find_payment_profile(state, payment_profile_id) if payment_profile_id else None
    record_kwargs = {
        "payment_profile_id": existing.payment_profile_id if existing else payment_profile_id,
        "label": _dashboard_form_str(form, "label", limit=200) or (existing.label if existing else "Unnamed profile"),
        "provider": _dashboard_form_str(form, "provider", limit=100) or (existing.provider if existing else "manual"),
        "masked_last4": _dashboard_form_str(form, "masked_last4", limit=8) or (existing.masked_last4 if existing else ""),
        "notes": _dashboard_form_str(form, "notes", limit=2000),
        "limit_cents": max(0, _dashboard_form_int(form, "limit_cents")),
        "active": _dashboard_form_bool(form, "active") or not any(key in form for key in ("active",)),
    }
    if existing:
        record_kwargs["created_at"] = existing.created_at
    record = PaymentProfileRecord(**record_kwargs)
    state.put_payment_profile(record)
    return RedirectResponse(url="/dashboard/payments", status_code=302)


@app.post("/dashboard/sessions/{session_id}/revoke")
async def dashboard_revoke_session(session_id: str, request: Request):
    _require_dashboard_session(request)
    state = store()
    record = _find_browser_session(state, session_id)
    if record is not None:
        try:
            settings.s3.delete_object(Bucket=settings.artifacts_bucket, Key=record.session_s3_key)
        except Exception as exc:
            logger.warning("failed to delete browser session artifact session_id=%s error=%s", session_id, exc)
        state.put_browser_session(record.model_copy(update={"status": "revoked"}))
    return RedirectResponse(url="/dashboard/sessions", status_code=302)


@app.get("/dashboard/{view_name}")
async def dashboard_view(view_name: str, request: Request):
    session = _require_dashboard_session(request)
    state = store()
    session_name = session.first_name or session.username or session.telegram_user_id
    logout_html = """
    <section class="panel">
      <form class="inline" action="/dashboard/logout" method="post">
        <button type="submit">Log out</button>
      </form>
    </section>
    """
    if view_name == "jobs":
        active_statuses = (
            JobStatus.RUNNING,
            JobStatus.WAITING_WORKER,
            JobStatus.QUEUED,
            JobStatus.PAUSED_FOR_INPUT,
            JobStatus.WAITING_APPROVAL,
        )
        recent_statuses = (JobStatus.INTERRUPTED, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.TIMED_OUT)
        active_jobs = state.list_jobs(statuses=active_statuses, limit=25)
        recent_jobs = state.list_jobs(statuses=recent_statuses, limit=25)
        active_rows = []
        for job in active_jobs:
            action = (
                f"<form class='inline' action='/dashboard/jobs/{escape(job.job_id)}/stop' method='post'>"
                "<button type='submit' class='secondary'>Stop</button></form>"
            )
            active_rows.append(
                [
                    escape(job.job_id[:8]),
                    f"<span class='pill'>{escape(job.status.value)}</span>",
                    escape(job.source.value),
                    escape((job.current_step or "-")[:80]),
                    escape((job.latest_checkpoint_summary or job.query)[:120]),
                    action,
                ]
            )
        recent_rows = [
            [
                escape(job.job_id[:8]),
                escape(job.status.value),
                escape(job.source.value),
                escape((job.current_step or "-")[:80]),
                escape((job.result_preview or job.error_message or job.query)[:140]),
            ]
            for job in recent_jobs
        ]
        body = (
            logout_html
            + "<section class='panel'><h2>Active jobs</h2>"
            + _html_table(["Job", "Status", "Source", "Step", "Latest detail", "Action"], active_rows)
            + "</section><section class='panel'><h2>Recent terminal jobs</h2>"
            + _html_table(["Job", "Status", "Source", "Step", "Outcome"], recent_rows)
            + "</section>"
        )
        return HTMLResponse(_dashboard_shell(title="Friday Jobs", active_view="jobs", body_html=body, session_name=session_name))
    if view_name == "mailbox":
        watch_state = _mailbox_watch_state(state)
        waits = state.list_active_mailbox_waits(limit=25)
        wait_rows = [
            [
                escape(wait.wait_id[:8]),
                escape(wait.site_key),
                escape(wait.workflow_id),
                escape(wait.job_id[:8]),
                escape(wait.status),
                escape(wait.expires_at or "-"),
            ]
            for wait in waits
        ]
        kpis = f"""
        <div class="kpi">
          <div><strong>Status</strong><br />{escape(watch_state.watch_status or 'unknown')}</div>
          <div><strong>Mailbox</strong><br />{escape(watch_state.mailbox_email or '-')}</div>
          <div><strong>History ID</strong><br />{escape(watch_state.history_id or '-')}</div>
          <div><strong>Last push</strong><br />{escape(watch_state.last_push_received_at or '-')}</div>
          <div><strong>Last OAuth test</strong><br />{escape(watch_state.last_oauth_tested_at or '-')}</div>
          <div><strong>Last watch renew</strong><br />{escape(watch_state.last_watch_renewed_at or '-')}</div>
        </div>
        """
        oauth_warning = (
            f"<p class='warn'>Last OAuth/watch error: {escape(watch_state.last_oauth_error)}</p>"
            if watch_state.last_oauth_error
            else ""
        )
        body = (
            logout_html
            + "<section class='panel'><h2>Mailbox health</h2>"
            + kpis
            + oauth_warning
            + "<form class='inline' action='/dashboard/mailbox/refresh-watch' method='post'><button type='submit'>Renew Gmail watch</button></form>"
            + "</section><section class='panel'><h2>Verification waits</h2>"
            + _html_table(["Wait", "Site", "Workflow", "Job", "Status", "Expires"], wait_rows)
            + "</section>"
        )
        return HTMLResponse(_dashboard_shell(title="Friday Mailbox", active_view="mailbox", body_html=body, session_name=session_name))
    if view_name == "identities":
        identity_records = state.list_identities(limit=100)
        create_form = """
        <form class="stack" action="/dashboard/identities/save" method="post">
          <div class="grid">
            <label>Label<input name="label" placeholder="Friday Gmail" /></label>
            <label>Email<input name="email" type="email" placeholder="friday.nyc.agent@gmail.com" /></label>
            <label>Provider<input name="provider" value="gmail" /></label>
            <label>Category<input name="category" value="general" /></label>
            <label>Site scope<input name="site_scope" placeholder="resy.com" /></label>
            <label>Status<input name="status" value="active" /></label>
            <label>Secret parameter name<input name="secret_parameter_name" placeholder="/friday/identity/resy/password" /></label>
            <label>Secret kind
              <select name="secret_kind">
                <option value="password">password</option>
                <option value="oauth_refresh_token">oauth_refresh_token</option>
                <option value="cookie_jar">cookie_jar</option>
              </select>
            </label>
          </div>
          <label>Notes<textarea name="notes" placeholder="What this identity is for"></textarea></label>
          <label><input type="checkbox" name="is_default" /> Default identity</label>
          <div><button type="submit">Save identity</button></div>
        </form>
        """
        rows = []
        for record in identity_records:
            pointer = state.get_identity_secret_pointer(record.identity_id)
            pointer_text = (
                f"{escape(pointer.parameter_name)}<br /><span class='muted'>{escape(pointer.secret_kind.value)}</span>"
                if pointer
                else "<span class='muted'>-</span>"
            )
            checked = " checked" if record.is_default else ""
            edit_form = f"""
            <details>
              <summary>Edit</summary>
              <form class="stack" action="/dashboard/identities/save" method="post">
                <input type="hidden" name="identity_id" value="{escape(record.identity_id)}" />
                <div class="grid">
                  <label>Label<input name="label" value="{escape(record.label)}" /></label>
                  <label>Email<input name="email" value="{escape(record.email)}" /></label>
                  <label>Provider<input name="provider" value="{escape(record.provider)}" /></label>
                  <label>Category<input name="category" value="{escape(record.category)}" /></label>
                  <label>Site scope<input name="site_scope" value="{escape(record.site_scope)}" /></label>
                  <label>Status<input name="status" value="{escape(record.status)}" /></label>
                  <label>Secret parameter name<input name="secret_parameter_name" value="{escape(pointer.parameter_name if pointer else '')}" /></label>
                  <label>Secret kind
                    <select name="secret_kind">
                      <option value="password"{' selected' if pointer and pointer.secret_kind == SecretKind.PASSWORD else ''}>password</option>
                      <option value="oauth_refresh_token"{' selected' if pointer and pointer.secret_kind == SecretKind.OAUTH_REFRESH_TOKEN else ''}>oauth_refresh_token</option>
                      <option value="cookie_jar"{' selected' if pointer and pointer.secret_kind == SecretKind.COOKIE_JAR else ''}>cookie_jar</option>
                    </select>
                  </label>
                </div>
                <label>Notes<textarea name="notes">{escape(record.notes)}</textarea></label>
                <label><input type="checkbox" name="is_default"{checked} /> Default identity</label>
                <div><button type="submit">Update</button></div>
              </form>
            </details>
            """
            rows.append(
                [
                    escape(record.label),
                    escape(record.email),
                    escape(record.provider),
                    escape(record.category),
                    escape(record.site_scope or "-"),
                    "yes" if record.is_default else "no",
                    escape(record.status),
                    pointer_text,
                    edit_form,
                ]
            )
        body = (
            logout_html
            + "<section class='panel'><h2>Identities</h2><p class='muted'>Store site-scoped identities and SSM parameter pointers without exposing raw secret values.</p>"
            + create_form
            + _html_table(["Label", "Email", "Provider", "Category", "Site scope", "Default", "Status", "Secret pointer", "Action"], rows)
            + "</section>"
        )
        return HTMLResponse(_dashboard_shell(title="Friday Identities", active_view="identities", body_html=body, session_name=session_name))
    if view_name == "policies":
        policy_records = state.list_automation_policies(limit=100)
        create_form = """
        <form class="stack" action="/dashboard/policies/save" method="post">
          <div class="grid">
            <label>Label<input name="label" placeholder="Resy zero-dollar policy" /></label>
            <label>Site scope<input name="site_scope" placeholder="resy.com" /></label>
            <label>Category<input name="category" value="restaurant" /></label>
            <label>Default identity ID<input name="default_identity_id" placeholder="optional identity id" /></label>
          </div>
          <div class="grid">
            <label><input type="checkbox" name="allow_zero_dollar_booking" /> Allow $0 booking</label>
            <label><input type="checkbox" name="allow_account_creation" /> Allow account creation</label>
            <label><input type="checkbox" name="allow_login_reuse" /> Allow login reuse</label>
            <label><input type="checkbox" name="pause_on_sms_or_captcha" checked /> Pause on SMS/CAPTCHA</label>
          </div>
          <div><button type="submit">Save policy</button></div>
        </form>
        """
        rows = []
        for record in policy_records:
            edit_form = f"""
            <details>
              <summary>Edit</summary>
              <form class="stack" action="/dashboard/policies/save" method="post">
                <input type="hidden" name="policy_id" value="{escape(record.policy_id)}" />
                <div class="grid">
                  <label>Label<input name="label" value="{escape(record.label)}" /></label>
                  <label>Site scope<input name="site_scope" value="{escape(record.site_scope)}" /></label>
                  <label>Category<input name="category" value="{escape(record.category)}" /></label>
                  <label>Default identity ID<input name="default_identity_id" value="{escape(record.default_identity_id)}" /></label>
                </div>
                <div class="grid">
                  <label><input type="checkbox" name="allow_zero_dollar_booking"{' checked' if record.allow_zero_dollar_booking else ''} /> Allow $0 booking</label>
                  <label><input type="checkbox" name="allow_account_creation"{' checked' if record.allow_account_creation else ''} /> Allow account creation</label>
                  <label><input type="checkbox" name="allow_login_reuse"{' checked' if record.allow_login_reuse else ''} /> Allow login reuse</label>
                  <label><input type="checkbox" name="pause_on_sms_or_captcha"{' checked' if record.pause_on_sms_or_captcha else ''} /> Pause on SMS/CAPTCHA</label>
                </div>
                <div><button type="submit">Update</button></div>
              </form>
            </details>
            """
            rows.append(
                [
                    escape(record.label),
                    escape(record.site_scope or "-"),
                    escape(record.category),
                    "yes" if record.allow_zero_dollar_booking else "no",
                    "yes" if record.allow_account_creation else "no",
                    "yes" if record.allow_login_reuse else "no",
                    "yes" if record.pause_on_sms_or_captcha else "no",
                    escape(record.default_identity_id or "-"),
                    edit_form,
                ]
            )
        body = (
            logout_html
            + "<section class='panel'><h2>Policies</h2><p class='muted'>These policies control which sites Friday may use for autonomous account creation and $0-only bookings.</p>"
            + create_form
            + _html_table(["Label", "Site scope", "Category", "$0 booking", "Account creation", "Login reuse", "Pause on SMS/CAPTCHA", "Default identity", "Action"], rows)
            + "</section>"
        )
        return HTMLResponse(_dashboard_shell(title="Friday Policies", active_view="policies", body_html=body, session_name=session_name))
    if view_name == "sessions":
        sessions = state.list_browser_sessions(limit=100)
        body = logout_html + "<section class='panel'><h2>Saved browser sessions</h2>" + _html_table(
            ["Session", "Site scope", "Identity", "User agent", "Viewport", "Status", "Updated", "Action"],
            [
                [
                    escape(record.session_id[:8]),
                    escape(record.site_scope),
                    escape(record.identity_id or "-"),
                    escape((record.user_agent or "")[:50]),
                    escape(f"{record.viewport_width}x{record.viewport_height}"),
                    escape(record.status),
                    escape(record.updated_at),
                    (
                        f"<form class='inline' action='/dashboard/sessions/{escape(record.session_id)}/revoke' method='post'>"
                        "<button type='submit' class='secondary'>Revoke</button></form>"
                    )
                    if record.status != "revoked"
                    else "<span class='muted'>Revoked</span>"
                ]
                for record in sessions
            ],
        ) + "</section>"
        return HTMLResponse(_dashboard_shell(title="Friday Sessions", active_view="sessions", body_html=body, session_name=session_name))
    if view_name == "payments":
        payment_records = state.list_payment_profiles(limit=100)
        create_form = """
        <form class="stack" action="/dashboard/payments/save" method="post">
          <div class="grid">
            <label>Label<input name="label" placeholder="Privacy $1 cap" /></label>
            <label>Provider<input name="provider" value="manual" /></label>
            <label>Masked last4<input name="masked_last4" placeholder="1234" /></label>
            <label>Limit cents<input name="limit_cents" type="number" min="0" value="100" /></label>
          </div>
          <label>Notes<textarea name="notes" placeholder="Phase 1 metadata only; runtime remains $0-only."></textarea></label>
          <label><input type="checkbox" name="active" checked /> Active</label>
          <div><button type="submit">Save payment profile</button></div>
        </form>
        """
        rows = []
        for record in payment_records:
            edit_form = f"""
            <details>
              <summary>Edit</summary>
              <form class="stack" action="/dashboard/payments/save" method="post">
                <input type="hidden" name="payment_profile_id" value="{escape(record.payment_profile_id)}" />
                <div class="grid">
                  <label>Label<input name="label" value="{escape(record.label)}" /></label>
                  <label>Provider<input name="provider" value="{escape(record.provider)}" /></label>
                  <label>Masked last4<input name="masked_last4" value="{escape(record.masked_last4)}" /></label>
                  <label>Limit cents<input name="limit_cents" type="number" min="0" value="{record.limit_cents}" /></label>
                </div>
                <label>Notes<textarea name="notes">{escape(record.notes)}</textarea></label>
                <label><input type="checkbox" name="active"{' checked' if record.active else ''} /> Active</label>
                <div><button type="submit">Update</button></div>
              </form>
            </details>
            """
            rows.append(
                [
                    escape(record.label),
                    escape(record.provider),
                    escape(record.masked_last4 or "-"),
                    escape(str(record.limit_cents)),
                    "yes" if record.active else "no",
                    escape(record.notes[:120] or "-"),
                    edit_form,
                ]
            )
        body = (
            logout_html
            + "<section class='panel'><h2>Payment metadata</h2><p class='muted'>Phase 1 keeps this metadata visible but runtime booking remains blocked to $0 only.</p>"
            + create_form
            + _html_table(
                ["Label", "Provider", "Last4", "Limit cents", "Active", "Notes", "Action"],
                rows,
            )
            + "</section>"
        )
        return HTMLResponse(_dashboard_shell(title="Friday Payments", active_view="payments", body_html=body, session_name=session_name))
    raise HTTPException(status_code=404, detail="unknown dashboard view")


@app.post("/internal/gmail/pubsub")
async def gmail_pubsub_ingress(request: Request, token: Optional[str] = None) -> dict[str, Any]:
    expected_token = _resolved_optional_secret(settings.gmail_pubsub_verification_token_param)
    if expected_token and token != expected_token:
        raise HTTPException(status_code=401, detail="invalid gmail pubsub token")
    payload = await request.json()
    event = decode_pubsub_push_body(payload)
    state = store()
    if not state.claim_pubsub_delivery(event.delivery_id):
        return {"status": "duplicate_ignored", "delivery_id": event.delivery_id}

    previous_watch_state = _mailbox_watch_state(state)
    history_cursor = (previous_watch_state.history_id or "").strip() or event.history_id
    mailbox_email = _resolved_optional_secret(settings.gmail_account_email_param)
    watch_state = previous_watch_state.model_copy(
        update={
            "mailbox_email": mailbox_email or event.email_address,
            "history_id": event.history_id,
            "last_push_received_at": _utc_now().isoformat(),
            "updated_at": _utc_now().isoformat(),
        }
    )
    state.put_mailbox_watch_state(watch_state)

    token_payload = await mint_gmail_access_token(settings)
    message_ids = await list_history_message_ids(
        settings,
        access_token=token_payload["access_token"],
        history_id=history_cursor,
    )
    waits = state.list_active_mailbox_waits(limit=100)
    matched_waits = 0
    signaled = 0

    from .temporal_client import signal_submit_verification_code

    for gmail_message_id in message_ids:
        try:
            message = await gmail_api_get(
                settings,
                f"/messages/{quote(gmail_message_id)}",
                access_token=token_payload["access_token"],
                params={"format": "full"},
            )
        except httpx.HTTPStatusError:
            continue
        headers = gmail_message_headers(message)
        sender = headers.get("from", "")
        subject = headers.get("subject", "")
        text = gmail_message_text(message)
        raw_internal_date = str(message.get("internalDate", "")).strip()
        try:
            message_internal_date = datetime.fromtimestamp(int(raw_internal_date) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            message_internal_date = _utc_now()
        for wait in waits:
            code = _mailbox_wait_matches(
                wait,
                sender=sender,
                subject=subject,
                text=text,
                message_internal_date=message_internal_date,
            )
            if not code:
                continue
            if not state.claim_mailbox_wait_message(wait_id=wait.wait_id, gmail_message_id=gmail_message_id):
                break
            matched_waits += 1
            if await signal_submit_verification_code(settings, wait.job_id, code):
                state.mark_mailbox_wait_matched(wait.wait_id, gmail_message_id=gmail_message_id)
                signaled += 1
            break

    return {
        "status": "ok",
        "delivery_id": event.delivery_id,
        "history_cursor": history_cursor,
        "history_id": event.history_id,
        "message_ids": len(message_ids),
        "matched_waits": matched_waits,
        "signals_sent": signaled,
    }


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
    owner_pairs = _telegram_owner_source_pairs(user_id)
    if not state.claim_telegram_update(chat_id=chat_id, update_id=update.update_id, user_id=user_id):
        logger.info("ignoring duplicate telegram update chat_id=%s update_id=%s", chat_id, update.update_id)
        return {"status": "duplicate_ignored"}
    state.put_session(channel="telegram", user_id=user_id, metadata={"chat_id": chat_id})
    await _active_jobs_for_pairs_async(state, owner_pairs)
    latest_status_job = await _preferred_latest_status_job_for_thread_async(
        state,
        channel="telegram",
        user_id=user_id,
        conversation_id=conversation_id,
        owner_pairs=owner_pairs,
    )

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
        jobs = await _active_jobs_for_pairs_async(state, owner_pairs)
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
            await _preferred_latest_status_job_for_thread_async(
                state,
                channel="telegram",
                user_id=user_id,
                conversation_id=conversation_id,
                owner_pairs=owner_pairs,
            ),
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
        jobs = await _active_jobs_for_pairs_async(state, owner_pairs)
        if _is_stop_all_request(query):
            stopped_now, signaled = await _stop_jobs_async(state, jobs)
            total = stopped_now + signaled
            if total == 0:
                reply = "I do not see any active long-running tasks to stop right now."
            elif signaled and stopped_now:
                reply = f"Stopping {total} tasks now. {stopped_now} were stopped immediately and {signaled} running task{'s were' if signaled != 1 else ' was'} asked to stop."
            elif signaled:
                reply = f"Stopping {signaled} running task{'s' if signaled != 1 else ''} now."
            else:
                reply = f"Stopped {stopped_now} task{'s' if stopped_now != 1 else ''}."
        else:
            target = _resolve_stop_target(query, jobs)
            if target is None:
                reply = "I could not tell which task to stop. Ask me to list your tasks, then say something like 'stop 1' or 'stop <job id>'."
            else:
                stopped_now, signaled = await _stop_jobs_async(state, [target])
                findings = _partial_findings_text(state, target) if _wants_findings_after_stop(query) else ""
                if stopped_now:
                    reply = f"Stopped task {target.job_id[:8]}."
                    if findings:
                        reply += f"\n\nWhat it found so far:\n{findings}"
                    elif _wants_findings_after_stop(query):
                        reply += "\n\nI do not have usable findings to show yet."
                elif signaled:
                    reply = f"I asked the worker to stop task {target.job_id[:8]}."
                    if _wants_findings_after_stop(query):
                        if findings:
                            reply += f"\n\nWhat it found so far:\n{findings}"
                        else:
                            reply += "\n\nI do not have usable findings to show yet, but I will stop it."
                    else:
                        reply += " I will update you when it is interrupted."
                else:
                    reply = "I do not see an active task to stop right now."
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

    if query and latest_status_job is not None and _is_findings_request(query):
        state.record_turn(
            channel="telegram",
            user_id=user_id,
            conversation_id=conversation_id,
            role=ThreadTurnRole.USER,
            text=query,
            task_class=TaskClass.LIGHT,
        )
        if latest_status_job.status in {JobStatus.RUNNING, JobStatus.WAITING_WORKER, JobStatus.QUEUED, JobStatus.WAITING_APPROVAL}:
            await _stop_jobs_async(state, [latest_status_job], note="stopped to present current findings")
        findings = _partial_findings_text(state, latest_status_job)
        if findings:
            reply = f"Here’s what I have so far:\n{findings}"
        else:
            progress = _progress_snapshot_text(state, latest_status_job)
            if progress:
                reply = f"I do not have concrete results yet.\n\nLatest progress:\n{progress}"
            else:
                reply = "I do not have concrete results to share from that run yet."
        state.record_turn(
            channel="telegram",
            user_id=user_id,
            conversation_id=conversation_id,
            role=ThreadTurnRole.ASSISTANT,
            text=reply,
            task_class=TaskClass.LIGHT,
        )
        await TelegramClient(settings).send_message(chat_id, reply)
        return {"status": "findings_reported"}

    paused_job = _latest_paused_input_job_for_pairs(state, owner_pairs)
    thread_active_job = _sync_thread_active_heavy_job(
        state,
        channel="telegram",
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if thread_active_job is not None and thread_active_job.status == JobStatus.PAUSED_FOR_INPUT:
        paused_job = thread_active_job
    clarification_job = None if paused_job is not None else _latest_completed_clarification_job_for_pairs(
        state,
        owner_pairs,
    )
    resume_job = paused_job or clarification_job
    paused_checkpoint = state.get_latest_checkpoint(resume_job.job_id) if resume_job is not None else None
    paused_job_uses_temporal = paused_job is not None and str((paused_job.metadata or {}).get("execution_backend", "")).strip().lower() == "temporal"
    superseded_running_job = False
    if (
        paused_job is not None
        and paused_job_uses_temporal
        and message.document is None
        and query
        and (
            _is_input_reply(query)
            or _natural_reply_can_resume(query, resume_job=paused_job, latest_status_job=latest_status_job)
        )
    ):
        reply_text = _strip_input_reply_prefix(query or "")
        from .temporal_client import signal_answer_heavy_job

        resumed = await signal_answer_heavy_job(settings, paused_job.job_id, reply_text)
        if resumed:
            state.update_job_status(paused_job.job_id, status=JobStatus.RUNNING, current_step="resuming task")
            state.set_active_heavy_job(
                channel="telegram",
                user_id=user_id,
                conversation_id=conversation_id,
                job_id=paused_job.job_id,
            )
            await TelegramClient(settings).send_message(chat_id, "I resumed that and will notify you in Telegram.")
            return {"status": "resumed", "job_id": paused_job.job_id}
    if resume_job is not None:
        if message.document is not None or (
            query
            and (
                _is_input_reply(query)
                or _natural_reply_can_resume(query, resume_job=resume_job, latest_status_job=latest_status_job)
            )
        ):
            effective_query = _build_paused_input_resume_query(
                resume_job,
                paused_checkpoint,
                _strip_input_reply_prefix(query or ""),
                has_attachment=message.document is not None,
            )
            resume_from_job_id = resume_job.job_id
            task_class = TaskClass.HEAVY
            if query:
                state.record_turn(
                    channel="telegram",
                    user_id=user_id,
                    conversation_id=conversation_id,
                    role=ThreadTurnRole.USER,
                    text=query,
                    task_class=task_class,
                )
        else:
            contextual_followup = bool(query) and await _resolve_contextual_heavy_followup(
                query or "",
                latest_job=resume_job,
            )
            if contextual_followup:
                task_class = TaskClass.HEAVY
                resume_from_job_id = resume_job.job_id
                effective_query = _build_contextual_heavy_followup_query(
                    resume_job,
                    paused_checkpoint,
                    query or "",
                )
                state.record_turn(
                    channel="telegram",
                    user_id=user_id,
                    conversation_id=conversation_id,
                    role=ThreadTurnRole.USER,
                    text=query,
                    task_class=task_class,
                )
                _log_followup_resolution(
                    channel="telegram",
                    query=query,
                    latest_job=resume_job,
                    action="continue_paused_task",
                )
            else:
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
                if query and _should_supersede_for_new_heavy_task(query, latest_job=resume_job, task_class=task_class):
                    await _supersede_running_heavy_job(state, resume_job)
                    superseded_running_job = True
                    _log_followup_resolution(
                        channel="telegram",
                        query=query,
                        latest_job=resume_job,
                        action="replace_paused_task_for_new_domain",
                    )
                resume_from_job_id = None
                effective_query = query
    else:
        task_class = classify_task(query or "file task", has_attachment=message.document is not None)
        contextual_followup_job = latest_status_job if query and await _resolve_contextual_heavy_followup(query, latest_job=latest_status_job) else None
        if contextual_followup_job is not None:
            task_class = TaskClass.HEAVY
            if await _signal_running_heavy_followup(contextual_followup_job, query or ""):
                state.record_turn(
                    channel="telegram",
                    user_id=user_id,
                    conversation_id=conversation_id,
                    role=ThreadTurnRole.USER,
                    text=query or "",
                    task_class=task_class,
                )
                state.set_active_heavy_job(
                    channel="telegram",
                    user_id=user_id,
                    conversation_id=conversation_id,
                    job_id=contextual_followup_job.job_id,
                )
                _log_followup_resolution(
                    channel="telegram",
                    query=query or "",
                    latest_job=contextual_followup_job,
                    action="signal_running_task",
                )
                await TelegramClient(settings).send_message(chat_id, "Updated. I'll keep going and message you here.")
                return {"status": "updated", "job_id": contextual_followup_job.job_id}
            if _job_can_be_superseded_by_followup(contextual_followup_job):
                await _supersede_running_heavy_job(state, contextual_followup_job)
                superseded_running_job = True
                _log_followup_resolution(
                    channel="telegram",
                    query=query or "",
                    latest_job=contextual_followup_job,
                    action="replace_running_task",
                )
            else:
                _log_followup_resolution(
                    channel="telegram",
                    query=query or "",
                    latest_job=contextual_followup_job,
                    action="continue_prior_heavy_task",
                )
            resume_from_job_id = contextual_followup_job.job_id
            effective_query = _build_contextual_heavy_followup_query(
                contextual_followup_job,
                state.get_latest_checkpoint(contextual_followup_job.job_id),
                query or "",
            )
        else:
            if query and _should_supersede_for_new_heavy_task(query, latest_job=latest_status_job, task_class=task_class):
                await _supersede_running_heavy_job(state, latest_status_job)
                superseded_running_job = True
                _log_followup_resolution(
                    channel="telegram",
                    query=query,
                    latest_job=latest_status_job,
                    action="replace_running_task_for_new_domain",
                )
            resume_from_job_id = None
            effective_query = query
        if query:
            state.record_turn(
                channel="telegram",
                user_id=user_id,
                conversation_id=conversation_id,
                role=ThreadTurnRole.USER,
                text=query,
                task_class=task_class,
            )
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
            metadata={"execution_backend": "temporal" if temporal_backend_enabled(settings) else "legacy"},
        )
        await _start_heavy_job(state, created_job)
        await TelegramClient(settings).send_message(chat_id, "On it. I'll message you here when I have something useful.")
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
            metadata={"execution_backend": "temporal" if temporal_backend_enabled(settings) else "legacy"},
        )
        await _start_heavy_job(state, job)
        ack = "Updated. I'll keep going and message you here." if superseded_running_job else "On it. I'll message you here when I have something useful."
        await TelegramClient(settings).send_message(chat_id, ack)
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
    owner_pairs = _siri_owner_source_pairs()
    await _active_jobs_for_pairs_async(state, owner_pairs)
    latest_status_job = await _preferred_latest_status_job_for_thread_async(
        state,
        channel="siri",
        user_id="siri",
        conversation_id="siri",
        owner_pairs=owner_pairs,
    )
    if _remember_if_tagged(state, user_id=_auth_owner_key_from_siri(), query=query):
        await _mirror_siri_to_telegram(query, reply="I will remember that.", state=state, task_class=TaskClass.LIGHT)
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
        jobs = await _active_jobs_for_pairs_async(state, owner_pairs)
        status_text = _format_tasks_list(state, jobs)
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id="siri",
            role=ThreadTurnRole.ASSISTANT,
            text=status_text,
            task_class=TaskClass.LIGHT,
        )
        await _mirror_siri_to_telegram(query, reply=status_text, state=state, task_class=TaskClass.LIGHT)
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
            await _preferred_latest_status_job_for_thread_async(
                state,
                channel="siri",
                user_id="siri",
                conversation_id="siri",
                owner_pairs=owner_pairs,
            ),
        )
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id="siri",
            role=ThreadTurnRole.ASSISTANT,
            text=status_text,
            task_class=TaskClass.LIGHT,
        )
        await _mirror_siri_to_telegram(query, reply=status_text, state=state, task_class=TaskClass.LIGHT)
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
        jobs = await _active_jobs_for_pairs_async(state, owner_pairs)
        if _is_stop_all_request(query):
            stopped_now, signaled = await _stop_jobs_async(state, jobs)
            total = stopped_now + signaled
            if total == 0:
                reply = "I do not see any active long-running tasks to stop right now."
            elif signaled and stopped_now:
                reply = f"Stopping {total} tasks now. {stopped_now} were stopped immediately and {signaled} running task{'s were' if signaled != 1 else ' was'} asked to stop."
            elif signaled:
                reply = f"Stopping {signaled} running task{'s' if signaled != 1 else ''} now."
            else:
                reply = f"Stopped {stopped_now} task{'s' if stopped_now != 1 else ''}."
        else:
            target = _resolve_stop_target(query, jobs)
            if target is None:
                reply = "I could not tell which task to stop. Ask me to list your tasks, then say something like stop 1."
            else:
                stopped_now, signaled = await _stop_jobs_async(state, [target])
                findings = _partial_findings_text(state, target) if _wants_findings_after_stop(query) else ""
                if stopped_now:
                    reply = f"Stopped task {target.job_id[:8]}."
                    if findings:
                        reply += f"\n\nWhat it found so far:\n{findings}"
                    elif _wants_findings_after_stop(query):
                        reply += "\n\nI do not have usable findings to show yet."
                elif signaled:
                    reply = f"I asked the worker to stop task {target.job_id[:8]}."
                    if _wants_findings_after_stop(query):
                        if findings:
                            reply += f"\n\nWhat it found so far:\n{findings}"
                        else:
                            reply += "\n\nI do not have usable findings to show yet, but I will stop it."
                else:
                    reply = "I do not see an active task to stop right now."
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id="siri",
            role=ThreadTurnRole.ASSISTANT,
            text=reply,
            task_class=TaskClass.LIGHT,
        )
        await _mirror_siri_to_telegram(query, reply=reply, state=state, task_class=TaskClass.LIGHT)
        return SiriResponse(response=reply)

    if latest_status_job is not None and _is_findings_request(query):
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id="siri",
            role=ThreadTurnRole.USER,
            text=query,
            task_class=TaskClass.LIGHT,
        )
        if latest_status_job.status in {JobStatus.RUNNING, JobStatus.WAITING_WORKER, JobStatus.QUEUED, JobStatus.WAITING_APPROVAL}:
            await _stop_jobs_async(state, [latest_status_job], note="stopped to present current findings")
        findings = _partial_findings_text(state, latest_status_job)
        if findings:
            reply = f"Here’s what I have so far:\n{findings}"
        else:
            progress = _progress_snapshot_text(state, latest_status_job)
            if progress:
                reply = f"I do not have concrete results yet.\n\nLatest progress:\n{progress}"
            else:
                reply = "I do not have concrete results to share from that run yet."
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id="siri",
            role=ThreadTurnRole.ASSISTANT,
            text=reply,
            task_class=TaskClass.LIGHT,
        )
        await _mirror_siri_to_telegram(query, reply=reply, state=state, task_class=TaskClass.LIGHT)
        return SiriResponse(response=reply)

    allowed_chat_id = settings.secret(settings.telegram_allowed_chat_id_param)
    conversation_id = "siri"
    paused_job = _latest_paused_input_job_for_pairs(state, owner_pairs)
    thread_active_job = _sync_thread_active_heavy_job(
        state,
        channel="siri",
        user_id="siri",
        conversation_id=conversation_id,
    )
    if thread_active_job is not None and thread_active_job.status == JobStatus.PAUSED_FOR_INPUT:
        paused_job = thread_active_job
    clarification_job = None if paused_job is not None else _latest_completed_clarification_job_for_pairs(
        state,
        owner_pairs,
    )
    resume_job = paused_job or clarification_job
    paused_checkpoint = state.get_latest_checkpoint(resume_job.job_id) if resume_job is not None else None
    paused_job_uses_temporal = paused_job is not None and str((paused_job.metadata or {}).get("execution_backend", "")).strip().lower() == "temporal"
    superseded_running_job = False
    if paused_job is not None and paused_job_uses_temporal and (
        _is_input_reply(query)
        or _natural_reply_can_resume(query, resume_job=paused_job, latest_status_job=latest_status_job)
    ):
        from .temporal_client import signal_answer_heavy_job

        resumed = await signal_answer_heavy_job(settings, paused_job.job_id, _strip_input_reply_prefix(query))
        if resumed:
            state.update_job_status(paused_job.job_id, status=JobStatus.RUNNING, current_step="resuming task")
            state.set_active_heavy_job(
                channel="siri",
                user_id="siri",
                conversation_id=conversation_id,
                job_id=paused_job.job_id,
            )
            reply = "I resumed that and will notify you in Telegram."
            await _mirror_siri_to_telegram(query, reply=reply, state=state, task_class=TaskClass.HEAVY)
            return SiriResponse(response=reply, queued=True, job_id=paused_job.job_id)
    if resume_job is not None:
        if _is_input_reply(query) or _natural_reply_can_resume(query, resume_job=resume_job, latest_status_job=latest_status_job):
            task_class = TaskClass.HEAVY
            resume_from_job_id = resume_job.job_id
            effective_query = _build_paused_input_resume_query(
                resume_job,
                paused_checkpoint,
                _strip_input_reply_prefix(query),
            )
        else:
            if await _resolve_contextual_heavy_followup(query, latest_job=resume_job):
                task_class = TaskClass.HEAVY
                resume_from_job_id = resume_job.job_id
                effective_query = _build_contextual_heavy_followup_query(
                    resume_job,
                    paused_checkpoint,
                    query,
                )
                _log_followup_resolution(
                    channel="siri",
                    query=query,
                    latest_job=resume_job,
                    action="continue_paused_task",
                )
            else:
                task_class = classify_task(query)
                if _should_supersede_for_new_heavy_task(query, latest_job=resume_job, task_class=task_class):
                    await _supersede_running_heavy_job(state, resume_job)
                    superseded_running_job = True
                    _log_followup_resolution(
                        channel="siri",
                        query=query,
                        latest_job=resume_job,
                        action="replace_paused_task_for_new_domain",
                    )
                resume_from_job_id = None
                effective_query = query
    else:
        task_class = classify_task(query)
        contextual_followup_job = latest_status_job if await _resolve_contextual_heavy_followup(query, latest_job=latest_status_job) else None
        if contextual_followup_job is not None:
            task_class = TaskClass.HEAVY
            if await _signal_running_heavy_followup(contextual_followup_job, query):
                state.record_turn(
                    channel="siri",
                    user_id="siri",
                    conversation_id=conversation_id,
                    role=ThreadTurnRole.USER,
                    text=query,
                    task_class=task_class,
                )
                state.set_active_heavy_job(
                    channel="siri",
                    user_id="siri",
                    conversation_id=conversation_id,
                    job_id=contextual_followup_job.job_id,
                )
                _log_followup_resolution(
                    channel="siri",
                    query=query,
                    latest_job=contextual_followup_job,
                    action="signal_running_task",
                )
                reply = "Updated. I'll keep going and message you here."
                await _mirror_siri_to_telegram(query, reply=reply, state=state, task_class=TaskClass.HEAVY)
                return SiriResponse(response=reply, queued=True, job_id=contextual_followup_job.job_id)
            if _job_can_be_superseded_by_followup(contextual_followup_job):
                await _supersede_running_heavy_job(state, contextual_followup_job)
                superseded_running_job = True
                _log_followup_resolution(
                    channel="siri",
                    query=query,
                    latest_job=contextual_followup_job,
                    action="replace_running_task",
                )
            else:
                _log_followup_resolution(
                    channel="siri",
                    query=query,
                    latest_job=contextual_followup_job,
                    action="continue_prior_heavy_task",
                )
            resume_from_job_id = contextual_followup_job.job_id
            effective_query = _build_contextual_heavy_followup_query(
                contextual_followup_job,
                state.get_latest_checkpoint(contextual_followup_job.job_id),
                query,
            )
        else:
            if _should_supersede_for_new_heavy_task(query, latest_job=latest_status_job, task_class=task_class):
                await _supersede_running_heavy_job(state, latest_status_job)
                superseded_running_job = True
                _log_followup_resolution(
                    channel="siri",
                    query=query,
                    latest_job=latest_status_job,
                    action="replace_running_task_for_new_domain",
                )
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
            metadata={"execution_backend": "temporal" if temporal_backend_enabled(settings) else "legacy"},
        )
        await _start_heavy_job(state, job)
        ack = "Updated. I'll keep going and message you here." if superseded_running_job else "On it. I'll message you in Telegram."
        await _mirror_siri_to_telegram(query, reply=ack, state=state, task_class=TaskClass.HEAVY)
        return SiriResponse(response=ack, queued=True, job_id=job.job_id)

    state.record_turn(
        channel="siri",
        user_id="siri",
        conversation_id=conversation_id,
        role=ThreadTurnRole.USER,
        text=query,
        task_class=task_class,
    )

    context_summary, recent_turns, memories, config = _build_light_context_for_query(
        state,
        channel="siri",
        user_id="siri",
        conversation_id=conversation_id,
        query=query,
    )
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
        cleaned_result = _clean_user_facing_result(result.text)
        state.record_turn(
            channel="siri",
            user_id="siri",
            conversation_id=conversation_id,
            role=ThreadTurnRole.ASSISTANT,
            text=cleaned_result,
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
                metadata={"execution_backend": "temporal" if temporal_backend_enabled(settings) else "legacy"},
            )
            await _start_heavy_job(state, job)
            reply = "On it. I'll message you in Telegram."
            await _mirror_siri_to_telegram(query, reply=reply, state=state, task_class=TaskClass.HEAVY)
            return SiriResponse(response=reply, queued=True, job_id=job.job_id)
        await _mirror_siri_to_telegram(query, reply=cleaned_result, state=state, task_class=TaskClass.LIGHT)
        return SiriResponse(response=cleaned_result)
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
            metadata={"execution_backend": "temporal" if temporal_backend_enabled(settings) else "legacy"},
        )
        await _start_heavy_job(state, job)
        reply = "On it. I'll message you in Telegram."
        await _mirror_siri_to_telegram(query, reply=reply, state=state, task_class=TaskClass.HEAVY)
        return SiriResponse(response=reply, queued=True, job_id=job.job_id)


@app.post("/internal/worker/claim", response_model=WorkerClaimResponse)
async def worker_claim(request: Request, x_friday_worker_key: Optional[str] = Header(default=None)) -> WorkerClaimResponse:
    expected_key = settings.secret(settings.worker_api_key_param)
    if expected_key and x_friday_worker_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid worker key")
    if temporal_backend_enabled(settings):
        return WorkerClaimResponse(ok=False)

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
    if temporal_backend_enabled(settings):
        return WorkerClaimResponse(ok=False)

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
    if not _job_accepts_live_worker_updates(job):
        return {"status": "ignored"}
    updated_job = state.update_job_heartbeat(body.job_id, current_step=body.current_step, summary=body.summary)
    config = state.get_config()
    if state.should_stop_for_stall(body.job_id, interval_seconds=config.status_update_interval_seconds):
        state.record_control_signal(
            body.job_id,
            command=ControlCommand.STOP,
            note="auto-stopped after repeated identical worker heartbeats with no meaningful progress",
        )
        state.mark_loop_stop_requested(body.job_id)
        if updated_job.chat_id:
            message = _stall_stop_user_message()
            _record_visible_assistant_turn(state, updated_job, message, task_class=TaskClass.HEAVY)
            await TelegramClient(settings).send_message(
                updated_job.chat_id,
                message,
            )
        return {"status": "stall_stop_requested"}
    if body.notify and job.chat_id:
        message = progress_notification_text(updated_job, current_step=body.current_step, summary=body.summary)
        if state.should_send_status_update(body.job_id, interval_seconds=config.status_update_interval_seconds, text=message):
            _record_visible_assistant_turn(state, updated_job, message, task_class=TaskClass.HEAVY)
            await TelegramClient(settings).send_message(job.chat_id, message)
            state.mark_status_update_sent(body.job_id, text=message)
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


@app.get("/internal/worker/job-status/{job_id}")
async def worker_job_status(job_id: str, x_friday_worker_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.worker_api_key_param)
    if expected_key and x_friday_worker_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid worker key")
    job = store().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"status": job.status.value}


@app.post("/internal/worker/checkpoint")
async def worker_checkpoint(body: WorkerCheckpointRequest, x_friday_worker_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.worker_api_key_param)
    if expected_key and x_friday_worker_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid worker key")
    state = store()
    job = state.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_accepts_live_worker_updates(job):
        return {"status": "ignored", "checkpoint_seq": 0}
    seq = state.save_checkpoint(body.job_id, body.checkpoint)
    return {"status": "ok", "checkpoint_seq": seq}


@app.post("/internal/worker/pause")
async def worker_pause(body: WorkerPauseRequest, x_friday_worker_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    expected_key = settings.secret(settings.worker_api_key_param)
    if expected_key and x_friday_worker_key != expected_key:
        raise HTTPException(status_code=401, detail="invalid worker key")
    state = store()
    job = state.get_job(body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not _job_accepts_live_worker_updates(job):
        return {"status": "ignored", "checkpoint_seq": 0}
    seq = state.save_checkpoint(body.job_id, body.checkpoint)
    state.update_job_status(body.job_id, status=JobStatus.PAUSED_FOR_INPUT, current_step=body.checkpoint.current_step or "waiting_for_user_input")
    if job.user_id and job.conversation_id:
        state.set_active_heavy_job(
            channel=job.source.value,
            user_id=job.user_id,
            conversation_id=job.conversation_id,
            job_id=job.job_id,
        )
    message = _paused_input_reply_text(state, state.get_job(body.job_id) or job)
    _record_visible_assistant_turn(state, job, message, task_class=TaskClass.HEAVY)
    if job.chat_id:
        await TelegramClient(settings).send_message(job.chat_id, message)
    _maybe_stop_dedicated_worker_if_idle(state)
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
    if not _job_accepts_live_worker_updates(job):
        return {"status": "ignored"}
    cleaned_result = _clean_user_facing_result(body.result_text)
    clarification_candidate = job.model_copy(update={"result_preview": cleaned_result})
    if _job_result_looks_like_booking_clarification(clarification_candidate):
        input_question, input_details = _booking_clarification_prompt_from_result(cleaned_result)
        state.save_checkpoint(
            body.job_id,
            CheckpointPayload(
                summary="waiting for your choice between the available booking options",
                current_step="waiting_for_user_input",
                resume_instructions="Use the user's selected booking option to continue the same reservation flow without asking again for the same choice.",
                metadata={
                    "input_question": input_question,
                    "input_details": input_details,
                },
            ),
        )
        state.update_job_status(
            body.job_id,
            status=JobStatus.PAUSED_FOR_INPUT,
            current_step="waiting_for_user_input",
            result_preview=cleaned_result,
            output_files=body.output_files,
            artifact_keys=body.artifact_keys,
        )
        paused_job = state.get_job(body.job_id)
        user_message = _paused_input_reply_text(state, paused_job) if paused_job is not None else cleaned_result
        _record_visible_assistant_turn(state, job, user_message, task_class=TaskClass.HEAVY)
        if job.chat_id:
            await TelegramClient(settings).send_message(job.chat_id, user_message)
        return {"status": "paused_for_input"}
    state.update_job_status(
        body.job_id,
        status=JobStatus.COMPLETED,
        current_step="completed",
        result_preview=cleaned_result,
        output_files=body.output_files,
        artifact_keys=body.artifact_keys,
    )
    _clear_thread_active_heavy_job_if_matches(state, job)
    _record_visible_assistant_turn(state, job, cleaned_result, task_class=TaskClass.HEAVY)
    if job.chat_id:
        include_browser_step_screenshots = query_requests_browser_images(job.query)
        include_requested_output_files = query_requests_output_files(job.query)
        visible_files = visible_output_files(
            body.output_files,
            include_browser_step_screenshots=include_browser_step_screenshots,
            include_other_output_files=include_requested_output_files,
        )
        message = cleaned_result
        if visible_files:
            message += "\n\nOutput files:\n" + "\n".join(f"- {name}" for name in visible_files[:20])
        telegram = TelegramClient(settings)
        await telegram.send_message(job.chat_id, message)
        zipped_screenshots: list[tuple[str, bytes]] = []
        for file_name, object_key in zip(body.output_files[:10], body.artifact_keys[:10]):
            if is_browser_step_screenshot(file_name):
                if not include_browser_step_screenshots:
                    continue
                response = s3_client().get_object(Bucket=settings.artifacts_bucket, Key=object_key)
                zipped_screenshots.append((file_name.rsplit("/", 1)[-1], response["Body"].read()))
                continue
            if not include_requested_output_files:
                continue
            response = s3_client().get_object(Bucket=settings.artifacts_bucket, Key=object_key)
            content = response["Body"].read()
            await telegram.send_document_bytes(job.chat_id, file_name, content, caption=file_name)
        if len(zipped_screenshots) == 1:
            screenshot_name, screenshot_bytes = zipped_screenshots[0]
            await telegram.send_document_bytes(job.chat_id, screenshot_name, screenshot_bytes, caption=screenshot_name)
        elif zipped_screenshots:
            zip_name = _zip_artifact_bundle_name(job)
            zip_bytes = _build_zip_bytes(zipped_screenshots)
            await telegram.send_document_bytes(job.chat_id, zip_name, zip_bytes, caption=zip_name)
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
    if not _job_accepts_live_worker_updates(job):
        return {"status": "ignored"}
    status = JobStatus.FAILED
    if body.interrupted:
        status = JobStatus.INTERRUPTED
    elif body.timed_out:
        status = JobStatus.TIMED_OUT
    user_error = _humanize_worker_failure(job.query, body.error_message, status)
    state.update_job_status(body.job_id, status=status, current_step="failed", error_message=user_error)
    _clear_thread_active_heavy_job_if_matches(state, job)
    _record_visible_assistant_turn(state, job, user_error, task_class=TaskClass.HEAVY)
    if job.chat_id:
        await TelegramClient(settings).send_message(job.chat_id, user_error)
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
