# Phase 9H: auth-004 Failure Analysis and Runtime Hardening v1

Status: **active and frozen**

Frozen date: 2026-07-27

Baseline: `origin/master` at `2e93c04d546efce5bffded054b4e8313b9e5b9df`

Task branch: `codex/phase9h-auth004-failure-hardening-v1`

## Goal and permanent boundary

Phase 9H is an offline engineering-hardening phase. It is not a new Pilot, a rerun,
or a continuation of the auth-004 paid execution. The five auth-004 attempt-1
headlines permanently remain `failed`; no PR may be replaced and the denominator of
five may not be changed.

The frozen claim boundary remains:

```text
evidence_type=single_participant_exploratory
business_claim_allowed=false
quality_claim_allowed=false
formal_quality_status=incomplete
model_quality_status=not_measured
```

An offline fake or synthetic success is engineering evidence only. It must never be
reported as a real model success, must never alter an auth-004 receipt, and must never
open a Business or Formal Quality claim.

## Frozen source evidence

Only committed sanitized auth-004 artifacts and, if explicitly supplied, repository-
external sanitized call/trace metadata may be read. Phase 9H does not discover or
print external host paths. No repository-external metadata path was supplied at
contract freeze, so the initial analysis uses only committed sanitized evidence.

The canonical anchors are:

- canonical bundle SHA-256:
  `b76b35da978b6cef5a8de681b849755607f43985c04b5ed45f9a52eaec04e619`;
- canonical report SHA-256:
  `c8cded85d2f49182bcb5482e32b324f1cf2e307757f3507946fe042342b7cd2b`;
- public run receipt SHA-256:
  `1c413b0bab5f32678291bbf274aae1a68385fbf387b009a5bfa3aaf8aa02fd20`;
- canonical revision: 2, while revision 1 remains visible with status
  `superseded_validator_gap` and must not be deleted or overwritten.

The immutable observed outcome is five selected PRs, five headline attempts, zero
completed, five failed, and stable category
`provider_or_pipeline_RuntimeError` five times. Aggregate usage is 59 logical calls,
59 HTTP attempts, 135,781 input Tokens, 47,301 output Tokens, 113,920 cached-input
Tokens, and 1,727,156 micro-CNY. There were zero diagnostic attempts, zero successful
reruns, zero feedback-eligible Findings, and zero seconds of human Review.

## Diagnostic authorization

The user approved the following exact Phase 9H table:

| Capability | Authorized |
| --- | --- |
| Read committed sanitized auth-004 receipt/report | yes |
| Read repository-external sanitized call/trace metadata | yes, read-only, only if explicitly supplied |
| Read Prompt content | no |
| Read raw diff content | no |
| Read model response content | no |
| Read keys, identity mappings, or host paths | no |
| Real model/API call | no |
| Real paid call | no |
| GitHub API for runtime/evidence collection | no |
| Comment, Check, or publication | no |
| Deployment | no |
| Local task-branch commit | yes |
| Push task branch | yes |
| Create PR | yes |
| Transition PR to Ready | yes |
| Repository-owner merge of `master` | yes; agent merge remains forbidden |

GitHub operations are authorized only for task-branch delivery, PR/CI observation,
and Ready transition. They do not authorize evidence collection or product runtime
access.

## Failure-analysis rule

The analysis must distinguish observed facts from causes. A cause is `confirmed` only
when a sanitized fixed field or deterministic offline reproduction directly proves
it. Absence of content, timing correlation, aggregate call count, or a log fragment is
not proof. Any candidate not confirmed or ruled out by authorized evidence is
`unresolved`; prose uses the word `unknown` rather than guessing.

The mandatory candidate paths are: second empty response; Finder/Verifier step cap;
tool-call loop; text response instead of submit; malformed tool arguments; invalid-
submit limit; output-token truncation; abnormal finish reason; repeated search/read
against an empty tool root; pipeline `RuntimeError`; provider response-schema
compatibility; and local budget or timeout termination.

