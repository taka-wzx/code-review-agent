# Phase 11D Pilot v2 Candidate 15: Budget Types

## Scope

Add regression coverage for strict numeric types in Repair budget limits.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c15-budget-types.md`
- `tests/test_phase11d_pilot_v2_c15_budget_types.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Default limits remain valid.
- Booleans cannot masquerade as integer counters.
- Non-finite and non-positive limits are rejected.
