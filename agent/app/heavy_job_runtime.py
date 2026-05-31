from __future__ import annotations

import io
import re
import zipfile
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import quote

from .artifacts import (
    is_browser_step_screenshot,
    query_requests_browser_images,
    query_requests_output_files,
    visible_output_files,
)
from .jobs import AgentJob, CheckpointPayload, JobStatus, TaskClass, ThreadTurnRole
from .routing import query_domain_tags, query_domains_compatible
from .schemas.execution_state import JobContext
from .settings import Settings
from .storage import StateStore
from .strategy_runtime import default_strategy_state, normalize_execution_progress_matrix, normalize_strategy_state
from .telegram import TelegramClient


def artifact_key(job_id: str, category: str, file_name: str) -> str:
    safe_name = quote(file_name, safe="/._-")
    return f"jobs/{job_id}/{category}/{safe_name}"


def plain_text_message(text: str) -> str:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"<\|.*?\|>", "", normalized)
    normalized = re.sub(r"</?tool_calls?>", "", normalized)
    normalized = re.sub(r"</?invoke[^>]*>", "", normalized)
    normalized = re.sub(r"</?parameter[^>]*>", "", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def clean_user_facing_result(text: str) -> str:
    cleaned = plain_text_message(text)
    if cleaned.lower().startswith("task failed:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    return cleaned


def has_useful_partial_findings(text: str) -> bool:
    normalized = plain_text_message(text or "").strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    generic_markers = (
        "# iata resolution",
        "attachments downloaded",
        "working through website steps",
        "working through the task",
        "running agent",
        "workspace prepared",
        "agent completed",
        "interrupted: working through",
        "checking reservation sources and matching the correct venue",
        "checking live flight options and collecting candidate itineraries",
        "collecting hotel candidates with live pricing and location details",
        "resolved airport code:",
        "original input:",
    )
    return not any(marker in lowered for marker in generic_markers)


def extract_partial_findings_block(text: str) -> str:
    normalized = plain_text_message(text or "")
    if not normalized:
        return ""
    for marker in ("PARTIAL_STAGEHAND_FINDINGS:", "PARTIAL_BROWSER_FINDINGS:"):
        if marker not in normalized:
            continue
        _, _, remainder = normalized.partition(marker)
        lines: list[str] = []
        for raw_line in remainder.splitlines():
            line = plain_text_message(raw_line).strip()
            if not line:
                if lines:
                    break
                continue
            lowered = line.lower()
            if lowered.startswith("stagehand issue:") or lowered.startswith("primary browser issue:"):
                break
            if lowered.startswith("the lightweight browser fallback could not verify more details"):
                break
            lines.append(line[:300])
        if lines:
            return "\n".join(lines[:6])
    return ""


def summarize_recoverable_failure(query: str, error_message: str, strategy_name: str = "") -> str:
    normalized = plain_text_message(error_message or "")
    lowered = normalized.lower()
    query_lowered = (query or "").lower()
    strategy = str(strategy_name or "").strip().lower()
    mode = "structured provider lane"
    if "stagehand" in strategy:
        mode = "browser interaction lane"
    elif "browser_use" in strategy:
        mode = "visual browser fallback lane"
    if any(token in query_lowered for token in ("flight", "flights", "airline", "airport", "travel")):
        if any(token in lowered for token in ("1015", "429", "cloudflare", "rate limit")):
            return f"The {mode} was rate-limited while checking live flight options, so I switched to the next approach."
        if any(token in lowered for token in ("502", "503", "504", "service unavailable", "gateway timeout", "internal server error")):
            return f"The {mode} failed while checking live flight options, so I switched to the next approach."
    if any(token in query_lowered for token in ("restaurant", "reservation", "resy", "opentable", "book me", "dinner")):
        if any(token in lowered for token in ("500", "502", "503", "504", "service unavailable", "gateway timeout", "internal server error")):
            return f"The {mode} failed while checking live reservation availability, so I switched to the next approach."
        if any(token in lowered for token in ("429", "1015", "cloudflare", "rate limit")):
            return f"The {mode} was rate-limited while checking live reservation availability, so I switched to the next approach."
    if any(token in lowered for token in ("429", "1015", "cloudflare", "rate limit")):
        return f"The {mode} was rate-limited, so I switched to the next approach."
    if any(token in lowered for token in ("500", "502", "503", "504", "service unavailable", "gateway timeout", "internal server error")):
        return f"The {mode} failed, so I switched to the next approach."
    return ""


def infer_stalled_findings_text(job: AgentJob, *, strategy_name: str = "") -> str:
    repeat_count = int(job.heartbeat_repeat_count or 0)
    current_step = (job.current_step or "").strip().lower()
    if repeat_count < 2 or current_step not in {"running_agent", "attachments_ready"}:
        return ""
    query_lowered = (job.query or "").lower()
    strategy = str(strategy_name or "").strip().lower()
    if any(token in query_lowered for token in ("flight", "flights", "airline", "airport", "travel")):
        if "stagehand" in strategy:
            return (
                "I do not have a usable flight shortlist yet. "
                "The direct flight sources already degraded, and I am checking the browser fallback now."
            )
        if "browser_use" in strategy:
            return (
                "I do not have a usable flight shortlist yet. "
                "The direct and primary browser lanes already degraded, and I am checking the last fallback now."
            )
        return (
            "I do not have a usable flight shortlist yet. "
            "The live flight sources are still not returning stable results, so I am continuing to work the search."
        )
    if any(token in query_lowered for token in ("hotel", "hotels", "stay", "accommodation")):
        return (
            "I do not have a usable hotel shortlist yet. "
            "The live hotel sources are still not returning stable results."
        )
    if any(token in query_lowered for token in ("rental car", "car rental", "rent a car")):
        return (
            "I do not have usable rental-car options yet. "
            "The live rental sources are still not returning stable results."
        )
    return ""


def durable_findings_text(
    *,
    job: AgentJob,
    checkpoint: Optional[CheckpointPayload],
    strategy_name: str = "",
    error_message: str = "",
) -> str:
    metadata = job.metadata or {}
    matrix = metadata.get("execution_progress_matrix") if isinstance(metadata.get("execution_progress_matrix"), dict) else {}
    job_context = metadata.get("job_context") if isinstance(metadata.get("job_context"), dict) else {}
    persisted_findings = plain_text_message(str(job_context.get("latest_findings_summary") or ""))
    artifact_findings = plain_text_message(str(matrix.get("last_meaningful_artifact") or ""))
    candidates = [
        extract_partial_findings_block(error_message),
        extract_partial_findings_block(persisted_findings),
        extract_partial_findings_block(artifact_findings),
        persisted_findings,
        artifact_findings,
        job.result_preview or "",
        job.latest_checkpoint_summary or "",
        checkpoint.summary if checkpoint else "",
    ]
    for candidate in candidates:
        cleaned = clean_user_facing_result(candidate)
        if cleaned and not _summary_matches_job_domain(job.query, cleaned):
            continue
        if has_useful_partial_findings(cleaned):
            return cleaned[:1200]
    stalled = infer_stalled_findings_text(job, strategy_name=strategy_name)
    if stalled:
        return stalled[:1200]
    return summarize_recoverable_failure(job.query, error_message or (job.error_message or ""), strategy_name)[:1200]


def partial_findings_text(job: AgentJob, checkpoint: Optional[CheckpointPayload]) -> str:
    strategy_name = ""
    metadata = job.metadata or {}
    if isinstance(metadata.get("strategy_state"), dict):
        strategy_name = str(metadata["strategy_state"].get("current_strategy") or "")
    return durable_findings_text(
        job=job,
        checkpoint=checkpoint,
        strategy_name=strategy_name,
        error_message=job.error_message or "",
    )


def progress_snapshot_text(job: AgentJob, checkpoint: Optional[CheckpointPayload]) -> str:
    candidates = [
        job.last_status_sent_text or "",
        job.latest_checkpoint_summary or "",
        checkpoint.summary if checkpoint else "",
    ]
    for candidate in candidates:
        cleaned = clean_user_facing_result(candidate)
        if not cleaned:
            continue
        if not _summary_matches_job_domain(job.query, cleaned):
            continue
        lowered = cleaned.lower()
        if lowered.startswith("interrupted:"):
            cleaned = cleaned.split(":", 1)[1].strip()
        if cleaned:
            return cleaned[:1200]
    step = humanize_step(job.current_step or (checkpoint.current_step if checkpoint else "") or "")
    return clean_user_facing_result(step)[:1200]


def interrupted_reply_text(
    job: AgentJob,
    checkpoint: Optional[CheckpointPayload],
    *,
    lead: str,
    include_checkpoint_note: bool = True,
) -> str:
    findings = partial_findings_text(job, checkpoint)
    if findings:
        tail = "\n\nI kept the latest checkpoint." if include_checkpoint_note else ""
        return f"{lead}\n\nCurrent findings:\n{findings}{tail}".strip()
    snapshot = progress_snapshot_text(job, checkpoint)
    if snapshot:
        tail = "\n\nI kept the latest checkpoint." if include_checkpoint_note else ""
        return f"{lead}\n\nLatest progress:\n{snapshot}{tail}".strip()
    if include_checkpoint_note:
        return f"{lead} I kept the latest checkpoint.".strip()
    return lead.strip()


def humanize_worker_failure(query: str, error_message: str, status: JobStatus) -> str:
    normalized = clean_user_facing_result(error_message)
    lowered = normalized.lower()
    query_lowered = query.lower()
    if status == JobStatus.INTERRUPTED:
        if "stopped by user" in lowered or "activity cancelled" in lowered or "activity canceled" in lowered:
            return "I stopped that task."
        return "I hit an interruption before that task finished. I kept the latest checkpoint."
    if "worker exited without reporting a terminal state" in lowered:
        return "The worker stopped unexpectedly before the task finished."
    if "request_limit of 50" in lowered or "would exceed the request_limit" in lowered:
        if any(token in query_lowered for token in ("flight", "hotel", "rental car", "travel")):
            return "The travel search got stuck in an internal browser loop and did not finish cleanly."
        if any(token in query_lowered for token in ("restaurant", "reservation", "resy", "opentable")):
            return "The reservation search got stuck in an internal browser loop and did not finish cleanly."
        return "The task hit an internal tool-step limit before it finished."
    if any(token in lowered for token in ("429", "1015", "cloudflare", "rate limit")):
        if any(token in query_lowered for token in ("flight", "hotel", "rental car", "travel")):
            return "Travel sources temporarily rate-limited or blocked this run, and the fallback paths still did not complete."
        if any(token in query_lowered for token in ("restaurant", "reservation", "resy", "opentable")):
            return "Booking sources temporarily blocked or rate-limited this run, and the fallback paths still did not complete."
        return "Live web sources temporarily blocked or rate-limited this run."
    if "timed out" in lowered:
        return "This task timed out before it finished."
    if normalized:
        return normalized
    return "I hit an internal error before the task finished."


def _summary_matches_job_domain(query: str, summary: str) -> bool:
    summary_tags = query_domain_tags(summary)
    if not summary_tags:
        return True
    return query_domains_compatible(query, summary)


def humanize_step(step: str) -> str:
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


def status_summary_for_query(query: str, *, attachments: bool) -> str:
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


def progress_summary_for_step(
    query: str,
    *,
    current_step: str,
    attachments: bool,
    summary: str = "",
    elapsed_seconds: float = 0.0,
) -> str:
    cleaned_summary = plain_text_message(summary)
    generic_markers = {
        "",
        "worker picked up job",
        "workspace prepared",
        "attachments downloaded",
        "agent completed",
    }
    baseline = status_summary_for_query(query, attachments=attachments)
    if cleaned_summary and cleaned_summary not in generic_markers and cleaned_summary != baseline:
        return cleaned_summary

    lowered = query.lower()
    running_phase = 0
    if elapsed_seconds >= 480:
        running_phase = 2
    elif elapsed_seconds >= 180:
        running_phase = 1

    if current_step == "starting worker":
        return "starting the task environment and loading the workspace"
    if current_step == "attachments_ready":
        if attachments:
            return "organizing the uploaded files and preparing the workspace"
        return "preparing the workspace and loading the first live sources"
    if current_step == "uploading_outputs":
        return "writing the final answer and packaging links or files"
    if current_step == "running_agent":
        if any(token in lowered for token in ("flight", "flights", "airline", "airport")):
            phases = (
                "checking live flight options and collecting candidate itineraries",
                "comparing fares, timing, and airline tradeoffs across the shortlist",
                "writing the final flight recommendation and direct booking links",
            )
            return phases[running_phase]
        if any(token in lowered for token in ("hotel", "hotels", "stay", "airbnb", "accommodation")):
            phases = (
                "collecting hotel candidates with live pricing and location details",
                "comparing neighborhoods, cancellation terms, and nightly rates",
                "writing the final hotel shortlist and direct booking links",
            )
            return phases[running_phase]
        if any(token in lowered for token in ("rental car", "car rental", "rent a car", "avis", "hertz", "enterprise")):
            phases = (
                "collecting rental car options and checking live availability",
                "comparing providers, coverage terms, and total pricing",
                "writing the final rental car shortlist and direct booking links",
            )
            return phases[running_phase]
        if any(token in lowered for token in ("restaurant", "reservation", "resy", "opentable", "book me", "dinner")):
            phases = (
                "checking reservation sources and matching the correct venue",
                "comparing available times, policies, and booking paths",
                "writing the final reservation options and handoff links",
            )
            return phases[running_phase]
        if any(token in lowered for token in ("jacket", "shoe", "shirt", "pants", "gift", "buy", "purchase", "order", "vendor", "product")):
            phases = (
                "reviewing product pages and collecting candidate options",
                "comparing pricing, specs, and direct vendor links across the shortlist",
                "writing the ranked recommendation and final purchase links",
            )
            return phases[running_phase]
        if any(token in lowered for token in ("research", "compare", "review", "reddit", "google")):
            phases = (
                "reviewing sources and collecting the strongest candidates",
                "comparing findings, checking links, and narrowing the shortlist",
                "writing the final recommendation and supporting notes",
            )
            return phases[running_phase]
        if any(token in lowered for token in ("browser", "website", "site", "search")):
            phases = (
                "opening live pages and collecting the first relevant details",
                "checking the most promising pages and comparing what matters",
                "writing the final summary from the gathered live sources",
            )
            return phases[running_phase]
        if attachments:
            phases = (
                "reviewing the uploaded material and collecting the key details",
                "comparing the extracted findings and narrowing the answer",
                "writing the final answer from the uploaded material",
            )
            return phases[running_phase]
    return cleaned_summary or baseline


def progress_notification_text(job: AgentJob, *, current_step: str, summary: str) -> str:
    cleaned_summary = progress_summary_for_step(
        job.query,
        current_step=current_step,
        attachments=bool(job.attachments),
        summary=summary,
    )
    message = cleaned_summary or status_summary_for_query(job.query, attachments=bool(job.attachments))
    message = " ".join(message.split()).strip()
    if not message:
        return "Continuing the task."
    if message[-1] not in ".!?":
        message += "."
    return message[:1000]


def checkpoint_input_prompt(checkpoint: Optional[CheckpointPayload]) -> tuple[str, str]:
    if checkpoint is None:
        return "", ""
    metadata = checkpoint.metadata or {}
    question = str(metadata.get("input_question", "")).strip()
    details = str(metadata.get("input_details", "")).strip()
    return question, details


def paused_input_reply_text(state: StateStore, job: AgentJob) -> str:
    checkpoint = state.get_latest_checkpoint(job.job_id)
    question, details = checkpoint_input_prompt(checkpoint)
    summary = plain_text_message(job.latest_checkpoint_summary or (checkpoint.summary if checkpoint else "") or "")
    question = plain_text_message(question)
    details = plain_text_message(details)

    lines = ["I need one thing before I continue."]
    if question:
        lines.append(question)
    if details:
        lines.extend(["", details])
    elif summary:
        lines.extend(["", summary])
    lines.extend(["", "Reply normally with the missing detail.", "If you want something else instead, just ask."])
    return "\n".join(lines)


def build_paused_input_resume_query(
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


def build_contextual_heavy_followup_query(
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
        "If the user changed provider, cuisine, neighborhood, budget, venue, or booking constraints, rerun the live work with the new constraints."
    )
    return "\n\n".join(part for part in parts if part)


def build_heavy_claim(state: StateStore, settings: Settings, job: AgentJob, *, query_override: Optional[str] = None) -> dict[str, Any]:
    context_summary, recent_turns, config = state.get_context_bundle(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
    )
    memories = state.list_memories(owner=job.user_id or "siri")
    resume_checkpoint = state.get_latest_checkpoint(job.resume_from_job_id) if job.resume_from_job_id else None
    execution_progress_matrix = normalize_execution_progress_matrix(
        (job.metadata or {}).get("execution_progress_matrix")
        or (job.metadata or {}).get("strategy_state")
        or default_strategy_state()
    )
    strategy_state = normalize_strategy_state(execution_progress_matrix.model_dump(mode="json"))
    persisted_job_context = JobContext.model_validate((job.metadata or {}).get("job_context") or {})
    metadata_query_override = str((job.metadata or {}).get("query_override") or "").strip()
    effective_query = query_override or metadata_query_override or job.query
    job_context = JobContext(
        original_query=job.query,
        current_query=effective_query,
        conversation_id=job.conversation_id or "default",
        source=job.source.value,
        resume_from_job_id=job.resume_from_job_id,
        latest_findings_summary=persisted_job_context.latest_findings_summary or job.result_preview or "",
        latest_checkpoint_summary=(
            persisted_job_context.latest_checkpoint_summary
            or (resume_checkpoint.summary if resume_checkpoint else "")
            or (job.latest_checkpoint_summary or "")
        ),
        active_topic_key=str((job.metadata or {}).get("active_topic_key") or persisted_job_context.active_topic_key or ""),
        thread_context_excerpt=context_summary[:3000],
        operator_constraints=str((job.metadata or {}).get("operator_constraints") or persisted_job_context.operator_constraints or ""),
        created_at=job.created_at or persisted_job_context.created_at,
        updated_at=job.last_heartbeat_at or job.created_at,
    )
    claim = {
        "job": {
            **job.model_dump(),
            "query": effective_query,
        },
        "attachments": [attachment.model_dump() for attachment in job.attachments],
        "context_summary": context_summary,
        "recent_turns": [turn.model_dump() for turn in recent_turns],
        "durable_memories": memories,
        "resume_checkpoint": resume_checkpoint.model_dump() if resume_checkpoint else None,
        "config": config.model_dump(),
        "strategy_state": strategy_state,
        "execution_progress_matrix": execution_progress_matrix.model_dump(mode="json"),
        "job_context": job_context.model_dump(mode="json"),
        "artifacts_bucket": settings.artifacts_bucket,
        "artifacts_prefix": f"jobs/{job.job_id}",
    }
    return _json_safe(claim)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


async def send_final_job_message(
    settings: Settings,
    state: StateStore,
    job: AgentJob,
    *,
    cleaned_result: str,
    output_files: list[str],
    artifact_keys: list[str],
) -> None:
    if not job.chat_id:
        return
    include_browser_step_screenshots = query_requests_browser_images(job.query)
    include_requested_output_files = query_requests_output_files(job.query)
    visible_files = visible_output_files(
        output_files,
        include_browser_step_screenshots=include_browser_step_screenshots,
        include_other_output_files=include_requested_output_files,
    )
    message = cleaned_result
    if visible_files:
        message += "\n\nOutput files:\n" + "\n".join(f"- {name}" for name in visible_files[:20])
    telegram = TelegramClient(settings)
    await telegram.send_message(job.chat_id, message)

    zipped_screenshots: list[tuple[str, bytes]] = []
    for file_name, object_key in zip(output_files[:10], artifact_keys[:10]):
        if is_browser_step_screenshot(file_name):
            if not include_browser_step_screenshots:
                continue
            response = settings.s3.get_object(Bucket=settings.artifacts_bucket, Key=object_key)
            zipped_screenshots.append((file_name.rsplit("/", 1)[-1], response["Body"].read()))
            continue
        if not include_requested_output_files:
            continue
        response = settings.s3.get_object(Bucket=settings.artifacts_bucket, Key=object_key)
        content = response["Body"].read()
        await telegram.send_document_bytes(job.chat_id, file_name, content, caption=file_name)
    if len(zipped_screenshots) == 1:
        screenshot_name, screenshot_bytes = zipped_screenshots[0]
        await telegram.send_document_bytes(job.chat_id, screenshot_name, screenshot_bytes, caption=screenshot_name)
    elif zipped_screenshots:
        zip_name = zip_artifact_bundle_name(job)
        zip_bytes = build_zip_bytes(zipped_screenshots)
        await telegram.send_document_bytes(job.chat_id, zip_name, zip_bytes, caption=zip_name)


def zip_artifact_bundle_name(job: AgentJob) -> str:
    source = "telegram" if job.source.value == "telegram" else "task"
    return f"{source}-{job.job_id[:8]}-screenshots.zip"


def build_zip_bytes(files: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_name, content in files:
            archive.writestr(file_name, content)
    return buffer.getvalue()


def record_job_assistant_turn(state: StateStore, job: AgentJob, text: str) -> None:
    state.record_turn(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
        role=ThreadTurnRole.ASSISTANT,
        text=text,
        task_class=TaskClass.HEAVY,
    )
