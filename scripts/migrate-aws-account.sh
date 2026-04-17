#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AWS_PROFILE_NAME="${AWS_PROFILE:-}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
EC2_SSH_KEY_VALUE="${EC2_SSH_KEY:-}"
KEY_NAME_VALUE="${KEY_NAME:-}"
INSTANCE_NAME_VALUE="${INSTANCE_NAME:-Friday}"
INSTANCE_TYPE_VALUE="${INSTANCE_TYPE:-t3.micro}"
STACK_NAME_VALUE="${STACK_NAME:-friday-shared-host}"
BACKUP_DIR_VALUE="${BACKUP_DIR:-$ROOT_DIR/backup/aws/latest}"
SSH_CIDR_VALUE="${SSH_CIDR:-}"
VPC_ID_VALUE="${VPC_ID:-}"
SUBNET_ID_VALUE="${SUBNET_ID:-}"
IMAGE_ID_VALUE="${IMAGE_ID:-}"
UBUNTU_AMI_SSM_PARAM="${UBUNTU_AMI_SSM_PARAM:-/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id}"
MTA_LED_SIGN_ROOT="${MTA_LED_SIGN_ROOT:-$HOME/Documents/mta-led-sign}"
RUN_LOCAL_UPDATES=1
CREATE_BACKUP_FIRST=1
CONFIRM=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup-dir)
      BACKUP_DIR_VALUE="$2"
      shift 2
      ;;
    --no-local-updates)
      RUN_LOCAL_UPDATES=0
      shift
      ;;
    --skip-backup)
      CREATE_BACKUP_FIRST=0
      shift
      ;;
    --yes)
      CONFIRM="yes"
      shift
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$CONFIRM" != "yes" ]]; then
  echo "Refusing to create AWS infrastructure without --yes" >&2
  exit 1
fi

for cmd in aws ssh scp curl rg perl tar; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "required command not found: $cmd" >&2; exit 1; }
done

[[ -n "$AWS_PROFILE_NAME" ]] || { echo "AWS_PROFILE is required" >&2; exit 1; }
[[ -n "$EC2_SSH_KEY_VALUE" ]] || { echo "EC2_SSH_KEY is required" >&2; exit 1; }
[[ -n "$KEY_NAME_VALUE" ]] || { echo "KEY_NAME is required" >&2; exit 1; }
[[ -f "$EC2_SSH_KEY_VALUE" ]] || { echo "ssh key not found: $EC2_SSH_KEY_VALUE" >&2; exit 1; }

if [[ "$CREATE_BACKUP_FIRST" -eq 1 ]]; then
  ./scripts/backup-aws-host.sh
  BACKUP_DIR_VALUE="$(readlink backup/aws/latest)"
fi

[[ -d "$BACKUP_DIR_VALUE" ]] || { echo "backup dir not found: $BACKUP_DIR_VALUE" >&2; exit 1; }
[[ -f "$BACKUP_DIR_VALUE/remote/aws-host-state.tgz" ]] || { echo "backup archive missing in $BACKUP_DIR_VALUE" >&2; exit 1; }

old_public_ip="$(sed -n 's/.*"PublicIpAddress": "\(.*\)".*/\1/p' "$BACKUP_DIR_VALUE/metadata/instance.json" | head -n 1)"
old_public_dns="$(sed -n 's/.*"PublicDnsName": "\(.*\)".*/\1/p' "$BACKUP_DIR_VALUE/metadata/instance.json" | head -n 1)"

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

AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
  aws cloudformation deploy \
    --stack-name "$STACK_NAME_VALUE" \
    --template-file "$ROOT_DIR/ops/aws/friday-shared-host.yaml" \
    --parameter-overrides \
      InstanceName="$INSTANCE_NAME_VALUE" \
      VpcId="$VPC_ID_VALUE" \
      SubnetId="$SUBNET_ID_VALUE" \
      KeyName="$KEY_NAME_VALUE" \
      ImageId="$IMAGE_ID_VALUE" \
      InstanceType="$INSTANCE_TYPE_VALUE" \
      SshCidr="$SSH_CIDR_VALUE"

new_public_ip="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
    --query 'Stacks[0].Outputs[?OutputKey==`PublicIp`].OutputValue' --output text
)"

new_public_dns="$(
  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws cloudformation describe-stacks --stack-name "$STACK_NAME_VALUE" \
    --query 'Stacks[0].Outputs[?OutputKey==`PublicDnsName`].OutputValue' --output text
)"

ssh_opts=(-i "$EC2_SSH_KEY_VALUE" -o StrictHostKeyChecking=accept-new)

echo "Waiting for SSH on ${new_public_ip}..."
for _ in $(seq 1 60); do
  if ssh "${ssh_opts[@]}" "ubuntu@${new_public_ip}" 'true' >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

ssh "${ssh_opts[@]}" "ubuntu@${new_public_ip}" 'true' >/dev/null 2>&1 || {
  echo "SSH never became ready on ${new_public_ip}" >&2
  exit 1
}

scp "${ssh_opts[@]}" "$ROOT_DIR/ops/aws/bootstrap-host.sh" "$ROOT_DIR/ops/aws/restore-host-from-backup.sh" \
  "ubuntu@${new_public_ip}:/tmp/"
scp "${ssh_opts[@]}" "$BACKUP_DIR_VALUE/remote/aws-host-state.tgz" "ubuntu@${new_public_ip}:/tmp/"

ssh "${ssh_opts[@]}" "ubuntu@${new_public_ip}" 'sudo bash /tmp/bootstrap-host.sh'
ssh "${ssh_opts[@]}" "ubuntu@${new_public_ip}" 'sudo bash /tmp/restore-host-from-backup.sh /tmp/aws-host-state.tgz'

if [[ "$RUN_LOCAL_UPDATES" -eq 1 ]]; then
  ./scripts/update-vpn-endpoint.sh "$new_public_ip"
  if [[ -d "$MTA_LED_SIGN_ROOT" && -n "$old_public_ip" ]]; then
    ./scripts/update-mta-led-sign-backend-url.sh "http://${old_public_ip}" "http://${new_public_ip}"
  fi
fi

echo "Migration host ready."
echo "New public IP: ${new_public_ip}"
echo "New public DNS: ${new_public_dns}"
if [[ -n "$old_public_ip" ]]; then
  echo "Old public IP still active: ${old_public_ip}"
fi
if [[ -n "$old_public_dns" ]]; then
  echo "Old public DNS: ${old_public_dns}"
fi
echo "Validate Friday VPN and Iris backend before terminating the old instance."
