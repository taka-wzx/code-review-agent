# Week 4: Trusted Review Evaluation

## Goal

Build an offline, auditable evaluation framework for Review Agent quality. The
framework must support a newly acquired cohort of at least 30 real pull requests
from at least three repositories, independent human annotation, adjudication,
repository-level isolation, deterministic statistics, and explicit degraded or
fail-open accounting.

Week 4 delivers the protocol, schemas, cohort preregistration, local metric
implementation, synthetic examples, tests, and documentation. It does not
claim that the real-PR cohort has already been acquired or evaluated. Network
download, external review-model calls, paid evaluation, and access to the
existing `eval/` or `eval/holdout/` assets remain prohibited unless the user
separately authorizes them.

## Base and delivery

- User-supplied historical baseline: `1bbf47b5576792097358946fe0277921de74069e`
- Clarified base policy: current latest local `master`
- Actual base commit: `9564cc817d5d0639b6c31cf4bde540594b38382d`
- Codex branch: `codex/week4-trusted-review-evaluation`
- Codex worktree:
  `E:\shiyan\code_review_agent\traces\worktrees\codex-week4`
- Planned Claude branch: `claude/week4-trusted-review-evaluation-review`
- Planned integration branch: `integration/week4-trusted-review-evaluation`

The actual base contains the Week 3 integration commit plus later Windows CI
path fixes. The user clarified that Week 4 should start from the latest
`master`, so the historical SHA is recorded for traceability but is not used as
the branch point.

No task step authorizes a merge, rebase, or push to `master`. No step
authorizes publication, PR comments, or changes to external repositories.

## Authorization boundary

Authorized local work:

- create the task contract, protocol, preregistration, schemas, examples,
  metric implementation, tests, and documentation;
- run offline tests, lint, type checks, coverage, and CLI smoke checks that do
  not inspect existing evaluation assets;
- create local task, Claude-review, and integration branches/worktrees;
- create local handoff and integration commits;
- run Claude only for the explicitly requested independent code review, without
  granting Claude access to evaluation data or credentials.

Not authorized:

- network access or downloading PR data;
- external model calls other than the explicitly requested Claude code-review
  phase;
- any paid Review or Repair evaluation;
- reading, copying, hashing, listing contents of, or running validation against
  `eval/` or `eval/holdout/`;
- modifying finder/verifier prompts, sentinel rules, runtime review behavior,
  existing evaluation scripts/assets, dependencies, lockfiles, packaging, CI,
  or public APIs;
- merging or pushing `master`.

If a validation helper would implicitly read `eval/` or `eval/holdout/`, use
the documented no-eval-assets mode or a focused command instead.

## File ownership

Codex may create or modify only:

- `docs/plans/week4-trusted-review-evaluation.md`
- `docs/trusted-review-evaluation.md`
- `trusted_review_eval.py`
- `trusted_review/cohort-plan.json`
- `trusted_review/schemas/cohort.schema.json`
- `trusted_review/schemas/annotations.schema.json`
- `trusted_review/schemas/runs.schema.json`
- `trusted_review/examples/annotations.jsonl`
- `trusted_review/examples/runs.jsonl`
- `tests/test_trusted_review_eval.py`
- `README.md`
- `AGENDA.md`

During the Claude review, Claude owns only:

- `docs/reviews/week4-claude.md`

Claude treats all implementation files as read-only and reports proposed fixes
with exact paths and tests. Codex may apply confirmed fixes during integration
only to the Codex-owned paths above. This avoids concurrent writers while
preserving independent review.

All other paths are read-only. Any ownership expansion requires a contract
update and explicit user approval before the first edit.

## Evaluation cohort preregistration

The acquisition plan contains two repository-disjoint parts:

| Role | Repository | Target | Purpose |
| --- | --- | ---: | --- |
| calibration | `pallets/click` | 10 PRs | annotation training and tool dry-runs only |
| reporting | `pallets/flask` | 10 PRs | sealed headline evaluation |
| reporting | `psf/requests` | 10 PRs | sealed headline evaluation |
| reporting | `encode/httpx` | 10 PRs | sealed headline evaluation |

