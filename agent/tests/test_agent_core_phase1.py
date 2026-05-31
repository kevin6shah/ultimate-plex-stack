from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import app.agent_core as agent_core
from app.agent_core import (
    OpenTablePolicyAssessment,
    PauseForInputRequested,
    _assess_opentable_policy_text,
    _booking_choice_pause_payload,
    _build_resy_booking_page_url,
    _canonical_booking_site_key,
    _direct_tool_mode_summary,
    _ensure_default_mailbox_identity,
    _enforce_automation_policy,
    _extract_party_size_value,
    _extract_intermediate_findings_text,
    _extract_resy_venue_note,
    _extract_restaurant_booking_prefill,
    _extract_restaurant_discovery_prefill,
    _find_automation_policy,
    _handle_phase1_blocking_error,
    _is_booking_cancellation_followup,
    _is_booking_replacement_request,
    _is_restaurant_discovery_request,
    _is_retryable_model_error,
    _maybe_raise_booking_cancellation_pause,
    _maybe_raise_nonfree_opentable_confirmation,
    _maybe_raise_nonfree_resy_confirmation,
    _raise_restaurant_provider_unavailable_pause,
    _restaurant_provider_browser_fallback_message,
    _restaurant_booking_missing_details,
    _phase1_booking_runtime_guidance,
    _persist_intermediate_findings,
    _persist_recoverable_failure_summary,
    _opentable_requires_login_gate,
    _restaurant_search_with_city_fallback,
    _render_clock_label,
    _render_resy_slot_policy,
    _select_resy_time_option_labels,
    _structured_travel_failure_message,
    _should_skip_booking_cancellation_precheck,
    _should_expose_browser_tools,
)
from app.jobs import AutomationPolicyRecord, BookingRecord, BrowserSessionRecord, CheckpointPayload, IdentityRecord, JobSource, TaskClass
from app.restaurant_cli import RestaurantSlotPolicy
from app.settings import Settings


def _local_date_iso(settings: Settings, days: int = 0) -> str:
    timezone_name = settings.restaurant_cli_timezone or "America/New_York"
    current = datetime.now(ZoneInfo(timezone_name)).date() + timedelta(days=days)
    return current.isoformat()


class _FakeStore:
    def __init__(
        self,
        existing: list[IdentityRecord] | None = None,
        policies: list[AutomationPolicyRecord] | None = None,
        bookings: list[BookingRecord] | None = None,
        browser_sessions: list[BrowserSessionRecord] | None = None,
    ) -> None:
        self.records = list(existing or [])
        self.created: list[IdentityRecord] = []
        self.policies = list(policies or [])
        self.bookings = list(bookings or [])
        self.browser_sessions = list(browser_sessions or [])

    def list_identities(self, limit: int = 100):
        return self.records[:limit]

    def put_identity(self, record: IdentityRecord) -> IdentityRecord:
        self.records.append(record)
        self.created.append(record)
        return record

    def list_automation_policies(self, limit: int = 200):
        return self.policies[:limit]

    def list_booking_records(self, limit: int = 50):
        return self.bookings[:limit]

    def list_browser_sessions(self, limit: int = 50):
        return self.browser_sessions[:limit]


def test_ensure_default_mailbox_identity_creates_one_when_missing() -> None:
    settings = Settings()
    store = _FakeStore()
    original_secret = settings.secret
    object.__setattr__(settings, "gmail_account_email_param", "GMAIL_ACCOUNT_EMAIL")
    object.__setattr__(settings, "secret", lambda parameter_name: "friday.nyc.agent@gmail.com" if parameter_name == "GMAIL_ACCOUNT_EMAIL" else "")
    try:
        identity = _ensure_default_mailbox_identity(settings, store)
    finally:
        object.__setattr__(settings, "secret", original_secret)

    assert identity is not None
    assert identity.email == "friday.nyc.agent@gmail.com"
    assert identity.is_default is True
    assert store.created


def test_ensure_default_mailbox_identity_reuses_existing_record() -> None:
    settings = Settings()
    existing = IdentityRecord(
        label="Friday Gmail",
        email="friday.nyc.agent@gmail.com",
        provider="gmail",
        category="shared_mailbox",
        site_scope="shared",
        is_default=True,
    )
    store = _FakeStore(existing=[existing])
    original_secret = settings.secret
    object.__setattr__(settings, "gmail_account_email_param", "GMAIL_ACCOUNT_EMAIL")
    object.__setattr__(settings, "secret", lambda parameter_name: "friday.nyc.agent@gmail.com" if parameter_name == "GMAIL_ACCOUNT_EMAIL" else "")
    try:
        identity = _ensure_default_mailbox_identity(settings, store)
    finally:
        object.__setattr__(settings, "secret", original_secret)

    assert identity == existing
    assert store.created == []


def test_opentable_requires_login_gate_ignores_generic_header_sign_in() -> None:
    text = "Sign in 1 Find a table 2 Add your details Reservation at Karma Modern Indian"
    assert _opentable_requires_login_gate(text) is False


