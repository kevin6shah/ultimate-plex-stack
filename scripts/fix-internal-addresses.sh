#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKUP_DIR="backup/internal-address-fix-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

cp config/overseerr/settings.json "$BACKUP_DIR/overseerr-settings.json"
cp config/maintainerr/data/maintainerr.sqlite "$BACKUP_DIR/maintainerr.sqlite"
cp "config/plex/Library/Application Support/Plex Media Server/Preferences.xml" "$BACKUP_DIR/plex-preferences.xml"

python3 - <<'PY'
import json
import pathlib
import re
import sqlite3

root = pathlib.Path.cwd()

overseerr_path = root / "config/overseerr/settings.json"
settings = json.loads(overseerr_path.read_text())
plex = settings.setdefault("plex", {})
plex["ip"] = "plex"
plex["port"] = 32400
plex["useSsl"] = False
overseerr_path.write_text(json.dumps(settings, indent=2) + "\n")

preferences_path = root / "config/plex/Library/Application Support/Plex Media Server/Preferences.xml"
preferences_text = preferences_path.read_text()
token_match = re.search(r'PlexOnlineToken="([^"]+)"', preferences_text)
plex_token = token_match.group(1) if token_match else None

db_path = root / "config/maintainerr/data/maintainerr.sqlite"
conn = sqlite3.connect(db_path)
cur = conn.cursor()
if plex_token:
    cur.execute(
        "UPDATE settings SET plex_hostname = ?, plex_port = ?, plex_ssl = ?, plex_auth_token = ? WHERE id = 1",
        ("plex", 32400, 0, plex_token),
    )
else:
    cur.execute(
        "UPDATE settings SET plex_hostname = ?, plex_port = ?, plex_ssl = ? WHERE id = 1",
        ("plex", 32400, 0),
    )
conn.commit()
conn.close()
PY

echo "Updated Overseerr and Maintainerr to use Docker-internal Plex addressing."
echo "Maintainerr Plex token was synced from Plex preferences when available."
echo "A service restart is still required before the running containers pick up these changes."
