"""Protocol-neutral asynchronous review service used by HTTP and MCP adapters."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Protocol
import uuid

from code_review_agent.agent import run_review
from code_review_agent.database import (
    Database,
    MigrationRequired,
    new_id,
    require_schema_head,
    sqlite_database_url,
    upgrade_database,
)
from code_review_agent.identity import (
    Permission,
    PermissionDenied,
    Principal,
    Role,
    current_correlation_id,
)
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


class AuthorizationDenied(ServiceError):
    code = "authorization_denied"


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
    organization_id: str = ""
    repository_id: str = ""
    principal_id: str = ""


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
                "crag.service.organization_id": request.organization_id,
                "crag.service.repository_id": request.repository_id,
                "crag.service.principal_id": request.principal_id,
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
    """Versioned tenant-aware job state with a local trace directory."""

    def __init__(
        self,
        state_dir: Path,
        *,
        database_url: str | None = None,
        auto_migrate: bool = True,
    ) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.trace_dir = self.state_dir / "traces"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.state_dir.chmod(0o700)
            self.trace_dir.chmod(0o700)
        self.database_path = self.state_dir / "reviews.sqlite3"
        self.database_url = database_url or sqlite_database_url(self.database_path)
        self._lock_file = (self.state_dir / ".service.lock").open("a+b")
        self._closed = False
        self._local_principal: Principal | None = None
        try:
            self._acquire_state_lock()
            if auto_migrate:
                if not self.database_url.startswith("sqlite"):
                    raise MigrationRequired("automatic migration is only allowed for local SQLite")
                upgrade_database(self.database_url)
            else:
                require_schema_head(self.database_url)
            self.database = Database(self.database_url, check_schema=False)
            self._recover_abandoned()
            if auto_migrate:
                self._local_principal = self.database.bootstrap_local(())
        except BaseException:
            self._lock_file.close()
            raise

    @property
    def local_principal(self) -> Principal | None:
        return self._local_principal

    def bootstrap_local(self, repository_aliases: Iterable[str]) -> Principal:
        self._local_principal = self.database.bootstrap_local(repository_aliases)
        return self._local_principal

    def _acquire_state_lock(self) -> None:
        try:
            self._lock_file.seek(0)
            if self._lock_file.read(1) == b"":
                self._lock_file.write(b"\0")
                self._lock_file.flush()
            self._lock_file.seek(0)
            if os.name == "nt":
                msvcrt: Any = importlib.import_module("msvcrt")
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl: Any = importlib.import_module("fcntl")
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise StateDirectoryInUse("service state directory is already in use") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.database.close()
            self._lock_file.seek(0)
            if os.name == "nt":
                msvcrt: Any = importlib.import_module("msvcrt")
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl: Any = importlib.import_module("fcntl")
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_file.close()

    def _recover_abandoned(self) -> None:
        from sqlalchemy import text

        with self.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE review_jobs SET state=:failed, completed_at=:completed, "
                    "error_code=:error WHERE state IN (:queued, :running)"
                ),
                {
                    "failed": JobState.FAILED.value,
                    "completed": _now(),
                    "error": "service_restarted",
                    "queued": JobState.QUEUED.value,
                    "running": JobState.RUNNING.value,
                },
            )

    def _local_repository(self, repository: str) -> tuple[Principal, Mapping[str, Any]]:
        principal = self._local_principal
        if principal is None:
            raise AuthorizationDenied("authenticated principal is required")
        record = self.database.authorized_repository(principal, repository)
        if record is None and self.database_url.startswith("sqlite"):
            self._local_principal = self.database.bootstrap_local((repository,))
            principal = self._local_principal
            record = self.database.authorized_repository(principal, repository)
        if record is None:
            raise InvalidRequest("repository is not registered")
        return principal, record

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
        organization_id: str | None = None,
        repository_id: str | None = None,
        submitted_by: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[str, bool]:
        from sqlalchemy import text

        if organization_id is None or repository_id is None or submitted_by is None:
            principal, record = self._local_repository(repository)
            organization_id = principal.organization_id
            repository_id = str(record["id"])
            submitted_by = principal.user_id
        job_id = uuid.uuid4().hex
        created = _now()
        correlation_id = correlation_id or job_id
        with self.database.engine.begin() as connection:
            if delivery_id is not None:
                row = connection.execute(
                    text(
                        "SELECT review_job_id FROM webhook_deliveries "
                        "WHERE delivery_id=:delivery"
                    ),
                    {"delivery": delivery_id},
                ).first()
                if row is not None:
                    return str(row._mapping["review_job_id"]), True
            connection.execute(
                text(
                    "INSERT INTO review_jobs "
                    "(id, organization_id, repository_id, submitted_by, correlation_id, "
                    "source_kind, repository_alias, source_ref, source_sha256, source_bytes, "
                    "state, created_at, started_at, completed_at, review_json, error_code) "
                    "VALUES (:id, :org, :repo, :actor, :correlation, :kind, :alias, :ref, "
                    ":sha, :bytes, :state, :created, NULL, NULL, NULL, NULL)"
                ),
                {
                    "id": job_id,
                    "org": organization_id,
                    "repo": repository_id,
                    "actor": submitted_by,
                    "correlation": correlation_id,
                    "kind": source_kind,
                    "alias": repository,
                    "ref": source_ref,
                    "sha": source_sha256,
                    "bytes": source_bytes,
                    "state": JobState.QUEUED.value,
                    "created": created,
                },
            )
            if delivery_id is not None:
                connection.execute(
                    text(
                        "INSERT INTO webhook_deliveries "
                        "(id, organization_id, repository_id, review_job_id, delivery_id, "
                        "event, received_at) VALUES (:id, :org, :repo, :job, :delivery, "
                        ":event, :received)"
                    ),
                    {
                        "id": new_id(),
                        "org": organization_id,
                        "repo": repository_id,
                        "job": job_id,
                        "delivery": delivery_id,
                        "event": event,
                        "received": created,
                    },
                )
        return job_id, False

    def delete_queued(self, job_id: str) -> None:
        """Compensate a committed submission that could not reach the executor."""
        from sqlalchemy import text

        with self.database.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM webhook_deliveries WHERE review_job_id=:job"),
                {"job": _job_id(job_id)},
            )
            result = connection.execute(
                text("DELETE FROM review_jobs WHERE id=:id AND state=:state"),
                {"id": _job_id(job_id), "state": JobState.QUEUED.value},
            )
            if result.rowcount != 1:
                raise RuntimeError("queued review could not be removed")

    def _transition(self, job_id: str, current: str, target: str, **fields: Any) -> None:
        from sqlalchemy import text

        allowed = {"started_at", "completed_at", "review_json", "error_code"}
        if not set(fields).issubset(allowed):
            raise RuntimeError("invalid review state field")
        assignments = ["state=:target"]
        values: dict[str, Any] = {
            "target": target,
            "id": _job_id(job_id),
            "current": current,
        }
        for name, value in fields.items():
            assignments.append(f"{name}=:{name}")
            values[name] = value
        with self.database.engine.begin() as connection:
            result = connection.execute(
                text(
                    f"UPDATE review_jobs SET {', '.join(assignments)} "
                    "WHERE id=:id AND state=:current"
                ),
                values,
            )
            if result.rowcount != 1:
                raise RuntimeError(f"invalid review state transition {current} -> {target}")

    def mark_running(self, job_id: str) -> None:
        self._transition(
            job_id,
            JobState.QUEUED.value,
            JobState.RUNNING.value,
            started_at=_now(),
        )

    def succeed(self, job_id: str, review: Mapping[str, Any]) -> None:
        from sqlalchemy import text

        encoded = json.dumps(review, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            self.fail(job_id, "result_too_large")
            return
        with self.database.engine.begin() as connection:
            job = connection.execute(
                text(
                    "SELECT organization_id, repository_id FROM review_jobs "
                    "WHERE id=:id AND state=:state"
                ),
                {"id": _job_id(job_id), "state": JobState.RUNNING.value},
            ).first()
            if job is None:
                raise RuntimeError("invalid review state transition running -> succeeded")
            result = connection.execute(
                text(
                    "UPDATE review_jobs SET state=:target, completed_at=:completed, "
                    "review_json=:review, error_code=NULL WHERE id=:id AND state=:current"
                ),
                {
                    "target": JobState.SUCCEEDED.value,
                    "completed": _now(),
                    "review": encoded,
                    "id": _job_id(job_id),
                    "current": JobState.RUNNING.value,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError("invalid review state transition running -> succeeded")
            for finding in review.get("findings", []):
                if not isinstance(finding, Mapping):
                    continue
                payload = json.dumps(finding, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                fingerprint = str(finding.get("fingerprint") or content_hash)
                connection.execute(
                    text(
                        "INSERT INTO findings "
                        "(id, organization_id, repository_id, review_job_id, fingerprint, "
                        "content_sha256, path, line, severity, category, status, payload_json, "
                        "created_at) VALUES (:id, :org, :repo, :job, :fingerprint, :content, "
                        ":path, :line, :severity, :category, 'pending_approval', :payload, "
                        ":created)"
                    ),
                    {
                        "id": new_id(),
                        "org": job._mapping["organization_id"],
                        "repo": job._mapping["repository_id"],
                        "job": job_id,
                        "fingerprint": fingerprint[:128],
                        "content": content_hash,
                        "path": finding.get("path") or finding.get("file"),
                        "line": finding.get("line"),
                        "severity": finding.get("severity"),
                        "category": finding.get("category"),
                        "payload": payload,
                        "created": _now(),
                    },
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

    def _row(self, job_id: str, principal: Principal | None = None) -> Mapping[str, Any]:
        from sqlalchemy import text

        parameters: dict[str, Any] = {"id": _job_id(job_id)}
        organization_clause = ""
        if principal is not None:
            organization_clause = " AND organization_id=:org"
            parameters["org"] = principal.organization_id
        with self.database.engine.connect() as connection:
            row = connection.execute(
                text("SELECT * FROM review_jobs WHERE id=:id" + organization_clause),
                parameters,
            ).first()
        if row is None:
            raise JobNotFound("review job was not found")
        item = row._mapping
        if principal is not None:
            repository = self.database.authorized_repository(
                principal, str(item["repository_id"])
            )
            if repository is None:
                raise JobNotFound("review job was not found")
        return dict(item)

    def get(self, job_id: str, principal: Principal | None = None) -> dict[str, Any]:
        row = self._row(job_id, principal)
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "review_id": row["id"],
            "organization_id": row["organization_id"],
            "repository_id": row["repository_id"],
            "source": {
                "kind": row["source_kind"],
                "repository": row["repository_alias"],
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

    def trace_path(self, job_id: str, principal: Principal | None = None) -> Path:
        self._row(job_id, principal)
        return self.trace_dir / f"{_job_id(job_id)}.jsonl"

    def read_trace(self, job_id: str, principal: Principal | None = None) -> str:
        job = self.get(job_id, principal)
        if job["state"] in {JobState.QUEUED.value, JobState.RUNNING.value}:
            raise InvalidRequest("trace is not available until the review is terminal")
        path = self.trace_path(job_id, principal)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise InvalidRequest("trace is unavailable") from exc
        if size > MAX_TRACE_BYTES:
            raise InvalidRequest("trace exceeds the service response limit")
        trace_text = path.read_text(encoding="utf-8")
        for line in trace_text.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InvalidRequest("trace is malformed") from exc
            if not isinstance(record, dict):
                raise InvalidRequest("trace is malformed")
        return trace_text


class ReviewService:
    def __init__(
        self,
        registry: RepositoryRegistry,
        store: JobStore,
        *,
        runner: ReviewRunner | None = None,
        workers: int = 2,
        local_mode: bool = True,
    ) -> None:
        if isinstance(workers, bool) or not 1 <= workers <= 8:
            raise ValueError("workers must be between 1 and 8")
        self.registry = registry
        self.store = store
        self.runner = runner or DefaultReviewRunner()
        self.local_mode = local_mode
        if local_mode:
            self.store.bootstrap_local(self.registry.paths.keys())
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crag-review")
        self._lock = threading.Lock()
        self._accepting = True

    def _principal(self, principal: Principal | None) -> Principal:
        resolved = principal or self.store.local_principal
        if resolved is None:
            raise AuthorizationDenied("authenticated principal is required")
        return resolved

    def _audit(
        self,
        principal: Principal,
        action: str,
        resource_type: str,
        resource_id: str,
        decision: str,
        *,
        repository_id: str | None = None,
        reason_code: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.store.database.audit(
            principal=principal,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            decision=decision,
            repository_id=repository_id,
            reason_code=reason_code,
            correlation_id=correlation_id or current_correlation_id(uuid.uuid4().hex),
        )

    def _require(self, principal: Principal, permission: Permission, action: str) -> None:
        try:
            principal.require(permission)
        except PermissionDenied as exc:
            self._audit(
                principal,
                action,
                "organization",
                principal.organization_id,
                "deny",
                reason_code="role_denied",
            )
            raise AuthorizationDenied("operation is not permitted") from exc

    def _repository(
        self,
        principal: Principal,
        repository: str,
        permission: Permission,
        action: str,
    ) -> tuple[str, Path, Mapping[str, Any]]:
        self._require(principal, permission, action)
        alias = normalize_repository(repository)
        record = self.store.database.authorized_repository(principal, alias)
        if record is None:
            self._audit(
                principal,
                action,
                "repository",
                alias,
                "deny",
                reason_code="not_found",
            )
            raise InvalidRequest("repository is not registered")
        _, root = self.registry.resolve(alias)
        return alias, root, record

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

    def submit_diff(
        self,
        repository: str,
        diff: str,
        *,
        principal: Principal | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_accepting()
        actor = self._principal(principal)
        alias, root, repository_record = self._repository(
            actor, repository, Permission.SUBMIT_REVIEW, "review.submit"
        )
        digest, size = validate_diff(diff)
        correlation = correlation_id or uuid.uuid4().hex
        with self._lock:
            self._ensure_accepting()
            job_id, _ = self.store.create(
                source_kind="diff",
                repository=alias,
                source_ref="inline",
                source_sha256=digest,
                source_bytes=size,
                organization_id=actor.organization_id,
                repository_id=str(repository_record["id"]),
                submitted_by=actor.user_id,
                correlation_id=correlation,
            )
            self._queue_locked(
                ReviewRequest(
                    job_id,
                    "diff",
                    alias,
                    root,
                    "inline",
                    diff,
                    actor.organization_id,
                    str(repository_record["id"]),
                    actor.principal_id,
                )
            )
        self._audit(
            actor,
            "review.submit",
            "review_job",
            job_id,
            "allow",
            repository_id=str(repository_record["id"]),
            correlation_id=correlation,
        )
        return self.store.get(job_id, actor)

    def submit_pr(
        self,
        repository: str,
        pull_request: str | int,
        *,
        delivery_id: str | None = None,
        principal: Principal | None = None,
        correlation_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        self._ensure_accepting()
        actor = self._principal(principal)
        alias, root, repository_record = self._repository(
            actor, repository, Permission.SUBMIT_REVIEW, "review.submit"
        )
        reference = normalize_pr_ref(alias, pull_request)
        digest = hashlib.sha256(f"{alias}\0{reference}".encode()).hexdigest()
        if delivery_id is not None:
            if not isinstance(delivery_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", delivery_id):
                raise InvalidRequest("delivery ID is invalid")
        correlation = correlation_id or uuid.uuid4().hex
        with self._lock:
            self._ensure_accepting()
            job_id, duplicate = self.store.create(
                source_kind="pull_request",
                repository=alias,
                source_ref=reference,
                source_sha256=digest,
                source_bytes=0,
                delivery_id=delivery_id,
                organization_id=actor.organization_id,
                repository_id=str(repository_record["id"]),
                submitted_by=actor.user_id,
                correlation_id=correlation,
            )
            if not duplicate:
                self._queue_locked(
                    ReviewRequest(
                        job_id,
                        "pull_request",
                        alias,
                        root,
                        reference,
                        organization_id=actor.organization_id,
                        repository_id=str(repository_record["id"]),
                        principal_id=actor.principal_id,
                    )
                )
        self._audit(
            actor,
            "review.submit",
            "review_job",
            job_id,
            "allow",
            repository_id=str(repository_record["id"]),
            correlation_id=correlation,
        )
        return self.store.get(job_id, actor), duplicate

    def submit_webhook_pr(
        self,
        repository: str,
        pull_request: str | int,
        *,
        delivery_id: str,
        correlation_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        self._ensure_accepting()
        alias, root = self.registry.resolve(repository)
        repository_record = self.store.database.repository_for_webhook(alias)
        if repository_record is None:
            raise InvalidRequest("repository is not registered")
        reference = normalize_pr_ref(alias, pull_request)
        digest = hashlib.sha256(f"{alias}\0{reference}".encode()).hexdigest()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", delivery_id):
            raise InvalidRequest("delivery ID is invalid")
        correlation = correlation_id or uuid.uuid4().hex
        with self._lock:
            self._ensure_accepting()
            job_id, duplicate = self.store.create(
                source_kind="pull_request",
                repository=alias,
                source_ref=reference,
                source_sha256=digest,
                source_bytes=0,
                delivery_id=delivery_id,
                organization_id=str(repository_record["organization_id"]),
                repository_id=str(repository_record["id"]),
                submitted_by="github-webhook",
                correlation_id=correlation,
            )
            if not duplicate:
                self._queue_locked(
                    ReviewRequest(
                        job_id,
                        "pull_request",
                        alias,
                        root,
                        reference,
                        organization_id=str(repository_record["organization_id"]),
                        repository_id=str(repository_record["id"]),
                        principal_id="github-webhook",
                    )
                )
        webhook_principal = Principal(
            principal_id="github-webhook",
            user_id="github-webhook",
            organization_id=str(repository_record["organization_id"]),
            role=Role.VIEWER,
            auth_method="webhook_hmac",
        )
        self._audit(
            webhook_principal,
            "webhook.review.submit",
            "review_job",
            job_id,
            "allow",
            repository_id=str(repository_record["id"]),
            correlation_id=correlation,
        )
        return self.store.get(job_id), duplicate

    def get(
        self, job_id: str, *, principal: Principal | None = None
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.READ, "review.read")
        job = self.store.get(job_id, actor)
        self._audit(
            actor,
            "review.read",
            "review_job",
            job_id,
            "allow",
            repository_id=str(job["repository_id"]),
        )
        return job

    def get_trace(self, job_id: str, *, principal: Principal | None = None) -> str:
        actor = self._principal(principal)
        self._require(actor, Permission.READ, "trace.read")
        trace = self.store.read_trace(job_id, actor)
        job = self.store.get(job_id, actor)
        self._audit(
            actor,
            "trace.read",
            "review_trace",
            job_id,
            "allow",
            repository_id=str(job["repository_id"]),
        )
        return trace

    @staticmethod
    def _finding_response(record: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(record)
        payload = result.pop("payload_json", None)
        if isinstance(payload, str):
            result["finding"] = json.loads(payload)
        return result

    def list_findings(
        self, job_id: str, *, principal: Principal | None = None
    ) -> list[dict[str, Any]]:
        actor = self._principal(principal)
        self._require(actor, Permission.READ, "finding.list")
        job = self.store.get(job_id, actor)
        records = self.store.database.findings_for_review(actor, job_id)
        self._audit(
            actor,
            "finding.list",
            "review_job",
            job_id,
            "allow",
            repository_id=str(job["repository_id"]),
        )
        return [self._finding_response(record) for record in records]

    def get_finding(
        self, finding_id: str, *, principal: Principal | None = None
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.READ, "finding.read")
        record = self.store.database.finding_detail(actor, finding_id)
        if record is None:
            raise JobNotFound("finding was not found")
        self._audit(
            actor,
            "finding.read",
            "finding",
            finding_id,
            "allow",
            repository_id=str(record["repository_id"]),
        )
        return self._finding_response(record)

    def principal_record(self, principal: Principal | None = None) -> dict[str, Any]:
        actor = self._principal(principal)
        return {
            "principal_id": actor.principal_id,
            "user_id": actor.user_id,
            "organization_id": actor.organization_id,
            "role": actor.role.value,
            "auth_method": actor.auth_method,
            "credential_id": actor.credential_id,
        }

    def list_members(self, principal: Principal | None = None) -> list[dict[str, Any]]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_MEMBERS, "membership.list")
        return self.store.database.list_members(actor.organization_id)

    def create_member(
        self,
        *,
        subject: str,
        display_name: str,
        role: Role,
        repository_ids: Iterable[str],
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_MEMBERS, "membership.create")
        record = self.store.database.create_membership(
            actor.organization_id,
            subject=subject,
            display_name=display_name,
            role=role,
            repository_ids=repository_ids,
        )
        self._audit(
            actor,
            "membership.create",
            "membership",
            str(record["membership_id"]),
            "allow",
        )
        return record

    def update_member(
        self,
        membership_id: str,
        *,
        role: Role,
        repository_ids: Iterable[str],
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_MEMBERS, "membership.update")
        own_membership = next(
            (
                item
                for item in self.store.database.list_members(actor.organization_id)
                if item["membership_id"] == membership_id
            ),
            None,
        )
        if own_membership is not None and own_membership["user_id"] == actor.user_id:
            self._audit(
                actor,
                "membership.update",
                "membership",
                membership_id,
                "deny",
                reason_code="self_role_change",
            )
            raise AuthorizationDenied("self role changes are not permitted")
        record = self.store.database.update_membership(
            actor.organization_id,
            membership_id,
            role=role,
            repository_ids=repository_ids,
        )
        if record is None:
            raise JobNotFound("membership was not found")
        self._audit(
            actor,
            "membership.update",
            "membership",
            membership_id,
            "allow",
        )
        return record

    def list_repositories(
        self, principal: Principal | None = None
    ) -> list[dict[str, Any]]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_REPOSITORIES, "repository.list")
        return self.store.database.list_repositories(actor.organization_id)

    def register_repository(
        self,
        repository: str,
        *,
        mode: str,
        budget_microusd: int | None,
        policy_version: str,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_REPOSITORIES, "repository.create")
        alias, _ = self.registry.resolve(repository)
        record = self.store.database.register_repository(
            actor.organization_id,
            alias,
            mode=mode,
            budget_microusd=budget_microusd,
            policy_version=policy_version,
        )
        self._audit(
            actor,
            "repository.create",
            "repository",
            str(record["id"]),
            "allow",
            repository_id=str(record["id"]),
            reason_code=None,
        )
        return record

    def update_repository(
        self,
        repository_id: str,
        *,
        mode: str,
        budget_microusd: int | None,
        policy_version: str,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.MANAGE_REPOSITORIES, "repository.update")
        record = self.store.database.update_repository(
            actor.organization_id,
            repository_id,
            mode=mode,
            budget_microusd=budget_microusd,
            policy_version=policy_version,
        )
        if record is None:
            raise JobNotFound("repository was not found")
        self._audit(
            actor,
            "repository.update",
            "repository",
            repository_id,
            "allow",
            repository_id=repository_id,
        )
        return record

    def create_credential(
        self,
        *,
        expires_in_seconds: int,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        record = self.store.database.create_credential(
            actor, expires_in_seconds=expires_in_seconds
        )
        self._audit(
            actor,
            "credential.create",
            "access_credential",
            str(record["credential_id"]),
            "allow",
        )
        return record

    def revoke_credential(
        self, credential_id: str, *, principal: Principal | None = None
    ) -> None:
        actor = self._principal(principal)
        allow_any = actor.allows(Permission.MANAGE_CREDENTIALS)
        if not self.store.database.revoke_credential(
            actor, credential_id, allow_any_user=allow_any
        ):
            raise JobNotFound("credential was not found")
        self._audit(
            actor,
            "credential.revoke",
            "access_credential",
            credential_id,
            "allow",
        )

    def list_audit(
        self, *, limit: int, principal: Principal | None = None
    ) -> list[dict[str, Any]]:
        actor = self._principal(principal)
        self._require(actor, Permission.READ_AUDIT, "audit.list")
        return self.store.database.list_audit_events(actor.organization_id, limit=limit)

    def submit_feedback(
        self,
        finding_id: str,
        *,
        decision: str,
        reason: str | None,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.SUBMIT_FEEDBACK, "finding.feedback")
        finding = self.store.database.finding_for_principal(actor, finding_id)
        if finding is None:
            raise JobNotFound("finding was not found")
        record = self.store.database.create_feedback(
            actor, finding, decision=decision, reason=reason
        )
        self._audit(
            actor,
            "finding.feedback",
            "finding",
            finding_id,
            "allow",
            repository_id=str(finding["repository_id"]),
        )
        return record

    def decide_finding(
        self,
        finding_id: str,
        *,
        decision: str,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        self._require(actor, Permission.DECIDE_FINDING, "finding.decide")
        finding = self.store.database.finding_for_principal(actor, finding_id)
        if finding is None:
            raise JobNotFound("finding was not found")
        repository = self.store.database.authorized_repository(
            actor, str(finding["repository_id"])
        )
        if repository is None:
            raise JobNotFound("finding was not found")
        record = self.store.database.decide_finding(
            actor,
            finding,
            decision=decision,
            policy_version=str(repository["policy_version"]),
        )
        self._audit(
            actor,
            "finding.decide",
            "finding",
            finding_id,
            "allow",
            repository_id=str(finding["repository_id"]),
        )
        return record

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
    database_url = os.environ.get("CRAG_DATABASE_URL") or sqlite_database_url(
        state / "reviews.sqlite3"
    )
    local_mode = os.environ.get("CRAG_ALLOW_LOCAL_TOKEN", "").casefold() in {
        "1",
        "true",
        "yes",
    }
    auto_migrate = os.environ.get("CRAG_AUTO_MIGRATE", "").casefold() in {
        "1",
        "true",
        "yes",
    }
    if auto_migrate and not local_mode:
        raise InvalidRequest("CRAG_AUTO_MIGRATE requires explicit local token mode")
    store = JobStore(
        state,
        database_url=database_url,
        auto_migrate=auto_migrate,
    )
    return ReviewService(
        registry,
        store,
        runner=runner,
        workers=workers,
        local_mode=local_mode,
    )