The canonical machine-readable analysis is
`phase9g_solo_run/phase9h-failure-analysis.json`. It must keep `no_rerun=true`,
`external_calls_made=false`, and both claim gates false.

## Safe telemetry contract

Future independently authorized runs may record only fixed enums, booleans, and
non-negative integers for:

- `pipeline_stage`;
- `stable_failure_code`;
- `finish_reason_category`;
- `response_shape_category`;
- `tool_call_present`;
- `submit_attempt_count`;
- `empty_response_count`;
- `step_count`;
- `output_limit_reached`;
- `usage_known`;
- `provider_exception_type`;
- `redaction_applied`.

No Prompt, diff, response text, tool arguments/results, exception message, identity,
credential, locator, or host path may enter a receipt or trace. Unknown provider
values map to a fixed `other`/`unknown` enum; they are never retained verbatim.

Changing `phase9g_solo_run.py` invalidates its historical auth-004 executor source
hash. That is intentional fail-closed behavior: auth-004 is finished and cannot be
resealed or rerun. Any future real executor requires a new authorization ID, runtime
hash, canonical authorization SHA-256, and denominator.

## Offline GLM compatibility acceptance

Fakes or synthetic fixtures must cover normal tool calls, one and repeated empty
responses, text-only responses, malformed tool calls, output exhaustion,
`finish_reason=length`, Finder step cap, Verifier invalid submits, search/read against
an empty tool root, immutable five-headline failure, diagnostic attempts not changing
headlines, SDK retries equal to zero, cumulative budget never rolling back, raw
content exclusion, and synthetic fixtures never opening a real gate.

These tests make no external request and cannot be called a model-quality result.

## Phase 9I-Solo-Run v2 decision boundary

Phase 9I v2 is not recommended for real execution while the auth-004 root cause is
unresolved. After the offline compatibility matrix passes and at least one stable
failure code resolves the provider/pipeline ambiguity, a separately reviewed proposal
may use:

- a new authorization ID and new canonical authorization SHA-256;
- a new deterministic cohort and therefore a new denominator, with no auth-004 PR
  replacement or backfill;
- zero SDK retries and immutable attempt-1 headlines;
- recommended ceilings of 80 logical calls, 80 HTTP attempts, 1,500,000 input Tokens,
  163,840 output Tokens, and 15,000,000 micro-CNY for five new targets;
- the complete safe telemetry field set above.

These are offline planning recommendations, not authorization. New cohort acquisition,
model access, payment, and execution remain closed until separately approved.

## Single Writer paths

Codex owns only:

- `docs/plans/phase9h-auth004-failure-hardening-v1.md`;
- `docs/phase9h-auth004-failure-hardening-v1.md`;
- `phase9g_solo_run/phase9h-failure-analysis.json`;
- `phase9g_solo_run.py`;
- `tests/test_phase9g_solo_run.py`;
- `README.md` (Phase 9H status and links only).

All auth-004 canonical evidence, superseded revision 1, production package code,
prompts, sentinels, dependencies, locks, migrations, CI, other tests, and evaluation
assets are read-only. No command may read, enumerate, execute, copy, or modify
`eval/**` or `eval/holdout/**`.

## Validation

```powershell
python -m unittest -v tests.test_phase9g_solo_run
python phase9g_solo_run.py validate-synthetic
python phase9g_solo.py validate-bundle --bundle phase9g_solo/examples/synthetic
python -m ruff check .
python -m mypy src/code_review_agent phase9g_pilot.py phase9g_solo.py phase9g_solo_run.py
python scripts/verify.py
python -m pip check
git diff --check
```

Every command is offline. A failure must be fixed or reported; it must not be bypassed.

## Delivery control

Local commit, task-branch push, PR creation, CI observation, and Ready transition are
authorized. The agent must not merge, rebase, or push `master`. Only the repository
owner may merge through the protected path. Delivery reports changed files, every
validation result, per-file disclosure findings, and remaining risks.
