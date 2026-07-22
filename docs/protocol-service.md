# HTTP, GitHub Webhook, and MCP service

Week 7 wraps the existing Review Agent in one protocol-neutral asynchronous job
service. FastAPI and the official MCP Python SDK are adapters over the same
`ReviewService`; they do not fork Finder/Verifier behavior or add repository
mutation authority.

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
  while streaming, before the service buffers more than 1 MiB. Worker
  concurrency is 1--8.
- Every production business table is organization-scoped. Repository access is checked for job,
  trace, Finding, feedback, and approval reads/writes; a cross-organization resource ID has the
  same not-found response as an unknown ID.
- Postgres is the production database target. SQLite remains a single-process local/test mode.
  Neither stores the submitted inline diff nor any HTTP/webhook/provider credential.
- Provider and command failures become stable error categories. Responses do
  not echo exception messages, host paths, command output, diffs, or keys.
- Pull-request diff stdout is spooled to a temporary file and stopped at the
  512 KiB/60-second bounds instead of being captured without a memory limit.
- Each job gets an exclusive canonical Week 6 trace file. Trace resources can
  only be addressed by a valid job ID; callers never provide a trace path.

The former static bearer token now exists only as an explicit loopback compatibility mode:
`CRAG_ALLOW_LOCAL_TOKEN=true` plus a sufficiently long `CRAG_SERVICE_TOKEN`. The service refuses
that mode on a non-loopback bind. It is not OAuth and must not be exposed to an untrusted network.
A remote deployment must terminate TLS and use database API credentials or an AuthBackend whose
deployment verifies OIDC/JWT signature, issuer, audience, expiry, and key rotation. Phase 9B does
not perform discovery or contact an identity provider.

`approve_patch` is intentionally absent. Existing Repair approval is one-use
and bound to an exact checkpoint, candidate, path set, and state snapshot. A
remote approval endpoint needs durable authenticated-principal and pending-
operation bindings; a generic boolean approval would weaken that invariant.

## Configuration

The service does not automatically load HTTP secrets from `.env`. Set these in
the process environment:

| Variable | Required | Meaning |
| --- | --- | --- |
| `CRAG_DATABASE_URL` | production | SQLAlchemy URL; use `postgresql+psycopg://...` in production |
| `CRAG_SERVICE_TOKEN` | local only | random local bearer token, at least 32 UTF-8 bytes |
| `CRAG_ALLOW_LOCAL_TOKEN` | no | explicit `true` opt-in for loopback static-token compatibility |
| `CRAG_AUTO_MIGRATE` | no | local/test SQLite empty-database convenience; requires local-token mode |
| `CRAG_WEBHOOK_SECRET` | webhook | GitHub webhook secret, at least 16 bytes |
| `CRAG_REPOSITORIES_JSON` | yes | JSON map from `owner/repo` to an absolute existing Git checkout |
| `CRAG_STATE_DIR` | no | private SQLite/trace directory; defaults to `~/.crag/service` |
| `CRAG_SERVICE_HOST` | no | bind address; defaults to `127.0.0.1` |
| `CRAG_SERVICE_PORT` | no | port; defaults to `8000` |
| `CRAG_SERVICE_WORKERS` | no | worker count `1..8`; defaults to `2` |
| `CRAG_ALLOWED_ORIGINS` | no | comma-separated exact MCP origins |
| `CRAG_ALLOWED_HOSTS` | no | comma-separated MCP hosts; `:*` permits any port |

Provider configuration remains the existing `LLM_PROVIDER`, provider key, and
optional `LLM_MODEL`. The review worker invokes `gh pr diff` for PR jobs, so
`gh` must be installed and authenticated for those registered checkouts. The
service image installs `gh` and `git`; inject a scoped `GH_TOKEN` at runtime
instead of mounting a host credential directory.

Exactly one `crag-service` or `crag-mcp` process may own a given
`CRAG_STATE_DIR`. The process holds an OS file lock for its lifetime and a
second process fails before the startup recovery sweep. Use a distinct state
directory for independent processes; do not point concurrent processes at the
same SQLite/trace directory.

## Database lifecycle

Alembic is the sole schema-version authority. Run migration as a separate deployment step before
starting any service worker:

```powershell
$env:CRAG_DATABASE_URL = "postgresql+psycopg://user:password@db/crag"
crag-db upgrade
crag-db check
crag-service
```

