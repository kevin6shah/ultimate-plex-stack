# AWS Cost Model

Last updated: `2026-06-15`

## Purpose

This is the deterministic AWS cost model for the Friday shared host, Iris backend, WireGuard VPN, and Friday personal agent.

Important current note:

- the older `~$12.566/month` baseline lower in this file is no longer the live June 2026 footprint
- the live account drifted away from that smaller shape
- on `2026-06-15`, AWS Budgets reported:
  - actual month-to-date spend: `$17.956`
  - forecasted month-end spend: `$39.065`
- see the June 2026 reality-check section below

Rules:

- Use a fixed `720-hour` month for every monthly compute and public IPv4 calculation.
- Use only official AWS pricing pages and AWS docs as sources.
- Treat CloudFormation templates and live AWS inventory as the architecture source of truth.
- Update this file whenever `ops/aws/*.yaml`, agent deployment topology, shared-host topology, or worker topology changes.
- Keep external model spend such as DeepSeek out of this file. This file is AWS-only.
  Historical note: this started as AWS-only, but now also includes a separate external model-cost section because DeepSeek spend is part of the real project budget.

## Current Live Inventory

Verified from the repo and the live AWS account on `2026-06-15`:

- Shared host stack: `friday-shared-host`
- Agent stack: `friday-agent`
- Dedicated hands worker stack: `friday-hands-worker`
- Region: `us-east-1`
- AWS account: `301142908919`
- Shared host instance type: `t3a.small`
- Shared host root disk: `8 GiB gp3`
- Shared host public IPv4: `1`
- Dedicated hands worker instance type: `t3a.small`
- Dedicated hands worker root disk: `20 GiB gp3`
- Dedicated hands worker public IPv4: currently attached while running
- Dedicated hands worker current state: `running`
- Dedicated hands worker instance ID: `i-04cf5a5edd3b4aa35`
- Extra stopped worker stack still retaining EBS: `friday-hands-worker-paid`
- Extra stopped worker root disk still allocated: `20 GiB gp3`
- Agent Lambda: `1`
- Agent Lambda memory: `2048 MB`
- Agent ECR repository: `friday-agent`
- Agent DynamoDB table: `friday-agent-state`
- Agent SQS queues: `2`
- Agent SNS topics: `1`
- Agent S3 artifacts bucket: `1`
- Agent CloudWatch dashboard: `1`
- Agent CloudWatch billing alarms: `4`
- Current artifacts bucket contents: empty
- Current ECR image storage proxy from live images: `4.9004 GiB`
- Migration anchor date for the current AWS account: `2026-05-10`
- Current 6-month account-expiry target: `2026-11-10`
- Current migration reminder date: `2026-10-26`

## Live Credit State

User-verified from the AWS Billing console on `2026-05-15`:

- Total amount remaining: `$120.00`
- Total amount used: `$0.00`
- Active credits: `2`
- Credit 1:
  - name: `AWS Free Tier`
  - amount: `$100.00`
  - start date: `2026-05-10`
  - expiration date: `2027-05-10`
- Credit 2:
  - name: `Explore AWS: Create a web app using AWS Lambda`
  - amount: `$20.00`
  - start date: `2026-05-14`
  - expiration date: `2027-05-10`

Interpretation:

- yes, these credits should apply automatically to eligible AWS charges
- no, the `Credits` page does not necessarily show in-month consumption immediately
- AWS documents that the `Credits` page balance is updated at the end of the billing cycle, while the `Bills` page `Savings` tab shows an estimated current-month credit balance updated every 24 hours
- User-reported current-month AWS usage so far on `2026-05-15`: `$1.39`

## June 2026 Reality Check

Live AWS Budget values on `2026-06-15`:

- actual month-to-date spend: `$17.956`
- forecasted month-end spend: `$39.065`

Using the same price inputs already documented in this file, the current live infrastructure shape reconciles to about:

```text
$38.602/month
```

That reconciliation is:

