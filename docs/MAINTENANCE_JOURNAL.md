# Maintenance Journal

## 2026-05-15

- Shifted the primary heavy-task browser substrate toward Browser-use instead of continuing to deepen the custom selector-driven Playwright layer.
- Added a Friday-owned local workspace MCP server and registered it into Browser-use so file operations can go through a narrower tool surface than Browser-use's default file actions.
- Added MarkItDown-backed workspace document conversion and wired it into the new workspace MCP/file flow.
- Kept the dedicated worker orchestration, checkpoints, Telegram/Siri routing, and auto-start/auto-stop lifecycle unchanged while replacing the primary browser/file execution path underneath.
- Added direct control-plane commands for long-running work:
  - `show my tasks`
  - `what's the status?`
  - `stop 1`
  - `stop <job id>`
- Fixed the bad light-path DSML/tool-call leak by:
  - classifying reservation/availability/restaurant queries as heavy
  - auto-upgrading light-path internal-tool-markup leaks to heavy instead of returning garbage to Telegram/Siri
- Proved the text-driven stop path end to end against a synthetic heavy task:
  - Siri created a heavy task
  - a text `stop 1` control request was accepted
  - the worker observed the control signal and the job ended `interrupted`
- Confirmed the practical browser limits that still remain after the Browser-use migration:
  - Cloudflare / human-verification pages
  - heavy retail sites that crash or render blank result regions
  - good final synthesized reports paired with ugly intermediate screenshots from blocked pages

### Change: dedicated worker path proved with resumable heavy-task execution

- Goal:
  - Move P1 from “infrastructure deployed” to “dedicated hands path does real work.”
- Change:
  - Switched the live `friday-agent` stack to `dedicated_ec2` worker mode with a `t3a.small` on-demand EC2 worker.
  - Fixed live config persistence so `PUT /config` can store float budget values in DynamoDB without crashing on raw Python `float` serialization.
  - Confirmed live config now enforces:
    - `daily_budget_usd = 0.4`
    - `monthly_budget_usd = 12.0`
  - Fixed the dedicated worker runtime by ensuring `boto3` is present in the worker image and by changing the worker deployment path to prefer a locally built image transfer over rebuilding Playwright on the small EC2 host.
  - Hardened `hands/host/broker.py` so container launch failures can be reported back to the API instead of silently leaving jobs stuck forever.
  - Added local filtering in `agent/app/workspace.py` so hidden runtime/browser-profile files are no longer treated as user-facing deliverables.
- Result:
  - The dedicated worker can now complete real long-running tasks and return real artifacts.
  - A resumed heavy task completed successfully and produced:
    - a browser screenshot
    - a PDF report
  - A controlled long-running workspace task also completed and returned its output files.
  - The hands runtime is now materially closer to a real P1 proof of concept instead of only a deployed shell.
- Remaining caveats:
  - Direct operator IAM still lacks some debugging/control actions for Codex:
    - `ec2:StartInstances`
    - `ec2:StopInstances`
    - `ec2:RebootInstances`
    - `cloudformation:DescribeStackResources`
    - `s3:GetObject`
  - A fresh live validation after the newer Lambda deploy confirmed that heavy tasks now remain `running` after early checkpoints instead of being incorrectly flipped to `checkpointed`.
  - Follow-up live validation on `2026-05-15` closed the substantive interruption/resume gap:
    - stopping the live worker container now transitions the job to `interrupted`
    - `resume that task` now preserves the original heavy-task query on the resumed job record
    - the resumed run completed with carried-forward workspace state
    - proof output from the completed resumed job:
      - `progress/state.txt` contained both `FIRST_RUN` and `RESUMED_OK`
      - `reports/resume-proof.txt` stated that the earlier file already existed before the resumed run continued
  - Remaining observability caveat:
    - Telegram interruption delivery was not independently observable from the current CloudWatch log shape, even though the job-state interruption/resume path is now proven live

### Change: deterministic AWS cost model added and wired into operations docs

- Goal:
  - Make the AWS cost picture durable, deterministic, and easy to maintain as the Friday shared host, Iris backend, and Friday agent evolve.
