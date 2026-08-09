# Phase 11D Pilot v2 Candidate 04: Bearer Parsing

## Scope

Add explicit authentication-header parsing regressions for the shared bearer boundary.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c04-bearer-parsing.md`
- `tests/test_phase11d_pilot_v2_c04_bearer_parsing.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Bearer scheme matching remains case-insensitive.
- Missing tokens and non-Bearer schemes fail closed with the stable error.
- No raw token is persisted by the test.
