# Maintenance Journal

## 2026-05-29

- Tightened restaurant booking preflight reuse so heavy runs can carry a concrete matched venue id/url forward instead of re-running a second structured venue search:
  - added `RestaurantBookingPreflightResult` / `RestaurantBookingPreflightVenue`
  - stored booking preflight state on `AgentDependencies`
  - added `_restaurant_find_availability_attempts(...)` to reuse preflight venue matches when the later tool call is still about the same restaurant
  - tolerated invented follow-up city values when the original user request did not specify a city
- Extended regression coverage around the reuse path:
  - preflight match reuse without a second search
  - tolerance for bogus later cities when the original request had no city
  - fallback back to real search when there is an actual city conflict
- Reran the broader focused slice:
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests/test_agent_core_browser_fallback.py agent/tests/test_agent_core_phase1.py agent/tests/test_status_requests.py agent/tests/test_phase1_control_plane.py`
  - `131 passed`
- Deployed the newer restaurant-reliability images live:
  - Lambda image `301142908919.dkr.ecr.us-east-1.amazonaws.com/friday-agent:20260529-preflight-reuse-v2`
  - dedicated worker rebuilt/restarted on `3.87.35.135`
  - confirmed Lambda still configured for `HANDS_WORKER_MODE=dedicated_ec2`
- Live validation on the Angel Resy booking case narrowed the remaining problem:
  - `api_direct` now goes from preflight directly to `restaurant_availability` with `venue_id=91940`, proving the duplicate structured venue search was removed in that leg
  - `stagehand_stealth_act` still sometimes starts with `restaurant_search` after the retry preflight, so the remaining gap is now specifically fallback-tool selection / prompting rather than preflight venue reuse itself
  - stopped the validation run after capturing that signal and cleaned live state back to:
    - `/worker/health` -> `{"active_jobs":[]}`
    - `/context/recent` -> `{"contexts":[]}`
- Continued from the earlier control-plane cleanup and widened the deterministic restaurant parser so booking/discovery inputs are less brittle across initial requests and resumed follow-ups:
  - latest `New user input:` / `New user direction:` fields now take precedence over stale earlier text
  - broader party-size parsing for digits and number words
  - broader date parsing for labeled fields, weekday+month-name dates, slash dates, and ordinal month-day inputs
  - broader time parsing for labeled fields, bare times, and flexible phrases like `any available time` / `earliest available`
  - discovery prefill now accepts the flexible-time sentinel instead of dropping out of structured preflight
- Persisted resumed/follow-up query text across Temporal retries so strategy switches keep the newest structured user input instead of falling back to the original request:
  - `agent/app/heavy_job_runtime.py`
  - `agent/app/temporal_control_activities.py`
- Extended regression coverage and reran the focused local slice:
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests/test_agent_core_phase1.py agent/tests/test_status_requests.py agent/tests/test_phase1_control_plane.py`
  - `121 passed`
- Rebuilt and redeployed both control plane and dedicated worker on the intended topology:
  - Lambda image:
    - `301142908919.dkr.ecr.us-east-1.amazonaws.com/friday-agent:20260529-booking-input-parser`
  - Lambda config after deploy:
    - `HANDS_WORKER_MODE=dedicated_ec2`
    - `HANDS_WORKER_INSTANCE_ID=i-04cf5a5edd3b4aa35`
    - `FRIDAY_EXECUTION_BACKEND=temporal`
  - dedicated worker redeployed on `3.87.35.135`
  - verified `friday-temporal-activity-worker.service` is active after the worker rebuild
- Live booking-parser validation on the previously failing Resy flow:
  - query:
    - `Book the earliest available reservation tonight for 3 people at Angel Indian Restaurant on Resy, but only if it has free cancellation.`
  - queued job id:
    - `1c019428-e603-408a-b70b-278893ad2fc2`
  - observed behavior:
    - no `paused_for_input` for missing `time`
    - `api_direct` hit the known Resy `500`
    - Temporal retry switched to `stagehand_stealth_act`
    - dedicated-worker logs confirmed the retry still carried `time=ANY AVAILABLE`, so the structured input was preserved across the strategy handoff
  - stopped the run after capturing the regression signal and cleaned live state back to:
    - `/worker/health` -> `{"active_jobs":[]}`
    - `/context/recent` -> `{"contexts":[]}`
