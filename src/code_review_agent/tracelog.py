"""Canonical JSONL traces with a bounded legacy event compatibility view.

New files contain ``crag.observability/v1alpha1`` span records.  ``iter_events``
continues to yield the historical ``{t, kind, ...}`` projection through the
documented 0.2.x window, so existing offline cost/replay readers do not need a
flag-day rewrite.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping

from code_review_agent.observability import (
    Clock,
    JsonlFileExporter,
    Span,
    SpanExporter,
    Tracer,
)

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._:/-]+")


def force_utf8() -> None:
    """Windows redirects default to GBK; model output may contain Unicode."""

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _legacy_timestamp(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        return round(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp(), 3)
    except ValueError:
        return 0.0


def _span_token(value: str, fallback: str = "unknown") -> str:
    token = _UNSAFE_NAME.sub("_", value).strip("._")
    return (token or fallback)[:96]


def iter_events(path):
    """Yield historical event dicts from either legacy or canonical JSONL."""

    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed trace JSONL line {line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"trace JSONL line {line_number} is not an object")
        if record.get("schema_version") != "crag.observability/v1alpha1":
            yield record
            continue
        events = record.get("events", [])
        if not isinstance(events, list):
            raise ValueError(f"canonical trace line {line_number} has invalid events")
        for event in events:
            if not isinstance(event, dict):
                continue
            name = event.get("name")
            if not isinstance(name, str) or not name.startswith("legacy."):
                continue
            attributes = event.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {}
            yield {
                "t": _legacy_timestamp(event.get("time")),
                "kind": name.removeprefix("legacy."),
                **attributes,
            }


class _NoopSpan:
    ended = False

    def set_attribute(self, name: str, value: Any) -> None:
        del name, value

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        del attributes

    def add_event(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        del name, attributes

    def end(self, **kwargs) -> dict[str, Any]:
        del kwargs
        self.ended = True
        return {}


@contextmanager
def tspan(
    trace,
    name: str,
    *,
    operation: str,
    kind: str = "INTERNAL",
    attributes: Mapping[str, Any] | None = None,
):
    """Enter a canonical span when supported, otherwise yield a no-op fake."""

    if trace is None or not hasattr(trace, "span"):
        yield _NoopSpan()
        return
    with trace.span(
        name,
        operation=operation,
        kind=kind,
        attributes=attributes,
    ) as span:
        yield span


class Trace:
    """One run-wide canonical trace plus the historical ``event`` adapter."""

    def __init__(
        self,
        path,
        *,
        run_id: str | None = None,
        source_commit: str | None = None,
        optional_exporters: Iterable[SpanExporter] = (),
        root_attributes: Mapping[str, Any] | None = None,
        clock: Clock | None = None,
    ):
        self.path = Path(path)
        self._local = JsonlFileExporter(self.path)
        try:
            self._tracer = Tracer(
                self._local,
                run_id=run_id,
                source_commit=source_commit,
                optional_exporters=optional_exporters,
                root_attributes=root_attributes,
                clock=clock,
            )
        except BaseException:
            try:
                self._local.close()
            except BaseException:
                pass
            raise
        self._provider = "unknown"
        self._model = "unknown"
        self._closed = False

    @property
    def trace_id(self) -> str:
        return self._tracer.trace_id

    @property
    def run_id(self) -> str:
        return self._tracer.run_id

    @property
    def telemetry_mode(self) -> str:
        return self._tracer.telemetry_mode

    def span(
        self,
        name: str,
        *,
        operation: str,
        kind: str = "INTERNAL",
        attributes: Mapping[str, Any] | None = None,
    ) -> Span:
        current = self._tracer.current_span()
        parent_span_id = (
            current.span_id if current is not None else self._tracer.root_span.span_id
        )
        return self._tracer.start_span(
            name,
            operation=operation,
            kind=kind,
            parent_span_id=parent_span_id,
            attributes=attributes,
        )

    def current_span(self) -> Span | None:
        return self._tracer.current_span()

    def event(self, kind: str, **data) -> None:
        if self._closed:
            raise RuntimeError("trace is closed")
        if kind == "meta":
            provider = data.get("provider")
            model = data.get("model")
            if isinstance(provider, str) and provider:
                self._provider = provider.casefold()
            if isinstance(model, str) and model:
                self._model = model
            self._tracer.root_span.set_attributes(
                {
                    "gen_ai.provider.name": self._provider,
                    "gen_ai.request.model": self._model,
                }
            )
        name, operation, span_kind, attributes = self._legacy_span_spec(kind, data)
        current = self.current_span()
        if current is not None and current.operation == operation and operation in {
            "llm.request",
            "tool.execute",
            "sandbox.command",
            "checkpoint",
            "policy.decision",
            "agent.stage",
        }:
            current.set_attributes(attributes)
            current.add_event(f"legacy.{kind}", data)
            return
        with self.span(
            name,
            operation=operation,
            kind=span_kind,
            attributes=attributes,
        ) as span:
            span.add_event(f"legacy.{kind}", data)

    def _legacy_span_spec(
        self,
        event_kind: str,
        data: Mapping[str, Any],
    ) -> tuple[str, str, str, dict[str, Any]]:
        if event_kind == "llm_response":
            attributes: dict[str, Any] = {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": self._provider,
                "gen_ai.request.model": self._model,
            }
            field_map = {
                "tokens_in": "gen_ai.usage.input_tokens",
                "tokens_out": "gen_ai.usage.output_tokens",
                "cache_hit": "gen_ai.usage.cache_read.input_tokens",
                "cache_miss": "gen_ai.usage.cache_creation.input_tokens",
            }
            for legacy_name, semantic_name in field_map.items():
                value = data.get(legacy_name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    attributes[semantic_name] = value
            return (
                f"chat {_span_token(self._model)}",
                "llm.request",
                "CLIENT",
                attributes,
            )
        if event_kind == "tool":
            tool = data.get("tool")
            tool_name = _span_token(tool) if isinstance(tool, str) and tool else "unknown"
            attributes = {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": tool_name,
                "gen_ai.tool.type": "function",
            }
            return (
                f"execute_tool {tool_name}",
                "tool.execute",
                "INTERNAL",
                attributes,
            )
        if event_kind == "checkpoint":
            operation = data.get("operation")
            operation_name = _span_token(operation, "save") if isinstance(operation, str) else "save"
            return (
                f"crag.checkpoint {operation_name}",
                "checkpoint",
                "INTERNAL",
                {"crag.checkpoint.operation": operation_name},
            )
        if event_kind in {"policy", "approval"}:
            operation = data.get("operation")
            operation_name = (
                _span_token(operation, event_kind)
                if isinstance(operation, str)
                else event_kind
            )
            return (
                f"crag.policy {operation_name}",
                "policy.decision",
                "INTERNAL",
                {"crag.policy.operation": operation_name},
            )
        if event_kind == "sandbox":
            command = data.get("command")
            command_name = _span_token(command, "command") if isinstance(command, str) else "command"
            return (
                f"crag.sandbox {command_name}",
                "sandbox.command",
                "INTERNAL",
                {"crag.sandbox.command": command_name},
            )
        if event_kind in {
            "finder2_failed",
            "tiebreak_failed",
            "verifier_fail_open",
            "verifier_pass_failed",
        } or (event_kind == "verdicts" and data.get("degraded") is True):
            fail_open = event_kind == "verifier_fail_open"
            decision = "fail_open" if fail_open else "degraded"
            return (
                f"crag.policy {_span_token(event_kind)}",
                "policy.decision",
                "INTERNAL",
                {
                    "crag.policy.operation": _span_token(event_kind),
                    "crag.policy.decision": decision,
                    "crag.review.degraded": True,
                    "crag.review.fail_open": fail_open,
                },
            )
        if event_kind == "review":
            return (
                "crag.stage review_terminal",
                "agent.stage",
                "INTERNAL",
                {
                    "crag.stage.name": "review_terminal",
                    "crag.review.terminal": "completed",
                },
            )
        if event_kind.startswith("parallel_stage_"):
            stage = data.get("stage")
            stage_name = _span_token(stage) if isinstance(stage, str) else "unknown"
            return (
                f"crag.stage {stage_name}",
                "agent.stage",
                "INTERNAL",
                {"crag.stage.name": stage_name},
            )
        return (
            f"crag.event {_span_token(event_kind)}",
            "legacy.event",
            "INTERNAL",
            {"crag.legacy.kind": event_kind},
        )

    def close(
        self,
        *,
        status: str = "ok",
        error_type: str | None = None,
        error_category: str | None = None,
    ) -> None:
        if self._closed:
            return
        if error_type is not None:
            status = "error"
        try:
            self._tracer.close(
                status=status,
                error_type=error_type,
                error_category=error_category,
            )
        finally:
            self._closed = True


def tev(trace, kind: str, **data) -> None:
    """Emit a compatibility event if tracing is on."""

    if trace is not None:
        trace.event(kind, **data)
