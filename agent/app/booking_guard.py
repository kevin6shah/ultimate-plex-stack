from __future__ import annotations

import re
from dataclasses import dataclass


_PAYMENT_FIELD_PATTERNS = (
    r"\bcredit card\b",
    r"\bdebit card\b",
    r"\bcard number\b",
    r"\bpayment method\b",
    r"\bbilling\b",
    r"\bcvv\b",
    r"\bexpiration date\b",
    r"\bstripe\b",
    r"\badyen\b",
    r"\bbraintree\b",
)

_NON_ZERO_PATTERNS = (
    r"\$\s*([1-9]\d*(?:\.\d{2})?)",
    r"\bUSD\s*([1-9]\d*(?:\.\d{2})?)",
    r"\btotal[:\s]+\$?\s*([1-9]\d*(?:\.\d{2})?)",
    r"\bfee[:\s]+\$?\s*([1-9]\d*(?:\.\d{2})?)",
    r"\bdeposit[:\s]+\$?\s*([1-9]\d*(?:\.\d{2})?)",
    r"\bhold[:\s]+\$?\s*([1-9]\d*(?:\.\d{2})?)",
)

_ZERO_PATTERNS = (
    r"\$\s*0(?:\.00)?\b",
    r"\bUSD\s*0(?:\.00)?\b",
    r"\bfree\b",
    r"\bno charge\b",
    r"\btotal[:\s]+\$?\s*0(?:\.00)?\b",
)


@dataclass(frozen=True)
class BookingCheckoutAssessment:
    payment_fields_present: bool
    non_zero_amount_present: bool
    zero_total_present: bool
    matched_fragments: tuple[str, ...]

    @property
    def should_block(self) -> bool:
        return self.payment_fields_present or self.non_zero_amount_present


def assess_checkout_text(text: str) -> BookingCheckoutAssessment:
    normalized = " ".join((text or "").split())
    lowered = normalized.lower()
    fragments: list[str] = []

    payment_fields_present = False
    for pattern in _PAYMENT_FIELD_PATTERNS:
        match = re.search(pattern, lowered, flags=re.IGNORECASE)
        if match:
            payment_fields_present = True
            fragments.append(match.group(0))

    non_zero_amount_present = False
    for pattern in _NON_ZERO_PATTERNS:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            non_zero_amount_present = True
            fragments.append(match.group(0))

    zero_total_present = False
    for pattern in _ZERO_PATTERNS:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            zero_total_present = True
            fragments.append(match.group(0))

    deduped = tuple(dict.fromkeys(fragment.strip() for fragment in fragments if fragment.strip()))
    return BookingCheckoutAssessment(
        payment_fields_present=payment_fields_present,
        non_zero_amount_present=non_zero_amount_present,
        zero_total_present=zero_total_present,
        matched_fragments=deduped,
    )


def enforce_zero_dollar_booking(text: str) -> None:
    assessment = assess_checkout_text(text)
    if not assessment.should_block:
        return
    matched = ", ".join(assessment.matched_fragments[:5]) or "payment wall"
    raise RuntimeError(
        "NON_ZERO_CHECKOUT_BLOCKED: Friday Phase 1 only permits autonomous $0 bookings. "
        f"Blocked because checkout indicators were detected: {matched}."
    )


def zero_dollar_booking_instruction() -> str:
    return (
        "Before any final submit, inspect the visible checkout state. If the page shows a payment form, card fields, "
        "deposit, hold, service fee, or any non-zero amount, stop immediately and report "
        "'NON_ZERO_CHECKOUT_BLOCKED' instead of clicking the final button."
    )
