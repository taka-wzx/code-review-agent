# Issue #32: Notification delivery and incident routing

## Goal

Add an isolated notification-routing core that accepts safe, typed service,
security, approval, and publication failure signals; deterministically routes,
deduplicates, retries, escalates, and dead-letters delivery attempts through an
injectable no-I/O sender boundary.

## Base

- Base branch: `master`
- Base commit: `6ced1c4ebde377bfc386c4173efb700acdb27416`
- Task branch: `codex/issue-32-notification-routing`
- GitHub Issue: https://github.com/taka-wzx/code-review-agent/issues/32

## Frozen Interfaces

- Existing service routes, public package APIs, database schema, migrations,
  workflow files, dependency declarations, and CLI behavior remain unchanged.
- This task adds a self-contained internal module only. It introduces no real
  delivery channel, HTTP client, webhook, credential, secret, cloud resource,
  background process, or database integration.
- The core accepts only bounded identifiers, enums, UTC timestamps, and safe
  reason codes. It must not retain or expose arbitrary exception messages,
  provider responses, repository locators, credentials, notification payloads,
  or filesystem paths.
- Unit tests use only an injected clock and deterministic in-memory fakes.
  `eval/**` and `eval/holdout/**` are not read, run, or modified.

## File Ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue32-notification-routing.md`, `src/code_review_agent/notification_routing.py`, `tests/test_issue32_notification_routing.py` | all other paths |

## Implementation

- Define fixed event classes for `service_health`, `security`, `approval`, and
  `publication`, plus bounded severity, reason-code, route, and delivery-state
  enums.
- Implement a policy matcher with explicit primary and escalation routes, a
  minimum severity threshold, per-policy retry limit, exponential backoff cap,
  and deduplication window.
- Accept an injected sender protocol and an injected UTC clock. The sender
  receives only a safe delivery projection and returns a bounded outcome.
- Deduplicate matching alerts during the policy window without resetting a
  prior delivery's retry state. Deliveries receive stable opaque IDs and never
  serialize raw event content or sender exception text.
- On a retryable failure, schedule bounded exponential retry; after the retry
  budget is exhausted, move the delivery to a terminal dead-letter state. A
  non-retryable result dead-letters immediately. Escalate the route only under
  the configured severity/attempt rule.

## Required Tests

- Routing-policy coverage for service-health, security, approval, and
  publication events, including severity threshold rejection.
- Successful delivery, retry scheduling, retry exhaustion/dead-letter,
  non-retryable dead-letter, and escalation-route coverage.
- Deduplication-window, clock-boundary, duplicate-delivery, invalid-input, and
  sender-failure redaction coverage.
- In-memory sender coverage demonstrating no filesystem, network, credential,
  or external-service dependency.

## Validation

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = 'E:\shiyan\code_review_agent\traces\worktrees\release-v0.1\.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $repoRoot 'src'
& $python -m unittest -v tests.test_issue32_notification_routing
& $python -m ruff check src/code_review_agent/notification_routing.py tests/test_issue32_notification_routing.py
& $python -m mypy src/code_review_agent/notification_routing.py
& $python scripts/verify.py
& $python -m pip check
git diff --check
```

## Acceptance Criteria

- Each of the four bounded event classes routes only to its matching policy and
  eligible severity routes.
- Duplicate alerts are suppressed inside the configured window without
  producing an extra sender call or delivery record.
- Retryable outcomes use a bounded backoff and end in a dead-letter record once
  exhausted; non-retryable outcomes do not retry.
- Escalations choose only the policy's explicit escalation route and expose no
  raw sender error details.
- The module and tests are offline, deterministic, and do not alter existing
  service/database behavior.