def test_opentable_requires_login_gate_detects_actual_auth_wall() -> None:
    text = "Sign in to continue Enter your email Continue with email"
    assert _opentable_requires_login_gate(text) is True


def test_render_clock_label_normalizes_to_twelve_hour_time() -> None:
    assert _render_clock_label("19:30") == "7:30 PM"
    assert _render_clock_label("7:15 pm") == "7:15 PM"


def test_find_automation_policy_prefers_exact_site_and_category() -> None:
    store = _FakeStore(
        policies=[
            AutomationPolicyRecord(label="generic", category="general", site_scope=""),
            AutomationPolicyRecord(label="restaurant-default", category="restaurant", site_scope=""),
            AutomationPolicyRecord(label="resy-restaurant", category="restaurant", site_scope="resy.com", allow_zero_dollar_booking=True),
        ]
    )

    record = _find_automation_policy(store, site_scope="bookings.resy.com", category="restaurant")

    assert record is not None
    assert record.label == "resy-restaurant"


def test_enforce_automation_policy_pauses_without_matching_policy() -> None:
    store = _FakeStore()

    with pytest.raises(PauseForInputRequested) as excinfo:
        _enforce_automation_policy(
            store,
            site_scope="resy.com",
            category="restaurant",
            action="zero_dollar_booking",
        )

    assert "automation policy" in excinfo.value.question.lower()


def test_enforce_automation_policy_pauses_when_action_not_allowed() -> None:
    store = _FakeStore(
        policies=[
            AutomationPolicyRecord(
                label="resy-read-only",
                site_scope="resy.com",
                category="restaurant",
                allow_zero_dollar_booking=False,
            )
        ]
    )

    with pytest.raises(PauseForInputRequested) as excinfo:
        _enforce_automation_policy(
            store,
            site_scope="resy.com",
            category="restaurant",
            action="zero_dollar_booking",
        )

    assert "not approved" in excinfo.value.question.lower()


def test_handle_phase1_blocking_error_turns_non_zero_checkout_into_pause() -> None:
    with pytest.raises(PauseForInputRequested) as excinfo:
        _handle_phase1_blocking_error(
            RuntimeError("NON_ZERO_CHECKOUT_BLOCKED: deposit $25.00 required"),
            action="browser_click(button[type=submit])",
        )

    assert excinfo.value.current_step == "payment_blocked"
    assert "stopped before submitting" in excinfo.value.question.lower()


def test_handle_phase1_blocking_error_turns_missing_payment_method_into_pause() -> None:
    with pytest.raises(PauseForInputRequested) as excinfo:
        _handle_phase1_blocking_error(
            RuntimeError("No payment method on file for this Resy account. Add one at resy.com before booking."),
            action="restaurant_book_or_handoff(resy)",
        )

    assert excinfo.value.current_step == "card_entry_required"
    assert "card on file" in excinfo.value.question.lower()


def test_restaurant_booking_missing_details_detects_missing_fields() -> None:
    missing = _restaurant_booking_missing_details(
        "Book Rubirosa for me on Resy tomorrow",
        "booking_commerce",
    )

    assert "party size" in missing
    assert "time" in missing
    assert "date" not in missing


def test_restaurant_booking_missing_details_accepts_complete_prompt() -> None:
    missing = _restaurant_booking_missing_details(
        "Book Rubirosa on Resy for 2 people on 2026-05-24 at 7:30 pm",
        "booking_commerce",
    )

    assert missing == []


def test_booking_choice_pause_payload_detects_slot_selection_question() -> None:
    payload = _booking_choice_pause_payload(
        (
            "I have a saved identity. Let me proceed to book. Let me confirm with you first which seating preference you'd like:\n\n"
            "- Main Dining Room at 1:00 PM\n"
            "- Outdoor Seating at 1:00 PM\n\n"
            "Which would you prefer?"
        ),
        "booking_commerce",
    )

    assert payload is not None
    assert payload["current_step"] == "waiting_for_user_input"
    assert "Which would you prefer?" in payload["question"]
    assert "Outdoor Seating" in payload["details"]


def test_retryable_model_error_detects_deepseek_internal_error() -> None:
    exc = RuntimeError(
        "status_code: 500, model_name: deepseek-chat, body: {'message': 'Internal Server Error', 'type': 'internal_error'}"
    )
    assert _is_retryable_model_error(exc) is True


def test_extract_intermediate_findings_text_prefers_partial_browser_block() -> None:
    text = (
        "PARTIAL_STAGEHAND_FINDINGS: the browser session gathered these findings before it stopped:\n"
        "- Air India nonstop was listed at $812\n"
        "- Etihad one-stop was listed at $734\n"
        "STAGEHAND_BROWSER_TASK_FAILED: selector timeout"
    )

    findings = _extract_intermediate_findings_text(text)

    assert "Air India nonstop" in findings
    assert "selector timeout" not in findings


