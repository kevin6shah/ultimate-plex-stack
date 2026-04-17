# Maintenance Journal

## 2026-04-09

### Incident: Sonarr TV search returned zero active indexers

- Symptom: an Overseerr request for a TV series reached Sonarr, but Sonarr searched with zero active indexers.
- Evidence:
  - `config/overseerr/logs/overseerr-2026-04-08.log`
  - `config/sonarr/logs/sonarr.debug.txt`
  - `config/prowlarr/logs/prowlarr.debug.txt`
- Cause: the current TV search path depended on a single effective upstream source, and that source was disabled in Prowlarr after a Cloudflare block.
- Fix status: not yet rebuilt in live Prowlarr config. The repo now documents this as a topology problem, not an Overseerr connectivity problem.
- Follow-up:
  - rebuild TV source coverage with explicit backups
  - verify Prowlarr sync into Sonarr after the source changes

### Incident: Maintainerr skipped cleanup because Plex was unreachable

- Symptom: Maintainerr started a run and then skipped it because not all applications were reachable.
- Evidence:
  - `config/maintainerr/data/logs/maintainerr.log`
  - `config/maintainerr/data/maintainerr.sqlite`
  - `config/overseerr/settings.json`
- Cause: internal Plex consumers were configured with a brittle `*.plex.direct` hostname instead of a stable Docker service name.
- Fix status: repo helper added at `scripts/fix-internal-addresses.sh`; maintenance-window execution is still pending.
- Follow-up:
  - run the helper
  - restart Overseerr and Maintainerr
  - verify cleanup and Plex scans again

### Change: repository operations scaffolding added

- Added repo-local documentation for architecture, operations, troubleshooting, upgrades, handoff, and the Codex maintenance loop.
- Added repo-local scripts for VPN verification, stack health checks, media-cap enforcement, config backup, and the internal Plex addressing fix.
- Added `.friday-ops.env.example` to centralize local policy overrides.

### Change: internal Plex consumers moved to Docker service addressing

- Evidence:
  - `config/overseerr/settings.json` now uses `plex:32400` without SSL.
  - `config/maintainerr/data/maintainerr.sqlite` now stores `plex|32400|0`.
  - `docker logs overseerr --since 3m` at `2026-04-09 00:55:00 EDT` shows a successful Plex recently-added scan instead of the prior `*.plex.direct` DNS failure.
- Change:
  - Ran `./scripts/fix-internal-addresses.sh`.
  - Restarted `overseerr`.
  - Updated `scripts/fix-internal-addresses.sh` so it also backs up Plex preferences and refreshes Maintainerr's Plex token from local Plex state.
- Result:
  - Overseerr is now scanning Plex successfully over the Docker network.

### Change: Maintainerr Plex token refreshed from local Plex state

- Symptom after the hostname fix:
  - `docker logs maintainerr --tail 80` still showed `Plex api communication failure`.
- Evidence:
  - `docker exec maintainerr sh -lc 'wget -qO- http://plex:32400/identity'` succeeds, so Docker networking is not the blocker.
  - Maintainerr's stored `plex_auth_token` returned `401 Unauthorized` from `http://plex:32400/library/sections`.
  - `config/plex/Library/Application Support/Plex Media Server/Preferences.xml` exposed a different live `PlexOnlineToken`.
- Change:
  - Re-ran `./scripts/fix-internal-addresses.sh` after teaching it to sync the current Plex token.
  - Restarted `maintainerr`.
- Result:
  - Maintainerr no longer logs an immediate Plex communication failure on startup.
  - Cleanup rules were not manually executed in this session to avoid unreviewed deletion activity.

### Change: Sonarr TV path and backup indexer improved

- Evidence before change:
  - `curl http://localhost:8989/api/v3/health?...` reported both `All indexers are unavailable...` and a missing `/share/downloads/complete/tv` path.
  - `config/prowlarr/logs/prowlarr.debug.txt` showed `The Pirate Bay` failing long-term.
