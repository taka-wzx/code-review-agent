# Phase 11D Pilot v2 Candidate 19: Envelope Binary

## Scope

Add strict base64 and object-identifier regressions for encrypted artifact envelopes.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c19-envelope-binary.md`
- `tests/test_phase11d_pilot_v2_c19_envelope_binary.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Canonical base64 decodes exactly.
- Empty, malformed, and non-ASCII encodings are rejected.
- Traversal-like object identifiers fail closed.
