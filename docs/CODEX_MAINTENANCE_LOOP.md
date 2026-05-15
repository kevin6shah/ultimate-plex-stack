# Codex Maintenance Loop

## Non-Negotiable Rules

1. Inspect local config, logs, and database state before making claims.
2. Prefer official docs, release notes, and primary repositories when external verification is needed.
3. If local evidence and primary sources do not support a claim, say `unknown`.
4. Do not guess at runtime behavior that can be verified locally.
5. Update the docs and journal after every material change.
6. Any Codex-managed AWS infrastructure change must take both a pre-change and post-change backup of the shared host.

## Anti-Hallucination SOP

- Extract exact log lines or config values before diagnosing.
- Ground every operational claim in one of:
  - a local file
  - a local database query
  - a cited primary external source
- If a claim cannot be supported after checking the repo and primary sources, retract it.
- If evidence conflicts, record the conflict instead of smoothing it over.

## Required Read Order

Before implementation work:

1. `docs/HANDOFF.md`
2. `docs/MAINTENANCE_JOURNAL.md`
3. `docs/TROUBLESHOOTING.md`
4. the relevant live config and logs

For Friday personal agent work, also read:

1. `docs/FRIDAY_AGENT.md`
2. `docs/AWS_MIGRATION.md`
3. `docs/AWS_MIGRATION_HISTORY.md`
4. `docs/AWS_COST_MODEL.md`
5. `ops/aws/iam/README.md`

## Change Loop

1. Inspect
2. Verify
3. Change
4. Re-test
5. Document
6. Commit and push when the repo is at a validated stable point and no local secrets are included

For AWS infrastructure work:

1. Run `./scripts/backup-aws-host.sh` before the change.
2. Prefer `./scripts/aws-infra-change.sh --reason "..." -- <command ...>` so pre/post backups happen automatically.
3. If a script already performs the backups internally, note that explicitly in the journal.
4. For Friday personal agent changes, run `./scripts/backup-agent-state.sh` before touching live agent infrastructure when the agent stack already exists.
5. If the AWS topology or service mix changes, update `docs/AWS_COST_MODEL.md` in the same change.

No step may be skipped without an explicit note in the journal.

## Research Boundaries

Allowed external sources by default:

- Docker official docs
- Servarr wiki
- Maintainerr docs
- official GitHub repositories and release notes

Secondary sources are allowed only when clearly labeled as secondary and only for non-core context.
