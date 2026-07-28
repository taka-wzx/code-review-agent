# Phase 11A: Synthetic Staging Deployment Validation v1

Status: **active — staging Python mirror remediation authorized for offline
implementation; deployment execution still requires a separate explicit confirmation**

Frozen baseline: `origin/master` at
`21344a2b72be8cb83361875b5cc8f2952e99ffbf`

Task branch: `codex/phase11a-synthetic-staging-v1`

## Claim boundary

Phase 11A is a synthetic-only staging engineering and deployment-validation phase. It
is not a Business Pilot, model-quality validation, production rollout, or evidence that
auth-004 succeeded. Every public phase record and generated synthetic receipt must keep:

```text
environment=synthetic_staging
synthetic_only=true
real_model_calls=false
real_repository_writes=false
business_claim_allowed=false
quality_claim_allowed=false
production_ready=false
```

The auth-004 permanent evidence boundary is unchanged: `selected=5`, `headline=5`,
`completed=0`, `failed=5`, and
`provider_or_pipeline_RuntimeError=5`; its root cause remains `unknown`. This phase
does not rerun, replace, alter the denominator of, or backfill auth-004. A synthetic
success never changes that evidence or either claim gate.

## Deployment authorization gate

The repository and Alibaba Cloud account owner authorized the zero-incremental-cost
preparation profile on 2026-07-28. This records the target and permitted preparation
design, but deliberately does not authorize any deployment-side mutation. Account IDs,
SSH private keys, secret values, and the operator's dynamic source IP are intentionally
kept out of the repository.

| Required deployment authority | Value |
| --- | --- |
| staging account/project | Owner-operated Alibaba Cloud ECS instance `i-bp12vpivp8pdpr0uq7uf`; no account identifier is recorded |
| region/namespace | `cn-hangzhou`; host namespace `/opt/crag-synthetic-staging` |
| staging hostname/URL | `http://127.0.0.1:8000` through an operator SSH tunnel only; no public hostname |
| container registry | No application-image registry; application-image push/pull is prohibited, the frozen source is built on the staging host, and the local image ID is recorded; public base-image pulls require the second confirmation |
| build dependency mirrors | Docker builds default to `https://deb.debian.org` and `https://pypi.org/simple`; `cn-hangzhou` staging may explicitly set `CRAG_DEBIAN_MIRROR=https://mirrors.aliyun.com` and `CRAG_PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`; Dockerfiles reject every other value |
| database instance/volume | Compose `postgres:16-alpine` on the same ECS host with the `postgres_data` named volume; port 5432 is not published |
| deploy API / push staging image / run schema migration / backup-restore | Preparation only is authorized; source transfer, base-image pull/build, migration, service start, and local backup/restore each remain blocked until the next explicit deployment confirmation |
| DNS/TLS change authority | No DNS or TLS change is authorized; loopback publication plus SSH tunneling is mandatory |
| deployment window and cost ceiling | One operator window of at most 60 minutes starts only after the next explicit confirmation; incremental spend ceiling is CNY 0 and any billable add-on aborts the operation |
| rollback/incident owner | The repository and Alibaba Cloud account owner; Codex may act only within the confirmed window and must stop on owner request or gate failure |
| secret-manager or `*_FILE` injection method | Root-owned host files below `/opt/crag-synthetic-staging/secrets`, mode `0600`, mounted through the existing Compose `*_FILE` declarations; values never enter Git, logs, receipts, or chat |

External telemetry, real model/API calls, paid calls, product-side GitHub API or writes,
and real repository/user data are prohibited. Local commits, task-branch pushes, and a
Draft PR are authorized; agent push/merge to `master` and auto-merge are prohibited.

The target profile is now recorded, but the operation-authority row intentionally keeps
the execution gate closed. Until the owner separately confirms actual deployment, this
task may prepare documentation and validate offline synthetic controls only. It must not
connect to or mutate the staging host, transfer source, pull or build images, run a
migration, start services, make DNS/TLS changes, conduct backup/restore against staging,
claim deployment success, or move a PR to Ready.

Two bounded deployment windows on 2026-07-28 stopped before image creation because
`deb.debian.org` package downloads did not finish before their hard timeouts. A third
window used the allowlisted Alibaba Debian mirror and reached the Python dependency
install, but downloads from `pypi.org` exceeded the build hard timeout. Every attempt
retained zero Compose containers, volumes, and networks and ran no migration, service,
smoke, or backup/restore operation. The owner subsequently authorized this offline
Python mirror-parameter remediation only. Its executable-code commit, source archive,
authorization record SHA, rendered Compose SHA, and local image ID must all be frozen
again before another deployment window; evidence from any stopped attempt cannot be
reused as deployment-success evidence.

## Goal and architecture

Phase 11A upgrades the Phase 10 file checkpoint prototype into a PostgreSQL-backed,
synthetic Repair control plane. The durable state machine remains additive to Review:

```text
queued_plan -> planning -> awaiting_write_approval
awaiting_write_approval -> queued_execution -> executing
executing -> awaiting_write_approval | awaiting_draft_pr_approval
awaiting_draft_pr_approval -> queued_publish -> publishing -> draft_published
```

