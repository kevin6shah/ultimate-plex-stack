#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AWS_PROFILE_NAME="${AWS_PROFILE:-}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
UBUNTU_AMI_SSM_PARAM="${UBUNTU_AMI_SSM_PARAM:-/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id}"
STACK_NAME_VALUE="${STACK_NAME:-friday-shared-host}"

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

check_cmd "Default VPC lookup" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws ec2 describe-vpcs --filters Name=isDefault,Values=true

check_cmd "Default subnet lookup" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws ec2 describe-subnets

check_cmd "Ubuntu AMI lookup through SSM" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws ssm get-parameter --name "$UBUNTU_AMI_SSM_PARAM"

check_cmd "CloudFormation template validation" \
  env AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws cloudformation validate-template --template-body "file://$ROOT_DIR/ops/aws/friday-shared-host.yaml"

stack_describe_output="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" 2>&1 || true
)"

if [[ -z "$stack_describe_output" || "$stack_describe_output" == *"does not exist"* ]]; then
  echo "OK: CloudFormation stack describe permission"
elif [[ "$stack_describe_output" == *"AccessDenied"* || "$stack_describe_output" == *"not authorized"* ]]; then
  echo "FAIL: CloudFormation stack describe permission" >&2
  failures=$((failures + 1))
else
  echo "OK: CloudFormation stack describe permission"
fi

if (( failures > 0 )); then
  echo "AWS migration readiness failed with ${failures} issue(s)." >&2
  exit 1
fi

echo "AWS migration readiness checks passed."
