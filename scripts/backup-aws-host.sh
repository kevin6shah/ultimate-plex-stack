#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AWS_PROFILE_NAME="${AWS_PROFILE:-friday-ec2}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
EC2_HOST_VALUE="${EC2_HOST:-54.90.132.5}"
EC2_USER_VALUE="${EC2_USER:-ubuntu}"
EC2_SSH_KEY_VALUE="${EC2_SSH_KEY:-$ROOT_DIR/Friday-key-pair-11102025.pem}"
EC2_INSTANCE_ID_VALUE="${EC2_INSTANCE_ID:-i-0c824a5a2b18d31d5}"
EC2_SECURITY_GROUP_ID_VALUE="${EC2_SECURITY_GROUP_ID:-sg-09479da35bed15790}"

if [[ ! -f "$EC2_SSH_KEY_VALUE" ]]; then
  echo "ssh key not found: $EC2_SSH_KEY_VALUE" >&2
  exit 1
fi

for cmd in aws ssh scp tar date; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "required command not found: $cmd" >&2; exit 1; }
done

timestamp="$(date +%Y%m%d-%H%M%S)"
target_dir="$ROOT_DIR/backup/aws/$timestamp"
metadata_dir="$target_dir/metadata"
remote_dir="$target_dir/remote"
mkdir -p "$metadata_dir" "$remote_dir"

AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws sts get-caller-identity >"$metadata_dir/sts-identity.json"
AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws ec2 describe-instances --instance-ids "$EC2_INSTANCE_ID_VALUE" >"$metadata_dir/instance.json"
AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws ec2 describe-security-groups --group-ids "$EC2_SECURITY_GROUP_ID_VALUE" >"$metadata_dir/security-group.json"

remote_tar="/tmp/friday-aws-host-state-${timestamp}.tgz"
ssh_opts=(-i "$EC2_SSH_KEY_VALUE" -o StrictHostKeyChecking=accept-new)

ssh "${ssh_opts[@]}" "${EC2_USER_VALUE}@${EC2_HOST_VALUE}" \
  "sudo tar -C / --exclude='opt/iris-backend/node_modules' -czf '$remote_tar' \
    etc/wireguard/wg0.conf \
    etc/nginx/sites-available/iris-backend \
    etc/systemd/system/iris-backend.service \
    opt/iris-backend"

scp "${ssh_opts[@]}" "${EC2_USER_VALUE}@${EC2_HOST_VALUE}:$remote_tar" "$remote_dir/aws-host-state.tgz"

ssh "${ssh_opts[@]}" "${EC2_USER_VALUE}@${EC2_HOST_VALUE}" \
  "hostname && uname -a && node --version && npm --version && systemctl is-active wg-quick@wg0 iris-backend nginx && sudo wg show" \
  >"$remote_dir/live-summary.txt"

ssh "${ssh_opts[@]}" "${EC2_USER_VALUE}@${EC2_HOST_VALUE}" \
  "sudo rm -f '$remote_tar'"

ln -sfn "$target_dir" "$ROOT_DIR/backup/aws/latest"

echo "AWS host backup written to $target_dir"
echo "Archive: $remote_dir/aws-host-state.tgz"
