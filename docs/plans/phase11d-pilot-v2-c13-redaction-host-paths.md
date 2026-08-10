# Phase 11D Pilot v2 Candidate 13: Redaction Host Paths

## Scope

Add nested forbidden-content regressions for credentials and absolute host paths.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c13-redaction-host-paths.md`
- `tests/test_phase11d_pilot_v2_c13_redaction_host_paths.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Nested credential-like keys and absolute host paths are detected.
- Ordinary bounded identifiers remain allowed.
- Tests contain no real secret or user path.
