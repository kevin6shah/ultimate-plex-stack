#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .friday-ops.env ]]; then
  # shellcheck disable=SC1091
  source .friday-ops.env
fi

MEDIA_CAP_GB="${MEDIA_CAP_GB:-80}"
MOVIE_RETENTION_DAYS="${MOVIE_RETENTION_DAYS:-15}"
TV_RETENTION_DAYS="${TV_RETENTION_DAYS:-30}"
PLEX_ACCOUNT_ID="${PLEX_ACCOUNT_ID:-1}"
MEDIA_NEVER_DELETE_PATTERNS="${MEDIA_NEVER_DELETE_PATTERNS:-}"
TRANSMISSION_RPC_USERNAME="${TRANSMISSION_RPC_USERNAME:-admin}"
TRANSMISSION_RPC_PASSWORD="${TRANSMISSION_RPC_PASSWORD:-adminadmin}"
MEDIA_CAP_IO_MODE="${MEDIA_CAP_IO_MODE:-host}"
PLEX_CONTAINER_NAME="${PLEX_CONTAINER_NAME:-plex}"
MEDIA_CONTAINER_NAME="${MEDIA_CONTAINER_NAME:-plex}"
MEDIA_ROOT_HOST="${MEDIA_ROOT_HOST:-$ROOT_DIR/share/media}"
MEDIA_ROOT_CONTAINER="${MEDIA_ROOT_CONTAINER:-/media}"
PLEX_DB_HOST="${PLEX_DB_HOST:-$ROOT_DIR/config/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db}"
PLEX_DB_CONTAINER_PATH="${PLEX_DB_CONTAINER_PATH:-/config/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db}"

APPLY=0
PAUSE_ON_CAP=1
TEMP_DB_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      APPLY=1
      ;;
    --no-pause)
      PAUSE_ON_CAP=0
      ;;
    --dry-run)
      APPLY=0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
  shift
done

cleanup_temp_db() {
  if [[ -n "$TEMP_DB_PATH" ]]; then
    rm -f "$TEMP_DB_PATH"
  fi
}
trap cleanup_temp_db EXIT

case "$MEDIA_CAP_IO_MODE" in
  host)
    [[ -d "$MEDIA_ROOT_HOST" ]] || { echo "Missing media directory: $MEDIA_ROOT_HOST" >&2; exit 1; }
    [[ -f "$PLEX_DB_HOST" ]] || { echo "Missing Plex database: $PLEX_DB_HOST" >&2; exit 1; }
    DB_PATH="$PLEX_DB_HOST"
    ;;
  docker)
    docker inspect "$PLEX_CONTAINER_NAME" >/dev/null
    docker inspect "$MEDIA_CONTAINER_NAME" >/dev/null
    TEMP_DB_PATH="$(mktemp -t friday-media-cap.XXXXXX.sqlite)"
    docker cp "${PLEX_CONTAINER_NAME}:${PLEX_DB_CONTAINER_PATH}" "$TEMP_DB_PATH"
    DB_PATH="$TEMP_DB_PATH"
    ;;
  *)
    echo "Unsupported MEDIA_CAP_IO_MODE: $MEDIA_CAP_IO_MODE" >&2
    echo "Expected one of: host, docker" >&2
    exit 1
    ;;
esac

get_current_kb() {
  case "$MEDIA_CAP_IO_MODE" in
    host)
      du -sk "$MEDIA_ROOT_HOST" | awk '{print $1}'
      ;;
    docker)
      docker exec "$MEDIA_CONTAINER_NAME" sh -lc "du -sk '$MEDIA_ROOT_CONTAINER' | awk '{print \$1}'"
      ;;
  esac
}

media_path_exists() {
  local path="$1"
  case "$MEDIA_CAP_IO_MODE" in
    host)
      [[ -f "$path" ]]
      ;;
    docker)
      docker exec -i "$MEDIA_CONTAINER_NAME" sh -s -- "$path" <<'SH'
