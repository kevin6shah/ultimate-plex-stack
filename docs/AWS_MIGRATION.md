# AWS Migration

## Why This Exists

The old renewal story in this repo was incomplete.

The shared EC2 host at `54.90.132.5` is not just the WireGuard VPN for Friday. It also runs the live Iris backend for `/Users/kevinshah/Documents/mta-led-sign`.

That means a future AWS account rotation must preserve both:

- Friday VPN continuity
- Iris backend state and runtime

This document is the durable source of truth for that shared-host migration.

## Current Live Inventory

Verified on `2026-04-10` from local AWS CLI and SSH:

- AWS CLI profile: `friday-ec2`
- AWS account ID: `767582655895`
- IAM user used locally: `arn:aws:iam::767582655895:user/codex-ec2-deploy`
- Region: `us-east-1`
- EC2 instance ID: `i-0c824a5a2b18d31d5`
- Public IP: `54.90.132.5`
- Public DNS: `ec2-54-90-132-5.compute-1.amazonaws.com`
- Private IP: `172.31.20.30`
- VPC: `vpc-08b8e549449b2d539`
- Subnet: `subnet-0ebe079417aec95cb`
- Security group: `sg-09479da35bed15790`
- Key pair name: `Friday-key-pair-11102025`
- Instance type: `t3.micro`
- Launch time: `2025-11-10T07:04:42Z`
- OS: Ubuntu `24.04`

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
- [bootstrap-host.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/bootstrap-host.sh)
- [restore-host-from-backup.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/restore-host-from-backup.sh)
- [ops/aws/iam/README.md](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/iam/README.md)
- [codex-migration-policy.json](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/iam/codex-migration-policy.json)
- [backup-aws-host.sh](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/scripts/backup-aws-host.sh)
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
10. run the combined Friday + Iris smoke checks before returning success

Current resilience improvements from the May 10, 2026 migration:

- `backup-aws-host.sh` now derives the active EC2 host from the local WireGuard endpoint, then discovers the matching instance ID and security group through AWS instead of relying on stale hardcoded values.
- `prepare-migration-day.sh` now auto-switches to offline-restore mode when the current Iris host is already dead but a valid saved backup exists.
- `migrate-aws-account.sh` now re-runs preflight internally, can continue from the latest saved backup if the old host dies before a fresh backup completes, and runs a combined post-cutover smoke test automatically.
- `restore-host-from-backup.sh` now explicitly enables the `iris-backend` nginx site and removes the default site, which was the real restore gap found during the May 10 cutover.
- `aws-infra-change.sh` now exists as the default wrapper for ad hoc AWS CLI changes so Codex-managed infra changes automatically capture pre-change and post-change shared-host backups.

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
