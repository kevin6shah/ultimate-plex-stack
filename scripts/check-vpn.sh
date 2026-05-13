#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .friday-ops.env ]]; then
  # shellcheck disable=SC1091
  source .friday-ops.env
fi

WIREGUARD_CONTAINER="${WIREGUARD_CONTAINER:-wireguard}"
TRANSMISSION_CONTAINER="${TRANSMISSION_CONTAINER:-transmission}"
VPN_MAX_HANDSHAKE_AGE_SECONDS="${VPN_MAX_HANDSHAKE_AGE_SECONDS:-300}"
VPN_PUBLIC_IP_URL="${VPN_PUBLIC_IP_URL:-https://checkip.amazonaws.com}"
VPN_EXPECTED_PUBLIC_IP="${VPN_EXPECTED_PUBLIC_IP:-}"
VPN_EGRESS_CONTAINER="${VPN_EGRESS_CONTAINER:-$WIREGUARD_CONTAINER}"

CHECK_EGRESS=1
if [[ "${1:-}" == "--no-egress" ]]; then
  CHECK_EGRESS=0
fi

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

require_command docker
require_command awk
require_command date

docker inspect "$WIREGUARD_CONTAINER" >/dev/null 2>&1 || fail "Container '$WIREGUARD_CONTAINER' not found"
docker inspect "$TRANSMISSION_CONTAINER" >/dev/null 2>&1 || fail "Container '$TRANSMISSION_CONTAINER' not found"
docker inspect "$VPN_EGRESS_CONTAINER" >/dev/null 2>&1 || fail "Container '$VPN_EGRESS_CONTAINER' not found"

handshakes="$(docker exec "$WIREGUARD_CONTAINER" wg show all latest-handshakes 2>/dev/null || true)"
[[ -n "$handshakes" ]] || fail "WireGuard handshake data is unavailable"

now_epoch="$(date +%s)"
freshest_age=""
freshest_peer=""

while read -r interface peer latest; do
  [[ -n "${latest:-}" ]] || continue
  [[ "$latest" =~ ^[0-9]+$ ]] || continue
  if [[ "$latest" -eq 0 ]]; then
    continue
  fi

  age="$((now_epoch - latest))"
  if [[ -z "$freshest_age" || "$age" -lt "$freshest_age" ]]; then
    freshest_age="$age"
    freshest_peer="$peer"
  fi
done <<<"$handshakes"

[[ -n "$freshest_age" ]] || fail "No successful WireGuard handshake has been recorded"
(( freshest_age <= VPN_MAX_HANDSHAKE_AGE_SECONDS )) || fail "Latest WireGuard handshake is ${freshest_age}s old (limit ${VPN_MAX_HANDSHAKE_AGE_SECONDS}s)"

echo "OK: WireGuard handshake is ${freshest_age}s old for peer ${freshest_peer}"

if [[ "$CHECK_EGRESS" -eq 0 ]]; then
  exit 0
fi

public_ip="$(docker exec "$VPN_EGRESS_CONTAINER" sh -lc "curl -fsSL --max-time 10 '$VPN_PUBLIC_IP_URL' 2>/dev/null || wget -qO- '$VPN_PUBLIC_IP_URL' 2>/dev/null" | tr -d '\r\n' || true)"
[[ -n "$public_ip" ]] || fail "Unable to determine public egress IP from the Transmission network namespace"

if [[ -n "$VPN_EXPECTED_PUBLIC_IP" && "$public_ip" != "$VPN_EXPECTED_PUBLIC_IP" ]]; then
  fail "Public egress IP mismatch: expected ${VPN_EXPECTED_PUBLIC_IP}, got ${public_ip}"
fi

echo "OK: Transmission egress IP is ${public_ip}"