```text
shared host compute (t3a.small)     = 720 * 0.0188 = 13.536
hands worker compute (t3a.small)    = 720 * 0.0188 = 13.536
two public IPv4s                    = 2 * 720 * 0.005 = 7.200
EBS (8 + 20 + 20 GiB)               = 48 * 0.08 = 3.840
ECR (~4.9004 GiB proxy)             = 4.9004 * 0.10 = 0.490

reconciled raw monthly              = 38.602
```

Interpretation:

- the `$38` to `$39` email forecast is directionally correct
- the old lower baseline is not the current live architecture
- the main drift sources are:
  - shared host is now `t3a.small`, not `t3.micro`
  - the hands worker has effectively been running as an always-on cost line this month
  - an extra stopped worker stack is still retaining `20 GiB` of EBS

### Cost Explorer breakdown on `2026-06-15`

After enabling Cost Explorer visibility and excluding `Credit` and `Refund` record types, the June month-to-date gross spend from `2026-06-01` through `2026-06-15` is about:

```text
$18.77
```

Service breakdown:

| Service | June 1-15 gross cost |
|---|---:|
| `Amazon Elastic Compute Cloud - Compute` | `$13.0898` |
| `Amazon Virtual Private Cloud` | `$3.5000` |
| `EC2 - Other` | `$1.8400` |
| `Amazon EC2 Container Registry (ECR)` | `$0.0476` |
| `Amazon DynamoDB` | `$0.0148` |
| `Amazon Simple Storage Service` | `$0.0002` |

Usage-type breakdown for the main cost centers:

| Usage type | June 1-15 gross cost |
|---|---:|
| `BoxUsage:t3a.small` | `$13.0898` |
| `USE1-PublicIPv4:InUseAddress` | `$3.5000` |
| `EBS:VolumeUsage.gp3` | `$1.8400` |
| `TimedStorage-ByteHrs` | `$0.0476` |

Daily gross burn has been very stable at roughly:

```text
$1.27/day
```

That daily burn projects naturally to the observed monthly budget forecast near:

```text
$39/month
```

### Net-vs-gross note

The default Cost Explorer daily total can look near zero because credits are offsetting the raw charges. For operational modeling, the correct view is:

- gross AWS usage:
  - exclude `Credit` and `Refund`
- net out-of-pocket:
  - include credits if you want the post-credit billing view

For June 2026 right now:

- gross infrastructure burn is real and near `$39/month`
- net out-of-pocket is still largely cushioned by credits

## Official Price Inputs

These are the live price inputs used in the formulas below.

| Item | Live price used | Source |
|---|---:|---|
| `t3.micro` | `$0.0104/hour` | `https://aws.amazon.com/ec2/instance-types/t3/` |
| `t3a.small` | `$0.0188/hour` | `https://aws.amazon.com/ec2/instance-types/t3/` |
| Public IPv4 | `$0.005/hour` | `https://aws.amazon.com/vpc/pricing/` |
| EBS `gp3` storage | `$0.08/GB-month` | `https://aws.amazon.com/ebs/pricing/` |
| ECR private image storage | `$0.10/GB-month` | `https://aws.amazon.com/ecr/pricing/` |
| S3 Standard storage | `$0.023/GB-month` | `https://aws.amazon.com/s3/pricing` |
| Lambda requests | `1M/month free`, then `$0.20/M` | `https://aws.amazon.com/lambda/pricing/` |
| Lambda compute | `400,000 GB-s/month free` | `https://aws.amazon.com/lambda/pricing/` |
| SQS | `1M requests/month free` | `https://aws.amazon.com/sqs/faqs` |
| DynamoDB free tier | `25 GB storage + baseline monthly usage` | `https://aws.amazon.com/dynamodb/pricing/` |
| CloudWatch free tier | `3 dashboards`, `10 standard alarms`, `5 GB logs` | `https://aws.amazon.com/cloudwatch/pricing/` |
| SNS free tier | `1M publishes` | `https://aws.amazon.com/sns/faqs/` |
| SSM Parameter Store standard | `No additional charge` | `https://docs.aws.amazon.com/systems-manager/latest/userguide/parameter-store-advanced-parameters.html` |
| AWS Budgets monitoring | `Free`; first two action-enabled budgets are free | `https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/` |
| AWS KMS request usage | first `20,000 requests/month` free, then `$0.03 / 10,000 requests` | `https://aws.amazon.com/kms/pricing/` |
| Internet egress | First `100 GB/month` free across AWS services | `https://aws.amazon.com/ec2/pricing/on-demand/` |

