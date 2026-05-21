# Friday Environment Keys

Last updated: `2026-05-20`

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

- `GMAIL_ACCOUNT_EMAIL`
- `GMAIL_ACCOUNT_PASSWORD`
- `GMAIL_APP_PASSWORD`
- `GMAIL_CLIENT_ID`
- `GMAIL_CLIENT_SECRET`
- `GMAIL_REFRESH_TOKEN`

Notes:

- `GMAIL_ACCOUNT_PASSWORD` still exists in the local env inventory for compatibility with older flows.
- `GMAIL_APP_PASSWORD` is the canonical SSM-backed Gmail password key used by the current deploy/runtime scripts.

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
