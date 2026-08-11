# Phase 11D Pilot v4 N11: Empty Ephemeral Text

## Scope

Add one regression test proving empty in-memory Repair text cannot be hash-bound.

## Ownership

- `docs/plans/phase11d-pilot-v4-n11-empty-ephemeral-text.md`
- `tests/test_phase11d_pilot_v4_n11.py`

All other files are read-only. Do not read or run `eval/**` or `eval/holdout/**`.

## Validation

- `python -m unittest -v tests.test_phase11d_pilot_v4_n11`
- `git diff --check`
