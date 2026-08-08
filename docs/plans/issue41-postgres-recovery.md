# Issue 41: Postgres recovery rehearsal

Status: implementation complete; pending Draft PR review

## Goal

Add a reusable Postgres logical backup/clean-restore verifier and a controlled
planned-promotion rehearsal for the service data path. Freeze an explicit
recovery-point objective and produce a bounded, redacted result artifact.

## Base

- Base branch: `master`
- Base commit: `6ced1c4ebde377bfc386c4173efb700acdb27416`
- Task branch: `codex/issue-41-postgres-recovery`
- Integration branch: `integration/issue41-postgres-recovery`

## Design boundary

- Connections are addressed only through validated libpq service aliases.
  Database URLs, passwords, raw command output, and host paths are never written
  to plans or result artifacts.
- Execution is a two-step flow: generate a canonical plan and SHA-256, then
  supply that exact SHA to execute. A mismatched or modified plan performs no
  command.
- Backup verification requires a clean target, restores a custom-format dump,
  and compares bounded service-table inventories. Failover requires a standby,
  acceptable replay lag, matching schema inventory, planned promotion, and a
  rollback-only write/read probe on the promoted target.
- Local tests inject command fakes and temporary files. This task does not run
  `pg_dump`, `pg_restore`, `psql`, `pg_promote`, Docker, or a real database.

## File ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue41-postgres-recovery.md`; `docs/postgres-recovery.md`; `reliability/postgres-recovery-policy.json`; `schemas/postgres-recovery-result.schema.json`; `scripts/postgres_recovery_rehearsal.py`; `tests/test_issue41_postgres_recovery.py` | Existing source, migrations, workflows, Compose files, dependencies, and all other tests |

No other agent has write ownership for this task.

## Prohibited changes

- No direct commit, merge, rebase, or push to `master`.
- No real database connection, backup, restore, promotion, failover, container,
  cloud, or network operation from this task.
- No changes to migrations, existing public APIs, dependencies, CI, Compose, or
  deployment manifests.
- No credentials, DSNs, raw SQL output, database rows, or host paths in committed
  artifacts or test output.
- No access to, execution of, or changes under `eval/` or `eval/holdout/`.

## Acceptance criteria

- The policy documents a 15-minute RPO and 30-minute RTO as initial operating
  targets, without claiming measured production performance.
- Backup execution refuses a non-clean target and succeeds only when restored
  Alembic heads and bounded critical-table row counts exactly match the source.
- Failover execution refuses a non-standby, excessive/unknown replay lag,
  inventory mismatch, failed promotion, or failed rollback-only write/read probe.
- Exact-plan confirmation is mandatory and result files are create-only,
  schema-valid, bounded, and free of service aliases, credentials, host paths,
  and raw subprocess output.
- Focused tests cover clean restore, dirty-target rejection, plan tampering,
  failover success, RPO rejection, command failure redaction, and result output.

## Required validation

```powershell
.venv\Scripts\python.exe -m unittest -v tests.test_issue41_postgres_recovery
.venv\Scripts\python.exe scripts\postgres_recovery_rehearsal.py --help
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src/code_review_agent
.venv\Scripts\python.exe scripts\verify.py
git diff --check
```

## Delivery report

- Summary: implemented exact-plan-gated backup/clean-restore verification and
  isolated planned-promotion rehearsal with frozen RPO/RTO policy and redacted
  result artifacts.
- Changed files: the six Codex-owned paths listed in the File ownership table.
- Commit: recorded in the task-branch history.
- Commands run and results: 10 focused Issue 41 tests, CLI help, ruff, script
  mypy, project mypy, and `git diff --check` passed. Focused script coverage is
  87%. The repository-wide test suite and `scripts/verify.py` both ran 1111
  tests but were blocked by 9 failures and 8 errors in existing Phase 9B/9D
  service tests; this task does not change their source or tests.
- Known risks or assumptions: a real rehearsal requires operator-managed libpq
  service definitions, Postgres client tools, an isolated clean restore target,
  a streaming replica, a verified source-fencing receipt, and a separately
  authorized maintenance window. No real database rehearsal was performed.
