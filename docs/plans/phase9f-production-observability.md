# Phase 9F: Production Observability, SLOs, Dashboards, and Alerts

Status: **active and frozen**

Frozen date: 2026-07-25

Baseline: `origin/master` at `651f3ba049f263154862ad5ba5411dddbe67f3ff`

Task branch: `codex/phase9f-production-observability`

## Goal

Add production-oriented aggregate metrics, a scrape endpoint, a loadable Grafana
dashboard, SLO definitions, and tested Prometheus alert rules on top of the existing
canonical JSONL trace. The canonical trace remains the per-run audit source and is not
replaced or rewritten.

All validation is offline. This phase does not configure a real collector, notification
channel, cloud deployment, GitHub write, or model call.

## Metrics Contract

The exporter provides these stable metric families:

- `review_jobs_total{status}` and `review_duration_seconds`;
- `webhook_ack_seconds`, `queue_depth`, and `queue_wait_seconds`;
- `llm_requests_total{provider,status}`, `llm_request_duration_seconds`,
  `llm_tokens_total{type}`, and `llm_cost_cny_total`;
- `tool_calls_total{tool,status}`, `fail_open_total`, and `degraded_total`;
- `approval_wait_seconds`, `approval_decisions_total{decision}`, and
  `finding_feedback_total{decision}`;
- `idempotency_hits_total` and `publisher_calls_total{status}`.

Allowed business label keys are exactly `status`, `provider`, `type`, `tool`, `decision`,
`operation`, and `reason`; Prometheus histograms additionally use the standard `le` label.
Each label has a frozen bounded value set. `user_id`, `review_id`, `repository`,
`trace_id`, correlation values, exception types/messages, paths, and arbitrary provider
responses are prohibited as metric labels. Identity remains only in access-controlled
audit and canonical trace records.

Counters and histograms are updated from actual durable transitions. Aggregate state is
stored in PostgreSQL/SQLite by migration 0006 and updated in the same transaction as the
authoritative business transition where possible. This gives multiple workers one
database-backed cumulative series instead of per-process counters that would be lost on
restart or double-counted by replica churn. `queue_depth` is a scrape-time gauge derived
from authoritative `received|queued` rows. API-only request latency that cannot share the
job transaction is recorded immediately after the response boundary.

Histogram buckets are fixed from the existing service deadlines and retry/lease ranges:
sub-second webhook acknowledgement; seconds-to-minutes provider and queue waits; and
minutes-to-days approval waits. Bucket rationale and exact boundaries are documented in
`docs/observability-slo.md`.

## Export, Dashboard, SLO, and Alerts

- `GET /metrics` returns Prometheus text format without authentication secrets or tenant
  identity. It exposes only globally aggregated, bounded-cardinality operational data.
- JSONL trace files remain one immutable file per run and keep the existing redaction and
  validation behavior.
- Grafana JSON uses stable dashboard/panel UIDs and covers business effect, stability,
  quality feedback, cost, queue capacity, and approval safety.
- Prometheus rules cover queue growth, completion decline, provider 429/5xx growth, p95
  latency, daily cost budget, telemetry degradation, unauthorized operations, and
  approval replay/mismatch.
- SLOs cover webhook acknowledgement, job completion, review latency, duplicate
  execution, unauthorized publish, and trace completeness, with explicit numerator,
  denominator, window, exclusions, empty-window semantics, and burn-rate guidance.

## Owned Paths

Codex owns only these paths for this task:

- `docs/plans/phase9f-production-observability.md`;
- `migrations/versions/0006_phase9f_production_metrics.py`;
- `src/code_review_agent/production_metrics.py`;
- `src/code_review_agent/service.py`;
- `src/code_review_agent/service_core.py`;
- `src/code_review_agent/service_queue.py`;
- `src/code_review_agent/worker.py`;
- `src/code_review_agent/approval_publish.py`;
- `compose.service.yml`;
- `README.md`;
- `docs/security-observability.md`;
- `docs/observability-slo.md`;
- `observability/grafana/phase9f-overview.json`;
- `observability/prometheus/alerts.yml`;
- `scripts/phase9f_load_test.py`;
- `scripts/phase9f_container_test.py`;
- `tests/test_phase9f_production_observability.py`;
- `pyproject.toml` and `requirements.lock` only if packaging the new non-Python data files
  requires it; no dependency or entry point may be added.

All other paths are read-only. `eval/**` and `eval/holdout/**` are prohibited: do not
enumerate, read, execute, or modify them.

## Validation

All commands use fakes and local databases and make no external model or publisher call:

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $repoRoot "src"

& $python -m unittest -v tests.test_phase9f_production_observability
& $python scripts\phase9f_load_test.py --submissions 50 --workers 2
& $python scripts\phase9f_container_test.py
& $python -m ruff check .
& $python -m mypy src/code_review_agent
& $python scripts\verify.py
& $python -m pip check
git diff --check
git diff --name-only origin/master...HEAD
git status --short --branch
```

Required tests cover fake clock, fake metrics, scrape parsing, real state-transition
updates, multi-worker aggregation, histogram boundaries, prohibited/high-cardinality
labels, dashboard JSON loading and stable UIDs, alert positive and negative fixtures,
telemetry degradation, authorization denial, approval replay/mismatch, and an offline
load using only `FakeReviewRunner`/`FakePublisher`.

Container validation may report a documented environment skip when Docker is absent,
but CI must execute the service container smoke. No validation command may use
`--eval-assets`.

## Delivery Control

The user authorizes a stable commit on this task branch, push of only
`codex/phase9f-production-observability`, Draft PR creation, CI observation, Ready state,
and merge through that PR after checks pass. Direct push, merge, or rebase of `master` is
prohibited. After merge, verify the merge SHA and master CI. Real collectors,
notification delivery, cloud deployment, GitHub publication, and model calls remain
unauthorized.

## Change Control

This contract is frozen after creation. Any new dependency, public entry point, external
call, state-machine semantic change, secret channel, or writable path requires explicit
user approval and a contract revision before implementation.
