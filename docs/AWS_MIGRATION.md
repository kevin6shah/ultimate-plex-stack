# AWS Migration

## Why This Exists

The old renewal story in this repo was incomplete.

The shared EC2 host at `54.90.132.5` is not just the WireGuard VPN for Friday. It also runs the live Iris backend for `/Users/kevinshah/Documents/mta-led-sign`.

That means a future AWS account rotation must preserve both:

- Friday VPN continuity
- Iris backend state and runtime
- Friday personal agent infrastructure

This document is the durable source of truth for that shared-host migration.

Migration changelog:

- `docs/AWS_MIGRATION_HISTORY.md`

## Current Live Inventory

Verified on `2026-05-13` from local WireGuard config and `backup/aws/latest` metadata:

- AWS CLI profile: `iris`
- AWS account ID: `301142908919`
- IAM user used locally: `arn:aws:iam::301142908919:user/codex-migration`
- Region: `us-east-1`
- EC2 instance ID: `i-0263b221709dce545`
- Public IP: `13.216.214.108`
- Public DNS: `ec2-13-216-214-108.compute-1.amazonaws.com`
- Private IP: `172.31.38.96`
- VPC: `vpc-0250e8d95b4d6e169`
- Subnet: `subnet-04105c013717bf298`
- Security group: `sg-0541241698170a453`
- Key pair name: `iris-migration-20260510`
- Instance type: `t3.micro`
- Launch time: `2026-05-10T23:09:25Z`
- OS: Ubuntu `24.04`

## Migration History

Recorded migration count: `1`

First recorded migration:

- migration date: `2026-05-10`
- destination AWS account: `301142908919`
- destination AWS profile: `iris`
- migration reminder date: `2026-10-26`
- current 6-month window end: `2026-11-10`
- durable log: `docs/AWS_MIGRATION_HISTORY.md`

For the current account, migration prep should start on `2026-10-26`, which is `15` days before the current six-month expiry target of `2026-11-10`.

Live services on the host:

- `wg-quick@wg0`
- `iris-backend`
- `nginx`

Live service files on the host:

- `/etc/wireguard/wg0.conf`
- `/etc/nginx/sites-available/iris-backend`
- `/etc/systemd/system/iris-backend.service`
- `/opt/iris-backend/.env`
- `/opt/iris-backend/iris-preferences.json`
- `/opt/iris-backend/iris-state.json`
- `/opt/iris-backend/iris-countdown-feedback.json`

## Important Findings

1. The old `QUICK-RENEWAL-GUIDE.md` is only a VPN rotation guide. It does not preserve the live Iris backend.
2. The easiest migration path is to restore the same WireGuard server config on the new host.
3. If the same WireGuard server config is restored, the local Friday client only needs its `Endpoint` IP updated.
4. Iris still hardcodes the EC2 backend URL in multiple files under `/Users/kevinshah/Documents/mta-led-sign`, so migration must update that repo too.
5. The current security group is wider than needed:
   - `22/tcp` is open to `0.0.0.0/0`
   - `51413/udp` is open even though the EC2 host does not run Transmission
6. Because you do not want a paid domain right now, the operational model must assume raw public IP cutovers and scripted source/config rewrites.

## New Backup Path

Use this before any AWS work:

```bash
./scripts/backup-aws-host.sh
```

For ad hoc AWS CLI changes, use:

```bash
AWS_PROFILE=<profile> ./scripts/aws-infra-change.sh --reason "short reason" -- <aws command ...>
```

That is now the hard rule for Codex-managed AWS infrastructure changes in this repo.

What it captures:

- local AWS identity and EC2 metadata
- local AWS security-group metadata
- remote WireGuard config
- remote nginx vhost
- remote `iris-backend` systemd unit
- remote `/opt/iris-backend` tree excluding `node_modules`

Output location:

- `backup/aws/<timestamp>/`
- convenience symlink: `backup/aws/latest`

The backup directory is already ignored by Git.

## New Migration Toolkit

Tracked artifacts:

