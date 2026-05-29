# Friday Mobile Handoff

Current focus: reliability first, capability second.

## Latest update

- Live control-plane image is now:
  - `301142908919.dkr.ecr.us-east-1.amazonaws.com/friday-agent:20260529-findings-handoff-v3`
- Findings-request interruption cleanup is now live:
  - `Okay present your findings now` stops the active heavy job and returns a single reply
  - the old duplicate follow-up interruption message is gone
  - validation on the Angel Resy booking task now leaves:
    - one assistant findings reply
    - `active_jobs: []`
- Current gap on that path:
  - if the run has not produced meaningful findings yet, the reply is still:
    - `I do not have useful findings to share from that run yet.`
  - this is cleaner than before, but the next quality pass should surface a better partial-progress summary when available

- Live runtime is still on the dedicated worker and now on the newer restaurant-reliability images:
  - Lambda image: `301142908919.dkr.ecr.us-east-1.amazonaws.com/friday-agent:20260529-preflight-reuse-v2`
  - worker rebuilt and restarted on `3.87.35.135`
- New local/live booking optimization:
  - restaurant booking preflight now carries a concrete matched venue id/url through the run
  - `restaurant_find_availability` can reuse that preflight venue instead of re-running a fresh structured venue search
  - the preflight reuse now tolerates bogus city values invented later by the model when the original user request did not specify a city
- Focused regression slice is green:
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests/test_agent_core_browser_fallback.py agent/tests/test_agent_core_phase1.py agent/tests/test_status_requests.py agent/tests/test_phase1_control_plane.py`
  - `131 passed`
- Latest live validation on the Angel Resy booking case proved a partial runtime win:
  - `api_direct` now goes straight from preflight to `restaurant_availability` on concrete `venue_id=91940`
  - this removed the old second structured venue-search hop in the `api_direct` leg
  - the remaining fallback issue is now narrower: the `stagehand_stealth_act` retry can still begin with `restaurant_search` instead of going straight to the preflight venue id
- Live state was cleaned again after validation:
  - `/worker/health` -> `{"active_jobs":[]}`
  - `/context/recent` -> `{"contexts":[]}`

- Live runtime is now intentionally back on the dedicated worker:
  - `HandsWorkerMode=dedicated_ec2`
  - `HandsWorkerInstanceId=i-04cf5a5edd3b4aa35`
  - Temporal heavy activity worker is running on `3.87.35.135`
- Latest deployed Lambda image:
  - `301142908919.dkr.ecr.us-east-1.amazonaws.com/friday-agent:20260529-booking-input-parser`
- Deterministic restaurant parsing is broader and newer-input-aware now:
  - resumed `party size`, `date`, and `time` labels override stale earlier text
  - month-name / ordinal dates and slash dates normalize correctly
  - flexible time phrases like `any available time` and `earliest available` are accepted for both booking and discovery prefill
- Latest focused local regression slice is green:
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests/test_agent_core_phase1.py agent/tests/test_status_requests.py agent/tests/test_phase1_control_plane.py`
  - `121 passed`

## What is working now

- Siri and Telegram follow-ups are much cleaner than before.
- Running-task cleanup is working again through the live stop endpoint.
- Resy `500` failures now pivot off `api_direct` instead of looping in the same structured lane.
- Fully specified restaurant discovery requests now complete quickly instead of idling in `running_agent`.
- The deterministic discovery path now returns a short NYC shortlist for the Midtown test case instead of cross-city junk.

Latest clean validation:
- Siri query:
  - `Find me an Indian restaurant on Resy near Midtown NYC for tonight at 8 PM for 3 people, preferably with free cancellation.`
- Result:
  - fast completion
  - no active jobs left behind
  - recent Siri context showed:
    - `Angel Indian Restaurant (New York) [venue 91940]`
    - `Muna Indian Restaurant (New York) [venue 95147]`

## Additional local fixes since that pass

- `/context/recent` now reconciles thread ownership before returning rows, so stale `active_heavy_job_id` values get cleared instead of surviving in the recent-context view after a job has already finished.
- Thread admin routes now normalize `telegram-owner` to the real allowed Telegram chat id, so the documented cleanup shortcut works instead of silently missing the stored thread.
- Focused regression slice is green:
  - `/tmp/friday-agent-venv/bin/python -m pytest -q agent/tests/test_phase1_control_plane.py agent/tests/test_agent_core_phase1.py`
  - `62 passed`

These fixes are now deployed live in image:
- `301142908919.dkr.ecr.us-east-1.amazonaws.com/friday-agent:20260529-booking-input-parser`

## Current live state after cleanup

- `/worker/health` returned:
  - `{"active_jobs":[]}`
- `/context/recent` was manually cleaned and now returns:
  - `{"contexts":[]}`

## Latest post-deploy validation

- Deploy:
  - `scripts/deploy-agent.sh`
  - `scripts/deploy-hands-worker.sh`
  - remote Docker build on worker host `3.87.35.135`
  - explicit live runtime parameters preserved:
    - `HandsWorkerMode=dedicated_ec2`
    - `ExecutionBackend=temporal`
    - `TemporalHost=ec2-3-80-179-123.compute-1.amazonaws.com:7233`
- Siri validation query:
  - `Find me an Indian restaurant on Resy near Midtown NYC for tonight at 8 PM for 3 people, preferably with free cancellation.`
- Immediate Siri response:
  - queued ack with job id `c2ac26d5-5d0d-42b1-a31b-c3b5ae522d14`
