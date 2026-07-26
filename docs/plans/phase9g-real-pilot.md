# Phase 9G-Prep: Real Business Pilot and Formal Quality Evaluation Preparation

Status: **active and frozen**

Frozen date: 2026-07-26

Baseline: `origin/master` at `13ca600d65ee60580f4e28ce3a35617d0089f030`

Task branch: `codex/phase9g-pilot-prep`

## Goal

Prepare, but do not execute, two independent real-evidence tracks:

1. **Business Pilot**: 3--5 confirmed real developers and 20--30 authorized real pull
   requests, with review time, Finding feedback, adoption, cost, latency, completion,
   and reliability receipts.
2. **Formal Quality**: repository-separated calibration/reporting cohorts, two
   independent real annotators, a distinct real adjudicator, a pre-run gold freeze,
   one immutable headline attempt per PR, Precision/Recall/F1, and repository-stratified
   PR Bootstrap confidence intervals.

This phase delivers contracts, local standard-library tooling, deterministic cohort and
packet protocols, authorization and retention runbooks, synthetic fixtures, and offline
acceptance tests. It does not collect real data or produce a quality/business claim.

## Authorization boundary

This task authorizes only:

- contracts and offline tooling;
- participant/repository/cohort/selection protocols;
- blinded annotation and feedback packets;
- budget, retention, consent, and run plans;
- offline validators and synthetic fixtures;
- a stable task-branch commit, task-branch push, Draft PR, CI observation, Ready state,
  and merge through the PR after all required checks pass.

It does **not** authorize:

- any model, provider, paid, or other network API call;
- any real GitHub comment, Check, review, Webhook creation, or product-side write;
- any cloud/staging deployment;
- reading an unlisted repository or raw diff;
- fabricating a participant, annotator, adjudicator, consent, feedback, annotation,
  receipt, or result;
- using a model as a human participant, annotator, adjudicator, custodian, or approver;
- reading, enumerating, executing, copying, or modifying `eval/**` or
  `eval/holdout/**`.

Every future real executor must validate a completed, hash-bound authorization table
before opening restricted material or making an external call. Missing, null, stale,
inconsistent, over-budget, or unauthorized fields fail closed. The offline Prep tool
never performs an external call even when supplied with a completed table.

## Evidence separation

### Business Pilot

The business track measures workflow outcomes, not model quality. Its target is 3--5
confirmed real developers and 20--30 deterministically selected authorized real PRs.
It records:

- active human review time and paused time;
- one response per assigned Finding: `accepted`, `rejected`, `uncertain`, `fixed`, or
  `duplicate`;
- adopted Finding rate and feedback coverage;
- headline completion/failure, including failures later followed by a successful
  diagnostic rerun;
- total and per-PR cost, input/output tokens, logical calls, HTTP attempts;
- p50/p95 end-to-end latency and stable error categories.

Business feedback is product telemetry. It is not independent double annotation, is
not gold, and cannot enter a Precision/Recall/F1 denominator.

### Formal Quality

The formal track follows `docs/trusted-review-evaluation.md` and uses
`trusted_review_eval.py` as the normative quality scorer. Calibration and reporting
repositories are disjoint. Reporting inputs remain sealed from configuration tuning.
The protocol requires:

- two fixed, distinct, confirmed-real annotators A/B working independently;
- one fixed, distinct, confirmed-real adjudicator C for disagreements or uncertainty;
- a human coordinator who may bind identities but cannot decide labels;
- gold completed and externally attested before any reporting run;
- exact provider/model snapshot, temperature, pricing, runtime and cohort hashes;
- one immutable headline attempt per reporting PR; later attempts remain visible and
  cannot replace a headline failure;
- Precision/Recall/F1 and repository-stratified PR Bootstrap 95% CI;
- reporting output prohibited from prompt, threshold, sentinel, model, or context
  tuning.

If A/B/C are absent, not confirmed real, or not three different stable identities,
formal quality remains incomplete. A model may validate JSON shape and hashes only.

## Data and identity policy

- Committed plans/templates contain opaque stable IDs and hashes only. Names, email,
  account handles, repository locators, raw diffs, prompts, tokens, credentials, host
  paths, and free-form source content stay in an access-controlled external data root.
- Participant and annotator IDs must be stable pseudonyms. The identity-to-person map
  remains with the named custodian and is never passed to the model or committed.
- Consent records bind consent version, scope, UTC time, retention, withdrawal process,
  and custodian. Revoked/expired consent makes the affected work ineligible.
- Repository authorization binds an opaque repository ID, locator hash, authorizer,
  allowed tracks, diff-read authority, GitHub API/write authority, mode, expiry, and
  retention. `shadow` is the default; publish requires separate positive authority.
- Raw traces and feedback have separate retention periods. Purge receipts preserve
  only stable artifact hashes, counts, UTC time, and custodian identity.
