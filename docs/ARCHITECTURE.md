# Friday Architecture

## Purpose

This repository is the operational source of truth for a MacBook Pro media stack that runs through Docker Desktop. The goal is reliability, auditability, and predictable storage behavior, not just initial setup.

## Current Topology

- `wireguard` provides the VPN network namespace for download traffic.
- `transmission` runs in `network_mode: service:wireguard`, so download traffic shares the WireGuard network namespace.
- `plex` is the primary media server.
- `jellyfin` is a secondary media server.
- `radarr` handles movie automation.
- `sonarr` handles TV automation.
- `prowlarr` manages indexer sync into the Arr apps.
- `overseerr` handles requests and hands them to Sonarr/Radarr.
- `bazarr` handles subtitles.
- `maintainerr` handles watched-media cleanup policy.
- `unpackerr` extracts completed downloads.
- `portainer` is available for container inspection.

## Storage Layout

- `share/media/movies`: imported movie library.
- `share/media/tv`: imported TV library.
- `share/downloads/complete`: completed downloads before import.
- `share/downloads/incomplete`: active downloads.
- `config/*`: per-service state.

The enforced storage boundary for automation is `share/media`, not the full Docker Desktop disk image and not the full macOS storage view.

## Operational Constraints

- Do not restart Plex during active playback unless the user explicitly accepts the interruption.
- Diagnose from local state first: `docker-compose.yml`, app configs, SQLite databases, and service logs.
- Treat official documentation and primary repositories as the only acceptable default sources when local evidence is not enough.

## Known Reliability Findings

### TV search path failure

- Request path is valid: Overseerr successfully created a Sonarr request.
- Failure occurred because Sonarr had no active TV indexers at search time.
- Root cause in current logs: the single active TV source was disabled upstream in Prowlarr after a Cloudflare block.

Evidence:

- `config/overseerr/logs/overseerr-2026-04-08.log`
- `config/sonarr/logs/sonarr.debug.txt`
- `config/prowlarr/logs/prowlarr.debug.txt`

### Internal Plex addressing drift

- `config/overseerr/settings.json` currently points at a `*.plex.direct` hostname for internal Plex access.
- `config/maintainerr/data/maintainerr.sqlite` also stores Plex as a `*.plex.direct` host with SSL enabled.
- Maintainerr logs show Plex communication failure, which blocks scheduled cleanup runs.

Evidence:

- `config/overseerr/settings.json`
- `config/maintainerr/data/maintainerr.sqlite`
- `config/maintainerr/data/logs/maintainerr.log`

## Operational Targets

- Media cap: `80 GiB` on `share/media`
- Movie retention target: delete watched movies after `15` days
- TV retention target: delete watched episodes after `30` days
- VPN safety: download traffic should be considered healthy only if WireGuard has a recent handshake and Transmission egress matches the expected VPN path

## Source Strategy

This repo documents source roles, priorities, and sync rules, but live source configuration must always be verified from Prowlarr before changes are made. Do not assume the UI state or database state without checking the live application.