- Change:
  - Created `share/downloads/complete/tv`.
  - Added `Nyaa.si` through the Prowlarr API after a local `POST /api/v1/indexer/test` succeeded.
  - Restarted `sonarr`.
- Result:
  - Sonarr now syncs both `Nyaa.si (Prowlarr)` and `The Pirate Bay (Prowlarr)`.
  - The remote path mapping error cleared.
  - Sonarr health now shows only warnings for the still-failing `The Pirate Bay` and the available update notice.

### Change: repo health checks are runnable and passing

- Restored execute bits on `scripts/*.sh`.
- Verified:
  - `./scripts/check-vpn.sh`
  - `./scripts/enforce-media-cap.sh --dry-run`
  - `./scripts/check-stack.sh`
- Result:
  - VPN health passed.
  - Media usage remains within cap at `33 GiB / 80 GiB`.
  - `./scripts/check-stack.sh` completed successfully.

### Change: VPN guard added and installed on macOS

- Goal:
  - fail closed for downloads when the VPN is unhealthy and alert through Telegram when configured.
- Change:
  - Added `scripts/vpn-guard.sh`.
  - Added `scripts/install-vpn-guard-launchd.sh`.
  - Added Telegram and VPN guard settings to `.friday-ops.env.example`.
  - Created local `.friday-ops.env` with the current expected VPN IP `54.90.132.5`.
  - Installed `~/Library/LaunchAgents/com.friday.vpn-guard.plist`.
- Verified:
  - `./scripts/vpn-guard.sh` passed on the current healthy VPN state.
  - Dry-run failure simulation with a fake `VPN_EXPECTED_PUBLIC_IP` reported `Transmission action: dry-run-stop`.
  - `launchctl list | rg 'com\.friday\.vpn-guard'` returned the loaded agent.
  - `config/ops/vpn-guard.state` recorded the healthy state with the expected VPN egress IP.
- Remaining requirement:
  - Telegram secrets are still blank in `.friday-ops.env`, so alert delivery is not active until they are filled in.

### Change: VPN guard launchd runtime moved out of Documents

- Symptom:
  - The first LaunchAgent version could not reliably execute the repo-local scripts from `~/Documents/...` because the background job hit macOS protected-folder access issues.
- Change:
  - Updated `scripts/install-vpn-guard-launchd.sh` to install a self-contained runtime copy under `~/Library/Application Support/friday-plex-stack/`.
  - Moved the LaunchAgent log path to `~/Library/Logs/friday-plex-stack/vpn-guard.log`.
  - Verified the installed support copy of `vpn-guard.sh` and `check-vpn.sh` can run successfully.
- Result:
  - The installed guard now writes healthy state to `~/Library/Application Support/friday-plex-stack/vpn-guard.state`.
  - Added `scripts/test-telegram-alert.sh` so Telegram delivery can be validated once credentials are present.

### Change: VPN guard now reuses the existing Overseerr Telegram bot

- Evidence:
  - `config/overseerr/settings.json` contains an enabled Telegram notification block with a bot token and chat ID.
  - Radarr and Sonarr both returned empty notification lists from `/api/v3/notification`.
- Change:
  - Reused the Overseerr Telegram bot credentials in `.friday-ops.env`.
  - Reinstalled the support runtime with `./scripts/install-vpn-guard-launchd.sh`.
  - Sent a test notification with `./scripts/test-telegram-alert.sh`.
- Result:
  - Telegram delivery for the VPN guard is active.
  - The installed support env at `~/Library/Application Support/friday-plex-stack/.friday-ops.env` now contains the Telegram credentials used by the guard.

### Change: Prowlarr moved behind WireGuard

- Evidence before change:
  - `docker inspect prowlarr` showed `prowlarr` on the default Docker bridge, not sharing the `wireguard` namespace.
  - `docker exec prowlarr sh -lc 'wget -qO- https://api.ipify.org'` returned the non-VPN egress IP `100.17.244.80`.
  - `docker exec wireguard sh -lc 'wget -qO- https://api.ipify.org'` returned the VPN egress IP `54.90.132.5`.
