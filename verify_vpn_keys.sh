#!/bin/bash
# Verify WireGuard keys match between Mac and EC2
# Run this after updating VPN config to catch key mismatches early

set -e

echo "🔍 WireGuard Key Verification Tool"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check if containers are running
if ! docker ps | grep -q wireguard; then
    echo "❌ WireGuard container is not running"
    echo "   Start it with: docker-compose up -d wireguard"
    exit 1
fi

echo "📋 Your Mac's WireGuard Public Key:"
MAC_PUBLIC_KEY=$(docker exec wireguard wg show | grep "public key" | awk '{print $3}')
echo "   $MAC_PUBLIC_KEY"
echo ""

echo "📋 Your WireGuard Config File:"
if [ -f "./config/wireguard/wg_confs/wg0.conf" ]; then
    PEER_KEY=$(grep "PublicKey" ./config/wireguard/wg_confs/wg0.conf | awk '{print $3}')
    ENDPOINT=$(grep "Endpoint" ./config/wireguard/wg_confs/wg0.conf | awk '{print $3}')
    echo "   Server Public Key: $PEER_KEY"
    echo "   Server Endpoint: $ENDPOINT"
else
    echo "   ⚠️  Config file not found!"
fi
echo ""

echo "🔄 Testing VPN Connection..."
sleep 2

# Check handshake
HANDSHAKE=$(docker exec wireguard wg show | grep "latest handshake" || echo "none")
if [[ "$HANDSHAKE" == "none" ]]; then
    echo "❌ NO HANDSHAKE DETECTED!"
    echo ""
    echo "🔧 To fix this, SSH into your EC2 instance and run:"
    echo "   sudo nano /etc/wireguard/wg0.conf"
    echo ""
    echo "   Update the [Peer] section with YOUR Mac's public key:"
    echo "   PublicKey = $MAC_PUBLIC_KEY"
    echo ""
    echo "   Then restart WireGuard:"
    echo "   sudo systemctl restart wg-quick@wg0"
    exit 1
else
    echo "✅ Handshake detected: $HANDSHAKE"
fi
echo ""

# Test actual connectivity
echo "🌐 Testing Internet Connectivity..."
if docker exec transmission curl -s -m 5 ifconfig.me > /dev/null 2>&1; then
    VPN_IP=$(docker exec transmission curl -s ifconfig.me)
    echo "✅ VPN is working! Your IP: $VPN_IP"
    echo ""
    echo "🎉 Everything looks good!"
else
    echo "❌ Cannot reach internet through VPN"
    echo ""
    echo "Possible issues:"
    echo "1. EC2 security group doesn't allow UDP port 51820"
    echo "2. NAT routing not configured on EC2"
    echo "3. DNS not working"
    echo ""
    echo "Check logs: docker logs wireguard"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
