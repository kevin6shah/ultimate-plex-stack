#!/usr/bin/env bash

set -euo pipefail

session_active="unknown"
display_sleep_allowed="unknown"
closed_display_mode="unknown"

if ! pgrep -x Amphetamine >/dev/null 2>&1; then
  echo "FAIL: Amphetamine is not running"
  exit 1
fi

osascript_bool() {
  local command="$1"
  osascript -e "tell application \"Amphetamine\" to ${command}" 2>/dev/null | tr '[:upper:]' '[:lower:]'
}

session_active="$(osascript_bool 'session is active' || echo unknown)"
display_sleep_allowed="$(osascript_bool 'display sleep allowed' || echo unknown)"
closed_display_mode="$(osascript_bool 'closed display mode enabled' || echo unknown)"

echo "Amphetamine running: yes"
echo "Session active: ${session_active}"
echo "Display sleep allowed: ${display_sleep_allowed}"
echo "Closed display mode enabled: ${closed_display_mode}"

if [[ "$session_active" != "true" ]]; then
  echo "FAIL: No active Amphetamine session" >&2
  exit 1
fi

if [[ "$display_sleep_allowed" != "false" ]]; then
  echo "FAIL: Active session allows display sleep" >&2
  exit 1
fi

if [[ "$closed_display_mode" != "true" ]]; then
  echo "FAIL: Closed-display mode is not enabled for the active session" >&2
  exit 1
fi

echo "OK: Amphetamine is configured for lid-closed server use during the active session"