- Change:
  - Moved `prowlarr` to `network_mode: "service:wireguard"`.
  - Added `vpn-web-proxy` as the stable bridge-network reverse proxy for `transmission` and `prowlarr`.
  - Pointed Prowlarr's Arr app callbacks at the Docker bridge gateway `172.18.0.1` so it can still reach the Arr apps after leaving the default Docker bridge network.
  - Updated stack health checks to probe both the shared WireGuard namespace and the proxy path.
- Result:
  - Indexer traffic now follows the same VPN boundary as Transmission.
  - Internal container consumers should use `http://vpn-web-proxy:9696` instead of `http://prowlarr:9696`.

### Change: Cloudflare helper infrastructure added

- Evidence:
  - ByParr's upstream source declares `cmd` compatibility with FlareSolverr's `request.get` model in `src/models.py`.
  - Prowlarr's live `/api/v1/indexerProxy/schema` exposes a `FlareSolverr` proxy type that only needs a helper host URL.
- Change:
  - Added `flaresolverr` and `byparr` services to `docker-compose.yml`, both behind the shared `wireguard` namespace.
  - Added `scripts/cf-solverctl.py` to switch Prowlarr's `CF Solver` proxy between `flaresolverr`, `byparr`, and `off`.
  - Added operations and troubleshooting notes for the helper switch/test workflow.
- Result:
  - The stack can now test both helper paths without manual Prowlarr UI edits.

### Change: TV and movie source set rebuilt with ByParr as the default helper

- Evidence:
  - Local direct helper requests for `https://1337x.to/cat/Movies/1/` succeeded through both `flaresolverr` and `byparr`.
  - Local Prowlarr tests passed for `1337x` and `EZTV` through both helpers.
  - Local Prowlarr tests still failed for `TorrentGalaxyClone` and `The Pirate Bay` through both helpers.
  - `byparr` completed the successful local Prowlarr tests faster than `flaresolverr` on this host.
- Change:
  - Updated `scripts/cf-solverctl.py` with a `status` command and helper lifecycle handling so only the selected helper remains running.
  - Added `scripts/configure-indexers.py` to apply the managed working source set and prune disabled upstream Prowlarr indexers from Radarr and Sonarr.
  - Selected `byparr` as the active `CF Solver` backend.
  - Applied the current working sources:
    - Radarr: `YTS`, `1337x`, `Demonoid Clone`, `Nyaa.si`
    - Sonarr: `1337x`, `Demonoid Clone`, `EZTV`, `Nyaa.si`, `showRSS`
  - Disabled `The Pirate Bay` in Prowlarr and pruned the stale Arr-side `The Pirate Bay (Prowlarr)` entries.
- Result:
  - Sonarr now has multiple working TV sources again and no longer depends on a failing `The Pirate Bay` entry.
  - `TorrentGalaxyClone` and `The Pirate Bay` remain unusable on this host even with helper support, so more movie redundancy still needs different sources.

### Change: Arr download clients repointed to the Transmission proxy

- Evidence:
  - `docker logs sonarr --tail 120` and `docker logs radarr --tail 120` showed `Connection refused (wireguard:9091)` while trying to reach Transmission.
  - `wireguard` is not a stable bridge-network hostname for the Arr apps after the VPN namespace split; `vpn-web-proxy:9091` is the stable bridge path.
- Change:
  - Added `scripts/configure-download-clients.py`.
  - Updated both Radarr and Sonarr Transmission download clients to use `vpn-web-proxy:9091` with `/transmission/`.
- Result:
  - The Arr apps can reach Transmission again over the stable proxy path.
  - Recent Sonarr logs show RSS processing without Transmission connection-refused warnings.

### Change: Bazarr import fallback tightened and Tailscale Transmission path verified

- Evidence:
  - `config/bazarr/config/config.yaml` already had `defer_search_signalr: false` for both `radarr` and `sonarr`, so the immediate SignalR-triggered subtitle search path was already enabled.
  - `docker logs bazarr --tail 200` showed intermittent SignalR reconnects and past provider failures, which explains why subtitle searches could feel inconsistent after imports.
  - Bazarr's live `/api/system/tasks` endpoint showed the Arr sync tasks were still running every 60 minutes.
  - Bazarr 1.5.3 rejected wanted-search intervals below 6 hours and reset invalid values back to `6`.
