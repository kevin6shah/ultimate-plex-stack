import asyncio
from datetime import datetime
from types import SimpleNamespace

from app.heavy_job_runtime import build_heavy_claim, progress_notification_text, progress_summary_for_step, status_summary_for_query
from app.jobs import AgentJob, CheckpointPayload, JobSource, JobStatus, TaskClass
from app.main import (
    _active_jobs_for_pairs_async,
    _active_job_is_stale,
    _build_light_context_for_query,
    _booking_clarification_prompt_from_result,
    _build_contextual_heavy_followup_query,
    _build_paused_input_resume_query,
    _clean_user_facing_result,
    _extract_explicit_memory_fact,
    _format_siri_telegram_mirror,
    _format_status_message,
    _format_tasks_list,
    _humanize_worker_failure,
    _is_input_reply,
    _job_can_be_superseded_by_followup,
    _looks_like_booking_cancel_request,
    _is_list_tasks_request,
    _is_stop_all_request,
    _is_status_request,
    _job_result_looks_like_booking_clarification,
    _job_indicates_user_stop,
    _job_accepts_live_worker_updates,
    _latest_status_job_for_pairs_async,
    _looks_like_natural_input_reply,
    _natural_reply_can_resume,
    _partial_findings_text,
    _preferred_latest_status_job_for_thread_async,
    _refresh_superseded_paused_jobs,
    _resolve_contextual_heavy_followup,
    _resolve_stop_target,
    _should_continue_contextual_heavy_followup,
    _followup_matcher_decision,
    _stop_jobs,
    _strip_input_reply_prefix,
    _sync_thread_active_heavy_job,
    _telegram_owner_source_pairs,
    _wants_findings_after_stop,
)
from app.jobs import AgentConfig, ThreadTurn, ThreadTurnRole


def test_status_request_detection() -> None:
    assert _is_status_request("what's the status on that task?")
    assert _is_status_request("any update?")
    assert _is_status_request("did it finish?")
    assert _is_status_request("Starts")
    assert _is_status_request("started?")
    assert not _is_status_request("find restaurant reservations for Sunday")


def test_extract_explicit_memory_fact_requires_explicit_marker() -> None:
    assert _extract_explicit_memory_fact("#remember Prefer outdoor tables") == "Prefer outdoor tables"
    assert _extract_explicit_memory_fact("#memory Prefer Midtown first") == "Prefer Midtown first"
    assert _extract_explicit_memory_fact("remember this: I prefer free cancellation") == "I prefer free cancellation"
    assert _extract_explicit_memory_fact("add vegetarian preference to your memory") == "vegetarian preference"
    assert _extract_explicit_memory_fact("#just-a-hashtag note") == ""
    assert _extract_explicit_memory_fact("Can you remember this?") == ""


def test_stop_all_request_detection() -> None:
    assert _is_stop_all_request("stop all")
    assert _is_stop_all_request("cancel all tasks")
    assert _is_stop_all_request("abort everything")
    assert not _is_stop_all_request("stop task 1")


def test_list_tasks_request_detection_accepts_slash_commands() -> None:
    assert _is_list_tasks_request("/show-tasks")
    assert _is_list_tasks_request("/tasks")


def test_stop_request_detection_catches_stop_the_agent_and_stop_it() -> None:
    from app.main import _is_stop_request

    assert _is_stop_request("Any findings? Stop the agent and reveal the findings")
    assert _is_stop_request("stop it")
    assert not _is_stop_request("Cancel that booking and find me one that has reservation for the night for Italian")
    assert not _is_stop_request("Cancel the booking for junoon and instead make a booking for an Italian restaurant for 2 tomorrow at 9pm")


def test_booking_cancel_detection_distinguishes_domain_action_from_task_stop() -> None:
    assert _looks_like_booking_cancel_request("Cancel that booking and find me one that has reservation for the night for Italian")
    assert _looks_like_booking_cancel_request("Cancel the booking for junoon and instead make a booking for an Italian restaurant for 2 tomorrow at 9pm")
    assert not _looks_like_booking_cancel_request("cancel this task")


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
    assert "Still working on it." in text
    assert "Step: comparing sources" in text
    assert "Update: reviewing travel and vlogging options" in text


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
    assert "I hit an interruption while finishing that task." in text
    assert "Still working on your latest task." not in text
    assert "latest checkpoint" in text


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
    assert "latest checkpoint" in text
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


