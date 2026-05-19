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

1. Keep the dedicated on-demand hands worker path as the production heavy-task path.
2. Keep the new Browser-use + deterministic-first + Friday-owned workspace MCP stack as the primary hands substrate.
3. Direct interruption/resume is now proven on the dedicated worker path. Text-driven task listing, status lookup, and stop commands are also live:
   - `show my tasks`
   - `what's the status?`
   - `stop 1`
   - `stop <job id>`
4. Durable pause-for-input is now live and must be preserved as the base HITL substrate for the next stages.
5. The next substantive P1 upgrade is an explicit hybrid tool-routing layer for common-use tasks, not deeper browser-first mechanics.
6. Continue runtime optimizations as real limitations are discovered, with special focus on browser anti-bot and challenge-heavy sites only where deterministic tools and connectors are insufficient.
7. Treat MCP/connectors as a layered isolated tool estate, not a flat set of equally trusted tools.

### P1 Remaining Work

1. Finish operator validation of the now-live `paused_for_input` / human-in-the-loop resume UX before treating it as accepted production behavior.
2. Build the explicit common-use routing hierarchy so Friday does not browse for work that should be deterministic:
   - spreadsheets / data outputs -> filesystem MCP + local file/spreadsheet tools + MarkItDown where needed
   - maps / itinerary / general fact gathering -> deterministic APIs, connectors, or deterministic search/fetch first
   - booking / reservation / commerce tasks -> vetted connector if one is actually installed and approved, otherwise deterministic fetch first and Browser-use only for the interaction step
3. Deploy and validate the current selected MCP/app comparison set for common use cases:
   - Firecrawl MCP
   - Skiplagged MCP for flights/hotels/rental cars
   - cablate Google Maps MCP
   - Google Maps / Places / Routes via OpenAPI MCP
   - dedicated Friday mailbox via Gmail/email MCP
   - later expand to other commerce/travel/common-use connectors after the same review/test loop
4. Build tiered MCP isolation harnesses so:
   - browser runtime
   - filesystem/file-helper runtime
   - read-only network MCPs
   - sensitive booking/identity MCPs
   do not all share the same blast radius
5. Improve browser reliability on hostile domains:
   - Cloudflare / challenge pages
   - heavy retail SPAs
   - blank result bodies after JS load
   - screenshot quality / step artifact selection
6. Decide whether to enable Browser Use Cloud, CAPTCHA support, or both for difficult sites, but only after exhausting deterministic/API routes first.
7. Move higher-risk MCP/tool classes toward per-tool container isolation instead of only the shared worker container boundary.
8. Add a safe account identity / sign-up gating flow for booking and commerce tasks:
   - pause at sign-in/sign-up gates
   - let the operator choose cached identity vs new email
   - keep password entry in a secure dashboard/operator surface, not the model prompt path
   - require explicit approval before account creation submits
9. Add login-wall pause/resume handling so account-gated sites stop at the correct decision point instead of failing or improvising.
10. Replace the current OAuth-style Gmail MCP path with a headless IMAP/SMTP Gmail MCP using the dedicated Friday mailbox plus Gmail App Password authentication, then validate inbox/OTP reads live.
11. Treat `Temporal + Stagehand + keep current Friday interfaces` as the current preferred v2 direction:
   - keep Telegram and Siri as the interfaces
   - keep Friday's current stop/pause/approval/status product semantics
   - add durable execution for retries, waiting, reminders, and HITL pauses
   - add a stronger browser interaction substrate for dynamic/hostile sites
   - borrow resilience patterns from OpenClaw/Nanoclaw-style runtimes without replacing Friday with a monolithic always-on agent platform
12. Close the manual-verification gaps in `docs/FRIDAY_MANUAL_VERIFICATION_REPORT_2026-05-18.md` before claiming Friday is stable enough for operator handoff:
   - false approval prompts on harmless research/planning
   - natural stop/cancel/reveal-findings UX
   - long-running tasks that appear hung and then fail vaguely
   - wrong restaurant identity resolution
   - overconfident or weakly grounded research/architecture answers
   - remaining internal/meta user-facing wording

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

### P3: Reminders

After dashboard work is underway, Friday should grow a real reminder system:

