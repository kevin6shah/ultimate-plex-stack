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

update_env_value() {
  local env_key="$1"
  local value="$2"

  ENV_FILE="$ENV_FILE" ENV_KEY="$env_key" ENV_VALUE="$value" python3 - <<'PY'
import os
from pathlib import Path

env_file = Path(os.environ["ENV_FILE"])
env_key = os.environ["ENV_KEY"]
value = os.environ["ENV_VALUE"]

lines = env_file.read_text(encoding="utf-8").splitlines()
updated = False
new_lines = []

for line in lines:
    if line.startswith(f"{env_key}="):
        new_lines.append(f"{env_key}={value}")
        updated = True
    else:
        new_lines.append(line)

if not updated:
    if new_lines and new_lines[-1] != "":
        new_lines.append("")
    new_lines.append(f"{env_key}={value}")

env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
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

  tmp_err="$(mktemp)"
  if value="$(
    AWS_PROFILE="$AWS_PROFILE_NAME" AWS_REGION="$AWS_REGION_NAME" \
      aws ssm get-parameter \
        --name "$param_name" \
        --with-decryption \
        --query 'Parameter.Value' \
        --output text 2>"$tmp_err"
  )"; then
    update_env_value "$env_key" "$value"
    echo "synced $env_key"
    rm -f "$tmp_err"
    continue
  fi

  if grep -q "ParameterNotFound" "$tmp_err"; then
    echo "missing in ssm, skipped: $env_key"
    rm -f "$tmp_err"
    continue
  fi

  cat "$tmp_err" >&2
  rm -f "$tmp_err"
  exit 1
done < "$MAP_FILE"

echo "Done: SSM -> .env"
