#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DISCOVERED_ENV_FILE="${ROOT_DIR}/backup/aws/latest/metadata/discovered.env"
read_discovered_env() {
  local key="$1"
  if [[ -f "$DISCOVERED_ENV_FILE" ]]; then
    sed -n "s/^${key}=//p" "$DISCOVERED_ENV_FILE" | head -n 1
  fi
}

AWS_PROFILE_NAME="${AWS_PROFILE:-$(read_discovered_env AWS_PROFILE)}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
STACK_NAME_VALUE="${AGENT_STACK_NAME:-friday-agent}"

[[ -n "$AWS_PROFILE_NAME" ]] || { echo "AWS_PROFILE is required; set it explicitly or ensure $DISCOVERED_ENV_FILE exists" >&2; exit 1; }

for cmd in aws date; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "required command not found: $cmd" >&2; exit 1; }
done

timestamp="$(date +%Y%m%d-%H%M%S)"
target_dir="$ROOT_DIR/backup/aws-agent/$timestamp"
metadata_dir="$target_dir/metadata"
mkdir -p "$metadata_dir"

AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws sts get-caller-identity >"$metadata_dir/sts-identity.json"
AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" >"$metadata_dir/stack.json"

function_name="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
      --query 'Stacks[0].Parameters[?ParameterKey==`AgentName`].ParameterValue' --output text
)"

repository_uri="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
      --query 'Stacks[0].Outputs[?OutputKey==`EcrRepositoryUri`].OutputValue' --output text
)"

table_name="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
      --query 'Stacks[0].Outputs[?OutputKey==`StateTableName`].OutputValue' --output text 2>/dev/null || true
)"

queue_url="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
      --query 'Stacks[0].Outputs[?OutputKey==`QueueUrl`].OutputValue' --output text 2>/dev/null || true
)"

artifacts_bucket="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
      --query 'Stacks[0].Outputs[?OutputKey==`ArtifactsBucketName`].OutputValue' --output text 2>/dev/null || true
)"

if [[ -n "$function_name" && "$function_name" != "None" ]]; then
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws lambda get-function-configuration --function-name "$function_name" >"$metadata_dir/lambda.json"
fi

AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws cloudwatch list-dashboards >"$metadata_dir/cloudwatch-dashboards.json" 2>/dev/null || true
AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws cloudwatch describe-alarms >"$metadata_dir/cloudwatch-alarms.json" 2>/dev/null || true
AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws sns list-topics >"$metadata_dir/sns-topics.json" 2>/dev/null || true

if [[ -n "$repository_uri" && "$repository_uri" != "None" ]]; then
  repository_name="${repository_uri##*/}"
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws ecr describe-images --repository-name "$repository_name" >"$metadata_dir/ecr-images.json" 2>/dev/null || true
fi

if [[ -n "$table_name" && "$table_name" != "None" ]]; then
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws dynamodb describe-table --table-name "$table_name" >"$metadata_dir/dynamodb-table.json"
fi

if [[ -n "$queue_url" && "$queue_url" != "None" ]]; then
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws sqs get-queue-attributes --queue-url "$queue_url" --attribute-names All >"$metadata_dir/sqs-queue.json"
fi

if [[ -n "$artifacts_bucket" && "$artifacts_bucket" != "None" ]]; then
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws s3api get-bucket-location --bucket "$artifacts_bucket" >"$metadata_dir/s3-bucket-location.json"
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws s3api get-bucket-lifecycle-configuration --bucket "$artifacts_bucket" >"$metadata_dir/s3-bucket-lifecycle.json" 2>/dev/null || true
fi

ln -sfn "$target_dir" "$ROOT_DIR/backup/aws-agent/latest"

echo "Agent backup metadata written to $target_dir"
echo "No plaintext SSM secret values were exported."
