# Phase 11C: Provider Protocol Canary v1

Status: **Gate A active — offline engineering preparation only; Gate B real-provider
execution is closed**

## Identity and frozen entry evidence

- Task branch: `codex/phase11c-provider-canary-v1`
- Task worktree: `.codex-worktrees/phase11c-provider-canary-v1`
- Frozen implementation baseline:
  `4af4b2756e8d2de6764d08e17a6e12040e24975e`
- Required master CI: run `30451250259`, attempt `1`, conclusion `success`
- Phase 11B canonical acceptance report SHA-256:
  `354398234ee34773f26b1811ece62a5ccc7ed9fd18472adb11e1907bec25c6f7`
- Phase 11B canonical acceptance status: `accepted`
- Phase 11B authorization SHA-256:
  `73c8367ce00ce4ad77798dbd1bcbf0f3995528096b18924f2a198ba290796745`
- Phase 11B runtime-config SHA-256:
  `e1a3d3adadc78ab0b11e8d28b60ba05552c503edf7b91661e895d24cb5ea8bdc`

Before this contract was written, Codex performed one `git fetch origin`, verified
`origin/master` and the Actions run against the exact baseline, and recalculated the
attached Phase 11B report hash. All values above matched. The report itself remains
read-only and is not copied into this repository.

## Goal and claim boundary

Phase 11C creates an independent, versioned, deterministic-synthetic provider protocol
canary executor. Gate A proves only offline behavior: canonical artifacts, strict
schemas, safe telemetry, budget accounting, fail-closed preflight, and fake provider
response-shape compatibility. It does not make a provider request.

This task is not an auth-004 rerun, denominator replacement, model-quality evaluation,
Business Pilot, real-business-data workflow, product GitHub publisher test, or
production-readiness proof. It must retain all of these final claim fields:

```text
auth004_rerun=false
auth004_modified=false
auth004_replaced=false
auth004_historical_root_cause=unknown
model_quality_status=not_measured
business_claim_allowed=false
quality_claim_allowed=false
real_business_data=false
real_business_repository_writes=false
product_github_publisher_used=false
production_ready=false
```

The historical auth-004 outcome remains permanently `selected=5`,
`headline_attempts=5`, `completed=0`, `failed=5`,
`provider_or_pipeline_RuntimeError=5`, `diagnostic_attempts=0`,
`successful_reruns=0`, and `no_rerun=true`. No Phase 11C artifact may explain,
overwrite, reseal, or recategorize it.

## Authorization boundary

### Gate A — authorized by the user request

Allowed work is limited to the files declared below: documentation, independent offline
executor code, strict schemas, deterministic synthetic candidate artifacts, fakes, and
tests. Local commits, one task-branch push, one Draft PR, and observation of the
necessary CI run are authorized after the required local validation passes.

Gate A explicitly forbids real or paid provider/API calls, provider credential injection,
real model responses, Aliyun deployment changes, migrations, real headlines, business
repository writes, and product-side GitHub API/write activity. The executor must make
`run-real` fail closed without attempting credential access or network I/O.

### Gate B — not authorized

No real canary may run until a repository owner separately supplies and approves every
exact frozen binding: authorization/runtime/code/tree/image/source/deployment/cohort/
tariff/preflight SHA, runtime identity hash, current provider-policy evidence, a bounded
authorization window and owners, a secure revocable credential channel, and independent
one-use `DIAGNOSTIC` then `HEADLINE_COHORT` human approvals. Neither this contract nor
any ambient key, Phase 9H ceiling, or prior authorization authorizes a paid call.

The Gate A preflight verdict is therefore expected to be blocking, with
`canary_allowed=false` and `real_run_recommended_now=false`.

## Complete Single Writer declaration

Codex is the sole writer for exactly these files in this task:

- `docs/plans/phase11c-provider-canary-v1.md`;
- `phase11c_provider_canary.py`;
- `schemas/phase11c-provider-canary-authorization.schema.json`;
- `schemas/phase11c-provider-canary-runtime.schema.json`;
- `schemas/phase11c-provider-canary-cohort.schema.json`;
- `schemas/phase11c-provider-canary-tariff.schema.json`;
- `schemas/phase11c-provider-canary-preflight.schema.json`;
- `phase11c_provider_canary/examples/gate_a/authorization.candidate.json`;
- `phase11c_provider_canary/examples/gate_a/runtime-config.candidate.json`;
- `phase11c_provider_canary/examples/gate_a/synthetic-cohort.candidate.json`;
- `phase11c_provider_canary/examples/gate_a/tariff.candidate.json`;
- `phase11c_provider_canary/examples/gate_a/budget-proposal.candidate.json`;
- `phase11c_provider_canary/examples/gate_a/preflight-verdict.candidate.json`;
- `tests/test_phase11c_provider_canary.py`.

All other files are read-only. In particular, production runtime/public interfaces,
`src/code_review_agent/`, dependency and lock files, workflows, migrations, historical
Phase 9G/9H/auth-004 executors and evidence, Phase 11A/11B files, and every evaluation
asset are out of scope. If another file is needed, Codex must stop and request explicit
authorization before editing it.

No command may read, enumerate, run, copy, or modify `eval/**` or `eval/holdout/**`.
Auth-004 raw Prompt, diff, response, tool arguments/results, and exception messages are
also prohibited. Non-overlap checks may use only stable IDs and canonical hashes from
already-committed sanitized evidence.

