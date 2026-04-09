#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .friday-ops.env ]]; then
  # shellcheck disable=SC1091
  source .friday-ops.env
fi

STATE_FILE="${VPN_GUARD_STATE_FILE:-$ROOT_DIR/config/ops/vpn-guard.state}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
TRANSMISSION_CONTAINER="${TRANSMISSION_CONTAINER:-transmission}"
VPN_GUARD_NOTIFY_RECOVERY="${VPN_GUARD_NOTIFY_RECOVERY:-1}"
VPN_GUARD_STOP_TRANSMISSION="${VPN_GUARD_STOP_TRANSMISSION:-1}"
VPN_GUARD_DRY_RUN="${VPN_GUARD_DRY_RUN:-0}"
HOST_LABEL="${VPN_GUARD_HOST_LABEL:-$(hostname -s 2>/dev/null || hostname)}"

mkdir -p "$(dirname "$STATE_FILE")"

PREVIOUS_STATUS="unknown"
PREVIOUS_MESSAGE=""
PREVIOUS_ACTION=""

if [[ -f "$STATE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$STATE_FILE"
  PREVIOUS_STATUS="${STATUS:-unknown}"
  PREVIOUS_MESSAGE="${MESSAGE:-}"
  PREVIOUS_ACTION="${ACTION:-}"
fi

write_state() {
  local status="$1"
  local action="$2"
  local message="$3"
  local timestamp
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  cat >"$STATE_FILE" <<EOF
STATUS=$(printf '%q' "$status")
ACTION=$(printf '%q' "$action")
MESSAGE=$(printf '%q' "$message")
UPDATED_AT=$(printf '%q' "$timestamp")
EOF
}

send_telegram() {
  local text="$1"

  if [[ -z "$TELEGRAM_BOT_TOKEN" || -z "$TELEGRAM_CHAT_ID" ]]; then
    echo "WARN: Telegram bot credentials are not configured; skipping notification." >&2
    return 0
  fi

  curl -fsS \
    -X POST \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${text}" \
    --data-urlencode "disable_web_page_preview=true" \
    >/dev/null
}

transmission_is_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "$TRANSMISSION_CONTAINER" 2>/dev/null || echo false)" == "true" ]]
}

stop_transmission() {
  if [[ "$VPN_GUARD_STOP_TRANSMISSION" != "1" ]]; then
    echo "skip-stop"
    return 0
  fi

  if ! transmission_is_running; then
    echo "already-stopped"
    return 0
  fi

  if [[ "$VPN_GUARD_DRY_RUN" == "1" ]]; then
    echo "dry-run-stop"
    return 0
  fi

  docker stop "$TRANSMISSION_CONTAINER" >/dev/null
  echo "stopped"
}

failure_output=""
if ! failure_output="$(./scripts/check-vpn.sh 2>&1)"; then
  action_taken="$(stop_transmission)"
  alert_message=$(
    cat <<EOF
Friday Plex VPN guard on ${HOST_LABEL} detected an unsafe VPN state.

Transmission action: ${action_taken}
Reason:
${failure_output}

Inspect the VPN before restarting Transmission.
EOF
  )

  if [[ "$PREVIOUS_STATUS" != "unhealthy" ]]; then
    send_telegram "$alert_message"
  fi

  write_state "unhealthy" "$action_taken" "$failure_output"
  echo "$alert_message" >&2
  exit 1
fi

recovery_message=""
if [[ "$PREVIOUS_STATUS" == "unhealthy" && "$VPN_GUARD_NOTIFY_RECOVERY" == "1" ]]; then
  recovery_message=$(
    cat <<EOF
Friday Plex VPN guard on ${HOST_LABEL} reports the VPN is healthy again.

Transmission remains in its current state. Review the VPN and start Transmission manually if you want downloads to resume.
EOF
  )
  send_telegram "$recovery_message"
fi

write_state "healthy" "none" "$failure_output"
echo "OK: VPN healthy on ${HOST_LABEL}"
