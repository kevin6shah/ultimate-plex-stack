#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AWS_PROFILE_NAME="${AWS_PROFILE:-}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
STACK_NAME_VALUE="${AGENT_STACK_NAME:-friday-agent}"
AGENT_NAME_VALUE="${AGENT_NAME:-friday-agent}"
SHARED_HOST_STACK_NAME_VALUE="${SHARED_HOST_STACK_NAME:-friday-shared-host}"
IMAGE_TAG_VALUE="${IMAGE_TAG:-$(date +%Y%m%d-%H%M%S)}"
IMAGE_URI_OVERRIDE_VALUE="${IMAGE_URI_OVERRIDE:-}"
BUDGET_ALERT_EMAIL_VALUE="${BUDGET_ALERT_EMAIL:-}"
AGENT_RESERVED_CONCURRENCY_VALUE="${AGENT_RESERVED_CONCURRENCY:-0}"
HANDS_WORKER_MODE_VALUE="${HANDS_WORKER_MODE:-shared_host}"
HANDS_WORKER_INSTANCE_ID_VALUE="${HANDS_WORKER_INSTANCE_ID:-}"
SET_TELEGRAM_WEBHOOK_VALUE="${SET_TELEGRAM_WEBHOOK:-0}"
DEEPSEEK_API_KEY_PARAM_VALUE="${DEEPSEEK_API_KEY_PARAM:-/friday/agent/deepseek-api-key}"
BRAVE_SEARCH_API_KEY_PARAM_VALUE="${BRAVE_SEARCH_API_KEY_PARAM:-/friday/agent/brave-search-api-key}"
BROWSER_USE_API_KEY_PARAM_VALUE="${BROWSER_USE_API_KEY_PARAM:-/friday/agent/browser-use-api-key}"
FIRECRAWL_API_KEY_PARAM_VALUE="${FIRECRAWL_API_KEY_PARAM:-/friday/agent/firecrawl-api-key}"
GOOGLE_MAPS_API_KEY_PARAM_VALUE="${GOOGLE_MAPS_API_KEY_PARAM:-/friday/agent/google-maps-api-key}"
MAPS_OPENAPI_HEADERS_PARAM_VALUE="${MAPS_OPENAPI_HEADERS_PARAM:-/friday/agent/maps-openapi-headers}"
MAPS_OPENAPI_AUTH_TOKEN_PARAM_VALUE="${MAPS_OPENAPI_AUTH_TOKEN_PARAM:-/friday/agent/maps-openapi-auth-token}"
RESY_API_KEY_PARAM_VALUE="${RESY_API_KEY_PARAM:-/friday/agent/resy-api-key}"
RESY_AUTH_TOKEN_PARAM_VALUE="${RESY_AUTH_TOKEN_PARAM:-/friday/agent/resy-auth-token}"
OPENTABLE_EMAIL_PARAM_VALUE="${OPENTABLE_EMAIL_PARAM:-/friday/agent/opentable-email}"
OPENTABLE_PASSWORD_PARAM_VALUE="${OPENTABLE_PASSWORD_PARAM:-/friday/agent/opentable-password}"
GMAIL_ACCOUNT_EMAIL_PARAM_VALUE="${GMAIL_ACCOUNT_EMAIL_PARAM:-/friday/agent/gmail-account-email}"
GMAIL_APP_PASSWORD_PARAM_VALUE="${GMAIL_APP_PASSWORD_PARAM:-/friday/agent/gmail-app-password}"
GMAIL_CLIENT_ID_PARAM_VALUE="${GMAIL_CLIENT_ID_PARAM:-/friday/agent/gmail-client-id}"
GMAIL_CLIENT_SECRET_PARAM_VALUE="${GMAIL_CLIENT_SECRET_PARAM:-/friday/agent/gmail-client-secret}"
GMAIL_REFRESH_TOKEN_PARAM_VALUE="${GMAIL_REFRESH_TOKEN_PARAM:-/friday/agent/gmail-refresh-token}"
TELEGRAM_BOT_TOKEN_PARAM_VALUE="${TELEGRAM_BOT_TOKEN_PARAM:-/friday/agent/telegram-bot-token}"
TELEGRAM_ALLOWED_CHAT_ID_PARAM_VALUE="${TELEGRAM_ALLOWED_CHAT_ID_PARAM:-/friday/agent/telegram-chat-id}"
TELEGRAM_WEBHOOK_SECRET_PARAM_VALUE="${TELEGRAM_WEBHOOK_SECRET_PARAM:-/friday/agent/telegram-webhook-secret}"
SIRI_API_KEY_PARAM_VALUE="${SIRI_API_KEY_PARAM:-/friday/agent/siri-api-key}"
WORKER_API_KEY_PARAM_VALUE="${WORKER_API_KEY_PARAM:-/friday/agent/worker-api-key}"
LOGFIRE_TOKEN_PARAM_VALUE="${LOGFIRE_TOKEN_PARAM:-/friday/agent/logfire-token}"
LOGFIRE_ENABLED_VALUE="${LOGFIRE_ENABLED:-false}"
LOGFIRE_FULL_CONTENT_VALUE="${LOGFIRE_FULL_CONTENT:-false}"
BROWSER_USE_ENABLED_VALUE="${BROWSER_USE_ENABLED:-true}"
BROWSER_USE_CLOUD_ENABLED_VALUE="${BROWSER_USE_CLOUD_ENABLED:-false}"
BROWSER_USE_MODEL_VALUE="${BROWSER_USE_MODEL:-deepseek-chat}"
BROWSER_USE_CLOUD_MODEL_VALUE="${BROWSER_USE_CLOUD_MODEL:-bu-latest}"
BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE_VALUE="${BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE:-us}"
FIRECRAWL_MCP_ENABLED_VALUE="${FIRECRAWL_MCP_ENABLED:-false}"
SKIPLAGGED_MCP_ENABLED_VALUE="${SKIPLAGGED_MCP_ENABLED:-false}"
SKIPLAGGED_MCP_COMMAND_VALUE="${SKIPLAGGED_MCP_COMMAND:-npx}"
SKIPLAGGED_MCP_ARGS_VALUE="${SKIPLAGGED_MCP_ARGS:--y mcp-remote https://mcp.skiplagged.com/mcp}"
GOOGLE_MAPS_MCP_ENABLED_VALUE="${GOOGLE_MAPS_MCP_ENABLED:-false}"
GOOGLE_MAPS_ENABLED_TOOLS_VALUE="${GOOGLE_MAPS_ENABLED_TOOLS:-}"
MAPS_OPENAPI_MCP_ENABLED_VALUE="${MAPS_OPENAPI_MCP_ENABLED:-false}"
MAPS_OPENAPI_SPEC_URL_VALUE="${MAPS_OPENAPI_SPEC_URL:-}"
MAPS_OPENAPI_BASE_URL_VALUE="${MAPS_OPENAPI_BASE_URL:-}"
RESY_MCP_ENABLED_VALUE="${RESY_MCP_ENABLED:-false}"
OPENTABLE_MCP_ENABLED_VALUE="${OPENTABLE_MCP_ENABLED:-false}"
GMAIL_MCP_ENABLED_VALUE="${GMAIL_MCP_ENABLED:-false}"
SHARED_HOST_INSTANCE_ID_VALUE="${SHARED_HOST_INSTANCE_ID:-}"