Thus the sealed reporting set is planned as 30 real PRs from three repositories,
with a fourth repository supplying ten separate calibration PRs. Repository
identity, not individual PR identity, is the split unit. A repository may
appear in exactly one role.

The repository names are preregistered targets, not evidence that any PR data
has been downloaded. Acquisition requires a later user-approved network phase.
If a target is unavailable, replacement happens only before selection and is
recorded as a preregistration amendment; a reporting repository may not be
replaced after any model result from it has been inspected.

### Deterministic selection

For each repository, an authorized acquisition script or human operator will:

1. enumerate merged, non-draft PRs in the preregistered UTC window;
2. exclude dependency-only, generated-file-only, documentation-only, vendored,
   security-embargoed, inaccessible, or non-reproducible PRs;
3. require a base SHA, head SHA, merge SHA, unified diff, changed-file metadata,
   and enough repository context to review the change offline;
4. place eligible PR identities in canonical `owner/repo#number` order;
5. rank them by `SHA256(cohort_seed + "\n" + canonical_pr_id)`;
6. take the first ten eligible PRs per repository before any Agent output is
   generated or inspected.

The cohort seed, window, exclusions, inclusion decisions, snapshot hashes, and
selection-log hash are immutable after materialization. A PR may occur once.
Base/head commits and snapshot artifacts are content-addressed.

The seed is not operator-selected. Version 1 derives it as
`SHA256(b"trusted-review-cohort-v1" + b"\x00" + ASCII(source_commit))`, where
`source_commit` is the user-confirmed Week 4 base. The cohort records the
derivation method and source commit, and validation recomputes the seed.

The selection log is JSONL with one row per candidate: identity, merge time,
eligibility, a preregistered exclusion reason or null, selected flag, and the
derived rank hash. `verify-selection` binds its exact byte hash to the cohort,
recomputes ranks, and requires both the log-selected set and materialized
manifest to equal the first `target_prs` eligible rows per repository.

### Required diversity and exclusions

The materialized reporting cohort must include at least:

- three repositories and ten PRs per repository;
- two meaningful size bands per repository;
- bug-fix and non-bug-fix changes, with type hidden from the tested Agent;
- PRs with and without human review comments;
- no PR authored by the benchmark implementer;
- no PR previously used in this repository's prompts, tests, examples,
  evaluation assets, issue pilot, or manual model inspection.

Selection is outcome-blind. PRs are never included or excluded based on Agent
quality, human defect count, or whether a model found an issue.

## Data model and immutable lineage

The framework consumes three local artifacts:

1. a cohort manifest containing repository roles, immutable PR identities,
   commit and artifact hashes, selection metadata, and freeze timestamps;
2. annotation JSONL containing independent labels and, where required,
   adjudication;
3. run JSONL containing one frozen Agent run per PR, its findings, final human
   judgments, resource usage, status, and policy events.

All schemas carry an integer `schema_version`, canonical IDs, and SHA-256
bindings. Metric reports include the input file hashes, metric version, seed,
bootstrap count, split, and creation timestamp. Input order never affects
results.

Before any reporting run, an authorized data-control branch must commit a
freeze attestation containing the raw cohort/annotation hashes, canonical
cohort hash, protocol version, and primary configuration. Each run binds that
prior Git commit as `gold_freeze_commit` and the materialized cohort as
`frozen_cohort_sha256`. The offline validator checks the hash and shared
identity; an independent auditor checks the external Git ordering because
self-reported timestamps alone cannot prove it.

The reporting command fails closed when:

