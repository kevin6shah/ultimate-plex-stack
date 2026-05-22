from __future__ import annotations

import asyncio
import io
import json
import hashlib
import hmac
import logging
import re
import zipfile
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Optional
from urllib.parse import quote
from uuid import uuid4

import boto3
from fastapi import FastAPI, Header, HTTPException, Request
from mangum import Mangum
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
    ArtifactUploadRequest,
    AttachmentRef,
    CheckpointPayload,
    ControlCommand,
    DashboardSessionRecord,
    JobSource,
    JobStatus,
    MailboxWatchState,
    SaveConfigRequest,
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
from .routing import classify_task, is_long_task, task_routing_profile
from .settings import settings
from .storage import StateStore
from .temporal_runtime import temporal_backend_enabled
from .telegram import TelegramClient, parse_telegram_update
from .worker_lifecycle import ensure_dedicated_worker_running as shared_ensure_dedicated_worker_running
from .worker_lifecycle import maybe_stop_dedicated_worker_if_idle as shared_maybe_stop_dedicated_worker_if_idle
from .heavy_job_runtime import progress_notification_text

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
    return cleaned or normalized


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
        return "This task was interrupted before it finished. Say 'resume that task' if you want me to continue from the last checkpoint."
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
        "stopped by user": "stopped by you",
        "approval received": "approval received",
        "waiting_worker": "waiting for the worker",
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
    if normalized.endswith("?"):
        return False
    if classify_task(normalized) == TaskClass.HEAVY:
        return False
    if task_routing_profile(normalized).name != "general":
        return False
    return len(normalized) <= 220


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


def _has_useful_partial_findings(summary: str) -> bool:
    normalized = _plain_text_message(summary or "").strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    generic_markers = (
        "attachments downloaded",
        "working through website steps",
        "working through the task",
        "running agent",
        "workspace prepared",
        "agent completed",
        "interrupted: working through",
    )
    return not any(marker in lowered for marker in generic_markers)


def _partial_findings_text(state: StateStore, job: AgentJob) -> str:
    checkpoint = state.get_latest_checkpoint(job.job_id)
    candidates = [
        job.result_preview or "",
        job.latest_checkpoint_summary or "",
        checkpoint.summary if checkpoint else "",
    ]
    for candidate in candidates:
        cleaned = _clean_user_facing_result(candidate)
        if _has_useful_partial_findings(cleaned):
            return cleaned[:1200]
    return ""


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
        elif status_name == "TIMED_OUT":
            state.update_job_status(
                job.job_id,
                status=JobStatus.TIMED_OUT,
                current_step="workflow timed out",
                error_message="The workflow timed out before the task finished.",
            )
        elif status_name == "FAILED":
            if _checkpoint_indicates_interruption(latest_summary):
                state.update_job_status(
                    job.job_id,
                    status=JobStatus.INTERRUPTED,
                    current_step=job.current_step or (checkpoint.current_step if checkpoint else "") or "interrupted",
                    error_message="This task was interrupted before it finished.",
                )
            else:
                state.update_job_status(
                    job.job_id,
                    status=JobStatus.FAILED,
                    current_step="workflow failed",
                    error_message="The workflow failed before the task finished.",
                )
        elif status_name == "COMPLETED":
            if (job.result_preview or "").strip() or job.output_files or job.artifact_keys:
                state.update_job_status(
                    job.job_id,
                    status=JobStatus.COMPLETED,
                    current_step="completed",
                    error_message="",
                )
            elif _job_indicates_user_stop(job, latest_summary):
                state.update_job_status(
                    job.job_id,
                    status=JobStatus.INTERRUPTED,
                    current_step=job.current_step or (checkpoint.current_step if checkpoint else "") or "stopped by user",
                    error_message=job.error_message or "stopped by user",
                )
            else:
                state.update_job_status(
                    job.job_id,
                    status=JobStatus.INTERRUPTED,
                    current_step="workflow finished without persisting a final result",
                    error_message="The workflow finished internally, but its final result was not persisted cleanly.",
                )
        refreshed_job = state.get_job(job.job_id)
        if refreshed_job is not None:
            status_updates[job.job_id] = refreshed_job
    return [status_updates.get(job.job_id, job) for job in refreshed]


