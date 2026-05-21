#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AWS_PROFILE_NAME="${AWS_PROFILE:-}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
STACK_NAME_VALUE="${STACK_NAME:-friday-shared-host}"
AGENT_STACK_NAME_VALUE="${AGENT_STACK_NAME:-friday-agent}"
INSTANCE_NAME_VALUE="${INSTANCE_NAME:-Friday}"
INSTANCE_TYPE_VALUE="${INSTANCE_TYPE:-t3a.small}"
KEY_NAME_VALUE="${KEY_NAME:-}"
SSH_CIDR_VALUE="${SSH_CIDR:-}"
ROOT_VOLUME_SIZE_GIB_VALUE="${ROOT_VOLUME_SIZE_GIB:-8}"
ENABLE_TEMPORAL_INGRESS_VALUE="${ENABLE_TEMPORAL_INGRESS:-true}"
TEMPORAL_PORT_VALUE="${TEMPORAL_PORT:-7233}"
TEMPORAL_INGRESS_CIDR_VALUE="${TEMPORAL_INGRESS_CIDR:-0.0.0.0/0}"
VPC_ID_VALUE="${VPC_ID:-}"
SUBNET_ID_VALUE="${SUBNET_ID:-}"
IMAGE_ID_VALUE="${IMAGE_ID:-}"
UBUNTU_AMI_SSM_PARAM="${UBUNTU_AMI_SSM_PARAM:-/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id}"
STATE_TABLE_NAME_VALUE="${STATE_TABLE_NAME:-}"
ARTIFACTS_BUCKET_NAME_VALUE="${ARTIFACTS_BUCKET_NAME:-}"

[[ -n "$AWS_PROFILE_NAME" ]] || { echo "AWS_PROFILE is required" >&2; exit 1; }
command -v aws >/dev/null 2>&1 || { echo "required command not found: aws" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "required command not found: curl" >&2; exit 1; }

stack_exists=1
if ! AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
  aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" >/dev/null 2>&1; then
  stack_exists=0
fi

read_stack_param() {
  local key="$1"
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
      --query "Stacks[0].Parameters[?ParameterKey==\`${key}\`].ParameterValue" --output text 2>/dev/null || true
}

if [[ $stack_exists -eq 1 ]]; then
  [[ -n "$KEY_NAME_VALUE" ]] || KEY_NAME_VALUE="$(read_stack_param KeyName)"
  [[ -n "$VPC_ID_VALUE" ]] || VPC_ID_VALUE="$(read_stack_param VpcId)"
  [[ -n "$SUBNET_ID_VALUE" ]] || SUBNET_ID_VALUE="$(read_stack_param SubnetId)"
  [[ -n "$IMAGE_ID_VALUE" ]] || IMAGE_ID_VALUE="$(read_stack_param ImageId)"
  [[ -n "$SSH_CIDR_VALUE" ]] || SSH_CIDR_VALUE="$(read_stack_param SshCidr)"
fi

if [[ -z "$SSH_CIDR_VALUE" ]]; then
  current_ip="$(curl -fsS https://checkip.amazonaws.com | tr -d '\r\n')"
  SSH_CIDR_VALUE="${current_ip}/32"
fi

if [[ -z "$VPC_ID_VALUE" ]]; then
  VPC_ID_VALUE="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text
  )"
fi

if [[ -z "$SUBNET_ID_VALUE" ]]; then
  SUBNET_ID_VALUE="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws ec2 describe-subnets --filters Name=vpc-id,Values="$VPC_ID_VALUE" Name=default-for-az,Values=true --query 'Subnets[0].SubnetId' --output text
  )"
fi

if [[ -z "$IMAGE_ID_VALUE" ]]; then
  IMAGE_ID_VALUE="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws ssm get-parameter --name "$UBUNTU_AMI_SSM_PARAM" --query 'Parameter.Value' --output text
  )"
fi

if [[ -z "$STATE_TABLE_NAME_VALUE" ]]; then
  STATE_TABLE_NAME_VALUE="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws cloudformation describe-stacks --stack-name "$AGENT_STACK_NAME_VALUE" \
        --query 'Stacks[0].Outputs[?OutputKey==`StateTableName`].OutputValue' --output text 2>/dev/null || true
  )"
fi

if [[ -z "$ARTIFACTS_BUCKET_NAME_VALUE" ]]; then
  ARTIFACTS_BUCKET_NAME_VALUE="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws cloudformation describe-stacks --stack-name "$AGENT_STACK_NAME_VALUE" \
        --query 'Stacks[0].Outputs[?OutputKey==`ArtifactsBucketName`].OutputValue' --output text 2>/dev/null || true
  )"
fi

[[ -n "$KEY_NAME_VALUE" ]] || { echo "KEY_NAME is required" >&2; exit 1; }

AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
  aws cloudformation deploy \
    --stack-name "$STACK_NAME_VALUE" \
    --template-file "$ROOT_DIR/ops/aws/friday-shared-host.yaml" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
      "InstanceName=${INSTANCE_NAME_VALUE}" \
      "VpcId=${VPC_ID_VALUE}" \
      "SubnetId=${SUBNET_ID_VALUE}" \
      "KeyName=${KEY_NAME_VALUE}" \
      "ImageId=${IMAGE_ID_VALUE}" \
      "InstanceType=${INSTANCE_TYPE_VALUE}" \
      "SshCidr=${SSH_CIDR_VALUE}" \
      "RootVolumeSizeGiB=${ROOT_VOLUME_SIZE_GIB_VALUE}" \
      "StateTableName=${STATE_TABLE_NAME_VALUE}" \
      "ArtifactsBucketName=${ARTIFACTS_BUCKET_NAME_VALUE}" \
      "EnableTemporalIngress=${ENABLE_TEMPORAL_INGRESS_VALUE}" \
      "TemporalPort=${TEMPORAL_PORT_VALUE}" \
      "TemporalIngressCidr=${TEMPORAL_INGRESS_CIDR_VALUE}"

AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
  aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
    --query 'Stacks[0].Outputs' --output table
