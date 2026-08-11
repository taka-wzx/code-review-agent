# Phase 11D Pilot v4 N03: Integer Boolean Rejection

## Scope

Add one regression test proving integer receipt fields reject booleans.

## Ownership

- `docs/plans/phase11d-pilot-v4-n03-integer-bool.md`
- `tests/test_phase11d_pilot_v4_n03.py`

All other files are read-only. Do not read or run `eval/**` or `eval/holdout/**`.

## Validation

- `python -m unittest -v tests.test_phase11d_pilot_v4_n03`
- `git diff --check`