## KMS Incident Note

On `2026-05-20`, Friday hit the AWS free-tier alert threshold for `awskms` request usage:

- alert value: `17,100 requests`
- free-tier limit: `20,000 requests/month`

Direct cost impact at the time of the alert:

```text
$0.00
```

Reason:

- AWS KMS request pricing stays free through the first `20,000 requests/month`
- the request spike was caused by repeated SecureString reads from SSM Parameter Store with `WithDecryption=True`
- the old `Settings.secret()` implementation did not cache decrypted values in-process, so repeated Siri/Telegram validation traffic amplified KMS request count

Guardrails added after this incident:

- `Settings.secret()` now memoizes decrypted SSM values for the life of the process
- the default dedicated-worker idle grace was reduced from `1800` seconds to `600` seconds so validation runs do not leave the on-demand worker up for an extra 30 minutes by default
- `scripts/stop-friday-runtime.sh` now provides a one-command operator kill switch that terminates running Temporal workflows and stops the dedicated worker instance

## External Model Price Inputs

The current Friday agent uses DeepSeek compatibility model names:

- `deepseek-chat`
- `deepseek-reasoner`

DeepSeek’s current docs state these compatibility names map to the non-thinking and thinking modes of `deepseek-v4-flash`, so the pricing model below uses the current `deepseek-v4-flash` rates.

The temporary `deepseek-v4-pro` discount is intentionally not used in budgeting because it is promotional and time-bounded.

| Item | Live price used | Source |
|---|---:|---|
| DeepSeek V4 Flash input, cache hit | `$0.0028 / 1M tokens` | `https://api-docs.deepseek.com/quick_start/pricing/` |
| DeepSeek V4 Flash input, cache miss | `$0.14 / 1M tokens` | `https://api-docs.deepseek.com/quick_start/pricing/` |
| DeepSeek V4 Flash output | `$0.28 / 1M tokens` | `https://api-docs.deepseek.com/quick_start/pricing/` |
| Context caching | enabled by default | `https://api-docs.deepseek.com/guides/kv_cache` |

## Deterministic Formula Set

### Shared host

```text
shared_host_compute = 720 * ec2_hourly_price
shared_host_public_ipv4 = 720 * public_ipv4_hourly_price
shared_host_ebs = root_volume_gib * gp3_price_per_gb_month
```

### Agent registry and storage

```text
ecr_storage = live_ecr_gib * ecr_price_per_gb_month
s3_artifacts = live_s3_gib * s3_standard_price_per_gb_month
```

### Serverless agent services

For the current personal-agent scale, these are modeled as `$0` unless usage exceeds AWS monthly free usage:

```text
lambda_requests = max(0, requests_over_1m) * 0.20 / 1_000_000
lambda_compute = max(0, gb_seconds_over_400k) * lambda_gb_second_price
sqs = $0 while <= 1M requests/month
dynamodb = $0 while within current free-tier envelope and tiny table size
cloudwatch = $0 while <= 3 dashboards, <= 10 standard alarms, <= 5 GB logs
sns = $0 while <= 1M publishes/month
ssm_standard = $0
budgets_monitoring = $0
```

### Internet egress

```text
internet_egress = max(0, total_aws_egress_gb - 100) * regional_data_transfer_price
```

For the current expected VPN usage of `21-30 GB/month`, this line item is `$0`.

