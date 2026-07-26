# Phase 9G Real Pilot Operator Runbook

## Current state

Phase 9G-Prep provides an offline, fail-closed protocol. It has not enrolled a
real participant, opened a real diff, called a model, used the GitHub API,
deployed a service, imported a real label, or produced a business/quality
result. The committed synthetic fixture proves only that the artifact and gate
machinery works; both claim flags remain false.

The frozen task contract is `docs/plans/phase9g-real-pilot.md`. Formal model
quality continues to use `docs/trusted-review-evaluation.md` and
`trusted_review_eval.py` as the normative scorer. Phase 9G adds human identity,
authorization, packet provenance, retention, budget, headline-attempt, and
claim gates around that scorer.

## Safety model

Keep two roots separate:

- the normal source checkout contains only code, plans, empty templates,
  opaque IDs, and hashes;
- an access-controlled data root contains the participant identity map,
  repository locator map, raw snapshots/diffs, human-visible packets,
  responses, traces, and reports.

Never copy names, email addresses, GitHub handles, repository locators, raw
diffs, prompts, credentials, tokens, host paths, or free-form source content
into a committed manifest. The offline artifacts identify them only by stable
pseudonymous IDs and SHA-256 bindings.

`phase9g_pilot.py` is standard-library-only. It does not import a network SDK,
start a subprocess, or execute a real run. It rejects an input before opening
it if any resolved path component is exactly `eval` or `holdout`.

## Artifact catalog

The compact synthetic descriptor under `phase9g/examples/synthetic/` expands
in memory into all of these artifacts and exercises every transformation:

| Artifact | Authority and purpose |
| --- | --- |
| authorization | Human-approved ceiling for every real/external operation |
| participants | 3--5 stable pseudonymous people, consent, scope, expiry, withdrawal |
| repositories | Locator hash, human authorizer, tracks, mode, access, expiry, retention |
| selection plan/log | Frozen seed/window/exclusions and the complete candidate ledger |
| cohort | Deterministic selected PR/snapshot/diff hashes |
| feedback packets/responses | Business Finding assignments and real-developer outcomes |
| review times | Active and paused human time per selected business PR |
| run receipts | All attempts, tokens, calls, HTTP attempts, cost, latency, errors, trace hash |
| run manifest | Immutable first/headline attempt and every later diagnostic attempt |
| annotation subjects/packets/responses | Blind formal A/B work and conflict-only C work |
| gold freeze | Cohort/rubric/packet/annotation hashes and external pre-run Git attestation |
| business report | Adoption, time, cost, latency, completion, feedback, failures |
| formal report receipt | Validation of the trusted Review P/R/F1 + Bootstrap report |

Every JSON object and JSONL row has an exact key set and a canonical SHA-256.
Unknown/missing keys, NaN/Infinity, duplicate IDs, stale hashes, cross-track
reuse, and incomplete required packet/cohort/headline coverage fail before
metrics. Missing optional human feedback remains an explicit measured outcome.

## Gate sequence

### 1. Complete and seal the authorization table

Copy `phase9g/authorization.template.json` into the restricted data root. Fill
every value. `false` is an explicit denial; `null`, an empty ID, or a missing
field is incomplete and cannot be treated as authority.

Seal and validate it locally:

```powershell
$python = ".\.venv\Scripts\python.exe"

& $python phase9g_pilot.py seal-authorization `
  --authorization X:\restricted\authorization.draft.json `
  --output X:\restricted\authorization.json

& $python phase9g_pilot.py validate-authorization `
  --authorization X:\restricted\authorization.json
