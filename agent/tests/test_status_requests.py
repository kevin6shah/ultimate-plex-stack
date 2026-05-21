from datetime import datetime
from types import SimpleNamespace

from app.heavy_job_runtime import progress_notification_text, status_summary_for_query
from app.jobs import AgentJob, CheckpointPayload, JobSource, JobStatus, TaskClass
from app.main import (
    _active_job_is_stale,
    _build_paused_input_resume_query,
    _clean_user_facing_result,
    _format_status_message,
    _format_tasks_list,
    _humanize_worker_failure,
    _is_input_reply,
    _is_stop_all_request,
    _is_status_request,
    _job_indicates_user_stop,
    _job_accepts_live_worker_updates,
    _looks_like_natural_input_reply,
    _partial_findings_text,
    _resolve_stop_target,
    _stop_jobs,
    _strip_input_reply_prefix,
    _wants_findings_after_stop,
)


def test_status_request_detection() -> None:
    assert _is_status_request("what's the status on that task?")
    assert _is_status_request("any update?")
    assert _is_status_request("did it finish?")
    assert not _is_status_request("find restaurant reservations for Sunday")


def test_stop_all_request_detection() -> None:
    assert _is_stop_all_request("stop all")
    assert _is_stop_all_request("cancel all tasks")
    assert _is_stop_all_request("abort everything")
    assert not _is_stop_all_request("stop task 1")


def test_stop_request_detection_catches_stop_the_agent_and_stop_it() -> None:
    from app.main import _is_stop_request

    assert _is_stop_request("Any findings? Stop the agent and reveal the findings")
    assert _is_stop_request("stop it")


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


def test_format_status_message_for_running_job_with_interrupted_checkpoint() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="research cameras",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
        current_step="running_agent",
        latest_checkpoint_summary="interrupted: preparing a report and output files",
    )
    state = SimpleNamespace(get_latest_checkpoint=lambda _job_id: None)
    text = _format_status_message(state, job)
    assert "appears interrupted" in text
    assert "Still working on your latest task." not in text
    assert "resume that task" in text


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
    assert "stopped by you" not in text


def test_humanize_worker_failure_for_non_user_interruption() -> None:
    text = _humanize_worker_failure(
        "research cameras",
        "worker interrupted before output upload",
        JobStatus.INTERRUPTED,
    )
    assert "interrupted before it finished" in text


def test_humanize_worker_failure_for_activity_cancelled_stop() -> None:
    text = _humanize_worker_failure(
        "research cameras",
        "Activity cancelled",
        JobStatus.INTERRUPTED,
    )
    assert text == "I stopped that task."


def test_job_indicates_user_stop_for_existing_interrupted_job() -> None:
    job = AgentJob(
        source=JobSource.SIRI,
        query="research cameras",
        task_class=TaskClass.HEAVY,
        status=JobStatus.INTERRUPTED,
        current_step="stopped by user",
        error_message="stopped by user",
    )
    assert _job_indicates_user_stop(job, "") is True


def test_format_status_message_for_paused_input_job() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="find dinner reservations",
        task_class=TaskClass.HEAVY,
        status=JobStatus.PAUSED_FOR_INPUT,
        current_step="waiting_for_user_input",
        latest_checkpoint_summary="blocked on party size",
    )
    checkpoint = CheckpointPayload(
        summary="blocked on party size",
        current_step="waiting_for_user_input",
        resume_instructions="Use the provided party size and continue the reservation flow.",
        metadata={
            "input_question": "How many people should I book for?",
            "input_details": "The restaurant flow cannot continue without party size.",
        },
    )
    state = SimpleNamespace(get_latest_checkpoint=lambda _job_id: checkpoint)
    text = _format_status_message(state, job)
    assert "paused and waiting for your input" in text
    assert "How many people should I book for?" in text
    assert "answer: ..." in text
    assert "Current step: waiting for your reply" in text
    assert "**" not in text
    assert "\n\nWhat I need:\n" in text


def test_paused_input_reply_prefix_is_detected_and_removed() -> None:
    assert _is_input_reply("answer: two people at 7pm")
    assert _is_input_reply("resume with the 9pm option")
    assert _strip_input_reply_prefix("continue with the Friday evening option") == "the Friday evening option"


