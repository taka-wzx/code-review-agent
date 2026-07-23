# HTTP, GitHub Webhook, and MCP service

FastAPI and the official MCP Python SDK are adapters over the same durable
`ReviewService`; they do not fork Finder/Verifier behavior or add repository
mutation authority. In the Phase 9C production path, API processes only
authenticate, authorize, apply quota/idempotency rules, persist submissions,
and serve reads. Independent `crag-worker` processes claim work through
Postgres leases. Only explicit local/test construction may embed a fake worker.

## Security model

- HTTP `/v1/*` and `/mcp` require an `AuthBackend`-resolved Principal. The default remote
  backend validates short-lived database API credentials; tests inject deterministic fake
  principals, and externally verified OIDC/JWT claims can be mapped by
  `VerifiedOIDCJWTAuthBackend` without coupling authorization to one identity provider.
- API credential plaintext is returned once at creation. SQLite/Postgres stores only a SHA-256
  digest of the at-least-256-bit random token, a non-secret prefix, expiry, last-use time, and
  revocation time. Every request reads current credential state, so revocation takes effect on
  the next authentication attempt.
- GitHub webhook requests use `X-Hub-Signature-256` over the exact request body,
  HMAC-SHA256, and constant-time comparison before JSON parsing.
- Requests name only an `owner/repo` alias registered by the operator. A caller
  cannot supply a filesystem path, model, command, output path, or credential.
- MCP Streamable HTTP validates both `Host` and optional `Origin` headers to
  prevent DNS rebinding. The process binds to `127.0.0.1` by default.
- Inline diffs are limited to 512 KiB, webhook bodies to 1 MiB, persisted
  results to 2 MiB, and trace responses to 4 MiB. Webhook bodies are rejected
  while streaming, before the service buffers more than 1 MiB. Per-process
  worker concurrency is bounded to 1--8.
- Every production business table is organization-scoped. Repository access is checked for job,
  trace, Finding, feedback, and approval reads/writes; a cross-organization resource ID has the
  same not-found response as an unknown ID.
- Postgres is the only production multi-worker database. SQLite remains a
  local/single-machine compatibility mode and does not prove `SKIP LOCKED` or
  cross-process lease behavior. Neither database stores the inline diff or any
  HTTP/webhook/provider credential.
- Provider and command failures become stable error categories. Responses do
  not echo exception messages, host paths, command output, diffs, or keys.
- Pull-request diff stdout is spooled to a temporary file and stopped at the
  512 KiB/60-second bounds instead of being captured without a memory limit.
- Inline payload and canonical trace files live under the private
  `CRAG_JOB_DATA_DIR`/`CRAG_TRACE_DIR` artifact boundary. The database stores
  only bounded opaque relative keys and hashes; callers never provide a path.
  Temp write, fsync, atomic replace/O_EXCL, fingerprint verification, and
  fencing prevent a stale attempt from publishing a trace pointer.
- Worker maintenance performs bounded, cursor-based best-effort orphan cleanup
  from database lineage. Strictly named abandoned payload temp files are eligible
  only after a safety age; active/recent temp files are preserved. Cleanup failure
  emits only a stable path-free warning and never rolls back a committed result;
  no retention or cleanup SLO is claimed.

The former static bearer token now exists only as an explicit loopback compatibility mode:
`CRAG_ALLOW_LOCAL_TOKEN=true` plus a sufficiently long token supplied by
`CRAG_SERVICE_TOKEN_FILE`. The service refuses
that mode on a non-loopback bind. It is not OAuth and must not be exposed to an untrusted network.
A remote deployment must terminate TLS and use database API credentials or an AuthBackend whose
deployment verifies OIDC/JWT signature, issuer, audience, expiry, and key rotation. Phase 9C does
not perform discovery or contact an identity provider.

`approve_patch` is intentionally absent. Existing Repair approval is one-use
and bound to an exact checkpoint, candidate, path set, and state snapshot. A
remote approval endpoint needs durable authenticated-principal and pending-
operation bindings; a generic boolean approval would weaken that invariant.

## Configuration

