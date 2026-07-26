# Phase 9G-Solo Exploratory v1

Status: **active and frozen**

Frozen date: 2026-07-26

Baseline: `origin/master` at `61ac9cf04930a1cb0fb27bd052a327bfbc758232`

Task branch: `codex/phase9g-solo-exploratory-v1`

## Goal

Prepare a strictly limited, single-participant exploratory Review exercise for the case
where the original Phase 9G real Business Pilot and Formal Quality requirements cannot
be met. The exercise may later measure one real developer's workflow experience, cost,
latency, completion, reliability, and Finding feedback over 5--10 authorized real PRs.

This phase does not replace or weaken `docs/plans/phase9g-real-pilot.md`. It creates a
different evidence type:

- `evidence_type=single_participant_exploratory`;
- `business_claim_allowed=false` permanently;
- `quality_claim_allowed=false` permanently;
- `formal_quality_status=incomplete` permanently.

No Solo result may be backfilled into the 3--5-person Business Pilot, independent A/B/C
gold, Precision/Recall/F1, or Bootstrap quality denominators.

## Authorization boundary

This task authorizes only:

- this frozen contract and operator runbook;
- standard-library-only offline schemas, validators, report builders, and CLI;
- deliberately incomplete authorization/templates;
- deterministic 5--10 PR selection and cohort protocols;
- single-human feedback/time imports;
- immutable attempt, budget, cost, latency, and failure receipts;
- a synthetic full-protocol fixture and offline tests.

It does not authorize:

- a real model/provider/API call or paid call;
- reading a real repository, PR, snapshot, or raw diff;
- real GitHub API access, comments, Checks, reviews, or publication;
- deployment or any product-side write;
- fabricating the participant, consent, repository authority, feedback, time, receipt,
  or result;
- treating one human under several IDs, or any AI/model, as additional people;
- producing Business Pilot, formal-quality, model-quality, generalization, productivity,
  time-saved, Precision/Recall/F1, or Bootstrap claims;
- reading, enumerating, executing, copying, or modifying `eval/**` or
  `eval/holdout/**`.

A later real Solo run requires a separately completed, signed, unexpired, hash-bound
authorization. The offline tool never performs an external call, even when the table is
complete.

## Evidence contract

The Solo exercise answers only bounded engineering and usability questions:

- did the selected Review attempts complete;
- what were the headline and all-attempt cost, token, call, HTTP-attempt, latency, and
  stable failure totals;
- how much active/paused time did the single participant record;
- which feedback-eligible Findings received `accepted`, `rejected`, `uncertain`,
  `fixed`, or `duplicate` feedback;
- what feedback was missing;
- did any successful diagnostic rerun follow a headline failure.

The report must use the phrases `single-participant exploratory observation` and
`model quality not measured`. It must not contain `precision`, `recall`, `f1`,
`bootstrap_95_ci`, `business_pilot_success`, `time_saved`, or a multi-user adoption
claim.

One participant may repeat an observation after a washout period, but repeated labels
remain within-person observations. They are not independent annotation or gold. A
multi-model comparison is also exploratory robustness evidence, not a human label.

## Required artifacts

All artifacts use exact keys, canonical UTF-8 JSON, no NaN/Infinity, and SHA-256 with
the artifact's own hash field replaced by an empty string:

1. `solo-authorization`: one participant, repository/model ceilings, retention, and
   explicit external-operation denials;
2. `participant`: one confirmed-real stable pseudonym, consent, expiry, scope, and
   repository bindings;
3. `repositories`: opaque repository IDs and locator hashes, authority, expiry, and
   raw-diff access bounded by the top-level authorization;
4. `selection-plan`: 5--10 PR target, frozen window, exclusions, and seed derived from
   the exact Solo-Prep merge commit;
5. `selection-log`: the complete in-window candidate ledger, including exclusions;
6. `cohort`: deterministic selected snapshot/diff hashes;
7. `finding-subjects` and `feedback-responses`: hash-only Finding identities and
   strictly imported single-human feedback;
8. `review-times`: one consolidated human time record per selected PR;
9. `run-receipts` and `run-manifest`: every attempt with the first attempt as the sole
   immutable headline;
10. `solo-report`: bounded exploratory metrics with every prohibited claim false.

## Authorization table

The complete real Solo authorization contains:

- participant stable ID and explicit real-person confirmation;
- opaque repository IDs, PR count 5--10, selection rule, and `shadow` mode;
- provider, exact model/snapshot, frozen runtime configuration hash and temperature;
- maximum logical calls, HTTP attempts, input/output tokens, and integer micro-CNY;
- explicit real paid-call and raw-diff-read authority;
- data, feedback, and raw-trace retention days;
- explicit `false` for staging deployment, real GitHub API, comment/Check creation,
  and publication;
