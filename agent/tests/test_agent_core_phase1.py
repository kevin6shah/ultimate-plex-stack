from __future__ import annotations

import pytest

from app.agent_core import (
    PauseForInputRequested,
    _booking_choice_pause_payload,
    _canonical_booking_site_key,
    _ensure_default_mailbox_identity,
    _enforce_automation_policy,
    _find_automation_policy,
    _handle_phase1_blocking_error,
    _is_booking_cancellation_followup,
    _is_retryable_model_error,
    _maybe_raise_booking_cancellation_pause,
    _maybe_raise_nonfree_resy_confirmation,
    _restaurant_booking_missing_details,
    _render_resy_slot_policy,
    _should_expose_browser_tools,
)
from app.jobs import AutomationPolicyRecord, BookingRecord, BrowserSessionRecord, IdentityRecord
from app.restaurant_cli import RestaurantSlotPolicy
from app.settings import Settings


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


def test_retryable_model_error_ignores_user_input_pause() -> None:
    exc = RuntimeError("I need party size before I can continue.")
    assert _is_retryable_model_error(exc) is False


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
            date="2026-05-26",
            time="21:00",
            party_size=2,
        )

    assert excinfo.value.current_step == "waiting_for_confirmation"
    assert "not free to cancel" in excinfo.value.question.lower()
    assert "$45.00" in excinfo.value.details


def test_unknown_resy_policy_requires_manual_confirmation() -> None:
    with pytest.raises(PauseForInputRequested) as excinfo:
        _maybe_raise_nonfree_resy_confirmation(
            None,
            venue_id="82481",
            date="2026-05-26",
            time="21:00",
            party_size=2,
        )

    assert excinfo.value.current_step == "waiting_for_confirmation"
    assert "could not verify" in excinfo.value.question.lower()
    assert "Cancellation policy: unavailable" in excinfo.value.details


def test_render_resy_slot_policy_handles_missing_policy() -> None:
    assert _render_resy_slot_policy(None) == "Cancellation policy: unavailable from the current Resy slot data."


def test_canonical_booking_site_key_normalizes_resy() -> None:
    assert _canonical_booking_site_key("resy") == "resy.com"
    assert _canonical_booking_site_key("resy.com") == "resy.com"


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