def test_persist_intermediate_findings_updates_job_metadata() -> None:
    job = agent_core.AgentJob(
        source=JobSource.TELEGRAM,
        query="Find flights to Delhi",
        task_class=TaskClass.HEAVY,
        metadata={},
    )
    merged: list[tuple[str, dict[str, object]]] = []

    class FakeStore:
        def merge_job_metadata(self, job_id: str, updates: dict[str, object]) -> None:
            merged.append((job_id, updates))

    ctx = SimpleNamespace(deps=SimpleNamespace(current_job=job, store=FakeStore()))

    rendered = _persist_intermediate_findings(
        ctx,
        "Air India nonstop is available for $812 and Etihad is available for $734.",
    )

    assert "Air India nonstop" in rendered
    assert merged
    _, updates = merged[-1]
    assert updates["job_context"]["latest_findings_summary"].startswith("Air India nonstop")
    assert updates["execution_progress_matrix"]["last_meaningful_artifact"].startswith("Air India nonstop")


def test_persist_intermediate_findings_skips_generic_progress() -> None:
    job = agent_core.AgentJob(
        source=JobSource.TELEGRAM,
        query="Find flights to Delhi",
        task_class=TaskClass.HEAVY,
        metadata={},
    )
    merged: list[tuple[str, dict[str, object]]] = []

    class FakeStore:
        def merge_job_metadata(self, job_id: str, updates: dict[str, object]) -> None:
            merged.append((job_id, updates))

    ctx = SimpleNamespace(deps=SimpleNamespace(current_job=job, store=FakeStore()))

    _persist_intermediate_findings(ctx, "checking live flight options and collecting candidate itineraries")

    assert merged == []


def test_persist_recoverable_failure_summary_updates_job_metadata() -> None:
    job = agent_core.AgentJob(
        source=JobSource.TELEGRAM,
        query="Find flights to Delhi",
        task_class=TaskClass.HEAVY,
        metadata={},
    )
    merged: list[tuple[str, dict[str, object]]] = []

    class FakeStore:
        def merge_job_metadata(self, job_id: str, updates: dict[str, object]) -> None:
            merged.append((job_id, updates))

    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            current_job=job,
            store=FakeStore(),
            strategy_mode="api_direct",
        )
    )

    _persist_recoverable_failure_summary(
        ctx,
        error="Cloudflare 1015 retry_after=30",
        query="Find flights from JFK to DEL next week",
    )

    assert merged
    _, updates = merged[-1]
    summary = updates["job_context"]["latest_findings_summary"]
    assert "rate-limited" in summary.lower()
    assert "flight" in summary.lower()


def test_retryable_model_error_ignores_user_input_pause() -> None:
    exc = RuntimeError("I need party size before I can continue.")
    assert _is_retryable_model_error(exc) is False


def test_retryable_model_error_ignores_structured_restaurant_api_direct_failure() -> None:
    exc = RuntimeError(
        "service unavailable: structured restaurant availability failed in api_direct mode. "
        "provider=resy venue=Indian Table venue_id=88720 error=500 Internal Server Error"
    )
    assert _is_retryable_model_error(exc) is False


def test_retryable_model_error_ignores_structured_travel_api_direct_failure() -> None:
    exc = RuntimeError(
        "service unavailable: structured travel flight search failed in api_direct mode. "
        "error=Cloudflare 1015 retry_after=45"
    )
    assert _is_retryable_model_error(exc) is False


def test_retryable_model_error_ignores_browser_fallback_failures() -> None:
    exc = RuntimeError("BROWSER_TASK_UNAVAILABLE: browser escalation is disabled in API_DIRECT strategy mode.")
    assert _is_retryable_model_error(exc) is False


def test_structured_travel_failure_message_includes_operation_and_strategy() -> None:
    text = _structured_travel_failure_message(
        operation="flight search",
        strategy_mode="stagehand_stealth_act",
        error="Cloudflare 1015",
    )

    assert "structured travel flight search failed" in text
    assert "stagehand_stealth_act" in text
    assert "Cloudflare 1015" in text


def test_booking_cancellation_followup_detection() -> None:
    assert _is_booking_cancellation_followup(
        "Cancel the booking for junoon & instead make a booking for an Italian restaurant for 2 tomorrow at 9pm",
        "booking_commerce",
    ) is True
    assert _is_booking_cancellation_followup("Cancel that reservation and find me another one", "booking_commerce") is True
    assert _is_booking_cancellation_followup("cancel this task", "booking_commerce") is False


def test_booking_cancellation_pause_when_no_saved_booking_exists() -> None:
    store = _FakeStore()
    with pytest.raises(PauseForInputRequested) as excinfo:
        _maybe_raise_booking_cancellation_pause(
            store,
            "Cancel that booking and find me an Italian restaurant for tomorrow at 9pm",
            "booking_commerce",
        )
    assert excinfo.value.current_step == "cancel_pending"
    assert "could not find a saved booking" in excinfo.value.question.lower()