- All timestamps use second-precision UTC `YYYY-MM-DDTHH:MM:SSZ`. Money uses integer
  micro-CNY. Raw floating provider prices are never treated as billing evidence.

## Required offline artifacts

The normative bundle contains these hash-bound artifact kinds:

1. `authorization`: the complete real-run authorization table;
2. `participants`: consented stable participant identities;
3. `repositories`: repository and external-operation authority;
4. `selection-plan`: deterministic business/formal selection rules and targets;
5. `selection-log`: all eligible and excluded candidates, never only selected PRs;
6. `cohort`: selected opaque PR/snapshot/diff identities for each evidence track;
7. `feedback-packet`: participant/PR/Finding assignment with no label filled by tooling;
8. `feedback-response`: strictly imported human feedback;
9. `review-time`: active/paused human review sessions;
10. `run-receipt`: logical calls, HTTP attempts, tokens, cost, latency, completion,
    stable error category, trace hash, and retention deadline for every attempt;
11. `annotation-packet`: independently shuffled A/B or conflict-only C assignments;
12. `annotation-response`: strictly imported human labels and rationales;
13. `gold-freeze`: Phase 9G cohort, normative trusted-Review cohort, annotation, packet,
    rubric, custodian, and external Git commit hashes frozen before reporting runs;
14. `run-manifest`: immutable headline attempt plus all non-headline attempts;
15. `business-report` and `formal-quality-report` validation receipts.

Canonical JSON uses UTF-8, sorted keys, compact separators, and no NaN/Infinity. Each
artifact's declared SHA-256 is calculated with its own hash field replaced by an empty
string. JSONL rows use the same rule. Unknown and missing fields fail closed.

## Selection and cohort materialization

- Selection occurs before any Agent output or participant feedback is inspected.
- The seed is derived as
  `SHA256(b"phase9g-pilot-selection-v1\0" + ASCII(source_commit))` from the exact
  Phase 9G-Prep merge commit; the opaque PR rank is `SHA256(seed + "\n" + pr_id)`.
  Each repository/track/role selects the lowest eligible ranks up to its frozen target.
- The full candidate log records a stable exclusion reason for every in-window
  candidate. An ineligible candidate cannot be selected.
- Business materialization requires exactly 20--30 selected PRs and 3--5 participants.
- Formal materialization keeps calibration and reporting repositories disjoint and
  keeps reporting PRs out of the business and calibration tracks. The trusted Review
  scorer's stricter 30-PR/3-repository reporting gate remains authoritative.
- Synthetic candidates may exercise every transformation but set `synthetic=true`.
  Any synthetic row propagates to every derived artifact and permanently keeps
  `quality_claim_allowed=false` and `business_claim_allowed=false`.

## Blind packets and human import

- A/B packets bind the same subject set and rubric hash but use different fixed shuffle
  seeds. They omit peer labels, model/provider identity, configuration name, scores,
  aggregate results, and whether a Finding is expected to match gold.
- A and B response templates are deliberately incomplete. The tool refuses blanks,
  duplicates, foreign IDs, stale packet hashes, extra rows, or a repeated annotator ID.
- C receives only subjects requiring adjudication plus the immutable A/B record hashes.
  C cannot be A or B and cannot return `uncertain`.
- Gold discovery/semantic identity merging remains a human coordinator task. The tool
  validates bindings and provenance but never merges two claims by semantic similarity.
- Business feedback packets use a different schema and cannot be imported as formal
  annotations. Formal annotations cannot be counted as business adoption.

## Run, budget, and failure discipline

- The authorization budget is cumulative across the entire pilot, including failed,
  timed-out, cancelled, and non-headline attempts.
- `logical_calls`, `http_attempts`, input/output tokens, and micro-CNY are validated both
  per receipt and in aggregate. `http_attempts >= logical_calls`; neither may exceed the
  authorized total.
- Every selected business, calibration, and reporting PR has exactly one headline
  attempt. It is the first registered attempt and cannot be superseded. A later success
  remains diagnostic and does not remove the headline failure from completion, latency,
  failure, or cost denominators. Formal report attempt/status denominators must equal the
  immutable `formal/reporting` headlines.
- Missing receipts are failures, not exclusions. Partial feedback remains in the
  feedback denominator. Invalid or absent cost/latency/error data fail report validation.
- `business_outcome` and `model_quality` are separate report blocks. The business report
  explicitly states that model quality was not measured. The formal report contains no
  adoption or time-saved claim.

## Real-run authorization table

The table must contain, at minimum:

- Business Pilot: participant stable IDs and real-person confirmation; repositories;
  PR count and selection rule; `shadow|publish`; real GitHub publication authority;
  publication approver; data and feedback retention days.
