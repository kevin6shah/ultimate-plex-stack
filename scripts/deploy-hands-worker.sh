#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AWS_PROFILE_NAME="${AWS_PROFILE:-}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
STACK_NAME_VALUE="${HANDS_WORKER_STACK_NAME:-friday-hands-worker}"
KEY_NAME_VALUE="${KEY_NAME:-}"
EC2_SSH_KEY_VALUE="${EC2_SSH_KEY:-}"
SSH_CIDR_VALUE="${SSH_CIDR:-0.0.0.0/0}"
INSTANCE_TYPE_VALUE="${INSTANCE_TYPE:-t3a.small}"
ROOT_VOLUME_SIZE_GIB_VALUE="${ROOT_VOLUME_SIZE_GIB:-20}"
INSTANCE_NAME_VALUE="${INSTANCE_NAME:-FridayHandsWorker}"
REMOTE_USER="${EC2_USER:-ubuntu}"
AGENT_STACK_NAME_VALUE="${AGENT_STACK_NAME:-friday-agent}"
SHARED_HOST_STACK_NAME_VALUE="${SHARED_HOST_STACK_NAME:-friday-shared-host}"
WORKER_API_KEY_PARAM_VALUE="${WORKER_API_KEY_PARAM:-/friday/agent/worker-api-key}"
DEEPSEEK_API_KEY_PARAM_VALUE="${DEEPSEEK_API_KEY_PARAM:-/friday/agent/deepseek-api-key}"
BROWSER_USE_API_KEY_PARAM_VALUE="${BROWSER_USE_API_KEY_PARAM:-/friday/agent/browser-use-api-key}"
BRAVE_SEARCH_API_KEY_PARAM_VALUE="${BRAVE_SEARCH_API_KEY_PARAM:-/friday/agent/brave-search-api-key}"
FIRECRAWL_API_KEY_PARAM_VALUE="${FIRECRAWL_API_KEY_PARAM:-/friday/agent/firecrawl-api-key}"
GOOGLE_MAPS_API_KEY_PARAM_VALUE="${GOOGLE_MAPS_API_KEY_PARAM:-/friday/agent/google-maps-api-key}"
RESY_API_KEY_PARAM_VALUE="${RESY_API_KEY_PARAM:-/friday/agent/resy-api-key}"
RESY_AUTH_TOKEN_PARAM_VALUE="${RESY_AUTH_TOKEN_PARAM:-/friday/agent/resy-auth-token}"
OPENTABLE_EMAIL_PARAM_VALUE="${OPENTABLE_EMAIL_PARAM:-/friday/agent/opentable-email}"
OPENTABLE_PASSWORD_PARAM_VALUE="${OPENTABLE_PASSWORD_PARAM:-/friday/agent/opentable-password}"
AGENT_MODEL_VALUE="${AGENT_MODEL:-deepseek:deepseek-chat}"
REASONER_MODEL_VALUE="${REASONER_MODEL:-deepseek:deepseek-reasoner}"
BROWSER_USE_CLOUD_ENABLED_VALUE="${BROWSER_USE_CLOUD_ENABLED:-false}"
BROWSER_USE_MODEL_VALUE="${BROWSER_USE_MODEL:-deepseek-chat}"
BROWSER_USE_CLOUD_MODEL_VALUE="${BROWSER_USE_CLOUD_MODEL:-bu-latest}"
BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE_VALUE="${BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE:-us}"
EXECUTION_BACKEND_VALUE="${FRIDAY_EXECUTION_BACKEND:-temporal}"
TEMPORAL_HOST_VALUE="${TEMPORAL_HOST:-}"
TEMPORAL_NAMESPACE_VALUE="${TEMPORAL_NAMESPACE:-default}"
TEMPORAL_WORKFLOW_TASK_QUEUE_VALUE="${TEMPORAL_WORKFLOW_TASK_QUEUE:-friday-workflow}"
TEMPORAL_HEAVY_ACTIVITY_TASK_QUEUE_VALUE="${TEMPORAL_HEAVY_ACTIVITY_TASK_QUEUE:-friday-heavy-activity}"