def test_booking_cancellation_pause_when_session_missing() -> None:
    store = _FakeStore(
        bookings=[
            BookingRecord(
                job_id="job-1",
                site_key="customsite.com",
                venue_name="Junoon",
                booking_time="2026-05-24T13:00:00-04:00",
                external_reference="879699798",
                can_cancel=False,
                status="created",
            )
        ]
    )
    with pytest.raises(PauseForInputRequested) as excinfo:
        _maybe_raise_booking_cancellation_pause(
            store,
            "Cancel the booking for Junoon and find me another Italian place tomorrow at 9pm",
            "booking_commerce",
        )
    assert excinfo.value.current_step == "cancel_pending"
    assert "reusable login session" in excinfo.value.question.lower()


def test_booking_cancellation_allows_structured_resy_cancel_without_browser_session() -> None:
    store = _FakeStore(
        bookings=[
            BookingRecord(
                job_id="job-1",
                site_key="resy.com",
                venue_name="Junoon",
                booking_time="2026-05-24T13:00:00-04:00",
                external_reference="879699798",
                can_cancel=True,
                status="created",
            )
        ]
    )
    _maybe_raise_booking_cancellation_pause(
        store,
        "Cancel the booking for Junoon and find me another Italian place tomorrow at 9pm",
        "booking_commerce",
    )


def test_booking_cancellation_allows_legacy_resy_record_without_can_cancel_flag() -> None:
    store = _FakeStore(
        bookings=[
            BookingRecord(
                job_id="job-legacy",
                site_key="resy",
                venue_name="Junoon",
                booking_time="2026-05-24T13:00:00-04:00",
                external_reference="879699798",
                can_cancel=False,
                status="created",
            )
        ]
    )
    _maybe_raise_booking_cancellation_pause(
        store,
        "Cancel the booking for Junoon and find me another Italian place tomorrow at 9pm",
        "booking_commerce",
    )


def test_booking_cancellation_allows_flow_when_session_exists() -> None:
    store = _FakeStore(
        bookings=[
            BookingRecord(
                job_id="job-1",
                site_key="customsite.com",
                venue_name="Junoon",
                booking_time="2026-05-24T13:00:00-04:00",
                external_reference="879699798",
                can_cancel=False,
                status="created",
            )
        ],
        browser_sessions=[
            BrowserSessionRecord(
                site_scope="customsite.com",
                session_s3_key="browser-sessions/test.json",
                user_agent="Mozilla/5.0",
                viewport_width=1280,
                viewport_height=800,
                fingerprint_seed="seed-1",
                status="active",
            )
        ],
    )
    _maybe_raise_booking_cancellation_pause(
        store,
        "Cancel the booking for Junoon and find me another Italian place tomorrow at 9pm",
        "booking_commerce",
    )


def test_nonfree_resy_policy_requires_manual_confirmation() -> None:
    policy = RestaurantSlotPolicy(
        provider="resy",
        slot_token="slot-1",
        time="21:00",
        slot_type="Main Dining Room",
        cancellation_fee=45.0,
        secs_cancel_cut_off=86400,
        requires_manual_confirmation=True,
        policy_text="Cancellation fee: $45.00 if cancelled within 24 hours.",
    )

    with pytest.raises(PauseForInputRequested) as excinfo:
        _maybe_raise_nonfree_resy_confirmation(
            policy,
            venue_id="73231",
            venue_name="Junoon",
            venue_city="New York",
            date="2026-05-26",
            time="21:00",
            party_size=2,
        )

    assert excinfo.value.current_step == "waiting_for_confirmation"
    assert "not free to cancel" in excinfo.value.question.lower()
    assert "Junoon (New York)" in excinfo.value.question
    assert "Venue: Junoon (New York)" in excinfo.value.details
    assert "$45.00" in excinfo.value.details


def test_unknown_resy_policy_requires_manual_confirmation() -> None:
    with pytest.raises(PauseForInputRequested) as excinfo:
        _maybe_raise_nonfree_resy_confirmation(
            None,
            venue_id="82481",
            venue_name="Indian Table",
            venue_city="Brooklyn",
            date="2026-05-26",
            time="21:00",
            party_size=2,
        )

    assert excinfo.value.current_step == "waiting_for_confirmation"
    assert "could not verify" in excinfo.value.question.lower()
    assert "Indian Table (Brooklyn)" in excinfo.value.question
    assert "Venue: Indian Table (Brooklyn)" in excinfo.value.details
    assert "Cancellation policy: unavailable" in excinfo.value.details


def test_restaurant_provider_unavailable_raises_pause() -> None:
    with pytest.raises(PauseForInputRequested) as excinfo:
        _raise_restaurant_provider_unavailable_pause(
            provider="resy",
            venue_name="Rubirosa (New York)",
            date="2026-05-26",
            party_size=2,
            details="The provider returned repeated errors while checking live availability.",
            venue_url="https://resy.com/cities/ny/rubirosa",
        )

    assert excinfo.value.current_step == "provider_unavailable"
    assert "try another time" in excinfo.value.question.lower()
    assert "Booking page: https://resy.com/cities/ny/rubirosa" in excinfo.value.details


