from __future__ import annotations

from app.restaurant_cli import _format_resy_slot_policy


def test_format_resy_slot_policy_marks_cancellation_fee_for_manual_confirmation() -> None:
    slot = {
        "config": {
            "token": "rgs://resy/73231/example",
            "type": "Outdoor Seating",
        },
        "date": {"start": "2026-05-25 21:00:00"},
        "payment": {
            "is_paid": True,
            "cancellation_fee": 45.0,
            "deposit_fee": None,
            "service_charge": None,
            "secs_cancel_cut_off": 86400,
            "secs_change_cut_off": 86400,
        },
    }

    policy = _format_resy_slot_policy(slot)

    assert policy.requires_manual_confirmation is True
    assert policy.free_cancellation is False
    assert policy.time == "21:00"
    assert "Cancellation fee: $45.00" in policy.policy_text
    assert "24 hours" in policy.policy_text


def test_format_resy_slot_policy_allows_free_cancellation_when_no_fee_is_shown() -> None:
    slot = {
        "config": {
            "token": "rgs://resy/73231/free",
            "type": "Main Dining Room",
        },
        "date": {"start": "2026-05-25 18:30:00"},
        "payment": {
            "is_paid": False,
            "cancellation_fee": None,
            "deposit_fee": None,
            "service_charge": None,
            "secs_cancel_cut_off": None,
            "secs_change_cut_off": None,
        },
    }

    policy = _format_resy_slot_policy(slot)

    assert policy.requires_manual_confirmation is False
    assert policy.free_cancellation is True
    assert "No cancellation fee is shown" in policy.policy_text
