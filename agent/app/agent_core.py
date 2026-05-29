from __future__ import annotations

import json
import os
import logging
import re
import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

from pydantic_ai import Agent, RunContext

from .browser import BrowserSession, run_browser_task
from .browser_use_runner import run_browser_use_task
from .booking_guard import enforce_zero_dollar_booking
from .budget import estimate_deepseek_cost, usage_from_pydantic_ai
from .jobs import (
    AgentConfig,
    AgentJob,
    AgentResult,
    AutomationPolicyRecord,
    BookingRecord,
    BrowserSessionRecord,
    CheckpointPayload,
    IdentityRecord,
    MailboxVerificationWaitRecord,
    ThreadTurn,
    ThreadTurnRole,
)
from .prompts import STATIC_SYSTEM_PROMPT
from .research import fetch_page_content, sanitize_tool_output, search_web
from .restaurant_cli import (
    build_opentable_booking_url,
    choose_best_restaurant_result,
    fetch_opentable_slots_via_browser,
    fetch_resy_slot_policies,
    normalize_restaurant_provider,
    restaurant_provider_sequence,
    RestaurantSlotPolicy,
    run_restaurant_cli,
    run_restaurant_cli_json,
)
from .routing import needs_confirmation, task_routing_profile
from .skiplagged import call_skiplagged_tool
from .stagehand_runner import run_stagehand_task
from .settings import Settings
from .storage import StateStore
from .strategy_runtime import (
    STRATEGY_API_DIRECT,
    STRATEGY_BROWSER_USE_VISUAL_PIVOT,
    STRATEGY_STAGEHAND_STEALTH_ACT,
    strategy_guidance,
)
from .temporal_runtime import heavy_workflow_id
from .workspace import Workspace

logger = logging.getLogger(__name__)


class PauseForInputRequested(RuntimeError):
    def __init__(
        self,
        *,
        question: str,
        details: str = "",
        summary: str = "",
        current_step: str = "waiting_for_user_input",
        resume_instructions: str = "",
    ) -> None:
        normalized_question = question.strip() or "additional user input required"
        super().__init__(normalized_question)
        self.question = normalized_question
        self.details = details.strip()
        self.summary = (summary.strip() or normalized_question)[:2000]
        self.current_step = (current_step.strip() or "waiting_for_user_input")[:500]
        self.resume_instructions = resume_instructions.strip()


def _is_retryable_model_error(exc: Exception) -> bool:
    normalized = str(exc or "").strip().lower()
    if not normalized:
        return False
    if "structured restaurant availability failed in api_direct mode" in normalized:
        return False
    retry_markers = (
        "status_code: 500",
        "internal server error",
        "internal_error",
        "model_name: deepseek-chat",
        "model_name: deepseek/deepseek-chat",
        "gateway timeout",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "service unavailable",
        "upstream connect error",
    )
    return any(marker in normalized for marker in retry_markers)


