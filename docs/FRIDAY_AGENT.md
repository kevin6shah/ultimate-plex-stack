# Friday Personal Agent

## Purpose

The Friday personal agent is a serverless AI assistant that lives in this repo and is deployed through the same AWS account-rotation workflow as the shared WireGuard/Iris host.

## Fresh Session Read Order

Before changing or deploying the agent:

1. `docs/HANDOFF.md`
2. `docs/FRIDAY_OPERATOR_BOARD.md`
3. `docs/FRIDAY_CAPABILITIES_MATRIX.md`
4. `docs/CODEX_MAINTENANCE_LOOP.md`
5. `docs/FRIDAY_AGENT.md`
6. `docs/FRIDAY_TOOL_SECURITY.md`
7. `docs/FRIDAY_AWS_SYSTEM_DESIGN.md`
8. `docs/AWS_MIGRATION.md`
9. `ops/aws/iam/README.md`

For account-creation / identity-management work, also read:

- `docs/FRIDAY_ACCOUNT_IDENTITY_FLOW.md`

For code changes, inspect `agent/app/` and run local tests. For live AWS changes, inspect the current CloudFormation stack and run `./scripts/backup-agent-state.sh` first if the stack exists.

## Runtime

- Telegram is the default text and notification interface.
- Siri calls `POST /siri` for short spoken answers or long task submission.
- Long Siri tasks immediately return "I started that and will notify you in Telegram." Status and final results go to Telegram only.
- SQS decouples webhooks from agent execution so Telegram and Siri requests can return quickly.
- DynamoDB stores transient session metadata, 48-hour conversation context, durable `#memory`, heavy-job metadata, checkpoints, approvals, and the model spend ledger.
- SSM SecureString stores tokens and API keys.
- Lambda handles only light tasks and job coordination.
- When `FRIDAY_EXECUTION_BACKEND=temporal`, Lambda is only ingress/client coordination:
  - it starts/signals Temporal workflows
  - it mirrors user-facing state into DynamoDB/Telegram
  - it starts the dedicated worker when heavy execution is needed
- Temporal mode only activates when a real `TEMPORAL_HOST` is configured. If that host is empty, the code intentionally does not pretend Temporal is live.
- Heavy browser/file tasks belong on the dedicated on-demand EC2 worker so the shared VPN/Iris host remains orchestration-only, not the heavy runtime target.

## Dashboard And Cost View

- The agent stack creates a CloudWatch dashboard named `${AgentName}-operations`.
- The dashboard includes total estimated AWS charges plus per-service billing series for the shared host and the serverless agent.
- Shared-host cost view is split between `AmazonEC2` and `AmazonVPC` so the public IPv4 charge is visible separately from the EC2 instance itself.
- Agent cost view tracks `AWSLambda`, `AmazonCloudWatch`, `AmazonDynamoDB`, `AmazonSQS`, `AmazonSNS`, and `AmazonEC2ContainerRegistry`.
- Billing metrics live in `us-east-1` and are the authoritative cost source for the dashboard.
- Billing widgets and billing alarms require `Receive CloudWatch Billing Alerts` to be enabled in AWS Billing Preferences for the payer account.
- `NetworkOut` on the shared EC2 host is useful for spotting VPN-heavy months, but it is only a proxy; the first 100 GB/month of AWS internet egress is free and the billable truth is the billing widget.
- Billing lines for a given service appear only after AWS has published charge data for that service.

## Repo Map

