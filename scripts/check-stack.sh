#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .friday-ops.env ]]; then
  # shellcheck disable=SC1091
  source .friday-ops.env
fi

failures=0

report_ok() {
  echo "OK: $*"
}

report_fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

require_container() {
  local name="$1"
  if docker inspect "$name" >/dev/null 2>&1; then
    report_ok "Container '$name' exists"
  else
    report_fail "Container '$name' is missing"
  fi
}

check_http() {
  local label="$1"
  local container="$2"
  local url="$3"
  local expected_codes="$4"
  local code

  code="$(docker exec "$container" curl -sS -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
  if [[ -z "$code" ]]; then
    report_fail "$label did not return an HTTP status"
    return
  fi

  if [[ " $expected_codes " == *" $code "* ]]; then
    report_ok "$label returned HTTP $code"
  else
    report_fail "$label returned HTTP $code (expected: $expected_codes)"
  fi
}

require_path() {
  local path="$1"
  if [[ -d "$path" ]]; then
    report_ok "Path '$path' exists"
  else
    report_fail "Path '$path' is missing"
  fi
}

for container in wireguard transmission plex radarr sonarr prowlarr vpn-web-proxy overseerr maintainerr; do
  require_container "$container"
done

if ./scripts/check-vpn.sh >/dev/null; then
  report_ok "VPN health check passed"
else
  report_fail "VPN health check failed"
fi

check_http "Transmission RPC" "wireguard" "http://localhost:9091/transmission/rpc" "401 409"
check_http "Plex identity" "plex" "http://localhost:32400/identity" "200"
check_http "Radarr ping" "radarr" "http://localhost:7878/ping" "200"
check_http "Sonarr ping" "sonarr" "http://localhost:8989/ping" "200"
check_http "Prowlarr root" "wireguard" "http://localhost:9696/" "200"
check_http "VPN web proxy Prowlarr" "radarr" "http://vpn-web-proxy:9696/" "200"
check_http "Overseerr status" "overseerr" "http://localhost:5055/api/v1/status" "200"
check_http "Maintainerr settings" "maintainerr" "http://localhost:6246/api/settings" "200"

for path in ./share/media ./share/downloads ./config ./config/plex ./config/radarr ./config/sonarr ./config/prowlarr; do
  require_path "$path"
done

if [[ "$failures" -gt 0 ]]; then
  echo "Stack health check completed with ${failures} failure(s)." >&2
  exit 1
fi

echo "Stack health check completed successfully."
