#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

AWS_PROFILE_NAME="${AWS_PROFILE:-}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
LOGFIRE_ENABLED_VALUE="${LOGFIRE_ENABLED:-false}"

DEEPSEEK_API_KEY_PARAM_VALUE="${DEEPSEEK_API_KEY_PARAM:-/friday/agent/deepseek-api-key}"
BRAVE_SEARCH_API_KEY_PARAM_VALUE="${BRAVE_SEARCH_API_KEY_PARAM:-/friday/agent/brave-search-api-key}"
BROWSER_USE_API_KEY_PARAM_VALUE="${BROWSER_USE_API_KEY_PARAM:-/friday/agent/browser-use-api-key}"
TELEGRAM_BOT_TOKEN_PARAM_VALUE="${TELEGRAM_BOT_TOKEN_PARAM:-/friday/agent/telegram-bot-token}"
TELEGRAM_ALLOWED_CHAT_ID_PARAM_VALUE="${TELEGRAM_ALLOWED_CHAT_ID_PARAM:-/friday/agent/telegram-chat-id}"
TELEGRAM_WEBHOOK_SECRET_PARAM_VALUE="${TELEGRAM_WEBHOOK_SECRET_PARAM:-/friday/agent/telegram-webhook-secret}"
SIRI_API_KEY_PARAM_VALUE="${SIRI_API_KEY_PARAM:-/friday/agent/siri-api-key}"
WORKER_API_KEY_PARAM_VALUE="${WORKER_API_KEY_PARAM:-/friday/agent/worker-api-key}"
LOGFIRE_TOKEN_PARAM_VALUE="${LOGFIRE_TOKEN_PARAM:-/friday/agent/logfire-token}"

[[ -n "$AWS_PROFILE_NAME" ]] || { echo "AWS_PROFILE is required" >&2; exit 1; }
command -v aws >/dev/null 2>&1 || { echo "required command not found: aws" >&2; exit 1; }

put_secret() {
  local name="$1"
  local prompt="$2"
  local value

  printf "%s: " "$prompt" > /dev/tty
  read -r -s value < /dev/tty
  echo
  [[ -n "$value" ]] || { echo "Value required for ${name}" >&2; exit 1; }

  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm put-parameter \
      --name "$name" \
      --type SecureString \
      --value "$value" \
      --overwrite \
      >/dev/null
}

put_optional_secret() {
  local name="$1"
  local prompt="$2"
  local value

  printf "%s (optional, press Enter to skip): " "$prompt" > /dev/tty
  read -r -s value < /dev/tty || true
  echo
  [[ -n "$value" ]] || return 0

  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm put-parameter \
      --name "$name" \
      --type SecureString \
      --value "$value" \
      --overwrite \
      >/dev/null
}

put_plaintext() {
  local name="$1"
  local prompt="$2"
  local value

  printf "%s: " "$prompt" > /dev/tty
  read -r value < /dev/tty
  [[ -n "$value" ]] || { echo "Value required for ${name}" >&2; exit 1; }

  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm put-parameter \
      --name "$name" \
      --type SecureString \
      --value "$value" \
      --overwrite \
      >/dev/null
}

put_secret "$DEEPSEEK_API_KEY_PARAM_VALUE" "DeepSeek API key"
put_optional_secret "$BRAVE_SEARCH_API_KEY_PARAM_VALUE" "Brave Search API key"
put_optional_secret "$BROWSER_USE_API_KEY_PARAM_VALUE" "Browser Use Cloud API key"
put_secret "$TELEGRAM_BOT_TOKEN_PARAM_VALUE" "Telegram bot token"
put_plaintext "$TELEGRAM_ALLOWED_CHAT_ID_PARAM_VALUE" "Telegram allowed chat id"
put_secret "$TELEGRAM_WEBHOOK_SECRET_PARAM_VALUE" "Telegram webhook secret"
put_secret "$SIRI_API_KEY_PARAM_VALUE" "Siri API key"
put_secret "$WORKER_API_KEY_PARAM_VALUE" "Worker API key"

if [[ "$LOGFIRE_ENABLED_VALUE" == "true" ]]; then
  put_secret "$LOGFIRE_TOKEN_PARAM_VALUE" "Logfire token"
fi

echo "Agent SSM parameters updated in ${AWS_PROFILE_NAME}/${AWS_REGION_NAME}."
