# Friday Operator Board

This file is operator-editable working state for Friday agent work.

Use it for:
- current task focus
- accepted backlog
- operator preferences
- completed items worth preserving between sessions

Future Codex/Friday sessions should read this early and keep it current.

## Current Focus

- P0 next: close the manual-verification gaps captured in `docs/FRIDAY_MANUAL_VERIFICATION_REPORT_2026-05-18.md` before calling Friday stable.
- P1 next: extend the now-proven travel and pause/resume substrate into restaurant/account-gated flows instead of adding more browser-first hacks.
- Use `docs/FRIDAY_CAPABILITIES_MATRIX.md` as the capability contract and backlog for what Friday is allowed to promise.
- Use `docs/FRIDAY_MCP_STACK_PLAN.md` as the MCP/connector evaluation list and trust-tier isolation plan.
- Use `docs/FRIDAY_AWS_SYSTEM_DESIGN.md` as the architecture source of truth before making any AWS or worker-topology decision.
- Routing target for common-use work:
  - spreadsheets / data outputs -> filesystem MCP + local file/spreadsheet tools + MarkItDown where needed
  - itineraries / maps / general web facts -> deterministic APIs/connectors or deterministic search/fetch first
  - booking / reservations / commerce -> vetted connector if one is actually installed and approved, otherwise deterministic fetch first, then Stagehand, then Browser-use only as the last browser fallback
  - login walls / sign-up pages -> pause and ask instead of improvising account creation
- Keep the dedicated on-demand Friday worker as the default heavy-task execution surface; do not fall back to the shared Iris/VPN host for rebuilds or runtime patching.
- Finish one clean end-to-end validation of the new screenshot-delivery defaults on a completed browser-heavy task.

## Operator Preferences

- Do not send browser step screenshots to Telegram by default.
- Do not improvise Friday heavy-worker rebuilds onto the shared Iris/VPN host without explicit operator approval after cost review.
- Only send browser screenshots when the request explicitly asks for screenshots/images.
- If multiple browser screenshots are explicitly requested, send them as one zip instead of many separate Telegram uploads.
- Keep durable repo-visible notes for backlog, active work, and recent completions here.
- Use `friday.nyc.agent@gmail.com` as Friday's default agent-owned mailbox identity for new account creation, verification emails, OTP retrieval, and booking-related confirmations unless the operator explicitly overrides it.

## Accepted Backlog

- Fix approval-gate false positives for harmless research/planning prompts:
  - hotel searches with `checkout`
  - architecture/tooling questions
  - other non-side-effecting queries that currently trip the spend/send confirmation gate
- Make natural-language task control reliable:
  - `cancel this task`
  - `stop all`
  - `stop the agent`
  - `stop and show me what it found`
- Add long-task stall detection plus one safe retry for read-only tasks so work does not appear to run forever and then end with a vague failure.
- Add restaurant entity-resolution before availability/booking logic so Friday stops confidently guessing the wrong restaurant identity.
- Tighten source-grounded answer rules for:
  - live event/news/trend questions
  - Friday architecture questions
  - capability explanations
- Keep improving user-facing message cleanup until internal/meta phrases and raw internal failures no longer leak into Telegram/Siri.
- Adopt `Temporal + Stagehand + keep current Friday interfaces` as the current preferred v2 architecture direction:
  - keep Telegram and Siri as the operator-facing surfaces
  - keep Friday's current control semantics such as status, stop, pause, approval, and notifications
  - introduce a durable execution backbone so retries, waiting, reminders, approvals, and human-in-the-loop pauses are first-class instead of ad hoc queue/state glue
  - evaluate Temporal as the durable workflow substrate
  - evaluate Stagehand as the browser interaction substrate for hostile or dynamic sites
  - copy the useful resilience patterns from OpenClaw and Nanoclaw-style runtimes without rewriting Friday into a monolithic always-on agent platform
- Tighten login-wall / verification pause handling on top of the now-live `paused_for_input` behavior.
- Deploy and validate the official broad filesystem MCP server in the worker, scoped to allowed workspace roots, on real file/spreadsheet tasks.
- Build the tiered MCP isolation harnesses so Browser-use, filesystem, read-only network MCPs, and sensitive booking/identity MCPs do not all share the same blast radius.
- Build the explicit common-use tool-routing layer so Friday does not open a browser for tasks that should be handled by deterministic tools:
  - spreadsheets / structured data export
  - itinerary planning and map lookups
  - flights / hotels / rental cars via Skiplagged MCP
  - restaurant reservations via a vetted connector only if there is a real runnable source artifact and it passes validation in this stack
- Add and compare the currently selected common-use candidates:
  - Skiplagged MCP
  - cablate Google Maps MCP
  - Google Maps / Places / Routes via OpenAPI MCP
  - dedicated Friday mailbox via Gmail/email MCP
