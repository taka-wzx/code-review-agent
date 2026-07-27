# Phase 10 Prep: offline Review-to-Repair service

Phase 10 Prep adds an offline, durable service control plane around the existing
Week 3 Repair safety primitives. It is not an online deployment and has no real
business validation. The implementation never calls a paid model, GitHub, or a
deployment API, and every public job response keeps:

```text
synthetic_only=true
real_writes_enabled=false
business_claim_allowed=false
quality_claim_allowed=false
```

## What is implemented

`code_review_agent.repair_service` provides Python service interfaces for:

- a maintainer/admin-created Repair job bound to the Finding hash, organization,
  repository, base/head SHA, requesting principal, and organization-policy hash;
- a unique executor-provided worktree and `repair/<job-id>` task branch;
- checksum-bound atomic checkpoints and a redacted append-only event journal;
- worker leases only while planning, executing, or publishing;
- a remote WRITE approval bound to the exact checkpoint, plan, patch, diff, attempt,
  writable paths, repository snapshot, and policy;
- sandbox receipt validation for Docker, non-root, `network=none`, fixed commands,
  timeout, output cap, test result, and unchanged original checkout;
- bounded retry through a revised plan and a new WRITE approval;
- a remote DRAFT_PR approval view containing the complete diff, structured tests,
  durable budget ledger, commit message, head branch, target/base, and all binding
  hashes;
- local commit creation only after DRAFT_PR approval;
- idempotent fake/dry-run Draft PR publication, with no merge operation.

`code_review_agent.repair_publish` provides `DraftPrPublisher`,
`FakeDraftPrPublisher`, `DryRunDraftPrPublisher`, and a deliberately disabled
`GitHubDraftPrPublisher`. All receipts are synthetic and contain only IDs and hashes.

No FastAPI route, database migration, deployment, provider client, GitHub credential,
real branch push, real PR creation, or merge API is included.

## Durable state flow

```text
queued_plan
  -> planning
  -> awaiting_write_approval          (no worker lease)
  -> queued_execution
  -> executing
       -> queued_plan                 (failed tests, bounded retry)
       -> awaiting_draft_pr_approval  (no worker lease)
  -> queued_publish
  -> publishing
  -> draft_published                  (synthetic receipt only)
```

`declined`, `failed`, and `quarantined` are terminal. A publisher failure after the
human-approved isolated commit enters `quarantined` and never records
`draft_published`. Test failure, budget exhaustion, or approval rejection creates no
commit or publication.

## Recovery semantics

The checkpoint records an operation intent before every planner, mutation, reflection,
commit, or publisher boundary. On restart:

1. the service reloads and checksum-verifies the checkpoint;
2. lease ownership is recovered only after expiry;
3. repository/base/head/worktree/current-diff and consumed approval bindings are
   revalidated;
4. a durable idempotent receipt is looked up;
5. an observed receipt is reconciled without reissuing the operation;
6. an unresolved prior intent with no receipt is quarantined and never replayed.

The budget snapshot includes outstanding model reservations. A restart either
reconciles the exact receipt into that reservation or quarantines the job; it never
constructs a fresh budget manager from defaults.

## Identity and approval rules

Only `maintainer` and `org_admin` principals from the bound organization may start or
approve. Principals whose authentication method is `model`, `finding`, `webhook`, or
`github_webhook` are rejected even if their role field is privileged. Viewer and
reviewer are also rejected.

Approvals are consumed atomically with their state transition. The stored approval
contains an irreversible approver digest, a nonce digest, the canonical binding, and
its binding hash. Concurrent or replayed approval requests cannot both succeed.

## Sandbox and publisher adapters

Phase 10 Prep accepts only planner and executor adapters explicitly marked
`offline_only=true`. The executor is responsible for using the existing Week 3
worktree and `DockerSandboxRunner` implementation. The service independently rejects
any receipt that does not prove:

- the bound repository/base/head/worktree and unchanged original checkout;
- Docker execution as non-root;
- network mode `none`;
- exactly the policy's fixed test commands;
- timeout and output limits no greater than policy limits;
- a full diff whose SHA-256 matches the repository snapshot.

The real GitHub publisher is intentionally disabled. Delivery of this repository
task branch and PR is not a product Repair publication and must not be confused with
one.

## Observability and Phase 9F metrics

Canonical trace events contain only bounded state, decision, approval kind, attempt,
and failure-code values. The redacted journal follows the same rule. Patch/diff text,
test output, prompts, model responses, credentials, principal IDs, repository aliases,
job IDs, branches, and host paths are excluded.

The service uses Phase 9F's pre-registered `unauthorized_operations_total` and
`approval_validation_failures_total` counter series through the existing metrics-sink
shape. A metrics failure emits a bounded degraded trace event and never weakens an
authorization or approval decision.

## Offline verification

The Phase 10 suite covers the complete fake flow, two interrupted model/mutation
recoveries, an interrupted commit, publisher timeout reconciliation, approval races,
stale bindings, patch/test/budget/repository changes, role and actor-type denial,
test retry, budget exhaustion, sandbox-policy rejection, rollback/quarantine,
publisher failure, checksum corruption, and telemetry redaction.

The existing opt-in Week 3 Docker E2E remains the live container gate for non-root,
network denial, writable-mount confinement, timeout cleanup, complete fake-model
Repair, checkpoint recovery, and local commit isolation. These are offline engineering
results only, not production capacity or real business evidence.
