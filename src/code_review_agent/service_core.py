"""Protocol-neutral asynchronous review service used by HTTP and MCP adapters."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Protocol
import uuid

from code_review_agent.agent import run_review
from code_review_agent.llm import make_client
from code_review_agent.tracelog import Trace, tev


SCHEMA_VERSION = "crag.service/v1alpha1"
MAX_DIFF_BYTES = 512 * 1024
MAX_TRACE_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_PR_REF_CHARS = 256
_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?\Z"
)
_PR_URL = re.compile(
    r"https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)/?\Z",
    re.IGNORECASE,
)
_JOB_ID = re.compile(r"[0-9a-f]{32}\Z")


class ServiceError(RuntimeError):
    """Base class for bounded, user-actionable service failures."""

    code = "service_error"


class InvalidRequest(ServiceError):
    code = "invalid_request"


class JobNotFound(ServiceError):
    code = "job_not_found"


class ServiceClosed(ServiceError):
    code = "service_closed"


class StateDirectoryInUse(ServiceError):
    code = "state_directory_in_use"


class ExternalCommandError(RuntimeError):
    """A bounded failure from an external command-line dependency."""


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _job_id(value: str) -> str:
    if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
        raise JobNotFound("review job was not found")
    return value


def normalize_repository(value: str) -> str:
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise InvalidRequest("repository must be an owner/repo alias")
    return value.casefold()


def normalize_pr_ref(repository: str, value: str | int) -> str:
    raw = str(value).strip()
    if not raw or len(raw) > MAX_PR_REF_CHARS:
        raise InvalidRequest("pull_request is invalid")
    if raw.isdigit() and int(raw) > 0:
        return str(int(raw))
    match = _PR_URL.fullmatch(raw)
    if match is None:
        raise InvalidRequest("pull_request must be a positive number or exact GitHub PR URL")
    if int(match.group("number")) <= 0:
        raise InvalidRequest("pull_request must be positive")
    url_repository = f"{match.group('owner')}/{match.group('repo')}".casefold()
    if url_repository != normalize_repository(repository):
        raise InvalidRequest("pull_request URL does not match the registered repository")
    return (
        f"https://github.com/{match.group('owner')}/{match.group('repo')}"
        f"/pull/{int(match.group('number'))}"
    )


def validate_diff(value: str) -> tuple[str, int]:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest("diff must be a non-empty string")
    size = len(value.encode("utf-8"))
    if size > MAX_DIFF_BYTES:
        raise InvalidRequest(f"diff exceeds the {MAX_DIFF_BYTES}-byte limit")
    if not any(line.startswith(("diff --git ", "--- ", "+++ ", "@@ ")) for line in value.splitlines()):
        raise InvalidRequest("diff does not look like a unified diff")
    return hashlib.sha256(value.encode("utf-8")).hexdigest(), size


@dataclass(frozen=True)
class RepositoryRegistry:
    paths: Mapping[str, Path]

    @classmethod
    def from_json(cls, raw: str) -> "RepositoryRegistry":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidRequest("CRAG_REPOSITORIES_JSON is not valid JSON") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise InvalidRequest("CRAG_REPOSITORIES_JSON must be a non-empty object")
        paths: dict[str, Path] = {}
        for alias, raw_path in parsed.items():
            normalized = normalize_repository(alias)
            if normalized in paths:
                raise InvalidRequest("repository aliases must be unique case-insensitively")
            if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                raise InvalidRequest("registered repository paths must be absolute")
            try:
                path = Path(raw_path).resolve(strict=True)
            except OSError as exc:
                raise InvalidRequest("a registered repository path does not exist") from exc
            if not (path / ".git").exists():
                raise InvalidRequest("a registered repository path is not a Git checkout")
            paths[normalized] = path
        return cls(paths)

    def resolve(self, repository: str) -> tuple[str, Path]:
        alias = normalize_repository(repository)
        path = self.paths.get(alias)
        if path is None:
            raise InvalidRequest("repository is not registered")
        return alias, path


@dataclass(frozen=True)
class ReviewRequest:
    job_id: str
    source_kind: str
    repository: str
    repo_root: Path
    source_ref: str
    diff: str | None = None


class ReviewRunner(Protocol):
    def __call__(self, request: ReviewRequest, trace_path: Path) -> dict[str, Any]: ...


def _safe_failure(exc: BaseException) -> str:
    name = type(exc).__name__.casefold()
    if "authentication" in name or isinstance(exc, SystemExit):
        return "configuration"
    if "ratelimit" in name:
        return "rate_limit"
    if "timeout" in name:
        return "timeout"
    if "connection" in name or "apistatus" in name:
        return "provider"
    if isinstance(exc, (ExternalCommandError, FileNotFoundError, subprocess.SubprocessError)):
        return "external_command"
    return "internal"


class DefaultReviewRunner:
    """Run the existing Review Agent while preserving its trace semantics."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], tuple[Any, str]] = make_client,
        process_factory: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client_factory = client_factory
        self._process_factory = process_factory
        self._clock = clock
        self._sleep = sleep

    def _pr_diff(self, request: ReviewRequest) -> str:
        with tempfile.TemporaryFile() as output:
            try:
                proc = self._process_factory(
                    ["gh", "pr", "diff", request.source_ref],
                    cwd=request.repo_root,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ExternalCommandError("GitHub diff command failed") from exc
            deadline = self._clock() + 60
            while proc.poll() is None:
                if os.fstat(output.fileno()).st_size > MAX_DIFF_BYTES:
                    proc.kill()
                    proc.wait()
                    raise ExternalCommandError("GitHub diff is too large")
                if self._clock() >= deadline:
                    proc.kill()
                    proc.wait()
                    raise ExternalCommandError("GitHub diff command timed out")
                self._sleep(0.01)
            if proc.returncode != 0:
                raise ExternalCommandError("GitHub diff command failed")
            output.seek(0)
            encoded = output.read(MAX_DIFF_BYTES + 1)
        if not encoded.strip() or len(encoded) > MAX_DIFF_BYTES:
            raise ExternalCommandError("GitHub diff is empty or too large")
        diff = encoded.decode("utf-8", errors="replace")
        return diff

    def __call__(self, request: ReviewRequest, trace_path: Path) -> dict[str, Any]:
        trace = Trace(
            trace_path,
            run_id=request.job_id,
            root_attributes={
                "crag.service.schema": SCHEMA_VERSION,
                "crag.service.source": request.source_kind,
                "crag.service.repository": request.repository,
            },
        )
        error: tuple[str, str] | None = None
        try:
            diff = request.diff if request.diff is not None else self._pr_diff(request)
            client, model = self._client_factory()
            tev(trace, "meta", provider=os.environ.get("LLM_PROVIDER", "deepseek"), model=model)
            return run_review(client, diff, request.repo_root, model, trace=trace)
        except BaseException as exc:
            error = (type(exc).__name__, _safe_failure(exc))
            raise
        finally:
            if error is None:
                trace.close()
            else:
                trace.close(error_type=error[0], error_category=error[1])


class JobStore:
    """Small SQLite state machine; connections are short-lived and thread-safe."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.trace_dir = self.state_dir / "traces"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.state_dir.chmod(0o700)
            self.trace_dir.chmod(0o700)
        self.database = self.state_dir / "reviews.sqlite3"
        self._lock_file = (self.state_dir / ".service.lock").open("a+b")
        self._closed = False
        try:
            self._acquire_state_lock()
            self._initialize()
        except BaseException:
            self._lock_file.close()
            raise

    def _acquire_state_lock(self) -> None:
        try:
            self._lock_file.seek(0)
            if self._lock_file.read(1) == b"":
                self._lock_file.write(b"\0")
                self._lock_file.flush()
            self._lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl: Any = importlib.import_module("fcntl")
                fcntl.flock(
                    self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
        except (OSError, BlockingIOError) as exc:
            raise StateDirectoryInUse("service state directory is already in use") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl: Any = importlib.import_module("fcntl")
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_file.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    source_kind TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    source_bytes INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    review_json TEXT,
                    error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    event TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "UPDATE jobs SET state=?, completed_at=?, error_code=? "
                "WHERE state IN (?, ?)",
                (
                    JobState.FAILED.value,
                    _now(),
                    "service_restarted",
                    JobState.QUEUED.value,
                    JobState.RUNNING.value,
                ),
            )

    def create(
        self,
        *,
        source_kind: str,
        repository: str,
        source_ref: str,
        source_sha256: str,
        source_bytes: int,
        delivery_id: str | None = None,
        event: str = "pull_request",
    ) -> tuple[str, bool]:
        job_id = uuid.uuid4().hex
        created = _now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if delivery_id is not None:
                    row = conn.execute(
                        "SELECT job_id FROM deliveries WHERE delivery_id=?", (delivery_id,)
                    ).fetchone()
                    if row is not None:
                        conn.commit()
                        return str(row["job_id"]), True
                conn.execute(
                    "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
                    (
                        job_id,
                        source_kind,
                        repository,
                        source_ref,
                        source_sha256,
                        source_bytes,
                        JobState.QUEUED.value,
                        created,
                    ),
                )
                if delivery_id is not None:
                    conn.execute(
                        "INSERT INTO deliveries VALUES (?, ?, ?, ?)",
                        (delivery_id, job_id, event, created),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return job_id, False

    def delete_queued(self, job_id: str) -> None:
        """Compensate a committed submission that could not reach the executor."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM deliveries WHERE job_id=?", (_job_id(job_id),))
                cursor = conn.execute(
                    "DELETE FROM jobs WHERE id=? AND state=?",
                    (_job_id(job_id), JobState.QUEUED.value),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("queued review could not be removed")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _transition(self, job_id: str, current: str, target: str, **fields: Any) -> None:
        assignments = ["state=?"] + [f"{name}=?" for name in fields]
        values = [target, *fields.values(), _job_id(job_id), current]
        with self._connection() as conn:
            cursor = conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id=? AND state=?",
                values,
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"invalid review state transition {current} -> {target}")

    def mark_running(self, job_id: str) -> None:
        self._transition(
            job_id,
            JobState.QUEUED.value,
            JobState.RUNNING.value,
            started_at=_now(),
        )

    def succeed(self, job_id: str, review: Mapping[str, Any]) -> None:
        encoded = json.dumps(review, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            self.fail(job_id, "result_too_large")
            return
        self._transition(
            job_id,
            JobState.RUNNING.value,
            JobState.SUCCEEDED.value,
            completed_at=_now(),
            review_json=encoded,
            error_code=None,
        )

    def fail(self, job_id: str, error_code: str) -> None:
        self._transition(
            job_id,
            JobState.RUNNING.value,
            JobState.FAILED.value,
            completed_at=_now(),
            error_code=error_code[:64],
            review_json=None,
        )

    def get(self, job_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (_job_id(job_id),)).fetchone()
        if row is None:
            raise JobNotFound("review job was not found")
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "review_id": row["id"],
            "source": {
                "kind": row["source_kind"],
                "repository": row["repository"],
                "reference": row["source_ref"],
                "sha256": row["source_sha256"],
                "bytes": row["source_bytes"],
            },
            "state": row["state"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }
        if row["review_json"] is not None:
            result["review"] = json.loads(row["review_json"])
        if row["error_code"] is not None:
            result["error"] = {"code": row["error_code"]}
        return result

    def trace_path(self, job_id: str) -> Path:
        self.get(job_id)
        return self.trace_dir / f"{_job_id(job_id)}.jsonl"

    def read_trace(self, job_id: str) -> str:
        job = self.get(job_id)
        if job["state"] in {JobState.QUEUED.value, JobState.RUNNING.value}:
            raise InvalidRequest("trace is not available until the review is terminal")
        path = self.trace_path(job_id)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise InvalidRequest("trace is unavailable") from exc
        if size > MAX_TRACE_BYTES:
            raise InvalidRequest("trace exceeds the service response limit")
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InvalidRequest("trace is malformed") from exc
            if not isinstance(record, dict):
                raise InvalidRequest("trace is malformed")
        return text


class ReviewService:
    def __init__(
        self,
        registry: RepositoryRegistry,
        store: JobStore,
        *,
        runner: ReviewRunner | None = None,
        workers: int = 2,
    ) -> None:
        if isinstance(workers, bool) or not 1 <= workers <= 8:
            raise ValueError("workers must be between 1 and 8")
        self.registry = registry
        self.store = store
        self.runner = runner or DefaultReviewRunner()
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crag-review")
        self._lock = threading.Lock()
        self._accepting = True

    def _ensure_accepting(self) -> None:
        if not self._accepting:
            raise ServiceClosed("review service is shutting down")

    def _queue_locked(self, request: ReviewRequest) -> None:
        try:
            self._executor.submit(self._run, request)
        except RuntimeError as exc:
            self.store.delete_queued(request.job_id)
            raise ServiceClosed("review service is shutting down") from exc

    def _run(self, request: ReviewRequest) -> None:
        try:
            self.store.mark_running(request.job_id)
            review = self.runner(request, self.store.trace_path(request.job_id))
            self.store.succeed(request.job_id, review)
        except BaseException as exc:
            try:
                job = self.store.get(request.job_id)
                if job["state"] == JobState.RUNNING.value:
                    self.store.fail(request.job_id, _safe_failure(exc))
            except BaseException:
                pass

    def submit_diff(self, repository: str, diff: str) -> dict[str, Any]:
        alias, root = self.registry.resolve(repository)
        digest, size = validate_diff(diff)
        with self._lock:
            self._ensure_accepting()
            job_id, _ = self.store.create(
                source_kind="diff",
                repository=alias,
                source_ref="inline",
                source_sha256=digest,
                source_bytes=size,
            )
            self._queue_locked(ReviewRequest(job_id, "diff", alias, root, "inline", diff))
        return self.store.get(job_id)

    def submit_pr(
        self,
        repository: str,
        pull_request: str | int,
        *,
        delivery_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        alias, root = self.registry.resolve(repository)
        reference = normalize_pr_ref(alias, pull_request)
        digest = hashlib.sha256(f"{alias}\0{reference}".encode()).hexdigest()
        if delivery_id is not None:
            if not isinstance(delivery_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", delivery_id):
                raise InvalidRequest("delivery ID is invalid")
        with self._lock:
            self._ensure_accepting()
            job_id, duplicate = self.store.create(
                source_kind="pull_request",
                repository=alias,
                source_ref=reference,
                source_sha256=digest,
                source_bytes=0,
                delivery_id=delivery_id,
            )
            if not duplicate:
                self._queue_locked(ReviewRequest(job_id, "pull_request", alias, root, reference))
        return self.store.get(job_id), duplicate

    def get(self, job_id: str) -> dict[str, Any]:
        return self.store.get(job_id)

    def get_trace(self, job_id: str) -> str:
        return self.store.read_trace(job_id)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
        try:
            self._executor.shutdown(wait=wait, cancel_futures=not wait)
        finally:
            if wait:
                self.store.close()


def create_review_service_from_env(*, runner: ReviewRunner | None = None) -> ReviewService:
    raw_repositories = os.environ.get("CRAG_REPOSITORIES_JSON", "")
    registry = RepositoryRegistry.from_json(raw_repositories)
    state = Path(os.environ.get("CRAG_STATE_DIR", Path.home() / ".crag" / "service"))
    try:
        workers = int(os.environ.get("CRAG_SERVICE_WORKERS", "2"))
    except ValueError as exc:
        raise InvalidRequest("CRAG_SERVICE_WORKERS must be an integer") from exc
    return ReviewService(registry, JobStore(state), runner=runner, workers=workers)
