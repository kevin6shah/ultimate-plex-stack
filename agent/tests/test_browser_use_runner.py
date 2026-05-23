from dataclasses import replace
import asyncio

from app.browser_use_runner import (
    _browser_use_result_needs_retry,
    _extract_browser_use_partial_findings,
    _filesystem_mcp_args,
    _firecrawl_mcp_env,
    _google_maps_mcp_env,
    _maps_openapi_mcp_env,
    _opentable_mcp_env,
    _register_optional_mcp,
    _resy_mcp_env,
    _split_mcp_args,
)
from app.settings import Settings
from app.workspace import Workspace


def test_filesystem_mcp_args_are_scoped_to_workspace_root(tmp_path) -> None:
    workspace = Workspace(str(tmp_path))
    assert _filesystem_mcp_args(workspace) == [str(tmp_path.resolve())]


def test_firecrawl_mcp_env_uses_secret() -> None:
    settings = replace(Settings(), firecrawl_api_key_param="/firecrawl/key")
    object.__setattr__(settings, "secret", lambda parameter_name: "fc-key" if parameter_name == "/firecrawl/key" else "")
    assert _firecrawl_mcp_env(settings) == {"FIRECRAWL_API_KEY": "fc-key"}


def test_google_maps_mcp_env_uses_secret_and_enabled_tools() -> None:
    settings = replace(
        Settings(),
        google_maps_api_key_param="/maps/key",
        google_maps_enabled_tools="maps_search_places,maps_plan_route",
    )
    object.__setattr__(settings, "secret", lambda parameter_name: "gm-key" if parameter_name == "/maps/key" else "")
    assert _google_maps_mcp_env(settings) == {
        "GOOGLE_MAPS_API_KEY": "gm-key",
        "GOOGLE_MAPS_ENABLED_TOOLS": "maps_search_places,maps_plan_route",
    }


def test_split_mcp_args_uses_shell_like_splitting() -> None:
    assert _split_mcp_args('foo "bar baz" --flag') == ["foo", "bar baz", "--flag"]


def test_browser_use_result_needs_retry_for_low_quality_meta_summary() -> None:
    assert _browser_use_result_needs_retry("OK, search is currently unavailable. Let me use what I've already gathered.")
    assert _browser_use_result_needs_retry("The site requires JavaScript to interact.")
    assert not _browser_use_result_needs_retry("Tonight's best available show is 10:00 PM at Village Underground for $15.")


def test_extract_browser_use_partial_findings_prefers_memory_lines() -> None:
    findings = _extract_browser_use_partial_findings(
        [
            "🎯 Task: Search for OpenClaw architecture framework tool.",
            "🧠 Memory: Found docs.openclaw.ai and a GitHub repo for OpenClaw.",
            "🧠 Memory: Docs describe OpenClaw as a self-hosted gateway for chat apps and coding agents.",
            "noise line",
            "🧠 Memory: Docs describe OpenClaw as a self-hosted gateway for chat apps and coding agents.",
        ]
    )

    assert "docs.openclaw.ai and a GitHub repo" in findings
    assert "self-hosted gateway for chat apps and coding agents" in findings
    assert findings.count("- ") >= 2


def test_maps_openapi_mcp_env_uses_base_url_headers_and_token() -> None:
    settings = replace(
        Settings(),
        maps_openapi_base_url="https://places.googleapis.com",
        maps_openapi_spec_url="https://example.com/spec.json",
        maps_openapi_auth_token_param="/maps/token",
        maps_openapi_headers_param="/maps/headers",
    )
    secret_values = {
        "/maps/token": "maps-token",
        "/maps/headers": '{"X-Goog-Api-Key":"gm-key","X-App":"friday"}',
    }
    object.__setattr__(settings, "secret", lambda parameter_name: secret_values.get(parameter_name, ""))
    assert _maps_openapi_mcp_env(settings) == {
        "BASE_URL": "https://places.googleapis.com",
        "MAPS_OPENAPI_SPEC_URL": "https://example.com/spec.json",
        "HEADERS": '{"Authorization": "Bearer maps-token", "X-Goog-Api-Key": "gm-key", "X-App": "friday"}',
    }


def test_resy_mcp_env_requires_api_key_and_auth_token() -> None:
    settings = replace(
        Settings(),
        resy_api_key_param="/resy/key",
        resy_auth_token_param="/resy/token",
    )
    secret_values = {
        "/resy/key": "resy-key",
        "/resy/token": "resy-token",
    }
    object.__setattr__(settings, "secret", lambda parameter_name: secret_values.get(parameter_name, ""))
    assert _resy_mcp_env(settings) == {
        "DOTENV_CONFIG_QUIET": "true",
        "RESY_API_KEY": "resy-key",
        "RESY_AUTH_TOKEN": "resy-token",
    }


def test_opentable_mcp_env_requires_email_and_password() -> None:
    settings = replace(
        Settings(),
        opentable_email_param="/ot/email",
        opentable_password_param="/ot/password",
    )
    secret_values = {
        "/ot/email": "agent@example.com",
        "/ot/password": "hunter2",
    }
    object.__setattr__(settings, "secret", lambda parameter_name: secret_values.get(parameter_name, ""))
    assert _opentable_mcp_env(settings) == {
        "OPENTABLE_EMAIL": "agent@example.com",
        "OPENTABLE_PASSWORD": "hunter2",
    }


def test_register_optional_mcp_times_out_and_returns_none() -> None:
    class HangingClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def register_to_tools(self, tools, **kwargs):
            await asyncio.sleep(60)

    result = asyncio.run(
        _register_optional_mcp(
            MCPClient=HangingClient,
            tools=object(),
            server_name="resy",
            command="node",
            args=["server.js"],
            prefix="resy_",
            timeout_seconds=1,
        )
    )

    assert result is None
