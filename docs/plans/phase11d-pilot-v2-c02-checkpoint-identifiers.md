# Phase 11D Pilot v2 Candidate 02: Checkpoint Identifiers

## Scope

Add regression coverage for portable checkpoint run identifiers and checksum comparison.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c02-checkpoint-identifiers.md`
- `tests/test_phase11d_pilot_v2_c02_checkpoint_identifiers.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Portable lowercase run IDs are accepted.
- Windows device aliases and trailing-dot IDs are rejected.
- Constant-time checksum comparison behavior is covered.
