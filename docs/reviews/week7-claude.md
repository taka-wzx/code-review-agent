# Week 7 Protocol Service — Independent Claude Review

- Reviewer model: `fable5` (Claude Code, read-only reviewer per `docs/plans/week7-protocol-service.md`)
- Review branch: `claude/week7-protocol-service-manual-review`
- Base commit: `2ae3bf242bfaac2a51940c1a4a0e5a76d1246cb3`
- Codex handoff commit: `dbfd3bebfe4fed109b3c7630f1ef1d2e3a8b3c88`
- Review date: 2026-07-19
- Scope inspected: every line of `git diff 2ae3bf2...dbfd3be` (17 files, +2007/−6), plus
  read-only inspection of the touched dependencies (`tracelog.Trace`/`JsonlFileExporter`,
  `llm.make_client`, `agent.run_review`, `scripts/verify.py`, and the installed
  `mcp==1.28.1` `transport_security` implementation).

## Verdict: CONDITIONAL PASS

No P0 or P1 finding. The frozen interfaces are implemented faithfully (see checklist
below), the offline gates all pass, and the security boundary design (Bearer before
routing, HMAC over the raw body before JSON parsing, exact alias registry, exclusive
canonical traces, CAS state transitions in SQLite) is sound. Four P2 findings —
all empirically confirmed on the handoff tree — require remediation before
integration; six P3 findings are acceptable with the rationale given.

## Frozen-interface verification

| # | Frozen requirement | Status |
| --- | --- | --- |
| 1 | Schema `crag.service/v1alpha1` | ✅ `service_core.py:25`, present in every response |
| 2 | States `queued -> running -> succeeded\|failed` | ✅ enum + CAS `UPDATE ... WHERE state=?` (`service_core.py:352-361`) |
| 3 | All seven HTTP endpoints | ✅ `service.py:179-233`; `/mcp` mounted Streamable HTTP |
| 4 | Bearer on `/v1/*` and HTTP `/mcp` | ✅ middleware before routing (`service.py:165-177`), constant-time compare |
| 5 | Webhook HMAC over unmodified body, constant-time, before JSON parse | ✅ `service.py:100-105,209-226` |
| 6 | `X-GitHub-Delivery` idempotency key | ✅ `BEGIN IMMEDIATE` check-then-insert (`service_core.py:318-350`); replay returns original job (but see W7C-P2-04) |
| 7 | Alias-only repository naming, never a path | ✅ regex + registry resolve at startup, `resolve(strict=True)` + `.git` check (`service_core.py:113-146`) |
| 8 | MCP: 3 tools, 2 resource templates, 1 prompt | ✅ verified via official in-memory client session (`tests/test_week7_mcp.py`) |
| 9 | `approve_patch` intentionally absent | ✅ absent; rationale preserved in docs. This review does **not** recommend adding a generic approval endpoint |
| 10 | A2A deferred | ✅ no A2A surface or claim |
| 11 | Existing Review/Repair/CLI/trace/eval/security behavior compatible | ✅ full suite green (584 tests, 0 modifications outside owned files) |
| 12 | MCP Python SDK stable 1.x | ✅ `mcp>=1.28,<2` in pyproject, `mcp==1.28.1` in lock |

Additional boundary checks performed: no path-normalization bypass of the auth
middleware (Starlette route/mount matching guarantees any request reaching `/v1`
routes or the MCP mount carries a path the middleware matched); `:*` port
wildcard in `CRAG_ALLOWED_HOSTS` is genuinely supported by the installed SDK's
`TransportSecurityMiddleware._validate_host`; trace files are created with
`O_CREAT|O_EXCL` (Week 6 `JsonlFileExporter`), so audit evidence cannot be
silently truncated; `gh` argv is list-form, cwd-bound, and both PR reference
shapes (positive integer, exact `https://github.com/<owner>/<repo>/pull/<n>`
matching the registered alias) cannot start with `-`, so no option injection;
sanitized error codes never carry provider/exception/path text; HTTP secrets are
not read from `.env` (only the pre-existing provider-key `load_dotenv` inside
`make_client` runs, at job time, exactly as documented).

## P0 findings

None.

## P1 findings

None.

## P2 findings

### W7C-P2-01 — Webhook body limit is enforced only after full in-memory buffering; chunked requests defeat the pre-read check