- Model: provider; exact model/snapshot; temperature; maximum logical calls and HTTP
  attempts; maximum input/output tokens; maximum micro-CNY; paid-call authority; raw
  diff-read authority; raw-trace retention days.
- Formal Quality: execute flag; stable A/B/C IDs; confirmation that all three are
  different real people; gold-freeze custodian; prohibition on tuning from reporting.
- Deployment/external operations: staging deploy flag and target; real GitHub API;
  comment/Check authority; local commit; task-branch push; PR; final master merge.

Presence is not permission: each Boolean must be explicit. `false` is a complete denial
and leaves the corresponding executor blocked. A complete table must be signed by a
stable human approver, carry an expiry, and match its canonical SHA-256.

## Metrics contract

All reports include counts, denominators, excluded counts, and `null` for undefined
rates.

Business metrics:

- completion = headline completed PRs / selected PRs;
- feedback coverage = unique feedback-eligible Findings with one imported response /
  unique feedback-eligible Findings;
- adoption = unique Findings whose final response is `accepted|fixed` / unique
  feedback-eligible Findings;
- fixed rate = unique `fixed` Findings / unique feedback-eligible Findings;
- review time = active seconds per selected PR, with p50/p95 over PRs;
- latency = headline end-to-end seconds over all selected PRs with a finite receipt;
- cost = all-attempt micro-CNY total, per selected PR, and per adopted Finding;
- reliability = headline failures, degraded/fail-open, retries, missing feedback, and
  stable error categories, all with full denominators.

Formal metrics are defined by `docs/trusted-review-evaluation.md` and calculated by
`trusted_review_eval.py`. Phase 9G validation additionally requires real-person
attestation, Phase 9G authorization, packet provenance, freeze-before-run timing, zero
synthetic inputs, and `reporting_results_no_tuning=true` before allowing a formal claim.

## Single Writer paths

Codex owns only:

- `docs/plans/phase9g-real-pilot.md`;
- `docs/phase9g-real-pilot.md`;
- `phase9g_pilot.py`;
- `phase9g/**`;
- `tests/test_phase9g_pilot.py`;
- `README.md` (Phase 9G status and links only).

All other paths are read-only. In particular, production package code, migrations,
dependencies, lock files, existing evaluation tools/data, CI workflows, prompts,
sentinels, and `eval/**` are frozen. The pre-existing untracked `%SystemDrive%/` path is
not owned and must not be deleted, staged, committed, or included in the diff.

## Offline validation

No command may include `--eval-assets` or contact an external service:

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $repoRoot "src"

& $python -m unittest -v tests.test_phase9g_pilot
& $python phase9g_pilot.py validate-bundle `
  --bundle phase9g/examples/synthetic
& $python -m ruff check .
& $python -m mypy src/code_review_agent phase9g_pilot.py
& $python scripts/verify.py
& $python -m pip check
git diff --check
git diff --name-only origin/master...HEAD
git status --short --branch
```

Required tests cover missing/false/stale authorization; 3--5 participant and 20--30 PR
limits; deterministic selection; repository/consent expiry; shadow/publish authority;
separate business/formal schemas; A/B/C distinctness; packet randomization and hash
binding; blank/foreign/duplicate response rejection; synthetic provenance propagation;
freeze-before-run; cumulative call/attempt/token/cost limits; immutable headline
failure; feedback/time/cost/latency/completion denominators; redaction/path rejection;
and report claim gates.

The synthetic bundle must validate structurally while returning both claim flags false
and every real executor blocked. Passing it proves only the offline protocol.

## Acceptance

- Contracts and templates cover all 15 required artifacts and the complete real-run
  authorization table.
- Every real executor is fail closed until the signed table is complete and positively
  authorizes its scope.
- No tool imports an SDK, opens a network connection, launches a subprocess, or reads a
  path component named `eval` or `holdout`.
- Synthetic fixtures cannot open a business, formal-quality, publish, deploy, raw-diff,
  or paid-call gate.
- Business outcome and model quality are never conflated.
- Three distinct confirmed-real people are mandatory for formal quality completion.
- Headline failures and all receipts remain in immutable denominators.
- All required offline validation, diff review, task-branch CI, and PR checks pass.

## Delivery control

The user authorizes a local stable commit, push of only
`codex/phase9g-pilot-prep`, Draft PR creation, CI observation, and transition to Ready
after all checks pass. Direct push, merge, or rebase of `master` remains prohibited by
the repository agent contract. The PR may be merged only through the protected GitHub
PR path by the human repository owner; after that handoff, the merge SHA and master CI
must be verified before Phase 9G-Run begins.

No real run authority is implied by merging this Prep code.

## Change control

This contract is frozen after creation. Any dependency, public package interface,
production table/state change, external call, new writable path, real data access, or
claim-semantic change requires explicit user approval and a contract revision before
implementation.