def _job_sort_timestamp(job: AgentJob) -> str:
    return job.created_at or job.updated_at or ""


async def _latest_status_job_for_user_async(state: StateStore, *, source: JobSource, user_id: str) -> Optional[AgentJob]:
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
    active_jobs = await _reconcile_temporal_jobs(
        state,
        state.list_jobs_for_user(source=source.value, user_id=user_id, statuses=active_statuses, limit=10),
    )
    recent_statuses = (
        JobStatus.COMPLETED,
        JobStatus.FAILED,
    )
    recent_jobs = state.list_jobs_for_user(source=source.value, user_id=user_id, statuses=recent_statuses, limit=10)
    candidates = active_jobs + recent_jobs
    if not candidates:
        return None
    ordered = sorted(candidates, key=_job_sort_timestamp, reverse=True)
    return ordered[0]


def _active_jobs_for_user(state: StateStore, *, source: JobSource, user_id: str) -> list[AgentJob]:
    raise RuntimeError("use async variant")


async def _active_jobs_for_user_async(state: StateStore, *, source: JobSource, user_id: str) -> list[AgentJob]:
    jobs = state.list_jobs_for_user(
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
    refreshed = await _reconcile_temporal_jobs(state, jobs)
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
    summary = _plain_text_message(job.latest_checkpoint_summary or (checkpoint.summary if checkpoint else "") or "")
    step = _humanize_step(job.current_step or (checkpoint.current_step if checkpoint else "") or "")
    question = _plain_text_message(question)
    details = _plain_text_message(details)

    lines = ["Your latest task is paused and waiting for your input."]
    if question:
        lines.extend(["", "What I need:", question])
    if details:
        lines.extend(["", "Details:", details])
    status_lines: list[str] = []
    if step:
        status_lines.append(f"Current step: {step}")
    if summary:
        status_lines.append(f"Latest update: {summary}")
    if status_lines:
        lines.extend(["", "Status:"])
        lines.extend(status_lines)
    lines.extend(
        [
            "",
            "Reply with 'answer: ...' to continue.",
            "If you want to start something new instead, just ask normally.",
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
    lines = ["Here are your current long-running tasks:"]
    for index, job in enumerate(jobs, start=1):
        summary = _plain_text_message(job.latest_checkpoint_summary or "")
        step = _humanize_step(job.current_step or "")
        line = f"{index}. {job.job_id[:8]} - {_humanize_status(job.status)}"
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
            stopped_now += 1
            continue
        if job.status in {JobStatus.QUEUED, JobStatus.WAITING_WORKER, JobStatus.WAITING_APPROVAL, JobStatus.PAUSED_FOR_INPUT}:
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step="stopped by user",
                error_message="stopped by user",
            )
            stopped_now += 1
        elif job.status == JobStatus.RUNNING:
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step="stopped by user",
                error_message="stopped by user",
            )
            state.record_control_signal(job.job_id, command=ControlCommand.STOP, note="stopped by user")
            stopped_now += 1
            signaled += 1
    return stopped_now, signaled


async def _stop_jobs_async(state: StateStore, jobs: list[AgentJob]) -> tuple[int, int]:
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
            stopped_now += 1
            continue
        if job.status in {JobStatus.QUEUED, JobStatus.WAITING_WORKER, JobStatus.WAITING_APPROVAL, JobStatus.PAUSED_FOR_INPUT}:
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step="stopped by user",
                error_message="stopped by user",
            )
            if uses_temporal:
                from .temporal_client import signal_stop_heavy_job

                signaled += 1 if await signal_stop_heavy_job(settings, job.job_id, "stopped by user") else 0
            stopped_now += 1
        elif job.status == JobStatus.RUNNING:
            state.update_job_status(
                job.job_id,
                status=JobStatus.INTERRUPTED,
                current_step="stopped by user",
                error_message="stopped by user",
            )
            if uses_temporal:
                from .temporal_client import signal_stop_heavy_job

                signaled += 1 if await signal_stop_heavy_job(settings, job.job_id, "stopped by user") else 0
            else:
                state.record_control_signal(job.job_id, command=ControlCommand.STOP, note="stopped by user")
                signaled += 1
            stopped_now += 1
    return stopped_now, signaled


