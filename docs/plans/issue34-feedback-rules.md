# Issue #34 Feedback Rules Contract

## Scope

Add persistent, versioned repository feedback-rule configurations with atomic
activation, rollback, immutable receipts, and review-evaluation binding.

This branch is stacked on the exact PR #51 commit
`5c9d17ac3307d2764b7b4c8da8c10d059b3e50b4` so migration `0011` has a stable
`0010_issue33_github_webhook` parent. PR #51 must merge before this branch is
landed on `master`.

## Owned Files

- `docs/plans/issue34-feedback-rules.md`
- `docs/feedback-rules.md`
- `migrations/versions/0011_issue34_feedback_rules.py`
- `src/code_review_agent/feedback_rules.py`
- `src/code_review_agent/service_queue.py`
- `src/code_review_agent/service_core.py`
- `src/code_review_agent/service.py`
- `src/code_review_agent/worker.py`
- `src/code_review_agent/agent.py`
- `tests/test_issue34_feedback_rules.py`

No other files may be modified.

## Frozen Interfaces

- Existing review, repository, policy, approval, and webhook API schemas remain
  backward compatible.
- Existing role permissions remain authoritative: repository readers may view
  rules; only principals with `manage_policy` may create, activate, or roll back
  versions.
- Existing review behavior is unchanged when no feedback-rule version is active.
- Migration `0011_issue34_feedback_rules` follows PR #51 migration `0010`.

## Rule Contract

Each immutable version contains 1-64 uniquely identified rules. Every rule has
`rule_id`, `category`, `action`, `condition`, and `rationale`. Action is one of
`prioritize`, `suppress`, or `require_verification`. The canonical rules JSON is
bounded and SHA-256 addressed.

## Acceptance Criteria

1. Version creation is immutable and idempotent only for identical canonical
   content.
2. Activation atomically advances a monotonic repository generation and writes
   an immutable receipt in the same transaction.
3. Rollback only targets a previously active version and writes a rollback
   receipt containing the prior and restored identities.
4. Review submission binds the active version, generation, rules hash, and
   canonical rules in the job-creation transaction. Later activation cannot
   change the in-flight job's binding.
5. The bound identity is present in API status, worker requests, traces, and the
   actual review input. No active version preserves prior behavior.
6. API and migration tests cover validation, RBAC, tenant isolation, immutable
   versions, activation, rollback, receipts, deduplication boundaries, and
   in-flight binding.

## Validation

- `tests.test_issue34_feedback_rules`
- `tests.test_runtime tests.test_phase9c_durable_service`
- `ruff check .`
- `mypy src/code_review_agent`
- `scripts/verify.py`
- `git diff --check`

## Delivery

Status: complete

## Result

- Added immutable, SHA-256-addressed repository feedback-rule versions with
  atomic activation, rollback, monotonic generations, and transition receipts.
- Bound the active rule snapshot to review jobs, status responses, worker
  requests, traces, and the actual finder/verifier input without changing the
  no-active-rule behavior.
- Added scoped RBAC API operations, migration `0011`, operator documentation,
  and regression coverage for validation, isolation, deduplication, and
  in-flight immutability.
- `scripts/verify.py` passed with 1122 tests, 18 skips, 85% total coverage,
  clean Ruff and mypy results, and working module/console entry points.
- `eval/` and `eval/holdout/` were not read or executed.
