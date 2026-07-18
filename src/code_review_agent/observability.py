"""Offline-first canonical trace/span records for Review and Repair runs.

This module intentionally has no OpenTelemetry SDK dependency.  It implements
the frozen ``crag.observability/v1alpha1`` storage profile and keeps the
Development GenAI field mapping behind a project-owned adapter.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from copy import deepcopy
from datetime import datetime, timezone
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
from threading import Lock, RLock
import time
from typing import Any, Iterable, Literal, Mapping, Protocol
from uuid import uuid4

from code_review_agent.redaction import (
    MAX_ATTRIBUTES,
    REDACTION_POLICY_VERSION,
    contains_forbidden_content,
    sanitize_attributes,
)


SCHEMA_VERSION = "crag.observability/v1alpha1"
MAX_RECORD_BYTES = 65_536
MAX_EVENTS_PER_SPAN = 128
MAX_LINKS_PER_SPAN = 32
MAX_LOCAL_FILE_BYTES = 67_108_864
_ENVELOPE_ATTRIBUTE_SLOTS = 8

_TRACE_ID = re.compile(r"[0-9a-f]{32}\Z")
_SPAN_ID = re.compile(r"[0-9a-f]{16}\Z")
_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_RUN_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9_-])?\Z")
_NAME = re.compile(r"[\x20-\x7e]{1,128}\Z")
_STATUS = frozenset({"unset", "ok", "error"})
_SPAN_KIND = frozenset({"INTERNAL", "CLIENT"})
_ERROR_CATEGORIES = frozenset(
    {
        "auth",
        "rate_limit",
        "timeout",
        "connection",
        "provider",
        "invalid_response",
        "budget_exhausted",
        "policy_denied",
        "approval_rejected",
        "sandbox_violation",
        "telemetry_write",
        "telemetry_export",
        "redaction_failure",
        "internal",
    }
)


class TelemetryError(RuntimeError):
    """Base class for observability failures."""


class TelemetryValidationError(TelemetryError, ValueError):
    """A canonical record or trace violates the frozen profile."""


class TelemetryWriteError(TelemetryError, OSError):
    """The mandatory local audit sink could not persist evidence."""


class SpanLifecycleError(TelemetryError):
    """A caller attempted to mutate or end a span illegally."""


def error_category_for_exception(exc: BaseException) -> str:
    """Classify by exception type only; messages may contain untrusted data."""

    name = type(exc).__name__.casefold()
    if "authentication" in name or name in {"permissionerror"}:
        return "auth"
    if "ratelimit" in name:
        return "rate_limit"
    if "timeout" in name:
        return "timeout"
    if "connection" in name:
        return "connection"
    if "budget" in name:
        return "budget_exhausted"
    if "approval" in name:
        return "approval_rejected"
    if "sandbox" in name or "quarantin" in name:
        return "sandbox_violation"
    if "telemetrywrite" in name:
        return "telemetry_write"
    if "telemetry" in name and "export" in name:
        return "telemetry_export"
    if "api" in name or "provider" in name:
        return "provider"
    return "internal"


class Clock(Protocol):
    def time_ns(self) -> int: ...

    def monotonic_ns(self) -> int: ...


class SystemClock:
    def time_ns(self) -> int:
        return time.time_ns()

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class SpanExporter(Protocol):
    def export(self, record: Mapping[str, Any]) -> None: ...

    def close(self) -> None: ...


def _canonical_json(record: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TelemetryValidationError(f"telemetry is not canonical JSON: {exc}") from exc


def _utc_text(timestamp_ns: int) -> str:
    if isinstance(timestamp_ns, bool) or not isinstance(timestamp_ns, int):
        raise TelemetryValidationError("timestamp must be integer nanoseconds")
    return (
        datetime.fromtimestamp(timestamp_ns / 1_000_000_000, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TelemetryValidationError(f"{field} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise TelemetryValidationError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise TelemetryValidationError(f"{field} must be UTC")
    return parsed


def _new_nonzero_hex(nbytes: int) -> str:
    while True:
        value = secrets.token_hex(nbytes)
        if set(value) != {"0"}:
            return value


def _runtime_version() -> str:
    try:
        return importlib.metadata.version("code-review-agent")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def discover_source_commit() -> str:
    """Return the agent checkout commit, or ``unknown`` in installed builds."""

    configured = os.environ.get("CRAG_SOURCE_COMMIT", "").strip().lower()
    if configured:
        return configured if _GIT_OBJECT_ID.fullmatch(configured) else "unknown"
    checkout = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="ascii",
            errors="replace",
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    candidate = completed.stdout.strip().lower()
    return candidate if completed.returncode == 0 and _GIT_OBJECT_ID.fullmatch(candidate) else "unknown"


class JsonlFileExporter:
    """Thread-safe bounded local JSONL sink with restrictive file creation."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_file_bytes: int = MAX_LOCAL_FILE_BYTES,
    ):
        if (
            isinstance(max_file_bytes, bool)
            or not isinstance(max_file_bytes, int)
            or max_file_bytes <= 0
            or max_file_bytes > MAX_LOCAL_FILE_BYTES
        ):
            raise ValueError("max_file_bytes must be within the frozen local file cap")
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TelemetryWriteError("cannot initialize local audit directory") from exc
        # A caller must choose a fresh file for each logical invocation.
        # Refusing an existing path preserves prior audit evidence instead of
        # silently truncating or mixing multiple root traces.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor: int | None = None
        try:
            descriptor = os.open(self.path, flags, 0o600)
            self._file = os.fdopen(descriptor, "wb")
            descriptor = None
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise TelemetryWriteError("cannot initialize local audit sink") from exc
        self._max_file_bytes = max_file_bytes
        self._written = 0
        self._lock = Lock()
        self._closed = False

    def export(self, record: Mapping[str, Any]) -> None:
        validate_span_record(record)
        payload = _canonical_json(record)
        if len(payload) > MAX_RECORD_BYTES:
            raise TelemetryValidationError("telemetry record exceeds 65,536 bytes")
        line = payload + b"\n"
        with self._lock:
            if self._closed:
                raise TelemetryWriteError("local audit sink is closed")
            if self._written + len(line) > self._max_file_bytes:
                raise TelemetryWriteError("local audit file exceeds its frozen byte cap")
            try:
                self._file.write(line)
                self._file.flush()
            except OSError as exc:
                raise TelemetryWriteError("cannot persist local audit record") from exc
            self._written += len(line)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()
            except OSError as exc:
                raise TelemetryWriteError("cannot close local audit sink") from exc


