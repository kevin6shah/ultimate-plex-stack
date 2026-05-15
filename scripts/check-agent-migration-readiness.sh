#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AWS_PROFILE_NAME="${AWS_PROFILE:-}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
AGENT_STACK_NAME_VALUE="${AGENT_STACK_NAME:-friday-agent}"

PARAMETERS=(
  "${DEEPSEEK_API_KEY_PARAM:-/friday/agent/deepseek-api-key}"
  "${TELEGRAM_BOT_TOKEN_PARAM:-/friday/agent/telegram-bot-token}"
  "${TELEGRAM_ALLOWED_CHAT_ID_PARAM:-/friday/agent/telegram-chat-id}"
  "${TELEGRAM_WEBHOOK_SECRET_PARAM:-/friday/agent/telegram-webhook-secret}"
  "${SIRI_API_KEY_PARAM:-/friday/agent/siri-api-key}"
  "${WORKER_API_KEY_PARAM:-/friday/agent/worker-api-key}"
)
if [[ "${LOGFIRE_ENABLED:-false}" == "true" ]]; then
  PARAMETERS+=("${LOGFIRE_TOKEN_PARAM:-/friday/agent/logfire-token}")
fi

[[ -n "$AWS_PROFILE_NAME" ]] || { echo "AWS_PROFILE is required" >&2; exit 1; }

failures=0

check_cmd() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "OK: ${label}"
  else
    echo "FAIL: ${label}" >&2
    failures=$((failures + 1))
  fi
}

check_cmd "STS identity" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws sts get-caller-identity

check_cmd "CloudFormation agent template validation" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws cloudformation validate-template --template-body "file://$ROOT_DIR/ops/aws/friday-agent.yaml"

check_cmd "ECR authorization" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws ecr get-authorization-token

check_cmd "Lambda list permission" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws lambda list-functions --max-items 1

check_cmd "SNS list permission" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws sns list-topics

check_cmd "SQS list permission" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws sqs list-queues --max-results 1

check_cmd "DynamoDB list permission" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws dynamodb list-tables --limit 1

check_cmd "EventBridge list permission" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws events list-rules --limit 1

check_cmd "CloudWatch alarm list permission" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws cloudwatch describe-alarms --max-records 1

check_cmd "CloudWatch dashboard list permission" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws cloudwatch list-dashboards

account_id="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws sts get-caller-identity --query Account --output text 2>/dev/null || true
)"

if [[ -n "$account_id" ]]; then
  check_cmd "Budgets describe permission" \
    env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws budgets describe-budgets --account-id "$account_id" --max-results 1
fi

for parameter in "${PARAMETERS[@]}"; do
  check_cmd "SSM parameter exists: ${parameter}" \
    env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws ssm get-parameter --name "$parameter" --with-decryption
done

stack_describe_output="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$AGENT_STACK_NAME_VALUE" 2>&1 || true
)"

if [[ -z "$stack_describe_output" || "$stack_describe_output" == *"does not exist"* ]]; then
  echo "OK: CloudFormation stack describe permission"
elif [[ "$stack_describe_output" == *"AccessDenied"* || "$stack_describe_output" == *"not authorized"* ]]; then
  echo "FAIL: CloudFormation stack describe permission" >&2
  failures=$((failures + 1))
else
  echo "OK: CloudFormation stack describe permission"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "FAIL: docker command available" >&2
  failures=$((failures + 1))
else
  echo "OK: docker command available"
fi

if (( failures > 0 )); then
  echo "Agent migration readiness failed with ${failures} issue(s)." >&2
  exit 1
fi

echo "Agent migration readiness checks passed."
