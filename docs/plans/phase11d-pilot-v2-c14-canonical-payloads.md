# Phase 11D Pilot v2 Candidate 14: Canonical Payloads

## Scope

Cover deterministic publication payload serialization and non-finite number rejection.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c14-canonical-payloads.md`
- `tests/test_phase11d_pilot_v2_c14_canonical_payloads.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Key ordering and separators are canonical and ASCII encoded.
- NaN is rejected instead of entering a hash-bound payload.
- Focused unittest and lint checks pass.
