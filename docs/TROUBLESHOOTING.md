# Friday Troubleshooting

## Local-First Workflow

1. Reproduce the issue.
2. Read the relevant logs locally.
3. Inspect local config and database state.
4. Only if uncertainty remains, check official documentation or primary repositories.
5. Record the evidence and outcome in `docs/MAINTENANCE_JOURNAL.md`.

## Key Logs

- Overseerr: `config/overseerr/logs/`
- Sonarr: `config/sonarr/logs/`
- Radarr: `config/radarr/logs/`
- Prowlarr: `config/prowlarr/logs/`
- Maintainerr: `config/maintainerr/data/logs/`
- Plex: `config/plex/Library/Application Support/Plex Media Server/Logs/`
- Transmission: `docker logs transmission`
- WireGuard: `docker logs wireguard`

## Common Checks

### VPN and download path

```bash
./scripts/check-vpn.sh
./scripts/vpn-guard.sh
docker logs wireguard --tail 100
docker logs transmission --tail 100
```

For the installed macOS watchdog:

```bash
launchctl print gui/$(id -u)/com.friday.vpn-guard | sed -n '1,80p'
cat "$HOME/Library/Application Support/friday-plex-stack/vpn-guard.state"
cat "$HOME/Library/Logs/friday-plex-stack/vpn-guard.log"
```

### Request path

```bash
docker logs overseerr --tail 100
docker logs sonarr --tail 100
docker logs prowlarr --tail 100
docker logs flaresolverr --tail 100
docker logs byparr --tail 100
```

### Storage and cleanup path

```bash
./scripts/enforce-media-cap.sh --dry-run
docker logs maintainerr --tail 100
```

## Incident Notes

### Sonarr could not search TV

Symptom:

- Overseerr request succeeded, but Sonarr found no active TV indexers.

Local evidence:

- `config/overseerr/logs/overseerr-2026-04-08.log` shows the request path into Sonarr.
- `config/sonarr/logs/sonarr.debug.txt` shows Sonarr searching with zero active indexers.
- `config/prowlarr/logs/prowlarr.debug.txt` shows the upstream source disabled after a Cloudflare block.

Interpretation:

- This is not an Overseerr-to-Sonarr connectivity failure.
- It is an upstream source availability problem combined with a fragile single-source TV setup.

If Cloudflare-protected trackers are failing:

```bash
./scripts/cf-solverctl.py status
./scripts/cf-solverctl.py test flaresolverr
./scripts/cf-solverctl.py test byparr
docker exec prowlarr sh -lc 'wget -qO- https://api.ipify.org || curl -fsS https://api.ipify.org'
```

Interpretation:

- If both helpers still fail, the current VPN egress IP is still being challenged or blocked.
- If one helper works and the other fails, switch to the working helper and leave the other idle.
- Current local baseline on this host:
  - `1337x` works through both helpers
  - `EZTV` works through both helpers
  - `TorrentGalaxyClone` fails through both helpers
  - `The Pirate Bay` fails through both helpers

If Radarr or Sonarr cannot send items to Transmission:

```bash
./scripts/configure-download-clients.py
docker logs radarr --tail 100
docker logs sonarr --tail 100
```

Interpretation:

- The Arr apps must reach Transmission at `vpn-web-proxy:9091`, not `wireguard:9091`.
- If the host drifts back to `wireguard`, downloads will not hand off even though Transmission itself is healthy.

If Bazarr does not grab subtitles soon after import:

```bash
docker logs bazarr --tail 120
BAZARR_KEY=$(awk '/apikey:/ {print $2; exit}' config/bazarr/config/config.yaml)
curl -sS -H "X-Api-Key: $BAZARR_KEY" http://localhost:6767/api/system/tasks | jq '.data[] | select(.name|test("Sync with|Search for Missing"))'
```

Interpretation:

- Immediate subtitle search depends on Bazarr's SignalR connection to Radarr and Sonarr.
- In the current live config, `defer_search_signalr` is disabled for both Arr apps, so Bazarr should react immediately when the SignalR feed is healthy.
- Bazarr 1.5.3 will not accept wanted-search intervals below 6 hours, so the practical fallback is the tightened 15-minute Arr sync cadence plus the 6-hour wanted scan.

### Tailscale remote access fails after closing the lid

Symptom:

- Tailscale URLs worked while the MacBook lid was open, then stopped responding after the lid was closed.

Local evidence:

- `pmset -g assertions` can confirm whether Amphetamine is currently preventing idle sleep.
- Closed-lid operation still depends on the active Amphetamine session settings, not just the general presence of the app.

Interpretation:

- Preventing idle sleep while the lid is open is not the same as preventing system sleep when the display is closed.
- For lid-closed server use, the active Amphetamine session must explicitly disable `Allow system sleep when display is closed`.
- Amphetamine Enhancer should be installed, and the MacBook should stay on AC power during the test.

Quick check:

```bash
pmset -g assertions
./scripts/print-remote-access.sh
```

If lid-closed mode is still unreliable:

- restart the current Amphetamine session after changing the closed-display option
- test one of the Tailscale URLs from your phone with the lid closed
- if reliability still matters more than silence/portability, keep the lid open and let only the display sleep

### Maintainerr skipped cleanup

Symptom:

- Maintainerr started a rules run but skipped it because not all apps were reachable.

Local evidence:

- `config/maintainerr/data/logs/maintainerr.log` contains `Plex api communication failure`.
- `config/maintainerr/data/maintainerr.sqlite` stores Plex using a `*.plex.direct` hostname and SSL.
- `config/overseerr/settings.json` also uses a `*.plex.direct` hostname internally.

Interpretation:

- Internal app-to-app traffic should use Docker service names, not Plex discovery hostnames intended for external clients.

If the hostname fix does not clear Maintainerr:

- Verify container reachability directly:

```bash
docker exec maintainerr sh -lc 'wget -qO- http://plex:32400/identity'
```

- Compare the stored Maintainerr token with the Plex server token:
  - `config/maintainerr/data/maintainerr.sqlite` column: `settings.plex_auth_token`
  - `config/plex/Library/Application Support/Plex Media Server/Preferences.xml` attribute: `PlexOnlineToken`
- A stale Maintainerr token returns `401 Unauthorized` against `http://plex:32400/library/sections`.
- `./scripts/fix-internal-addresses.sh` now refreshes both the internal hostname and the Plex token from local Plex preferences.

## Allowed External Sources

Use these only after local inspection:

- Docker Compose docs: `https://docs.docker.com/compose/`
- Servarr wiki: `https://wiki.servarr.com/`
- Maintainerr docs: `https://docs.maintainerr.info/`
- Official GitHub repositories and release notes for the affected service
