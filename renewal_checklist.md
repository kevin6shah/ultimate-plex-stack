# 🔄 VPN Renewal Checklist - Every 6 Months

**Last Renewal:** `__________`  
**Next Renewal:** `__________`  
**Current EC2 IP:** `__________`  
**Current AWS Account:** `__________`

---

## Pre-Renewal (1 week before)

- [ ] **Verify current setup is working**
  ```bash
  docker exec transmission curl ifconfig.me
  # Should show: 54.90.132.5 (or your current EC2 IP)
  ```

- [ ] **Check AWS billing** - Make sure you're not being charged
  - https://console.aws.amazon.com/billing/home#/freetier

- [ ] **Backup important configs** (optional, if you customized anything)
  ```bash
  cp docker-compose.yml docker-compose.yml.backup
  cp ./config/transmission/settings.json ./backup-settings.json
  ```

---

## Day 1: New AWS Account Setup (10 minutes)

### ✅ Step 1: Create New AWS Account
- [ ] Use new email: `youremail+X@gmail.com` (increment X)
- [ ] New AWS Account Email: `________________`
- [ ] Add payment method (different card if possible)
- [ ] Complete verification

### ✅ Step 2: Set Billing Alarm **IMMEDIATELY**
- [ ] AWS Console → CloudWatch → Alarms → Billing
- [ ] Create alarm for **$1.00**
- [ ] Confirm email subscription!

### ✅ Step 3: Launch EC2 Instance
- [ ] Region: `__________` (use same as before)
- [ ] AMI: Ubuntu Server 22.04 or 24.04 LTS
- [ ] Instance type: t2.micro
- [ ] Create/download new key pair: `________________.pem`
- [ ] Security group rules:
  - [ ] SSH (22) from My IP
  - [ ] Custom UDP (51820) from 0.0.0.0/0
- [ ] Launch!
- [ ] **New EC2 Public IP:** `________________`

---

## Day 1: VPN Server Setup (5 minutes)

### ✅ Step 4: SSH and Run Setup Script

```bash
# Set permissions on new key
chmod 400 ~/Downloads/YOUR-NEW-KEY.pem

# SSH into new instance
ssh -i ~/Downloads/YOUR-NEW-KEY.pem ubuntu@NEW-EC2-IP

# Run automated setup
curl -sSL https://gist.githubusercontent.com/kevin6shah/7362dd9d7103c39f02130f158b0df42c/raw/automated-wireguard-setup.sh | sudo bash
```

- [ ] Script completed successfully
- [ ] **CLIENT CONFIGURATION copied** (from `[Interface]` to `PersistentKeepalive`)

---

## Day 1: Update Mac (5 minutes)

### ✅ Step 5: Update VPN Config on Mac

```bash
cd ~/Documents/friday-plex-stack  # Or your project path

# Update VPN config
./update-vpn-config.sh
# Paste the CLIENT CONFIGURATION, press Ctrl+D
```

- [ ] Config saved successfully
- [ ] Containers restarted

### ✅ Step 6: Verify Keys Match

```bash
./verify-vpn-keys.sh
```

- [ ] **If handshake fails:** Follow the script's instructions to update EC2 server with Mac's public key

### ✅ Step 7: Final Verification

```bash
# Should show NEW EC2 IP
docker exec transmission curl ifconfig.me

# Check it's actually different from your home IP
curl ifconfig.me
```

- [ ] **VPN IP:** `________________`
- [ ] **Home IP:** `________________`
- [ ] They are different ✅

### ✅ Step 8: Test Download

- [ ] Go to http://localhost:9091
- [ ] Add a small test torrent
- [ ] Verify it downloads through VPN

---

## Day 2: Cleanup Old Infrastructure

**⚠️ ONLY do this after confirming new VPN works for 24 hours!**

### ✅ Step 9: Terminate Old EC2 Instance

- [ ] Log into **OLD** AWS account
- [ ] EC2 Console → Instances
- [ ] Select old instance
- [ ] Instance State → Terminate
- [ ] Confirm termination

### ✅ Step 10: Update Documentation

- [ ] Update this checklist with new dates/IPs
- [ ] Update `README.md` if anything changed
- [ ] Commit changes to git:
  ```bash
  git add .
  git commit -m "VPN renewal: $(date +%Y-%m-%d)"
  git push
  ```

---

## Post-Renewal Tasks

- [ ] **Set calendar reminder** for 5.5 months from now
- [ ] **Test torrenting** for a few days to ensure stability
- [ ] **Monitor AWS billing** for the first week
- [ ] **Keep old key pair** file somewhere safe (just in case)

---

## 📊 Account History

| # | AWS Email | Created | Expired | EC2 IP | Notes |
|---|-----------|---------|---------|--------|-------|
| 1 | youremail@gmail.com | 2025-11 | 2026-05 | 54.90.132.5 | Initial setup |
| 2 | youremail+1@gmail.com | - | - | - | - |
| 3 | youremail+2@gmail.com | - | - | - | - |

---

## 🆘 Troubleshooting

### VPN not connecting after renewal?

```bash
# Check logs
docker logs wireguard
docker logs transmission

# Verify handshake
docker exec wireguard wg show

# Run verification
./verify-vpn-keys.sh
```

### Forgot to set billing alarm?

**Do it NOW!** Even after setup:
1. AWS Console → CloudWatch → Alarms → Billing
2. Create alarm for $1.00
3. Confirm email

### Old EC2 still running?

**Terminate it ASAP!** You'll be charged after free tier expires.

---

**Notes:**
- Keep this file updated with each renewal
- Save key pair files securely
- Document any custom changes you make
- Test thoroughly before terminating old infrastructure