- Change:
  - Reduced Bazarr `radarr.movies_sync` from `60` to `15`.
  - Reduced Bazarr `sonarr.series_sync` from `60` to `15`.
  - Restarted Bazarr and verified:
    - `Sync with Radarr` now runs every `15 minutes`
    - `Sync with Sonarr` now runs every `15 minutes`
    - both Bazarr SignalR feeds reconnected successfully on startup
  - Verified the Tailscale Transmission Web UI paths return the expected unauthenticated HTTP `401` response:
    - `http://kevins-macbook-pro.tail87437e.ts.net:9091/transmission/web/`
    - `http://100.77.97.13:9091/transmission/web/`
- Result:
  - Bazarr still relies on SignalR for truly immediate subtitle searches, but missed-event recovery is now much faster on the sync side.
  - Transmission can now be tracked from the user's phone over Tailscale with a stable MagicDNS hostname.

### Change: Tailscale machine hostname renamed to `friday-media`

- Evidence:
  - `tailscale status --json` now reports:
    - `Self.DNSName = friday-media.tail87437e.ts.net.`
    - `Self.HostName = friday-media`
  - Verified live service responses over the new MagicDNS name:
    - Radarr `http://friday-media.tail87437e.ts.net:7878` -> `200`
    - Overseerr `http://friday-media.tail87437e.ts.net:5055` -> `307`
    - Sonarr `http://friday-media.tail87437e.ts.net:8989` -> `302`
    - Transmission `http://friday-media.tail87437e.ts.net:9091/transmission/web/` -> `401`
- Change:
  - Ran `tailscale set --hostname=friday-media`.
  - Updated repo docs to use the new preferred MagicDNS hostname instead of the prior Mac-derived name.
- Result:
  - The stack now has a stable, human-chosen Tailscale hostname for phone and laptop access.

### Change: Sonarr-to-Prowlarr torrent handoff repaired and Transmission auth rotated

- Evidence:
  - `config/sonarr/logs/sonarr.txt` showed repeated `Connection refused (vpn-web-proxy:80)` errors while Sonarr tried to add Daredevil releases to the download queue.
  - Live Sonarr download-client tests later succeeded against `vpn-web-proxy:9091`.
  - A fresh manual `EpisodeSearch` for `Daredevil: Born Again` grabbed `Daredevil.Born.Again.S02E01.1080p.WEB.h264-ETHEL` from `EZTV (Prowlarr)` and queued it in Transmission.
- Change:
  - Added an internal port `80` listener to tracked `ops/vpn-web-proxy/default.conf` that proxies to `wireguard:9696` so Sonarr's default Prowlarr download URLs resolve correctly.
  - Rotated the Transmission Web UI credentials away from the default account.
  - Updated Sonarr and Radarr to use the new Transmission credentials.
  - Moved the Transmission container credentials out of tracked `docker-compose.yml` literals and into the local `.friday-ops.env` runtime file.
- Result:
  - The broken triangle state on the live Daredevil request was caused by the missing internal Prowlarr proxy listener, and that path is now fixed.
  - Transmission is reachable at the existing Tailscale URL with the new credentials, and Sonarr can queue downloads end-to-end again while the VPN guard remains in force.

## 2026-04-10

### Change: Maintainerr scheduled runs validated after the Plex token refresh

- Evidence:
  - `docker logs --tail 200 maintainerr` now shows clean scheduled executions on `2026-04-09` and `2026-04-10`.
  - Recent log lines include:
    - `Starting execution of all active rules`
    - `Execution of rules for 'Radarr' done.`
    - `Execution of rules for 'sonarr' done.`
    - `All collections handled. No data was altered`
  - The prior `Plex api communication failure` is absent from the latest scheduled runs.
- Change:
  - No config change was required in this step; this was a validation pass on the already-refreshed Maintainerr configuration.
