# Phase 11D Pilot v2 Candidate 11: Webhook Identities

## Scope

Cover strict GitHub webhook event, delivery, and positive-ID parsing boundaries.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c11-webhook-identities.md`
- `tests/test_phase11d_pilot_v2_c11_webhook_identities.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Canonical event and delivery values are accepted.
- Whitespace, overlong values, and bool IDs fail closed.
- Stable InvalidRequest failures are preserved.
