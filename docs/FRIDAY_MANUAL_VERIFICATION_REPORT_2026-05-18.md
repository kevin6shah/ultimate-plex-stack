# Friday Manual Verification Report

Date: 2026-05-18 / 2026-05-19

This report captures what the operator actually saw in live Telegram/Siri use, not just what lower-level tools or isolated worker tests did.

## What Worked

- Siri travel improved materially:
  - flights returned usable answers
  - hotels returned usable answers
  - rental cars eventually returned usable answers
- Browser-heavy research improved enough to complete a live Comedy Cellar query with real lineup data.
- The dedicated worker path is the right heavy-task architecture and is now the only approved place for Friday heavy work.
- Default artifact spam is better than before:
  - browser screenshots were no longer visibly dumped into the thread during the later Comedy Cellar run

## Main Failures Seen In Manual Verification

### 1. False confirmation / spend gate on harmless research

Observed live:
- Ibiza hotel search triggered:
  - `This task may change data, send information, or spend money. Reply with explicit confirmation and the exact action you want me to take.`
- A pure architecture / tooling question later triggered the same incorrect warning:
  - `Is temporal free to use?...`

Why this is bad:
- It makes basic research feel blocked and untrustworthy.
- It trains the operator to ignore a safety prompt that should only appear for real risky actions.

What needs to happen:
- Narrow approval gating so it only fires on real side-effecting actions:
  - booking submit
  - account creation submit
  - payment
  - message send
  - irreversible form submission
- Add regression tests for terms like:
  - `check out`
  - `checkout`
  - `book later`
  - `research`
  - `is Temporal free`

### 2. Natural-language stop/cancel did not work reliably

Observed live:
- After the Comedy Cellar task:
  - user: `Cancel this task`
  - Friday: `I could not tell which running task to stop...`
- Then:
  - user: `Stop all`
  - Friday again failed to stop correctly.

Later Telegram verification improved, but another failure remained:
- user: `Any findings? Stop the agent and reveal the findings`
- Friday replied with a confused mixed message:
  - `Let me stop that task and share what it found.`
  - `stop 1`
  - `I'll reveal the findings now — can you remind me which task/agent this was working on?`

Why this is bad:
- Stop/cancel is a core operator control.
- Mixed “I’m stopping it / I’m asking you again” behavior feels sloppy and unsafe.

What needs to happen:
- Treat natural stop utterances as first-class control intents.
- Support:
  - `cancel this task`
  - `stop all`
  - `stop the agent`
  - `stop and show me what it found`
- When partial findings exist, return them deterministically after interruption instead of asking the operator to restate context.

### 3. Long-running tasks can still appear stuck without a satisfying finish

Observed live:
- The Telegram Vik-hotel-deals task showed:
  - `running`
  - `working through website steps`
  - then needed manual stop
  - then ended with `I hit an internal error before the task finished.`
- Separate Siri/worker runs also sat in `attachments_ready` for too long before cleanup.

Why this is bad:
- The operator perceives “the agent just kept working but never finished.”
- A later failure string does not make up for poor progress semantics during the run.

What needs to happen:
- Add a stronger stall detector on:
  - no heartbeat advancement
  - no checkpoint progression
  - container alive but no meaningful state change
- Distinguish:
  - actively progressing
  - waiting on slow website
  - stalled / likely hung
- Auto-retry once on safe read-only tasks before surfacing failure.
- Improve final failure wording so it says what happened in human terms:
  - `This run got stuck while checking live hotel sites and did not finish cleanly.`

### 4. Restaurant correctness is still too weak

Observed live:
- `Help me make a dinner reservation for 2 at bungalow nyc`
- Friday confidently assumed the wrong restaurant:
  - `Bowery Bungalow NYC in SoHo`
- Even after clarification:
  - `bungalow the Indian restaurant`
- Friday still responded:
  - `The Indan restaurant Bungalow in NYC is Bowery Bungalow in SoHo.`

Only after a Google Maps link did it recover to the correct restaurant.

Why this is bad:
- This is a trust-breaking hallucination in a core assistant use case.
- It shows the system is willing to confidently anchor on the wrong entity instead of clarifying or checking first.

What needs to happen:
- Add restaurant entity resolution before reservation search:
  - name normalization
  - borough/neighborhood match
  - cuisine cross-check
  - source-of-truth lookup before claiming identity
- If ambiguity remains, ask one short clarification question instead of guessing.
- Add regression coverage for:
  - `Bungalow`
  - similarly ambiguous restaurant names

### 5. Research answers sometimes look authoritative without enough grounding

Observed live:
- `Who said ve kamleya` got a confident but likely incorrect answer.
- `What are your capabilities` returned a generic capability list that overstates some current reliability.
- `What do you understand about our current architecture` returned an incomplete and partly wrong summary.
- `surprise celebrity in Manhattan tonight` first failed with:
  - `Received empty model response`
  - then returned a thin answer after rerun.

Why this is bad:
- Friday should not bluff when the operator is explicitly asking for live or architectural truth.
- Capability claims should match the real proven surface, not aspirational behavior.

What needs to happen:
- Tighten source-grounding rules for:
  - live event/news/trend questions
  - architecture questions about Friday itself
  - capability explanations
- Prefer:
  - repo docs for Friday architecture
  - direct live web checks for current-event claims
- Add a “don’t know yet / need to check” path instead of generic filler.

### 6. User-facing messaging is still too meta in places

Observed live:
- phrases like:
  - `Now I have a clear picture...`
  - `Here's the full analysis...`
  - `Let me now compile all the information...`
  - `The page is dynamic/JS rendered...`
- raw failures like:
  - `Received empty model response`
  - `I hit an internal error before the task finished.`

Why this is bad:
- It reads like an agent narrating its prompt, not like a polished assistant.

What needs to happen:
- Continue stripping meta-openers and internal implementation language.
- Replace raw internal failures with clean human-readable outcomes.
- Keep answers concise and decisive by default.

## Highest-Priority Fixes Before Calling Friday “Stable”

1. Fix approval-gate false positives for harmless research and planning.
2. Make stop/cancel/reveal-findings work naturally in Telegram and Siri.
3. Add true stall detection plus one safe automatic retry for read-only long tasks.
4. Add restaurant entity resolution so Friday stops confidently guessing the wrong place.
5. Tighten grounded-answer rules for live research, architecture, and capability questions.
6. Keep cleaning user-facing wording until internal/meta phrasing is gone.

## Suggested Acceptance Criteria For The Next Manual Pass

- `Cancel this task` stops the currently running task without needing `show tasks`.
- `Stop all` works.
- `Stop the agent and show me what it found` either:
  - returns partial findings, or
  - says clearly that nothing usable was gathered yet.
- Harmless hotel/travel/research questions never trigger the spend/send confirmation prompt.
- Ambiguous restaurant names cause either:
  - correct resolution, or
  - one short clarification question.
- A stuck long-running task is surfaced as stalled and either:
  - retried automatically once, or
  - failed with a human-readable reason.
- No raw `empty model response`, `internal error`, `JS rendered`, or similar internal phrasing reaches the user.