set -eu
test -f "$1"
SH
      ;;
  esac
}

delete_media_path() {
  local path="$1"
  case "$MEDIA_CAP_IO_MODE" in
    host)
      rm -f "$path"
      cleanup_empty_dirs "$(dirname "$path")"
      ;;
    docker)
      docker exec -i "$MEDIA_CONTAINER_NAME" sh -s -- "$path" "$MEDIA_ROOT_CONTAINER" <<'SH'
set -eu
target="$1"
media_root="$2"
rm -f "$target"
dir="$(dirname "$target")"
while [ "${dir#"$media_root"}" != "$dir" ] && [ "$dir" != "$media_root" ]; do
  rmdir "$dir" 2>/dev/null || break
  dir="$(dirname "$dir")"
done
SH
      ;;
  esac
}

cleanup_empty_dirs() {
  local dir="$1"
  while [[ "$dir" == "$MEDIA_ROOT_HOST"* && "$dir" != "$MEDIA_ROOT_HOST" ]]; do
    rmdir "$dir" 2>/dev/null || break
    dir="$(dirname "$dir")"
  done
}

current_kb="$(get_current_kb)"
cap_kb="$((MEDIA_CAP_GB * 1024 * 1024))"

echo "Media usage: $((current_kb / 1024 / 1024)) GiB / ${MEDIA_CAP_GB} GiB cap"

if (( current_kb <= cap_kb )); then
  echo "Nothing to do. Media usage is within the configured cap."
  exit 0
fi

patterns_json="$(python3 - <<'PY'
import json, os
raw = os.environ.get("MEDIA_NEVER_DELETE_PATTERNS", "")
patterns = [item.strip() for item in raw.split(",") if item.strip()]
print(json.dumps(patterns))
PY
)"

candidate_lines="$(
FRIDAY_DB_PATH="$DB_PATH" \
FRIDAY_MEDIA_ROOT_HOST="$MEDIA_ROOT_HOST" \
FRIDAY_MEDIA_ROOT_CONTAINER="$MEDIA_ROOT_CONTAINER" \
FRIDAY_MEDIA_CAP_IO_MODE="$MEDIA_CAP_IO_MODE" \
python3 - <<'PY'
import json
import os
import pathlib
import sqlite3
import time

db_path = pathlib.Path(os.environ["FRIDAY_DB_PATH"])
account_id = int(os.environ["PLEX_ACCOUNT_ID"])
movie_days = int(os.environ["MOVIE_RETENTION_DAYS"])
tv_days = int(os.environ["TV_RETENTION_DAYS"])
patterns = [item.casefold() for item in json.loads(os.environ["FRIDAY_NEVER_DELETE_PATTERNS_JSON"])]
io_mode = os.environ["FRIDAY_MEDIA_CAP_IO_MODE"]
media_root_host = pathlib.Path(os.environ["FRIDAY_MEDIA_ROOT_HOST"])
media_root_container = os.environ["FRIDAY_MEDIA_ROOT_CONTAINER"]

movie_cutoff = int(time.time()) - (movie_days * 86400)
tv_cutoff = int(time.time()) - (tv_days * 86400)

conn = sqlite3.connect(db_path)
rows = conn.execute(
    """
    SELECT
      mi.metadata_type,
      mi.title,
      COALESCE(mis.last_viewed_at, 0),
      COALESCE(mis.view_count, 0),
      mp.file,
      COALESCE(mp.size, 0)
    FROM metadata_items mi
    JOIN media_items med ON med.metadata_item_id = mi.id
    JOIN media_parts mp ON mp.media_item_id = med.id
    LEFT JOIN metadata_item_settings mis
      ON mis.guid = mi.guid AND mis.account_id = ?
    WHERE mi.deleted_at IS NULL
      AND med.deleted_at IS NULL
      AND mp.deleted_at IS NULL
      AND mp.file != ''
      AND mi.metadata_type IN (1, 4)
    ORDER BY COALESCE(mis.last_viewed_at, 0) ASC, mp.size DESC
    """,
    (account_id,),
).fetchall()

