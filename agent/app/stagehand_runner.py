from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from .research import sanitize_tool_output
from .settings import Settings
from .workspace import Workspace


logger = logging.getLogger(__name__)

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


def _stagehand_browser_payload(*, settings: Settings, workspace: Workspace, chrome_path: str) -> dict[str, object]:
    profile_dir = _workspace_dir(workspace, ".stagehand/profile")
    launch_options: dict[str, object] = {
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        "chromiumSandbox": False,
        "headless": settings.stagehand_local_headless,
        "userDataDir": str(profile_dir),
        "preserveUserDataDir": True,
    }
    if chrome_path:
        launch_options["executablePath"] = chrome_path
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
        "If a site blocks access, summarize what you were still able to verify instead of pretending you found nothing."
    )
    normalized_task = (
        "Interactive browser fallback mode. Deterministic MCP/API/public-web paths were insufficient for this task. "
        "Stay bounded, prefer factual extraction over exploration, and finish with a concise summary.\n\n"
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
            browser=_stagehand_browser_payload(settings=settings, workspace=workspace, chrome_path=chrome_path),
            self_heal=settings.stagehand_self_heal,
            verbose=1,
            system_prompt=system_prompt,
        )
        session_id = getattr(getattr(session_response, "data", None), "session_id", "") or ""
        if not session_id:
            return "STAGEHAND_BROWSER_TASK_FAILED: stagehand did not return a session id."

        execute_response = await asyncio.wait_for(
            client.sessions.execute(
                session_id,
                agent_config={
                    "mode": settings.stagehand_mode,
                    "model": {"model_name": model_name, "api_key": deepseek_key},
                    "execution_model": {"model_name": model_name, "api_key": deepseek_key},
                    "system_prompt": system_prompt,
                },
                execute_options={
                    "instruction": normalized_task,
                    "max_steps": float(max(1, max_steps)),
                    "tool_timeout": float(max(1000, settings.stagehand_tool_timeout_ms)),
                    "use_search": False,
                },
            ),
            timeout=max(5, settings.stagehand_task_timeout_seconds),
        )
        result = getattr(getattr(execute_response, "data", None), "result", None)
        if result is None:
            return "STAGEHAND_BROWSER_TASK_FAILED: stagehand returned no result payload."
        message = sanitize_tool_output(getattr(result, "message", "") or "")
        partial_findings = _stagehand_partial_findings(list(getattr(result, "actions", []) or []))
        if getattr(result, "success", False) and not _stagehand_result_needs_fallback(message):
            return message or _merge_stagehand_partial_findings(
                message="",
                partial_findings=partial_findings,
                primary_error="Stagehand completed without a usable summary.",
            )
        return _merge_stagehand_partial_findings(
            message=message,
            partial_findings=partial_findings,
            primary_error="Stagehand did not complete the task cleanly.",
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
