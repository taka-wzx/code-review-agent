# Phase 11D Pilot v2 Candidate 06: Sandbox Argv

## Scope

Add fail-closed regression coverage for Repair sandbox command argument validation.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c06-sandbox-argv.md`
- `tests/test_phase11d_pilot_v2_c06_sandbox_argv.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Safe argv sequences normalize to immutable tuples.
- Shells, inline interpreter snippets, strings, and NUL arguments are rejected.
- No process or container is started.
