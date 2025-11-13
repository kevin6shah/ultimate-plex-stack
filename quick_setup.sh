#!/bin/bash
# Quick setup script for initial installation or fresh setups
# Run this once when first setting up the stack

set -e

echo "🚀 Plex Stack Quick Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker is not running. Please start Docker Desktop first."
    exit 1
fi

# Create directory structure
echo "📁 Creating directory structure..."
mkdir -p share/media/{movies,tv}
mkdir -p share/downloads/{complete,incomplete,watch}
mkdir -p config/{plex,jellyfin,radarr,sonarr,prowlarr,transmission,bazarr,overseerr,maintainerr,wireguard,portainer}

# Set permissions
chmod -R 755 share
echo "✅ Directories created"
echo ""

# Check for .env file
if [ ! -f ".env" ]; then
    echo "📝 Creating .env file from template..."
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "⚠️  Please edit .env file with your settings:"
        echo "   - PLEX_CLAIM token from https://www.plex.tv/claim"
        echo "   - Your PUID/PGID (run 'id' command)"
        echo "   - Your timezone"
        echo ""
    else
        echo "⚠️  .env.example not found, skipping..."
    fi
fi

# Make scripts executable
echo "🔧 Making scripts executable..."
chmod +x update-vpn-config.sh 2>/dev/null || echo "update-vpn-config.sh not found"
chmod +x verify-vpn-keys.sh 2>/dev/null || echo "verify-vpn-keys.sh not found"

echo ""
echo "✅ Setup complete!"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 Next Steps:"
echo ""
echo "1. Edit .env file with your settings:"
echo "   nano .env"
echo ""
echo "2. Set up AWS EC2 VPN server (see README.md)"
echo ""
echo "3. Update VPN config:"
echo "   ./update-vpn-config.sh"
echo ""
echo "4. Start the stack:"
echo "   docker-compose up -d"
echo ""
echo "5. Verify VPN is working:"
echo "   ./verify-vpn-keys.sh"
echo "   docker exec transmission curl ifconfig.me"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
