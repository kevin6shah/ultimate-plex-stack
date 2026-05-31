from app.schemas.execution_state import ExecutionProgressMatrix, ExecutionTier, JobContext, PauseReason


def test_execution_progress_matrix_default_uses_api_first() -> None:
    matrix = ExecutionProgressMatrix.default()

    assert matrix.current_tier == ExecutionTier.API_MCP
    assert list(matrix.tiers) == [
        ExecutionTier.API_MCP.value,
        ExecutionTier.STAGEHAND.value,
        ExecutionTier.BROWSER_USE.value,
    ]


def test_execution_progress_matrix_from_legacy_preserves_counts() -> None:
    matrix = ExecutionProgressMatrix.from_legacy(
        {
            "current_strategy": ExecutionTier.STAGEHAND.value,
            "failure_counts": {
                ExecutionTier.API_MCP.value: 3,
                ExecutionTier.STAGEHAND.value: 1,
            },
            "history": [ExecutionTier.API_MCP.value],
            "last_error": "network timeout",
        }
    )

    assert matrix.current_tier == ExecutionTier.STAGEHAND
    assert matrix.tiers[ExecutionTier.API_MCP.value].attempt_count == 3
    assert matrix.tiers[ExecutionTier.API_MCP.value].consecutive_no_progress == 3
    assert matrix.tiers[ExecutionTier.STAGEHAND.value].consecutive_no_progress == 1
    assert matrix.active_failure_signature is not None
    assert matrix.active_failure_signature.raw_message == "network timeout"


def test_execution_progress_matrix_legacy_round_trip_keeps_shape() -> None:
    matrix = ExecutionProgressMatrix.default()
    legacy = matrix.to_legacy_strategy_state(
        retry_requested=True,
        switched_strategy=False,
        exhausted=False,
        failed_strategy=ExecutionTier.API_MCP.value,
    )

    assert legacy["current_strategy"] == ExecutionTier.API_MCP.value
    assert legacy["failure_counts"][ExecutionTier.BROWSER_USE.value] == 0
    assert legacy["retry_requested"] is True
    assert "execution_progress_matrix" in legacy


def test_job_context_defaults_backwards_compatibly() -> None:
    context = JobContext.model_validate({})

    assert context.original_query == ""
    assert context.current_query == ""


def test_execution_progress_matrix_can_mark_terminal_pause() -> None:
    matrix = ExecutionProgressMatrix.default()
    matrix.requires_operator_pause = True
    matrix.hard_blocked = True
    matrix.pause_reason = PauseReason.STRATEGY_EXHAUSTED

    legacy = matrix.to_legacy_strategy_state(
        retry_requested=False,
        switched_strategy=False,
        exhausted=True,
        failed_strategy=ExecutionTier.BROWSER_USE.value,
    )

    assert legacy["retry_requested"] is False
    assert legacy["exhausted"] is True
