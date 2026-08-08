# Phase 11C Gate B Live-Diagnostic Freeze v1

Status: **implementation and offline validation only; live provider dispatch remains closed**

## Identity and current authority

- Task branch: `codex/phase11c-gateb-live-diagnostic-v1`
- Task worktree: `.codex-worktrees/phase11c-gateb-live-diagnostic-v1`
- Base commit: `8c726759adeae04d55a1368e135b50bf553f9f46`
- Gate A base commit: `72de06368672d4cc72f7750ee10cb88b6d8aee42`
- Aggregate hard ceiling: `15,000,000` micro-CNY
- Accountable owner: `taka-wzx`; the same owner is the kill-switch operator.
- Credential mode: `local_one_time_secure_file` on the target Linux ECS only.

The user authorized Gate B preparation and supplied a non-secret ECS inventory.  The
inventory establishes that Docker is available, but no current source checkout,
Dockerfile, Compose file, or lockfile is present at the historical deployment location.
It cannot be used as a Gate B deployment or image binding.  This task creates only an
offline, deterministic freeze implementation and tests; it does not create cloud
resources, upload code, alter ECS, read a credential, open a socket, invoke a provider,
or spend money.

## Goal and strict boundary

Create a standalone Gate B `DIAGNOSTIC` freeze tool.  It shall build and validate
non-secret canonical authorization, preflight, budget-reservation, approval-receipt,
and blocked-execution receipt structures; model a single sequential request with a
fake transport; and fail closed for every incomplete or drifted binding.

The tool must never provide a convenient implicit execution path.  In this task,
`run-live` always emits `not_run_gate_blocked` with zero provider and HTTP attempts.
A later task may add a real credential reader and transport only after a complete,
user-approved authorization document exists and a separate ownership declaration
explicitly permits those capabilities.

`HEADLINE_COHORT`, product GitHub writes, real business data, auth-004 reruns,
deployment, migrations, real provider responses, and any quality/business/production
claim are out of scope.

## Required final inputs that remain unbound

- current provider-policy and retention evidence SHA-256, evidence dates, and explicit
  owner acceptance;
- current standard-API tariff SHA-256, effective date, exact input/output/cached-input
  integer micro-CNY rates, and derived diagnostic sub-caps;
- a positive, future UTC window;
- a fresh target source/tree/image/rendered-deployment/runtime-identity binding;
- a separate, dedicated, revocable provider key installed only on the Linux ECS as a
  root-owned `0600`, repository-external regular file, plus a non-secret fingerprint;
- a one-use approval text exactly equal to
  `APPROVE PHASE11C DIAGNOSTIC <approval_binding_sha256>`.

The former `15:46–15:46` window is both zero-duration and expired.  It is invalid and
is never copied into an artifact.

## Complete Single Writer declaration

Codex is the sole writer for exactly these files:

- `docs/plans/phase11c-gateb-live-diagnostic-v1.md`;
- `phase11c_gateb_live_diagnostic.py`;
- `schemas/phase11c-gateb-live-diagnostic-authorization.schema.json`;
- `schemas/phase11c-gateb-live-diagnostic-preflight.schema.json`;
- `schemas/phase11c-gateb-live-diagnostic-receipt.schema.json`;
- `tests/test_phase11c_gateb_live_diagnostic.py`.

All other files are read-only.  In particular, Gate A and prior Gate B code/artifacts,
`src/code_review_agent/`, dependencies and lockfiles, workflows, migrations, cloud
configuration, historical executors/evidence, and every `eval/**` asset are prohibited.

## Offline implementation requirements

- Use only the Python standard library.  No provider SDK, HTTP client, socket, cloud
  SDK, subprocess, dotenv, credential-file reader, or real transport import is allowed.
- Canonical JSON rejects duplicate keys, floats, negative integers, unexpected fields,
  malformed hashes, and secret/path-bearing fields or values.
- A draft is permanently disabled: zero budget, `PENDING_FREEZE` non-secret bindings,
  false live flags, and a sealed SHA-256.
- A final-binding validator may validate injected, synthetic data in tests but must not
  synthesize a final binding or make a policy/tariff/window/credential decision.
- Preapproval eligibility assertions are explicit, injected, non-secret test/future-task
  inputs.  This task never discovers or infers policy acceptance, tariff currency,
  target binding validity, or window validity from an external system.
- A one-use approval is modeled through durable injected state in fakes.  Invalid text
  and drift must not consume it; a valid consumption must precede budget reservation;
  reservation must precede credential stage and the sole HTTP-attempt record; the fake
  transport verifies that order.
- Integer-only reservation uses the exact frozen tariff.  It reserves one logical call,
  one HTTP attempt, maximum input/output tokens, and worst-case non-cached cost.  Any
  unknown usage remains conservatively reserved and never rolls back after failure.
- Receipts contain only schema/version/hash/enum/boolean/integer fields.  They never
  retain prompt, response, tool payload, exception text, credential, path, hostname,
  account secret, or raw policy/tariff content.
- The only executable outcome in this task is a zero-I/O blocked receipt.  The CLI has
  no API-key, key-file, endpoint, prompt, or arbitrary output-path option.

## Acceptance tests

Tests use only fakes and synthetic hashes.  They must cover at least:

- strict canonical JSON/schema/seal validation and exact field sets;
- every Gate A/final source/image/deployment/runtime/cohort/tariff/policy/window drift;
- invalid/expired/zero-duration windows, budgets above `15,000,000`, non-integer rates,
  cached-price misuse, retry/concurrency values other than one/zero as applicable;
- Windows and all non-POSIX credential metadata rejected before any read; POSIX metadata
  requires synthetic absolute-repository-external, non-symlink ancestors, root owner,
  exact `0600`, one link, and bounded length;
- correct approval/ledger/credential/HTTP/fake-transport order, one-use CAS behavior,
  unknown usage, and zero rollback;
- fake transport only; AST guard against networking, cloud, credential, and subprocess
  imports; source scan for sentinel secret/path/prompt/response leakage;
- `run-live` always returns zero calls and zero HTTP attempts in this task.

## Validation and delivery

All commands remain offline and must not access `eval/**`, credentials, provider APIs,
cloud control planes, or GitHub write APIs:

```powershell
$Python = (Resolve-Path '..\\..\\.venv\\Scripts\\python.exe').Path
$env:PYTHONPATH = (Resolve-Path 'src').Path
& $Python -m unittest -v tests.test_phase11c_gateb_live_diagnostic
& $Python phase11c_gateb_live_diagnostic.py validate-draft
& $Python phase11c_gateb_live_diagnostic.py run-live
& $Python -m unittest discover -s tests
& $Python -m ruff check .
& $Python -m mypy src/code_review_agent phase9g_pilot.py phase9g_solo.py phase9g_solo_run.py phase11c_gateb_live_diagnostic.py
& $Python scripts/verify.py
& $Python -m pip check
git diff --cached --check
```

Before delivery, inspect the full diff and confirm it matches this declaration.  A
local commit is allowed after validation.  Pushing, PR creation, deployment, credential
reading, and provider dispatch require new explicit authorization after the exact final
binding is shown to the user.
