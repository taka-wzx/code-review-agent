# Phase 9G-Solo Exploratory v1 operator runbook

## What this phase can establish

Phase 9G-Solo is a deliberately separate evidence track for one real developer
when the original Phase 9G Business Pilot and Formal Quality staffing cannot be
met. A valid result may be described only as a **single-participant exploratory
observation**. **Model quality not measured** is mandatory report language.

The following values never change:

```text
evidence_type=single_participant_exploratory
business_claim_allowed=false
quality_claim_allowed=false
formal_quality_status=incomplete
```

One person cannot act under several identities. DeepSeek, GLM, Claude, or any
other model cannot be counted as another person, annotator, or adjudicator.
Feedback from the Solo participant is workflow feedback, not double-annotated
gold and not a substitute for Precision/Recall/F1.

This repository state is preparation only. It does not itself authorize a real
model call, real diff read, paid use, GitHub API access, publication, deployment,
commit, push, PR, or merge.

## Mandatory authorization table

Copy the table below into a controlled approval record. Do not place names,
repository locators, diffs, tokens, prompts, credentials, or local paths in the
repository. Use stable pseudonymous IDs and opaque repository IDs.

```text
Phase 9G-Solo Exploratory v1

- authorization ID:
- Solo participant stable ID:
- participant confirmed to be one real human: yes/no
- opaque repository IDs:
- PR count (5-10):
- frozen selection rule: lowest deterministic eligible ranks before output inspection
- mode: shadow

Model
- provider:
- exact model/snapshot:
- frozen runtime-configuration SHA-256:
- temperature:
- maximum logical calls:
- maximum HTTP attempts:
- maximum input tokens:
- maximum output tokens:
- maximum cost micro-CNY:
- real paid calls allowed: yes/no
- specified raw diffs may be read: yes/no

Retention
- data retention days:
- feedback retention days:
- sanitized raw-trace retention days:

Structurally mandatory external denials
- staging deployment allowed: no
- deployment target: null
- real GitHub API allowed: no
- comments/Checks allowed: no
- GitHub publication allowed: no

Approval proof
- stable human approver ID:
- approved-at UTC:
- expires-at UTC:
- canonical authorization SHA-256:
```

Every field must be explicit. Presence is not permission. `real_paid_calls=false`
or `read_raw_diff=false` keeps model execution closed even if every other field
is complete. Solo v1 always rejects deployment, GitHub API, comments, Checks,
and publication.

## Before any real materialization

1. Merge the frozen Solo contract through a human-owned PR. Record the exact
   merge commit containing `docs/plans/phase9g-solo-exploratory-v1.md`.
2. Complete the external approval record and the deliberately incomplete
   `phase9g_solo/authorization.template.json` copy.
3. Obtain one real participant's versioned consent, withdrawal acknowledgement,
   repository bindings, and retention agreement.
4. Obtain repository-owner authority for each opaque repository. Raw-diff
   authority must agree with the top-level authorization; GitHub API and
   publication authority must remain false.
5. Validate the sealed authorization at the intended run time:

   ```powershell
   python phase9g_solo.py validate-authorization `
     --authorization <authorization.json> `
     --at <YYYY-MM-DDTHH:MM:SSZ>
   ```

If any field is null, malformed, inconsistent, stale, expired, synthetic, or
over budget, the corresponding real/model gate stays closed. The offline tool
does not perform the subsequently authorized model execution; an executor must
independently consume only a validated authorization and enforce the same
cumulative ceilings.

## Deterministic cohort protocol

The target is exactly 5--10 PRs. Freeze the time window, repository IDs,
exclusion vocabulary, and target before inspecting any model output or human
feedback. The seed is:

```text
SHA256(b"phase9g-solo-selection-v1\0" + ASCII(solo_contract_merge_commit))
```

For each complete in-window candidate ledger row, compute:

```text
rank = SHA256(seed + "\n" + opaque_pr_id)
```

Each ineligible candidate needs one preregistered exclusion. The selected set
is the lowest 5--10 eligible ranks. Selected records bind snapshot and diff
hashes; excluded records may leave them null. Validation of a real plan requires
the expected merge commit from an external trusted source so an operator cannot
shop for a favorable seed.

