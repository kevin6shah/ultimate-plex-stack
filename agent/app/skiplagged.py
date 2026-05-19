from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
from datetime import timedelta
from typing import Any

from .settings import Settings


logger = logging.getLogger(__name__)
_SKIPLAGGED_OUTAGE_UNTIL = 0.0


def _split_mcp_args(value: str) -> list[str]:
    if not value.strip():
        return []
    return shlex.split(value)


def _content_item_to_text(item: Any) -> str:
    text = getattr(item, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    data = getattr(item, "data", None)
    if data is not None:
        try:
            return json.dumps(data, indent=2, ensure_ascii=True)
        except TypeError:
            return str(data)
    return str(item)


def _call_result_to_text(result: Any) -> str:
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts = [_content_item_to_text(item).strip() for item in content]
        rendered = "\n\n".join(part for part in parts if part)
        if rendered:
            return rendered
    structured_content = getattr(result, "structuredContent", None)
    if structured_content is not None:
        try:
            return json.dumps(structured_content, indent=2, ensure_ascii=True)
        except TypeError:
            return str(structured_content)
    return str(result)


def _retry_after_seconds(message: str) -> int:
    match = re.search(r'"retry_after"\s*:\s*(\d+)', message, re.IGNORECASE)
    if match:
        return max(1, int(match.group(1)))
    match = re.search(r"retry[_ -]?after[^0-9]*(\d+)", message, re.IGNORECASE)
    if match:
        return max(1, int(match.group(1)))
    return 0


def _skiplagged_outage_seconds(message: str) -> int:
    lowered = message.lower()
    retry_after = _retry_after_seconds(message)
    if retry_after:
        return retry_after
    if "429" in lowered or "1015" in lowered or "rate limit" in lowered or "rate-limit" in lowered or "rate limited" in lowered:
        return 60
    if "502" in lowered or "bad gateway" in lowered or "cloudflare" in lowered:
        return 60
    return 0


async def call_skiplagged_tool(
    settings: Settings,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_seconds: int | None = None,
) -> str:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    global _SKIPLAGGED_OUTAGE_UNTIL
    now = time.monotonic()
    if now < _SKIPLAGGED_OUTAGE_UNTIL:
        remaining = int(max(1, _SKIPLAGGED_OUTAGE_UNTIL - now))
        raise RuntimeError(
            "Skiplagged MCP upstream outage is still in effect for this task. "
            f"Do not retry Skiplagged tools for about {remaining} more seconds; switch to another live travel source."
        )

    env = os.environ.copy()
    env.setdefault("HOME", os.environ.get("HOME", "/tmp"))

    server = StdioServerParameters(
        command=settings.skiplagged_mcp_command,
        args=_split_mcp_args(settings.skiplagged_mcp_args),
        env=env,
    )
    effective_timeout = timeout_seconds or max(30, settings.browser_use_task_timeout_seconds)
    read_timeout = timedelta(seconds=effective_timeout)

    async def _invoke() -> str:
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments, read_timeout_seconds=read_timeout)
                return _call_result_to_text(result)

    try:
        return await asyncio.wait_for(_invoke(), timeout=effective_timeout + 10)
    except asyncio.TimeoutError as exc:
        message = f"Skiplagged MCP timed out after {effective_timeout}s"
        outage_seconds = _skiplagged_outage_seconds(message) or 60
        _SKIPLAGGED_OUTAGE_UNTIL = time.monotonic() + outage_seconds
        raise RuntimeError(message) from exc
    except Exception as exc:
        message = str(exc)
        outage_seconds = _skiplagged_outage_seconds(message)
        if outage_seconds > 0:
            _SKIPLAGGED_OUTAGE_UNTIL = time.monotonic() + outage_seconds
        raise