class InMemoryExporter:
    """Deterministic fake exporter for offline tests and aggregation."""

    def __init__(self):
        self.records: list[dict[str, Any]] = []
        self._lock = Lock()
        self.closed = False

    def export(self, record: Mapping[str, Any]) -> None:
        validate_span_record(record)
        with self._lock:
            if self.closed:
                raise TelemetryWriteError("in-memory exporter is closed")
            self.records.append(deepcopy(dict(record)))

    def close(self) -> None:
        with self._lock:
            self.closed = True


class FailingExporter:
    """A bounded fake remote exporter that always fails without I/O."""

    def __init__(self, error_type: str = "SyntheticExportError"):
        self.error_type = error_type
        self.attempts = 0
        self.closed = False

    def export(self, record: Mapping[str, Any]) -> None:
        del record
        self.attempts += 1
        raise TelemetryWriteError(self.error_type)

    def close(self) -> None:
        self.closed = True


class Span:
    """Mutable span builder; its exported snapshot is immutable."""

    def __init__(
        self,
        tracer: "Tracer",
        *,
        name: str,
        kind: str,
        operation: str,
        parent_span_id: str | None,
        attributes: Mapping[str, Any] | None,
    ):
        if not _NAME.fullmatch(name):
            raise TelemetryValidationError("span name must be bounded printable ASCII")
        if not _NAME.fullmatch(operation):
            raise TelemetryValidationError("operation must be bounded printable ASCII")
        if contains_forbidden_content({"name": name, "operation": operation}):
            raise TelemetryValidationError("span identity contains forbidden content")
        if kind not in _SPAN_KIND:
            raise TelemetryValidationError(f"unsupported span kind: {kind!r}")
        if parent_span_id is not None and (
            not _SPAN_ID.fullmatch(parent_span_id) or set(parent_span_id) == {"0"}
        ):
            raise TelemetryValidationError("parent_span_id is invalid")
        self.tracer = tracer
        self.trace_id = tracer.trace_id
        self.span_id = _new_nonzero_hex(8)
        self.parent_span_id = parent_span_id
        self.name = name
        self.kind = kind
        self.operation = operation
        self._start_time_ns = tracer.clock.time_ns()
        self._start_monotonic_ns = tracer.clock.monotonic_ns()
        self._end_time_ns: int | None = None
        self._end_monotonic_ns: int | None = None
        self._status = "unset"
        self._attributes: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self._redaction_count = 0
        self._omitted_count = 0
        self._truncated = False
        self._token: Token[Span | None] | None = None
        self._lock = RLock()
        self.set_attributes(attributes or {})
        tracer._register(self)

    @property
    def ended(self) -> bool:
        return self._end_time_ns is not None

    def __enter__(self) -> "Span":
        with self._lock:
            if self.ended or self._token is not None:
                raise SpanLifecycleError("span cannot be entered in its current state")
            self._token = self.tracer._current.set(self)
            return self

    def __exit__(self, exc_type, exc, traceback) -> Literal[False]:
        del traceback
        try:
            try:
                if self.ended:
                    pass
                elif exc is not None:
                    self.end(
                        status="error",
                        error_type=exc_type.__name__ if exc_type else "Exception",
                        error_category=error_category_for_exception(exc),
                    )
                else:
                    self.end(status="ok")
            except BaseException as telemetry_exc:
                if exc is None:
                    raise
                add_note = getattr(exc, "add_note", None)
                if callable(add_note):
                    add_note(
                        "telemetry span finalization failed: "
                        f"{type(telemetry_exc).__name__}"
                    )
        finally:
            if self._token is not None:
                self.tracer._current.reset(self._token)
                self._token = None
        return False

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        with self._lock:
            if self.ended:
                raise SpanLifecycleError("ended span attributes are immutable")
            sanitized = sanitize_attributes(attributes)
            for key, value in sanitized.value.items():
                if (
                    len(self._attributes) >= MAX_ATTRIBUTES - _ENVELOPE_ATTRIBUTE_SLOTS
                    and key not in self._attributes
                ):
                    self._omitted_count += 1
                    self._truncated = True
                    continue
                self._attributes[key] = value
            self._redaction_count += sanitized.redaction_count
            self._omitted_count += sanitized.omitted_count
            self._truncated = self._truncated or sanitized.truncated

    def set_attribute(self, name: str, value: Any) -> None:
        self.set_attributes({name: value})

    def add_event(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        if not _NAME.fullmatch(name):
            raise TelemetryValidationError("event name must be bounded printable ASCII")
        with self._lock:
            if self.ended:
                raise SpanLifecycleError("ended span events are immutable")
            if len(self._events) >= MAX_EVENTS_PER_SPAN:
                self._omitted_count += 1
                self._truncated = True
                return
            sanitized = sanitize_attributes(attributes)
            self._redaction_count += sanitized.redaction_count
            self._omitted_count += sanitized.omitted_count
            self._truncated = self._truncated or sanitized.truncated
            self._events.append(
                {
                    "name": name,
                    "time": _utc_text(self.tracer.clock.time_ns()),
                    "attributes": sanitized.value,
                }
            )

    def end(
        self,
        *,
        status: str = "ok",
        error_type: str | None = None,
        error_category: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self.ended:
                raise SpanLifecycleError("span has already ended")
            if status not in _STATUS:
                raise TelemetryValidationError(f"unsupported span status: {status!r}")
            if status == "error":
                if not error_type:
                    raise TelemetryValidationError("error spans require error.type")
                category = error_category or "internal"
                if category not in _ERROR_CATEGORIES:
                    raise TelemetryValidationError("unsupported crag.error.category")
                self._set_terminal_error_attributes(
                    {
                        "error.type": error_type,
                        "crag.error.category": category,
                    }
                )
            elif error_type is not None or error_category is not None:
                raise TelemetryValidationError("non-error span cannot carry error metadata")
            self._status = status
            self._end_time_ns = self.tracer.clock.time_ns()
            self._end_monotonic_ns = self.tracer.clock.monotonic_ns()
            validation_error: TelemetryValidationError | None = None
            try:
                record = self._snapshot()
            except TelemetryValidationError as exc:
                validation_error = exc
                record = self._fallback_snapshot(exc)
        self.tracer._finish(self, record)
        if validation_error is not None:
            raise validation_error
        return record

    def _set_terminal_error_attributes(self, attributes: Mapping[str, Any]) -> None:
        """Prioritize required error evidence over caller-supplied attributes."""

        sanitized = sanitize_attributes(attributes)
        user_attribute_limit = MAX_ATTRIBUTES - _ENVELOPE_ATTRIBUTE_SLOTS
        new_keys = [key for key in sanitized.value if key not in self._attributes]
        protected = {
            "agent.run": {
                "gen_ai.operation.name",
                "gen_ai.agent.name",
                "gen_ai.agent.version",
            },
            "llm.request": {
                "gen_ai.operation.name",
                "gen_ai.provider.name",
                "gen_ai.request.model",
            },
            "tool.execute": {
                "gen_ai.operation.name",
                "gen_ai.tool.name",
            },
        }.get(self.operation, set())
        while len(self._attributes) + len(new_keys) > user_attribute_limit:
            evicted = next(
                key for key in reversed(self._attributes) if key not in protected
            )
            self._attributes.pop(evicted)
            self._omitted_count += 1
            self._truncated = True
        self._attributes.update(sanitized.value)
        self._redaction_count += sanitized.redaction_count
        self._omitted_count += sanitized.omitted_count
        self._truncated = self._truncated or sanitized.truncated

    def _fallback_snapshot(self, exc: TelemetryValidationError) -> dict[str, Any]:
        """Build bounded local evidence for an invalid terminal snapshot."""

        self.tracer.telemetry_mode = "degraded"
        end_time_ns = max(self._end_time_ns or self._start_time_ns, self._start_time_ns)
        end_monotonic_ns = max(
            self._end_monotonic_ns or self._start_monotonic_ns,
            self._start_monotonic_ns,
        )
        self._end_time_ns = end_time_ns
        self._end_monotonic_ns = end_monotonic_ns
        self._status = "error"
        attributes: dict[str, Any] = {
            "error.type": type(exc).__name__,
            "crag.error.category": "internal",
            "crag.telemetry.degraded_reason": "span_validation_failed",
            "crag.telemetry.failed_operation": self.operation,
            "crag.schema.version": SCHEMA_VERSION,
            "crag.run.id": self.tracer.run_id,
            "crag.source.commit": self.tracer.source_commit,
            "crag.redaction.policy_version": REDACTION_POLICY_VERSION,
            "crag.redaction.count": self._redaction_count,
            "crag.redaction.omitted_count": (
                self._omitted_count + len(self._attributes) + len(self._events)
            ),
            "crag.redaction.truncated": True,
            "crag.telemetry.mode": "degraded",
        }
        attributes.update(
            {
                "agent.run": {
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": "code-review-agent",
                    "gen_ai.agent.version": self.tracer.runtime_version,
                },
                "llm.request": {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "unknown",
                    "gen_ai.request.model": "unknown",
                },
                "tool.execute": {
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": "unknown",
                },
            }.get(self.operation, {})
        )
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "span",
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "run_id": self.tracer.run_id,
            "name": self.name,
            "kind": self.kind,
            "operation": self.operation,
            "start_time": _utc_text(self._start_time_ns),
            "end_time": _utc_text(end_time_ns),
            "duration_ms": (end_monotonic_ns - self._start_monotonic_ns) / 1_000_000,
            "status": "error",
            "source_commit": self.tracer.source_commit,
            "runtime_version": self.tracer.runtime_version,
            "redaction_policy_version": REDACTION_POLICY_VERSION,
            "attributes": attributes,
            "events": [
                {
                    "name": "crag.telemetry.span_validation_failed",
                    "time": _utc_text(end_time_ns),
                    "attributes": {
                        "error.type": type(exc).__name__,
                        "crag.error.category": "internal",
                    },
                }
            ],
            "links": [],
        }
        validate_span_record(record)
        return record

    def _snapshot(self) -> dict[str, Any]:
        if self._end_time_ns is None or self._end_monotonic_ns is None:
            raise SpanLifecycleError("cannot snapshot an open span")
        duration_ns = self._end_monotonic_ns - self._start_monotonic_ns
        if duration_ns < 0:
            raise TelemetryValidationError("span monotonic duration is negative")
        attributes = dict(self._attributes)
        attributes.update(
            {
                "crag.schema.version": SCHEMA_VERSION,
                "crag.run.id": self.tracer.run_id,
                "crag.source.commit": self.tracer.source_commit,
                "crag.redaction.policy_version": REDACTION_POLICY_VERSION,
                "crag.redaction.count": self._redaction_count,
                "crag.redaction.omitted_count": self._omitted_count,
                "crag.redaction.truncated": self._truncated,
                "crag.telemetry.mode": self.tracer.telemetry_mode,
            }
        )
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "span",
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "run_id": self.tracer.run_id,
            "name": self.name,
            "kind": self.kind,
            "operation": self.operation,
            "start_time": _utc_text(self._start_time_ns),
            "end_time": _utc_text(self._end_time_ns),
            "duration_ms": duration_ns / 1_000_000,
            "status": self._status,
            "source_commit": self.tracer.source_commit,
            "runtime_version": self.tracer.runtime_version,
            "redaction_policy_version": REDACTION_POLICY_VERSION,
            "attributes": attributes,
            "events": deepcopy(self._events),
            "links": [],
        }
        validate_span_record(record)
        return record