The service does not load HTTP secrets from `.env`. Container deployments pass
only `_FILE` paths backed by runtime secrets; secret values must not appear in
Compose, image layers, argv, logs, health responses, or traces.

| Variable | Required | Meaning |
| --- | --- | --- |
| `CRAG_DATABASE_URL` | production | password-free SQLAlchemy URL, normally `postgresql+psycopg://user@host/db` |
| `CRAG_DATABASE_URL_FILE` | alternative | file containing the complete URL when the deployment secret manager supplies it as one value |
| `CRAG_DATABASE_PASSWORD_FILE` | production | file containing only the database password; injected into the password-free URL in memory |
| `CRAG_WEBHOOK_SECRET_FILE` | API | file containing the Webhook HMAC secret, at least 16 UTF-8 bytes |
| `CRAG_SERVICE_TOKEN_FILE` | local only | file containing the loopback compatibility token, at least 32 UTF-8 bytes |
| `CRAG_ALLOW_LOCAL_TOKEN` | no | explicit `true` opt-in for loopback static-token compatibility |
| `CRAG_REPOSITORIES_JSON` | yes | JSON map from `owner/repo` to absolute **container** checkout paths |
| `CRAG_STATE_DIR` | no | local state root; defaults to `~/.crag/service` |
| `CRAG_JOB_DATA_DIR` | no | private durable inline-payload directory |
| `CRAG_TRACE_DIR` | no | private per-attempt/final trace directory |
| `CRAG_SERVICE_HOST` / `CRAG_SERVICE_PORT` | no | API bind; defaults to `127.0.0.1:8000` outside the image |
| `CRAG_ALLOWED_ORIGINS` / `CRAG_ALLOWED_HOSTS` | no | exact MCP HTTP Origin/Host allowlists |
| `CRAG_WORKER_RUNNER` | worker | `real` by default; `fake` is accepted only for offline/container validation |
| `CRAG_WORKER_CONCURRENCY` | no | local worker concurrency `1..8`, default `2` |
| `CRAG_JOB_LEASE_SECONDS` | no | lease duration `1..3600`, default `60` |
| `CRAG_JOB_HEARTBEAT_SECONDS` | no | heartbeat `0.1..600`, default `10`, and strictly below half the lease |
| `CRAG_WORKER_POLL_SECONDS` | no | bounded claim poll interval, default `1` |
| `CRAG_WORKER_STALE_SECONDS` | no | readiness heartbeat freshness, default `30` |
| `CRAG_SHUTDOWN_GRACE_SECONDS` | no | worker drain bound, default `30` |
| `CRAG_CONTAINER_STOP_GRACE_PERIOD` | no | Compose stop timeout, default `35s`; must exceed the worker drain bound |
| `CRAG_RECEIVED_TIMEOUT_SECONDS` | no | age before bounded recovery reconciles an incomplete `received` job, default `60` |

Provider configuration remains `LLM_PROVIDER` plus optional `LLM_MODEL`.
Workers require `DEEPSEEK_API_KEY_FILE`, `GLM_API_KEY_FILE`, or
`ZHIPUAI_API_KEY_FILE` for the selected real runner; durable workers reject
direct provider-key environment values so unrelated child processes cannot
inherit them. The fake runner never reads a provider key or creates a provider
client. Real GitHub and provider calls are outside Phase 9C validation, and the
Compose file never mounts a host credential directory or Docker socket.

Postgres API/worker processes may share one artifact volume and never acquire
the former `.service.lock`. This proves only same-host Compose coordination;
the volume is not a cross-host object store. SQLite remains a compatibility
path and must not be used to claim production concurrency or recovery.

## Database lifecycle

Alembic is the sole schema-version authority. Run migration as a separate
deployment step before starting the API or any worker:

```powershell
$env:CRAG_DATABASE_URL = "postgresql+psycopg://user@db/crag"
$env:CRAG_DATABASE_PASSWORD_FILE = "<private-password-file>"
crag-db upgrade
crag-db check
crag-service
crag-worker
```