- File/line: [service.py:201-211](../../src/code_review_agent/service.py#L201-L211)
- Evidence: the handler checks `Content-Length` only when the header is present,
  then calls `await request.body()`, which accumulates the entire body in memory
  before `len(body)` is compared to `MAX_WEBHOOK_BYTES`. Reproduced on the
  handoff tree: an ASGI probe streaming a 5 MiB body with no `Content-Length`
  (chunked transfer) was fully buffered and only then rejected with
  400 `invalid_request`. uvicorn/h11 impose no request-body cap of their own, so
  the same holds over a real socket.
- Impact: pre-authentication memory exhaustion on the one endpoint that must be
  reachable from the internet for GitHub delivery. An attacker who can reach
  `/webhooks/github` (no valid signature needed — the signature is checked after
  the body is read, necessarily so for HMAC, but the cap must not be) can open
  several concurrent chunked uploads and drive the process out of memory. This
  also contradicts the frozen contract sentence "Ingress limits are enforced
  before JSON parsing or job creation: 1 MiB webhook body" in spirit: the limit
  is checked before parsing, but resource consumption is unbounded before the
  check.
- Remediation: read `request.stream()` chunk-by-chunk into a bounded buffer and
  abort with 400/413 the moment the running total exceeds `MAX_WEBHOOK_BYTES`,
  before computing the HMAC. Keep the existing `Content-Length` fast-path
  rejection. Add a regression test posting an over-limit body without
  `Content-Length`.

### W7C-P2-02 — 422 validation errors echo the submitted diff back, violating the frozen "errors never echo a diff" contract

- File/line: [service.py:35-44](../../src/code_review_agent/service.py#L35-L44)
  (Pydantic models; the defect is the absence of a bounded
  `RequestValidationError` handler in `create_app`)
- Evidence: FastAPI's default `RequestValidationError` handler serializes
  Pydantic v2 errors including the `input` field. Reproduced on the handoff
  tree: `POST /v1/reviews/diff` with a diff one byte over `max_length` returned
  HTTP 422 with a 524,498-byte body containing the complete submitted diff
  (marker string verified present). Any `string_too_long`/pattern failure on the
  `diff` field echoes the full diff.
- Impact: direct violation of the frozen contract line "User-facing errors never
  echo a diff, credential, provider exception, host path, or command output"
  (`docs/plans/week7-protocol-service.md:86-87`). The reflection goes to the
  authenticated submitter, so no cross-principal disclosure, but half-megabyte
  diffs land in client logs/proxies the operator does not control, and the
  response amplification is contract-breaking regardless of audience.
- Remediation: register an `app.exception_handler(RequestValidationError)` that
  returns `{"schema_version": ..., "error": {"code": "invalid_request"}}` (or at
  most `loc`/`type` per error, never `input`/`ctx`), plus a regression test
  asserting an oversized/invalid diff is absent from the 422 body.

### W7C-P2-03 — Any second process on the same `CRAG_STATE_DIR` sweeps the live service's in-flight jobs to `failed` and discards their results

- File/line: [service_core.py:293-303](../../src/code_review_agent/service_core.py#L293-L303)
  (unconditional startup sweep), [mcp_server.py:86-92](../../src/code_review_agent/mcp_server.py#L86-L92)
  (second entry point with the same default state dir)
- Evidence: `JobStore._initialize` unconditionally marks every `queued`/`running`
  row `failed/service_restarted`. Both `crag-service` and `crag-mcp` default to
  `~/.crag/service` (`service_core.py:540`). Launching `crag-mcp` (e.g. an IDE
  MCP client spawning the stdio server — the configuration
  `docs/protocol-service.md` recommends as "the safest local integration") while
  `crag-service` is mid-review therefore fails the running job in SQLite. The
  existing test `test_store_marks_abandoned_work_failed_on_restart` demonstrates
  exactly this mechanism with a second live `JobStore` on the same directory.
  When the first process's runner then completes, `succeed()`'s CAS transition
  finds `state != running`, raises, and `_run` silently discards the finished
  review (`service_core.py:479-485`).
- Impact: plausible default-configuration data corruption: in-flight and queued
  reviews are spuriously terminal-failed, completed results are thrown away
  without any log, and webhook deliveries bound to those jobs are permanently
  consumed (their replay returns the failed job as `duplicate`). No test or
  documentation covers concurrent processes sharing a state dir.
- Remediation (either is acceptable): (a) hold an exclusive advisory lock file
  in the state dir for the process lifetime and refuse to start when it is held;
  or at minimum (b) give `crag-mcp` a distinct default state dir and add an
  explicit warning to `docs/protocol-service.md` that a state dir belongs to
  exactly one process at a time. Add a test for whichever behavior is chosen.

### W7C-P2-04 — Submission racing shutdown strands a `queued` job and permanently burns the webhook delivery ID

- File/line: [service_core.py:510-523](../../src/code_review_agent/service_core.py#L510-L523)
  (delivery+job committed before `_queue`), [service_core.py:465-472](../../src/code_review_agent/service_core.py#L465-L472)
- Evidence: `submit_pr` commits the job row and the `deliveries` row inside
  `store.create`, and only then calls `_queue`, which raises `ServiceClosed`
  once shutdown has begun. Reproduced on the handoff tree: after
  `service.shutdown()`, `submit_pr(..., delivery_id="race-delivery-1")` raised
  `ServiceClosed`, yet SQLite retained the delivery row and a job stuck in
  `queued`; replaying the same delivery then returned `duplicate: True` with the
  never-executed job. `submit_diff` has the same create-then-queue ordering
  (stranded `queued` row only, no delivery burn).
- Impact: the webhook caller receives 503 (correct), but the idempotency key is
  already consumed, so a later redelivery of that GitHub delivery — GitHub does
  not auto-retry; an operator redelivers manually — is answered 202
  `duplicate: true` and never runs a review: an explicit failure is converted
  into a fake success. After restart the job at least becomes visible as
  `failed/service_restarted`, and a manual `POST /v1/reviews/pr` (fresh job, no
  delivery ID) can recover, which is why this is P2 rather than P1.
- Remediation: make delivery consumption conditional on successful queueing —
  e.g. check `_accepting` (under the lock) before `store.create`, and on
  executor rejection delete the just-inserted delivery/job rows (or mark the job
  `failed/service_closed`) in a compensating transaction. Add a shutdown-race
  test asserting a rejected submission does not consume the delivery ID.

## P3 findings

### W7C-P3-01 — `gh` failures are miscategorized as `internal`; the `external_command` category is unreachable from the PR path

- File/line: [service_core.py:202-209](../../src/code_review_agent/service_core.py#L202-L209)
  vs. [service_core.py:173-175](../../src/code_review_agent/service_core.py#L173-L175)
- Evidence: `_pr_diff` wraps `OSError`/`SubprocessError` (including gh-not-found
  and the 60 s timeout) into a bare `RuntimeError`, so `_safe_failure`'s
  `isinstance(exc, (FileNotFoundError, subprocess.SubprocessError))` branch can
  never fire for PR jobs; every gh failure surfaces as `error.code: "internal"`.
- Impact: operators cannot distinguish "gh missing/unauthenticated/timed out"
  from an actual service bug; the documented stable error taxonomy is misleading
  for its most likely failure mode. No security impact (messages stay sanitized).
- Remediation: raise a dedicated `ExternalCommandError(RuntimeError)` in
  `_pr_diff` and map it to `external_command` in `_safe_failure`; assert the
  category in the existing command-failure test.
- Acceptance rationale if deferred: cosmetic taxonomy fidelity; no leak, no
  state corruption.

### W7C-P3-02 — `gh pr diff` stdout is buffered unbounded before the 512 KiB check

- File/line: [service_core.py:190-209](../../src/code_review_agent/service_core.py#L190-L209)
- Evidence: `capture_output=True` accumulates the whole diff in memory; the
  `MAX_DIFF_BYTES` check runs only after the process exits. Anyone able to open
  a PR against a registered public repository influences that size (GitHub PR
  diffs can reach hundreds of MB).
- Impact: per-worker memory spikes on hostile PRs (bounded by worker count and
  the 60 s timeout, hence P3 not P2).
- Remediation: stream via `Popen` and stop reading at `MAX_DIFF_BYTES + 1`, or
  use `gh pr diff --name-only`-style pre-checks; keep the timeout.
- Acceptance rationale if deferred: requires a registered repo that accepts
  external PRs plus a webhook/API trigger; blast radius is one worker thread.

### W7C-P3-03 — Blocking SQLite/file I/O runs directly on the event loop in `async def` endpoints

- File/line: [service.py:183-231](../../src/code_review_agent/service.py#L183-L231)
- Evidence: every endpoint is `async def` but calls synchronous
  `ReviewService`/`JobStore` methods (SQLite with `busy_timeout=10000`, up to
  4 MiB trace reads). A contended database can stall the entire event loop —
  including `/healthz` — for up to 10 s per call.
- Impact: availability degradation under concurrent load; no correctness issue.
- Remediation: declare the handlers as plain `def` (FastAPI then runs them on
  the threadpool) or wrap calls in `anyio.to_thread.run_sync`.
- Acceptance rationale if deferred: single-operator local service with worker
  threads doing the heavy work; stalls are bounded by `busy_timeout`.

### W7C-P3-04 — Lifespan shutdown is not exception-safe and can block indefinitely

- File/line: [service.py:138-143](../../src/code_review_agent/service.py#L138-L143)
- Evidence: `service.shutdown()` runs after the `async with
  mcp.session_manager.run()` block rather than in a `finally`; an exception
  during session-manager teardown skips executor shutdown. `shutdown(wait=True)`
  also joins all queued+running reviews (each up to the ~300 s soft deadline)
  with no upper bound, so a Ctrl+C can appear to hang.
- Impact: unclean teardown on rare teardown exceptions; slow but honest graceful
  shutdown otherwise (the plan's "drains executor work" wording permits it).
- Remediation: move `service.shutdown()` into `try/finally`; optionally bound
  the drain and let the startup sweep reconcile the remainder.
- Acceptance rationale if deferred: startup sweep already converts any stranded
  work to a visible `failed/service_restarted` state.

### W7C-P3-05 — Missing security/concurrency tests on the new boundary

- File/line: [tests/test_week7_service.py:1](../../tests/test_week7_service.py#L1)
  (file scope)
- Evidence: no test covers (a) a `pull_request` webhook with a **missing**
  `X-GitHub-Delivery` header (code rejects via the delivery regex — correct, but
  unpinned); (b) Host-header rejection (421) at `/mcp` — only the allowed-host
  path is exercised; (c) two concurrent submissions of the same delivery ID
  racing `BEGIN IMMEDIATE` (the acceptance criterion "concurrent submissions"
  is only tested sequentially); (d) an oversized webhook body (with or without
  `Content-Length`); (e) a successful MCP session over the mounted Streamable
  HTTP transport (the boundary test stops at 406; the full MCP surface is only
  exercised in-memory).
- Impact: the strongest Week 7 security claims rest partly on code reading
  rather than pinned regressions; a future refactor could silently regress them.
- Remediation: add the five tests above; (c) can use two threads and a barrier
  as in the Week 2 concurrency tests.
- Acceptance rationale if deferred: behaviors were manually verified in this
  review; risk is regression, not current defect.

### W7C-P3-06 — Documentation/CLI nits: AGENDA test count is wrong; malformed `CRAG_SERVICE_PORT` crashes `--help` with a raw traceback

- File/line: [AGENDA.md:26](../../AGENDA.md#L26),
  [service.py:240](../../src/code_review_agent/service.py#L240)
- Evidence: AGENDA claims "21 个新增离线协议测试"; the three new test modules
  contain 24 tests (13 + 9 + 2, all executed). And
  `CRAG_SERVICE_PORT=abc crag-service --help` dies with a `ValueError` traceback
  because the env var is parsed inside `build_parser()` at default-construction
  time (reproduced; exit 1 with traceback).
- Impact: minor report inaccuracy; ugly-but-safe startup failure (no secret in
  the traceback beyond install paths on the operator's own console).
- Remediation: correct the count; parse the env port after `parse_args` (or
  wrap in a clean `SystemExit` message like the existing range check).
- Acceptance rationale if deferred: no functional or security consequence.

## Commands run and results

All commands were run from the review worktree at `dbfd3be` with
`PYTHONPATH=<worktree>\src` and the Codex venv interpreter
`E:\shiyan\code_review_agent\traces\worktrees\codex-week7\.venv\Scripts\python.exe`
(Python 3.13, Windows 11), per the task authorization. The worktree was not
modified (coverage data was redirected outside the repo via `COVERAGE_FILE`).

| Command | Result |
| --- | --- |
| `python -m unittest tests.test_week7_service_core tests.test_week7_service tests.test_week7_mcp -v` | **OK — 24 tests, 0 failures** |
| `python -m ruff check src\...service_core.py src\...service.py src\...mcp_server.py tests\test_week7_*.py` | **All checks passed** |
| `python -m mypy src\code_review_agent` | **Success: no issues in 26 source files** |
| `python scripts\verify.py` (no `--eval-assets`, per contract) | **All offline validation passed** — Ruff clean; **584 tests OK (3 env skips)**; coverage **TOTAL 86%** (≥ 85 gate); mypy clean; module + `crag` console smokes OK |
| `python -m code_review_agent.service --help` | OK (exit 0) |
| `python -m code_review_agent.mcp_server --help` | OK (exit 0) |
| `git diff --check 2ae3bf2...dbfd3be` | clean (exit 0) |
| Probe: 422 on oversized diff | **echoes full diff**, 524,498-byte response (W7C-P2-02) |
| Probe: 5 MiB chunked webhook body, no `Content-Length` | fully buffered, then 400 (W7C-P2-01) |
| Probe: `submit_pr` with delivery ID after `shutdown()` | `ServiceClosed` raised, delivery row + `queued` job persisted; replay → `duplicate: True` (W7C-P2-04) |
| Probe: `CRAG_SERVICE_PORT=abc` + `--help` | `ValueError` traceback, exit 1 (W7C-P3-06) |

These results independently reproduce the Codex evidence (584/3 skips, 86%,
ruff, mypy 26 files, help smokes, lock content). Restricted paths (`eval/`,
`eval/holdout/`, sealed Week 4/5 artifacts, immutable Week 6 reports, `.env`,
credentials) were not read, listed, or hashed; nothing was pushed, merged,
posted, containerized, or sent to a model.

## Residual risks and unverified items

- **Docker/CI unexecuted**: `Dockerfile.service` and the two new CI steps have
  never been built anywhere (local Docker engine unavailable per handoff; CI
  runs only after push). The `apt-get install gh` step assumes the
  `python:3.13-slim` base's Debian release ships a `gh` package; if the resolved
  base is older this fails Linux-only, exactly as the task warns. CI must be
  watched to terminal state after push.
- **Linux lockfile install unverified locally**: `requirements.lock` was proven
  on Windows only; `pywin32` is correctly marker-guarded and the `lock-check` CI
  job will prove Linux, but that evidence does not exist yet.
- **Single-version local evidence**: this review ran on Python 3.13/Windows.
  Python 3.10–3.12 and Linux coverage relies on the (unrun) CI matrix.
- **MCP-over-HTTP end-to-end untested**: the full tool/resource/prompt surface
  is proven only over the in-memory transport; the mounted Streamable HTTP path
  is verified to the auth/Origin/406 boundary (see W7C-P3-05). MCP spec-level
  conformance (protocol revision negotiation) is delegated to `mcp==1.28.1`.
- **No live-path evidence**: real GitHub webhook deliveries, `gh` execution,
  provider calls, and the `DefaultReviewRunner` happy path against a real model
  are untested by design this week; docs correctly refrain from production
  claims.
- **Unauthenticated `/openapi.json`, `/docs`**: intentional (tests pin it);
  disclosure is route shapes only, no secrets. Worth an explicit sentence in
  `docs/protocol-service.md` if the surface ever leaves loopback.
- **No backlog bound or rate limit**: the executor's worker count is bounded but
  its queue is not; authenticated clients (or rapid `synchronize` events on a
  registered repo) can grow the queue and SQLite without limit. Acceptable for
  the stated operator-local scope; a gateway must provide rate limiting for any
  remote exposure.
- **Observed timing**: FakeRunner-based tests prove protocol semantics, not
  service latency under real review load.

## Required-remediation checklist (before integration)

- [ ] W7C-P2-01: cap webhook body during streaming read (chunked bodies must
      abort at 1 MiB), with regression test.
- [ ] W7C-P2-02: bounded `RequestValidationError` handler; 422 must not echo
      `input`; regression test asserting no diff reflection.
- [ ] W7C-P2-03: state-dir exclusivity (advisory lock, or distinct `crag-mcp`
      default + explicit doc warning), with test or documented operator rule.
- [ ] W7C-P2-04: delivery ID must not be consumed by a submission the executor
      rejected; shutdown-race regression test.
- [ ] P3 items (W7C-P3-01…06): fix or accept individually with the rationale
      recorded above; none blocks integration on its own.
