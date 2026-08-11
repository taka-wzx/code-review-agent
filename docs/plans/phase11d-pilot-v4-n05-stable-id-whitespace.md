# Phase 11D Pilot v4 N05: Stable ID Whitespace

## Scope

Add one regression test proving stable receipt identifiers reject whitespace.

## Ownership

- `docs/plans/phase11d-pilot-v4-n05-stable-id-whitespace.md`
- `tests/test_phase11d_pilot_v4_n05.py`

All other files are read-only. Do not read or run `eval/**` or `eval/holdout/**`.

## Validation

- `python -m unittest -v tests.test_phase11d_pilot_v4_n05`
- `git diff --check`