- stable human approver, approval/expiry timestamps, authorization ID, synthetic flag,
  and canonical authorization SHA-256.

Presence is not permission. Missing/null/stale fields fail closed. A structurally
complete table may still deny model execution when paid-call or raw-diff authority is
false.

## Selection discipline

- Selection occurs before model output or feedback is inspected.
- The seed is
  `SHA256(b"phase9g-solo-selection-v1\0" + ASCII(source_commit))`, where
  `source_commit` is the exact merge commit containing this Solo contract.
- Rank is `SHA256(seed + "\n" + opaque_pr_id)`.
- The complete candidate log must include every in-window candidate and a preregistered
  exclusion reason for every ineligible row.
- Exactly 5--10 eligible PRs are selected by the lowest frozen ranks.
- Excluded rows may leave snapshot/diff hashes null to avoid unauthorized reads;
  selected rows must bind both.
- Synthetic provenance propagates through every derived artifact and can never open a
  real-exploratory gate.

## Run, budget, and failure discipline

- Budgets are cumulative across completed, degraded, fail-open, failed, cancelled,
  timed-out, and diagnostic attempts.
- HTTP attempts must be at least logical calls and both remain below their authorized
  ceilings; token and micro-CNY totals are hard ceilings.
- Every selected PR has exactly one attempt-1 headline. Later attempts are diagnostic
  and never replace a headline.
- Missing feedback remains in the full Finding denominator.
- Every selected PR requires a time record and headline receipt. An execution that
  never reached the model is represented by an explicit zero-usage failure receipt,
  not silently excluded.
- Raw traces contain only sanitized evidence and are bound by hash and retention date.

## Metrics

The Solo report includes counts and denominators for:

- headline completion and status;
- feedback coverage, accepted/fixed observations, and raw decisions;
- active review time and headline latency distributions;
- all-attempt logical calls, HTTP attempts, tokens, cost, retries, and error categories.

`exploratory_summary_allowed` may become true only for one confirmed real participant,
5--10 real authorized PRs, complete time/headline coverage, hash-valid evidence, and an
authorization whose Solo and model scopes are ready. It still does not permit a
business/model-quality claim.

## Single Writer paths

Codex owns only:

- `docs/plans/phase9g-solo-exploratory-v1.md`;
- `docs/phase9g-solo-exploratory-v1.md`;
- `phase9g_solo.py`;
- `phase9g_solo/**`;
- `tests/test_phase9g_solo.py`;
- `README.md` (Solo status and links only).

All production code, existing Phase 9G artifacts, dependencies, CI, prompts, sentinels,
evaluation tools, and `eval/**` remain read-only. The pre-existing `%SystemDrive%/`
path is not owned and must not be read, deleted, staged, or committed.

## Offline validation

No command may include `--eval-assets` or contact an external service:

```powershell
$python = ".\.venv\Scripts\python.exe"

& $python -m unittest -v tests.test_phase9g_solo
& $python phase9g_solo.py validate-bundle --bundle phase9g_solo/examples/synthetic
& $python -m ruff check .
& $python -m mypy src/code_review_agent phase9g_pilot.py phase9g_solo.py
& $python scripts/verify.py
& $python -m pip check
git diff --check
```

Tests cover incomplete/stale authorization; one-person identity; 5--10 PR selection;
external-write denials; raw-diff/paid-call gates; consent/repository expiry; deterministic
selection and external merge-commit anchoring; cumulative budgets; immutable headline
failure; partial feedback; full time/headline denominators; synthetic propagation;
forbidden paths; exact report recomputation; and permanent business/quality claim denial.

## Acceptance

- Solo is a separate evidence type and does not revise the merged Phase 9G real-pilot
  contract.
- One real participant is necessary and sufficient only for the bounded exploratory
  summary gate.
- All real external operations remain fail closed until separately authorized.
- GitHub publish/deploy gates are structurally impossible in Solo v1.
- Synthetic evidence validates structurally while keeping every real/model/exploratory
  scope blocked.
- Business/model-quality claims and formal quality remain permanently false/incomplete.
- Offline validation and per-file diff review pass.

## Delivery control

This request authorizes implementation on `codex/phase9g-solo-exploratory-v1` only.
It does not by itself authorize a local commit, push, PR, merge, or any real Solo run.
Those operations require separate explicit user approval.

## Change control

This contract is frozen after creation. Any real data/model access, external write,
dependency, production API/table change, new writable path, or broader claim requires
an explicit contract revision and user approval before implementation.
