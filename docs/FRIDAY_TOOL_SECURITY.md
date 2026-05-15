# Friday Tool Security Model

## Purpose

Friday's control plane is allowed to reason. It is not allowed to have raw power.

All high-risk capabilities must be mediated through a Friday-owned wrapper and an isolated execution runtime. This applies whether the underlying capability is a local Python helper, Browser-use, Playwright, or an MCP server.

## Trust Layers

### 1. LLM Layer

The model only receives:
- task intent
- selected 48-hour context
- durable `#memory`
- curated tool outputs

The model must never receive:
- raw API keys
- raw session cookies
- bearer tokens
- SSM parameter values
- broad file trees outside the workspace

### 2. Friday Adapter / Policy Layer

Friday-owned Python wrappers decide:
- which tools can run
- which arguments are valid
- when a deterministic tool should be used before a browser
- how outputs are redacted before they return to the model
- when a task needs approval instead of automatic execution

The model does not call MCP servers or shell processes directly.

### 3. Isolated Tool Runtime

High-risk tools must run in isolated worker containers with:
- pinned dependency versions
- read-only container filesystems
- explicit writable mounts only where needed
- CPU / memory / pids limits
- no-new-privileges
- dropped Linux capabilities

Where possible, the runtime should also use:
- `--network none` for tools that do not need internet
- a separate container per tool class

## Current Worker Enforcement

The dedicated hands worker currently enforces:
- Docker container execution
- `--read-only`
- `--tmpfs /tmp`
- `--cap-drop=ALL`
- `--security-opt no-new-privileges`
- limited CPU / memory / pids
- writable bind mount only for `/workspace`

This is the baseline security posture for all future MCP-style tool servers too.

## Deterministic-First Rule

For research tasks:

1. Use deterministic search first.
2. Use deterministic page fetch / conversion second.
3. Escalate to browser interaction only when the site actually needs interaction or deterministic tools fail.

Why:
- lower attack surface
- faster
- cheaper
- easier to retry deterministically
- easier to sanitize before prompt exposure

## Secrets Firewall

Secrets live in AWS SSM Parameter Store as `SecureString`.

The worker:
- fetches secrets locally at runtime
- uses them inside the tool/runtime layer
- returns only status or sanitized output to the model

The model never sees raw secrets.

This applies to:
- API keys
- browser session cookies
- bearer tokens
- future dashboard-managed session material

## Human-In-The-Loop

Any destructive or externally committing action must require explicit approval:
- delete
- buy / pay / order
- submit
- send
- overwrite important files
- account mutation
- credential entry

The correct execution pattern is:
1. write `WAITING_APPROVAL` state
2. send Telegram approval prompt
3. resume only after explicit approval

## MCP Usage Rules

If MCP servers are added later:
- run them locally on the worker, not as arbitrary hosted services
- pin versions
- containerize them separately when practical
- mount only the minimum workspace paths they need
- deny internet unless required
- wrap them behind Friday-owned adapters instead of exposing them directly to the model

Official MCP reference servers should be treated as reference-quality components, not automatically as production-safe defaults.

## Logging Rules

- CloudWatch retention stays at one day
- worker logs must avoid raw secrets
- tool output returned to the model must be redacted and truncated
- artifacts should store handles and references where possible, not raw secret material

## Near-Term Implementation Direction

1. Deterministic search/fetch tool wrappers
2. Browser stealth + user-agent rotation
3. Browser session persistence to S3 for resumable logged-in sessions
4. Approval-button flow in Telegram
5. Optional MCP-style isolated tool containers for document conversion and file operations

## Non-Goals

Friday should not:
- install arbitrary tools at runtime
- let the LLM invent raw shell launch templates for privileged helpers
- expose host filesystems, Docker socket, or AWS credential paths to general-purpose tools
