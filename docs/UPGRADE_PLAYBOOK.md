# Friday Upgrade Playbook

## Rule

Do not blind-update the stack. Upgrade one non-Plex service at a time, with backups and verification between each step.

## Current Upgrade Candidates

- Prowlarr
- Sonarr
- Radarr
- Overseerr
- Maintainerr
- Transmission and WireGuard only after compatibility review

Plex is explicitly excluded from active playback windows.

## Required Process

1. Inspect current running version from logs or the live app.
2. Read the official release notes or primary repository before changing anything.
3. Run `./scripts/backup-services.sh`.
4. Pull or edit only the target service.
5. Restart only the target service and its direct dependencies if required.
6. Run `./scripts/check-stack.sh`.
7. Inspect logs for regressions.
8. Record the change in `docs/MAINTENANCE_JOURNAL.md`.

## Verification Template

For each upgrade, confirm:

- container starts cleanly
- health endpoint responds
- app can still reach its direct dependencies
- request and import paths still work
- no new warning pattern appears in logs

## Research Rule

If the release notes are unclear, write down the uncertainty and stop. Do not infer compatibility.