## Offline design requirements

The independent executor must use standard-library canonical JSON and SHA-256 logic,
strict duplicate-key rejection, exact field sets, and a path-safe artifact root. It must
generate deterministic Gate A candidates for:

- a new authorization receipt with all Gate B-only values visibly pending;
- runtime config for `glm`, request model `glm-5.2`, `chat.completions.create`,
  `https://open.bigmodel.cn/api/paas/v4`, `open.bigmodel.cn:443`, TLS verification,
  redirect denial, zero SDK/transport retries, concurrency one, `openai==2.46.0`, and
  a generated SHA-256 binding to the current `requirements.lock` package set; its
  candidate executable-source SHA uses fixed UTF-8/LF normalization so checkout line
  endings cannot silently drift it;
- a deterministic synthetic cohort with at most five targets, a proposed headline
  denominator of three, and no auth-004 stable-ID/hash overlap;
- integer-only micro-CNY tariff and budget proposal below the Phase 9H planning ceiling;
- a canonical, machine-readable, redacted preflight verdict.

The candidate config must say `snapshot_immutability=false` for the provider alias, must
not infer provider retention behavior from local redaction, and must set
`publisher_mode=fake_dry_run`. Candidate data may contain only fixed enums, hashes,
booleans, timestamps, and non-negative integers; it may never retain prompt/diff/
response/tool/exception/credential/identity/host-path content.

Freeze and fake execution must distinguish logical calls from HTTP attempts; reserve
budget before any credential or transport action; never roll back reservations after a
failure, restart, timeout, or missing receipt; and preserve diagnostic/headline
denominator separation. Gate A fakes must not open a socket or inspect a credential.

The stable telemetry shape is restricted to fixed enum/boolean/integer fields:
`pipeline_stage`, `stable_failure_code`, `finish_reason_category`,
`response_shape_category`, `tool_call_present`, `submit_attempt_count`,
`empty_response_count`, `step_count`, `output_limit_reached`, `usage_known`,
`provider_exception_type`, and `redaction_applied`. Required pipeline stages are
`preflight`, `authorization`, `credential`, `budget_reservation`,
`provider_transport`, `response_decode`, `finder`, `verifier`, `submit`,
`receipt_reconcile`, and `cleanup`. `completed` requires a null failure code; otherwise
unknown causes remain `other`/`unknown`/`inconclusive` rather than guessed.

The response-shape fake matrix must cover normal tool-call/submit, empty and repeated
empty responses, text-only response, malformed tool call, `finish_reason` length/other,
Finder/Verifier caps, tool-call loop, provider auth/rate-limit/timeout/server/schema,
budget reservation/restart/unknown usage, retry values of zero, ambiguous request
quarantine, redaction scan, SHA drift, credential metadata validation, and fake-only
publisher enforcement.

## Candidate budget and preflight policy

All money is integer micro-CNY. The Phase 9H values are ceilings only:
80 logical calls, 80 HTTP attempts, 1,500,000 input tokens, 163,840 output tokens, and
15,000,000 micro-CNY. The candidate uses lower exact caps and a conservative cached-input
rule: reserve all input at the non-cached rate; count cached input against input caps;
avoid double counting a cached-token subset; use a cached tariff only when usage is
known and verifiable; otherwise settle conservatively at the reservation maximum.

The candidate preflight must explicitly report each missing Gate B binding as a stable
blocking code, including policy acceptance, runtime image/source/deployment/identity
freeze, authorization window/owners, secure credential handoff, kill switch, and human
approval. It must never create a real attempt, inject a credential, call a provider, or
report the phase as passed.

## Validation and delivery

All validation remains offline and uses no eval assets, credentials, paid API, real
provider, real product publisher, or deployment. Run, at minimum:

```powershell
$Python = (Resolve-Path '..\..\.venv\Scripts\python.exe').Path
$env:PYTHONPATH = (Resolve-Path 'src').Path
& $Python -m unittest -v tests.test_phase11c_provider_canary
& $Python phase11c_provider_canary.py validate-gate-a
& $Python phase11c_provider_canary.py generate-gate-a --output phase11c_provider_canary/examples/gate_a
& $Python phase9g_solo_run.py validate-synthetic
& $Python phase9g_solo.py validate-bundle --bundle phase9g_solo/examples/synthetic
& $Python -m unittest discover -s tests
& $Python -m ruff check .
& $Python -m mypy src/code_review_agent phase9g_pilot.py phase9g_solo.py phase9g_solo_run.py
& $Python scripts/verify.py
& $Python -m pip check
git diff --check
git diff --name-only 4af4b2756e8d2de6764d08e17a6e12040e24975e...HEAD
```

If Docker, Postgres, Compose, GitHub, or another unavailable external component blocks a
required command, record that fact once and do not retry merely to consume resources.
Before delivery, inspect the full diff and confirm that it is a subset of this declaration
and contains no raw content, credential, key, or host-address leakage.

After a single stable executable commit, push this task branch once and create exactly
one Draft PR. CI results bind only to that exact SHA; diagnose a failed job before any
change, use failed-job-only rerun only for demonstrated infrastructure flakiness, never
rerun all jobs, and do not merge, auto-merge, rebase, or push `master`.