[[ -n "$AWS_PROFILE_NAME" ]] || { echo "AWS_PROFILE is required" >&2; exit 1; }

for cmd in aws docker; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "required command not found: $cmd" >&2; exit 1; }
done

stack_status="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
      --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true
)"

if [[ -z "$SHARED_HOST_INSTANCE_ID_VALUE" ]]; then
  SHARED_HOST_INSTANCE_ID_VALUE="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws cloudformation describe-stacks --stack-name "$SHARED_HOST_STACK_NAME_VALUE" \
        --query 'Stacks[0].Outputs[?OutputKey==`InstanceId`].OutputValue' --output text 2>/dev/null || true
  )"
fi

if [[ -z "$stack_status" ]]; then
  common_parameters=(
    "BootstrapOnly=true"
    "AgentName=${AGENT_NAME_VALUE}"
    "BudgetAlertEmail=${BUDGET_ALERT_EMAIL_VALUE}"
    "AgentReservedConcurrency=${AGENT_RESERVED_CONCURRENCY_VALUE}"
    "SharedHostInstanceId=${SHARED_HOST_INSTANCE_ID_VALUE}"
    "HandsWorkerMode=${HANDS_WORKER_MODE_VALUE}"
    "HandsWorkerInstanceId=${HANDS_WORKER_INSTANCE_ID_VALUE}"
    "DeepSeekApiKeyParam=${DEEPSEEK_API_KEY_PARAM_VALUE}"
    "BraveSearchApiKeyParam=${BRAVE_SEARCH_API_KEY_PARAM_VALUE}"
    "BrowserUseApiKeyParam=${BROWSER_USE_API_KEY_PARAM_VALUE}"
    "FirecrawlApiKeyParam=${FIRECRAWL_API_KEY_PARAM_VALUE}"
    "GoogleMapsApiKeyParam=${GOOGLE_MAPS_API_KEY_PARAM_VALUE}"
    "MapsOpenApiHeadersParam=${MAPS_OPENAPI_HEADERS_PARAM_VALUE}"
    "MapsOpenApiAuthTokenParam=${MAPS_OPENAPI_AUTH_TOKEN_PARAM_VALUE}"
    "ResyApiKeyParam=${RESY_API_KEY_PARAM_VALUE}"
    "ResyAuthTokenParam=${RESY_AUTH_TOKEN_PARAM_VALUE}"
    "OpenTableEmailParam=${OPENTABLE_EMAIL_PARAM_VALUE}"
    "OpenTablePasswordParam=${OPENTABLE_PASSWORD_PARAM_VALUE}"
    "GmailAccountEmailParam=${GMAIL_ACCOUNT_EMAIL_PARAM_VALUE}"
    "GmailAppPasswordParam=${GMAIL_APP_PASSWORD_PARAM_VALUE}"
    "GmailClientIdParam=${GMAIL_CLIENT_ID_PARAM_VALUE}"
    "GmailClientSecretParam=${GMAIL_CLIENT_SECRET_PARAM_VALUE}"
    "GmailRefreshTokenParam=${GMAIL_REFRESH_TOKEN_PARAM_VALUE}"
    "TelegramBotTokenParam=${TELEGRAM_BOT_TOKEN_PARAM_VALUE}"
    "TelegramAllowedChatIdParam=${TELEGRAM_ALLOWED_CHAT_ID_PARAM_VALUE}"
    "TelegramWebhookSecretParam=${TELEGRAM_WEBHOOK_SECRET_PARAM_VALUE}"
    "SiriApiKeyParam=${SIRI_API_KEY_PARAM_VALUE}"
    "WorkerApiKeyParam=${WORKER_API_KEY_PARAM_VALUE}"
    "LogfireTokenParam=${LOGFIRE_TOKEN_PARAM_VALUE}"
    "LogfireEnabled=${LOGFIRE_ENABLED_VALUE}"
    "LogfireFullContent=${LOGFIRE_FULL_CONTENT_VALUE}"
    "BrowserUseEnabled=${BROWSER_USE_ENABLED_VALUE}"
    "BrowserUseCloudEnabled=${BROWSER_USE_CLOUD_ENABLED_VALUE}"
    "BrowserUseModel=${BROWSER_USE_MODEL_VALUE}"
    "BrowserUseCloudModel=${BROWSER_USE_CLOUD_MODEL_VALUE}"
    "BrowserUseCloudProxyCountryCode=${BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE_VALUE}"
    "FirecrawlMcpEnabled=${FIRECRAWL_MCP_ENABLED_VALUE}"
    "SkiplaggedMcpEnabled=${SKIPLAGGED_MCP_ENABLED_VALUE}"
    "SkiplaggedMcpCommand=${SKIPLAGGED_MCP_COMMAND_VALUE}"
    "SkiplaggedMcpArgs=${SKIPLAGGED_MCP_ARGS_VALUE}"
    "GoogleMapsMcpEnabled=${GOOGLE_MAPS_MCP_ENABLED_VALUE}"
    "GoogleMapsEnabledTools=${GOOGLE_MAPS_ENABLED_TOOLS_VALUE}"
    "MapsOpenApiMcpEnabled=${MAPS_OPENAPI_MCP_ENABLED_VALUE}"
    "MapsOpenApiSpecUrl=${MAPS_OPENAPI_SPEC_URL_VALUE}"
    "MapsOpenApiBaseUrl=${MAPS_OPENAPI_BASE_URL_VALUE}"
    "ResyMcpEnabled=${RESY_MCP_ENABLED_VALUE}"
    "OpenTableMcpEnabled=${OPENTABLE_MCP_ENABLED_VALUE}"
    "GmailMcpEnabled=${GMAIL_MCP_ENABLED_VALUE}"
  )

  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation deploy \
      --stack-name "$STACK_NAME_VALUE" \
      --template-file "$ROOT_DIR/ops/aws/friday-agent.yaml" \
      --capabilities CAPABILITY_IAM \
      --parameter-overrides "${common_parameters[@]}"