### DeepSeek task formula

```text
deepseek_task_cost =
  (cache_miss_input_tokens / 1_000_000 * 0.14) +
  (cache_hit_input_tokens / 1_000_000 * 0.0028) +
  (output_tokens / 1_000_000 * 0.28)
```

### DeepSeek monthly formula

```text
deepseek_monthly_cost = tasks_per_month * deepseek_task_cost
```

## Current Baseline Architecture Cost

This is the current always-on baseline architecture as of `2026-05-17`, using the fixed 720-hour month and current live ECR storage.

It intentionally excludes the dedicated on-demand worker runtime because that worker is meant to be modeled separately by running hours, not as always-on baseline spend.

| Item | Formula | Monthly raw cost |
|---|---|---:|
| Shared host `t3.micro` | `720 * 0.0104` | `$7.488` |
| Shared host public IPv4 | `720 * 0.005` | `$3.600` |
| Shared host EBS `8 GiB gp3` | `8 * 0.08` | `$0.640` |
| Agent ECR storage `8.379806 GiB` | `8.379806 * 0.10` | `$0.838` |
| Agent S3 artifacts | bucket currently empty | `$0.000` |
| Lambda | within free tier at current scale | `$0.000` |
| SQS | within free tier at current scale | `$0.000` |
| DynamoDB | within free tier at current scale | `$0.000` |
| CloudWatch dashboard + 4 alarms + 1-day logs | within free tier at current scale | `$0.000` |
| SNS | within free tier at current scale | `$0.000` |
| SSM standard params | no additional charge | `$0.000` |
| AWS Budgets monitoring | free | `$0.000` |
| Internet egress | current modeled usage below first `100 GB/month` | `$0.000` |

### Current baseline raw monthly AWS cost

```text
$12.566/month
```

### Current baseline paid-account monthly AWS cost after monthly always-free deductions

```text
$12.566/month
```

Reason: the monthly always-free deductions already reduce the variable serverless lines to zero. The remaining nonzero baseline lines are the shared EC2 host, public IPv4, EBS, and current ECR image storage.

## Current Free-Plan Out-Of-Pocket View

The current AWS account was created on `2026-05-10`. AWS documents for post-`2025-07-15` accounts say:

- the Free account plan lasts for `6 months` or until credits are used up, whichever happens first
- on the Free account plan, you do not incur charges until you upgrade to a Paid account plan
- if the Free account plan expires and you do not upgrade, the account closes automatically

That means:

- if this account is still on the AWS Free account plan and has not exhausted its credits, the expected AWS out-of-pocket cost is:

```text
$0.000/month
```

- the raw cost is still real and should still be tracked, because it consumes the finite six-month free-plan envelope
- if credits are exhausted early, the free-plan protection ends early
- if the account has already been upgraded to a Paid account plan, the paid-account numbers above apply immediately

### Current answer to “what do I pay out of pocket right now?”

If the account is still on the Free account plan from the `2026-05-10` signup and has not been upgraded, the expected AWS out-of-pocket number is:

```text
$0.000/month
```

What still matters operationally:

- the baseline always-on architecture is consuming about `$12.566/month` of AWS value
- the migration before the free-plan expiry is mandatory if you want to preserve the `$0` out-of-pocket strategy
- durable state must migrate with the account

## Credit-Adjusted View

AWS documents:

- credits are applied automatically to eligible services until exhausted or expired
- Free Tier credits expire `12 months` after account creation
- if a free-plan account upgrades to paid, remaining credits continue to apply to future AWS bills until they expire, unless the upgrade happened through joining AWS Organizations or Control Tower

### Current credit-adjusted answer

Given the live credit balance of `$120.00` and the current baseline raw AWS burn estimate of `$12.566/month`:

```text
estimated credit runway = 120.000 / 12.566 = 9.55 months
```

That means:

- if the current baseline architecture stayed flat and all current AWS charges remained credit-eligible, the current credit balance is more than enough to cover the present raw AWS cost
- the expected out-of-pocket AWS cost right now is still:

