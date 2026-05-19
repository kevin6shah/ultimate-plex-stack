from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property


def _secret_setting(param_env: str, value_env: str) -> str:
    parameter_name = os.environ.get(param_env, "").strip()
    if parameter_name:
        return parameter_name
    return os.environ.get(value_env, "")


@dataclass(frozen=True)
class Settings:
    state_table: str = os.environ.get("STATE_TABLE", "")
    job_queue_url: str = os.environ.get("JOB_QUEUE_URL", "")
    artifacts_bucket: str = os.environ.get("ARTIFACTS_BUCKET", "")
    aws_region: str = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    agent_model: str = os.environ.get("AGENT_MODEL", "deepseek:deepseek-chat")
    reasoner_model: str = os.environ.get("REASONER_MODEL", "deepseek:deepseek-reasoner")
    daily_budget_usd: float = float(os.environ.get("DAILY_BUDGET_USD", "0.40"))
    monthly_budget_usd: float = float(os.environ.get("MONTHLY_BUDGET_USD", "12.00"))
    session_ttl_seconds: int = int(os.environ.get("SESSION_TTL_SECONDS", "600"))
    conversation_ttl_seconds: int = int(os.environ.get("CONVERSATION_TTL_SECONDS", str(48 * 60 * 60)))
    spend_ttl_days: int = int(os.environ.get("SPEND_TTL_DAYS", "45"))
    completed_job_ttl_days: int = int(os.environ.get("COMPLETED_JOB_TTL_DAYS", "7"))
    interrupted_job_ttl_days: int = int(os.environ.get("INTERRUPTED_JOB_TTL_DAYS", "21"))
    siri_short_timeout_seconds: float = float(os.environ.get("SIRI_SHORT_TIMEOUT_SECONDS", "12"))
    light_task_timeout_seconds: float = float(os.environ.get("LIGHT_TASK_TIMEOUT_SECONDS", "720"))
    telegram_timeout_seconds: float = float(os.environ.get("TELEGRAM_HTTP_TIMEOUT_SECONDS", "10"))
    max_browser_steps: int = int(os.environ.get("MAX_BROWSER_STEPS", "12"))
    max_browser_pages: int = int(os.environ.get("MAX_BROWSER_PAGES", "3"))
    heavy_task_wall_clock_seconds: int = int(os.environ.get("HEAVY_TASK_WALL_CLOCK_SECONDS", "1800"))
    workspace_output_limit_bytes: int = int(os.environ.get("WORKSPACE_OUTPUT_LIMIT_BYTES", str(25 * 1024 * 1024)))
    artifact_url_ttl_seconds: int = int(os.environ.get("ARTIFACT_URL_TTL_SECONDS", "3600"))
    max_attachment_bytes: int = int(os.environ.get("MAX_ATTACHMENT_BYTES", str(20 * 1024 * 1024)))
    deepseek_input_cache_miss_per_1m: float = float(os.environ.get("DEEPSEEK_INPUT_CACHE_MISS_PER_1M", "0.55"))
    deepseek_input_cache_hit_per_1m: float = float(os.environ.get("DEEPSEEK_INPUT_CACHE_HIT_PER_1M", "0.14"))
    deepseek_output_per_1m: float = float(os.environ.get("DEEPSEEK_OUTPUT_PER_1M", "2.19"))
    telegram_bot_token_param: str = _secret_setting("TELEGRAM_BOT_TOKEN_PARAM", "TELEGRAM_BOT_TOKEN")
    telegram_allowed_chat_id_param: str = _secret_setting("TELEGRAM_ALLOWED_CHAT_ID_PARAM", "TELEGRAM_ALLOWED_CHAT_ID")
    telegram_webhook_secret_param: str = _secret_setting("TELEGRAM_WEBHOOK_SECRET_PARAM", "TELEGRAM_WEBHOOK_SECRET")
    siri_api_key_param: str = _secret_setting("SIRI_API_KEY_PARAM", "SIRI_API_KEY")
    deepseek_api_key_param: str = _secret_setting("DEEPSEEK_API_KEY_PARAM", "DEEPSEEK_API_KEY")
    brave_search_api_key_param: str = _secret_setting("BRAVE_SEARCH_API_KEY_PARAM", "BRAVE_SEARCH_API_KEY")
    browser_use_api_key_param: str = _secret_setting("BROWSER_USE_API_KEY_PARAM", "BROWSER_USE_API_KEY")
    firecrawl_api_key_param: str = _secret_setting("FIRECRAWL_API_KEY_PARAM", "FIRECRAWL_API_KEY")
    google_maps_api_key_param: str = _secret_setting("GOOGLE_MAPS_API_KEY_PARAM", "GOOGLE_MAPS_API_KEY")
    maps_openapi_headers_param: str = _secret_setting("MAPS_OPENAPI_HEADERS_PARAM", "MAPS_OPENAPI_HEADERS_JSON")
    maps_openapi_auth_token_param: str = _secret_setting("MAPS_OPENAPI_AUTH_TOKEN_PARAM", "MAPS_OPENAPI_AUTH_TOKEN")
    resy_api_key_param: str = _secret_setting("RESY_API_KEY_PARAM", "RESY_API_KEY")
    resy_auth_token_param: str = _secret_setting("RESY_AUTH_TOKEN_PARAM", "RESY_AUTH_TOKEN")
    opentable_email_param: str = _secret_setting("OPENTABLE_EMAIL_PARAM", "OPENTABLE_EMAIL")
    opentable_password_param: str = _secret_setting("OPENTABLE_PASSWORD_PARAM", "OPENTABLE_PASSWORD")
    gmail_account_email_param: str = _secret_setting("GMAIL_ACCOUNT_EMAIL_PARAM", "GMAIL_ACCOUNT_EMAIL")
    gmail_app_password_param: str = _secret_setting("GMAIL_APP_PASSWORD_PARAM", "GMAIL_APP_PASSWORD")
    gmail_client_id_param: str = _secret_setting("GMAIL_CLIENT_ID_PARAM", "GMAIL_CLIENT_ID")
    gmail_client_secret_param: str = _secret_setting("GMAIL_CLIENT_SECRET_PARAM", "GMAIL_CLIENT_SECRET")
    gmail_refresh_token_param: str = _secret_setting("GMAIL_REFRESH_TOKEN_PARAM", "GMAIL_REFRESH_TOKEN")
    logfire_token_param: str = _secret_setting("LOGFIRE_TOKEN_PARAM", "LOGFIRE_TOKEN")
    worker_api_key_param: str = _secret_setting("WORKER_API_KEY_PARAM", "WORKER_API_KEY")
    browser_stealth_enabled: bool = os.environ.get("BROWSER_STEALTH_ENABLED", "true").lower() == "true"
    browser_user_agent_rotation: bool = os.environ.get("BROWSER_USER_AGENT_ROTATION", "true").lower() == "true"
    browser_use_enabled: bool = os.environ.get("BROWSER_USE_ENABLED", "true").lower() == "true"
    browser_use_cloud_enabled: bool = os.environ.get("BROWSER_USE_CLOUD_ENABLED", "false").lower() == "true"
    browser_use_model: str = os.environ.get("BROWSER_USE_MODEL", "deepseek-chat")
    browser_use_cloud_model: str = os.environ.get("BROWSER_USE_CLOUD_MODEL", "bu-latest")
    browser_use_cloud_proxy_country_code: str = os.environ.get("BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE", "us")
    browser_use_step_timeout_seconds: int = int(os.environ.get("BROWSER_USE_STEP_TIMEOUT_SECONDS", "120"))
    browser_use_max_failures: int = int(os.environ.get("BROWSER_USE_MAX_FAILURES", "3"))
    browser_use_task_timeout_seconds: int = int(os.environ.get("BROWSER_USE_TASK_TIMEOUT_SECONDS", "90"))
    mcp_registration_timeout_seconds: int = int(os.environ.get("MCP_REGISTRATION_TIMEOUT_SECONDS", "20"))
    firecrawl_mcp_enabled: bool = os.environ.get("FIRECRAWL_MCP_ENABLED", "false").lower() == "true"
    skiplagged_mcp_enabled: bool = os.environ.get("SKIPLAGGED_MCP_ENABLED", "false").lower() == "true"
    skiplagged_mcp_command: str = os.environ.get("SKIPLAGGED_MCP_COMMAND", "npx")
    skiplagged_mcp_args: str = os.environ.get("SKIPLAGGED_MCP_ARGS", "-y mcp-remote https://mcp.skiplagged.com/mcp")
    restaurant_cli_enabled: bool = os.environ.get("RESTAURANT_CLI_ENABLED", "false").lower() == "true"
    restaurant_cli_command: str = os.environ.get("RESTAURANT_CLI_COMMAND", "restaurant")
    restaurant_cli_ot_mode: str = os.environ.get("RESTAURANT_CLI_OT_MODE", "auto")
    restaurant_cli_timezone: str = os.environ.get("RESTAURANT_CLI_TIMEZONE", os.environ.get("TZ", "America/New_York"))
    google_maps_mcp_enabled: bool = os.environ.get("GOOGLE_MAPS_MCP_ENABLED", "false").lower() == "true"
    google_maps_enabled_tools: str = os.environ.get("GOOGLE_MAPS_ENABLED_TOOLS", "")
    maps_openapi_mcp_enabled: bool = os.environ.get("MAPS_OPENAPI_MCP_ENABLED", "false").lower() == "true"
    maps_openapi_mcp_command: str = os.environ.get("MAPS_OPENAPI_MCP_COMMAND", "npx")
    maps_openapi_mcp_args: str = os.environ.get("MAPS_OPENAPI_MCP_ARGS", "-y @openapi-mcp/server")
    maps_openapi_spec_url: str = os.environ.get("MAPS_OPENAPI_SPEC_URL", "")
    maps_openapi_base_url: str = os.environ.get("MAPS_OPENAPI_BASE_URL", "")
    resy_mcp_enabled: bool = os.environ.get("RESY_MCP_ENABLED", "false").lower() == "true"
    resy_mcp_command: str = os.environ.get("RESY_MCP_COMMAND", "node")
    resy_mcp_args: str = os.environ.get("RESY_MCP_ARGS", "/opt/friday/mcp/resy-mcp/build/index.js --stdio")
    opentable_mcp_enabled: bool = os.environ.get("OPENTABLE_MCP_ENABLED", "false").lower() == "true"
    opentable_mcp_command: str = os.environ.get("OPENTABLE_MCP_COMMAND", "npx")
    opentable_mcp_args: str = os.environ.get("OPENTABLE_MCP_ARGS", "-y @striderlabs/mcp-opentable")
    gmail_mcp_enabled: bool = os.environ.get("GMAIL_MCP_ENABLED", "false").lower() == "true"
    logfire_enabled: bool = os.environ.get("LOGFIRE_ENABLED", "false").lower() == "true"
    full_logfire_logging: bool = os.environ.get("LOGFIRE_FULL_CONTENT", "false").lower() == "true"
    hands_worker_mode: str = os.environ.get("HANDS_WORKER_MODE", "shared_host")
    hands_worker_instance_id: str = os.environ.get("HANDS_WORKER_INSTANCE_ID", "")

    @cached_property
    def ssm(self):
        import boto3

        return boto3.client("ssm", region_name=self.aws_region)

    @cached_property
    def s3(self):
        import boto3

        return boto3.client("s3", region_name=self.aws_region)

    @cached_property
    def ec2(self):
        import boto3

        return boto3.client("ec2", region_name=self.aws_region)

    def secret(self, parameter_name: str) -> str:
        if not parameter_name:
            return ""
        if not parameter_name.startswith("/"):
            return parameter_name
        response = self.ssm.get_parameter(Name=parameter_name, WithDecryption=True)
        return response["Parameter"]["Value"]


settings = Settings()
