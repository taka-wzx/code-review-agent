# Phase 11D Pilot v2 Candidate 05: Installation IDs

## Scope

Strengthen regression coverage for GitHub installation numeric identity validation.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c05-installation-ids.md`
- `tests/test_phase11d_pilot_v2_c05_installation_ids.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Positive integer IDs are accepted.
- Boolean, zero, negative, and textual IDs are rejected.
- Focused unittest and lint checks pass.
