# Friday Audit

Date: `2026-06-15`

## Scope

This audit used the data that is still available in the current system:

- recent raw Siri and Telegram thread turns from DynamoDB
- durable heavy-job metadata from DynamoDB over the last `21` days
- live AWS budget data
- live AWS infrastructure inventory

What is not available anymore:

- full raw Siri/Telegram transcripts older than about `48 hours`
- Lambda request logs older than `1 day`

That is an infrastructure gap by itself. It means we can audit behavior, but we cannot reconstruct every literal user/agent message over the last 3 weeks from the current retention settings.

## Data Sources Used

- DynamoDB thread store: `friday-agent-state`
- DynamoDB heavy-job metadata via `GSI1`
- AWS Budget: `My Zero-Spend Budget`
- Live stacks:
  - `friday-shared-host`
  - `friday-hands-worker`
  - `friday-hands-worker-paid`

## Executive Summary

The earlier `85/100` reliability score is no longer an honest current score.

The latest evidence shows that the substrate improvements from late May were real, but the production UX still has multiple serious gaps:

1. task ownership is still brittle after completed work
2. Siri and Telegram stop semantics are still inconsistent
3. internal tool/browser markup can still leak into Telegram
4. long-running work still overuses generic progress copy
5. cost modeling in the repo drifted away from the live AWS footprint

## Current Shortcomings

### 1. Completed-task follow-up contamination still exists

Recent live Telegram evidence on `2026-06-15` showed a new job with this query shape:

- prior task: `Find me some GTA 5 cheats for PS4 and create a pdf of it`
- later user request: Comedy Cellar lineup lookup
- persisted heavy-job query:
  - `Find me some GTA 5 cheats for PS4 and create a pdf of it Continue the same task using the user's new follow-up ... New user direction: Yeah start working on that`

The result_preview for that contaminated job returned the Comedy Cellar lineup.

Interpretation:

- the follow-up classifier is still over-attaching after task completion
- the system is still willing to reuse the wrong completed task context

### 2. Siri-owned heavy work is still not cleanly stoppable from Telegram

Recent live Telegram turns on `2026-06-15`:

- `Okay stop this task`
- reply: `I could not tell which task to stop. Ask me to list your tasks, then say something like 'stop 1' or 'stop <job id>'.`
- then `Stop all`
- reply: `I do not see any active long-running tasks to stop right now.`

At the same time, the mirrored Siri flight task had been queued to Telegram.

Interpretation:

- task ownership across Siri and Telegram is still not consistent enough for operator controls
- stop commands are less capable than the mirroring model implies

### 3. Internal browser/tool markup is still leaking into Telegram

Recent Telegram assistant turn on `2026-06-15`:

```text
<input type="text" value="" />Let me browse the Comedy Cellar schedule page to see if the lineup is listed.

<web_browser_task>
  Navigate to https://comedycellar.com/
</web_browser_task>
```

Interpretation:

- user-facing message sanitization is still incomplete
- the renderer is still exposing internal control/UI/task markup

### 4. The agent still falls back to weak “general knowledge” answers when live search degrades

Recent Siri thread on `2026-06-15`:

- user asked for cheapest flights to Chicago next weekend with a free carry-on
- assistant first asked for the missing departure city
- later the task completed with:
  - `Let me use what I know from the JetBlue data and general knowledge...`
  - an approximate `NYC -> Chicago` answer

Interpretation:

- the system is still too willing to degrade from live search into approximate knowledge answers
- it also inferred or reused origin context in a way that was not cleanly surfaced to the user

### 5. Progress messaging is still too generic in the durable job record

Across the last `21` days of heavy-job metadata:

- `88` Siri/Telegram heavy jobs were observed in the retained corpus
- `76` ended `interrupted`
- `6` ended `failed`
- `6` ended `completed`

Important caveat:

- this corpus includes manual validation traffic, so it is not a clean user-success metric
- but it is still valid evidence for the kinds of failure modes the system produces

Repeated progress/status text still appears heavily:

- restaurant generic progress: `49` jobs
- flight generic progress: `18` jobs

Most common repeated internal summaries:

- `checking reservation sources and matching the correct venue`
- `checking live flight options and collecting candidate itineraries`

Interpretation:

- even after the May fixes, long-running work is still too dependent on repetitive generic progress states
- the system still spends too long in “running agent” without yielding a user-useful outcome

### 6. Restaurant flows are still the weakest lane

Repeated retained failures include:

- Midtown/Angel Indian restaurant runs ending interrupted repeatedly
- `strategy exhausted after automatic retries`
- `The reservation search got stuck in an internal browser loop and did not finish cleanly.`
- `Activity task failed`

