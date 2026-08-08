# Postgres backup, restore, and failover rehearsal

This runbook covers a bounded database recovery rehearsal for the CRAG service
data path. It does not automate production failover and does not establish a
measured availability SLA. Real execution requires a separately authorized
maintenance window and operator-managed Postgres infrastructure.

## Initial recovery objectives

The frozen policy is
[`reliability/postgres-recovery-policy.json`](../reliability/postgres-recovery-policy.json):

- Recovery point objective (RPO): **900 seconds (15 minutes)**.
- Recovery time objective (RTO): **1800 seconds (30 minutes)**.

The failover harness treats `clock_timestamp() -
pg_last_xact_replay_timestamp()` as a conservative replay-lag gate. A quiet
database can report an old replay timestamp even when no WAL is outstanding,
so this check can fail closed and require operator investigation. It must not be
weakened by substituting an assumed zero.

RTO timing starts after the exact plan and external source-fencing receipt have
already been supplied. It covers database inventory checks, promotion, and the
rollback-only write/read probe. It does not include incident detection, human
approval, traffic routing, DNS, application restart, or client recovery time.

These numbers are operating targets. Only repeated authorized rehearsals with
retained results can support a later reliability claim.

## Prerequisites

- PostgreSQL client tools compatible with Postgres 16: `psql`, `pg_dump`, and
  `pg_restore`.
- Valid libpq service aliases in an operator-controlled `pg_service.conf`.
  Plans accept aliases only; never put a DSN or password on the command line.
- For backup/restore, a pre-created target database with zero tables in its
  `public` schema. The harness refuses an existing schema instead of cleaning it.
- For failover, an isolated streaming replica and a SHA-256 receipt proving the
  former primary has been fenced from application writes. The plan scope is
  fixed to `isolated_rehearsal`.
- A restricted artifact directory. A custom-format dump contains real database
  data even though the result JSON contains only hashes and bounded counts.

## Two-step execution

Every operation first writes a create-only canonical plan. The printed
`plan_sha256` must be supplied unchanged to `execute`; editing any plan field
invalidates it before a command is run.

### Backup and clean restore

```powershell
python scripts/postgres_recovery_rehearsal.py plan `
  --operation backup_restore `
  --rehearsal-id backup-window-001 `
  --source-service crag_primary `
  --target-service crag_clean_restore `
  --output restricted/recovery/backup-plan.json

python scripts/postgres_recovery_rehearsal.py execute `
  --plan restricted/recovery/backup-plan.json `
  --confirmation-sha256 <exact-plan-sha256> `
  --artifact-directory restricted/recovery/dumps `
  --result-output restricted/recovery/backup-result.json
```

The verifier records source inventory, confirms that the target has no public
tables, creates a custom-format dump, restores it, and compares exact Alembic
head sets plus bounded row counts for the policy's critical service tables. A
dirty target, missing dump, command failure, or any mismatch produces no success
result.

### Isolated planned promotion

First stop application traffic to the former primary and obtain the external
fencing receipt. This repository does not implement cloud, load-balancer, or
database fencing.

```powershell
python scripts/postgres_recovery_rehearsal.py plan `
  --operation failover `
  --rehearsal-id failover-window-001 `
  --source-service crag_fenced_primary `
  --target-service crag_isolated_standby `
  --source-fence-receipt-sha256 <fence-receipt-sha256> `
  --output restricted/recovery/failover-plan.json

python scripts/postgres_recovery_rehearsal.py execute `
  --plan restricted/recovery/failover-plan.json `
  --confirmation-sha256 <exact-plan-sha256> `
  --artifact-directory restricted/recovery/dumps `
  --result-output restricted/recovery/failover-result.json
```

The harness verifies that the source reports primary mode, the target reports
standby mode, replay lag is known and within 900 seconds, and critical
inventories match. It then calls `pg_promote`, verifies recovery mode ended,
and performs a write/read probe inside one transaction that always rolls back.
It does not redirect application traffic or authorize the old primary to rejoin.

## Result handling

Successful results conform to
[`schemas/postgres-recovery-result.schema.json`](../schemas/postgres-recovery-result.schema.json).
They contain hashes of the plan, rehearsal identity, service aliases, bounded
inventories, and backup artifact plus timing and command counts. They retain no
credentials, host paths, row content, DSNs, stdout, or stderr. Result files and
plans are create-only and are written with mode `0600` on POSIX.

The backup dump is different: it contains database contents and must be
encrypted, access-controlled, retained, and destroyed according to the
operator's backup policy. A result hash is not a substitute for protecting the
dump itself.

After an isolated failover rehearsal, keep the result, destroy the rehearsal
target according to infrastructure policy, and rebuild replication from a
known primary. Never reconnect an unfenced former primary based only on a
successful harness result.