API and worker startup perform only a read-only exact-head check. They never
call `upgrade`, so concurrent processes cannot race to modify schema; an
unversioned, pending, inaccessible, or failed migration prevents startup.
Revision 0001 creates the Phase 9B tenant schema, 0002 imports isolated Week 7
legacy rows, and 0003 adds durable state, leases, quotas, worker heartbeats,
attempt usage, and the new state constraint. Historical `succeeded` maps to
`awaiting_approval`; recoverable `pull_request` work in `running` maps back to
`queued` during a stopped deployment. Because Phase 9B did not persist inline
diff payloads, its nonterminal inline jobs fail closed as
`legacy_payload_unavailable`. Revision 0003 seeds default organization/repository quota rows for
existing registrations; later repository registration creates its defaults in
the same transaction, so quota GETs do not depend on a first submission.

Structural rollback uses a pre-migration database backup plus the matching
artifact-volume backup, not a lossy downgrade. Stop old workers before 0003;
never reconnect a Phase 9B worker after Phase 9C states have been written.
The API rejects `CRAG_AUTO_MIGRATE`, including in loopback local mode. SQLite
development databases must also be migrated explicitly with `crag-db upgrade`.
SQLite no longer uses the state-directory process lock; its write-transaction
behavior is not a substitute for Postgres concurrency tests.

The OpenAPI document is available at `/openapi.json`. `GET /healthz` is pure
process liveness and intentionally contains only schema version plus `ok`.
`GET /readyz` returns 200 only when the schema is at head, `SELECT 1` succeeds,
the API accepts submissions, and at least one worker heartbeat is fresh. A
worker container uses `crag-worker --check` for database/self-heartbeat health.

## REST job flow

Queue an inline diff:

```http
POST /v1/reviews/diff
Authorization: Bearer <token>
Idempotency-Key: <bounded-client-key>
Content-Type: application/json

{"repository":"owner/repo","diff":"diff --git ..."}
```

Queue a PR:

```http
POST /v1/reviews/pr
Authorization: Bearer <token>
Idempotency-Key: <bounded-client-key>
Content-Type: application/json

{"repository":"owner/repo","pull_request":"42","head_sha":"<40-hex-head>"}
```

Both return `202` only after the durable payload/reference is ready and expose a
`crag.service/v1alpha1` record. A repeated idempotency key with the same request
fingerprint returns the original job and `duplicate=true`; reuse with different
content returns stable 409 `idempotency_conflict`. A manual PR request without
`head_sha` requires an explicit idempotency key and does not claim an immutable
GitHub snapshot.

Poll `GET /v1/reviews/{review_id}` through:

```text
received -> queued -> leased -> running -> awaiting_approval
```

`received` is internal and never acknowledged with 202. A successful Review
first passes bounded result-schema validation, then persists its
result/Findings/usage and enters `awaiting_approval`; Phase 9C does
not advance it to approved/published/declined. Permanent authentication,
authorization, schema/policy, external-command, internal, or budget errors enter
`failed`. Transient network/provider 5xx/rate-limit errors retry at most three
total attempts; exhausted retries enter `dead_letter`, with rate limits honoring
bounded `Retry-After`/reset timing against the database clock. Success, retry,
lease-expiry recovery, and terminal transitions append a stable audit event in
the same transaction as fencing, usage/quota settlement, and the state change.

Claim is atomic Postgres `FOR UPDATE SKIP LOCKED`. Each attempt receives a new
lease owner/token; heartbeat extends only a live matching lease, and every
start/retry/terminal write is fenced by owner + token + expiry. Execution is
at-least-once: a dead worker's lease can be reclaimed, while its stale token
cannot publish results. This guarantees one visible result, not exactly-once
provider calls.

Organization and repository quotas independently cap queued jobs, concurrent
leases, fixed-window submissions, monthly model calls, and calls per job.
Queue/rate/budget admission failures return stable 429 codes `queue_full`,
`submission_rate_limited`, or `model_budget_exhausted`; idempotent replay does
not consume quota again. `GET /v1/reviews/{review_id}/trace` returns the winning
redacted canonical JSONL after a terminal or `awaiting_approval` result.

## Identity and management REST API

All paths below require the same authenticated Principal and organization/repository predicates as
Review reads. `org_admin` may query/manage members, register/query repositories, and update mode,
budget, and policy; it may query only its own audit events. A user cannot modify its own role.