1. Friday should manage reminders about its own setup, expiring sessions, and operator follow-ups.
2. Later, Friday should support user-facing reminders such as `remind me tomorrow about this`.
3. Keep this behind P1/P2 work; do not let reminders preempt the current agent-readiness path.

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
- Browser-use is now the primary browser/computer-use loop, but it is still not enough by itself for some challenge-heavy sites. Current observed failure classes:
  - Cloudflare verification / anti-bot pages
  - retail site “page crashed” or blank result regions
  - successful final reports paired with ugly intermediate screenshots from blocked pages
- For common-use tasks, Friday should follow this routing order:
  - real connector or deterministic API when one exists and is vetted
  - workspace MCP / MarkItDown / local structured file tools for files and spreadsheets
  - deterministic search/fetch and page conversion for general web reading
  - Browser-use only for interaction, login, form fill, confirmation, or unsupported flows
- The current restaurant-reservation path does not yet have a vetted installed connector. Friday must use deterministic search/fetch first and browser fallback only when necessary.
- The current active travel connector direction is Skiplagged for flights/hotels/rental cars; the old Google Flights browser path is not good enough.
- Do not rely on the current LobeHub listing for `musemen-resy-mcp-server` as an install source. The listing points to a GitHub repo that currently 404s, so it is metadata only until a real source artifact appears.
- Do not assume that named third-party MCP servers from external recommendations are production-worthy by default. Treat them as candidates that still need security review, maintenance review, and real-task validation in this stack.
- The MCP/connector program should add multiple candidates where necessary, but they should be separated by trust tier and secret scope instead of all running as one flat tool surface.
- Local Docker is not reliably available on this Mac session, so remote Docker builds on the dedicated worker have been used as the practical deployment path for recent Lambda image pushes.

## Current Working Sources

- Radarr: `YTS`, `1337x`, `Demonoid Clone`, `Nyaa.si`
- Sonarr: `1337x`, `Demonoid Clone`, `EZTV`, `Nyaa.si`, `showRSS`
- Disabled upstream: `The Pirate Bay`
- Still failing even with helpers: `TorrentGalaxyClone`, `The Pirate Bay`

## Immediate Next Steps

1. Keep the Friday control-plane + dedicated-worker deployment path healthy.
2. Work directly from `docs/FRIDAY_MANUAL_VERIFICATION_REPORT_2026-05-18.md` until the current manual-verification failures are closed.
3. Finish live validation of the new screenshot-delivery defaults and zip behavior on a completed browser-heavy task.
4. Implement the explicit hybrid tool-routing layer for common-use tasks so Friday stays deterministic-first.
5. Deploy and validate the new official filesystem MCP worker path on real file/spreadsheet tasks.
6. Build the first tiered MCP isolation harnesses for filesystem, helper, and read-only network MCPs.
7. Deploy and validate the now-wired travel/maps candidates, with Skiplagged first, before narrowing any primary path.
8. Improve hostile-site browser behavior instead of assuming Browser-use solved anti-bot completely.
9. Only after the agent is stable enough, move to dashboard/P2 work.

## Backlog

1. Evaluate the best remote playback path over Tailscale for Plex and Jellyfin when the laptop stays awake under Amphetamine.
2. Turn the shared AWS migration into a true single-command cutover that also updates the live Iris board/backend URL, not just the Friday repo and local mta-led-sign sources.
3. Add a redacted shared-host inventory export so fresh agents can inspect the topology without touching secrets.
4. Turn the Friday local stack into a preflighted, mostly one-command bootstrap with env-driven settings instead of hardcoded host-specific values in tracked files.
5. After P1/P2, decide whether to consolidate the shared AWS systems into a monorepo and define the migration path for Git history, issues, and CI.
6. Add reminder primitives only after dashboard/P2 surfaces exist for visibility and editability.

## Resume Checklist

When returning to this repo in a future session:

1. Read `SAY_THIS_WHEN_AUTO_COMPACT.md`
2. Read `docs/FRIDAY_OPERATOR_BOARD.md`
3. Read `docs/FRIDAY_CAPABILITIES_MATRIX.md`
4. Read `docs/FRIDAY_MCP_STACK_PLAN.md`
5. Read `docs/CODEX_MAINTENANCE_LOOP.md`
6. Read `docs/MAINTENANCE_JOURNAL.md`
7. Read `docs/FRIDAY_AGENT.md`
8. Read `docs/FRIDAY_AWS_SYSTEM_DESIGN.md`
9. Read `docs/FRIDAY_TOOL_SECURITY.md`
10. Read `docs/NEXT_CODEX_PROMPT.md`
11. Run `./scripts/check-stack.sh`
12. Read the logs before making claims