Workers perform a read-only comparison with the exact Alembic head before creating the executor
or MCP session. They never call `upgrade`, so concurrent workers cannot race to modify schema; an
unversioned, pending, inaccessible, or failed migration prevents service startup. Revision 0001
creates the Phase 9B schema. Revision 0002 imports a Week 7 `jobs`/`deliveries` SQLite database into
an isolated `local-legacy` organization while preserving job IDs, terminal results, and delivery
idempotency rows. Structural production rollback uses a pre-migration backup rather than a lossy
downgrade.

SQLite uses foreign keys, WAL, a busy timeout, and the existing state-directory process lock. It
is not a multi-host production database. A direct `JobStore(state_dir)` may initialize a temporary
local/test database while holding that lock; `create_review_service_from_env` defaults to revision
check only. Set `CRAG_AUTO_MIGRATE=true` only for explicit local mode.

PowerShell example (generate fresh values; never copy these placeholders):

```powershell
$env:CRAG_ALLOW_LOCAL_TOKEN = "true"
$env:CRAG_AUTO_MIGRATE = "true"
$env:CRAG_SERVICE_TOKEN = "replace-with-at-least-32-random-bytes"
$env:CRAG_WEBHOOK_SECRET = "replace-with-at-least-16-random-bytes"
$env:CRAG_REPOSITORIES_JSON = '{"owner/repo":"E:\\src\\repo"}'
$env:CRAG_STATE_DIR = "E:\\private\\crag-state"
$env:DEEPSEEK_API_KEY = "..."

.\.venv\Scripts\crag-service.exe
```

The OpenAPI document is available at `/openapi.json`; liveness is `GET
/healthz` and intentionally contains only the schema version and `ok`.

## REST job flow

Queue an inline diff:

```http
POST /v1/reviews/diff
Authorization: Bearer <token>
Content-Type: application/json

{"repository":"owner/repo","diff":"diff --git ..."}
```

Queue a PR:

```http
POST /v1/reviews/pr
Authorization: Bearer <token>
Content-Type: application/json

{"repository":"owner/repo","pull_request":"42"}
```

Both return `202` and a `crag.service/v1alpha1` record. Poll
`GET /v1/reviews/{review_id}` until `succeeded` or `failed`. A succeeded record
contains the unchanged Review Agent result; a failure contains only a stable
error code. `GET /v1/reviews/{review_id}/trace` returns redacted canonical
JSONL after the job is terminal.

States are monotonic: `queued -> running -> succeeded|failed`. Committing a
submission and placing it on the executor are serialized against shutdown; a
failed executor submission removes its queued job and delivery idempotency row.
On startup,
abandoned queued/running records become `failed/service_restarted`. This is a
single-process bounded executor, not a distributed durable queue.

## Identity and management REST API

All paths below require the same authenticated Principal and organization/repository predicates as
Review reads. `org_admin` may query/manage members, register/query repositories, and update mode,
budget, and policy; it may query only its own audit events. A user cannot modify its own role.

- `GET /v1/principal`
- `GET|POST /v1/organizations/{organization_id}/memberships`
- `PATCH /v1/organizations/{organization_id}/memberships/{membership_id}`
- `GET|POST /v1/organizations/{organization_id}/repositories`
- `PATCH /v1/organizations/{organization_id}/repositories/{repository_id}`
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

## GitHub webhook

Configure the GitHub webhook URL as `/webhooks/github`, content type JSON, and
the exact `CRAG_WEBHOOK_SECRET`. Subscribe only to pull requests. The service
accepts `opened`, `reopened`, `synchronize`, and `ready_for_review`; other
events are ignored and other pull-request actions are rejected without model
work. `ping` returns `pong`.

`X-GitHub-Delivery` is the idempotency key. Re-delivery returns the original
job with `duplicate: true` and does not queue a second review. The payload's
`repository.full_name` must match the operator registry.

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
- `review_pr(repository, pull_request)`
- `get_review_status(review_id)`

Resources:

- `crag://reviews/{review_id}`
- `crag://traces/{review_id}`

Prompt: `review_change(repository, change, focus="correctness")`.

The official SDK clients are used by the offline tests both in memory and over
the mounted Streamable HTTP ASGI path. They initialize a real MCP session, list
all capabilities, call each tool, read both resource templates, and retrieve
the prompt without network or model access.

## Container

`Dockerfile.service` packages the service separately from the existing CLI
image. It runs as a non-root user, writes only `/state`, and binds `0.0.0.0`
inside the container so an explicitly published port works. Mount only the
registered read-only checkouts and a private state volume; do not mount host
credentials or the Docker socket.

```bash
docker build -f Dockerfile.service -t code-review-agent-service .
docker run --rm code-review-agent-service --help
```

Mandatory Week 7 validation is offline and uses injected fake runners. It does
not send a webhook, contact GitHub, call a model, post a review, or run Docker.