def test_short_natural_reply_is_treated_as_paused_input() -> None:
    assert _looks_like_natural_input_reply("West Village, 2 people, 7pm, Italian, under $200")
    assert not _looks_like_natural_input_reply("Plan a simple coffee-to-park walking itinerary near SoHo tomorrow afternoon")
    assert not _looks_like_natural_input_reply("What's the status?")


def test_format_tasks_list_humanizes_status_and_step() -> None:
    jobs = [
        AgentJob(
            source=JobSource.SIRI,
            query="find dinner reservations",
            task_class=TaskClass.HEAVY,
            status=JobStatus.PAUSED_FOR_INPUT,
            current_step="waiting_for_user_input",
            latest_checkpoint_summary="blocked on party size",
        )
    ]
    text = _format_tasks_list(SimpleNamespace(), jobs)
    assert "paused for your input" in text
    assert "waiting for your reply" in text


def test_resolve_stop_target_prefers_running_task_over_paused_threads() -> None:
    paused = AgentJob(
        job_id="11111111-1111-1111-1111-111111111111",
        source=JobSource.TELEGRAM,
        query="find dinner reservations",
        task_class=TaskClass.HEAVY,
        status=JobStatus.PAUSED_FOR_INPUT,
    )
    running = AgentJob(
        job_id="22222222-2222-2222-2222-222222222222",
        source=JobSource.TELEGRAM,
        query="find hotels in Ibiza",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
    )
    target = _resolve_stop_target("cancel this task", [paused, running])
    assert target is running


def test_resolve_stop_target_uses_latest_paused_when_no_running_job_exists() -> None:
    latest_paused = AgentJob(
        job_id="33333333-3333-3333-3333-333333333333",
        source=JobSource.TELEGRAM,
        query="latest paused task",
        task_class=TaskClass.HEAVY,
        status=JobStatus.PAUSED_FOR_INPUT,
    )
    older_paused = AgentJob(
        job_id="44444444-4444-4444-4444-444444444444",
        source=JobSource.TELEGRAM,
        query="older paused task",
        task_class=TaskClass.HEAVY,
        status=JobStatus.PAUSED_FOR_INPUT,
    )
    target = _resolve_stop_target("stop this task", [latest_paused, older_paused])
    assert target is latest_paused


def test_wants_findings_after_stop_detection() -> None:
    assert _wants_findings_after_stop("Any findings? Stop the agent and reveal the findings")
    assert _wants_findings_after_stop("stop this and show me what it found")
    assert not _wants_findings_after_stop("stop this task")


def test_stop_jobs_stops_paused_and_signals_running() -> None:
    updates: list[str] = []
    signals: list[str] = []

    class FakeState:
        def update_job_status(self, job_id: str, **_: object) -> None:
            updates.append(job_id)

        def record_control_signal(self, job_id: str, **_: object) -> None:
            signals.append(job_id)

    paused = AgentJob(
        job_id="55555555-5555-5555-5555-555555555555",
        source=JobSource.TELEGRAM,
        query="paused task",
        task_class=TaskClass.HEAVY,
        status=JobStatus.PAUSED_FOR_INPUT,
    )
    running = AgentJob(
        job_id="66666666-6666-6666-6666-666666666666",
        source=JobSource.TELEGRAM,
        query="running task",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
    )
    stopped_now, signaled = _stop_jobs(FakeState(), [paused, running])
    assert stopped_now == 2
    assert signaled == 1
    assert updates == [paused.job_id, running.job_id]
    assert signals == [running.job_id]


def test_job_accepts_live_worker_updates_only_while_running() -> None:
    running = AgentJob(
        source=JobSource.TELEGRAM,
        query="running task",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
    )
    interrupted = AgentJob(
        source=JobSource.TELEGRAM,
        query="stopped task",
        task_class=TaskClass.HEAVY,
        status=JobStatus.INTERRUPTED,
    )
    assert _job_accepts_live_worker_updates(running) is True
    assert _job_accepts_live_worker_updates(interrupted) is False


def test_active_job_is_stale_faster_for_attachments_ready() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="find hotels in ibiza",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
        current_step="attachments_ready",
        created_at="2026-05-18T00:00:00+00:00",
        last_heartbeat_at="2026-05-18T00:00:00+00:00",
    )
    now = datetime.fromisoformat("2026-05-18T00:04:01+00:00")
    assert _active_job_is_stale(job, now=now)


