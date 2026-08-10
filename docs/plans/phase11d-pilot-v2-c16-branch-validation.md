# Phase 11D Pilot v2 Candidate 16: Branch Validation

## Scope

Cover normalized Git branch validation used by Draft Repair publication requests.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c16-branch-validation.md`
- `tests/test_phase11d_pilot_v2_c16_branch_validation.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- A namespaced repair branch is accepted.
- Dot traversal, duplicate separators, hidden segments, and lock suffixes are rejected.
- Focused unittest and lint checks pass.