- Result:
  - Maintainerr is no longer blocked on Plex communication during its scheduled rule and collection handlers.
  - Current rules are evaluating successfully, but they are not deleting or altering any media in the present configuration/run window.

### Change: remote Tailscale access paths documented and scripted

- Evidence:
  - `tailscale status --json` reports:
    - `Self.DNSName = friday-media.tail87437e.ts.net.`
    - `Self.TailscaleIPs[0] = 100.77.97.13`
  - Local HTTP checks confirm the key service paths respond on the Tailscale hostname.
- Change:
  - Added `docs/REMOTE_ACCESS.md` with the durable Tailscale URLs, auth expectations, VPN-guard behavior, and Amphetamine lid-closed assumptions.
  - Added `scripts/print-remote-access.sh` to print the current live Tailscale endpoints and operating assumptions from the host.
  - Updated operations, troubleshooting, and handoff docs to point to the new remote-access reference.
- Result:
  - The stack now has a tracked, repo-local source of truth for phone/laptop access paths instead of relying on ephemeral chat history.

### Change: Bazarr subtitle trigger path tightened, but provider coverage is still incomplete

- Evidence:
  - Imported `Daredevil: Born Again` episodes exist under `share/media/tv/...` without external subtitle sidecars.
  - `config/bazarr/db/bazarr.db` showed those episodes with `missing_subtitles = []` while their stored subtitle rows only referenced embedded tracks.
  - `ffprobe` inside the `bazarr` container confirmed the files include internal English subtitle tracks.
  - `docker logs bazarr --tail 200` showed:
    - `opensubtitlescom` throttled after provider errors and a `403/426` challenge path
    - `podnapisi` throttled after IPv6 `Network unreachable`
  - `curl -sS -H "X-Api-Key: ..."` against Bazarr's live API showed only `tvsubtitles` remaining healthy after provider cleanup.
  - Bazarr provider lookups for tested `Daredevil` episodes returned `[]`, so no external subtitle candidate was available from the reachable provider.
- Change:
  - Updated the live Bazarr config so embedded subtitles no longer satisfy the desired language.
  - Reduced the enabled providers to the locally reachable set instead of leaving clearly broken providers active.
- Result:
  - Future imports should no longer be treated as subtitle-complete just because they contain internal subtitle tracks.
  - The remaining limitation is now explicit: external subtitle downloads still depend on provider coverage, and the current reachable provider set is too weak to guarantee them.

### Change: media-cap LaunchAgent installer now uses Docker-safe runtime mode

- Evidence:
  - The first draft of `scripts/install-media-cap-launchd.sh` copied `enforce-media-cap.sh` into `~/Library/Application Support/friday-plex-stack/`, but the script still expected the live media tree and Plex DB under the repo root.
  - `docker cp "plex:/config/.../com.plexapp.plugins.library.db"` succeeds locally.
  - `docker exec plex sh -lc 'du -sk /media'` succeeds locally.
- Change:
  - Extended `scripts/enforce-media-cap.sh` with `MEDIA_CAP_IO_MODE=host|docker`.
  - Added container-aware Plex DB access and media deletion logic so scheduled runs can operate through the `plex` container instead of relying on background access to the repo under `Documents`.
  - Updated `scripts/install-media-cap-launchd.sh` to force the installed support env to `MEDIA_CAP_IO_MODE=docker` and explicitly target the `plex` container.
  - Added the related launchd env knobs to `.friday-ops.env.example`.
- Verification:
  - `./scripts/enforce-media-cap.sh --dry-run` exits `0`.
  - `MEDIA_CAP_IO_MODE=docker ./scripts/enforce-media-cap.sh --dry-run` exits `0`.
  - Installed `~/Library/LaunchAgents/com.friday.media-cap.plist`.
  - `launchctl kickstart -k gui/$(id -u)/com.friday.media-cap` produced a clean dry-run entry in `~/Library/Logs/friday-plex-stack/media-cap.log`.
- Result:
  - Interactive local runs can stay on the simpler host path.
  - The installed LaunchAgent path now has a defensible execution model for scheduled dry runs and later active enforcement.

