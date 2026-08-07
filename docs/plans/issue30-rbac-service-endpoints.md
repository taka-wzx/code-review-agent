# Issue 30: organization and repository RBAC on service endpoints

## Goal

Enforce one organization-and-repository authorization boundary across Review
submission, the durable worker claim path, synthetic Repair endpoints, and
organization policy/quota management. Cross-tenant and same-tenant
unauthorized repository access must have stable not-found/forbidden behavior
and produce bounded audit events.

## Base

- Base branch: `master`
- Base commit: `345b3035eca2f7af65f650fdeaa7a1e5e7297194`
- Integration branch: `integration/issue30-rbac-service-endpoints`

## Frozen interfaces

- Keep existing REST paths, MCP tool names, service method names, response
  fields, and error codes compatible.
- Keep `Principal`, `Role`, and `Permission` values unchanged.
- Review submissions require the authenticated principal's organization and
  repository access; Webhook HMAC remains a system-attributed submission path.
- Review workers are system actors. Before claiming or executing work they
  must verify the job's organization/repository lineage and that the active
  repository still exists; they do not receive a user bearer token.
- Repair reads and decisions require the same organization, repository access,
  role, and allowed human auth-method checks as Repair creation.
- Policy and quota management remains `org_admin`-only; organization path
  parameters must match the principal and repository IDs must belong to that
  organization.
- Audit records contain only existing bounded fields and stable reason codes;
  no credentials, request bodies, diffs, prompts, or filesystem paths are
  added.

## File ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue30-rbac-service-endpoints.md`; `src/code_review_agent/database.py`; `src/code_review_agent/repair_service.py`; `src/code_review_agent/service.py`; `src/code_review_agent/service_core.py`; `src/code_review_agent/service_queue.py`; `src/code_review_agent/worker.py`; `tests/test_issue30_rbac_service_endpoints.py` | all other repository paths |
| Claude Code | none | all paths |
| Integrator | conflict resolution only | all paths |

## Prohibited changes

- No direct commits, merges, or pushes to `master`.
- No external Provider, GitHub, OAuth, Postgres, or network calls.
- Do not read or modify `eval/**` or `eval/holdout/**`.
- No dependency, schema, public API, or unrelated formatting changes.
- No credentials, tokens, `.env` files, or absolute private paths in source,
  tests, commits, or handoffs.

## Agent assignments

### Codex

- Objective: implement the bounded RBAC and audit hardening in the owned files.
- Required tests: `tests.test_issue30_rbac_service_endpoints`, the focused
  Phase 9B/9C/10 regressions, Ruff, Mypy, `scripts/verify.py`, and
  `git diff --check`.
- Delivery commit: pending.

### Claude Code

- Objective: none.
- Required tests: none.
- Delivery commit: none.

## Validation

Use the repository `.venv` and `PYTHONPATH=src`. Tests must use fakes and
temporary SQLite only. Do not run evaluation assets or real external calls.

## Acceptance criteria

- Same-tenant users cannot read or approve Repair jobs for repositories they do
  not access.
- Cross-tenant Review, Repair, policy, and quota requests are rejected without
  revealing resource existence.
- Worker claims re-check active organization/repository lineage before work.
- Policy/quota denial and allow paths are audited with stable correlation IDs.
- Principal-to-role fixtures cover viewer, reviewer, maintainer, and org_admin.
- Existing offline regression tests remain passing.

## Handoff and integration

Codex delivers one task-branch commit. The reviewer inspects the full diff and
validation results. Integration occurs only on the integration branch; merge,
push, or PR publication requires explicit user authorization.

## Delivery report

- Summary: Implemented organization and repository RBAC enforcement across
  Review submission, durable worker claiming/execution, synthetic Repair
  endpoints, and organization policy/quota management. Added bounded denial
  auditing and explicit role fixtures/regression coverage.
- Changed files: `src/code_review_agent/database.py`,
  `src/code_review_agent/repair_service.py`,
  `src/code_review_agent/service.py`,
  `src/code_review_agent/service_core.py`,
  `src/code_review_agent/service_queue.py`,
  `src/code_review_agent/worker.py`, and
  `tests/test_issue30_rbac_service_endpoints.py`.
- Commit: the local task-branch commit SHA is reported in the handoff.
- Commands and results: Issue #30 tests 5/5; Phase 9B 8/8; Phase 9C 46/46;
  Phase 10 22/22; Week 7 service/core/MCP regressions passed; Ruff clean;
  Mypy clean for 37 source files; `scripts/verify.py` passed 972 tests with
  18 skips and 85% total coverage; `git diff --check` passed.
- Known risks or assumptions: validation used fakes and temporary SQLite only;
  no external Provider, GitHub, OAuth, Postgres, or network path was called.
  The worker still leaves queued jobs for repositories that are inactive and
  terminalizes only the already-leased race, preserving the existing recovery
  model.
- Suggested review focus: composite organization/repository lineage checks,
  same-tenant repository denial behavior, worker deactivation races, and
  bounded authorization audit events.
