#!/bin/bash
# Automated WireGuard VPN Setup for AWS EC2
# Run this on your NEW Ubuntu 22.04/24.04 EC2 instance every 6 months
# Usage: curl -sSL https://your-gist-url/script.sh | sudo bash

set -e

echo "🚀 Starting Automated WireGuard VPN Setup..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Update system
echo "📦 Updating system packages..."
apt update && apt upgrade -y

# Install WireGuard
echo "🔧 Installing WireGuard..."
apt install -y wireguard curl qrencode

# Generate keys
echo "🔑 Generating encryption keys..."
cd /etc/wireguard
umask 077

wg genkey | tee server_private.key | wg pubkey > server_public.key
wg genkey | tee client_private.key | wg pubkey > client_public.key

SERVER_PRIVATE_KEY=$(cat server_private.key)
SERVER_PUBLIC_KEY=$(cat server_public.key)
CLIENT_PRIVATE_KEY=$(cat client_private.key)
CLIENT_PUBLIC_KEY=$(cat client_public.key)

# Get public IP
PUBLIC_IP=$(curl -s ifconfig.me || curl -s icanhazip.com)

# Detect network interface
echo "🔍 Detecting network interface..."
INTERFACE=$(ip route | grep default | awk '{print $5}')
echo "   Network interface detected: $INTERFACE"

# Enable IP forwarding
echo "🌐 Enabling IP forwarding..."
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv6.conf.all.forwarding=1
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
echo "net.ipv6.conf.all.forwarding=1" >> /etc/sysctl.conf

# Create server config with correct interface
echo "⚙️  Creating WireGuard server configuration..."
cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = 10.8.0.1/24
ListenPort = 51820
PrivateKey = $SERVER_PRIVATE_KEY
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o $INTERFACE -j MASQUERADE; iptables -A FORWARD -o wg0 -j ACCEPT
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o $INTERFACE -j MASQUERADE; iptables -D FORWARD -o wg0 -j ACCEPT

[Peer]
PublicKey = $CLIENT_PUBLIC_KEY
AllowedIPs = 10.8.0.2/32
EOF

# Set proper permissions
chmod 600 /etc/wireguard/wg0.conf
chmod 600 /etc/wireguard/*.key

# Enable and start WireGuard
echo "🚀 Starting WireGuard service..."
systemctl enable wg-quick@wg0
systemctl start wg-quick@wg0

# Install iptables-persistent to save rules
echo "💾 Making firewall rules persistent..."
echo iptables-persistent iptables-persistent/autosave_v4 boolean true | debconf-set-selections
echo iptables-persistent iptables-persistent/autosave_v6 boolean true | debconf-set-selections
apt install -y iptables-persistent
netfilter-persistent save

# Create client config
CLIENT_CONFIG="/root/wg0-client.conf"
cat > $CLIENT_CONFIG <<EOF
[Interface]
PrivateKey = $CLIENT_PRIVATE_KEY
Address = 10.8.0.2/24
DNS = 1.1.1.1, 8.8.8.8

[Peer]
PublicKey = $SERVER_PUBLIC_KEY
Endpoint = $PUBLIC_IP:51820
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
EOF

# Display results
clear
echo "✅ WireGuard VPN Server Successfully Installed!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📋 CLIENT CONFIGURATION (Copy this ENTIRE block):"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
cat $CLIENT_CONFIG
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "💾 Configuration also saved to: $CLIENT_CONFIG"
echo ""
echo "📱 QR Code for mobile (scan with WireGuard app):"
qrencode -t ansiutf8 < $CLIENT_CONFIG
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔍 Server Status:"
systemctl status wg-quick@wg0 --no-pager | head -5
echo ""
echo "🌍 Server Public IP: $PUBLIC_IP"
echo "📡 WireGuard Port: 51820"
echo "🔌 Network Interface: $INTERFACE"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✨ Next Steps:"
echo "1. Copy the CLIENT CONFIGURATION above"
echo "2. On your Mac, run: ./update-vpn-config.sh"
echo "3. Paste the config when prompted"
echo "4. Restart your Docker containers: docker-compose up -d"
echo ""
echo "⚠️  CRITICAL: Set up billing alarm NOW!"
echo "   Go to AWS Console → CloudWatch → Billing → Create Alarm"
echo "   Set threshold to \$1.00 to get email alerts"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"