- Live flexible-time discovery validation:
  - query:
    - `Find me Indian restaurants on Resy near Midtown NYC for tomorrow any available time for two people.`
  - queued job id:
    - `7ff5e280-f9a8-45a9-8905-fc36f5d27148`
  - final result:
    - completed in `api_direct`
    - result preview started with:
      - `I found a few likely Resy options near Midtown Nyc for 2 for any available time on 2026-05-30.`
      - `INDIAN TABLE (New York) [venue 88720]`
  - cleaned Siri + Telegram thread state afterward and confirmed:
    - `/worker/health` -> `{"active_jobs":[]}`
    - `/context/recent` -> `{"contexts":[]}`
- Continued the restaurant-reliability branch from the mobile handoff and verified the focused local regression slice with:
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests/test_phase1_control_plane.py agent/tests/test_agent_core_phase1.py`
  - `62 passed`
- Found a concrete control-plane cleanup mismatch in live state:
  - `/worker/health` returned `{"active_jobs":[]}`
  - `/context/recent` still showed a Siri context row with a stale `active_heavy_job_id`
- Fixed that mismatch locally by reconciling thread ownership during `/context/recent` reads:
  - added `_parse_context_pk(...)`
  - added `_recent_contexts_with_synced_active_jobs(...)`
  - updated the `/context/recent` route to clear stale thread job pointers before returning rows
- Added a regression test for that endpoint behavior so recent-context reads now clear completed-job pointers instead of surfacing misleading active ownership.
- While cleaning live thread state, found that the documented Telegram cleanup shortcut used `telegram-owner` while the stored conversation id was actually the allowed Telegram chat id.
- Fixed that admin-route mismatch locally:
  - added `_thread_owner(...)`
  - added `_normalized_thread_conversation_id(...)`
  - normalized `telegram-owner` to the real allowed chat id in both `GET /threads/{conversation_id}` and `DELETE /threads/{conversation_id}`
  - added a regression test for the alias path
- Manually cleaned the live Siri and Telegram validation context after the checks:
  - `DELETE /threads/siri?channel=siri&user_id=siri`
  - `DELETE /threads/<telegram chat id>?channel=telegram`
  - follow-up `/context/recent` returned `{"contexts":[]}`
- Full `agent/tests` is still not a clean signal in this local venv because collection hits a pre-existing Temporal import problem:
  - `ModuleNotFoundError: No module named 'temporalio.exceptions'; 'temporalio' is not a package`
  - this appeared in `agent/tests/test_temporal_control_activities.py`
  - the focused control-plane and restaurant slices passed, so the new branch work is locally verified where it changed behavior
- Deployed the control-plane cleanup fixes live with:
  - `AWS_PROFILE=iris AWS_REGION=us-east-1 HANDS_WORKER_MODE=shared_host SHARED_HOST_INSTANCE_ID=i-0263b221709dce545 TEMPORAL_ENABLED=true TEMPORAL_HOST=ec2-3-80-179-123.compute-1.amazonaws.com:7233 FRIDAY_EXECUTION_BACKEND=temporal EC2_SSH_KEY=/Users/kevinshah/.aws/keys/iris-migration-20260510.pem REMOTE_BUILD_HOST=3.87.35.135 IMAGE_TAG=20260529-control-plane-cleanup ./scripts/deploy-agent.sh`
  - local Docker was still unavailable, so the deploy used the remote worker build fallback again
  - image pushed and deployed:
    - `301142908919.dkr.ecr.us-east-1.amazonaws.com/friday-agent:20260529-control-plane-cleanup`
- Post-deploy live validation:
  - Lambda config updated at `2026-05-29T05:54:55+0000`
  - `GET /worker/health` returned `{"active_jobs":[]}`
  - `GET /context/recent` returned `{"contexts":[]}` before validation
  - repeated the fast Siri restaurant-discovery validation:
    - query: `Find me an Indian restaurant on Resy near Midtown NYC for tonight at 8 PM for 3 people, preferably with free cancellation.`
    - queued job id: `c2ac26d5-5d0d-42b1-a31b-c3b5ae522d14`
    - final DynamoDB job status: `completed`
    - final preview again listed:
      - `Angel Indian Restaurant (New York) [venue 91940]`
      - `Muna Indian Restaurant (New York) [venue 95147]`
  - verified the newly deployed cleanup behavior on live state:
    - `/context/recent` showed both Siri and Telegram rows with `active_heavy_job_id: ""` after the completed run
    - `DELETE /threads/telegram-owner?channel=telegram` removed the mirrored Telegram row after a short propagation delay
    - `DELETE /threads/siri?channel=siri&user_id=siri` removed the Siri row
    - final `/context/recent` returned `{"contexts":[]}`
- Important live-state note:
  - the deployed `friday-agent` Lambda configuration still has `HANDS_WORKER_MODE=shared_host`
  - that does not match the current docs/handoff narrative that says heavy work is on the dedicated worker by default
  - do not silently flip that topology in a routine code deploy; treat it as a separate architecture/runtime reconciliation item

## 2026-05-18

- Started the first real Stagehand migration in repo instead of leaving it as a pure architecture note:
  - added `stagehand==3.20.0` to the Lambda and dedicated-worker dependency sets
  - added new Stagehand runtime settings in `agent/app/settings.py`
  - added `agent/app/stagehand_runner.py` as a guarded local Stagehand browser lane
  - kept MCP/API/public-web reads first, inserted Stagehand before Browser-use, and left Browser-use as the last browser fallback
  - added focused fallback-order tests plus Stagehand settings/tests
- Local validation for the Stagehand migration passed:
  - `python3 -m py_compile` on the patched browser files
  - focused pytest slice: `24 passed`
  - broader routing/Telegram/Siri/browser regression slice: `45 passed`
- Pulled the exact operator Telegram thread and converted the observed issues into a dedicated repo report:
  - `docs/FRIDAY_MANUAL_VERIFICATION_REPORT_2026-05-18.md`
- Captured the main operator-visible failures that still block a “stable” handoff:
  - false spend/send confirmation prompts on harmless research/planning queries
  - natural-language stop/cancel/reveal-findings UX still unreliable
  - long-running tasks that appear to keep working without a satisfying finish
  - confident restaurant identity mistakes before reservation search
  - overconfident / weakly grounded answers on some research and architecture questions
  - remaining internal/meta wording leaks in user-facing responses
- Updated:
  - `docs/FRIDAY_OPERATOR_BOARD.md`
  - `docs/HANDOFF.md`
  so future sessions treat those manual-verification findings as current P0/P1 work instead of drifting back into lower-level tool work alone.

## 2026-05-17

- Restored the intended Friday heavy-task topology after the shared-host detour:
  - cleaned up the broken `friday-hands-worker` stack state after the IAM policy fix
  - recreated the proper dedicated on-demand Friday worker
  - confirmed the worker now runs as a separate `t3a.small` with `40 GiB` root disk instead of leaning on the shared Iris/VPN host
- Added `docs/FRIDAY_AWS_SYSTEM_DESIGN.md` and wired it into the normal session read path so future AWS decisions stay aligned with the intended system design.
- Updated `docs/AWS_COST_MODEL.md` to reflect the actual dedicated-worker architecture and the current raw monthly scenarios.
- Fixed the worker image/runtime for the newer MCP stack:
  - moved the worker to Node 22 so `mcp-remote` / Skiplagged no longer fail under the older Node 18 runtime
  - kept Gmail disabled in live deployment because the dedicated Friday mailbox was blocked by Google
  - continued with Firecrawl, Google Maps, and Skiplagged while leaving restaurant/account-verification flows on pause/resume fallback
- Added a direct structured Skiplagged client in `agent/app/skiplagged.py` and wired typed travel tools into `agent/app/agent_core.py` for:
  - `travel_resolve_iata`
  - `travel_search_flights`
  - `travel_search_flexible_departures`
  - `travel_search_hotels`
  - `travel_search_cars`
- Added Skiplagged outage/rate-limit handling:
  - detect `429`, `1015`, `retry_after`, and related upstream-rate-limit failures
  - set a temporary outage window instead of hammering the upstream MCP/service repeatedly
- Fixed worker/control-plane bookkeeping for exited heavy jobs:
  - added a new internal worker job-status endpoint
  - updated the broker to verify that a container exit produced a real terminal job state
  - if the worker exits without reporting a terminal state, the broker now fails the job instead of leaving it silently stuck in `running`
- Fixed worker workspace-read ergonomics so missing files degrade cleanly instead of crashing the travel flow when an agent checks for an output before writing it.
- Deployed a new Lambda image with the paused-input/status UX cleanup and the worker-status verification endpoint:
  - image tag: `20260517-lambda-ux-skiplagged-bg-181948`
- Live-proved the direct Skiplagged travel stack through the Siri/worker path:
  - flights: completed with `skiplagged-nyc-sfo-flights.txt`
  - hotels: completed with `skiplagged-sfo-hotels.txt`
  - rental cars: completed with `skiplagged-sfo-rental-cars.txt`
- Live-proved the improved paused-input UX:
  - paused tasks now render with readable sections (`What I need`, `Details`, `Status`)
  - step names are humanized
  - the reply instruction now explicitly says `Reply with 'answer: ...' to continue` and clarifies that a normal new request can start something else
- Cleaned several stale validation jobs out of the control plane after earlier worker/runtime failures so task/status views are less misleading.
- Current accepted live state at end of day:
  - `Firecrawl`: proven
  - `cablate` Google Maps path: proven
  - `Skiplagged` flights / hotels / rental cars: proven
  - `Gmail`: intentionally disabled
  - `OpenTable` / `Resy`: not current production paths
  - restaurant/account-gated work still depends on pause/resume rather than a finished connector-backed identity flow

## 2026-05-15

- Replaced the intended Gmail mailbox direction:
  - stopped treating the OAuth-style `@cablate/mcp-gmail` path as the target
  - switched the repo to a headless IMAP/SMTP Gmail MCP plan using the dedicated Friday mailbox plus Gmail App Password authentication
  - added `GMAIL_ACCOUNT_EMAIL` / `GMAIL_APP_PASSWORD` plumbing, kept the older Google OAuth fields only as legacy compatibility for now
- Tightened the MCP security posture for the mailbox path:
  - Gmail MCP registration now exposes inbox-read/search tools only
  - send-email stays out of the default Friday tool surface until approval gating is implemented
- Fixed the worker-runtime propagation gap so dedicated-worker containers can receive either direct MCP secrets or SSM parameter names for the newer MCP stack.
- Fixed a real argument-order bug in `scripts/deploy-hands-worker.sh` where Browser-use settings were being shifted into the wrong installer parameters.
- Added a new explicit backlog item for reminder management:
  - Friday should eventually manage its own operational reminders
  - later support normal user reminders like `remind me tomorrow about this`
  - treat this as P3 after dashboard work, not current P1
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
- Added a first durable pause-for-input / human-in-the-loop resume path in repo:
  - heavy jobs can now enter `paused_for_input`
  - the worker preserves a checkpoint and asks only for the missing answer
  - resume currently uses an explicit `answer: ...` reply or the requested attachment
- Changed heavy-task artifact delivery defaults:
  - Browser-use step screenshots are no longer uploaded/sent as default user-facing outputs
  - explicit screenshot/image requests still allow screenshot delivery
  - multiple requested screenshots are bundled into one zip for Telegram delivery
- Added `docs/FRIDAY_OPERATOR_BOARD.md` as the operator-editable current-work/backlog/completed view for future Codex sessions.
- Added a dedicated spec/backlog doc for future account identity and sign-up gating work:
  - `docs/FRIDAY_ACCOUNT_IDENTITY_FLOW.md`
  - captures the intended cached-email vs new-email choice
  - records that password entry should go through a secure operator/dashboard surface backed by SSM, not through model-visible prompts
- Realigned the operator board, handoff, security model, and auto-compact prompts around the next P1 execution order:
  - finish operator validation of the shipped pause/resume and screenshot-delivery work
  - build the hybrid common-use routing layer for spreadsheets, itinerary/maps, and booking/reservations
  - review real connectors/MCPs only where they materially beat deterministic tools
  - keep Browser-use as the interaction fallback rather than the default reader
  - add login-wall and sign-up gating on top of the existing pause-for-input substrate
- Added a first code pass for the hybrid routing layer:
  - introduced task-routing profiles for spreadsheet/data, itinerary/maps, booking/commerce, and login/account tasks
  - those profiles now influence heavy/light classification and heavy-task prompt guidance
  - this is a routing/intention layer only, not yet the full connector-backed implementation
- Added `docs/FRIDAY_CAPABILITIES_MATRIX.md` as the operator-visible capability contract, proof standard, and editable backlog for what Friday may actually promise.
- Recorded the new file-surface direction:
  - migrate from the narrow custom-only workspace MCP shape toward the official filesystem MCP server
  - keep it scoped to allowed worker roots instead of exposing the full host filesystem
- Implemented the first repo-side official filesystem MCP migration:
  - the worker image now installs the official Node-based filesystem MCP server
  - Browser-use now mounts the official filesystem MCP for broad file/directory tools
  - the Friday helper MCP remains for preview, markdown conversion, and PDF generation
  - the filesystem MCP is scoped to the task workspace root
  - the Browser-use path falls back cleanly if either MCP server is unavailable
- Added a dedicated MCP stack-plan doc and first isolation harness scaffold:
  - `docs/FRIDAY_MCP_STACK_PLAN.md`
  - `ops/mcp/docker-compose.trust-tiers.yml`
  - records L1/L2/L3/L4 trust tiers
  - records the full MCP/connector candidate set discussed so far
  - records that Browser-use, filesystem, read-only network MCPs, and sensitive booking/identity MCPs should not remain a flat shared trust boundary
- Recorded the currently selected next evaluation set:
  - Resy
  - OpenTable
  - cablate Google Maps MCP
  - Google Maps / Places / Routes via OpenAPI MCP
  - dedicated Friday mailbox via Gmail/email MCP
- Recorded the dedicated Friday mailbox direction:
  - use a separate mailbox owned by the agent, not the operator's primary inbox
  - operator can still monitor it directly from normal mail clients
  - use it for verification emails, OTP fallback, and account-gated workflows
  - exact Gmail/email MCP server choice still needs implementation verification
- Added the first repo-side selected-candidate MCP wiring:
  - Firecrawl MCP registration path
  - cablate Google Maps MCP registration path
  - Gmail MCP candidate registration path for a dedicated Friday mailbox
  - worker image now includes the relevant npm packages for these candidates
- Extended the repo-side selected-candidate MCP wiring from "candidate hooks" to concrete travel/booking runtime paths:
  - added Google Maps / Places / Routes via OpenAPI MCP runtime settings and env shaping
  - added Resy MCP runtime settings plus worker-image build of `Jpc54066/resy-mcp`
  - added OpenTable MCP runtime settings using `@striderlabs/mcp-opentable`
  - updated the trust-tier compose harness so maps-openapi, Resy, and OpenTable are concrete services instead of generic placeholders
- Deployed the new Lambda image and refreshed the dedicated worker runtime in the live `iris` account.
- Proved the new paused-input path end to end on the live dedicated-worker stack:
  - Siri queued an underspecified reservation task
  - the task entered `paused_for_input`
  - the checkpoint stored a structured missing-input prompt
  - a follow-up `answer: ...` request created a new heavy job with `resume_from_job_id`
  - the resumed job claimed the worker and entered `running`
- Confirmed the deployment fallback still matters:
  - local Docker CLI was present but the local Docker daemon was unavailable
  - Lambda deployment used a remote Docker build on the dedicated worker host, then a CloudFormation stack update with the new ECR image URI
  - the worker runtime refresh needed a manual Docker restart and manual image rebuild after the scripted install path stalled in `docker build`
- Proved the text-driven stop path end to end against a synthetic heavy task:
  - Siri created a heavy task
  - a text `stop 1` control request was accepted
  - the worker observed the control signal and the job ended `interrupted`
- Confirmed the practical browser limits that still remain after the Browser-use migration:
  - Cloudflare / human-verification pages
  - heavy retail sites that crash or render blank result regions
  - good final synthesized reports paired with ugly intermediate screenshots from blocked pages

### Change: durable pause-for-input state added to the heavy-task path

- Goal:
  - Stop long-running tasks from failing terminally when the agent genuinely needs one more user answer or attachment to continue.
- Change:
  - Added a first-class `paused_for_input` job status plus worker pause API handling.
  - Added a heavy-mode `pause_for_input` tool so the agent can explicitly checkpoint and stop when it is blocked on missing user input.
  - Updated Telegram and Siri routing so paused jobs return a clear prompt and resume from checkpoint/workspace state when the user replies with `answer: ...` or sends the requested attachment.
  - Updated task/status handling so paused jobs show up in task listings and can be stopped cleanly without waiting on a live worker.
  - Documented the new repo behavior in `docs/FRIDAY_AGENT.md`, `docs/FRIDAY_TOOL_SECURITY.md`, and `docs/HANDOFF.md`.
- Result:
  - The repo now has a durable HITL pause/resume path aligned with the current dedicated-worker architecture instead of another light-path prompt workaround.
  - The first shipped resume UX is intentionally explicit and conservative:
    - text reply: `answer: ...`
    - file reply: send the requested attachment
- Validation:
  - `python3 -m py_compile agent/app/*.py hands/worker/runner.py` passed.
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests` passed with `30 passed`.
- Not yet validated live:
  - real Telegram/Siri pause and resume behavior after deploy
  - whether the explicit `answer: ...` contract should later be relaxed into a more automatic reply heuristic

### Change: browser screenshot spam suppressed and operator board added

- Goal:
  - Reduce noisy Telegram artifact delivery and give the operator a simple repo-visible place to edit active work and backlog state.
- Change:
  - Added artifact filtering so Browser-use step screenshots are excluded from uploaded outputs unless the original request explicitly asks for screenshots/images.
  - Updated Telegram completion delivery so explicitly requested multiple screenshots are sent as one zip instead of many separate documents.
  - Stopped Browser-use from advertising saved screenshot paths in its synthesized result unless screenshots were explicitly requested.
  - Added `docs/FRIDAY_OPERATOR_BOARD.md` and updated the handoff/session read order to include it.
- Result:
  - Default long-task completions should now send only the real deliverables such as PDFs, CSVs, and text reports.
  - Operator-visible backlog/current/completed state is now editable in one markdown file instead of living only in handoff prose and journal entries.
- Validation:
  - `python3 -m py_compile agent/app/*.py hands/worker/runner.py` passed.
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests` passed with the new artifact tests included.
- Not yet validated live:
  - real Telegram delivery behavior for explicit screenshot requests versus default research/report tasks

### Change: account identity / sign-up gating spec recorded

- Goal:
  - Preserve the intended future booking/account flow so later sessions do not improvise a weaker credential or sign-up pattern.
- Change:
  - Added `docs/FRIDAY_ACCOUNT_IDENTITY_FLOW.md` to capture the desired behavior for:
    - pausing at sign-in/sign-up gates
    - asking for cached identity vs new email
    - supporting operator-provided `Hide My Email`
    - keeping password entry in a secure operator/dashboard path backed by SSM
    - requiring explicit approval before account creation submits
  - Added the item to `docs/FRIDAY_OPERATOR_BOARD.md`, `docs/HANDOFF.md`, and `docs/FRIDAY_TOOL_SECURITY.md`.
- Result:
  - The repo now has a durable source of truth for this account-identity feature even though the supporting CRUD/dashboard/secret-entry implementation does not exist yet.
- Validation:
  - documentation-only change

### Change: backlog and auto-compact prompts aligned to hybrid hands plan

- Goal:
  - Preserve the intended next-stage Friday execution order so future Codex sessions do not regress into browser-first work or treat unreviewed MCP suggestions as already-approved architecture.
- Change:
  - Updated `docs/FRIDAY_OPERATOR_BOARD.md` so current focus explicitly targets the hybrid common-use routing layer:
    - spreadsheets / data outputs via workspace/file tools
    - itinerary/maps via deterministic tools or vetted connectors
    - reservations/commerce via vetted connectors when available, otherwise deterministic fetch first and Browser-use only for interaction
    - login/sign-up walls must pause and ask
  - Updated `docs/HANDOFF.md`, `docs/FRIDAY_AGENT.md`, and `docs/FRIDAY_TOOL_SECURITY.md` to reflect the same routing hierarchy and to record that named third-party MCPs are candidates, not approvals.
  - Updated `SAY_THIS_WHEN_AUTO_COMPACT.md` and `docs/NEXT_CODEX_PROMPT.md` so future re-entry prompts carry the same P1 work order.
- Result:
  - The repo now has a durable operator-visible and auto-compact-visible statement of what "agent readiness" still means before final acceptance testing:
    - validate shipped pause/resume and screenshot delivery
    - build hybrid hands routing
    - selectively add vetted connectors
    - add login-wall/sign-up gating
    - improve hostile-site browser handling only where still necessary
- Validation:
  - documentation-only change

### Change: first task-routing-profile layer added for hybrid hands work

- Goal:
  - Start turning the hybrid-hands plan into runtime behavior by teaching Friday to distinguish common-use task types before it decides how to work.
- Change:
  - Added `TaskRoutingProfile` detection in `agent/app/routing.py` for:
    - spreadsheet/data tasks
    - itinerary/maps tasks
    - booking/commerce tasks
    - login/account-gated tasks
  - Updated heavy/light classification so those task classes route to the heavy worker instead of being treated like generic light queries.
  - Updated heavy-task prompt construction in `agent/app/agent_core.py` so the worker receives explicit task-specific routing guidance instead of only generic deterministic-first instructions.
  - Added routing tests covering the new profiles and classifications.
- Result:
  - Friday now has a first explicit intent/routing layer for the hybrid tool strategy, which should reduce default browser drift on common-use tasks even before connector-specific work lands.
  - The implementation is still intentionally shallow:
    - it does not yet mount new connectors
    - it does not yet add login-wall pausing logic automatically
    - it does not yet encode per-profile execution metrics or observability
- Validation:
  - `python3 -m py_compile agent/app/*.py hands/worker/runner.py` passed.
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests/test_routing.py agent/tests/test_status_requests.py` passed with `14 passed`.

### Change: capability matrix and filesystem-MCP direction recorded

- Goal:
  - Make Friday's promised capabilities explicit and operator-editable, and avoid getting trapped later by an unnecessarily narrow file-tool decision.
- Change:
  - Added `docs/FRIDAY_CAPABILITIES_MATRIX.md` with:
    - `Promised` / `Caveated` / `Backlog` / `Blocked` states
    - proof standards for moving a capability into `Promised`
    - the current matrix for research, files, spreadsheets, itinerary/maps, booking, commerce, and account-gated workflows
    - an operator-editable backlog section
  - Updated `docs/FRIDAY_OPERATOR_BOARD.md`, `docs/HANDOFF.md`, `docs/FRIDAY_AGENT.md`, `docs/FRIDAY_TOOL_SECURITY.md`, `SAY_THIS_WHEN_AUTO_COMPACT.md`, and `docs/NEXT_CODEX_PROMPT.md` to treat the matrix as part of the core session context.
  - Recorded the explicit direction to adopt the official filesystem MCP server, but only within allowed worker roots rather than exposing the whole host.
- Result:
  - Friday now has a capability contract instead of only scattered handoff notes.
  - Future sessions should treat broad filesystem capability inside the worker workspace as the intended direction and should validate it on real file/spreadsheet tasks before retiring the current narrow-only path.
- Validation:
  - documentation-only change

### Change: official filesystem MCP mounted in repo worker path

- Goal:
  - Replace the narrow custom-only file surface with the official filesystem MCP server while keeping file access bounded to the isolated task workspace.
- Change:
  - Updated `hands/worker/Dockerfile` to install `nodejs`, `npm`, and the official `@modelcontextprotocol/server-filesystem` package in the worker image.
  - Updated `agent/app/browser_use_runner.py` so Browser-use now mounts:
    - the official filesystem MCP server for broad file and directory operations within the workspace root
    - the Friday helper MCP for preview, markdown conversion, and PDF generation
  - Added a small regression test for the filesystem MCP argument scoping in `agent/tests/test_browser_use_runner.py`.
  - Updated the capability matrix, handoff docs, agent docs, security model, and re-entry prompts so they reflect that the repo now has the filesystem MCP path in code, but still needs deployment/live validation.
- Result:
  - Friday now has a repo-level path to use a production-grade filesystem MCP surface instead of relying only on the earlier narrow helper server.
  - The scope remains bounded to the task workspace root, not the full host filesystem.
  - MarkItDown and PDF/report helpers remain available through the helper MCP instead of being dropped during the migration.
- Validation:
  - `python3 -m py_compile agent/app/*.py hands/worker/runner.py` passed.
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests/test_browser_use_runner.py agent/tests/test_routing.py agent/tests/test_status_requests.py` passed with `15 passed`.
- Not yet validated live:
  - worker image rebuild and deployment with the new Node/npm filesystem MCP dependency
  - real Browser-use task using the official filesystem MCP end to end in AWS

### Change: MCP trust-tier stack plan and harness scaffold added

- Goal:
  - Turn the high-level "layered isolated MCPs" guidance into durable repo architecture so future work does not collapse all MCPs into one flat execution boundary.
- Change:
  - Added `docs/FRIDAY_MCP_STACK_PLAN.md` with:
    - L1/L2/L3/L4 trust tiers
    - the current MCP/connector inventory
    - Gemini-discussed MCPs and Friday status
    - the isolation harness plan
    - the phased MCP program
  - Added `ops/mcp/docker-compose.trust-tiers.yml` as the first compose-level harness scaffold for:
    - filesystem MCP
    - workspace helper MCP
    - Firecrawl MCP
    - maps OpenAPI MCP placeholder
    - reservation MCP placeholder
    - identity MCP placeholder
  - Updated `docs/FRIDAY_TOOL_SECURITY.md`, `docs/FRIDAY_OPERATOR_BOARD.md`, `docs/HANDOFF.md`, `SAY_THIS_WHEN_AUTO_COMPACT.md`, and `docs/NEXT_CODEX_PROMPT.md` so the stack plan and trust-tier isolation are now part of normal session context.
- Result:
  - Friday now has an explicit MCP program with multiple candidates and a first isolation-harness artifact instead of only a generic "add some MCPs later" backlog note.
  - The harness is still a scaffold, not a deployed production runtime:
    - filesystem and Firecrawl entries are concrete
    - maps, reservation, and identity entries are placeholders pending final server selection
- Validation:
  - `docker compose -f ops/mcp/docker-compose.trust-tiers.yml config` passed.
- Not yet validated live:
  - actual multi-container MCP runtime wiring into the dedicated worker path
  - live output-quality comparison across Firecrawl, maps/travel, and reservation candidates

### Change: dedicated Friday mailbox and selected travel/booking candidate set recorded

- Goal:
  - Turn the operator's preferred travel, booking, and mailbox directions into durable architecture choices instead of leaving them implicit in chat history.
- Change:
  - Updated `docs/FRIDAY_MCP_STACK_PLAN.md` so the selected next evaluation set explicitly includes:
    - Resy
    - OpenTable
    - cablate Google Maps MCP
    - Google Maps / Places / Routes via OpenAPI MCP
    - a dedicated Friday mailbox via Gmail/email MCP
  - Updated `docs/FRIDAY_ACCOUNT_IDENTITY_FLOW.md` so a dedicated Friday-owned mailbox is now a first-class identity option for account-gated workflows and verification handling.
  - Updated `docs/FRIDAY_OPERATOR_BOARD.md` to reflect the selected evaluation set and the dedicated-mailbox requirement.
  - Recorded one important verification caveat:
    - the dedicated Gmail/mailbox architecture is accepted
    - the exact Gmail/email MCP implementation still needs a concrete server choice and verification
    - an "official Gmail MCP in the MCP reference repo" was not verified from primary sources in this session
- Result:
  - Future sessions now have a durable statement that Friday should grow toward:
    - NYC-focused restaurant booking via Resy/OpenTable comparison
    - maps/travel via Google Maps MCP/API comparison
    - a dedicated agent-owned mailbox that both Friday and the operator can access
- Validation:
  - documentation-only change

### Change: selected Firecrawl, maps, and mailbox MCP candidates wired into repo

- Goal:
  - Move the chosen read-only and mailbox candidates from "selected in docs" to "mountable in code" so the next deploy can validate them instead of starting from scratch.
- Change:
  - Expanded `agent/app/settings.py` with feature flags and secret parameters for:
    - Firecrawl MCP
    - Google Maps MCP
    - Gmail MCP
  - Refactored `agent/app/browser_use_runner.py` so Browser-use can now register optional MCP candidates for:
    - Firecrawl
    - cablate Google Maps
    - Gmail mailbox access
    in addition to the filesystem and helper MCPs.
  - Updated `hands/worker/Dockerfile` to include npm installs for:
    - `firecrawl-mcp`
    - `@cablate/mcp-google-map`
    - `@cablate/mcp-gmail`
  - Updated `ops/mcp/docker-compose.trust-tiers.yml` so the trust-tier harness now contains concrete `google-maps-mcp` and `gmail-mcp` services instead of only placeholders.
  - Added focused tests for the new MCP env-building helpers in `agent/tests/test_browser_use_runner.py`.
- Result:
  - The repo can now mount Firecrawl, Google Maps, and Gmail candidate MCPs through explicit settings/secrets rather than requiring another design round first.
  - Resy/OpenTable are still selected targets but remain unwired until a concrete implementation choice is made.
- Validation:
  - `python3 -m py_compile agent/app/*.py hands/worker/runner.py` passed.
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests/test_browser_use_runner.py agent/tests/test_routing.py agent/tests/test_status_requests.py` passed with `18 passed`.
  - `docker compose -f ops/mcp/docker-compose.trust-tiers.yml config` passed.
- Not yet validated live:
  - AWS deployment of the new worker image and settings
  - real Firecrawl task quality
  - real Google Maps itinerary task quality
  - real dedicated-mailbox Gmail MCP behavior

### Change: live deploy + resumable validation completed on `iris`

- Goal:
  - Move the new pause/resume and screenshot-delivery changes from repo-only to deployed/live-tested state.
- Change:
  - Built and pushed a new Lambda image remotely on the dedicated worker host because the local Docker daemon was unavailable.
  - Updated the live `friday-agent` CloudFormation stack to image `301142908919.dkr.ecr.us-east-1.amazonaws.com/friday-agent:20260515-pause-resume-remote-162800`.
  - Refreshed the dedicated worker runtime on instance `i-0ba8310cd02e1c847` and manually rebuilt/restarted `friday-hands-broker` after the scripted install path stalled in `docker build`.
- Result:
  - `GET /health` returns `{"status":"ok"}` on the live Function URL after deploy.
  - Live Siri validation proved:
    - an underspecified reservation task enters `paused_for_input`
    - the checkpoint contains a structured missing-input prompt
    - an `answer: ...` follow-up creates a resumed heavy job linked by `resume_from_job_id`
    - the resumed job claims the worker and enters `running`
  - Explicit stop still works on the new runtime:
    - an in-flight browser-heavy job was interrupted by control signal
    - the resumed reservation job then claimed the worker as expected
- Validation details:
  - live pause job: `01714970-8a39-4c6d-a283-040dee3f98f9`
  - live resumed job: `49573959-9feb-4e93-a5a8-b6b792485ae3`
  - interrupted browser-heavy validation job: `8c20cc69-72fb-4832-b9c7-4c8d1f6098fc`
- Remaining caveat:
  - the new screenshot-suppression default is deployed, but this session did not capture a fully completed browser-heavy job proving the exact final Telegram file-delivery behavior end to end.
- Backup note:
  - `scripts/backup-agent-state.sh` hit the known IAM gap on `s3:GetBucketLocation`
  - a manual metadata backup was still written under `backup/aws-agent/20260515-162622-manual`

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
