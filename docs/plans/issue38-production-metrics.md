# Issue 38: Cross-service production metrics assets

Status: in progress

## Goal

Deliver a versioned, machine-readable contract and Grafana dashboard for the
existing production metrics exporter. The assets must cover review, queue,
provider, approval, and publication paths and be validated offline before a
focused implementation PR is opened.

## Base

- Base branch: `master`
- Base commit: `6ced1c4ebde377bfc386c4173efb700acdb27416`
- Task branch: `codex/issue-38-production-metrics`
- Integration branch: `integration/issue38-production-metrics`

## Frozen interfaces

- `GET /metrics`, `ProductionMetrics`, metric names, metric semantics, and
  bounded-label behavior remain unchanged.
- Existing Phase 9F dashboard, alert rules, migrations, service routes, and
  database schema remain unchanged.
- The new validator uses only the Python standard library and only reads local
  source and asset files. It performs no network, credential, provider, or
  GitHub operation.

## File ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue38-production-metrics.md`; `observability/metric-contract-v1.json`; `observability/grafana/production-overview-v1.json`; `scripts/validate_production_metrics_assets.py`; `tests/test_issue38_production_metrics_assets.py` | `src/code_review_agent/production_metrics.py`; `observability/grafana/phase9f-overview.json`; `observability/prometheus/alerts.yml`; `docs/observability-slo.md`; migrations; all other repository files |

No other agent has write ownership for this task.

## Prohibited changes

- No direct commit, merge, rebase, or push to `master`.
- No changes to `src/`, migrations, dependencies, packaging, CI, deployment,
  existing dashboard, or alert rules.
- No access to, execution of, or changes under `eval/` or `eval/holdout/`.
- No credentials, tokens, repository aliases, trace IDs, absolute host paths,
  or raw operational records in the new assets.
- No external service calls from the validator or tests.

## Codex assignment

- Objective: add a versioned metric contract, importable Grafana dashboard,
  offline validator, and focused tests for Issue #38.
- Required tests:

  ```powershell
  .venv\Scripts\python.exe -m unittest -v tests.test_issue38_production_metrics_assets
  .venv\Scripts\python.exe scripts\validate_production_metrics_assets.py
  .venv\Scripts\python.exe -m unittest discover -s tests
  .venv\Scripts\python.exe -m ruff check .
  .venv\Scripts\python.exe -m mypy src/code_review_agent
  .venv\Scripts\python.exe scripts\verify.py
  git diff --check
  ```

- Delivery commit: pending implementation.

## Acceptance criteria

- The versioned contract names every current exporter metric, its Prometheus
  type, service path, permitted labels, and bounded enum values where present.
- The dashboard has a stable UID, unique panel IDs, importable JSON, and
  query coverage for review, queue, provider, approval, and publication paths.
- The validator rejects unknown metric references, unbounded or prohibited
  label references, malformed dashboard panels, missing service-path coverage,
  and contracts that drift from the exporter label policy or metric inventory.
- Focused tests exercise valid assets and representative invalid fixtures.
- The final diff is limited to the declared paths and all required offline
  checks pass.

## Delivery report

- Summary: pending implementation.
- Changed files: pending implementation.
- Commit: pending implementation.
- Commands run and results: pending implementation.
- Known risks or assumptions: the validator is deliberately a local asset
  contract check, not a Grafana server integration test.
