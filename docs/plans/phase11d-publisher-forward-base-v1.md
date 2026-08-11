# Phase 11D Publisher Forward Base v1

## Scope

Correct the real Draft Repair publisher so the exact Repair commit is parented by
the frozen selected PR head, while allowing the target base branch to advance only
when GitHub proves the frozen base is an ancestor of the current base. No Provider
call, Repair publication, Ready transition, or merge is part of this implementation.

## Owned Files

- `docs/plans/phase11d-publisher-forward-base-v1.md`
- `docs/phase11d-human-pilot-v1.md`
- `phase11d_gate_b_executor.py`
- `tests/test_phase11d_human_pilot.py`

All other paths are read-only. Dependencies, workflows, public package interfaces,
`eval/**`, and `eval/holdout/**` must not be read, run, or changed.

## Frozen Boundaries

- The selected PR number, GitHub ID, base branch, frozen base SHA, and frozen head
  SHA remain bound to the original selection and Review receipts.
- Git object creation uses the frozen selected PR head as the sole commit parent.
- An unchanged target base is accepted without an extra request. An advanced target
  base requires a GitHub compare result proving the frozen base is the merge base and
  ancestor. Diverged, behind, missing, malformed, or ambiguous results fail closed.
- Publication remains limited to one dedicated branch and one Draft PR. No Ready,
  merge, comment, check, label, review, or protected-branch mutation route is added.
- Existing receipts remain hash-only; raw patch bytes and credentials remain memory-only.

## Acceptance Criteria

1. Draft publication and journal bindings include the frozen source head SHA.
2. The Git commit request uses the source head SHA as its only parent and still checks
   the exact commit SHA returned by GitHub.
3. Forward-only base movement is accepted only after a strict compare response;
   divergence, reversal, malformed responses, and source-head drift fail before writes.
4. Tests cover unchanged base, valid forward movement, denied divergence, and the exact
   commit-parent payload without opening any real transport.
5. Existing timeout recovery, approval, sandbox, Draft-only, and no-merge tests pass.

## Validation

- `python -m unittest -v tests.test_phase11d_human_pilot`
- `python -m unittest discover -s tests -p "test_phase11*.py"`
- `python -m ruff check phase11d_gate_b_executor.py tests/test_phase11d_human_pilot.py`
- `python -m mypy phase11d_gate_b_executor.py`
- `python -m pip check`
- `git diff --check`

Do not run evaluation or holdout commands.