- fewer than three reporting repositories or 30 reporting PRs are present;
- repositories overlap calibration and reporting roles;
- PRs, snapshot hashes, or run IDs are duplicated;
- run PRs differ from the selected split;
- gold annotations were not frozen before model runs;
- required independent labels or adjudications are missing;
- an annotation references an unknown PR, finding, or gold unit;
- a reporting run declares a tuning, development, or prompt-selection purpose;
- non-finite, negative, or inconsistent telemetry is supplied.

## Annotation protocol

### Roles and blinding

Use two trained annotators, `A` and `B`, who work independently. They cannot see
each other's labels, Agent outputs during gold construction, repository split
metrics, or prior model results. Annotator IDs in committed artifacts are
pseudonyms. A third person `C` adjudicates only after both independent records
are frozen.

Annotators receive the same offline snapshot, PR intent, repository policy, and
rubric. File order is randomized independently. The Agent name, model, prompt,
and ablation are hidden.

### Stage 1: independent defect discovery

Each annotator reviews every calibration or reporting PR without Agent output
and proposes concrete, actionable defect units. A unit needs:

- stable candidate ID and PR ID;
- affected file and minimal line/range anchor;
- claim, failure mechanism, evidence, severity, and match criteria;
- discovery provenance indicating A, B, or both.

A coordinator canonicalizes the union without deciding validity. Discovery set
Jaccard and set-F1 are reported before validity adjudication.

### Stage 2: independent validity labels

Both annotators independently label every canonical gold candidate as:

- `valid_defect`
- `not_defect`
- `uncertain`

Exact agreement and Cohen's kappa are reported overall and by repository.
Severity agreement is secondary and never changes whether a unit enters the
headline recall denominator.

### Stage 3: third-party adjudication

If A and B disagree, either uses `uncertain`, or their match targets differ, C
must provide an adjudication. C may not silently delete records. The final
label, rationale, evidence, and source-label hashes are persisted. Agreed
non-uncertain pairs become final without a C record.

Only final `valid_defect` gold units frozen before any reporting run enter the
recall denominator.

### Stage 4: system-finding labels

After gold freeze and one frozen run per configuration, A and B independently
label each system finding:

- `matched` with exactly one frozen gold ID;
- `novel_valid` for a valid defect outside frozen gold;
- `invalid`;
- `duplicate` with the already matched gold ID or same-run primary novel
  finding ID;
- `unscorable` for corrupt or missing evidence;
- `uncertain`.

The same arbitration rule applies. A gold unit may be credited at most once per
PR; duplicate findings stay in the precision denominator and receive no extra
recall credit. `novel_valid` counts as valid for precision but is reported
separately and never retroactively expands the frozen recall denominator.
`unscorable` stays in the denominator as non-valid unless the entire PR is
invalidated by a preregistered data-integrity rule.

Exact repeated novel fingerprints receive at most one TP even if mislabeled:
later identical fingerprints count as duplicate FP. This makes precision
robust to repeated emission while preserving an explicit annotation route for
semantic novel duplicates.

## Metrics

### Headline Review metrics

For each PR:

- `TP_findings = matched + novel_valid`
- `FP_findings = invalid + duplicate + unscorable`
- `TP_gold = number of unique frozen gold IDs matched`
- `FN_gold = frozen valid gold units - TP_gold`
- `precision = TP_findings / (TP_findings + FP_findings)`
- `recall = TP_gold / (TP_gold + FN_gold)`
- `F1 = 2 * precision * recall / (precision + recall)`

Headline values are micro-aggregated over the sealed reporting split. Undefined
zero-denominator metrics are JSON `null`, never zero. The report also includes
per-repository micro metrics, repository-macro metrics, PR-macro metrics, raw
numerators/denominators, novel-valid count, duplicate count, and unscorable
count. Macro blocks report the number of defined repository/PR components so
undefined values are never silently omitted.

### Bootstrap 95% confidence intervals

Use a deterministic percentile bootstrap with a recorded seed and at least
10,000 replicates for a final report. The sampling unit is PR, stratified by
repository: sample ten PR records with replacement inside each reporting
repository, then recompute the complete micro metric. This preserves repository
weights and within-PR clustering of findings and gold units. The 2.5% and
97.5% endpoints use the same linear-interpolation percentile definition as the
resource distributions.

