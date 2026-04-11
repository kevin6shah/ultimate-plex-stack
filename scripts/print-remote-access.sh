#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPS_ENV="${ROOT_DIR}/.friday-ops.env"

if ! command -v tailscale >/dev/null 2>&1; then
  echo "tailscale CLI not found" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq not found" >&2
  exit 1
fi

TAILSCALE_JSON="$(tailscale status --json)"
TAILSCALE_HOST="$(printf '%s' "${TAILSCALE_JSON}" | jq -r '.Self.DNSName // empty' | sed 's/\.$//')"
TAILSCALE_IP="$(printf '%s' "${TAILSCALE_JSON}" | jq -r '.Self.TailscaleIPs[0] // empty')"
SSH_USER="$(id -un)"

transmission_user() {
  if [[ -f "${OPS_ENV}" ]]; then
    awk -F= '$1=="TRANSMISSION_RPC_USERNAME" {print $2; exit}' "${OPS_ENV}"
  fi
}

check_url() {
  local label="$1"
  local url="$2"
  local extra_args=()

  if [[ "${label}" == "Transmission" ]]; then
    local user
    user="$(transmission_user)"
    if [[ -n "${user}" ]]; then
      extra_args=(-u "${user}:***")
    fi
  fi

  if [[ ${#extra_args[@]} -gt 0 ]]; then
    echo "${label}: ${url} (auth required)"
  else
    echo "${label}: ${url}"
  fi
}

if [[ -z "${TAILSCALE_HOST}" || -z "${TAILSCALE_IP}" ]]; then
  echo "Unable to resolve local Tailscale hostname or IP" >&2
  exit 1
fi

cat <<EOF
Tailscale host: ${TAILSCALE_HOST}
Tailscale IP:   ${TAILSCALE_IP}
SSH:            ssh ${SSH_USER}@${TAILSCALE_HOST}

Remote URLs:
EOF

check_url "Overseerr" "http://${TAILSCALE_HOST}:5055"
check_url "Radarr" "http://${TAILSCALE_HOST}:7878"
check_url "Sonarr" "http://${TAILSCALE_HOST}:8989"
check_url "Bazarr" "http://${TAILSCALE_HOST}:6767"
check_url "Transmission" "http://${TAILSCALE_HOST}:9091/transmission/web/"
check_url "Maintainerr" "http://${TAILSCALE_HOST}:6246"

cat <<EOF

Operational assumptions:
- phone/laptop must be connected to Tailscale
- MacBook must stay awake; lid-closed use depends on Amphetamine + Amphetamine Enhancer + AC power
- Transmission traffic still fails closed if the VPN guard stops the download client
EOF
