# Phase 11D Pilot v4 N04: UTC Timestamp Boundary

## Scope

Add one regression test proving receipt timestamps reject numeric UTC offsets.

## Ownership

- `docs/plans/phase11d-pilot-v4-n04-utc-offset.md`
- `tests/test_phase11d_pilot_v4_n04.py`

All other files are read-only. Do not read or run `eval/**` or `eval/holdout/**`.

## Validation

- `python -m unittest -v tests.test_phase11d_pilot_v4_n04`
- `git diff --check`
