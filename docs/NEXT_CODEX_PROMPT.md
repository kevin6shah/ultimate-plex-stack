# Next Codex Prompt

Paste this into a fresh Codex chat in this repo:

```md
You are continuing Friday agent work in `/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack`.

Read first:
1. `docs/HANDOFF.md`
2. `docs/FRIDAY_OPERATOR_BOARD.md`
3. `docs/FRIDAY_CAPABILITIES_MATRIX.md`
4. `docs/FRIDAY_MCP_STACK_PLAN.md`
5. `docs/FRIDAY_AGENT.md`
6. `docs/FRIDAY_TOOL_SECURITY.md`
7. `docs/MAINTENANCE_JOURNAL.md`

Current branch:
- `friday-local`

Current architectural state:
- Lambda is the control plane.
- Heavy tasks run on a dedicated on-demand `t3a.small` EC2 worker.
- Primary hands substrate is now:
  - `PydanticAI`
  - deterministic search/fetch first
  - `Browser-use`
  - `playwright-stealth`
  - official filesystem MCP server scoped to worker roots
  - Friday-owned workspace helper MCP server
  - `MarkItDown`
- The older custom selector-centric Playwright path is no longer the intended primary browser architecture.
- The repo now mounts the official filesystem MCP server scoped to worker roots, alongside a Friday-owned workspace helper MCP server, but live deployment/validation is still pending.

What is already live/proven:
- heavy dedicated-worker path
- auto-start / auto-stop worker lifecycle
- interruption / resume
- durable `paused_for_input` plus explicit `answer: ...` resume
- Browser-use-backed camera research that returned a real PDF
- direct long-task control commands:
  - `show my tasks`
  - `what's the status?`
  - `stop 1`
  - `stop <job id>`
- light-path DSML/tool-call leakage is fixed by rerouting/upgrading instead of returning raw markup

Critical learnings to preserve:
- MCP/connectors are part of the hands substrate, but Browser-use is still the interaction fallback; do not treat browser automation as the universal answer.
- MCP/connectors should be treated as a layered isolated estate by trust tier, not as one flat pool of equally trusted tools.
- Do not keep deepening custom browser mechanics if a higher-level substrate is already the agreed direction.
- Use real connectors/apps where they actually exist.
- For common-use routing:
  - flights -> connector
  - hotels -> connector
  - maps / itinerary -> deterministic API or vetted connector first
  - files/docs -> workspace MCP + MarkItDown
  - spreadsheets / data outputs -> local file/spreadsheet tools, not browser-first
  - general web facts -> deterministic search/fetch
  - interactive sites / unsupported flows -> Browser-use
- sign-in / sign-up walls should pause and ask instead of improvising account creation
- Do not pretend restaurant reservations already have a vetted installed connector in this stack. They currently still rely on deterministic search/fetch plus browser fallback.
- `playwright-stealth` helps but does not solve all hostile sites.
- Browser-use is better, but still fails on:
  - Cloudflare / human verification pages
  - retail sites that crash or return blank result regions
  - ugly step screenshots even when the final synthesized report is good
- If you need to deploy, local Docker on this Mac may be unavailable. Recent working fallback was remote Docker build on the dedicated worker host, then `aws lambda update-function-code` with the new ECR image.

What still needs work:
1. Finish operator validation of the shipped pause-for-input / resume flow and screenshot-delivery behavior.
2. Build the explicit hybrid tool-routing layer for spreadsheets, itinerary/maps, and booking/reservations so Friday stays deterministic-first.
3. Adopt and validate the official filesystem MCP server scoped to allowed worker roots.
4. Build the first tiered MCP isolation harnesses for filesystem, helper, read-only network, and sensitive booking/identity tools.
5. Review and selectively add real MCP/app connectors for common use cases only after security review.
6. Implement login-wall / sign-up gating on top of the pause-for-input substrate.
7. Improve Browser-use behavior on hostile domains.
8. Decide whether to add Browser Use Cloud and/or optional CAPTCHA support.
9. Move higher-risk tool classes toward per-tool container isolation, not only the shared worker-container boundary.

How to behave:
- Do not redo the old browser-hardening detour unless it is directly necessary for a current bug.
- Prefer shipping the agreed substrate direction over polishing superseded layers.
- Do not assume externally suggested MCP server names or model rankings are automatically correct; verify them before promoting them into the architecture.
- Keep docs current as you go so the next Codex instance does not lose time re-deriving the same conclusions.
```
