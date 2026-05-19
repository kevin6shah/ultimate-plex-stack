from __future__ import annotations

from dataclasses import dataclass

import app.skiplagged as skiplagged
from app.skiplagged import (
    _call_result_to_text,
    _retry_after_seconds,
    _skiplagged_outage_seconds,
    _split_mcp_args,
)


@dataclass
class _FakeTextContent:
    text: str


@dataclass
class _FakeDataContent:
    data: dict[str, object]


@dataclass
class _FakeResult:
    content: list[object]


def test_split_mcp_args_handles_remote_bridge_default() -> None:
    assert _split_mcp_args("-y mcp-remote https://mcp.skiplagged.com/mcp") == [
        "-y",
        "mcp-remote",
        "https://mcp.skiplagged.com/mcp",
    ]


def test_call_result_to_text_prefers_text_content() -> None:
    result = _FakeResult(content=[_FakeTextContent("first"), _FakeTextContent("second")])
    assert _call_result_to_text(result) == "first\n\nsecond"


def test_call_result_to_text_renders_data_content() -> None:
    result = _FakeResult(content=[_FakeDataContent({"price": 409, "currency": "USD"})])
    rendered = _call_result_to_text(result)
    assert '"price": 409' in rendered
    assert '"currency": "USD"' in rendered


def test_skiplagged_outage_window_can_be_set_and_reset() -> None:
    skiplagged._SKIPLAGGED_OUTAGE_UNTIL = 123.0
    assert skiplagged._SKIPLAGGED_OUTAGE_UNTIL == 123.0
    skiplagged._SKIPLAGGED_OUTAGE_UNTIL = 0.0


def test_retry_after_seconds_parses_json_style_field() -> None:
    message = 'HTTP 429 {"error":"rate limited","retry_after":30}'
    assert _retry_after_seconds(message) == 30


def test_skiplagged_outage_seconds_uses_retry_after_when_present() -> None:
    message = 'Cloudflare 1015 {"retry_after":45}'
    assert _skiplagged_outage_seconds(message) == 45


def test_skiplagged_outage_seconds_detects_rate_limit_without_retry_after() -> None:
    message = "429 Error 1015: You are being rate limited."
    assert _skiplagged_outage_seconds(message) == 60
