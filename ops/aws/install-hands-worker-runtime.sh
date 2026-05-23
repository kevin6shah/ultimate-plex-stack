#!/usr/bin/env bash

set -euo pipefail

RUNTIME_TGZ="${1:-}"
API_BASE_URL="${2:-}"
WORKER_API_KEY="${3:-}"
DEEPSEEK_API_KEY="${4:-}"
WORKER_IMAGE_ARCHIVE="${5:-}"
AGENT_MODEL_VALUE="${6:-deepseek:deepseek-chat}"
REASONER_MODEL_VALUE="${7:-deepseek:deepseek-reasoner}"
BROWSER_USE_API_KEY="${8:-}"
BROWSER_USE_CLOUD_ENABLED_VALUE="${9:-false}"
BROWSER_USE_MODEL_VALUE="${10:-deepseek-chat}"
BROWSER_USE_CLOUD_MODEL_VALUE="${11:-bu-latest}"
BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE_VALUE="${12:-us}"
SECRETS_ENV_FILE="${13:-}"
STATE_TABLE_NAME="${14:-}"
ARTIFACTS_BUCKET_NAME="${15:-}"
WORK_ROOT="/srv/friday-hands"
INSTALL_ROOT="/opt/friday-hands"
SWAPFILE_MB="${FRIDAY_SWAPFILE_MB:-2048}"
EXECUTION_BACKEND="${FRIDAY_EXECUTION_BACKEND:-legacy}"
TEMPORAL_HOST_VALUE="${TEMPORAL_HOST:-}"
TEMPORAL_NAMESPACE_VALUE="${TEMPORAL_NAMESPACE:-default}"
TEMPORAL_WORKFLOW_TASK_QUEUE_VALUE="${TEMPORAL_WORKFLOW_TASK_QUEUE:-friday-workflow}"
TEMPORAL_HEAVY_ACTIVITY_TASK_QUEUE_VALUE="${TEMPORAL_HEAVY_ACTIVITY_TASK_QUEUE:-friday-heavy-activity}"

[[ -f "$RUNTIME_TGZ" ]] || { echo "runtime archive not found: $RUNTIME_TGZ" >&2; exit 1; }
[[ -n "$API_BASE_URL" ]] || { echo "api base url is required" >&2; exit 1; }
[[ -n "$WORKER_API_KEY" ]] || { echo "worker api key is required" >&2; exit 1; }
[[ -n "$DEEPSEEK_API_KEY" ]] || { echo "deepseek api key is required" >&2; exit 1; }
if [[ "$EXECUTION_BACKEND" == "temporal" ]]; then
  [[ -n "$STATE_TABLE_NAME" ]] || { echo "state table is required for temporal mode" >&2; exit 1; }
  [[ -n "$ARTIFACTS_BUCKET_NAME" ]] || { echo "artifacts bucket is required for temporal mode" >&2; exit 1; }
  [[ -n "$TEMPORAL_HOST_VALUE" ]] || { echo "TEMPORAL_HOST is required for temporal mode" >&2; exit 1; }
fi

export DEBIAN_FRONTEND=noninteractive
shutdown -c >/dev/null 2>&1 || true
apt-get update
apt-get install -y ca-certificates curl jq docker.io python3

systemctl enable --now docker
usermod -aG docker ubuntu || true

install -d -m 755 \
  "$WORK_ROOT/workspaces" \
  "$WORK_ROOT/cache" \
  "$WORK_ROOT/artifacts-staging" \
  "$INSTALL_ROOT"

systemctl stop friday-hands-broker.service >/dev/null 2>&1 || true

