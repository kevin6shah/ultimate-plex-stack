#!/bin/bash
# Mac-side script to update WireGuard VPN configuration
# Run this on your MacBook Pro whenever you create a new VPN server

set -e

echo "🔄 WireGuard VPN Configuration Updater"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker is not running. Please start Docker Desktop first."
    exit 1
fi

# Create config directory if it doesn't exist
CONFIG_DIR="./config/wireguard"
mkdir -p "$CONFIG_DIR"

echo "📋 Paste your WireGuard client configuration below."
echo "   (Copy everything from [Interface] to PersistentKeepalive)"
echo "   Press Ctrl+D when done:"
echo ""

# Read multi-line input
CONFIG_FILE="$CONFIG_DIR/wg0.conf"
cat > "$CONFIG_FILE"

echo ""
echo "✅ Configuration saved to $CONFIG_FILE"
echo ""

# Stop affected containers
echo "🛑 Stopping VPN-related containers..."
docker-compose stop wireguard transmission qbittorrent 2>/dev/null || true

# Remove old containers
echo "🗑️  Removing old containers..."
docker-compose rm -f wireguard transmission qbittorrent 2>/dev/null || true

# Start containers
echo "🚀 Starting containers with new VPN configuration..."
docker-compose up -d wireguard
sleep 5
docker-compose up -d transmission

echo ""
echo "⏳ Waiting for VPN connection to establish..."
sleep 10

# Test VPN connection
echo ""
echo "🔍 Testing VPN connection..."
VPN_IP=$(docker exec transmission curl -s ifconfig.me 2>/dev/null || echo "Failed")

if [ "$VPN_IP" != "Failed" ]; then
    echo "✅ VPN is working! Your Transmission IP is: $VPN_IP"
    echo ""
    echo "🌐 Access Transmission at: http://localhost:9091"
    echo "   Default login - Username: admin, Password: adminadmin"
else
    echo "⚠️  Could not verify VPN connection. Check logs:"
    echo "   docker logs wireguard"
    echo "   docker logs transmission"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✨ VPN update complete!"
echo ""
echo "📝 Useful commands:"
echo "   Check VPN IP: docker exec transmission curl ifconfig.me"
echo "   View logs: docker logs wireguard"
echo "   Restart VPN: docker-compose restart wireguard transmission"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"