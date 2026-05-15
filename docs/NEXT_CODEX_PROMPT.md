# Next Codex Prompt

Paste this into a fresh Codex chat in this repo:

```md
You are continuing Friday agent work in `/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack`.

Read first:
1. `docs/HANDOFF.md`
2. `docs/FRIDAY_AGENT.md`
3. `docs/FRIDAY_TOOL_SECURITY.md`
4. `docs/MAINTENANCE_JOURNAL.md`

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
  - Friday-owned local workspace MCP server
  - `MarkItDown`
- The older custom selector-centric Playwright path is no longer the intended primary browser architecture.

What is already live/proven:
- heavy dedicated-worker path
- auto-start / auto-stop worker lifecycle
- interruption / resume
- Browser-use-backed camera research that returned a real PDF
- direct long-task control commands:
  - `show my tasks`
  - `what's the status?`
  - `stop 1`
  - `stop <job id>`
- light-path DSML/tool-call leakage is fixed by rerouting/upgrading instead of returning raw markup

Critical learnings to preserve:
- Do not keep deepening custom browser mechanics if a higher-level substrate is already the agreed direction.
- Use real connectors/apps where they actually exist.
- For common-use routing:
  - flights -> connector
  - hotels -> connector
  - files/docs -> workspace MCP + MarkItDown
  - general web facts -> deterministic search/fetch
  - interactive sites / unsupported flows -> Browser-use
- Do not pretend restaurant reservations already have a vetted installed connector in this stack. They currently still rely on deterministic search/fetch plus browser fallback.
- `playwright-stealth` helps but does not solve all hostile sites.
- Browser-use is better, but still fails on:
  - Cloudflare / human verification pages
  - retail sites that crash or return blank result regions
  - ugly step screenshots even when the final synthesized report is good
- If you need to deploy, local Docker on this Mac may be unavailable. Recent working fallback was remote Docker build on the dedicated worker host, then `aws lambda update-function-code` with the new ECR image.

What still needs work:
1. Implement durable pause-for-input / human-in-the-loop resume instead of failing when the agent needs user input.
2. Review and selectively add real MCP/app connectors for common use cases only after security review.
3. Improve Browser-use behavior on hostile domains.
4. Decide whether to add Browser Use Cloud and/or optional CAPTCHA support.
5. Move higher-risk tool classes toward per-tool container isolation, not only the shared worker-container boundary.

How to behave:
- Do not redo the old browser-hardening detour unless it is directly necessary for a current bug.
- Prefer shipping the agreed substrate direction over polishing superseded layers.
- Keep docs current as you go so the next Codex instance does not lose time re-deriving the same conclusions.
```