seen = set()
container_prefix = media_root_container.rstrip("/") + "/"

for metadata_type, title, last_viewed_at, view_count, file_path, size in rows:
    if view_count <= 0 or last_viewed_at <= 0:
        continue

    cutoff = movie_cutoff if metadata_type == 1 else tv_cutoff
    if last_viewed_at > cutoff:
        continue

    if file_path in seen:
        continue
    seen.add(file_path)

    haystack = f"{title} {file_path}".casefold()
    if any(pattern in haystack for pattern in patterns):
        continue

    resolved_path = None
    if io_mode == "host" and file_path.startswith(container_prefix):
        resolved_path = str(media_root_host / file_path[len(container_prefix):])
    elif io_mode == "docker" and file_path.startswith(container_prefix):
        resolved_path = file_path

    if not resolved_path:
        continue

    print("\t".join([
        str(last_viewed_at),
        str(size),
        "movie" if metadata_type == 1 else "episode",
        title.replace("\t", " "),
        resolved_path.replace("\t", " "),
    ]))
PY
)"

if [[ -z "$candidate_lines" ]]; then
  echo "No watched media is currently eligible for deletion."
else
  echo "Eligible cleanup candidates:"
  while IFS=$'\t' read -r last_viewed_at size_bytes media_type title resolved_path; do
    printf '  - %s | %s | %.2f GiB | %s\n' \
      "$media_type" \
      "$title" \
      "$(awk "BEGIN { print $size_bytes / 1024 / 1024 / 1024 }")" \
      "$resolved_path"
  done <<<"$candidate_lines"
fi

if [[ "$APPLY" -eq 0 ]]; then
  echo "Dry run only. Re-run with --apply to delete eligible media."
  exit 2
fi

bytes_freed=0

while IFS=$'\t' read -r last_viewed_at size_bytes media_type title resolved_path; do
  (( current_kb <= cap_kb )) && break
  media_path_exists "$resolved_path" || continue

  echo "Deleting $media_type: $title"
  delete_media_path "$resolved_path"

  bytes_freed="$((bytes_freed + size_bytes))"
  current_kb="$(get_current_kb)"
done <<<"$candidate_lines"

echo "Freed approximately $(awk "BEGIN { print $bytes_freed / 1024 / 1024 / 1024 }") GiB"

pause_transmission() {
  local session_id
  session_id="$(
    curl -sSI -u "${TRANSMISSION_RPC_USERNAME}:${TRANSMISSION_RPC_PASSWORD}" http://localhost:9091/transmission/rpc \
      | awk -F': ' '/^X-Transmission-Session-Id:/ { gsub("\r", "", $2); print $2 }'
  )"
  [[ -n "$session_id" ]] || return 1

  curl -fsS \
    -u "${TRANSMISSION_RPC_USERNAME}:${TRANSMISSION_RPC_PASSWORD}" \
    -H "X-Transmission-Session-Id: ${session_id}" \
    -H 'Content-Type: application/json' \
    --data '{"method":"torrent-stop","arguments":{"ids":"recently-active"}}' \
    http://localhost:9091/transmission/rpc >/dev/null
}

if (( current_kb > cap_kb )); then
  echo "Media usage is still above cap after deleting eligible watched media." >&2
  if [[ "$PAUSE_ON_CAP" -eq 1 ]]; then
    if pause_transmission; then
      echo "Stopped recently active Transmission torrents because the media cap is still exceeded." >&2
    else
      echo "Unable to stop Transmission automatically. Check Transmission RPC credentials and status." >&2
    fi
  fi
  exit 1
fi

echo "Media usage is now back within the configured cap."
