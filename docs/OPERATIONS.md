# Friday Operations

## Read This First

Before making any change:

1. Read `docs/CODEX_MAINTENANCE_LOOP.md`.
2. Read the latest entries in `docs/MAINTENANCE_JOURNAL.md`.
3. Run `./scripts/check-stack.sh`.
4. Inspect the relevant service logs before acting.

## Day-to-Day Commands

### Health verification

```bash
./scripts/check-vpn.sh
./scripts/vpn-guard.sh
./scripts/check-stack.sh
./scripts/cf-solverctl.py status
./scripts/cf-solverctl.py test flaresolverr
```

### Storage verification

```bash
du -sh share/media share/media/movies share/media/tv share/downloads
./scripts/enforce-media-cap.sh --dry-run
```

### Backup before maintenance

```bash
./scripts/backup-services.sh
./scripts/backup-aws-host.sh
```

## Safe Restart Order

Use this order during maintenance windows:

1. `wireguard`
2. `transmission`
3. `prowlarr`
4. `radarr`
5. `sonarr`
6. `overseerr`
7. `maintainerr`
8. `plex` only if playback is not active

Preferred commands:

```bash
docker compose up -d wireguard transmission
docker compose up -d prowlarr flaresolverr byparr radarr sonarr overseerr maintainerr
docker compose up -d plex
```

## Cloudflare Helper Switching

`prowlarr`, `flaresolverr`, and `byparr` all share the `wireguard` network namespace so they egress through the same VPN IP.

Use the switcher to repoint Prowlarr's Cloudflare helper:

```bash
./scripts/cf-solverctl.py status
./scripts/cf-solverctl.py switch flaresolverr
./scripts/cf-solverctl.py switch byparr
./scripts/cf-solverctl.py switch off
```

To test the Cloudflare-prone indexers through a helper:

```bash
./scripts/cf-solverctl.py test flaresolverr
./scripts/cf-solverctl.py test byparr
```

The switcher maintains a Prowlarr proxy named `CF Solver` tagged with `cf-bypass`.

Current local result:

- `byparr` is the default helper to keep active.
- `1337x` and `EZTV` pass local Prowlarr tests through both helpers.
- `TorrentGalaxyClone` and `The Pirate Bay` still fail local Prowlarr tests through both helpers.

To reapply the managed working source set:

```bash
./scripts/configure-indexers.py
```

To reapply the stable Transmission host in Radarr and Sonarr:

```bash
./scripts/configure-download-clients.py
```

## Tailscale Access

Transmission is reachable from other Tailscale devices through the Mac's Tailnet identity:

```text
http://friday-media.tail87437e.ts.net:9091/transmission/web/
```

Alternate direct Tailscale IP:

```text
http://100.77.97.13:9091/transmission/web/
```

Verification on this host returned HTTP `401`, which is the expected unauthenticated Transmission login response.

## VPN-First Policy

- Treat the VPN as a hard prerequisite for download health.
- If `./scripts/check-vpn.sh` fails, assume downloads are unsafe until proven otherwise.
- Do not continue with download troubleshooting until the VPN check passes.
- When the media cap is exceeded and cleanup cannot recover enough space, `./scripts/enforce-media-cap.sh --apply` stops recently active Transmission torrents as a safety brake.
- `./scripts/vpn-guard.sh` is the repo-local fail-closed watchdog:
  - it runs `./scripts/check-vpn.sh`
  - it stops `transmission` if the VPN check fails
  - it sends a Telegram alert when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured
  - it now checks egress through the `wireguard` namespace directly, so a stopped `transmission` container does not create a false VPN failure by itself
  - by default it suppresses unhealthy Telegram alerts when `transmission` is already stopped
  - by default it requires `3` consecutive failed checks before sending an unhealthy alert
  - it writes state to `config/ops/vpn-guard.state`

To install the watchdog on macOS:

```bash
./scripts/install-vpn-guard-launchd.sh
launchctl list | rg 'com\.friday\.vpn-guard'
```

This installs:

- `~/Library/LaunchAgents/com.friday.vpn-guard.plist`
- runtime copy: `~/Library/Application Support/friday-plex-stack/`
- log file: `~/Library/Logs/friday-plex-stack/vpn-guard.log`

