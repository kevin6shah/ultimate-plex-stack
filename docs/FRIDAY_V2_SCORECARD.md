# Friday V2 Scorecard

Last updated: 2026-05-31

## Scoring
- Routing and follow-up attribution: 5/5
- Durability and auto-recovery: 3/5
- Cleanup and cost control: 4/5
- Communication UX: 3/5
- Memory and context continuity: 4/5
- Parallel and isolated work behavior: 3/5

Weighted score: 72/100

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

## What is still broken
- Long-running travel work still degrades too often before producing user-useful findings.
- `Present your findings now` is cleaner, but still often returns only progress state instead of useful partial findings.
- Status replies are cleaner now, but the interrupted-findings path still needs stronger durable artifacts from the worker.
- Telegram rich formatting is partially improved, but the broader conversation polish still is not at the target bar.
- The updated routing/status fixes were validated locally on 2026-05-31, but still need the next isolated live replay before the score moves again.

## Root causes confirmed live
- The validated flight run degraded on the worker through the travel MCP path:
  - Skiplagged MCP hit Cloudflare `1015` rate limiting
  - then hit Cloudflare `502` origin errors
- This means the interruption is not only copy-layer UX. The agent is still missing a strong enough provider-fallback path for travel when the first MCP lane degrades.
- A separate workflow bug was confirmed and fixed:
  - follow-up signals could cancel the running activity
  - the workflow was treating `Activity cancelled` as terminal instead of recovering the pending follow-up
  - the live fix now keeps the same job running through that follow-up path
- The current remaining findings bug is narrower:
  - strategy retry state survives
  - but the worker still does not reliably leave behind a strong enough `latest_findings_summary` artifact before an operator findings request

## Current top bugs
1. Travel MCP degradation still does not leave durable operator-useful findings early enough in the run.
2. Partial-findings persistence is still too weak, so interrupted research often has little useful material to present.
3. Status/finding summaries still over-report internal progress states instead of operator-useful outcomes.
4. There is still too much dependence on request-time reconciliation instead of stronger autonomous background convergence.

## Next queue
1. Force durable findings writes before every strategy retry so operator findings requests never fall back to generic progress text.
2. Add a travel fallback ladder after MCP degradation instead of treating the first provider failure as near-terminal.
3. Tighten interruption semantics so recoverable provider failures stay inside the workflow instead of surfacing as user-visible interruption.
4. Expand the live validation matrix to restaurants, OTP/account continuity, and explicit parallel-task isolation.
5. Keep `docs/FRIDAY_V2_VALIDATION_MATRIX.md` current whenever a new live failure class is found.

## Acceptance bar for V2 handoff
- Weighted score at least `85/100`
- No category below `4/5`
- No generic mutation blocker on non-booking work
- Follow-ups, resumability, and cleanup proven live
- Interrupted tasks surface useful current findings without babysitting
