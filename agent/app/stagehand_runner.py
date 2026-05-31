from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from .browser import _choose_user_agent
from .browser_validation import (
    browser_modal_dismissal_instruction,
    result_has_validation_evidence,
    task_requires_confirmation_evidence,
    validation_failure_text,
    validation_instruction_for_task,
)
from .booking_guard import zero_dollar_booking_instruction
from .browser_fingerprint import browser_fingerprint_seed, build_browser_fingerprint, stagehand_launch_options
from .research import sanitize_tool_output
from .settings import Settings
from .workspace import Workspace


logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s<>\"]+")
DATE_PATTERN = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
PARTY_SIZE_PATTERN = re.compile(r"\bfor\s+(\d+)\s+people\b", re.IGNORECASE)

LOW_QUALITY_STAGEHAND_PATTERNS = (
    "i need more information",
    "i'm unable to complete",
    "i’m unable to complete",
    "i could not complete",
    "timed out",
    "timeout",
    "captcha",
    "sign in",
)

CHROME_PATH_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/ms-playwright/chromium-1148/chrome-linux/chrome",
)


def _workspace_dir(workspace: Workspace, relative_path: str) -> Path:
    path = workspace.resolve(relative_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stagehand_model_name(settings: Settings) -> str:
    configured = (settings.stagehand_model or "").strip()
    if not configured:
        return "deepseek/deepseek-chat"
    if ":" in configured and "/" not in configured:
        provider, model = configured.split(":", 1)
        provider = provider.strip().lower() or "deepseek"
        model = model.strip()
        if model:
            return f"{provider}/{model}"
    return configured


def _stagehand_result_needs_fallback(result_text: str) -> bool:
    normalized = (result_text or "").strip().lower()
    if not normalized:
        return True
    return any(pattern in normalized for pattern in LOW_QUALITY_STAGEHAND_PATTERNS)


def _stagehand_chrome_path(settings: Settings) -> str:
    configured = (settings.stagehand_local_chrome_path or "").strip()
    if configured:
        return configured
    env_value = os.environ.get("CHROME_PATH", "").strip()
    if env_value:
        return env_value
    for candidate in CHROME_PATH_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def _stagehand_partial_findings(actions: list[object]) -> str:
    findings: list[str] = []
    seen: set[str] = set()
    for action in actions[-6:]:
        page_url = getattr(action, "page_url", None)
        instruction = getattr(action, "instruction", None)
        page_text = getattr(action, "page_text", None)
        reasoning = getattr(action, "reasoning", None)
        candidates = [instruction, page_text, reasoning]
        if page_url:
            candidates.insert(0, f"Observed page: {page_url}")
        for candidate in candidates:
            text = " ".join(str(candidate or "").split())
            if len(text) < 24 or text in seen:
                continue
            seen.add(text)
            findings.append(text[:400])
            break
    if not findings:
        return ""
    return "\n".join(f"- {item}" for item in findings[-4:])


def _stagehand_extract_explicit_urls(task: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_PATTERN.findall(task or ""):
        candidate = match.rstrip(").,]")
        if candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
    return urls


def _stagehand_task_needs_interaction(task: str) -> bool:
    normalized = " ".join((task or "").lower().split())
    markers = (
        "booking flow",
        "bookable times",
        "reservation time",
        "reservation times",
        "available times",
        "reservation page",
        "party size",
        "date selector",
        "cancellation",
        "deposit",
        "prepaid reservation",
    )
    return any(marker in normalized for marker in markers)


def _stagehand_interaction_instruction(task: str) -> str:
    return (
        browser_modal_dismissal_instruction()
        + " "
        + "Interact with the current page before summarizing. "
        "Set or confirm the requested date, party size, and reservation controls from the task. "
        "Open the time selector or reservation area if needed so visible bookable times are shown. "
        "Do not submit or finalize any booking."
    )


def _stagehand_interaction_steps(task: str) -> list[str]:
    steps: list[str] = [browser_modal_dismissal_instruction()]
    date_match = DATE_PATTERN.search(task or "")
    if date_match:
        requested_date = date_match.group(1)
        steps.append(
            f"Open the date selector and set the reservation date to {requested_date}. "
            "Do not book anything."
        )
    party_match = PARTY_SIZE_PATTERN.search(task or "")
    if party_match:
        party_size = party_match.group(1)
        steps.append(
            f"Set the party size selector to {party_size} guests. "
            "Do not book anything."
        )
    steps.append(
        "Open the reservation time selector or reservation results area so visible bookable times are shown. "
        "Do not book anything."
    )
    return steps


def _stagehand_verification_instruction(task: str) -> str:
    guidance: list[str] = [
        "Summarize the current page for the user's task.",
        "Return concrete findings and note any blocker briefly.",
    ]
    validation_instruction = validation_instruction_for_task(task)
    if validation_instruction:
        guidance.append(validation_instruction)
    date_match = DATE_PATTERN.search(task or "")
    if date_match:
        requested_date = date_match.group(1)
        guidance.append(
            f"If the page shows relative wording like Today for the requested date {requested_date}, treat the requested date as satisfied instead of calling it a mismatch."
        )
        guidance.append(
            f"If the calendar shows {requested_date} or its human-readable equivalent as selected, treat the date as set correctly."
        )
        guidance.append(
            f"If the current page URL includes date={requested_date}, treat the reservation date as set correctly."
        )
        try:
            timezone_name = os.environ.get("TZ", "America/New_York")
            local_today = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
        except Exception:
            local_today = ""
        if local_today and local_today == requested_date:
            guidance.append(
                f"The requested date {requested_date} is the current local date, so a Today label is valid and should not be treated as a mismatch."
            )
    party_match = PARTY_SIZE_PATTERN.search(task or "")
    if party_match:
        party_size = party_match.group(1)
        guidance.append(
            f"If the guests selector shows {party_size} Guests or an equivalent selected state, treat the party size as set correctly."
        )
        guidance.append(
            f"If the current page URL includes seats={party_size}, treat the party size as set correctly."
        )
    return " ".join(guidance)


def _stagehand_search_url(task: str) -> str:
    query = " ".join((task or "").split())
    query = query[:280]
    return f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"


def _coerce_stagehand_json(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _coerce_stagehand_findings(value: object) -> list[str]:
    if isinstance(value, list):
        findings: list[str] = []
        for item in value:
            text = " ".join(str(item or "").split())
            if text:
                findings.append(text[:400])
        return findings
    if isinstance(value, str):
        text = " ".join(value.split())
        return [text[:400]] if text else []
    return []


def _coerce_stagehand_search_results(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    results: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title", "")).split())
        url = " ".join(str(item.get("url", "")).split())
        snippet = " ".join(str(item.get("snippet", "")).split())
        if not url.startswith(("http://", "https://")):
            continue
        results.append(
            {
                "title": title[:200],
                "url": url[:500],
                "snippet": snippet[:300],
            }
        )
    return results


def _format_stagehand_summary(
    *,
    summary: str,
    findings: list[str],
    validation_evidence: list[str],
    current_url: str,
    search_results: list[dict[str, str]],
    blocker: str,
) -> str:
    parts: list[str] = []
    normalized_summary = " ".join(summary.split())
    if normalized_summary:
        parts.append(normalized_summary[:2500])
    if findings:
        parts.append("\n".join(f"- {item}" for item in findings[:5]))
    if validation_evidence:
        parts.append("Validation evidence:\n" + "\n".join(f"- {item}" for item in validation_evidence[:5]))
    if search_results:
        rendered = []
        for item in search_results[:3]:
            title = item.get("title") or item.get("url") or "Candidate result"
            url = item.get("url") or ""
            snippet = item.get("snippet") or ""
            line = f"- {title}"
            if url:
                line += f" ({url})"
            if snippet:
                line += f": {snippet}"
            rendered.append(line[:700])
        if rendered:
            parts.append("Candidate sources:\n" + "\n".join(rendered))
    if current_url.strip():
        parts.append(f"Current page: {current_url.strip()[:500]}")
    if blocker.strip():
        parts.append(f"Stagehand issue: {blocker.strip()[:500]}")
    return sanitize_tool_output("\n\n".join(part for part in parts if part).strip())


async def _stagehand_raw_json(awaitable: object) -> dict[str, object]:
    response = await awaitable
    try:
        data = await response.json()
    finally:
        try:
            await response.close()
        except Exception:
            pass
    return _coerce_stagehand_json(data)


def _stagehand_browser_payload(*, settings: Settings, workspace: Workspace, chrome_path: str, task: str) -> dict[str, object]:
    profile_dir = _workspace_dir(workspace, ".stagehand/profile")
    downloads_dir = _workspace_dir(workspace, ".stagehand/downloads")
    fingerprint = build_browser_fingerprint(
        seed=browser_fingerprint_seed(task),
        user_agent=_choose_user_agent(settings),
    )
    launch_options = stagehand_launch_options(
        fingerprint=fingerprint,
        executable_path=chrome_path,
        user_data_dir=str(profile_dir),
        downloads_path=str(downloads_dir),
    )
    launch_options["headless"] = settings.stagehand_local_headless
    return {
        "type": "local",
        "launchOptions": launch_options,
    }


def _merge_stagehand_partial_findings(*, message: str, partial_findings: str, primary_error: str) -> str:
    parts: list[str] = []
    if message.strip():
        parts.append(message.strip())
    if partial_findings:
        parts.append(
            "PARTIAL_STAGEHAND_FINDINGS: the browser session gathered these findings before it stopped:\n"
            + partial_findings
        )
    if primary_error:
        parts.append(f"Stagehand issue: {primary_error}")
    if not parts:
        parts.append("STAGEHAND_BROWSER_TASK_FAILED: the browser session did not return usable findings.")
    return sanitize_tool_output("\n\n".join(parts))


async def run_stagehand_task(
    task: str,
    *,
    max_steps: int,
    settings: Settings,
    workspace: Optional[Workspace],
) -> str:
    if not settings.stagehand_enabled:
        return "STAGEHAND_BROWSER_TASK_UNAVAILABLE: stagehand is disabled."
    if workspace is None:
        return "STAGEHAND_BROWSER_TASK_UNAVAILABLE: workspace was unavailable."

    try:
        from stagehand import AsyncStagehand
    except Exception as exc:
        logger.warning("stagehand unavailable, continuing without it: %s", exc)
        return sanitize_tool_output(f"STAGEHAND_BROWSER_TASK_UNAVAILABLE: stagehand import failed due to {exc}.")

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not deepseek_key:
        try:
            deepseek_key = settings.secret(settings.deepseek_api_key_param).strip()
        except Exception as exc:
            logger.warning("stagehand deepseek key unavailable: %s", exc)
            deepseek_key = ""
    if not deepseek_key:
        return "STAGEHAND_BROWSER_TASK_UNAVAILABLE: missing model API key."
    os.environ["DEEPSEEK_API_KEY"] = deepseek_key

    chrome_path = _stagehand_chrome_path(settings)
    if chrome_path:
        os.environ["CHROME_PATH"] = chrome_path

    _workspace_dir(workspace, ".stagehand/profile")
    _workspace_dir(workspace, ".stagehand/logs")

    model_name = _stagehand_model_name(settings)
    system_prompt = (
        "You are Friday's bounded browser fallback. "
        "Only use this browser session after deterministic MCP/API/public-web paths were insufficient. "
        "Keep the task tightly scoped, avoid loops, and return a concise human-readable summary with concrete findings. "
        "If a site blocks access, summarize what you were still able to verify instead of pretending you found nothing. "
        + zero_dollar_booking_instruction()
    )
    normalized_task = (
        "Interactive browser fallback mode. Deterministic MCP/API/public-web paths were insufficient for this task. "
        "Stay bounded, prefer factual extraction over exploration, and finish with a concise summary. "
        + browser_modal_dismissal_instruction()
        + (" " + validation_instruction_for_task(task) if validation_instruction_for_task(task) else "")
        + "\n\n"
        + task
    )

    client = AsyncStagehand(
        model_api_key=deepseek_key,
        server="local",
        local_headless=settings.stagehand_local_headless,
        local_chrome_path=chrome_path or None,
        local_ready_timeout_s=max(5.0, settings.stagehand_local_ready_timeout_seconds),
    )
    session_id = ""
    try:
        session_response = await client.sessions.start(
            model_name=model_name,
            browser=_stagehand_browser_payload(settings=settings, workspace=workspace, chrome_path=chrome_path, task=task),
            self_heal=settings.stagehand_self_heal,
            verbose=1,
            system_prompt=system_prompt,
        )
        session_id = getattr(getattr(session_response, "data", None), "session_id", "") or ""
        if not session_id:
            return "STAGEHAND_BROWSER_TASK_FAILED: stagehand did not return a session id."
        raw_sessions = client.sessions.with_raw_response
        model_config = {"model_name": model_name, "api_key": deepseek_key}
        explicit_urls = _stagehand_extract_explicit_urls(task)
        target_url = explicit_urls[0] if explicit_urls else _stagehand_search_url(task)
        search_results: list[dict[str, str]] = []
        blocker = ""

        await asyncio.wait_for(
            _stagehand_raw_json(
                raw_sessions.navigate(
                    session_id,
                    url=target_url,
                    options={
                        "wait_until": "domcontentloaded",
                        "timeout": float(max(5000, settings.stagehand_tool_timeout_ms)),
                    },
                )
            ),
            timeout=max(5, settings.stagehand_task_timeout_seconds),
        )

        if not explicit_urls:
            initial_extract = await asyncio.wait_for(
                _stagehand_raw_json(
                    raw_sessions.extract(
                        session_id,
                        instruction=(
                            "Extract the best search results for this task. "
                            "Prefer direct sources over summaries.\n\n"
                            + normalized_task
                        ),
                        schema={
                            "type": "object",
                            "properties": {
                                "summary": {"type": "string"},
                                "results": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "title": {"type": "string"},
                                            "url": {"type": "string"},
                                            "snippet": {"type": "string"},
                                        },
                                        "required": ["title", "url"],
                                    },
                                },
                            },
                            "required": ["summary"],
                        },
                        options={
                            "model": model_config,
                            "timeout": float(max(5000, settings.stagehand_tool_timeout_ms)),
                        },
                    )
                ),
                timeout=max(5, settings.stagehand_task_timeout_seconds),
            )
            extract_result = _coerce_stagehand_json(_coerce_stagehand_json(initial_extract).get("data", {})).get("result", {})
            extract_result = _coerce_stagehand_json(extract_result)
            search_results = _coerce_stagehand_search_results(extract_result.get("results"))
            if search_results:
                target_url = search_results[0]["url"]
                await asyncio.wait_for(
                    _stagehand_raw_json(
                        raw_sessions.navigate(
                            session_id,
                            url=target_url,
                            options={
                                "wait_until": "domcontentloaded",
                                "timeout": float(max(5000, settings.stagehand_tool_timeout_ms)),
                            },
                        )
                    ),
                    timeout=max(5, settings.stagehand_task_timeout_seconds),
                )

        if explicit_urls and _stagehand_task_needs_interaction(task):
            for interaction_step in _stagehand_interaction_steps(task):
                try:
                    await asyncio.wait_for(
                        _stagehand_raw_json(
                            raw_sessions.act(
                                session_id,
                                input=interaction_step,
                                options={
                                    "model": model_config,
                                    "timeout": float(max(5000, settings.stagehand_tool_timeout_ms)),
                                },
                            )
                        ),
                        timeout=max(5, settings.stagehand_task_timeout_seconds),
                    )
                except Exception as exc:
                    logger.warning("stagehand act step failed: %s", exc)
                    blocker = " ".join((blocker + " " + f"Interactive step failed: {exc}").split())
                    break

        final_extract = await asyncio.wait_for(
            _stagehand_raw_json(
                raw_sessions.extract(
                    session_id,
                    instruction=(
                        _stagehand_verification_instruction(task)
                        + "\n\n"
                        + normalized_task
                    ),
                    schema={
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "findings": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "validated_success": {"type": "boolean"},
                            "validation_evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "current_url": {"type": "string"},
                            "blocker": {"type": ["string", "null"]},
                        },
                        "required": ["summary"],
                    },
                    options={
                        "model": model_config,
                        "timeout": float(max(5000, settings.stagehand_tool_timeout_ms)),
                    },
                )
            ),
            timeout=max(5, settings.stagehand_task_timeout_seconds),
        )
        extract_payload = _coerce_stagehand_json(_coerce_stagehand_json(final_extract).get("data", {})).get("result", {})
        extract_payload = _coerce_stagehand_json(extract_payload)
        message = sanitize_tool_output(str(extract_payload.get("summary", "") or ""))
        current_url = str(extract_payload.get("current_url", "") or target_url)
        findings = _coerce_stagehand_findings(extract_payload.get("findings"))
        validation_evidence = _coerce_stagehand_findings(extract_payload.get("validation_evidence"))
        validated_success = bool(extract_payload.get("validated_success"))
        blocker = " ".join(str(extract_payload.get("blocker", blocker) or "").split())
        rendered = _format_stagehand_summary(
            summary=message,
            findings=findings,
            validation_evidence=validation_evidence,
            current_url=current_url,
            search_results=search_results,
            blocker=blocker,
        )
        if task_requires_confirmation_evidence(task) and not result_has_validation_evidence(
            task,
            rendered,
            explicit_evidence=validation_evidence if validated_success else [],
        ):
            return _merge_stagehand_partial_findings(
                message=validation_failure_text(task),
                partial_findings="\n".join(f"- {item}" for item in findings[:4]) if findings else "",
                primary_error=blocker or "missing explicit success evidence",
            )
        if rendered and not _stagehand_result_needs_fallback(rendered):
            return rendered
        return _merge_stagehand_partial_findings(
            message=rendered,
            partial_findings="",
            primary_error=blocker or "Stagehand did not return a usable summary.",
        )
    except TimeoutError:
        logger.warning("stagehand browser task timed out after %ss", settings.stagehand_task_timeout_seconds)
        return "STAGEHAND_BROWSER_TASK_FAILED: stagehand timed out before finishing."
    except Exception as exc:
        logger.warning("stagehand browser task failed: %s", exc)
        return sanitize_tool_output(f"STAGEHAND_BROWSER_TASK_FAILED: {exc}")
    finally:
        if session_id:
            try:
                await client.sessions.end(session_id)
            except Exception as exc:
                logger.warning("stagehand session cleanup failed: %s", exc)
        try:
            await client.close()
        except Exception:
            pass
