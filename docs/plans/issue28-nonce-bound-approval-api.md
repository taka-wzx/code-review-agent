# Issue #28: Nonce-bound approval service API

## Goal

Expose the existing nonce-bound maintainer publication approval binding through
the durable service API. The API must make the repository, pull request, head
SHA, finding-set hash, approver, and expiry visible without exposing raw
finding text or reusable nonces after consumption.

## Base

- Base branch: `master`
- Base commit: `345b3035eca2f7af65f650fdeaa7a1e5e7297194`
- Task branch: `codex/issue-28-nonce-bound-approval-api`
- GitHub Issue: https://github.com/taka-wzx/code-review-agent/issues/28

## Frozen Interfaces

- Existing publication approval endpoints remain:
  `/v1/reviews/pending-approval`, `/v1/reviews/{review_id}/approve`,
  `/v1/reviews/{review_id}/reject`, and `/v1/reviews/{review_id}/approvals`.
- Approval decisions continue to require `payload_sha256` and one-time `nonce`.
- Consumed nonces remain hidden from approval-history responses.
- No raw finding text, publisher payload, token, or GitHub response body is
  persisted or returned by the approval-history API.

## File Ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue28-nonce-bound-approval-api.md`, `src/code_review_agent/database.py`, `tests/test_issue28_approval_binding_api.py` | all other paths |

## Prohibited Changes

- No changes to `eval/**`, `eval/holdout/**`, dependencies, CI, migrations, or
  GitHub publisher implementations.
- No weakening of replay, stale payload/head, expiry, or unauthorized-actor
  checks.
- No storage or response of raw nonces after an approval is consumed.

## Implementation

- Extend pending approval proposals with repository alias and PR reference.
- Extend approval decision/history records with repository alias, PR reference,
  durable finding-set hash, and approver principal ID.
- Add focused HTTP/service tests for binding visibility, replay rejection,
  stale-head rejection, unauthorized approval denial, and nonce redaction.

## Validation

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = 'E:\shiyan\code_review_agent\traces\worktrees\release-v0.1\.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $repoRoot 'src'
& $python -m unittest -v tests.test_issue28_approval_binding_api
& $python -m unittest -v tests.test_phase9d_approval_feedback
& $python -m ruff check src/code_review_agent/database.py tests/test_issue28_approval_binding_api.py
& $python -m mypy src/code_review_agent
git diff --check
```

## Acceptance Criteria

- Pending approval proposals expose repository, PR, head SHA, payload hash,
  finding-set hash, nonce, and expiry.
- Approval decisions and approval-history responses expose approver, repository,
  PR, head SHA, payload hash, finding-set hash, and expiry while hiding consumed
  nonces.
- Replayed, stale-head, expired, and unauthorized decisions remain rejected.
