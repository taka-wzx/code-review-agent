"""Durable review worker with Postgres leases, heartbeats, and retry policy."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import threading
import time
from typing import Any, Mapping
import uuid

from sqlalchemy.exc import SQLAlchemyError

from code_review_agent.database import database_url_from_env, sqlite_database_url
from code_review_agent.llm import make_client
from code_review_agent.observability import aggregate_trace, load_span_records
from code_review_agent.service_core import (
    AuthorizationDenied,
    DefaultReviewRunner,
    ExternalCommandError,
    InvalidRequest,
    ModelCallBudgetExceeded,
    RepositoryRegistry,
    ReviewRequest,
    ReviewRunner,
)
from code_review_agent.service_queue import JobLease, JobStore, LeaseLost, SCHEMA_VERSION


@dataclass(frozen=True)
class RetryDecision:
    category: str
    retryable: bool
    delay_seconds: float = 0.0
    not_before: datetime | None = None


@dataclass(frozen=True)
class WorkerSettings:
    worker_id: str
    concurrency: int = 2
    lease_seconds: float = 60.0
    heartbeat_seconds: float = 10.0
    poll_seconds: float = 1.0
    stale_seconds: float = 30.0
    shutdown_grace_seconds: float = 30.0
    received_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.worker_id) is None:
            raise InvalidRequest("CRAG_WORKER_ID is invalid")
        if isinstance(self.concurrency, bool) or not 1 <= self.concurrency <= 8:
            raise InvalidRequest("CRAG_WORKER_CONCURRENCY must be between 1 and 8")
        if not 1 <= self.lease_seconds <= 3600:
            raise InvalidRequest("CRAG_JOB_LEASE_SECONDS must be between 1 and 3600")
        if not 0.1 <= self.heartbeat_seconds <= 600:
            raise InvalidRequest("CRAG_JOB_HEARTBEAT_SECONDS is outside the supported range")
        if self.heartbeat_seconds >= self.lease_seconds / 2:
            raise InvalidRequest("job heartbeat must be less than half the lease duration")
        if not 0.05 <= self.poll_seconds <= 60:
            raise InvalidRequest("CRAG_WORKER_POLL_SECONDS is outside the supported range")
        if not 1 <= self.stale_seconds <= 3600:
            raise InvalidRequest("CRAG_WORKER_STALE_SECONDS is outside the supported range")
        if not 0 <= self.shutdown_grace_seconds <= 3600:
            raise InvalidRequest("CRAG_SHUTDOWN_GRACE_SECONDS is outside the supported range")
        if not 1 <= self.received_timeout_seconds <= 3600:
            raise InvalidRequest("CRAG_RECEIVED_TIMEOUT_SECONDS is outside the supported range")

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        hostname = re.sub(r"[^A-Za-z0-9_.:-]", "-", socket.gethostname())[:96]
        worker_id = os.environ.get("CRAG_WORKER_ID") or f"worker-{hostname}"
        try:
            return cls(
                worker_id=worker_id,
                concurrency=int(os.environ.get("CRAG_WORKER_CONCURRENCY", "2")),
                lease_seconds=float(os.environ.get("CRAG_JOB_LEASE_SECONDS", "60")),
                heartbeat_seconds=float(
                    os.environ.get("CRAG_JOB_HEARTBEAT_SECONDS", "10")
                ),
                poll_seconds=float(os.environ.get("CRAG_WORKER_POLL_SECONDS", "1")),
                stale_seconds=float(os.environ.get("CRAG_WORKER_STALE_SECONDS", "30")),
                shutdown_grace_seconds=float(
                    os.environ.get("CRAG_SHUTDOWN_GRACE_SECONDS", "30")
                ),
                received_timeout_seconds=float(
                    os.environ.get("CRAG_RECEIVED_TIMEOUT_SECONDS", "60")
                ),
            )
        except ValueError as exc:
            raise InvalidRequest("worker timing configuration must be numeric") from exc


@dataclass
class _ActiveJob:
    lease: JobLease
    thread: threading.Thread
    done: threading.Event


def _status_code(exc: BaseException) -> int | None:
    direct = getattr(exc, "status_code", None)
    if isinstance(direct, int) and not isinstance(direct, bool):
        return direct
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _headers(exc: BaseException) -> Mapping[str, Any]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def _duration(value: str) -> float | None:
    raw = value.strip().casefold()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)?", raw)
    if match is None:
        return None
    amount = float(match.group(1))
    multiplier = {None: 1.0, "ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[
        match.group(2)
    ]
    return amount * multiplier


def _rate_limit_timing(exc: BaseException) -> tuple[float, datetime | None]:
    normalized = {str(key).casefold(): str(value) for key, value in _headers(exc).items()}
    relative_candidates: list[float] = []
    absolute_candidates: list[datetime] = []
    retry_after = normalized.get("retry-after")
    if retry_after:
        seconds = _duration(retry_after)
        if seconds is None:
            try:
                target = parsedate_to_datetime(retry_after)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                absolute_candidates.append(target.astimezone(timezone.utc))
            except (TypeError, ValueError, OverflowError):
                pass
        elif math.isfinite(seconds):
            relative_candidates.append(seconds)
    for name in (
        "x-ratelimit-reset",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
    ):
        value = normalized.get(name)
        if not value:
            continue
        seconds = _duration(value)
        if seconds is None or not math.isfinite(seconds):
            continue
        is_plain_number = re.fullmatch(
            r"[0-9]+(?:\.[0-9]+)?", value.strip()
        ) is not None
        if is_plain_number and seconds > 1000000000:
            try:
                absolute_candidates.append(datetime.fromtimestamp(seconds, timezone.utc))
            except (OSError, OverflowError, ValueError):
                continue
        else:
            relative_candidates.append(seconds)
    relative = min(7 * 86400.0, max([0.0, *relative_candidates]))
    absolute = max(absolute_candidates) if absolute_candidates else None
    return relative, absolute


def _backoff(job_id: str, attempt_count: int) -> float:
    base = float(2 ** max(0, attempt_count - 1))
    jitter_byte = hashlib.sha256(f"{job_id}:{attempt_count}".encode()).digest()[0]
    return base + jitter_byte / 1024.0


def classify_failure(
    exc: BaseException,
    *,
    job_id: str,
    attempt_count: int,
    now: datetime | None = None,
) -> RetryDecision:
    del now  # Absolute provider reset times are resolved against the database clock.
    name = type(exc).__name__.casefold()
    status = _status_code(exc)
    backoff = _backoff(job_id, attempt_count)
    if "ratelimit" in name or status == 429:
        provider_delay, not_before = _rate_limit_timing(exc)
        return RetryDecision(
            "rate_limit", True, max(backoff, provider_delay), not_before
        )
    if isinstance(exc, ModelCallBudgetExceeded) or "budget" in name:
        return RetryDecision("budget_exhausted", False)
    if "authentication" in name or status == 401 or isinstance(exc, SystemExit):
        return RetryDecision("authentication", False)
    if (
        "permission" in name
        or "authorization" in name
        or status == 403
        or isinstance(exc, AuthorizationDenied)
    ):
        return RetryDecision("authorization", False)
    if status in {400, 404, 409, 422} or isinstance(exc, InvalidRequest):
        return RetryDecision("schema_policy", False)
    if status is not None and 500 <= status <= 599:
        return RetryDecision("provider_5xx", True, backoff)
    if "timeout" in name or "connection" in name:
        return RetryDecision("transient_network", True, backoff)
    if isinstance(exc, ExternalCommandError):
        return RetryDecision("external_command", False)
    return RetryDecision("internal", False)


class FakeReviewRunner:
    """Explicit container/test runner; never creates a provider client."""

    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds

    def __call__(self, request: ReviewRequest, trace_path: Path) -> dict[str, Any]:
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(trace_path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "event": "fake_review_completed",
                        "review_id": request.job_id,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        return {"summary": "fake-run", "findings": []}


def _trace_usage(path: Path) -> Mapping[str, Any] | None:
    try:
        aggregate = aggregate_trace(load_span_records(path))
    except BaseException:
        return None
    return {
        "provider": os.environ.get("LLM_PROVIDER", "unknown"),
        "model": os.environ.get("LLM_MODEL", "unknown"),
        "input_tokens": aggregate.get("input_tokens", 0),
        "output_tokens": aggregate.get("output_tokens", 0),
        "cost_microusd": aggregate.get("cost_microusd"),
        "llm_calls": aggregate.get("llm_calls", 0),
        "pricing_version": "canonical-trace/v1",
    }


class ReviewWorker:
    def __init__(
        self,
        registry: RepositoryRegistry,
        store: JobStore,
        *,
        runner: ReviewRunner | None = None,
        worker_id: str | None = None,
        concurrency: int = 2,
        lease_seconds: float = 60.0,
        heartbeat_seconds: float = 10.0,
        poll_seconds: float = 1.0,
        stale_seconds: float = 30.0,
        shutdown_grace_seconds: float = 30.0,
        received_timeout_seconds: float = 60.0,
    ) -> None:
        self.registry = registry
        self.store = store
        self.runner = runner or DefaultReviewRunner()
        self.settings = WorkerSettings(
            worker_id=worker_id or f"worker-{uuid.uuid4().hex}",
            concurrency=concurrency,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
            poll_seconds=poll_seconds,
            stale_seconds=stale_seconds,
            shutdown_grace_seconds=shutdown_grace_seconds,
            received_timeout_seconds=received_timeout_seconds,
        )
        self._stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._maintenance: threading.Thread | None = None
        self._maintenance_lock = threading.Lock()
        self._active: dict[str, _ActiveJob] = {}
        self._active_lock = threading.Lock()

    @property
    def worker_id(self) -> str:
        return self.settings.worker_id

    def start(self) -> None:
        if self._supervisor is not None and self._supervisor.is_alive():
            return
        self._supervisor = threading.Thread(
            target=self.run_forever,
            name=f"crag-worker-{self.worker_id}",
            daemon=True,
        )
        self._supervisor.start()

    def request_shutdown(self) -> None:
        self._stop.set()

    def shutdown(self, *, wait: bool = True) -> None:
        self.request_shutdown()
        if wait and self._supervisor is not None:
            self._supervisor.join(self.settings.shutdown_grace_seconds + 2.0)

    def _record_runner_failure(
        self, lease: JobLease, trace_path: Path, exc: BaseException
    ) -> None:
        decision = classify_failure(
            exc,
            job_id=lease.job_id,
            attempt_count=lease.attempt_count,
        )
        try:
            self.store.finish_failure(
                lease,
                decision.category,
                retryable=decision.retryable,
                trace_key=trace_path.name if trace_path.is_file() else None,
                usage=_trace_usage(trace_path) if trace_path.is_file() else None,
                delay_seconds=decision.delay_seconds,
                available_at=decision.not_before,
            )
        except LeaseLost:
            pass
        except BaseException:
            # The lease or database will be recovered by another worker.
            pass

    def _execute(self, active: _ActiveJob) -> None:
        lease = active.lease
        trace_path = self.store.trace_path_for_lease(lease)
        trace_key = trace_path.name
        try:
            try:
                self.store.mark_running(lease)
            except BaseException:
                # Persistence uncertainty is recovered by the lease; it is not a
                # provider/runner failure and must not be made terminal here.
                return
            try:
                alias, root = self.registry.resolve(lease.repository_alias)
                payload = self.store.load_payload(lease)
                request = ReviewRequest(
                    job_id=lease.job_id,
                    source_kind=lease.source_kind,
                    repository=alias,
                    repo_root=root,
                    source_ref=lease.source_ref,
                    diff=payload,
                    organization_id=lease.organization_id,
                    repository_id=lease.repository_id,
                    principal_id=lease.submitted_by,
                    head_sha=lease.head_sha,
                    attempt_count=lease.attempt_count,
                    model_call_limit=lease.model_call_limit,
                )
                review = self.runner(request, trace_path)
            except BaseException as exc:
                self._record_runner_failure(lease, trace_path, exc)
                return
            try:
                self.store.complete(
                    lease,
                    review,
                    trace_key=trace_key,
                    usage=_trace_usage(trace_path),
                )
            except (LeaseLost, SQLAlchemyError):
                # A lost/uncertain commit is resolved from durable state or lease
                # expiry. Converting it into a business failure would corrupt the
                # successful runner outcome.
                return
            except BaseException as exc:
                self._record_runner_failure(lease, trace_path, exc)
        finally:
            active.done.set()

    def _start_claimed(self, lease: JobLease) -> None:
        done = threading.Event()
        placeholder = threading.Thread()
        active = _ActiveJob(lease=lease, thread=placeholder, done=done)
        thread = threading.Thread(
            target=self._execute,
            args=(active,),
            name=f"crag-job-{lease.job_id[:8]}",
            daemon=True,
        )
        active.thread = thread
        with self._active_lock:
            self._active[lease.job_id] = active
        thread.start()

    def _heartbeat_active(self) -> None:
        self._reap_done()
        with self._active_lock:
            active_jobs = list(self._active.values())
        for active in active_jobs:
            try:
                refreshed = self.store.heartbeat(
                    active.lease, lease_seconds=self.settings.lease_seconds
                )
            except LeaseLost:
                with self._active_lock:
                    self._active.pop(active.lease.job_id, None)
            else:
                active.lease = refreshed

    def _reap_done(self) -> None:
        with self._active_lock:
            completed = [
                job_id for job_id, active in self._active.items() if active.done.is_set()
            ]
            for job_id in completed:
                self._active.pop(job_id, None)

    def _active_count(self) -> int:
        with self._active_lock:
            return len(self._active)

    def _reconcile_received_once(self) -> None:
        try:
            self.store.reconcile_received(
                timeout_seconds=self.settings.received_timeout_seconds,
                batch_size=8,
            )
        except BaseException:
            # Submission replay or the next bounded maintenance pass recovers it.
            pass
        try:
            self.store.cleanup_orphans(
                limit=32,
                temporary_min_age_seconds=max(
                    60.0, self.settings.received_timeout_seconds
                ),
            )
        except BaseException:
            # Orphan cleanup is best effort and never changes committed job state.
            pass

    def _start_received_reconciliation(self) -> None:
        with self._maintenance_lock:
            if self._maintenance is not None and self._maintenance.is_alive():
                return
            self._maintenance = threading.Thread(
                target=self._reconcile_received_once,
                name=f"crag-reconcile-{self.worker_id}",
                daemon=True,
            )
            self._maintenance.start()

    def run_forever(self) -> None:
        self.store.worker_heartbeat(
            self.worker_id,
            status="ready",
            capacity=self.settings.concurrency,
        )
        last_heartbeat = 0.0
        while not self._stop.is_set():
            self._reap_done()
            monotonic = time.monotonic()
            if monotonic - last_heartbeat >= self.settings.heartbeat_seconds:
                self.store.worker_heartbeat(
                    self.worker_id,
                    status="ready",
                    capacity=self.settings.concurrency,
                )
                self._heartbeat_active()
                self._start_received_reconciliation()
                last_heartbeat = monotonic
            while (
                not self._stop.is_set()
                and self._active_count() < self.settings.concurrency
            ):
                lease = self.store.claim(
                    self.worker_id, lease_seconds=self.settings.lease_seconds
                )
                if lease is None:
                    break
                if self._stop.is_set():
                    break
                self._start_claimed(lease)
            heartbeat_due_in = max(
                0.0,
                self.settings.heartbeat_seconds
                - (time.monotonic() - last_heartbeat),
            )
            self._stop.wait(min(self.settings.poll_seconds, heartbeat_due_in))

        self.store.worker_heartbeat(
            self.worker_id,
            status="draining",
            capacity=self.settings.concurrency,
        )
        deadline = time.monotonic() + self.settings.shutdown_grace_seconds
        while self._active_count() and time.monotonic() < deadline:
            self._heartbeat_active()
            time.sleep(min(self.settings.heartbeat_seconds, 0.25))
        with self._maintenance_lock:
            maintenance = self._maintenance
        if maintenance is not None and maintenance.is_alive():
            maintenance.join(max(0.0, deadline - time.monotonic()))
        self.store.worker_heartbeat(
            self.worker_id,
            status="stopped",
            capacity=self.settings.concurrency,
        )


def _validate_provider_credentials() -> None:
    provider = os.environ.get("LLM_PROVIDER", "deepseek").casefold()
    names = {
        "deepseek": ("DEEPSEEK_API_KEY",),
        "glm": ("GLM_API_KEY", "ZHIPUAI_API_KEY"),
    }.get(provider)
    if names is None:
        raise InvalidRequest("LLM_PROVIDER is unsupported")
    if any(os.environ.get(name) for name in names):
        raise InvalidRequest("durable workers require provider credentials via _FILE")
    for name in names:
        path_value = os.environ.get(f"{name}_FILE")
        if not path_value:
            continue
        try:
            encoded = Path(path_value).read_bytes()
        except OSError as exc:
            raise InvalidRequest(f"{name}_FILE is unavailable") from exc
        if len(encoded) > 4096:
            raise InvalidRequest(f"{name}_FILE exceeds the supported size")
        try:
            value = encoded.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise InvalidRequest(f"{name}_FILE is not UTF-8") from exc
        if not value:
            raise InvalidRequest(f"{name}_FILE is empty")
        return
    raise InvalidRequest("provider credential file is required")


def create_worker_from_env() -> ReviewWorker:
    settings = WorkerSettings.from_env()
    configured_state = os.environ.get("CRAG_STATE_DIR")
    state = (
        Path(configured_state)
        if configured_state
        else Path.home() / ".crag" / "service"
    )
    database_url = database_url_from_env(
        default=sqlite_database_url(state / "reviews.sqlite3")
    )
    store = JobStore(
        state,
        database_url=database_url,
        auto_migrate=False,
        job_data_dir=Path(os.environ.get("CRAG_JOB_DATA_DIR", state / "jobs")),
        trace_dir=Path(os.environ.get("CRAG_TRACE_DIR", state / "traces")),
    )
    for name in (
        "CRAG_DATABASE_URL",
        "CRAG_DATABASE_URL_FILE",
        "CRAG_DATABASE_PASSWORD_FILE",
    ):
        os.environ.pop(name, None)
    try:
        registry = RepositoryRegistry.from_json(
            os.environ.get("CRAG_REPOSITORIES_JSON", "")
        )
        runner_name = os.environ.get("CRAG_WORKER_RUNNER", "real").casefold()
        if runner_name == "fake":
            try:
                delay = float(os.environ.get("CRAG_FAKE_RUN_SECONDS", "0"))
            except ValueError as exc:
                raise InvalidRequest("CRAG_FAKE_RUN_SECONDS must be numeric") from exc
            runner: ReviewRunner = FakeReviewRunner(delay_seconds=delay)
        elif runner_name == "real":
            _validate_provider_credentials()
            client_model = make_client(load_env_file=False)
            for name in (
                "DEEPSEEK_API_KEY",
                "DEEPSEEK_API_KEY_FILE",
                "GLM_API_KEY",
                "GLM_API_KEY_FILE",
                "ZHIPUAI_API_KEY",
                "ZHIPUAI_API_KEY_FILE",
            ):
                os.environ.pop(name, None)

            def cached_client_factory() -> tuple[Any, str]:
                return client_model

            runner = DefaultReviewRunner(
                client_factory=cached_client_factory
            )
        else:
            raise InvalidRequest("CRAG_WORKER_RUNNER must be real or fake")
        return ReviewWorker(
            registry,
            store,
            runner=runner,
            worker_id=settings.worker_id,
            concurrency=settings.concurrency,
            lease_seconds=settings.lease_seconds,
            heartbeat_seconds=settings.heartbeat_seconds,
            poll_seconds=settings.poll_seconds,
            stale_seconds=settings.stale_seconds,
            shutdown_grace_seconds=settings.shutdown_grace_seconds,
            received_timeout_seconds=settings.received_timeout_seconds,
        )
    except BaseException:
        store.close()
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a durable code-review-agent worker")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit zero when the database is ready and a worker heartbeat is fresh",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    worker = create_worker_from_env()
    if args.check:
        healthy = worker.store.database_ready() and worker.store.worker_is_live(
            worker.worker_id, stale_seconds=worker.settings.stale_seconds
        )
        worker.store.close()
        raise SystemExit(0 if healthy else 1)

    def stop(signum: int, frame: Any) -> None:
        del signum, frame
        worker.request_shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        worker.run_forever()
    finally:
        worker.store.close()


if __name__ == "__main__":
    main()
