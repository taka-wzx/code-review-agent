# Phase 11D Live Review-to-Repair v1

## Scope

Add a local, loopback-only operator session that keeps structured Review findings in
memory until a confirmed human selects one. Bind that selection to one exact Repair
Plan, one WRITE approval, one isolated sandbox result, one exact-commit DRAFT_PR
approval, and at most one real Draft Repair PR. Produce only sanitized hash-bound
receipts and reports on disk.

## Owned Files

- `docs/plans/phase11d-live-review-repair-v1.md`
- `docs/phase11d-human-pilot-v1.md`
- `phase11d_gate_b_executor.py`
- `tests/test_phase11d_human_pilot.py`

All other files are read-only. Dependencies, workflows, public service interfaces,
`eval/**`, and `eval/holdout/**` must not be read or changed.

## Frozen Interfaces

- Existing authorization, selection, Review, Resume, Repair, and publisher receipts
  remain compatible.
- Raw diffs, prompts, Provider responses, Finding text, Repair Plan text, patch bytes,
  credentials, and approval secrets are never written to a receipt or journal.
- The operator binds only to `127.0.0.1`, requires a bearer token, and keeps all raw
  Review findings in the serving process. It clears unselected findings after the
  human selection and terminates after publication, decline, stop, or timeout.
- Review still stops on the first terminal failure. No selected PR is replaced and no
  completed Provider call is replayed.
- Repair requires a confirmed maintainer/org-admin selection, exact plan hash, a
  single-use WRITE approval, passing isolated sandbox evidence, and a second
  single-use DRAFT_PR approval for the exact commit.
- Publisher remains limited to one `crag/phase11d/` branch and one Draft PR. It has no
  Ready, merge, comment, check, label, review, or protected/default-branch route.

## Acceptance Criteria

1. Structured Review parsing can return an ephemeral human-readable Finding view and
   the existing hash-only `ReviewOutcome` from the same Provider response.
2. A loopback operator session exposes only the current state and required human
   action, rejects unauthenticated/non-loopback requests, and never serializes raw
   findings to disk.
3. Human selection and exact Repair Plan create the existing hash-bound Repair intent;
   WRITE and DRAFT_PR approvals are one-use and actor/expiry checked.
4. Sandbox evidence is accepted only after WRITE approval and must bind patch, tests,
   checkpoint, budget, branch, tree, and exact commit.
5. Publication is impossible before DRAFT_PR approval and remains one Draft PR with
   read-back reconciliation and sanitized receipts.
6. Final feedback, cost/time, claim-decision, acceptance, and canonical manifest
   outputs preserve `model_quality_status=not_measured` and
   `formal_quality_status=incomplete` unless a separately authorized formal study
   changes them.

## Implementation Status

- The Provider parser now emits the existing hash-only outcome and an optional
  in-process Finding view from the same validated response.
- `run-review-repair-session` runs a fresh cohort and exposes the one-job coordinator
  through an authenticated loopback-only HTTP session.
- The session clears unselected Findings after selection, accepts only hash-bound
  sandbox evidence, consumes both approvals once, and terminates after publication,
  decline, stop, or timeout.
- `prepare-pilot-closeout` and `approve-pilot-closeout` generate the feedback,
  time/cost, business, claim, final-acceptance, and canonical-manifest chain with an
  exact org-admin sign-off between the two commands.
- Real execution remains blocked until this implementation is merged and a new
  runtime, denominator, write-enabled authorization, and exact approval are frozen.

## Validation

- `python -m unittest -v tests.test_phase11d_human_pilot`
- `python -m unittest discover -s tests`
- `python -m ruff check .`
- `python -m mypy phase11d_gate_b_executor.py`
- `python scripts/verify.py`
- `python -m pip check`
- `git diff --check`

## Delivery

Commit and push only `codex/phase11d-live-review-repair-v1`, create a Draft
implementation PR, wait for all required CI checks, mark it Ready, and merge with a
merge commit under the owner's explicit authorization. Real Pilot execution remains
closed until a new runtime, new denominator, full write-enabled authorization, and
exact owner approval are frozen after merge.
