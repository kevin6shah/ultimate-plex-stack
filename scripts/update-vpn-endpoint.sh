#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

NEW_PUBLIC_IP="${1:-}"
WG_FILE="$ROOT_DIR/config/wireguard/wg_confs/wg0.conf"
LOCAL_ENV="$ROOT_DIR/.friday-ops.env"
SUPPORT_ENV="$HOME/Library/Application Support/friday-plex-stack/.friday-ops.env"

if [[ -z "$NEW_PUBLIC_IP" ]]; then
  echo "usage: $0 <new-public-ip>" >&2
  exit 1
fi

if [[ ! -f "$WG_FILE" ]]; then
  echo "wireguard config not found: $WG_FILE" >&2
  exit 1
fi

perl -0pi -e "s#^Endpoint = .*:51820#Endpoint = ${NEW_PUBLIC_IP}:51820#m" "$WG_FILE"

for env_file in "$LOCAL_ENV" "$SUPPORT_ENV"; do
  [[ -f "$env_file" ]] || continue
  if rg -q '^VPN_EXPECTED_PUBLIC_IP=' "$env_file"; then
    perl -0pi -e "s#^VPN_EXPECTED_PUBLIC_IP=.*#VPN_EXPECTED_PUBLIC_IP=${NEW_PUBLIC_IP}#m" "$env_file"
  else
    printf '\nVPN_EXPECTED_PUBLIC_IP=%s\n' "$NEW_PUBLIC_IP" >>"$env_file"
  fi
done

echo "Updated WireGuard endpoint and VPN expected public IP to ${NEW_PUBLIC_IP}"
