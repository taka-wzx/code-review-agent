# Phase 11D Pilot v2 Candidate 20: Packed Ref Resolution

## Scope

Cover source-revision resolution for Git worktree pointers backed only by packed refs.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c20-packed-ref-resolution.md`
- `tests/test_phase11d_pilot_v2_c20_packed_ref_resolution.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- A valid worktree gitdir pointer resolves a packed branch SHA.
- Uppercase SHA text normalizes to lowercase.
- Missing refs return `None` without shell execution.
