# Phase 11D Pilot v4 N09: Patch File Mode

## Scope

Add one regression test proving sandbox patches reject unsupported Git file modes.

## Ownership

- `docs/plans/phase11d-pilot-v4-n09-patch-mode.md`
- `tests/test_phase11d_pilot_v4_n09.py`

All other files are read-only. Do not read or run `eval/**` or `eval/holdout/**`.

## Validation

- `python -m unittest -v tests.test_phase11d_pilot_v4_n09`
- `git diff --check`