- Change:
  - Added `docs/AWS_COST_MODEL.md` as the canonical AWS-only cost model.
  - Added `docs/AWS_MIGRATION_HISTORY.md` as the durable changelog of AWS account migrations.
  - Expanded the cost model to include DeepSeek as a separate external model-cost section with deterministic task-based estimates.
  - Fixed the model to a deterministic `720-hour` month and live official AWS pricing inputs.
  - Used the live stack inventory plus current ECR image storage to calculate the current raw monthly AWS cost.
  - Recorded the user-verified current credit balance of `$120.00` and the first recorded AWS migration date of `2026-05-10`.
  - Recorded the current migration-prep reminder date of `2026-10-26` and the current six-month expiry target of `2026-11-10`.
  - Added future scenario tables for the shared-host POC, a dedicated on-demand `t3a.small` worker, and simple horizontal scaling examples.
  - Added explicit notes covering the post-`2025-07-15` AWS six-month Free account plan so the model distinguishes between raw cost and current expected out-of-pocket spend.
  - Linked the new cost model and migration changelog from `docs/OPERATIONS.md` and `docs/AWS_MIGRATION.md`.
  - Added a one-time EventBridge migration reminder to the agent stack so Telegram will notify on `2026-10-26`.
- Result:
  - The repo now has a maintained cost source of truth that can later feed the dashboard work.
  - The current raw AWS architecture is modeled at `$12.566/month`, while the expected out-of-pocket number remains `$0.000/month` with the current `$120.00` credit balance, assuming current charges stay credit-eligible.
- Validation:
  - Verified live stack outputs through AWS CLI for `friday-shared-host` and `friday-agent`.
  - Verified live ECR image storage through AWS CLI and encoded the resulting `8.379806 GiB` in the model.

### Change: long-term roadmap priorities recorded explicitly

- Goal:
  - Preserve the user’s multi-day priority stack so future sessions do not lose track of the intended sequencing.
- Change:
  - Added explicit P1/P2/P3 roadmap sections to `docs/HANDOFF.md`.
  - Recorded that current focus remains P1: agent readiness, dedicated hands, runtime hardening, and E2E validation.
  - Recorded that P2 is the dashboard and secure configuration/memory/token management surface.
  - Recorded that P3 is the future repo/monorepo reorganization across Friday infrastructure, Iris backend, and possible future FBA harness work.
  - Updated `docs/FRIDAY_AGENT.md` so the long-term execution direction reflects the dedicated on-demand worker path rather than treating the shared host as the permanent hands target.
- Result:
  - Future Codex sessions now have a durable roadmap and should not prematurely optimize for repo reorganization or dashboard work while P1 is still in flight.

## 2026-05-14

### Change: shared-host hands substrate implemented in repo

- Goal:
  - Turn the Friday agent from a Lambda-only coordinator into a control plane with a real heavy-task execution substrate while keeping the project inside the single-EC2 budget model.
- Change:
  - Expanded the agent state model in `agent/app/storage.py` to support 48-hour conversation context, durable `#memory`, heavy-job metadata, checkpoints, approvals, and spend.
  - Reworked the agent API in `agent/app/main.py` to classify light vs heavy tasks, preserve 48-hour follow-up context, store `#memory`, expose job/memory/status endpoints, and add internal worker claim/heartbeat/checkpoint/complete/fail endpoints.
  - Refactored `agent/app/agent_core.py` so Lambda light-mode stays coordination-only while heavy-mode can register browser and workspace tools.
  - Added `agent/app/workspace.py` and expanded `agent/app/browser.py` so the heavy worker has isolated browser/session/file/shell tools.
  - Added the hands runtime under `hands/` plus host/runtime install helpers:
    - `hands/host/broker.py`
    - `hands/worker/runner.py`
    - `scripts/deploy-hands-host.sh`
    - `ops/aws/install-hands-runtime.sh`
  - Extended `ops/aws/friday-agent.yaml` with an artifacts S3 bucket, a worker API key parameter, and a DynamoDB GSI used for heavy-job claiming.
  - Updated shared-host bootstrap/restore/backup/migration scripts so the hands runtime is part of the shared-host lifecycle instead of a second EC2 plan.
