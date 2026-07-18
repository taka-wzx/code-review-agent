# Week 7: Standard Protocols and Service Delivery

## Goal

Deliver a secure, offline-testable FastAPI service and Model Context Protocol
(MCP) adapter around the existing Review Agent without changing Finder,
Verifier, Repair, evaluation, or security-corpus semantics.

The Week 7 outcome is an authenticated asynchronous review API, a verified
GitHub pull-request webhook ingress, and an MCP server exposing the same review
job model through standard tools, resources, and prompts. A2A is deliberately
deferred until the single-Agent HTTP and MCP surfaces have operational evidence.

## Base and delivery

- Base branch: `master`
- Base commit: `2ae3bf242bfaac2a51940c1a4a0e5a76d1246cb3`
- Codex branch: `codex/week7-protocol-service`
- Codex worktree: `E:\shiyan\code_review_agent\traces\worktrees\codex-week7`
- Claude review branch: `claude/week7-protocol-service-review`
- Integration branch: `integration/week7-protocol-service`

The user authorized implementation, a local Claude review using the `fable5`
model, remediation, task/integration commits, push, and CI tracking. Direct
development on `master` remains prohibited. The final integration branch may be
fast-forwarded to `master` only after all local gates and Claude review pass.

## Frozen standards and dependency policy

- FastAPI remains on the Python 3.10-compatible `0.x`/`<1` line.
- The official MCP Python SDK remains on stable `1.x` (`mcp>=1.28,<2`). MCP 2
  prereleases are not selected during this week.
- MCP uses the 2025-11-25 protocol and Streamable HTTP or stdio transports.
- Streamable HTTP validates Origin, binds to loopback by default, and requires
  HTTP authentication. Remote production exposure needs an OAuth 2.1-capable
  gateway or a future native OAuth phase; the static service token is not
  presented as an OAuth implementation.
- GitHub webhook delivery authenticity uses the unmodified request body,
  `X-Hub-Signature-256`, HMAC-SHA256, and constant-time comparison.
- No new dependency may load `.env` implicitly or serialize credentials.

Primary references frozen on 2026-07-19:

- <https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- <https://github.com/modelcontextprotocol/python-sdk>
- <https://fastapi.tiangolo.com/advanced/events/>
- <https://fastapi.tiangolo.com/tutorial/background-tasks/>
- <https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries>

## Frozen service interfaces

### Configuration

- `CRAG_SERVICE_TOKEN`: bearer token required by `/v1/*` and HTTP `/mcp`.
- `CRAG_WEBHOOK_SECRET`: secret required by `/webhooks/github`.
- `CRAG_REPOSITORIES_JSON`: JSON object mapping exact `owner/repo` aliases to
  absolute, existing local checkout paths. Requests never provide raw paths.
- `CRAG_STATE_DIR`: state/trace directory; defaults to a user-local directory,
  never the reviewed repository.
- `CRAG_SERVICE_HOST` / `CRAG_SERVICE_PORT`: defaults `127.0.0.1:8000`.
- `CRAG_SERVICE_WORKERS`: bounded `1..8`, default `2`.
- `CRAG_ALLOWED_ORIGINS`: exact HTTP MCP Origin allowlist; localhost origins are
  the secure local default.

### HTTP endpoints

| Method/path | Authentication | Contract |
| --- | --- | --- |
| `GET /healthz` | none | liveness plus schema version only; no secret/config detail |
| `POST /v1/reviews/diff` | bearer | queue a bounded unified diff against a registered repository |
| `POST /v1/reviews/pr` | bearer | queue an exact GitHub PR URL or number for a registered repository |
| `GET /v1/reviews/{job_id}` | bearer | return immutable source metadata, state, timestamps, sanitized error, and completed review |
| `GET /v1/reviews/{job_id}/trace` | bearer | return the canonical trace records for that job, never an arbitrary path |
| `POST /webhooks/github` | GitHub HMAC | accept selected `pull_request` actions and queue the PR idempotently by delivery ID |
| `/mcp` | bearer + Origin | official MCP Streamable HTTP endpoint |

The API schema is `crag.service/v1alpha1`. Review states are `queued`,
`running`, `succeeded`, and `failed`. Submission returns HTTP 202. Replaying a
GitHub delivery returns the original job identity without a second review.
Unknown repositories/actions/events are ignored or rejected without model work.

Ingress limits are enforced before JSON parsing or job creation: 1 MiB webhook
body, 512 KiB diff, 128-character repository alias, 256-character PR reference,
and bounded headers. User-facing errors never echo a diff, credential, provider
exception, host path, or command output.

### MCP surface

Tools:

- `review_diff(repository, diff)` -> asynchronous job identity;
- `review_pr(repository, pull_request)` -> asynchronous job identity;
- `get_review_status(review_id)` -> the same state/result projection as HTTP.

Resources:

- `crag://reviews/{review_id}` -> review state/result JSON;
- `crag://traces/{review_id}` -> canonical trace JSONL after authorization by
  the transport boundary.

Prompt:

- `review_change(repository, change, focus="correctness")` -> a reusable
  client-side instruction for selecting `review_diff` or `review_pr` and polling
  status without granting extra authority.

`approve_patch` is not exposed in v1. The current Repair approval is an
in-process, one-use human binding to an exact checkpoint/candidate. A remote
approval tool without durable authenticated principal and pending-operation
binding would weaken that invariant. It requires a separately frozen Repair
service contract. `get_trace` is represented as a Resource, matching MCP's
resource model instead of duplicating it as a mutation-shaped tool.

