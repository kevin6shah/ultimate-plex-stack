#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_PATH="${HOME}/Library/LaunchAgents/com.friday.media-cap.plist"
SUPPORT_DIR="${HOME}/Library/Application Support/friday-plex-stack"
SCRIPT_DIR="${SUPPORT_DIR}/scripts"
ENV_PATH="${SUPPORT_DIR}/.friday-ops.env"
LOG_DIR="${HOME}/Library/Logs/friday-plex-stack"
LOG_PATH="${LOG_DIR}/media-cap.log"

SOURCE_ENV="${ROOT_DIR}/.friday-ops.env"
if [[ ! -f "$SOURCE_ENV" ]]; then
  SOURCE_ENV="${ROOT_DIR}/.friday-ops.env.example"
fi

MODE="${MEDIA_CAP_LAUNCHD_MODE:-dry-run}"
HOUR="${MEDIA_CAP_LAUNCHD_HOUR:-3}"
MINUTE="${MEDIA_CAP_LAUNCHD_MINUTE:-30}"

case "$MODE" in
  dry-run)
    MODE_ARG="--dry-run"
    ;;
  apply)
    MODE_ARG="--apply"
    ;;
  *)
    echo "Unsupported MEDIA_CAP_LAUNCHD_MODE: ${MODE}" >&2
    echo "Expected one of: dry-run, apply" >&2
    exit 1
    ;;
esac

mkdir -p "$(dirname "$PLIST_PATH")" "$LOG_DIR" "$SCRIPT_DIR"

cp "${ROOT_DIR}/scripts/enforce-media-cap.sh" "${SCRIPT_DIR}/enforce-media-cap.sh"
chmod +x "${SCRIPT_DIR}/enforce-media-cap.sh"
cp "$SOURCE_ENV" "$ENV_PATH"
tmp_env="$(mktemp)"
grep -v '^MEDIA_CAP_IO_MODE=' "$ENV_PATH" | \
  grep -v '^PLEX_CONTAINER_NAME=' | \
  grep -v '^MEDIA_CONTAINER_NAME=' >"$tmp_env" || true
printf 'MEDIA_CAP_IO_MODE=%q\n' "docker" >>"$tmp_env"
printf 'PLEX_CONTAINER_NAME=%q\n' "plex" >>"$tmp_env"
printf 'MEDIA_CONTAINER_NAME=%q\n' "plex" >>"$tmp_env"
mv "$tmp_env" "$ENV_PATH"

cat >"$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.friday.media-cap</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd "${SUPPORT_DIR}" &amp;&amp; ./scripts/enforce-media-cap.sh ${MODE_ARG} &gt;&gt; "${LOG_PATH}" 2&gt;&amp;1</string>
  </array>
  <key>RunAtLoad</key>
  <false/>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>${HOUR}</integer>
    <key>Minute</key>
    <integer>${MINUTE}</integer>
  </dict>
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
echo "Installed media-cap runtime at ${SUPPORT_DIR}"
echo "Media-cap log: ${LOG_PATH}"
echo "Schedule: daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE")"
echo "Mode: ${MODE}"