def test_restaurant_provider_browser_fallback_message_instructs_browser_verification() -> None:
    message = _restaurant_provider_browser_fallback_message(
        provider="resy",
        venue_name="Rubirosa (New York)",
        date="2026-05-26",
        party_size=2,
        details="The provider returned repeated errors while checking live availability.",
        venue_url="https://resy.com/cities/ny/rubirosa",
    )

    assert message.startswith("RESTAURANT_TOOL_UNAVAILABLE:")
    assert "hardened browser fallback" in message
    assert "verify live slots and cancellation policy directly before booking" in message
    assert "Booking page: https://resy.com/cities/ny/rubirosa" in message


def test_direct_tool_mode_summary_allows_browser_after_restaurant_provider_failure() -> None:
    summary = _direct_tool_mode_summary(
        "Book Rubirosa on Resy for 2 people tomorrow at 7pm",
        "booking_commerce",
    )

    assert "try opentable next" in summary.lower()
    assert "use the hardened browser fallback" in summary.lower()
    assert "only pause with provider_unavailable after the browser fallback also fails" in summary.lower()


def test_direct_tool_mode_summary_keeps_restaurant_discovery_in_search_mode() -> None:
    summary = _direct_tool_mode_summary(
        "Find me Indian restaurants for 8 PM tonight",
        "booking_commerce",
    )

    assert "restaurant discovery task" in summary.lower()
    assert "use restaurant_search first" in summary.lower()
    assert "Do not call restaurant_book_or_handoff yet." in summary


def test_phase1_booking_guidance_for_complete_restaurant_request_starts_with_availability() -> None:
    guidance = _phase1_booking_runtime_guidance(
        "Book Rubirosa in New York City on Resy for 2 people on 2026-05-27 at 11:00 AM",
        "booking_commerce",
    )

    assert "start immediately with one restaurant_find_availability call" in guidance
    assert "try opentable next before falling back to browser verification" in guidance.lower()


def test_assess_opentable_policy_text_marks_free_cancellation() -> None:
    assessment = _assess_opentable_policy_text(
        "Reserve now. Free cancellation up to 24 hours before your reservation."
    )

    assert assessment.free_cancellation is True
    assert assessment.requires_manual_confirmation is False


def test_assess_opentable_policy_text_marks_unknown_as_manual_confirmation() -> None:
    assessment = _assess_opentable_policy_text("Reserve now. Table for 2 at 7:30 PM.")

    assert assessment.free_cancellation is False
    assert assessment.requires_manual_confirmation is True


def test_nonfree_opentable_policy_requires_manual_confirmation() -> None:
    assessment = OpenTablePolicyAssessment(
        free_cancellation=False,
        requires_manual_confirmation=True,
        policy_text="Cancellation policy: deposit required",
    )

    with pytest.raises(PauseForInputRequested) as excinfo:
        _maybe_raise_nonfree_opentable_confirmation(
            assessment,
            venue_id="1046758",
            venue_name="Carbone",
            venue_city="New York",
            date="2026-05-27",
            time="7:30 PM",
            party_size=2,
            booking_url="https://www.opentable.com/restref/client?rid=1046758",
        )

    assert excinfo.value.current_step == "waiting_for_confirmation"
    assert "opentable" in excinfo.value.details.lower()


def test_extract_restaurant_booking_prefill_parses_explicit_resy_request() -> None:
    settings = Settings()
    prefill = _extract_restaurant_booking_prefill(
        "Book Rubirosa in New York City on Resy for 2 people on Wednesday, May 27, 2026 at 11:00 AM",
        settings=settings,
        routing_profile_name="booking_commerce",
    )

    assert prefill is not None
    assert prefill.venue_query == "Rubirosa"
    assert prefill.city == "New York City"
    assert prefill.provider == "resy"
    assert prefill.date == "2026-05-27"
    assert prefill.time == "11:00 AM"
    assert prefill.party_size == 2


def test_restaurant_booking_missing_details_accepts_flexible_time_language() -> None:
    missing = _restaurant_booking_missing_details(
        "Book the earliest available reservation tonight for 3 people at Angel Indian Restaurant on Resy, but only if it has free cancellation.",
        "booking_commerce",
    )

    assert missing == []


def test_extract_restaurant_booking_prefill_accepts_flexible_time_and_venue_after_party_size() -> None:
    settings = Settings()
    prefill = _extract_restaurant_booking_prefill(
        "Book the earliest available reservation tonight for 3 people at Angel Indian Restaurant on Resy, but only if it has free cancellation.",
        settings=settings,
        routing_profile_name="booking_commerce",
    )

    assert prefill is not None
    assert prefill.venue_query == "Angel Indian Restaurant"
    assert prefill.provider == "resy"
    assert prefill.date == _local_date_iso(settings)
    assert prefill.time == "ANY AVAILABLE"
    assert prefill.party_size == 3


