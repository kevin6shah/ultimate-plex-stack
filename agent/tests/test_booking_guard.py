from __future__ import annotations

import pytest

from app.booking_guard import assess_checkout_text, enforce_zero_dollar_booking


def test_assess_checkout_text_allows_free_checkout_copy() -> None:
    assessment = assess_checkout_text("Reservation confirmed. Total: $0.00. No charge today.")
    assert assessment.zero_total_present is True
    assert assessment.should_block is False


def test_assess_checkout_text_blocks_non_zero_total() -> None:
    assessment = assess_checkout_text("Checkout summary. Service fee $12.00. Total $12.00.")
    assert assessment.non_zero_amount_present is True
    assert assessment.should_block is True


def test_assess_checkout_text_blocks_payment_fields() -> None:
    assessment = assess_checkout_text("Enter credit card number and CVV to continue.")
    assert assessment.payment_fields_present is True
    assert assessment.should_block is True


def test_enforce_zero_dollar_booking_raises_for_non_zero_checkout() -> None:
    with pytest.raises(RuntimeError, match="NON_ZERO_CHECKOUT_BLOCKED"):
        enforce_zero_dollar_booking("Billing details. Deposit $25.00 required to reserve.")