def test_humanize_worker_failure_for_transient_model_provider_error() -> None:
    text = _humanize_worker_failure(
        "Book junoon now 2ppl 1pm outside",
        "status_code: 504, model_name: deepseek-chat, body: gateway timeout",
        JobStatus.FAILED,
    )
    assert text == "The model provider had a transient failure while I was working through the reservation flow."


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
    assert "I need one thing before I continue." in text
    assert "How many people should I book for?" in text
    assert "Reply normally with the missing detail." in text
    assert "If you want something else instead, just ask." in text
    assert "Step: waiting for your reply" in text
    assert "**" not in text
    assert "\n\nHow many people should I book for?\n" in text


def test_completed_booking_clarification_is_treated_like_waiting_for_input() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="Okay book junoon for 1pm for 2 people",
        task_class=TaskClass.HEAVY,
        status=JobStatus.COMPLETED,
        result_preview=(
            "I have a saved identity. Let me proceed to book. I'll pick the 1:00 PM Main Dining Room slot. "
            "Let me confirm with you first which seating preference you'd like:\n\n"
            "- Main Dining Room at 1:00 PM\n"
            "- Outdoor Seating at 1:00 PM\n\n"
            "Which would you prefer?"
        ),
    )
    state = SimpleNamespace(get_latest_checkpoint=lambda _job_id: None)
    assert _job_result_looks_like_booking_clarification(job) is True
    text = _format_status_message(state, job)
    assert "I need your choice before I continue." in text
    assert "Which would you prefer?" in text
    assert "Your latest task completed." not in text


def test_booking_clarification_prompt_parser_handles_alternative_question_style() -> None:
    result = (
        "Unfortunately, Junoon has no 9 PM slots available on Tuesday, May 26.\n\n"
        "Would you like me to:\n"
        "1. Book one of the available 5:30–6:15 PM slots at Junoon instead, or\n"
        "2. Search for another restaurant that has 9 PM availability tomorrow?"
    )
    question, details = _booking_clarification_prompt_from_result(result)
    assert "Would you like me to:" in question
    assert "1. Book one of the available" in details


def test_paused_input_reply_prefix_is_detected_and_removed() -> None:
    assert _is_input_reply("answer: two people at 7pm")
    assert _is_input_reply("resume with the 9pm option")
    assert _strip_input_reply_prefix("continue with the Friday evening option") == "the Friday evening option"


def test_short_natural_reply_is_treated_as_paused_input() -> None:
    assert _looks_like_natural_input_reply("West Village, 2 people, 7pm, Italian, under $200")


def test_contextual_heavy_followup_continues_booking_thread() -> None:
    latest_job = AgentJob(
        source=JobSource.TELEGRAM,
        query="Find me Indian restaurants with free bookings for 3 people 10:30pm today",
        task_class=TaskClass.HEAVY,
        status=JobStatus.PAUSED_FOR_INPUT,
    )
    assert _should_continue_contextual_heavy_followup(
        "What cuisines have the most availabilities for 10:30 tonight?",
        latest_job=latest_job,
    )
    assert _should_continue_contextual_heavy_followup(
        "Go with Tamarind",
        latest_job=latest_job,
    )
    assert _should_continue_contextual_heavy_followup(
        "Preferably ones with free cancellation",
        latest_job=latest_job,
    )
    assert not _should_continue_contextual_heavy_followup(
        "What time is it in Tokyo?",
        latest_job=latest_job,
    )
    assert not _should_continue_contextual_heavy_followup(
        "Find me Italian restaurants in Soho tomorrow at 8",
        latest_job=latest_job,
    )


def test_ambiguous_contextual_followup_uses_llm_resolver(monkeypatch) -> None:
    latest_job = AgentJob(
        source=JobSource.TELEGRAM,
        query="Find me Indian restaurants with free bookings for 3 people 10:30pm today",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
    )
    assert _followup_matcher_decision("Maybe Flatiron", latest_job=latest_job) is None

    async def fake_llm_followup_decision(query: str, *, latest_job=None) -> bool:
        assert query == "Maybe Flatiron"
        assert latest_job is not None
        return True

    monkeypatch.setattr("app.main._llm_followup_decision", fake_llm_followup_decision)
    assert asyncio.run(_resolve_contextual_heavy_followup("Maybe Flatiron", latest_job=latest_job)) is True


