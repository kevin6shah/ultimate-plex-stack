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
BUDGET_ALERT_EMAIL_VALUE="${BUDGET_ALERT_EMAIL:-}"
AGENT_RESERVED_CONCURRENCY_VALUE="${AGENT_RESERVED_CONCURRENCY:-0}"
HANDS_WORKER_MODE_VALUE="${HANDS_WORKER_MODE:-shared_host}"
HANDS_WORKER_INSTANCE_ID_VALUE="${HANDS_WORKER_INSTANCE_ID:-}"
SET_TELEGRAM_WEBHOOK_VALUE="${SET_TELEGRAM_WEBHOOK:-0}"
DEEPSEEK_API_KEY_PARAM_VALUE="${DEEPSEEK_API_KEY_PARAM:-/friday/agent/deepseek-api-key}"
TELEGRAM_BOT_TOKEN_PARAM_VALUE="${TELEGRAM_BOT_TOKEN_PARAM:-/friday/agent/telegram-bot-token}"
TELEGRAM_ALLOWED_CHAT_ID_PARAM_VALUE="${TELEGRAM_ALLOWED_CHAT_ID_PARAM:-/friday/agent/telegram-chat-id}"
TELEGRAM_WEBHOOK_SECRET_PARAM_VALUE="${TELEGRAM_WEBHOOK_SECRET_PARAM:-/friday/agent/telegram-webhook-secret}"
SIRI_API_KEY_PARAM_VALUE="${SIRI_API_KEY_PARAM:-/friday/agent/siri-api-key}"
WORKER_API_KEY_PARAM_VALUE="${WORKER_API_KEY_PARAM:-/friday/agent/worker-api-key}"
LOGFIRE_TOKEN_PARAM_VALUE="${LOGFIRE_TOKEN_PARAM:-/friday/agent/logfire-token}"
LOGFIRE_ENABLED_VALUE="${LOGFIRE_ENABLED:-false}"
LOGFIRE_FULL_CONTENT_VALUE="${LOGFIRE_FULL_CONTENT:-false}"
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
    "TelegramBotTokenParam=${TELEGRAM_BOT_TOKEN_PARAM_VALUE}"
    "TelegramAllowedChatIdParam=${TELEGRAM_ALLOWED_CHAT_ID_PARAM_VALUE}"
    "TelegramWebhookSecretParam=${TELEGRAM_WEBHOOK_SECRET_PARAM_VALUE}"
    "SiriApiKeyParam=${SIRI_API_KEY_PARAM_VALUE}"
    "WorkerApiKeyParam=${WORKER_API_KEY_PARAM_VALUE}"
    "LogfireTokenParam=${LOGFIRE_TOKEN_PARAM_VALUE}"
    "LogfireEnabled=${LOGFIRE_ENABLED_VALUE}"
    "LogfireFullContent=${LOGFIRE_FULL_CONTENT_VALUE}"
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

registry="${repository_uri%/*}"
AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
  aws ecr get-login-password | docker login --username AWS --password-stdin "$registry" >/dev/null

image_uri="${repository_uri}:${IMAGE_TAG_VALUE}"
docker build --platform linux/amd64 --provenance=false -t "$image_uri" "$ROOT_DIR/agent"
docker push "$image_uri"

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
  "TelegramBotTokenParam=${TELEGRAM_BOT_TOKEN_PARAM_VALUE}"
  "TelegramAllowedChatIdParam=${TELEGRAM_ALLOWED_CHAT_ID_PARAM_VALUE}"
  "TelegramWebhookSecretParam=${TELEGRAM_WEBHOOK_SECRET_PARAM_VALUE}"
  "SiriApiKeyParam=${SIRI_API_KEY_PARAM_VALUE}"
  "WorkerApiKeyParam=${WORKER_API_KEY_PARAM_VALUE}"
  "LogfireTokenParam=${LOGFIRE_TOKEN_PARAM_VALUE}"
  "LogfireEnabled=${LOGFIRE_ENABLED_VALUE}"
  "LogfireFullContent=${LOGFIRE_FULL_CONTENT_VALUE}"
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