def _format_status_message(state: StateStore, job: Optional[AgentJob]) -> str:
    if job is None:
        return "I do not see a recent long-running task to report on."
    checkpoint = state.get_latest_checkpoint(job.job_id)
    summary = _plain_text_message(job.latest_checkpoint_summary or (checkpoint.summary if checkpoint else "") or "")
    step = _humanize_step(job.current_step or (checkpoint.current_step if checkpoint else "") or "")
    step_line = f"\nCurrent step: {step}" if step else ""
    summary_line = f"\nLatest update: {summary}" if summary else ""
    if job.status in {JobStatus.QUEUED, JobStatus.WAITING_WORKER}:
        return f"Your latest task is queued.{step_line}{summary_line}".strip()
    if job.status == JobStatus.RUNNING:
        if _checkpoint_indicates_interruption(summary):
            tail = "\nSay 'resume that task' if you want me to continue from the last checkpoint."
            return f"Your latest task appears interrupted while I reconcile its final state.{step_line}{summary_line}{tail}".strip()
        return f"Still working on your latest task.{step_line}{summary_line}".strip()
    if job.status == JobStatus.WAITING_APPROVAL:
        return f"Your latest task is waiting for approval.{step_line}{summary_line}".strip()
    if job.status == JobStatus.PAUSED_FOR_INPUT:
        return _paused_input_reply_text(state, job)
    if job.status in {JobStatus.CHECKPOINTED, JobStatus.INTERRUPTED, JobStatus.TIMED_OUT}:
        tail = " Say 'resume that task' when you want me to continue."
        return f"Your latest task is {_humanize_status(job.status)}.{step_line}{summary_line}{tail}".strip()
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
    return f"Your latest task is {_humanize_status(job.status)}.{step_line}{summary_line}".strip()


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
            metadata={"execution_backend": "temporal" if temporal_backend_enabled(settings) else "legacy"},
        )
        await _start_heavy_job(state, heavy_job)
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
    if job.status in {JobStatus.QUEUED, JobStatus.WAITING_WORKER, JobStatus.WAITING_APPROVAL, JobStatus.PAUSED_FOR_INPUT}:
        state.update_job_status(job_id, status=JobStatus.INTERRUPTED, current_step="stopped by user", error_message="stopped by user")
        return {"status": "ok"}
    state.record_control_signal(job_id, command=ControlCommand.STOP, note="stopped by user")
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
        identities = [record.model_dump() for record in state.list_identities(limit=100)]
        body = logout_html + "<section class='panel'><h2>Identities</h2>" + _html_table(
            ["Label", "Email", "Provider", "Category", "Site scope", "Default", "Status"],
            _dashboard_record_rows(
                identities,
                fields=["label", "email", "provider", "category", "site_scope", "is_default", "status"],
            ),
        ) + "</section>"
        return HTMLResponse(_dashboard_shell(title="Friday Identities", active_view="identities", body_html=body, session_name=session_name))
    if view_name == "policies":
        policies = [record.model_dump() for record in state.list_automation_policies(limit=100)]
        body = logout_html + "<section class='panel'><h2>Policies</h2>" + _html_table(
            ["Label", "Site scope", "Category", "$0 booking", "Account creation", "Login reuse", "Pause on SMS/CAPTCHA"],
            _dashboard_record_rows(
                policies,
                fields=[
                    "label",
                    "site_scope",
                    "category",
                    "allow_zero_dollar_booking",
                    "allow_account_creation",
                    "allow_login_reuse",
                    "pause_on_sms_or_captcha",
                ],
            ),
        ) + "</section>"
        return HTMLResponse(_dashboard_shell(title="Friday Policies", active_view="policies", body_html=body, session_name=session_name))
    if view_name == "sessions":
        sessions = [record.model_dump() for record in state.list_browser_sessions(limit=100)]
        body = logout_html + "<section class='panel'><h2>Saved browser sessions</h2>" + _html_table(
            ["Session", "Site scope", "Identity", "User agent", "Viewport", "Status", "Updated"],
            [
                [
                    escape(record["session_id"][:8]),
                    escape(record.get("site_scope", "")),
                    escape(record.get("identity_id", "")),
                    escape((record.get("user_agent", "") or "")[:50]),
                    escape(f'{record.get("viewport_width", 0)}x{record.get("viewport_height", 0)}'),
                    escape(record.get("status", "")),
                    escape(record.get("updated_at", "")),
                ]
                for record in sessions
            ],
        ) + "</section>"
        return HTMLResponse(_dashboard_shell(title="Friday Sessions", active_view="sessions", body_html=body, session_name=session_name))
    if view_name == "payments":
        payments = [record.model_dump() for record in state.list_payment_profiles(limit=100)]
        body = (
            logout_html
            + "<section class='panel'><h2>Payment metadata</h2><p class='muted'>Phase 1 keeps this metadata visible but runtime booking remains blocked to $0 only.</p>"
            + _html_table(
                ["Label", "Provider", "Last4", "Limit cents", "Active", "Notes"],
                _dashboard_record_rows(
                    payments,
                    fields=["label", "provider", "masked_last4", "limit_cents", "active", "notes"],
                ),
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

    mailbox_email = _resolved_optional_secret(settings.gmail_account_email_param)
    watch_state = _mailbox_watch_state(state).model_copy(
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
        history_id=event.history_id,
    )
    waits = state.list_active_mailbox_waits(limit=100)
    matched_waits = 0
    signaled = 0

    from .temporal_client import signal_submit_verification_code

    for gmail_message_id in message_ids:
        message = await gmail_api_get(
            settings,
            f"/messages/{quote(gmail_message_id)}",
            access_token=token_payload["access_token"],
            params={"format": "full"},
        )
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
    if not state.claim_telegram_update(chat_id=chat_id, update_id=update.update_id, user_id=user_id):
        logger.info("ignoring duplicate telegram update chat_id=%s update_id=%s", chat_id, update.update_id)
        return {"status": "duplicate_ignored"}
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
        jobs = await _active_jobs_for_user_async(state, source=JobSource.TELEGRAM, user_id=user_id)
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
            await _latest_status_job_for_user_async(state, source=JobSource.TELEGRAM, user_id=user_id),
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
        jobs = await _active_jobs_for_user_async(state, source=JobSource.TELEGRAM, user_id=user_id)
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

    paused_job = _latest_paused_input_job_for_user(state, source=JobSource.TELEGRAM, user_id=user_id)
    paused_checkpoint = state.get_latest_checkpoint(paused_job.job_id) if paused_job is not None else None
    paused_job_uses_temporal = paused_job is not None and str((paused_job.metadata or {}).get("execution_backend", "")).strip().lower() == "temporal"
    if (
        paused_job is not None
        and paused_job_uses_temporal
        and message.document is None
        and query
        and (_is_input_reply(query) or _looks_like_natural_input_reply(query))
    ):
        reply_text = _strip_input_reply_prefix(query or "")
        from .temporal_client import signal_answer_heavy_job

        resumed = await signal_answer_heavy_job(settings, paused_job.job_id, reply_text)
        if resumed:
            state.update_job_status(paused_job.job_id, status=JobStatus.RUNNING, current_step="resuming task")
            await TelegramClient(settings).send_message(chat_id, "I resumed that and will notify you in Telegram.")
            return {"status": "resumed", "job_id": paused_job.job_id}
    if paused_job is not None:
        if message.document is not None or (query and (_is_input_reply(query) or _looks_like_natural_input_reply(query))):
            effective_query = _build_paused_input_resume_query(
                paused_job,
                paused_checkpoint,
                _strip_input_reply_prefix(query or ""),
                has_attachment=message.document is not None,
            )
            resume_from_job_id = paused_job.job_id
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
            metadata={"execution_backend": "temporal" if temporal_backend_enabled(settings) else "legacy"},
        )
        await _start_heavy_job(state, created_job)
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
            metadata={"execution_backend": "temporal" if temporal_backend_enabled(settings) else "legacy"},
        )
        await _start_heavy_job(state, job)
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
        jobs = await _active_jobs_for_user_async(state, source=JobSource.SIRI, user_id="siri")
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
            await _latest_status_job_for_user_async(state, source=JobSource.SIRI, user_id="siri"),
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
        jobs = await _active_jobs_for_user_async(state, source=JobSource.SIRI, user_id="siri")
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
        return SiriResponse(response=reply)

    allowed_chat_id = settings.secret(settings.telegram_allowed_chat_id_param)
    conversation_id = "siri"
    paused_job = _latest_paused_input_job_for_user(state, source=JobSource.SIRI, user_id="siri")
    paused_checkpoint = state.get_latest_checkpoint(paused_job.job_id) if paused_job is not None else None
    paused_job_uses_temporal = paused_job is not None and str((paused_job.metadata or {}).get("execution_backend", "")).strip().lower() == "temporal"
    if paused_job is not None and paused_job_uses_temporal and (_is_input_reply(query) or _looks_like_natural_input_reply(query)):
        from .temporal_client import signal_answer_heavy_job

        resumed = await signal_answer_heavy_job(settings, paused_job.job_id, _strip_input_reply_prefix(query))
        if resumed:
            state.update_job_status(paused_job.job_id, status=JobStatus.RUNNING, current_step="resuming task")
            return SiriResponse(response="I resumed that and will notify you in Telegram.", queued=True, job_id=paused_job.job_id)
    if paused_job is not None:
        if _is_input_reply(query) or _looks_like_natural_input_reply(query):
            task_class = TaskClass.HEAVY
            resume_from_job_id = paused_job.job_id
            effective_query = _build_paused_input_resume_query(
                paused_job,
                paused_checkpoint,
                _strip_input_reply_prefix(query),
            )
        else:
            task_class = classify_task(query)
            resume_from_job_id = None
            effective_query = query
    else:
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
            metadata={"execution_backend": "temporal" if temporal_backend_enabled(settings) else "legacy"},
        )
        await _start_heavy_job(state, job)
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
                metadata={"execution_backend": "temporal" if temporal_backend_enabled(settings) else "legacy"},
            )
            await _start_heavy_job(state, job)
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
            metadata={"execution_backend": "temporal" if temporal_backend_enabled(settings) else "legacy"},
        )
        await _start_heavy_job(state, job)
        return SiriResponse(response="I started that and will notify you in Telegram.", queued=True, job_id=job.job_id)


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
            await TelegramClient(settings).send_message(
                updated_job.chat_id,
                "I stopped this task because it appeared stuck on the same step without meaningful progress. "
                "If you want, I can resume it or try a different approach.",
            )
        return {"status": "stall_stop_requested"}
    if body.notify and job.chat_id:
        message = progress_notification_text(updated_job, current_step=body.current_step, summary=body.summary)
        if state.should_send_status_update(body.job_id, interval_seconds=config.status_update_interval_seconds, text=message):
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
    message = _paused_input_reply_text(state, state.get_job(body.job_id) or job)
    state.record_turn(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
        role=ThreadTurnRole.ASSISTANT,
        text=message,
        task_class=TaskClass.HEAVY,
    )
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
    state.update_job_status(
        body.job_id,
        status=JobStatus.COMPLETED,
        current_step="completed",
        result_preview=cleaned_result,
        output_files=body.output_files,
        artifact_keys=body.artifact_keys,
    )
    state.record_turn(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
        role=ThreadTurnRole.ASSISTANT,
        text=cleaned_result,
        task_class=TaskClass.HEAVY,
    )
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
    state.record_turn(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
        role=ThreadTurnRole.ASSISTANT,
        text=user_error,
        task_class=TaskClass.HEAVY,
    )
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
