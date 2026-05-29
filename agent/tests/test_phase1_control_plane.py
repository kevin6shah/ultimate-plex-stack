from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

import app.main as main_module
from app.jobs import (
    AutomationPolicyRecord,
    AgentJob,
    BrowserSessionRecord,
    DashboardSessionRecord,
    IdentityRecord,
    IdentitySecretPointer,
    JobSource,
    JobStatus,
    MailboxVerificationWaitRecord,
    MailboxWatchState,
    PaymentProfileRecord,
)


def _telegram_hash(payload: dict[str, str], bot_token: str) -> str:
    data_check = "\n".join(
        f"{key}={value}"
        for key, value in sorted((key, value) for key, value in payload.items() if key != "hash" and value is not None)
    )
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    return hmac.new(secret_key, data_check.encode("utf-8"), hashlib.sha256).hexdigest()


def _dashboard_session_record() -> DashboardSessionRecord:
    return DashboardSessionRecord(
        telegram_user_id="123",
        telegram_auth_date=str(int(datetime.now(timezone.utc).timestamp())),
        first_name="Kevin",
        username="kevinshah",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )


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


def test_stop_job_uses_async_stop_path(monkeypatch) -> None:
    job = AgentJob(
        job_id="job-123",
        source=JobSource.SIRI,
        query="Find me a restaurant",
        task_class="heavy",
        status=JobStatus.RUNNING,
        user_id="siri",
        conversation_id="siri",
    )
    calls: list[str] = []

    class FakeStore:
        def get_job(self, job_id: str):
            assert job_id == "job-123"
            return job

    async def fake_stop_jobs_async(state, jobs):
        calls.append(",".join(item.job_id for item in jobs))
        return (1, 1)

    monkeypatch.setattr(main_module, "store", lambda: FakeStore())
    monkeypatch.setattr(main_module, "_stop_jobs_async", fake_stop_jobs_async)
    monkeypatch.setattr(type(main_module.settings), "secret", lambda self, parameter_name: "test-key")

    client = TestClient(main_module.app)
    response = client.post("/jobs/job-123/stop", headers={"x-friday-siri-key": "test-key"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert calls == ["job-123"]


def test_recent_context_clears_stale_active_heavy_job_id(monkeypatch) -> None:
    job = AgentJob(
        job_id="job-123",
        source=JobSource.SIRI,
        query="Find me a restaurant",
        task_class="heavy",
        status=JobStatus.COMPLETED,
        user_id="siri",
        conversation_id="siri",
    )
    cleared: list[str] = []

    class FakeStore:
        def list_recent_contexts(self, limit: int = 20):
            assert limit == 20
            return [
                {
                    "pk": "CTX#siri#siri#siri",
                    "summary": "summary",
                    "updated_at": "2026-05-29T05:00:00+00:00",
                    "task_class": "heavy",
                    "active_heavy_job_id": "job-123",
                }
            ]

        def get_active_heavy_job_id(self, *, channel: str, user_id: str, conversation_id: str):
            assert channel == "siri"
            assert user_id == "siri"
            assert conversation_id == "siri"
            return "job-123"

        def get_job(self, job_id: str):
            assert job_id == "job-123"
            return job

        def clear_active_heavy_job(self, *, channel: str, user_id: str, conversation_id: str, only_if_job_id: str | None = None):
            cleared.append(f"{channel}:{user_id}:{conversation_id}:{only_if_job_id}")
            return True

    monkeypatch.setattr(main_module, "store", lambda: FakeStore())
    monkeypatch.setattr(type(main_module.settings), "secret", lambda self, parameter_name: "test-key")

    client = TestClient(main_module.app)
    response = client.get("/context/recent", headers={"x-friday-siri-key": "test-key"})

    assert response.status_code == 200
    assert response.json()["contexts"][0]["active_heavy_job_id"] == ""
    assert cleared == ["siri:siri:siri:job-123"]


def test_delete_thread_normalizes_telegram_owner_alias(monkeypatch) -> None:
    cleared: list[str] = []

    class FakeStore:
        def clear_thread(self, *, channel: str, user_id: str, conversation_id: str) -> None:
            cleared.append(f"{channel}:{user_id}:{conversation_id}")

    secret_values = {
        main_module.settings.telegram_allowed_chat_id_param: "1106318894",
    }
    monkeypatch.setattr(main_module, "store", lambda: FakeStore())
    monkeypatch.setattr(main_module, "_auth_or_401", lambda expected_key, supplied_key: None)
    monkeypatch.setattr(type(main_module.settings), "secret", lambda self, parameter_name: secret_values.get(parameter_name, ""))

    client = TestClient(main_module.app)
    response = client.delete("/threads/telegram-owner?channel=telegram", headers={"x-friday-siri-key": "test-key"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert cleared == ["telegram:1106318894:1106318894"]


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
        assert history_id == "history-0"
        return ["draft-1", "gmail-1"]

    async def fake_gmail_get(_settings, path: str, *, access_token: str, params=None):
        assert access_token == "access-token"
        if path == "/messages/draft-1":
            request = httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages/draft-1?format=full")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("draft lookup failed", request=request, response=response)
        assert path == "/messages/gmail-1"
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
        monkeypatch.setattr(
            main_module,
            "_mailbox_watch_state",
            lambda _state: MailboxWatchState(
                mailbox_email="friday.nyc.agent@gmail.com",
                history_id="history-0",
                watch_status="active",
            ),
        )
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
        assert response.json()["history_cursor"] == "history-0"
        assert matched == {
            "wait_id": wait.wait_id,
            "gmail_message_id": "gmail-1",
            "job_id": "job-123",
            "code": "482991",
        }
        assert saved_watch_states
    finally:
        object.__setattr__(main_module.settings, "secret", original_secret)


def test_dashboard_identity_save_persists_identity_and_secret_pointer(monkeypatch) -> None:
    saved: dict[str, object] = {}

    class FakeStore:
        def list_identities(self, limit: int = 200):
            return []

        def put_identity(self, record: IdentityRecord) -> IdentityRecord:
            saved["identity"] = record
            return record

        def put_identity_secret_pointer(self, record: IdentitySecretPointer) -> IdentitySecretPointer:
            saved["pointer"] = record
            return record

    monkeypatch.setattr(main_module, "store", lambda: FakeStore())
    monkeypatch.setattr(
        main_module,
        "_require_dashboard_session",
        lambda _request: _dashboard_session_record(),
    )

    client = TestClient(main_module.app)
    response = client.post(
        "/dashboard/identities/save",
        data={
            "label": "Friday Gmail",
            "email": "friday.nyc.agent@gmail.com",
            "provider": "gmail",
            "category": "shared_mailbox",
            "site_scope": "shared",
            "status": "active",
            "notes": "Primary verification mailbox",
            "secret_parameter_name": "/friday/identity/gmail/password",
            "secret_kind": "password",
            "is_default": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard/identities"
    identity = saved["identity"]
    pointer = saved["pointer"]
    assert isinstance(identity, IdentityRecord)
    assert identity.email == "friday.nyc.agent@gmail.com"
    assert identity.is_default is True
    assert isinstance(pointer, IdentitySecretPointer)
    assert pointer.parameter_name == "/friday/identity/gmail/password"


def test_dashboard_policy_save_persists_zero_dollar_policy(monkeypatch) -> None:
    saved: list[AutomationPolicyRecord] = []

    class FakeStore:
        def list_automation_policies(self, limit: int = 200):
            return []

        def put_automation_policy(self, record: AutomationPolicyRecord) -> AutomationPolicyRecord:
            saved.append(record)
            return record

    monkeypatch.setattr(main_module, "store", lambda: FakeStore())
    monkeypatch.setattr(
        main_module,
        "_require_dashboard_session",
        lambda _request: _dashboard_session_record(),
    )

    client = TestClient(main_module.app)
    response = client.post(
        "/dashboard/policies/save",
        data={
            "label": "Resy zero-dollar",
            "site_scope": "resy.com",
            "category": "restaurant",
            "default_identity_id": "identity-123",
            "allow_zero_dollar_booking": "on",
            "allow_account_creation": "on",
            "allow_login_reuse": "on",
            "pause_on_sms_or_captcha": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard/policies"
    assert saved
    assert saved[0].allow_zero_dollar_booking is True
    assert saved[0].allow_account_creation is True
    assert saved[0].default_identity_id == "identity-123"


def test_dashboard_session_revoke_marks_session_revoked(monkeypatch) -> None:
    session = BrowserSessionRecord(
        session_id="session-123",
        site_scope="resy.com",
        session_s3_key="browser-sessions/job/session.json",
        user_agent="ua",
        viewport_width=1440,
        viewport_height=900,
        fingerprint_seed="seed",
    )
    updated: list[BrowserSessionRecord] = []
    deleted: list[tuple[str, str]] = []

    class FakeStore:
        def list_browser_sessions(self, limit: int = 200):
            return [session]

        def put_browser_session(self, record: BrowserSessionRecord) -> BrowserSessionRecord:
            updated.append(record)
            return record

    class FakeS3:
        def delete_object(self, *, Bucket: str, Key: str) -> None:
            deleted.append((Bucket, Key))

    monkeypatch.setattr(main_module, "store", lambda: FakeStore())
    monkeypatch.setattr(
        main_module,
        "_require_dashboard_session",
        lambda _request: _dashboard_session_record(),
    )
    original_s3 = getattr(main_module.settings, "s3", None)
    object.__setattr__(main_module.settings, "s3", FakeS3())
    try:
        client = TestClient(main_module.app)
        response = client.post("/dashboard/sessions/session-123/revoke", follow_redirects=False)
    finally:
        object.__setattr__(main_module.settings, "s3", original_s3)

    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard/sessions"
    assert deleted == [(main_module.settings.artifacts_bucket, "browser-sessions/job/session.json")]
    assert updated
    assert updated[0].status == "revoked"


def test_dashboard_payment_save_persists_metadata_only(monkeypatch) -> None:
    saved: list[PaymentProfileRecord] = []

    class FakeStore:
        def list_payment_profiles(self, limit: int = 200):
            return []

        def put_payment_profile(self, record: PaymentProfileRecord) -> PaymentProfileRecord:
            saved.append(record)
            return record

    monkeypatch.setattr(main_module, "store", lambda: FakeStore())
    monkeypatch.setattr(
        main_module,
        "_require_dashboard_session",
        lambda _request: _dashboard_session_record(),
    )

    client = TestClient(main_module.app)
    response = client.post(
        "/dashboard/payments/save",
        data={
            "label": "Privacy $1 cap",
            "provider": "manual",
            "masked_last4": "4242",
            "limit_cents": "100",
            "notes": "Operator-managed temp card",
            "active": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard/payments"
    assert saved
    assert saved[0].masked_last4 == "4242"
    assert saved[0].limit_cents == 100
