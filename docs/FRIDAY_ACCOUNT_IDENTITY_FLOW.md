# Friday Account Identity Flow

This document captures the intended flow for account creation, account reuse, and operator-provided email/password handling.

It is a backlog/spec document, not a fully implemented feature.

## Goal

Support tasks like "book this for me" where a site requires sign-up or sign-in, while keeping:
- the LLM away from raw secrets
- the operator in control of which identity is used
- the heavy-task pause/resume flow durable
- future dashboard editing of saved identities possible

## Desired Operator Flow

When a site requires an account:

1. Friday pauses at the sign-in / sign-up decision point.
2. Friday asks whether it should:
   - use a cached identity for that site or merchant
   - use a new email address
3. If the operator chooses a new email:
   - the operator can provide a `Hide My Email` address or another email
   - Friday resumes with that chosen email
   - preferred medium-term path: Friday can also offer a dedicated Friday-owned mailbox identity when one is configured
4. If a password is needed:
   - preferred path: Friday creates a credential entry and asks the operator to populate the password through the dashboard or another secure entry surface backed by SSM SecureString
   - acceptable fallback path: use an operator-approved default common password for low-risk consumer account creation, if explicitly configured
5. Before actually creating the account or submitting the sign-up form, Friday must require explicit approval because account creation is an account mutation.

## Required Product Behavior

### Pause Semantics

- Hitting a sign-in/sign-up gate should use `paused_for_input`, not generic failure.
- The pause payload should identify:
  - site / merchant
  - whether sign-in or sign-up is needed
  - whether a cached identity exists
  - what exact input is missing from the operator

### Identity Selection

- Friday should support a site-scoped or merchant-scoped identity registry.
- The operator should be able to see and edit:
  - label
  - email address
  - notes
  - whether the identity is the default for a given site/domain/category
- A dedicated Friday-owned mailbox should be a first-class identity option for account-gated workflows and verification-email handling.
- The LLM should see only the safe metadata needed for selection, not the raw password or secret values.

### Secret Handling

- Passwords must not be embedded in prompts, checkpoints, logs, or general artifact files.
- Password material should live in SSM SecureString or another equivalent secure store.
- The heavy worker should fetch the secret only at the moment it is needed inside the tool/runtime layer.
- The dashboard or secure operator surface should support write-only or masked secret updates.
- The dedicated Friday mailbox secret material must still live in SSM or equivalent secure storage, not in prompts or general env files.
- Current preferred mailbox shape is Gmail OAuth credentials plus Pub/Sub-driven event delivery, not an IMAP/App Password poller inside the worker.

### Mailbox Handling

- Friday should support a dedicated mailbox that belongs to the agent, not the operator's primary inbox.
- The operator should still be able to monitor that mailbox directly from normal mail clients.
- The mailbox path should support:
  - OTP retrieval
  - verification-email retrieval
  - confirmation-email review
  - pause/resume fallback when a site requires email-based verification
- Friday should not have delete permission by default for this mailbox; preserve the paper trail.

### Approval Rules

- Creating an account is an account mutation and must require explicit approval.
- Reusing a cached identity for a sign-in should still be operator-visible, but may be allowed to proceed without a second approval if the operator already approved the broader task and the policy later allows it.
- Any first-time merchant account creation should be treated as higher-risk than reusing a known saved identity.

## Data Model Direction

This likely needs a new first-class record type beyond the current bare credential stub:

- identity record:
  - `identity_id`
  - `label`
  - `email`
  - `site`
  - `category`
  - `is_default`
  - `notes`
  - `created_at`
  - `updated_at`
- secret pointer:
  - `parameter_name`
  - `secret_kind=password`
  - optional rotation / expiry metadata

The existing `SecureCredentialRecord` in `agent/app/jobs.py` is not yet enough for the full operator-visible identity flow.

## Dashboard Requirements

The future dashboard should support:
- listing saved identities
- editing visible metadata like email / label / notes / defaults
- creating a new identity placeholder before a password exists
- setting or rotating a password without exposing it back to the model
- showing which identities are associated with which sites

## Implementation Order

Suggested order:

1. add identity metadata records and CRUD APIs
2. add secure secret-write path backed by SSM
3. add dedicated Friday mailbox support and MCP integration
4. add pause payload structure for identity/email choice prompts
5. add sign-in/sign-up gate detection in the browser workflow
6. add dashboard identity editor
7. only then let live booking/account flows rely on cached identities

## Current Status

- `paused_for_input` exists
- approval gating exists
- secure secret storage in SSM exists for system secrets
- dedicated Friday mailbox is now created as `friday.nyc.agent@gmail.com` and should be treated as the default agent-owned email identity for future account-gated work unless the operator chooses another address
- operator-visible identity management does not exist yet
- secure per-identity password entry/editing does not exist yet
- sign-in/sign-up gate detection for this workflow does not exist yet