[[ -n "$AWS_PROFILE_NAME" ]] || { echo "AWS_PROFILE is required" >&2; exit 1; }
[[ -n "$KEY_NAME_VALUE" ]] || { echo "KEY_NAME is required" >&2; exit 1; }
[[ -n "$EC2_SSH_KEY_VALUE" ]] || { echo "EC2_SSH_KEY is required" >&2; exit 1; }
[[ -f "$EC2_SSH_KEY_VALUE" ]] || { echo "ssh key not found: $EC2_SSH_KEY_VALUE" >&2; exit 1; }

if [[ "$EXECUTION_BACKEND_VALUE" == "temporal" && -z "$TEMPORAL_HOST_VALUE" ]]; then
  shared_host_public_dns="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws cloudformation describe-stacks --stack-name "$SHARED_HOST_STACK_NAME_VALUE" \
        --query 'Stacks[0].Outputs[?OutputKey==`PublicDnsName`].OutputValue' --output text 2>/dev/null || true
  )"
  if [[ -n "$shared_host_public_dns" && "$shared_host_public_dns" != "None" ]]; then
    TEMPORAL_HOST_VALUE="${shared_host_public_dns}:7233"
  fi
fi

vpc_id="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text
)"
subnet_id="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ec2 describe-subnets --filters Name=default-for-az,Values=true Name=vpc-id,Values="$vpc_id" --query 'Subnets[0].SubnetId' --output text
)"
image_id="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id --query 'Parameter.Value' --output text
)"
state_table_name="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$AGENT_STACK_NAME_VALUE" \
      --query 'Stacks[0].Outputs[?OutputKey==`StateTableName`].OutputValue' --output text
)"
artifacts_bucket_name="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$AGENT_STACK_NAME_VALUE" \
      --query 'Stacks[0].Outputs[?OutputKey==`ArtifactsBucketName`].OutputValue' --output text
)"

AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
  aws cloudformation deploy \
    --stack-name "$STACK_NAME_VALUE" \
    --template-file "$ROOT_DIR/ops/aws/friday-hands-worker.yaml" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
      "InstanceName=${INSTANCE_NAME_VALUE}" \
      "VpcId=${vpc_id}" \
      "SubnetId=${subnet_id}" \
      "KeyName=${KEY_NAME_VALUE}" \
      "ImageId=${image_id}" \
      "InstanceType=${INSTANCE_TYPE_VALUE}" \
      "SshCidr=${SSH_CIDR_VALUE}" \
      "RootVolumeSizeGiB=${ROOT_VOLUME_SIZE_GIB_VALUE}" \
      "StateTableName=${state_table_name}" \
      "ArtifactsBucketName=${artifacts_bucket_name}"

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
browser_use_key="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm get-parameter --name "$BROWSER_USE_API_KEY_PARAM_VALUE" --with-decryption --query 'Parameter.Value' --output text 2>/dev/null || true
)"
brave_search_key="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm get-parameter --name "$BRAVE_SEARCH_API_KEY_PARAM_VALUE" --with-decryption --query 'Parameter.Value' --output text 2>/dev/null || true
)"
firecrawl_key="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm get-parameter --name "$FIRECRAWL_API_KEY_PARAM_VALUE" --with-decryption --query 'Parameter.Value' --output text 2>/dev/null || true
)"
google_maps_key="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm get-parameter --name "$GOOGLE_MAPS_API_KEY_PARAM_VALUE" --with-decryption --query 'Parameter.Value' --output text 2>/dev/null || true
)"
resy_api_key="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm get-parameter --name "$RESY_API_KEY_PARAM_VALUE" --with-decryption --query 'Parameter.Value' --output text 2>/dev/null || true
)"
resy_auth_token="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm get-parameter --name "$RESY_AUTH_TOKEN_PARAM_VALUE" --with-decryption --query 'Parameter.Value' --output text 2>/dev/null || true
)"
opentable_email="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm get-parameter --name "$OPENTABLE_EMAIL_PARAM_VALUE" --with-decryption --query 'Parameter.Value' --output text 2>/dev/null || true
)"
opentable_password="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm get-parameter --name "$OPENTABLE_PASSWORD_PARAM_VALUE" --with-decryption --query 'Parameter.Value' --output text 2>/dev/null || true
)"

instance_id="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
      --query 'Stacks[0].Outputs[?OutputKey==`InstanceId`].OutputValue' --output text
)"

AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
  aws ec2 wait instance-running --instance-ids "$instance_id"

public_ip="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ec2 describe-instances --instance-ids "$instance_id" \
      --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
)"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
COPYFILE_DISABLE=1 tar -czf "$tmp_dir/friday-hands-runtime.tgz" agent/app hands
remote_worker_image_archive=""
if command -v /usr/local/bin/docker >/dev/null 2>&1 && /usr/local/bin/docker info >/dev/null 2>&1; then
  /usr/local/bin/docker build --platform linux/amd64 -t friday-hands-worker:latest -f "$ROOT_DIR/hands/worker/Dockerfile" "$ROOT_DIR"
  /usr/local/bin/docker save friday-hands-worker:latest | gzip -1 > "$tmp_dir/friday-hands-worker.tar.gz"
  remote_worker_image_archive="/tmp/friday-hands-worker.tar.gz"
else
  echo "Local Docker unavailable; falling back to remote worker image build." >&2
fi
cat >"$tmp_dir/friday-hands-secrets.env" <<EOF
BRAVE_SEARCH_API_KEY_PARAM=
BRAVE_SEARCH_API_KEY=${brave_search_key}
FIRECRAWL_API_KEY_PARAM=
FIRECRAWL_API_KEY=${firecrawl_key}
GOOGLE_MAPS_API_KEY_PARAM=
GOOGLE_MAPS_API_KEY=${google_maps_key}
RESY_API_KEY_PARAM=
RESY_API_KEY=${resy_api_key}
RESY_AUTH_TOKEN_PARAM=
RESY_AUTH_TOKEN=${resy_auth_token}
OPENTABLE_EMAIL_PARAM=
OPENTABLE_EMAIL=${opentable_email}
OPENTABLE_PASSWORD_PARAM=
OPENTABLE_PASSWORD=${opentable_password}
GMAIL_ACCOUNT_EMAIL_PARAM=
GMAIL_APP_PASSWORD_PARAM=
GMAIL_CLIENT_ID_PARAM=
GMAIL_CLIENT_SECRET_PARAM=
GMAIL_REFRESH_TOKEN_PARAM=
EOF

ssh_opts=(-i "$EC2_SSH_KEY_VALUE" -o StrictHostKeyChecking=accept-new)
scp_args=(
  "${ssh_opts[@]}"
  "$ROOT_DIR/ops/aws/install-hands-worker-runtime.sh"
  "$tmp_dir/friday-hands-runtime.tgz"
  "$tmp_dir/friday-hands-secrets.env"
)
if [[ -n "$remote_worker_image_archive" ]]; then
  scp_args+=("$tmp_dir/friday-hands-worker.tar.gz")
fi
scp_args+=("${REMOTE_USER}@${public_ip}:/tmp/")
scp "${scp_args[@]}"

ssh "${ssh_opts[@]}" "${REMOTE_USER}@${public_ip}" \
  "sudo FRIDAY_EXECUTION_BACKEND='${EXECUTION_BACKEND_VALUE}' TEMPORAL_HOST='${TEMPORAL_HOST_VALUE}' TEMPORAL_NAMESPACE='${TEMPORAL_NAMESPACE_VALUE}' TEMPORAL_WORKFLOW_TASK_QUEUE='${TEMPORAL_WORKFLOW_TASK_QUEUE_VALUE}' TEMPORAL_HEAVY_ACTIVITY_TASK_QUEUE='${TEMPORAL_HEAVY_ACTIVITY_TASK_QUEUE_VALUE}' bash /tmp/install-hands-worker-runtime.sh /tmp/friday-hands-runtime.tgz '${function_url%/}' '$worker_key' '$deepseek_key' '${remote_worker_image_archive}' '$AGENT_MODEL_VALUE' '$REASONER_MODEL_VALUE' '$browser_use_key' '$BROWSER_USE_CLOUD_ENABLED_VALUE' '$BROWSER_USE_MODEL_VALUE' '$BROWSER_USE_CLOUD_MODEL_VALUE' '$BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE_VALUE' /tmp/friday-hands-secrets.env '${state_table_name}' '${artifacts_bucket_name}'"

echo "Dedicated hands worker deployed."
echo "Instance ID: ${instance_id}"
echo "Public IP: ${public_ip}"
