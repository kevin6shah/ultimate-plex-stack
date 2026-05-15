# Friday Handoff

## Current Goal

Keep this repository as a maintained operations project for a local MacBook Pro media stack with:

- reliable request flow
- VPN-first download safety
- hard media-cap enforcement
- durable documentation and incident history
- a personal agent that becomes the primary long-term automation/control plane for this shared AWS footprint

## Current Priorities

1. Keep the rebuilt TV automation path healthy with the current working source set.
2. Keep the media library inside the `80 GiB` cap.
3. Maintain repo-local operations docs after every change.
4. Expand movie backup coverage beyond the currently working public sources if needed.
5. Keep the AWS shared-host migration workflow documented and usable before the current free-tier window ends.

## Product Roadmap Priorities

### P1: Agent Readiness

This is the current highest-priority work and should stay the focus until validated live:

1. Finish the dedicated on-demand hands worker path.
2. Make browser/file execution reliable and fast enough for real use.
3. Direct interruption/resume is now proven on the dedicated worker path. An operator-stopped worker container transitions the job to `interrupted`, and `resume that task` now preserves the original heavy-task query and completes with carried-forward workspace state. The remaining observability gap is that Telegram interruption delivery was not independently inspectable from current CloudWatch logging, even though the control-plane/job-state path is behaving correctly.
4. Run Codex-driven E2E tests before the user’s first real heavy-task test.
5. Continue runtime optimizations as real limitations are discovered.

### P2: Dashboard

Do not prioritize this ahead of P1, but preserve dashboard-readiness in backend design:

1. Mobile-friendly dashboard for jobs, worker state, spend, and logs.
2. View/edit persistent memory.
3. View/edit context window settings.
4. View/edit persona/system prompt settings.
5. View live budget/runway state from `docs/AWS_COST_MODEL.md` logic.
6. Securely manage session tokens/cookies without exposing them to the model.
7. Show live long-task status and allow stop/nudge controls.
8. Later possible live browser view is a P3-grade extension, not a P2 blocker.

### P3: Repo / Platform Organization

This is intentionally deferred until the agent is working well and the dashboard exists:

1. Re-evaluate repository boundaries across:
   - `friday-plex-stack`
   - Iris backend
   - future FBA harness work
2. Strong candidate direction: one monorepo for systems that share the same AWS infrastructure and are expected to be operated from the same VS Code / Codex context.
3. Preserve the ability to work on all AWS-shared systems from one local workspace and from Codex mobile.
4. Decide whether the future monorepo should absorb:
   - Friday shared infrastructure
   - Friday agent
   - Iris backend
   - future FBA harness components that run on the same AWS footprint
5. Do not do this reorg until P1 is validated and P2 is materially underway.

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
- The Friday personal agent now lives under `agent/` and is deployed by `scripts/deploy-agent.sh`; account migration includes it by default after the shared host is restored, with `--skip-agent` available for emergency host-only rotations.
- The medium-term direction is now dedicated on-demand hands infrastructure for heavy tasks, with the shared host retained for VPN + Iris and as the current bridge state during the transition.
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
5. After P1/P2, decide whether to consolidate the shared AWS systems into a monorepo and define the migration path for Git history, issues, and CI.

## Resume Checklist

When returning to this repo in a future session:

1. Read `SAY_THIS_WHEN_AUTO_COMPACT.md`
2. Read `docs/CODEX_MAINTENANCE_LOOP.md`
3. Read `docs/MAINTENANCE_JOURNAL.md`
4. Run `./scripts/check-stack.sh`
5. Read the logs before making claims
