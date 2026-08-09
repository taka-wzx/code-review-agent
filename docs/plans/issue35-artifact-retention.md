# Issue 35: Artifact retention and hash-only receipts

Status: implementation complete; pending Draft PR review

## Goal

Provide a bounded, local retention ledger that an operator scheduler can run to
delete explicitly registered artifacts after their retention deadline. The
ledger must preserve legal holds, support dry runs and retry-safe deletion, and
retain only hash-only deletion receipts.

## Base

- Base branch: `master`
- Base commit: `6ced1c4ebde377bfc386c4173efb700acdb27416`
- Task branch: `codex/issue-35-artifact-retention`
- Integration branch: `integration/issue35-artifact-retention`

## Design boundary

- The implementation is a separate local SQLite ledger and a scheduler-facing
  script. It handles only artifacts explicitly registered under a configured
  artifact root; it does not alter existing job lineage or migrations.
- Registration, legal-hold changes, dry-run scheduling, deletion retries, and
  receipt queries are local operations. Tests use temporary directories and
  injected unlink fakes only.
- Receipts store SHA-256 values and operational metadata only. They must not
  expose artifact IDs, relative paths, file content, credentials, or host
  paths.

## File ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue35-artifact-retention.md`; `src/code_review_agent/artifact_retention.py`; `scripts/run_artifact_retention.py`; `tests/test_issue35_artifact_retention.py` | Existing source, migrations, workflows, packaging, and all other tests |

No other agent has write ownership for this task.

## Prohibited changes

- No direct commit, merge, rebase, or push to `master`.
- No real deletion outside test-owned temporary directories.
- No migration, dependency, existing public API, CI workflow, or deployment
  configuration changes.
- No access to, execution of, or changes under `eval/` or `eval/holdout/`.
- No secret, raw artifact identifier, raw artifact path, or host path in a
  committed receipt fixture or test output.

## Acceptance criteria

- A registered artifact cannot escape its configured root and is retained until
  its deadline.
- Active legal holds prevent both dry-run candidates and real deletion.
- Dry runs make no state or file changes, while scheduled runs are skipped
  until their configured interval has elapsed.
- Failed deletion attempts retry safely; a completed or recovered deletion has
  exactly one stable hash-only receipt.
- Focused tests cover schedule behavior, legal holds, dry run, retries,
  idempotency, receipt privacy, and CLI output without real external calls.

## Required validation

```powershell
.venv\Scripts\python.exe -m unittest -v tests.test_issue35_artifact_retention
.venv\Scripts\python.exe scripts\run_artifact_retention.py --help
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src/code_review_agent
.venv\Scripts\python.exe scripts\verify.py
git diff --check
```

## Delivery report

- Summary: implemented a bounded local retention ledger, scheduler-facing CLI,
  legal holds, retry-safe deletion, and verifiable hash-only receipts.
- Changed files: the four Codex-owned paths listed in the File ownership table.
- Commit: recorded in the task-branch history.
- Commands run and results: focused Issue 35 tests, CLI help, ruff, mypy,
  `git diff --check`, and focused module coverage passed. The new module has
  86% focused coverage. The repository-wide test suite and `scripts/verify.py`
  both ran 1102 tests but were blocked by 9 failures and 9 errors in existing
  Phase 9B/9D service tests; this task does not change their source or tests.
- Known risks or assumptions: an operator must configure the external scheduler
  that invokes the supplied local script; this task does not create cloud or
  orchestration resources. The repository-wide green gate remains blocked until
  the unrelated Phase 9B/9D failures are resolved and the suite is rerun in CI.
