from __future__ import annotations

import asyncio
import json
import os
import signal
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .settings import Settings
from .workspace import Workspace


def _is_retryable_restaurant_error(message: str) -> bool:
    normalized = (message or "").lower()
    return any(
        token in normalized
        for token in (
            "timed out",
            "timeout",
            "429",
            "502",
            "503",
            "504",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
            "connection closed",
            "network",
        )
    )


def _restaurant_cli_env(settings: Settings, workspace: Workspace) -> dict[str, str]:
    home = str(workspace.root)
    env = {
        "HOME": home,
        "XDG_CONFIG_HOME": str(workspace.root / ".config"),
        "RESTAURANT_CLI_AGENT": "1",
        "RESTAURANT_CLI_OT_MODE": settings.restaurant_cli_ot_mode,
        "PATH": os.environ.get("PATH", ""),
    }
    resy_auth_token = settings.secret(settings.resy_auth_token_param)
    if resy_auth_token:
        env["RESY_AUTH_TOKEN"] = resy_auth_token
    return env


def _restaurant_cli_config_doc(settings: Settings) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    resy_auth_token = settings.secret(settings.resy_auth_token_param)
    resy_api_key = settings.secret(settings.resy_api_key_param)
    if resy_auth_token:
        providers["resy"] = {
            "tokenRef": {"source": "env", "id": "RESY_AUTH_TOKEN"},
        }
        if resy_api_key:
            providers["resy"]["apiKey"] = resy_api_key
    return {
        "version": 1,
        "defaults": {
            "provider": "resy",
            "partySize": 2,
            "timezone": settings.restaurant_cli_timezone,
        },
        "providers": providers,
        "scheduler": {"backend": "at"},
        "logging": {"level": "info"},
    }


def ensure_restaurant_cli_state(settings: Settings, workspace: Workspace) -> Path:
    config_dir = workspace.root / ".config" / "restaurant-cli"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(json.dumps(_restaurant_cli_config_doc(settings), indent=2), encoding="utf-8")
    return config_path


def build_opentable_booking_url(*, restaurant_id: str, date: str, time: str, party_size: int) -> str:
    return "https://www.opentable.com/restref/client?" + urlencode(
        {
            "rid": str(restaurant_id),
            "restref": str(restaurant_id),
            "partysize": str(party_size),
            "datetime": f"{date}T{time}",
        }
    )


def normalize_restaurant_provider(provider: str, *, default: str = "resy") -> str:
    normalized = provider.strip().lower()
    if not normalized:
        return default
    return normalized


@dataclass(frozen=True)
class RestaurantSlotPolicy:
    provider: str
    slot_token: str
    time: str
    slot_type: str = ""
    payment_is_paid: Optional[bool] = None
    cancellation_fee: Optional[float] = None
    deposit_fee: Optional[float] = None
    service_charge: Optional[float] = None
    secs_cancel_cut_off: Optional[int] = None
    secs_change_cut_off: Optional[int] = None
    free_cancellation: bool = False
    requires_manual_confirmation: bool = False
    policy_text: str = ""


def _coerce_optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _hours_from_seconds(seconds: Optional[int]) -> Optional[int]:
    if not seconds or seconds <= 0:
        return None
    return max(1, round(seconds / 3600))


