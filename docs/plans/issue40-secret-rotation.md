# Issue 40: Runtime secret-manager injection and rotation

Status: complete

## Goal

Add a production-oriented, versioned file secret-manager adapter and integrate
it into real durable Review workers so provider credentials can rotate between
jobs without restarting the process. Preserve redacted telemetry and explicit
fail-closed error states.

## Base

- Base branch: `master`
- Base commit: `6ced1c4ebde377bfc386c4173efb700acdb27416`
- Task branch: `codex/issue-40-secret-rotation`
- Integration branch: `integration/issue40-secret-rotation`

## Design boundary

- `CRAG_PROVIDER_SECRET_FILE` selects one atomically replaced JSON file rendered
  by an external secret manager or sidecar. Legacy provider `*_FILE` behavior
  remains compatible when this variable is absent; mixed modes are rejected.
- The adapter reads a bounded regular file without following symlinks where the
  host supports `O_NOFOLLOW`, validates secret ID, generation, validity window,
  and restrictive write permissions, and returns an in-memory snapshot whose
  representation excludes the value.
- A thread-safe client factory loads once, reuses an unchanged generation, and
  atomically swaps to a higher generation between jobs. Existing in-flight jobs
  keep their already-issued client; new jobs never fall back to a failed,
  expired, or rolled-back rotation.
- Telemetry contains only bounded status enums, generation integers, timestamp,
  and version SHA-256. No secret, file path, exception text, or provider response
  is emitted.
- Tests use temporary files, fake clients, and existing local SQLite service
  setup. No real secret manager, provider, database service, or network call is
  authorized.

## File ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue40-secret-rotation.md`; `docs/secret-manager-rotation.md`; `src/code_review_agent/secret_manager.py`; `src/code_review_agent/llm.py`; `src/code_review_agent/worker.py`; `tests/test_issue40_secret_rotation.py` | Existing migrations, workflows, Compose/deployment files, dependencies, public CLI signatures, and all other tests |

No other agent has write ownership for this task.

## Frozen interfaces

- Existing `make_client`, worker constructors, CLI entry points, environment
  behavior without `CRAG_PROVIDER_SECRET_FILE`, dependencies, and database
  schema remain unchanged.
- No database connection rotation is claimed. SQLAlchemy engine credential
  rotation requires a separate connection-pool design and deployment contract.

## Prohibited changes

- No direct commit, merge, rebase, or push to `master`.
- No real credentials, secret-manager API, provider call, database service,
  network request, deployment, or paid operation.
- No secret value, secret-shaped token, DSN, raw exception, or host path in
  committed artifacts, telemetry, errors, or test output.
- No changes to migrations, dependencies, CI, Compose, deployment manifests, or
  existing public function signatures.
- No access to, execution of, or changes under `eval/` or `eval/holdout/`.

## Acceptance criteria

- Adapter contract tests cover secure read, malformed/oversized/expired/future
  payloads, symlink denial, and bounded error codes.
- Rotation tests prove unchanged versions reuse the client, a higher generation
  swaps clients once under concurrency, in-flight users can retain the old
  client, rollback is rejected, and a failed rotation does not silently return
  the cached client.
- Worker environment integration accepts the new mode exclusively, removes
  secret configuration from `os.environ` after preflight, and observes a later
  atomic file replacement without restarting the worker.
- Telemetry and all exposed errors remain free of values and paths.
- Existing legacy provider file tests and the complete offline validation are
  rerun without real external calls.

## Required validation

```powershell
.venv\Scripts\python.exe -m unittest -v tests.test_issue40_secret_rotation
.venv\Scripts\python.exe -m unittest -v tests.test_runtime tests.test_phase9c_durable_service
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src/code_review_agent
.venv\Scripts\python.exe scripts\verify.py
git diff --check
```

## Delivery report

- Summary: added a bounded atomic-file secret adapter, thread-safe fail-closed
  client rotation, durable-worker environment integration, redacted lifecycle
  events, bounded retry categories, contract tests, and an operator runbook.
- Changed files:
  - `docs/plans/issue40-secret-rotation.md`
  - `docs/secret-manager-rotation.md`
  - `src/code_review_agent/secret_manager.py`
  - `src/code_review_agent/llm.py`
  - `src/code_review_agent/worker.py`
  - `tests/test_issue40_secret_rotation.py`
- Commit: the task-branch delivery commit; its exact SHA is recorded in the Draft
  PR and final handoff because a commit cannot contain its own SHA.
- Commands run and results:
  - Issue 40 tests: 12/12 passed.
  - Legacy runtime and durable-worker regressions: 58/58 passed.
  - Full suite: 1,123 passed, 18 skipped by existing platform/optional conditions.
  - Ruff: clean; mypy: clean across 39 source files.
  - `scripts/verify.py`: passed 1,123 tests, the 85% coverage gate, mypy, Ruff,
    module entry point, and console entry point.
  - `git diff --check`: passed.
- Known risks or assumptions: the external injector must use atomic replacement,
  overlapping credential validity, and monotonic generations. Highest-generation
  memory is process-local, so the external manager must prevent rollback across
  worker restarts. POSIX write permissions are checked directly; Windows DACLs
  remain a deployment responsibility. No live provider or external secret
  manager was exercised, and database connection rotation remains out of scope.
- Deviations: none.