Unit tests may use fewer replicates for speed. Intervals are omitted with an
explicit reason when fewer than two PRs contribute or a metric is undefined in
every resample.

### Resource and reliability statistics

Report over all attempted PRs, including failed runs:

- cost in integer micro-USD: total, mean, median, p95, and cost per scorable PR;
- latency seconds: p50, p95, mean, and maximum;
- tool calls: total, mean, p50, p95, and optional component breakdown;
- `fail_open_rate = fail_open runs / attempted runs`;
- `degraded_rate = degraded runs / attempted runs`;
- hard-failure rate and scorable-run rate;
- test-failure rate and unauthorized-operation rate when Repair telemetry is
  present;
- status counts and explicit denominators.

`fail_open` and `degraded` are mutually exclusive primary run statuses.
Account-level authentication or rate-limit failures remain hard failures and
must not be recoded as fail-open.

## Annotation agreement statistics

The framework reports:

- exact independent-label agreement;
- Cohen's kappa for categorical validity labels;
- discovery-set Jaccard and F1;
- number and rate of arbitrated subjects;
- number of unresolved or malformed subjects;
- overall and per-repository breakdowns.

Kappa is `null` with a reason when expected agreement is one or the required
two-annotator matrix cannot be formed. Adjudicated labels never replace the two
raw independent labels in agreement calculations.

## Leakage and tuning-pollution controls

- The existing `eval/` and `eval/holdout/` are forbidden inputs and forbidden
  comparison sources for Week 4.
- Calibration and reporting repositories are disjoint. Any repo overlap is a
  fatal validation error.
- Reporting snapshots, annotations, and results live outside normal model
  development paths; only content hashes and aggregate reports may be
  committed after authorization.
- Gold is frozen before system execution. Run timestamps and purpose fields are
  validated against the freeze; the required pre-run Git attestation provides
  the external time anchor.
- Reporting data may be opened only for `annotation`, `audit`, or `final_report`
  purposes. `tuning`, `prompt_selection`, `sentinel_design`, and
  `threshold_search` are rejected.
- Version 1 accepts exactly one headline run per PR and preregistered
  configuration. It does not silently replace infrastructure failures: a
  headline failure remains a failure. Any future rerun protocol requires a
  preregistration/schema amendment before results are viewed.
- No prompt, sentinel, threshold, model, context policy, or decoding parameter
  may be changed based on reporting outcomes.
- Ablations are preregistered and run against identical snapshots. Their
  results support mechanism comparison, not post-hoc variant selection.
- Report hashes, cohort hashes, annotation hashes, exact model IDs, pricing
  revision, source commit, freeze commit, and runtime configuration provide an
  audit trail.

## Preregistered ablations

The later paid phase, if authorized, compares:

1. single Finder;
2. dual Finder;
3. context retrieval off/on;
4. Verifier off/on;
5. Review-only versus Repair Reflection where applicable;
6. exact model A versus exact model B.

All configurations are frozen before opening reporting results. The Week 4
offline implementation does not execute these ablations.

## Implementation requirements

`trusted_review_eval.py` is a repository-only, standard-library CLI and importable
module. It must:

- load and strictly validate cohort, annotation, and run records;
- verify the exact selection-log hash and deterministic per-repository
  selected set;
- enforce repository split and timeline constraints;
- resolve two-person labels plus required adjudication;
- calculate annotation agreement;
- calculate exact Review counts and metrics;
- calculate deterministic repository-stratified PR bootstrap intervals;
- calculate cost, latency, tool-call, degraded, fail-open, failure, test, and
  policy-violation statistics;
- emit JSON with input hashes, a report-generation timestamp, and deterministic
  calculation sections for fixed inputs/seed;
- make no network, subprocess, SDK, model, GitHub, or evaluation-asset calls.