- `GET /v1/principal`
- `GET|POST /v1/organizations/{organization_id}/memberships`
- `PATCH /v1/organizations/{organization_id}/memberships/{membership_id}`
- `GET|POST /v1/organizations/{organization_id}/repositories`
- `PATCH /v1/organizations/{organization_id}/repositories/{repository_id}`
- `GET|PATCH /v1/organizations/{organization_id}/service-quota`
- `GET|PATCH /v1/organizations/{organization_id}/repositories/{repository_id}/service-quota`
- `POST /v1/credentials` and `DELETE /v1/credentials/{credential_id}`
- `GET /v1/audit-events?limit=...`
- `GET /v1/reviews/{review_id}/findings` and `GET /v1/findings/{finding_id}`
- `POST /v1/findings/{finding_id}/feedback`
- `POST /v1/findings/{finding_id}/decisions`

Role boundaries are explicit: viewer reads; reviewer submits Review and Finding feedback;
maintainer also approves/rejects exact Finding versions; org_admin manages the organization but
does not substitute for a maintainer approval. Audit events contain principal, organization,
action, resource type/ID, allow/deny/error decision, policy version, UTC time, and request/run
correlation ID. They never contain a token, Cookie, Authorization header, diff, prompt, exception,
or host path.

Phase 9B Finding decision records remain version-bound authorization evidence;
they do not advance the Phase 9C Review job beyond `awaiting_approval` and do
not publish anything. Phase 9D owns the complete approval/publish state change.

## GitHub webhook

Configure the GitHub webhook URL as `/webhooks/github`, content type JSON, and
the exact secret supplied through `CRAG_WEBHOOK_SECRET_FILE`. Subscribe only to pull requests. The service
accepts `opened`, `reopened`, `synchronize`, and `ready_for_review`; other
events are ignored and other pull-request actions are rejected without model
work. `ping` returns `pong`.

`X-GitHub-Delivery` is the idempotency key. Re-delivery returns the original
job with `duplicate: true` and does not queue a second review. The payload's
`repository.full_name` must match the operator registry and `pull_request.head.sha`
is the immutable source identity. Different delivery IDs for the same
organization/repository/PR/head/policy submission key also converge on one
logical job. The handler persists and acknowledges; it never waits for Review
execution.

Webhook HMAC is a system delivery identity, not a user authentication mechanism. Webhook-created
jobs are attributed to the `github-webhook` system actor and can never call the user Finding
decision endpoint or create an approval, even if the JSON body includes spoofed approval fields.

See GitHub's authoritative verification procedure:
<https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries>.

## MCP

The stdio server is the safest local integration and requires explicit local principal mode:

```powershell
.\.venv\Scripts\crag-mcp.exe
```

The same server is mounted as MCP Streamable HTTP at `/mcp/` by
`crag-service`. HTTP clients must send the bearer token, an allowed `Host`, and
an allowed `Origin` when Origin is present. Streamable HTTP is the current MCP
transport; legacy HTTP+SSE is not exposed.

Tools:

- `review_diff(repository, diff)`
- `review_pr(repository, pull_request, head_sha=None, idempotency_key=None)`
- `get_review_status(review_id)`

Resources:

- `crag://reviews/{review_id}`
- `crag://traces/{review_id}`

Prompt: `review_change(repository, change, focus="correctness")`.

For compatibility, the historical two-argument `review_pr` form remains
accepted. When neither optional field is supplied, the MCP adapter derives a
stable organization-scoped compatibility idempotency key whose raw value is
never persisted or traced; this path does not claim an immutable PR snapshot.
Supplying `head_sha` enables the worker's fail-closed pre/post head check.

The official SDK clients are used by the offline tests both in memory and over
the mounted Streamable HTTP ASGI path. They initialize a real MCP session, list
all capabilities, call each tool, read both resource templates, and retrieve
the prompt without network or model access.

## Container

`Dockerfile.service` is one immutable non-root image. Its default role is
`crag-service`; Compose overrides the entrypoint to `crag-db` for the explicit
migration job and `crag-worker` for workers. The API is published only on host
loopback by default. Postgres has no host port in the Compose deployment.