fi

repository_uri="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
      --query 'Stacks[0].Outputs[?OutputKey==`EcrRepositoryUri`].OutputValue' --output text
)"

if [[ -n "$IMAGE_URI_OVERRIDE_VALUE" ]]; then
  image_uri="$IMAGE_URI_OVERRIDE_VALUE"
else
  registry="${repository_uri%/*}"
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ecr get-login-password | docker login --username AWS --password-stdin "$registry" >/dev/null

  image_uri="${repository_uri}:${IMAGE_TAG_VALUE}"
  docker build --platform linux/amd64 --provenance=false -t "$image_uri" "$ROOT_DIR/agent"
  docker push "$image_uri"
fi

runtime_parameters=(
  "BootstrapOnly=false"
  "AgentName=${AGENT_NAME_VALUE}"
  "ImageUri=${image_uri}"
  "BudgetAlertEmail=${BUDGET_ALERT_EMAIL_VALUE}"
  "AgentReservedConcurrency=${AGENT_RESERVED_CONCURRENCY_VALUE}"
  "SharedHostInstanceId=${SHARED_HOST_INSTANCE_ID_VALUE}"
  "HandsWorkerMode=${HANDS_WORKER_MODE_VALUE}"
  "HandsWorkerInstanceId=${HANDS_WORKER_INSTANCE_ID_VALUE}"
  "DeepSeekApiKeyParam=${DEEPSEEK_API_KEY_PARAM_VALUE}"
  "BraveSearchApiKeyParam=${BRAVE_SEARCH_API_KEY_PARAM_VALUE}"
  "BrowserUseApiKeyParam=${BROWSER_USE_API_KEY_PARAM_VALUE}"
  "FirecrawlApiKeyParam=${FIRECRAWL_API_KEY_PARAM_VALUE}"
  "GoogleMapsApiKeyParam=${GOOGLE_MAPS_API_KEY_PARAM_VALUE}"
  "MapsOpenApiHeadersParam=${MAPS_OPENAPI_HEADERS_PARAM_VALUE}"
  "MapsOpenApiAuthTokenParam=${MAPS_OPENAPI_AUTH_TOKEN_PARAM_VALUE}"
  "ResyApiKeyParam=${RESY_API_KEY_PARAM_VALUE}"
  "ResyAuthTokenParam=${RESY_AUTH_TOKEN_PARAM_VALUE}"
  "OpenTableEmailParam=${OPENTABLE_EMAIL_PARAM_VALUE}"
  "OpenTablePasswordParam=${OPENTABLE_PASSWORD_PARAM_VALUE}"
  "GmailAccountEmailParam=${GMAIL_ACCOUNT_EMAIL_PARAM_VALUE}"
  "GmailAppPasswordParam=${GMAIL_APP_PASSWORD_PARAM_VALUE}"
  "GmailClientIdParam=${GMAIL_CLIENT_ID_PARAM_VALUE}"
  "GmailClientSecretParam=${GMAIL_CLIENT_SECRET_PARAM_VALUE}"
  "GmailRefreshTokenParam=${GMAIL_REFRESH_TOKEN_PARAM_VALUE}"
  "TelegramBotTokenParam=${TELEGRAM_BOT_TOKEN_PARAM_VALUE}"
  "TelegramAllowedChatIdParam=${TELEGRAM_ALLOWED_CHAT_ID_PARAM_VALUE}"
  "TelegramWebhookSecretParam=${TELEGRAM_WEBHOOK_SECRET_PARAM_VALUE}"
  "SiriApiKeyParam=${SIRI_API_KEY_PARAM_VALUE}"
  "WorkerApiKeyParam=${WORKER_API_KEY_PARAM_VALUE}"
  "LogfireTokenParam=${LOGFIRE_TOKEN_PARAM_VALUE}"
  "LogfireEnabled=${LOGFIRE_ENABLED_VALUE}"
  "LogfireFullContent=${LOGFIRE_FULL_CONTENT_VALUE}"
  "BrowserUseEnabled=${BROWSER_USE_ENABLED_VALUE}"
  "BrowserUseCloudEnabled=${BROWSER_USE_CLOUD_ENABLED_VALUE}"
  "BrowserUseModel=${BROWSER_USE_MODEL_VALUE}"
  "BrowserUseCloudModel=${BROWSER_USE_CLOUD_MODEL_VALUE}"
  "BrowserUseCloudProxyCountryCode=${BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE_VALUE}"
  "FirecrawlMcpEnabled=${FIRECRAWL_MCP_ENABLED_VALUE}"
  "SkiplaggedMcpEnabled=${SKIPLAGGED_MCP_ENABLED_VALUE}"
  "SkiplaggedMcpCommand=${SKIPLAGGED_MCP_COMMAND_VALUE}"
  "SkiplaggedMcpArgs=${SKIPLAGGED_MCP_ARGS_VALUE}"
  "GoogleMapsMcpEnabled=${GOOGLE_MAPS_MCP_ENABLED_VALUE}"
  "GoogleMapsEnabledTools=${GOOGLE_MAPS_ENABLED_TOOLS_VALUE}"
  "MapsOpenApiMcpEnabled=${MAPS_OPENAPI_MCP_ENABLED_VALUE}"
  "MapsOpenApiSpecUrl=${MAPS_OPENAPI_SPEC_URL_VALUE}"
  "MapsOpenApiBaseUrl=${MAPS_OPENAPI_BASE_URL_VALUE}"
  "ResyMcpEnabled=${RESY_MCP_ENABLED_VALUE}"
  "OpenTableMcpEnabled=${OPENTABLE_MCP_ENABLED_VALUE}"
  "GmailMcpEnabled=${GMAIL_MCP_ENABLED_VALUE}"
)

AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
  aws cloudformation deploy \
    --stack-name "$STACK_NAME_VALUE" \
    --template-file "$ROOT_DIR/ops/aws/friday-agent.yaml" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides "${runtime_parameters[@]}"

function_url="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
      --query 'Stacks[0].Outputs[?OutputKey==`FunctionUrl`].OutputValue' --output text
)"
telegram_webhook_url="${function_url}telegram"
siri_url="${function_url}siri"

if [[ "$SET_TELEGRAM_WEBHOOK_VALUE" == "1" ]]; then
  token_param="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
        --query 'Stacks[0].Parameters[?ParameterKey==`TelegramBotTokenParam`].ParameterValue' --output text
  )"
  secret_param="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
        --query 'Stacks[0].Parameters[?ParameterKey==`TelegramWebhookSecretParam`].ParameterValue' --output text
  )"
  bot_token="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws ssm get-parameter --name "$token_param" --with-decryption --query 'Parameter.Value' --output text
  )"
  webhook_secret="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws ssm get-parameter --name "$secret_param" --with-decryption --query 'Parameter.Value' --output text
  )"
  curl -fsS "https://api.telegram.org/bot${bot_token}/setWebhook" \
    --data-urlencode "url=${telegram_webhook_url}" \
    --data-urlencode "secret_token=${webhook_secret}" \
    >/dev/null
fi

echo "Agent deployed."
echo "Image: ${image_uri}"
echo "Telegram webhook URL: ${telegram_webhook_url}"
echo "Siri URL: ${siri_url}"
echo "Dashboard: ${AGENT_NAME_VALUE}-operations"
