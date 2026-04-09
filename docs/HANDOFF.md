# Friday Handoff

## Current Goal

Keep this repository as a maintained operations project for a local MacBook Pro media stack with:

- reliable request flow
- VPN-first download safety
- hard media-cap enforcement
- durable documentation and incident history

## Current Priorities

1. Keep the rebuilt TV automation path healthy with the current working source set.
2. Validate the next scheduled Maintainerr rule run with the refreshed Plex token before forcing any cleanup.
3. Keep the media library inside the `80 GiB` cap.
4. Maintain repo-local operations docs after every change.
5. Expand movie backup coverage beyond the currently working public sources if needed.

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
- `prowlarr` now shares the `wireguard` network namespace for egress, uses `http://vpn-web-proxy:9696` as the stable bridge-network access point, and reaches `radarr`/`sonarr` through the Docker bridge gateway at `172.18.0.1`.
- Maintainerr no longer uses the brittle `*.plex.direct` hostname, but its cleanup path still needs validation on the next scheduled run after the Plex token refresh.
- The installed VPN guard now runs from `~/Library/Application Support/friday-plex-stack/` because macOS background jobs could not reliably execute the repo copy from `Documents`.
- Docker Desktop storage and macOS storage views are larger than `share/media`; that overhead must be tracked separately from the media cap.

## Current Working Sources

- Radarr: `YTS`, `1337x`, `Demonoid Clone`, `Nyaa.si`
- Sonarr: `1337x`, `Demonoid Clone`, `EZTV`, `Nyaa.si`, `showRSS`
- Disabled upstream: `The Pirate Bay`
- Still failing even with helpers: `TorrentGalaxyClone`, `The Pirate Bay`

## Immediate Next Steps

1. Observe the next scheduled Maintainerr rule execution before manually running cleanup actions.
2. Decide how the media-cap script should be scheduled on macOS after validation.
3. Add alternative movie backups for the still-failing `TorrentGalaxyClone` and `The Pirate Bay` slots if you want more redundancy.
4. Add Tailscale access paths for Overseerr and Transmission from phone/laptop to the durable docs, not just the current live hostname.

## Backlog

1. Add Tailscale-based remote access for request apps so Overseerr can be used off-LAN from phone and laptop.
2. Add Tailscale-based remote access for Transmission Web UI from phone, with VPN guard behavior documented so downloads still fail closed if WireGuard is unhealthy.
3. Evaluate the best remote playback path over Tailscale for Plex and Jellyfin when the laptop stays awake under Amphetamine.
4. Document the exact remote URLs, auth expectations, and battery/sleep assumptions for the MacBook-hosted stack.
5. Add a durable AWS/WireGuard rotation workflow so yearly account replacement is a scripted config swap instead of a manual rebuild.

## Resume Checklist

When returning to this repo in a future session:

1. Read `SAY_THIS_WHEN_AUTO_COMPACT.md`
2. Read `docs/CODEX_MAINTENANCE_LOOP.md`
3. Read `docs/MAINTENANCE_JOURNAL.md`
4. Run `./scripts/check-stack.sh`
5. Read the logs before making claims
