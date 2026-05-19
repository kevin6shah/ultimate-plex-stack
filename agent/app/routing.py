from __future__ import annotations

import re
from dataclasses import dataclass

from .jobs import TaskClass


@dataclass(frozen=True)
class TaskRoutingProfile:
    name: str
    summary: str
    instructions: tuple[str, ...] = ()


LONG_TASK_PATTERNS = (
    r"\b(browser|browse|research|compare|book|buy|order|reserve|apply|fill out)\b",
    r"\b(login|account|website|web site|form|checkout|cart)\b",
    r"\b(reservation|reservations|availability|available|booking|bookings|restaurant)\b",
    r"\b(itinerary|travel plan|trip plan|route|directions|google maps|map out)\b",
    r"\b(deep dive|investigate|audit|debug|deploy|migrate|scrape)\b",
    r"\b(monitor|track|watch for|keep checking)\b",
)
HEAVY_TASK_PATTERNS = (
    r"\b(browser|browse|website|web site|login|account|form|checkout|click|upload|download)\b",
    r"\b(compare|research|deep dive|investigate|audit|scrape|debug|deploy|migrate)\b",
    r"\b(reservation|reservations|availability|available|booking|bookings|restaurant)\b",
    r"\b(csv|xlsx|spreadsheet|excel|pdf|document|attachment|file|image)\b",
    r"\b(itinerary|travel plan|trip plan|route|directions|google maps|map out|places to visit)\b",
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
    r"\b(itinerary|travel plan|trip plan|route|directions|google maps|map out)\b",
)

SPREADSHEET_DATA_PATTERNS = (
    r"\b(csv|xlsx|spreadsheet|excel|table|tabular|dataset|data set)\b",
    r"\b(export|chart|rows|columns)\b",
)
ITINERARY_MAPS_PATTERNS = (
    r"\b(itinerary|travel plan|trip plan|route|directions|map out|google maps)\b",
    r"\b(places to visit|things to do|stops along the way|walking route|driving route)\b",
    r"\b(flight|flights|airfare|hotel|hotels|rental car|rental cars|car rental|car rentals|google flights|google travel|skiplagged)\b",
)
BOOKING_COMMERCE_PATTERNS = (
    r"\b(book|booking|reserve|reservation|availability|available)\b",
    r"\b(restaurant|hotel|flight|flights|table|tickets|ticket)\b",
    r"\b(buy|purchase|order|checkout|cart)\b",
)
LOGIN_ACCOUNT_PATTERNS = (
    r"\b(login|log in|sign in|sign-in|sign up|signup|register)\b",
    r"\b(account|password|email address|verification code|otp|2fa|captcha)\b",
)

GENERAL_PROFILE = TaskRoutingProfile(
    name="general",
    summary="Use deterministic search/fetch first; escalate to browser interaction only when the site truly requires it.",
)
SPREADSHEET_DATA_PROFILE = TaskRoutingProfile(
    name="spreadsheet_data",
    summary="This is a spreadsheet or structured-data task.",
    instructions=(
        "Prefer workspace files, Python, CSV/XLSX generation, and concise tabular outputs over browser-heavy collection.",
        "If the user wants a spreadsheet or data file, create the file directly in the workspace.",
        "Use the browser only if the source data cannot be gathered deterministically.",
    ),
)
ITINERARY_MAPS_PROFILE = TaskRoutingProfile(
    name="itinerary_maps",
    summary="This is an itinerary, routes, or maps task.",
    instructions=(
        "Prefer deterministic search/fetch and structured planning over browsing around map UIs.",
        "For flights, hotels, and rental cars, prefer direct structured travel tools before any browser path.",
        "Gather places, travel times, and route structure first; use browser interaction only for unsupported map-specific steps.",
        "Produce a concise itinerary or route artifact when the user asks for a deliverable.",
    ),
)
BOOKING_COMMERCE_PROFILE = TaskRoutingProfile(
    name="booking_commerce",
    summary="This is a booking, reservation, or commerce task.",
    instructions=(
        "Prefer a vetted connector or deterministic availability research first.",
        "For restaurant reservation work, prefer structured restaurant tools with Resy first for search, availability, and booking. Only use OpenTable when explicitly requested or for a manual handoff URL.",
        "For flights, hotels, and cars, prefer structured travel tools over opening aggregator websites.",
        "Use Browser-use only for the interaction or confirmation step when a deterministic path is insufficient.",
        "Pause for input or approval instead of improvising risky commits.",
    ),
)
LOGIN_ACCOUNT_PROFILE = TaskRoutingProfile(
    name="login_account",
    summary="This task involves login, sign-in, sign-up, or account gates.",
    instructions=(
        "Do not improvise account creation or credential entry.",
        "Pause at login, sign-up, or verification walls when user input, identity choice, or approval is required.",
        "Reserve browser steps for the actual interaction flow after the needed input is available.",
    ),
)


def _matches_any(normalized: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, normalized) for pattern in patterns)


def task_routing_profile(query: str) -> TaskRoutingProfile:
    normalized = query.strip().lower()
    if _matches_any(normalized, LOGIN_ACCOUNT_PATTERNS):
        return LOGIN_ACCOUNT_PROFILE
    if _matches_any(normalized, SPREADSHEET_DATA_PATTERNS):
        return SPREADSHEET_DATA_PROFILE
    if _matches_any(normalized, ITINERARY_MAPS_PATTERNS):
        return ITINERARY_MAPS_PROFILE
    if _matches_any(normalized, BOOKING_COMMERCE_PATTERNS):
        return BOOKING_COMMERCE_PROFILE
    return GENERAL_PROFILE


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
    if task_routing_profile(normalized).name in {"spreadsheet_data", "itinerary_maps", "booking_commerce", "login_account"}:
        return TaskClass.HEAVY
    if any(re.search(pattern, normalized) for pattern in HEAVY_TASK_PATTERNS):
        return TaskClass.HEAVY
    return TaskClass.LIGHT


def needs_confirmation(text: str) -> bool:
    normalized = text.lower()
    if not normalized.strip():
        return False

    imperative_spend_patterns = (
        r"^\s*(buy|purchase|order|pay|subscribe)\b",
        r"\b(use my card|use my credit card|charge my card)\b",
    )
    if any(re.search(pattern, normalized) for pattern in imperative_spend_patterns):
        return True

    if any(normalized.startswith(prefix) for prefix in ("what ", "how ", "why ", "when ", "where ", "who ", "is ", "are ", "does ", "do ", "should ", "would ", "could ")):
        return False
    if any(phrase in normalized for phrase in ("not right now", "later", "for later", "just research", "look into it", "tell me if", "compare", "pros and cons")):
        return False

    if any(
        re.search(pattern, normalized)
        for pattern in (
            r"\b(delete|remove|destroy|terminate|wipe)\b",
            r"\b(change password|reset password|update account|close account)\b",
            r"\b(ssn|social security|credit card|bank account|routing number)\b",
            r"\b(login with|sign in with|enter my password|use my password|use my card)\b",
            r"\bsubmit\b.{0,30}\b(form|application|claim|ticket|request)\b",
            r"\b(checkout|check out)\b.{0,20}\b(cart|payment|pay|purchase|order)\b",
        )
    ):
        return True

    outbound_patterns = (
        r"\b(send|email|text|message|post)\b.{0,40}\b(to|for|on)\b",
        r"\b(reply|respond)\b.{0,20}\b(to)\b",
    )
    return any(re.search(pattern, normalized) for pattern in outbound_patterns)
