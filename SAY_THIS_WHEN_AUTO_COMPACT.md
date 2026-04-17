# Friday Auto-Compact Re-entry Prompt

Treat `/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack` as an actively maintained operations project, not a one-off setup.

Before you do anything:

1. Read `docs/HANDOFF.md`
2. Read `docs/CODEX_MAINTENANCE_LOOP.md`
3. Read `docs/MAINTENANCE_JOURNAL.md`
4. Run `./scripts/check-stack.sh`
5. Inspect the relevant logs and local config before making any claim

Rules:

- local evidence first
- official docs and primary repositories only when external verification is needed
- say `unknown` when the repo and sources do not support a claim
- update the docs and journal after every material change
- after a validated stable point, commit and push the repo state unless doing so would include local secrets or unrelated user changes
- avoid restarting Plex during active playback unless the user explicitly accepts it

Current project goals:

- restore reliable TV automation
- keep VPN-first download safety intact
- keep the `launchd` VPN guard and Telegram alert path intact
- keep the installed guard runtime under `~/Library/Application Support/friday-plex-stack/` healthy
- enforce the `80 GiB` media cap on `share/media`
- maintain durable handoff and troubleshooting docs

Current live baseline:

- `byparr` is the active Cloudflare helper
- working Radarr sources: `YTS`, `1337x`, `Demonoid Clone`, `Nyaa.si`
- working Sonarr sources: `1337x`, `Demonoid Clone`, `EZTV`, `Nyaa.si`, `showRSS`
- `The Pirate Bay` is intentionally disabled
- `TorrentGalaxyClone` and `The Pirate Bay` still fail local helper-backed tests
- Radarr and Sonarr should use Transmission at `vpn-web-proxy:9091`
- `vpn-web-proxy` must also listen on internal port `80` and proxy that to Prowlarr on `wireguard:9696`, otherwise Sonarr release grabs can fail with `Connection refused (vpn-web-proxy:80)`
- Bazarr should have `radarr.movies_sync: 15`, `sonarr.series_sync: 15`, and `defer_search_signalr: false` for both
- Bazarr wanted-search intervals cannot go below 6 hours in version `1.5.3`
- Bazarr should not count embedded subtitles as satisfying the desired language, but external subtitle success is still limited by provider coverage; on this host `tvsubtitles` is currently the only clean provider path
- preferred Tailscale hostname: `friday-media.tail87437e.ts.net`
- Transmission credentials should live in the local `.friday-ops.env`, not as tracked `docker-compose.yml` literals
- `docs/REMOTE_ACCESS.md` and `./scripts/print-remote-access.sh` are the durable sources of truth for current Tailscale URLs and lid-closed assumptions
- Maintainerr scheduled runs are now passing without Plex reachability errors, but current runs have not altered media yet
- `./scripts/install-media-cap-launchd.sh` exists now, and the installed runtime must force `MEDIA_CAP_IO_MODE=docker` so scheduled cleanup does not depend on background access to the repo under `Documents`
- The AWS EC2 host is shared with the production Iris backend in `/Users/kevinshah/Documents/mta-led-sign`; `docs/AWS_MIGRATION.md` is now the primary account-rotation doc, not `QUICK-RENEWAL-GUIDE.md`
- The current AWS migration model is no-domain by design; use `docs/AWS_BLUE_GREEN_RUNBOOK.md`, `./scripts/check-aws-migration-readiness.sh`, and `ops/aws/iam/codex-migration-policy.json` when preparing a new free AWS account
- `./scripts/prepare-migration-day.sh` is the final green-light gate before the user says `migrate`
