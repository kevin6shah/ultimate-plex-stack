from __future__ import annotations

import asyncio
import json
import os
import signal
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

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
