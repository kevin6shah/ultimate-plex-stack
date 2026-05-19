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

The target tiered-MCP architecture is documented in `docs/FRIDAY_MCP_STACK_PLAN.md`, with the first harness scaffold in `ops/mcp/docker-compose.trust-tiers.yml`.

## Current MCP Usage

Friday now uses:
- the official filesystem MCP server for broad file and directory operations inside allowed worker roots
- a Friday-owned helper MCP server for preview, markdown conversion, and PDF generation

Constraint:
- broad file capability is allowed only inside explicit worker roots, not the full host filesystem.

Current pattern:
- Browser-use remains the primary browser/computer-use loop
- Browser-use built-in file actions are excluded where Friday has safer equivalents
- the official filesystem MCP handles broad file/directory operations
- a Friday-owned stdio helper MCP exposes:
  - preview workspace file
  - convert workspace file to markdown
  - write workspace PDF report

This keeps file operations inside a bounded worker-root scope while still giving Friday a broader, production-grade filesystem surface.

Common-use MCP/app policy:
- prefer real connectors for categories that have first-class tool support, such as flights and hotels
- do not treat generic browser automation as equivalent to a reservation/booking connector when none is installed
- if no production-worthy connector exists for a common use case, Friday should use deterministic search/fetch first and only escalate to browser interaction when needed
- treat named third-party MCP servers from external recommendations as candidates, not approvals; they still need maintenance, security, and real-task review in this stack

Current repo-wired candidate classes behind settings/secrets:
- L2:
  - Firecrawl MCP
  - cablate Google Maps MCP
  - Google Maps / Places / Routes via OpenAPI MCP
- L4:
  - Resy MCP path with session-token secrets
  - OpenTable MCP path with credential secrets
  - Gmail IMAP/SMTP MCP candidate for the dedicated Friday mailbox

Current limitation:
- these MCP tools currently run inside the already-isolated worker container, not yet in separate per-tool containers
- per-tool container isolation remains the next hardening step for higher-risk tool classes
- the long-term target should not leave the broad filesystem MCP in the same effective trust boundary as the browser runtime

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

Common-use routing targets:

- spreadsheets / data export:
  - prefer local structured file tools and workspace MCP
  - do not use a browser unless the source itself is browser-only
- itinerary / maps / travel lookup:
  - prefer deterministic APIs/connectors first
  - use browser interaction only for unsupported flows or final confirmation steps
- reservations / commerce:
  - prefer a vetted connector only if one is actually installed and approved
  - otherwise use deterministic fetch/search for availability research and reserve Browser-use for the interaction step
- sign-in / sign-up walls:
  - do not improvise autonomous account creation
  - pause for input and/or approval at the decision point

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

For non-destructive heavy tasks that are blocked on missing user input, the correct pattern is:
1. write `PAUSED_FOR_INPUT` state with a checkpoint and resume instructions
2. ask only for the missing answer/attachment
3. resume from the saved checkpoint only after that input arrives

For account creation / sign-up gates:
1. pause at the sign-in/sign-up decision point
2. ask whether to use a cached identity or a new operator-provided email
3. require explicit approval before the account-creation submit step
4. keep password entry outside the model prompt path and inside a secure operator surface backed by SSM or equivalent

For mailbox tools specifically:
1. prefer a headless IMAP/SMTP MCP plus Gmail App Password over desktop-browser OAuth flows inside the worker
2. use the dedicated Friday mailbox only, not the operator's primary inbox
3. keep outbound send-email capability disabled or approval-gated by default

## MCP Usage Rules

If MCP servers are added later:
- run them locally on the worker, not as arbitrary hosted services
- pin versions
- containerize them separately when practical
- mount only the minimum workspace paths they need
- deny internet unless required
- wrap them behind Friday-owned adapters instead of exposing them directly to the model

Official MCP reference servers should be treated as reference-quality components, not automatically as production-safe defaults.

Connector/MCP evaluation policy:
- add multiple candidates where the ecosystem is still unsettled
- compare output quality, failure modes, and fallback behavior before choosing a primary
- keep trust-tier isolation in place instead of giving all candidates identical access

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
