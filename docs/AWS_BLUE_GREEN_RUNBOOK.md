# AWS Blue/Green Runbook

This runbook is for the actual account-hop day.

It assumes:

- no paid domain
- raw public IP cutover
- both Friday and Iris must survive the migration
- the old EC2 host stays up until green is validated

## Inputs To Prepare

- New AWS account already created
- New AWS CLI profile configured on this Mac
- New EC2 key pair created in the new account
- New `.pem` file present locally
- Fresh backup created from blue

Required local values:

- `AWS_PROFILE`
- `AWS_REGION`
- `KEY_NAME`
- `EC2_SSH_KEY`

## Preflight

1. Confirm blue is healthy.

```bash
./scripts/check-vpn.sh
./scripts/check-stack.sh
curl http://54.90.132.5/api/iris/preferences
```

2. Take a fresh shared-host backup.

```bash
./scripts/backup-aws-host.sh
```

3. Validate the new AWS account access.

```bash
AWS_PROFILE=<new-profile> ./scripts/check-aws-migration-readiness.sh
AWS_PROFILE=<new-profile> KEY_NAME=<new-keypair-name> EC2_SSH_KEY=/path/to/new-key.pem ./scripts/prepare-migration-day.sh
```

4. Record the blue host values from `backup/aws/latest/metadata/instance.json`.

If blue is already dead, replace the second command with:

```bash
AWS_PROFILE=<new-profile> KEY_NAME=<new-keypair-name> EC2_SSH_KEY=/path/to/new-key.pem ./scripts/prepare-migration-day.sh --offline-restore
```

In normal use, `prepare-migration-day.sh` now auto-detects that failure mode and switches to offline restore if `backup/aws/latest` is still valid.

## Green Build

Run:

```bash
AWS_PROFILE=<new-profile> \
AWS_REGION=us-east-1 \
KEY_NAME=<new-keypair-name> \
EC2_SSH_KEY=/path/to/new-key.pem \
./scripts/migrate-aws-account.sh --yes
```

What this should do:

1. create a green EC2 host
2. install WireGuard, nginx, nodejs, and npm
3. restore shared-host state
4. rewrite local Friday VPN endpoint
5. rewrite local `mta-led-sign` source references from old IP to new IP
6. run the combined post-migration smoke checks before returning success

## Green Validation

Validate by the new public IP before touching blue.

### Friday

```bash
EC2_SSH_KEY=/path/to/new-key.pem ./scripts/post-migration-smoke.sh NEW_IP
```

### Iris backend

```bash
curl http://NEW_IP/api/iris/preferences
curl http://NEW_IP/api/iris/state
ssh -i /path/to/new-key.pem ubuntu@NEW_IP 'systemctl is-active wg-quick@wg0 iris-backend nginx'
```

The standalone commands above are still useful if you need to debug the smoke script instead of just trusting its pass/fail result.

### Iris board

From `/Users/kevinshah/Documents/mta-led-sign`:

```bash
python3 scripts/regression_backend.py --url http://NEW_IP
```

If the board still points at the old backend URL, update and redeploy it during the cutover window.

## Soak Window

Keep blue alive while green runs.

Minimum recommended soak:

- `12-24 hours`

During soak:

- confirm Friday downloads still route through VPN
- confirm the Telegram VPN guard still works
- confirm Iris responds and the board reaches the backend cleanly

## Blue Teardown

Only after green is healthy:

1. terminate the old EC2 instance
2. remove its security group if it is standalone
3. keep the old backup and key material

## Rollback

If green fails:

1. do not terminate blue
2. restore the old Friday WireGuard endpoint IP if it was rewritten locally
3. restore the old Iris backend URL references locally if they were rewritten
4. debug green by direct IP until it is trustworthy

This is why the model is blue/green, not in-place replacement.
