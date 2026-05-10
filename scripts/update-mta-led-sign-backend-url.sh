#!/usr/bin/env bash

set -euo pipefail

MTA_ROOT="${MTA_LED_SIGN_ROOT:-$HOME/Documents/mta-led-sign}"
OLD_URL="${1:-}"
NEW_URL="${2:-}"
OLD_DNS_URL="${OLD_DNS_URL:-http://ec2-54-90-132-5.compute-1.amazonaws.com}"
OLD_HOST="${OLD_HOST:-}"
NEW_HOST="${NEW_HOST:-}"
OLD_DNS_HOST="${OLD_DNS_HOST:-ec2-54-90-132-5.compute-1.amazonaws.com}"

if [[ -z "$OLD_URL" || -z "$NEW_URL" ]]; then
  echo "usage: $0 <old-backend-url> <new-backend-url>" >&2
  exit 1
fi

files=(
  "docs/SOFTWARE.md"
  "docs/BACKEND.md"
  "docs/GIFT_DEFAULTS.md"
  "docs/TEST_PLAN.md"
  "scripts/deploy_backend_ec2.sh"
  "scripts/prepare_gift_board.py"
  "Iris/docs/CONFIG_SCHEMA.md"
  "Iris/board_config.example.json"
  "Iris/lib/iris_runtime.py"
  "Iris/app/runtime.py"
)

if [[ -z "$OLD_HOST" ]]; then
  OLD_HOST="${OLD_URL#http://}"
  OLD_HOST="${OLD_HOST#https://}"
  OLD_HOST="${OLD_HOST%%/*}"
fi

if [[ -z "$NEW_HOST" ]]; then
  NEW_HOST="${NEW_URL#http://}"
  NEW_HOST="${NEW_HOST#https://}"
  NEW_HOST="${NEW_HOST%%/*}"
fi

for relative_path in "${files[@]}"; do
  file_path="${MTA_ROOT}/${relative_path}"
  [[ -f "$file_path" ]] || continue
  perl -0pi -e "s#\Q${OLD_URL}\E#${NEW_URL}#g; s#\Q${OLD_DNS_URL}\E#${NEW_URL}#g; s#\Q${OLD_HOST}\E#${NEW_HOST}#g; s#\Q${OLD_DNS_HOST}\E#${NEW_HOST}#g" "$file_path"
done

echo "Updated local mta-led-sign backend references from ${OLD_URL} to ${NEW_URL}"
