#!/usr/bin/env bash

set -euo pipefail

RUNTIME_TGZ="${1:-}"
API_BASE_URL="${2:-}"
WORKER_API_KEY="${3:-}"
DEEPSEEK_API_KEY="${4:-}"
AGENT_MODEL_VALUE="${5:-deepseek:deepseek-chat}"
REASONER_MODEL_VALUE="${6:-deepseek:deepseek-reasoner}"
SECRETS_ENV_FILE="${7:-}"
WORKER_USER="friday-hands"
WORK_ROOT="/srv/friday-hands"
INSTALL_ROOT="/opt/friday-hands"
CONTAINER_ENGINE_VALUE="${FRIDAY_CONTAINER_ENGINE:-podman}"
SWAPFILE_MB="${FRIDAY_SWAPFILE_MB:-1024}"

[[ -f "$RUNTIME_TGZ" ]] || { echo "runtime archive not found: $RUNTIME_TGZ" >&2; exit 1; }
[[ -n "$API_BASE_URL" ]] || { echo "api base url is required" >&2; exit 1; }
[[ -n "$WORKER_API_KEY" ]] || { echo "worker api key is required" >&2; exit 1; }
[[ -n "$DEEPSEEK_API_KEY" ]] || { echo "deepseek api key is required" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y podman uidmap dbus-user-session slirp4netns fuse-overlayfs jq curl python3

if ! id "$WORKER_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$WORKER_USER"
fi

grep -q "^${WORKER_USER}:" /etc/subuid || echo "${WORKER_USER}:100000:65536" >> /etc/subuid
grep -q "^${WORKER_USER}:" /etc/subgid || echo "${WORKER_USER}:100000:65536" >> /etc/subgid

install -d -o "$WORKER_USER" -g "$WORKER_USER" -m 755 \
  "$WORK_ROOT/workspaces" \
  "$WORK_ROOT/cache" \
  "$WORK_ROOT/artifacts-staging" \
  "$INSTALL_ROOT"

rm -rf "$INSTALL_ROOT"/*
tar -xzf "$RUNTIME_TGZ" -C "$INSTALL_ROOT"
chown -R "$WORKER_USER:$WORKER_USER" "$INSTALL_ROOT" "$WORK_ROOT"

loginctl enable-linger "$WORKER_USER" || true

if [[ "$SWAPFILE_MB" -gt 0 ]] && ! swapon --show | grep -q .; then
  available_kb="$(df --output=avail / | tail -1 | tr -d ' ')"
  required_kb=$((SWAPFILE_MB * 1024 + 1048576))
  if [[ "${available_kb:-0}" -gt "$required_kb" ]]; then
    fallocate -l "${SWAPFILE_MB}M" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count="$SWAPFILE_MB"
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    cat >/etc/sysctl.d/99-friday-hands.conf <<EOF
vm.swappiness=80
vm.vfs_cache_pressure=200
EOF
    sysctl --system >/dev/null 2>&1 || true
  fi
fi

cat >/etc/friday-hands.env <<EOF
FRIDAY_API_BASE_URL=${API_BASE_URL}
FRIDAY_WORKER_KEY=${WORKER_API_KEY}
DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
AGENT_MODEL=${AGENT_MODEL_VALUE}
REASONER_MODEL=${REASONER_MODEL_VALUE}
FRIDAY_WORKSPACE_ROOT=${WORK_ROOT}/workspaces
FRIDAY_POLL_INTERVAL_SECONDS=15
FRIDAY_CONTAINER_ENGINE=${CONTAINER_ENGINE_VALUE}
FRIDAY_WORKER_IMAGE=friday-hands-worker:latest
FRIDAY_WORKER_TIMEOUT_SECONDS=1800
FRIDAY_WORKER_CPUS=0.50
FRIDAY_WORKER_MEMORY=512m
FRIDAY_WORKER_PIDS_LIMIT=512
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
EOF
chmod 600 /etc/friday-hands.env

if [[ -n "$SECRETS_ENV_FILE" && -f "$SECRETS_ENV_FILE" ]]; then
  cat "$SECRETS_ENV_FILE" >> /etc/friday-hands.env
  chmod 600 /etc/friday-hands.env
fi

su - "$WORKER_USER" -c "podman system prune -af >/dev/null 2>&1 || true"
su - "$WORKER_USER" -c "cd '$INSTALL_ROOT' && podman build --format docker -t friday-hands-worker:latest -f hands/worker/Dockerfile ."

cat >/etc/systemd/system/friday-hands-broker.service <<EOF
[Unit]
Description=Friday hands broker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${WORKER_USER}
Group=${WORKER_USER}
EnvironmentFile=/etc/friday-hands.env
ExecStart=/bin/bash -lc 'export XDG_RUNTIME_DIR=/run/user/$(id -u); exec python3 ${INSTALL_ROOT}/hands/host/broker.py'
Restart=always
RestartSec=10
Environment=PODMAN_SYSTEMD_UNIT=friday-hands-broker.service

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now friday-hands-broker.service

echo "Friday hands runtime installed."
