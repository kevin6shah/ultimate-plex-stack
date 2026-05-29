# Friday Environment Keys

Last updated: `2026-05-23`

This file is the key-only inventory for the local Friday `.env` file and the SSM sync map.

Rules:

- document keys here, never secret values
- whenever `.env` keys or `scripts/friday-secret-map.tsv` change, update this file in the same change
- run `./scripts/check-env-key-docs.sh` after any env-key or secret-map edit
- the live production AWS CLI profile is currently `iris`; that value must remain explicit in the docs and migration flow instead of being rediscovered ad hoc

## Core Infra

- `AWS_PROFILE`
- `AWS_REGION`
- `AGENT_STACK_NAME`
- `WORKER_API_KEY`

## Identity And Display

- `FRIDAY_AGENT_EMAIL`
- `FRIDAY_BOOKING_DISPLAY_NAME`
- `TELEGRAM_BOT_USERNAME`

## Model And Research

- `DEEPSEEK_API_KEY`
- `BRAVE_SEARCH_API_KEY`
- `BROWSER_USE_API_KEY`
- `LOGFIRE_TOKEN`
- `FIRECRAWL_API_KEY`

## Maps And Travel

- `GOOGLE_MAPS_API_KEY`
- `MAPS_OPENAPI_HEADERS_JSON`
- `MAPS_OPENAPI_AUTH_TOKEN`

## Restaurants

- `RESY_API_KEY`
- `RESY_AUTH_TOKEN`
- `OPENTABLE_EMAIL`
- `OPENTABLE_PASSWORD`

## Gmail

- `GMAIL_PUBSUB_TOPIC_NAME`
- `GMAIL_WATCH_LABEL_IDS`
- `GMAIL_WATCH_LABEL_FILTER_ACTION`
- `GMAIL_WATCH_RENEWAL_DAYS`
- `GMAIL_ACCOUNT_EMAIL`
- `GMAIL_ACCOUNT_PASSWORD`
- `GMAIL_APP_PASSWORD`
- `GMAIL_PUBSUB_VERIFICATION_TOKEN`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `GMAIL_CLIENT_ID`
- `GMAIL_CLIENT_SECRET`
- `GMAIL_REFRESH_TOKEN`
- `DASHBOARD_SESSION_TTL_SECONDS`
- `DASHBOARD_SESSION_SECRET`

Notes:

- The local `.env` may intentionally contain both `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` and `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN`.
- Both OAuth key families are currently active in runtime resolution for mailbox auth. The settings layer accepts either naming scheme so existing local env files and older deploy paths continue to work.
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REFRESH_TOKEN` remain the preferred canonical names for new env/SSM wiring and future normalization work.
- `scripts/sync-ssm-from-env.sh` now prefers the first non-empty mapping for a target parameter, so the canonical `GOOGLE_*` Gmail OAuth keys win and legacy `GMAIL_*` aliases do not overwrite them later in the same sync run.
- `GMAIL_ACCOUNT_PASSWORD` and `GMAIL_APP_PASSWORD` still appear in the local env inventory for compatibility and migration continuity only.
- `GMAIL_APP_PASSWORD` is not part of the active mailbox event-driven pipeline and should not be used as the primary mailbox runtime credential path.
- `GMAIL_ACCOUNT_PASSWORD` is not part of the active mailbox event-driven pipeline either; keep it documented only until the local env and secret map are fully normalized.

The separate repo-local `.friday-ops.env` inventory is documented in `docs/FRIDAY_OPS_ENV_KEYS.md` so this file can remain aligned with `./scripts/check-env-key-docs.sh`.

## Messaging

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_CHAT_ID`
- `TELEGRAM_WEBHOOK_SECRET`
- `SIRI_API_KEY`

## Migration Contract

These keys are part of the AWS-account rotation contract and must stay stable or be updated in the migration docs at the same time:

- `AWS_PROFILE`
- `AWS_REGION`
- `AGENT_STACK_NAME`
- `WORKER_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_CHAT_ID`
- `TELEGRAM_WEBHOOK_SECRET`
- `SIRI_API_KEY`