```text
$0.000/month
```

- this remains true both while the free account plan is active and, if you later upgrade to a paid plan normally, until the credits are exhausted or hit their `2027-05-10` expiration date

Important caveat:

- the free account plan itself still expires earlier, on or around `2026-11-10`, unless credits are exhausted sooner
- if you do not upgrade the account before or shortly after that point, AWS documents that the account closes automatically
- so the credit pool and the free-plan window are related, but they are not the same date boundary

### Current-month sanity check

User-reported current-month AWS usage so far:

```text
$1.39
```

This does add up with the current architecture.

Reason:

- the raw monthly model is `$12.566/month`
- that is about `$0.419/day` on average using a 30-day month
- after only a few days of runtime since the `2026-05-10` migration, a bill around `$1.39` is directionally consistent with the model

### Current migration schedule for this account

- account/migration anchor date: `2026-05-10`
- start migration prep: `2026-10-26`
- current 6-month window ends: `2026-11-10`
- the agent stack now includes a one-time Telegram reminder rule for `2026-10-26`

## Shared-Host Only Comparison

This is the older shared-host-only comparison case.

It is no longer the preferred Friday heavy-task architecture, but it remains useful as a cost baseline.

If artifacts grow, add:

```text
artifact_cost = artifact_gib * 0.023
```

Example:

- `5 GiB` of artifacts in S3 adds:

```text
5 * 0.023 = $0.115/month
```

So a realistic shared-host POC with `5 GiB` of artifacts is:

```text
$12.566 + $0.115 = $12.681/month raw
```

## Dedicated On-Demand Worker

This is now the intended Friday heavy-task architecture.

Assumptions:

- keep the existing shared host
- add one dedicated worker instance
- worker type: `t3a.small`
- worker disk: `40 GiB gp3`
- public IPv4 only while the worker is running
- worker runtime is measured in total running hours per month

### Worker incremental formula

```text
worker_increment = (worker_hours * (0.0188 + 0.005)) + (40 * 0.08)
```

### Worker scenario table

| Worker runtime | Incremental worker cost | Total monthly raw AWS cost |
|---|---:|---:|
| `30 h/month` | `$3.914` | `$16.480` |
| `60 h/month` | `$4.628` | `$17.194` |
| `120 h/month` | `$6.056` | `$18.622` |
| `240 h/month` | `$8.912` | `$21.478` |

### Worker always-on upper bound

If the dedicated worker were left running all month with this `40 GiB` disk:

```text
(720 * (0.0188 + 0.005)) + (40 * 0.08) = $20.336/month worker-only
```

That would produce:

```text
$12.566 + $20.336 = $32.902/month raw total
```

This is intentionally not the target operating model.

Interpretation:

- if the worker is only used intermittently, this is cheaper than permanently upgrading the shared host
- if the worker is running most of the month, the economics converge toward a bigger always-on instance
- the `40 GiB` disk was chosen to support the real Playwright/MCP worker image build reliably, not to minimize pennies at the expense of repeated infra failure

## Future Horizontal Scaling

These examples assume:

- current architecture stays in place
- each additional worker is `t3a.small`
- each worker has `40 GiB gp3`
- each worker runs `60 hours/month`

| Worker count | Total monthly raw AWS cost |
|---|---:|
| `1` worker | `$17.194` |
| `2` workers | `$21.822` |
| `3` workers | `$26.450` |

This is why the current plan is:

1. prove the POC on the shared `t3.micro`
2. move to one dedicated on-demand worker only if the POC is useful
3. scale horizontally only after actual queue pressure proves the need

## DeepSeek Cost Scenarios

These scenarios are deterministic planning assumptions for long-running heavy tasks.

### Heavy 45-minute task assumptions

Baseline heavy task:

- cache-miss input: `250,000 tokens`
- cache-hit input: `750,000 tokens`
- output: `100,000 tokens`