`compose.service.yml` requires paths to four runtime secret files, a private
registered-checkout root, and container-path repository mappings. The provider
file may be a noncredential fixture only when `CRAG_WORKER_RUNNER=fake`; the
fake runner does not read it. Never put a real value in Compose or a committed
`.env` file.

```powershell
$env:CRAG_POSTGRES_PASSWORD_FILE = "<private-path>\postgres_password"
$env:CRAG_WEBHOOK_SECRET_FILE = "<private-path>\webhook_secret"
$env:CRAG_SERVICE_TOKEN_FILE = "<private-path>\local_service_token"
$env:CRAG_PROVIDER_API_KEY_FILE = "<private-path>\provider_api_key"
$env:CRAG_REPOSITORY_ROOT = "<private-registered-checkout-root>"
$env:CRAG_REPOSITORIES_JSON = '{"owner/repo":"/repositories/repo"}'
$env:CRAG_BUILD_CONTEXT = "<filtered-build-context>"

# Populate the filtered context without traversing forbidden frozen assets.
python scripts\phase9c_container_test.py --prepare-context $env:CRAG_BUILD_CONTEXT

# 1. Database readiness does not migrate schema.
docker compose -f compose.service.yml up -d postgres

# 2. Explicit, one-shot migration. A failure stops the rollout.
docker compose -f compose.service.yml --profile migration run --rm migrate

# 3. Start one API and two independently leasing workers.
docker compose -f compose.service.yml up -d --scale worker=2 api worker

# Process-only liveness and traffic readiness are intentionally separate.
Invoke-WebRequest http://127.0.0.1:8000/healthz
Invoke-WebRequest http://127.0.0.1:8000/readyz
```

API and worker share only the named `service_artifacts` volume and the read-only
registered checkout mount. The volume contains bounded job artifacts/traces,
not credentials. Neither role mounts a host credential directory, `.env`, or
the Docker socket. Both use a read-only root, dropped capabilities,
`no-new-privileges`, a bounded tmpfs, SIGTERM, and a finite stop grace.
The image starts through a minimal root-only bootstrap because Compose secret
long syntax cannot portably set ownership across runtimes. The bootstrap reads
only the mounted secret files, copies the allow-listed values into the `/tmp`
tmpfs with mode `0600`, and receives only `CHOWN`, `DAC_READ_SEARCH`, `SETGID`,
and `SETUID` during bootstrap. It then execs the API, worker, or migration
command with `setpriv` as UID/GID `1000:1000`. It drops the capability bounding,
inheritable, and ambient sets before the application starts. No service code
runs as root, and secret contents never enter image layers, Compose output,
argv, logs, or traces.
The Compose stop grace is a separate outer deadline and must remain longer
than `CRAG_SHUTDOWN_GRACE_SECONDS`; using the same deadline lets Docker send
SIGKILL before the worker can persist its final `stopped` heartbeat and exit.
The Compose build context is parameterized by `CRAG_BUILD_CONTEXT`; CI and the
container harness always use the script-generated filtered directory rather
than sending the repository root to Docker. The filtered context includes
`requirements.lock`; the image installs those pinned versions before installing
the project itself with `--no-deps`.

The image itself has no universal healthcheck because it serves three roles.
Compose checks API `/healthz`; deployment traffic uses `/readyz`; worker health
runs `crag-worker --check`. API/worker depend only on Postgres health, never on
automatic migration, so starting them against a pending schema fails closed.

`CRAG_ALLOW_LOCAL_TOKEN` defaults to `false`. The container acceptance script
may enable it with a generated temporary token to bootstrap an empty fake-run
database. That compatibility mode must also set `CRAG_SERVICE_HOST=127.0.0.1`
and issue requests from inside the API container; the service intentionally
refuses a local token on `0.0.0.0` or any externally reachable bind.

The CI container gate creates temporary noncredential secret files, runs the
explicit migration, starts API plus at least two fake workers, and exercises
Postgres claim/recovery without GitHub, OAuth, or a model. This is a same-host
deployment foundation, not a cloud rollout, multi-host artifact guarantee, or
production capacity result.
