from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ExecutionTier(str, Enum):
    API_MCP = "api_direct"
    STAGEHAND = "stagehand_stealth_act"
    BROWSER_USE = "browser_use_visual_pivot"


DEFAULT_TIER_ORDER = [
    ExecutionTier.API_MCP,
    ExecutionTier.STAGEHAND,
    ExecutionTier.BROWSER_USE,
]


class PauseReason(str, Enum):
    NEED_BOOKING_CONFIRMATION = "need_booking_confirmation"
    NEED_LOGIN = "need_login"
    NEED_OTP = "need_otp"
    NEED_PAYMENT = "need_payment"
    NEED_MISSING_REQUIRED_INPUT = "need_missing_required_input"
    STRATEGY_EXHAUSTED = "strategy_exhausted"
    MANUAL_CANCELLATION_REQUIRED = "manual_cancellation_required"


class ProgressOutcome(str, Enum):
    PROGRESSED = "progressed"
    NO_PROGRESS = "no_progress"
    BLOCKED_RETRYABLE = "blocked_retryable"
    BLOCKED_TERMINAL = "blocked_terminal"
    VALIDATED_SUCCESS = "validated_success"
    VALIDATED_FAILURE = "validated_failure"


class StrategyFailureSignature(BaseModel):
    error_code: str = ""
    raw_message: str = ""
    source_tier: ExecutionTier = ExecutionTier.API_MCP
    retryable: bool = True
    timestamp: str = ""


class TierProgressState(BaseModel):
    tier: ExecutionTier
    attempt_count: int = 0
    consecutive_no_progress: int = 0
    last_outcome: ProgressOutcome | None = None
    last_error_code: str = ""
    last_error_message: str = ""
    last_progress_at: str = ""
    last_validation_at: str = ""
    exhausted: bool = False


class ExecutionProgressMatrix(BaseModel):
    current_tier: ExecutionTier = ExecutionTier.API_MCP
    tier_order: list[ExecutionTier] = Field(default_factory=lambda: list(DEFAULT_TIER_ORDER))
    tiers: dict[str, TierProgressState] = Field(default_factory=dict)
    active_failure_signature: StrategyFailureSignature | None = None
    last_meaningful_artifact: str = ""
    last_successful_action: str = ""
    last_operator_visible_summary: str = ""
    requires_operator_pause: bool = False
    pause_reason: PauseReason | None = None
    hard_blocked: bool = False
    completed: bool = False
    validated_success: bool = False
    version: int = 1
    history: list[str] = Field(default_factory=list)
    legacy_last_error: str = ""

    @model_validator(mode="after")
    def _ensure_tiers(self) -> "ExecutionProgressMatrix":
        existing = dict(self.tiers)
        normalized: dict[str, TierProgressState] = {}
        for tier in self.tier_order:
            key = tier.value
            state = existing.get(key)
            if isinstance(state, TierProgressState):
                normalized[key] = state
            elif isinstance(state, dict):
                normalized[key] = TierProgressState.model_validate({"tier": tier, **state})
            else:
                normalized[key] = TierProgressState(tier=tier)
        self.tiers = normalized
        self.history = [item for item in self.history if item in normalized]
        if self.current_tier.value not in normalized:
            self.current_tier = self.tier_order[0]
        return self

    @classmethod
    def default(cls) -> "ExecutionProgressMatrix":
        return cls()

    @classmethod
    def from_legacy(cls, value: Any) -> "ExecutionProgressMatrix":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return cls.default()
        if "current_tier" in value or "tiers" in value:
            return cls.model_validate(value)

        raw_tier = str(value.get("current_strategy") or ExecutionTier.API_MCP.value).strip().lower()
        try:
            current_tier = ExecutionTier(raw_tier)
        except ValueError:
            current_tier = ExecutionTier.API_MCP

        raw_counts = value.get("failure_counts") if isinstance(value.get("failure_counts"), dict) else {}
        tiers: dict[str, TierProgressState] = {}
        for tier in DEFAULT_TIER_ORDER:
            try:
                count = max(0, int(raw_counts.get(tier.value) or 0))
            except Exception:
                count = 0
            tiers[tier.value] = TierProgressState(
                tier=tier,
                attempt_count=count,
                consecutive_no_progress=count,
                last_error_message=str(value.get("last_error") or "")[:1000] if tier == current_tier else "",
                exhausted=bool(count) and tier != current_tier and tier.value in list(value.get("history") or []),
            )
        history = [
            item
            for item in [str(item).strip().lower() for item in list(value.get("history") or []) if str(item).strip()]
            if item in {tier.value for tier in DEFAULT_TIER_ORDER}
        ]
        legacy_last_error = str(value.get("last_error") or "")[:1000]
        retry_requested = bool(value.get("retry_requested")) if "retry_requested" in value else True
        exhausted = bool(value.get("exhausted")) if "exhausted" in value else not retry_requested
        hard_blocked = exhausted and not retry_requested
        return cls(
            current_tier=current_tier,
            tiers=tiers,
            history=history,
            legacy_last_error=legacy_last_error,
            active_failure_signature=StrategyFailureSignature(
                raw_message=legacy_last_error,
                source_tier=current_tier,
                retryable=not hard_blocked,
            )
            if legacy_last_error
            else None,
            requires_operator_pause=hard_blocked,
            pause_reason=PauseReason.STRATEGY_EXHAUSTED if hard_blocked else None,
            hard_blocked=hard_blocked,
        )

    def current_tier_state(self) -> TierProgressState:
        return self.tiers[self.current_tier.value]

    def next_tier(self) -> ExecutionTier | None:
        try:
            current_index = self.tier_order.index(self.current_tier)
        except ValueError:
            current_index = 0
        next_index = current_index + 1
        if next_index >= len(self.tier_order):
            return None
        return self.tier_order[next_index]

    def to_legacy_strategy_state(
        self,
        *,
        retry_requested: bool | None = None,
        switched_strategy: bool = False,
        exhausted: bool | None = None,
        failed_strategy: str = "",
    ) -> dict[str, Any]:
        counts = {
            tier.value: self.tiers[tier.value].consecutive_no_progress
            for tier in self.tier_order
        }
        final_exhausted = self.hard_blocked if exhausted is None else bool(exhausted)
        final_retry_requested = (not final_exhausted) if retry_requested is None else bool(retry_requested)
        last_error = ""
        if self.active_failure_signature is not None and self.active_failure_signature.raw_message:
            last_error = self.active_failure_signature.raw_message[:1000]
        elif self.legacy_last_error:
            last_error = self.legacy_last_error[:1000]
        return {
            "current_strategy": self.current_tier.value,
            "failure_counts": counts,
            "history": list(self.history),
            "last_error": last_error,
            "retry_requested": final_retry_requested,
            "switched_strategy": bool(switched_strategy),
            "exhausted": final_exhausted,
            "failed_strategy": failed_strategy or self.current_tier.value,
            "execution_progress_matrix": self.model_dump(mode="json"),
        }


class JobContext(BaseModel):
    original_query: str = ""
    current_query: str = ""
    conversation_id: str = "default"
    source: str = ""
    resume_from_job_id: str | None = None
    latest_findings_summary: str = ""
    latest_checkpoint_summary: str = ""
    active_topic_key: str = ""
    thread_context_excerpt: str = ""
    operator_constraints: str = ""
    created_at: str = ""
    updated_at: str = ""
