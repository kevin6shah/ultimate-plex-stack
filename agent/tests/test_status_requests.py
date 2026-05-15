from types import SimpleNamespace

from app.jobs import AgentJob, JobSource, JobStatus, TaskClass
from app.main import _format_status_message, _is_status_request


def test_status_request_detection() -> None:
    assert _is_status_request("what's the status on that task?")
    assert _is_status_request("any update?")
    assert _is_status_request("did it finish?")
    assert not _is_status_request("find restaurant reservations for Sunday")


def test_format_status_message_for_running_job() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="research cameras",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
        current_step="comparing sources",
        latest_checkpoint_summary="reviewing travel and vlogging options",
    )
    state = SimpleNamespace(get_latest_checkpoint=lambda _job_id: None)
    text = _format_status_message(state, job)
    assert "Still working on your latest task." in text
    assert "Current step: comparing sources" in text
    assert "Latest update: reviewing travel and vlogging options" in text


def test_format_status_message_for_interrupted_job() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="research cameras",
        task_class=TaskClass.HEAVY,
        status=JobStatus.INTERRUPTED,
        latest_checkpoint_summary="worker interrupted during browser step",
    )
    state = SimpleNamespace(get_latest_checkpoint=lambda _job_id: None)
    text = _format_status_message(state, job)
    assert "interrupted" in text
    assert "resume that task" in text