- `agent/app/main.py`: FastAPI routes, Lambda handler, and SQS event handling.
- `agent/app/agent_core.py`: PydanticAI agent construction, model selection, tool registration, and spend recording.
- `agent/app/routing.py`: task classification and confirmation-gating heuristics.
- `agent/app/budget.py`: DeepSeek token-cost estimation and date keys.
- `agent/app/storage.py`: DynamoDB access for session context, memory, jobs, checkpoints, approvals, and spend.
- `agent/app/telegram.py`: Telegram update parsing and notification delivery.
- `agent/app/browser.py`: Playwright browser task implementation.
- `agent/app/research.py`: deterministic search/fetch wrappers and tool-output sanitization.
- `agent/app/workspace.py`: isolated workspace file and shell helpers used by the hands worker.
- `agent/app/temporal_runtime.py`: Temporal backend gating and client connection helpers.
- `agent/app/temporal_client.py`: control-plane workflow start/signal helpers.
- `agent/app/temporal_workflows.py`: durable heavy-task workflow definition.
- `agent/app/temporal_control_activities.py`: shared-host orchestration activities that prepare claims and finalize user-visible state.
- `agent/app/heavy_job_runtime.py`: shared heavy-job helpers for claims, pause/resume text, and final Telegram delivery.
- `hands/host/temporal_workflow_worker.py`: shared-host Temporal workflow worker.
- `hands/worker/temporal_activity_worker.py`: dedicated-worker Temporal heavy activity worker.
- `hands/host/broker.py`: legacy heavy-job claim loop, retained only for explicit legacy mode.
- `hands/worker/runner.py`: legacy claim-driven worker container entrypoint.
- `ops/aws/friday-agent.yaml`: CloudFormation for ECR, Lambda, Function URL, SQS, DynamoDB, IAM, logs, and optional budget.
- `ops/aws/install-hands-runtime.sh`: installs the rootless Docker worker runtime on the shared host.
- `scripts/deploy-hands-host.sh`: pushes the hands runtime to the shared host and wires it to the live Lambda Function URL.
- `ops/aws/friday-hands-worker.yaml`: CloudFormation for the dedicated on-demand hands worker EC2 instance.
- `ops/aws/install-hands-worker-runtime.sh`: installs the dedicated worker runtime on the EC2 worker host and prefers loading a prebuilt image archive when one is provided.
- `scripts/deploy-hands-worker.sh`: creates the dedicated worker instance, builds the worker image locally, transfers it to the worker host, and installs the hands runtime onto it.
- `scripts/deploy-agent.sh`: two-pass ECR/image/runtime deployment.
- `scripts/check-agent-migration-readiness.sh`: target-account preflight.
- `scripts/sync-env-from-ssm.sh`: syncs known Friday secrets from AWS SSM into the local `.env` file.
- `scripts/sync-ssm-from-env.sh`: syncs all non-empty mapped values from the local `.env` file back into AWS SSM.
- `scripts/backup-agent-state.sh`: metadata-only backup; no plaintext secrets.

## Behavior Contract

- Telegram webhook returns quickly after queueing work; the worker sends the final answer to Telegram.
- Siri short tasks are answered synchronously so the Shortcut can speak them.
- Siri long tasks are queued and return the immediate fixed response; updates and final answers go to Telegram only.
- The browser tool is not offered in Lambda light-mode at all.
- Heavy tasks are classified before execution. Browser actions, attachments, file-processing work, and explicit resume requests are routed to the hands runtime instead of Lambda.
- Heavy tasks can now pause durably for missing user input instead of failing terminally. The worker writes a `paused_for_input` state, preserves a checkpoint, and asks the user for the missing answer.
- In Temporal mode, the explicit resume contract for paused-input jobs is: reply with `answer: ...`. That signals the existing workflow to continue from the saved checkpoint/workspace instead of creating a separate replacement job.
- Attachment-driven resume is still handled by creating a new heavy run when the workflow needs a newly uploaded file.
- Browser-use step screenshots are no longer sent back to Telegram by default. They are only kept/sent when the original request explicitly asks for screenshots or images, and multiple requested screenshots are bundled into one zip.
- The intended common-use routing hierarchy is:
  - real connector or deterministic API when one exists and is vetted
  - workspace MCP / MarkItDown / local structured file tools for files, spreadsheets, and document outputs
  - deterministic search/fetch and page conversion for general web reading
  - Stagehand for bounded interactive fallback after deterministic/API paths fail
  - Browser-use only for the final interaction/login/form-fill fallback when Stagehand is unavailable or also fails
