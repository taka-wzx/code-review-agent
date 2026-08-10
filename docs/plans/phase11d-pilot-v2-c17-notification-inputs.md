# Phase 11D Pilot v2 Candidate 17: Notification Inputs

## Scope

Add regression coverage for bounded notification identifiers, reason codes, and counters.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c17-notification-inputs.md`
- `tests/test_phase11d_pilot_v2_c17_notification_inputs.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Canonical identifiers and reason codes are accepted.
- Whitespace, punctuation, and bool counters fail closed.
- Stable InvalidNotificationInput failures are preserved.
