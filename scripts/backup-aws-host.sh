#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

WG_FILE="${ROOT_DIR}/config/wireguard/wg_confs/wg0.conf"
AWS_PROFILE_NAME="${AWS_PROFILE:-friday-ec2}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
EC2_HOST_VALUE="${EC2_HOST:-}"
EC2_USER_VALUE="${EC2_USER:-ubuntu}"
EC2_SSH_KEY_VALUE="${EC2_SSH_KEY:-$ROOT_DIR/Friday-key-pair-11102025.pem}"
EC2_INSTANCE_ID_VALUE="${EC2_INSTANCE_ID:-}"
EC2_SECURITY_GROUP_ID_VALUE="${EC2_SECURITY_GROUP_ID:-}"

if [[ -z "$EC2_HOST_VALUE" && -f "$WG_FILE" ]]; then
  EC2_HOST_VALUE="$(sed -n 's/^Endpoint = \([^:]*\):51820$/\1/p' "$WG_FILE" | head -n 1)"
fi

if [[ -z "$EC2_HOST_VALUE" ]]; then
  echo "unable to determine EC2 host; set EC2_HOST or ensure $WG_FILE has the current Endpoint" >&2
  exit 1
fi

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

if [[ -z "$EC2_INSTANCE_ID_VALUE" ]]; then
  EC2_INSTANCE_ID_VALUE="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws ec2 describe-instances \
      --filters "Name=ip-address,Values=${EC2_HOST_VALUE}" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
      --query 'Reservations[0].Instances[0].InstanceId' \
      --output text
  )"
fi

[[ -n "$EC2_INSTANCE_ID_VALUE" && "$EC2_INSTANCE_ID_VALUE" != "None" ]] || {
  echo "unable to determine EC2 instance id for host ${EC2_HOST_VALUE}" >&2
  exit 1
}

AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws ec2 describe-instances --instance-ids "$EC2_INSTANCE_ID_VALUE" >"$metadata_dir/instance.json"

if [[ -z "$EC2_SECURITY_GROUP_ID_VALUE" ]]; then
  EC2_SECURITY_GROUP_ID_VALUE="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws ec2 describe-instances --instance-ids "$EC2_INSTANCE_ID_VALUE" \
      --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' \
      --output text
  )"
fi

[[ -n "$EC2_SECURITY_GROUP_ID_VALUE" && "$EC2_SECURITY_GROUP_ID_VALUE" != "None" ]] || {
  echo "unable to determine security group for instance ${EC2_INSTANCE_ID_VALUE}" >&2
  exit 1
}

AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" aws ec2 describe-security-groups --group-ids "$EC2_SECURITY_GROUP_ID_VALUE" >"$metadata_dir/security-group.json"

cat >"$metadata_dir/discovered.env" <<EOF
AWS_PROFILE=${AWS_PROFILE_NAME}
AWS_REGION=${AWS_REGION_NAME}
EC2_HOST=${EC2_HOST_VALUE}
EC2_INSTANCE_ID=${EC2_INSTANCE_ID_VALUE}
EC2_SECURITY_GROUP_ID=${EC2_SECURITY_GROUP_ID_VALUE}
EC2_USER=${EC2_USER_VALUE}
EC2_SSH_KEY=${EC2_SSH_KEY_VALUE}
EOF

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