- The capability contract for what Friday may actually promise lives in `docs/FRIDAY_CAPABILITIES_MATRIX.md`.
- Stable general questions should stay synchronous and answer from model knowledge.
- Conversation/task context is remembered for up to 48 hours per channel/user/conversation so follow-up questions still work.
- The 48-hour layer now stores raw thread turns plus a rolling summary. Prompt assembly uses both, instead of relying on one coarse summary blob.
- Context-window behavior is persisted as agent config in DynamoDB. The backend can now store values like max recent turns, summary size, and prompt suffix without code edits.
- Only explicit `#...` facts are durable long-term memory.
- Any task that may buy, delete, submit, send, change accounts, or expose sensitive data must ask for explicit confirmation first.
- Browser work on the hands worker is still bounded and isolated. Do not assume privileged host access, direct access to Iris/VPN files, or persistent login sessions unless a later change explicitly adds them.
- Browser search no longer depends on Google result pages; the Playwright worker uses direct URLs when present and otherwise falls back to public search results plus HTTP text extraction when a site blocks normal automation.
- Static MCP configuration may be added later, but the agent must not fetch or install arbitrary tools at runtime.
- The static system prompt in `agent/app/prompts.py` should remain byte-stable where possible for DeepSeek cache behavior; dynamic context belongs in user/task inputs.

## Deployment

Required SSM SecureString parameters in each AWS account:

```text
/friday/agent/deepseek-api-key
/friday/agent/brave-search-api-key
/friday/agent/telegram-bot-token
/friday/agent/telegram-chat-id
/friday/agent/telegram-webhook-secret
/friday/agent/siri-api-key
/friday/agent/worker-api-key
```

`/friday/agent/brave-search-api-key` is optional. When present, heavy-task research uses Brave Search API as the deterministic search layer before escalating to full browser automation.

Optional MCP/connector SSM SecureString parameters now supported by the stack:

```text
/friday/agent/firecrawl-api-key
/friday/agent/google-maps-api-key
/friday/agent/maps-openapi-headers
/friday/agent/maps-openapi-auth-token
/friday/agent/resy-api-key
/friday/agent/resy-auth-token
/friday/agent/opentable-email
/friday/agent/opentable-password
/friday/agent/gmail-account-email
/friday/agent/gmail-app-password
/friday/agent/gmail-client-id
/friday/agent/gmail-client-secret
/friday/agent/gmail-refresh-token
```

Those are only needed when the corresponding MCP/connector is enabled in deployment settings.

For the Friday mailbox specifically:

- preferred current path: `/friday/agent/gmail-account-email` + `/friday/agent/gmail-app-password`
- legacy path kept only for migration compatibility: `/friday/agent/gmail-client-id`, `/friday/agent/gmail-client-secret`, `/friday/agent/gmail-refresh-token`

## Current Hands Stack

Friday's intended heavy-task execution stack now is:

- `PydanticAI` for planning and typed tool use
- deterministic research wrappers (`web_search`, `fetch_web_page`) before browser escalation
- `Temporal` for durable heavy-task orchestration, stop/resume signals, and pause-for-input continuity
- `Stagehand` as the preferred interactive browser lane on the dedicated worker
- `Browser-use` only as the last browser fallback
- `playwright-stealth` plus rotated user agents in the dedicated worker
- the official filesystem MCP server for broad file/directory operations within the worker workspace roots
- a Friday-owned workspace helper MCP server for preview, markdown conversion, and PDF generation
- `MarkItDown`-backed workspace document conversion
- workspace file tools for PDF/text/table generation and shell/Python execution

Additional repo-wired MCP candidates now exist behind settings/secrets for evaluation:

- Firecrawl MCP
- cablate Google Maps MCP
- Google Maps / Places / Routes via OpenAPI MCP
- Resy MCP runtime hook
- OpenTable MCP runtime hook
- Gmail IMAP/SMTP MCP candidate for a dedicated Friday mailbox

Current live proof on the dedicated worker:

- Firecrawl-backed research has completed real file/report tasks
- cablate Google Maps has completed real map/planning tasks
- direct Skiplagged travel tools have completed real flights, hotels, and rental-car tasks
- Gmail remains intentionally disabled because the dedicated Friday mailbox was blocked by Google; email/OTP steps should currently fall back to pause/resume plus operator input

This is now meant to be a Temporal-orchestrated, Stagehand-first heavy-task substrate. The older claim-loop broker and Browser-use-first path remain in repo only as explicit legacy fallbacks and migration references.