def test_extract_restaurant_booking_prefill_accepts_resumed_labeled_inputs() -> None:
    settings = Settings()
    prefill = _extract_restaurant_booking_prefill(
        "Book the earliest available reservation tonight for 3 people at Angel Indian Restaurant on Resy, but only if it has free cancellation.\n\n"
        "Resume the task from the prior paused-for-input checkpoint.\n\n"
        "New user input:\nparty size: three\ndate: tomorrow\ntime: 2:30 AM\n\n"
        "Continue from the saved workspace state. Do not ask the same question again unless the new input is still insufficient.",
        settings=settings,
        routing_profile_name="booking_commerce",
    )

    assert prefill is not None
    assert prefill.venue_query == "Angel Indian Restaurant"
    assert prefill.date == _local_date_iso(settings, days=1)
    assert prefill.time == "2:30 AM"
    assert prefill.party_size == 3


def test_extract_restaurant_booking_prefill_accepts_slash_date_format() -> None:
    settings = Settings()
    prefill = _extract_restaurant_booking_prefill(
        "Book Rubirosa on Resy for party size: 2 date: 05/30/2026 time: 7:30 PM",
        settings=settings,
        routing_profile_name="booking_commerce",
    )

    assert prefill is not None
    assert prefill.venue_query == "Rubirosa"
    assert prefill.date == "2026-05-30"
    assert prefill.time == "7:30 PM"
    assert prefill.party_size == 2


def test_extract_restaurant_booking_prefill_prefers_latest_resumed_inputs() -> None:
    settings = Settings()
    prefill = _extract_restaurant_booking_prefill(
        "Book Rubirosa on Resy for 2 people tomorrow at 8 PM\n\n"
        "Resume the task from the prior paused-for-input checkpoint.\n\n"
        "New user input:\nparty size: four\ndate: 2026-05-31\ntime: 7:15 PM\n\n"
        "Continue from the saved workspace state. Do not ask the same question again unless the new input is still insufficient.",
        settings=settings,
        routing_profile_name="booking_commerce",
    )

    assert prefill is not None
    assert prefill.venue_query == "Rubirosa"
    assert prefill.date == "2026-05-31"
    assert prefill.time == "7:15 PM"
    assert prefill.party_size == 4


def test_extract_restaurant_booking_prefill_accepts_ordinal_month_date_format() -> None:
    settings = Settings()
    prefill = _extract_restaurant_booking_prefill(
        "Book Rubirosa on Resy for two people on May 31st, 2026 at 7 PM",
        settings=settings,
        routing_profile_name="booking_commerce",
    )

    assert prefill is not None
    assert prefill.venue_query == "Rubirosa"
    assert prefill.date == "2026-05-31"
    assert prefill.time == "7 PM"
    assert prefill.party_size == 2


def test_extract_party_size_value_accepts_number_words() -> None:
    assert _extract_party_size_value("find me restaurants tonight at 9:30 PM for three people") == 3


def test_extract_restaurant_discovery_prefill_parses_complete_discovery_request() -> None:
    settings = Settings()
    prefill = _extract_restaurant_discovery_prefill(
        "Find me an Indian restaurant on Resy near Midtown NYC for tonight at 8 PM for 3 people",
        settings=settings,
        routing_profile_name="booking_commerce",
    )

    assert prefill is not None
    assert prefill.search_query == "an Indian restaurant"
    assert prefill.city == "Midtown Nyc"
    assert prefill.provider == "resy"
    assert prefill.time == "8 PM"
    assert prefill.party_size == 3


def test_extract_restaurant_discovery_prefill_accepts_flexible_time_language() -> None:
    settings = Settings()
    prefill = _extract_restaurant_discovery_prefill(
        "Find me Indian restaurants on Resy near Midtown NYC for tomorrow any available time for two people",
        settings=settings,
        routing_profile_name="booking_commerce",
    )

    assert prefill is not None
    assert prefill.search_query == "Indian restaurants"
    assert prefill.city == "Midtown Nyc"
    assert prefill.provider == "resy"
    assert prefill.time == "ANY AVAILABLE"
    assert prefill.party_size == 2


def test_render_restaurant_discovery_direct_response_lists_candidates() -> None:
    prefill = agent_core.RestaurantDiscoveryPrefill(
        search_query="an Indian restaurant",
        city="Midtown NYC",
        provider="resy",
        date="2026-05-29",
        time="8 PM",
        party_size=3,
    )
    preflight = agent_core.RestaurantDiscoveryPreflightResult(
        summary="STRUCTURED_DISCOVERY_PREFLIGHT",
        candidates=(
            agent_core.RestaurantDiscoveryCandidate(
                venue_id="95147",
                venue_name="Angel Indian Restaurant",
                venue_city="New York",
                venue_url="https://resy.com/cities/ny/angel-indian-restaurant",
                provider="resy",
            ),
            agent_core.RestaurantDiscoveryCandidate(
                venue_id="91940",
                venue_name="Muna",
                venue_city="New York",
                venue_url="https://resy.com/cities/ny/muna",
                provider="resy",
            ),
        ),
    )

    response = agent_core._render_restaurant_discovery_direct_response(
        prefill=prefill,
        preflight=preflight,
    )

    assert "near Midtown NYC" in response
    assert "Angel Indian Restaurant" in response
    assert "venue 95147" in response
    assert "Muna" in response


