# Friday AWS System Design

Last updated: `2026-05-19`

## Purpose

This document is the durable high-level AWS and runtime design for Friday in this repo.

Use it to remember:

- what each AWS component is for
- which host owns which responsibility
- why Friday is split the way it is
- which compromises are allowed and which are not
- how to reason about cost-affecting changes before making them

This file is intentionally architecture-first, not a changelog.

For cost math, use `docs/AWS_COST_MODEL.md`.
For migration mechanics, use `docs/AWS_MIGRATION.md`.
For agent behavior/product state, use `docs/FRIDAY_AGENT.md`.

## System Goals

Friday is meant to be:

- a personal control-plane agent
- cheap to idle
- able to run heavy tasks when needed
- safe around shared infrastructure
- durable across AWS account rotation

That leads to one core design principle:

- keep the always-on shared host small and stable
- keep heavy/interactive hands work separate and on-demand

## Top-Level Architecture

There are two different compute planes in this stack.

### 1. Shared host

This is the always-on EC2 instance.

Current role:

- Iris backend
- WireGuard / VPN support
- some Friday support/runtime/deployment adjacency

Current live shape from the repo cost model:

- `t3.micro`
- `8 GiB gp3` root disk
- `1` public IPv4

Production responsibilities that already exist here today:

- Iris production backend
- WireGuard / VPN ingress
- Friday shared-host deployment/runtime adjacency
- any future always-on Friday orchestration substrate must fit around those responsibilities instead of displacing them

What it is **not** for:

- it is not the intended permanent Friday heavy-task worker
- it is not the place to casually rebuild large Playwright/browser worker images
- it should not absorb Friday hands growth by default just because it is already running

Why:

- it is shared infrastructure
- it carries non-Friday responsibilities
- it has small compute/disk limits by design
- mutations here affect Iris and VPN stability, not just Friday

What is allowed here for Friday:

- small control-plane-adjacent services that must be always on
- orchestration metadata or workflow workers that are light on CPU, disk, and browser/runtime dependencies
- deployment helpers and low-churn operational glue

What is not allowed here for Friday without explicit operator approval:

- Playwright/Chromium image rebuild churn
- always-on browser automation sessions
- large artifact processing
- the primary heavy task runtime
- casual “temporary” fallback of broken dedicated-worker responsibilities

### 2. Friday control plane

This is the serverless Friday stack.

Current role:

- Telegram webhook handling
- Siri ingress
- light synchronous requests
- job coordination
- status, pause, resume, stop, and notifications

Current AWS shape:

- Lambda
- Function URL
- SQS
- DynamoDB
- S3 artifacts bucket
- ECR image repo
- CloudWatch dashboard/alarms
- SNS notifications
- SSM parameters

Design intent:

- Lambda should remain coordination-first
- light work stays here
- heavy browser/file flows should not live here

### 3. Friday dedicated hands worker

This is the intended Friday-only heavy-task execution substrate.

Current intended role:

- Browser-use
- browser automation fallback
- filesystem/file work
- MCP mounting
- heavy research/booking/tasks that exceed Lambda’s model/runtime limits

Current intended shape:

- dedicated on-demand EC2 worker
- `t3a.small`
- separate from the shared host
- started/stopped around heavy use

Why this exists:

- isolate heavy browser/file/runtime churn away from Iris/VPN
- keep baseline AWS cost low
- preserve a clean Lambda control-plane -> worker execution-plane split
- allow future per-tool or per-tier isolation without destabilizing the shared host

## Friday Runtime Design

Friday should be understood as three logical layers.

### A. Interfaces

- Telegram is the primary operator interface
- Siri is a secondary ingress for short requests and task submission
- future dashboard is the operator surface for state, secrets, identities, and jobs

### B. Control plane

- receives requests
- classifies light vs heavy
- stores job/session/checkpoint state
- enforces confirmation/pause/resume boundaries
- sends updates back to Telegram

### C. Hands plane

- executes browser/file/connector tasks
- mounts MCPs/connectors
- produces artifacts
- pauses when human input or verification is needed

## Current Tooling Direction

Friday is no longer supposed to be browser-first.

Preferred execution order:

1. deterministic API / connector
2. filesystem / workspace / structured local tooling
3. deterministic fetch/search/page conversion
4. browser only for interaction or unsupported flows

Current known strong paths:

- Firecrawl for read-oriented web extraction
- cablate Google Maps for maps/travel primitives
- Skiplagged MCP for flights/hotels/rental cars
- official filesystem MCP + workspace helper MCP for file operations

Current weak/unresolved paths:

- restaurant booking connector
- account-gated identity flows
- mailbox/OTP automation

## Hard Architectural Rules

### Rule 1: Cost model first

Before any AWS change that can affect monthly cost:

