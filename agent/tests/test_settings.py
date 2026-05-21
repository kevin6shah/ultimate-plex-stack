from __future__ import annotations

import importlib
from datetime import datetime

import app.settings as settings_module
from app.storage import StateStore
from botocore.exceptions import ClientError


def test_secret_returns_literal_value_for_direct_env_secret(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "firecrawl-direct-secret")
    monkeypatch.delenv("FIRECRAWL_API_KEY_PARAM", raising=False)

    reloaded = importlib.reload(settings_module)
    settings = reloaded.Settings()

    assert settings.firecrawl_api_key_param == "firecrawl-direct-secret"
    assert settings.secret(settings.firecrawl_api_key_param) == "firecrawl-direct-secret"


def test_secret_prefers_parameter_name_when_explicit_param_is_set(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "firecrawl-direct-secret")
    monkeypatch.setenv("FIRECRAWL_API_KEY_PARAM", "/friday/agent/firecrawl-api-key")

    reloaded = importlib.reload(settings_module)
    settings = reloaded.Settings()

    assert settings.firecrawl_api_key_param == "/friday/agent/firecrawl-api-key"


def test_secret_falls_back_to_direct_value_when_param_env_is_blank(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "firecrawl-direct-secret")
    monkeypatch.setenv("FIRECRAWL_API_KEY_PARAM", "")

    reloaded = importlib.reload(settings_module)
    settings = reloaded.Settings()

    assert settings.firecrawl_api_key_param == "firecrawl-direct-secret"
    assert settings.secret(settings.firecrawl_api_key_param) == "firecrawl-direct-secret"


def test_skiplagged_defaults_are_remote_bridge_based(monkeypatch):
    monkeypatch.delenv("SKIPLAGGED_MCP_ENABLED", raising=False)
    monkeypatch.delenv("SKIPLAGGED_MCP_COMMAND", raising=False)
    monkeypatch.delenv("SKIPLAGGED_MCP_ARGS", raising=False)

    reloaded = importlib.reload(settings_module)
    settings = reloaded.Settings()

    assert settings.skiplagged_mcp_enabled is False
    assert settings.skiplagged_mcp_command == "npx"
    assert settings.skiplagged_mcp_args == "-y mcp-remote https://mcp.skiplagged.com/mcp"


def test_stagehand_defaults_are_local_and_enabled(monkeypatch):
    monkeypatch.delenv("STAGEHAND_ENABLED", raising=False)
    monkeypatch.delenv("STAGEHAND_MODEL", raising=False)
    monkeypatch.delenv("STAGEHAND_MODE", raising=False)
    monkeypatch.delenv("STAGEHAND_LOCAL_CHROME_PATH", raising=False)
    monkeypatch.delenv("CHROME_PATH", raising=False)

    reloaded = importlib.reload(settings_module)
    settings = reloaded.Settings()

    assert settings.stagehand_enabled is True
    assert settings.stagehand_model == "deepseek/deepseek-chat"
    assert settings.stagehand_mode == "hybrid"
    assert settings.stagehand_local_chrome_path == ""


def test_secret_caches_ssm_decrypt_reads(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("FIRECRAWL_API_KEY_PARAM", "/friday/agent/firecrawl-api-key")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    reloaded = importlib.reload(settings_module)

    class FakeSSM:
        def __init__(self):
            self.calls = 0

        def get_parameter(self, *, Name, WithDecryption):
            self.calls += 1
            assert Name == "/friday/agent/firecrawl-api-key"
            assert WithDecryption is True
            return {"Parameter": {"Value": "cached-secret"}}

    fake = FakeSSM()
    settings = reloaded.Settings()
    monkeypatch.setattr(type(settings), "ssm", property(lambda self: fake))

    assert settings.secret(settings.firecrawl_api_key_param) == "cached-secret"
    assert settings.secret(settings.firecrawl_api_key_param) == "cached-secret"
    assert fake.calls == 1


def test_hands_worker_idle_grace_default_is_ten_minutes(monkeypatch):
    monkeypatch.delenv("HANDS_WORKER_IDLE_GRACE_SECONDS", raising=False)

    reloaded = importlib.reload(settings_module)
    settings = reloaded.Settings()

    assert settings.hands_worker_idle_grace_seconds == 600


def test_worker_stall_defaults_are_tighter_than_old_status_timer(monkeypatch):
    monkeypatch.delenv("WORKER_STALL_REPEAT_HEARTBEATS", raising=False)
    monkeypatch.delenv("WORKER_RUNNING_STALL_SECONDS", raising=False)
    monkeypatch.delenv("WORKER_PREFLIGHT_STALL_SECONDS", raising=False)

    reloaded = importlib.reload(settings_module)
    settings = reloaded.Settings()

    assert settings.worker_stall_repeat_heartbeats == 18
    assert settings.worker_running_stall_seconds == 900
    assert settings.worker_preflight_stall_seconds == 300


def test_claim_telegram_update_is_idempotent(monkeypatch):
    class FakeTable:
        def __init__(self):
            self.seen = set()

        def put_item(self, *, Item, ConditionExpression=None):
            key = (Item["PK"], Item["SK"])
            if key in self.seen:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "duplicate"}},
                    "PutItem",
                )
            self.seen.add(key)

    fake_table = FakeTable()

    class FakeDynamo:
        def Table(self, _name):
            return fake_table

    monkeypatch.setattr("app.storage.boto3.resource", lambda *args, **kwargs: FakeDynamo())

    reloaded = importlib.reload(settings_module)
    settings = reloaded.Settings()
    state = StateStore(settings)

    assert state.claim_telegram_update(chat_id="123", update_id=42, user_id="456") is True
    assert state.claim_telegram_update(chat_id="123", update_id=42, user_id="456") is False


def test_should_send_status_update_suppresses_identical_message(monkeypatch):
    reloaded = importlib.reload(settings_module)
    settings = reloaded.Settings()
    state = StateStore(settings)
    job = state._job_from_item(
        {
            "job_id": "job-1",
            "source": "telegram",
            "query": "Find flights",
            "task_class": "heavy",
            "status": "running",
            "created_at": "2026-05-20T00:00:00+00:00",
            "last_status_sent_at": "2099-05-20T00:00:00+00:00",
            "last_status_sent_text": "same progress",
        }
    )
    monkeypatch.setattr(state, "get_job", lambda _job_id: job)
    assert state.should_send_status_update("job-1", interval_seconds=60, text="same progress") is False


def test_should_stop_for_stall_uses_running_step_threshold(monkeypatch):
    reloaded = importlib.reload(settings_module)
    settings = reloaded.Settings()
    state = StateStore(settings)
    job = state._job_from_item(
        {
            "job_id": "job-1",
            "source": "telegram",
            "query": "Research jackets",
            "task_class": "heavy",
            "status": "running",
            "current_step": "running_agent",
            "created_at": "2026-05-20T00:00:00+00:00",
            "heartbeat_repeat_count": 18,
            "last_progress_at": "2026-05-20T00:00:00+00:00",
        }
    )
    monkeypatch.setattr(state, "get_job", lambda _job_id: job)
    monkeypatch.setattr("app.storage.utc_now", lambda: datetime.fromisoformat("2026-05-20T00:15:00+00:00"))
    assert state.should_stop_for_stall("job-1", interval_seconds=300) is True
