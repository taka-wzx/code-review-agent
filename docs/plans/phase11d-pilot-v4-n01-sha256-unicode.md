# Phase 11D Pilot v4 N01: SHA-256 Unicode Boundary

## Scope

Add one regression test proving that hash-only receipts encode text as UTF-8.

## Ownership

- `docs/plans/phase11d-pilot-v4-n01-sha256-unicode.md`
- `tests/test_phase11d_pilot_v4_n01.py`

All other files are read-only. Do not read or run `eval/**` or `eval/holdout/**`.

## Validation

- `python -m unittest -v tests.test_phase11d_pilot_v4_n01`
- `git diff --check`