Current explicit direction:

- keep the official filesystem MCP server as the primary broad file surface, scoped to allowed worker roots
- keep root scope bounded to the isolated worker workspace rather than the full host filesystem

The near-term product direction on top of this stack is a hybrid hands layer for common-use tasks:

- spreadsheets / structured data should primarily use local file/spreadsheet tooling, not the browser
- itinerary planning and map lookups should prefer deterministic APIs/connectors or deterministic search/fetch
- reservations and commerce should prefer vetted connectors when they actually exist in this stack
- Browser-use should be reserved for interaction steps, unsupported sites, and true browser-only flows

The repo now includes a first task-routing-profile pass for that direction:

- spreadsheet/data requests
- itinerary/maps requests
- booking/commerce requests
- login/account-gated requests

Those profiles currently shape heavy-task routing guidance and heavy/light classification. They are not yet the full connector-backed routing layer by themselves.

The repo now includes a durable pause-for-input path that is intended to live on the Temporal workflow, not as a new replacement heavy job each time:
- a heavy Siri or Telegram task can enter `paused_for_input`
- a follow-up `answer: ...` signal can resume the same workflow
- the dedicated worker can continue from the prior checkpoint/workspace path without handing orchestration back to the old claim loop

The screenshot-suppression default is deployed live as part of the same rollout, but a clean end-to-end completed browser job proving the exact Telegram artifact/zip behavior is still pending.

The remaining acceptance gap is operator validation:
- pause/resume has been live-tested by Codex, but the operator has not yet accepted it through normal use
- login-wall and sign-in/sign-up gate pausing is still a backlog item, not a finished behavior
- the old OAuth-style Gmail MCP path is no longer the intended direction; use a headless IMAP/App Password MCP path for the Friday mailbox instead

`/friday/agent/logfire-token` is only required when `LOGFIRE_ENABLED=true`.

`/friday/agent/telegram-chat-id` must be the direct chat ID that Telegram sends in webhook payloads for the owner chat, not the bot ID.

Do not commit plaintext values or export them through backup scripts.

Pull the current AWS SSM secrets into the local `.env`:

```bash
bash ./scripts/sync-env-from-ssm.sh
```

Push the local `.env` values back into AWS SSM:

```bash
bash ./scripts/sync-ssm-from-env.sh
```

Deploy or update:

```bash
AWS_PROFILE=iris AWS_REGION=us-east-1 ./scripts/deploy-agent.sh
```

To also update Telegram's webhook:

```bash
AWS_PROFILE=iris AWS_REGION=us-east-1 SET_TELEGRAM_WEBHOOK=1 ./scripts/deploy-agent.sh
```

The deploy script creates the ECR repository first, builds and pushes the Lambda image, then updates the runtime stack.

Deploy the shared-host hands runtime after the Lambda stack is live:

```bash
AWS_PROFILE=iris AWS_REGION=us-east-1 HOST=<shared-host-ip> EC2_SSH_KEY=/path/to/key.pem ./scripts/deploy-hands-host.sh
```

Deploy or refresh the dedicated on-demand worker runtime:

```bash
AWS_PROFILE=iris AWS_REGION=us-east-1 \
KEY_NAME=<aws-keypair-name> \
EC2_SSH_KEY=/path/to/key.pem \
./scripts/deploy-hands-worker.sh
```

Deployment prerequisites:

- `aws` CLI configured with the target profile.
- `docker` installed and running.
- Required SSM parameters already present in the target account.
- IAM policy from `ops/aws/iam/codex-migration-policy.json` attached to the migration user.

The Lambda image is built for `linux/amd64` because the Playwright base image is x86_64. The function memory is `2048MB`, timeout is `900` seconds, and reserved concurrency is capped in CloudFormation.

## Migration

The main migration script includes the agent by default:

```bash
AWS_PROFILE=<new-account-profile> \
AWS_REGION=us-east-1 \
KEY_NAME=<new-keypair-name> \
EC2_SSH_KEY=/path/to/new-key.pem \
./scripts/migrate-aws-account.sh --yes
```

Migration order:

