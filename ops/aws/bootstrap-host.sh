#!/usr/bin/env bash

set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y wireguard nginx nodejs npm curl docker.io uidmap dbus-user-session slirp4netns fuse-overlayfs python3

install -d -m 755 /opt/iris-backend
install -d -m 755 /opt/friday-hands /srv/friday-hands

cat >/etc/sysctl.d/99-friday-network.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF

sysctl --system >/dev/null

systemctl enable nginx

echo "Bootstrap complete."