Baseline heavy-task cost:

```text
(0.25 * 0.14) + (0.75 * 0.0028) + (0.10 * 0.28) = $0.0651 per task
```

Conservative heavy task:

- cache-miss input: `500,000 tokens`
- cache-hit input: `1,500,000 tokens`
- output: `200,000 tokens`

Conservative heavy-task cost:

```text
(0.50 * 0.14) + (1.50 * 0.0028) + (0.20 * 0.28) = $0.1302 per task
```

Interpretation:

- baseline assumes decent cache reuse and a practical browser/file tool loop
- conservative assumes roughly double the token burn and is the safer budget setting

### Monthly DeepSeek estimates

Assume `30 days/month`.

| Usage pattern | Tasks/month | Baseline model cost | Conservative model cost |
|---|---:|---:|---:|
| `5` heavy tasks/day | `150` | `$9.765` | `$19.530` |
| `10` heavy tasks/day | `300` | `$19.530` | `$39.060` |
| `12` heavy tasks/day | `360` | `$23.436` | `$46.872` |

## Full Project Cost: Shared-Host POC + DeepSeek

This section combines the current raw AWS cost with the DeepSeek scenarios above.

| Usage pattern | AWS raw | DeepSeek baseline | DeepSeek conservative | Full total baseline | Full total conservative |
|---|---:|---:|---:|---:|---:|
| `5` heavy tasks/day | `$12.566` | `$9.765` | `$19.530` | `$22.331` | `$32.096` |
| `10` heavy tasks/day | `$12.566` | `$19.530` | `$39.060` | `$32.096` | `$51.626` |

## Full Project Cost: Dedicated On-Demand Worker + DeepSeek

One `t3a.small` worker with `40 GiB` disk, `60 h/month`:

| Usage pattern | AWS raw with worker | DeepSeek baseline | DeepSeek conservative | Full total baseline | Full total conservative |
|---|---:|---:|---:|---:|---:|
| `5` heavy tasks/day | `$17.194` | `$9.765` | `$19.530` | `$26.959` | `$36.724` |
| `10` heavy tasks/day | `$17.194` | `$19.530` | `$39.060` | `$36.724` | `$56.254` |

## Recommended DeepSeek Balance / Cap

These are practical recommendations for the DeepSeek side only.

### If you want about `5` heavy 45-minute tasks/day

- locked current monthly model cap: `$12`
- safer future monthly model cap if real usage demands it: `$25`
- recommended current DeepSeek balance policy: top up in `$5` increments up to `$12` max for the month

Reason:

- `$12` matches the current operating limit you chose
- `$12` is below the baseline `5/day` estimate, which means true heavy daily use would hit the cap and throttle
- `$25` gives room for burstier tasks, weaker cache reuse, and occasional verbose runs

### If you want about `10` heavy 45-minute tasks/day

- minimum workable monthly model cap: `$25`
- safer monthly model cap: `$45`
- recommended DeepSeek balance to keep topped up: `$50-$60`

Reason:

- the baseline estimate alone is already about `$19.53/month`
- a realistic heavy-use safety margin lands closer to the conservative track

### Practical recommendation for now

For the current shared-host POC:

- keep the in-app monthly model cap at `$12`
- keep the in-app daily model cap at `$0.40`
- top up DeepSeek in `$5` increments and do not exceed `$12` total top-up for the month
- treat `5` heavy tasks/day as an upper-bound modeling scenario, not the expected day-to-day reality
- if actual usage starts hitting the `$12` cap too often, raise the cap only after reviewing the real spend trace
- raise it to `$45` only if you actually start using the agent closer to `10` heavy tasks/day
- if usage ramps hard, move to the higher caps deliberately instead of silently drifting upward

## Shared-Host Upgrade Comparison

If the shared host is upgraded from `t3.micro` to `t3a.small` instead of adding a dedicated worker:

