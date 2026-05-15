# AWS Migration History

This file is the durable changelog of AWS account and shared-host migrations for the Friday shared infrastructure.

It exists so future sessions can answer these questions quickly:

- how many migrations have happened
- when each migration happened
- which AWS account became active
- what infrastructure moved
- what the trigger and outcome were

## Migration 1

- Migration date: `2026-05-10`
- Migration number: `1`
- Direction: previous AWS account -> current `iris` AWS account
- New AWS account ID: `301142908919`
- New AWS profile: `iris`
- New shared-host instance ID: `i-0263b221709dce545`
- New shared-host public IP: `13.216.214.108`
- New shared-host public DNS: `ec2-13-216-214-108.compute-1.amazonaws.com`
- New shared-host region: `us-east-1`
- New shared-host role: shared EC2 host for:
  - Friday WireGuard VPN
  - Iris backend
  - Friday personal agent migration path
- Notes:
  - This was the first recorded AWS migration for this project.
  - The account creation / migration date for the current AWS environment is `2026-05-10`.
  - The current AWS credit pool for this account started on `2026-05-10`.
  - Current migration reminder date for this account: `2026-10-26`.
  - Current 6-month account-expiry target for this account: `2026-11-10`.
  - The free-plan / credit assumptions in `docs/AWS_COST_MODEL.md` should be anchored to this migration date unless a newer migration supersedes it.

## Update Rule

Every future AWS migration must append a new numbered section here and also update:

- `docs/AWS_MIGRATION.md`
- `docs/AWS_COST_MODEL.md`
- `docs/MAINTENANCE_JOURNAL.md`
