# Phase 11D Pilot v4 N08: Empty Patch Content

## Scope

Add one regression test proving sandbox patch files cannot contain empty bytes.

## Ownership

- `docs/plans/phase11d-pilot-v4-n08-empty-patch-content.md`
- `tests/test_phase11d_pilot_v4_n08.py`

All other files are read-only. Do not read or run `eval/**` or `eval/holdout/**`.

## Validation

- `python -m unittest -v tests.test_phase11d_pilot_v4_n08`
- `git diff --check`
