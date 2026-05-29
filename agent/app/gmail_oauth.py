from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from typing import Any, Iterable

import httpx

from .settings import Settings

GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
OTP_PATTERNS = (
    re.compile(r"\b(\d{4,8})\b"),
    re.compile(r"\b([A-Z0-9]{6,10})\b"),
)


@dataclass(frozen=True)
class GmailPushEvent:
    delivery_id: str
    email_address: str
    history_id: str


def _google_secret(settings: Settings, parameter_name: str) -> str:
    value = settings.secret(parameter_name).strip()
    if not value:
        raise RuntimeError(f"missing required Google OAuth secret: {parameter_name}")
    return value


async def mint_gmail_access_token(settings: Settings) -> dict[str, Any]:
    client_id = _google_secret(settings, settings.google_client_id_param)
    client_secret = _google_secret(settings, settings.google_client_secret_param)
    refresh_token = _google_secret(settings, settings.google_refresh_token_param)
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            GMAIL_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        response.raise_for_status()
        payload = response.json()
    if not payload.get("access_token"):
        raise RuntimeError("gmail oauth token mint returned no access_token")
    return payload


async def gmail_api_get(settings: Settings, path: str, *, access_token: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"{GMAIL_API_BASE}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()


async def gmail_api_post(settings: Settings, path: str, *, access_token: str, json_body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{GMAIL_API_BASE}{path}",
            json=json_body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()


def decode_pubsub_push_body(payload: dict[str, Any]) -> GmailPushEvent:
    message = payload.get("message") or {}
    delivery_id = str(message.get("messageId") or "").strip()
    encoded_data = str(message.get("data") or "").strip()
    if not delivery_id or not encoded_data:
        raise ValueError("pubsub push body was missing messageId or data")
    decoded = base64.b64decode(encoded_data + "=" * (-len(encoded_data) % 4))
    parsed = json.loads(decoded.decode("utf-8"))
    email_address = str(parsed.get("emailAddress") or "").strip()
    history_id = str(parsed.get("historyId") or "").strip()
    if not email_address or not history_id:
        raise ValueError("gmail push event missing emailAddress or historyId")
    return GmailPushEvent(delivery_id=delivery_id, email_address=email_address, history_id=history_id)


async def renew_gmail_watch(settings: Settings) -> dict[str, Any]:
    token = await mint_gmail_access_token(settings)
    topic_name = settings.gmail_pubsub_topic_name.strip()
    if not topic_name:
        raise RuntimeError("missing gmail pubsub topic name")
    label_ids = [value.strip() for value in settings.gmail_watch_label_ids.split(",") if value.strip()]
    body: dict[str, Any] = {"topicName": topic_name}
    if label_ids:
        body["labelIds"] = label_ids
    if settings.gmail_watch_label_filter_action.strip():
        body["labelFilterAction"] = settings.gmail_watch_label_filter_action.strip()
    return await gmail_api_post(settings, "/watch", access_token=token["access_token"], json_body=body)


async def list_history_message_ids(settings: Settings, *, access_token: str, history_id: str) -> list[str]:
    payload = await gmail_api_get(
        settings,
        "/history",
        access_token=access_token,
        params={"startHistoryId": history_id, "historyTypes": "messageAdded"},
    )
    message_ids: list[str] = []
    seen: set[str] = set()
    for item in payload.get("history", []) or []:
        for message in item.get("messagesAdded", []) or []:
            raw = message.get("message") or {}
            label_ids = {str(label).strip().upper() for label in (raw.get("labelIds") or []) if str(label).strip()}
            if "DRAFT" in label_ids:
                continue
            message_id = str(raw.get("id") or "").strip()
            if message_id and message_id not in seen:
                seen.add(message_id)
                message_ids.append(message_id)
    return message_ids


def _decode_message_part_body(data: str) -> str:
    if not data:
        return ""
    decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        parsed = BytesParser(policy=policy.default).parsebytes(decoded)
    except Exception:
        return decoded.decode("utf-8", errors="ignore")
    if parsed.is_multipart():
        return "\n".join(
            part.get_content()
            for part in parsed.walk()
            if part.get_content_type() in {"text/plain", "text/html"} and not part.is_multipart()
        )
    return parsed.get_content()


def gmail_message_text(message: dict[str, Any]) -> str:
    payload = message.get("payload") or {}
    texts: list[str] = []

    def collect(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        body = part.get("body") or {}
        data = str(body.get("data") or "")
        if data and mime_type in {"text/plain", "text/html", ""}:
            texts.append(_decode_message_part_body(data))
        for child in part.get("parts", []) or []:
            collect(child)

    collect(payload)
    snippet = str(message.get("snippet") or "").strip()
    if snippet:
        texts.append(snippet)
    return "\n".join(text for text in texts if text).strip()


def gmail_message_headers(message: dict[str, Any]) -> dict[str, str]:
    headers = (message.get("payload") or {}).get("headers") or []
    normalized: dict[str, str] = {}
    for item in headers:
        name = str(item.get("name") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if name:
            normalized[name] = value
    return normalized


def extract_otp_codes(text: str) -> list[str]:
    normalized = " ".join((text or "").split())
    seen: set[str] = set()
    codes: list[str] = []
    for pattern in OTP_PATTERNS:
        for match in pattern.finditer(normalized):
            code = match.group(1).strip()
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
    return codes


def first_matching_code(text: str, patterns: Iterable[str]) -> str:
    normalized = text or ""
    for raw_pattern in patterns:
        pattern = raw_pattern.strip()
        if not pattern:
            continue
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return (match.group(1) if match.groups() else match.group(0)).strip()
    extracted = extract_otp_codes(normalized)
    return extracted[0] if extracted else ""