- read `docs/AWS_COST_MODEL.md`
- evaluate the change against the current modeled architecture
- discuss with the operator
- do not provision/remove/resize without approval

### Rule 2: Shared host is not the default fallback for Friday heavy work

Do not “temporarily” move Friday heavy-worker rebuilds or browser-runtime expansion onto the shared Iris/VPN host unless the operator explicitly approves that compromise after cost and reliability review.

Reason:

- it contaminates the shared-host boundary
- it risks Iris/VPN stability
- it hides the fact that the real worker path is broken
- it can fail predictably on small disk/compute footprints

### Rule 3: Fix the intended plane before mutating the wrong plane

If the dedicated worker path is broken:

- first inspect the dedicated-worker infra problem
- first inspect IAM / stack / worker lifecycle / disk sizing there
- do not assume the right fix is to grow the shared host

### Rule 4: Friday hands belong on the Friday worker

The intended home for:

- Browser-use
- Playwright-based runtime
- heavy MCP execution
- large browser images
- worker rebuild churn

is the dedicated Friday on-demand worker, not the shared host.

## Temporal Direction

Friday v2 should treat Temporal as orchestration, not as an excuse to collapse the compute planes.

Approved placement:

- Temporal server and lightweight workflow worker:
  - always-on shared host
  - only if they remain small, predictable, and non-browser-heavy
- Temporal heavy activity worker:
  - dedicated Friday on-demand worker
  - owns browser execution, heavy MCPs, file/artifact work, and other long-running task bodies
- Lambda / Function URL control plane:
  - remains the Siri/Telegram ingress
  - starts workflows, signals workflows, queries workflow state, and starts the dedicated worker when needed

Why this split is approved:

- the dedicated worker is not always on, so it cannot be the sole home for Temporal orchestration
- the shared host is always on, so it can own durable orchestration state and lightweight workflow polling
- heavy runtime still stays off the shared host, preserving the original boundary

What this means in practice:

- shared host may gain Temporal server/workflow-process responsibilities
- shared host must not become the browser or Playwright runtime just because Temporal lives there
- dedicated worker remains the only approved home for Stagehand/browser-use/heavy task execution
- the repo-level Temporal backend should be considered inactive until `TEMPORAL_HOST` points at a real always-on Temporal service

## Shared Host Inventory Rule

Any session that changes Friday infra or runtime assumptions must keep an explicit inventory of what is already live on the shared host.

At minimum, that inventory should answer:

- what non-Friday production services live there
- what Friday services live there
- what new always-on services are being proposed there
- why those services are small enough to coexist safely

If that inventory changes, update this document and any operator-facing handoff notes in the same workstream.

### Rule 5: Cost-effective does not mean architecture-agnostic

The cheapest fix is not always the right fix.

Use this decision order:

1. prefer the correct architecture if the cost delta is acceptable and the path is supportable
2. only choose a shared-host compromise when explicitly approved
3. document the compromise and why it was accepted

## Current Live AWS Structure

As of the latest known repo-verified state:

- shared host stack / live host:
  - EC2 `t3.micro`
  - carries Iris + VPN + shared support
- Friday agent stack:
  - Lambda control plane and serverless dependencies
- dedicated hands worker:
  - intended architecture
  - currently the correct home for Friday heavy work
  - may be temporarily unavailable if stack/IAM issues exist

## Cost Interpretation

The cost model already includes the shared host.

That means:

- the Iris/VPN EC2 box is already in the baseline monthly number
- dedicated-worker scenarios are additive to that baseline
- any decision to resize the shared host or add worker runtime must be compared against the modeled scenarios, not guessed

Important implication:

- “shared host resize” and “dedicated worker restore” are different architectural moves, even when their monthly raw AWS costs are numerically close

## Known Failure Pattern To Remember

The following is a design smell and should be recognized quickly in future sessions:

- dedicated worker path breaks
- heavy runtime is rerouted onto the shared host
- Playwright/browser image rebuild starts on a tiny shared-host root disk
- disk exhaustion follows

Correct response:

- stop
- consult this architecture doc and the cost model
- fix the dedicated worker path or explicitly escalate the compromise to the operator

## Decision Record

Current intended direction:

- Friday heavy tasks belong on the dedicated on-demand worker
- the shared host should stay focused on Iris/VPN/shared services
- if Temporal is adopted, it should own orchestration on the shared host while heavy activities stay on the dedicated worker
- Skiplagged should replace brittle browser-first travel search
- broken reservation connectors should not stay active just because they exist in repo

## Update Rule

Update this file whenever any of the following changes:

- shared host responsibilities
- Friday control-plane topology
- dedicated-worker topology
- approved infrastructure compromise
- the meaning/purpose of a major MCP or worker tier