class Tracer:
    """Own one run-wide trace and export ended spans exactly once."""

    def __init__(
        self,
        primary_exporter: SpanExporter,
        *,
        run_id: str | None = None,
        source_commit: str | None = None,
        clock: Clock | None = None,
        optional_exporters: Iterable[SpanExporter] = (),
        root_attributes: Mapping[str, Any] | None = None,
    ):
        selected_run_id = run_id or uuid4().hex
        if not _RUN_ID.fullmatch(selected_run_id):
            raise TelemetryValidationError("run_id is not a bounded stable identifier")
        selected_commit = (source_commit or discover_source_commit()).lower()
        if selected_commit != "unknown" and not _GIT_OBJECT_ID.fullmatch(selected_commit):
            raise TelemetryValidationError("source_commit must be a Git object id or unknown")
        self.run_id = selected_run_id
        self.source_commit = selected_commit
        self.runtime_version = _runtime_version()
        self.clock = clock or SystemClock()
        self.trace_id = _new_nonzero_hex(16)
        self.primary_exporter = primary_exporter
        self.optional_exporters = tuple(optional_exporters)
        self.telemetry_mode = "degraded" if selected_commit == "unknown" else "normal"
        self._current: ContextVar[Span | None] = ContextVar(
            f"crag-current-span-{self.trace_id}", default=None
        )
        self._open: dict[str, Span] = {}
        self._finished_ids: set[str] = set()
        self._lock = RLock()
        self._optional_export_lock = Lock()
        self._disabled_optional_exporters: set[int] = set()
        self._closed = False
        self._recording_export_failure = False
        root = self.start_span(
            "invoke_agent code-review-agent",
            kind="INTERNAL",
            operation="agent.run",
            parent_span_id=None,
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "code-review-agent",
                "gen_ai.agent.version": self.runtime_version,
                **dict(root_attributes or {}),
            },
        )
        self.root_span = root
        root.__enter__()
        if selected_commit == "unknown":
            root.add_event(
                "crag.telemetry.degraded",
                {"crag.telemetry.degraded_reason": "source_commit_unknown"},
            )

    def start_span(
        self,
        name: str,
        *,
        kind: str = "INTERNAL",
        operation: str,
        parent_span_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Span:
        with self._lock:
            if self._closed:
                raise SpanLifecycleError("tracer is closed")
            if parent_span_id is None:
                current = self._current.get()
                if current is not None:
                    parent_span_id = current.span_id
            return Span(
                self,
                name=name,
                kind=kind,
                operation=operation,
                parent_span_id=parent_span_id,
                attributes=attributes,
            )

    def current_span(self) -> Span | None:
        return self._current.get()

    def _register(self, span: Span) -> None:
        with self._lock:
            if span.span_id in self._open or span.span_id in self._finished_ids:
                raise TelemetryValidationError("duplicate span id")
            self._open[span.span_id] = span

    def _finish(self, span: Span, record: Mapping[str, Any]) -> None:
        with self._lock:
            if self._open.pop(span.span_id, None) is None:
                raise SpanLifecycleError("span is not registered as open")
            if span.span_id in self._finished_ids:
                raise SpanLifecycleError("span was already exported")
            self._finished_ids.add(span.span_id)
        if span is self.root_span:
            for exporter in self.optional_exporters:
                exc = self._export_optional(exporter, record)
                if exc is not None:
                    self._mark_ended_root_export_failure(record, exc)
            self.primary_exporter.export(record)
            return
        self.primary_exporter.export(record)
        if self._recording_export_failure:
            return
        for exporter in self.optional_exporters:
            exc = self._export_optional(exporter, record)
            if exc is not None:
                self._record_optional_failure(exc)

    def _export_optional(
        self,
        exporter: SpanExporter,
        record: Mapping[str, Any],
    ) -> BaseException | None:
        """Try once and circuit-break the exporter after its first failure."""

        identity = id(exporter)
        with self._optional_export_lock:
            if identity in self._disabled_optional_exporters:
                return None
            try:
                exporter.export(deepcopy(record))
            except BaseException as exc:
                self._disabled_optional_exporters.add(identity)
                return exc
        return None

    def _mark_ended_root_export_failure(
        self,
        record: Mapping[str, Any],
        exc: BaseException,
    ) -> None:
        """Add bounded local evidence before the ended root is persisted."""

        self.telemetry_mode = "degraded"
        if not isinstance(record, dict):
            raise TelemetryValidationError("root record is not mutable")
        attributes = record["attributes"]
        events = record["events"]
        if not isinstance(attributes, dict) or not isinstance(events, list):
            raise TelemetryValidationError("root record has invalid telemetry fields")
        attributes["crag.telemetry.mode"] = "degraded"
        if len(events) < MAX_EVENTS_PER_SPAN:
            events.append(
                {
                    "name": "crag.telemetry.export_failed",
                    "time": record["end_time"],
                    "attributes": {
                        "error.type": type(exc).__name__,
                        "crag.error.category": "telemetry_export",
                        "crag.telemetry.degraded_reason": "optional_export_failed",
                    },
                }
            )
        validate_span_record(record)

    def _record_optional_failure(self, exc: BaseException) -> None:
        self.telemetry_mode = "degraded"
        root = self.root_span
        if not root.ended:
            root.set_attribute("crag.telemetry.mode", "degraded")
            root.add_event(
                "crag.telemetry.export_failed",
                {
                    "error.type": type(exc).__name__,
                    "crag.error.category": "telemetry_export",
                    "crag.telemetry.degraded_reason": "optional_export_failed",
                },
            )
            if self._recording_export_failure:
                return
            self._recording_export_failure = True
            try:
                failure = Span(
                    self,
                    name="crag.telemetry.export",
                    kind="INTERNAL",
                    operation="telemetry.export",
                    parent_span_id=root.span_id,
                    attributes={
                        "crag.telemetry.mode": "degraded",
                        "crag.telemetry.degraded_reason": "optional_export_failed",
                    },
                )
                failure.end(
                    status="error",
                    error_type=type(exc).__name__,
                    error_category="telemetry_export",
                )
            finally:
                self._recording_export_failure = False

    def close(
        self,
        *,
        status: str = "ok",
        error_type: str | None = None,
        error_category: str | None = None,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            non_root = [span_id for span_id in self._open if span_id != self.root_span.span_id]
            if non_root:
                raise SpanLifecycleError("cannot close tracer with open child spans")
        try:
            if not self.root_span.ended:
                self.root_span.end(
                    status=status,
                    error_type=error_type,
                    error_category=error_category,
                )
            if self.root_span._token is not None:
                self._current.reset(self.root_span._token)
                self.root_span._token = None
            self.primary_exporter.close()
        finally:
            for exporter in self.optional_exporters:
                try:
                    exporter.close()
                except BaseException:
                    pass
            self._closed = True


def validate_span_record(record: Mapping[str, Any]) -> None:
    """Validate one ended span without requiring its parent record yet."""

    required = {
        "schema_version",
        "record_type",
        "trace_id",
        "span_id",
        "parent_span_id",
        "run_id",
        "name",
        "kind",
        "operation",
        "start_time",
        "end_time",
        "duration_ms",
        "status",
        "source_commit",
        "runtime_version",
        "redaction_policy_version",
        "attributes",
        "events",
        "links",
    }
    if set(record) != required:
        missing = sorted(required - set(record))
        extra = sorted(set(record) - required)
        raise TelemetryValidationError(
            f"span record fields mismatch; missing={missing}, extra={extra}"
        )
    if record["schema_version"] != SCHEMA_VERSION or record["record_type"] != "span":
        raise TelemetryValidationError("unsupported telemetry schema or record type")
    trace_id = record["trace_id"]
    span_id = record["span_id"]
    parent_id = record["parent_span_id"]
    if not isinstance(trace_id, str) or not _TRACE_ID.fullmatch(trace_id) or set(trace_id) == {"0"}:
        raise TelemetryValidationError("invalid trace_id")
    if not isinstance(span_id, str) or not _SPAN_ID.fullmatch(span_id) or set(span_id) == {"0"}:
        raise TelemetryValidationError("invalid span_id")
    if parent_id is not None and (
        not isinstance(parent_id, str)
        or not _SPAN_ID.fullmatch(parent_id)
        or set(parent_id) == {"0"}
        or parent_id == span_id
    ):
        raise TelemetryValidationError("invalid parent_span_id")
    if not isinstance(record["run_id"], str) or not _RUN_ID.fullmatch(record["run_id"]):
        raise TelemetryValidationError("invalid run_id")
    if not isinstance(record["name"], str) or not _NAME.fullmatch(record["name"]):
        raise TelemetryValidationError("invalid span name")
    if not isinstance(record["operation"], str) or not _NAME.fullmatch(record["operation"]):
        raise TelemetryValidationError("invalid operation")
    if record["kind"] not in _SPAN_KIND or record["status"] not in _STATUS:
        raise TelemetryValidationError("invalid span kind or status")
    start = _parse_utc(record["start_time"], "start_time")
    end = _parse_utc(record["end_time"], "end_time")
    if end < start:
        raise TelemetryValidationError("span end precedes start")
    duration = record["duration_ms"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or duration < 0
    ):
        raise TelemetryValidationError("duration_ms must be finite and non-negative")
    source_commit = record["source_commit"]
    if source_commit != "unknown" and (
        not isinstance(source_commit, str) or not _GIT_OBJECT_ID.fullmatch(source_commit)
    ):
        raise TelemetryValidationError("invalid source_commit")
    if record["redaction_policy_version"] != REDACTION_POLICY_VERSION:
        raise TelemetryValidationError("unexpected redaction policy version")
    if (
        not isinstance(record["runtime_version"], str)
        or not _NAME.fullmatch(record["runtime_version"])
    ):
        raise TelemetryValidationError("invalid runtime_version")
    attributes = record["attributes"]
    if not isinstance(attributes, dict) or len(attributes) > MAX_ATTRIBUTES:
        raise TelemetryValidationError("span attributes exceed their cap")
    mode = attributes.get("crag.telemetry.mode")
    if mode not in {"normal", "degraded"}:
        raise TelemetryValidationError("invalid telemetry mode")
    if source_commit == "unknown" and mode != "degraded":
        raise TelemetryValidationError("unknown source commit requires degraded telemetry")
    envelope = {
        "crag.schema.version": SCHEMA_VERSION,
        "crag.run.id": record["run_id"],
        "crag.source.commit": source_commit,
        "crag.redaction.policy_version": REDACTION_POLICY_VERSION,
        "crag.telemetry.mode": mode,
    }
    if any(attributes.get(key) != value for key, value in envelope.items()):
        raise TelemetryValidationError("span envelope attributes do not match the record")
    required_attributes = {
        "agent.run": (
            "gen_ai.operation.name",
            "gen_ai.agent.name",
            "gen_ai.agent.version",
        ),
        "llm.request": (
            "gen_ai.operation.name",
            "gen_ai.provider.name",
            "gen_ai.request.model",
        ),
        "tool.execute": (
            "gen_ai.operation.name",
            "gen_ai.tool.name",
        ),
    }.get(record["operation"], ())
    if any(
        not isinstance(attributes.get(name), str) or not attributes[name]
        for name in required_attributes
    ):
        raise TelemetryValidationError(
            f"{record['operation']} span lacks required semantic attributes"
        )
    for count_name in ("crag.redaction.count", "crag.redaction.omitted_count"):
        count = attributes.get(count_name)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise TelemetryValidationError("invalid redaction counter")
    if not isinstance(attributes.get("crag.redaction.truncated"), bool):
        raise TelemetryValidationError("invalid redaction truncation flag")
    error_type = attributes.get("error.type")
    if record["status"] == "error":
        if not isinstance(error_type, str) or not error_type:
            raise TelemetryValidationError("error span lacks error.type")
        if attributes.get("crag.error.category") not in _ERROR_CATEGORIES:
            raise TelemetryValidationError("error span lacks a valid category")
    elif error_type is not None:
        raise TelemetryValidationError("non-error span carries error.type")
    events = record["events"]
    if not isinstance(events, list) or len(events) > MAX_EVENTS_PER_SPAN:
        raise TelemetryValidationError("span events exceed their cap")
    for index, event in enumerate(events):
        if not isinstance(event, dict) or set(event) != {"name", "time", "attributes"}:
            raise TelemetryValidationError(f"event {index} has invalid fields")
        if not isinstance(event["name"], str) or not _NAME.fullmatch(event["name"]):
            raise TelemetryValidationError(f"event {index} has invalid name")
        event_time = _parse_utc(event["time"], f"events[{index}].time")
        if event_time < start or event_time > end:
            raise TelemetryValidationError(f"event {index} lies outside its span")
        if not isinstance(event["attributes"], dict) or len(event["attributes"]) > MAX_ATTRIBUTES:
            raise TelemetryValidationError(f"event {index} attributes exceed their cap")
    links = record["links"]
    if not isinstance(links, list) or len(links) > MAX_LINKS_PER_SPAN:
        raise TelemetryValidationError("span links exceed their cap")
    if contains_forbidden_content(record):
        raise TelemetryValidationError("record contains forbidden raw content")
    if len(_canonical_json(record)) > MAX_RECORD_BYTES:
        raise TelemetryValidationError("telemetry record exceeds 65,536 bytes")


def validate_trace(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate a complete one-run trace, including parents and cycles."""

    materialized = [deepcopy(dict(record)) for record in records]
    if not materialized:
        raise TelemetryValidationError("trace is empty")
    for record in materialized:
        validate_span_record(record)
    trace_ids = {record["trace_id"] for record in materialized}
    run_ids = {record["run_id"] for record in materialized}
    if len(trace_ids) != 1 or len(run_ids) != 1:
        raise TelemetryValidationError("trace mixes trace_id or run_id values")
    immutable_envelopes = {
        (
            record["source_commit"],
            record["runtime_version"],
            record["redaction_policy_version"],
        )
        for record in materialized
    }
    if len(immutable_envelopes) != 1:
        raise TelemetryValidationError("trace mixes immutable envelope values")
    by_id: dict[str, dict[str, Any]] = {}
    for record in materialized:
        span_id = record["span_id"]
        if span_id in by_id:
            raise TelemetryValidationError("duplicate span id in trace")
        by_id[span_id] = record
    roots = [record for record in materialized if record["parent_span_id"] is None]
    if len(roots) != 1:
        raise TelemetryValidationError("trace must contain exactly one root span")
    root = roots[0]
    if root["operation"] != "agent.run":
        raise TelemetryValidationError("root span must be the agent.run operation")
    for record in materialized:
        parent = record["parent_span_id"]
        if parent is not None and parent not in by_id:
            raise TelemetryValidationError("span references an unknown parent")
        if parent is not None:
            parent_record = by_id[parent]
            child_start = _parse_utc(record["start_time"], "child.start_time")
            child_end = _parse_utc(record["end_time"], "child.end_time")
            parent_start = _parse_utc(parent_record["start_time"], "parent.start_time")
            parent_end = _parse_utc(parent_record["end_time"], "parent.end_time")
            if child_start < parent_start or child_end > parent_end:
                raise TelemetryValidationError(
                    "child span lies outside its parent interval"
                )
        visited: set[str] = set()
        cursor: dict[str, Any] | None = record
        while cursor is not None:
            current_id = cursor["span_id"]
            if current_id in visited:
                raise TelemetryValidationError("span parent graph contains a cycle")
            visited.add(current_id)
            parent_id = cursor["parent_span_id"]
            cursor = by_id.get(parent_id) if parent_id is not None else None
    return materialized


def load_span_records(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load canonical JSONL with duplicate-key and malformed-line rejection."""

    def reject_duplicates(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise TelemetryValidationError(f"duplicate JSON key: {key}")
            output[key] = value
        return output

    records = []
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        try:
            record = json.loads(line, object_pairs_hook=reject_duplicates)
        except (json.JSONDecodeError, TelemetryValidationError) as exc:
            raise TelemetryValidationError(
                f"malformed telemetry JSONL line {line_number}: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise TelemetryValidationError(
                f"telemetry JSONL line {line_number} is not an object"
            )
        records.append(record)
    return validate_trace(records)


def aggregate_trace(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive bounded usage/tool/policy counters from canonical spans."""

    materialized = validate_trace(records)
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 0,
        "llm_calls": 0,
        "tool_calls": 0,
        "policy_decisions": 0,
        "policy_rejections": 0,
        "degraded_decisions": 0,
        "fail_open_decisions": 0,
        "sandbox_commands": 0,
        "checkpoint_operations": 0,
        "stage_spans": 0,
        "retry_events": 0,
        "error_spans": 0,
        "degraded_spans": 0,
        "llm_duration_ms": 0.0,
        "tool_duration_ms": 0.0,
        "sandbox_duration_ms": 0.0,
        "policy_duration_ms": 0.0,
        "cost_microusd": 0,
        "extension_total_tokens": 0,
    }
    observed = {key: False for key in tuple(totals)[:5]}
    cost_observed = False
    extension_tokens_observed = False
    field_map = {
        "input_tokens": "gen_ai.usage.input_tokens",
        "output_tokens": "gen_ai.usage.output_tokens",
        "cache_read_tokens": "gen_ai.usage.cache_read.input_tokens",
        "cache_creation_tokens": "gen_ai.usage.cache_creation.input_tokens",
        "reasoning_tokens": "gen_ai.usage.reasoning_tokens",
    }
    for record in materialized:
        attributes = record["attributes"]
        if record["operation"] == "llm.request":
            totals["llm_calls"] += 1
            totals["llm_duration_ms"] += record["duration_ms"]
        if record["operation"] == "tool.execute":
            totals["tool_calls"] += 1
            totals["tool_duration_ms"] += record["duration_ms"]
        if record["operation"] == "policy.decision":
            totals["policy_decisions"] += 1
            totals["policy_duration_ms"] += record["duration_ms"]
            if attributes.get("crag.policy.decision") in {"denied", "rejected"}:
                totals["policy_rejections"] += 1
            if attributes.get("crag.policy.decision") == "degraded":
                totals["degraded_decisions"] += 1
            if attributes.get("crag.policy.decision") == "fail_open":
                totals["fail_open_decisions"] += 1
        if record["operation"] == "sandbox.command":
            totals["sandbox_commands"] += 1
            totals["sandbox_duration_ms"] += record["duration_ms"]
        if record["operation"] == "checkpoint":
            totals["checkpoint_operations"] += 1
        if record["operation"] == "agent.stage":
            totals["stage_spans"] += 1
        if record["status"] == "error":
            totals["error_spans"] += 1
        if attributes.get("crag.telemetry.mode") == "degraded":
            totals["degraded_spans"] += 1
        totals["retry_events"] += sum(
            event["name"].startswith("crag.retry.")
            for event in record["events"]
        )
        cost = attributes.get("crag.cost.micro_usd")
        if cost is not None:
            if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
                raise TelemetryValidationError(
                    "crag.cost.micro_usd must be a non-negative integer"
                )
            totals["cost_microusd"] += cost
            cost_observed = True
        extension_tokens = attributes.get("crag.usage.total_tokens")
        if extension_tokens is not None:
            if (
                isinstance(extension_tokens, bool)
                or not isinstance(extension_tokens, int)
                or extension_tokens < 0
            ):
                raise TelemetryValidationError(
                    "crag.usage.total_tokens must be a non-negative integer"
                )
            totals["extension_total_tokens"] += extension_tokens
            extension_tokens_observed = True
        for total_name, attribute_name in field_map.items():
            value = attributes.get(attribute_name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TelemetryValidationError(f"{attribute_name} must be non-negative")
            totals[total_name] += value
            observed[total_name] = True
    standard_total_tokens = (
        totals["input_tokens"] + totals["output_tokens"]
        if observed["input_tokens"] or observed["output_tokens"]
        else 0
    )
    root = next(record for record in materialized if record["parent_span_id"] is None)
    result = {
        **{
            key: (totals[key] if observed[key] else None)
            for key in observed
        },
        **{
            key: value
            for key, value in totals.items()
            if key not in observed and key not in {"cost_microusd", "extension_total_tokens"}
        },
        "total_tokens": (
            standard_total_tokens + totals["extension_total_tokens"]
            if standard_total_tokens or extension_tokens_observed
            else None
        ),
        "cost_microusd": totals["cost_microusd"] if cost_observed else None,
        "run_duration_ms": root["duration_ms"],
        "run_status": root["status"],
        "telemetry_mode": root["attributes"]["crag.telemetry.mode"],
    }
    return result