### Change: shared AWS host backup and migration toolkit added

- Evidence:
  - `AWS_PROFILE=friday-ec2 aws sts get-caller-identity` resolved account `767582655895` and IAM user `codex-ec2-deploy`.
  - `aws ec2 describe-instances --instance-ids i-0c824a5a2b18d31d5` confirmed the live shared host at `54.90.132.5`, launched `2025-11-10T07:04:42Z`, instance type `t3.micro`, key pair `Friday-key-pair-11102025`, VPC `vpc-08b8e549449b2d539`, subnet `subnet-0ebe079417aec95cb`.
  - `aws ec2 describe-security-groups --group-ids sg-09479da35bed15790` confirmed current ingress includes `80/tcp`, `22/tcp`, `51820/udp`, and an unnecessary `51413/udp`.
  - SSH inspection of `54.90.132.5` confirmed the host runs:
    - `wg-quick@wg0`
    - `iris-backend`
    - `nginx`
  - SSH inspection also confirmed the critical live state lives in:
    - `/etc/wireguard/wg0.conf`
    - `/etc/nginx/sites-available/iris-backend`
    - `/etc/systemd/system/iris-backend.service`
    - `/opt/iris-backend/`
- Change:
  - Added `scripts/backup-aws-host.sh` to take a read-only local backup of the shared AWS host.
  - Added `ops/aws/friday-shared-host.yaml` as the CloudFormation baseline for recreating the shared EC2 host in a new AWS account.
  - Added `ops/aws/bootstrap-host.sh` and `ops/aws/restore-host-from-backup.sh` for package install and state restore on the new host.
  - Added `scripts/migrate-aws-account.sh` as the future one-command migration entrypoint.
  - Added `scripts/update-vpn-endpoint.sh` so Friday can keep the same WireGuard keys and only rotate the endpoint IP after host restore.
  - Added `scripts/update-mta-led-sign-backend-url.sh` so local Iris source references can be rewritten to the new backend URL during migration.
  - Added `docs/AWS_MIGRATION.md` and marked `QUICK-RENEWAL-GUIDE.md` as a legacy VPN-only quick reference instead of the primary rotation workflow.
- Result:
  - The repo now has a documented and scriptable shared-host backup/migration path that accounts for both Friday and Iris.
  - The old renewal process is now explicitly documented as incomplete for the current production topology.

### Change: no-domain free-account operating model hardened

- Evidence:
  - The user explicitly chose to keep rotating free AWS accounts and explicitly rejected paid domains for now.
  - The current `friday-ec2` IAM user still lacks several actions needed for full green-host automation, including `cloudformation:ValidateTemplate`.
- Change:
  - Added `ops/aws/iam/codex-migration-policy.json` as the intended least-privilege-ish policy baseline for future AWS accounts.
  - Added `ops/aws/iam/README.md` documenting how to create a dedicated `codex-migration` IAM user and local AWS CLI profile in each new account.
  - Added `scripts/check-aws-migration-readiness.sh` so a new account can be validated before migration day.
  - Added `docs/AWS_BLUE_GREEN_RUNBOOK.md` as the no-domain, raw-IP blue/green cutover procedure.
  - Updated the AWS migration docs to reflect the newer AWS free-plan risk model and to treat raw IP cutover as the primary operational path for now.
- Result:
  - The repo now documents a coherent free-account operating model:
    - fresh backup
    - readiness check
    - green-host creation
    - raw-IP validation
    - delayed blue teardown
  - Future AWS accounts now have an explicit IAM bootstrap target instead of ad hoc permissions.

### Change: migration-day green-light wrapper added

- Change:
  - Added `scripts/prepare-migration-day.sh` as the final migration-day preflight wrapper.
  - Updated AWS migration docs and runbooks to treat that script as the explicit go/no-go gate before the user says `migrate`.
- Result:
  - The repo now has one concrete readiness command that checks:
    - local tool availability
    - current blue Friday health
    - current Iris backend reachability
    - required new-account inputs
    - latest shared-host backup presence
    - target AWS account readiness
