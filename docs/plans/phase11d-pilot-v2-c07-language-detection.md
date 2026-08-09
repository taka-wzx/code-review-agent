# Phase 11D Pilot v2 Candidate 07: Language Detection

## Scope

Cover case-insensitive, deduplicated language detection for changed-file context.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c07-language-detection.md`
- `tests/test_phase11d_pilot_v2_c07_language_detection.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Supported suffixes are case-insensitive and deduplicated.
- Unsupported files do not add language entries.
- Empty input returns an empty tuple.
