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
INSTANCE_TYPE_VALUE="${INSTANCE_TYPE:-t3.micro}"
ROOT_VOLUME_SIZE_GIB_VALUE="${ROOT_VOLUME_SIZE_GIB:-20}"
INSTANCE_NAME_VALUE="${INSTANCE_NAME:-FridayHandsWorker}"
REMOTE_USER="${EC2_USER:-ubuntu}"
AGENT_STACK_NAME_VALUE="${AGENT_STACK_NAME:-friday-agent}"
WORKER_API_KEY_PARAM_VALUE="${WORKER_API_KEY_PARAM:-/friday/agent/worker-api-key}"
DEEPSEEK_API_KEY_PARAM_VALUE="${DEEPSEEK_API_KEY_PARAM:-/friday/agent/deepseek-api-key}"

[[ -n "$AWS_PROFILE_NAME" ]] || { echo "AWS_PROFILE is required" >&2; exit 1; }
[[ -n "$KEY_NAME_VALUE" ]] || { echo "KEY_NAME is required" >&2; exit 1; }
[[ -n "$EC2_SSH_KEY_VALUE" ]] || { echo "EC2_SSH_KEY is required" >&2; exit 1; }
[[ -f "$EC2_SSH_KEY_VALUE" ]] || { echo "ssh key not found: $EC2_SSH_KEY_VALUE" >&2; exit 1; }

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
      "RootVolumeSizeGiB=${ROOT_VOLUME_SIZE_GIB_VALUE}"

instance_id="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
      --query 'Stacks[0].Outputs[?OutputKey==`InstanceId`].OutputValue' --output text
)"
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
/usr/local/bin/docker build --platform linux/amd64 -t friday-hands-worker:latest -f "$ROOT_DIR/hands/worker/Dockerfile" "$ROOT_DIR"
/usr/local/bin/docker save friday-hands-worker:latest | gzip -1 > "$tmp_dir/friday-hands-worker.tar.gz"

ssh_opts=(-i "$EC2_SSH_KEY_VALUE" -o StrictHostKeyChecking=accept-new)
scp "${ssh_opts[@]}" "$ROOT_DIR/ops/aws/install-hands-worker-runtime.sh" "$tmp_dir/friday-hands-runtime.tgz" "$tmp_dir/friday-hands-worker.tar.gz" \
  "${REMOTE_USER}@${public_ip}:/tmp/"

ssh "${ssh_opts[@]}" "${REMOTE_USER}@${public_ip}" \
  "sudo bash /tmp/install-hands-worker-runtime.sh /tmp/friday-hands-runtime.tgz '${function_url%/}' '$worker_key' '$deepseek_key' /tmp/friday-hands-worker.tar.gz"

echo "Dedicated hands worker deployed."
echo "Instance ID: ${instance_id}"
echo "Public IP: ${public_ip}"
