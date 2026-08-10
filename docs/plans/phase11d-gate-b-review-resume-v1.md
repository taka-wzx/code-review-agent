# Phase 11D Gate B Review Resume v1

## Scope

Add a hash-chained, Review-only recovery path for an immutable Gate B cohort that
stopped on a GitHub `http_transport_failure` before the affected PR reached the
Provider. Preserve completed rows and cumulative budget usage without replaying a
Provider call.

## Owned Files

- `docs/plans/phase11d-gate-b-review-resume-v1.md`
- `docs/phase11d-human-pilot-v1.md`
- `phase11d_gate_b_executor.py`
- `tests/test_phase11d_human_pilot.py`

All other files are read-only. Dependencies, workflows, public service interfaces,
`eval/**`, and `eval/holdout/**` must not be read or changed.

## Frozen Interfaces

- Existing Gate B commands and v1alpha1 Review receipts remain valid.
- Review-only authorization may enable Provider calls while both Repair write
  switches remain false. Repair and publication still require both switches true.
- Resume accepts only a validated prior receipt whose first failure is
  `http_transport_failure` with zero Provider calls, HTTP attempts, and tokens.
- Selected PR rows must be byte-for-byte equivalent across the prior and current
  selection receipts. Completed rows are carried forward and never replayed.
- No comment, check, label, review, branch, Draft PR, Ready, merge, or default-branch
  write is added to Review or Resume.

## Acceptance Criteria

1. Exact approval text reflects the actual Review-only or full-Pilot permission
   switches instead of hard-coding Repair writes.
2. `resume-selected-pull-requests` validates the current gate plus the prior
   authorization, selection receipt, and Review receipt before a resumed call.
3. The v1alpha2 receipt binds the immediate prior receipt SHA-256, carries completed
   rows unchanged, restores cumulative budget usage, and preserves all denominator
   rows.
4. Provider failures, snapshot drift, nonzero failed-row usage, changed selection,
   and a completed cohort cannot be resumed.
5. Repair coordination and publication reject Review-only authorization.

## Validation

- `python -m unittest -v tests.test_phase11d_human_pilot`
- `python -m unittest discover -s tests`
- `python -m ruff check .`
- `python -m mypy phase11d_gate_b_executor.py`
- `python scripts/verify.py`
- `git diff --check`

## Delivery

Commit and push only `codex/phase11d-review-resume-v1`, create a Draft PR, wait for
all required CI checks, mark it Ready, and merge with a merge commit under the
owner's explicit authorization. Real Resume remains closed until a newly frozen
Review-only authorization is exactly approved after the implementation is merged.
