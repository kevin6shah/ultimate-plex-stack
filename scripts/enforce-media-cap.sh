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

APPLY=0
PAUSE_ON_CAP=1

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

media_root="$ROOT_DIR/share/media"
plex_db="$ROOT_DIR/config/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"

[[ -d "$media_root" ]] || { echo "Missing media directory: $media_root" >&2; exit 1; }
[[ -f "$plex_db" ]] || { echo "Missing Plex database: $plex_db" >&2; exit 1; }

current_kb="$(du -sk "$media_root" | awk '{print $1}')"
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

candidate_lines="$(python3 - <<'PY'
import json
import os
import pathlib
import sqlite3
import sys
import time

root = pathlib.Path(os.getcwd())
db_path = root / "config/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
account_id = int(os.environ["PLEX_ACCOUNT_ID"])
movie_days = int(os.environ["MOVIE_RETENTION_DAYS"])
tv_days = int(os.environ["TV_RETENTION_DAYS"])
patterns = [item.casefold() for item in json.loads(os.environ["FRIDAY_NEVER_DELETE_PATTERNS_JSON"])]

movie_cutoff = int(time.time()) - (movie_days * 86400)
tv_cutoff = int(time.time()) - (tv_days * 86400)

conn = sqlite3.connect(db_path)
cur = conn.cursor()
rows = cur.execute(
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

    host_path = file_path
    if host_path.startswith("/media/"):
        host_path = str(root / "share" / "media" / host_path[len('/media/'):])
    else:
        continue

    print("\t".join([
        str(last_viewed_at),
        str(size),
        "movie" if metadata_type == 1 else "episode",
        title.replace("\t", " "),
        host_path.replace("\t", " "),
    ]))
PY
)"

if [[ -z "$candidate_lines" ]]; then
  echo "No watched media is currently eligible for deletion."
else
  echo "Eligible cleanup candidates:"
  while IFS=$'\t' read -r last_viewed_at size_bytes media_type title host_path; do
    printf '  - %s | %s | %.2f GiB | %s\n' \
      "$media_type" \
      "$title" \
      "$(awk "BEGIN { print $size_bytes / 1024 / 1024 / 1024 }")" \
      "$host_path"
  done <<<"$candidate_lines"
fi

if [[ "$APPLY" -eq 0 ]]; then
  echo "Dry run only. Re-run with --apply to delete eligible media."
  exit 2
fi

bytes_freed=0

cleanup_empty_dirs() {
  local dir="$1"
  while [[ "$dir" == "$media_root"* && "$dir" != "$media_root" ]]; do
    rmdir "$dir" 2>/dev/null || break
    dir="$(dirname "$dir")"
  done
}

while IFS=$'\t' read -r last_viewed_at size_bytes media_type title host_path; do
  (( current_kb <= cap_kb )) && break
  [[ -f "$host_path" ]] || continue

  echo "Deleting $media_type: $title"
  rm -f "$host_path"
  cleanup_empty_dirs "$(dirname "$host_path")"

  bytes_freed="$((bytes_freed + size_bytes))"
  current_kb="$(du -sk "$media_root" | awk '{print $1}')"
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
