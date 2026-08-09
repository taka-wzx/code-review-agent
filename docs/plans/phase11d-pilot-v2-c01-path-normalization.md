# Phase 11D Pilot v2 Candidate 01: Path Normalization

## Scope

Add focused regression coverage for cross-platform writable-path normalization and
escape/alias rejection in Repair approval bindings.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c01-path-normalization.md`
- `tests/test_phase11d_pilot_v2_c01_path_normalization.py`

All production code, dependencies, workflows, and `eval/**` remain read-only.

## Acceptance

- Windows separators normalize to sorted POSIX repo-relative paths.
- Traversal and case-insensitive aliases remain rejected.
- Focused unittest, Ruff, and `git diff --check` pass.
