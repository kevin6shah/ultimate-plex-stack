from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from pathlib import Path
from typing import Optional

from .artifacts import query_requests_browser_images
from .browser import _choose_user_agent, run_browser_task
from .research import sanitize_tool_output
from .settings import Settings
from .workspace import Workspace


logger = logging.getLogger(__name__)

LOW_QUALITY_BROWSER_USE_PATTERNS = (
    "requires javascript",
    "would you like me to try the browser",
    "i could try the browser later",
    "search is currently unavailable",
    "use what i've already gathered",
    "let me use what i've already gathered",
)


class _BrowserUseCaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if not message:
            return
        normalized = str(message).strip()
        if not normalized:
            return
        self.messages.append(normalized)


def _model_name(value: str) -> str:
    if ":" in value:
        return value.split(":", 1)[1]
    return value


def _workspace_dir(workspace: Workspace, relative_path: str) -> Path:
    path = workspace.resolve(relative_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _filesystem_mcp_args(workspace: Workspace) -> list[str]:
    return [str(workspace.root)]


def _split_mcp_args(value: str) -> list[str]:
    if not value.strip():
        return []
    return shlex.split(value)


def _browser_use_result_needs_retry(result_text: str) -> bool:
    normalized = (result_text or "").strip().lower()
    if not normalized:
        return True
    return any(pattern in normalized for pattern in LOW_QUALITY_BROWSER_USE_PATTERNS)


def _browser_fallback_failed(result: str) -> bool:
    normalized = (result or "").strip().lower()
    if not normalized:
        return True
    return any(
        marker in normalized
        for marker in (
            "browser task could not read any public pages",
            "browser task failed",
            "browser fallback also failed",
            "public-web fallback failed",
            "no public pages could be read",
            "browser task unavailable",
        )
    )


def _extract_browser_use_partial_findings(messages: list[str]) -> str:
    findings: list[str] = []
    seen: set[str] = set()
    for raw in messages:
        normalized = (raw or "").strip()
        if "Memory:" in normalized:
            normalized = normalized.split("Memory:", 1)[1].strip()
        elif normalized.startswith("🎯 Task:"):
            normalized = normalized.split("🎯 Task:", 1)[1].strip()
        elif normalized.startswith("Task:"):
            normalized = normalized.split("Task:", 1)[1].strip()
        else:
            continue
        if len(normalized) < 24:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        findings.append(normalized)
    if not findings:
        return ""
    trimmed = findings[-4:]
    return "\n".join(f"- {item}" for item in trimmed)


def _merge_browser_use_partial_findings(*, partial_findings: str, fallback_result: str, primary_error: str) -> str:
    fallback_failed = _browser_fallback_failed(fallback_result)
    parts: list[str] = []
    if partial_findings:
        parts.append(
            "PARTIAL_BROWSER_FINDINGS: the interactive browser session gathered these findings before it stopped:\n"
            + partial_findings
        )
    if primary_error:
        parts.append(f"Primary browser issue: {primary_error}")
    if fallback_result and not fallback_failed:
        parts.append(fallback_result)
    elif not partial_findings:
        parts.append(fallback_result or "Browser research did not return any usable public-web results.")
    elif fallback_failed:
        parts.append(
            "The lightweight browser fallback could not verify more details, so rely on the partial findings above instead of claiming there was no public information."
        )
    return sanitize_tool_output("\n\n".join(part for part in parts if part))


def _optional_secret(settings: Settings, parameter_name: str, *, label: str) -> str:
    if not parameter_name:
        return ""
    try:
        return settings.secret(parameter_name)
    except Exception as exc:
        logger.warning("%s secret unavailable: %s", label, exc)
        return ""


def _firecrawl_mcp_env(settings: Settings) -> dict[str, str]:
    api_key = _optional_secret(settings, settings.firecrawl_api_key_param, label="firecrawl")
    return {"FIRECRAWL_API_KEY": api_key} if api_key else {}


def _google_maps_mcp_env(settings: Settings) -> dict[str, str]:
    api_key = _optional_secret(settings, settings.google_maps_api_key_param, label="google_maps")
    env: dict[str, str] = {"GOOGLE_MAPS_API_KEY": api_key} if api_key else {}
    enabled_tools = settings.google_maps_enabled_tools.strip()
    if enabled_tools:
        env["GOOGLE_MAPS_ENABLED_TOOLS"] = enabled_tools
    return env


def _gmail_mcp_env(settings: Settings) -> dict[str, str]:
    email_address = _optional_secret(settings, settings.gmail_account_email_param, label="gmail_account_email")
    app_password = _optional_secret(settings, settings.gmail_app_password_param, label="gmail_app_password")
    if not (email_address and app_password):
        return {}
    return {
        "EMAIL_ADDRESS": email_address,
        "EMAIL_PASSWORD": app_password,
        "IMAP_HOST": "imap.gmail.com",
        "IMAP_PORT": "993",
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "587",
    }


def _maps_openapi_mcp_env(settings: Settings) -> dict[str, str]:
    base_url = settings.maps_openapi_base_url.strip()
    spec_url = settings.maps_openapi_spec_url.strip()
    auth_token = _optional_secret(settings, settings.maps_openapi_auth_token_param, label="maps_openapi_auth_token")
    headers_secret = _optional_secret(settings, settings.maps_openapi_headers_param, label="maps_openapi_headers")

    env: dict[str, str] = {}
    if base_url:
        env["BASE_URL"] = base_url
    if spec_url:
        env["MAPS_OPENAPI_SPEC_URL"] = spec_url

    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if headers_secret:
        try:
            parsed = json.loads(headers_secret)
        except json.JSONDecodeError as exc:
            logger.warning("maps_openapi_headers secret was not valid JSON: %s", exc)
        else:
            if isinstance(parsed, dict):
                headers.update({str(key): str(value) for key, value in parsed.items()})
            else:
                logger.warning("maps_openapi_headers secret must decode to an object")
    if headers:
        env["HEADERS"] = json.dumps(headers)
    return env


def _resy_mcp_env(settings: Settings) -> dict[str, str]:
    api_key = _optional_secret(settings, settings.resy_api_key_param, label="resy_api_key")
    auth_token = _optional_secret(settings, settings.resy_auth_token_param, label="resy_auth_token")
    if not (api_key and auth_token):
        return {}
    return {
        "RESY_API_KEY": api_key,
        "RESY_AUTH_TOKEN": auth_token,
        "DOTENV_CONFIG_QUIET": "true",
    }


def _opentable_mcp_env(settings: Settings) -> dict[str, str]:
    email = _optional_secret(settings, settings.opentable_email_param, label="opentable_email")
    password = _optional_secret(settings, settings.opentable_password_param, label="opentable_password")
    if not (email and password):
        return {}
    return {
        "OPENTABLE_EMAIL": email,
        "OPENTABLE_PASSWORD": password,
    }


async def _register_optional_mcp(
    *,
    MCPClient,
    tools,
    server_name: str,
    command: str,
    args: list[str],
    prefix: str,
    env: Optional[dict[str, str]] = None,
    tool_filter: Optional[list[str]] = None,
    timeout_seconds: int = 20,
) -> Optional[object]:
    try:
        client = MCPClient(
            server_name=server_name,
            command=command,
            args=args,
            env=env,
        )
        register_kwargs = {"prefix": prefix}
        if tool_filter is not None:
            register_kwargs["tool_filter"] = tool_filter
        await asyncio.wait_for(
            client.register_to_tools(tools, **register_kwargs),
            timeout=max(1, timeout_seconds),
        )
        return client
    except TimeoutError:
        logger.warning("%s MCP registration timed out after %ss, continuing without it", server_name, timeout_seconds)
        return None
    except Exception as exc:
        logger.warning("%s MCP unavailable, continuing without it: %s", server_name, exc)
        return None


async def run_browser_use_task(
    task: str,
    *,
    max_pages: int,
    max_steps: int,
    settings: Settings,
    workspace: Optional[Workspace],
    enable_optional_mcps: bool = False,
) -> str:
    if not settings.browser_use_enabled:
        return await run_browser_task(task, max_pages=max_pages, max_steps=max_steps)

    if workspace is None:
        logger.info("browser-use skipped because workspace was unavailable")
        return await run_browser_task(task, max_pages=max_pages, max_steps=max_steps)

    normalized_task = (
        task.replace("Skiplagged.com", "Skiplagged")
        .replace("skiplagged.com", "Skiplagged")
        .replace("SKIPLAGGED.COM", "Skiplagged")
    )

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

    optional_mcp_guidance = (
        " Use Firecrawl tools for read-only extraction/search when available."
        " Use Skiplagged MCP tools for flights, hotels, flexible-date travel search, and rental cars when available."
        " When using any MCP tool, the action must stay inside the browser-use action envelope."
        " Do not return raw MCP arguments by themselves."
        " Example MCP action item: {\"action\":[{\"skiplagged_sk_flights_search\":{\"origin\":\"NYC\",\"destination\":\"SFO\",\"departureDate\":\"2026-06-03\",\"returnDate\":\"2026-06-07\",\"renderMode\":\"text\"}}]}."
        " If Skiplagged MCP tools are available, prefer them over opening the Skiplagged website directly."
        " Use Google Maps MCP tools for itinerary, place, route, and area-exploration tasks when available."
        " Use maps OpenAPI MCP tools for stable Google Maps/Places/Routes API calls when available."
        " Use reservation MCP tools only when they are explicitly enabled and healthy, and still ask for approval before a booking-commit step."
        " Use Gmail tools only for the dedicated Friday mailbox, OTP retrieval, inbox summaries, confirmations, and verification emails when available."
        " Do not send email unless the operator explicitly asked for it or approved it."
        " Do not delete email or rely on mailbox mutation as the default flow."
    )
    if not enable_optional_mcps:
        optional_mcp_guidance = " Do not attempt MCP travel, maps, reservation, or email actions inside this browser session."

    tools = Tools(
        exclude_actions=[
            "write_file",
            "replace_file",
            "read_file",
        ],
        display_files_in_done_text=False,
    )
    mcp_clients: list[object] = []
    filesystem_mcp = await _register_optional_mcp(
        MCPClient=MCPClient,
        tools=tools,
        server_name="filesystem",
        command="mcp-server-filesystem",
        args=_filesystem_mcp_args(workspace),
        tool_filter=[
            "read_text_file",
            "read_media_file",
            "read_multiple_files",
            "write_file",
            "edit_file",
            "create_directory",
            "list_directory",
            "list_directory_with_sizes",
            "move_file",
            "search_files",
            "directory_tree",
            "get_file_info",
            "list_allowed_directories",
        ],
        prefix="fs_",
        timeout_seconds=settings.mcp_registration_timeout_seconds,
    )
    if filesystem_mcp is not None:
        mcp_clients.append(filesystem_mcp)

    workspace_helper_mcp = await _register_optional_mcp(
        MCPClient=MCPClient,
        tools=tools,
        server_name="friday-workspace",
        command="python",
        args=["-m", "app.mcp_workspace_server"],
        env={
            "FRIDAY_WORKSPACE_ROOT": str(workspace.root),
            "PYTHONPATH": os.environ.get("PYTHONPATH", "/app"),
        },
        tool_filter=[
            "preview_workspace_file",
            "convert_workspace_file_to_markdown",
            "write_workspace_pdf_report",
        ],
        prefix="workspace_helper_",
        timeout_seconds=settings.mcp_registration_timeout_seconds,
    )
    if workspace_helper_mcp is not None:
        mcp_clients.append(workspace_helper_mcp)

    if enable_optional_mcps and settings.firecrawl_mcp_enabled:
        firecrawl_env = _firecrawl_mcp_env(settings)
        if firecrawl_env:
            firecrawl_mcp = await _register_optional_mcp(
                MCPClient=MCPClient,
                tools=tools,
                server_name="firecrawl",
                command="npx",
                args=["-y", "firecrawl-mcp"],
                env=firecrawl_env,
                prefix="firecrawl_",
                timeout_seconds=settings.mcp_registration_timeout_seconds,
            )
            if firecrawl_mcp is not None:
                mcp_clients.append(firecrawl_mcp)
        else:
            logger.warning("firecrawl MCP enabled but FIRECRAWL_API_KEY was unavailable")

    if enable_optional_mcps and settings.skiplagged_mcp_enabled:
        skiplagged_mcp = await _register_optional_mcp(
            MCPClient=MCPClient,
            tools=tools,
            server_name="skiplagged",
            command=settings.skiplagged_mcp_command,
            args=_split_mcp_args(settings.skiplagged_mcp_args),
            prefix="skiplagged_",
            timeout_seconds=settings.mcp_registration_timeout_seconds,
        )
        if skiplagged_mcp is not None:
            mcp_clients.append(skiplagged_mcp)

    if enable_optional_mcps and settings.google_maps_mcp_enabled:
        google_maps_env = _google_maps_mcp_env(settings)
        if google_maps_env.get("GOOGLE_MAPS_API_KEY"):
            google_maps_mcp = await _register_optional_mcp(
                MCPClient=MCPClient,
                tools=tools,
                server_name="google-maps",
                command="npx",
                args=["-y", "@cablate/mcp-google-map", "--stdio"],
                env=google_maps_env,
                prefix="maps_",
                timeout_seconds=settings.mcp_registration_timeout_seconds,
            )
            if google_maps_mcp is not None:
                mcp_clients.append(google_maps_mcp)
        else:
            logger.warning("google maps MCP enabled but GOOGLE_MAPS_API_KEY was unavailable")

    if enable_optional_mcps and settings.gmail_mcp_enabled:
        gmail_env = _gmail_mcp_env(settings)
        if gmail_env:
            gmail_mcp = await _register_optional_mcp(
                MCPClient=MCPClient,
                tools=tools,
                server_name="gmail",
                command="node",
                args=["/opt/friday/mcp/gmail-mcp/dist/index.js"],
                env=gmail_env,
                prefix="gmail_",
                tool_filter=[
                    "listMessages",
                    "findMessage",
                ],
                timeout_seconds=settings.mcp_registration_timeout_seconds,
            )
            if gmail_mcp is not None:
                mcp_clients.append(gmail_mcp)
        else:
            logger.warning("gmail MCP enabled but Gmail account email or app password was unavailable")

    if enable_optional_mcps and settings.maps_openapi_mcp_enabled:
        maps_openapi_env = _maps_openapi_mcp_env(settings)
        maps_openapi_spec_url = settings.maps_openapi_spec_url.strip()
        if maps_openapi_spec_url and maps_openapi_env.get("BASE_URL"):
            maps_openapi_mcp = await _register_optional_mcp(
                MCPClient=MCPClient,
                tools=tools,
                server_name="maps-openapi",
                command=settings.maps_openapi_mcp_command,
                args=_split_mcp_args(settings.maps_openapi_mcp_args) + ["--api", maps_openapi_spec_url],
                env=maps_openapi_env,
                prefix="maps_openapi_",
                timeout_seconds=settings.mcp_registration_timeout_seconds,
            )
            if maps_openapi_mcp is not None:
                mcp_clients.append(maps_openapi_mcp)
        else:
            logger.warning("maps OpenAPI MCP enabled but spec URL or BASE_URL was unavailable")

    if enable_optional_mcps and settings.resy_mcp_enabled:
        resy_env = _resy_mcp_env(settings)
        if resy_env:
            resy_mcp = await _register_optional_mcp(
                MCPClient=MCPClient,
                tools=tools,
                server_name="resy",
                command=settings.resy_mcp_command,
                args=_split_mcp_args(settings.resy_mcp_args),
                env=resy_env,
                prefix="resy_",
                timeout_seconds=settings.mcp_registration_timeout_seconds,
            )
            if resy_mcp is not None:
                mcp_clients.append(resy_mcp)
        else:
            logger.warning("resy MCP enabled but RESY_API_KEY or RESY_AUTH_TOKEN was unavailable")

    if enable_optional_mcps and settings.opentable_mcp_enabled:
        opentable_env = _opentable_mcp_env(settings)
        if opentable_env:
            opentable_mcp = await _register_optional_mcp(
                MCPClient=MCPClient,
                tools=tools,
                server_name="opentable",
                command=settings.opentable_mcp_command,
                args=_split_mcp_args(settings.opentable_mcp_args),
                env=opentable_env,
                prefix="opentable_",
                timeout_seconds=settings.mcp_registration_timeout_seconds,
            )
            if opentable_mcp is not None:
                mcp_clients.append(opentable_mcp)
        else:
            logger.warning("opentable MCP enabled but OPENTABLE_EMAIL or OPENTABLE_PASSWORD was unavailable")

    agent = Agent(
        task=normalized_task,
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
            " Use the official filesystem MCP tools for broad file and directory operations within the allowed workspace roots."
            " Use the Friday workspace helper MCP tools for preview, markdown conversion, and PDF generation."
            + optional_mcp_guidance
            +
            " Do not rely on built-in browser-use file actions."
        ),
    )

    capture_handler = _BrowserUseCaptureHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(capture_handler)

    try:
        history = await asyncio.wait_for(
            agent.run(max_steps=max_steps),
            timeout=max(30, settings.browser_use_task_timeout_seconds),
        )
        include_screenshots_in_result = query_requests_browser_images(task)
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
        if copied_shots and include_screenshots_in_result:
            parts.append("Browser screenshots saved:\n" + "\n".join(f"- {name}" for name in copied_shots))
        if errors:
            parts.append("Browser warnings:\n" + "\n".join(f"- {value}" for value in errors[:8]))
        if not parts:
            parts.append("Browser-use completed without a final summary. Continue with the evidence already gathered.")
        result_text = sanitize_tool_output("\n\n".join(parts))
        if _browser_use_result_needs_retry(result_text):
            logger.info("browser-use result looked too weak, retrying with legacy browser task")
            return await run_browser_task(task, max_pages=max_pages, max_steps=max_steps)
        return result_text
    except TimeoutError as exc:
        logger.warning("browser-use timed out after %ss, falling back to custom browser task", settings.browser_use_task_timeout_seconds)
        fallback = await run_browser_task(task, max_pages=max_pages, max_steps=max_steps)
        return _merge_browser_use_partial_findings(
            partial_findings=_extract_browser_use_partial_findings(capture_handler.messages),
            fallback_result=fallback,
            primary_error=str(exc),
        )
    except Exception as exc:
        logger.warning("browser-use failed, falling back to custom browser task: %s", exc)
        fallback = await run_browser_task(task, max_pages=max_pages, max_steps=max_steps)
        return _merge_browser_use_partial_findings(
            partial_findings=_extract_browser_use_partial_findings(capture_handler.messages),
            fallback_result=fallback,
            primary_error=str(exc),
        )
    finally:
        root_logger.removeHandler(capture_handler)
        for client in mcp_clients:
            try:
                await client.disconnect()
            except Exception:
                logger.info("browser-use MCP disconnect failed", exc_info=True)
        try:
            await browser.stop()
        except Exception:
            logger.info("browser-use browser close failed", exc_info=True)