- Add account identity / sign-up gating flow:
  - pause at sign-in/sign-up gates during booking/commerce tasks
  - ask whether to use a cached identity or a new email
  - support operator-provided `Hide My Email` addresses
  - support a dedicated Friday-owned mailbox identity
  - create secure password-entry placeholders for dashboard-backed SSM storage
  - require explicit approval before account creation submits
- Replace the OAuth-style Gmail MCP path with a headless IMAP/SMTP Gmail MCP using the dedicated Friday mailbox plus a Gmail App Password.
- If the dedicated Friday mailbox is blocked or unavailable, continue booking/account work with Gmail disabled and fall back to pause-for-input plus operator-provided email/verification steps.
- Add login-wall pause/resume handling on top of the new `paused_for_input` substrate before attempting autonomous account creation.
- Keep Gmail disabled until a safer mailbox strategy is ready; use operator-assisted pause/resume for email/OTP gates in the meantime.
- Improve Browser-use behavior on hostile domains without drifting back into the old selector-hardening detour.
- Finish the current Stagehand migration and validate it live as the primary interactive browser fallback ahead of Browser-use.
- Review common-use connectors only after security review and only where they materially beat deterministic search/fetch plus browser fallback.
- Evaluate deterministic web extraction/search upgrades only if they fit Friday's current security and deployment model.
- Evaluate whether optional Browser Use Cloud and/or CAPTCHA support is worth the additional risk/complexity.
- Move higher-risk tool classes toward per-tool container isolation.
- Do not adopt `musemen-resy-mcp-server` from the current LobeHub listing unless a real upstream source artifact appears.
  - The current LobeHub page points to a GitHub repo that 404s.
  - Its `skill.md` export is a marketplace template, not a sufficient install source.
- Add reminder management after dashboard work:
  - Friday should manage agent-related reminders and expiring setup tasks by default
  - later support general user reminders like `remind me tomorrow about this`
  - treat this as P3 after dashboard, not current P1/P2 work

## Candidate Integrations To Evaluate, Not Assume

- Maps / itinerary: only adopt if the connector or MCP is actively maintained, security-reviewable, and materially better than current deterministic wrappers.
- Reservations: do not assume any specific OpenTable/Resy MCP is production-worthy until it is reviewed and tested in this stack.
- Web extraction: hosted Firecrawl-style services may help, but they are candidates, not committed architecture.
- Temporary/burner identity services are not approved by default and must go through the same security review as any other connector.
- Gmail/email MCPs are part of the selected evaluation set, but the exact server choice should be based on real implementation quality, not on unverified claims of "official" status.
- Add multiple candidates where uncertainty is high; compare them instead of forcing a premature single winner.

## Recently Completed

- Added `docs/FRIDAY_MANUAL_VERIFICATION_REPORT_2026-05-18.md` to capture exact live Telegram/Siri failures from operator manual verification, including false approval prompts, weak stop UX, wrong restaurant identity resolution, stale/stuck long-task behavior, and overconfident research answers.
- Added durable `paused_for_input` state and explicit resume flow for heavy tasks.
- Suppressed Browser-use step screenshots from default Telegram artifact delivery.
- Added zip bundling for explicitly requested multi-screenshot deliveries.
- Deployed the new Lambda + dedicated-worker runtime and proved live `paused_for_input` plus `answer: ...` resume behavior.
- Added a first routing-profile pass in code for spreadsheet/data, itinerary/maps, booking/commerce, and login/account tasks so the heavy prompt can steer toward the right tool class before browser fallback.
- Added `docs/FRIDAY_CAPABILITIES_MATRIX.md` as the operator-visible capability contract and backlog.
- Added `docs/FRIDAY_MCP_STACK_PLAN.md` plus `ops/mcp/docker-compose.trust-tiers.yml` to capture the tiered isolation harness direction for MCPs/connectors.
- Wired Firecrawl, cablate Google Maps, Google Maps OpenAPI, Resy, OpenTable, and Gmail MCP candidate hooks into the repo worker path behind settings/secrets, pending deployment and live validation.
- Added Skiplagged MCP runtime wiring through `mcp-remote` so flights/hotels/rental cars can move off brittle browser-first flows.
- Restored the proper dedicated Friday on-demand worker and proved live Skiplagged flights, hotels, and rental cars through the Siri -> worker path.
- Deployed the cleaned-up paused-input/status UX so blocked tasks now ask for missing details in readable sections instead of raw/internal-looking status text.
- Added the first local Stagehand integration path in code:
  - deterministic public-web read still runs first
  - Stagehand now sits ahead of Browser-use in the interactive browser fallback order
  - Browser-use remains the last-resort browser fallback instead of the default interactive lane

## Notes

- Keep this file short and high signal. It is the operator-facing scratchpad, not the full historical log.
- Durable historical detail still belongs in `docs/MAINTENANCE_JOURNAL.md`.
