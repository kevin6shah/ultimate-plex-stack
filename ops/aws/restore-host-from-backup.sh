#!/usr/bin/env bash

set -euo pipefail

BACKUP_TGZ="${1:-}"
REMOTE_USER="${SUDO_USER:-ubuntu}"

if [[ -z "$BACKUP_TGZ" ]]; then
  echo "usage: sudo $0 /path/to/aws-host-state.tgz" >&2
  exit 1
fi

if [[ ! -f "$BACKUP_TGZ" ]]; then
  echo "backup archive not found: $BACKUP_TGZ" >&2
  exit 1
fi

tar -xzf "$BACKUP_TGZ" -C /

install -d -m 700 /etc/wireguard
chmod 600 /etc/wireguard/wg0.conf
install -d -m 755 /etc/nginx/sites-enabled
ln -sfn /etc/nginx/sites-available/iris-backend /etc/nginx/sites-enabled/iris-backend
rm -f /etc/nginx/sites-enabled/default

chown -R "${REMOTE_USER}:${REMOTE_USER}" /opt/iris-backend

cd /opt/iris-backend
npm install --omit=dev

nginx -t
systemctl daemon-reload
systemctl enable wg-quick@wg0 iris-backend nginx
systemctl restart wg-quick@wg0
systemctl restart iris-backend
systemctl restart nginx

echo "Restore complete."
