# Friday V2 Validation Matrix

Last updated: 2026-05-31

## Purpose
This matrix exists to catch the exact reliability failures that were slipping through local validation:
- cross-domain heavy-task contamination on the same thread
- stale progress/findings leaking into status for the wrong task
- resumability that re-attaches to the wrong active job
- interruptions that preserve only control-plane progress instead of useful findings

The goal is to keep validation cheap:
- default to local deterministic tests
- use one isolated live replay only after the local matrix is green
- always clean up validation jobs and restore thread state afterward

## Required local regression slices

### Routing and task ownership
- same-thread booking refinement stays on the same heavy job
- same-thread orthogonal light question does not hijack the heavy job
- same-thread cross-domain heavy request supersedes the old job instead of appending to it
- paused-for-input reply resumes the paused job only when it actually answers the blocker
- completed booking clarification accepts natural answers without forcing `answer:`

### Status and findings integrity
- paused-input status preserves the real blocker detail
- task list preserves useful checkpoint detail
- stale flight progress never appears on a Willow/Fubo/account task
- stale restaurant progress never appears on a travel task
- `present your findings now` prefers durable findings over generic progress

### Durability and retry behavior
- strategy retry payload preserves `job_context.latest_findings_summary`
- strategy retry payload preserves `job_context.latest_checkpoint_summary`
- recoverable provider failures produce operator-usable summaries
- control-plane pause/resume/stop remains compatible with Temporal claims

## Live smoke order
Run only after the local matrix passes.

1. same-thread modifier on a running heavy task
2. orthogonal light query during a running heavy task
3. cross-domain heavy request on the same thread
4. `present your findings now`
5. cleanup verification:
   - no active validation jobs
   - no stale active thread job
   - original Siri/Telegram thread state restored

## Real regressions now covered
- `Not flights I’m thinking activities in NYC` must not absorb `Create an account with a free trial for Willow TV`
- a Willow/Fubo/login task must not surface `checking live flight options and collecting candidate itineraries`
- paused-input and task-list replies must retain the real blocker summary such as `blocked on party size`

## Acceptance rule
No live replay should happen until the local routing/status/durability slices are green together.