def test_partial_findings_text_ignores_generic_progress_summary() -> None:
    job = AgentJob(
        job_id="77777777-7777-7777-7777-777777777777",
        source=JobSource.TELEGRAM,
        query="find hotel deals",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
        latest_checkpoint_summary="working through website steps",
    )

    class FakeState:
        def get_latest_checkpoint(self, _job_id: str):
            return CheckpointPayload(summary="attachments downloaded", current_step="attachments_ready")

    assert _partial_findings_text(FakeState(), job) == ""


def test_partial_findings_text_prefers_useful_summary() -> None:
    job = AgentJob(
        job_id="88888888-8888-8888-8888-888888888888",
        source=JobSource.TELEGRAM,
        query="find hotel deals",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
        latest_checkpoint_summary="Found two Vik hotels under $300 with free cancellation.",
    )

    class FakeState:
        def get_latest_checkpoint(self, _job_id: str):
            return None

    assert "Vik hotels under $300" in _partial_findings_text(FakeState(), job)


def test_build_paused_input_resume_query_includes_new_input() -> None:
    paused_job = AgentJob(
        source=JobSource.TELEGRAM,
        query="Find a restaurant and book it for me",
        task_class=TaskClass.HEAVY,
        status=JobStatus.PAUSED_FOR_INPUT,
    )
    checkpoint = CheckpointPayload(
        summary="need party size",
        resume_instructions="Continue the reservation flow once the user gives the party size.",
    )
    query = _build_paused_input_resume_query(paused_job, checkpoint, "Two people at 7pm")
    assert "Find a restaurant and book it for me" in query
    assert "Two people at 7pm" in query
    assert "Do not ask the same question again" in query


def test_humanize_worker_failure_for_worker_exit() -> None:
    message = _humanize_worker_failure(
        "Find rental cars in Chicago for tomorrow evening",
        "worker exited without reporting a terminal state (last status: running)",
        JobStatus.FAILED,
    )
    assert "internal worker problem" in message.lower()
    assert "terminal state" not in message.lower()


def test_humanize_worker_failure_for_rate_limit() -> None:
    message = _humanize_worker_failure(
        "Find nonstop flights to Chicago",
        "Error 1015: You are being rate limited",
        JobStatus.FAILED,
    )
    assert "rate-limited" in message.lower() or "blocked" in message.lower()
    assert "1015" not in message


def test_clean_user_facing_result_strips_meta_openers_and_file_saved_line() -> None:
    text = """Here's my summary of rental cars for Chicago.

Top pick is Budget downtown.

> File saved: `cars.txt`
"""
    cleaned = _clean_user_facing_result(text)
    assert cleaned.startswith("rental cars for Chicago.")
    assert "File saved" not in cleaned


def test_active_job_is_stale_when_running_heartbeat_is_old() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="research hotels",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
        created_at="2026-05-18T20:00:00+00:00",
        last_heartbeat_at="2026-05-18T20:01:00+00:00",
    )
    assert _active_job_is_stale(job, now=datetime.fromisoformat("2026-05-18T20:20:00+00:00"))


def test_active_job_is_not_stale_when_paused_for_input() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="find dinner reservations",
        task_class=TaskClass.HEAVY,
        status=JobStatus.PAUSED_FOR_INPUT,
        created_at="2026-05-18T20:00:00+00:00",
    )
    assert not _active_job_is_stale(job, now=datetime.fromisoformat("2026-05-18T22:00:00+00:00"))


def test_status_summary_for_query_is_domain_specific() -> None:
    assert status_summary_for_query("Find flights from NYC to Chicago", attachments=False) == "checking live flight options and comparing fares"
    assert status_summary_for_query("Find me a rental car in Ibiza", attachments=False) == "checking rental car availability and comparing prices"
    assert status_summary_for_query("Book me a restaurant reservation tonight", attachments=False) == "checking reservation sources and matching real venues"


def test_progress_notification_text_is_descriptive() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="Find flights from NYC to Chicago",
        task_class=TaskClass.HEAVY,
    )
    message = progress_notification_text(job, current_step="running_agent", summary="checking live flight options and comparing fares")
    assert "Still working on your task." in message
    assert "Current step: running agent" in message
    assert "Latest progress: checking live flight options and comparing fares" in message
