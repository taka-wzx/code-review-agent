# Issue 42: Provider failure and long-job heartbeat load harness

Status: in progress

## Goal

Add an offline, deterministic load harness that exercises durable worker retry
and heartbeat behavior for simulated provider 429, 5xx, and cancelled-request
failures plus a long-running job. It must prove one side effect per logical job
and no lease-expiry recovery while the long job is heartbeated.

## Base

- Base branch: `master`
- Base commit: `6ced1c4ebde377bfc386c4173efb700acdb27416`
- Task branch: `codex/issue-42-provider-load-harness`
- Integration branch: `integration/issue42-provider-load-harness`

## Frozen interfaces

- `ReviewWorker`, `JobStore`, retry classification, lease/heartbeat behavior,
  database schema, public APIs, and all existing scripts remain unchanged.
- The harness creates only temporary local SQLite state and never opens a
  provider, GitHub, database-service, or notification transport.
- Simulated failures are local exceptions and the simulated provider records no
  raw input, credential, repository locator, or external request.

## File ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue42-provider-load-harness.md`; `scripts/issue42_provider_load_harness.py`; `tests/test_issue42_provider_load_harness.py` | `src/code_review_agent/worker.py`; `src/code_review_agent/service_queue.py`; `src/code_review_agent/service_core.py`; `scripts/phase9c_load_test.py`; `scripts/phase9f_load_test.py`; migrations; all other repository files |

No other agent has write ownership for this task.

## Prohibited changes

- No direct commit, merge, rebase, or push to `master`.
- No changes to `src/`, migrations, dependencies, packaging, CI, deployment, or
  existing tests and load harnesses.
- No access to, execution of, or changes under `eval/` or `eval/holdout/`.
- No real provider, GitHub, database-service, notification, credential, or
  cloud calls.
- No raw job IDs, repository aliases, diffs, trace paths, tokens, or error
  messages in the machine-readable report.

## Codex assignment

- Objective: add the deterministic Issue #42 harness and focused tests only.
- Required tests:

  ```powershell
  .venv\Scripts\python.exe -m unittest -v tests.test_issue42_provider_load_harness
  .venv\Scripts\python.exe scripts\issue42_provider_load_harness.py --jobs-per-scenario 1 --timeout 15
  .venv\Scripts\python.exe -m unittest discover -s tests
  .venv\Scripts\python.exe -m ruff check .
  .venv\Scripts\python.exe -m mypy src/code_review_agent
  .venv\Scripts\python.exe scripts\verify.py
  git diff --check
  ```

## Acceptance criteria

- A deterministic simulated provider makes each 429, 5xx, and cancellation
  scenario fail once before succeeding on its retry.
- A long-running job stays on its first attempt after its initial lease would
  have expired, with a demonstrably renewed heartbeat/lease and no
  `review.lease_expired` audit event.
- Provider-usage and simulated side-effect accounting show one side effect per
  logical job and one durable usage row per attempt.
- The report is aggregate-only, JSON-serializable, identity-free, and has
  bounded arguments plus a non-zero failure path.
- Focused tests and the complete offline repository validation pass.

## Delivery report

- Summary: pending implementation.
- Changed files: pending implementation.
- Commit: pending implementation.
- Commands run and results: pending implementation.
- Known risks or assumptions: the harness verifies deterministic local worker
  behavior; it is not a load or latency measurement against a real provider.