def test_restaurant_city_matches_request_handles_nyc_aliases() -> None:
    assert agent_core._restaurant_city_matches_request("Midtown NYC", "New York") is True
    assert agent_core._restaurant_city_matches_request("Midtown NYC", "Las Vegas") is False
    assert agent_core._restaurant_city_matches_request("Flatiron Manhattan", "New York") is True


def test_restaurant_discovery_request_overrides_stray_booking_words() -> None:
    query = "Book me a find me restaurants first for tonight at 9:30 PM for three people"

    assert _is_restaurant_discovery_request(query, "booking_commerce") is True
    assert _restaurant_booking_missing_details(query, "booking_commerce") == []


def test_build_resy_booking_page_url_sets_date_and_seats() -> None:
    assert (
        _build_resy_booking_page_url(
            "https://resy.com/cities/new-york-ny/venues/rubirosa",
            date="2026-05-27",
            party_size=2,
        )
        == "https://resy.com/cities/new-york-ny/venues/rubirosa?date=2026-05-27&seats=2"
    )


def test_select_resy_time_option_labels_finds_exact_and_nearest() -> None:
    exact_label, nearest_labels, visible_labels = _select_resy_time_option_labels(
        "9:45 PM",
        [
            {"label": "All Day", "value": ""},
            {"label": "9:30 PM", "value": "2130"},
            {"label": "9:45 PM", "value": "2145"},
            {"label": "10:00 PM", "value": "2200"},
        ],
    )

    assert exact_label == "9:45 PM"
    assert nearest_labels[:2] == ("9:30 PM", "10:00 PM")
    assert visible_labels[0] == "All Day"


def test_extract_resy_venue_note_pulls_booking_window_message() -> None:
    note = _extract_resy_venue_note(
        "Reservations open up for dinner 14 days in advance via Resy. "
        "If you do not see availability, we recommend you add your name to the notify list."
    )

    assert "14 days in advance via Resy" in note
    assert "notify list" in note


@pytest.mark.asyncio
async def test_resy_availability_browser_probe_summary_uses_probe_result(monkeypatch) -> None:
    async def fake_run_resy_browser_probe(**kwargs):
        assert kwargs["venue_url"] == "https://resy.com/cities/ny/indian-table"
        assert kwargs["date"] == "2026-05-29"
        assert kwargs["party_size"] == 3
        return agent_core.ResyBrowserProbeResult(
            booking_url="https://resy.com/cities/ny/indian-table?date=2026-05-29&seats=3",
            selected_exact_time_label="",
            nearest_time_labels=("7:45 PM", "8:15 PM"),
            visible_time_labels=("7:45 PM", "8:15 PM", "8:30 PM"),
            venue_note="Reservations open up for dinner 14 days in advance via Resy.",
            current_url="https://resy.com/cities/ny/indian-table?date=2026-05-29&seats=3",
        )

    monkeypatch.setattr(agent_core, "_run_resy_browser_probe", fake_run_resy_browser_probe)

    summary = await agent_core._resy_availability_browser_probe_summary(
        settings=Settings(),
        workspace=object(),
        venue_id="88720",
        venue_name="Indian Table",
        venue_city="New York",
        venue_url="https://resy.com/cities/ny/indian-table",
        date="2026-05-29",
        party_size=3,
    )

    assert summary is not None
    assert "BROWSER_RESY_PROBE:" in summary
    assert "Venue id: 88720" in summary
    assert "Live time options visible on the venue page:" in summary
    assert "Reservations open up for dinner 14 days in advance via Resy." in summary


@pytest.mark.asyncio
async def test_restaurant_search_with_city_fallback_retries_without_city(monkeypatch) -> None:
    calls: list[list[str]] = []

    async def fake_run_restaurant_cli_json(_settings, _workspace, *args, **_kwargs):
        calls.append(list(args))
        if "--city" in args:
            return {"ok": True, "results": [], "failures": []}
        return {
            "ok": True,
            "results": [
                {
                    "id": "466",
                    "name": "Rubirosa",
                    "city": "New York",
                    "url": "https://resy.com/cities/ny/rubirosa",
                }
            ],
            "failures": [],
        }

    monkeypatch.setattr(agent_core, "run_restaurant_cli_json", fake_run_restaurant_cli_json)

    payload, best_match = await _restaurant_search_with_city_fallback(
        settings=Settings(),
        workspace=object(),
        query="Rubirosa",
        provider="resy",
        city="New York City",
    )

    assert best_match is not None
    assert best_match["id"] == "466"
    assert payload["city_filter_relaxed"] is True
    assert any("--city" in call for call in calls)
    assert any("--city" not in call for call in calls)