def _format_resy_slot_policy(slot: dict[str, Any]) -> RestaurantSlotPolicy:
    config = slot.get("config") or {}
    payment = slot.get("payment") or {}
    token = str(config.get("token") or "").strip()
    slot_type = str(config.get("type") or "").strip()
    time_value = ""
    start_raw = str((slot.get("date") or {}).get("start") or "").strip()
    match = re.search(r"\b(\d{2}:\d{2})(?::\d{2})?\b", start_raw)
    if match:
        time_value = match.group(1)
    cancellation_fee = _coerce_optional_float(payment.get("cancellation_fee"))
    deposit_fee = _coerce_optional_float(payment.get("deposit_fee"))
    service_charge = _coerce_optional_float(payment.get("service_charge"))
    secs_cancel_cut_off = _coerce_optional_int(payment.get("secs_cancel_cut_off"))
    secs_change_cut_off = _coerce_optional_int(payment.get("secs_change_cut_off"))
    payment_is_paid = payment.get("is_paid")
    has_nonzero_cancellation_fee = cancellation_fee is not None and cancellation_fee > 0
    has_nonzero_deposit = deposit_fee is not None and deposit_fee > 0
    has_nonzero_service_charge = service_charge is not None and service_charge > 0
    requires_manual_confirmation = has_nonzero_cancellation_fee or has_nonzero_deposit or has_nonzero_service_charge
    free_cancellation = not requires_manual_confirmation
    policy_parts: list[str] = []
    cutoff_hours = _hours_from_seconds(secs_cancel_cut_off)
    if has_nonzero_cancellation_fee:
        if cutoff_hours is not None:
            policy_parts.append(f"Cancellation fee: ${cancellation_fee:.2f} if cancelled within {cutoff_hours} hours.")
        else:
            policy_parts.append(f"Cancellation fee: ${cancellation_fee:.2f}.")
    elif cancellation_fee == 0:
        policy_parts.append("Free cancellation: no cancellation fee shown.")
    elif payment_is_paid:
        policy_parts.append("No cancellation fee is shown, but the slot still uses a card-on-file payment policy.")
    else:
        policy_parts.append("No cancellation fee is shown in the current Resy slot data.")
    if has_nonzero_deposit:
        policy_parts.append(f"Deposit required: ${deposit_fee:.2f}.")
    if has_nonzero_service_charge:
        policy_parts.append(f"Service charge required: ${service_charge:.2f}.")
    if secs_change_cut_off and secs_change_cut_off > 0:
        change_hours = _hours_from_seconds(secs_change_cut_off)
        if change_hours is not None:
            policy_parts.append(f"Changes lock within {change_hours} hours of the reservation.")
    return RestaurantSlotPolicy(
        provider="resy",
        slot_token=token,
        time=time_value,
        slot_type=slot_type,
        payment_is_paid=payment_is_paid if isinstance(payment_is_paid, bool) else None,
        cancellation_fee=cancellation_fee,
        deposit_fee=deposit_fee,
        service_charge=service_charge,
        secs_cancel_cut_off=secs_cancel_cut_off,
        secs_change_cut_off=secs_change_cut_off,
        free_cancellation=free_cancellation,
        requires_manual_confirmation=requires_manual_confirmation,
        policy_text=" ".join(part for part in policy_parts if part).strip(),
    )