rm -rf "$INSTALL_ROOT"/*
tar -xzf "$RUNTIME_TGZ" -C "$INSTALL_ROOT"

if [[ "$SWAPFILE_MB" -gt 0 ]] && ! swapon --show | grep -q .; then
  available_kb="$(df --output=avail / | tail -1 | tr -d ' ')"
  required_kb=$((SWAPFILE_MB * 1024 + 1048576))
  if [[ "${available_kb:-0}" -gt "$required_kb" ]]; then
    fallocate -l "${SWAPFILE_MB}M" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count="$SWAPFILE_MB"
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
fi

cat >/etc/friday-hands.env <<ENVEOF
FRIDAY_API_BASE_URL=${API_BASE_URL}
FRIDAY_WORKER_KEY=${WORKER_API_KEY}
DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
AGENT_MODEL=${AGENT_MODEL_VALUE}
REASONER_MODEL=${REASONER_MODEL_VALUE}
FRIDAY_WORKSPACE_ROOT=${WORK_ROOT}/workspaces
FRIDAY_POLL_INTERVAL_SECONDS=10
FRIDAY_CONTAINER_ENGINE=docker
FRIDAY_WORKER_IMAGE=friday-hands-worker:latest
FRIDAY_WORKER_TIMEOUT_SECONDS=5400
FRIDAY_WORKER_CPUS=1.50
FRIDAY_WORKER_MEMORY=1536m
FRIDAY_WORKER_PIDS_LIMIT=1024
FRIDAY_STOP_ON_IDLE=1
FRIDAY_IDLE_STOP_SECONDS=600
BROWSER_USE_API_KEY=${BROWSER_USE_API_KEY}
BROWSER_USE_ENABLED=true
BROWSER_USE_CLOUD_ENABLED=${BROWSER_USE_CLOUD_ENABLED_VALUE}
BROWSER_USE_MODEL=${BROWSER_USE_MODEL_VALUE}
BROWSER_USE_CLOUD_MODEL=${BROWSER_USE_CLOUD_MODEL_VALUE}
BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE=${BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE_VALUE}
BROWSER_USE_STEP_TIMEOUT_SECONDS=120
BROWSER_USE_MAX_FAILURES=3
BROWSER_USE_TASK_TIMEOUT_SECONDS=90
CHROME_PATH=/ms-playwright/chromium-1148/chrome-linux/chrome
STAGEHAND_LOCAL_CHROME_PATH=/ms-playwright/chromium-1148/chrome-linux/chrome
MCP_REGISTRATION_TIMEOUT_SECONDS=20
BRAVE_SEARCH_API_KEY_PARAM=/friday/agent/brave-search-api-key
FIRECRAWL_MCP_ENABLED=${FIRECRAWL_MCP_ENABLED:-true}
FIRECRAWL_API_KEY_PARAM=/friday/agent/firecrawl-api-key
SKIPLAGGED_MCP_ENABLED=${SKIPLAGGED_MCP_ENABLED:-true}
SKIPLAGGED_MCP_COMMAND=${SKIPLAGGED_MCP_COMMAND:-npx}
SKIPLAGGED_MCP_ARGS=${SKIPLAGGED_MCP_ARGS:--y mcp-remote https://mcp.skiplagged.com/mcp}
RESTAURANT_CLI_ENABLED=${RESTAURANT_CLI_ENABLED:-true}
RESTAURANT_CLI_COMMAND=${RESTAURANT_CLI_COMMAND:-restaurant}
RESTAURANT_CLI_OT_MODE=${RESTAURANT_CLI_OT_MODE:-auto}
RESTAURANT_CLI_TIMEZONE=${RESTAURANT_CLI_TIMEZONE:-America/New_York}
GOOGLE_MAPS_MCP_ENABLED=${GOOGLE_MAPS_MCP_ENABLED:-true}
GOOGLE_MAPS_API_KEY_PARAM=/friday/agent/google-maps-api-key
GOOGLE_MAPS_ENABLED_TOOLS=
MAPS_OPENAPI_MCP_ENABLED=${MAPS_OPENAPI_MCP_ENABLED:-false}
MAPS_OPENAPI_SPEC_URL=
MAPS_OPENAPI_BASE_URL=
MAPS_OPENAPI_HEADERS_PARAM=/friday/agent/maps-openapi-headers
MAPS_OPENAPI_AUTH_TOKEN_PARAM=/friday/agent/maps-openapi-auth-token
RESY_MCP_ENABLED=${RESY_MCP_ENABLED:-false}
RESY_API_KEY_PARAM=/friday/agent/resy-api-key
RESY_AUTH_TOKEN_PARAM=/friday/agent/resy-auth-token
OPENTABLE_MCP_ENABLED=${OPENTABLE_MCP_ENABLED:-false}
OPENTABLE_EMAIL_PARAM=/friday/agent/opentable-email
OPENTABLE_PASSWORD_PARAM=/friday/agent/opentable-password
GMAIL_MCP_ENABLED=${GMAIL_MCP_ENABLED:-false}
GMAIL_ACCOUNT_EMAIL_PARAM=/friday/agent/gmail-account-email
GMAIL_APP_PASSWORD_PARAM=/friday/agent/gmail-app-password
GOOGLE_CLIENT_ID_PARAM=/friday/agent/gmail-client-id
GOOGLE_CLIENT_SECRET_PARAM=/friday/agent/gmail-client-secret
GOOGLE_REFRESH_TOKEN_PARAM=/friday/agent/gmail-refresh-token
STATE_TABLE=${STATE_TABLE_NAME}
ARTIFACTS_BUCKET=${ARTIFACTS_BUCKET_NAME}
FRIDAY_EXECUTION_BACKEND=${EXECUTION_BACKEND}
TEMPORAL_ENABLED=$([[ "$EXECUTION_BACKEND" == "temporal" ]] && echo true || echo false)
TEMPORAL_HOST=${TEMPORAL_HOST_VALUE}
TEMPORAL_NAMESPACE=${TEMPORAL_NAMESPACE_VALUE}
TEMPORAL_WORKFLOW_TASK_QUEUE=${TEMPORAL_WORKFLOW_TASK_QUEUE_VALUE}
TEMPORAL_HEAVY_ACTIVITY_TASK_QUEUE=${TEMPORAL_HEAVY_ACTIVITY_TASK_QUEUE_VALUE}
TEMPORAL_ACTIVITY_WORKER_IDENTITY=friday-dedicated-activity-worker
TELEGRAM_BOT_TOKEN_PARAM=/friday/agent/telegram-bot-token
ENVEOF
chmod 600 /etc/friday-hands.env

if [[ -n "$SECRETS_ENV_FILE" && -f "$SECRETS_ENV_FILE" ]]; then
  cat "$SECRETS_ENV_FILE" >> /etc/friday-hands.env
  chmod 600 /etc/friday-hands.env
fi

docker system prune -af >/dev/null 2>&1 || true
cd "$INSTALL_ROOT"
if [[ -n "$WORKER_IMAGE_ARCHIVE" && -f "$WORKER_IMAGE_ARCHIVE" ]]; then
  case "$WORKER_IMAGE_ARCHIVE" in
    *.gz)
      gunzip -c "$WORKER_IMAGE_ARCHIVE" | docker load
      ;;
    *)
      docker load -i "$WORKER_IMAGE_ARCHIVE"
      ;;
  esac
else
  docker build --platform linux/amd64 -t friday-hands-worker:latest -f hands/worker/Dockerfile .
fi

cat >/etc/systemd/system/friday-hands-broker.service <<SERVICEEOF
[Unit]
Description=Friday hands broker
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=root
Group=root
EnvironmentFile=/etc/friday-hands.env
ExecStart=/usr/bin/python3 ${INSTALL_ROOT}/hands/host/broker.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
if [[ "$EXECUTION_BACKEND" == "temporal" ]]; then
  systemctl stop friday-hands-broker.service >/dev/null 2>&1 || true
  systemctl disable friday-hands-broker.service >/dev/null 2>&1 || true
  cat >/etc/systemd/system/friday-temporal-activity-worker.service <<SERVICEEOF
[Unit]
Description=Friday Temporal heavy activity worker
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=root
Group=root
EnvironmentFile=/etc/friday-hands.env
ExecStart=/usr/bin/docker run --rm --name friday-temporal-activity-worker --env-file /etc/friday-hands.env --network bridge --mount type=bind,src=${WORK_ROOT}/workspaces,dst=${WORK_ROOT}/workspaces friday-hands-worker:latest python /opt/friday/temporal_activity_worker.py
ExecStop=/usr/bin/docker stop friday-temporal-activity-worker
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICEEOF
  systemctl daemon-reload
  systemctl enable --now friday-temporal-activity-worker.service
  echo "Friday dedicated Temporal activity worker installed."
  exit 0
fi

systemctl enable --now friday-hands-broker.service

echo "Friday dedicated hands worker runtime installed."
