# Phase 11D Pilot v3 Candidate 19

## Scope

Add one offline regression for operator patch Base64 decoding. This is an actual test-maintenance task and does not authorize Provider calls or GitHub publication behavior.

## Owned Files

- `docs/plans/phase11d-pilot-v3-c19-patch-base64-decoding.md`
- `tests/test_phase11d_pilot_v3_c19.py`

All other files are read-only. Do not read or run `eval/**` or `eval/holdout/**`.

## Acceptance

- The focused unittest passes offline.
- No production interface, dependency, workflow, credential, or external-write behavior changes.
- Deliver as a non-Draft PR to `master`; do not merge it during denominator preparation.

## Validation

- `python -m unittest -v tests.test_phase11d_pilot_v3_c19`
- `python -m ruff check tests/test_phase11d_pilot_v3_c19.py`
- `git diff --check`

