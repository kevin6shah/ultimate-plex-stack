# Friday MCP Stack Plan

This file is the source of truth for Friday's MCP and connector stack plan.

Use it for:
- the full list of MCPs/connectors Friday is using or evaluating
- the trust tier for each one
- why it exists
- why it is not yet adopted, if applicable
- the isolation harness plan for reducing blast radius

## Trust Tiers

### L1: Trusted Read-Only Research

Low-blast-radius tools that primarily return deterministic or sanitized data.

Examples:
- Brave Search wrapper
- deterministic HTTP fetch/conversion

### L2: Network Research and API Tools

Remote-network tools that read public or semi-structured data, but do not directly mutate user state.

Examples:
- Firecrawl MCP
- Google Maps / Places / Routes via OpenAPI MCP
- Skiplagged MCP
- travel-planner comparison MCPs

### L3: Internal File and Workspace Tools

Tools that can read or modify task-local files inside an isolated workspace.

Examples:
- official filesystem MCP
- Friday workspace helper MCP
- MarkItDown-backed local conversion helpers

### L4: Sensitive Identity / Booking / Commerce Tools

Tools that can touch accounts, inboxes, reservations, or money-adjacent workflows.

Examples:
- Resy / OpenTable / restaurant reservation connectors
- email inbox MCP
- temp-mail / burner identity MCP
- future commerce checkout helpers

## Current MCP / Connector Inventory

| Candidate | Type | Tier | Status | Why It Exists | Why It Is Not Fully Adopted Yet |
| --- | --- | --- | --- | --- | --- |
| Official filesystem MCP | MCP | L3 | Repo-implemented | Broad file/directory operations within worker roots | Not yet deployed/live-validated in AWS |
| Friday workspace helper MCP | MCP | L3 | Active | Preview, markdown conversion, PDF generation | Still coupled to the current worker path |
| MarkItDown | local helper | L3 | Active | Document-to-markdown conversion | Needs more real operator tasks |
| Brave Search wrapper | native wrapper | L1 | Active | Deterministic search before browser escalation | Already sufficient for first-pass search; no MCP needed yet |
| Firecrawl MCP | MCP | L2 | Repo-wired candidate | Stronger read-only web scrape/search/extract path for hostile sites | Needs deployment, key wiring in AWS, and output-quality comparison |
| Skiplagged MCP | remote MCP via `mcp-remote` bridge | L2 | Repo-wired selected candidate | Flights, hotels, flexible-date travel search, and rental cars without browser-first scraping | Needs live validation in this stack and fallback behavior verification |
| Google Maps / Places / Routes via OpenAPI MCP | API through MCP | L2 | Repo-wired selected candidate | Stable maps/travel primitives without browser-first routing | Needs deployment, spec/base-url wiring, and schema-scoping validation |
| cablate Google Maps MCP | MCP | L2 | Repo-wired selected candidate | Rich place, route, explore-area, and along-route travel primitives | Needs deployment, key wiring, and real itinerary output comparison |
| Travel Planner MCP | MCP | L2 | Candidate for comparison only | Fast way to test maps/travel capabilities | Upstream repo is archived, so it is comparison-only until proven worth keeping |
| Resy integration path | connector / MCP-like remote integration | L4 | Disabled candidate | High-value restaurant availability and booking target in NYC | Current runtime candidate hung in this stack; replacement source still needed |
| OpenTable extraction/booking path | connector / MCP | L4 | Disabled candidate | High-value restaurant availability and booking target in NYC | Current runtime candidate did not provide a clean MCP-backed success in this stack |
| Unified restaurant reservation MCPs | MCP | L4 | Candidate | Could provide Resy/OpenTable fallback and comparison path | Need maintenance/security review and real output comparison |
| Dedicated Friday mailbox via Gmail OAuth + Pub/Sub push | mailbox event substrate | L4 | Repo-wired selected candidate | Agent-owned inbox for verification emails, confirmations, OTP fallback, and operator-visible account workflows | Needs Gmail OAuth secret wiring, Pub/Sub push validation, and live workflow-resume validation |
| Temp-mail / burner identity MCP | MCP | L4 | Candidate | Burner signup and verification support | Identity/password/dashboard flow is not ready yet |
| Browser-use | browser substrate | separate browser tier | Active | Interaction fallback for unsupported flows | Must not remain the only path for maps/booking/files |

## Gemini-Discussed MCPs and Friday Status

