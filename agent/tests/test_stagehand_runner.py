from dataclasses import replace

from app.settings import Settings
from app.stagehand_runner import (
    _coerce_stagehand_search_results,
    _format_stagehand_summary,
    _merge_stagehand_partial_findings,
    _stagehand_browser_payload,
    _stagehand_chrome_path,
    _stagehand_extract_explicit_urls,
    _stagehand_interaction_steps,
    _stagehand_interaction_instruction,
    _stagehand_model_name,
    _stagehand_result_needs_fallback,
    _stagehand_search_url,
    _stagehand_task_needs_interaction,
    _stagehand_verification_instruction,
)
from app.workspace import Workspace


def test_stagehand_model_name_preserves_provider_prefixed_value() -> None:
    settings = replace(Settings(), stagehand_model="deepseek/deepseek-chat")
    assert _stagehand_model_name(settings) == "deepseek/deepseek-chat"


def test_stagehand_model_name_normalizes_colon_format() -> None:
    settings = replace(Settings(), stagehand_model="deepseek:deepseek-chat")
    assert _stagehand_model_name(settings) == "deepseek/deepseek-chat"


def test_stagehand_result_needs_fallback_for_low_quality_summary() -> None:
    assert _stagehand_result_needs_fallback("I need more information before I can complete this.")
    assert _stagehand_result_needs_fallback("The page timed out before I could finish.")
    assert not _stagehand_result_needs_fallback("Best available option: Hotel Vibra Mare, about 12 minutes by taxi to Ushuaia.")


def test_stagehand_chrome_path_prefers_explicit_setting() -> None:
    settings = replace(Settings(), stagehand_local_chrome_path="/custom/chrome")
    assert _stagehand_chrome_path(settings) == "/custom/chrome"


def test_stagehand_browser_payload_includes_launch_options(tmp_path) -> None:
    settings = replace(Settings(), stagehand_local_headless=False)
    workspace = Workspace(tmp_path)
    payload = _stagehand_browser_payload(
        settings=settings,
        workspace=workspace,
        chrome_path="/custom/chrome",
        task="book a free reservation",
    )
    assert payload["type"] == "local"
    launch_options = payload["launchOptions"]
    assert "--no-sandbox" in launch_options["args"]
    assert "--disable-dev-shm-usage" in launch_options["args"]
    assert "--disable-blink-features=AutomationControlled" in launch_options["args"]
    assert any(arg.startswith("--user-agent=") for arg in launch_options["args"])
    assert any(arg.startswith("--window-size=") for arg in launch_options["args"])
    assert launch_options["chromiumSandbox"] is False
    assert launch_options["headless"] is False
    assert launch_options["executablePath"] == "/custom/chrome"
    assert launch_options["preserveUserDataDir"] is True
    assert launch_options["acceptDownloads"] is True
    assert launch_options["ignoreHTTPSErrors"] is True
    assert launch_options["locale"] == "en-US"
    assert launch_options["viewport"]["width"] > 0
    assert launch_options["viewport"]["height"] > 0
    assert str(tmp_path / ".stagehand" / "profile") == launch_options["userDataDir"]
    assert str(tmp_path / ".stagehand" / "downloads") == launch_options["downloadsPath"]


def test_merge_stagehand_partial_findings_preserves_message_and_partial_context() -> None:
    merged = _merge_stagehand_partial_findings(
        message="I reached the reservation page but could not submit the form.",
        partial_findings="- Observed page: https://example.com/booking\n- Found a 7:30 PM slot.",
        primary_error="captcha challenge blocked the final step.",
    )
    assert "reservation page" in merged
    assert "Found a 7:30 PM slot" in merged
    assert "captcha challenge" in merged


def test_stagehand_extract_explicit_urls_finds_http_links() -> None:
    task = "Check https://example.com/pricing and compare it to https://example.org/docs."
    assert _stagehand_extract_explicit_urls(task) == [
        "https://example.com/pricing",
        "https://example.org/docs",
    ]


def test_stagehand_search_url_uses_duckduckgo_html() -> None:
    url = _stagehand_search_url("Find best boutique hotels in Ibiza")
    assert url.startswith("https://html.duckduckgo.com/html/?q=")
    assert "boutique+hotels+in+Ibiza" in url


def test_stagehand_task_needs_interaction_for_booking_flow() -> None:
    assert _stagehand_task_needs_interaction(
        "Use the booking flow, set party size to 3, and reveal available times."
    )
    assert not _stagehand_task_needs_interaction("Summarize the restaurant's homepage.")


def test_stagehand_interaction_instruction_is_non_destructive() -> None:
    instruction = _stagehand_interaction_instruction("Find dinner slots")
    assert "dismiss non-essential cookie banners" in instruction
    assert "Set or confirm the requested date, party size, and reservation controls" in instruction
    assert "Do not submit or finalize any booking." in instruction


def test_stagehand_interaction_steps_prioritize_date_then_party_size() -> None:
    steps = _stagehand_interaction_steps(
        "Open the booking flow for 2026-05-28 for 3 people and reveal visible reservation times."
    )
    assert "dismiss non-essential cookie banners" in steps[0]
    assert steps[1].startswith("Open the date selector and set the reservation date to 2026-05-28")
    assert steps[2].startswith("Set the party size selector to 3 guests")
    assert steps[3].startswith("Open the reservation time selector or reservation results area")


def test_stagehand_verification_instruction_understands_relative_date_labels() -> None:
    instruction = _stagehand_verification_instruction(
        "Use the booking flow for 2026-05-28 for 3 people and summarize visible times."
    )
    assert "treat the requested date as satisfied" in instruction
    assert "calendar shows 2026-05-28 or its human-readable equivalent as selected" in instruction
    assert "current page URL includes date=2026-05-28" in instruction
    assert "If the guests selector shows 3 Guests" in instruction
    assert "current page URL includes seats=3" in instruction


def test_coerce_stagehand_search_results_discards_invalid_rows() -> None:
    rows = _coerce_stagehand_search_results(
        [
            {"title": "Good", "url": "https://example.com", "snippet": "A result"},
            {"title": "Bad", "url": "javascript:void(0)"},
            "not-a-dict",
        ]
    )
    assert rows == [{"title": "Good", "url": "https://example.com", "snippet": "A result"}]


def test_format_stagehand_summary_renders_sources_and_findings() -> None:
    rendered = _format_stagehand_summary(
        summary="Hotel A is the closest option with decent reviews.",
        findings=["About 10 minutes from the venue", "Roughly $240/night on the checked dates"],
        validation_evidence=["Confirmation number: ABC123"],
        current_url="https://example.com/hotel-a",
        search_results=[{"title": "Hotel A", "url": "https://example.com/hotel-a", "snippet": "Closest option"}],
        blocker="",
    )
    assert "Hotel A is the closest option" in rendered
    assert "Validation evidence:" in rendered
    assert "Candidate sources:" in rendered
    assert "Current page: https://example.com/hotel-a" in rendered
