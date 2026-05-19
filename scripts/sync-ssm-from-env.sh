#!/usr/bin/env bash

set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${FRIDAY_SECRET_ENV_FILE:-$ROOT_DIR/.env}"
MAP_FILE="$ROOT_DIR/scripts/friday-secret-map.tsv"

[[ -f "$ENV_FILE" ]] || { echo "secret env file not found: $ENV_FILE" >&2; exit 1; }
[[ -f "$MAP_FILE" ]] || { echo "secret map not found: $MAP_FILE" >&2; exit 1; }
command -v aws >/dev/null 2>&1 || { echo "required command not found: aws" >&2; exit 1; }

get_env_value() {
  local key="$1"
  ENV_FILE="$ENV_FILE" ENV_KEY="$key" python3 - <<'PY'
import os
from pathlib import Path

env_file = Path(os.environ["ENV_FILE"])
env_key = os.environ["ENV_KEY"]

for raw_line in env_file.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in raw_line:
        continue
    key, value = raw_line.split("=", 1)
    if key.strip() == env_key:
        print(value)
        break
PY
}

AWS_PROFILE_NAME="$(get_env_value AWS_PROFILE)"
AWS_REGION_NAME="$(get_env_value AWS_REGION)"
AWS_REGION_NAME="${AWS_REGION_NAME:-us-east-1}"

[[ -n "$AWS_PROFILE_NAME" ]] || { echo "AWS_PROFILE is required in $ENV_FILE" >&2; exit 1; }

while IFS=$'\t' read -r env_key param_name; do
  [[ -n "$env_key" ]] || continue
  [[ "$env_key" == \#* ]] && continue
  [[ "$param_name" != "-" ]] || continue

  value="$(get_env_value "$env_key")"
  if [[ -z "$value" ]]; then
    echo "empty in .env, skipped: $env_key"
    continue
  fi

  AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
    aws ssm put-parameter \
      --name "$param_name" \
      --type SecureString \
      --value "$value" \
      --overwrite \
      >/dev/null
  echo "pushed $env_key"
done < "$MAP_FILE"

echo "Done: .env -> SSM"
