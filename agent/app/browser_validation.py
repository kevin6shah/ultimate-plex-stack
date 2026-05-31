from __future__ import annotations

import re


_BOOKING_MARKERS = (
    "confirmation number",
    "reservation confirmed",
    "booking confirmed",
    "confirmed reservation",
    "successfully booked",
    "booking complete",
)
_CANCELLATION_MARKERS = (
    "cancellation confirmed",
    "reservation cancelled",
    "reservation canceled",
    "successfully cancelled",
    "successfully canceled",
    "cancellation number",
)
_PAYMENT_MARKERS = (
    "order number",
    "receipt",
    "payment confirmed",
    "transaction id",
)


def browser_modal_dismissal_instruction() -> str:
    return (
        "Before any primary action, dismiss non-essential cookie banners, promo modals, chat widgets, sign-in nags, "
        "and overlays that block clicks or typing. If a blocking overlay reappears once, dismiss it once and continue."
    )


def task_requires_confirmation_evidence(task: str) -> bool:
    normalized = " ".join((task or "").lower().split())
    booking_markers = (
        "book ",
        "book me",
        "reserve ",
        "reservation",
        "checkout",
        "place order",
        "complete booking",
        "confirm the booking",
    )
    cancellation_markers = (
        "cancel reservation",
        "cancel that reservation",
        "cancel booking",
        "cancel that booking",
        "cancellation flow",
    )
    payment_markers = (
        "submit payment",
        "pay ",
        "complete checkout",
    )
    return any(marker in normalized for marker in booking_markers + cancellation_markers + payment_markers)


def validation_instruction_for_task(task: str) -> str:
    if not task_requires_confirmation_evidence(task):
        return ""
    return (
        "Do not report the task as completed unless the page shows explicit success evidence. "
        "Look for literal confirmation text such as a confirmation number, reservation confirmed, booking confirmed, "
        "order number, receipt, cancellation confirmed, or an equivalent success state visible on the page."
    )


def extract_validation_evidence(task: str, result_text: str) -> list[str]:
    normalized = re.split(r"[\r\n]+", result_text or "")
    markers = list(_BOOKING_MARKERS + _CANCELLATION_MARKERS + _PAYMENT_MARKERS)
    evidence: list[str] = []
    for line in normalized:
        cleaned = " ".join(line.split())
        lowered = cleaned.lower()
        if not cleaned:
            continue
        if any(marker in lowered for marker in markers):
            evidence.append(cleaned[:300])
    return evidence[:6]


def result_has_validation_evidence(task: str, result_text: str, *, explicit_evidence: list[str] | None = None) -> bool:
    if not task_requires_confirmation_evidence(task):
        return True
    if explicit_evidence:
        return True
    return bool(extract_validation_evidence(task, result_text))


def validation_failure_text(task: str) -> str:
    if "cancel" in (task or "").lower():
        return "BROWSER_VALIDATION_FAILED: the browser flow did not surface explicit cancellation confirmation evidence."
    if "pay " in (task or "").lower() or "checkout" in (task or "").lower():
        return "BROWSER_VALIDATION_FAILED: the browser flow did not surface explicit payment or order confirmation evidence."
    return "BROWSER_VALIDATION_FAILED: the browser flow did not surface explicit booking confirmation evidence."
