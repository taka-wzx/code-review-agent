# Phase 11D Gate B Review Execution v1

## Scope

Expose the already implemented, default-closed GitHub reader, GLM structured Review
client, and Review budget as one hash-bound cohort command. The command must preserve
the selected 20-PR denominator, stop on the first fail-closed terminal outcome, and
write only a sanitized self-hashed receipt.

## Owned Files

- `docs/plans/phase11d-gate-b-review-execution-v1.md`
- `docs/phase11d-human-pilot-v1.md`
- `phase11d_gate_b_executor.py`
- `tests/test_phase11d_human_pilot.py`

All other files are read-only. In particular, dependencies, workflows, production
service interfaces, `eval/**`, and `eval/holdout/**` must not be read or changed.

## Frozen Interfaces

- Existing Gate B commands and receipt schemas remain compatible.
- Real transport remains closed unless exact authorization, live runtime hashes,
  credential fingerprints, repository identity, selection receipt, and UTC window pass.
- No comments, checks, labels, reviews, branch pushes, Draft PRs, Ready transitions,
  merges, or protected/default-branch mutations are added by this command.
- Provider retries remain zero and selected PRs are never replaced.

## Acceptance Criteria

1. `review-selected-pull-requests` validates the complete Gate B authorization before
   credential access or transport creation.
2. Each selected PR is read back and matched to its frozen ID/base/head snapshot before
   its diff can reach the provider.
3. The output contains exactly 20 hash-only rows; after a terminal failure all remaining
   rows are retained as `cohort_stopped` with zero calls.
4. Budget usage, selection receipt hash, stop category, and receipt SHA-256 are validated.
5. Offline fakes cover success, provider text-only stop, PR drift, tampering, and parser
   wiring without real credentials or network calls.

## Validation

- `python -m unittest -v tests.test_phase11d_human_pilot`
- `python -m unittest discover -s tests`
- `python -m ruff check .`
- `python -m mypy phase11d_gate_b_executor.py`
- `python scripts/verify.py`
- `git diff --check`

## Delivery

Commit and push only `codex/phase11d-gate-b-review-execution-v1`, create a Draft
implementation PR, wait for CI, then merge only under the owner's existing explicit
instruction to complete Phase 11D. Real Gate B remains closed until a newly frozen exact
approval text is separately approved after this implementation is merged.
