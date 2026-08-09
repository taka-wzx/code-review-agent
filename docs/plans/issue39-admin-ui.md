# Issue #39 Admin UI Contract

## Scope

Implement a small, dependency-free administration console for organization and
repository configuration and maintainer approval actions.

## Owned Files

- `docs/plans/issue39-admin-ui.md` - task contract and delivery record.
- `docs/admin-ui.md` - operator and security notes for the console.
- `src/code_review_agent/admin_ui.py` - HTML, CSS, JavaScript, and route installer.
- `src/code_review_agent/service.py` - install the console routes.
- `tests/test_issue39_admin_ui.py` - focused route and browser-contract tests.

No other files may be modified in this task worktree.

## Frozen Interfaces

- Existing `/v1` API paths and request/response schemas remain unchanged.
- Existing authentication and server-side RBAC remain authoritative.
- The console is exposed at `/admin` and its same-origin assets under
  `/admin/assets/`.
- The console does not introduce a frontend build dependency or third-party
  runtime asset.

## Acceptance Criteria

1. `/admin` serves a responsive, usable administration console without
   embedding credentials or organization data.
2. The console authenticates with a session-only bearer token and loads the
   principal before rendering role-gated views.
3. Organization administrators can view and edit organization policy, manage
   registered repositories, and manage memberships through the existing API.
4. Maintainers and organization administrators can inspect pending approvals;
   each approval or rejection requires an explicit browser confirmation before
   the existing audited API action is sent.
5. Viewers and reviewers do not receive controls for operations outside their
   role, while the API remains the final authorization boundary.
6. Focused tests verify HTML/assets, session-only token handling, role gating,
   confirmation behavior, and that the service still rejects unauthorized API
   writes.

## Validation

- `tests.test_issue39_admin_ui`
- `tests.test_runtime tests.test_phase9c_durable_service`
- `ruff check .`
- `mypy src/code_review_agent`
- `scripts/verify.py`
- `git diff --check`

## Delivery

Status: complete

- Focused tests: 4 passed.
- Runtime and durable-service regression tests: 58 passed.
- Full suite: 1115 passed, 18 skipped.
- `scripts/verify.py`: passed with 85% total coverage.
- Ruff, mypy, module entry point, console entry point, Node JavaScript syntax,
  and `git diff --check`: passed.