- Result:
  - The repo now supports a shared-host heavy-task substrate design with private 48-hour context, durable `#memory`, job checkpoints, and a rootless Docker worker path.
  - Live deployment of the shared-host hands runtime was not performed in this session; the repo and migration paths were updated for it.
- Validation:
  - `python3 -m py_compile agent/app/*.py hands/worker/runner.py hands/host/broker.py` passed.
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests` passed with `16 passed`.
  - `bash -n` passed for the updated/new agent and host scripts.
  - YAML parsing passed for `ops/aws/friday-agent.yaml` and `ops/aws/friday-shared-host.yaml`.

### Change: Friday Siri/browser routing stabilized after live queue misclassification

- Goal:
  - Stop simple Siri knowledge questions from needlessly queueing and stop the browser worker from relying on fragile Google result scraping.
- Change:
  - Tightened `agent/app/routing.py` so only clearly live-web or automation-oriented prompts expose the browser tool or pre-queue as long-running work.
  - Updated `agent/app/agent_core.py` so stable general questions do not even register `web_browser_task`, which keeps Siri answers synchronous when browser access is not actually needed.
  - Replaced the original Google-search browser scaffold in `agent/app/browser.py` with a Playwright flow that prefers direct URLs, falls back to DuckDuckGo HTML search, and uses HTTP text extraction when a page blocks normal automation.
  - Restored Lambda browser stability by using a simpler Playwright page creation path and the Lambda-friendly Chromium launch flags.
  - Added CloudWatch-facing browser logs so future debugging shows attempted targets and fallback behavior.
- Result:
  - A live Siri request for `how does one get water in barcelona?` now returns synchronously instead of queueing.
  - A live Siri request that explicitly needed current web content still queues as intended for Telegram follow-up.
- Validation:
  - `python3 -m py_compile agent/app/*.py` passed.
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests` passed with `12 passed`.
  - Redeployed `friday-agent` on `iris` with live image `301142908919.dkr.ecr.us-east-1.amazonaws.com/friday-agent:20260514-013002`.
  - Live Siri checks confirmed synchronous behavior for the Barcelona question and queued behavior for an explicit current-homepage query.

### Change: Friday agent live deployment completed on `iris`

- Goal:
  - Finish the first real AWS deployment of the Friday serverless agent and clear the runtime blockers after CloudFormation reached green.
- Change:
  - Deployed the live `friday-agent` stack in `us-east-1` with ECR, Lambda Function URL, SQS, DynamoDB, SNS, billing alarms, and the CloudWatch dashboard.
  - Registered the Telegram webhook against the Function URL `/telegram`.
  - Corrected `/friday/agent/telegram-chat-id` to the real direct-chat ID observed in Telegram webhook payloads.
  - Fixed a PydanticAI tool-registration bug in `agent/app/agent_core.py` by moving `RunContext` import scope out of `run_agent()` so the nested `web_browser_task` annotation resolves correctly at runtime.
  - Fixed the mixed HTTP/SQS Lambda warm-container bug in `agent/app/main.py` by recreating an event loop before handing non-SQS events to Mangum after `asyncio.run(...)` was used for SQS processing.
  - Expanded `ops/aws/iam/codex-migration-policy.json` with additional live-debug permissions for CloudWatch Logs, Lambda policy reads, Lambda invoke, and EventBridge tag operations.
- Result:
  - `/health` returns `200`.
  - Authenticated `/siri` returns `200` and can answer a short prompt.
  - `/telegram` accepts webhook events and queues jobs successfully.
  - The SQS queue drains back to zero after worker processing.
- Validation:
  - `python3 -m py_compile agent/app/*.py` passed.
  - Local container reproduction of `run_agent()` after the `RunContext` fix returned `pong` successfully.
  - Live AWS checks confirmed `friday-agent` stack `UPDATE_COMPLETE`, Function URL health, Siri success, Telegram webhook registration, and empty SQS queue after processing.

## 2026-05-13

### Change: Friday personal agent stack added and documented

- Goal:
  - Add a portable, near-zero-idle personal agent to the same AWS account-rotation workflow that already preserves WireGuard and Iris.
- Change:
  - Added the agent application under `agent/` with FastAPI routes for Telegram, Siri, health checks, and SQS worker handling.
  - Added PydanticAI/DeepSeek wiring, spend accounting, confirmation gating, Siri long-task routing to Telegram, and a Playwright browser-task scaffold.
  - Set default DeepSeek spend accounting to the higher current `deepseek-reasoner` rates so the budget guard is conservative for mixed model usage.
  - Added `ops/aws/friday-agent.yaml` for the serverless stack: ECR, Lambda Function URL, SQS, DynamoDB, IAM, CloudWatch retention, and optional Budget alert.
  - Added `scripts/deploy-agent.sh`, `scripts/check-agent-migration-readiness.sh`, and `scripts/backup-agent-state.sh`.
  - Updated `scripts/migrate-aws-account.sh` so agent deployment runs by default after shared-host restore and smoke checks; `--skip-agent` is available for emergency host-only migrations.
  - Expanded `ops/aws/iam/codex-migration-policy.json` so new migration accounts can create and update the serverless agent resources.
- Documentation:
  - Added `docs/FRIDAY_AGENT.md` as the durable source of truth for agent architecture, repo map, behavior contract, deployment, migration, validation, budget/privacy, and troubleshooting.
  - Updated `docs/CODEX_MAINTENANCE_LOOP.md`, `docs/OPERATIONS.md`, `docs/AWS_MIGRATION.md`, `docs/HANDOFF.md`, `ops/aws/iam/README.md`, and `SAY_THIS_WHEN_AUTO_COMPACT.md` so fresh sessions know the agent exists and where to start.
- Validation:
  - `python3 -m py_compile agent/app/*.py` passed.
  - `/tmp/friday-agent-venv/bin/python -m pytest -q` passed with `10 passed`.
  - `bash -n scripts/deploy-agent.sh scripts/check-agent-migration-readiness.sh scripts/backup-agent-state.sh scripts/migrate-aws-account.sh` passed.
  - `python3 -m json.tool ops/aws/iam/codex-migration-policy.json >/dev/null` passed.
  - Ruby YAML parsing passed for `ops/aws/friday-shared-host.yaml` and `ops/aws/friday-agent.yaml`.
- Not validated:
  - Real `aws cloudformation validate-template`, Docker image build, and deployment were not run because `aws` and `docker` were not available on `PATH` in the working environment.

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

### Change: proxy and byparr health plumbing repaired

- Symptom:
  - `byparr` showed as `unhealthy` in Docker even though the service process was running.
  - `vpn-web-proxy` returned local `504` responses when slow upstream Prowlarr requests exceeded the default proxy timeout.
- Evidence:
  - `docker inspect byparr` showed the image healthcheck was probing `http://localhost:8191/health` while this stack moved `byparr` to port `8192`.
  - `docker logs vpn-web-proxy` showed `upstream timed out` on proxied Prowlarr requests.
- Fix:
  - Added an explicit `byparr` healthcheck on `127.0.0.1:8192`.
  - Added an explicit `vpn-web-proxy` healthcheck on `127.0.0.1:9696`.
  - Raised `proxy_connect_timeout`, `proxy_send_timeout`, and `proxy_read_timeout` in `ops/vpn-web-proxy/default.conf`.
  - Recreated only `byparr` and `vpn-web-proxy`.
- Result:
  - `byparr` is now healthy.
  - `vpn-web-proxy` is now healthy.
  - This fixes generic container health and proxy tolerance, but it does not guarantee any specific upstream source is available.

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

### Change: May 10 migration hardening pass

- Change:
  - Patched `ops/aws/restore-host-from-backup.sh` so restore now enables the `iris-backend` nginx site and removes the default nginx site.
  - Added `scripts/post-migration-smoke.sh` to validate remote Iris, remote shared-host services, local Friday VPN health, and local stack health in one command.
  - Updated `scripts/migrate-aws-account.sh` to rerun `prepare-migration-day.sh` internally, support explicit `--offline-restore`, fall back to the latest saved backup if a fresh backup fails because the old host is already gone, and run the new post-migration smoke checks automatically.
  - Updated `scripts/prepare-migration-day.sh` so it can auto-switch to offline-restore mode when the current Iris host is unreachable but `backup/aws/latest` is still valid.
  - Updated `scripts/backup-aws-host.sh` so it discovers the current AWS host from the tracked WireGuard endpoint and then derives the instance ID and security group from AWS, instead of relying on stale old-account defaults.
  - Updated `scripts/update-mta-led-sign-backend-url.sh` so it also rewrites bare host references like the ones in `scripts/deploy_backend_ec2.sh`, not just full `http://...` URLs.
- Validation:
  - `./scripts/post-migration-smoke.sh 13.216.214.108` passed.
  - `./scripts/check-vpn.sh` passed with Transmission egress `13.216.214.108`.
  - `./scripts/check-stack.sh` passed.
  - Public `http://13.216.214.108/api/iris/preferences` and `/api/iris/state` both returned live data after the nginx site fix.
- Result:
  - The next AWS cutover should require fewer manual decisions:
    - no manual offline/online mode choice in the common failure case,
    - no stale EC2 metadata defaults in the backup path,
    - and one built-in smoke verdict after restore instead of multiple ad hoc manual checks.

### Change: AWS backup rule hardened after the `iris` cutover

- Evidence:
  - `AWS_PROFILE=iris EC2_SSH_KEY=/Users/kevinshah/.aws/keys/iris-migration-20260510.pem ./scripts/backup-aws-host.sh` succeeded against the new host and wrote `backup/aws/20260510-192341/`.
  - `backup/aws/latest/metadata/discovered.env` now records:
    - `EC2_HOST=13.216.214.108`
    - `EC2_INSTANCE_ID=i-0263b221709dce545`
    - `EC2_SECURITY_GROUP_ID=sg-0541241698170a453`
- Change:
  - Added `scripts/aws-infra-change.sh` as the default wrapper for Codex-managed AWS CLI changes.
  - The wrapper now forces:
    - one pre-change shared-host backup
    - execution of the requested AWS change command
    - one post-change shared-host backup
    - a timestamped change log under `backup/aws/change-log/`
  - Updated the maintenance loop, auto-compact prompt, AWS migration docs, and handoff to make this a hard rule for future Codex sessions.
- Result:
  - The new `iris` account already has a recoverable shared-host backup.
  - Future Codex-managed AWS infrastructure changes now have an explicit automation path that captures before/after restore points instead of relying on memory or manual discipline.

### Change: 48-hour thread memory upgraded from summary-only to turns + summary

- Evidence:
  - The prior 48-hour memory layer behaved too much like isolated requests because it only kept one coarse summary blob per conversation.
  - Follow-up UX did not feel like a long-lived chat thread, especially when the user returned with a related but not identical request.
- Change:
  - Added raw thread-turn storage for each conversation with 48-hour TTL.
  - Added persisted agent config for context-window behavior (`context_max_turns`, `context_summary_max_chars`, `conversation_ttl_hours`, `system_prompt_suffix`, optional budget overrides).
  - Updated prompt assembly so the agent now receives:
    - durable `#memory`
    - rolling thread summary
    - recent raw turns
    - current request
  - Added authenticated backend endpoints for later dashboard control:
    - `GET /config`
    - `PUT /config`
    - `GET /threads/{conversation_id}`
- Validation:
  - `python3 -m py_compile agent/app/*.py hands/worker/runner.py hands/host/broker.py`
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests` → `18 passed`
- Result:
  - The memory layer is closer to “one long thread for 48 hours” instead of a summary-only approximation.
  - Dashboard work can later expose these config and inspection surfaces without backend redesign.

### Change: VPN guard false-positive spam reduced

- Evidence:
  - Telegram alerts were flapping between unhealthy and healthy even when downloads were not active.
  - Current unhealthy messages showed `Transmission action: already-stopped`.
  - `./scripts/check-vpn.sh` had been validating public egress from the `transmission` container namespace, which fails trivially when `transmission` is stopped even if the WireGuard tunnel itself is healthy.
  - Live validation showed the `wireguard` namespace could still reach `https://checkip.amazonaws.com` and reported the correct egress IP `13.216.214.108`.
- Change:
  - Updated `scripts/check-vpn.sh` so egress validation uses the `wireguard` namespace directly.
  - Updated `scripts/vpn-guard.sh` to:
    - add `VPN_GUARD_NOTIFY_ALREADY_STOPPED` with default `0`
    - add `VPN_GUARD_FAILURE_STREAK_THRESHOLD` with default `3`
    - persist `NOTIFIED` and `FAILURE_STREAK` in guard state
    - send recovery notifications only when an unhealthy notification was actually sent
  - Updated `scripts/install-vpn-guard-launchd.sh` and reinstalled the support runtime so the installed LaunchAgent uses the new logic and the cleaned state path.
- Validation:
  - `./scripts/check-vpn.sh` passed with a fresh handshake and egress IP `13.216.214.108`.
  - The installed guard runtime reported `STATUS=healthy`, `NOTIFIED=0`, `FAILURE_STREAK=0`.
  - `launchctl print gui/$(id -u)/com.friday.vpn-guard` showed the agent loaded with `last exit code = 0`.
- Result:
  - The guard still fail-closes downloads when the VPN path is genuinely unsafe.
  - Idle tunnel flaps while `transmission` is already stopped should no longer spam Telegram by default.

### Change: Amphetamine lid-closed readiness check added

- Evidence:
  - Amphetamine was installed and running, but there was no active Amphetamine session.
  - Amphetamine preferences already showed:
    - `Allow Closed-Display Sleep = 0`
    - `Allow Display Sleep = 0`
  - AppleScript checks still returned:
    - `session is active = false`
    - `display sleep allowed = false`
    - `closed display mode enabled = true`
  - `pmset -g assertions` showed no Amphetamine-owned sleep-prevention assertion, which means saved preferences alone were not enough to keep the Mac awake with the lid closed.
- Change:
  - Added `scripts/check-amphetamine.sh` to report whether Amphetamine is running, whether a session is active, and whether the current session is suitable for lid-closed server use.
  - Updated `docs/REMOTE_ACCESS.md` so the remote-access checklist explicitly says a timed Amphetamine session must actually be active before the lid is closed.
- Result:
  - There is now a concrete local command to answer “is this Mac safe to close right now?” instead of relying on menu-bar guesswork.

### Change: Heavy-task research hardened enough for real camera-search E2E

- Evidence:
  - The first real camera-research task on the dedicated worker failed on a single blocked source (`403` from DPReview).
  - After deterministic search/fetch was added, the next failure mode was a single brittle browser action (`Locator.click` on `#article-body`) aborting the whole task.
  - A later live run proved the PDF was actually generated, but worker output handling needed to avoid treating runtime junk as meaningful output.
- Change:
  - Added deterministic research wrappers in `agent/app/research.py` with Brave Search support when configured and public fallback otherwise.
  - Updated heavy-task prompt guidance and tool wrappers so:
    - blocked deterministic fetches become warnings, not task-ending exceptions
    - failed browser actions become `BROWSER_ACTION_BLOCKED...` warnings instead of hard failures
    - the agent is instructed to continue with alternate sources instead of retrying one dead path forever
  - Kept the dedicated worker security posture:
    - read-only container filesystem
    - tmpfs for `/tmp`
    - dropped Linux capabilities
    - no-new-privileges
    - CPU / memory / pids limits
- Live validation:
  - Stopped-worker lifecycle proved again: `stopped -> auto-start -> claim -> complete`.
  - Real camera-search job `c6f5cb9f-ef4b-4c6c-a9ea-a0af1eaaf7da` completed successfully.
  - Output artifact:
    - `reports/vlogging-travel-snowboarding-cameras.pdf`
  - The uploaded PDF extracted cleanly and contained a structured multi-tier recommendation report covering budget, mid, and premium camera options with review/spec context.
- Result:
  - Friday now has a working end-to-end heavy research flow that:
    - starts the dedicated worker automatically
    - performs deterministic research first
    - escalates to browser only as needed
    - creates a user-facing PDF artifact
    - uploads the artifact back through the control plane
  - The browser layer is substantially more resilient than before, but it is still a custom hardened Playwright layer, not yet a full Browser-use migration.
