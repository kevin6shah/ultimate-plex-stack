import sys
import types
from types import SimpleNamespace

fake_temporalio = types.ModuleType("temporalio")
fake_temporalio.activity = types.SimpleNamespace(defn=lambda fn: fn)
sys.modules.setdefault("temporalio", fake_temporalio)

from app.jobs import ControlCommand, ControlSignal
from app.temporal_control_activities import _stop_reason_from_control_signal


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


def test_stop_reason_defaults_to_generic_interrupted_without_signal() -> None:
    state = SimpleNamespace(get_latest_control_signal=lambda _job_id: None)

    current_step, error_message = _stop_reason_from_control_signal(state, "job-1")

    assert current_step == "interrupted"
    assert "interrupted before it finished" in error_message