- [friday-shared-host.yaml](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/friday-shared-host.yaml)
- [friday-agent.yaml](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/friday-agent.yaml)
- [bootstrap-host.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/bootstrap-host.sh)
- [restore-host-from-backup.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/restore-host-from-backup.sh)
- [ops/aws/iam/README.md](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/iam/README.md)
- [codex-migration-policy.json](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/iam/codex-migration-policy.json)
- [backup-aws-host.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/scripts/backup-aws-host.sh)
- [backup-agent-state.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/scripts/backup-agent-state.sh)
- [check-agent-migration-readiness.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/scripts/check-agent-migration-readiness.sh)
- [deploy-agent.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/scripts/deploy-agent.sh)
- [aws-infra-change.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/scripts/aws-infra-change.sh)
- [check-aws-migration-readiness.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/scripts/check-aws-migration-readiness.sh)
- [prepare-migration-day.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/scripts/prepare-migration-day.sh)
- [migrate-aws-account.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/scripts/migrate-aws-account.sh)
- [post-migration-smoke.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/scripts/post-migration-smoke.sh)
- [update-vpn-endpoint.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/scripts/update-vpn-endpoint.sh)
- [update-mta-led-sign-backend-url.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/scripts/update-mta-led-sign-backend-url.sh)
- [AWS_BLUE_GREEN_RUNBOOK.md](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/docs/AWS_BLUE_GREEN_RUNBOOK.md)

## IAM Access Model For Future Accounts

For each new AWS account:

1. create one IAM user for Codex-driven migration/debugging
2. attach the policy in `ops/aws/iam/codex-migration-policy.json`
3. create a local AWS CLI profile on this Mac
4. create a new EC2 key pair and keep the `.pem` locally

Recommended validation:

```bash
AWS_PROFILE=<new-profile> ./scripts/check-aws-migration-readiness.sh
AWS_PROFILE=<new-profile> KEY_NAME=<new-keypair-name> EC2_SSH_KEY=/path/to/new-key.pem ./scripts/prepare-migration-day.sh
```

If a raw `aws ...` command fails on this Mac, use `/usr/local/bin/aws ...` or run the repo scripts directly. The scripts already prepend `/usr/local/bin:/opt/homebrew/bin` to `PATH`.

This is the minimum clean handoff so Codex can:

- inspect the account
- provision the green host
- debug failures
- re-run the migration safely

## Intended Future One-Command Migration

When the new AWS account exists and has a usable key pair:

```bash
AWS_PROFILE=<new-account-profile> \
AWS_REGION=us-east-1 \
KEY_NAME=<new-keypair-name> \
EC2_SSH_KEY=/path/to/new-key.pem \
./scripts/migrate-aws-account.sh --yes
```

What that command is designed to do:

1. take a fresh backup of the current shared host unless `--skip-backup` is used
2. discover the default VPC and subnet in the target account
3. resolve the latest Ubuntu 24.04 AMI through SSM
4. create a new EC2 instance and tighter security group through CloudFormation
5. wait for SSH
6. install WireGuard, nginx, nodejs, and npm
7. restore the saved shared-host state onto the new instance
8. update the local Friday WireGuard endpoint and VPN expected IP
9. update local `mta-led-sign` source references from the old backend URL to the new one
10. run the combined Friday + Iris smoke checks
11. validate the target account for agent deployment
12. rebuild/push the Friday agent Lambda image and deploy the serverless stack
13. install the Friday hands runtime onto the shared host with the live Function URL and worker key
14. print the new Telegram and Siri webhook URLs before returning success

To migrate only the shared host in an emergency, pass `--skip-agent`.

Agent secrets are expected as SSM SecureString standard parameters in the target account:

- `/friday/agent/deepseek-api-key`
- `/friday/agent/telegram-bot-token`
- `/friday/agent/telegram-chat-id`
- `/friday/agent/telegram-webhook-secret`
- `/friday/agent/siri-api-key`
- `/friday/agent/worker-api-key`

`/friday/agent/logfire-token` is only required when Logfire is enabled.

Agent runtime state is now split:

- 48-hour conversation context is intentionally ephemeral
- explicit `#memory` and heavy-task checkpoints are durable and should survive account rotation
- `./scripts/backup-agent-state.sh` captures stack metadata, image digests, bucket/table metadata, and webhook URLs before account changes; it does not export plaintext secret values
- `./scripts/deploy-hands-host.sh` must be rerun on the new shared host after the Lambda stack is live so the rootless Docker worker receives the current worker key and Function URL without relying on backed-up secret env files

