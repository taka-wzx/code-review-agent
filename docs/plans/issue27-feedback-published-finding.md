# Issue #27: Published finding feedback binding

## Goal

Persist developer accept/reject feedback only when it is bound to a published
GitHub pull-request finding version. Reject stale content hashes, unpublished
findings, and cross-repository or cross-tenant attempts fail-closed.

## Base

- Base branch: `master`
- Base commit: `345b3035eca2f7af65f650fdeaa7a1e5e7297194`
- Task branch: `codex/issue-27-feedback-published-finding`
- GitHub Issue: https://github.com/taka-wzx/code-review-agent/issues/27

## Frozen Interfaces

- No real GitHub API, token, or webhook calls are introduced.
- Feedback records store only stable IDs, bounded decisions/rationales, and
  hashes. They must not store raw finding text, publisher payloads, repository
  aliases, PR URLs, tokens, or GitHub response bodies.
- Existing feedback decision vocabulary remains `accepted`, `rejected`,
  `uncertain`, `fixed`, and `duplicate`.
- Repository scoping remains enforced through existing principals and
  repository access checks.

## File Ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue27-feedback-published-finding.md`, `migrations/versions/0009_issue27_published_feedback.py`, `src/code_review_agent/database.py`, `src/code_review_agent/service.py`, `src/code_review_agent/service_core.py`, `tests/test_issue27_published_feedback.py`, `tests/test_phase9b_identity_rbac.py`, `tests/test_phase9d_approval_feedback.py` | all other paths |

## Prohibited Changes

- No changes to `eval/**`, `eval/holdout/**`, dependency files, CI, public
  review/finder prompts, or GitHub publisher implementations.
- No fallback path that accepts feedback for unpublished findings.
- No persistence of raw tokens, private keys, raw GitHub payloads, or finding
  message/body text inside feedback binding fields.

## Implementation

- Add a migration that extends `finding_feedback` with publish binding fields:
  publish approval ID, published payload hash, published head SHA, and a
  canonical published finding identity hash.
- Resolve feedback binding from the published review job, successful publish
  attempt, current finding content hash, and repository scope before insert.
- Require callers to provide the observed `finding_hash`; reject stale hashes.
- Emit aggregate-safe metric events keyed only by content hash and existing
  bounded IDs.
- Add HTTP/service tests for published feedback success, version drift,
  unpublished finding denial, and cross-tenant repository scoping.

## Validation

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = 'E:\shiyan\code_review_agent\traces\worktrees\release-v0.1\.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $repoRoot 'src'
& $python -m unittest -v tests.test_issue27_published_feedback
& $python -m unittest -v tests.test_phase9b_identity_rbac
& $python -m unittest -v tests.test_phase9d_approval_feedback
& $python -m ruff check migrations/versions/0009_issue27_published_feedback.py src/code_review_agent/database.py src/code_review_agent/service.py src/code_review_agent/service_core.py tests/test_issue27_published_feedback.py tests/test_phase9b_identity_rbac.py tests/test_phase9d_approval_feedback.py
& $python -m mypy src/code_review_agent
git diff --check
```

## Acceptance Criteria

- Feedback submitted with the current published finding hash persists with the
  published payload/head/identity binding and does not store raw finding text.
- Feedback submitted before publication, for stale finding content, or outside
  the actor's repository scope is rejected.
- Feedback metrics remain aggregate-safe and use the content hash subject.
- The focused implementation is delivered as one Draft PR targeting `master`.