@pytest.mark.asyncio
async def test_resolve_restaurant_venue_reference_uses_structured_search_for_nonnumeric_input(monkeypatch) -> None:
    async def fake_search_with_city_fallback(**kwargs):
        assert kwargs["query"] == "indian-table"
        return (
            {"results": []},
            {
                "id": "88720",
                "name": "Indian Table",
                "city": "New York",
                "url": "https://resy.com/cities/ny/indian-table",
            },
        )

    monkeypatch.setattr(agent_core, "_restaurant_search_with_city_fallback", fake_search_with_city_fallback)

    resolved_id, resolved_name, resolved_city, resolved_url = await agent_core._resolve_restaurant_venue_reference(
        settings=Settings(),
        workspace=object(),
        venue_reference="indian-table",
        provider="resy",
    )

    assert resolved_id == "88720"
    assert resolved_name == "Indian Table"
    assert resolved_city == "New York"
    assert resolved_url == "https://resy.com/cities/ny/indian-table"


def test_render_resy_slot_policy_handles_missing_policy() -> None:
    assert _render_resy_slot_policy(None) == "Cancellation policy: unavailable from the current Resy slot data."


def test_canonical_booking_site_key_normalizes_resy() -> None:
    assert _canonical_booking_site_key("resy") == "resy.com"
    assert _canonical_booking_site_key("resy.com") == "resy.com"


def test_booking_replacement_request_detects_rebook_language() -> None:
    assert _is_booking_replacement_request(
        "Cancel the booking for Junoon and instead make a booking for an Italian restaurant for 2 tomorrow at 9pm"
    )


def test_cancel_and_replace_request_does_not_pause_when_no_saved_booking_exists() -> None:
    store = _FakeStore()

    _maybe_raise_booking_cancellation_pause(
        store,
        "Cancel the booking for Junoon and instead make a booking for an Italian restaurant for 2 tomorrow at 9pm",
        "booking_commerce",
    )


def test_resume_checkpoint_can_skip_repeat_booking_cancellation_precheck() -> None:
    checkpoint = CheckpointPayload(
        summary="Which city are you looking for an Italian restaurant in tomorrow night at 9 PM?",
        current_step="awaiting_city_and_restaurant_choice",
        metadata={
            "input_question": "Which city are you looking for an Italian restaurant in tomorrow night at 9 PM?",
            "input_details": "Let's move forward with the Italian booking.",
        },
    )

    assert (
        _should_skip_booking_cancellation_precheck(
            query="Cancel the booking for Junoon and instead make a booking for an Italian restaurant for 2 tomorrow at 9pm",
            effective_query="Cancel the booking for Junoon and instead make a booking for an Italian restaurant for 2 tomorrow at 9pm",
            routing_profile_name="booking_commerce",
            resume_checkpoint=checkpoint,
        )
        is True
    )


def test_cancel_pending_checkpoint_can_resume_into_replacement_booking() -> None:
    checkpoint = CheckpointPayload(
        summary="waiting for a concrete reservation to cancel",
        current_step="cancel_pending",
        metadata={
            "input_question": "I could not find a saved booking to cancel yet.",
            "input_details": "Please tell me the restaurant name or reservation reference, or cancel it manually and then ask me to book the replacement.",
        },
    )

    assert (
        _should_skip_booking_cancellation_precheck(
            query="Cancel the booking for Junoon and instead make a booking for an Italian restaurant for 2 tomorrow at 9pm",
            effective_query=(
                "Cancel the booking for Junoon and instead make a booking for an Italian restaurant for 2 tomorrow at 9pm\n\n"
                "New user input:\nNYC, Bar Italia"
            ),
            routing_profile_name="booking_commerce",
            resume_checkpoint=checkpoint,
        )
        is True
    )


def test_booking_cancel_queries_enable_browser_tools() -> None:
    assert _should_expose_browser_tools(
        "Cancel the booking for junoon and instead make a booking for an Italian restaurant for 2 tomorrow at 9pm",
        "booking_commerce",
    ) is True


def test_basic_restaurant_lookup_query_keeps_browser_tools_off() -> None:
    assert _should_expose_browser_tools(
        "Find Italian restaurants tomorrow at 9pm for 2 people",
        "booking_commerce",
    ) is False


def test_free_cancel_booking_language_is_not_treated_as_cancel_followup() -> None:
    assert not _is_booking_cancellation_followup(
        "Find Italian restaurants in NYC tomorrow at 9pm for 2 and get ready to book the best free-cancel option",
        "booking_commerce",
    )


def test_free_to_cancel_language_is_not_treated_as_cancel_followup() -> None:
    assert not _is_booking_cancellation_followup(
        "Find available times tomorrow at Angel Indian Restaurant on Resy for 2 people. Only show slots that are free to cancel.",
        "booking_commerce",
    )
