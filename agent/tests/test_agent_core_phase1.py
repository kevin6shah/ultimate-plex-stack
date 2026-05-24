from __future__ import annotations

import pytest

from app.agent_core import (
    PauseForInputRequested,
    _booking_choice_pause_payload,
    _ensure_default_mailbox_identity,
    _enforce_automation_policy,
    _find_automation_policy,
    _handle_phase1_blocking_error,
    _restaurant_booking_missing_details,
)
from app.jobs import AutomationPolicyRecord, IdentityRecord
from app.settings import Settings


class _FakeStore:
    def __init__(
        self,
        existing: list[IdentityRecord] | None = None,
        policies: list[AutomationPolicyRecord] | None = None,
    ) -> None:
        self.records = list(existing or [])
        self.created: list[IdentityRecord] = []
        self.policies = list(policies or [])

    def list_identities(self, limit: int = 100):
        return self.records[:limit]

    def put_identity(self, record: IdentityRecord) -> IdentityRecord:
        self.records.append(record)
        self.created.append(record)
        return record

    def list_automation_policies(self, limit: int = 200):
        return self.policies[:limit]


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

    assert excinfo.value.current_step == "payment_blocked"
    assert "payment" in excinfo.value.question.lower()


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
