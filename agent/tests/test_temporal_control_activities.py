import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

fake_temporalio = types.ModuleType("temporalio")
fake_temporalio.activity = types.SimpleNamespace(defn=lambda fn: fn)
fake_temporalio_exceptions = types.ModuleType("temporalio.exceptions")
fake_temporalio_exceptions.ApplicationError = RuntimeError
sys.modules.setdefault("temporalio", fake_temporalio)
sys.modules.setdefault("temporalio.exceptions", fake_temporalio_exceptions)

from app.jobs import ControlCommand, ControlSignal
from app.temporal_control_activities import _stop_reason_from_control_signal, prepare_heavy_job_followup_claim


def test_stop_reason_uses_user_stop_when_signal_says_stopped_by_user() -> None:
    state = SimpleNamespace(
        get_latest_control_signal=lambda _job_id: ControlSignal(command=ControlCommand.STOP, note="stopped by user")
    )

    current_step, error_message = _stop_reason_from_control_signal(state, "job-1")

    assert current_step == "stopped by user"
    assert error_message == "stopped by user"


def test_stop_reason_uses_auto_stop_message_for_stall_signal() -> None:
    state = SimpleNamespace(
        get_latest_control_signal=lambda _job_id: ControlSignal(
            command=ControlCommand.STOP,
            note="auto-stopped after repeated identical worker heartbeats with no meaningful progress",
        )
    )

    current_step, error_message = _stop_reason_from_control_signal(state, "job-1")

    assert current_step == "auto-stopped after repeated identical steps"
    assert "appeared stuck" in error_message


def test_stop_reason_suppresses_present_findings_interrupt_message() -> None:
    state = SimpleNamespace(
        get_latest_control_signal=lambda _job_id: ControlSignal(
            command=ControlCommand.STOP,
            note="stopped to present current findings",
        )
    )

    current_step, error_message = _stop_reason_from_control_signal(state, "job-1")

    assert current_step == "stopped to present current findings"
    assert error_message == ""


def test_stop_reason_defaults_to_generic_interrupted_without_signal() -> None:
    state = SimpleNamespace(get_latest_control_signal=lambda _job_id: None)

    current_step, error_message = _stop_reason_from_control_signal(state, "job-1")

    assert current_step == "interrupted"
    assert "interruption before that task finished" in error_message


def test_prepare_followup_claim_uses_persisted_pending_followup_when_signal_race_loses_text() -> None:
    merged_updates: list[dict[str, object]] = []
    state = SimpleNamespace(
        get_job=lambda _job_id: SimpleNamespace(
            job_id="job-1",
            metadata={"pending_followup_text": "Prefer nonstop if the price difference is not too big."},
        ),
        get_latest_checkpoint=lambda _job_id: None,
        merge_job_metadata=lambda _job_id, updates: merged_updates.append(dict(updates)),
        update_job_status=lambda *_args, **_kwargs: None,
    )

    with patch("app.temporal_control_activities._store", return_value=state), patch(
        "app.temporal_control_activities.build_contextual_heavy_followup_query",
        return_value="followup query",
    ), patch(
        "app.temporal_control_activities.build_heavy_claim",
        return_value={
            "strategy_state": {"current_strategy": "api_direct"},
            "execution_progress_matrix": {"current_tier": "api_mcp"},
            "job_context": {"original_query": "Find flights"},
        },
    ):
        claim = __import__("asyncio").run(prepare_heavy_job_followup_claim("job-1", ""))

    assert claim["job_context"] == {"original_query": "Find flights"}
    assert merged_updates[0]["pending_followup_text"] is None
