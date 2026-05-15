# Friday Personal Agent

## Purpose

The Friday personal agent is a serverless AI assistant that lives in this repo and is deployed through the same AWS account-rotation workflow as the shared WireGuard/Iris host.

## Fresh Session Read Order

Before changing or deploying the agent:

1. `docs/HANDOFF.md`
2. `docs/CODEX_MAINTENANCE_LOOP.md`
3. `docs/FRIDAY_AGENT.md`
4. `docs/FRIDAY_TOOL_SECURITY.md`
5. `docs/AWS_MIGRATION.md`
6. `ops/aws/iam/README.md`

For code changes, inspect `agent/app/` and run local tests. For live AWS changes, inspect the current CloudFormation stack and run `./scripts/backup-agent-state.sh` first if the stack exists.

## Runtime

- Telegram is the default text and notification interface.
- Siri calls `POST /siri` for short spoken answers or long task submission.
- Long Siri tasks immediately return "I started that and will notify you in Telegram." Status and final results go to Telegram only.
- SQS decouples webhooks from agent execution so Telegram and Siri requests can return quickly.
- DynamoDB stores transient session metadata, 48-hour conversation context, durable `#memory`, heavy-job metadata, checkpoints, approvals, and the model spend ledger.
- SSM SecureString stores tokens and API keys.
- Lambda handles only light tasks and job coordination.
- Heavy browser/file tasks are being moved to a dedicated on-demand EC2 hands worker so Lambda remains coordination-only and the shared VPN/Iris host is no longer the long-term execution target.

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
- `hands/host/broker.py`: local broker used by the hands worker host to claim heavy jobs and launch the isolated worker container.
- `hands/worker/runner.py`: hands worker container entrypoint.
- `ops/aws/friday-agent.yaml`: CloudFormation for ECR, Lambda, Function URL, SQS, DynamoDB, IAM, logs, and optional budget.
- `ops/aws/install-hands-runtime.sh`: installs the rootless Docker worker runtime on the shared host.
- `scripts/deploy-hands-host.sh`: pushes the hands runtime to the shared host and wires it to the live Lambda Function URL.
- `ops/aws/friday-hands-worker.yaml`: CloudFormation for the dedicated on-demand hands worker EC2 instance.
- `ops/aws/install-hands-worker-runtime.sh`: installs the dedicated worker runtime on the EC2 worker host and prefers loading a prebuilt image archive when one is provided.
- `scripts/deploy-hands-worker.sh`: creates the dedicated worker instance, builds the worker image locally, transfers it to the worker host, and installs the hands runtime onto it.
- `scripts/deploy-agent.sh`: two-pass ECR/image/runtime deployment.
- `scripts/check-agent-migration-readiness.sh`: target-account preflight.
- `scripts/bootstrap-agent-ssm.sh`: interactive SecureString creation for required agent secrets.
- `scripts/backup-agent-state.sh`: metadata-only backup; no plaintext secrets.

## Behavior Contract

- Telegram webhook returns quickly after queueing work; the worker sends the final answer to Telegram.
- Siri short tasks are answered synchronously so the Shortcut can speak them.
- Siri long tasks are queued and return the immediate fixed response; updates and final answers go to Telegram only.
- The browser tool is not offered in Lambda light-mode at all.
- Heavy tasks are classified before execution. Browser actions, attachments, file-processing work, and explicit resume requests are routed to the hands runtime instead of Lambda.
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

## Current Hands Stack

Friday's heavy-task execution stack currently is:

- `PydanticAI` for planning and typed tool use
- deterministic research wrappers (`web_search`, `fetch_web_page`) before browser escalation
- `playwright-stealth` plus rotated user agents in the dedicated worker
- a custom hardened Playwright layer for live website interaction
- workspace file tools for PDF/text/table generation and shell/Python execution

This is not yet a full `Browser-use` migration. The current browser substrate is still Friday-owned code on top of Playwright, hardened with retries, source-skipping, and deterministic-first routing.

`/friday/agent/logfire-token` is only required when `LOGFIRE_ENABLED=true`.

`/friday/agent/telegram-chat-id` must be the direct chat ID that Telegram sends in webhook payloads for the owner chat, not the bot ID.

Do not commit plaintext values or export them through backup scripts.

Create or update them interactively:

```bash
AWS_PROFILE=iris AWS_REGION=us-east-1 ./scripts/bootstrap-agent-ssm.sh
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

Current known worker caveats:

- The dedicated worker deploy path now depends on local Docker image build + transfer. Rebuilding the Playwright image on the small worker host itself proved unreliable.
- Operator IAM still needs `ec2:StartInstances`, `ec2:StopInstances`, `ec2:RebootInstances`, `cloudformation:DescribeStackResources`, and `s3:GetObject` if Codex should fully operate and debug the worker directly.
- Hidden runtime files should not be treated as user deliverables. `Workspace.list_files()` now filters dotfiles/directories so `.cache/...` and `.pki/...` do not become reported outputs.
- Live validation on `2026-05-15` confirmed that newer Lambda checkpoint handling keeps active heavy jobs in `running` state instead of incorrectly flipping them to `checkpointed` after the first checkpoint.
- Explicit operator interruption is now proven on the live dedicated worker path:
  - an operator-stopped worker container transitions the job to `interrupted`
  - `resume that task` preserves the original heavy-task prompt instead of treating the literal resume text as the new job body
  - resumed work reuses prior workspace state and can complete with carried-forward files
- Real camera-research E2E is now proven on the hardened stack:
  - auto-start from a stopped worker instance
  - deterministic search/fetch first
  - browser escalation only when needed
  - a successful final PDF artifact:
    - `reports/vlogging-travel-snowboarding-cameras.pdf`
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
