# Phase 9D: Maintainer Approval, Guarded Publish, and Finding Feedback

Status: **active and frozen**

Frozen date: 2026-07-24

Baseline: `origin/master` at `047e1618957ae982baecb710b47913fdc634159b`

Task branch: `codex/phase9d-approval-feedback`

## Goal

Complete the Review product control-plane loop:

```text
shadow review -> durable maintainer approval -> guarded publisher adapter
-> finding feedback -> business-metric event data
```

Review remains shadow-only by default. This task implements only offline
`FakePublisher`, `DryRunPublisher`, and the fail-closed `GitHubPublisher`
interface. It must not make an HTTP request, invoke `gh`, call a real GitHub
API, publish a real GitHub comment, or call a real model.

## Scope and compatibility

- Add stable Finding IDs plus content, evidence, and source-revision hashes to
  durable results. A Finding version is bound to an immutable PR head SHA and
  source revision.
- Add a one-use, expiring publication approval that is persisted before any
  publisher invocation. Its binding includes `principal_id`, `organization_id`,
  `repository_id`, `review_job_id`, PR/head SHA, exact canonical-payload
  SHA-256, `policy_version`, nonce, expiry, and use time.
- Only an authorized `maintainer` or `org_admin` with access to the same
  repository may approve or reject. Webhook/system actors, viewers, reviewers,
  and model tools cannot create, approve, consume, or publish an approval.
- Any payload, Finding, PR/head SHA, or policy-version change invalidates the
  old approval. Replays, expired approvals, consumed approvals, and payload
  mismatches fail closed.
- Use a database transaction to record approval consumption and the durable
  publish intent before publisher invocation. A failure or timeout must not
  mark a comment as published. Retry/recovery must reuse one idempotency key and
  never invoke a publisher twice after a durable successful receipt.
- Provide `FakePublisher`, `DryRunPublisher`, and a `GitHubPublisher` protocol
  or interface. `GitHubPublisher` must reject direct use in this phase.
- Persist developer feedback as `accepted`, `rejected`, `uncertain`, `fixed`,
  or `duplicate`, bound to the acting principal, timestamp, rationale, and
  Finding hash. Feedback is audit and metric data only: it must not mutate a
  prompt, rule, or long-term/user memory.
- Add authenticated REST APIs to list pending reviews, approve or reject an
  exact publish proposal, submit finding feedback, and read approval/feedback
  audits. Tenant predicates and existing stable not-found behavior apply to all
  resource reads and writes.
- Create only bounded, redacted audit/trace data. It must contain no review
  body, diff, token, raw approval challenge/nonce, authorization data, or
  publisher payload. Metric events contain stable IDs and hashes, never raw
  review content.

Existing public Review submission, worker, webhook, MCP tool, and CLI
semantics remain compatible. This task adds no model-callable approval tool and
does not alter Finder/Verifier prompts, sentinel policy, evaluation assets,
dependencies, package entry points, CI workflows, or deployment configuration.

## Durable state and publication invariants

The existing job state machine remains authoritative:

```text
awaiting_approval -> approved | declined
approved -> published | failed
```

`approved` means a valid approval and durable pending publication intent exist;
it never implies an externally delivered comment. `published` requires a
durably recorded idempotent publisher success. `failed` retains the approval,
publish attempt audit, and failure category without fabricating success.

The exact canonical payload uses deterministic JSON serialization and is hashed
with SHA-256. The same canonical bytes are used for validation and publisher
input. The publisher receives a stable non-secret idempotency key derived from
the review job, Finding hashes, payload hash, and policy version. It never
receives an access credential from this task.

Approval APIs must be atomic under concurrent requests: a double-click or two
maintainers racing for the same proposal produces exactly one successful state
change and at most one publisher call. A timeout after a publisher call leaves
the intent recoverable; a retry may query an idempotent fake/dry-run receipt,
but cannot publish another comment.

## API surface

All endpoints require an authenticated human principal and repository access.
Their request/response schemas expose IDs, policy/version data, hashes, times,
and stable status/reason codes only; they do not return a raw publisher payload
or approval challenge.

