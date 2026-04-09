#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_PATH="${HOME}/Library/LaunchAgents/com.friday.vpn-guard.plist"
SUPPORT_DIR="${HOME}/Library/Application Support/friday-plex-stack"
SCRIPT_DIR="${SUPPORT_DIR}/scripts"
ENV_PATH="${SUPPORT_DIR}/.friday-ops.env"
LOG_DIR="${HOME}/Library/Logs/friday-plex-stack"
LOG_PATH="${LOG_DIR}/vpn-guard.log"
INTERVAL_SECONDS="${VPN_GUARD_LAUNCHD_INTERVAL_SECONDS:-60}"

mkdir -p "$(dirname "$PLIST_PATH")" "$LOG_DIR" "$SCRIPT_DIR"

cp "${ROOT_DIR}/scripts/check-vpn.sh" "${SCRIPT_DIR}/check-vpn.sh"
cp "${ROOT_DIR}/scripts/vpn-guard.sh" "${SCRIPT_DIR}/vpn-guard.sh"
chmod +x "${SCRIPT_DIR}/check-vpn.sh" "${SCRIPT_DIR}/vpn-guard.sh"

SOURCE_ENV="${ROOT_DIR}/.friday-ops.env"
if [[ ! -f "$SOURCE_ENV" ]]; then
  SOURCE_ENV="${ROOT_DIR}/.friday-ops.env.example"
fi

cp "$SOURCE_ENV" "$ENV_PATH"
tmp_env="$(mktemp)"
grep -v '^VPN_GUARD_STATE_FILE=' "$ENV_PATH" >"$tmp_env" || true
printf 'VPN_GUARD_STATE_FILE=%q\n' "${SUPPORT_DIR}/vpn-guard.state" >>"$tmp_env"
mv "$tmp_env" "$ENV_PATH"

cat >"$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.friday.vpn-guard</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd "${SUPPORT_DIR}" &amp;&amp; ./scripts/vpn-guard.sh &gt;&gt; "${LOG_PATH}" 2&gt;&amp;1</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>${INTERVAL_SECONDS}</integer>
  <key>StandardOutPath</key>
  <string>${LOG_PATH}</string>
  <key>StandardErrorPath</key>
  <string>${LOG_PATH}</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load "$PLIST_PATH"

echo "Installed launchd agent at ${PLIST_PATH}"
echo "Installed guard runtime at ${SUPPORT_DIR}"
echo "VPN guard log: ${LOG_PATH}"
echo "Run interval: ${INTERVAL_SECONDS}s"
