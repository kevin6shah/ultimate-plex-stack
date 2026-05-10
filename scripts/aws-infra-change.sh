#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REASON=""

usage() {
  cat >&2 <<'EOF'
usage:
  AWS_PROFILE=<profile> ./scripts/aws-infra-change.sh --reason "short reason" -- <command ...>

example:
  AWS_PROFILE=iris ./scripts/aws-infra-change.sh --reason "update shared-host stack" -- \
    aws cloudformation update-stack ...
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reason)
      REASON="${2:-}"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      usage
      ;;
  esac
done

[[ -n "$REASON" ]] || usage
[[ $# -gt 0 ]] || usage

timestamp="$(date +%Y%m%d-%H%M%S)"
change_log_dir="$ROOT_DIR/backup/aws/change-log"
mkdir -p "$change_log_dir"
change_log_file="$change_log_dir/${timestamp}.log"

{
  echo "timestamp=${timestamp}"
  echo "reason=${REASON}"
  echo "cwd=${ROOT_DIR}"
  echo "aws_profile=${AWS_PROFILE:-}"
  echo "aws_region=${AWS_REGION:-us-east-1}"
  printf 'command='
  printf '%q ' "$@"
  echo
} >"$change_log_file"

echo "== Pre-change backup =="
./scripts/backup-aws-host.sh

echo "== AWS infra change =="
"$@" 2>&1 | tee -a "$change_log_file"

echo "== Post-change backup =="
./scripts/backup-aws-host.sh

echo "Recorded AWS infra change: $change_log_file"
