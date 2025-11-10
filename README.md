# The Ultimate Plex Stack with Free VPN!

Welcome to my enhanced Plex stack repository! This repository showcases a complete Docker Compose setup for managing media services with a **free, self-hosted VPN** using AWS EC2 and WireGuard. No paid VPN subscriptions needed!

## 🎯 Overview

This Plex Stack includes the following services:

### Media Management
- **Plex:** Media server for streaming movies and TV shows
- **Jellyfin:** Alternative open-source media server
- **Radarr:** Movie management and automation
- **Sonarr:** TV show management and automation
- **Bazarr:** Subtitle management for movies and TV shows
- **Maintainerr:** Automated media library cleanup and management

### Download & Indexing
- **Transmission:** BitTorrent client with VPN support (routes through WireGuard)
- **Prowlarr:** Indexer manager for Radarr and Sonarr
- **Unpackerr:** Automatically extracts downloaded archives

### VPN & Security
- **WireGuard:** Self-hosted VPN client connecting to your AWS EC2 server
- **Free Tier AWS EC2:** VPN server (no cost for first 12 months!)

### Management & Monitoring
- **Overseerr:** Request management and monitoring for Plex
- **Portainer:** Docker container management GUI

## 🚀 Why This Setup?

### Free VPN Forever (with account rotation)
- ✅ **No monthly VPN fees** ($0-13/month saved)
- ✅ **Complete control** over your VPN server
- ✅ **No traffic logging** - you own the server
- ✅ **AWS Free Tier** for 12 months, then rotate to new account
- ✅ **Easy 6-month rotation** with automated scripts

### Secure Torrenting
- All torrent traffic routes through your VPN
- Transmission only accessible through VPN tunnel
- Kill-switch built-in (if VPN dies, torrenting stops)

## 📋 Dependencies

1. **macOS** (tested on MacBook Pro with Apple Silicon)
2. **Docker Desktop** for Mac
3. **AWS Account** (free tier eligible)
4. **SSH Access** to manage EC2 instances

## 🛠️ Initial Setup (First Time - 30 minutes)

### Part 1: AWS EC2 VPN Server Setup