def test_contextual_heavy_followup_query_warns_against_stale_results() -> None:
    prior_job = AgentJob(
        source=JobSource.TELEGRAM,
        query="Find me Indian restaurants with free bookings for 3 people 10:30pm today",
        task_class=TaskClass.HEAVY,
        status=JobStatus.COMPLETED,
    )
    query = _build_contextual_heavy_followup_query(prior_job, None, "None of these sites are valid")
    assert "Continue the same task using the user's new follow-up." in query
    assert "Do not repeat stale results" in query
    assert not _looks_like_natural_input_reply("Plan a simple coffee-to-park walking itinerary near SoHo tomorrow afternoon")
    assert not _looks_like_natural_input_reply("What's the status?")
    assert not _looks_like_natural_input_reply("Friday, find me Indian restaurants for 8 PM tonight")


def test_light_context_drops_heavy_turns_for_unrelated_light_query() -> None:
    turns = [
        ThreadTurn(role=ThreadTurnRole.USER, text="Find me Indian restaurants tonight", task_class=TaskClass.HEAVY),
        ThreadTurn(role=ThreadTurnRole.ASSISTANT, text="On it. I'll message you in Telegram.", task_class=TaskClass.HEAVY),
        ThreadTurn(role=ThreadTurnRole.USER, text="What time is it in Tokyo?", task_class=TaskClass.LIGHT),
    ]

    class FakeState:
        def get_context_bundle(self, **_: object):
            return ("heavy summary", turns, AgentConfig())

        def list_memories(self, *, owner: str):
            assert owner == "siri"
            return []

        def get_context_record(self, *, channel: str, user_id: str, conversation_id: str):
            return {"active_heavy_job_id": "job-1"}

        def get_active_heavy_job_id(self, *, channel: str, user_id: str, conversation_id: str):
            return "job-1"

        def get_job(self, job_id: str):
            if job_id != "job-1":
                return None
            return AgentJob(
                job_id="job-1",
                source=JobSource.SIRI,
                user_id="siri",
                conversation_id="siri",
                query="Find me Indian restaurants tonight",
                task_class=TaskClass.HEAVY,
                status=JobStatus.RUNNING,
            )

        def set_active_heavy_job(self, **_: object) -> None:
            raise AssertionError("should not rewrite active heavy job")

        def clear_active_heavy_job(self, **_: object) -> None:
            raise AssertionError("should not clear active heavy job")

    context_summary, recent_turns, memories, config = _build_light_context_for_query(
        FakeState(),
        channel="siri",
        user_id="siri",
        conversation_id="siri",
        query="My test token is maple-orbit-372",
    )

    assert context_summary == ""
    assert [turn.text for turn in recent_turns] == ["What time is it in Tokyo?"]
    assert memories == []
    assert isinstance(config, AgentConfig)


def test_natural_reply_only_resumes_latest_job() -> None:
    paused = AgentJob(
        job_id="11111111-1111-1111-1111-111111111111",
        source=JobSource.SIRI,
        query="Book dinner",
        task_class=TaskClass.HEAVY,
        status=JobStatus.PAUSED_FOR_INPUT,
    )
    newer_running = AgentJob(
        job_id="22222222-2222-2222-2222-222222222222",
        source=JobSource.SIRI,
        query="Find Indian restaurants tonight",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
    )
    assert not _natural_reply_can_resume("2 people", resume_job=paused, latest_status_job=newer_running)
    assert _natural_reply_can_resume("2 people", resume_job=paused, latest_status_job=paused)


def test_refresh_superseded_paused_jobs_interrupts_older_paused_threads() -> None:
    updates: list[str] = []

    class FakeState:
        def update_job_status(self, job_id: str, **_: object) -> None:
            updates.append(job_id)

        def get_job(self, _job_id: str):
            return None

    newer_paused = AgentJob(
        job_id="11111111-1111-1111-1111-111111111111",
        source=JobSource.TELEGRAM,
        user_id="u1",
        query="new paused task",
        task_class=TaskClass.HEAVY,
        status=JobStatus.PAUSED_FOR_INPUT,
        created_at="2026-05-25T22:00:00+00:00",
    )
    older_paused = AgentJob(
        job_id="22222222-2222-2222-2222-222222222222",
        source=JobSource.SIRI,
        user_id="siri",
        query="old paused task",
        task_class=TaskClass.HEAVY,
        status=JobStatus.PAUSED_FOR_INPUT,
        created_at="2026-05-25T21:00:00+00:00",
    )
    running = AgentJob(
        job_id="33333333-3333-3333-3333-333333333333",
        source=JobSource.SIRI,
        user_id="siri",
        query="running task",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
        created_at="2026-05-25T22:05:00+00:00",
    )
    refreshed = _refresh_superseded_paused_jobs(FakeState(), [newer_paused, older_paused, running])
    assert updates == [older_paused.job_id]
    assert refreshed[0].job_id == newer_paused.job_id


