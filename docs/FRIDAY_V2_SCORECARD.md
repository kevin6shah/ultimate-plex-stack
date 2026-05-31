# Friday V2 Scorecard

Last updated: 2026-05-31

## Scoring
- Routing and follow-up attribution: 5/5
- Durability and auto-recovery: 4/5
- Cleanup and cost control: 4/5
- Communication UX: 4/5
- Memory and context continuity: 4/5
- Parallel and isolated work behavior: 4/5

Weighted score: 85/100

## Live evidence
- Deploys completed:
  - Lambda/control plane: `v2-20260530-052200`, then `v2fix-20260530-060106`
  - Shared Temporal host: refreshed after each control-plane deploy
  - Dedicated activity worker: refreshed directly on `3.87.35.135`
- Live Siri validation on 2026-05-30:
  - Heavy flight task queued normally
  - Follow-up modifier stayed on the same `job_id`
  - Orthogonal Siri question answered directly without hijacking the heavy task
  - Validation cleanup restored the original Siri and Telegram thread state and left `active_jobs: []`
- Additional live validation on 2026-05-30:
  - Running Siri follow-up no longer collapsed immediately into `interrupted`
  - The same live flight task stayed attached to the original `job_id` and carried the updated query override through Temporal
  - `api_direct` retry state remained durable across the workflow handoff
  - Validation cleanup again left `active_jobs: []`
- Additional live validation on 2026-05-31:
  - Cross-domain heavy-task contamination was reproduced from your real Telegram/Siri thread state and then covered with explicit regressions.
  - The bad active Telegram heavy job was stopped and cleared after the fix.
  - `Present your findings now` on a live travel run now returns a real degradation summary once the provider failure is recorded:
    - `The structured provider lane failed while checking live flight options, so I switched to the next approach.`
  - The dedicated worker was refreshed multiple times during this pass and ended clean with `active_jobs: []`.
  - A real Stagehand runtime bug was found and fixed live:
    - Stagehand session start was failing with `400`
    - cause: unsupported `browser.launchOptions.env`
    - result: that field was removed from the Stagehand payload
- Additional live validation on 2026-05-31 after the stalled-findings deploy:
  - A fresh Siri flight run returned a useful partial result on `Present your findings now` before final completion:
    - a real flexible-date fare calendar for `NYC -> DEL`
    - concrete departure dates and prices instead of raw progress text
  - The findings selector no longer surfaced the low-signal `IATA resolution` setup artifact when better live travel results were already persisted.
  - The validation job was stopped and cleanup again left `active_jobs: []`.
- Additional live validation on 2026-05-31 for task isolation:
  - A running Siri heavy flight task stayed active while an orthogonal light Siri question (`What time is it in Tokyo?`) returned an immediate direct answer.
  - A same-thread cross-domain heavy request (`Create an account with a free trial for Willow TV.`) superseded the flight task cleanly:
    - the old flight `job_id` was replaced
    - the new active job query was the Willow TV task only
    - no flight status/findings leaked into the new task
  - Validation cleanup again left `active_jobs: []`.

## What improved
- Non-booking heavy tasks no longer hit the generic mutation-confirmation wall.
- Running-task follow-ups can stay on the same live job.
- Siri and Telegram no longer leak raw `Step:` and `Update:` formatting in the tested status and findings replies after the `v2fix` deploy.
- Validation cleanup now has a proven restore path for Siri and Telegram thread state.
- Running follow-ups now survive the activity-cancel race and continue on the same Temporal job instead of immediately finalizing as interrupted.
- Live heavy-task context continuity is better because the updated follow-up query is durably carried into the resumed claim path.
- Local validation now explicitly covers cross-domain thread contamination and stale-summary leakage instead of only same-domain follow-up cases.
- Same-thread heavy domain shifts can now supersede the old task instead of silently appending to it.
- Status and findings snapshots now ignore persisted summaries that clearly belong to a different task domain.
- Structured travel degradation now persists a usable operator-facing summary instead of only raw provider error text.
- The Stagehand fallback lane no longer fails immediately on the invalid launch payload that was blocking browser fallback.
- Interrupted travel findings are materially better when the failure state has already been reached in the workflow.
- Interrupted travel findings are now better even earlier in the run because low-signal setup artifacts no longer override persisted live fare results.
- Same-thread isolated work behavior now has live proof for both:
  - orthogonal light work during a running heavy task
  - cross-domain heavy-task supersession without contamination

## What is still broken
- Long-running travel work still degrades too often on Skiplagged before producing actual itinerary candidates.
- `Present your findings now` is materially better now, but it still needs broader live proof across more degraded travel/provider combinations.
- Telegram rich formatting is partially improved, but the broader conversation polish still is not at the target bar.
- Parallel-task isolation now has the core live proof it was missing, but broader multi-domain breadth is still lighter than routing/durability coverage.

## Root causes confirmed live
- The validated flight run degraded on the worker through the travel MCP path:
  - Skiplagged MCP hit Cloudflare `1015` rate limiting
  - then hit Cloudflare `502` origin errors
- This means the interruption is not only copy-layer UX. The agent is still missing a strong enough provider-fallback path for travel when the first MCP lane degrades.
- A second separate runtime bug was confirmed and fixed:
  - Stagehand fallback was failing before it could help
  - the request payload included an unsupported `launchOptions.env` field
  - after removing that field, the Stagehand lane stopped failing on request validation
- A separate workflow bug was confirmed and fixed:
  - follow-up signals could cancel the running activity
  - the workflow was treating `Activity cancelled` as terminal instead of recovering the pending follow-up
  - the live fix now keeps the same job running through that follow-up path
- The current remaining findings gap is narrower:
  - strategy retry state survives
  - partial findings are now useful for the validated Siri flight case
  - but broader provider degradation paths still need more live proof

## Current top bugs
1. Travel MCP degradation still occurs too early and too often, so the system needs a stronger provider fallback ladder before browser escalation.
2. Telegram rich formatting and broader conversation polish still are not at the target bar.
3. Booking/account mutation flows still need the same level of live proof that discovery and travel have now received.
4. Travel provider breadth still needs more fallback diversity beyond Skiplagged degradation.

## Next queue
1. Add a stronger travel fallback ladder after Skiplagged degradation instead of leaning so hard on one provider path.
2. Expand live validation to restaurants and OTP/account continuity.
3. Improve Telegram output formatting and conversation polish without regressing the reliability work.
4. Keep `docs/FRIDAY_V2_VALIDATION_MATRIX.md` current whenever a new live failure class is found.

## Acceptance bar for V2 handoff
- Weighted score at least `85/100`
- No category below `4/5`
- No generic mutation blocker on non-booking work
- Follow-ups, resumability, and cleanup proven live
- Interrupted tasks surface useful current findings without babysitting