1. Validate/restore the shared EC2 host for WireGuard and Iris.
2. Run shared-host smoke checks.
3. Validate agent target-account readiness.
4. Build/push the agent image.
5. Deploy/update the agent CloudFormation stack.
6. Install the shared-host hands runtime against the new Function URL and worker key.
7. Print the new Telegram and Siri URLs.

Use `--skip-agent` only when the shared host must be recovered urgently and agent deployment should be deferred.

Agent session state is intentionally not migrated. The target account starts with clean transient memory and a fresh spend ledger.

## Validation

Local code checks:

```bash
python3 -m py_compile agent/app/*.py
cd agent && python3 -m pytest -q
```

AWS template/readiness check:

```bash
AWS_PROFILE=<profile> AWS_REGION=us-east-1 ./scripts/check-agent-migration-readiness.sh
```

The repo scripts prepend `/usr/local/bin:/opt/homebrew/bin` to `PATH`. On this Mac, a raw `aws ...` command may fail in some shells even though `/usr/local/bin/aws` works and the scripts run correctly.

Current live `iris` account status:

1. `friday-agent` is deployed in `us-east-1`.
2. Telegram webhook is registered against the Lambda Function URL `/telegram`.
3. Siri is live on the Lambda Function URL `/siri`.
4. CloudWatch dashboard `friday-agent-operations` is present.
5. Billing alarms and Telegram cost notifier resources are deployed.
6. A dedicated `t3a.small` on-demand worker is the current heavy-task execution target.
7. The dedicated worker has been validated for:
   - long-running workspace tasks that create and return files
   - checkpointed heavy-task resume
   - browser screenshot + PDF artifact generation
   - real end-to-end camera research that generated and uploaded a final PDF artifact
   - Browser-use as the primary browser/computer-use path on the dedicated worker

Current known worker caveats:

- Browser-use is materially better than the old selector-driven path, but it is still not magic on hostile sites. Current observed failure classes:
  - Cloudflare / “verify you are human” challenge pages
  - retail pages that crash or show blank result areas under automation
  - anti-bot flows that still produce ugly step screenshots even when the final synthesized report is good
- `playwright-stealth` helps but is not sufficient by itself for some hostile domains.
- The right routing rule is still:
  - connector/API if we have one
  - deterministic search/fetch second
  - browser only when interaction is required
- Restaurant reservation availability is still on the browser/deterministic path because no vetted installed reservation connector exists in this stack yet.
- The live “show my tasks / what’s the status / stop 1” control-plane path is now available and should be used instead of re-prompting the model for job state.

- The dedicated worker deploy path now depends on local Docker image build + transfer. Rebuilding the Playwright image on the small worker host itself proved unreliable.
- Operator IAM still needs `ec2:StartInstances`, `ec2:StopInstances`, `ec2:RebootInstances`, `cloudformation:DescribeStackResources`, and `s3:GetObject` if Codex should fully operate and debug the worker directly.
- Hidden runtime files should not be treated as user deliverables. `Workspace.list_files()` now filters dotfiles/directories so `.cache/...` and `.pki/...` do not become reported outputs.
- Live validation on `2026-05-15` confirmed that newer Lambda checkpoint handling keeps active heavy jobs in `running` state instead of incorrectly flipping them to `checkpointed` after the first checkpoint.
- Explicit operator interruption is now proven on the live dedicated worker path:
  - an operator-stopped worker container transitions the job to `interrupted`
  - `resume that task` preserves the original heavy-task prompt instead of treating the literal resume text as the new job body
  - resumed work reuses prior workspace state and can complete with carried-forward files
- Live validation on `2026-05-15` also proved the newer paused-input path:
  - an underspecified reservation task entered `paused_for_input`
  - the checkpoint stored a structured missing-input prompt
  - an `answer: ...` follow-up created a resumed heavy job linked through `resume_from_job_id`
  - the resumed job successfully reclaimed the worker and entered `running`
- Real camera-research E2E is now proven on the Browser-use-backed stack:
  - auto-start from a stopped worker instance
  - deterministic search/fetch first
  - Browser-use browser escalation only when needed
  - Friday-owned workspace MCP tools for file handoff inside the worker
  - a successful final PDF artifact:
    - `reports/vlogging-travel-snowboarding-cameras.pdf`
    - `reports/adventure-cameras-2025.pdf`
