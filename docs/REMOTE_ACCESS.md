# Friday Remote Access

This stack is intended to be reached privately over Tailscale, not exposed on the public internet.

## Current Access Paths

Preferred hostname:

- `friday-media.tail87437e.ts.net`

Live services:

- Overseerr: `http://friday-media.tail87437e.ts.net:5055`
- Radarr: `http://friday-media.tail87437e.ts.net:7878`
- Sonarr: `http://friday-media.tail87437e.ts.net:8989`
- Bazarr: `http://friday-media.tail87437e.ts.net:6767`
- Transmission: `http://friday-media.tail87437e.ts.net:9091/transmission/web/`
- Maintainerr: `http://friday-media.tail87437e.ts.net:6246`
- SSH: `ssh <macOS-user>@friday-media.tail87437e.ts.net`

Current raw Tailscale IP:

- `100.77.97.13`

Use the hostname unless DNS troubleshooting forces you to fall back to the raw Tailscale IP.

## Authentication Expectations

- Overseerr, Radarr, Sonarr, Bazarr, and Maintainerr use their normal app logins.
- Transmission requires its Web UI credentials.
- The current Transmission username is stored only in local `.friday-ops.env`.
- Do not move Transmission secrets back into tracked files.

To print the current remote endpoints from the local host:

```bash
./scripts/print-remote-access.sh
```

## Sleep and Lid Assumptions

Remote access only works while the MacBook stays awake.

For reliable lid-closed operation:

- Amphetamine must be running
- Amphetamine Enhancer must be installed
- the active Amphetamine session must have `Allow system sleep when display is closed` unchecked
- the MacBook should stay on AC power

Practical guidance:

- safest: keep the lid open and let only the display sleep
- if you want lid-closed use, restart the Amphetamine session after changing the closed-display setting, then test reachability from another Tailscale device

## Transmission and VPN Behavior

- Transmission is reachable over Tailscale, but its torrent traffic still runs behind WireGuard.
- The VPN guard watches WireGuard health every 60 seconds.
- If the VPN becomes unhealthy, the guard stops Transmission and sends a Telegram alert.
- That means remote access to the Transmission UI can still work while active torrenting is intentionally blocked by the guard.

## Validation Checklist

From another Tailscale device:

1. Open Overseerr and submit a request.
2. Open Sonarr or Radarr and verify the item appears.
3. Open Transmission and confirm the torrent enters the queue.
4. If testing lid-closed mode, close the lid briefly and re-check one of the URLs above from your phone.
