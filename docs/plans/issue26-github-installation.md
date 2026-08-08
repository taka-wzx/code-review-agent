# Issue #26: GitHub App installation lifecycle validation

## Goal

Validate the GitHub App, installation, account, and registered repository
identity before a service operation is allowed. Detect suspended, deleted, or
ambiguous installations fail-closed and emit a sanitized lifecycle receipt
through an injected audit sink.

## Base

- Base branch: `master`
- Base commit: `345b3035eca2f7af65f650fdeaa7a1e5e7297194`
- Task branch: `codex/issue-26-github-installation`

## Frozen interfaces

- The GitHub API client is an injected protocol; tests never open a socket and
  the validator never selects a token source.
- The installation credential value is accepted only in memory and is never
  copied into a receipt, exception, audit payload, or log.
- Registered owner/name comparisons are case-insensitive, while immutable
  numeric IDs and installation/account IDs must match exactly.
- Receipt fields are bounded IDs, enums, booleans, timestamps, and hashes only.
  No response body, repository payload, URL, token, or owner/name is emitted.

## File ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue26-github-installation.md`, `src/code_review_agent/github_installation.py`, `tests/test_issue26_github_installation.py` | all other paths |

## Prohibited changes

- No changes to `eval/**`, `eval/holdout/**`, migrations, dependencies, CI,
  existing GitHub canary publishers, or public REST routes.
- No real GitHub API calls, App private keys, installation tokens, `gh` state,
  or credential files in tests or commits.
- No automatic user/repository enrollment and no fallback when identity checks
  fail.

## Implementation

- Add validated registration and short-lived in-memory credential value types.
- Add an injected client protocol for installation and repository reads.
- Validate App ID, installation ID/account ID, repository ID, canonical name,
  suspension/deletion markers, and API error states.
- Produce a canonical hash-bound lifecycle receipt and an optional database-like
  audit sink adapter that records only stable audit fields.
- Add fake-client tests for active, mismatch, suspended, deleted, expired,
  revoked, malformed, and audit/redaction paths.

## Validation

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = 'E:\shiyan\code_review_agent\traces\worktrees\release-v0.1\.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $repoRoot 'src'
& $python -m unittest -v tests.test_issue26_github_installation
& $python -m ruff check src/code_review_agent/github_installation.py tests/test_issue26_github_installation.py
& $python -m mypy src/code_review_agent/github_installation.py
git diff --check
```

## Acceptance criteria

- A matching active installation and repository returns an allow receipt.
- Any App/installation/account/repository identity drift denies validation
  without calling later API stages or leaking response data.
- Suspended, deleted, expired, or revoked credentials deny validation and are
  represented by bounded lifecycle reason codes.
- Audit receipts contain no raw token and remain usable after process restart
  when the caller persists them through the supplied sink.
