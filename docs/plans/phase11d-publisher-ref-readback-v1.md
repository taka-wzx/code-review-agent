# Phase 11D Publisher Ref Read-back v1

## Scope

Correct Draft Repair branch read-back so GitHub receives the slash-delimited ref path
it accepts after branch creation. No Provider call, Repair publication, Ready transition,
or merge is part of this implementation.

## Owned Files

- `docs/plans/phase11d-publisher-ref-readback-v1.md`
- `docs/phase11d-human-pilot-v1.md`
- `phase11d_gate_b_executor.py`
- `tests/test_phase11d_human_pilot.py`

All other paths are read-only. Dependencies, workflows, public package interfaces,
`eval/**`, and `eval/holdout/**` must not be read, run, or changed.

## Frozen Boundaries

- Branch names remain validated by the existing Phase 11D branch boundary.
- The GitHub ref endpoint preserves slash separators while URL-encoding unsafe branch
  characters.
- Branch creation, exact commit verification, Draft-only publication, and journal
  quarantine behavior remain unchanged.
- The quarantined 012 publication journal is never retried or upgraded.

## Acceptance Criteria

1. Ref read-back sends `heads/crag/phase11d/...`, not an encoded `%2F` path.
2. The transport allowlist still accepts only the existing ref endpoint family.
3. The publisher regression fake rejects encoded slash paths and the publication tests
   still pass.
4. Existing timeout recovery, approval, sandbox, Draft-only, and no-merge tests pass.

## Validation

- `python -m unittest -v tests.test_phase11d_human_pilot`
- `python -m unittest discover -s tests -p "test_phase11*.py"`
- `python -m ruff check phase11d_gate_b_executor.py tests/test_phase11d_human_pilot.py`
- `python -m mypy phase11d_gate_b_executor.py`
- `python -m pip check`
- `git diff --check`

Do not run evaluation or holdout commands.