async def _run_agent_with_model_retries(agent: Agent, effective_query: str, *, deps: "AgentDependencies"):
    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            return await agent.run(effective_query, deps=deps)
        except PauseForInputRequested:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= 3 or not _is_retryable_model_error(exc):
                raise
            delay = float(attempt)
            logger.warning(
                "agent model retry attempt=%s/3 delay=%.1fs error=%s",
                attempt,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


@dataclass
class AgentDependencies:
    settings: Settings
    store: StateStore
    workspace: Optional[Workspace] = None
    browser: Optional[BrowserSession] = None
    current_job: Optional[AgentJob] = None
    strategy_mode: str = STRATEGY_API_DIRECT
    restaurant_booking_prefill: Optional["RestaurantBookingPrefill"] = None
    restaurant_booking_preflight: Optional["RestaurantBookingPreflightResult"] = None


@dataclass(frozen=True)
class RestaurantBookingPrefill:
    venue_query: str
    city: str
    provider: str
    date: str
    time: str
    party_size: int


@dataclass(frozen=True)
class RestaurantBookingPreflightVenue:
    venue_id: str
    venue_name: str
    venue_city: str
    venue_url: str
    provider: str


@dataclass(frozen=True)
class RestaurantBookingPreflightResult:
    summary: str
    matched_venue: Optional[RestaurantBookingPreflightVenue] = None


@dataclass(frozen=True)
class RestaurantDiscoveryPrefill:
    search_query: str
    city: str
    provider: str
    date: str
    time: str
    party_size: int


@dataclass(frozen=True)
class RestaurantDiscoveryCandidate:
    venue_id: str
    venue_name: str
    venue_city: str
    venue_url: str
    provider: str


@dataclass(frozen=True)
class RestaurantDiscoveryPreflightResult:
    summary: str
    candidates: tuple[RestaurantDiscoveryCandidate, ...]


@dataclass(frozen=True)
class ResyBrowserProbeResult:
    booking_url: str
    selected_exact_time_label: str = ""
    nearest_time_labels: tuple[str, ...] = ()
    visible_time_labels: tuple[str, ...] = ()
    venue_note: str = ""
    current_url: str = ""


@dataclass(frozen=True)
class RestaurantSearchAttempt:
    provider: str
    payload: dict[str, object]
    best_match: Optional[dict[str, object]]
    error: str = ""


@dataclass(frozen=True)
class OpenTablePolicyAssessment:
    free_cancellation: bool = False
    requires_manual_confirmation: bool = True
    policy_text: str = "Cancellation policy: unavailable from the current OpenTable page."


def _resolved_secret(settings: Settings, parameter_name: str) -> str:
    if not parameter_name:
        return ""
    value = settings.secret(parameter_name).strip()
    if not value:
        return ""
    if not parameter_name.startswith("/") and value == parameter_name and parameter_name.isupper():
        return ""
    return value


def _build_resy_booking_page_url(venue_url: str, *, date: str, party_size: int) -> str:
    parsed = urlparse(venue_url.strip())
    if not parsed.scheme or not parsed.netloc:
        return venue_url.strip()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["date"] = date
    query["seats"] = str(max(1, party_size))
    return urlunparse(parsed._replace(query=urlencode(query)))


def _parse_clock_minutes(value: str) -> Optional[int]:
    normalized = " ".join(value.strip().upper().replace(".", "").split())
    if not normalized or normalized in {"ALL DAY", "ANY TIME"}:
        return None
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([AP]M)?\b", normalized)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = match.group(3)
    if meridiem:
        hour %= 12
        if meridiem == "PM":
            hour += 12
    if hour >= 24 or minute >= 60:
        return None
    return hour * 60 + minute


def _render_clock_label(value: str) -> str:
    minutes = _parse_clock_minutes(value)
    if minutes is None:
        return value
    hour = minutes // 60
    minute = minutes % 60
    meridiem = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {meridiem}"


def _select_resy_time_option_labels(requested_time: str, options: list[dict[str, str]]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    visible_labels = tuple(
        label
        for label in (
            " ".join(str(option.get("label") or option.get("value") or "").split())
            for option in options
        )
        if label
    )
    requested_minutes = _parse_clock_minutes(requested_time)
    if requested_minutes is None:
        return "", tuple(), visible_labels
    candidates: list[tuple[int, int, str]] = []
    exact_label = ""
    for option in options:
        option_label = " ".join(str(option.get("label") or option.get("value") or "").split())
        if not option_label:
            continue
        option_minutes = _parse_clock_minutes(option_label)
        if option_minutes is None:
            continue
        if option_minutes == requested_minutes and not exact_label:
            exact_label = option_label
        candidates.append((abs(option_minutes - requested_minutes), option_minutes, option_label))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    nearest_labels: list[str] = []
    seen: set[str] = set()
    for _, _, label in candidates:
        if label in seen or label == exact_label:
            continue
        seen.add(label)
        nearest_labels.append(label)
        if len(nearest_labels) >= 4:
            break
    return exact_label, tuple(nearest_labels), visible_labels


def _extract_resy_venue_note(body_text: str) -> str:
    compact = " ".join(body_text.split())
    if not compact:
        return ""
    patterns = (
        r"(You can check availability and reserve on Resy starting at midnight[^.?!]*[.?!])",
        r"(Reservations open up for dinner \d+ days in advance via Resy[^.?!]*[.?!])",
        r"(If you do not see availability[^.?!]*[.?!])",
        r"(We recommend you add your name to the notify list[^.?!]*[.?!])",
        r"(No times are available[^.?!]*[.?!])",
    )
    notes: list[str] = []
    for pattern in patterns:
        match = re.search(pattern, compact, flags=re.IGNORECASE)
        if not match:
            continue
        note = match.group(1).strip()
        if note not in notes:
            notes.append(note)
        if len(notes) >= 2:
            break
    return " ".join(notes)


async def _run_resy_browser_probe(
    *,
    settings: Settings,
    workspace: Workspace,
    venue_url: str,
    venue_name: str,
    venue_city: str,
    date: str,
    time: str,
    party_size: int,
) -> Optional[ResyBrowserProbeResult]:
    if not venue_url.strip():
        return None
    booking_url = _build_resy_booking_page_url(venue_url, date=date, party_size=party_size)
    browser = BrowserSession(workspace=workspace, settings=settings)
    try:
        await browser.start(booking_url)
        if await browser.has_selector("select[name='party_size']"):
            await browser.type_text("select[name='party_size']", str(max(1, party_size)))
        time_options = await browser.select_options("select[name='time']") if await browser.has_selector("select[name='time']") else []
        exact_label, nearest_labels, visible_labels = _select_resy_time_option_labels(time, time_options)
        if exact_label:
            await browser.type_text("select[name='time']", exact_label)
        body_text = await browser.read(limit=7000)
        return ResyBrowserProbeResult(
            booking_url=booking_url,
            selected_exact_time_label=exact_label,
            nearest_time_labels=nearest_labels,
            visible_time_labels=visible_labels,
            venue_note=_extract_resy_venue_note(body_text),
            current_url=await browser.current_url(),
        )
    except Exception as exc:
        logger.warning(
            "resy_browser_probe_failed venue=%s city=%s date=%s party_size=%s error=%s",
            venue_name,
            venue_city,
            date,
            party_size,
            exc,
        )
        return None
    finally:
        await browser.close()


def _render_resy_browser_probe_summary(
    probe: ResyBrowserProbeResult,
    *,
    venue_id: str,
    venue_name: str,
    venue_city: str,
    date: str,
    time: str,
    party_size: int,
) -> str:
    normalized_requested_time = str(time or "").strip().lower()
    flexible_requested_time = normalized_requested_time in {"", "(not specified)"} or normalized_requested_time == "any available"
    lines = [
        "BROWSER_RESY_PROBE:",
        f"Matched venue: {venue_name}" + (f" ({venue_city})" if venue_city else ""),
        f"Venue id: {venue_id}",
        f"Booking page: {probe.booking_url}",
        f"Requested date: {date}",
        f"Requested time: {time}",
        f"Party size: {max(1, party_size)}",
    ]
    if probe.current_url and probe.current_url != probe.booking_url:
        lines.append(f"Current browser URL: {probe.current_url}")
    if probe.selected_exact_time_label:
        lines.append(f"Exact requested time is selectable on the live venue page: {probe.selected_exact_time_label}.")
    elif flexible_requested_time and probe.visible_time_labels:
        lines.append("Live time options visible on the venue page:")
    elif probe.nearest_time_labels:
        lines.append("Exact requested time is not selectable on the live venue page.")
        lines.append("Closest live time options on the page:")
        for label in probe.nearest_time_labels:
            lines.append(f"- {label}")
    elif probe.visible_time_labels:
        lines.append("The live venue page exposed a time selector, but the requested time was not present.")
    elif flexible_requested_time:
        lines.append("The live venue page did not expose selectable time options.")
    if probe.visible_time_labels:
        if not flexible_requested_time:
            lines.append("Visible time selector options:")
        for label in probe.visible_time_labels[:8]:
            lines.append(f"- {label}")
    if probe.venue_note:
        lines.append(f"Venue note: {probe.venue_note}")
    lines.append(
        "Use this as the matched Resy venue context immediately. Do not restart venue matching. "
        "Prefer structured Resy tools with this venue id for live availability, policy checks, and booking. "
        "Only fall back to raw browser selectors if the structured provider call still fails after the venue is matched. "
        "If the exact time is not selectable or the venue page says availability opens later, pause and ask the user whether to choose another time, venue, or source."
    )
    return sanitize_tool_output("\n".join(lines))


async def _resy_availability_browser_probe_summary(
    *,
    settings: Settings,
    workspace: Workspace,
    venue_id: str,
    venue_name: str,
    venue_city: str,
    venue_url: str,
    date: str,
    party_size: int,
) -> Optional[str]:
    probe = await _run_resy_browser_probe(
        settings=settings,
        workspace=workspace,
        venue_url=venue_url,
        venue_name=venue_name,
        venue_city=venue_city,
        date=date,
        time="",
        party_size=party_size,
    )
    if probe is None:
        return None
    return _render_resy_browser_probe_summary(
        probe,
        venue_id=venue_id,
        venue_name=venue_name,
        venue_city=venue_city,
        date=date,
        time="(not specified)",
        party_size=party_size,
    )


def _ensure_default_mailbox_identity(settings: Settings, store: StateStore) -> Optional[IdentityRecord]:
    mailbox_email = _resolved_secret(settings, settings.gmail_account_email_param)
    if not mailbox_email:
        return None
    for record in store.list_identities(limit=100):
        if record.email.strip().lower() == mailbox_email.lower():
            return record
    identity = IdentityRecord(
        label="Friday Gmail",
        email=mailbox_email,
        provider="gmail",
        category="shared_mailbox",
        site_scope="shared",
        is_default=True,
        notes="Default Friday-controlled mailbox identity for verification and low-risk account flows.",
    )
    store.put_identity(identity)
    return identity


def _site_scope_matches(policy_scope: str, target_scope: str) -> bool:
    normalized_policy = policy_scope.strip().lower()
    normalized_target = target_scope.strip().lower()
    if not normalized_policy:
        return True
    if not normalized_target:
        return False
    if normalized_policy == normalized_target:
        return True
    return normalized_target.endswith("." + normalized_policy)


def _policy_category_for_site(site_scope: str, *, fallback: str = "general") -> str:
    normalized = site_scope.strip().lower()
    if any(token in normalized for token in ("resy", "opentable", "restaurant")):
        return "restaurant"
    return fallback


def _find_automation_policy(store: StateStore, *, site_scope: str, category: str) -> Optional[AutomationPolicyRecord]:
    normalized_site = site_scope.strip().lower()
    normalized_category = category.strip().lower() or "general"
    candidates: list[tuple[int, AutomationPolicyRecord]] = []
    for record in store.list_automation_policies(limit=200):
        site_match = _site_scope_matches(record.site_scope, normalized_site)
        category_match = not record.category.strip() or record.category.strip().lower() == normalized_category
        if not site_match or not category_match:
            continue
        score = 0
        if record.category.strip().lower() == normalized_category:
            score += 2
        if record.site_scope.strip():
            score += 4 + len(record.site_scope.strip())
        candidates.append((score, record))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _raise_phase1_pause(*, question: str, details: str, summary: str, current_step: str, resume_instructions: str = "") -> None:
    raise PauseForInputRequested(
        question=question,
        details=details,
        summary=summary,
        current_step=current_step,
        resume_instructions=resume_instructions,
    )


def _raise_non_zero_checkout_pause(*, details: str) -> None:
    _raise_phase1_pause(
        question="I hit a payment or non-zero checkout wall, so I stopped before submitting anything.",
        details=details,
        summary="blocked by a payment or non-zero checkout wall",
        current_step="payment_blocked",
        resume_instructions=(
            "Do not keep clicking through the checkout flow. Wait for the operator to decide whether to stop, "
            "switch to a different venue, or handle the booking manually."
        ),
    )


def _raise_card_on_file_pause(*, details: str) -> None:
    _raise_phase1_pause(
        question="This looks like a $0 booking, but the site requires a card on file before it will confirm anything.",
        details=details,
        summary="waiting for approval before adding or using a card on file for a $0 booking",
        current_step="card_entry_required",
        resume_instructions=(
            "Do not continue past the card-entry step until the operator explicitly approves the card-on-file action "
            "or confirms that the payment method is already set up."
        ),
    )


def _raise_restaurant_provider_unavailable_pause(
    *,
    provider: str,
    venue_name: str,
    date: str,
    party_size: int,
    details: str = "",
    venue_url: str = "",
) -> None:
    normalized_provider = provider.strip().title() or "provider"
    extra_lines = [line for line in [details.strip(), f"Booking page: {venue_url}" if venue_url.strip() else ""] if line]
    _raise_phase1_pause(
        question=(
            f"I could not verify live availability on {normalized_provider} for {venue_name} because the provider kept returning errors. "
            "Do you want me to try another time, another restaurant, or a different booking source?"
        ),
        details="\n".join(
            [
                f"Venue: {venue_name}",
                f"Date: {date}",
                f"Party: {max(1, party_size)}",
                *extra_lines,
            ]
        ),
        summary=f"{normalized_provider} availability is currently unavailable for {venue_name}",
        current_step="provider_unavailable",
        resume_instructions=(
            "Do not keep retrying the same provider request. Use the user's next reply to switch time, venue, or booking source."
        ),
    )


def _restaurant_provider_browser_fallback_message(
    *,
    provider: str,
    venue_name: str,
    date: str,
    party_size: int,
    details: str = "",
    venue_url: str = "",
) -> str:
    normalized_provider = provider.strip().title() or "provider"
    lines = [
        (
            "RESTAURANT_TOOL_UNAVAILABLE: "
            f"{normalized_provider} live availability verification failed due to repeated provider errors. "
            "Continue with the hardened browser fallback to verify live slots and cancellation policy directly before booking."
        ),
        f"Venue: {venue_name}",
        f"Date: {date}",
        f"Party: {max(1, party_size)}",
    ]
    if details.strip():
        lines.append(details.strip())
    if venue_url.strip():
        lines.append(f"Booking page: {venue_url.strip()}")
    lines.append(
        "If browser verification also fails, pause with provider_unavailable instead of retrying the same structured provider path."
    )
    return "\n".join(lines)


def _restaurant_provider_label(provider: str) -> str:
    normalized = normalize_restaurant_provider(provider)
    if normalized == "opentable":
        return "OpenTable"
    if normalized == "resy":
        return "Resy"
    return normalized.title() or "provider"


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _assess_opentable_policy_text(page_text: str) -> OpenTablePolicyAssessment:
    normalized = " ".join((page_text or "").split())
    lowered = normalized.lower()
    free_patterns = (
        r"\bfree cancellation\b",
        r"\bcancel(?:ation)?(?: is)? free\b",
        r"\bcancel for free\b",
        r"\bfully refundable\b",
        r"\bno cancellation fee\b",
    )
    manual_patterns = (
        r"\bcancellation fee\b",
        r"\bdeposit\b",
        r"\bprepaid\b",
        r"\bnon-refundable\b",
        r"\bwill be charged\b",
        r"\bcancel within\b",
        r"\bcard required\b",
    )
    for pattern in free_patterns:
        match = re.search(pattern, lowered, flags=re.IGNORECASE)
        if match:
            return OpenTablePolicyAssessment(
                free_cancellation=True,
                requires_manual_confirmation=False,
                policy_text=f"Cancellation policy: {match.group(0).strip()}",
            )
    for pattern in manual_patterns:
        match = re.search(pattern, lowered, flags=re.IGNORECASE)
        if match:
            return OpenTablePolicyAssessment(
                free_cancellation=False,
                requires_manual_confirmation=True,
                policy_text=f"Cancellation policy: {match.group(0).strip()}",
            )
    return OpenTablePolicyAssessment()


def _opentable_requires_login_gate(page_text: str) -> bool:
    lowered = " ".join((page_text or "").split()).lower()
    if "continue with email" in lowered:
        return True
    required_markers = (
        "enter your email",
        "enter your password",
        "log in to continue",
        "sign in to continue",
        "continue signing in",
    )
    return any(marker in lowered for marker in required_markers)


def _maybe_raise_nonfree_opentable_confirmation(
    assessment: OpenTablePolicyAssessment,
    *,
    venue_id: str,
    venue_name: str = "",
    venue_city: str = "",
    date: str,
    time: str,
    party_size: int,
    booking_url: str = "",
) -> None:
    if assessment.free_cancellation and not assessment.requires_manual_confirmation:
        return
    venue_label = venue_name.strip()
    if venue_city.strip():
        venue_label = f"{venue_label} ({venue_city.strip()})" if venue_label else venue_city.strip()
    question = (
        f"I could not verify that {venue_label or 'this OpenTable reservation'} has free cancellation, "
        "so I need your explicit confirmation before booking it."
    )
    if assessment.policy_text and assessment.policy_text != "Cancellation policy: unavailable from the current OpenTable page.":
        question = (
            f"{venue_label or 'This OpenTable reservation'} is not clearly free to cancel, so I need your explicit confirmation before booking it. "
            + assessment.policy_text
        ).strip()
    details_lines = [
        "Provider: opentable",
        f"Venue: {venue_label}" if venue_label else "",
        f"Venue id: {venue_id}",
        f"Date: {date}",
        f"Time: {time}",
        f"Party size: {max(1, party_size)}",
        assessment.policy_text,
        f"Booking page: {booking_url}" if booking_url else "",
    ]
    _raise_phase1_pause(
        question=question,
        details="\n".join(line for line in details_lines if line).strip(),
        summary="waiting for your confirmation because this OpenTable booking is not proven free to cancel",
        current_step="waiting_for_confirmation",
        resume_instructions=(
            "Only continue if the user explicitly confirms that Friday should proceed despite the OpenTable cancellation policy. "
            "Otherwise, look for a free-cancellation alternative."
        ),
    )


def _extract_booking_confirmation_reference(page_text: str) -> str:
    patterns = (
        r"(?:confirmation|reservation)\s*(?:number|id|#)\s*[:#]?\s*([A-Z0-9-]{4,})",
        r"\b([A-Z0-9]{6,})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, page_text or "", flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


async def _run_opentable_booking_browser_flow(
    *,
    settings: Settings,
    workspace: Workspace,
    booking_url: str,
    venue_id: str,
    venue_name: str,
    venue_city: str,
    date: str,
    time: str,
    party_size: int,
) -> str:
    session = BrowserSession(workspace, settings=settings)
    try:
        await session.start(booking_url)
        requested_time_label = _render_clock_label(time)
        try:
            await session.click_text("Find a table")
        except Exception:
            pass
        try:
            await session.click_text(requested_time_label)
        except Exception:
            pass
        body = await session.read(limit=12000)
        lowered = body.lower()
        if "select a seating option" in lowered:
            try:
                await session.click_text("Standard")
            except Exception:
                pass
            body = await session.read(limit=12000)
            lowered = body.lower()
        if "standard reservation" in lowered:
            try:
                await session.click_text("Select")
            except Exception:
                pass
            body = await session.read(limit=12000)
            lowered = body.lower()
        if _opentable_requires_login_gate(body):
            email = _resolved_secret(settings, settings.opentable_email_param)
            password = _resolved_secret(settings, settings.opentable_password_param)
            if not email or not password:
                _raise_phase1_pause(
                    question="OpenTable needs a login session before I can continue this booking.",
                    details=f"Venue: {venue_name or venue_id}\nBooking page: {booking_url}",
                    summary="waiting for an OpenTable login session",
                    current_step="login_required",
                    resume_instructions="Provide or restore an OpenTable login session before continuing the same booking flow.",
                )
            for selector in (
                "button:has-text('Sign in')",
                "button:has-text('Log in')",
                "text=Continue with email",
            ):
                if await session.has_selector(selector):
                    await session.click(selector)
                    break
            for selector in ("input[type='email']", "input[name='email']", "input[autocomplete='email']"):
                if await session.has_selector(selector):
                    await session.type_text(selector, email)
                    break
            for selector in (
                "button:has-text('Continue')",
                "button:has-text('Next')",
                "button:has-text('Sign in')",
                "button:has-text('Log in')",
            ):
                if await session.has_selector(selector):
                    await session.click(selector)
                    break
            for selector in ("input[type='password']", "input[name='password']", "input[autocomplete='current-password']"):
                if await session.has_selector(selector):
                    await session.type_text(selector, password)
                    break
            for selector in (
                "button:has-text('Sign in')",
                "button:has-text('Log in')",
                "button:has-text('Continue')",
            ):
                if await session.has_selector(selector):
                    await session.click(selector)
                    break
            body = await session.read(limit=12000)
            lowered = body.lower()
        if any(marker in lowered for marker in ("verification code", "one-time code", "otp", "captcha", "verify you are human")):
            _raise_phase1_pause(
                question="OpenTable requires an extra verification step before I can finish this booking.",
                details=f"Venue: {venue_name or venue_id}\nBooking page: {booking_url}",
                summary="waiting at an OpenTable verification wall",
                current_step="verification_required",
                resume_instructions="Pause until the verification challenge is cleared or a reusable OpenTable session is restored.",
            )
        try:
            enforce_zero_dollar_booking(body)
        except Exception as exc:
            _handle_phase1_blocking_error(exc, action=f"opentable_booking_page({venue_id}, {date}, {time})")
            raise
        assessment = _assess_opentable_policy_text(body)
        _maybe_raise_nonfree_opentable_confirmation(
            assessment,
            venue_id=venue_id,
            venue_name=venue_name,
            venue_city=venue_city,
            date=date,
            time=time,
            party_size=party_size,
            booking_url=booking_url,
        )
        for selector in (
            "button:has-text('Complete reservation')",
            "button:has-text('Confirm reservation')",
            "button:has-text('Reserve now')",
            "button:has-text('Book now')",
            "button:has-text('Confirm')",
            "button:has-text('Complete')",
        ):
            if await session.has_selector(selector):
                await session.click(selector)
                break
        confirmation_text = await session.read(limit=12000)
        if "confirmed" not in confirmation_text.lower() and "reservation complete" not in confirmation_text.lower():
            return sanitize_tool_output(
                json.dumps(
                    {
                        "ok": False,
                        "provider": "opentable",
                        "booking_url": booking_url,
                        "message": "OpenTable booking page was reached, but Friday could not verify a final confirmation automatically.",
                        "policy": assessment.policy_text,
                    }
                )
            )
        confirmation_reference = _extract_booking_confirmation_reference(confirmation_text)
        payload: dict[str, Any] = {
            "ok": True,
            "provider": "opentable",
            "booking_url": booking_url,
            "message": "OpenTable reservation confirmed.",
            "policy": assessment.policy_text,
        }
        if confirmation_reference:
            payload["confirmation_reference"] = confirmation_reference
        return sanitize_tool_output(json.dumps(payload))
    finally:
        await session.close()


def _enforce_automation_policy(
    store: StateStore,
    *,
    site_scope: str,
    category: str,
    action: str,
) -> AutomationPolicyRecord:
    record = _find_automation_policy(store, site_scope=site_scope, category=category)
    if record is None:
        _raise_phase1_pause(
            question="I need an automation policy before I can continue this account or booking flow.",
            details=(
                f"Site: {site_scope or '(unknown)'}\n"
                f"Category: {category}\n"
                f"Action requested: {action}\n"
                "No matching automation policy is configured yet."
            ),
            summary="waiting for an automation policy before continuing",
            current_step="waiting_for_user_input",
            resume_instructions="Once a matching policy exists, continue the same login or booking flow from the last stable page.",
        )
    allowed = False
    if action == "zero_dollar_booking":
        allowed = record.allow_zero_dollar_booking
    elif action == "login_reuse":
        allowed = record.allow_login_reuse
    elif action == "account_creation":
        allowed = record.allow_account_creation
    else:
        allowed = False
    if allowed:
        return record
    _raise_phase1_pause(
        question="I stopped because this site is not approved for that automated action yet.",
        details=(
            f"Site: {site_scope or '(unknown)'}\n"
            f"Category: {category}\n"
            f"Action requested: {action}\n"
            f"Policy: {record.label}\n"
            "The configured automation policy does not allow this step."
        ),
        summary="blocked by the current automation policy",
        current_step="waiting_for_user_input",
        resume_instructions="Wait for the operator to update the automation policy or take over the task manually.",
    )
    return record


def _handle_phase1_blocking_error(exc: Exception, *, action: str) -> None:
    message = str(exc or "").strip()
    if "NON_ZERO_CHECKOUT_BLOCKED" in message:
        _raise_non_zero_checkout_pause(details=f"Action: {action}\nReason: {message}")
    lowered = message.lower()
    if "payment method on file" in lowered or (
        "payment method" in lowered and any(token in lowered for token in ("required", "missing", "needed", "before booking"))
    ):
        _raise_card_on_file_pause(details=f"Action: {action}\nReason: {message}")


def _should_expose_browser_tools(query: str, routing_profile_name: str) -> bool:
    lowered = query.lower()
    if routing_profile_name == "booking_commerce":
        browser_needed_tokens = (
            "cancel",
            "reschedule",
            "login",
            "sign in",
            "account",
            "verify",
            "verification",
            "otp",
            "code",
        )
        return any(token in lowered for token in browser_needed_tokens)
    if routing_profile_name == "itinerary_maps":
        travel_tokens = (
            "flight",
            "flights",
            "airfare",
            "hotel",
            "hotels",
            "rental car",
            "rental cars",
            "car rental",
            "car rentals",
            "google flights",
            "google travel",
            "skiplagged",
        )
        if any(token in lowered for token in travel_tokens):
            return False
    return True


def _should_expose_travel_browser_fallback(query: str, routing_profile_name: str) -> bool:
    lowered = query.lower()
    if routing_profile_name not in {"booking_commerce", "itinerary_maps"}:
        return False
    travel_tokens = (
        "flight",
        "flights",
        "airfare",
        "hotel",
        "hotels",
        "rental car",
        "rental cars",
        "car rental",
        "car rentals",
        "google flights",
        "google travel",
        "skiplagged",
    )
    return any(token in lowered for token in travel_tokens)


def _direct_tool_mode_summary(query: str, routing_profile_name: str) -> str:
    lowered = query.lower()
    if routing_profile_name == "booking_commerce":
        if any(token in lowered for token in ("restaurant", "reservation", "resy", "opentable", "table")):
            if _is_restaurant_discovery_request(query, routing_profile_name):
                return (
                    "This is a restaurant discovery task, not an approved booking task yet. "
                    "Use restaurant_search first to build a shortlist of candidate venues, then use restaurant_availability or restaurant_find_availability only after the user has picked a specific restaurant or clearly asked for a single best option. "
                    "If preflight already listed concrete candidate venue ids, use restaurant_availability with those venue ids instead of rerunning a name-based search. "
                    "Do not call restaurant_book_or_handoff yet. "
                    "Do not stop on one venue's cancellation policy before you have named the venue and given the user a clear choice."
                )
            return (
                "This is a structured reservation task for restaurants. Use restaurant_search first to find the venue, "
                "prefer restaurant_find_availability for the full search-plus-slots flow, and only use restaurant_book_or_handoff for an approved booking or manual handoff step. "
                "For read-only availability checks, stay on the structured restaurant tools. "
                "If the user asks to cancel or modify an existing reservation, first inspect saved bookings and current reservations, then use the saved browser session or a fresh login flow only if the structured tools cannot finish the account step cleanly. "
                "If Resy cannot verify live availability, try OpenTable next before falling back to the hardened browser path. "
                "Use the hardened browser fallback only after the structured provider sequence is exhausted, to verify the live slot and exact cancellation policy directly before booking. "
                "Only pause with provider_unavailable after the browser fallback also fails or the user needs to choose a different venue, time, or booking source."
            )
        if any(token in lowered for token in ("flight", "flights", "hotel", "hotels", "rental car", "rental cars", "car rental", "car rentals")):
            return (
                "This is a structured travel booking task. Complete it with direct travel tools first. "
                "Do not open aggregator or airline websites unless the direct travel tools are unavailable. "
                "Stay strictly grounded in the tool output: do not invent itineraries, do not combine one-way legs unless the tool output explicitly supports that pairing, "
                "and do not narrate intermediate reasoning like 'let me calculate' or 'here's where things stand'. "
                "If nothing matches every constraint, say that plainly and then list the closest verified options with exact tradeoffs."
            )
    if routing_profile_name == "itinerary_maps":
        return (
            "This is a structured travel/maps task. Prefer direct travel and maps tools, plus deterministic research/fetch. "
            "Do not browse map or travel UIs unless a direct tool cannot support the needed step. "
            "For travel results, stay strictly grounded in the tool output: no invented route pairings, no speculative pricing math, and no internal reasoning chatter."
        )
    return ""


def _is_structured_restaurant_task(query: str, routing_profile_name: str) -> bool:
    if routing_profile_name != "booking_commerce":
        return False
    lowered = query.lower()
    return any(token in lowered for token in ("restaurant", "reservation", "reservations", "resy", "opentable", "table"))


_NUMBER_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

_NUMBER_WORD_PATTERN = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_WEEKDAY_ALIASES: dict[str, str] = {
    "mon": "monday",
    "monday": "monday",
    "tue": "tuesday",
    "tues": "tuesday",
    "tuesday": "tuesday",
    "wed": "wednesday",
    "wednesday": "wednesday",
    "thu": "thursday",
    "thur": "thursday",
    "thurs": "thursday",
    "thursday": "thursday",
    "fri": "friday",
    "friday": "friday",
    "sat": "saturday",
    "saturday": "saturday",
    "sun": "sunday",
    "sunday": "sunday",
}
_WEEKDAY_PATTERN = r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
_MONTH_PATTERN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_RELATIVE_DATE_PATTERN = rf"(?:today|tomorrow|tonight|this (?:morning|afternoon|evening|{_WEEKDAY_PATTERN})|next {_WEEKDAY_PATTERN}|{_WEEKDAY_PATTERN})"
_MONTH_DAY_PATTERN = rf"(?:{_MONTH_PATTERN})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*\d{{4}})?"
_WEEKDAY_MONTH_DAY_PATTERN = rf"(?:{_WEEKDAY_PATTERN}),?\s+(?:{_MONTH_DAY_PATTERN})"
_DATE_SLASH_PATTERN = r"\d{1,2}/\d{1,2}(?:/\d{2,4})?"
_TIME_VALUE_PATTERN = r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)"
_FLEXIBLE_TIME_PATTERN = (
    r"(?:any available time|any time|anytime|first available|earliest available|next available|"
    r"closest available|flexible on time|whenever available)"
)


def _prioritized_query_segments(query: str) -> tuple[str, ...]:
    normalized = str(query or "").strip()
    if not normalized:
        return tuple()
    lowered = normalized.lower()
    segments: list[str] = []
    for marker in ("new user input:", "new user direction:"):
        index = lowered.rfind(marker)
        if index >= 0:
            segment = normalized[index + len(marker) :].strip()
            if segment:
                segments.append(segment)
    segments.append(normalized)
    deduped: list[str] = []
    seen: set[str] = set()
    for segment in segments:
        compact = segment.strip()
        if not compact or compact in seen:
            continue
        seen.add(compact)
        deduped.append(compact)
    return tuple(deduped)


def _search_query_segments(query: str, patterns: tuple[str, ...], *, flags: int = re.IGNORECASE) -> str:
    for segment in _prioritized_query_segments(query):
        for pattern in patterns:
            match = re.search(pattern, segment, flags=flags)
            if match:
                return " ".join(match.group("value").strip(" ,.\n\t").split())
    return ""


def _normalize_restaurant_time_token(raw_value: str) -> str:
    cleaned = " ".join(raw_value.strip().split())
    lowered = cleaned.lower().replace(".", "")
    if not lowered:
        return ""
    if re.fullmatch(_FLEXIBLE_TIME_PATTERN, lowered):
        return "ANY AVAILABLE"
    if lowered in {"noon", "midnight"}:
        return lowered.upper()
    match = re.fullmatch(r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<suffix>[ap]m)?", lowered)
    if not match:
        return cleaned.upper()
    hour = int(match.group("hour"))
    minute = match.group("minute")
    suffix = (match.group("suffix") or "").upper()
    if suffix:
        return f"{hour}:{minute} {suffix}" if minute is not None else f"{hour} {suffix}"
    return f"{hour}:{minute}" if minute is not None else str(hour)


def _normalize_restaurant_date_phrase(raw_value: str) -> str:
    cleaned = " ".join(raw_value.strip().split())
    cleaned = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bsept\b", "sep", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" ,.")


def _parse_month_day_date(value: str, *, current: datetime) -> Optional[datetime]:
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d", "%b %d"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt in {"%B %d", "%b %d"}:
                parsed = parsed.replace(year=current.year)
                if parsed.date() < current.date():
                    parsed = parsed.replace(year=current.year + 1)
            return parsed
        except ValueError:
            continue
    return None


def _render_requested_time_phrase(time_value: str) -> str:
    normalized = str(time_value or "").strip().upper()
    if not normalized:
        return ""
    if normalized == "ANY AVAILABLE":
        return "for any available time"
    return f"around {time_value}"


def _extract_party_size_value(query: str) -> Optional[int]:
    raw_value = _search_query_segments(
        query,
        (
            rf"\b(?:party size|party|size|guests|people|persons)\s*:\s*(?P<value>\d{{1,2}}|{_NUMBER_WORD_PATTERN})\b",
            rf"\b(?:for|table for|party of|for a party of)\s+(?P<value>\d{{1,2}}|{_NUMBER_WORD_PATTERN})(?=\s*(?:people|persons|person|guests|diners|of us)\b|\s+(?:at|on|in|near|today|tomorrow|tonight)\b|[,.]|$)",
            rf"\b(?P<value>\d{{1,2}}|{_NUMBER_WORD_PATTERN})\s+(?:people|persons|person|guests|diners)\b",
        ),
    )
    if raw_value.isdigit():
        return max(1, int(raw_value))
    if raw_value:
        return _NUMBER_WORDS.get(raw_value.lower())
    return None


def _is_restaurant_discovery_request(query: str, routing_profile_name: str) -> bool:
    if routing_profile_name != "booking_commerce":
        return False
    lowered = query.lower()
    has_restaurant_context = any(
        token in lowered
        for token in (
            "restaurant",
            "restaurants",
            "resy",
            "opentable",
            "table",
            "dinner",
            "lunch",
            "brunch",
            "breakfast",
        )
    )
    if not has_restaurant_context:
        return False
    has_booking_intent = any(
        token in lowered
        for token in (
            "book ",
            "book me",
            "reserve",
            "reservation for",
            "make a reservation",
            "get me a table",
            "secure a table",
        )
    )
    has_discovery_intent = any(
        token in lowered
        for token in (
            "find ",
            "show ",
            "list ",
            "suggest ",
            "recommend ",
            "options",
            "best ",
            "good ",
            "available ",
        )
    )
    generic_plural_discovery = bool(
        re.search(r"\b(find|show|list|suggest|recommend)\b", lowered)
        and re.search(r"\brestaurants\b", lowered)
    )
    return generic_plural_discovery or (has_discovery_intent and not has_booking_intent)


def _phase1_booking_runtime_guidance(query: str, routing_profile_name: str) -> str:
    lowered = query.lower()
    if routing_profile_name != "booking_commerce" and not any(
        token in lowered for token in ("login", "sign in", "account", "book", "booking", "reservation", "cancel")
    ):
        return ""
    restaurant_booking_start = ""
    if routing_profile_name == "booking_commerce":
        has_restaurant_context = any(token in lowered for token in ("restaurant", "resy", "opentable", "table", "dinner", "lunch", "brunch", "breakfast"))
        has_booking_intent = any(
            token in lowered
            for token in (
                "book ",
                "book me",
                "reserve",
                "reservation for",
                "make a reservation",
                "get me a table",
                "secure a table",
            )
        )
        if has_restaurant_context and has_booking_intent and not _restaurant_booking_missing_details(query, routing_profile_name):
            restaurant_booking_start = (
                "- For a complete restaurant booking request, start immediately with one restaurant_find_availability call using the user's venue, city, date, party size, and provider instead of spending turns narrating venue matching.\n"
                "- If the first structured availability attempt is on Resy and it fails, try OpenTable next before falling back to browser verification.\n"
                "- Use the hardened browser fallback only after the structured provider sequence is exhausted, to verify the slot and cancellation policy directly on the booking page.\n"
            )
    return (
        "Phase 1 booking/account rules:\n"
        "- Friday may only autonomously complete $0 bookings in this flow. If a payment form, deposit, hold, fee, or non-zero total appears, stop immediately.\n"
        f"{restaurant_booking_start}"
        "- If the user is searching for restaurant options rather than explicitly booking one, first surface a shortlist with venue names, neighborhoods, and live slots when possible. Do not auto-pick one venue and then ask for confirmation about its cancellation policy before the user chooses.\n"
        "- Before or during login/account creation, call list_saved_identities to see if an existing identity already fits the site.\n"
        "- If an email verification step appears, call wait_for_email_verification instead of polling the inbox manually. The workflow will resume automatically when Gmail push delivers the code.\n"
        "- After a successful login or account creation, call browser_save_session so the site session can be reused later.\n"
        "- If you return to a known site, prefer browser_restore_session before signing in again.\n"
        "- Before any terminal booking submission, call browser_assert_zero_dollar_checkout unless the browser tool already blocked the action.\n"
        "- Once a $0 booking is confirmed, call record_zero_dollar_booking with the site, venue, session, and any confirmation reference.\n"
        "- If the user asks to cancel or replace an existing booking, call list_saved_bookings first so the follow-up is tied to a real saved reservation instead of guessing.\n"
        "- If a $0 booking requires a card on file, pause and ask for explicit confirmation before using or adding any card.\n"
        "- If you need the user to choose between booking options such as seating sections, time slots, or date variants, call pause_for_input instead of returning the question as a final answer.\n"
        "- If SMS OTP, CAPTCHA, device verification, or any non-zero checkout appears, pause instead of improvising."
    )


def _booking_choice_pause_payload(output_text: str, routing_profile_name: str) -> Optional[dict[str, str]]:
    if routing_profile_name != "booking_commerce":
        return None
    normalized = str(output_text or "").strip()
    if not normalized:
        return None
    lowered = normalized.lower()
    triggers = (
        "which would you prefer",
        "which do you prefer",
        "which would you like",
        "which option would you like",
        "which seating preference",
        "let me confirm with you first",
        "reply with your preferred",
    )
    if not any(trigger in lowered for trigger in triggers):
        return None
    option_lines = [line.strip() for line in normalized.splitlines() if re.match(r"^\s*[-*•]", line)]
    question = ""
    for line in reversed([line.strip() for line in normalized.splitlines() if line.strip()]):
        if line.endswith("?"):
            question = line
            break
    if not question:
        question = "I need your choice between the available booking options before I can continue."
    details_parts = option_lines[:8]
    if not details_parts:
        details_parts.append(normalized[:800])
    return {
        "question": question[:500],
        "details": "\n".join(details_parts)[:1500],
        "summary": "waiting for your choice between the available booking options",
        "current_step": "waiting_for_user_input",
        "resume_instructions": "Use the user's selected booking option to continue the same reservation flow without asking again for the same choice.",
    }


def _is_booking_cancellation_followup(query: str, routing_profile_name: str) -> bool:
    if routing_profile_name != "booking_commerce":
        return False
    lowered = query.lower()
    if not re.search(r"\bcancel(?:led|ing|ation)?\b", lowered):
        return False
    policy_contexts = (
        "free cancel",
        "free-cancel",
        "free cancellation",
        "cancellation policy",
        "cancel policy",
        "best free-cancel option",
    )
    if any(marker in lowered for marker in policy_contexts):
        return False
    return any(
        token in lowered for token in ("booking", "reservation", "restaurant", "resy", "opentable", "seating", "table")
    )


def _is_booking_replacement_request(query: str) -> bool:
    lowered = query.lower()
    replacement_markers = (
        "instead make a booking",
        "instead book",
        "find me another",
        "find another",
        "replacement booking",
        "replace it with",
        "instead make me",
    )
    return any(marker in lowered for marker in replacement_markers)


def _latest_relevant_booking_record(store: StateStore, query: str) -> Optional[BookingRecord]:
    lowered = query.lower()
    records = store.list_booking_records(limit=25)
    active_records = [
        record for record in records if str(record.status or "").lower() not in {"cancelled", "canceled", "cancel_completed"}
    ]
    for record in active_records:
        venue = (record.venue_name or "").lower()
        site = (record.site_key or "").lower()
        external_reference = (record.external_reference or "").lower()
        if any(token and token in lowered for token in (venue, site, external_reference)):
            return record
    return active_records[0] if active_records else None


def _has_active_browser_session_for_site(store: StateStore, site_scope: str) -> bool:
    normalized_site = (site_scope or "").strip().lower()
    if not normalized_site:
        return False
    for record in store.list_browser_sessions(limit=100):
        if record.status != "active":
            continue
        if (record.site_scope or "").strip().lower() == normalized_site:
            return True
    return False


def _can_structurally_cancel_booking(record: BookingRecord) -> bool:
    normalized_site = (record.site_key or "").strip().lower()
    if normalized_site in {"resy", "resy.com"} and record.external_reference.strip():
        return True
    return False


def _canonical_booking_site_key(site_key: str) -> str:
    normalized = (site_key or "").strip().lower()
    if normalized in {"resy", "resy.com"}:
        return "resy.com"
    if normalized in {"opentable", "opentable.com"}:
        return "opentable.com"
    return site_key.strip() or "generic-booking"


def _maybe_raise_booking_cancellation_pause(store: StateStore, query: str, routing_profile_name: str) -> None:
    if not _is_booking_cancellation_followup(query, routing_profile_name):
        return
    record = _latest_relevant_booking_record(store, query)
    if record is None:
        if _is_booking_replacement_request(query):
            return
        _raise_phase1_pause(
            question="I could not find a saved booking to cancel yet.",
            details=(
                "I do not see a saved reservation record for this request.\n"
                "Please tell me the restaurant name or reservation reference, or cancel it manually and then ask me to book the replacement."
            ),
            summary="waiting for a concrete reservation to cancel",
            current_step="cancel_pending",
            resume_instructions="Use the provided restaurant name or reservation reference to continue the cancellation or replacement flow.",
        )
    if _can_structurally_cancel_booking(record):
        return
    if not _has_active_browser_session_for_site(store, record.site_key):
        _raise_phase1_pause(
            question="I found the booking, but I do not have a reusable login session to cancel it autonomously yet.",
            details=(
                f"Venue: {record.venue_name or '(unknown)'}\n"
                f"Site: {record.site_key or '(unknown)'}\n"
                f"Booked time: {record.booking_time or '(unknown)'}\n"
                f"Reference: {record.external_reference or '(none)'}\n"
                "I can still search for a replacement, but I cannot safely auto-cancel this reservation until a reusable site session exists."
            ),
            summary="waiting for a reusable login session before cancelling the existing booking",
            current_step="cancel_pending",
            resume_instructions=(
                "Once a reusable login session exists for this site, continue the cancellation first and only then place the replacement booking."
            ),
        )


def _should_skip_booking_cancellation_precheck(
    *,
    query: str,
    effective_query: str,
    routing_profile_name: str,
    resume_checkpoint: Optional[CheckpointPayload],
) -> bool:
    if resume_checkpoint is None:
        return False
    if not _is_booking_cancellation_followup(query, routing_profile_name):
        return False
    current_step = (resume_checkpoint.current_step or "").strip().lower()
    metadata = resume_checkpoint.metadata or {}
    question = str(metadata.get("input_question", "")).strip().lower()
    details = str(metadata.get("input_details", "")).strip().lower()
    if current_step == "cancel_pending":
        if "could not find a saved booking" in question:
            lowered_effective = effective_query.lower()
            marker = "new user input:"
            reply_text = ""
            if marker in lowered_effective:
                reply_text = lowered_effective.split(marker, 1)[1].strip()
            replacement_markers = (
                "nyc",
                "new york",
                "greenville",
                "bar italia",
                "nonna dora",
                "da claudio",
                "foodance",
                "cucina",
                "ramerino",
                "duomo51",
            )
            if reply_text and any(marker in reply_text for marker in replacement_markers):
                return True
        return False
    if current_step.startswith("awaiting_"):
        return True
    combined = "\n".join(part for part in (question, details) if part)
    skip_markers = (
        "which city are you looking for",
        "which restaurant",
        "let's move forward with the italian booking",
        "italian restaurants with 9 pm availability",
    )
    return any(marker in combined for marker in skip_markers)


def _format_optional_currency(amount: Optional[float]) -> str:
    if amount is None:
        return ""
    return f"${amount:.2f}"


async def _lookup_resy_slot_policy(
    settings: Settings,
    *,
    venue_id: str,
    date: str,
    party_size: int,
    slot_token: str = "",
    time: str = "",
) -> Optional[RestaurantSlotPolicy]:
    try:
        policy_map = await fetch_resy_slot_policies(
            settings,
            venue_id=venue_id,
            date=date,
            party_size=max(1, party_size),
        )
    except Exception as exc:
        logger.warning(
            "resy_slot_policy_lookup_failed venue_id=%s date=%s party_size=%s error=%s",
            venue_id,
            date,
            party_size,
            exc,
        )
        return None
    normalized_token = slot_token.strip()
    if normalized_token and normalized_token in policy_map:
        return policy_map[normalized_token]
    normalized_time = time.strip()
    if normalized_time:
        for policy in policy_map.values():
            if policy.time == normalized_time:
                return policy
    return None


def _render_resy_slot_policy(policy: Optional[RestaurantSlotPolicy]) -> str:
    if policy is None:
        return "Cancellation policy: unavailable from the current Resy slot data."
    return f"Cancellation policy: {policy.policy_text or 'unavailable from the current Resy slot data.'}"


def _render_resy_policy_slot_lines(
    policy_map: dict[str, RestaurantSlotPolicy],
    *,
    date: str,
    party_size: int,
    venue_name: str,
    venue_city: str,
    venue_url: str,
) -> str:
    lines = [
        f"Matched venue: {venue_name}"
        + (f" ({venue_city})" if venue_city else "")
        + " on Resy",
    ]
    if venue_url:
        lines.append(f"Booking page: {venue_url}")
    ordered = sorted(
        (policy for policy in policy_map.values() if policy.time),
        key=lambda policy: (policy.time, policy.slot_type.lower(), policy.slot_token),
    )
    if not ordered:
        lines.append(f"No live slots were returned for {date} for {max(1, party_size)} people.")
        return "\n".join(lines)
    lines.append(f"Live availability for {date} for {max(1, party_size)} people:")
    for policy in ordered[:12]:
        line = f"- {policy.time}" + (f" ({policy.slot_type})" if policy.slot_type else "")
        if policy.policy_text:
            line += f" — {policy.policy_text}"
        lines.append(line)
    return "\n".join(lines)


def _maybe_raise_nonfree_resy_confirmation(
    policy: Optional[RestaurantSlotPolicy],
    *,
    venue_id: str,
    venue_name: str = "",
    venue_city: str = "",
    date: str,
    time: str,
    party_size: int,
) -> None:
    if policy is not None and policy.free_cancellation:
        return
    venue_label = venue_name.strip()
    if venue_city.strip():
        venue_label = f"{venue_label} ({venue_city.strip()})" if venue_label else venue_city.strip()
    details_lines = [
        f"Provider: resy",
        f"Venue: {venue_label}" if venue_label else "",
        f"Venue id: {venue_id}",
        f"Date: {date}",
        f"Time: {time}",
        f"Party size: {max(1, party_size)}",
        _render_resy_slot_policy(policy),
    ]
    if policy is not None and policy.deposit_fee is not None and policy.deposit_fee > 0:
        details_lines.append(f"Deposit fee: {_format_optional_currency(policy.deposit_fee)}")
    if policy is not None and policy.service_charge is not None and policy.service_charge > 0:
        details_lines.append(f"Service charge: {_format_optional_currency(policy.service_charge)}")
    question = "I need your explicit confirmation before booking this Resy slot."
    if policy is None:
        question = (
            f"I could not verify whether {venue_label or 'this Resy slot'} has free cancellation, so I need your explicit confirmation before booking it."
        )
    elif policy.policy_text:
        question = (
            f"{venue_label or 'This Resy slot'} is not free to cancel, so I need your explicit confirmation before booking it. "
            + policy.policy_text
        ).strip()
    _raise_phase1_pause(
        question=question,
        details="\n".join(line for line in details_lines if line).strip(),
        summary="waiting for your confirmation because this booking is not free to cancel",
        current_step="waiting_for_confirmation",
        resume_instructions=(
            "Only continue if the user explicitly confirms that Friday should proceed despite the cancellation policy. "
            "Otherwise, look for a free-cancellation alternative."
        ),
    )


def _restaurant_booking_missing_details(query: str, routing_profile_name: str) -> list[str]:
    if routing_profile_name != "booking_commerce":
        return []
    if _is_restaurant_discovery_request(query, routing_profile_name):
        return []
    lowered = query.lower()
    booking_intent = any(
        token in lowered
        for token in (
            "book ",
            "book me",
            "reserve",
            "reservation for",
            "make a reservation",
            "get me a table",
            "secure a table",
        )
    )
    restaurant_context = any(token in lowered for token in ("restaurant", "resy", "opentable", "table", "dinner", "lunch", "brunch", "breakfast"))
    if not booking_intent or not restaurant_context:
        return []

    missing: list[str] = []
    if _extract_party_size_value(query) is None:
        missing.append("party size")

    if not _extract_restaurant_date_value(query):
        missing.append("date")

    has_time = bool(_extract_restaurant_time_value(query))
    if not has_time:
        missing.append("time")
    return missing


def _extract_restaurant_time_value(query: str) -> str:
    raw_value = _search_query_segments(
        query,
        (
            rf"\btime\s*:\s*(?P<value>{_FLEXIBLE_TIME_PATTERN})\b",
            rf"\btime\s*:\s*(?P<value>{_TIME_VALUE_PATTERN})\b",
            r"\btime\s*:\s*(?P<value>\d{1,2}:\d{2})\b",
            r"\btime\s*:\s*(?P<value>noon|midnight)\b",
            rf"\b(?:at|around)\s+(?P<value>{_TIME_VALUE_PATTERN})\b",
            r"\b(?:at|around)\s+(?P<value>\d{1,2}:\d{2})\b",
            rf"\b(?P<value>{_TIME_VALUE_PATTERN})\b",
            r"\b(?P<value>\d{1,2}:\d{2})\b",
            r"\b(?P<value>noon|midnight)\b",
            rf"\b(?P<value>{_FLEXIBLE_TIME_PATTERN})\b",
        ),
    )
    return _normalize_restaurant_time_token(raw_value)


def _extract_restaurant_date_value(query: str) -> str:
    raw_value = _search_query_segments(
        query,
        (
            rf"\bdate\s*:\s*(?P<value>{_WEEKDAY_MONTH_DAY_PATTERN})\b",
            rf"\bdate\s*:\s*(?P<value>{_MONTH_DAY_PATTERN})\b",
            r"\bdate\s*:\s*(?P<value>\d{4}-\d{2}-\d{2})\b",
            rf"\bdate\s*:\s*(?P<value>{_DATE_SLASH_PATTERN})\b",
            rf"\bdate\s*:\s*(?P<value>{_RELATIVE_DATE_PATTERN})\b",
            rf"\b(?P<value>{_WEEKDAY_MONTH_DAY_PATTERN})\b",
            rf"\b(?P<value>{_MONTH_DAY_PATTERN})\b",
            r"\b(?P<value>\d{4}-\d{2}-\d{2})\b",
            rf"\b(?P<value>{_DATE_SLASH_PATTERN})\b",
            rf"\b(?P<value>{_RELATIVE_DATE_PATTERN})\b",
        ),
    )
    return _normalize_restaurant_date_phrase(raw_value)


def _extract_restaurant_venue_query(query: str) -> str:
    patterns = (
        rf"\bfor\s+(?:\d+|{_NUMBER_WORD_PATTERN})\s+(?:people|persons|person|guests)\s+at\s+(?P<value>[A-Za-z][A-Za-z0-9 .'\-&]+?)(?=\s+in\s+[A-Za-z0-9 .'-]+?(?:\s+on\s+(?:resy|opentable)\b|\s+for\s+(?:\d+|{_NUMBER_WORD_PATTERN})\b|\s+(?:\d+|{_NUMBER_WORD_PATTERN})\s+(?:people|persons|person|guests)\b)|\s+on\s+(?:resy|opentable)\b|\s+for\s+(?:\d+|{_NUMBER_WORD_PATTERN})\b|\s+(?:\d+|{_NUMBER_WORD_PATTERN})\s+(?:people|persons|person|guests)\b|\s+on\s+(?:{_RELATIVE_DATE_PATTERN}|{_MONTH_PATTERN}|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}})\b|\s+at\s+\d|\s*,?\s*but\b|\s*$)",
        rf"\bat\s+(?P<value>[A-Za-z][A-Za-z0-9 .'\-&]+?)(?=\s+in\s+[A-Za-z0-9 .'-]+?(?:\s+on\s+(?:resy|opentable)\b|\s+for\s+(?:\d+|{_NUMBER_WORD_PATTERN})\b|\s+(?:\d+|{_NUMBER_WORD_PATTERN})\s+(?:people|persons|person|guests)\b)|\s+on\s+(?:resy|opentable)\b|\s+for\s+(?:\d+|{_NUMBER_WORD_PATTERN})\b|\s+(?:\d+|{_NUMBER_WORD_PATTERN})\s+(?:people|persons|person|guests)\b|\s+on\s+(?:{_RELATIVE_DATE_PATTERN}|{_MONTH_PATTERN}|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}})\b|\s+at\s+\d|\s*,?\s*but\b|\s*$)",
        rf"\b(?:book|book me|reserve|secure a table at|make a reservation at|get me a table at)\s+(?P<value>.+?)(?=\s+in\s+[A-Za-z0-9 .'-]+?(?:\s+on\s+(?:resy|opentable)\b|\s+for\s+(?:\d+|{_NUMBER_WORD_PATTERN})\b|\s+(?:\d+|{_NUMBER_WORD_PATTERN})\s+(?:people|persons|person|guests)\b)|\s+on\s+(?:resy|opentable)\b|\s+for\s+(?:\d+|{_NUMBER_WORD_PATTERN})\b|\s+(?:\d+|{_NUMBER_WORD_PATTERN})\s+(?:people|persons|person|guests)\b|\s+on\s+(?:{_RELATIVE_DATE_PATTERN}|{_MONTH_PATTERN}|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}})\b|\s+at\s+\d|\s*$)",
    )
    return _search_query_segments(query, patterns)


def _normalize_restaurant_booking_date(raw_date: str, *, settings: Settings) -> str:
    value = _normalize_restaurant_date_phrase(raw_date)
    if not value:
        return ""
    lowered = value.lower()
    timezone_name = settings.restaurant_cli_timezone or "America/New_York"
    try:
        current = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        current = datetime.now(timezone.utc)
    if lowered in {"today", "tonight"}:
        return current.date().isoformat()
    if lowered in {"this morning", "this afternoon", "this evening"}:
        return current.date().isoformat()
    if lowered == "tomorrow":
        return (current.date() + timedelta(days=1)).isoformat()
    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    if lowered.startswith("next "):
        weekday_name = _WEEKDAY_ALIASES.get(lowered.split(" ", 1)[1].strip(), "")
        if weekday_name in weekdays:
            delta = (weekdays[weekday_name] - current.weekday()) % 7
            delta = 7 if delta == 0 else delta
            return (current.date() + timedelta(days=delta)).isoformat()
    if lowered.startswith("this "):
        weekday_name = _WEEKDAY_ALIASES.get(lowered.split(" ", 1)[1].strip(), "")
        if weekday_name in weekdays:
            delta = (weekdays[weekday_name] - current.weekday()) % 7
            return (current.date() + timedelta(days=delta)).isoformat()
    weekday_name = _WEEKDAY_ALIASES.get(lowered, "")
    if weekday_name in weekdays:
        delta = (weekdays[weekday_name] - current.weekday()) % 7
        return (current.date() + timedelta(days=delta)).isoformat()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m/%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt == "%m/%d":
                parsed = parsed.replace(year=current.year)
                if parsed.date() < current.date():
                    parsed = parsed.replace(year=current.year + 1)
            return parsed.date().isoformat()
        except ValueError:
            continue
    weekday_prefix_match = re.fullmatch(rf"(?P<weekday>{_WEEKDAY_PATTERN}),?\s+(?P<rest>{_MONTH_DAY_PATTERN})", value, flags=re.IGNORECASE)
    if weekday_prefix_match:
        parsed = _parse_month_day_date(weekday_prefix_match.group("rest"), current=current)
        if parsed is not None:
            return parsed.date().isoformat()
    parsed = _parse_month_day_date(value, current=current)
    if parsed is not None:
        return parsed.date().isoformat()
    for fmt in ("%Y-%m-%d",):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return value


def _extract_restaurant_booking_prefill(query: str, *, settings: Settings, routing_profile_name: str) -> Optional[RestaurantBookingPrefill]:
    if routing_profile_name != "booking_commerce":
        return None
    if _restaurant_booking_missing_details(query, routing_profile_name):
        return None
    lowered = query.lower()
    if not any(token in lowered for token in ("book ", "book me", "reserve", "reservation", "get me a table", "secure a table")):
        return None
    if not any(token in lowered for token in ("resy", "opentable", "restaurant", "table", "dinner", "lunch", "brunch", "breakfast")):
        return None

    provider = "resy" if "resy" in lowered else "opentable" if "opentable" in lowered else "resy"

    party_size = _extract_party_size_value(query)
    if party_size is None:
        return None

    time_value = _extract_restaurant_time_value(query)
    if not time_value:
        return None

    date_value = _extract_restaurant_date_value(query)
    if not date_value:
        return None

    city = _search_query_segments(
        query,
        (
            rf"\bin\s+(?P<value>[A-Za-z0-9 .'-]+?)(?=\s+on\s+(?:resy|opentable)\b|\s+for\s+(?:\d+|{_NUMBER_WORD_PATTERN})\b|\s+(?:\d+|{_NUMBER_WORD_PATTERN})\s+(?:people|persons|person|guests)\b|\s+on\s+(?:{_RELATIVE_DATE_PATTERN}|{_MONTH_PATTERN}|\d{{4}}-\d{{2}}-\d{{2}})\b|\s+at\s+\d|\s*,?\s*but\b|\s*$)",
        ),
    )

    venue_query = _extract_restaurant_venue_query(query)
    if not venue_query:
        return None

    normalized_date = _normalize_restaurant_booking_date(date_value, settings=settings)
    if not normalized_date:
        return None
    return RestaurantBookingPrefill(
        venue_query=venue_query,
        city=city.title() if city else "",
        provider=provider,
        date=normalized_date,
        time=time_value.upper(),
        party_size=party_size,
    )


def _extract_restaurant_discovery_prefill(
    query: str,
    *,
    settings: Settings,
    routing_profile_name: str,
) -> Optional[RestaurantDiscoveryPrefill]:
    if routing_profile_name != "booking_commerce":
        return None
    if not _is_restaurant_discovery_request(query, routing_profile_name):
        return None
    lowered = query.lower()
    provider = "resy" if "resy" in lowered else "opentable" if "opentable" in lowered else "resy"
    party_size = _extract_party_size_value(query)
    if party_size is None:
        return None
    time_value = _extract_restaurant_time_value(query)
    if not time_value:
        return None
    date_value = _extract_restaurant_date_value(query)
    if not date_value:
        return None
    city = _search_query_segments(
        query,
        (
            rf"\b(?:near|in)\s+(?P<value>[A-Za-z0-9 .'-]+?)(?=\s+on\s+(?:resy|opentable)\b|\s+for\s+(?:\d+|{_NUMBER_WORD_PATTERN})\b|\s+for\s+(?:{_RELATIVE_DATE_PATTERN}|{_MONTH_PATTERN}|\d{{4}}-\d{{2}}-\d{{2}})\b|\s+(?:\d+|{_NUMBER_WORD_PATTERN})\s+(?:people|persons|person|guests)\b|\s+on\s+(?:{_RELATIVE_DATE_PATTERN}|{_MONTH_PATTERN}|\d{{4}}-\d{{2}}-\d{{2}})\b|\s+at\s+\d|\s*$)",
        ),
    )
    search_query = query.strip()
    search_match = re.search(
        r"\b(?:find|show|list|suggest|recommend)(?:\s+me)?\s+(.+?)(?=\s+(?:near|in)\s+[A-Za-z0-9 .'-]+?(?:\s+on\s+(?:resy|opentable)\b|\s+for\s+\d+\b|\s+\d+\s+(?:people|persons|person|guests)\b)|\s+on\s+(?:resy|opentable)\b|\s+for\s+\d+\b|\s+\d+\s+(?:people|persons|person|guests)\b|\s+on\s+(?:today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|\d{4}-\d{2}-\d{2})\b|\s+at\s+\d|\s*$)",
        query,
        flags=re.IGNORECASE,
    )
    if search_match:
        search_query = search_match.group(1).strip(" ,.")
    if not search_query:
        return None
    normalized_date = _normalize_restaurant_booking_date(date_value, settings=settings)
    if not normalized_date:
        return None
    return RestaurantDiscoveryPrefill(
        search_query=search_query,
        city=city.title() if city else "",
        provider=provider,
        date=normalized_date,
        time=time_value.upper(),
        party_size=party_size,
    )


async def _restaurant_search_with_city_fallback(
    *,
    settings: Settings,
    workspace: Workspace,
    query: str,
    provider: str,
    city: str = "",
    limit: int = 8,
    timeout_seconds: int = 25,
) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    normalized_provider = normalize_restaurant_provider(provider)
    city_value = city.strip()
    attempted_cities = [city_value] if city_value else [""]
    if city_value:
        attempted_cities.append("")

    last_payload: dict[str, Any] = {"results": [], "failures": []}
    for attempt_city in attempted_cities:
        search_args = [
            "search",
            query,
            "--limit",
            str(max(1, min(limit, 25))),
            "--provider",
            normalized_provider,
            "--agent",
        ]
        if attempt_city:
            search_args.extend(["--city", attempt_city])
        payload = await run_restaurant_cli_json(
            settings,
            workspace,
            *search_args,
            timeout_seconds=timeout_seconds,
        )
        last_payload = payload if isinstance(payload, dict) else {"results": [], "failures": []}
        results = list(last_payload.get("results") or [])
        best_match = choose_best_restaurant_result(query, results)
        if best_match is not None:
            if city_value and not attempt_city:
                last_payload = dict(last_payload)
                last_payload["city_filter_relaxed"] = True
            return last_payload, best_match
    return last_payload, None


async def _restaurant_search_attempts(
    *,
    settings: Settings,
    workspace: Workspace,
    query: str,
    provider: str,
    city: str = "",
    limit: int = 8,
    timeout_seconds: int = 25,
) -> list[RestaurantSearchAttempt]:
    started_at = time.monotonic()
    logger.warning(
        "restaurant_search_attempts_start query=%s provider=%s city=%s limit=%s",
        query,
        provider,
        city,
        limit,
    )
    attempts: list[RestaurantSearchAttempt] = []
    for provider_name in restaurant_provider_sequence(provider):
        try:
            payload, best_match = await _restaurant_search_with_city_fallback(
                settings=settings,
                workspace=workspace,
                query=query,
                provider=provider_name,
                city=city,
                limit=limit,
                timeout_seconds=timeout_seconds,
            )
            attempts.append(
                RestaurantSearchAttempt(
                    provider=provider_name,
                    payload=payload if isinstance(payload, dict) else {"results": [], "failures": []},
                    best_match=best_match,
                )
            )
        except Exception as exc:
            attempts.append(
                RestaurantSearchAttempt(
                    provider=provider_name,
                    payload={"results": [], "failures": []},
                    best_match=None,
                    error=str(exc),
                )
            )
    logger.warning(
        "restaurant_search_attempts_complete query=%s provider=%s city=%s attempts=%s elapsed=%.2fs",
        query,
        provider,
        city,
        len(attempts),
        max(0.0, time.monotonic() - started_at),
    )
    return attempts


async def _resolve_restaurant_venue_reference(
    *,
    settings: Settings,
    workspace: Workspace,
    venue_reference: str,
    provider: str,
    timeout_seconds: int = 25,
) -> tuple[str, str, str, str]:
    raw_reference = str(venue_reference or "").strip()
    if not raw_reference:
        return "", "", "", ""
    if raw_reference.isdigit():
        return raw_reference, "", "", ""
    payload, best_match = await _restaurant_search_with_city_fallback(
        settings=settings,
        workspace=workspace,
        query=raw_reference,
        provider=provider,
        city="",
        limit=5,
        timeout_seconds=timeout_seconds,
    )
    _ = payload
    if not isinstance(best_match, dict):
        return raw_reference, "", "", ""
    resolved_id = str(best_match.get("id") or raw_reference).strip() or raw_reference
    resolved_name = str(best_match.get("name") or raw_reference).strip()
    resolved_city = str(best_match.get("city") or "").strip()
    resolved_url = str(best_match.get("url") or "").strip()
    return resolved_id, resolved_name, resolved_city, resolved_url


def _render_restaurant_search_shortlist(
    *,
    query: str,
    attempts: list[RestaurantSearchAttempt],
    city: str = "",
    limit: int = 8,
) -> str:
    lines = [f"Candidate venues for {query}:"]
    any_results = False
    for attempt in attempts:
        provider_label = _restaurant_provider_label(attempt.provider)
        if attempt.error:
            lines.append(f"- {provider_label}: search failed because {attempt.error}")
            continue
        payload = attempt.payload if isinstance(attempt.payload, dict) else {}
        results = list(payload.get("results") or [])
        if not results:
            relaxed = " after relaxing the city filter" if payload.get("city_filter_relaxed") else ""
            lines.append(f"- {provider_label}: no matches{relaxed}.")
            continue
        any_results = True
        lines.append(f"- {provider_label}:")
        for item in results[: max(1, min(limit, 5))]:
            name = str(item.get("name") or "").strip() or "Unknown venue"
            venue_city = str(item.get("city") or "").strip()
            venue_url = str(item.get("url") or "").strip()
            line = f"  - {name}"
            if venue_city:
                line += f" ({venue_city})"
            if venue_url:
                line += f" — {venue_url}"
            lines.append(line)
        if payload.get("city_filter_relaxed") and city.strip():
            lines.append(f"  - note: this provider only matched after relaxing the city filter from '{city.strip()}'.")
    if not any_results:
        lines.append("No structured providers returned usable matches.")
    lines.append("Pick one venue if you want a specific availability check, or ask for another area, cuisine, or provider.")
    return sanitize_tool_output("\n".join(lines))


def _restaurant_city_matches_request(requested_city: str, venue_city: str) -> bool:
    requested = re.sub(r"[^a-z0-9]+", " ", requested_city.lower()).strip()
    venue = re.sub(r"[^a-z0-9]+", " ", venue_city.lower()).strip()
    if not requested or not venue:
        return True
    requested_tokens = {token for token in requested.split() if token}
    venue_tokens = {token for token in venue.split() if token}
    if requested_tokens & venue_tokens:
        return True
    nyc_tokens = {"nyc", "new", "york", "manhattan", "midtown", "brooklyn", "queens", "bronx"}
    if requested_tokens & nyc_tokens and {"new", "york"} <= venue_tokens:
        return True
    return False


def _normalize_restaurant_lookup_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _matching_booking_preflight_venue(
    *,
    query: str,
    provider: str,
    city: str = "",
    prefill: Optional[RestaurantBookingPrefill],
    preflight: Optional[RestaurantBookingPreflightResult],
) -> Optional[RestaurantBookingPreflightVenue]:
    if prefill is None or preflight is None or preflight.matched_venue is None:
        return None
    matched_venue = preflight.matched_venue
    if normalize_restaurant_provider(provider) != normalize_restaurant_provider(matched_venue.provider):
        return None
    normalized_query = _normalize_restaurant_lookup_text(query)
    candidate_values = {
        _normalize_restaurant_lookup_text(prefill.venue_query),
        _normalize_restaurant_lookup_text(matched_venue.venue_name),
        _normalize_restaurant_lookup_text(matched_venue.venue_id),
    }
    candidate_values.discard("")
    if normalized_query and candidate_values and not any(
        normalized_query == candidate
        or normalized_query in candidate
        or candidate in normalized_query
        for candidate in candidate_values
    ):
        return None
    requested_city = city.strip()
    if requested_city and prefill.city.strip():
        if not (
            _restaurant_city_matches_request(requested_city, matched_venue.venue_city)
            or _restaurant_city_matches_request(requested_city, prefill.city)
        ):
            return None
    return matched_venue


async def _restaurant_find_availability_attempts(
    *,
    settings: Settings,
    workspace: Workspace,
    query: str,
    provider: str,
    city: str = "",
    limit: int = 8,
    timeout_seconds: int = 25,
    booking_prefill: Optional[RestaurantBookingPrefill] = None,
    booking_preflight: Optional[RestaurantBookingPreflightResult] = None,
) -> list[RestaurantSearchAttempt]:
    cached_venue = _matching_booking_preflight_venue(
        query=query,
        provider=provider,
        city=city,
        prefill=booking_prefill,
        preflight=booking_preflight,
    )
    if cached_venue is not None:
        logger.warning(
            "restaurant_find_availability_using_preflight venue_query=%s venue_id=%s provider=%s city=%s",
            query,
            cached_venue.venue_id,
            cached_venue.provider,
            cached_venue.venue_city,
        )
        return [
            RestaurantSearchAttempt(
                provider=cached_venue.provider,
                payload={
                    "results": [
                        {
                            "id": cached_venue.venue_id,
                            "name": cached_venue.venue_name,
                            "city": cached_venue.venue_city,
                            "url": cached_venue.venue_url,
                        }
                    ],
                    "failures": [],
                    "from_booking_preflight": True,
                },
                best_match={
                    "id": cached_venue.venue_id,
                    "name": cached_venue.venue_name,
                    "city": cached_venue.venue_city,
                    "url": cached_venue.venue_url,
                },
            )
        ]
    return await _restaurant_search_attempts(
        settings=settings,
        workspace=workspace,
        query=query,
        provider=provider,
        city=city,
        limit=limit,
        timeout_seconds=timeout_seconds,
    )


async def _restaurant_booking_preflight(
    *,
    settings: Settings,
    workspace: Workspace,
    prefill: RestaurantBookingPrefill,
) -> RestaurantBookingPreflightResult:
    attempts = await _restaurant_search_attempts(
        settings=settings,
        workspace=workspace,
        query=prefill.venue_query,
        provider=prefill.provider,
        city=prefill.city,
        limit=8,
        timeout_seconds=25,
    )
    browser_probe_candidate: Optional[RestaurantBookingPreflightVenue] = None
    failure_lines: list[str] = []
    requested_time = prefill.time.lower().replace(" ", "")
    for attempt in attempts:
        normalized_provider = normalize_restaurant_provider(attempt.provider)
        provider_label = _restaurant_provider_label(normalized_provider)
        if attempt.error:
            failure_lines.append(f"{provider_label} search failed because {attempt.error}.")
            continue
        search_payload = attempt.payload if isinstance(attempt.payload, dict) else {}
        best_match = attempt.best_match
        if best_match is None:
            failure_lines.append(f"{provider_label} had no structured match for {prefill.venue_query}.")
            continue
        venue_id = str(best_match.get("id") or "").strip()
        venue_name = str(best_match.get("name") or prefill.venue_query).strip() or prefill.venue_query
        venue_city = str(best_match.get("city") or "").strip()
        venue_url = str(best_match.get("url") or "").strip()
        matched_venue = RestaurantBookingPreflightVenue(
            venue_id=venue_id,
            venue_name=venue_name,
            venue_city=venue_city,
            venue_url=venue_url,
            provider=normalized_provider,
        )
        if not venue_id:
            failure_lines.append(f"{provider_label} matched {venue_name}, but returned no usable venue id.")
            continue
        if normalized_provider == "resy" and venue_url and browser_probe_candidate is None:
            browser_probe_candidate = matched_venue
        availability_args = [
            "availability",
            "--venue",
            venue_id,
            "--date",
            prefill.date,
            "--party",
            str(prefill.party_size),
            "--provider",
            normalized_provider,
            "--agent",
        ]
        try:
            slots_payload = await run_restaurant_cli_json(
                settings,
                workspace,
                *availability_args,
                timeout_seconds=35,
            )
        except Exception as exc:
            failure_lines.append(
                f"{provider_label} availability failed for {venue_name}"
                + (f" ({venue_city})" if venue_city else "")
                + f" because {exc}."
            )
            continue
        slots = slots_payload if isinstance(slots_payload, list) else []
        policy_map: dict[str, RestaurantSlotPolicy] = {}
        if normalized_provider == "resy" and slots:
            try:
                policy_map = await fetch_resy_slot_policies(
                    settings,
                    venue_id=venue_id,
                    date=prefill.date,
                    party_size=max(1, prefill.party_size),
                )
            except Exception:
                policy_map = {}
        exact_match = None
        for slot in slots:
            slot_time = str(slot.get("time") or "").strip()
            if slot_time.lower().replace(" ", "") == requested_time:
                exact_match = slot
                break
        lines = [
            "STRUCTURED_BOOKING_PREFLIGHT:",
            f"Matched venue: {venue_name}" + (f" ({venue_city})" if venue_city else ""),
            f"Venue id: {venue_id}",
            f"Provider: {normalized_provider}",
            f"Booking page: {venue_url}" if venue_url else "Booking page: unavailable",
            f"Requested date: {prefill.date}",
            f"Requested time: {prefill.time}",
            f"Party size: {prefill.party_size}",
        ]
        if search_payload.get("city_filter_relaxed"):
            lines.append(f"Venue search only matched after relaxing the city filter from '{prefill.city}'.")
        if exact_match is not None:
            lines.append(f"Exact requested time is present in the structured slot list: {str(exact_match.get('time') or '').strip()}.")
            slot_token = str(exact_match.get("token") or "").strip()
            policy = policy_map.get(slot_token)
            if policy is not None and policy.policy_text:
                lines.append(f"Cancellation policy: {policy.policy_text}")
        elif slots:
            lines.append("Exact requested time was not found in the structured slot list.")
            lines.append("Closest structured slots:")
            for slot in slots[:5]:
                slot_time = str(slot.get("time") or "").strip()
                line = f"- {slot_time or '(unknown time)'}"
                slot_token = str(slot.get("token") or "").strip()
                policy = policy_map.get(slot_token)
                if policy is not None and policy.policy_text:
                    line += f" — {policy.policy_text}"
                lines.append(line)
        else:
            lines.append("Structured availability returned no slots.")
        lines.append(
            "Use this preflight context immediately. If structured slot verification is incomplete, continue through the provider sequence before using the browser fallback."
        )
        if normalized_provider == "resy" and venue_url:
            probe = await _run_resy_browser_probe(
                settings=settings,
                workspace=workspace,
                venue_url=venue_url,
                venue_name=venue_name,
                venue_city=venue_city,
                date=prefill.date,
                time=prefill.time,
                party_size=prefill.party_size,
            )
            if probe is not None:
                lines.append("")
                lines.append(
                    _render_resy_browser_probe_summary(
                        probe,
                        venue_id=venue_id,
                        venue_name=venue_name,
                        venue_city=venue_city,
                        date=prefill.date,
                        time=prefill.time,
                        party_size=prefill.party_size,
                    )
                )
        return RestaurantBookingPreflightResult(
            summary=sanitize_tool_output("\n".join(lines)),
            matched_venue=matched_venue,
        )
    if browser_probe_candidate is not None:
        probe = await _run_resy_browser_probe(
            settings=settings,
            workspace=workspace,
            venue_url=browser_probe_candidate.venue_url,
            venue_name=browser_probe_candidate.venue_name,
            venue_city=browser_probe_candidate.venue_city,
            date=prefill.date,
            time=prefill.time,
            party_size=prefill.party_size,
        )
        if probe is not None:
            return RestaurantBookingPreflightResult(
                summary=_render_resy_browser_probe_summary(
                    probe,
                    venue_id=browser_probe_candidate.venue_id,
                    venue_name=browser_probe_candidate.venue_name,
                    venue_city=browser_probe_candidate.venue_city,
                    date=prefill.date,
                    time=prefill.time,
                    party_size=prefill.party_size,
                ),
                matched_venue=browser_probe_candidate,
            )
    if failure_lines:
        return RestaurantBookingPreflightResult(
            summary=sanitize_tool_output(
                _restaurant_provider_browser_fallback_message(
                    provider=restaurant_provider_sequence(prefill.provider)[0],
                    venue_name=prefill.venue_query,
                    date=prefill.date,
                    party_size=prefill.party_size,
                    details="\n".join(failure_lines[:6]),
                )
            ),
            matched_venue=browser_probe_candidate,
        )
    return RestaurantBookingPreflightResult(
        summary=sanitize_tool_output(
            f"RESTAURANT_PREFLIGHT_NO_MATCH: no structured venue match was found for {prefill.venue_query}."
        )
    )


async def _restaurant_discovery_preflight(
    *,
    settings: Settings,
    workspace: Workspace,
    prefill: RestaurantDiscoveryPrefill,
) -> RestaurantDiscoveryPreflightResult:
    try:
        payload, best_match = await _restaurant_search_with_city_fallback(
            settings=settings,
            workspace=workspace,
            query=prefill.search_query,
            provider=prefill.provider,
            city=prefill.city,
            limit=4,
            timeout_seconds=12,
        )
        attempts = [
            RestaurantSearchAttempt(
                provider=prefill.provider,
                payload=payload if isinstance(payload, dict) else {"results": [], "failures": []},
                best_match=best_match,
            )
        ]
    except Exception as exc:
        attempts = [
            RestaurantSearchAttempt(
                provider=prefill.provider,
                payload={"results": [], "failures": []},
                best_match=None,
                error=str(exc),
            )
        ]
    candidate_lines: list[str] = []
    candidates: list[RestaurantDiscoveryCandidate] = []
    for attempt in attempts:
        payload = attempt.payload if isinstance(attempt.payload, dict) else {}
        results = list(payload.get("results") or [])
        filtered_results = [
            item
            for item in results
            if _restaurant_city_matches_request(prefill.city, str(item.get("city") or ""))
        ] or results
        for item in filtered_results[:4]:
            venue_id = str(item.get("id") or "").strip()
            venue_name = str(item.get("name") or "").strip() or "Unknown venue"
            venue_city = str(item.get("city") or "").strip()
            venue_url = str(item.get("url") or "").strip()
            candidates.append(
                RestaurantDiscoveryCandidate(
                    venue_id=venue_id,
                    venue_name=venue_name,
                    venue_city=venue_city,
                    venue_url=venue_url,
                    provider=attempt.provider,
                )
            )
            candidate_lines.append(
                "- "
                + venue_name
                + (f" ({venue_city})" if venue_city else "")
                + f" | provider={attempt.provider} | venue_id={venue_id or '(missing)'}"
            )
        if candidate_lines:
            break
    rendered = _render_restaurant_search_shortlist(
        query=prefill.search_query,
        attempts=attempts,
        city=prefill.city,
        limit=4,
    )
    candidate_block = "Preflight candidates:\n" + ("\n".join(candidate_lines) if candidate_lines else "- none")
    return RestaurantDiscoveryPreflightResult(
        summary=(
        "STRUCTURED_DISCOVERY_PREFLIGHT:\n"
        f"Requested date: {prefill.date}\n"
        f"Requested time: {prefill.time}\n"
        f"Party size: {prefill.party_size}\n"
        f"Preferred provider: {prefill.provider}\n"
        + candidate_block
        + "\n"
        + rendered
        + "\nUse these preflight candidates first. For live slot checks, prefer restaurant_availability with the listed venue_id instead of restaurant_find_availability with a candidate name. Do not run another broad restaurant search unless every candidate here fails the live slot check."
        ),
        candidates=tuple(candidates[:4]),
    )


def _select_model(query: str, settings: Settings) -> str:
    lowered = query.lower()
    if any(token in lowered for token in ("think deeply", "reason", "plan carefully", "complex")):
        return settings.reasoner_model
    return settings.agent_model


def _text_only_agent_result(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=sanitize_tool_output(text))


def _render_restaurant_discovery_direct_response(
    *,
    prefill: RestaurantDiscoveryPrefill,
    preflight: RestaurantDiscoveryPreflightResult,
) -> str:
    provider_label = _restaurant_provider_label(prefill.provider)
    time_phrase = _render_requested_time_phrase(prefill.time)
    lines = [
        f"I found a few likely {provider_label} options for {max(1, prefill.party_size)} {time_phrase} on {prefill.date}.",
    ]
    if prefill.city:
        lines[0] = (
            f"I found a few likely {provider_label} options near {prefill.city} for {max(1, prefill.party_size)} {time_phrase} on {prefill.date}."
        )
    if not preflight.candidates:
        lines.append(f"I couldn't find a clean shortlist yet for {prefill.search_query}.")
        lines.append("Try another area, another cuisine, or a different provider.")
        return sanitize_tool_output("\n".join(lines))
    for candidate in preflight.candidates[:3]:
        line = f"- {candidate.venue_name}"
        if candidate.venue_city:
            line += f" ({candidate.venue_city})"
        if candidate.venue_id:
            line += f" [venue {candidate.venue_id}]"
        lines.append(line)
    lines.append("If you want, I can check one of these first or try another area.")
    return sanitize_tool_output("\n".join(lines))


async def _run_travel_browser_fallback(
    *,
    settings: Settings,
    workspace: Optional[Workspace],
    task: str,
    max_pages: int = 2,
    max_steps: int = 8,
    strategy_mode: str = STRATEGY_API_DIRECT,
) -> str:
    if strategy_mode == STRATEGY_API_DIRECT:
        return "BROWSER_TASK_UNAVAILABLE: browser escalation is disabled in API_DIRECT strategy mode."
    if strategy_mode == STRATEGY_BROWSER_USE_VISUAL_PIVOT:
        if workspace is None:
            raise RuntimeError("BROWSER_USE_VISUAL_PIVOT requires a workspace.")
        if not settings.browser_use_enabled:
            return "BROWSER_TASK_UNAVAILABLE: Browser Use is disabled in BROWSER_USE_VISUAL_PIVOT strategy mode."
        return await run_browser_use_task(
            (
                "Visual travel pivot mode. Use visual browser automation as the active fallback for this run. "
                "Stay tightly scoped and summarize the best live options clearly.\n\n"
                + task
            ),
            max_pages=max(1, min(max_pages, 2)),
            max_steps=max(10, min(max_steps * 2, 16)),
            settings=settings,
            workspace=workspace,
            enable_optional_mcps=False,
        )
    scoped_task = (
        "Travel fallback mode. The structured travel tools were unavailable or rate-limited. "
        "Use Google Travel / Google Flights / Google Hotels, Kayak, or another mainstream travel source only as needed. "
        "Stay tightly scoped and summarize the best options clearly.\n\n"
        + task
    )
    if workspace is None:
        return "BROWSER_TASK_UNAVAILABLE: workspace is unavailable for browser strategy execution."

    stagehand_result = await run_stagehand_task(
        (
            "Travel interaction fallback. The direct travel tools were unavailable or rate-limited, "
            "and this strategy run is explicitly using Stagehand before any other browser fallback. "
            "Use at most one or two mainstream travel sites, keep steps bounded, and return the best live options you can verify.\n\n"
            + task
        ),
        max_steps=max(10, min(max_steps * 2, 16)),
        settings=settings,
        workspace=workspace,
    )
    return stagehand_result


def _browser_fallback_failed(result: str) -> bool:
    normalized = (result or "").strip().lower()
    if not normalized:
        return True
    return any(
        marker in normalized
        for marker in (
            "browser task could not read any public pages",
            "browser task failed",
            "browser fallback also failed",
            "public-web fallback failed",
            "no public pages could be read",
            "browser task unavailable",
            "stagehand browser task failed",
            "stagehand browser task unavailable",
            "stagehand_browser_task_failed",
            "stagehand_browser_task_unavailable",
        )
    )


async def _run_general_browser_task(
    *,
    settings: Settings,
    workspace: Optional[Workspace],
    task: str,
    max_pages: int,
    max_steps: int,
    strategy_mode: str = STRATEGY_API_DIRECT,
) -> str:
    if strategy_mode == STRATEGY_API_DIRECT:
        return "BROWSER_TASK_UNAVAILABLE: browser interaction is disabled in API_DIRECT strategy mode."
    if workspace is None:
        return "BROWSER_TASK_UNAVAILABLE: workspace is unavailable for browser strategy execution."
    if strategy_mode == STRATEGY_STAGEHAND_STEALTH_ACT:
        return await run_stagehand_task(
            (
                "Stagehand browser escalation mode. Use Stagehand as the primary browser strategy for this run. "
                "Keep steps bounded and finish with a concise summary.\n\n"
                + task
            ),
            max_steps=max_steps,
            settings=settings,
            workspace=workspace,
        )
    if strategy_mode == STRATEGY_BROWSER_USE_VISUAL_PIVOT:
        if not settings.browser_use_enabled:
            return "BROWSER_TASK_UNAVAILABLE: Browser Use is disabled in BROWSER_USE_VISUAL_PIVOT strategy mode."
        return await run_browser_use_task(
            (
                "Visual browser escalation mode. Use Browser Use as the active strategy for this run. "
                "Keep the steps bounded and finish with a concise summary.\n\n"
                + task
            ),
            max_pages=max_pages,
            max_steps=max_steps,
            settings=settings,
            workspace=workspace,
            enable_optional_mcps=False,
        )
    return await run_browser_task(
        task,
        max_pages=max_pages,
        max_steps=max_steps,
    )


def _current_local_datetime_text(settings: Settings) -> str:
    timezone_name = settings.restaurant_cli_timezone or os.environ.get("TZ", "America/New_York")
    try:
        current = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        timezone_name = "UTC"
        current = datetime.now(timezone.utc)
    rendered = current.strftime("%A, %B %d, %Y at %I:%M %p").replace(" 0", " ")
    return (
        f"Current local date/time: {rendered} ({timezone_name}). "
        "Resolve relative dates like today, tomorrow, this Friday, and next week against this timestamp."
    )


def _restaurant_browser_availability_task(
    *,
    booking_url: str,
    venue_name: str,
    venue_city: str,
    provider_label: str,
    date: str,
    party_size: int,
) -> str:
    venue_line = venue_name + (f" in {venue_city}" if venue_city else "")
    return (
        f"Open {booking_url}. "
        f"Use the live {provider_label} booking flow for {venue_line} on {date} for {party_size} people. "
        "Do not just summarize the landing screen. "
        "Actively set or confirm the requested date and party size, then open the reservation time area and interact with the page controls if needed to reveal actual bookable times. "
        "If times are available, list the visible bookable times. "
        "If no times are available, say that clearly. "
        "Also include any visible cancellation, deposit, or prepaid reservation language. "
        "Keep it concise and plain text."
    )


async def _restaurant_browser_availability_summary(
    *,
    settings: Settings,
    workspace: Workspace,
    venue_id: str,
    venue_name: str,
    venue_city: str,
    venue_url: str,
    provider: str,
    date: str,
    party_size: int,
    requested_time: str = "",
    strategy_mode: str = STRATEGY_API_DIRECT,
) -> str:
    normalized_provider = normalize_restaurant_provider(provider)
    provider_label = _restaurant_provider_label(normalized_provider)
    if normalized_provider == "resy" and venue_url:
        probe = await _run_resy_browser_probe(
            settings=settings,
            workspace=workspace,
            venue_url=venue_url,
            venue_name=venue_name,
            venue_city=venue_city,
            date=date,
            time=requested_time,
            party_size=party_size,
        )
        if probe is not None:
            return _render_resy_browser_probe_summary(
                probe,
                venue_id=venue_id,
                venue_name=venue_name,
                venue_city=venue_city,
                date=date,
                time=requested_time or "(not specified)",
                party_size=party_size,
            )
    browser_booking_url = venue_url
    if normalized_provider == "resy" and venue_url:
        browser_booking_url = _build_resy_booking_page_url(
            venue_url,
            date=date,
            party_size=max(1, party_size),
        )
    browser_summary = await _run_general_browser_task(
        settings=settings,
        workspace=workspace,
        task=_restaurant_browser_availability_task(
            booking_url=browser_booking_url,
            venue_name=venue_name,
            venue_city=venue_city,
            provider_label=provider_label,
            date=date,
            party_size=max(1, party_size),
        ),
        max_pages=2,
        max_steps=12,
        strategy_mode=strategy_mode,
    )
    lines = [
        f"Matched venue: {venue_name}" + (f" ({venue_city})" if venue_city else "") + f" on {provider_label}",
        f"Booking page: {browser_booking_url}",
        browser_summary.strip(),
    ]
    return sanitize_tool_output("\n".join(line for line in lines if line.strip()))


def _render_user_query(
    query: str,
    *,
    settings: Settings,
    agent_name: str,
    persona_summary: str,
    context_summary: str,
    recent_turns: list[ThreadTurn],
    durable_memories: list[str],
    attachments: list[str],
    mode: str,
    resume_checkpoint: Optional[CheckpointPayload],
) -> str:
    parts = []
    routing_profile = task_routing_profile(query)
    parts.append(_current_local_datetime_text(settings))
    if persona_summary.strip():
        parts.append(f"{agent_name} persona:\n{persona_summary.strip()[:1200]}")
    if durable_memories:
        parts.append("Durable memory:\n" + "\n".join(f"- {memory}" for memory in durable_memories[:20]))
    if context_summary:
        parts.append("Recent 48-hour context:\n" + context_summary[:3500])
    if recent_turns:
        rendered_turns = []
        for turn in recent_turns[-12:]:
            speaker = "User" if turn.role == ThreadTurnRole.USER else "Friday"
            rendered_turns.append(f"{speaker}: {turn.text}")
        parts.append("Recent turns:\n" + "\n".join(rendered_turns)[-5000:])
    if attachments:
        parts.append("Workspace inputs:\n" + "\n".join(f"- {name}" for name in attachments[:50]))
    if resume_checkpoint is not None:
        resume_parts = [f"Previous checkpoint summary:\n{resume_checkpoint.summary[:3000]}"]
        if resume_checkpoint.current_step:
            resume_parts.append(f"Previous step: {resume_checkpoint.current_step[:500]}")
        if resume_checkpoint.resume_instructions:
            resume_parts.append(f"Resume instructions:\n{resume_checkpoint.resume_instructions[:3000]}")
        if resume_checkpoint.workspace_files:
            resume_parts.append("Prior workspace files:\n" + "\n".join(f"- {name}" for name in resume_checkpoint.workspace_files[:50]))
        if resume_checkpoint.tool_outputs:
            resume_parts.append("Prior tool outputs:\n" + "\n".join(f"- {value}" for value in resume_checkpoint.tool_outputs[:20])[:3500])
        parts.append("\n".join(resume_parts))
    if mode == "heavy":
        parts.append(
            "You are running inside the dedicated hands worker. "
            "Prefer deterministic search/fetch tools for research first. "
            "Use browser tools only when a site actually requires interaction or the deterministic tools are insufficient. "
            "If you cannot continue without a missing user answer, choice, or attachment, use pause_for_input instead of finishing with a plain question. "
            "If one public source blocks you or fails, skip it, note the warning, and continue with other sources. "
            "Do not fail the whole task just because one site returns 403/404/timeout. "
            "Keep actions bounded and leave clear intermediate state."
        )
        parts.append(f"Task routing profile: {routing_profile.name}\n{routing_profile.summary}")
        if routing_profile.instructions:
            parts.append("Task-specific routing guidance:\n" + "\n".join(f"- {line}" for line in routing_profile.instructions))
        direct_tool_mode = _direct_tool_mode_summary(query, routing_profile.name)
        if direct_tool_mode:
            parts.append("Direct-tool operating mode:\n" + direct_tool_mode)
    parts.append("User request:\n" + query)
    return "\n\n".join(parts)


async def run_agent(
    query: str,
    *,
    settings: Settings,
    store: StateStore,
    spend_store: Optional[object] = None,
    mode: str = "light",
    context_summary: str = "",
    recent_turns: Optional[list[ThreadTurn]] = None,
    durable_memories: Optional[list[str]] = None,
    workspace: Optional[Workspace] = None,
    attachment_names: Optional[list[str]] = None,
    config: Optional[AgentConfig] = None,
    resume_checkpoint: Optional[CheckpointPayload] = None,
    current_job: Optional[AgentJob] = None,
    strategy_mode: str = STRATEGY_API_DIRECT,
) -> AgentResult:
    spend_target = spend_store or store
    if not spend_target.budget_available():
        logger.warning("agent budget blocked request")
        return AgentResult(text="Daily Budget Reached", budget_blocked=True)

    if needs_confirmation(query):
        logger.info("agent request requires confirmation")
        return AgentResult(text="This task may change data, send information, or spend money. Reply with explicit confirmation and the exact action you want me to take.")

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not deepseek_key:
        deepseek_key = settings.secret(settings.deepseek_api_key_param)
    if deepseek_key:
        os.environ["DEEPSEEK_API_KEY"] = deepseek_key

    memories = durable_memories or []
    attachments = attachment_names or []
    effective_config = config or AgentConfig()
    routing_profile = task_routing_profile(query)
    allow_browser_tools = _should_expose_browser_tools(query, routing_profile.name) and strategy_mode != STRATEGY_API_DIRECT
    allow_travel_browser_fallback = _should_expose_travel_browser_fallback(query, routing_profile.name) and strategy_mode != STRATEGY_API_DIRECT
    structured_restaurant_task = _is_structured_restaurant_task(query, routing_profile.name)
    missing_restaurant_booking_details = _restaurant_booking_missing_details(query, routing_profile.name)
    prefill: Optional[RestaurantBookingPrefill] = None
    booking_preflight: Optional[RestaurantBookingPreflightResult] = None
    effective_query = _render_user_query(
        query,
        settings=settings,
        agent_name=effective_config.agent_name,
        persona_summary=effective_config.persona_summary,
        context_summary=context_summary,
        recent_turns=recent_turns or [],
        durable_memories=memories,
        attachments=attachments,
        mode=mode,
        resume_checkpoint=resume_checkpoint,
    )

    if mode == "heavy":
        strategy_note = strategy_guidance(strategy_mode)
        if strategy_note:
            effective_query += "\n\nExecution strategy:\n" + strategy_note
        if structured_restaurant_task and not missing_restaurant_booking_details:
            effective_query += (
                "\n\nRestaurant execution directive:\n"
                "This is a complete restaurant task. Start with the restaurant availability/search flow immediately. "
                "Do not spend extra turns planning, narrating, or rephrasing the request before the first concrete tool call."
            )
        if structured_restaurant_task and strategy_mode != STRATEGY_API_DIRECT:
            effective_query += (
                "\n\nRestaurant fallback directive:\n"
                "This restaurant run has already left the structured API_DIRECT lane. "
                "Do not spend extra turns re-matching the same venue or retrying the same structured availability path. "
                "Use the restaurant availability flow once to trigger the browser-backed provider path for the best venue match, "
                "then summarize the concrete live result."
            )
        _ensure_default_mailbox_identity(settings, store)
        if not _should_skip_booking_cancellation_precheck(
            query=query,
            effective_query=effective_query,
            routing_profile_name=routing_profile.name,
            resume_checkpoint=resume_checkpoint,
        ):
            _maybe_raise_booking_cancellation_pause(store, query, routing_profile.name)
        if missing_restaurant_booking_details:
            missing_text = ", ".join(missing_restaurant_booking_details)
            raise PauseForInputRequested(
                question=f"I need the missing booking details before I can make the reservation: {missing_text}.",
                details=(
                    "Please reply with the missing details in one message, for example:\n"
                    "- party size: 2\n"
                    "- date: 2026-05-24\n"
                    "- time: 7:30 PM"
                ),
                summary="waiting for the missing booking details before making the reservation",
                current_step="waiting_for_user_input",
                resume_instructions="Use the provided booking details to continue the same reservation flow without asking again for the same fields.",
            )
        prefill = _extract_restaurant_booking_prefill(query, settings=settings, routing_profile_name=routing_profile.name)
        if prefill is not None and workspace is not None:
            preflight_started_at = time.monotonic()
            logger.warning(
                "restaurant_booking_preflight_start venue_query=%s provider=%s city=%s date=%s time=%s party_size=%s",
                prefill.venue_query,
                prefill.provider,
                prefill.city,
                prefill.date,
                prefill.time,
                prefill.party_size,
            )
            booking_preflight = await _restaurant_booking_preflight(
                settings=settings,
                workspace=workspace,
                prefill=prefill,
            )
            logger.warning(
                "restaurant_booking_preflight_complete venue_query=%s provider=%s elapsed=%.2fs",
                prefill.venue_query,
                prefill.provider,
                max(0.0, time.monotonic() - preflight_started_at),
            )
            effective_query += (
                "\n\nStructured restaurant preflight:\n"
                + booking_preflight.summary
                + "\n\nStart from this preflight context. Do not spend extra turns re-matching the same venue before taking the next concrete action."
            )
        discovery_prefill = _extract_restaurant_discovery_prefill(
            query,
            settings=settings,
            routing_profile_name=routing_profile.name,
        )
        if prefill is None and discovery_prefill is not None and workspace is not None:
            discovery_preflight_started_at = time.monotonic()
            logger.warning(
                "restaurant_discovery_preflight_start search_query=%s provider=%s city=%s date=%s time=%s party_size=%s",
                discovery_prefill.search_query,
                discovery_prefill.provider,
                discovery_prefill.city,
                discovery_prefill.date,
                discovery_prefill.time,
                discovery_prefill.party_size,
            )
            discovery_preflight = await _restaurant_discovery_preflight(
                settings=settings,
                workspace=workspace,
                prefill=discovery_prefill,
            )
            logger.warning(
                "restaurant_discovery_preflight_complete search_query=%s provider=%s elapsed=%.2fs",
                discovery_prefill.search_query,
                discovery_prefill.provider,
                max(0.0, time.monotonic() - discovery_preflight_started_at),
            )
            if strategy_mode == STRATEGY_API_DIRECT:
                return _text_only_agent_result(
                    _render_restaurant_discovery_direct_response(
                        prefill=discovery_prefill,
                        preflight=discovery_preflight,
                    )
                )
            effective_query += (
                "\n\nStructured restaurant discovery kickoff:\n"
                + discovery_preflight.summary
                + "\n\nStart from this discovery context and continue directly to live slot checks or a concise shortlist. "
                "Do not call another broad restaurant search while these preflight candidates remain usable."
            )
            if strategy_mode != STRATEGY_API_DIRECT:
                effective_query += (
                    "\n\nRestaurant discovery fallback directive:\n"
                    "This retry run is already past API_DIRECT. Use only restaurant_availability with the venue_id values from the preflight candidates. "
                    "Do not call restaurant_find_availability with candidate names in this run."
                )

    deps = AgentDependencies(
        settings=settings,
        store=store,
        workspace=workspace,
        browser=BrowserSession(workspace=workspace, settings=settings) if mode == "heavy" else None,
        current_job=current_job,
        strategy_mode=strategy_mode,
        restaurant_booking_prefill=prefill,
        restaurant_booking_preflight=booking_preflight,
    )
    system_prompt = STATIC_SYSTEM_PROMPT
    if effective_config.agent_name.strip() and effective_config.agent_name.strip() != "Friday":
        system_prompt = system_prompt.replace("You are Friday, Kevin's private personal task agent.", f"You are {effective_config.agent_name.strip()}, Kevin's private personal task agent.")
    if effective_config.system_prompt_suffix.strip():
        system_prompt = f"{system_prompt}\n\nAdditional operator guidance:\n{effective_config.system_prompt_suffix.strip()}"
    agent = Agent(_select_model(query, settings), deps_type=AgentDependencies, system_prompt=system_prompt)

    if mode == "heavy":
        effective_query += (
            "\n\nDo not create PDF, TXT, CSV, spreadsheet, or other deliverable files unless the user explicitly asked for a file, report, export, or document. "
            "If the user explicitly asks for a deliverable file such as a PDF, create it in the workspace before you finish. "
            "Prefer concise, useful files over large raw dumps. "
            "If you need the user to answer something before continuing, call pause_for_input."
        )
        booking_guidance = _phase1_booking_runtime_guidance(query, routing_profile.name)
        if booking_guidance:
            effective_query += "\n\n" + booking_guidance

        def _browser_tool_warning(action: str, exc: Exception) -> str:
            logger.warning("browser tool degraded action=%s error=%s", action, exc)
            return sanitize_tool_output(
                f"BROWSER_ACTION_BLOCKED: {action} failed due to {exc}. "
                "Try another selector, another source, or continue with the information already gathered."
            )

        if not structured_restaurant_task:

            @agent.tool
            async def web_search(ctx: RunContext[AgentDependencies], task: str, max_results: int = 5) -> str:
                """Use deterministic web search results before escalating to full browser automation."""
                try:
                    result = await search_web(task, settings=ctx.deps.settings, max_results=max_results)
                except Exception as exc:
                    logger.warning("web_search degraded task=%s error=%s", task, exc)
                    return sanitize_tool_output(
                        f"DETERMINISTIC_SEARCH_UNAVAILABLE: search failed for '{task}' due to {exc}. "
                        "Try another query or use the browser only if needed."
                    )
                return sanitize_tool_output(result)

            @agent.tool
            async def fetch_web_page(ctx: RunContext[AgentDependencies], url: str, max_chars: int = 6000) -> str:
                """Fetch and clean a public web page without opening a full browser."""
                try:
                    result = await fetch_page_content(url, max_chars=max_chars)
                except Exception as exc:
                    logger.warning("fetch_web_page degraded url=%s error=%s", url, exc)
                    return sanitize_tool_output(
                        f"PUBLIC_PAGE_FETCH_BLOCKED: could not fetch {url} due to {exc}. "
                        "Skip this source, try another public source, or use browser tools only if interaction is truly needed.",
                        limit=max_chars,
                    )
                return sanitize_tool_output(result, limit=max_chars)

        if settings.skiplagged_mcp_enabled:

            @agent.tool
            async def travel_resolve_iata(ctx: RunContext[AgentDependencies], place: str) -> str:
                """Resolve a city or airport phrase into an IATA code using Skiplagged. Prefer this before flight or car searches when the code is uncertain."""
                try:
                    result = await call_skiplagged_tool(
                        ctx.deps.settings,
                        tool_name="sk_resolve_iata",
                        arguments={
                            "input": place,
                            "renderMode": "text",
                        },
                    )
                except Exception as exc:
                    logger.warning("travel_resolve_iata degraded place=%s error=%s", place, exc)
                    return sanitize_tool_output(
                        f"TRAVEL_TOOL_UNAVAILABLE: could not resolve '{place}' to an IATA code because {exc}."
                    )
                return sanitize_tool_output(result)

            @agent.tool
            async def travel_search_flights(
                ctx: RunContext[AgentDependencies],
                origin: str,
                destination: str,
                departure_date: str,
                return_date: str = "",
                adults: int = 1,
                fare_class: str = "economy",
                max_results: int = 12,
                sort: str = "value",
            ) -> str:
                """Search flights using Skiplagged MCP. Prefer this over opening flight websites directly."""
                arguments: dict[str, object] = {
                    "origin": origin,
                    "destination": destination,
                    "departureDate": departure_date,
                    "adults": max(1, adults),
                    "fareClass": fare_class,
                    "limit": max(1, min(max_results, 25)),
                    "sort": sort,
                    "renderMode": "text",
                }
                if return_date.strip():
                    arguments["returnDate"] = return_date
                try:
                    result = await call_skiplagged_tool(
                        ctx.deps.settings,
                        tool_name="sk_flights_search",
                        arguments=arguments,
                    )
                except Exception as exc:
                    logger.warning("travel_search_flights degraded origin=%s destination=%s error=%s", origin, destination, exc)
                    try:
                        fallback = await _run_travel_browser_fallback(
                            settings=ctx.deps.settings,
                            workspace=ctx.deps.workspace,
                            task=(
                                f"Find round-trip flights from {origin} to {destination} departing {departure_date} "
                                + (f"and returning {return_date} " if return_date.strip() else "")
                                + f"for {max(1, adults)} adult(s) in {fare_class}. "
                                "Prefer nonstop or low-stop options when they are clearly better value. "
                                "Summarize the best options with airline, times, duration, and price."
                            ),
                            strategy_mode=ctx.deps.strategy_mode,
                        )
                    except Exception as fallback_exc:
                        logger.warning(
                            "travel_search_flights fallback degraded origin=%s destination=%s error=%s",
                            origin,
                            destination,
                            fallback_exc,
                        )
                        return sanitize_tool_output(
                            "TRAVEL_TOOL_UNAVAILABLE: Skiplagged flight search failed due to "
                            f"{exc}. Browser fallback also failed due to {fallback_exc}."
                        )
                    return sanitize_tool_output(
                        "TRAVEL_TOOL_UNAVAILABLE: Skiplagged flight search was unavailable, so browser fallback was used.\n\n"
                        + fallback
                    )
                return sanitize_tool_output(result)

            @agent.tool
            async def travel_search_flexible_departures(
                ctx: RunContext[AgentDependencies],
                origin: str,
                destination: str,
                departure_date: str,
                return_date: str = "",
                sort: str = "date",
            ) -> str:
                """Check nearby departure dates using Skiplagged flex calendar data."""
                arguments: dict[str, object] = {
                    "origin": origin,
                    "destination": destination,
                    "departureDate": departure_date,
                    "sort": sort,
                    "renderMode": "text",
                }
                if return_date.strip():
                    arguments["returnDate"] = return_date
                try:
                    result = await call_skiplagged_tool(
                        ctx.deps.settings,
                        tool_name="sk_flex_departure_calendar",
                        arguments=arguments,
                    )
                except Exception as exc:
                    logger.warning("travel_search_flexible_departures degraded origin=%s destination=%s error=%s", origin, destination, exc)
                    return sanitize_tool_output(
                        f"TRAVEL_TOOL_UNAVAILABLE: flexible departure search failed because {exc}."
                    )
                return sanitize_tool_output(result)

            @agent.tool
            async def travel_search_hotels(
                ctx: RunContext[AgentDependencies],
                city: str,
                checkin: str,
                checkout: str,
                adults: int = 2,
                rooms: int = 1,
                max_results: int = 12,
                sort: str = "value",
            ) -> str:
                """Search hotels using Skiplagged MCP. Prefer this over browsing hotel aggregators when a structured result is enough."""
                try:
                    result = await call_skiplagged_tool(
                        ctx.deps.settings,
                        tool_name="sk_hotels_search",
                        arguments={
                            "city": city,
                            "checkin": checkin,
                            "checkout": checkout,
                            "numAdults": max(1, adults),
                            "numRooms": max(1, rooms),
                            "limit": max(1, min(max_results, 25)),
                            "sort": sort,
                            "renderMode": "text",
                        },
                    )
                except Exception as exc:
                    logger.warning("travel_search_hotels degraded city=%s error=%s", city, exc)
                    try:
                        fallback = await _run_travel_browser_fallback(
                            settings=ctx.deps.settings,
                            workspace=ctx.deps.workspace,
                            task=(
                                f"Find hotels in {city} for check-in {checkin} and checkout {checkout} "
                                f"for {max(1, adults)} adult(s) and {max(1, rooms)} room(s). "
                                "Use Google Travel / Google Hotels first and summarize strong options with nightly price, total price, rating, and neighborhood."
                            ),
                            strategy_mode=ctx.deps.strategy_mode,
                        )
                    except Exception as fallback_exc:
                        logger.warning("travel_search_hotels fallback degraded city=%s error=%s", city, fallback_exc)
                        return sanitize_tool_output(
                            f"TRAVEL_TOOL_UNAVAILABLE: hotel search failed because {exc}. Browser fallback also failed because {fallback_exc}."
                        )
                    return sanitize_tool_output(
                        "TRAVEL_TOOL_UNAVAILABLE: direct hotel search was unavailable, so browser fallback was used.\n\n"
                        + fallback
                    )
                return sanitize_tool_output(result)

            @agent.tool
            async def travel_search_cars(
                ctx: RunContext[AgentDependencies],
                pickup_location: str,
                pickup_date: str,
                dropoff_date: str,
                pickup_time: str = "10:00",
                dropoff_time: str = "10:00",
                dropoff_location: str = "",
                max_results: int = 12,
            ) -> str:
                """Search rental cars using Skiplagged MCP."""
                arguments: dict[str, object] = {
                    "pickupLocation": pickup_location,
                    "pickupDate": pickup_date,
                    "pickupTime": pickup_time,
                    "dropoffDate": dropoff_date,
                    "dropoffTime": dropoff_time,
                    "limit": max(1, min(max_results, 25)),
                    "renderMode": "text",
                }
                if dropoff_location.strip():
                    arguments["dropoffLocation"] = dropoff_location
                try:
                    result = await call_skiplagged_tool(
                        ctx.deps.settings,
                        tool_name="sk_cars_search",
                        arguments=arguments,
                    )
                except Exception as exc:
                    logger.warning("travel_search_cars degraded pickup_location=%s error=%s", pickup_location, exc)
                    try:
                        fallback = await _run_travel_browser_fallback(
                            settings=ctx.deps.settings,
                            workspace=ctx.deps.workspace,
                            task=(
                                f"Find rental cars for pickup in {pickup_location} on {pickup_date} at {pickup_time} "
                                f"and dropoff on {dropoff_date} at {dropoff_time}"
                                + (f" in {dropoff_location}" if dropoff_location.strip() else "")
                                + ". Summarize strong options with company, vehicle class, cancellation terms if shown, and total price."
                            ),
                            strategy_mode=ctx.deps.strategy_mode,
                        )
                    except Exception as fallback_exc:
                        logger.warning("travel_search_cars fallback degraded pickup_location=%s error=%s", pickup_location, fallback_exc)
                        return sanitize_tool_output(
                            f"TRAVEL_TOOL_UNAVAILABLE: car rental search failed because {exc}. Browser fallback also failed because {fallback_exc}."
                        )
                    return sanitize_tool_output(
                        "TRAVEL_TOOL_UNAVAILABLE: direct car-rental search was unavailable, so browser fallback was used.\n\n"
                        + fallback
                    )
                return sanitize_tool_output(result)

            if allow_travel_browser_fallback:

                @agent.tool
                async def travel_browser_fallback(
                    ctx: RunContext[AgentDependencies],
                    task: str,
                    max_pages: int = 2,
                    max_steps: int = 8,
                ) -> str:
                    """Use browser automation only after the direct travel tools fail or are unavailable. Keep the scope tight to flights, hotels, or rental cars."""
                    try:
                        result = await run_browser_task(
                            (
                                "Travel fallback mode. Use travel websites only because the direct travel tools were unavailable. "
                                "Stay tightly scoped to the user's travel search, avoid wandering, and summarize the best options clearly.\n\n"
                                + task
                            ),
                            max_pages=max_pages,
                            max_steps=max_steps,
                        )
                    except Exception as exc:
                        logger.warning("travel_browser_fallback degraded task=%s error=%s", task, exc)
                        return sanitize_tool_output(
                            f"TRAVEL_BROWSER_FALLBACK_UNAVAILABLE: browser fallback failed because {exc}."
                        )
                    return sanitize_tool_output(result)

        if settings.restaurant_cli_enabled and workspace is not None:

            @agent.tool
            async def restaurant_find_availability(
                ctx: RunContext[AgentDependencies],
                query: str,
                date: str,
                party_size: int = 2,
                provider: str = "resy",
                city: str = "",
                limit: int = 8,
            ) -> str:
                """Fetch live availability for a specific restaurant venue. Use restaurant_search first for cuisine discovery or multi-option venue research."""
                assert ctx.deps.workspace is not None
                attempts = await _restaurant_find_availability_attempts(
                    settings=ctx.deps.settings,
                    workspace=ctx.deps.workspace,
                    query=query,
                    provider=provider,
                    city=city,
                    limit=limit,
                    timeout_seconds=25,
                    booking_prefill=ctx.deps.restaurant_booking_prefill,
                    booking_preflight=ctx.deps.restaurant_booking_preflight,
                )
                if _is_restaurant_discovery_request(query, "booking_commerce"):
                    return _render_restaurant_search_shortlist(
                        query=query,
                        attempts=attempts,
                        city=city,
                        limit=limit,
                    )
                browser_probe_candidate: Optional[tuple[str, str, str, str]] = None
                failure_lines: list[str] = []
                for attempt in attempts:
                    normalized_provider = normalize_restaurant_provider(attempt.provider)
                    provider_label = _restaurant_provider_label(normalized_provider)
                    if attempt.error:
                        logger.warning(
                            "restaurant_find_availability search degraded query=%s provider=%s error=%s",
                            query,
                            attempt.provider,
                            attempt.error,
                        )
                        failure_lines.append(f"{provider_label} search failed because {attempt.error}.")
                        continue
                    search_payload = attempt.payload if isinstance(attempt.payload, dict) else {}
                    failures = list(search_payload.get("failures") or [])
                    best_match = attempt.best_match
                    if best_match is None:
                        failure_lines.append(f"{provider_label} had no matching venue for {query}.")
                        continue
                    venue_id = str(best_match.get("id") or "").strip()
                    venue_name = str(best_match.get("name") or query).strip() or query
                    venue_city = str(best_match.get("city") or "").strip()
                    venue_url = str(best_match.get("url") or "").strip()
                    if not venue_id:
                        failure_lines.append(f"{provider_label} matched {venue_name}, but returned no usable venue id.")
                        continue
                    if normalized_provider == "resy" and venue_url and browser_probe_candidate is None:
                        browser_probe_candidate = (venue_id, venue_name, venue_city, venue_url)
                    if ctx.deps.strategy_mode != STRATEGY_API_DIRECT:
                        if not venue_url:
                            failure_lines.append(
                                f"{provider_label} matched {venue_name}, but there was no live booking page URL to inspect in {ctx.deps.strategy_mode} mode."
                            )
                            continue
                        return await _restaurant_browser_availability_summary(
                            settings=ctx.deps.settings,
                            workspace=ctx.deps.workspace,
                            venue_id=venue_id,
                            venue_name=venue_name,
                            venue_city=venue_city,
                            venue_url=venue_url,
                            provider=normalized_provider,
                            date=date,
                            party_size=max(1, party_size),
                            requested_time="",
                            strategy_mode=ctx.deps.strategy_mode,
                        )
                    availability_args = [
                        "availability",
                        "--venue",
                        venue_id,
                        "--date",
                        date,
                        "--party",
                        str(max(1, party_size)),
                        "--provider",
                        normalized_provider,
                        "--agent",
                    ]
                    try:
                        slots_payload = await run_restaurant_cli_json(
                            ctx.deps.settings,
                            ctx.deps.workspace,
                            *availability_args,
                            timeout_seconds=35,
                        )
                    except Exception as exc:
                        logger.warning(
                            "restaurant_find_availability availability degraded query=%s venue_id=%s provider=%s error=%s",
                            query,
                            venue_id,
                            normalized_provider,
                            exc,
                        )
                        if ctx.deps.strategy_mode == STRATEGY_API_DIRECT:
                            raise RuntimeError(
                                "service unavailable: structured restaurant availability failed in api_direct mode. "
                                f"provider={normalized_provider} venue={venue_name} venue_id={venue_id} error={exc}"
                            ) from exc
                        if normalized_provider == "resy":
                            try:
                                resy_policy_by_token = await fetch_resy_slot_policies(
                                    ctx.deps.settings,
                                    venue_id=venue_id,
                                    date=date,
                                    party_size=max(1, party_size),
                                )
                            except Exception as policy_exc:
                                logger.warning(
                                    "restaurant_find_availability direct resy fallback failed query=%s venue_id=%s error=%s",
                                    query,
                                    venue_id,
                                    policy_exc,
                                )
                            else:
                                return sanitize_tool_output(
                                    _render_resy_policy_slot_lines(
                                        resy_policy_by_token,
                                        date=date,
                                        party_size=party_size,
                                        venue_name=venue_name,
                                        venue_city=venue_city,
                                        venue_url=venue_url,
                                    )
                                )
                        failure_lines.append(
                            f"{provider_label} live availability failed for {venue_name}"
                            + (f" ({venue_city})" if venue_city else "")
                            + f" because {exc}."
                        )
                        continue
                    slots = slots_payload if isinstance(slots_payload, list) else []
                    if normalized_provider == "opentable" and not slots:
                        try:
                            slots = await fetch_opentable_slots_via_browser(
                                ctx.deps.settings,
                                ctx.deps.workspace,
                                venue_id=venue_id,
                                date=date,
                                time="19:00",
                                party_size=max(1, party_size),
                            )
                        except Exception as exc:
                            logger.warning(
                                "restaurant_find_availability opentable browser probe degraded query=%s venue_id=%s error=%s",
                                query,
                                venue_id,
                                exc,
                            )
                    resy_policy_by_token: dict[str, RestaurantSlotPolicy] = {}
                    if normalized_provider == "resy" and slots:
                        try:
                            resy_policy_by_token = await fetch_resy_slot_policies(
                                ctx.deps.settings,
                                venue_id=venue_id,
                                date=date,
                                party_size=max(1, party_size),
                            )
                        except Exception as exc:
                            logger.warning(
                                "restaurant_find_availability policy degraded query=%s venue_id=%s provider=%s error=%s",
                                query,
                                venue_id,
                                normalized_provider,
                                exc,
                            )
                    lines = [
                        f"Matched venue: {venue_name}"
                        + (f" ({venue_city})" if venue_city else "")
                        + f" on {provider_label}",
                    ]
                    if venue_url:
                        lines.append(f"Booking page: {venue_url}")
                    if slots:
                        lines.append(f"Live availability for {date} for {max(1, party_size)} people:")
                        for slot in slots[:12]:
                            slot_time = str(slot.get('time') or '').strip()
                            slot_type = str(slot.get('type') or '').strip()
                            if slot_time:
                                line = f"- {slot_time}" + (f" ({slot_type})" if slot_type else "")
                                slot_token = str(slot.get("token") or "").strip()
                                policy = resy_policy_by_token.get(slot_token)
                                if policy is not None and policy.policy_text:
                                    line += f" — {policy.policy_text}"
                                lines.append(line)
                    else:
                        lines.append(f"No live slots were returned for {date} for {max(1, party_size)} people.")
                    if failures:
                        provider_labels = ", ".join(str(item.get("provider") or "provider") for item in failures[:3])
                        lines.append(f"Other provider lookups also had issues: {provider_labels}.")
                    return sanitize_tool_output("\n".join(lines))
                if browser_probe_candidate is not None and ctx.deps.strategy_mode != STRATEGY_API_DIRECT:
                    venue_id, venue_name, venue_city, venue_url = browser_probe_candidate
                    probe = await _run_resy_browser_probe(
                        settings=ctx.deps.settings,
                        workspace=ctx.deps.workspace,
                        venue_url=venue_url,
                        venue_name=venue_name,
                        venue_city=venue_city,
                        date=date,
                        time="",
                        party_size=party_size,
                    )
                    if probe is not None:
                        return _render_resy_browser_probe_summary(
                            probe,
                            venue_id=venue_id,
                            venue_name=venue_name,
                            venue_city=venue_city,
                            date=date,
                            time="(not specified)",
                            party_size=party_size,
                        )
                return sanitize_tool_output(
                    _restaurant_provider_browser_fallback_message(
                        provider=restaurant_provider_sequence(provider)[0],
                        venue_name=query,
                        date=date,
                        party_size=party_size,
                        details="\n".join(failure_lines[:6]) if failure_lines else "The provider sequence exhausted without a usable live availability result.",
                    )
                )

            @agent.tool
            async def restaurant_search(
                ctx: RunContext[AgentDependencies],
                query: str,
                provider: str = "resy",
                city: str = "",
                limit: int = 10,
            ) -> str:
                """Search restaurant venues with Resy by default. Use this first for cuisine discovery or when the user has not picked a specific restaurant yet."""
                assert ctx.deps.workspace is not None
                attempts = await _restaurant_search_attempts(
                    settings=ctx.deps.settings,
                    workspace=ctx.deps.workspace,
                    query=query,
                    provider=provider,
                    city=city,
                    limit=limit,
                    timeout_seconds=25,
                )
                return _render_restaurant_search_shortlist(
                    query=query,
                    attempts=attempts,
                    city=city,
                    limit=limit,
                )

            @agent.tool
            async def restaurant_availability(
                ctx: RunContext[AgentDependencies],
                venue_id: str,
                date: str,
                party_size: int = 2,
                provider: str = "resy",
            ) -> str:
                """Look up restaurant reservation availability for a venue and date using a concrete venue id. Prefer this when discovery/preflight already provided a venue_id."""
                assert ctx.deps.workspace is not None
                normalized_provider = normalize_restaurant_provider(provider)
                resolved_venue_id, resolved_venue_name, resolved_venue_city, resolved_venue_url = await _resolve_restaurant_venue_reference(
                    settings=ctx.deps.settings,
                    workspace=ctx.deps.workspace,
                    venue_reference=venue_id,
                    provider=normalized_provider,
                    timeout_seconds=25,
                )
                venue_label = resolved_venue_name or f"venue {resolved_venue_id or venue_id}"
                args = [
                    "availability",
                    "--venue",
                    resolved_venue_id or venue_id,
                    "--date",
                    date,
                    "--party",
                    str(max(1, party_size)),
                    "--provider",
                    normalized_provider,
                    "--agent",
                ]
                try:
                    result = await run_restaurant_cli(
                        ctx.deps.settings,
                        ctx.deps.workspace,
                        *args,
                        timeout_seconds=35,
                    )
                except Exception as exc:
                    logger.warning(
                        "restaurant_availability degraded venue_id=%s resolved_venue_id=%s provider=%s error=%s",
                        venue_id,
                        resolved_venue_id,
                        provider,
                        exc,
                    )
                    if ctx.deps.strategy_mode == STRATEGY_API_DIRECT:
                        raise RuntimeError(
                            "service unavailable: structured restaurant availability failed in api_direct mode. "
                            f"provider={normalized_provider} venue={venue_label} venue_id={resolved_venue_id or venue_id} error={exc}"
                        ) from exc
                    if normalized_provider == "resy" and resolved_venue_url:
                        browser_summary = await _resy_availability_browser_probe_summary(
                            settings=ctx.deps.settings,
                            workspace=ctx.deps.workspace,
                            venue_id=resolved_venue_id or venue_id,
                            venue_name=venue_label,
                            venue_city=resolved_venue_city,
                            venue_url=resolved_venue_url,
                            date=date,
                            party_size=party_size,
                        )
                        if browser_summary is not None:
                            return browser_summary
                    if normalized_provider == "resy":
                        try:
                            policy_map = await fetch_resy_slot_policies(
                                ctx.deps.settings,
                                venue_id=resolved_venue_id or venue_id,
                                date=date,
                                party_size=max(1, party_size),
                            )
                        except Exception as policy_exc:
                            logger.warning(
                                "restaurant_availability direct resy fallback failed venue_id=%s resolved_venue_id=%s provider=%s error=%s",
                                venue_id,
                                resolved_venue_id,
                                provider,
                                policy_exc,
                            )
                        else:
                            return sanitize_tool_output(
                                _render_resy_policy_slot_lines(
                                    policy_map,
                                    date=date,
                                    party_size=party_size,
                                    venue_name=venue_label,
                                    venue_city=resolved_venue_city,
                                    venue_url=resolved_venue_url,
                                )
                            )
                    return sanitize_tool_output(
                        _restaurant_provider_browser_fallback_message(
                            provider=normalized_provider,
                            venue_name=venue_label,
                            date=date,
                            party_size=party_size,
                            details=f"Availability lookup failed because {exc}.",
                        )
                    )
                if normalized_provider == "opentable":
                    try:
                        browser_slots = await fetch_opentable_slots_via_browser(
                            ctx.deps.settings,
                            ctx.deps.workspace,
                            venue_id=venue_id,
                            date=date,
                            time="19:00",
                            party_size=max(1, party_size),
                        )
                    except Exception as exc:
                        logger.warning(
                            "restaurant_availability opentable browser probe degraded venue_id=%s error=%s",
                            venue_id,
                            exc,
                        )
                    else:
                        if browser_slots:
                            return sanitize_tool_output(json.dumps(browser_slots))
                return sanitize_tool_output(result)

            @agent.tool
            async def restaurant_book_or_handoff(
                ctx: RunContext[AgentDependencies],
                venue_id: str,
                date: str,
                time: str,
                party_size: int = 2,
                provider: str = "resy",
                slot_token: str = "",
                notes: str = "",
                venue_name: str = "",
                venue_city: str = "",
            ) -> str:
                """Book a restaurant reservation after explicit approval. Resy uses structured booking; OpenTable uses a browser-confirmed deep-link flow."""
                assert ctx.deps.workspace is not None
                normalized_provider = normalize_restaurant_provider(provider)
                if normalized_provider == "opentable":
                    _enforce_automation_policy(
                        ctx.deps.store,
                        site_scope="opentable.com",
                        category="restaurant",
                        action="zero_dollar_booking",
                    )
                    booking_url = slot_token.strip() if _is_http_url(slot_token) else build_opentable_booking_url(
                        restaurant_id=venue_id,
                        date=date,
                        time=time,
                        party_size=max(1, party_size),
                    )
                    try:
                        return await _run_opentable_booking_browser_flow(
                            settings=ctx.deps.settings,
                            workspace=ctx.deps.workspace,
                            booking_url=booking_url,
                            venue_id=venue_id,
                            venue_name=venue_name,
                            venue_city=venue_city,
                            date=date,
                            time=time,
                            party_size=max(1, party_size),
                        )
                    except PauseForInputRequested:
                        raise
                    except Exception as exc:
                        _handle_phase1_blocking_error(exc, action=f"restaurant_book_or_handoff(opentable, venue={venue_id}, date={date}, time={time})")
                        logger.warning("restaurant_book_or_handoff opentable browser flow failed venue_id=%s error=%s", venue_id, exc)
                        return sanitize_tool_output(
                            f"RESTAURANT_TOOL_UNAVAILABLE: OpenTable browser booking failed because {exc}.\nBooking page: {booking_url}"
                        )
                site_scope = "resy.com" if normalized_provider == "resy" else normalized_provider
                _enforce_automation_policy(
                    ctx.deps.store,
                    site_scope=site_scope,
                    category="restaurant",
                    action="zero_dollar_booking",
                )
                args = [
                    "book",
                    "--venue",
                    venue_id,
                    "--date",
                    date,
                    "--time",
                    time,
                    "--party",
                    str(max(1, party_size)),
                    "--provider",
                    normalized_provider,
                    "--agent",
                    "--idempotent",
                ]
                if slot_token.strip():
                    args.extend(["--slot-token", slot_token.strip()])
                if notes.strip():
                    args.extend(["--notes", notes.strip()])
                policy: Optional[RestaurantSlotPolicy] = None
                if normalized_provider == "resy":
                    policy = await _lookup_resy_slot_policy(
                        ctx.deps.settings,
                        venue_id=venue_id,
                        date=date,
                        party_size=max(1, party_size),
                        slot_token=slot_token.strip(),
                        time=time.strip(),
                    )
                    _maybe_raise_nonfree_resy_confirmation(
                        policy,
                        venue_id=venue_id,
                        venue_name=venue_name,
                        venue_city=venue_city,
                        date=date,
                        time=time,
                        party_size=party_size,
                    )
                try:
                    result = await run_restaurant_cli(
                        ctx.deps.settings,
                        ctx.deps.workspace,
                        *args,
                        timeout_seconds=45,
                    )
                except Exception as exc:
                    _handle_phase1_blocking_error(exc, action=f"restaurant_book_or_handoff({normalized_provider}, venue={venue_id}, date={date}, time={time})")
                    logger.warning("restaurant_book_or_handoff degraded venue_id=%s provider=%s error=%s", venue_id, provider, exc)
                    return sanitize_tool_output(
                        f"RESTAURANT_TOOL_UNAVAILABLE: booking failed because {exc}."
                    )
                rendered = result.strip()
                if normalized_provider == "resy":
                    rendered = (rendered + "\n" + _render_resy_slot_policy(policy)).strip()
                return sanitize_tool_output(rendered)

            @agent.tool
            async def restaurant_list_reservations(
                ctx: RunContext[AgentDependencies],
                provider: str = "resy",
                upcoming_only: bool = True,
            ) -> str:
                """List existing reservations for the configured restaurant account. Currently most useful for Resy."""
                assert ctx.deps.workspace is not None
                args = ["list", "--provider", normalize_restaurant_provider(provider), "--agent"]
                if upcoming_only:
                    args.append("--upcoming")
                try:
                    result = await run_restaurant_cli(
                        ctx.deps.settings,
                        ctx.deps.workspace,
                        *args,
                        timeout_seconds=30,
                    )
                except Exception as exc:
                    logger.warning("restaurant_list_reservations degraded provider=%s error=%s", provider, exc)
                    return sanitize_tool_output(
                        f"RESTAURANT_TOOL_UNAVAILABLE: reservation list failed because {exc}."
                    )
                return sanitize_tool_output(result)

            @agent.tool
            async def restaurant_cancel_reservation(
                ctx: RunContext[AgentDependencies],
                reservation_id: str,
                provider: str = "resy",
                booking_id: str = "",
            ) -> str:
                """Cancel an existing restaurant reservation by provider reservation id. Confirm with the user before using this."""
                assert ctx.deps.workspace is not None
                normalized_provider = normalize_restaurant_provider(provider)
                args = ["cancel", reservation_id.strip(), "--provider", normalized_provider, "--agent"]
                result: dict[str, object] | None = None
                try:
                    raw_result = await run_restaurant_cli_json(
                        ctx.deps.settings,
                        ctx.deps.workspace,
                        *args,
                        timeout_seconds=30,
                    )
                except Exception as exc:
                    if normalized_provider == "resy" and "Resy cancel returned no confirmation" in str(exc):
                        try:
                            listed = await run_restaurant_cli_json(
                                ctx.deps.settings,
                                ctx.deps.workspace,
                                "list",
                                "--provider",
                                normalized_provider,
                                "--upcoming",
                                "--json",
                                timeout_seconds=30,
                            )
                        except Exception:
                            listed = []
                        rows = listed if isinstance(listed, list) else []
                        still_present = any(str(row.get("id") or "").strip() == reservation_id.strip() for row in rows if isinstance(row, dict))
                        if not still_present:
                            result = {
                                "ok": True,
                                "provider": normalized_provider,
                                "reservationId": reservation_id.strip(),
                                "message": "Cancellation completed even though Resy returned no explicit confirmation token.",
                            }
                        else:
                            logger.warning("restaurant_cancel_reservation degraded provider=%s reservation_id=%s error=%s", provider, reservation_id, exc)
                            return sanitize_tool_output(
                                f"RESTAURANT_TOOL_UNAVAILABLE: reservation cancel failed because {exc}."
                            )
                    else:
                        logger.warning("restaurant_cancel_reservation degraded provider=%s reservation_id=%s error=%s", provider, reservation_id, exc)
                        return sanitize_tool_output(
                            f"RESTAURANT_TOOL_UNAVAILABLE: reservation cancel failed because {exc}."
                        )
                else:
                    result = raw_result if isinstance(raw_result, dict) else {"ok": True, "raw": raw_result}
                if result is None:
                    logger.warning("restaurant_cancel_reservation degraded provider=%s reservation_id=%s empty_result=true", provider, reservation_id)
                    return sanitize_tool_output(
                        "RESTAURANT_TOOL_UNAVAILABLE: reservation cancel failed because the provider returned an empty response."
                    )
                if result.get("ok"):
                    target_record = None
                    if booking_id.strip():
                        target_record = ctx.deps.store.get_booking_record(booking_id.strip())
                    if target_record is None:
                        for record in ctx.deps.store.list_booking_records(limit=50):
                            if (record.external_reference or "").strip() == reservation_id.strip():
                                target_record = record
                                break
                    if target_record is not None:
                        ctx.deps.store.put_booking_record(
                            target_record.model_copy(update={"status": "cancelled"})
                        )
                return sanitize_tool_output(json.dumps(result))

        if allow_browser_tools and not structured_restaurant_task:

            @agent.tool
            async def web_browser_task(ctx: RunContext[AgentDependencies], task: str, max_pages: int = 3, max_steps: int = 12) -> str:
                """Read live web pages when deterministic search/fetch is insufficient or browser interaction is required."""
                bounded_pages = min(max(max_pages, 1), ctx.deps.settings.max_browser_pages)
                bounded_steps = min(max(max_steps, 1), ctx.deps.settings.max_browser_steps)
                try:
                    return await _run_general_browser_task(
                        settings=ctx.deps.settings,
                        workspace=ctx.deps.workspace,
                        task=task,
                        max_pages=bounded_pages,
                        max_steps=bounded_steps,
                        strategy_mode=ctx.deps.strategy_mode,
                    )
                except Exception as exc:
                    logger.warning("web_browser_task degraded task=%s error=%s", task, exc)
                    return sanitize_tool_output(
                        "BROWSER_TASK_UNAVAILABLE: live browser reading failed for this step due to "
                        f"{exc}. Try another source or finish with the information already gathered."
                    )

        @agent.tool
        async def pause_for_input(
            ctx: RunContext[AgentDependencies],
            question: str,
            details: str = "",
            summary: str = "",
            current_step: str = "waiting_for_user_input",
            resume_instructions: str = "",
        ) -> str:
            """Pause the heavy task when a missing user answer, choice, or attachment is required before you can continue."""
            raise PauseForInputRequested(
                question=question,
                details=details,
                summary=summary,
                current_step=current_step,
                resume_instructions=resume_instructions,
            )

        @agent.tool
        async def list_saved_identities(ctx: RunContext[AgentDependencies], limit: int = 20) -> str:
            """List saved identities the operator already configured for reusable account or mailbox flows."""
            records = ctx.deps.store.list_identities(limit=max(1, min(limit, 100)))
            if not records:
                return "No saved identities are configured yet."
            lines = []
            for record in records:
                default_marker = " default" if record.is_default else ""
                scope = record.site_scope or "shared"
                lines.append(f"- {record.identity_id[:8]} | {record.label} | {record.email} | {record.provider} | {scope}{default_marker}")
            return "\n".join(lines)


        @agent.tool
        async def list_saved_bookings(ctx: RunContext[AgentDependencies], limit: int = 10, active_only: bool = True) -> str:
            """List recent saved bookings so follow-up cancel or rebook requests can target a real reservation."""
            records = ctx.deps.store.list_booking_records(limit=max(1, min(limit * 5, 100)))
            filtered: list[BookingRecord] = []
            current_source = ctx.deps.current_job.source.value if ctx.deps.current_job is not None else ""
            current_user = ctx.deps.current_job.user_id if ctx.deps.current_job is not None else ""
            for record in records:
                if active_only and str(record.status or "").lower() in {"cancelled", "canceled", "cancel_completed"}:
                    continue
                job = ctx.deps.store.get_job(record.job_id)
                if current_source and job is not None and job.source.value != current_source:
                    continue
                if current_user and job is not None and (job.user_id or "") != current_user:
                    continue
                filtered.append(record)
                if len(filtered) >= limit:
                    break
            if not filtered:
                return "No saved bookings matched this user yet."
            lines = []
            for record in filtered:
                cancel_marker = "cancelable" if record.can_cancel else "cancel status unknown"
                lines.append(
                    f"- {record.booking_id[:8]} | {record.venue_name or record.site_key} | "
                    f"{record.booking_time or '(time unknown)'} | ref={record.external_reference or '(none)'} | "
                    f"{record.status} | {cancel_marker}"
                )
            return "\n".join(lines)

        @agent.tool
        async def wait_for_email_verification(
            ctx: RunContext[AgentDependencies],
            site_key: str,
            sender_patterns: str = "",
            subject_patterns: str = "",
            otp_regex: str = "",
            identity_id: str = "",
            expires_in_minutes: int = 15,
            summary: str = "waiting for verification email",
        ) -> str:
            """Pause the task while waiting for a verification email; Friday will resume automatically when the OTP arrives through Gmail push."""
            if ctx.deps.current_job is None:
                raise RuntimeError("mailbox verification wait requires a heavy job context")
            mailbox_email = _resolved_secret(ctx.deps.settings, ctx.deps.settings.gmail_account_email_param)
            workflow_id = heavy_workflow_id(ctx.deps.settings, ctx.deps.current_job.job_id)

            def _split_patterns(value: str) -> list[str]:
                return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]

            wait_record = MailboxVerificationWaitRecord(
                workflow_id=workflow_id,
                job_id=ctx.deps.current_job.job_id,
                site_key=site_key.strip() or "generic-login",
                identity_id=identity_id.strip(),
                expected_sender_patterns=_split_patterns(sender_patterns),
                expected_subject_patterns=_split_patterns(subject_patterns),
                otp_regex=_split_patterns(otp_regex),
                created_after=datetime.now(timezone.utc).isoformat(),
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=max(1, expires_in_minutes))).isoformat(),
            )
            ctx.deps.store.put_mailbox_wait(wait_record)
            raise PauseForInputRequested(
                question="I am waiting for the verification email and will resume automatically when it arrives.",
                details=(
                    f"Mailbox: {mailbox_email or 'Friday mailbox not configured'}\n"
                    f"Site: {wait_record.site_key}\n"
                    f"Workflow: {workflow_id}\n"
                    "If the email never arrives, you can still reply manually with 'answer: verification code: <code>'."
                ),
                summary=summary,
                current_step="verification_waiting_email",
                resume_instructions=(
                    "When this task resumes, treat the new input as the verification code or verification email content "
                    "and continue the login or booking flow without asking the same question again."
                ),
            )

        if allow_browser_tools:

            @agent.tool
            async def browser_start(ctx: RunContext[AgentDependencies], start_url: str = "") -> str:
                """Start a persistent browser session for multi-step website actions."""
                assert ctx.deps.browser is not None
                try:
                    return await ctx.deps.browser.start(start_url)
                except Exception as exc:
                    return _browser_tool_warning("browser_start", exc)

            @agent.tool
            async def browser_navigate(ctx: RunContext[AgentDependencies], url: str) -> str:
                """Navigate the persistent browser session to a URL."""
                assert ctx.deps.browser is not None
                try:
                    return await ctx.deps.browser.goto(url)
                except Exception as exc:
                    _handle_phase1_blocking_error(exc, action=f"browser_navigate({url})")
                    return _browser_tool_warning(f"browser_navigate({url})", exc)

            @agent.tool
            async def browser_click(ctx: RunContext[AgentDependencies], selector: str) -> str:
                """Click an element in the persistent browser session using a CSS selector."""
                assert ctx.deps.browser is not None
                try:
                    return await ctx.deps.browser.click(selector)
                except Exception as exc:
                    _handle_phase1_blocking_error(exc, action=f"browser_click({selector})")
                    return _browser_tool_warning(f"browser_click({selector})", exc)

            @agent.tool
            async def browser_type(ctx: RunContext[AgentDependencies], selector: str, text: str, submit: bool = False) -> str:
                """Fill an input in the persistent browser session."""
                assert ctx.deps.browser is not None
                try:
                    return await ctx.deps.browser.type_text(selector, text, submit=submit)
                except Exception as exc:
                    _handle_phase1_blocking_error(exc, action=f"browser_type({selector}, submit={submit})")
                    return _browser_tool_warning(f"browser_type({selector})", exc)

            @agent.tool
            async def browser_press(ctx: RunContext[AgentDependencies], selector: str, key: str) -> str:
                """Press a keyboard key on an element in the persistent browser session."""
                assert ctx.deps.browser is not None
                try:
                    return await ctx.deps.browser.press(selector, key)
                except Exception as exc:
                    _handle_phase1_blocking_error(exc, action=f"browser_press({selector}, {key})")
                    return _browser_tool_warning(f"browser_press({selector}, {key})", exc)

            @agent.tool
            async def browser_read(ctx: RunContext[AgentDependencies], selector: str = "body", limit: int = 3500) -> str:
                """Read visible text from the current page or a selector in the persistent browser session."""
                assert ctx.deps.browser is not None
                try:
                    return await ctx.deps.browser.read(selector=selector, limit=limit)
                except Exception as exc:
                    return _browser_tool_warning(f"browser_read({selector})", exc)

            @agent.tool
            async def browser_wait_for_text(ctx: RunContext[AgentDependencies], text: str, timeout_seconds: int = 10) -> str:
                """Wait for specific text to appear on the current page."""
                assert ctx.deps.browser is not None
                try:
                    return await ctx.deps.browser.wait_for_text(text=text, timeout_seconds=timeout_seconds)
                except Exception as exc:
                    return _browser_tool_warning(f"browser_wait_for_text({text[:60]})", exc)

            @agent.tool
            async def browser_upload_file(ctx: RunContext[AgentDependencies], selector: str, relative_path: str) -> str:
                """Upload a workspace file through a file input selector."""
                assert ctx.deps.browser is not None
                assert ctx.deps.workspace is not None
                try:
                    return await ctx.deps.browser.upload_file(selector, str(ctx.deps.workspace.resolve(relative_path)))
                except Exception as exc:
                    return _browser_tool_warning(f"browser_upload_file({selector}, {relative_path})", exc)

            @agent.tool
            async def browser_list_links(ctx: RunContext[AgentDependencies], limit: int = 20) -> str:
                """List visible links on the current page."""
                assert ctx.deps.browser is not None
                try:
                    return await ctx.deps.browser.list_links(limit=limit)
                except Exception as exc:
                    return _browser_tool_warning("browser_list_links", exc)

            @agent.tool
            async def browser_screenshot(ctx: RunContext[AgentDependencies], relative_path: str = "browser/current-page.png", full_page: bool = True) -> str:
                """Save a screenshot of the current page into the workspace."""
                assert ctx.deps.browser is not None
                try:
                    return await ctx.deps.browser.screenshot(relative_path=relative_path, full_page=full_page)
                except Exception as exc:
                    return _browser_tool_warning(f"browser_screenshot({relative_path})", exc)

            @agent.tool
            async def browser_close(ctx: RunContext[AgentDependencies]) -> str:
                """Close the persistent browser session."""
                assert ctx.deps.browser is not None
                try:
                    await ctx.deps.browser.close()
                    return "Browser session closed."
                except Exception as exc:
                    return _browser_tool_warning("browser_close", exc)

            @agent.tool
            async def browser_save_session(
                ctx: RunContext[AgentDependencies],
                site_scope: str,
                identity_id: str = "",
                status: str = "active",
            ) -> str:
                """Persist the current browser session for future account-gated runs on the same site."""
                assert ctx.deps.browser is not None
                if ctx.deps.current_job is None:
                    raise RuntimeError("saving a browser session requires a heavy job context")
                normalized_site = site_scope.strip()
                if not normalized_site:
                    raise RuntimeError("site_scope is required to save a browser session")
                payload = await ctx.deps.browser.export_session_state()
                fingerprint = payload.get("fingerprint") or {}
                viewport = fingerprint.get("viewport") or {}
                record = BrowserSessionRecord(
                    identity_id=identity_id.strip(),
                    site_scope=normalized_site,
                    session_s3_key=f"browser-sessions/{ctx.deps.current_job.job_id}/{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json",
                    user_agent=str(fingerprint.get("user_agent") or ""),
                    viewport_width=int(viewport.get("width") or 0),
                    viewport_height=int(viewport.get("height") or 0),
                    fingerprint_seed=str(fingerprint.get("seed") or ""),
                    locale=str(fingerprint.get("locale") or "en-US"),
                    timezone_id=str(fingerprint.get("timezone_id") or "America/New_York"),
                    status=status.strip() or "active",
                )
                ctx.deps.settings.s3.put_object(
                    Bucket=ctx.deps.settings.artifacts_bucket,
                    Key=record.session_s3_key,
                    Body=json.dumps(payload).encode("utf-8"),
                    ContentType="application/json",
                )
                ctx.deps.store.put_browser_session(record)
                return sanitize_tool_output(
                    f"Saved browser session {record.session_id[:8]} for {normalized_site} with identity {identity_id.strip() or '(none)'}."
                )

            @agent.tool
            async def browser_restore_session(
                ctx: RunContext[AgentDependencies],
                site_scope: str,
                session_id: str = "",
                start_url: str = "",
            ) -> str:
                """Restore a previously saved browser session for the same site before continuing an account or booking flow."""
                assert ctx.deps.browser is not None
                normalized_site = site_scope.strip()
                if not normalized_site and not session_id.strip():
                    raise RuntimeError("site_scope or session_id is required to restore a browser session")
                _enforce_automation_policy(
                    ctx.deps.store,
                    site_scope=normalized_site,
                    category=_policy_category_for_site(normalized_site),
                    action="login_reuse",
                )
                candidates = ctx.deps.store.list_browser_sessions(limit=100)
                chosen = None
                requested_id = session_id.strip()
                for record in candidates:
                    if requested_id and record.session_id == requested_id:
                        chosen = record
                        break
                    if not requested_id and record.site_scope == normalized_site and record.status == "active":
                        chosen = record
                        break
                if chosen is None:
                    return "No saved browser session matched that site scope yet."
                response = ctx.deps.settings.s3.get_object(Bucket=ctx.deps.settings.artifacts_bucket, Key=chosen.session_s3_key)
                payload = json.loads(response["Body"].read().decode("utf-8"))
                await ctx.deps.browser.restore_session_state(payload, start_url=start_url)
                return sanitize_tool_output(
                    f"Restored browser session {chosen.session_id[:8]} for {chosen.site_scope}."
                )

            @agent.tool
            async def browser_assert_zero_dollar_checkout(ctx: RunContext[AgentDependencies], selector: str = "body", limit: int = 8000) -> str:
                """Inspect the current page text and stop if a payment form, deposit, or non-zero total is visible."""
                assert ctx.deps.browser is not None
                try:
                    page_text = await ctx.deps.browser.read(selector=selector, limit=limit)
                    enforce_zero_dollar_booking(page_text)
                except Exception as exc:
                    _handle_phase1_blocking_error(exc, action=f"browser_assert_zero_dollar_checkout({selector})")
                    raise
                return "No payment form or non-zero total was detected in the inspected page text."

        @agent.tool
        async def record_zero_dollar_booking(
            ctx: RunContext[AgentDependencies],
            site_key: str,
            venue_name: str,
            booking_time: str = "",
            external_reference: str = "",
            identity_id: str = "",
            session_id: str = "",
            can_cancel: bool = False,
        ) -> str:
            """Persist a confirmed $0 booking so it can be reused for later status or cancellation flows."""
            if ctx.deps.current_job is None:
                raise RuntimeError("recording a booking requires a heavy job context")
            normalized_site_key = _canonical_booking_site_key(site_key)
            record = BookingRecord(
                job_id=ctx.deps.current_job.job_id,
                site_key=normalized_site_key,
                identity_id=identity_id.strip(),
                session_id=session_id.strip(),
                external_reference=external_reference.strip(),
                venue_name=venue_name.strip(),
                booking_time=booking_time.strip(),
                booking_total_cents=0,
                can_cancel=can_cancel or ("resy" in normalized_site_key and bool(external_reference.strip())),
                status="created",
            )
            ctx.deps.store.put_booking_record(record)
            return sanitize_tool_output(
                f"Recorded $0 booking {record.booking_id[:8]} for {record.venue_name or record.site_key}."
            )

        @agent.tool
        async def mark_booking_cancelled(
            ctx: RunContext[AgentDependencies],
            booking_id: str,
            external_reference: str = "",
        ) -> str:
            """Mark a previously saved booking as cancelled after a structured or browser cancellation succeeds."""
            record = ctx.deps.store.get_booking_record(booking_id.strip())
            if record is None:
                return "No saved booking matched that booking id."
            updated = record.model_copy(
                update={
                    "status": "cancelled",
                    "external_reference": external_reference.strip() or record.external_reference,
                }
            )
            ctx.deps.store.put_booking_record(updated)
            return sanitize_tool_output(
                f"Marked booking {updated.booking_id[:8]} for {updated.venue_name or updated.site_key} as cancelled."
            )

        if workspace is not None and not structured_restaurant_task:

            @agent.tool
            async def workspace_list_files(ctx: RunContext[AgentDependencies]) -> str:
                """List files available in the current workspace."""
                assert ctx.deps.workspace is not None
                return "\n".join(ctx.deps.workspace.list_files())[:4000]

            @agent.tool
            async def workspace_read_file(ctx: RunContext[AgentDependencies], relative_path: str, limit: int = 6000) -> str:
                """Read a text-like workspace file."""
                assert ctx.deps.workspace is not None
                try:
                    return ctx.deps.workspace.read_text(relative_path, limit=limit)
                except ValueError:
                    return sanitize_tool_output(
                        f"WORKSPACE_PATH_INVALID: {relative_path} escapes the allowed workspace. "
                        "Use a relative path inside the current workspace."
                    )
                except FileNotFoundError:
                    return sanitize_tool_output(
                        f"WORKSPACE_FILE_NOT_FOUND: {relative_path} does not exist yet. "
                        "List workspace files first, or write the file before reading it."
                    )

            @agent.tool
            async def workspace_preview_file(ctx: RunContext[AgentDependencies], relative_path: str, rows: int = 10) -> str:
                """Preview CSV, XLSX, PDF, JSON, or text content from the workspace."""
                assert ctx.deps.workspace is not None
                try:
                    return ctx.deps.workspace.preview_table(relative_path, rows=rows)
                except ValueError:
                    return sanitize_tool_output(
                        f"WORKSPACE_PATH_INVALID: {relative_path} escapes the allowed workspace. "
                        "Use a relative path inside the current workspace."
                    )
                except FileNotFoundError:
                    return sanitize_tool_output(
                        f"WORKSPACE_FILE_NOT_FOUND: {relative_path} does not exist yet. "
                        "List workspace files first, or write the file before previewing it."
                    )

            @agent.tool
            async def workspace_convert_to_markdown(
                ctx: RunContext[AgentDependencies],
                relative_path: str,
                output_relative_path: str = "",
            ) -> str:
                """Convert a workspace document or image into markdown using the wrapped MarkItDown file tool."""
                assert ctx.deps.workspace is not None
                normalized_output = output_relative_path.strip() or None
                try:
                    return ctx.deps.workspace.convert_to_markdown(relative_path, normalized_output)
                except ValueError:
                    return sanitize_tool_output(
                        f"WORKSPACE_PATH_INVALID: {relative_path} escapes the allowed workspace. "
                        "Use a relative path inside the current workspace."
                    )

            @agent.tool
            async def workspace_write_text_file(ctx: RunContext[AgentDependencies], relative_path: str, content: str) -> str:
                """Write or overwrite a text file inside the workspace."""
                assert ctx.deps.workspace is not None
                try:
                    return ctx.deps.workspace.write_text(relative_path, content)
                except ValueError:
                    return sanitize_tool_output(
                        f"WORKSPACE_PATH_INVALID: {relative_path} escapes the allowed workspace. "
                        "Use a relative path inside the current workspace."
                    )

            @agent.tool
            async def workspace_write_pdf_report(ctx: RunContext[AgentDependencies], relative_path: str, title: str, body_text: str) -> str:
                """Create a simple PDF report inside the workspace."""
                assert ctx.deps.workspace is not None
                try:
                    return ctx.deps.workspace.write_pdf(relative_path, title, body_text)
                except ValueError:
                    return sanitize_tool_output(
                        f"WORKSPACE_PATH_INVALID: {relative_path} escapes the allowed workspace. "
                        "Use a relative path inside the current workspace."
                    )

            @agent.tool
            async def workspace_run_shell(ctx: RunContext[AgentDependencies], command: str, timeout_seconds: int = 60) -> str:
                """Run a shell command inside the isolated workspace container."""
                assert ctx.deps.workspace is not None
                return ctx.deps.workspace.run_shell(command, timeout_seconds=timeout_seconds)

            @agent.tool
            async def workspace_run_python(ctx: RunContext[AgentDependencies], code: str, timeout_seconds: int = 60) -> str:
                """Run Python code inside the isolated workspace container."""
                assert ctx.deps.workspace is not None
                return ctx.deps.workspace.run_python(code, timeout_seconds=timeout_seconds)

            @agent.tool
            async def workspace_snapshot(ctx: RunContext[AgentDependencies]) -> str:
                """Summarize the current workspace contents."""
                assert ctx.deps.workspace is not None
                return ctx.deps.workspace.workspace_snapshot()

    try:
        logger.warning(
            "agent_model_run_start mode=%s strategy_mode=%s routing_profile=%s structured_restaurant_task=%s",
            mode,
            strategy_mode,
            routing_profile.name,
            structured_restaurant_task,
        )
        result = await _run_agent_with_model_retries(agent, effective_query, deps=deps)
    finally:
        if deps.browser is not None:
            await deps.browser.close()

    raw_usage = result.usage() if callable(getattr(result, "usage", None)) else getattr(result, "usage", None)
    usage = usage_from_pydantic_ai(raw_usage)
    cost = estimate_deepseek_cost(
        usage,
        input_cache_miss_per_1m=settings.deepseek_input_cache_miss_per_1m,
        input_cache_hit_per_1m=settings.deepseek_input_cache_hit_per_1m,
        output_per_1m=settings.deepseek_output_per_1m,
    )
    spend_target.add_spend(cost)
    logger.info(
        "agent completed request mode=%s input_tokens=%s output_tokens=%s cache_hits=%s cost_usd=%s",
        mode,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        cost,
    )
    output = getattr(result, "output", None)
    output_text = str(output or "")
    if mode == "heavy":
        pause_payload = _booking_choice_pause_payload(output_text, routing_profile.name)
        if pause_payload is not None:
            raise PauseForInputRequested(**pause_payload)
    return AgentResult(text=output_text, cost_usd=str(cost.quantize(Decimal("0.000001"))))
