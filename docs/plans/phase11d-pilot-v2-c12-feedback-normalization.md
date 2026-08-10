# Phase 11D Pilot v2 Candidate 12: Feedback Normalization

## Scope

Add deterministic normalization and duplicate-ID regression coverage for feedback rules.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c12-feedback-normalization.md`
- `tests/test_phase11d_pilot_v2_c12_feedback_normalization.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Category/action values casefold and surrounding whitespace is removed.
- Canonical JSON hash matches the normalized payload.
- Rule IDs remain unique case-insensitively.
