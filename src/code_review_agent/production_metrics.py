"""Bounded-cardinality Prometheus metrics derived from durable state and traces."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable, Mapping, Protocol

from sqlalchemy import Engine, text

from code_review_agent.observability import load_span_records


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_ALLOWED_LABELS = frozenset(
    {"status", "provider", "type", "tool", "decision", "operation", "reason", "le"}
)
_PROHIBITED_LABELS = frozenset(
    {"user_id", "review_id", "repository", "trace_id", "error", "message"}
)
_PROVIDERS = frozenset({"deepseek", "zhipuai", "openai", "unknown", "other"})
_TOOLS = frozenset({"read_file", "search_repo", "run_linter", "submit_review", "other"})
_TOOL_ALIASES = {
    "search": "search_repo",
    "search_repository": "search_repo",
    "submit_findings": "submit_review",
    "submit_verdicts": "submit_review",
}
_TOKEN_TYPES = (
    "input",
    "output",
    "cache_read",
    "cache_creation",
    "reasoning",
)
_HISTOGRAM_BUCKETS = {
    "review_duration_seconds": (5, 15, 30, 60, 120, 300, 600, 1200),
    "queue_wait_seconds": (0.1, 0.5, 1, 5, 15, 30, 60, 120, 300, 600),
    "llm_request_duration_seconds": (0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
    "approval_wait_seconds": (60, 300, 900, 3600, 14400, 86400, 604800),
}
_ATTEMPT_TRACE = re.compile(r"[0-9a-f]{32}\.[1-9][0-9]*\.[0-9a-f]{32}\.jsonl\Z")

_HELP = {
    "review_jobs_total": "Durable review jobs reaching each outcome.",
    "review_submissions_total": "Unique durable review submissions.",
    "review_duration_seconds": "Review wall time from durable creation to completion.",
    "webhook_ack_seconds": "GitHub webhook acknowledgement latency.",
    "queue_depth": "Jobs currently waiting in received or queued state.",
    "queue_wait_seconds": "Time from queued state to first worker start.",
    "llm_requests_total": "Canonical LLM request spans by provider and status.",
    "llm_request_duration_seconds": "Canonical LLM request span duration.",
    "llm_tokens_total": "Canonical LLM token usage by bounded token type.",
    "llm_cost_cny_total": "Settled model cost converted with the configured fixed rate.",
    "tool_calls_total": "Canonical tool execution spans by bounded tool and status.",
    "fail_open_total": "Canonical fail-open policy decisions.",
    "degraded_total": "Canonical degraded decisions and telemetry runs.",
    "approval_wait_seconds": "Time from review completion to human decision.",
    "approval_decisions_total": "Durable publication approval decisions.",
    "finding_feedback_total": "Durable finding feedback decisions.",
    "idempotency_hits_total": "Duplicate submissions resolved to existing work.",
    "publisher_calls_total": "Durable publisher attempt outcomes.",
    "trace_completeness_total": "Terminal jobs partitioned by canonical trace completeness.",
    "duplicate_executions_total": "Additional worker attempts beyond the first attempt.",
    "unauthorized_operations_total": "Denied operations grouped into bounded classes.",
    "approval_validation_failures_total": "Approval replay and binding failures.",
}


class MetricsClock(Protocol):
    def monotonic(self) -> float: ...


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _seconds(start: Any, end: Any) -> float | None:
    left = _parse_time(start)
    right = _parse_time(end)
    if left is None or right is None or right < left:
        return None
    return (right - left).total_seconds()


def _labels_text(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    invalid = set(labels) - _ALLOWED_LABELS
    if invalid or set(labels) & _PROHIBITED_LABELS:
        raise ValueError(f"metric labels are not allowed: {sorted(invalid)}")
    values = []
    for key in sorted(labels):
        value = labels[key]
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("metric label values must be bounded strings")
        escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
        values.append(f'{key}="{escaped}"')
    return "{" + ",".join(values) + "}"


def _metric_line(name: str, value: int | float, labels: Mapping[str, str] = {}) -> str:
    numeric = format(value, ".12g") if isinstance(value, float) else str(value)
    return f"{name}{_labels_text(labels)} {numeric}"


def _histogram_lines(name: str, values: Iterable[float], buckets: Iterable[float]) -> list[str]:
    samples = [value for value in values if math.isfinite(value) and value >= 0]
    lines = []
    for bound in buckets:
        count = sum(value <= bound for value in samples)
        lines.append(_metric_line(f"{name}_bucket", count, {"le": format(bound, "g")}))
    lines.append(_metric_line(f"{name}_bucket", len(samples), {"le": "+Inf"}))
    lines.append(_metric_line(f"{name}_sum", float(sum(samples))))
    lines.append(_metric_line(f"{name}_count", len(samples)))
    return lines


class ProductionMetrics:
    """One global exporter over shared durable state and the trace volume."""

    def __init__(self, engine: Engine, trace_dir: Path, *, usd_cny_rate: float | None = None):
        self.engine = engine
        self.trace_dir = Path(trace_dir)
        raw_rate = usd_cny_rate if usd_cny_rate is not None else os.getenv("CRAG_USD_CNY_RATE", "7.2")
        try:
            self.usd_cny_rate = float(raw_rate)
        except (TypeError, ValueError) as exc:
            raise ValueError("CRAG_USD_CNY_RATE must be numeric") from exc
        if not math.isfinite(self.usd_cny_rate) or not 0 < self.usd_cny_rate <= 100:
            raise ValueError("CRAG_USD_CNY_RATE is outside the supported range")

    def increment(self, name: str, labels: Mapping[str, str] | None = None) -> None:
        encoded = json.dumps(labels or {}, sort_keys=True, separators=(",", ":"))
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE production_metric_counters SET value=value+1 "
                    "WHERE metric_name=:name AND labels_json=:labels"
                ),
                {"name": name, "labels": encoded},
            )
            if result.rowcount != 1:
                raise ValueError("metric counter series is not pre-registered")

    def observe_webhook_ack(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("webhook duration must be finite and non-negative")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE production_metric_histogram_totals SET "
                    "sample_count=sample_count+1, sample_sum=sample_sum+:value "
                    "WHERE metric_name='webhook_ack_seconds'"
                ),
                {"value": seconds},
            )
            connection.execute(
                text(
                    "UPDATE production_metric_histogram_buckets SET "
                    "sample_count=sample_count+1 WHERE metric_name='webhook_ack_seconds' "
                    "AND (upper_bound IS NULL OR upper_bound>=:value)"
                ),
                {"value": seconds},
            )

    @staticmethod
    def _provider(value: Any) -> str:
        provider = str(value or "unknown").casefold()
        return provider if provider in _PROVIDERS else "other"

    @staticmethod
    def _tool(value: Any) -> str:
        tool = str(value or "other").casefold()
        tool = _TOOL_ALIASES.get(tool, tool)
        return tool if tool in _TOOLS else "other"

    def _trace_metrics(self, trace_keys: Iterable[str]) -> dict[str, Any]:
        llm: Counter[tuple[str, str]] = Counter()
        tools: Counter[tuple[str, str]] = Counter()
        tokens = Counter({kind: 0 for kind in _TOKEN_TYPES})
        llm_duration: list[float] = []
        fail_open = 0
        degraded = 0
        complete = 0
        incomplete = 0
        for key in trace_keys:
            path = self.trace_dir / key
            try:
                records = load_span_records(path)
            except BaseException:
                incomplete += 1
                continue
            complete += 1
            root_degraded = False
            for record in records:
                attributes = record["attributes"]
                if record["operation"] == "llm.request":
                    provider = self._provider(attributes.get("gen_ai.provider.name"))
                    category = attributes.get("crag.error.category")
                    status = (
                        "ok"
                        if record["status"] == "ok"
                        else "429"
                        if category == "rate_limit"
                        else "5xx"
                        if category in {"provider_5xx", "transient_network"}
                        else "error"
                    )
                    llm[(provider, status)] += 1
                    llm_duration.append(float(record["duration_ms"]) / 1000.0)
                    fields = {
                        "input": "gen_ai.usage.input_tokens",
                        "output": "gen_ai.usage.output_tokens",
                        "cache_read": "gen_ai.usage.cache_read.input_tokens",
                        "cache_creation": "gen_ai.usage.cache_creation.input_tokens",
                        "reasoning": "gen_ai.usage.reasoning_tokens",
                    }
                    for kind, field in fields.items():
                        value = attributes.get(field)
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                            tokens[kind] += value
                elif record["operation"] == "tool.execute":
                    tool = self._tool(attributes.get("gen_ai.tool.name"))
                    status = "ok" if record["status"] == "ok" else "error"
                    tools[(tool, status)] += 1
                if attributes.get("crag.review.fail_open") is True:
                    fail_open += 1
                if attributes.get("crag.review.degraded") is True:
                    degraded += 1
                if attributes.get("crag.telemetry.mode") == "degraded":
                    root_degraded = True
            degraded += int(root_degraded)
        return {
            "llm": llm,
            "tools": tools,
            "tokens": tokens,
            "llm_duration": llm_duration,
            "fail_open": fail_open,
            "degraded": degraded,
            "complete": complete,
            "incomplete": incomplete,
        }

    def _attempt_trace_keys(self) -> list[str]:
        keys: list[str] = []
        try:
            entries = self.trace_dir.iterdir()
        except OSError:
            return keys
        for path in entries:
            if _ATTEMPT_TRACE.fullmatch(path.name) is None:
                continue
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                keys.append(path.name)
        return sorted(keys)

    def render(self) -> str:
        with self.engine.connect() as connection:
            jobs = [dict(row._mapping) for row in connection.execute(text(
                "SELECT state, created_at, queued_at, started_at, completed_at, final_trace_key, "
                "attempt_count "
                "FROM review_jobs"
            ))]
            event_rows = list(connection.execute(text(
                "SELECT event_type, occurred_at FROM metric_events"
            )))
            usage = dict((connection.execute(text(
                "SELECT COALESCE(SUM(input_tokens),0) AS input_tokens, "
                "COALESCE(SUM(output_tokens),0) AS output_tokens, "
                "COALESCE(SUM(cost_microusd),0) AS cost_microusd FROM provider_usage"
            )).one())._mapping)
            approvals = list(connection.execute(text(
                "SELECT pa.created_at, rj.completed_at FROM publish_approvals pa "
                "JOIN review_jobs rj ON rj.id=pa.review_job_id"
            )))
            runtime_counters = list(connection.execute(text(
                "SELECT metric_name, labels_json, value FROM production_metric_counters"
            )))
            webhook_total = connection.execute(text(
                "SELECT sample_count, sample_sum FROM production_metric_histogram_totals "
                "WHERE metric_name='webhook_ack_seconds'"
            )).one()
            webhook_buckets = list(connection.execute(text(
                "SELECT le_text, sample_count FROM production_metric_histogram_buckets "
                "WHERE metric_name='webhook_ack_seconds' ORDER BY bucket_order"
            )))

        events = Counter(str(row._mapping["event_type"]) for row in event_rows)
        states = Counter(str(row["state"]) for row in jobs)
        trace_keys = [
            str(row["final_trace_key"])
            for row in jobs
            if row.get("final_trace_key")
        ]
        trace = self._trace_metrics(self._attempt_trace_keys())
        final_trace = self._trace_metrics(trace_keys)
        lines: list[str] = []

        def family(name: str, kind: str, values: Iterable[str]) -> None:
            lines.append(f"# HELP {name} {_HELP[name]}")
            lines.append(f"# TYPE {name} {kind}")
            lines.extend(values)

        outcomes = {
            "awaiting_approval": events["review.awaiting_approval"],
            "failed": states["failed"],
            "dead_letter": states["dead_letter"],
            "declined": events["publication.approval.rejected"],
            "published": events["publication.published"],
        }
        family("review_jobs_total", "counter", (
            _metric_line("review_jobs_total", value, {"status": status})
            for status, value in outcomes.items()
        ))
        family("review_submissions_total", "counter", [
            _metric_line("review_submissions_total", len(jobs))
        ])
        review_durations = [
            value for row in jobs
            if (value := _seconds(row["created_at"], row["completed_at"])) is not None
        ]
        family("review_duration_seconds", "histogram", _histogram_lines(
            "review_duration_seconds", review_durations, _HISTOGRAM_BUCKETS["review_duration_seconds"]
        ))
        family("webhook_ack_seconds", "histogram", [
            *(
                _metric_line(
                    "webhook_ack_seconds_bucket",
                    int(row._mapping["sample_count"]),
                    {"le": str(row._mapping["le_text"])},
                )
                for row in webhook_buckets
            ),
            _metric_line("webhook_ack_seconds_sum", float(webhook_total._mapping["sample_sum"])),
            _metric_line("webhook_ack_seconds_count", int(webhook_total._mapping["sample_count"])),
        ])
        family("queue_depth", "gauge", [_metric_line("queue_depth", states["received"] + states["queued"])])
        queue_waits = [
            value for row in jobs
            if (value := _seconds(row["queued_at"], row["started_at"])) is not None
        ]
        family("queue_wait_seconds", "histogram", _histogram_lines(
            "queue_wait_seconds", queue_waits, _HISTOGRAM_BUCKETS["queue_wait_seconds"]
        ))
        family("llm_requests_total", "counter", (
            _metric_line("llm_requests_total", value, {"provider": provider, "status": status})
            for (provider, status), value in sorted(trace["llm"].items())
        ))
        family("llm_request_duration_seconds", "histogram", _histogram_lines(
            "llm_request_duration_seconds", trace["llm_duration"], _HISTOGRAM_BUCKETS["llm_request_duration_seconds"]
        ))
        input_total = max(int(usage.get("input_tokens", 0)), trace["tokens"]["input"])
        output_total = max(int(usage.get("output_tokens", 0)), trace["tokens"]["output"])
        trace["tokens"]["input"] = input_total
        trace["tokens"]["output"] = output_total
        family("llm_tokens_total", "counter", (
            _metric_line("llm_tokens_total", trace["tokens"][kind], {"type": kind})
            for kind in _TOKEN_TYPES
        ))
        cost_micro = int(usage.get("cost_microusd", 0))
        family("llm_cost_cny_total", "counter", [
            _metric_line("llm_cost_cny_total", cost_micro * self.usd_cny_rate / 1_000_000.0)
        ])
        family("tool_calls_total", "counter", (
            _metric_line("tool_calls_total", value, {"tool": tool, "status": status})
            for (tool, status), value in sorted(trace["tools"].items())
        ))
        family("fail_open_total", "counter", [_metric_line("fail_open_total", trace["fail_open"])])
        family("degraded_total", "counter", [_metric_line("degraded_total", trace["degraded"])])
        approval_waits = [
            value for row in approvals
            if (value := _seconds(row._mapping["completed_at"], row._mapping["created_at"])) is not None
        ]
        family("approval_wait_seconds", "histogram", _histogram_lines(
            "approval_wait_seconds", approval_waits, _HISTOGRAM_BUCKETS["approval_wait_seconds"]
        ))
        family("approval_decisions_total", "counter", (
            _metric_line("approval_decisions_total", events[f"publication.approval.{decision}"], {"decision": decision})
            for decision in ("approved", "rejected")
        ))
        family("finding_feedback_total", "counter", (
            _metric_line("finding_feedback_total", events[f"finding.feedback.{decision}"], {"decision": decision})
            for decision in ("accepted", "rejected", "uncertain", "fixed", "duplicate")
        ))
        runtime: dict[str, list[str]] = defaultdict(list)
        for row in runtime_counters:
            labels = json.loads(str(row._mapping["labels_json"]))
            name = str(row._mapping["metric_name"])
            runtime[name].append(_metric_line(name, int(row._mapping["value"]), labels))
        for name in ("idempotency_hits_total", "unauthorized_operations_total", "approval_validation_failures_total"):
            family(name, "counter", runtime[name])
        family("publisher_calls_total", "counter", (
            _metric_line(
                "publisher_calls_total",
                events["publication.published" if status == "succeeded" else "publication.failed"],
                {"status": status},
            )
            for status in ("succeeded", "failed")
        ))
        terminal_states = {"awaiting_approval", "declined", "published", "failed", "dead_letter"}
        missing_terminal_trace = sum(
            row["state"] in terminal_states and not row.get("final_trace_key") for row in jobs
        )
        family("trace_completeness_total", "counter", [
            _metric_line(
                "trace_completeness_total",
                final_trace["complete"],
                {"status": "complete"},
            ),
            _metric_line(
                "trace_completeness_total",
                final_trace["incomplete"] + missing_terminal_trace,
                {"status": "incomplete"},
            ),
        ])
        family("duplicate_executions_total", "counter", [
            _metric_line(
                "duplicate_executions_total",
                sum(max(0, int(row.get("attempt_count") or 0) - 1) for row in jobs),
            )
        ])
        return "\n".join(lines) + "\n"


__all__ = ["CONTENT_TYPE", "ProductionMetrics", "_labels_text"]
