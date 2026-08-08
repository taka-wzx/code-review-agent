# Phase 11C Gate B Local Diagnostic Preparation v1

Status: **offline preparation only; no provider request is authorized by this task**

## Identity and base

- Task branch: `codex/phase11c-gateb-local-diagnostic-v1`
- Task worktree: `.codex-worktrees/phase11c-gateb-local-diagnostic-v1`
- Gate A base commit: `72de06368672d4cc72f7750ee10cb88b6d8aee42`
- Gate A PR: `#22` (read-only; remains a completed Gate A review)
- User-selected credential delivery design: one-time local secure credential file
- Maximum aggregate ceiling: `15,000,000` micro-CNY
- Proposed accountable GitHub account: `taka-wzx` (must be re-confirmed in the
  final one-use authorization)

## Goal and strict boundary

This task creates a standalone, deterministic, offline Gate B preparation tool for
the `DIAGNOSTIC` stage only.  It validates a non-secret approval worksheet, produces
canonical SHA-256 bindings, validates *synthetic* local-file metadata, and emits a
safe blocking receipt.  It does not read any credential bytes, open a socket, import
an HTTP/provider SDK, call a provider, spend money, create cloud resources, or alter
Gate A.

The preparation tool must keep Gate B closed until a repository owner separately
approves an exact, future-bounded `DIAGNOSTIC` binding after all fields below are
frozen.  `HEADLINE_COHORT` is out of scope and always requires a later independent
approval.

## Required final DIAGNOSTIC inputs (not invented by this task)

- exact provider-policy evidence SHA-256 and current evidence date;
- exact provider/model, endpoint, tariff, request/token caps, and a budget no greater
  than the aggregate ceiling;
- exact UTC authorization start and end timestamps, with a positive bounded interval;
- accountable owner and independent kill-switch procedure;
- source/tree/image/deployment/runtime-identity/cohort/tariff/preflight bindings;
- a user-supplied explicit local credential-file path at execution time only.

The local file path, owner identity details, credential bytes, provider response,
prompt, tool arguments/results, exception messages, and host paths are never stored
in candidate artifacts, receipts, telemetry, test fixtures, or console output.

## Local secure-file policy

The future execution path is deliberately absent from this task.  The offline policy
model accepts only Linux/POSIX metadata: an absolute repository-external regular file,
with no symlinked file or ancestor, bounded length, exactly one link, root ownership,
and exact `0600` permissions.  Windows is always rejected with
`credential_platform_unsupported`; a claimed Windows ACL proof is not accepted as an
equivalent control in this version.  The preparation CLI must never open a user file.
A later execution implementation can read a file only after exact authorization and
must revalidate metadata immediately before and after opening it.

## Complete Single Writer declaration

Codex is the sole writer for exactly these files:

- `docs/plans/phase11c-gateb-local-diagnostic-v1.md`;
- `phase11c_gateb_local_diagnostic.py`;
- `schemas/phase11c-gateb-local-authorization.schema.json`;
- `schemas/phase11c-gateb-local-receipt.schema.json`;
- `tests/test_phase11c_gateb_local_diagnostic.py`.

All other files are read-only.  In particular, the Phase 11C Gate A executor and
artifacts, `src/code_review_agent/`, dependencies and lockfiles, workflows,
migrations, cloud configuration, historical executors/evidence, and every `eval/**`
asset are prohibited.

## Offline implementation requirements

- Standard-library-only canonical JSON and SHA-256; reject duplicate JSON keys,
  unexpected fields, floats, raw strings that could contain secret material, and
  malformed hashes.
- A draft authorization uses only fixed enums, booleans, non-negative integers,
  SHA-256 values, and UTC timestamps.  It binds the Gate A base commit and the
  selected `local_one_time_secure_file` delivery mode without recording a path.
- A safe receipt reports `execution_status=not_run_gate_blocked`,
  `provider_call_count=0`, `http_attempt_count=0`, and stable missing-binding codes.
- Credential-file validation is an injected metadata-only pure function.  No normal
  test may create, read, or retain a real provider credential.
- The executable has no `openai`, `http`, `requests`, `urllib`, `subprocess`, cloud
  SDK, dotenv, or provider-client import.  A future real transport requires a new
  explicit contract and exact one-use approval.

## Validation and delivery

All commands remain offline and must not access `eval/**`, credentials, provider APIs,
cloud control planes, or GitHub write APIs:

```powershell
$Python = (Resolve-Path '..\\..\\.venv\\Scripts\\python.exe').Path
$env:PYTHONPATH = (Resolve-Path 'src').Path
& $Python -m unittest -v tests.test_phase11c_gateb_local_diagnostic
& $Python phase11c_gateb_local_diagnostic.py validate-draft
& $Python -m unittest discover -s tests
& $Python -m ruff check .
& $Python -m mypy src/code_review_agent phase9g_pilot.py phase9g_solo.py phase9g_solo_run.py phase11c_gateb_local_diagnostic.py
& $Python scripts/verify.py
& $Python -m pip check
git diff --cached --check
```

Before delivery, inspect the full diff and confirm the changed-file set matches this
declaration.  A local commit is allowed after validation.  Pushing, PR creation,
cloud actions, credential-file reading, and any provider call require fresh explicit
approval after the exact final binding is presented.