Materialize only after sealing every selection row:

```powershell
python phase9g_solo.py materialize-cohort `
  --authorization <authorization.json> `
  --plan <selection-plan.json> `
  --selection-log <selection-log.jsonl> `
  --repositories <repository-manifest.json> `
  --expected-source-commit <40-character-merge-commit> `
  --materialized-at <YYYY-MM-DDTHH:MM:SSZ> `
  --output <cohort.json>
```

Seal a completed JSON or JSONL template with the generic offline hash helper;
hashing alone never grants a permission or validates semantic authority:

```powershell
python phase9g_solo.py hash-artifact `
  --input <artifact.json-or-jsonl> `
  --hash-field <declared-self-hash-field> `
  --output <sealed-artifact> `
  [--jsonl]
```

## Run and receipt discipline

- Attempt 1 is always the sole headline for a selected PR.
- Later attempts are diagnostic. They never replace the headline, even when a
  diagnostic succeeds after a headline failure.
- Count logical calls, HTTP attempts, input/output tokens, and integer
  micro-CNY cumulatively across completed, degraded, fail-open, failed,
  cancelled, timed-out, and diagnostic attempts.
- HTTP attempts must be at least logical calls. Every cumulative total must be
  no greater than its authorization ceiling.
- A pre-model failure is a zero-usage failure receipt, not an omitted PR.
- Every selected PR needs one headline receipt and one consolidated human time
  record. Missing rows are validation failures.
- Store only a sanitized trace hash and deletion deadline. Never place a raw
  diff, prompt, token, credential, identity, repository locator, or local path
  in the artifact bundle.
- Stop before the next call if any ceiling would be exceeded. Do not rerun to
  hide or overwrite failure evidence.

The operator should use `run-receipt.template.jsonl` for both headline and
diagnostic attempts. After strict import, derive the run manifest from the
complete receipt list and retain the original append-only receipts.

## Feedback and time import

The participant receives a hash-bound list of feedback-eligible Finding IDs.
The participant—not a model—selects `accepted`, `rejected`, `uncertain`,
`fixed`, or `duplicate`. Rejected, uncertain, and duplicate responses require
a human rationale; `fixed` requires `fixed_at`.

Partial feedback is valid and must not be imputed. Every missing response stays
in the full feedback-eligible Finding denominator. A decision is a within-person
workflow observation; it is not gold, not an independent label, and not a
model-quality judgment.

Record active and paused time once per selected PR. Do not calculate or claim
time saved, productivity improvement, adoption, team impact, or generalization.

## Report and acceptance

The report validator recomputes exact metrics from the sealed evidence:

- selected PR and immutable headline status counts;
- completed-headline count and denominator;
- feedback-eligible, responded, missing, and raw decision counts;
- active/paused time and headline-latency distributions;
- all-attempt calls, HTTP attempts, tokens, micro-CNY, diagnostic attempts,
  headline-failure reruns, and stable error categories.

`exploratory_summary_allowed=true` requires one confirmed real participant,
5--10 non-synthetic authorized PRs, unexpired consent/repository/authorization,
complete time and headline coverage, and both real-paid-call and raw-diff scopes.
It still leaves all business and quality claims false.

Validate the final directory or JSON bundle offline:

```powershell
python phase9g_solo.py validate-bundle `
  --bundle <bundle-directory-or-json> `
  --expected-source-commit <40-character-merge-commit>
```

The committed synthetic gate uses no external source commit and must keep every
real scope closed:

```powershell
python phase9g_solo.py validate-bundle --bundle phase9g_solo/examples/synthetic
```

Do not use `--eval-assets`; do not read or enumerate `eval/` or
`eval/holdout/`. Before delivery, run the Solo tests, Ruff, mypy,
`scripts/verify.py`, `pip check`, and `git diff --check` exactly as frozen in the
task contract.

## Permitted interview description

An accurate résumé or interview statement is:

> I separated a one-developer exploratory workflow study from a formal
> multi-person quality evaluation, built deterministic selection and immutable
> failure/budget evidence, and kept business and model-quality claims closed.

Do not say the Solo exercise proved product adoption, business success, review
accuracy, precision/recall/F1, time savings, or performance across developers.