- `GET /v1/reviews/pending-approval`
- `POST /v1/reviews/{review_job_id}/approve`
- `POST /v1/reviews/{review_job_id}/reject`
- `POST /v1/findings/{finding_id}/feedback`
- `GET /v1/reviews/{review_job_id}/approvals`
- `GET /v1/findings/{finding_id}/feedback`

The implementation may add a narrow read endpoint or query parameters required
to select a specific Finding/proposal, but may not add an external-write or
model-control endpoint.

## Owned paths

Codex owns only the following paths for this task:

- `docs/plans/phase9d-approval-feedback.md`;
- `migrations/versions/0004_phase9d_approval_feedback.py`;
- `src/code_review_agent/database.py`;
- `src/code_review_agent/identity.py` (add a publication-approval permission
  granted only to `maintainer` and `org_admin`; legacy single-Finding shadow
  decisions keep their frozen Phase 9B permission semantics);
- `src/code_review_agent/service_core.py`;
- `src/code_review_agent/service.py`;
- `src/code_review_agent/service_queue.py` (generate durable Finding identity
  and hash lineage when a worker persists a review result);
- `src/code_review_agent/approval_publish.py` (new);
- `tests/test_phase9d_approval_feedback.py` (new);
- `tests/test_phase9b_identity_rbac.py` (only additive Phase 9D API/RBAC
  coverage if existing test helpers require it);
- `tests/test_phase9c_durable_service.py` (only additive Phase 9D durable
  state compatibility coverage if existing test helpers require it).

All other paths are read-only. In particular, `eval/**`, including
`eval/holdout/**`, is prohibited: do not enumerate, read, execute, or modify
it. The pre-existing untracked `%SystemDrive%/` path is not owned, must not be
deleted, staged, committed, or included in the diff.

## Validation

All validation is offline and uses fake principals, fake runners, SQLite test
databases, and fake/dry-run publishers only. It must not contact GitHub or a
model provider.

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = Join-Path $repoRoot ".venv\\Scripts\\python.exe"
$env:PYTHONPATH = Join-Path $repoRoot "src"

& $python -m unittest -v tests.test_phase9d_approval_feedback `
  tests.test_phase9b_identity_rbac tests.test_phase9c_durable_service `
  tests.test_week7_service tests.test_week7_service_core tests.test_week7_mcp

& $python -m ruff check .
& $python -m mypy src/code_review_agent
& $python scripts\verify.py
& $python -m pip check
git diff --check
git diff --name-only origin/master...HEAD
git status --short --branch
```

Required automated cases include a complete fake-publisher loop; restart with a
pending approval; concurrent approval; cross-organization isolation; approval
role and webhook/model rejection; expiry, replay, consumed, and payload-change
rejection; zero publisher calls before approval; one success under double-click;
no duplicate comment after a timeout; feedback actor/rationale/hash lineage;
and trace/audit redaction.

## Delivery control

The user authorizes a stable task-branch commit, push of only
`codex/phase9d-approval-feedback`, a Draft PR, CI observation, Ready state, and
merge through that PR only after required checks pass. Direct push, merge, or
rebase of `master` remains prohibited. After merge, verify the merge SHA and
the `master` CI result. Real GitHub publishing, real model calls, external
GitHub content writes, and cloud deployment remain prohibited even after the
task PR is merged.

## Change control

This contract is frozen after creation. Any dependency, public interface,
state-semantic, external-call, writable-path, or policy change beyond this
document requires explicit user approval and a contract revision before code is
edited.

### Contract correction

- 2026-07-24: The initial owned-path list omitted `identity.py` and
  `service_queue.py`. Both are required by the user-authorized requirements:
  the former introduces a separate publication-approval permission for the
  explicitly authorized maintainer/org-admin pair without turning the legacy
  shadow-decision endpoint into a publication bypass, and the latter is the
  sole existing Finding-persistence boundary where stable Finding IDs and
  content/evidence/source-revision hashes can be created. No dependency,
  external call, or scope expansion is introduced.
