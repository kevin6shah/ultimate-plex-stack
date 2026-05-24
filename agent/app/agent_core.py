from __future__ import annotations

import json
import os
import logging
import re
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
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
    normalize_restaurant_provider,
    run_restaurant_cli,
    run_restaurant_cli_json,
)
from .routing import needs_confirmation, task_routing_profile
from .skiplagged import call_skiplagged_tool
from .stagehand_runner import run_stagehand_task
from .settings import Settings
from .storage import StateStore
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


def _resolved_secret(settings: Settings, parameter_name: str) -> str:
    if not parameter_name:
        return ""
    value = settings.secret(parameter_name).strip()
    if not value:
        return ""
    if not parameter_name.startswith("/") and value == parameter_name and parameter_name.isupper():
        return ""
    return value


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
            return (
                "This is a structured reservation task for restaurants. Use restaurant_search first to find the venue, "
                "prefer restaurant_find_availability for the full search-plus-slots flow, and only use restaurant_book_or_handoff for an approved booking or manual handoff step. "
                "For read-only availability checks, stay on the structured restaurant tools. "
                "If the user asks to cancel or modify an existing reservation, first inspect saved bookings and current reservations, then use the saved browser session or a fresh login flow only if the structured tools cannot finish the account step cleanly. "
                "If the structured path cannot verify live availability, return the structured result plus a clean handoff path instead of drifting into browser automation."
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


def _phase1_booking_runtime_guidance(query: str, routing_profile_name: str) -> str:
    lowered = query.lower()
    if routing_profile_name != "booking_commerce" and not any(
        token in lowered for token in ("login", "sign in", "account", "book", "booking", "reservation", "cancel")
    ):
        return ""
    return (
        "Phase 1 booking/account rules:\n"
        "- Friday may only autonomously complete $0 bookings in this flow. If a payment form, deposit, hold, fee, or non-zero total appears, stop immediately.\n"
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
    return "cancel" in lowered and any(
        token in lowered for token in ("booking", "reservation", "restaurant", "resy", "opentable", "seating", "table")
    )


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


def _maybe_raise_booking_cancellation_pause(store: StateStore, query: str, routing_profile_name: str) -> None:
    if not _is_booking_cancellation_followup(query, routing_profile_name):
        return
    record = _latest_relevant_booking_record(store, query)
    if record is None:
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


def _restaurant_booking_missing_details(query: str, routing_profile_name: str) -> list[str]:
    if routing_profile_name != "booking_commerce":
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
    if not re.search(r"\b(?:party of|table for|for)\s+\d+\b", lowered) and not re.search(r"\b\d+\s+(?:people|persons|person|guests)\b", lowered):
        missing.append("party size")

    has_date = bool(
        re.search(r"\b(today|tomorrow|tonight|this (?:morning|afternoon|evening)|next (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", lowered)
        or re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered)
        or re.search(r"\b\d{4}-\d{2}-\d{2}\b", lowered)
        or re.search(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", lowered)
        or re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b", lowered)
    )
    if not has_date:
        missing.append("date")

    has_time = bool(
        re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", lowered)
        or re.search(r"\b\d{1,2}:\d{2}\b", lowered)
        or re.search(r"\b(noon|midnight)\b", lowered)
    )
    if not has_time:
        missing.append("time")
    return missing


def _select_model(query: str, settings: Settings) -> str:
    lowered = query.lower()
    if any(token in lowered for token in ("think deeply", "reason", "plan carefully", "complex")):
        return settings.reasoner_model
    return settings.agent_model


async def _run_travel_browser_fallback(
    *,
    settings: Settings,
    workspace: Optional[Workspace],
    task: str,
    max_pages: int = 2,
    max_steps: int = 8,
) -> str:
    scoped_task = (
        "Travel fallback mode. The structured travel tools were unavailable or rate-limited. "
        "Use Google Travel / Google Flights / Google Hotels, Kayak, or another mainstream travel source only as needed. "
        "Stay tightly scoped and summarize the best options clearly.\n\n"
        + task
    )
    public_web_result = await run_browser_task(
        scoped_task,
        max_pages=max_pages,
        max_steps=max_steps,
    )
    if not _browser_fallback_failed(public_web_result):
        return public_web_result
    if workspace is None:
        raise RuntimeError(public_web_result)

    stagehand_result = await run_stagehand_task(
        (
            "Travel interaction fallback. The direct travel tools were unavailable or rate-limited, "
            "and the lightweight public-web pass could not gather enough data. "
            "Use at most one or two mainstream travel sites, keep steps bounded, and return the best live options you can verify.\n\n"
            + task
        ),
        max_steps=max(10, min(max_steps * 2, 16)),
        settings=settings,
        workspace=workspace,
    )
    if not _browser_fallback_failed(stagehand_result):
        return stagehand_result

    if not settings.browser_use_enabled:
        raise RuntimeError(
            "public-web and stagehand travel fallback failed. "
            f"Public-web result: {public_web_result}. "
            f"Stagehand result: {stagehand_result}."
        )

    interaction_result = await run_browser_use_task(
        (
            "Last-resort travel interaction mode. The direct travel tools were unavailable or rate-limited, "
            "the lightweight public-web pass was insufficient, and the Stagehand browser pass did not finish cleanly. "
            "Use at most one or two mainstream travel sites, keep steps bounded, avoid loops, "
            "and return the best live options you can find.\n\n"
            + task
        ),
        max_pages=max(1, min(max_pages, 2)),
        max_steps=max(10, min(max_steps * 2, 16)),
        settings=settings,
        workspace=workspace,
        enable_optional_mcps=False,
    )
    if _browser_fallback_failed(interaction_result):
        raise RuntimeError(
            "public-web fallback failed, Stagehand fallback failed, and last-resort browser interaction also failed. "
            f"Public-web result: {public_web_result}. "
            f"Stagehand result: {stagehand_result}. "
            f"Interaction result: {interaction_result}."
        )
    return interaction_result


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
) -> str:
    public_web_result = await run_browser_task(
        task,
        max_pages=max_pages,
        max_steps=max_steps,
    )
    if not _browser_fallback_failed(public_web_result):
        return public_web_result
    if workspace is None:
        return public_web_result
    stagehand_result = await run_stagehand_task(
        (
            "Interactive browser escalation mode. The lightweight public-web pass could not gather enough information. "
            "Use browser automation only as needed, keep steps bounded, and finish with a concise summary.\n\n"
            + task
        ),
        max_steps=max_steps,
        settings=settings,
        workspace=workspace,
    )
    if not _browser_fallback_failed(stagehand_result):
        return stagehand_result
    if not settings.browser_use_enabled:
        return stagehand_result
    return await run_browser_use_task(
        (
            "Last-resort browser escalation mode. The lightweight public-web pass was insufficient and the Stagehand browser pass did not finish cleanly. "
            "Use browser automation only as needed, keep the steps bounded, and finish with a concise summary.\n\n"
            + task
        ),
        max_pages=max_pages,
        max_steps=max_steps,
        settings=settings,
        workspace=workspace,
        enable_optional_mcps=False,
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
    allow_browser_tools = _should_expose_browser_tools(query, routing_profile.name)
    allow_travel_browser_fallback = _should_expose_travel_browser_fallback(query, routing_profile.name)
    structured_restaurant_task = _is_structured_restaurant_task(query, routing_profile.name)
    missing_restaurant_booking_details = _restaurant_booking_missing_details(query, routing_profile.name)
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
        _ensure_default_mailbox_identity(settings, store)
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

    deps = AgentDependencies(
        settings=settings,
        store=store,
        workspace=workspace,
        browser=BrowserSession(workspace=workspace, settings=settings) if mode == "heavy" else None,
        current_job=current_job,
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
                """Search for a restaurant venue and then fetch live availability for the best match in one step. Prefer this for restaurant reservation research."""
                assert ctx.deps.workspace is not None
                normalized_provider = normalize_restaurant_provider(provider)
                search_args = [
                    "search",
                    query,
                    "--limit",
                    str(max(1, min(limit, 25))),
                    "--provider",
                    normalized_provider,
                    "--agent",
                ]
                if city.strip():
                    search_args.extend(["--city", city.strip()])
                try:
                    search_payload = await run_restaurant_cli_json(
                        ctx.deps.settings,
                        ctx.deps.workspace,
                        *search_args,
                        timeout_seconds=25,
                    )
                except Exception as exc:
                    logger.warning("restaurant_find_availability search degraded query=%s provider=%s error=%s", query, provider, exc)
                    return sanitize_tool_output(
                        f"RESTAURANT_TOOL_UNAVAILABLE: restaurant search failed because {exc}."
                    )
                results = list(search_payload.get("results") or []) if isinstance(search_payload, dict) else []
                failures = list(search_payload.get("failures") or []) if isinstance(search_payload, dict) else []
                best_match = choose_best_restaurant_result(query, results)
                if best_match is None:
                    return sanitize_tool_output(
                        f"I could not find a matching {normalized_provider.title()} venue for {query}."
                    )
                venue_id = str(best_match.get("id") or "").strip()
                venue_name = str(best_match.get("name") or query).strip() or query
                venue_city = str(best_match.get("city") or "").strip()
                venue_url = str(best_match.get("url") or "").strip()
                if not venue_id:
                    return sanitize_tool_output(
                        f"I found a likely match for {venue_name}, but I could not resolve a usable venue id for live availability."
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
                        provider,
                        exc,
                    )
                    return sanitize_tool_output(
                        (
                            f"I matched {venue_name}"
                            + (f" in {venue_city}" if venue_city else "")
                            + f" on {normalized_provider.title()}, but I could not verify live availability for {date}."
                            + (f"\nBooking page: {venue_url}" if venue_url else "")
                        )
                    )
                slots = slots_payload if isinstance(slots_payload, list) else []
                lines = [
                    f"Matched venue: {venue_name}"
                    + (f" ({venue_city})" if venue_city else "")
                    + f" on {normalized_provider.title()}",
                ]
                if venue_url:
                    lines.append(f"Booking page: {venue_url}")
                if slots:
                    lines.append(f"Live availability for {date} for {max(1, party_size)} people:")
                    for slot in slots[:12]:
                        slot_time = str(slot.get('time') or '').strip()
                        slot_type = str(slot.get('type') or '').strip()
                        if slot_time:
                            lines.append(f"- {slot_time}" + (f" ({slot_type})" if slot_type else ""))
                else:
                    lines.append(f"No live slots were returned for {date} for {max(1, party_size)} people.")
                if failures:
                    provider_labels = ", ".join(str(item.get("provider") or "provider") for item in failures[:3])
                    lines.append(f"Other provider lookups also had issues: {provider_labels}.")
                return sanitize_tool_output("\n".join(lines))

            if not structured_restaurant_task:

                @agent.tool
                async def restaurant_search(
                    ctx: RunContext[AgentDependencies],
                    query: str,
                    provider: str = "resy",
                    city: str = "",
                    limit: int = 10,
                ) -> str:
                    """Search restaurant venues with Resy by default. Use OpenTable only when explicitly requested."""
                    assert ctx.deps.workspace is not None
                    normalized_provider = normalize_restaurant_provider(provider)
                    args = ["search", query, "--limit", str(max(1, min(limit, 25))), "--agent"]
                    args.extend(["--provider", normalized_provider])
                    if city.strip():
                        args.extend(["--city", city.strip()])
                    try:
                        result = await run_restaurant_cli(
                            ctx.deps.settings,
                            ctx.deps.workspace,
                            *args,
                            timeout_seconds=25,
                        )
                    except Exception as exc:
                        logger.warning("restaurant_search degraded query=%s provider=%s error=%s", query, provider, exc)
                        return sanitize_tool_output(
                            f"RESTAURANT_TOOL_UNAVAILABLE: restaurant search failed because {exc}."
                        )
                    return sanitize_tool_output(result)

                @agent.tool
                async def restaurant_availability(
                    ctx: RunContext[AgentDependencies],
                    venue_id: str,
                    date: str,
                    party_size: int = 2,
                    provider: str = "resy",
                ) -> str:
                    """Look up restaurant reservation availability for a venue and date using restaurant-cli."""
                    assert ctx.deps.workspace is not None
                    normalized_provider = normalize_restaurant_provider(provider)
                    args = [
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
                        result = await run_restaurant_cli(
                            ctx.deps.settings,
                            ctx.deps.workspace,
                            *args,
                            timeout_seconds=35,
                        )
                    except Exception as exc:
                        logger.warning("restaurant_availability degraded venue_id=%s provider=%s error=%s", venue_id, provider, exc)
                        return sanitize_tool_output(
                            f"RESTAURANT_TOOL_UNAVAILABLE: availability lookup failed because {exc}."
                        )
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
            ) -> str:
                """Book a Resy reservation or return a manual OpenTable handoff URL. Use only after explicit user approval."""
                assert ctx.deps.workspace is not None
                normalized_provider = normalize_restaurant_provider(provider)
                if normalized_provider == "opentable":
                    return sanitize_tool_output(
                        json.dumps(
                            {
                                "ok": True,
                                "provider": "opentable",
                                "handoff": True,
                                "message": "OpenTable booking must be completed manually by the user.",
                                "url": build_opentable_booking_url(
                                    restaurant_id=venue_id,
                                    date=date,
                                    time=time,
                                    party_size=max(1, party_size),
                                ),
                            }
                        )
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
                return sanitize_tool_output(result)

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

        if allow_browser_tools:

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
            record = BookingRecord(
                job_id=ctx.deps.current_job.job_id,
                site_key=site_key.strip() or "generic-booking",
                identity_id=identity_id.strip(),
                session_id=session_id.strip(),
                external_reference=external_reference.strip(),
                venue_name=venue_name.strip(),
                booking_time=booking_time.strip(),
                booking_total_cents=0,
                can_cancel=can_cancel or ("resy" in site_key.strip().lower() and bool(external_reference.strip())),
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

        if workspace is not None:

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
                return ctx.deps.workspace.convert_to_markdown(relative_path, normalized_output)

            @agent.tool
            async def workspace_write_text_file(ctx: RunContext[AgentDependencies], relative_path: str, content: str) -> str:
                """Write or overwrite a text file inside the workspace."""
                assert ctx.deps.workspace is not None
                return ctx.deps.workspace.write_text(relative_path, content)

            @agent.tool
            async def workspace_write_pdf_report(ctx: RunContext[AgentDependencies], relative_path: str, title: str, body_text: str) -> str:
                """Create a simple PDF report inside the workspace."""
                assert ctx.deps.workspace is not None
                return ctx.deps.workspace.write_pdf(relative_path, title, body_text)

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
