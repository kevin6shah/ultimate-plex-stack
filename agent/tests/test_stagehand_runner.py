from dataclasses import replace

from app.settings import Settings
from app.stagehand_runner import (
    _merge_stagehand_partial_findings,
    _stagehand_chrome_path,
    _stagehand_model_name,
    _stagehand_result_needs_fallback,
)


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


def test_merge_stagehand_partial_findings_preserves_message_and_partial_context() -> None:
    merged = _merge_stagehand_partial_findings(
        message="I reached the reservation page but could not submit the form.",
        partial_findings="- Observed page: https://example.com/booking\n- Found a 7:30 PM slot.",
        primary_error="captcha challenge blocked the final step.",
    )
    assert "reservation page" in merged
    assert "Found a 7:30 PM slot" in merged
    assert "captcha challenge" in merged