Current resilience improvements from the May 10, 2026 migration:

- `backup-aws-host.sh` now derives the active EC2 host from the local WireGuard endpoint, then discovers the matching instance ID and security group through AWS instead of relying on stale hardcoded values.
- `prepare-migration-day.sh` now auto-switches to offline-restore mode when the current Iris host is already dead but a valid saved backup exists.
- `migrate-aws-account.sh` now re-runs preflight internally, can continue from the latest saved backup if the old host dies before a fresh backup completes, and runs a combined post-cutover smoke test automatically.
- `restore-host-from-backup.sh` now explicitly enables the `iris-backend` nginx site and removes the default site, which was the real restore gap found during the May 10 cutover.
- `aws-infra-change.sh` now exists as the default wrapper for ad hoc AWS CLI changes so Codex-managed infra changes automatically capture pre-change and post-change shared-host backups.
- `docs/AWS_COST_MODEL.md` is now the deterministic AWS cost source of truth and must be updated whenever migration changes the AWS topology.

What it does not do yet:

- redeploy the live Iris board automatically
- terminate the old instance automatically
- change DNS for you

Those are intentionally left manual until the new host is validated.

## Migration-Day Trigger

The intended operational gate is:

```bash
AWS_PROFILE=<new-profile> \
KEY_NAME=<new-keypair-name> \
EC2_SSH_KEY=/path/to/new-key.pem \
./scripts/prepare-migration-day.sh
```

If that script returns `READY`, then saying `migrate` should be enough context for Codex to execute the blue/green cutover workflow.

If the old host is already gone and only the saved backup remains, use:

```bash
AWS_PROFILE=<new-profile> \
KEY_NAME=<new-keypair-name> \
EC2_SSH_KEY=/path/to/new-key.pem \
./scripts/prepare-migration-day.sh --offline-restore
```

That treats the migration as a restore from `backup/aws/latest` instead of a live blue/green cutover.

If you omit `--offline-restore`, the preflight now tries to detect that state automatically and falls back to the saved backup when possible.

## Validation After Migration

Before terminating the old EC2 instance:

1. Combined smoke:
   - `EC2_SSH_KEY=/path/to/new-key.pem ./scripts/post-migration-smoke.sh NEW_IP`
2. Iris board:
   - confirm the board is pointing at the new backend URL
   - run the relevant backend/board smoke flow from the `mta-led-sign` repo

Do not terminate the old host until both Friday and Iris are validated against the new host.

## Optimizations To Make This Better

Highest value:

1. Tighten the EC2 security group permanently:
   - restrict `22/tcp` to your current IP or Tailscale exit range
   - remove `51413/udp` from the server-side group unless a real server-side listener is introduced
2. Convert the old manual VPN renewal docs to point at this shared-host workflow first.
3. Make the Friday local stack setup more parameterized:
   - move hardcoded `PUID`, `PGID`, `TZ`, `PLEX_CLAIM`, and `ADVERTISE_IP` out of tracked `docker-compose.yml`
   - add a preflight script that verifies secrets, ports, Docker, Tailscale, and launchd assumptions before first start

Secondary:

1. Add a redacted inventory export for the shared host so fresh agents can inspect the topology without touching secrets.
2. If you later accept free dynamic DNS, replace raw-IP rewrites with stable hostnames. That is optional, not required for the current model.

## Free-Tier Expiration

The exact AWS account plan and free-tier expiration are not exposed by the current local IAM access.

What is known:

- the shared EC2 instance was launched on `2025-11-10`
- local docs also point to `2025-11` as the initial setup window
- AWS changed Free Tier handling for accounts created on or after `2025-07-15`

So the current best-supported estimate is:

- if this account is still on AWS's `free plan`, likely risk window starts around `2026-05-10`
- if this account was upgraded to `paid plan`, the account will not simply shut off; you would instead incur charges when credits/free usage are exhausted

Treat that as an inference, not a billing-authoritative value. Confirm the exact plan state and date in the AWS Billing Free Tier console before the actual migration window.
