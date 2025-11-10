# 🔄 6-Month VPN Renewal Guide

**⏱️ Time Required:** 10 minutes  
**📅 Do this:** Every 5.5 months (set calendar reminder!)

---

## ✅ Checklist

### Part 1: New AWS Account (5 minutes)

- [ ] Create new AWS account with unique email
  - Use: `youremail+1@gmail.com`, `+2@gmail.com`, etc.
- [ ] Add payment method
- [ ] **Set billing alarm to $1** (CloudWatch → Billing)
- [ ] Confirm email subscriptions

### Part 2: New EC2 Instance (3 minutes)

- [ ] Launch Instance:
  - **AMI:** Ubuntu Server 22.04 or 24.04 LTS
  - **Type:** t2.micro
  - **Key pair:** Create new or reuse
  - **Security Group:**
    - SSH (22) from My IP
    - Custom UDP (51820) from 0.0.0.0/0
- [ ] Note Public IP: `____________________`

### Part 3: Run Setup Script (2 minutes)

```bash
# SSH into new instance
chmod 400 ~/Downloads/your-new-key.pem
ssh -i ~/Downloads/your-new-key.pem ubuntu@NEW-EC2-IP

# Run automated setup
curl -sSL https://gist.githubusercontent.com/kevin6shah/7362dd9d7103c39f02130f158b0df42c/raw/automated-wireguard-setup.sh | sudo bash
```

- [ ] Copy entire CLIENT CONFIGURATION output (from `[Interface]` to `PersistentKeepalive`)

### Part 4: Update Mac (1 minute)

```bash
# In your project directory
cd ~/path/to/your-plex-stack

# Run updater
./update-vpn-config.sh

# Paste config when prompted, then Ctrl+D
```

### Part 5: Verify & Cleanup

```bash
# Test VPN (should show NEW EC2 IP)
docker exec transmission curl ifconfig.me

# If working, log into OLD AWS account and terminate old instance
```

- [ ] New VPN IP: `____________________`
- [ ] Old instance terminated
- [ ] Calendar reminder set for next rotation

---

## 🆘 Quick Troubleshooting

**VPN not connecting?**
```bash
docker logs wireguard
docker exec wireguard wg show
# Should see: "latest handshake: X seconds ago"
```

**Wrong IP showing?**
```bash
# Restart containers
docker-compose restart wireguard transmission
sleep 10
docker exec transmission curl ifconfig.me
```

**Can't SSH into EC2?**
- Check Security Group has port 22 open to YOUR current IP
- Verify key permissions: `chmod 400 key.pem`

---

## 📞 Support Links

- **AWS Free Tier Dashboard:** https://console.aws.amazon.com/billing/home#/freetier
- **EC2 Console:** https://console.aws.amazon.com/ec2
- **Setup Script Gist:** https://gist.github.com/kevin6shah/7362dd9d7103c39f02130f158b0df42c

---

## 💡 Pro Tips

1. **Do NOT delete old instance until new one is confirmed working**
2. **Keep old key pair files** - you might need to troubleshoot
3. **Run test torrents** before terminating old server
4. **Take screenshots** of your config for reference
5. **Update this guide** if you find better methods!

---

## 📊 Cost Tracking

| Account | Email | Created | Expires | Status |
|---------|-------|---------|---------|--------|
| #1 | youremail@gmail.com | 2025-11 | 2026-05 | ✅ Active |
| #2 | youremail+1@gmail.com | 2026-05 | 2026-11 | ⏳ Pending |
| #3 | youremail+2@gmail.com | - | - | - |

---

**Last Renewal:** ___________  
**Next Renewal:** ___________  
**Current EC2 IP:** ___________