- Users should be able to ask for progress explicitly while a heavy task is running.
  - Telegram/Siri status questions like `what's the status?`, `any update?`, and `did it finish?` should return the latest job state directly instead of invoking the model again.
- Common-use connector policy:
  - use real MCP/app connectors for categories that have them
  - current examples include flights and hotels
  - restaurant reservation availability does not currently have an installed production-worthy connector in this stack, so it still routes to deterministic web research and then browser automation when needed
- Current remaining caveat:
  - Telegram interruption delivery was not independently observable from the current CloudWatch log shape, even though job-state interruption and resume behavior are proven live

After deployment:

```bash
curl <FunctionUrl>/health
```

Then set Telegram's webhook with `SET_TELEGRAM_WEBHOOK=1 ./scripts/deploy-agent.sh` or by calling Telegram `setWebhook` manually with the printed `/telegram` URL and the SSM-stored webhook secret.

## Budget And Privacy

- App-level model budget defaults to `$0.40/day` and `$12/month`.
- Default cost accounting uses the higher `deepseek-reasoner` rates unless overridden, so budget enforcement is conservative when mixed chat/reasoner usage is enabled.
- CloudWatch log retention is one day.
- Logfire is disabled by default.
- Full prompt/tool logging is a separate opt-in and should stay off unless debugging requires it.
- Agent session state TTL is 10 minutes; 48-hour context expires automatically; heavy jobs/checkpoints retain their own bounded TTLs; spend rows live longer so budget enforcement still works.
- The model should not receive all retained history by default. Prompt assembly should be: stable system prompt + durable `#memory` + rolling topic summary + recent relevant turns + current request.
- `./scripts/backup-agent-state.sh` captures stack metadata only and must not export plaintext SSM secret values.

## Memory Model

- `#memory` is explicit durable memory and survives migrations.
- Thread memory is conversation-scoped and expires after 48 hours.
- Thread memory now has two layers:
  - raw turns (`user` / `assistant`)
  - rolling summary for cheap prompt assembly
- The context window is not “all messages in the last 48 hours”. It is a retained store with a configurable retrieval window.
- Current backend config supports:
  - `context_max_turns`
  - `context_summary_max_chars`
  - `conversation_ttl_hours`
  - `system_prompt_suffix`
  - optional per-app budget overrides
- API surfaces already exist for later dashboard work:
  - `GET /config`
  - `PUT /config`
  - `GET /threads/{conversation_id}`

## Troubleshooting

- If `/telegram` returns `401`, check the Telegram webhook secret header and `/friday/agent/telegram-webhook-secret`.
- If `/telegram` returns `403`, check `/friday/agent/telegram-chat-id`.
- If Siri returns `401`, check the Shortcut header `x-friday-siri-key` and `/friday/agent/siri-api-key`.
- If Siri or Telegram starts failing right after an image update, re-test once the stack is `UPDATE_COMPLETE`; requests during rollout can hit containers that are still draining.
- If PydanticAI tool registration fails with a `RunContext` `NameError`, check `agent/app/agent_core.py` for annotation scope regressions around `web_browser_task`.
- If HTTP requests fail after an SQS worker run with `There is no current event loop in thread 'MainThread'`, check the Lambda `handler()` path in `agent/app/main.py` and make sure it recreates an event loop before calling Mangum on non-SQS events.
- If heavy jobs queue but never start, inspect Lambda logs for `/internal/worker/claim` activity and verify `friday-hands-broker.service` is active on the shared host.
- If jobs queue but no response arrives, inspect Lambda logs, shared-host `friday-hands-broker.service`, and the latest job/checkpoint state through `/jobs/<job_id>`.
- If heavy browser/file tasks fail, verify the hands runtime was redeployed to the shared host, rootless Docker is healthy for `friday-hands`, and the worker image was rebuilt from `hands/worker/Dockerfile`.
- If deployment fails before image push, run `./scripts/check-agent-migration-readiness.sh` and fix target-account IAM/SSM/ECR access first.