Interpretation:

- the Resy/restaurant path remains materially less reliable than the travel discovery path
- the biggest remaining durability gap is still browser-backed transactional restaurant work

### 7. Retention is too short for real operator-grade auditing

Current live retention realities:

- thread memory: about `48 hours`
- Lambda log retention: `1 day`

Interpretation:

- we do not currently retain enough user-visible conversation history to do a trustworthy retro audit without external chat history
- this is why a “look through all messages from the last 3 weeks” audit cannot be perfectly literal from infra alone

## Cost Audit

### Live budget numbers

From AWS Budgets on `2026-06-15`:

- Budget name: `My Zero-Spend Budget`
- Actual spend this month: `$17.956`
- Forecasted month-end spend: `$39.065`

So the email you saw around `$38` to `$39` is directionally correct.

### Why the repo cost model drifted

The repo cost model was stale relative to the live account.

Live footprint observed on `2026-06-15`:

- shared host instance: `t3a.small` and running
- shared host public IPv4: attached
- shared host EBS: `8 GiB`
- hands worker instance: `t3a.small` and running
- hands worker public IPv4: attached
- hands worker EBS: `20 GiB`
- extra stopped worker stack exists:
  - stack: `friday-hands-worker-paid`
  - root EBS still allocated: `20 GiB`
- current observed ECR image storage proxy: about `4.9004 GiB`

### Raw monthly reconciliation

Using the same price inputs already documented in `docs/AWS_COST_MODEL.md`:

- `t3a.small`: `$0.0188/hour`
- public IPv4: `$0.005/hour`
- gp3: `$0.08/GB-month`
- ECR: `$0.10/GB-month`

Reconciled raw monthly shape:

```text
shared host compute      = 720 * 0.0188 = 13.536
worker compute           = 720 * 0.0188 = 13.536
two public IPv4s         = 2 * 720 * 0.005 = 7.200
EBS (8 + 20 + 20 GiB)    = 48 * 0.08 = 3.840
ECR (~4.9004 GiB)        = 4.9004 * 0.10 = 0.490

reconciled raw monthly   = 38.602 USD
```

That is close enough to the live budget forecast of:

```text
39.065 USD
```

Conclusion:

- the cost email is probably right
- the older repo cost model is no longer reflecting the live topology

## IAM Visibility Gap

Current reality:

- `budgets:ViewBudget` is live and working
- `ce:GetCostAndUsage` is still denied for the `codex-migration` user

That means:

- we can see the monthly budget forecast and actuals
- we cannot yet break spend down properly by service, day, or usage dimension using Cost Explorer

The repo IAM policy has been updated in:

- [ops/aws/iam/codex-migration-policy.json](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/iam/codex-migration-policy.json)

Added billing/cost visibility actions:

- `billing:ListBillingViews`
- `ce:GetCostAndUsageWithResources`
- `ce:GetCostForecast`
- `ce:GetDimensionValues`
- `ce:GetTags`
- `ce:ListCostCategoryDefinitions`
- `ce:GetAnomalies`
- `ce:GetAnomalyMonitors`
- `ce:GetAnomalySubscriptions`

Also added IAM read actions needed to verify what policy is actually attached:

- `iam:GetPolicy`
- `iam:GetPolicyVersion`
- `iam:GetUserPolicy`
- `iam:ListAttachedUserPolicies`
- `iam:ListPolicies`

## What Needs To Be Fixed Next

### Highest priority behavior fixes

1. Stop completed-task follow-up contamination.
2. Fix Telegram stop ownership for Siri-mirrored heavy jobs.
3. Block all internal task/browser markup from ever reaching user-visible Telegram replies.
4. Tighten degraded live-search behavior so the assistant stays explicit about uncertainty instead of quietly falling back to approximate knowledge answers.

### Reliability and UX fixes

5. Make restaurant/Resy loops fail faster and pivot faster.
6. Replace generic progress spam with user-meaningful partial findings earlier.
7. Add stronger operator-visible task identity so `stop this task` works consistently.

### Infrastructure fixes

8. Increase transcript/log retention enough to support real retro audits.
9. Update `docs/AWS_COST_MODEL.md` to the live infrastructure shape.
10. Reattach the updated IAM policy so Cost Explorer data becomes available for real cost attribution.

## Recommendation

Do not treat the old `85/100` score as current truth.

The more honest current state is:

- the May substrate work improved the system materially
- but the live June UX still has meaningful task-boundary, rendering, and operator-control failures
- cost visibility is incomplete until Cost Explorer access is fixed
- the repo cost model should now be treated as stale until it is refreshed to the live topology
