from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from .browser import _choose_user_agent, run_browser_task
from .research import sanitize_tool_output
from .settings import Settings
from .workspace import Workspace


logger = logging.getLogger(__name__)


def _model_name(value: str) -> str:
    if ":" in value:
        return value.split(":", 1)[1]
    return value


def _workspace_dir(workspace: Workspace, relative_path: str) -> Path:
    path = workspace.resolve(relative_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def run_browser_use_task(
    task: str,
    *,
    max_pages: int,
    max_steps: int,
    settings: Settings,
    workspace: Optional[Workspace],
) -> str:
    if not settings.browser_use_enabled:
        return await run_browser_task(task, max_pages=max_pages, max_steps=max_steps)

    if workspace is None:
        logger.info("browser-use skipped because workspace was unavailable")
        return await run_browser_task(task, max_pages=max_pages, max_steps=max_steps)

    try:
        from browser_use import Agent, Browser, BrowserProfile, ChatBrowserUse
        from browser_use.llm import ChatDeepSeek
        from browser_use.mcp.client import MCPClient
        from browser_use.tools.service import Tools
    except Exception as exc:
        logger.warning("browser-use unavailable, falling back to custom browser task: %s", exc)
        return await run_browser_task(task, max_pages=max_pages, max_steps=max_steps)

    user_data_dir = _workspace_dir(workspace, ".browser-use/profile")
    downloads_dir = _workspace_dir(workspace, "browser/downloads")
    conversation_dir = _workspace_dir(workspace, ".browser-use/logs")
    screenshots_dir = _workspace_dir(workspace, "browser/screenshots")

    use_cloud = False
    browser_use_key = ""
    if settings.browser_use_cloud_enabled and settings.browser_use_api_key_param:
        try:
            browser_use_key = settings.secret(settings.browser_use_api_key_param)
        except Exception as exc:
            logger.warning("browser-use cloud key unavailable, staying local: %s", exc)
        if browser_use_key:
            os.environ["BROWSER_USE_API_KEY"] = browser_use_key
            use_cloud = True

    user_agent = _choose_user_agent(settings)
    browser = Browser(
        browser_profile=BrowserProfile(
            headless=True,
            user_agent=user_agent,
            user_data_dir=str(user_data_dir),
            downloads_path=str(downloads_dir),
            disable_security=False,
            deterministic_rendering=False,
        ),
        use_cloud=use_cloud,
        cloud_proxy_country_code=settings.browser_use_cloud_proxy_country_code if use_cloud else None,
    )

    if use_cloud:
        llm = ChatBrowserUse(model=settings.browser_use_cloud_model)
    else:
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not deepseek_key:
            deepseek_key = settings.secret(settings.deepseek_api_key_param)
            os.environ["DEEPSEEK_API_KEY"] = deepseek_key
        llm = ChatDeepSeek(
            model=_model_name(settings.browser_use_model or settings.agent_model),
            api_key=deepseek_key,
        )

    tools = Tools(
        exclude_actions=[
            "write_file",
            "replace_file",
            "read_file",
        ],
        display_files_in_done_text=False,
    )
    workspace_mcp = MCPClient(
        server_name="friday-workspace",
        command="python",
        args=["-m", "app.mcp_workspace_server"],
        env={
            "FRIDAY_WORKSPACE_ROOT": str(workspace.root),
            "PYTHONPATH": os.environ.get("PYTHONPATH", "/app"),
        },
    )
    await workspace_mcp.register_to_tools(
        tools,
        tool_filter=[
            "list_workspace_files",
            "read_workspace_file",
            "preview_workspace_file",
            "write_workspace_file",
            "convert_workspace_file_to_markdown",
            "write_workspace_pdf_report",
        ],
        prefix="workspace_",
    )

    agent = Agent(
        task=task,
        llm=llm,
        browser=browser,
        tools=tools,
        max_failures=max(1, settings.browser_use_max_failures),
        step_timeout=max(30, settings.browser_use_step_timeout_seconds),
        save_conversation_path=str(conversation_dir / "browser-use.json"),
        available_file_paths=[str(workspace.root)],
        display_files_in_done_text=True,
        use_vision=False if not use_cloud else "auto",
        use_judge=False,
        extend_system_message=(
            "Use the browser only when deterministic search/fetch was insufficient. "
            "Prefer robust interaction over brittle repeated clicks. "
            "If one source blocks or fails, continue to other sources and finish with partial results when necessary."
            " Use the Friday workspace MCP tools for file reading, file writing, markdown conversion, and PDF generation."
            " Do not rely on built-in browser-use file actions."
        ),
    )

    try:
        history = await agent.run(max_steps=max_steps)
        final_result = ""
        if hasattr(history, "final_result"):
            final_result = history.final_result() or ""
        if not final_result and hasattr(history, "extracted_content"):
            extracted = history.extracted_content()
            if isinstance(extracted, list):
                final_result = "\n".join(str(item) for item in extracted if item)
        errors: list[str] = []
        if hasattr(history, "errors"):
            raw_errors = history.errors()
            if isinstance(raw_errors, list):
                errors = [str(item) for item in raw_errors if item]

        screenshot_paths: list[str] = []
        if hasattr(history, "screenshot_paths"):
            raw_paths = history.screenshot_paths()
            if isinstance(raw_paths, list):
                screenshot_paths = [str(item) for item in raw_paths if item]

        copied_shots: list[str] = []
        for index, shot in enumerate(screenshot_paths[:5], start=1):
            source = Path(shot)
            if not source.exists():
                continue
            target = screenshots_dir / f"browser-use-step-{index}{source.suffix or '.png'}"
            target.write_bytes(source.read_bytes())
            copied_shots.append(str(target.relative_to(workspace.root)))

        parts = []
        if final_result.strip():
            parts.append(final_result.strip())
        if copied_shots:
            parts.append("Browser screenshots saved:\n" + "\n".join(f"- {name}" for name in copied_shots))
        if errors:
            parts.append("Browser warnings:\n" + "\n".join(f"- {value}" for value in errors[:8]))
        if not parts:
            parts.append("Browser-use completed without a final summary. Continue with the evidence already gathered.")
        return sanitize_tool_output("\n\n".join(parts))
    except Exception as exc:
        logger.warning("browser-use failed, falling back to custom browser task: %s", exc)
        fallback = await run_browser_task(task, max_pages=max_pages, max_steps=max_steps)
        return sanitize_tool_output(
            "BROWSER_USE_FALLBACK: primary browser agent hit an error and the task was retried with the legacy browser path.\n\n"
            f"Primary error: {exc}\n\n{fallback}"
        )
    finally:
        try:
            await workspace_mcp.disconnect()
        except Exception:
            logger.info("browser-use MCP disconnect failed", exc_info=True)
        try:
            await browser.stop()
        except Exception:
            logger.info("browser-use browser close failed", exc_info=True)