- Final result:
  - completed quickly
  - DynamoDB job status: `completed`
  - `/worker/health` returned `{"active_jobs":[]}`
  - `/context/recent` showed:
    - Siri row with `active_heavy_job_id: ""`
    - Telegram mirror row with `active_heavy_job_id: ""`
- Cleanup validation:
  - `DELETE /threads/telegram-owner?channel=telegram` cleared the Telegram row after a short propagation delay
  - `DELETE /threads/siri?channel=siri&user_id=siri` cleared the Siri row
  - final `/context/recent` returned `{"contexts":[]}`

- Booking parser regression validation:
  - query:
    - `Book the earliest available reservation tonight for 3 people at Angel Indian Restaurant on Resy, but only if it has free cancellation.`
  - queued job id:
    - `1c019428-e603-408a-b70b-278893ad2fc2`
  - live result:
    - the job did **not** enter `paused_for_input` for missing `time`
    - `api_direct` failed with the known Resy `500`
    - strategy switched to `stagehand_stealth_act`
    - the retry still carried `time=ANY AVAILABLE` in the dedicated-worker logs, so resumed/latest structured inputs are no longer getting dropped on strategy retry
  - the run was manually stopped after capturing the regression signal, then cleaned back to:
    - `/worker/health` -> `{"active_jobs":[]}`
    - `/context/recent` -> `{"contexts":[]}`

- Flexible-time discovery validation:
  - query:
    - `Find me Indian restaurants on Resy near Midtown NYC for tomorrow any available time for two people.`
  - queued job id:
    - `7ff5e280-f9a8-45a9-8905-fc36f5d27148`
  - final result:
    - completed in `api_direct`
    - no pause / no fallback needed
    - result preview:
      - `I found a few likely Resy options near Midtown Nyc for 2 for any available time on 2026-05-30.`
      - `INDIAN TABLE (New York) [venue 88720]`
  - post-run cleanup again returned:
    - `/worker/health` -> `{"active_jobs":[]}`
    - `/context/recent` -> `{"contexts":[]}`

## What is not done yet

- End-to-end booking validation is not complete yet.
- End-to-end cancel validation is not complete yet.
- OTP/account-create/account-login continuity still needs a fresh live pass after the latest reliability changes.
- Telegram thread mirroring still favors the short Siri ack more than the final completed restaurant summary.
- Stagehand fallback still needs its own runtime-quality pass; the parser fix is live, but the long browser fallback path can still take too long on the Angel validation case.

## Next validation order

1. Restaurant discovery
2. Restaurant availability follow-up on one returned venue
3. Free-cancel booking on Resy
4. Cancel that same Resy booking
5. OpenTable booking fallback
6. Cancel that same OpenTable booking
7. OTP/account-gated flow

Do not start broad new capability work before those are green.

## Hard cleanup rule

Every Codex validation must end with:

1. stop the validation job
2. confirm `/worker/health` shows `active_jobs: []`
3. clear Siri thread context if the validation was Siri-owned
4. clear Telegram owner thread context if the validation polluted Telegram

Never hand the system back with stale running or paused validation jobs.

## Live endpoints

- Function URL:
  - `https://z44myckacp7lv7ndjmzmkmqfle0nwlwx.lambda-url.us-east-1.on.aws`
- Siri endpoint:
  - `POST /siri`
- Worker health:
  - `GET /worker/health`
- Recent contexts:
  - `GET /context/recent`
- Stop job:
  - `POST /jobs/{job_id}/stop`

All of those use the header:
- `x-friday-siri-key: <live siri key>`

## Useful live checks

Check active jobs:

```bash
curl -s -H "x-friday-siri-key: $SIRI_KEY" \
  "$FRIDAY_URL/worker/health"
```

Clear Siri thread:

```bash
curl -s -X DELETE -H "x-friday-siri-key: $SIRI_KEY" \
  "$FRIDAY_URL/threads/siri?channel=siri&user_id=siri"
```

Clear Telegram owner thread:

```bash
curl -s -X DELETE -H "x-friday-siri-key: $SIRI_KEY" \
  "$FRIDAY_URL/threads/telegram-owner?channel=telegram"
```

If that alias ever looks suspicious again, confirm the actual stored conversation id through `/context/recent`.

Run a Siri validation:

```bash
curl -s -X POST \
  -H "content-type: application/json" \
  -H "x-friday-siri-key: $SIRI_KEY" \
  "$FRIDAY_URL/siri" \
  -d '{"query":"Find me an Indian restaurant on Resy near Midtown NYC for tonight at 8 PM for 3 people, preferably with free cancellation."}'
```

Stop a validation job:

```bash
curl -s -X POST -H "x-friday-siri-key: $SIRI_KEY" \
  "$FRIDAY_URL/jobs/<job_id>/stop"
```

Read recent contexts:

```bash
curl -s -H "x-friday-siri-key: $SIRI_KEY" \
  "$FRIDAY_URL/context/recent"
```

## Infra map

- Control plane / Lambda:
  - handles Siri, Telegram, job state, stop/status/context endpoints
- Shared host:
  - Temporal workflow worker
- Dedicated worker:
  - Temporal activity worker
  - structured restaurant tools
  - Stagehand / browser fallback

## Files touched in this pass

- `agent/app/agent_core.py`
- `agent/app/main.py`
- `agent/tests/test_agent_core_phase1.py`
- `agent/tests/test_phase1_control_plane.py`

## Current branch / commit context

- Branch:
  - `friday-local`
- Last pushed commit before the newest local reliability changes:
  - `c4be514`

If you pick this up from mobile, start by asking for:
- current `git status`
- `/worker/health`
- `/context/recent`
- whether there are any stale validation jobs to clean first
