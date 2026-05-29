from app.strategy_runtime import (
    STRATEGY_API_DIRECT,
    STRATEGY_BROWSER_USE_VISUAL_PIVOT,
    STRATEGY_STAGEHAND_STEALTH_ACT,
    advance_strategy_state,
    default_strategy_state,
    is_retryable_interaction_failure,
    normalize_strategy_state,
    stall_thresholds_for_strategy,
    strategy_threshold_for_error,
)


def test_retryable_interaction_failure_detects_selector_and_timeout_markers() -> None:
    assert is_retryable_interaction_failure("selector #submit timed out")
    assert is_retryable_interaction_failure("ERR_HTTP2_PROTOCOL_ERROR on opentable")
    assert not is_retryable_interaction_failure("verification code required")


def test_advance_strategy_state_retries_then_switches_after_threshold() -> None:
    state = default_strategy_state()
    for _ in range(3):
        state = advance_strategy_state(state, "selector timed out", threshold=4)
        assert state["current_strategy"] == STRATEGY_API_DIRECT
        assert state["retry_requested"] is True
        assert state["exhausted"] is False
    state = advance_strategy_state(state, "selector timed out", threshold=4)
    assert state["current_strategy"] == STRATEGY_STAGEHAND_STEALTH_ACT
    assert state["retry_requested"] is True
    assert state["switched_strategy"] is True


def test_advance_strategy_state_exhausts_after_last_strategy() -> None:
    state = normalize_strategy_state(
        {
            "current_strategy": STRATEGY_BROWSER_USE_VISUAL_PIVOT,
            "failure_counts": {
                STRATEGY_API_DIRECT: 4,
                STRATEGY_STAGEHAND_STEALTH_ACT: 4,
                STRATEGY_BROWSER_USE_VISUAL_PIVOT: 3,
            },
        }
    )
    state = advance_strategy_state(state, "network timeout", threshold=4)
    assert state["current_strategy"] == STRATEGY_BROWSER_USE_VISUAL_PIVOT
    assert state["retry_requested"] is False
    assert state["exhausted"] is True


def test_stall_thresholds_pivot_api_direct_faster_than_browser_visual() -> None:
    api_repeat, api_seconds = stall_thresholds_for_strategy(
        STRATEGY_API_DIRECT,
        "running_agent",
        running_default=120,
        preflight_default=60,
    )
    browser_repeat, browser_seconds = stall_thresholds_for_strategy(
        STRATEGY_BROWSER_USE_VISUAL_PIVOT,
        "running_agent",
        running_default=120,
        preflight_default=60,
    )
    assert api_repeat == 8
    assert api_seconds == 180
    assert browser_repeat == 12
    assert browser_seconds == 420


def test_strategy_threshold_for_resy_structured_500_pivots_immediately() -> None:
    threshold = strategy_threshold_for_error(
        STRATEGY_API_DIRECT,
        "service unavailable: structured restaurant availability failed in api_direct mode. provider=resy venue=Foo venue_id=123 error=Resy GET /4/find?x=1 -> 500",
        default_threshold=4,
    )
    assert threshold == 1


def test_strategy_threshold_for_other_errors_keeps_default() -> None:
    threshold = strategy_threshold_for_error(
        STRATEGY_API_DIRECT,
        "selector timed out while clicking submit",
        default_threshold=4,
    )
    assert threshold == 4