The Python validator is normative. JSON Schemas are deliberately weaker
interoperability references because cross-row, cross-file, role, count, and
timeline constraints cannot all be expressed there without adding a runtime
dependency.

The implementation uses integer micro-USD for exact cost accounting. JSON
numbers must be finite. Unknown keys may be rejected where accepting them could
hide a misspelling in a metric-critical field.

## Validation

Focused development:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_trusted_review_eval
.\.venv\Scripts\python.exe -m ruff check trusted_review_eval.py tests\test_trusted_review_eval.py
```

Full offline validation, explicitly without evaluation assets:

```powershell
.\.venv\Scripts\python.exe scripts\verify.py
```

The implementation must not run:

```text
scripts\verify.py --eval-assets
eval\check_consistency.py
run_eval.py
judge.py
repeat_eval.py
replay_verifier.py
bench_verifier.py
```

Before every handoff or commit:

```powershell
git status --short
git diff --check
git diff --stat <base>...HEAD
git diff <base>...HEAD
```

The complete changed-path set must be a subset of the ownership list and must
contain no secrets, raw PR data, credentials, private paths in committed
artifacts, generated reports, or unrelated formatting.

## Automated acceptance criteria

- A valid synthetic cohort with one calibration repository and three reporting
  repositories validates without network or external models.
- A materialized headline cohort with fewer than 30 PRs or three reporting
  repositories is rejected.
- Any calibration/reporting repository overlap or duplicate PR is rejected.
- Missing second labels, required adjudication, or invalid gold/finding links
  are rejected.
- Agreement and kappa use only the two independent labels.
- Duplicate findings cannot inflate recall and reduce precision as specified.
- Repeated novel fingerprints cannot inflate precision, and a duplicate may
  reference a same-run primary novel finding.
- Novel valid findings improve precision but never mutate the frozen recall
  denominator.
- Precision, recall, F1, per-repository, macro, bootstrap CI, cost, latency,
  tool-call, fail-open, degraded, hard-failure, test-failure, and unauthorized
  rates have deterministic tests including zero denominators.
- Bootstrap is order-independent for identical canonical inputs and stable for
  a fixed seed.
- Reporting runs marked for tuning or started before gold freeze are rejected.
- Cohort seed derivation, selection-log ranking/set equality, freeze-commit
  identity, and frozen cohort hashes fail closed when inconsistent.
- Negative, non-finite, duplicate, or internally inconsistent telemetry fails
  closed.
- The full offline repository validation passes without reading existing
  evaluation assets or calling external services.

## Codex, Claude, and integration workflow

1. Codex writes this contract before implementation.
2. Codex implements only the owned paths, runs focused and full offline
   validation, reviews the complete diff, and creates one local handoff commit.
3. Claude reviews the exact handoff commit in a separate worktree. Claude may
   read the owned implementation and tests but may write only
   `docs/reviews/week4-claude.md`. The report classifies findings by severity,
   gives exact evidence, suggests tests/fixes, records commands and results,
   and creates one local Claude commit. Claude does not use network data,
   external evaluation models, paid evals, or existing eval assets.
4. Codex creates `integration/week4-trusted-review-evaluation` from the Codex
   handoff, integrates the Claude report, applies confirmed fixes within Codex
   ownership, and records rejected suggestions with rationale.
5. Codex runs the focused and full offline validation again and reviews the
   complete integration diff.
6. Stop on the validated integration branch. Merge or push to `master` only
   after explicit user confirmation.

## Claude review disposition

Claude reviewed handoff `d7aa90ac359432029bf86ba776a443427534eba0`
and recorded 13 findings in commit
`852b253a3fff12da8f2a57f4a36f9578e9fef506`.

| Finding | Disposition |
| --- | --- |
| F-1 (P1) repeated novel findings inflate precision | Fixed: exact repeated novel fingerprints receive one TP; later occurrences are duplicate FP. `duplicate` may also reference a same-run credited primary novel finding. |
| F-2 (P2) seed shopping | Fixed: seed provenance is explicit and machine-recomputed from the user-confirmed base commit with a domain-separated formula. |
| F-3 (P2) self-reported freeze timeline | Mitigated: every run binds one pre-run Git freeze commit and the canonical materialized cohort hash; the protocol requires an independently audited attestation. Git ordering remains an explicit external trust boundary. |
| F-4 (P2) unverifiable selection log | Fixed: a strict JSONL contract and `verify-selection` command bind exact bytes, recompute ranks, and compare declared and materialized selected sets. |
| F-5 (P3) silent undefined macro components | Fixed: macro blocks report defined and total repository/PR counts per metric. |
| F-6 (P3) bootstrap percentile mismatch | Fixed: CI endpoints use the same linear-interpolation percentile implementation as resource statistics. |
| F-7 (P3) zero-only unresolved/malformed fields | Clarified: successful reports identify `fail_closed_before_metrics`; invalid subjects never enter metric calculation. |
| F-8 (P3) selection before merge | Fixed: `selected_at < merged_at` is rejected. |
| F-9 (P3) noncanonical timestamps | Fixed: Python and schemas require `YYYY-MM-DDTHH:MM:SSZ`. |
| F-10 (P3) undiscovered gold injection | Fixed: every gold candidate requires `discovered=true` from at least one independent annotator. |
| F-11 (P3) schema/runtime authority ambiguity | Fixed: Python is documented as normative, schemas as weaker interoperability references; tests lock top-level property parity. |
| F-12 (P3) seed/fingerprint value reuse | Fixed: example fingerprint is independent and the seed has auditable provenance. |
| F-13 (P3) missing fail-closed regression tests | Fixed: unannotated findings, malformed JSONL, and calibration-PR annotations have regression tests. |

## Delivery record

- Codex handoff branch/SHA:
  `codex/week4-trusted-review-evaluation` /
  `d7aa90ac359432029bf86ba776a443427534eba0`
- Codex review-fix SHA:
  `a92817240ace76940e234b3ef369844e3e7e3c30`
- Claude review branch/SHA:
  `claude/week4-trusted-review-evaluation-review` /
  `852b253a3fff12da8f2a57f4a36f9578e9fef506`
- Integration branch/integrated implementation SHA:
  `integration/week4-trusted-review-evaluation` /
  `849727556f6d043d2afb0b601e32bf3edbe5b11d`
- Changed files: 13 paths relative to base—12 Codex-owned implementation,
  test, schema, plan, and documentation paths plus Claude-owned
  `docs/reviews/week4-claude.md`.
- Focused validation: 52 trusted-review tests passed; root evaluation module
  Ruff and mypy passed; preregistered cohort CLI validation returned
  `valid: true`.
- Full offline validation: 403 tests passed with 3 environment skips; total
  branch coverage 85%; Ruff, mypy for 21 package source files, module entry
  point, and console entry point passed. The command was run without
  `--eval-assets` and with this worktree's `src` first on `PYTHONPATH`.
- Claude findings and dispositions: one P1, three P2, and nine P3 findings
  were accepted; F-1/F-2/F-4 and all code-level P3 findings were fixed, while
  F-3 is mitigated by a mandatory pre-run Git freeze attestation and retained
  as an explicit external audit boundary. The full mapping is recorded above.
- External/network/paid actions performed: none. No PR data was downloaded,
  no external evaluation model was called, no paid evaluation was run, and
  no existing `eval/` or `eval/holdout/` content was read, listed, searched,
  hashed, or validated.
- Remaining risks: the real cohort is not materialized; remote candidate
  enumeration completeness, exclusion attestations, and freeze-commit
  ordering require independent audit; 30 PRs across three repositories remain
  a small sample; shared editable environments require an explicit worktree
  `PYTHONPATH` or reinstall to avoid validating another worktree.
- Ownership deviations: none.
