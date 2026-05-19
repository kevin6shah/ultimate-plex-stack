from __future__ import annotations

import importlib

import app.settings as settings_module


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
