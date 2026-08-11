# Phase 11D Pilot v4 N06: Branch Double Slash

## Scope

Add one regression test proving repair branch validation rejects empty path segments.

## Ownership

- `docs/plans/phase11d-pilot-v4-n06-branch-double-slash.md`
- `tests/test_phase11d_pilot_v4_n06.py`

All other files are read-only. Do not read or run `eval/**` or `eval/holdout/**`.

## Validation

- `python -m unittest -v tests.test_phase11d_pilot_v4_n06`
- `git diff --check`
