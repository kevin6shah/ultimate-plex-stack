# Friday Capabilities Matrix

This file is the operator-visible capability contract for Friday.

Use it for:
- what Friday is currently allowed to promise
- what is partially working but still caveated
- what is not ready yet
- which missing capability should become the next implementation target

Update this file whenever a capability materially changes state.

## Status Legend

- `Promised`: Friday can claim this as a supported capability.
- `Caveated`: Friday can attempt it, but should not present it as reliable or final.
- `Backlog`: desired capability, not ready to promise.
- `Blocked`: intentionally deferred or blocked on architecture/security work.

## Proof Standard

Do not move an item to `Promised` until it has the right end-to-end proof:

- capability claims should follow user-visible task outcomes, not just lower-level tool smokes
- a one-off fallback success is not enough unless that fallback is the intended product behavior and is clearly reflected in the result
- repeated `failed`, `interrupted`, or stuck `running` jobs for the same use case should keep the capability at `Caveated` or worse until the path is cleaned up

- research/report tasks:
  - real task completed
  - final artifact delivered
- booking/reservation tasks:
  - real availability lookup completed
  - real booking completed where possible without credit-card entry
  - pause/approval behavior validated at the commit step
- account-gated tasks:
  - pause triggered at the right login/sign-up wall
  - resume succeeded after operator input
- connector-backed tasks:
  - connector works in this stack, not just in docs
  - fallback behavior is understood when the connector is unavailable

## Current Capability Matrix

| Capability | Status | Current Path | Current Proof | Main Gaps | Next Upgrade |
| --- | --- | --- | --- | --- | --- |
| General Q&A / short answers | `Promised` | Lambda light path | Existing synchronous behavior | None material | Keep stable |
| Long-running research with file outputs | `Promised` | Dedicated worker + deterministic search/fetch + Browser-use fallback + workspace files | Live PDF/report generation proven | Browser reliability still imperfect on hostile sites | Keep deterministic-first |
| Task controls: `show my tasks`, `what's the status?`, `stop 1` | `Promised` | Control plane + DynamoDB job state | Live-proven | Operator still needs to exercise normal use | Keep stable |
| Pause for missing input and resume from a natural follow-up or `answer: ...` | `Promised` | `paused_for_input` heavy-task flow | Live-proven by Codex and live UX cleanup deployed | Login-wall/sign-up-specific gating is still a separate backlog item | Extend the same substrate into login/account-gated flows |
| File reading/writing inside worker workspace | `Promised` | Workspace tools + MCP-backed file surface in the worker | In-code and exercised by report flows | Official filesystem MCP path is implemented in repo but not yet deployed/validated live | Deploy and validate official filesystem MCP scoped to workspace roots |
| Document conversion to markdown | `Promised` | MarkItDown via workspace tool/MCP | In-code and exercised in stack | Need more real operator tasks | Keep stable |
| Spreadsheet/data artifact generation | `Caveated` | Workspace files + Python/CSV/XLSX-capable environment | Routing support now exists; CSV path is real | No polished first-class spreadsheet workflow yet | Official filesystem MCP + explicit spreadsheet workflow validation |
| Itinerary planning | `Caveated` | Google Maps MCP + deterministic research + report generation | Live maps task proven; itinerary-quality bundling still limited | Needs broader itinerary scenarios and nicer route/POI packaging | Expand around live-proven maps tool path |
| Maps browsing / route planning | `Promised` | `cablate` Google Maps MCP + deterministic research fallback | Live-proven on the dedicated worker | Still narrower than a full consumer map UI | Keep deterministic/API-first and avoid browser map UI drift |
| Restaurant reservation research | `Caveated` | Deterministic search/fetch first, Browser-use fallback | Reservation-heavy tasks route correctly | No vetted installed reservation connector; browser reliability limits remain | Evaluate connector and validate real no-CC bookings |
| Real restaurant booking | `Backlog` | Browser-only fallback path today | Not proven as reliable | Missing connector, missing login-wall flow, missing approval polish | Connector + login-wall pause/resume + live booking validation |
| Flights / hotels / rental cars discovery | `Promised` | Direct Skiplagged MCP travel tools on the dedicated worker; browser is fallback only | Live-proven for flights, hotels, and rental cars in this stack | Need continued monitoring of upstream rate limits / outage windows | Keep structured travel tools primary and tighten fallback behavior |
| True commerce / checkout-style flows | `Backlog` | Browser fallback only | Not proven | Approval, anti-bot, login/account, and payment boundaries not complete | Treat as later-stage booking/commerce program |
| Account-gated workflows | `Backlog` | Pause-for-input substrate exists | Only generic missing-input pause proven | No dedicated login-wall/sign-up gating behavior yet | Implement login-wall gate detection and identity flow |
| Account creation with cached/new identity choice | `Backlog` | Spec only | Documented only | No identity registry, no secure password-entry path, no operator UI | Build identity foundation from spec |
| Agent-owned mailbox / OTP retrieval | `Blocked` | Gmail path intentionally disabled for now | Gmail account was blocked by Google; no live MCP proof | Needs a recovered or replacement mailbox strategy before this can be promised | Continue with pause/resume fallback, then reintroduce headless mail later |
| Reminder management for Friday itself | `Backlog` | One-off infra reminders exist today | Migration reminder exists | No general reminder model or user-facing reminder UX | Add dashboard-backed reminder model after P2 |
| General user reminders | `Backlog` | No current reminder workflow | Not proven | Needs scheduling, timezone UX, persistence, and notification delivery contract | Treat as P3 after dashboard and core agent readiness |

## Explicit Decisions

### Filesystem MCP

Decision:
- Friday should adopt the official broad filesystem MCP server as part of the worker tool surface.

Constraint:
- It must be scoped to allowed worker roots such as the isolated task workspace, not the full host filesystem.

Reason:
- We do not want to discover later that Friday is missing mature file capabilities because we kept a custom narrow MCP surface too long.
- The right tradeoff is broad file capability within bounded roots, not unrestricted host access.

Current repo state:
1. The worker code now mounts the official filesystem MCP server for broad file/directory operations.
2. It is scoped to explicit allowed directories rooted in the worker workspace.
3. MarkItDown and PDF/report helpers remain available through the Friday helper MCP.
4. Live deployment and real-task validation are still pending before this becomes an accepted proof point.

### Browser Strategy

Decision:
- Browser-use remains the interaction fallback, not the universal hands layer.

That means:
- files/spreadsheets should not default to browser work
- maps/travel should prefer connectors or deterministic tools
- booking/commerce should prefer connectors when available
- browser automation is still required for unsupported sites and final interaction steps

## Operator-Editable Capability Backlog

Add items here when you want Friday to learn a new category of work.

- Adopt official filesystem MCP server scoped to worker roots and validate spreadsheet/file-heavy tasks.
- Continue monitoring the live-proven Skiplagged MCP path for flights/hotels/rental-car tasks and keep browser as fallback only.
- Add vetted reservation connector and validate real no-credit-card restaurant bookings.
- Implement login-wall pause/resume and sign-up gating on top of the existing pause substrate.
- Build true account-gated workflow support with cached identity vs new email choice.
- Expand from restaurant booking into broader commerce only after the account/approval path is stable.
- Add reminder support for agent-owned follow-ups and later for normal user reminder requests.