| Mentioned In Conversation | Friday Status | Decision |
| --- | --- | --- |
| Filesystem MCP | Being added | Correct direction; use broad file capability inside bounded worker roots |
| MarkItDown | Already active | Keep as helper; do not treat it as a replacement for filesystem MCP |
| Firecrawl MCP | Not rejected; not installed yet | Add as an L2 read-only comparison candidate |
| cablate Google Maps MCP | Selected candidate | Add to the maps/travel comparison set; compare against a stricter OpenAPI-based Google Maps path |
| Travel-Planner-MCP | Not rejected; not preferred as sole solution | Compare it, but do not rely on an archived repo as the only maps path |
| OpenTable MCP | Disabled for now | Do not keep it in the active runtime path until there is a cleaner proven source |
| Resy MCP / connector | Disabled for now | Do not keep the current broken runtime candidate active; only reintroduce when there is a real runnable source with validation |
| TempMail MCP | Deferred, not rejected | Add only as part of the broader account-gated identity program |
| Brave Search | Already active | Keep as the safest first-step search path |
| Gmail / mailbox event path | Selected target | Use a dedicated Friday mailbox with Gmail OAuth, Gmail API watch/history, and GCP Pub/Sub push into AWS/Temporal |

## Selected Next Evaluation Set

The current operator-selected set to add and compare next is:

- Firecrawl MCP
- Skiplagged MCP
- cablate Google Maps MCP
- Google Maps / Places / Routes via OpenAPI MCP
- dedicated Friday mailbox via Gmail/email MCP

This is the set that should be installed, isolated, and evaluated before narrowing to primaries.

Current repo state for this set:

- Firecrawl MCP can now be mounted by worker settings.
- Skiplagged MCP can now be mounted by worker settings through `mcp-remote` and the hosted Skiplagged endpoint.
- cablate Google Maps MCP can now be mounted by worker settings.
- Google Maps / Places / Routes via OpenAPI MCP can now be mounted by worker settings using a spec URL plus `BASE_URL` / `HEADERS`.
- The older Resy/OpenTable runtime candidates remain in repo for reference but should stay disabled until replaced or rehabilitated.
- Gmail mailbox support is being switched to a Gmail OAuth + watch/history + Pub/Sub push design, not an IMAP/App Password poller.

## Dedicated Friday Mailbox Direction

Friday should have its own dedicated mailbox rather than sharing the operator's primary inbox.

Preferred operating model:

1. Create a dedicated mailbox such as `friday...@gmail.com`.
2. The operator can also log into this mailbox directly from phone/laptop.
3. Friday uses an email MCP against that mailbox for:
   - verification emails
   - OTP/code retrieval
   - confirmation emails
   - burner or low-risk account workflows
4. The mailbox should be treated as an L4 sensitive tool, not as a generic read-only research source.

Current implementation direction:

- The selected direction is Gmail OAuth plus Gmail API watch/history and GCP Pub/Sub push into AWS/Temporal.
- Canonical runtime secrets:
  - `GMAIL_ACCOUNT_EMAIL`
  - `GOOGLE_CLIENT_ID`
  - `GOOGLE_CLIENT_SECRET`
  - `GOOGLE_REFRESH_TOKEN`
  - `GMAIL_PUBSUB_VERIFICATION_TOKEN`
- For safety, Friday should keep mailbox automation read/verification-oriented by default; sending email remains approval-gated future work.

## Isolation Harness Plan

Friday should not treat all MCPs as equal.

Target separation:

1. Browser runtime
- isolated browser container
- no direct broad filesystem MCP mounted in the same trust boundary in the final target architecture

2. L3 file runtime
- official filesystem MCP
- Friday helper MCP
- workspace-root-only mounts
- no host filesystem exposure

3. L2 network-research runtime
- Firecrawl MCP
- OpenAPI MCP gateways for maps/travel APIs
- API-key-scoped env or SSM fetch path

4. L4 sensitive runtime
- booking connectors
- inbox/temp-mail tools
- strongest approval and secret-isolation rules

## Near-Term MCP Program

### Phase 1

- Deploy and validate official filesystem MCP in AWS
- Keep helper MCP for preview/markdown/PDF

### Phase 2

- Add Firecrawl MCP
- Add one maps/travel API path through an OpenAPI MCP harness
- Add one comparison maps MCP if useful

### Phase 3

- Add multiple restaurant-booking candidates in parallel
- compare:
  - availability quality
  - booking handoff quality
  - failure modes
  - account/login friction

### Phase 4

- Add email/inbox and temp-mail support only after login-wall and identity flow groundwork exists

## Rules

- Add multiple candidates where uncertainty is high.
- Keep fallback systems instead of forcing a premature winner.
- Do not give every MCP the same blast radius or secret scope.
- Do not promote a candidate to `Promised` until it passes the proof standards in `docs/FRIDAY_CAPABILITIES_MATRIX.md`.
