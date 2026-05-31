from __future__ import annotations

from typing import Any

from .schemas.execution_state import ExecutionProgressMatrix, ExecutionTier, PauseReason, ProgressOutcome, StrategyFailureSignature

STRATEGY_API_DIRECT = ExecutionTier.API_MCP.value
STRATEGY_STAGEHAND_STEALTH_ACT = ExecutionTier.STAGEHAND.value
STRATEGY_BROWSER_USE_VISUAL_PIVOT = ExecutionTier.BROWSER_USE.value
STRATEGY_SEQUENCE = (
    STRATEGY_API_DIRECT,
    STRATEGY_STAGEHAND_STEALTH_ACT,
    STRATEGY_BROWSER_USE_VISUAL_PIVOT,
)


def default_execution_progress_matrix() -> ExecutionProgressMatrix:
    return ExecutionProgressMatrix.default()


def normalize_execution_progress_matrix(value: Any) -> ExecutionProgressMatrix:
    return ExecutionProgressMatrix.from_legacy(value)


def default_strategy_state() -> dict[str, Any]:
    return default_execution_progress_matrix().to_legacy_strategy_state(
        retry_requested=True,
        switched_strategy=False,
        exhausted=False,
        failed_strategy=STRATEGY_API_DIRECT,
    )


def normalize_strategy_state(value: Any) -> dict[str, Any]:
    matrix = normalize_execution_progress_matrix(value)
    if isinstance(value, dict):
        retry_requested = bool(value.get("retry_requested")) if "retry_requested" in value else not matrix.hard_blocked
        switched_strategy = bool(value.get("switched_strategy"))
        exhausted = bool(value.get("exhausted")) if "exhausted" in value else matrix.hard_blocked
        failed_strategy = str(value.get("failed_strategy") or matrix.current_tier.value).strip().lower()
    else:
        retry_requested = not matrix.hard_blocked
        switched_strategy = False
        exhausted = matrix.hard_blocked
        failed_strategy = matrix.current_tier.value
    return matrix.to_legacy_strategy_state(
        retry_requested=retry_requested,
        switched_strategy=switched_strategy,
        exhausted=exhausted,
        failed_strategy=failed_strategy,
    )


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
        "browser_validation_failed",
        "validation failed",
        "missing explicit success evidence",
        "missing explicit booking confirmation evidence",
        "missing explicit cancellation confirmation evidence",
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
    matrix = normalize_execution_progress_matrix(state_value)
    current_tier = matrix.current_tier
    tier_state = matrix.current_tier_state()
    tier_state.attempt_count += 1
    tier_state.consecutive_no_progress += 1
    tier_state.last_outcome = ProgressOutcome.BLOCKED_RETRYABLE
    tier_state.last_error_message = str(error_message or "")[:1000]
    matrix.active_failure_signature = StrategyFailureSignature(
        raw_message=str(error_message or "")[:1000],
        source_tier=current_tier,
        retryable=True,
    )
    matrix.legacy_last_error = tier_state.last_error_message
    if not matrix.history or matrix.history[-1] != current_tier.value:
        matrix.history.append(current_tier.value)

    should_retry_same = tier_state.consecutive_no_progress < max(1, int(threshold))
    switched = False
    exhausted = False
    if should_retry_same:
        matrix.requires_operator_pause = False
        matrix.hard_blocked = False
        matrix.pause_reason = None
        return matrix.to_legacy_strategy_state(
            retry_requested=True,
            switched_strategy=False,
            exhausted=False,
            failed_strategy=current_tier.value,
        )

    tier_state.exhausted = True
    next_tier = matrix.next_tier()
    if next_tier is None:
        exhausted = True
        matrix.requires_operator_pause = True
        matrix.hard_blocked = True
        matrix.pause_reason = PauseReason.STRATEGY_EXHAUSTED
    else:
        matrix.current_tier = next_tier
        matrix.requires_operator_pause = False
        matrix.hard_blocked = False
        matrix.pause_reason = None
        switched = True

    return matrix.to_legacy_strategy_state(
        retry_requested=not exhausted,
        switched_strategy=switched,
        exhausted=exhausted,
        failed_strategy=current_tier.value,
    )


def strategy_threshold_for_error(
    strategy_name: str,
    error_message: str,
    *,
    default_threshold: int,
) -> int:
    strategy = str(strategy_name or STRATEGY_API_DIRECT).strip().lower()
    threshold = max(1, int(default_threshold))
    if strategy not in STRATEGY_SEQUENCE:
        return threshold
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
