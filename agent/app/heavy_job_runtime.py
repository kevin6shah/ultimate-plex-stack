from __future__ import annotations

import io
import re
import zipfile
from typing import Any, Optional
from urllib.parse import quote

from .artifacts import (
    is_browser_step_screenshot,
    query_requests_browser_images,
    query_requests_output_files,
    visible_output_files,
)
from .jobs import AgentJob, CheckpointPayload, JobStatus, TaskClass, ThreadTurnRole
from .settings import Settings
from .storage import StateStore
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


def humanize_worker_failure(query: str, error_message: str, status: JobStatus) -> str:
    normalized = clean_user_facing_result(error_message)
    lowered = normalized.lower()
    query_lowered = query.lower()
    if status == JobStatus.INTERRUPTED:
        if "stopped by user" in lowered or "activity cancelled" in lowered or "activity canceled" in lowered:
            return "I stopped that task."
        return "This task was interrupted before it finished. Say 'resume that task' if you want me to continue from the last checkpoint."
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
    cleaned_step = humanize_step(current_step)
    cleaned_summary = progress_summary_for_step(
        job.query,
        current_step=current_step,
        attachments=bool(job.attachments),
        summary=summary,
    )
    lines = ["Still working on your task."]
    if cleaned_step:
        lines.append(f"Current step: {cleaned_step}")
    if cleaned_summary:
        lines.append(f"Latest progress: {cleaned_summary[:1000]}")
    else:
        lines.append(f"Latest progress: {status_summary_for_query(job.query, attachments=bool(job.attachments))}")
    return "\n".join(lines)


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
    step = humanize_step(job.current_step or (checkpoint.current_step if checkpoint else "") or "")
    question = plain_text_message(question)
    details = plain_text_message(details)

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


def build_heavy_claim(state: StateStore, settings: Settings, job: AgentJob, *, query_override: Optional[str] = None) -> dict[str, Any]:
    context_summary, recent_turns, config = state.get_context_bundle(
        channel=job.source.value,
        user_id=job.user_id or "unknown",
        conversation_id=job.conversation_id or "default",
    )
    memories = state.list_memories(owner=job.user_id or "siri")
    resume_checkpoint = state.get_latest_checkpoint(job.resume_from_job_id) if job.resume_from_job_id else None
    return {
        "job": {
            **job.model_dump(),
            "query": query_override or job.query,
        },
        "attachments": [attachment.model_dump() for attachment in job.attachments],
        "context_summary": context_summary,
        "recent_turns": [turn.model_dump() for turn in recent_turns],
        "durable_memories": memories,
        "resume_checkpoint": resume_checkpoint.model_dump() if resume_checkpoint else None,
        "config": config.model_dump(),
        "artifacts_bucket": settings.artifacts_bucket,
        "artifacts_prefix": f"jobs/{job.job_id}",
    }


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