`declined`, `failed`, and `quarantined` are terminal. Only planning, executing, and
publishing states may hold a repair-worker lease; approval waits hold none. The database
is the authoritative store for repair jobs, immutable checkpoint versions, budget and
reservations, approval bindings and consumption, operation intents, idempotent receipts,
outbox records, failure codes, leases, and worker heartbeat/fencing metadata. Each
transition uses a transaction and version/CAS predicate; stale, replayed, mismatched,
expired, or concurrent approvals fail closed.

The migration is explicit: the `migrate` Compose job invokes `crag-db upgrade`. API and
worker only check the exact Alembic head and must never execute DDL at startup or during
a request. SQLite and in-memory stores are test compatibility tools only and cannot be
selected for a synthetic-staging runtime.

The authenticated API adds only repair resources:

- create and read a repair job;
- read and decide an exact WRITE approval view;
- read and decide an exact DRAFT_PR approval view;
- read a redacted repair receipt/audit;
- health, readiness, and bounded metrics integration.

Only a same-organization `maintainer` or `org_admin` can create or decide a repair.
Viewer, reviewer, webhook, model, Finding, and unauthenticated identities are denied;
a Finding remains evidence and never grants write authority. Tenant predicates make
cross-organization object IDs indistinguishable from missing resources. Complete
private diffs are never emitted to traces, metrics, journals, or receipts and are
returned only within an authorized approval view.

The worker accepts only offline planner/executor adapters and
`FakeDraftPrPublisher` or `DryRunDraftPrPublisher`. Any real provider, GitHub writer,
merge API, protected/default branch push, real repository adapter, or non-synthetic
runtime configuration is rejected during startup. Sandbox receipts require Docker,
non-root execution, `network=none`, a policy-fixed command list, bounded timeout and
output, and a scoped worktree. Every retry creates a new plan and new WRITE approval.
An unreceipted mutation or publication intent is quarantined instead of replayed.

Compose keeps PostgreSQL, repair migration, API, and worker separate; default runtime
egress is denied. Images are verified by digest only after a separately authorized
deployment freeze. SBOM, dependency/image scan, TLS, secret-manager injection,
backup/restore, worker drain, rollback, and quarantine artifacts are specified as
offline runbooks and tests here; they are not claimed as executed staging operations.
Telemetry accepts only fixed enums, booleans, non-negative counts, SHA-256 hashes,
durations, and low-cardinality labels. It rejects prompt/diff/patch/body/tool/stdout/
stderr/exception/credential/identity/path content and high-cardinality identifiers.

## Single Writer declaration

Codex owns exactly the following paths for this task. Every other path is read-only;
`eval/**` and `eval/holdout/**` must not be enumerated, read, run, copied, or modified.

- `docs/plans/phase11a-synthetic-staging-v1.md`;
- `docs/phase11a-synthetic-staging-v1.md`;
- `migrations/versions/0007_phase11a_synthetic_repair.py`;
- `src/code_review_agent/database.py`;
- `src/code_review_agent/identity.py`;
- `src/code_review_agent/production_metrics.py`;
- `src/code_review_agent/repair_service.py`;
- `src/code_review_agent/repair_publish.py`;
- `src/code_review_agent/service.py`;
- `src/code_review_agent/service_core.py`;
- `src/code_review_agent/service_queue.py`;
- `src/code_review_agent/worker.py`;
- `compose.service.yml`, `Dockerfile.service`, and `Dockerfile.repair`;
- `scripts/phase11a_synthetic_staging_test.py`;
- `tests/test_phase11a_synthetic_staging.py`.

Any need to touch a path outside this list requires a contract revision before the edit.
Public API additions above are expressly authorized by this Phase 11A request; existing
interfaces and dependency/packaging configuration remain frozen.

## Offline acceptance and validation

The synthetic suite must cover 30 jobs, three forced crash/restart boundaries (after
plan receipt, mutation intent, and publish intent), post-restart duplicate side-effect
counts of zero, one winner of a concurrent double approval, stale/replay/mismatch
rejection, zero lease count while waiting approval, durable budget, quarantine of an
unresolved intent, redaction, fake-only provider/publisher startup gates, and a
PostgreSQL transaction/CAS/outbox/recovery path. It must also verify Docker non-root and
`network=none` receipts plus backup/restore consistency using test-only local fixtures.

All commands are offline, use fakes/synthetic data only, and must not invoke
`scripts/verify.py` with any eval-assets mode:

```powershell
python -m unittest -v tests.test_phase11a_synthetic_staging
python scripts/phase11a_synthetic_staging_test.py
python -m unittest discover -s tests
python -m ruff check .
python -m mypy src/code_review_agent
python scripts/verify.py
python -m pip check
git diff --check
git diff --name-only 21344a2b72be8cb83361875b5cc8f2952e99ffbf...HEAD
```

Docker/Postgres-dependent tests may report an explicit local-environment skip only when
the executable/runtime is unavailable; such a skip is not staging validation.

## Delivery control

Before delivery, inspect every changed file and audit it for unauthorized scope and
sensitive data. A stable local task-branch commit, push of this task branch only, and a
Draft PR are permitted after offline validation. Because deployment execution still
requires a second confirmation, no staging host mutation, deployment receipt, image ID,
authorization SHA, deployment configuration SHA, Ready transition, merge, or
completed-phase claim may be made in this task state.

## Change control

This contract is frozen after creation. A new dependency, additional writable path,
real external call, secret channel, migration/deployment authority, public API change
beyond the listed routes, or state-semantic change requires an explicit contract
revision before implementation.
