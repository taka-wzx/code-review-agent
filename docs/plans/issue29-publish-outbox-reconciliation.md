# Issue 29: Publish Outbox Reconciliation

Status: complete

## Scope

Implement offline, fake-tested durable publication outbox behavior for guarded
Review publication. A remote publisher success must be recoverable after local
process failure, duplicate publish attempts must not produce duplicate writes,
and ambiguous outcomes must fail closed into quarantine instead of being retried
as fresh writes.

An in-flight attempt has a bounded reconciliation grace interval. This prevents
a second service instance from interpreting a live publisher request as an
unconfirmed crash; after the interval, recovery uses read-back only and never
issues a second publish write.

This task does not authorize real GitHub writes, provider calls, credential reads,
deployment changes, or protected branch mutation. Publisher tests use only in-memory
fakes and local SQLite databases.

## Single Writer Declaration

Codex owns exactly these paths for this task:

- `docs/plans/issue29-publish-outbox-reconciliation.md`
- `src/code_review_agent/approval_publish.py`
- `src/code_review_agent/database.py`
- `src/code_review_agent/service_core.py`
- `migrations/versions/0009_issue29_publish_outbox.py`
- `tests/test_issue29_publish_outbox_reconciliation.py`
- `tests/test_phase11b_github_sandbox_canary.py`

All other paths are read-only unless this contract is revised before editing. In
particular, `eval/**` and `eval/holdout/**` must not be read, run, copied, or
modified.

## Acceptance Criteria

- Publication approval prepares a durable pending outbox row before the publisher
  can execute a write.
- A crash or timeout after remote success can be reconciled by idempotency key and
  completed without issuing a duplicate publish write.
- A duplicate approval replay or restart path never creates a second remote write
  for the same idempotency key.
- A publisher failure whose outcome cannot be confirmed by read-back records the
  attempt as quarantined and leaves the job in a fail-closed terminal state.
- Publication receipts remain hash-only in durable storage.
- The Alembic migration is additive and has a downgrade.

## Validation

Run before delivery:

```powershell
python -m unittest -v tests.test_issue29_publish_outbox_reconciliation
python -m unittest -v tests.test_phase9d_approval_feedback
python -m unittest -v tests.test_phase11b_github_sandbox_canary
python -m unittest -v tests.test_phase9b_migrations
python -m ruff check .
python -m mypy src/code_review_agent
git diff --check
```

Do not run `eval/holdout` or any paid/provider/GitHub write command.
