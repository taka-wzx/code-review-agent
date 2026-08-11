# Phase 11D Pilot v4 N02: Canonical JSON Finite Numbers

## Scope

Add one regression test proving canonical receipts reject non-finite JSON numbers.

## Ownership

- `docs/plans/phase11d-pilot-v4-n02-canonical-nan.md`
- `tests/test_phase11d_pilot_v4_n02.py`

All other files are read-only. Do not read or run `eval/**` or `eval/holdout/**`.

## Validation

- `python -m unittest -v tests.test_phase11d_pilot_v4_n02`
- `git diff --check`