def _fetch_resy_slot_policies_sync(
    settings: Settings,
    *,
    venue_id: str,
    date: str,
    party_size: int,
) -> dict[str, RestaurantSlotPolicy]:
    auth_token = settings.secret(settings.resy_auth_token_param)
    if not auth_token:
        raise RuntimeError("Missing Resy auth token for slot policy inspection.")
    api_key = settings.secret(settings.resy_api_key_param) or "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"
    query = urlencode(
        {
            "lat": "0",
            "long": "0",
            "day": date,
            "party_size": str(max(1, party_size)),
            "venue_id": venue_id,
        }
    )
    req = Request(
        f"https://api.resy.com/4/find?{query}",
        headers={
            "Authorization": f'ResyAPI api_key="{api_key}"',
            "X-Resy-Auth-Token": auth_token,
            "X-Resy-Universal-Auth": auth_token,
            "User-Agent": "restaurant-cli/0.1.0 (+https://github.com/omarshahine/restaurant-cli)",
            "Accept": "application/json, text/plain, */*",
        },
        method="GET",
    )
    with urlopen(req, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    venues = ((payload or {}).get("results") or {}).get("venues") or []
    policies: dict[str, RestaurantSlotPolicy] = {}
    for venue in venues:
        for slot in venue.get("slots") or []:
            policy = _format_resy_slot_policy(slot)
            if policy.slot_token:
                policies[policy.slot_token] = policy
    return policies


async def fetch_resy_slot_policies(
    settings: Settings,
    *,
    venue_id: str,
    date: str,
    party_size: int,
) -> dict[str, RestaurantSlotPolicy]:
    return await asyncio.to_thread(
        _fetch_resy_slot_policies_sync,
        settings,
        venue_id=venue_id,
        date=date,
        party_size=party_size,
    )


async def run_restaurant_cli(
    settings: Settings,
    workspace: Workspace,
    *args: str,
    env_overrides: Optional[dict[str, str]] = None,
    timeout_seconds: int = 120,
) -> str:
    ensure_restaurant_cli_state(settings, workspace)
    env = os.environ.copy()
    env.update(_restaurant_cli_env(settings, workspace))
    if env_overrides:
        env.update(env_overrides)
    last_error = "restaurant-cli failed"
    for attempt in range(1, 3):
        process = await asyncio.create_subprocess_exec(
            settings.restaurant_cli_command,
            *args,
            cwd=str(workspace.root),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()
            last_error = f"restaurant-cli timed out after {timeout_seconds}s"
            if attempt >= 2:
                raise RuntimeError(last_error)
            await asyncio.sleep(2)
            continue
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode == 0:
            if stderr_text and not stdout_text:
                return stderr_text
            return stdout_text
        last_error = stderr_text or stdout_text or f"restaurant-cli exited with status {process.returncode}"
        if attempt >= 2 or not _is_retryable_restaurant_error(last_error):
            raise RuntimeError(last_error)
        await asyncio.sleep(2)
    raise RuntimeError(last_error)


async def run_restaurant_cli_json(
    settings: Settings,
    workspace: Workspace,
    *args: str,
    env_overrides: Optional[dict[str, str]] = None,
    timeout_seconds: int = 120,
) -> Any:
    raw = await run_restaurant_cli(
        settings,
        workspace,
        *args,
        env_overrides=env_overrides,
        timeout_seconds=timeout_seconds,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"restaurant-cli did not return valid JSON: {exc}") from exc


def choose_best_restaurant_result(query: str, results: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not results:
        return None
    normalized_query = re.sub(r"\s+", " ", query.strip().lower())

    def _clean_phrase(value: str) -> str:
        cleaned = value.lower()
        cleaned = re.sub(r"https?://\S+", " ", cleaned)
        cleaned = re.sub(
            r"\b(help me|make|find|get|available|availability|dinner|lunch|brunch|reservation|reservations|book|booking|restaurant|restaurants|for|at|in|near|nyc|new york|tonight|tomorrow|people|person|party|the)\b",
            " ",
            cleaned,
        )
        cleaned = re.sub(r"\b\d{1,2}(:\d{2})?\s*(am|pm)?\b", " ", cleaned)
        cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _tokens(value: str) -> list[str]:
        return [token for token in _clean_phrase(value).split() if len(token) >= 3]

    query_core = _clean_phrase(normalized_query)
    query_tokens = _tokens(normalized_query)
    cuisine_keywords = {token for token in query_tokens if token in {"indian", "italian", "japanese", "thai", "mexican", "korean", "chinese", "greek", "french"}}

    def _score(item: dict[str, Any]) -> tuple[int, int, int]:
        name = str(item.get("name") or "").strip().lower()
        city = str(item.get("city") or "").strip().lower()
        cuisine = " ".join(
            str(item.get(key) or "").strip().lower()
            for key in ("cuisine", "cuisines", "category", "categories", "description")
        )
        name_core = _clean_phrase(name)
        name_tokens = _tokens(name)
        score = 0
        if query_core and name_core == query_core:
            score += 100
        if query_core and name_core.startswith(query_core):
            score += 40
        if query_core and query_core in name_core:
            score += 20
        overlap = len(set(query_tokens) & set(name_tokens))
        score += overlap * 15
        if cuisine_keywords and any(keyword in cuisine for keyword in cuisine_keywords):
            score += 20
        if "new york" in normalized_query or "nyc" in normalized_query:
            if city in {"new york", "nyc", "manhattan", "brooklyn", "queens"}:
                score += 5
        extra_name_tokens = max(0, len(name_tokens) - overlap)
        score -= extra_name_tokens * 2
        return (score, -extra_name_tokens, -len(name))

    exact_name_matches = [
        item for item in results
        if query_core and _clean_phrase(str(item.get("name") or "")) == query_core
    ]
    if exact_name_matches:
        return exact_name_matches[0]
    return max(results, key=_score)
