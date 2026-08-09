# Issue #33: GitHub App webhook acknowledgement

## Goal

Add a durable, fail-closed GitHub App installation lifecycle and webhook
acknowledgement path. Valid signed installation and pull-request deliveries
must receive a prompt durable acknowledgement; repeated delivery IDs must never
create a second installation transition or review job. Unsupported event types
retain the existing no-side-effect acknowledgement behavior.

## Base

- Base branch: `master`
- Base commit: `6ced1c4ebde377bfc386c4173efb700acdb27416`
- Task branch: `codex/issue-33-github-webhook-ack`

## File ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue33-github-webhook-ack.md`, `migrations/versions/0010_issue33_github_webhook.py`, `src/code_review_agent/database.py`, `src/code_review_agent/github_webhook.py`, `src/code_review_agent/service.py`, `tests/test_issue33_github_webhook_ack.py` | all other paths |

## Frozen interfaces

- The existing `/webhooks/github` endpoint remains HMAC authenticated and
  preserves the existing response shape for legacy pull-request webhooks.
- No GitHub API, App private key, installation token, repository content, or
  webhook body is persisted by this task.
- Installation and delivery records contain only validated numeric IDs, event
  types, state enums, HTTP acknowledgement status, and SHA-256 payload hashes.
- Existing HMAC webhooks without an `installation` object remain supported.
  When an App installation is supplied, it must be active and its account ID
  must match the repository owner before a review job can be queued.

## Prohibited changes

- No changes to `eval/**`, `eval/holdout/**`, dependencies, CI, public CLI
  options, GitHub publishers, or protected-branch behavior.
- No live GitHub calls or credentials in tests, commits, logs, or receipts.
- No automatic repository enrollment, App installation creation, or fallback
  from an inactive or identity-mismatched App installation to a review job.

## Implementation

- Add a migration for hash-only webhook delivery receipts and GitHub App
  installation state records.
- Add atomic database helpers that bind every accepted delivery ID to its event
  type and payload SHA-256 and reject conflicting re-use.
- Accept `installation` events for `created`, `suspend`, `unsuspend`,
  `deleted`, and `new_permissions_accepted`; transitions are fail-closed.
- Route signed webhook events through a processor that returns durable `200`
  or `202` acknowledgements, ignores untrusted/inactive App PR events, and
  delegates accepted PR submission to the existing durable review queue.
- Add integration tests covering signature rejection, lifecycle transitions,
  replay/conflict behavior, restart durability, account binding, and prompt
  PR acknowledgement.

## Validation

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = 'E:\shiyan\code_review_agent\traces\worktrees\release-v0.1\.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $repoRoot 'src'
& $python -m unittest -v tests.test_issue33_github_webhook_ack
& $python -m unittest -v tests.test_week7_service
& $python -m ruff check src/code_review_agent/database.py src/code_review_agent/github_webhook.py src/code_review_agent/service.py tests/test_issue33_github_webhook_ack.py
& $python -m mypy src/code_review_agent/database.py src/code_review_agent/github_webhook.py src/code_review_agent/service.py
git diff --check
```

## Acceptance criteria

- A valid installation lifecycle delivery reaches the expected durable state.
- A repeated delivery returns the original acknowledgement without a second
  state transition or review job; conflicting payload reuse is rejected.
- An active App installation with matching account identity can queue a PR
  review, while unknown, suspended, deleted, or mismatched installations are
  acknowledged as ignored without queueing work.
- Invalid signatures mutate no delivery or installation record.