## Execution and persistence semantics

- A bounded in-process executor performs reviews after the 202 response.
- SQLite persists job identity/state/result and webhook delivery idempotency.
  Raw bearer/webhook/provider credentials are never persisted. Diff text is
  held only for the live queued job; SQLite stores its SHA-256 and byte count.
- On startup, abandoned `queued`/`running` jobs become a sanitized `failed`
  terminal state. Week 7 does not claim a distributed or durable work queue.
- Each job gets a new canonical trace file created with exclusive semantics.
- The runner uses the existing `make_client`, `run_review`, `Trace`, and GitHub
  diff behavior. Tests inject fakes and make zero model/network calls.
- Repository lookup is an exact alias allowlist; symlink/resolved-path escape,
  missing checkout, option injection, and unregistered repositories fail before
  `gh` or model work.
- Shutdown stops new submissions and drains/cancels executor work within the
  process lifecycle; no claim of multi-process worker coordination is made.

## Single Writer ownership

Codex may create or modify only:

- `docs/plans/week7-protocol-service.md`
- `docs/protocol-service.md`
- `docs/reviews/week7-claude.md` only when integrating Claude's read-only report
- `src/code_review_agent/service_core.py`
- `src/code_review_agent/service.py`
- `src/code_review_agent/mcp_server.py`
- `tests/test_week7_service_core.py`
- `tests/test_week7_service.py`
- `tests/test_week7_mcp.py`
- `pyproject.toml`
- `requirements.lock`
- `requirements.txt`
- `README.md`
- `AGENDA.md`
- `.env.example`
- `Dockerfile.service`
- `.dockerignore`
- `.github/workflows/ci.yml` only if a service/container smoke is required

Claude is a read-only reviewer. Its only writable path in its review worktree
is `docs/reviews/week7-claude.md`. Finder/Verifier/Repair runtime, prompts,
observability/redaction implementations, old CLI behavior, all evaluation
assets, security corpus/reports, prior plans/reviews, and release history are
read-only. Integration remediation remains within the Codex list above.

## Prohibited changes

- No direct development commit on `master` and no force push.
- No A2A endpoint or multi-Agent routing claim this week.
- No external model call, live GitHub delivery/post, paid evaluation, or Docker
  daemon use is required for mandatory validation.
- No reading or modifying `eval/`, `eval/holdout/`, sealed Week 4/5 artifacts,
  or immutable Week 6 reports during implementation.
- No arbitrary repository paths, shell commands, webhook-selected model,
  webhook-selected output path, or webhook-selected authorization.
- No API that consumes a Repair approval or mutates/commits a repository.
- No weakening tests, redaction, telemetry, approval, sandbox, or resource
  budgets to make service checks pass.

## Validation

Focused development gates:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_week7_service_core `
  tests.test_week7_service tests.test_week7_mcp -v
.venv\Scripts\python.exe -m ruff check src\code_review_agent\service_core.py `
  src\code_review_agent\service.py src\code_review_agent\mcp_server.py `
  tests\test_week7_service_core.py tests\test_week7_service.py `
  tests\test_week7_mcp.py
.venv\Scripts\python.exe -m mypy src\code_review_agent
```

Full pre-handoff and post-review gates:

```powershell
.venv\Scripts\python.exe scripts\verify.py
.venv\Scripts\python.exe -m code_review_agent.service --help
.venv\Scripts\python.exe -m code_review_agent.mcp_server --help
git diff --check
```

The existing evaluation-assets consistency gate is intentionally omitted:
Week 7 changes neither prompts/judging nor evaluation fixtures and must not read
the protected `eval/holdout/` assets. CI may run the existing gate from a clean
checkout; implementation does not inspect its contents.

## Acceptance criteria

- Invalid/missing bearer tokens, webhook secrets/signatures, Origins,
  repositories, actions, delivery IDs, payloads, and oversized bodies fail
  before job/model work.
- Duplicate webhook delivery IDs create exactly one review job.
- Concurrent submissions have unique identities and valid monotonic state
  transitions; abandoned work is marked failed on restart.
- Fake-runner success/failure produces bounded persisted status and a trace;
  provider/diff/path content is absent from sanitized errors.
- HTTP OpenAPI exposes the frozen REST schemas; MCP client tests list and call
  the three tools, two resource templates, and one prompt over a standard
  transport without network/model access.
- Existing CLI, Review, Repair, Week 4/5 evaluation, and Week 6 security tests
  remain green; coverage remains at least 85%.
- Service and MCP CLIs have working help output; the service image builds and
  reaches a help/health smoke when CI coverage is added.
- Claude `fable5` review has no unresolved P0/P1/P2 finding, remediation is
  independently revalidated, the final branch is pushed, and GitHub CI reaches
  a terminal success state.

## Handoff and integration

1. Commit this contract before implementation.
2. Codex implements only its owned files, reviews the complete diff, runs all
   local gates, and creates a handoff commit.
3. Claude `fable5` reads the exact handoff diff and contract, writes only the
   Week 7 review report, and commits it on its review branch.
4. Integration starts from the exact Codex handoff, imports the review report,
   remediates actionable findings within the frozen scope, and reruns all gates.
5. Only after local success may integration be fast-forwarded to `master`,
   pushed, and monitored to terminal GitHub CI success.

## Delivery report

- Summary: pending
- Changed files: pending
- Codex handoff commit: pending
- Claude review commit: pending
- Integration/master commit: pending
- Commands and results: pending
- Known risks or assumptions: pending
