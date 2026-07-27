# Phase 10 Prep: offline Review-to-Repair service

Status: active

Date: 2026-07-27

Base: `master` at `405e90a955ae28c3ad7e19fcfb7b7dfcf34bd37a`

Branch: `codex/phase10-review-repair-service`

## Claim and authorization boundary

The former prerequisite "at least one real Business Pilot" is explicitly revoked.
This phase is **Phase 10 Prep**, an offline engineering exercise. It must never be
described as a deployed online loop, a production-ready Repair service, a real
Business Pilot, or real business validation. All acceptance uses fake models,
fake/dry-run publishers, synthetic repositories, and the existing offline Docker
sandbox. No paid model call, real GitHub product write, deployment, automatic merge,
or protected-branch mutation is authorized.

Task-branch Git operations used to deliver this repository change (commit, push,
pull request, CI, Ready, and repository-owner merge) are control-plane delivery and
must remain separate from the disabled product-side Repair publisher.

`eval/**` and `eval/holdout/**` must not be read, enumerated, modified, or executed.

## Goal

Add an offline-testable durable service control plane for:

```text
human-selected Finding
  -> bound Repair plan
  -> remote WRITE approval
  -> sandboxed PATCH / TEST / REFLECT
  -> remote DRAFT_PR approval
  -> fake or dry-run Draft PR publisher
```

The service is additive and does not replace the Week 3 local Repair orchestrator.
It exposes Python service interfaces and durable checkpoints, not new public HTTP
routes or a production deployment.

## Frozen security semantics

- Only `maintainer` and `org_admin` principals may start or approve a Repair.
  `viewer`, `reviewer`, webhook, model, Finding, and unauthenticated actors cannot.
- A Finding is input evidence only and never grants write authority. The request is
  bound to its SHA-256, organization, repository, base/head SHA, requesting
  principal, and organization-policy hash.
- Each job binds a unique task branch/worktree receipt. The original checkout is
  read-only and must be revalidated before every mutation or publication step.
- WRITE approval is one-use and binds the exact checkpoint, plan, attempt, current
  diff, repository/base/head, policy, and writable paths.
- DRAFT_PR approval is independent, one-use, and binds the exact checkpoint, full
  diff hash, test evidence hash, durable budget hash, commit message, head branch,
  target/base, repository/base/head, and publisher payload hash.
- Stale, replayed, concurrent, expired, rejected, or mismatched approvals fail
  closed. Every retry has a revised plan and a fresh WRITE approval.
- Fixed test commands run only through a sandbox receipt proving Docker, non-root,
  network `none`, timeout, bounded output, and the unique task worktree scope.
- Budget usage is checkpointed before and after operations and is never reset by a
  process restart.
- Approval-wait states own no worker lease. Crash recovery revalidates the
  repository, base/head, worktree, diff, checkpoint checksum, budget, operation
  receipt, and approval before continuing.
- An interrupted model or mutation intent with no durable idempotent receipt is
  quarantined rather than replayed. A completed receipt is reconciled without a
  duplicate call or mutation.
- Test failure, budget exhaustion, approval rejection, protected task branch,
  invalid sandbox evidence, and publisher failure cannot be reported as success.
  Failure rolls back through the executor or quarantines when safe rollback cannot
  be proved.
- The service has no merge API and never pushes `master` or another protected
  branch.
- Traces and metrics contain only bounded enums, booleans, counts, hashes, and
  durations. They never contain a patch, full diff, prompt, model response,
  stdout/stderr, credential, identity mapping, or host path.

## Durable states and leases

The additive Phase 10 state model is:

```text
queued_plan -> planning -> awaiting_write_approval
awaiting_write_approval -> queued_execution -> executing
executing -> awaiting_write_approval       (bounded retry)
executing -> awaiting_draft_pr_approval     (tests passed)
awaiting_draft_pr_approval -> queued_publish -> publishing -> draft_published
```

`declined`, `failed`, and `quarantined` are terminal. Only `planning`, `executing`,
and `publishing` may hold a worker lease. Lease expiry never authorizes blind replay
of an unresolved external or mutation operation.

## Publisher boundary

The new publisher module provides:

- `DraftPrPublisher` protocol;
- `FakeDraftPrPublisher` for deterministic idempotency/failure tests;
- `DryRunDraftPrPublisher`, which returns synthetic receipts and performs no I/O;
- `GitHubDraftPrPublisher`, a fail-closed interface whose real implementation is
  deliberately absent.

No merge method exists. Publisher receipts and traces store only stable IDs/hashes,
never raw diff or test output.

## Observability and metrics

The service accepts the existing canonical trace and Phase 9F-compatible metrics
sink. It emits bounded state/approval/outcome/sandbox/publisher events and counters.
Repository alias, principal, job ID, branch, path, patch, stdout/stderr, and exception
messages are excluded from telemetry.

## Single Writer files

Codex owns only:

- `docs/plans/phase10-review-repair-service.md`;
- `docs/phase10-review-repair-service.md`;
- `src/code_review_agent/repair_service.py`;
- `src/code_review_agent/repair_publish.py`;
- `tests/test_phase10_repair_service.py`.

All existing Repair, identity, worker, approval, publisher, metrics, sandbox,
database, migration, service, CLI, packaging, workflow, and evaluation files are
read-only. Scope expansion requires explicit approval before editing.

## Offline acceptance

- Fake model/executor/publisher completes Finding -> Repair -> synthetic Draft PR.
- At least two forced interruption boundaries resume without duplicate model calls,
  mutations, commits, or publications.
- Approval rejection has no commit, publisher, or external side effect.
- Viewer/reviewer/webhook/model/Finding approvals are rejected.
- Changed plan, patch/diff, tests, budget, checkpoint, base/head, policy, or target
  invalidates prior approval.
- Two simultaneous approvals yield exactly one successful transition.
- Test failure/retry requires a new WRITE approval; finite retry, timeout, output,
  network, and budget failures fail closed.
- Protected task branches are rejected; publisher failure is not success.
- Restart revalidates repository/base/head/worktree/diff/checkpoint/approval.
- Trace and metrics scans prove absence of patch, stdout/stderr, credentials, and
  host paths.
- Synthetic fixtures can never enable a real publisher or business/quality gate.

## Validation

All commands are offline and do not read evaluation assets:

```powershell
python -m unittest -v tests.test_phase10_repair_service
python -m unittest -v tests.test_week3_repair
python -m unittest discover -s tests
python -m ruff check .
python -m mypy src/code_review_agent
python scripts/verify.py
python -m pip check
git diff --check
```

When Docker is available, also run:

```powershell
$env:CRAG_RUN_DOCKER_E2E = "1"
python -m unittest -v tests.test_week3_docker_e2e
```

The existing Docker suite proves the reused sandbox boundary; it is not deployment,
production capacity, or real Repair evidence.

## Delivery

Create one stable local commit, push only the task branch, create a Draft PR, wait
for all CI jobs, and turn Ready only after they pass. Never push or merge directly to
`master`; the repository owner performs the merge. After merge, confirm the exact
merge SHA and master CI.