#### Step 1: Create AWS Account
1. Go to [aws.amazon.com](https://aws.amazon.com)
2. Create a new account (use unique email)
3. Enter payment method (won't be charged during free tier)
4. Complete identity verification

#### Step 2: Set Up Billing Alarm (CRITICAL!)
**Do this IMMEDIATELY to avoid surprise charges!**

1. AWS Console → **Billing and Cost Management**
2. **Billing preferences** → Enable:
   - ✅ Receive Free Tier Usage Alerts
   - ✅ Receive Billing Alerts
3. Enter your email → Save
4. Go to **CloudWatch** → **Alarms** → **Billing**
5. Create alarm:
   - Metric: EstimatedCharges
   - Threshold: **$1.00**
   - Create SNS topic with your email
6. **Confirm both email subscriptions!**

#### Step 3: Launch EC2 Instance
1. AWS Console → **EC2** → **Launch Instance**
2. Configure:
   - **Name:** `wireguard-vpn-server`
   - **AMI:** Ubuntu Server 22.04 LTS or 24.04 LTS
   - **Instance type:** t2.micro (free tier)
   - **Key pair:** Create new (download `.pem` file!)
   - **Network settings:**
     - Allow SSH (22) from "My IP"
     - Add rule: Custom UDP, port **51820**, source **0.0.0.0/0**
   - **Storage:** 8 GB (default)
3. **Launch instance**
4. Note the **Public IPv4 address**

#### Step 4: SSH Into EC2 and Run Setup Script

On your Mac terminal:
```bash
# Set key permissions
chmod 400 ~/Downloads/your-key-name.pem

# SSH into instance
ssh -i ~/Downloads/your-key-name.pem ubuntu@YOUR-EC2-PUBLIC-IP

# Run the automated setup script
curl -sSL https://gist.githubusercontent.com/kevin6shah/7362dd9d7103c39f02130f158b0df42c/raw/automated-wireguard-setup.sh | sudo bash
```

**IMPORTANT:** The script will output a **CLIENT CONFIGURATION** block. Copy the ENTIRE block from `[Interface]` to `PersistentKeepalive = 25`.

### Part 2: Mac Docker Setup

#### Step 1: Clone/Download This Repository
```bash
cd ~/Documents
git clone https://github.com/YOUR-USERNAME/your-plex-stack.git
cd your-plex-stack
```

#### Step 2: Create Required Directories
```bash
# Create folder structure
mkdir -p share/media/{movies,tv}
mkdir -p share/downloads/{complete,incomplete,watch}
mkdir -p config/{plex,jellyfin,radarr,sonarr,prowlarr,transmission,bazarr,overseerr,maintainerr,wireguard,portainer}

# Set permissions
chmod -R 755 share
```

#### Step 3: Configure Environment Variables

Edit `docker-compose.yml` and update:
- `PLEX_CLAIM`: Get from [plex.tv/claim](https://www.plex.tv/claim)
- `TZ`: Your timezone (default: `America/New_York`)
- File paths (if different from defaults)

#### Step 4: Save Update Script

Create `update-vpn-config.sh` in your project directory:
```bash
nano update-vpn-config.sh
```

Paste the contents from the `update-vpn-config.sh` artifact, then:
```bash
chmod +x update-vpn-config.sh
```

#### Step 5: Configure VPN
```bash
./update-vpn-config.sh
```

When prompted, paste the CLIENT CONFIGURATION you copied from EC2. Press `Ctrl+D` when done.

#### Step 6: Start Everything!
```bash
docker-compose up -d
```

Wait ~30 seconds for everything to start, then verify VPN:
```bash
docker exec transmission curl ifconfig.me
```

**This should show your AWS EC2 IP, not your home IP!** ✅

### Part 3: Configure Applications

#### Access Your Services
- **Plex:** http://localhost:32400/web
- **Jellyfin:** http://localhost:8096
- **Transmission:** http://localhost:9091 (user: admin, pass: adminadmin)
- **Radarr:** http://localhost:7878
- **Sonarr:** http://localhost:8989
- **Prowlarr:** http://localhost:9696
- **Overseerr:** http://localhost:5055
- **Bazarr:** http://localhost:6767
- **Portainer:** http://localhost:9000

#### Configure Download Client in Radarr/Sonarr

**Radarr** (http://localhost:7878):
1. Settings → Download Clients → Add → Transmission
2. Configure:
   - Name: `Transmission`
   - Host: `wireguard` (⚠️ Important! Not localhost)
   - Port: `9091`
   - Username: `admin`
   - Password: `adminadmin`
   - Category: `movies`
3. Test → Save

**Sonarr** (http://localhost:8989):
- Same as Radarr, but use Category: `tv`

#### Configure Indexers in Prowlarr
1. Add your preferred indexers
2. Go to Settings → Apps
3. Add Radarr and Sonarr
4. Sync will happen automatically

## 🔄 Every 6 Months: VPN Renewal (10 minutes)

When your AWS free tier is about to expire (or you want to rotate accounts):

### Step 1: Create New AWS Account
- Use new email (or `youremail+1@gmail.com`, `+2`, etc.)
- Different payment method if possible

### Step 2: Launch New EC2 Instance
- Same settings as initial setup
- **Set up billing alarm again!**

### Step 3: Run Setup Script on New Instance
```bash
ssh -i ~/Downloads/new-key-name.pem ubuntu@NEW-EC2-IP
curl -sSL https://gist.githubusercontent.com/kevin6shah/7362dd9d7103c39f02130f158b0df42c/raw/automated-wireguard-setup.sh | sudo bash
```

Copy the new CLIENT CONFIGURATION.

### Step 4: Update Your Mac
```bash
cd ~/path/to/your-plex-stack
./update-vpn-config.sh
# Paste new config, Ctrl+D
```

### Step 5: Verify & Cleanup
```bash
# Test new VPN
docker exec transmission curl ifconfig.me

# If working, terminate old EC2 instance in AWS Console
```

**Set calendar reminder for 5.5 months from now!** ⏰

## 📁 Folder Structure

```
your-plex-stack/
├── docker-compose.yml
├── update-vpn-config.sh          # VPN updater script
├── share/
│   ├── downloads/
│   │   ├── complete/             # Finished downloads
│   │   ├── incomplete/           # In-progress downloads
│   │   └── watch/                # Drop .torrent files here
│   └── media/
│       ├── movies/               # Radarr moves movies here
│       └── tv/                   # Sonarr moves TV here
└── config/
    ├── wireguard/
    │   └── wg0.conf             # VPN client config
    ├── transmission/
    ├── radarr/
    ├── sonarr/
    └── [other services]/
```

## ⚠️ Important Notes

### Hardlinks
To enable hardlinks (saves disk space):
- All containers must use the same root path (`/share`)
- Don't use different mount points like `/downloads` and `/movies`
- Correct: `/share/downloads/complete` → `/share/media/movies`
- Wrong: `/downloads` → `/movies` (copies files, wastes space)

### VPN Traffic
- Only **Transmission** routes through VPN
- All other services use your normal connection
- If VPN fails, Transmission stops working (kill-switch)

### AWS Free Tier Limits
- ✅ 750 hours/month EC2 (covers 24/7)
- ⚠️ 15 GB bandwidth/month outbound (can be limiting for heavy torrenting)
- After 15 GB: $0.09/GB (~$7.65 for 100 GB total)
- Still cheaper than VPN subscriptions!

### Bandwidth Management
If you hit the 15 GB limit:
1. Limit upload/download speeds in Transmission
2. Only download during certain hours
3. Disable seeding in Transmission settings
4. Consider Oracle Cloud (10 TB/month free forever)

## 🔧 Troubleshooting

### VPN Not Working
```bash
# Check WireGuard logs
docker logs wireguard

# Check if tunnel is active
docker exec wireguard wg show

# Should show: latest handshake within last 2 minutes

# Test connectivity
docker exec wireguard ping -c 3 8.8.8.8
docker exec transmission curl ifconfig.me
```

### Transmission Can't Connect
```bash
# Restart VPN and Transmission
docker-compose restart wireguard transmission

# Check if folders exist
ls -la ./share/downloads/
```

### Services Not Starting
```bash
# View all container logs
docker-compose logs

# View specific service
docker logs radarr
```

### EC2 SSH Not Working
1. Check Security Group has port 22 open to your IP
2. Verify key file permissions: `chmod 400 your-key.pem`
3. Use correct username: `ubuntu` (not `root` or `ec2-user`)

## 💰 Cost Breakdown

### Year 1 (AWS Free Tier)
- EC2 Instance: **FREE**
- First 15 GB bandwidth: **FREE**
- Additional bandwidth: **~$0-8/month** (depending on usage)
- **Total: $0-96/year**

### Year 2+ (After Free Tier)
**Don't pay! Rotate to new AWS account every 6 months**
- Stay free forever with account rotation

### vs. Commercial VPNs
- NordVPN: ~$60-156/year
- ExpressVPN: ~$96-156/year
- **Your savings: $60-156/year**

## 📝 Useful Commands

```bash
# Start all services
docker-compose up -d

# Stop all services
docker-compose down

# View logs
docker-compose logs -f

# Restart specific service
docker-compose restart transmission

# Check VPN IP
docker exec transmission curl ifconfig.me

# Update all containers
docker-compose pull
docker-compose up -d

# Check disk usage
du -sh ./share/*
```

## 🎨 Possible Additions

- **Organizr:** Dashboard to navigate all your apps
- **Tautulli:** Plex monitoring and statistics
- **Requestrr:** Discord bot for media requests
- **Notifiarr:** Unified notification system
- **Autoscan:** Faster Plex library updates
- **Tdarr:** Automated media transcoding

## 🤝 Contributing

Feel free to fork this repository and customize it to your needs! If you find improvements or fixes, pull requests are welcome.

## 📜 License

This project is open source and available under the MIT License.

## ⭐ Credits

- Original Plex Stack concept by DonMcD
- Enhanced with self-hosted VPN by kevin6shah
- Built with love for the r/Plex and r/selfhosted communities

---

**Happy streaming! 🎬🍿**

*Last updated: November 2025*