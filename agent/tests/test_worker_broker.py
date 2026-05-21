from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace


def _load_broker(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("FRIDAY_API_BASE_URL", "https://example.com")
    monkeypatch.setenv("FRIDAY_WORKER_KEY", "worker-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("FRIDAY_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("FRIDAY_CONTAINER_ENGINE", "docker")
    monkeypatch.setenv("FIRECRAWL_MCP_ENABLED", "true")
    monkeypatch.setenv("FIRECRAWL_API_KEY_PARAM", "/friday/agent/firecrawl-api-key")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "firecrawl-key")
    monkeypatch.setenv("GMAIL_MCP_ENABLED", "true")
    monkeypatch.setenv("GMAIL_ACCOUNT_EMAIL_PARAM", "/friday/agent/gmail-account-email")
    monkeypatch.setenv("GMAIL_APP_PASSWORD_PARAM", "/friday/agent/gmail-app-password")
    monkeypatch.setenv("GMAIL_ACCOUNT_EMAIL", "friday.nyc.agent@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    from hands.host import broker as broker_module

    return importlib.reload(broker_module)


def test_run_worker_places_env_flags_before_image(monkeypatch, tmp_path: Path) -> None:
    broker = _load_broker(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(broker.subprocess, "run", fake_run)

    claim = {
        "job": {
            "job_id": "job-123",
            "resume_from_job_id": None,
        }
    }
    broker.run_worker(claim)

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    image_index = cmd.index(broker.WORKER_IMAGE)
    assert image_index < len(cmd) - 1
    assert "-e" in cmd[:image_index]
    assert "FIRECRAWL_MCP_ENABLED=true" in cmd[:image_index]
    assert "FIRECRAWL_API_KEY_PARAM=/friday/agent/firecrawl-api-key" in cmd[:image_index]
    assert "GMAIL_ACCOUNT_EMAIL_PARAM=/friday/agent/gmail-account-email" in cmd[:image_index]
    assert "GMAIL_ACCOUNT_EMAIL=friday.nyc.agent@gmail.com" in cmd[:image_index]
    assert "GMAIL_APP_PASSWORD=app-password" in cmd[:image_index]
    assert cmd[image_index + 1 :] == broker.WORKER_COMMAND.split()

    claim_path = tmp_path / "workspaces" / "job-123" / "claim.json"
    assert json.loads(claim_path.read_text(encoding="utf-8"))["job"]["job_id"] == "job-123"


def test_verify_terminal_job_state_reports_non_terminal_exit(monkeypatch, tmp_path: Path) -> None:
    broker = _load_broker(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    monkeypatch.setattr(broker, "get_json", lambda path: {"status": "running"})

    def fake_report_failure(job_id: str, error_message: str, *, interrupted: bool = False, timed_out: bool = False) -> None:
        captured["job_id"] = job_id
        captured["error_message"] = error_message
        captured["interrupted"] = interrupted
        captured["timed_out"] = timed_out

    monkeypatch.setattr(broker, "report_failure", fake_report_failure)

    broker.verify_terminal_job_state("job-verify")

    assert captured["job_id"] == "job-verify"
    assert "worker exited without reporting a terminal state" in str(captured["error_message"])
    assert captured["interrupted"] is False
    assert captured["timed_out"] is False