def test_telegram_owner_source_pairs_include_siri_thread() -> None:
    pairs = _telegram_owner_source_pairs("123456")
    assert pairs == [(JobSource.TELEGRAM, "123456"), (JobSource.SIRI, "siri")]


def test_latest_status_job_ignores_light_jobs() -> None:
    heavy_completed = AgentJob(
        job_id="11111111-1111-1111-1111-111111111111",
        source=JobSource.TELEGRAM,
        user_id="u1",
        query="Find me Indian restaurants for tonight",
        task_class=TaskClass.HEAVY,
        status=JobStatus.COMPLETED,
        result_preview="Heavy result",
        created_at="2026-05-26T01:00:00+00:00",
    )
    newer_light_completed = AgentJob(
        job_id="22222222-2222-2222-2222-222222222222",
        source=JobSource.TELEGRAM,
        user_id="u1",
        query="Thanks",
        task_class=TaskClass.LIGHT,
        status=JobStatus.COMPLETED,
        result_preview="Light result",
        created_at="2026-05-26T01:05:00+00:00",
    )

    class FakeState:
        def list_jobs_for_user(self, *, source: str, user_id: str, statuses: tuple[JobStatus, ...], limit: int = 20):
            assert source == JobSource.TELEGRAM.value
            assert user_id == "u1"
            if JobStatus.COMPLETED in statuses or JobStatus.FAILED in statuses:
                return [newer_light_completed, heavy_completed]
            return []

    job = asyncio.run(_latest_status_job_for_pairs_async(FakeState(), [(JobSource.TELEGRAM, "u1")]))
    assert job is not None
    assert job.job_id == heavy_completed.job_id


def test_active_jobs_list_ignores_light_jobs() -> None:
    heavy_running = AgentJob(
        job_id="11111111-1111-1111-1111-111111111111",
        source=JobSource.TELEGRAM,
        user_id="u1",
        query="Find me restaurants tonight",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
    )
    light_running = AgentJob(
        job_id="22222222-2222-2222-2222-222222222222",
        source=JobSource.TELEGRAM,
        user_id="u1",
        query="Quick chat",
        task_class=TaskClass.LIGHT,
        status=JobStatus.RUNNING,
    )

    class FakeState:
        def list_jobs_for_user(self, *, source: str, user_id: str, statuses: tuple[JobStatus, ...], limit: int = 20):
            return [light_running, heavy_running]

    jobs = asyncio.run(_active_jobs_for_pairs_async(FakeState(), [(JobSource.TELEGRAM, "u1")]))
    assert [job.job_id for job in jobs] == [heavy_running.job_id]


def test_running_heavy_job_can_be_superseded_by_followup() -> None:
    running = AgentJob(
        source=JobSource.TELEGRAM,
        query="Find me Indian restaurants tonight",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
    )
    paused = AgentJob(
        source=JobSource.TELEGRAM,
        query="Find me Indian restaurants tonight",
        task_class=TaskClass.HEAVY,
        status=JobStatus.PAUSED_FOR_INPUT,
    )
    light_running = AgentJob(
        source=JobSource.TELEGRAM,
        query="Thanks",
        task_class=TaskClass.LIGHT,
        status=JobStatus.RUNNING,
    )
    assert _job_can_be_superseded_by_followup(running) is True
    assert _job_can_be_superseded_by_followup(paused) is False
    assert _job_can_be_superseded_by_followup(light_running) is False


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