```

The second command reports five independent scopes: `business`, `model`,
`formal_quality`, `publish`, and `deploy`. A scope is usable only when
`ready=true` and `blocked_by=[]`. A complete table may deliberately keep one
or more scopes denied.

The budget is cumulative over the entire Pilot, including failures and later
diagnostic attempts. Maximum cost is integer micro-CNY. A real executor must
stop before the next call/attempt/token if it would exceed any ceiling.

### 2. Freeze consent and repository authority

Use:

- `phase9g/templates/participant-manifest.template.json`;
- `phase9g/templates/repository-authorization.template.json`.

Participants must be 3--5 different real people, confirmed outside the model,
and represented only by stable pseudonyms. Consent covers business feedback
and review-time measurement, binds a version/expiry/retention period, and
acknowledges withdrawal. The identity custodian retains the off-repository map.

Each repository row binds an opaque ID to a locator hash and an authorizing
human. Its allowed tracks are one or more of `business`,
`formal_calibration`, and `formal_reporting`. Raw diff read, GitHub API, and
publication are separate booleans; a repository row cannot grant more than the
top-level authorization. `shadow` cannot publish.

Hash a completed template, then let full bundle validation apply the semantic
checks:

```powershell
& $python phase9g_pilot.py hash-artifact `
  --input X:\restricted\participants.draft.json `
  --hash-field manifest_sha256 `
  --output X:\restricted\participants.json
```

Repeat for repository rows (`repository_sha256`) before hashing their parent
manifest (`manifest_sha256`). The generic hash command is not approval and
does not validate semantics; it only canonicalizes hashes.

### 3. Freeze selection before model output

Use `phase9g/templates/selection-plan.template.json` and
`selection-log.template.jsonl`.

For every repository/track/role, enumerate the full in-window candidate set.
Record eligible and excluded PRs; never submit only selected rows. Derive the
seed from the exact Phase 9G-Prep merge commit so an operator cannot shop among
seeds after seeing candidates:

```text
seed = SHA256(b"phase9g-pilot-selection-v1\0" + ASCII(source_commit))
```

The fixed rank is:

```text
SHA256(selection_seed + "\n" + opaque_pr_id)
```

The selected rows must be the lowest eligible ranks up to each frozen target.
Business targets total exactly 20--30. Formal calibration and reporting
repositories are disjoint; real formal reporting retains the existing minimum
of 30 PRs from at least three repositories. Formal reporting PRs cannot appear
in the business or calibration cohort. An excluded candidate may keep
`snapshot_sha256`/`diff_sha256` null when access was not authorized or the
snapshot could not be materialized; every eligible candidate must bind both
hashes. The selector never opens a diff merely to complete an excluded row.

After hashing the plan and every log row, materialize the cohort:

```powershell
& $python phase9g_pilot.py materialize-cohort `
  --plan X:\restricted\selection-plan.json `
  --selection-log X:\restricted\selection-log.jsonl `
  --repositories X:\restricted\repositories.json `
  --expected-source-commit <exact-phase9g-prep-merge-sha> `
  --materialized-at 2026-08-01T00:00:00Z `
  --output X:\restricted\cohort.json
```

Remote candidate enumeration completeness and exclusion truth remain a human
audit boundary. Preserve query parameters, pagination counts, and collector
signature in the restricted evidence store.

### 4. Prepare the Business Pilot packets

The future executor converts each headline run's durable Finding identities
into `finding-subject` rows. These rows contain hashes only; a local UI may
display authorized Finding content by resolving the stable IDs against the
existing authenticated Finding endpoint. The packet itself never embeds raw
content.

Assign each selected PR to one consented participant. A feedback packet binds:

- participant, PR, review, and packet identities;
- every feedback-eligible Finding ID;
- immutable Finding/evidence hashes;
- packet creation time and provenance.

The response form is
`phase9g/templates/feedback-response.template.jsonl`. The participant chooses
exactly one of `accepted`, `rejected`, `uncertain`, `fixed`, or `duplicate` per
Finding. `rejected`, `uncertain`, and `duplicate` require a rationale; `fixed`
requires a non-earlier fix timestamp. Tooling rejects blank submitted fields
and duplicate, foreign, stale, or extra rows. A missing response remains
missing, stays in the full feedback denominator, is reported explicitly, and
closes the business claim gate; tooling never fabricates a replacement. A
model cannot fill the `completed_by_human` attestation.

Business feedback is not gold. Do not transform accept/reject/fixed into
`matched|invalid`, and never use it to populate a Precision/Recall denominator.

### 5. Record human time and complete receipts

`review-time.template.jsonl` records one consolidated session per selected PR:
start/end, active seconds, paused seconds, participant, and human attestation.
Active plus paused time cannot exceed wall time.

`run-receipt.template.jsonl` records every attempt, including failures:

