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

It also includes the serverless permissions needed for the Friday personal agent stack: ECR, Lambda, DynamoDB, SQS, CloudWatch Logs, Budgets, IAM role creation/pass-through for Lambda, `/friday/agent/*` SSM parameters, and enough Friday state-table access to inspect or delete stale job rows when a broken worker run leaves bad control-plane state behind.

It also includes the IAM role and instance-profile lifecycle permissions needed to clean up and recreate the dedicated `friday-hands-worker` stack if a failed worker stack leaves behind stale IAM resources.

It also includes the Friday worker access permissions Codex needs for live debugging and rollout validation on the dedicated worker:

- `ec2-instance-connect:SendSSHPublicKey` so Codex can inject the current local SSH public key into the worker without relying on a separate `.pem`
- SSM managed-instance inspection/command permissions so Codex can inspect or repair the worker through Systems Manager when SSH is unavailable

When Codex later needs more AWS permissions, the workflow should be:

1. update [codex-migration-policy.json](/Users/kevinshah/Documents/Friday.nosync/friday-plex-stack/ops/aws/iam/codex-migration-policy.json) in the repo first
2. ask the operator to copy/paste the full policy into the `codex-migration` user
3. continue only after the operator confirms it was applied

Typical migration inputs:

```bash
AWS_PROFILE=friday-next \
AWS_REGION=us-east-1 \
KEY_NAME=<new-keypair-name> \
EC2_SSH_KEY=/path/to/new-key.pem \
./scripts/migrate-aws-account.sh --yes
```

Before running a migration that includes the agent, create the required SSM SecureString parameters in the target account and validate them:

```bash
AWS_PROFILE=<new-profile> AWS_REGION=us-east-1 ./scripts/bootstrap-agent-ssm.sh
AWS_PROFILE=<new-profile> AWS_REGION=us-east-1 ./scripts/check-agent-migration-readiness.sh
```

For the agent cost dashboard and billing alarms to work, the payer account must also have `Receive CloudWatch Billing Alerts` enabled in AWS Billing Preferences.
