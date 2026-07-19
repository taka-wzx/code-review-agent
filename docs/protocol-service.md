# HTTP, GitHub Webhook, and MCP service

Week 7 wraps the existing Review Agent in one protocol-neutral asynchronous job
service. FastAPI and the official MCP Python SDK are adapters over the same
`ReviewService`; they do not fork Finder/Verifier behavior or add repository
mutation authority.

## Security model

- HTTP `/v1/*` and `/mcp` require `Authorization: Bearer ...`.
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
- SQLite stores job metadata, the completed review, and a diff hash/byte count;
  it does not persist the submitted diff or any HTTP/webhook/provider secret.
- Provider and command failures become stable error categories. Responses do
  not echo exception messages, host paths, command output, diffs, or keys.
- Pull-request diff stdout is spooled to a temporary file and stopped at the
  512 KiB/60-second bounds instead of being captured without a memory limit.
- Each job gets an exclusive canonical Week 6 trace file. Trace resources can
  only be addressed by a valid job ID; callers never provide a trace path.

The static bearer token is suitable for an operator-controlled local service.
It is not OAuth. Do not expose the process directly to an untrusted network.
A remote deployment must terminate TLS and enforce OAuth 2.1/resource-server
policy in a gateway (or wait for a separately reviewed native OAuth phase).

`approve_patch` is intentionally absent. Existing Repair approval is one-use
and bound to an exact checkpoint, candidate, path set, and state snapshot. A
remote approval endpoint needs durable authenticated-principal and pending-
operation bindings; a generic boolean approval would weaken that invariant.

## Configuration

The service does not automatically load HTTP secrets from `.env`. Set these in
the process environment:

| Variable | Required | Meaning |
| --- | --- | --- |
| `CRAG_SERVICE_TOKEN` | HTTP | random bearer token, at least 32 UTF-8 bytes |
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

PowerShell example (generate fresh values; never copy these placeholders):

```powershell
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

## GitHub webhook

Configure the GitHub webhook URL as `/webhooks/github`, content type JSON, and
the exact `CRAG_WEBHOOK_SECRET`. Subscribe only to pull requests. The service
accepts `opened`, `reopened`, `synchronize`, and `ready_for_review`; other
events are ignored and other pull-request actions are rejected without model
work. `ping` returns `pong`.

`X-GitHub-Delivery` is the idempotency key. Re-delivery returns the original
job with `duplicate: true` and does not queue a second review. The payload's
`repository.full_name` must match the operator registry.

See GitHub's authoritative verification procedure:
<https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries>.

## MCP

The stdio server is the safest local integration:

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
