from __future__ import annotations

import re

from .jobs import TaskClass


LONG_TASK_PATTERNS = (
    r"\b(browser|browse|research|compare|book|buy|order|reserve|apply|fill out)\b",
    r"\b(login|account|website|web site|form|checkout|cart)\b",
    r"\b(reservation|reservations|availability|available|booking|bookings|restaurant)\b",
    r"\b(deep dive|investigate|audit|debug|deploy|migrate|scrape)\b",
    r"\b(monitor|track|watch for|keep checking)\b",
)
HEAVY_TASK_PATTERNS = (
    r"\b(browser|browse|website|web site|login|account|form|checkout|click|upload|download)\b",
    r"\b(compare|research|deep dive|investigate|audit|scrape|debug|deploy|migrate)\b",
    r"\b(reservation|reservations|availability|available|booking|bookings|restaurant)\b",
    r"\b(csv|xlsx|spreadsheet|excel|pdf|document|attachment|file|image)\b",
    r"\b(run python|run shell|script|workspace|artifact)\b",
    r"\b(continue that task|resume that task|resume the task|continue the task)\b",
)
LIVE_WEB_PATTERNS = (
    r"https?://",
    r"\b(latest|current|today|tonight|tomorrow|right now|live)\b",
    r"\b(weather|forecast|headline|news|price|stock|score|status)\b",
    r"\b(browser|browse|website|web site|login|account|form|checkout)\b",
    r"\b(research|compare|search the web|search online|look on the web)\b",
    r"\b(reservation|reservations|availability|available|booking|bookings|restaurant)\b",
)


def is_long_task(query: str) -> bool:
    normalized = query.strip().lower()
    if len(normalized) > 220:
        return True
    return any(re.search(pattern, normalized) for pattern in LONG_TASK_PATTERNS)


def should_offer_browser_tool(query: str) -> bool:
    normalized = query.strip().lower()
    return any(re.search(pattern, normalized) for pattern in LIVE_WEB_PATTERNS)


def classify_task(query: str, *, has_attachment: bool = False) -> TaskClass:
    normalized = query.strip().lower()
    if has_attachment:
        return TaskClass.HEAVY
    if len(normalized) > 220:
        return TaskClass.HEAVY
    if any(re.search(pattern, normalized) for pattern in HEAVY_TASK_PATTERNS):
        return TaskClass.HEAVY
    return TaskClass.LIGHT


def needs_confirmation(text: str) -> bool:
    normalized = text.lower()
    if any(
        re.search(pattern, normalized)
        for pattern in (
            r"\b(buy|purchase|order|checkout|pay|subscribe|cancel subscription)\b",
            r"\b(delete|remove|destroy|terminate|wipe)\b",
            r"\b(change password|reset password|update account|close account)\b",
            r"\b(ssn|social security|credit card|bank account|routing number)\b",
            r"\b(login with|sign in with|enter my password|use my password|use my card)\b",
            r"\bsubmit\b.{0,30}\b(form|application|claim|ticket|request)\b",
        )
    ):
        return True

    outbound_patterns = (
        r"\b(send|email|text|message|post)\b.{0,40}\b(to|for|on)\b",
        r"\b(reply|respond)\b.{0,20}\b(to)\b",
    )
    return any(re.search(pattern, normalized) for pattern in outbound_patterns)
