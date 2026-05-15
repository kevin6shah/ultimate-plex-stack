#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AWS_PROFILE_NAME="${AWS_PROFILE:-}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
AGENT_STACK_NAME_VALUE="${AGENT_STACK_NAME:-friday-agent}"
HOST_VALUE="${HOST:-}"
EC2_SSH_KEY_VALUE="${EC2_SSH_KEY:-}"
REMOTE_USER="${EC2_USER:-ubuntu}"
WORKER_API_KEY_PARAM_VALUE="${WORKER_API_KEY_PARAM:-/friday/agent/worker-api-key}"
DEEPSEEK_API_KEY_PARAM_VALUE="${DEEPSEEK_API_KEY_PARAM:-/friday/agent/deepseek-api-key}"

[[ -n "$AWS_PROFILE_NAME" ]] || { echo "AWS_PROFILE is required" >&2; exit 1; }
[[ -n "$HOST_VALUE" ]] || { echo "HOST is required" >&2; exit 1; }
[[ -n "$EC2_SSH_KEY_VALUE" ]] || { echo "EC2_SSH_KEY is required" >&2; exit 1; }
[[ -f "$EC2_SSH_KEY_VALUE" ]] || { echo "ssh key not found: $EC2_SSH_KEY_VALUE" >&2; exit 1; }

function_url="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$AGENT_STACK_NAME_VALUE" \
      --query 'Stacks[0].Outputs[?OutputKey==`FunctionUrl`].OutputValue' --output text
)"
worker_key="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm get-parameter --name "$WORKER_API_KEY_PARAM_VALUE" --with-decryption --query 'Parameter.Value' --output text
)"
deepseek_key="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm get-parameter --name "$DEEPSEEK_API_KEY_PARAM_VALUE" --with-decryption --query 'Parameter.Value' --output text
)"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

tar -czf "$tmp_dir/friday-hands-runtime.tgz" agent/app hands

ssh_opts=(-i "$EC2_SSH_KEY_VALUE" -o StrictHostKeyChecking=accept-new)
scp "${ssh_opts[@]}" "$ROOT_DIR/ops/aws/install-hands-runtime.sh" "$tmp_dir/friday-hands-runtime.tgz" \
  "${REMOTE_USER}@${HOST_VALUE}:/tmp/"

ssh "${ssh_opts[@]}" "${REMOTE_USER}@${HOST_VALUE}" \
  "sudo bash /tmp/install-hands-runtime.sh /tmp/friday-hands-runtime.tgz '${function_url%/}' '$worker_key' '$deepseek_key'"

echo "Friday hands runtime deployed to ${HOST_VALUE}."