- immutable `run_id`, evidence track/role, `pr_id`, attempt number and headline flag;
- exact provider/model snapshot/temperature;
- logical calls and actual HTTP attempts;
- input/output tokens and integer micro-CNY;
- start/end, latency, status and stable error category;
- feedback-eligible Finding IDs;
- sanitized raw-trace hash and retention deadline.

Every selected Business Pilot, formal calibration, and formal reporting PR has
exactly one attempt-1 headline. A later success is diagnostic and never becomes
headline. All attempts remain in cumulative cost/call/token/HTTP totals;
headline completion and failure always use attempt 1. Business reports use
only `business/pilot` receipts; formal reports must match the complete
`formal/reporting` headline count and status denominators.

### 6. Prepare blind Formal Quality packets

Gold candidate identity merging is performed by a human coordinator. The tool
does not use semantic similarity or a model to merge claims. After stable
subjects are prepared from the sealed reporting cohort, generate independently
shuffled A/B packets:

```powershell
& $python phase9g_pilot.py export-annotation-packets `
  --subjects X:\restricted\gold-subjects.jsonl `
  --cohort X:\restricted\cohort.json `
  --stage gold `
  --annotator-a <stable-A-id> `
  --annotator-b <stable-B-id> `
  --rubric-sha256 <rubric-sha256> `
  --seed-a 20260801 `
  --seed-b 20260802 `
  --generated-at 2026-08-02T00:00:00Z `
  --output-a X:\restricted\packet-a.json `
  --output-b X:\restricted\packet-b.json
```

A/B see the same subjects and rubric in different order. Their packets omit
peer labels, provider/model/configuration identity, scores, aggregates, and
expected gold matches. Both must finish independently. Disagreement or either
`uncertain` creates a conflict-only C packet, which binds the immutable A/B
response hashes. C must be the third authorized real person and cannot return
`uncertain`.

After the reporting run, repeat the same independent/third-person discipline
for system Findings using the labels defined in the trusted Review protocol.
Model code may check format/hashes only.

### 7. Freeze gold before any reporting run

The gold freeze binds:

- cohort, rubric, A/B packet, optional C packet, and final annotation hashes;
- the canonical hash of the separate normative trusted-Review cohort consumed
  by `trusted_review_eval.py`;
- stable custodian identity and UTC freeze time;
- an external Git commit containing only the attestation hashes.

Confirm that commit exists in a clean, access-controlled data-control branch
before the first reporting request. Every reporting run binds that exact commit
and cohort hash. A freeze always has `quality_claim_allowed=false`; at most it
can set `real_run_ready=true`. Quality is allowed only after real runs,
post-run double annotation/adjudication, and final report validation.

### 8. Validate reports without hiding failure

The bundle validator recomputes the complete business report from immutable
inputs:

```powershell
& $python phase9g_pilot.py validate-bundle `
  --bundle X:\restricted\bundle.json `
  --expected-source-commit <exact-phase9g-prep-merge-sha>
```

Business output reports:

- completion over all selected PRs;
- feedback coverage, adoption, fixed, and raw decision counts;
- active review-time p50/p95;
- headline latency p50/p95;
- all-attempt cost/call/HTTP/token totals;
- headline status, retry count, missing feedback and stable error categories.

Its `model_quality` block is always unmeasured with null P/R/F1.

Formal scoring remains:

```powershell
& $python trusted_review_eval.py report `
  --cohort X:\restricted\trusted-cohort.json `
  --selection-log X:\restricted\trusted-selection.jsonl `
  --annotations X:\restricted\trusted-annotations.jsonl `
  --runs X:\restricted\trusted-runs.jsonl `
  --config-id frozen-v1 `
  --bootstrap 10000 `
  --seed 20260718 `
  --output X:\restricted\formal-report.json

& $python phase9g_pilot.py validate-formal-report `
  --report X:\restricted\formal-report.json `
  --authorization X:\restricted\authorization.json `
  --gold-freeze X:\restricted\gold-freeze.json `
  --validated-at 2026-08-10T00:00:00Z
