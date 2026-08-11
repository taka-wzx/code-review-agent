# Phase 11D Pilot v4 N10: Cost Ceiling

## Scope

Add one regression test proving the frozen single-token input tariff in micro-CNY.

## Ownership

- `docs/plans/phase11d-pilot-v4-n10-cost-ceiling.md`
- `tests/test_phase11d_pilot_v4_n10.py`

All other files are read-only. Do not read or run `eval/**` or `eval/holdout/**`.

## Validation

- `python -m unittest -v tests.test_phase11d_pilot_v4_n10`
- `git diff --check`
