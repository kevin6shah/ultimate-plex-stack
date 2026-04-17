#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AWS_PROFILE_NAME="${AWS_PROFILE:-}"
AWS_REGION_NAME="${AWS_REGION:-us-east-1}"
KEY_NAME_VALUE="${KEY_NAME:-}"
EC2_SSH_KEY_VALUE="${EC2_SSH_KEY:-}"
CURRENT_IRIS_URL="${CURRENT_IRIS_URL:-http://54.90.132.5/api/iris/preferences}"
LATEST_BACKUP_DIR="${LATEST_BACKUP_DIR:-$ROOT_DIR/backup/aws/latest}"

failures=0

pass() {
  echo "OK: $1"
}

fail() {
  echo "FAIL: $1" >&2
  failures=$((failures + 1))
}

section() {
  echo
  echo "== $1 =="
}

check_cmd() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    pass "$label"
  else
    fail "$label"
  fi
}

section "Local Tools"
for cmd in docker aws ssh scp curl bash; do
  check_cmd "Command available: ${cmd}" command -v "$cmd"
done

section "Blue Health"
if ./scripts/check-vpn.sh >/dev/null 2>&1; then
  pass "Friday VPN health"
else
  fail "Friday VPN health"
fi

if ./scripts/check-stack.sh >/dev/null 2>&1; then
  pass "Friday stack health"
else
  fail "Friday stack health"
fi

if curl -fsS "$CURRENT_IRIS_URL" >/dev/null 2>&1; then
  pass "Iris backend reachable at current host"
else
  fail "Iris backend reachable at current host"
fi

section "Local Migration Inputs"
if [[ -n "$AWS_PROFILE_NAME" ]]; then
  pass "AWS_PROFILE is set (${AWS_PROFILE_NAME})"
else
  fail "AWS_PROFILE is set"
fi

if [[ -n "$KEY_NAME_VALUE" ]]; then
  pass "KEY_NAME is set (${KEY_NAME_VALUE})"
else
  fail "KEY_NAME is set"
fi

if [[ -n "$EC2_SSH_KEY_VALUE" && -f "$EC2_SSH_KEY_VALUE" ]]; then
  pass "EC2_SSH_KEY exists (${EC2_SSH_KEY_VALUE})"
else
  fail "EC2_SSH_KEY exists"
fi

if [[ -L "$LATEST_BACKUP_DIR" || -d "$LATEST_BACKUP_DIR" ]]; then
  if [[ -f "$LATEST_BACKUP_DIR/remote/aws-host-state.tgz" ]]; then
    pass "Latest AWS host backup exists"
  else
    fail "Latest AWS host backup exists"
  fi
else
  fail "Latest AWS host backup exists"
fi

section "Target Account Readiness"
if [[ -n "$AWS_PROFILE_NAME" ]]; then
  if AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" ./scripts/check-aws-migration-readiness.sh >/dev/null 2>&1; then
    pass "Target AWS account readiness"
  else
    fail "Target AWS account readiness"
  fi
else
  fail "Target AWS account readiness"
fi

echo
if (( failures > 0 )); then
  echo "NOT READY: migration day is blocked by ${failures} issue(s)." >&2
  echo "Resolve the failed checks above, then rerun ./scripts/prepare-migration-day.sh." >&2
  exit 1
fi

echo "READY: migration day preflight passed."
echo "If you say 'migrate' with these inputs still valid, the scripted blue/green flow is ready to run."
