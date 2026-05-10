# Friday Handoff

## Current Goal

Keep this repository as a maintained operations project for a local MacBook Pro media stack with:

- reliable request flow
- VPN-first download safety
- hard media-cap enforcement
- durable documentation and incident history

## Current Priorities

1. Keep the rebuilt TV automation path healthy with the current working source set.
2. Keep the media library inside the `80 GiB` cap.
3. Maintain repo-local operations docs after every change.
4. Expand movie backup coverage beyond the currently working public sources if needed.
5. Keep the AWS shared-host migration workflow documented and usable before the current free-tier window ends.

## Current Defaults

- Media cap: `80 GiB`
- Watched movie retention: `15` days
- Watched TV retention: `30` days
- Hard boundary: `share/media`
- Internal verification commands:
  - `./scripts/check-vpn.sh`
  - `./scripts/check-stack.sh`
  - `./scripts/enforce-media-cap.sh --dry-run`

## Known Risks

- `byparr` is the current default Cloudflare helper because it solved local `1337x` and `EZTV` tests faster than `flaresolverr` on this host, but both helpers still fail `TorrentGalaxyClone` and `The Pirate Bay`.
- `GloDLS` was requested but is not available in the current live Prowlarr schema on this host, so it is not part of the managed source set.
- Radarr and Sonarr now reach Transmission through `vpn-web-proxy:9091`; if download handoff breaks again, check that hostname before debugging Transmission itself.
- Bazarr still depends on SignalR for truly immediate subtitle searches after import. The sync fallback is now 15 minutes, but Bazarr's built-in wanted-search scheduler cannot be set below 6 hours in this version.
- Bazarr now rejects embedded subtitles as satisfying the desired language, but the current reachable provider set is effectively `tvsubtitles` only. That means the trigger path is better, but external subtitle downloads are still limited by provider coverage.
- `prowlarr` now shares the `wireguard` network namespace for egress, uses `http://vpn-web-proxy:9696` as the stable bridge-network access point, and reaches `radarr`/`sonarr` through the Docker bridge gateway at `172.18.0.1`.
- Maintainerr no longer uses the brittle `*.plex.direct` hostname, and scheduled rule runs now complete without app reachability errors, but the current runs have not altered any data yet.
- The installed VPN guard now runs from `~/Library/Application Support/friday-plex-stack/` because macOS background jobs could not reliably execute the repo copy from `Documents`.
- The media-cap script now supports `MEDIA_CAP_IO_MODE=docker`, which is the only safe way for a LaunchAgent to enforce the cap without depending on background access to the repo under `Documents`.
- The AWS EC2 host is shared with the Iris production backend in `/Users/kevinshah/Documents/mta-led-sign`; any account rotation must preserve both systems, not just WireGuard.
- The current AWS migration model is intentionally no-domain and raw-IP based; the durable references are `docs/AWS_MIGRATION.md`, `docs/AWS_BLUE_GREEN_RUNBOOK.md`, and `ops/aws/iam/README.md`.
- The AWS migration path now auto-detects offline-restore mode when the old host is dead, auto-runs `scripts/post-migration-smoke.sh`, and no longer depends on stale hardcoded EC2 instance/security-group IDs in `scripts/backup-aws-host.sh`.
- AWS shared-host backup is now a hard rule for Codex-managed infra changes: use `scripts/aws-infra-change.sh` for ad hoc AWS changes so pre/post backups happen automatically.
- Docker Desktop storage and macOS storage views are larger than `share/media`; that overhead must be tracked separately from the media cap.

## Current Working Sources

- Radarr: `YTS`, `1337x`, `Demonoid Clone`, `Nyaa.si`
- Sonarr: `1337x`, `Demonoid Clone`, `EZTV`, `Nyaa.si`, `showRSS`
- Disabled upstream: `The Pirate Bay`
- Still failing even with helpers: `TorrentGalaxyClone`, `The Pirate Bay`

## Immediate Next Steps

1. Keep the installed `com.friday.media-cap` LaunchAgent in its default `dry-run` mode until you explicitly want scheduled deletions, then switch it to `apply` and reinstall it.
2. Add alternative movie backups for the still-failing `TorrentGalaxyClone` and `The Pirate Bay` slots if you want more redundancy.
3. Improve Bazarr provider coverage if external sidecar subtitles should be guaranteed instead of opportunistic.
4. If Maintainerr should actively delete media instead of only evaluating rules, review the current rule set before forcing a manual execution.

## Backlog

1. Evaluate the best remote playback path over Tailscale for Plex and Jellyfin when the laptop stays awake under Amphetamine.
2. Turn the shared AWS migration into a true single-command cutover that also updates the live Iris board/backend URL, not just the Friday repo and local mta-led-sign sources.
3. Add a redacted shared-host inventory export so fresh agents can inspect the topology without touching secrets.
4. Turn the Friday local stack into a preflighted, mostly one-command bootstrap with env-driven settings instead of hardcoded host-specific values in tracked files.

## Resume Checklist

When returning to this repo in a future session:

1. Read `SAY_THIS_WHEN_AUTO_COMPACT.md`
2. Read `docs/CODEX_MAINTENANCE_LOOP.md`
3. Read `docs/MAINTENANCE_JOURNAL.md`
4. Run `./scripts/check-stack.sh`
5. Read the logs before making claims
