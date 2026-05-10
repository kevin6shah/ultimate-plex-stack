#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

NEW_PUBLIC_IP="${NEW_PUBLIC_IP:-}"
EC2_SSH_KEY_VALUE="${EC2_SSH_KEY:-}"
REMOTE_USER="${EC2_USER:-ubuntu}"
SKIP_LOCAL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-local)
      SKIP_LOCAL=1
      shift
      ;;
    --help)
      echo "usage: EC2_SSH_KEY=/path/to/key.pem $0 <new-public-ip> [--skip-local]" >&2
      exit 0
      ;;
    *)
      if [[ "$1" == --* || -n "$NEW_PUBLIC_IP" ]]; then
        echo "unknown option: $1" >&2
        exit 1
      fi
      NEW_PUBLIC_IP="$1"
      shift
      ;;
  esac
done

[[ -n "$NEW_PUBLIC_IP" ]] || { echo "new public ip is required" >&2; exit 1; }
[[ -n "$EC2_SSH_KEY_VALUE" && -f "$EC2_SSH_KEY_VALUE" ]] || { echo "EC2_SSH_KEY is required and must exist" >&2; exit 1; }

echo "== Remote Iris =="
curl -fsS --max-time 10 "http://${NEW_PUBLIC_IP}/api/iris/preferences" | head -c 250
echo
curl -fsS --max-time 10 "http://${NEW_PUBLIC_IP}/api/iris/state" | head -c 250
echo

echo "== Remote Services =="
ssh -o StrictHostKeyChecking=accept-new -i "$EC2_SSH_KEY_VALUE" "${REMOTE_USER}@${NEW_PUBLIC_IP}" \
  'systemctl is-active wg-quick@wg0 iris-backend nginx && sudo wg show'

if (( SKIP_LOCAL == 1 )); then
  exit 0
fi

echo "== Local Friday =="
./scripts/check-vpn.sh
./scripts/check-stack.sh
