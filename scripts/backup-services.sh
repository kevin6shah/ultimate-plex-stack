#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

timestamp="$(date +%Y%m%d-%H%M%S)"
target_dir="backup/${timestamp}"

mkdir -p "$target_dir"

tar -czf "${target_dir}/stack-config.tgz" \
  docker-compose.yml \
  README.md \
  QUICK-RENEWAL-GUIDE.md \
  renewal_checklist.md \
  env_example \
  config/radarr \
  config/sonarr \
  config/prowlarr \
  config/overseerr \
  config/maintainerr \
  config/transmission \
  config/wireguard

echo "Backup written to ${target_dir}/stack-config.tgz"
