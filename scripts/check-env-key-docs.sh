#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${FRIDAY_SECRET_ENV_FILE:-$ROOT_DIR/.env}"
MAP_FILE="$ROOT_DIR/scripts/friday-secret-map.tsv"
DOC_FILE="$ROOT_DIR/docs/FRIDAY_ENV_KEYS.md"

[[ -f "$ENV_FILE" ]] || { echo "env file not found: $ENV_FILE" >&2; exit 1; }
[[ -f "$MAP_FILE" ]] || { echo "secret map not found: $MAP_FILE" >&2; exit 1; }
[[ -f "$DOC_FILE" ]] || { echo "doc file not found: $DOC_FILE" >&2; exit 1; }

ENV_FILE="$ENV_FILE" MAP_FILE="$MAP_FILE" DOC_FILE="$DOC_FILE" python3 - <<'PY'
import os
import re
import sys
from pathlib import Path

env_file = Path(os.environ["ENV_FILE"])
map_file = Path(os.environ["MAP_FILE"])
doc_file = Path(os.environ["DOC_FILE"])

env_keys = set()
for raw_line in env_file.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in raw_line:
        continue
    key, _ = raw_line.split("=", 1)
    env_keys.add(key.strip())

map_keys = set()
for raw_line in map_file.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    key = raw_line.split("\t", 1)[0].strip()
    if key:
        map_keys.add(key)

doc_keys = set(re.findall(r"- `([A-Z0-9_]+)`", doc_file.read_text(encoding="utf-8")))
expected = env_keys | map_keys

missing = sorted(expected - doc_keys)
extra = sorted(doc_keys - expected)

if missing:
    print("Missing documented env keys:", file=sys.stderr)
    for key in missing:
        print(f"  - {key}", file=sys.stderr)

if extra:
    print("Documented keys not found in .env or secret map:", file=sys.stderr)
    for key in extra:
        print(f"  - {key}", file=sys.stderr)

if missing or extra:
    sys.exit(1)

print("Environment key docs are in sync.")
PY
