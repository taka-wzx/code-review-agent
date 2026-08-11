# Phase 11D Pilot v4 N07: Absolute Repository Path

## Scope

Add one regression test proving sandbox patch paths must remain repository-relative.

## Ownership

- `docs/plans/phase11d-pilot-v4-n07-absolute-path.md`
- `tests/test_phase11d_pilot_v4_n07.py`

All other files are read-only. Do not read or run `eval/**` or `eval/holdout/**`.

## Validation

- `python -m unittest -v tests.test_phase11d_pilot_v4_n07`
- `git diff --check`