def test_build_heavy_claim_prefers_persisted_query_override() -> None:
    job = AgentJob(
        job_id="job-123",
        source=JobSource.SIRI,
        query="Book dinner tonight",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
        user_id="siri",
        conversation_id="siri",
        metadata={"query_override": "Book dinner tonight\n\nNew user input:\ntime: 7:30 PM"},
    )

    class FakeState:
        def get_context_bundle(self, *, channel: str, user_id: str, conversation_id: str):
            return ("", [], AgentConfig())

        def list_memories(self, *, owner: str):
            return []

        def get_latest_checkpoint(self, job_id: str):
            return None

    claim = build_heavy_claim(
        FakeState(),
        SimpleNamespace(artifacts_bucket="bucket"),
        job,
    )

    assert claim["job"]["query"] == "Book dinner tonight\n\nNew user input:\ntime: 7:30 PM"


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
    assert "`" not in cleaned


def test_clean_user_facing_result_strips_basic_markdown_emphasis() -> None:
    cleaned = _clean_user_facing_result("Current time in Tokyo: **Thursday at 12:45 PM JST**")
    assert cleaned == "Current time in Tokyo: Thursday at 12:45 PM JST"


def test_format_siri_telegram_mirror_includes_query_and_plain_reply() -> None:
    message = _format_siri_telegram_mirror(
        "What time is it in Tokyo?",
        reply="Current time in Tokyo: **Thursday at 12:45 PM JST**",
    )
    assert message == "Siri: What time is it in Tokyo?\n\nCurrent time in Tokyo: Thursday at 12:45 PM JST"


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


def test_sync_thread_active_heavy_job_returns_running_job() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="Find Indian restaurants",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
        user_id="u1",
        conversation_id="c1",
    )

    class FakeStore:
        def get_active_heavy_job_id(self, *, channel: str, user_id: str, conversation_id: str):
            assert (channel, user_id, conversation_id) == ("telegram", "u1", "c1")
            return job.job_id

        def get_job(self, job_id: str):
            assert job_id == job.job_id
            return job

        def clear_active_heavy_job(self, **_kwargs):
            raise AssertionError("should not clear active running job")

    resolved = _sync_thread_active_heavy_job(FakeStore(), channel="telegram", user_id="u1", conversation_id="c1")
    assert resolved is job


def test_sync_thread_active_heavy_job_clears_terminal_job() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="Find Indian restaurants",
        task_class=TaskClass.HEAVY,
        status=JobStatus.COMPLETED,
        user_id="u1",
        conversation_id="c1",
    )
    cleared: dict[str, str] = {}

    class FakeStore:
        def get_active_heavy_job_id(self, *, channel: str, user_id: str, conversation_id: str):
            return job.job_id

        def get_job(self, job_id: str):
            assert job_id == job.job_id
            return job

        def clear_active_heavy_job(self, **kwargs):
            cleared.update(kwargs)
            return True

    resolved = _sync_thread_active_heavy_job(FakeStore(), channel="telegram", user_id="u1", conversation_id="c1")
    assert resolved is None
    assert cleared["only_if_job_id"] == job.job_id


def test_preferred_latest_status_job_for_thread_prefers_thread_owned_active_job() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="Find Indian restaurants",
        task_class=TaskClass.HEAVY,
        status=JobStatus.RUNNING,
        user_id="u1",
        conversation_id="c1",
    )

    class FakeStore:
        def get_active_heavy_job_id(self, *, channel: str, user_id: str, conversation_id: str):
            return job.job_id

        def get_job(self, job_id: str):
            assert job_id == job.job_id
            return job

        def clear_active_heavy_job(self, **_kwargs):
            raise AssertionError("should not clear active running job")

    resolved = asyncio.run(
        _preferred_latest_status_job_for_thread_async(
            FakeStore(),
            channel="telegram",
            user_id="u1",
            conversation_id="c1",
            owner_pairs=[(JobSource.TELEGRAM, "u1")],
        )
    )
    assert resolved is job


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
    assert "Still working on it." in message
    assert "Step: running agent" in message
    assert "Update: checking live flight options and collecting candidate itineraries" in message


def test_progress_summary_for_step_gets_more_specific_over_time() -> None:
    early = progress_summary_for_step(
        "Research and compare lightweight running jackets with direct vendor links",
        current_step="running_agent",
        attachments=False,
        summary="researching sources and comparing findings",
        elapsed_seconds=30,
    )
    later = progress_summary_for_step(
        "Research and compare lightweight running jackets with direct vendor links",
        current_step="running_agent",
        attachments=False,
        summary="researching sources and comparing findings",
        elapsed_seconds=600,
    )
    assert "collecting candidate options" in early
    assert "final purchase links" in later
