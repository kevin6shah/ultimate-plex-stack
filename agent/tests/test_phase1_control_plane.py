from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import types
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.main as main_module
from app.jobs import DashboardSessionRecord, MailboxVerificationWaitRecord


def _telegram_hash(payload: dict[str, str], bot_token: str) -> str:
    data_check = "\n".join(
        f"{key}={value}"
        for key, value in sorted((key, value) for key, value in payload.items() if key != "hash" and value is not None)
    )
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    return hmac.new(secret_key, data_check.encode("utf-8"), hashlib.sha256).hexdigest()


def test_dashboard_auth_telegram_sets_signed_cookie(monkeypatch) -> None:
    created: list[DashboardSessionRecord] = []

    class FakeStore:
        def create_dashboard_session(self, record: DashboardSessionRecord) -> DashboardSessionRecord:
            created.append(record)
            return record

    secret_values = {
        main_module.settings.telegram_bot_token_param: "bot-token",
        main_module.settings.dashboard_session_secret_param: "dashboard-secret",
    }
    monkeypatch.setattr(type(main_module.settings), "secret", lambda self, parameter_name: secret_values.get(parameter_name, ""))
    monkeypatch.setattr(main_module, "store", lambda: FakeStore())
    monkeypatch.setattr(main_module, "_telegram_login_is_valid", lambda _payload: True)
    monkeypatch.setattr(main_module, "_encode_dashboard_session_cookie", lambda session_id: f"{session_id}.sig")

    auth_date = str(int(datetime.now(timezone.utc).timestamp()))
    payload = {
        "id": "12345",
        "first_name": "Kevin",
        "username": "kevinshah",
        "auth_date": auth_date,
    }
    payload["hash"] = _telegram_hash(payload, "bot-token")

    client = TestClient(main_module.app)
    response = client.get("/dashboard/auth/telegram", params=payload, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard/jobs"
    assert created
    assert response.cookies.get("friday_dashboard_session")


def test_gmail_pubsub_ingress_signals_matching_wait(monkeypatch) -> None:
    matched: dict[str, str] = {}
    saved_watch_states = []

    wait = MailboxVerificationWaitRecord(
        workflow_id="wf-123",
        job_id="job-123",
        site_key="resy",
        expected_sender_patterns=[r"resy"],
        expected_subject_patterns=[r"code"],
        otp_regex=[r"\b(\d{6})\b"],
        created_after=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )

    class FakeStore:
        def claim_pubsub_delivery(self, delivery_id: str) -> bool:
            assert delivery_id == "pubsub-1"
            return True

        def get_mailbox_watch_state(self, mailbox_email: str):
            return None

        def put_mailbox_watch_state(self, state):
            saved_watch_states.append(state)
            return state

        def list_active_mailbox_waits(self, limit: int = 50):
            return [wait]

        def claim_mailbox_wait_message(self, *, wait_id: str, gmail_message_id: str) -> bool:
            assert wait_id == wait.wait_id
            assert gmail_message_id == "gmail-1"
            return True

        def mark_mailbox_wait_matched(self, wait_id: str, *, gmail_message_id: str) -> None:
            matched["wait_id"] = wait_id
            matched["gmail_message_id"] = gmail_message_id

    async def fake_mint_token(_settings):
        return {"access_token": "access-token"}

    async def fake_list_history(_settings, *, access_token: str, history_id: str):
        assert access_token == "access-token"
        assert history_id == "history-1"
        return ["gmail-1"]

    async def fake_gmail_get(_settings, path: str, *, access_token: str, params=None):
        assert path == "/messages/gmail-1"
        assert access_token == "access-token"
        return {
            "internalDate": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
            "snippet": "Your Resy verification code is 482991.",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Resy <notifications@resy.com>"},
                    {"name": "Subject", "value": "Your login code"},
                ]
            },
        }

    async def fake_signal(_settings, job_id: str, code: str) -> bool:
        matched["job_id"] = job_id
        matched["code"] = code
        return True

    secret_values = {
        main_module.settings.gmail_pubsub_verification_token_param: "",
        main_module.settings.gmail_account_email_param: "friday.nyc.agent@gmail.com",
    }
    original_secret = main_module.settings.secret
    object.__setattr__(main_module.settings, "secret", lambda parameter_name: secret_values.get(parameter_name, ""))
    try:
        monkeypatch.setattr(main_module, "store", lambda: FakeStore())
        monkeypatch.setattr(main_module, "mint_gmail_access_token", fake_mint_token)
        monkeypatch.setattr(main_module, "list_history_message_ids", fake_list_history)
        monkeypatch.setattr(main_module, "gmail_api_get", fake_gmail_get)

        fake_temporal_client = types.ModuleType("app.temporal_client")
        fake_temporal_client.signal_submit_verification_code = fake_signal
        monkeypatch.setitem(sys.modules, "app.temporal_client", fake_temporal_client)

        envelope = {
            "emailAddress": "friday.nyc.agent@gmail.com",
            "historyId": "history-1",
        }
        payload = {
            "message": {
                "messageId": "pubsub-1",
                "data": base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("utf-8"),
            }
        }

        client = TestClient(main_module.app)
        response = client.post("/internal/gmail/pubsub", json=payload)

        assert response.status_code == 200
        assert response.json()["signals_sent"] == 1
        assert matched == {
            "wait_id": wait.wait_id,
            "gmail_message_id": "gmail-1",
            "job_id": "job-123",
            "code": "482991",
        }
        assert saved_watch_states
    finally:
        object.__setattr__(main_module.settings, "secret", original_secret)
