# Phase 11D Pilot v2 Candidate 09: Retry Durations

## Scope

Cover provider retry-duration parsing across supported units and malformed inputs.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c09-retry-durations.md`
- `tests/test_phase11d_pilot_v2_c09_retry_durations.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Millisecond, second, minute, and hour values convert correctly.
- Whitespace and unit case normalize consistently.
- Signed, non-numeric, and composite values are rejected.