| Scenario | Monthly raw AWS cost |
|---|---:|
| Current shared host | `$12.566` |
| Shared host upgraded to `t3a.small` | `$18.614` |
| Difference | `$6.048` |

Interpretation:

- a permanent shared-host upgrade is simpler
- a dedicated on-demand worker is more flexible and usually cheaper until heavy usage becomes frequent
- the dedicated worker is also cleaner for future horizontal scaling

## Credit Runway

This section is the dashboard-ready runway model based on the current remaining credit balance of `$120.00`.

Formula:

```text
credit_runway_months = remaining_credits_usd / monthly_raw_cost
```

### Current runway table

| Scenario | Monthly raw AWS cost | Credit runway |
|---|---:|---:|
| Current shared-host architecture | `$12.566` | `9.55 months` |
| Shared host + `5 GiB` artifacts | `$12.681` | `9.46 months` |
| Dedicated worker `30 h/month` | `$16.480` | `7.28 months` |
| Dedicated worker `60 h/month` | `$17.194` | `6.98 months` |
| Dedicated worker `120 h/month` | `$18.622` | `6.44 months` |
| Dedicated worker `240 h/month` | `$21.478` | `5.59 months` |
| Two workers, `60 h/month` each | `$21.822` | `5.50 months` |
| Three workers, `60 h/month` each | `$26.450` | `4.54 months` |

### Runway interpretation

- if runway is `> 6 months`, the current credit balance is enough to cover the current architecture beyond the next planned migration window
- if runway falls below `6 months`, the current architecture can burn credits faster than the current six-month migration cadence
- this is why a dedicated worker with a realistic `40 GiB` disk should stay well below always-on use if the goal is to preserve the current six-month migration cushion

### Current conclusion

With the current shared-host baseline architecture and your reported `$120.00` remaining credits:

- yes, you are still in a good position
- yes, the reported `$1.39` month-to-date usage is consistent with the model
- yes, the one-worker `t3a.small` path with a real `40 GiB` disk still fits within the current credit strategy if runtime stays meaningfully on-demand rather than always-on

## Migration Implications

The six-month account-rotation strategy only works if persistent agent state migrates with the account.

Must migrate:

- durable `#memory`
- thread/topic summaries
- unexpired 48-hour thread turns
- job metadata
- resumable checkpoints
- S3 artifacts
- app config such as context-window settings and persona/system-prompt settings

Do not rely on CloudWatch logs as durable state.

The migration system should continue to treat:

- CloudFormation as infra source of truth
- DynamoDB as structured state source of truth
- S3 as artifact source of truth

Migration history source of truth:

- `docs/AWS_MIGRATION_HISTORY.md`

## Maintenance Checklist

Update this file whenever any of these change:

- EC2 instance type
- EC2 disk size
- whether a dedicated worker exists
- worker instance type
- worker disk size
- presence of a public IPv4 on any new worker
- ECR image-retention policy or actual retained image count
- S3 artifact-retention policy
- any newly added AWS service
- any removed AWS service

Refresh commands:

```bash
AWS_PROFILE=iris AWS_REGION=us-east-1 /usr/local/bin/aws cloudformation describe-stacks --stack-name friday-shared-host --query 'Stacks[0].Outputs' --output json
AWS_PROFILE=iris AWS_REGION=us-east-1 /usr/local/bin/aws cloudformation describe-stacks --stack-name friday-agent --query 'Stacks[0].Outputs' --output json
AWS_PROFILE=iris AWS_REGION=us-east-1 /usr/local/bin/aws ecr describe-images --repository-name friday-agent --query 'imageDetails[].imageSizeInBytes' --output json
AWS_PROFILE=iris AWS_REGION=us-east-1 /usr/local/bin/aws s3api list-objects-v2 --bucket friday-agent-301142908919-artifacts --output json
```

When the architecture changes, update:

- this file
- `docs/AWS_MIGRATION.md`
- `docs/OPERATIONS.md`
- `docs/MAINTENANCE_JOURNAL.md`
