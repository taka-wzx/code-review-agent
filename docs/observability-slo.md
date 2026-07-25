# Production metrics, SLOs, and alert operations

## Export and aggregation

`GET /metrics` exposes Prometheus text from one designated API exporter. It combines
authoritative shared-database state with canonical JSONL traces on the shared trace volume.
All workers therefore contribute to one cumulative view; worker process lifetime and
replica count are not metric dimensions. If the API is scaled horizontally, scrape exactly
one designated exporter replica because every API replica reads the same global database
and trace volume.

The exporter has no tenant or identity labels. Allowed business labels are bounded status,
provider, token type, tool, and decision enums. IDs, repository aliases, trace IDs, error
messages, paths, credentials, headers, and arbitrary model/provider strings are never
labels. Unknown providers and tools collapse to `other`.

Canonical JSONL remains the immutable per-run audit. Metrics are aggregate operational
views and cannot replace a trace or access-controlled audit record.

`llm_cost_cny_total` converts settled integer micro-USD usage with the fixed
`CRAG_USD_CNY_RATE` configured for the deployment (default `7.2`). It is an accounting
estimate bound to that configured rate, not a live foreign-exchange quote.

## Histogram buckets

| Metric | Buckets (seconds) | Basis |
| --- | --- | --- |
| `webhook_ack_seconds` | .01, .025, .05, .1, .25, .5, 1, 2, 5 | ACK should stay sub-second; 5s leaves diagnostic space below common webhook deadlines. |
| `queue_wait_seconds` | .1, .5, 1, 5, 15, 30, 60, 120, 300, 600 | Covers the 1s default poll, 30s stale-worker window, 60s lease, and sustained backlog. |
| `llm_request_duration_seconds` | .1, .25, .5, 1, 2, 5, 10, 30, 60, 120 | Covers fast rejects through the existing bounded provider-call deadline range. |
| `review_duration_seconds` | 5, 15, 30, 60, 120, 300, 600, 1200 | Covers short fake/small reviews through the 20-minute operational ceiling. |
| `approval_wait_seconds` | 60, 300, 900, 3600, 14400, 86400, 604800 | Human control-plane latency spans minutes through seven days. |

Prometheus histogram buckets are cumulative and include `+Inf`. Percentiles use
`histogram_quantile` over a five-minute rate for alerting and a 30-day window for SLO
reporting. An empty denominator is `null` in reports and must not be rendered as 0%.

## SLO contract

| SLO | Objective / window | Numerator and denominator | Exclusions |
| --- | --- | --- | --- |
| Webhook ACK | 99.9% <= 500ms / 30d | ACK observations at or below .5s / all ACK observations | Invalid HMAC and oversized requests are security rejects, reported separately. |
| Job completion | 99.0% / 30d | `awaiting_approval|declined|published` outcomes / unique submissions | Explicit policy/schema rejects before durable submission. |
| Review latency | 95% <= 10m / 30d | completed reviews at or below 600s / completed reviews | Human approval wait is separate. |
| Duplicate execution | 99.9% single-attempt / 30d | submissions minus additional attempts / submissions | Operator-requested replay is not available in this phase; retries remain visible. |
| Unauthorized publish | 100% prevented / 30d | denied publish attempts with zero publisher success caused by them / denied publish attempts | Empty denominator is compliant but reported as `null`, not 100%. |
| Trace completeness | 99.9% / 30d | terminal jobs with a valid canonical final trace / terminal jobs expected to carry one | Legacy pre-canonical records are reported separately during migration. |

The error budget is `1-objective`. Use 1h/6h fast burn and 6h/3d slow burn windows before
paging; the committed rules are conservative single-window deployment defaults and should
be tuned only with production evidence.

## Alerts and response

Rules in `observability/prometheus/alerts.yml` cover queue growth, completion decline,
provider 429/5xx growth, p95 review latency, daily CNY budget, telemetry degradation,
unauthorized operations, and approval replay/mismatch. Every rule has a stable alert name,
bounded labels, a runbook action, and a `for` interval to suppress one-scrape noise.

On queue or latency alerts, stop new submissions before changing worker capacity. On
telemetry degradation, preserve local JSONL and investigate exporter/database access. On
unauthorized or approval-binding alerts, disable publication, retain audit/trace evidence,
and investigate before replaying any job. No rule in this repository configures or sends a
notification.