The guard runs every 60 seconds by default.

To test Telegram delivery after adding bot credentials:

```bash
./scripts/test-telegram-alert.sh
```

## Storage Policy

- Hard media cap: `80 GiB`
- Watched movie retention: `15` days
- Watched TV retention: `30` days
- Pinned items should be protected with `MEDIA_NEVER_DELETE_PATTERNS` in `.friday-ops.env`

Recommended local override file:

```bash
cp .friday-ops.env.example .friday-ops.env
```

Example overrides:

```bash
MEDIA_CAP_GB=80
MEDIA_NEVER_DELETE_PATTERNS=The Godfather,Friday Favorites
MEDIA_CAP_IO_MODE=host
MEDIA_CAP_LAUNCHD_MODE=dry-run
MEDIA_CAP_LAUNCHD_HOUR=3
MEDIA_CAP_LAUNCHD_MINUTE=30
VPN_EXPECTED_PUBLIC_IP=203.0.113.10
TELEGRAM_BOT_TOKEN=123456:example
TELEGRAM_CHAT_ID=123456789
```

Transmission credentials are local runtime secrets now. Keep the active values in `.friday-ops.env` and in the Arr download-client configs; do not reintroduce them as tracked literals in `docker-compose.yml`.

## Remote Access

Use Tailscale for phone and laptop access. The durable URL and sleep notes are documented in `docs/REMOTE_ACCESS.md`.

To print the current live endpoints from the host:

```bash
./scripts/print-remote-access.sh
```

## AWS Shared Host

The AWS EC2 host is shared infrastructure:

- Friday WireGuard VPN
- Iris backend from `/Users/kevinshah/Documents/mta-led-sign`
- Friday personal agent account-rotation workflow

Do not treat AWS rotation as a Friday-only VPN task.

Primary doc:

- `docs/AWS_MIGRATION.md`
- `docs/AWS_BLUE_GREEN_RUNBOOK.md`

Safe backup command:

```bash
./scripts/backup-aws-host.sh
./scripts/check-aws-migration-readiness.sh
./scripts/prepare-migration-day.sh
```

Agent deployment and budget/cost visibility:

```bash
bash ./scripts/sync-env-from-ssm.sh
bash ./scripts/sync-ssm-from-env.sh
AWS_PROFILE=iris AWS_REGION=us-east-1 ./scripts/check-agent-migration-readiness.sh
AWS_PROFILE=iris AWS_REGION=us-east-1 ./scripts/deploy-agent.sh
AWS_PROFILE=iris AWS_REGION=us-east-1 HOST=<shared-host-ip> EC2_SSH_KEY=/path/to/key.pem ./scripts/deploy-hands-host.sh
```

Deterministic AWS cost model:

- `docs/AWS_COST_MODEL.md`
- update it whenever the AWS architecture changes

AWS migration changelog:

- `docs/AWS_MIGRATION_HISTORY.md`

Before expecting CloudWatch billing widgets or `$5/$10/$15/$20` cost alarms to work, enable `Receive CloudWatch Billing Alerts` in the payer account's AWS Billing Preferences.

Future migration entrypoint:

```bash
AWS_PROFILE=<new-account-profile> \
KEY_NAME=<new-keypair-name> \
EC2_SSH_KEY=/path/to/new-key.pem \
./scripts/migrate-aws-account.sh --yes
```

IAM bootstrap for future accounts:

- `ops/aws/iam/README.md`
- `ops/aws/iam/codex-migration-policy.json`

## Friday Personal Agent

Primary doc:

- `docs/FRIDAY_AGENT.md`

The control plane is serverless AWS infrastructure, but heavy-task execution now reuses the shared EC2 host through an isolated rootless Docker worker. It uses:

- Lambda container image from `agent/`
- ECR repository from `ops/aws/friday-agent.yaml`
- Lambda Function URL for `/telegram`, `/siri`, and `/health`
- SQS for async light Telegram work
- DynamoDB for transient sessions, 48-hour context, durable `#memory`, heavy jobs, checkpoints, approvals, and spend
- The 48-hour context layer now stores raw thread turns and a rolling summary, with retrieval controlled by persisted config rather than hard-coded assumptions.
- S3 for heavy-task inputs and outputs
- SSM SecureString parameters for tokens and the worker key
- Rootless Docker on the shared host for the Friday hands runtime