```

The final wrapper checks authorization, three-person identity, trusted report
version/split and exact shape, freeze commit, timing, agreement resolution,
input hashes, count-derived P/R/F1, and repository-stratified 95% Bootstrap CI
shape. The standalone command deliberately reports
`system_packet_provenance_not_bound`; it validates the scorer output but cannot
by itself open the claim gate. Only full-bundle validation, with completed
post-run system-Finding A/B packets and any required C packet bound alongside
the report, may set `quality_claim_allowed=true`. The formal receipt explicitly
says that business outcome was not measured.

## Retention and withdrawal runbook

1. At enrollment, the identity custodian records consent version, exact scope,
   retention periods, expiry, and withdrawal channel outside the repository.
2. At acquisition, the repository custodian records locator hash, snapshot/diff
   hashes, allowed tracks, operation authority, expiry, and data-retention days.
3. Store feedback separately from raw trace data so their independent retention
   clocks can be enforced.
4. On withdrawal or repository authority expiry, stop new assignment/execution
   immediately. Mark affected unreported evidence ineligible; never silently
   substitute a participant or PR after looking at results.
5. Purge raw diffs/snapshots, feedback, identity mappings, and raw traces at
   their respective deadlines. Do not shorten a frozen reporting window
   without invalidating the run.
6. Keep a purge receipt containing only artifact class, count, prior manifest
   hash, UTC purge time, custodian ID and outcome. Do not retain deleted content
   in logs or error text.
7. Aggregated reports may remain only if consent/authorization explicitly
   permits it and re-identification risk has been reviewed. Otherwise purge the
   report and retain only a non-content invalidation receipt.

## Pre-run checklist

- [ ] Authorization table is complete, hash-valid, unexpired, and the required
  `business`, `model`, and optionally `formal_quality|publish|deploy` scopes say
  `ready=true`.
- [ ] Participants are 3--5 confirmed real people with unexpired consent.
- [ ] Repository authority covers every selected track and operation.
- [ ] Full candidate log and deterministic cohort checks pass before outputs.
- [ ] Pilot mode is `shadow` unless both global and repository publication
  authority are positive and a human publish approver is named.
- [ ] Exact provider/model snapshot, temperature, pricing and cumulative
  budgets are frozen.
- [ ] A/B/C are three different confirmed real people; reporting output is
  prohibited from tuning.
- [ ] Gold attestation commit exists before reporting execution.
- [ ] First/headline attempt IDs are registered before calls; replacement runs
  are disabled.
- [ ] Restricted roots, retention deadlines, purge owner and failure storage
  are ready.
- [ ] No input or command uses `eval/` or `eval/holdout/`.

## Required authorization table for Phase 9G-Run

The next run request must provide every value below. Do not place secrets,
names, repository locators, tokens, raw diffs, prompts, or host paths in it.

```text
Business Pilot
- participant stable IDs:
- participants confirmed real: yes/no
- opaque repository IDs:
- PR count (20--30):
- frozen PR selection rule:
- mode: shadow/publish
- real GitHub publication allowed: yes/no
- stable publication approver ID:
- data retention days:
- feedback retention days:

Model
- provider:
- exact model/snapshot:
- temperature:
- maximum logical calls (entire Pilot):
- maximum HTTP attempts (entire Pilot):
- maximum input tokens (entire Pilot):
- maximum output tokens (entire Pilot):
- maximum cost micro-CNY (entire Pilot):
- real paid calls allowed: yes/no
- authorized raw diff read: yes/no
- raw trace retention days:

Formal Quality
- execute formal quality this run: yes/no
- stable annotator A ID:
- stable annotator B ID:
- stable independent adjudicator C ID:
- A/B/C confirmed as three different real people: yes/no
- stable gold-freeze custodian ID:
- reporting results prohibited from tuning: yes/no

Deployment and external operations
- staging deployment allowed: yes/no
- explicit deployment target (or null):
- real GitHub API allowed: yes/no
- GitHub comments/Checks allowed: yes/no
- local task-branch commit allowed: yes/no
- task-branch push allowed: yes/no
- PR creation allowed: yes/no
- final master merge allowed: yes/no

Attestation
- exact Phase 9G-Prep merge commit (selection anchor):
- stable human approver ID:
- approved-at UTC:
- expires-at UTC:
- authorization ID:
- canonical authorization SHA-256:
```

No real executor may start until the table is sealed and its required scope is
ready. A missing field is not a default denial that can be silently bypassed;
it is an incomplete authorization and a hard stop.
