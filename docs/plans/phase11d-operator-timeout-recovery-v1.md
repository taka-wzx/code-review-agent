# Phase 11D Operator Timeout Recovery v1

## Scope

Add a fail-closed recovery path for a Review-to-Repair operator session that expired
after a human selected one Finding and approved WRITE, but before sandbox evidence was
accepted. Recovery reuses the completed Review receipt and timeout lineage and must
never call the Provider again.

## Owned Files

- `docs/plans/phase11d-operator-timeout-recovery-v1.md`
- `docs/phase11d-human-pilot-v1.md`
- `phase11d_gate_b_executor.py`
- `tests/test_phase11d_human_pilot.py`

All other paths are read-only. Dependencies, workflows, public package interfaces,
`eval/**`, and `eval/holdout/**` must not be read, run, or changed.

## Frozen Boundaries

- Existing authorization, selection, Review, Repair, Draft PR, and closeout receipts
  remain compatible.
- Recovery requires a new runtime and approved authorization whose canonical selection
  seed equals the recovery checkpoint SHA-256.
- The checkpoint binds the source authorization/runtime, completed Review receipt,
  terminal timeout receipt, selected Finding, prior selection/Plan/WRITE hashes, and
  the confirmed recovery actor and new recovery identifiers.
- Recovery has no Provider client or Review execution path. It rebuilds a coordinator
  from hash-only receipts and starts at a new single-use WRITE reapproval.
- Sandbox, DRAFT_PR approval, publisher limits, Draft-only state, and no-merge rules
  remain unchanged.
- Raw Finding text, Plan text, patch bytes, and credentials remain memory-only.

## Acceptance Criteria

1. An offline command creates a self-hashed recovery checkpoint only from a completed
   20-30 PR Review and an `expired/timeout` operator receipt.
2. Any mismatched source authorization, runtime, receipt SHA, selected Finding, prior
   binding, or checkpoint hash fails before credentials or transport access.
3. The recovery authorization binds the checkpoint SHA through its deterministic
   selection seed; a generic Gate B authorization cannot resume a timed-out session.
4. Recovery performs zero Provider calls and reconstructs a valid selection/review
   lineage under the new authorization without replacing or replaying PRs.
5. A maintainer/org-admin must approve a new WRITE binding, submit passing isolated
   sandbox evidence, and separately approve the exact DRAFT_PR binding before the
   existing publisher can create one Draft PR.
6. Tests cover valid recovery, Provider non-use, timeout/source/checkpoint drift,
   unauthorized actors, approval replay, and publication remaining closed.

## Validation

- `python -m unittest -v tests.test_phase11d_human_pilot`
- `python -m unittest discover -s tests`
- `python -m ruff check .`
- `python -m mypy phase11d_gate_b_executor.py`
- `python scripts/verify.py`
- `python -m pip check`
- `git diff --check`

Do not run evaluation or holdout commands.

## Delivery

Commit and push only `codex/phase11d-operator-timeout-recovery-v1`, create a Draft PR,
wait for required CI, mark it Ready, and merge with a merge commit under the owner's
explicit authorization. No Repair branch or Draft Repair PR may be published until a
post-merge runtime, recovery checkpoint authorization, new WRITE approval, sandbox
binding, and exact DRAFT_PR approval all pass.