Required target-account preflight before deployment or migration:

```bash
AWS_PROFILE=<profile> AWS_REGION=us-east-1 ./scripts/check-agent-migration-readiness.sh
```

Deploy/update:

```bash
AWS_PROFILE=<profile> AWS_REGION=us-east-1 ./scripts/deploy-agent.sh
```

Install/update the shared-host hands runtime:

```bash
AWS_PROFILE=<profile> AWS_REGION=us-east-1 HOST=<shared-host-ip> EC2_SSH_KEY=/path/to/key.pem ./scripts/deploy-hands-host.sh
```

Deploy/update and set the Telegram webhook:

```bash
AWS_PROFILE=<profile> AWS_REGION=us-east-1 SET_TELEGRAM_WEBHOOK=1 ./scripts/deploy-agent.sh
```

Backup metadata before live agent infrastructure changes:

```bash
AWS_PROFILE=<profile> AWS_REGION=us-east-1 ./scripts/backup-agent-state.sh
```

Local validation:

```bash
python3 -m py_compile agent/app/*.py
cd agent && python3 -m pytest -q
```

Operational notes:

- Siri long tasks must notify through Telegram only; do not add delayed Siri speech for queued work.
- `--skip-agent` on `scripts/migrate-aws-account.sh` is only for emergency host-only rotations.
- Logfire is disabled by default; full content logging is opt-in only.

## Autonomous Cleanup Workflow

1. Review current usage:

```bash
./scripts/enforce-media-cap.sh --dry-run
```

2. During a maintenance window, enable cleanup:

```bash
./scripts/enforce-media-cap.sh --apply
```

Behavior:

- watched media is selected from the Plex database
- deletion candidates are ordered oldest watched first
- if the cap is still exceeded after eligible cleanup, Transmission is paused

`MEDIA_CAP_IO_MODE=host` is appropriate for direct interactive runs from the repo. The macOS LaunchAgent installer overrides the installed runtime to `MEDIA_CAP_IO_MODE=docker` so scheduled runs can operate through the `plex` container without depending on background access to the repo under `Documents`.

To install the scheduled macOS dry run:

```bash
./scripts/install-media-cap-launchd.sh
launchctl print gui/$(id -u)/com.friday.media-cap | sed -n '1,80p'
tail -n 50 "$HOME/Library/Logs/friday-plex-stack/media-cap.log"
```

This installs:

- `~/Library/LaunchAgents/com.friday.media-cap.plist`
- runtime copy: `~/Library/Application Support/friday-plex-stack/`
- log file: `~/Library/Logs/friday-plex-stack/media-cap.log`

Default schedule:

- daily at `03:30`
- mode `dry-run`

To switch the scheduled job to active deletion, update local `.friday-ops.env` and reinstall:

```bash
MEDIA_CAP_LAUNCHD_MODE=apply ./scripts/install-media-cap-launchd.sh
```

## Internal Plex Addressing Fix

The repo includes a helper to move internal Plex consumers to Docker-internal addressing:

```bash
./scripts/fix-internal-addresses.sh
```

This updates on-disk config for Overseerr and Maintainerr. Restart those services afterwards for the running containers to pick up the new settings.

## VPN Guard Notes

- `VPN_EXPECTED_PUBLIC_IP` should be the VPN server's public egress IP, not the home WAN IP.
- If the VPN is renewed and the egress IP changes, update `.friday-ops.env` before relying on the guard.
- The guard does not automatically restart `transmission` after recovery. Inspect the VPN first, then start it manually.
- The installed LaunchAgent uses the copied support runtime under `~/Library/Application Support/friday-plex-stack/` so it does not depend on macOS background access to the repo in `Documents`.
- Check the installed state file at `~/Library/Application Support/friday-plex-stack/vpn-guard.state`.

## Post-Change Checklist

After every material change:

1. Re-run `./scripts/check-stack.sh`
2. Check the affected service logs
3. Update `docs/MAINTENANCE_JOURNAL.md`
4. Update the relevant doc in `docs/`
5. If the change altered normal operations, update `SAY_THIS_WHEN_AUTO_COMPACT.md`
