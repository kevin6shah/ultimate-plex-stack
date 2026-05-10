# AWS IAM Setup For Codex

Each new free AWS account should get one dedicated IAM user for migration and debugging.

Recommended name:

- `codex-migration`

Attach this policy:

- [codex-migration-policy.json](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/iam/codex-migration-policy.json)

## Why A Separate IAM User

- keeps migration access explicit
- avoids using the root account for day-to-day work
- makes it easy to rotate CLI credentials between accounts
- keeps future Codex sessions predictable

## Setup Steps In A New AWS Account

1. Sign in to the new AWS account.
2. Create IAM user `codex-migration`.
3. Enable `Access key - Programmatic access`.
4. Attach the custom JSON policy from this folder.
5. Download the access key and secret once.
6. On this Mac, configure a profile:

```bash
aws configure --profile friday-next
```

Fill in:

- AWS Access Key ID
- AWS Secret Access Key
- Region: `us-east-1`
- Output format: `json`

7. Validate access:

```bash
AWS_PROFILE=friday-next ./scripts/check-aws-migration-readiness.sh
```

## What Else Must Exist Locally

The IAM access key is not enough by itself.

You also need:

- a key pair created in the new account
- the matching local `.pem` file

The policy now also includes `ec2:CreateKeyPair` and `ec2:DeleteKeyPair`, so Codex can create the migration key pair directly if you prefer not to do it manually in the console.

Typical migration inputs:

```bash
AWS_PROFILE=friday-next \
AWS_REGION=us-east-1 \
KEY_NAME=<new-keypair-name> \
EC2_SSH_KEY=/path/to/new-key.pem \
./scripts/migrate-aws-account.sh --yes
```
