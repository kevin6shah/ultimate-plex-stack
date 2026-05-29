from __future__ import annotations

from typing import Any


STRATEGY_API_DIRECT = "api_direct"
STRATEGY_STAGEHAND_STEALTH_ACT = "stagehand_stealth_act"
STRATEGY_BROWSER_USE_VISUAL_PIVOT = "browser_use_visual_pivot"
STRATEGY_SEQUENCE = (
    STRATEGY_API_DIRECT,
    STRATEGY_STAGEHAND_STEALTH_ACT,
    STRATEGY_BROWSER_USE_VISUAL_PIVOT,
)


def default_strategy_state() -> dict[str, Any]:
    return {
        "current_strategy": STRATEGY_API_DIRECT,
        "failure_counts": {name: 0 for name in STRATEGY_SEQUENCE},
        "history": [],
        "last_error": "",
    }


def normalize_strategy_state(value: Any) -> dict[str, Any]:
    state = default_strategy_state()
    if not isinstance(value, dict):
        return state
    current_strategy = str(value.get("current_strategy") or STRATEGY_API_DIRECT).strip().lower()
    if current_strategy not in STRATEGY_SEQUENCE:
        current_strategy = STRATEGY_API_DIRECT
    failure_counts: dict[str, int] = {}
    raw_counts = value.get("failure_counts")
    if isinstance(raw_counts, dict):
        for name in STRATEGY_SEQUENCE:
            try:
                failure_counts[name] = max(0, int(raw_counts.get(name) or 0))
            except Exception:
                failure_counts[name] = 0
    else:
        failure_counts = {name: 0 for name in STRATEGY_SEQUENCE}
    history = [str(item).strip().lower() for item in list(value.get("history") or []) if str(item).strip()]
    state.update(
        {
            "current_strategy": current_strategy,
            "failure_counts": failure_counts,
            "history": [item for item in history if item in STRATEGY_SEQUENCE],
            "last_error": str(value.get("last_error") or "")[:1000],
        }
    )
    return state


def strategy_guidance(strategy_name: str) -> str:
    strategy = str(strategy_name or STRATEGY_API_DIRECT).strip().lower()
    if strategy == STRATEGY_API_DIRECT:
        return (
            "Strategy mode: API_DIRECT. Prefer structured provider tools, deterministic fetch/search, MCPs, and direct APIs. "
            "Do not escalate to browser interaction unless this run is restarted in a different strategy."
        )
    if strategy == STRATEGY_STAGEHAND_STEALTH_ACT:
        return (
            "Strategy mode: STAGEHAND_STEALTH_ACT. Prefer Stagehand-style browser interaction and stealth navigation. "
            "Avoid Browser Use visual automation in this run unless the workflow explicitly pivots strategies."
        )
    if strategy == STRATEGY_BROWSER_USE_VISUAL_PIVOT:
        return (
            "Strategy mode: BROWSER_USE_VISUAL_PIVOT. Use visual browser automation as the primary fallback for interaction-heavy flows. "
            "Do not bounce back to earlier strategies in this run."
        )
    return ""


def is_retryable_interaction_failure(message: str) -> bool:
    normalized = str(message or "").strip().lower()
    if not normalized:
        return False
    markers = (
        "timeout",
        "timed out",
        "selector",
        "target page, context or browser has been closed",
        "context or browser has been closed",
        "browser task unavailable",
        "browser task failed",
        "stagehand browser task failed",
        "stagehand browser task unavailable",
        "stagehand_browser_task_failed",
        "stagehand_browser_task_unavailable",
        "browser_use",
        "err_http2_protocol_error",
        "transporterror",
        "network",
        "connection reset",
        "connection aborted",
        "connection refused",
        "service unavailable",
        "gateway timeout",
        "internal server error",
        "429",
        "cloudflare",
        "rate limit",
    )
    return any(marker in normalized for marker in markers)


def is_unskippable_pause_blocker(message: str) -> bool:
    normalized = str(message or "").strip().lower()
    if not normalized:
        return False
    markers = (
        "otp",
        "verification code",
        "mfa",
        "2fa",
        "captcha",
        "payment",
        "card entry",
        "non_zero_checkout_blocked",
    )
    return any(marker in normalized for marker in markers)


def advance_strategy_state(state_value: Any, error_message: str, *, threshold: int) -> dict[str, Any]:
    state = normalize_strategy_state(state_value)
    strategy = state["current_strategy"]
    failure_counts = dict(state.get("failure_counts") or {})
    failure_counts[strategy] = int(failure_counts.get(strategy) or 0) + 1
    history = list(state.get("history") or [])
    if not history or history[-1] != strategy:
        history.append(strategy)
    last_error = str(error_message or "")[:1000]
    should_retry_same = failure_counts[strategy] < max(1, int(threshold))
    next_strategy = strategy
    exhausted = False
    switched = False
    if not should_retry_same:
        try:
            current_index = STRATEGY_SEQUENCE.index(strategy)
        except ValueError:
            current_index = 0
        if current_index + 1 < len(STRATEGY_SEQUENCE):
            next_strategy = STRATEGY_SEQUENCE[current_index + 1]
            switched = next_strategy != strategy
        else:
            exhausted = True
    return {
        "current_strategy": next_strategy,
        "failure_counts": failure_counts,
        "history": history,
        "last_error": last_error,
        "retry_requested": not exhausted,
        "switched_strategy": switched,
        "exhausted": exhausted,
        "failed_strategy": strategy,
    }


def strategy_threshold_for_error(
    strategy_name: str,
    error_message: str,
    *,
    default_threshold: int,
) -> int:
    strategy = str(strategy_name or STRATEGY_API_DIRECT).strip().lower()
    normalized = str(error_message or "").strip().lower()
    threshold = max(1, int(default_threshold))
    if strategy != STRATEGY_API_DIRECT:
        return threshold
    if (
        "structured restaurant availability failed in api_direct mode" in normalized
        and "provider=resy" in normalized
        and "500" in normalized
    ):
        return 1
    return threshold


def stall_thresholds_for_strategy(strategy_name: str, current_step: str, *, running_default: int, preflight_default: int) -> tuple[int, int]:
    strategy = str(strategy_name or STRATEGY_API_DIRECT).strip().lower()
    step = str(current_step or "").strip().lower()
    if step == "running_agent":
        if strategy == STRATEGY_API_DIRECT:
            return 8, max(running_default, 180)
        if strategy == STRATEGY_STAGEHAND_STEALTH_ACT:
            return 10, max(running_default, 300)
        if strategy == STRATEGY_BROWSER_USE_VISUAL_PIVOT:
            return 12, max(running_default, 420)
        return 8, max(running_default, 180)
    return 6, max(preflight_default, 120)
