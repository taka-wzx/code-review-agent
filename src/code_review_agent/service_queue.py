"""Durable Postgres-first queue primitives for the Phase 9C review service.

The database owns logical job identity, quotas, leases, and fencing.  Bounded
inline diffs remain outside business tables in a private shared artifact
directory and are addressed only by opaque job-derived keys.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Iterator, Mapping
import uuid
import warnings

from sqlalchemy import text
from sqlalchemy.engine import Connection

from code_review_agent.database import (
    Database,
    MigrationRequired,
    new_id,
    require_schema_head,
    sqlite_database_url,
    upgrade_database,
)
from code_review_agent.identity import Principal


SCHEMA_VERSION = "crag.service/v1alpha1"
MAX_TRACE_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_RESULT_FINDINGS = 1000
MAX_PAYLOAD_BYTES = 512 * 1024
MAX_RETRY_DELAY_SECONDS = 7 * 24 * 60 * 60
DEFAULT_TEMP_ARTIFACT_MIN_AGE_SECONDS = 300
_JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
_PAYLOAD_KEY = re.compile(r"[0-9a-f]{32}\.diff\Z")
_PAYLOAD_TEMP_KEY = re.compile(
    r"\.([0-9a-f]{32}\.diff)\.([0-9a-f]{32})\.tmp\Z"
)
_TRACE_KEY = re.compile(r"[0-9a-f]{32}\.[1-9][0-9]*\.[0-9a-f]{32}\.jsonl\Z")


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
    """Retained as an import-compatible Phase 9B error; no longer raised."""

    code = "state_directory_in_use"


class IdempotencyConflict(ServiceError):
    code = "idempotency_conflict"


class LeaseLost(ServiceError):
    code = "lease_lost"


class QuotaExceeded(ServiceError):
    code = "quota_exceeded"

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class QueueFull(QuotaExceeded):
    code = "queue_full"


class SubmissionRateLimited(QuotaExceeded):
    code = "submission_rate_limited"


class ModelBudgetExhausted(QuotaExceeded):
    code = "model_budget_exhausted"


class JobState(str, Enum):
    RECEIVED = "received"
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    # Source-compatible name; the serialized Phase 9C value remains
    # ``awaiting_approval`` rather than the removed ``succeeded`` value.
    SUCCEEDED = "awaiting_approval"
    APPROVED = "approved"
    PUBLISHED = "published"
    DECLINED = "declined"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


TERMINAL_JOB_STATES = frozenset(
    {
        JobState.AWAITING_APPROVAL.value,
        JobState.PUBLISHED.value,
        JobState.DECLINED.value,
        JobState.FAILED.value,
        JobState.DEAD_LETTER.value,
    }
)


@dataclass(frozen=True)
class JobLease:
    job_id: str
    organization_id: str
    repository_id: str
    repository_alias: str
    source_kind: str
    source_ref: str
    source_sha256: str
    head_sha: str | None
    submitted_by: str
    correlation_id: str
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime
    attempt_count: int
    max_attempts: int
    model_call_limit: int
    payload_key: str | None


@dataclass(frozen=True)
class FailureOutcome:
    state: str
    retry_scheduled: bool
    available_at: datetime | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _database_clock(
    connection: Connection, injected: datetime | None = None
) -> datetime:
    """Return the authoritative clock for transaction and lease decisions.

    Postgres workers may run on hosts with skewed clocks, so an injected or
    application clock is never authoritative there.  SQLite keeps the injected
    clock used by deterministic local compatibility tests.
    """

    if connection.dialect.name == "postgresql":
        value = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
        current = _coerce_datetime(value)
        if current is None:
            raise RuntimeError("database clock is unavailable")
        return current
    return (injected or utc_now()).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _public_datetime(value: Any) -> str | None:
    parsed = _coerce_datetime(value)
    return _iso(parsed) if parsed is not None else None


def _job_id(value: str) -> str:
    if not isinstance(value, str) or _JOB_ID.fullmatch(value) is None:
        raise JobNotFound("review job was not found")
    return value


def _row_mapping(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def _request_fingerprint(
    organization_id: str,
    repository_id: str,
    source_kind: str,
    source_ref: str,
    source_sha256: str,
    head_sha: str | None,
) -> str:
    material = "\0".join(
        (
            organization_id,
            repository_id,
            source_kind,
            source_ref,
            source_sha256,
            head_sha or "",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class JobStore:
    """Tenant-aware durable job, quota, lease, and artifact store.

    No Phase 9C path acquires a state-directory process lock.  Postgres row
    locks are authoritative; SQLite uses ``BEGIN IMMEDIATE`` only as a local
    compatibility serialization boundary.
    """

    ORG_QUOTA_DEFAULTS = {
        "max_queued_jobs": 1000,
        "max_concurrent_jobs": 16,
        "submission_rate_limit": 600,
        "submission_window_seconds": 60,
        "monthly_model_call_budget": 100000,
        "model_call_limit_per_job": 64,
    }
    REPOSITORY_QUOTA_DEFAULTS = {
        "max_queued_jobs": 100,
        "max_concurrent_jobs": 2,
        "submission_rate_limit": 60,
        "submission_window_seconds": 60,
        "monthly_model_call_budget": 10000,
        "model_call_limit_per_job": 64,
    }
    QUOTA_BOUNDS = {
        "max_queued_jobs": (1, 100000),
        "max_concurrent_jobs": (1, 64),
        "submission_rate_limit": (1, 100000),
        "submission_window_seconds": (1, 86400),
        "monthly_model_call_budget": (1, 1000000000),
        "model_call_limit_per_job": (1, 256),
    }
    CLAIM_PAGE_SIZE = 32
    CLAIM_MAX_PAGES = 4
    RECEIVED_RECONCILE_BATCH_SIZE = 32

    def __init__(
        self,
        state_dir: Path,
        *,
        database_url: str | None = None,
        auto_migrate: bool = True,
        job_data_dir: Path | None = None,
        trace_dir: Path | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.job_data_dir = Path(job_data_dir or (self.state_dir / "jobs")).resolve()
        self.trace_dir = Path(trace_dir or (self.state_dir / "traces")).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.job_data_dir.mkdir(parents=True, exist_ok=True)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.state_dir.chmod(0o700)
            self.job_data_dir.chmod(0o700)
            self.trace_dir.chmod(0o700)
        self.database_path = self.state_dir / "reviews.sqlite3"
        self.database_url = database_url or sqlite_database_url(self.database_path)
        self._closed = False
        self._local_principal: Principal | None = None
        self._compat_leases: dict[str, JobLease] = {}
        self._claim_cursor = 0
        self._claim_cursor_lock = threading.Lock()
        self._cleanup_offsets = {"payload": 0, "trace": 0}
        self._cleanup_cursor_lock = threading.Lock()
        if auto_migrate:
            if not self.database_url.startswith("sqlite"):
                raise MigrationRequired("automatic migration is only allowed for local SQLite")
            upgrade_database(self.database_url)
        else:
            require_schema_head(self.database_url)
        self.database = Database(self.database_url, check_schema=False)
        if auto_migrate:
            self._local_principal = self.database.bootstrap_local(())

    @property
    def local_principal(self) -> Principal | None:
        return self._local_principal

    def bootstrap_local(self, repository_aliases: Any) -> Principal:
        self._local_principal = self.database.bootstrap_local(repository_aliases)
        return self._local_principal

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.database.close()

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[Connection]:
        connection = self.database.engine.connect()
        transaction = None
        sqlite_immediate = immediate and connection.dialect.name == "sqlite"
        try:
            if sqlite_immediate:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                transaction = connection.begin()
            yield connection
            if sqlite_immediate:
                connection.commit()
            elif transaction is not None:
                transaction.commit()
        except BaseException:
            if sqlite_immediate:
                connection.rollback()
            elif transaction is not None and transaction.is_active:
                transaction.rollback()
            raise
        finally:
            connection.close()

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

    def repository_scope_active(
        self, organization_id: str, repository_id: str
    ) -> bool:
        """Check the worker's system-actor organization/repository lineage."""
        return (
            self.database.repository_in_organization(
                organization_id, repository_id, active_only=True
            )
            is not None
        )

    def _ensure_quota_rows(
        self,
        connection: Connection,
        organization_id: str,
        repository_id: str | None,
        now: datetime,
    ) -> None:
        statement = text(
            "INSERT INTO service_quotas "
            "(id, organization_id, repository_id, scope_kind, max_queued_jobs, "
            "max_concurrent_jobs, submission_rate_limit, submission_window_seconds, "
            "submission_window_started_at, submission_window_count, "
            "monthly_model_call_budget, model_call_month, monthly_model_calls_used, "
            "monthly_model_calls_reserved, model_call_limit_per_job, created_at, updated_at) "
            "VALUES (:id, :org, :repo, :kind, :queued, :concurrent, :rate, :window, "
            ":window_started, 0, :budget, :month, 0, 0, :per_job, :created, :updated) "
            "ON CONFLICT DO NOTHING"
        )
        scopes: list[tuple[str, str | None, dict[str, int]]] = [
            ("organization", None, self.ORG_QUOTA_DEFAULTS)
        ]
        if repository_id is not None:
            scopes.append(
                ("repository", repository_id, self.REPOSITORY_QUOTA_DEFAULTS)
            )
        for scope_kind, scope_repository, defaults in scopes:
            connection.execute(
                statement,
                {
                    "id": new_id(),
                    "org": organization_id,
                    "repo": scope_repository,
                    "kind": scope_kind,
                    "queued": defaults["max_queued_jobs"],
                    "concurrent": defaults["max_concurrent_jobs"],
                    "rate": defaults["submission_rate_limit"],
                    "window": defaults["submission_window_seconds"],
                    "window_started": now,
                    "budget": defaults["monthly_model_call_budget"],
                    "month": now.strftime("%Y-%m"),
                    "per_job": defaults["model_call_limit_per_job"],
                    "created": now,
                    "updated": now,
                },
            )

    def _lock_quotas(
        self,
        connection: Connection,
        organization_id: str,
        repository_id: str,
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._ensure_quota_rows(connection, organization_id, repository_id, now)
        suffix = " FOR UPDATE" if connection.dialect.name == "postgresql" else ""
        rows = connection.execute(
            text(
                "SELECT * FROM service_quotas WHERE organization_id=:org AND "
                "((scope_kind='organization' AND repository_id IS NULL) OR "
                "(scope_kind='repository' AND repository_id=:repo)) "
                "ORDER BY CASE scope_kind WHEN 'organization' THEN 0 ELSE 1 END" + suffix
            ),
            {"org": organization_id, "repo": repository_id},
        ).all()
        if len(rows) != 2:
            raise RuntimeError("service quota rows are incomplete")
        quotas = [_row_mapping(row) for row in rows]
        for quota in quotas:
            updates: dict[str, Any] = {}
            window_started = _coerce_datetime(quota["submission_window_started_at"])
            if window_started is None or now >= window_started + timedelta(
                seconds=int(quota["submission_window_seconds"])
            ):
                updates["submission_window_started_at"] = now
                updates["submission_window_count"] = 0
            month = now.strftime("%Y-%m")
            if quota.get("model_call_month") != month:
                carry_parameters: dict[str, Any] = {"org": organization_id}
                carry_repository = ""
                if quota["scope_kind"] == "repository":
                    carry_repository = " AND repository_id=:repo"
                    carry_parameters["repo"] = repository_id
                carry = connection.execute(
                    text(
                        "SELECT COALESCE(SUM(model_calls_reserved), 0) FROM review_jobs "
                        "WHERE organization_id=:org AND model_calls_reserved>0"
                        + carry_repository
                    ),
                    carry_parameters,
                ).scalar_one()
                updates["model_call_month"] = month
                updates["monthly_model_calls_used"] = 0
                updates["monthly_model_calls_reserved"] = int(carry or 0)
            if updates:
                assignments = ", ".join(f"{key}=:{key}" for key in updates)
                connection.execute(
                    text(
                        f"UPDATE service_quotas SET {assignments}, updated_at=:updated "
                        "WHERE id=:id"
                    ),
                    {**updates, "updated": now, "id": quota["id"]},
                )
                quota.update(updates)
        return quotas[0], quotas[1]

    def configure_quota(
        self,
        organization_id: str,
        *,
        repository_id: str | None = None,
        **values: int | None,
    ) -> dict[str, Any]:
        unknown = set(values) - set(self.QUOTA_BOUNDS)
        if unknown:
            raise InvalidRequest("unknown service quota field")
        for name, value in values.items():
            if value is None and name == "monthly_model_call_budget":
                continue
            lower, upper = self.QUOTA_BOUNDS[name]
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise InvalidRequest(f"{name} is outside the supported range")
        with self._transaction(immediate=True) as connection:
            now = _database_clock(connection)
            if repository_id is None:
                self._ensure_quota_rows(connection, organization_id, None, now)
                clause = "scope_kind='organization' AND repository_id IS NULL"
                parameters: dict[str, Any] = {"org": organization_id}
            else:
                self._ensure_quota_rows(connection, organization_id, repository_id, now)
                clause = "scope_kind='repository' AND repository_id=:repo"
                parameters = {"org": organization_id, "repo": repository_id}
            if values:
                assignments = ", ".join(f"{name}=:{name}" for name in values)
                result = connection.execute(
                    text(
                        f"UPDATE service_quotas SET {assignments}, updated_at=:updated "
                        f"WHERE organization_id=:org AND {clause}"
                    ),
                    {**parameters, **values, "updated": now},
                )
                if result.rowcount != 1:
                    raise JobNotFound("service quota was not found")
            row = connection.execute(
                text(
                    f"SELECT * FROM service_quotas WHERE organization_id=:org AND {clause}"
                ),
                parameters,
            ).one()
            return _row_mapping(row)

    def get_quota(
        self, organization_id: str, *, repository_id: str | None = None
    ) -> dict[str, Any]:
        clause = (
            "scope_kind='organization' AND repository_id IS NULL"
            if repository_id is None
            else "scope_kind='repository' AND repository_id=:repo"
        )
        parameters = {"org": organization_id, "repo": repository_id}
        with self.database.engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT * FROM service_quotas WHERE organization_id=:org AND {clause}"
                ),
                parameters,
            ).first()
        if row is None:
            raise JobNotFound("service quota was not found")
        return _row_mapping(row)

    @staticmethod
    def _find_duplicate(
        connection: Connection,
        organization_id: str,
        submission_key: str,
        idempotency_key_hash: str | None,
    ) -> dict[str, Any] | None:
        if idempotency_key_hash is not None:
            parameters = {
                "org": organization_id,
                "idempotency": idempotency_key_hash,
            }
            row = connection.execute(
                text(
                    "SELECT j.* FROM review_idempotency_keys k JOIN review_jobs j "
                    "ON j.organization_id=k.organization_id AND j.id=k.review_job_id "
                    "WHERE k.organization_id=:org AND k.key_hash=:idempotency"
                ),
                parameters,
            ).first()
            if row is not None:
                return _row_mapping(row)
            row = connection.execute(
                text(
                    "SELECT * FROM review_jobs WHERE organization_id=:org "
                    "AND idempotency_key_hash=:idempotency"
                ),
                parameters,
            ).first()
            if row is not None:
                return _row_mapping(row)
        row = connection.execute(
            text(
                "SELECT * FROM review_jobs WHERE organization_id=:org "
                "AND submission_key=:submission ORDER BY created_at LIMIT 1"
            ),
            {"org": organization_id, "submission": submission_key},
        ).first()
        return None if row is None else _row_mapping(row)

    @staticmethod
    def _bind_idempotency_key(
        connection: Connection,
        *,
        organization_id: str,
        job_id: str,
        idempotency_key_hash: str | None,
        request_fingerprint: str,
        now: datetime,
    ) -> None:
        if idempotency_key_hash is None:
            return
        connection.execute(
            text(
                "INSERT INTO review_idempotency_keys "
                "(id, organization_id, key_hash, review_job_id, request_fingerprint, "
                "created_at) VALUES (:id, :org, :key, :job, :fingerprint, :created) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": new_id(),
                "org": organization_id,
                "key": idempotency_key_hash,
                "job": job_id,
                "fingerprint": request_fingerprint,
                "created": now,
            },
        )
        bound = connection.execute(
            text(
                "SELECT review_job_id, request_fingerprint FROM review_idempotency_keys "
                "WHERE organization_id=:org AND key_hash=:key"
            ),
            {"org": organization_id, "key": idempotency_key_hash},
        ).one()
        if (
            str(bound._mapping["review_job_id"]) != job_id
            or str(bound._mapping["request_fingerprint"]) != request_fingerprint
        ):
            raise IdempotencyConflict("idempotency key was reused for another request")
        connection.execute(
            text(
                "UPDATE review_jobs SET idempotency_key_hash=:key "
                "WHERE id=:job AND organization_id=:org "
                "AND idempotency_key_hash IS NULL"
            ),
            {"key": idempotency_key_hash, "job": job_id, "org": organization_id},
        )

    @staticmethod
    def _assert_duplicate_matches(
        record: Mapping[str, Any], request_fingerprint: str
    ) -> None:
        if record.get("request_fingerprint") != request_fingerprint:
            raise IdempotencyConflict("idempotency key was reused for another request")

    @staticmethod
    def _record_delivery(
        connection: Connection,
        *,
        organization_id: str,
        repository_id: str,
        job_id: str,
        delivery_id: str,
        event: str,
        received_at: datetime,
    ) -> None:
        connection.execute(
            text(
                "INSERT INTO webhook_deliveries "
                "(id, organization_id, repository_id, review_job_id, delivery_id, event, "
                "received_at) VALUES (:id, :org, :repo, :job, :delivery, :event, :received) "
                "ON CONFLICT(delivery_id) DO NOTHING"
            ),
            {
                "id": new_id(),
                "org": organization_id,
                "repo": repository_id,
                "job": job_id,
                "delivery": delivery_id,
                "event": event,
                "received": _iso(received_at),
            },
        )
        existing = connection.execute(
            text(
                "SELECT review_job_id FROM webhook_deliveries WHERE delivery_id=:delivery"
            ),
            {"delivery": delivery_id},
        ).one()
        if str(existing._mapping["review_job_id"]) != job_id:
            raise IdempotencyConflict("webhook delivery identity conflicts with a job")

    @staticmethod
    def _queue_counts(
        connection: Connection, organization_id: str, repository_id: str
    ) -> tuple[int, int]:
        row = connection.execute(
            text(
                "SELECT "
                "SUM(CASE WHEN organization_id=:org THEN 1 ELSE 0 END) AS org_count, "
                "SUM(CASE WHEN organization_id=:org AND repository_id=:repo THEN 1 ELSE 0 END) "
                "AS repo_count FROM review_jobs WHERE state IN ('received','queued')"
            ),
            {"org": organization_id, "repo": repository_id},
        ).one()
        return int(row._mapping["org_count"] or 0), int(row._mapping["repo_count"] or 0)

    @staticmethod
    def _check_rate(quota: Mapping[str, Any], now: datetime) -> None:
        if int(quota["submission_window_count"]) < int(quota["submission_rate_limit"]):
            return
        started = _coerce_datetime(quota["submission_window_started_at"]) or now
        retry = max(
            1,
            math.ceil(
                (
                    started
                    + timedelta(seconds=int(quota["submission_window_seconds"]))
                    - now
                ).total_seconds()
            ),
        )
        raise SubmissionRateLimited("submission rate is exhausted", retry_after=retry)

    @staticmethod
    def _check_budget(quota: Mapping[str, Any], reservation: int) -> None:
        budget = quota.get("monthly_model_call_budget")
        if budget is None:
            return
        committed = int(quota["monthly_model_calls_used"]) + int(
            quota["monthly_model_calls_reserved"]
        )
        if committed + reservation > int(budget):
            raise ModelBudgetExhausted("model call budget is exhausted")

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
        principal: Principal | None = None,
        correlation_id: str | None = None,
        submission_key: str | None = None,
        idempotency_key_hash: str | None = None,
        payload: str | None = None,
        head_sha: str | None = None,
        max_attempts: int = 3,
        now: datetime | None = None,
    ) -> tuple[str, bool]:
        if principal is not None:
            authorized = self.database.authorized_repository(principal, repository)
            if authorized is None:
                raise AuthorizationDenied("repository access is not permitted")
            expected = (
                principal.organization_id,
                str(authorized["id"]),
                principal.user_id,
            )
            supplied = (organization_id, repository_id, submitted_by)
            if any(value is not None for value in supplied) and supplied != expected:
                raise AuthorizationDenied("submission tenant identity is invalid")
            organization_id, repository_id, submitted_by = expected
        elif organization_id is None or repository_id is None or submitted_by is None:
            principal, record = self._local_repository(repository)
            organization_id = principal.organization_id
            repository_id = str(record["id"])
            submitted_by = principal.user_id
        if organization_id is None or repository_id is None or submitted_by is None:
            raise AuthorizationDenied("submission tenant identity is required")
        scope = self.database.repository_in_organization(
            organization_id, repository_id, active_only=True
        )
        if scope is None or str(scope["alias"]).casefold() != repository.casefold():
            raise AuthorizationDenied("submission repository scope is not permitted")
        current = (now or utc_now()).astimezone(timezone.utc)
        source_fingerprint = _request_fingerprint(
            organization_id,
            repository_id,
            source_kind,
            source_ref,
            source_sha256,
            head_sha,
        )
        submission = submission_key or hashlib.sha256(
            (source_fingerprint + "\0logical").encode("utf-8")
        ).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", submission):
            raise InvalidRequest("submission key is invalid")
        # The submission identity carries policy version and other logical-job
        # boundaries supplied by ReviewService.  Bind it into the request
        # fingerprint so a caller cannot reuse an explicit idempotency key
        # across a policy change and silently receive the older logical job.
        fingerprint = hashlib.sha256(
            f"{source_fingerprint}\0{submission}".encode("utf-8")
        ).hexdigest()
        if idempotency_key_hash is not None and re.fullmatch(
            r"[0-9a-f]{64}", idempotency_key_hash
        ) is None:
            raise InvalidRequest("idempotency key hash is invalid")
        if isinstance(max_attempts, bool) or not 1 <= max_attempts <= 10:
            raise InvalidRequest("max attempts must be between 1 and 10")
        correlation = correlation_id or uuid.uuid4().hex
        job_id = uuid.uuid4().hex
        payload_key = f"{job_id}.diff" if payload is not None else None
        needs_finalize = False

        with self._transaction(immediate=True) as connection:
            current = _database_clock(connection, now)
            duplicate = self._find_duplicate(
                connection, organization_id, submission, idempotency_key_hash
            )
            if duplicate is not None:
                self._assert_duplicate_matches(duplicate, fingerprint)
                job_id = str(duplicate["id"])
                self._bind_idempotency_key(
                    connection,
                    organization_id=organization_id,
                    job_id=job_id,
                    idempotency_key_hash=idempotency_key_hash,
                    request_fingerprint=fingerprint,
                    now=current,
                )
                if delivery_id is not None:
                    self._record_delivery(
                        connection,
                        organization_id=organization_id,
                        repository_id=repository_id,
                        job_id=job_id,
                        delivery_id=delivery_id,
                        event=event,
                        received_at=current,
                    )
                existing_payload = duplicate.get("payload_key")
                payload_key = str(existing_payload) if existing_payload else payload_key
                inserted = False
                needs_finalize = duplicate.get("state") == JobState.RECEIVED.value
            else:
                org_quota, repo_quota = self._lock_quotas(
                    connection, organization_id, repository_id, current
                )
                duplicate = self._find_duplicate(
                    connection, organization_id, submission, idempotency_key_hash
                )
                if duplicate is not None:
                    self._assert_duplicate_matches(duplicate, fingerprint)
                    job_id = str(duplicate["id"])
                    self._bind_idempotency_key(
                        connection,
                        organization_id=organization_id,
                        job_id=job_id,
                        idempotency_key_hash=idempotency_key_hash,
                        request_fingerprint=fingerprint,
                        now=current,
                    )
                    if delivery_id is not None:
                        self._record_delivery(
                            connection,
                            organization_id=organization_id,
                            repository_id=repository_id,
                            job_id=job_id,
                            delivery_id=delivery_id,
                            event=event,
                            received_at=current,
                        )
                    existing_payload = duplicate.get("payload_key")
                    payload_key = str(existing_payload) if existing_payload else payload_key
                    inserted = False
                    needs_finalize = duplicate.get("state") == JobState.RECEIVED.value
                else:
                    org_count, repo_count = self._queue_counts(
                        connection, organization_id, repository_id
                    )
                    if org_count >= int(org_quota["max_queued_jobs"]):
                        raise QueueFull("organization queue is full", retry_after=1)
                    if repo_count >= int(repo_quota["max_queued_jobs"]):
                        raise QueueFull("repository queue is full", retry_after=1)
                    self._check_rate(org_quota, current)
                    self._check_rate(repo_quota, current)
                    model_call_limit = min(
                        int(org_quota["model_call_limit_per_job"]),
                        int(repo_quota["model_call_limit_per_job"]),
                    )
                    self._check_budget(org_quota, model_call_limit)
                    self._check_budget(repo_quota, model_call_limit)
                    connection.execute(
                        text(
                            "INSERT INTO review_jobs "
                            "(id, organization_id, repository_id, submitted_by, correlation_id, "
                            "source_kind, repository_alias, source_ref, source_sha256, source_bytes, "
                            "state, created_at, started_at, completed_at, review_json, error_code, "
                            "submission_key, idempotency_key_hash, request_fingerprint, payload_key, "
                            "head_sha, queued_at, available_at, lease_owner, lease_token, "
                            "lease_expires_at, heartbeat_at, attempt_count, max_attempts, "
                            "last_error_category, model_call_limit, model_calls_reserved, "
                            "final_trace_key, updated_at) VALUES "
                            "(:id, :org, :repo, :actor, :correlation, :kind, :alias, :ref, :sha, "
                            ":bytes, :state, :created, NULL, NULL, NULL, NULL, :submission, "
                            ":idempotency, :fingerprint, :payload, :head, NULL, :available, NULL, "
                            "NULL, NULL, NULL, 0, :max_attempts, NULL, :model_limit, :reserved, "
                            "NULL, :updated)"
                        ),
                        {
                            "id": job_id,
                            "org": organization_id,
                            "repo": repository_id,
                            "actor": submitted_by,
                            "correlation": correlation,
                            "kind": source_kind,
                            "alias": repository,
                            "ref": source_ref,
                            "sha": source_sha256,
                            "bytes": source_bytes,
                            "state": JobState.RECEIVED.value,
                            "created": _iso(current),
                            "submission": submission,
                            "idempotency": idempotency_key_hash,
                            "fingerprint": fingerprint,
                            "payload": payload_key,
                            "head": head_sha,
                            "available": current,
                            "max_attempts": max_attempts,
                            "model_limit": model_call_limit,
                            "reserved": model_call_limit,
                            "updated": current,
                        },
                    )
                    self._bind_idempotency_key(
                        connection,
                        organization_id=organization_id,
                        job_id=job_id,
                        idempotency_key_hash=idempotency_key_hash,
                        request_fingerprint=fingerprint,
                        now=current,
                    )
                    for quota in (org_quota, repo_quota):
                        connection.execute(
                            text(
                                "UPDATE service_quotas SET "
                                "submission_window_count=submission_window_count+1, "
                                "monthly_model_calls_reserved=monthly_model_calls_reserved+:calls, "
                                "updated_at=:updated WHERE id=:id"
                            ),
                            {
                                "calls": model_call_limit,
                                "updated": current,
                                "id": quota["id"],
                            },
                        )
                    connection.execute(
                        text(
                            "INSERT INTO submission_events "
                            "(id, organization_id, repository_id, review_job_id, occurred_at) "
                            "VALUES (:id, :org, :repo, :job, :occurred)"
                        ),
                        {
                            "id": new_id(),
                            "org": organization_id,
                            "repo": repository_id,
                            "job": job_id,
                            "occurred": current,
                        },
                    )
                    if delivery_id is not None:
                        self._record_delivery(
                            connection,
                            organization_id=organization_id,
                            repository_id=repository_id,
                            job_id=job_id,
                            delivery_id=delivery_id,
                            event=event,
                            received_at=current,
                        )
                    inserted = True
                    needs_finalize = True

        if needs_finalize and payload is not None:
            try:
                self._write_payload(job_id, payload, source_sha256)
            except BaseException:
                if inserted:
                    self.fail_received(job_id, "payload_unavailable", now=current)
                raise
        if needs_finalize:
            self.finalize_received(job_id, payload_key=payload_key, now=current)
        return job_id, not inserted

    def _payload_path(self, payload_key: str) -> Path:
        if _PAYLOAD_KEY.fullmatch(payload_key) is None:
            raise InvalidRequest("job payload key is invalid")
        return self.job_data_dir / payload_key

    def _write_payload(self, job_id: str, payload: str, expected_sha256: str) -> None:
        encoded = payload.encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != expected_sha256:
            raise InvalidRequest("job payload fingerprint does not match")
        final_path = self._payload_path(f"{_job_id(job_id)}.diff")
        temporary = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, final_path)
            if os.name != "nt":
                descriptor = os.open(self.job_data_dir, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        if hashlib.sha256(final_path.read_bytes()).hexdigest() != expected_sha256:
            raise InvalidRequest("job payload could not be verified")

    def finalize_received(
        self,
        job_id: str,
        *,
        payload_key: str | None,
        now: datetime | None = None,
    ) -> None:
        with self._transaction(immediate=True) as connection:
            current = _database_clock(connection, now)
            row = self._locked_job(connection, job_id)
            if row["state"] != JobState.RECEIVED.value:
                return
            stored_payload = row["payload_key"]
            if stored_payload is not None:
                if payload_key is None or not self._payload_path(str(stored_payload)).is_file():
                    raise InvalidRequest("job payload is not durable")
            result = connection.execute(
                text(
                    "UPDATE review_jobs SET state=:queued, queued_at=:queued_at, "
                    "available_at=:available, updated_at=:updated WHERE id=:id AND state=:received"
                ),
                {
                    "queued": JobState.QUEUED.value,
                    "queued_at": current,
                    "available": current,
                    "updated": current,
                    "id": _job_id(job_id),
                    "received": JobState.RECEIVED.value,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError("received review could not be queued")

    def fail_received(
        self, job_id: str, error_code: str, *, now: datetime | None = None
    ) -> bool:
        scope = self._scope(job_id)
        with self._transaction(immediate=True) as connection:
            current = _database_clock(connection, now)
            org_quota, repo_quota = self._lock_quotas(
                connection,
                str(scope["organization_id"]),
                str(scope["repository_id"]),
                current,
            )
            row = self._locked_job(connection, job_id)
            if row["state"] != JobState.RECEIVED.value:
                return False
            reserved = int(row.get("model_calls_reserved") or 0)
            for quota in (org_quota, repo_quota):
                connection.execute(
                    text(
                        "UPDATE service_quotas SET monthly_model_calls_reserved="
                        "CASE WHEN monthly_model_calls_reserved>=:reserved THEN "
                        "monthly_model_calls_reserved-:reserved ELSE 0 END, updated_at=:updated "
                        "WHERE id=:id"
                    ),
                    {"reserved": reserved, "updated": current, "id": quota["id"]},
                )
            result = connection.execute(
                text(
                    "UPDATE review_jobs SET state=:failed, completed_at=:completed, "
                    "error_code=:error, last_error_category=:error, model_calls_reserved=0, "
                    "updated_at=:updated WHERE id=:id AND state=:received"
                ),
                {
                    "failed": JobState.FAILED.value,
                    "completed": _iso(current),
                    "error": error_code[:64],
                    "updated": current,
                    "id": _job_id(job_id),
                    "received": JobState.RECEIVED.value,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError("received review could not be failed")
            self._insert_job_audit(
                connection,
                row,
                action="review.failed",
                decision="error",
                reason_code=error_code,
                attempt_count=int(row.get("attempt_count") or 0),
                now=current,
            )
            return True

    def _scope(self, job_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, organization_id, repository_id FROM review_jobs WHERE id=:id"
                ),
                {"id": _job_id(job_id)},
            ).first()
        if row is None:
            raise JobNotFound("review job was not found")
        return _row_mapping(row)

    @staticmethod
    def _locked_job(
        connection: Connection, job_id: str, *, skip_locked: bool = False
    ) -> dict[str, Any]:
        suffix = ""
        if connection.dialect.name == "postgresql":
            suffix = " FOR UPDATE SKIP LOCKED" if skip_locked else " FOR UPDATE"
        row = connection.execute(
            text("SELECT * FROM review_jobs WHERE id=:id" + suffix),
            {"id": _job_id(job_id)},
        ).first()
        if row is None:
            raise JobNotFound("review job was not found")
        return _row_mapping(row)

    @staticmethod
    def _active_counts(
        connection: Connection,
        organization_id: str,
        repository_id: str,
        now: datetime,
        *,
        exclude_job_id: str | None = None,
    ) -> tuple[int, int]:
        exclusion = " AND id<>:exclude" if exclude_job_id is not None else ""
        parameters: dict[str, Any] = {
            "org": organization_id,
            "repo": repository_id,
            "now": now,
        }
        if exclude_job_id is not None:
            parameters["exclude"] = exclude_job_id
        row = connection.execute(
            text(
                "SELECT "
                "SUM(CASE WHEN organization_id=:org THEN 1 ELSE 0 END) AS org_count, "
                "SUM(CASE WHEN organization_id=:org AND repository_id=:repo THEN 1 ELSE 0 END) "
                "AS repo_count FROM review_jobs WHERE state IN ('leased','running') "
                "AND lease_expires_at>:now" + exclusion
            ),
            parameters,
        ).one()
        return int(row._mapping["org_count"] or 0), int(row._mapping["repo_count"] or 0)

    @staticmethod
    def _settle_quota_counters(
        connection: Connection,
        quotas: tuple[Mapping[str, Any], Mapping[str, Any]],
        *,
        reserved: int,
        actual: int,
        now: datetime,
    ) -> None:
        for quota in quotas:
            connection.execute(
                text(
                    "UPDATE service_quotas SET monthly_model_calls_reserved="
                    "CASE WHEN monthly_model_calls_reserved>=:reserved THEN "
                    "monthly_model_calls_reserved-:reserved ELSE 0 END, "
                    "monthly_model_calls_used=monthly_model_calls_used+:actual, "
                    "updated_at=:updated WHERE id=:id"
                ),
                {
                    "reserved": reserved,
                    "actual": actual,
                    "updated": now,
                    "id": quota["id"],
                },
            )

    def _reserve_attempt(
        self,
        connection: Connection,
        quotas: tuple[Mapping[str, Any], Mapping[str, Any]],
        calls: int,
        now: datetime,
    ) -> bool:
        try:
            for quota in quotas:
                self._check_budget(quota, calls)
        except ModelBudgetExhausted:
            return False
        for quota in quotas:
            connection.execute(
                text(
                    "UPDATE service_quotas SET monthly_model_calls_reserved="
                    "monthly_model_calls_reserved+:calls, updated_at=:updated WHERE id=:id"
                ),
                {"calls": calls, "updated": now, "id": quota["id"]},
            )
            if isinstance(quota, dict):
                quota["monthly_model_calls_reserved"] = int(
                    quota["monthly_model_calls_reserved"]
                ) + calls
        return True

    @staticmethod
    def _insert_usage(
        connection: Connection,
        row: Mapping[str, Any],
        *,
        attempt_count: int,
        llm_calls: int,
        usage: Mapping[str, Any] | None,
        now: datetime,
    ) -> None:
        details = usage or {}
        connection.execute(
            text(
                "INSERT INTO provider_usage "
                "(id, organization_id, repository_id, review_job_id, provider, model, "
                "input_tokens, output_tokens, cost_microusd, pricing_version, created_at, "
                "attempt_count, llm_calls) VALUES "
                "(:id, :org, :repo, :job, :provider, :model, :input, :output, :cost, "
                ":pricing, :created, :attempt, :calls) ON CONFLICT DO NOTHING"
            ),
            {
                "id": new_id(),
                "org": row["organization_id"],
                "repo": row["repository_id"],
                "job": row["id"],
                "provider": str(details.get("provider") or "unknown")[:64],
                "model": str(details.get("model") or "unknown")[:128],
                "input": max(0, int(details.get("input_tokens") or 0)),
                "output": max(0, int(details.get("output_tokens") or 0)),
                "cost": details.get("cost_microusd"),
                "pricing": (
                    str(details["pricing_version"])[:128]
                    if details.get("pricing_version") is not None
                    else None
                ),
                "created": _iso(now),
                "attempt": attempt_count,
                "calls": llm_calls,
            },
        )

    def _settle_expired_attempt(
        self,
        connection: Connection,
        row: dict[str, Any],
        quotas: tuple[dict[str, Any], dict[str, Any]],
        now: datetime,
    ) -> None:
        reserved = int(row.get("model_calls_reserved") or 0)
        if int(row.get("attempt_count") or 0) <= 0 or reserved <= 0:
            return
        self._settle_quota_counters(
            connection, quotas, reserved=reserved, actual=reserved, now=now
        )
        self._insert_usage(
            connection,
            row,
            attempt_count=int(row["attempt_count"]),
            llm_calls=reserved,
            usage={"pricing_version": "lease-expired-reservation/v1"},
            now=now,
        )
        row["model_calls_reserved"] = 0
        for quota in quotas:
            quota["monthly_model_calls_reserved"] = max(
                0, int(quota["monthly_model_calls_reserved"]) - reserved
            )
            quota["monthly_model_calls_used"] = int(
                quota["monthly_model_calls_used"]
            ) + reserved

    def _terminalize_claim(
        self,
        connection: Connection,
        row: Mapping[str, Any],
        *,
        state: JobState,
        error_category: str,
        now: datetime,
    ) -> None:
        attempt = int(row.get("attempt_count") or 0)
        result = connection.execute(
            text(
                "UPDATE review_jobs SET state=:state, completed_at=:completed, "
                "error_code=:error, last_error_category=:error, lease_owner=NULL, "
                "lease_token=NULL, lease_expires_at=NULL, heartbeat_at=NULL, "
                "model_calls_reserved=0, final_trace_key=:trace, updated_at=:updated "
                "WHERE id=:id"
            ),
            {
                "state": state.value,
                "completed": _iso(now),
                "error": error_category,
                "trace": None,
                "updated": now,
                "id": row["id"],
            },
        )
        if result.rowcount != 1:
            raise RuntimeError("claim terminal state could not be persisted")
        self._insert_job_audit(
            connection,
            row,
            action=f"review.{state.value}",
            decision="error",
            reason_code=error_category,
            attempt_count=attempt,
            now=now,
        )

    def _claim_candidates(self, now: datetime | None) -> list[Any]:
        """Read a bounded set of fair candidates across more than one page.

        Quota and job eligibility are still rechecked under locks by ``claim``.
        Paging here prevents a permanently capacity-blocked first page from
        hiding the next repository scope forever while keeping each poll
        bounded.
        """

        candidates: list[Any] = []
        capacity = self.CLAIM_PAGE_SIZE * self.CLAIM_MAX_PAGES
        with self._claim_cursor_lock, self.database.engine.connect() as connection:
            current = _database_clock(connection, now)
            start_offset = self._claim_cursor
            for pass_number in range(2):
                candidates.clear()
                for page_number in range(self.CLAIM_MAX_PAGES):
                    page = connection.execute(
                        text(
                            "SELECT id, organization_id, repository_id FROM ("
                            "SELECT j.id, j.organization_id, j.repository_id, j.available_at, "
                            "j.queued_at, ROW_NUMBER() OVER (PARTITION BY j.organization_id, "
                            "j.repository_id ORDER BY j.available_at, j.queued_at, j.id) AS scope_rank "
                            "FROM review_jobs j JOIN repositories r ON "
                            "r.id=j.repository_id AND r.organization_id=j.organization_id "
                            "AND r.active=:active WHERE ((j.state='queued' AND "
                            "j.available_at<=:now) OR (j.state IN ('leased','running') "
                            "AND j.lease_expires_at<=:now))) "
                            "AS eligible ORDER BY scope_rank, available_at, queued_at, id "
                            "LIMIT :page_size OFFSET :page_offset"
                        ),
                        {
                            "now": current,
                            "active": True,
                            "page_size": self.CLAIM_PAGE_SIZE,
                            "page_offset": start_offset
                            + page_number * self.CLAIM_PAGE_SIZE,
                        },
                    ).all()
                    candidates.extend(page)
                    if len(page) < self.CLAIM_PAGE_SIZE:
                        break
                if candidates or start_offset == 0 or pass_number == 1:
                    break
                # Eligible rows may have disappeared before a saved offset.
                # Wrap once so a shrinking queue cannot produce a false empty poll.
                start_offset = 0
            self._claim_cursor = (
                start_offset + len(candidates) if len(candidates) == capacity else 0
            )
        return candidates

    def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 60,
        now: datetime | None = None,
        requested_job_id: str | None = None,
    ) -> JobLease | None:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", worker_id):
            raise InvalidRequest("worker ID is invalid")
        if not 1 <= lease_seconds <= 3600:
            raise InvalidRequest("lease duration is outside the supported range")
        if requested_job_id is None:
            candidates = self._claim_candidates(now)
        else:
            with self.database.engine.connect() as connection:
                requested = connection.execute(
                    text(
                        "SELECT j.id, j.organization_id, j.repository_id FROM review_jobs j "
                        "JOIN repositories r ON r.id=j.repository_id "
                        "AND r.organization_id=j.organization_id AND r.active=:active "
                        "WHERE j.id=:id"
                    ),
                    {"id": _job_id(requested_job_id), "active": True},
                ).first()
            candidates = [] if requested is None else [requested]
        for candidate in candidates:
            candidate_map = candidate._mapping
            organization_id = str(candidate_map["organization_id"])
            repository_id = str(candidate_map["repository_id"])
            job_id = str(candidate_map["id"])
            terminalized = False
            terminal_payload_key: str | None = None
            claimed_lease: JobLease | None = None
            with self._transaction(immediate=True) as connection:
                current = _database_clock(connection, now)
                org_quota, repo_quota = self._lock_quotas(
                    connection, organization_id, repository_id, current
                )
                current = _database_clock(connection, now)
                try:
                    row = self._locked_job(connection, job_id, skip_locked=True)
                except JobNotFound:
                    continue
                state = str(row["state"])
                expired = state in {
                    JobState.LEASED.value,
                    JobState.RUNNING.value,
                } and (_coerce_datetime(row.get("lease_expires_at")) or current) <= current
                queued = state == JobState.QUEUED.value and (
                    _coerce_datetime(row.get("available_at")) or current
                ) <= current
                if not queued and not expired:
                    continue
                repository_active = connection.execute(
                    text(
                        "SELECT 1 FROM repositories WHERE id=:repo AND "
                        "organization_id=:org AND active=:active"
                    ),
                    {
                        "repo": repository_id,
                        "org": organization_id,
                        "active": True,
                    },
                ).first()
                if repository_active is None:
                    continue
                quotas = (org_quota, repo_quota)
                org_active, repo_active = self._active_counts(
                    connection,
                    organization_id,
                    repository_id,
                    current,
                    exclude_job_id=job_id,
                )
                if org_active >= int(org_quota["max_concurrent_jobs"]):
                    continue
                if repo_active >= int(repo_quota["max_concurrent_jobs"]):
                    continue
                if expired:
                    self._insert_job_audit(
                        connection,
                        row,
                        action="review.lease_expired",
                        decision="error",
                        reason_code="lease_expired",
                        attempt_count=int(row.get("attempt_count") or 0),
                        now=current,
                    )
                    self._settle_expired_attempt(connection, row, quotas, current)
                    if int(row["attempt_count"]) >= int(row["max_attempts"]):
                        self._terminalize_claim(
                            connection,
                            row,
                            state=JobState.DEAD_LETTER,
                            error_category="lease_expired",
                            now=current,
                        )
                        terminalized = True
                    else:
                        calls = int(row["model_call_limit"])
                        if not self._reserve_attempt(
                            connection, quotas, calls, current
                        ):
                            self._terminalize_claim(
                                connection,
                                row,
                                state=JobState.FAILED,
                                error_category="budget_exhausted",
                                now=current,
                            )
                            terminalized = True
                        else:
                            row["model_calls_reserved"] = calls
                elif not terminalized and int(row.get("model_calls_reserved") or 0) <= 0:
                    calls = int(row["model_call_limit"])
                    if not self._reserve_attempt(connection, quotas, calls, current):
                        self._terminalize_claim(
                            connection,
                            row,
                            state=JobState.FAILED,
                            error_category="budget_exhausted",
                            now=current,
                        )
                        terminalized = True
                    else:
                        row["model_calls_reserved"] = calls
                if terminalized:
                    terminal_payload_key = (
                        str(row["payload_key"]) if row.get("payload_key") else None
                    )
                else:
                    token = uuid.uuid4().hex
                    expires = current + timedelta(seconds=lease_seconds)
                    result = connection.execute(
                        text(
                            "UPDATE review_jobs SET state=:leased, lease_owner=:owner, "
                            "lease_token=:token, lease_expires_at=:expires, "
                            "heartbeat_at=:heartbeat, attempt_count=attempt_count+1, "
                            "model_calls_reserved=:reserved, updated_at=:updated WHERE id=:id "
                            "AND ((state='queued' AND available_at<=:now) OR "
                            "(state IN ('leased','running') AND lease_expires_at<=:now))"
                        ),
                        {
                            "leased": JobState.LEASED.value,
                            "owner": worker_id,
                            "token": token,
                            "expires": expires,
                            "heartbeat": current,
                            "reserved": int(row["model_calls_reserved"]),
                            "updated": current,
                            "id": job_id,
                            "now": current,
                        },
                    )
                    if result.rowcount != 1:
                        raise RuntimeError("locked review could not be claimed")
                    claimed_lease = JobLease(
                            job_id=job_id,
                            organization_id=organization_id,
                            repository_id=repository_id,
                            repository_alias=str(row["repository_alias"]),
                            source_kind=str(row["source_kind"]),
                            source_ref=str(row["source_ref"]),
                            source_sha256=str(row["source_sha256"]),
                            head_sha=(
                                str(row["head_sha"]) if row.get("head_sha") else None
                            ),
                            submitted_by=str(row["submitted_by"]),
                            correlation_id=str(row["correlation_id"]),
                            lease_owner=worker_id,
                            lease_token=token,
                            lease_expires_at=expires,
                            attempt_count=int(row["attempt_count"]) + 1,
                            max_attempts=int(row["max_attempts"]),
                            model_call_limit=int(row["model_call_limit"]),
                            payload_key=(
                                str(row["payload_key"]) if row.get("payload_key") else None
                            ),
                        )
            if terminalized:
                self._delete_payload(terminal_payload_key)
                continue
            if claimed_lease is not None:
                return claimed_lease
        return None

    @staticmethod
    def _assert_lease(
        row: Mapping[str, Any], lease: JobLease, now: datetime
    ) -> None:
        expiry = _coerce_datetime(row.get("lease_expires_at"))
        if (
            str(row.get("lease_owner")) != lease.lease_owner
            or str(row.get("lease_token")) != lease.lease_token
            or int(row.get("attempt_count") or 0) != lease.attempt_count
            or expiry is None
            or expiry <= now
            or row.get("state") not in {JobState.LEASED.value, JobState.RUNNING.value}
        ):
            raise LeaseLost("job lease is no longer valid")

    def mark_running(
        self, lease: JobLease | str, *, now: datetime | None = None
    ) -> None:
        if isinstance(lease, str):
            if not self.database_url.startswith("sqlite"):
                raise LeaseLost("direct state transitions are local SQLite compatibility only")
            claimed = self.claim(
                f"local-compat-{uuid.uuid4().hex}",
                lease_seconds=60,
                now=now,
                requested_job_id=lease,
            )
            if claimed is None or claimed.job_id != lease:
                raise RuntimeError("queued review could not be claimed for local compatibility")
            self._compat_leases[lease] = claimed
            lease = claimed
        with self._transaction(immediate=True) as connection:
            self._locked_job(connection, lease.job_id)
            current = _database_clock(connection, now)
            result = connection.execute(
                text(
                    "UPDATE review_jobs SET state=:running, started_at=COALESCE(started_at, "
                    ":started), updated_at=:updated WHERE id=:id AND state=:leased "
                    "AND lease_owner=:owner AND lease_token=:token AND attempt_count=:attempt "
                    "AND lease_expires_at>:now"
                ),
                {
                    "running": JobState.RUNNING.value,
                    "started": _iso(current),
                    "updated": current,
                    "id": lease.job_id,
                    "leased": JobState.LEASED.value,
                    "owner": lease.lease_owner,
                    "token": lease.lease_token,
                    "attempt": lease.attempt_count,
                    "now": current,
                },
            )
            if result.rowcount != 1:
                raise LeaseLost("job lease was lost before running")

    def heartbeat(
        self,
        lease: JobLease,
        *,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> JobLease:
        scope = self._scope(lease.job_id)
        with self._transaction(immediate=True) as connection:
            current = _database_clock(connection, now)
            self._lock_quotas(
                connection,
                str(scope["organization_id"]),
                str(scope["repository_id"]),
                current,
            )
            row = self._locked_job(connection, lease.job_id)
            current = _database_clock(connection, now)
            self._assert_lease(row, lease, current)
            expires = current + timedelta(seconds=lease_seconds)
            result = connection.execute(
                text(
                    "UPDATE review_jobs SET heartbeat_at=:heartbeat, lease_expires_at=:expires, "
                    "updated_at=:updated WHERE id=:id AND state IN ('leased','running') "
                    "AND lease_owner=:owner AND lease_token=:token AND attempt_count=:attempt "
                    "AND lease_expires_at>:now"
                ),
                {
                    "heartbeat": current,
                    "expires": expires,
                    "updated": current,
                    "id": lease.job_id,
                    "owner": lease.lease_owner,
                    "token": lease.lease_token,
                    "attempt": lease.attempt_count,
                    "now": current,
                },
            )
            if result.rowcount != 1:
                raise LeaseLost("job lease heartbeat was rejected")
        return JobLease(**{**lease.__dict__, "lease_expires_at": expires})

    def succeed(
        self,
        job_id: str,
        review: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        lease = self._compat_leases.pop(_job_id(job_id), None)
        if lease is None:
            raise LeaseLost("local compatibility lease is unavailable")
        trace_path = self.trace_path_for_lease(lease)
        if not trace_path.is_file():
            trace_path.write_text('{"trace":"local-compat"}\n', encoding="utf-8")
        self.complete(
            lease,
            review,
            trace_key=trace_path.name,
            now=now,
            _allow_legacy_missing_findings=True,
        )

    def fail(
        self, job_id: str, error_code: str, *, now: datetime | None = None
    ) -> None:
        lease = self._compat_leases.pop(_job_id(job_id), None)
        if lease is None:
            raise LeaseLost("local compatibility lease is unavailable")
        trace_path = self.trace_path_for_lease(lease)
        if not trace_path.is_file():
            trace_path.write_text('{"trace":"local-compat"}\n', encoding="utf-8")
        self.finish_failure(
            lease,
            error_code,
            retryable=False,
            trace_key=trace_path.name,
            now=now,
        )

    def worker_heartbeat(
        self,
        worker_id: str,
        *,
        status: str,
        capacity: int,
        version: str = SCHEMA_VERSION,
        now: datetime | None = None,
    ) -> None:
        if status not in {"ready", "draining", "stopped"}:
            raise InvalidRequest("worker status is invalid")
        with self._transaction(immediate=True) as connection:
            current = _database_clock(connection, now)
            connection.execute(
                text(
                    "INSERT INTO worker_instances "
                    "(worker_id, status, capacity, version, started_at, heartbeat_at, updated_at) "
                    "VALUES (:id, :status, :capacity, :version, :started, :heartbeat, :updated) "
                    "ON CONFLICT(worker_id) DO UPDATE SET status=:status, capacity=:capacity, "
                    "version=:version, heartbeat_at=:heartbeat, updated_at=:updated"
                ),
                {
                    "id": worker_id,
                    "status": status,
                    "capacity": capacity,
                    "version": version,
                    "started": current,
                    "heartbeat": current,
                    "updated": current,
                },
            )

    def live_worker_count(
        self, *, stale_seconds: float, now: datetime | None = None
    ) -> int:
        with self.database.engine.connect() as connection:
            current = _database_clock(connection, now)
            cutoff = current - timedelta(seconds=stale_seconds)
            count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM worker_instances WHERE status='ready' "
                    "AND heartbeat_at>=:cutoff"
                ),
                {"cutoff": cutoff},
            ).scalar_one()
        return int(count)

    def worker_is_live(
        self,
        worker_id: str,
        *,
        stale_seconds: float,
        now: datetime | None = None,
    ) -> bool:
        with self.database.engine.connect() as connection:
            current = _database_clock(connection, now)
            cutoff = current - timedelta(seconds=stale_seconds)
            count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM worker_instances WHERE worker_id=:worker "
                    "AND status='ready' AND heartbeat_at>=:cutoff"
                ),
                {"worker": worker_id, "cutoff": cutoff},
            ).scalar_one()
        return int(count) == 1

    def database_ready(self) -> bool:
        try:
            with self.database.engine.connect() as connection:
                connection.execute(text("SELECT 1")).scalar_one()
            require_schema_head(self.database_url)
        except BaseException:
            return False
        return True

    def trace_path_for_lease(self, lease: JobLease) -> Path:
        key = (
            f"{lease.job_id}.{lease.attempt_count}.{lease.lease_token}.jsonl"
        )
        if _TRACE_KEY.fullmatch(key) is None:
            raise InvalidRequest("trace identity is invalid")
        return self.trace_dir / key

    def load_payload(self, lease: JobLease) -> str | None:
        if lease.payload_key is None:
            return None
        path = self._payload_path(lease.payload_key)
        try:
            encoded = path.read_bytes()
        except OSError as exc:
            raise InvalidRequest("job payload is unavailable") from exc
        if hashlib.sha256(encoded).hexdigest() != lease.source_sha256:
            raise InvalidRequest("job payload fingerprint does not match")
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidRequest("job payload is not UTF-8") from exc

    def _row(self, job_id: str, principal: Principal | None = None) -> Mapping[str, Any]:
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
        item = _row_mapping(row)
        if principal is not None:
            repository = self.database.authorized_repository(
                principal, str(item["repository_id"])
            )
            if repository is None:
                raise JobNotFound("review job was not found")
        return item

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
                "head_sha": row.get("head_sha"),
            },
            "state": row["state"],
            "created_at": _public_datetime(row["created_at"]),
            "queued_at": _public_datetime(row.get("queued_at")),
            "started_at": _public_datetime(row["started_at"]),
            "completed_at": _public_datetime(row["completed_at"]),
            "attempt_count": row.get("attempt_count", 0),
        }
        if row["review_json"] is not None:
            result["review"] = json.loads(row["review_json"])
        if row["error_code"] is not None:
            result["error"] = {"code": row["error_code"]}
        return result

    @staticmethod
    def _normalized_usage(
        row: Mapping[str, Any], usage: Mapping[str, Any] | None
    ) -> tuple[int, Mapping[str, Any]]:
        details = usage or {}
        observed = details.get("llm_calls")
        if isinstance(observed, int) and not isinstance(observed, bool) and observed >= 0:
            return min(observed, int(row["model_call_limit"])), details
        return int(row.get("model_calls_reserved") or row["model_call_limit"]), {
            **details,
            "pricing_version": details.get("pricing_version")
            or "conservative-reservation/v1",
        }

    @staticmethod
    def _encode_review_result(
        review: Mapping[str, Any], *, allow_missing_findings: bool = False
    ) -> tuple[str, list[dict[str, Any]]] | None:
        if not isinstance(review, Mapping) or any(
            not isinstance(key, str) for key in review
        ):
            return None
        if "findings" not in review and not allow_missing_findings:
            return None
        summary = review.get("summary")
        findings = review.get("findings", [])
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or not isinstance(findings, list)
            or len(findings) > MAX_RESULT_FINDINGS
        ):
            return None
        encoded_findings: list[dict[str, Any]] = []
        normalized_findings: list[dict[str, Any]] = []
        for finding in findings:
            if not isinstance(finding, Mapping) or any(
                not isinstance(key, str) for key in finding
            ):
                return None
            for key in ("file", "issue", "suggestion", "message"):
                value = finding.get(key)
                if value is not None and not isinstance(value, str):
                    return None
            locator = finding.get("path") or finding.get("file")
            message = finding.get("message") or finding.get("issue")
            severity = finding.get("severity")
            if (
                not isinstance(locator, str)
                or not locator.strip()
                or len(locator) > 512
                or "\x00" in locator
                or not isinstance(message, str)
                or not message.strip()
                or not isinstance(severity, str)
                or severity not in {"high", "medium", "low"}
            ):
                return None
            line = finding.get("line")
            if (
                isinstance(line, bool)
                or not isinstance(line, int)
                or not 1 <= line <= 2147483647
            ):
                return None
            for key, limit in (("path", 512), ("fingerprint", 128), ("category", 128)):
                value = finding.get(key)
                if value is not None and (
                    not isinstance(value, str)
                    or len(value) > limit
                    or "\x00" in value
                ):
                    return None
            encoded_finding = dict(finding)
            normalized_finding = dict(encoded_finding)
            normalized_finding["path"] = locator
            normalized_finding["severity"] = severity
            encoded_findings.append(encoded_finding)
            normalized_findings.append(normalized_finding)
        normalized = dict(review)
        if "findings" in review:
            normalized["findings"] = encoded_findings
        try:
            encoded = json.dumps(
                normalized,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError):
            return None
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            return None
        return encoded, normalized_findings

    @staticmethod
    def _finding_lineage(
        finding: Mapping[str, Any],
        organization_id: str,
        repository_id: str,
        source_revision: str,
    ) -> tuple[str, str, str]:
        """Return stable ID plus content/evidence hashes without model-owned IDs."""
        content = {
            key: finding.get(key)
            for key in ("path", "file", "line", "severity", "category", "message", "issue", "suggestion")
            if key in finding
        }
        evidence = {
            key: value
            for key, value in finding.items()
            if key not in content
        }
        content_hash = hashlib.sha256(
            json.dumps(content, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        evidence_hash = hashlib.sha256(
            json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        stable_id = hashlib.sha256(
            f"{organization_id}\0{repository_id}\0{source_revision}\0{content_hash}\0{evidence_hash}".encode(
                "utf-8"
            )
        ).hexdigest()
        return stable_id, content_hash, evidence_hash

    @staticmethod
    def _insert_job_audit(
        connection: Connection,
        row: Mapping[str, Any],
        *,
        action: str,
        decision: str,
        reason_code: str | None,
        attempt_count: int,
        now: datetime,
    ) -> None:
        job_id = str(row["id"])
        connection.execute(
            text(
                "INSERT INTO audit_events "
                "(id, organization_id, principal_id, repository_id, credential_id, "
                "auth_method, action, resource_type, resource_id, decision, reason_code, "
                "policy_version, occurred_at_utc, correlation_id) VALUES "
                "(:id, :org, :principal, :repo, NULL, 'durable_worker', :action, "
                "'review_job', :job, :decision, :reason, :policy, :occurred, :correlation)"
            ),
            {
                "id": new_id(),
                "org": row["organization_id"],
                "principal": row["submitted_by"],
                "repo": row["repository_id"],
                "action": action,
                "job": job_id,
                "decision": decision,
                "reason": reason_code[:64] if reason_code is not None else None,
                "policy": row.get("policy_version") or "rbac/v1",
                "occurred": _iso(now),
                "correlation": f"{job_id}:{attempt_count}"[:128],
            },
        )

    def _trace_is_complete(self, trace_key: str) -> bool:
        path = self.trace_dir / trace_key
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_TRACE_BYTES:
                return False
            trace_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        lines = trace_text.splitlines()
        if not lines:
            return False
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                return False
            if not isinstance(record, dict):
                return False
        return True

    def complete(
        self,
        lease: JobLease,
        review: Mapping[str, Any],
        *,
        trace_key: str,
        usage: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        _allow_legacy_missing_findings: bool = False,
    ) -> None:
        if (
            _TRACE_KEY.fullmatch(trace_key) is None
            or trace_key != self.trace_path_for_lease(lease).name
        ):
            raise InvalidRequest("final trace key is invalid")
        if not self._trace_is_complete(trace_key):
            raise InvalidRequest("final trace is unavailable or incomplete")
        normalized_result = self._encode_review_result(
            review, allow_missing_findings=_allow_legacy_missing_findings
        )
        if normalized_result is None:
            self.finish_failure(
                lease,
                "schema_policy",
                retryable=False,
                trace_key=trace_key,
                usage=usage,
                now=now,
            )
            return
        encoded, normalized_findings = normalized_result
        scope = self._scope(lease.job_id)
        with self._transaction(immediate=True) as connection:
            current = _database_clock(connection, now)
            quotas = self._lock_quotas(
                connection,
                str(scope["organization_id"]),
                str(scope["repository_id"]),
                current,
            )
            row = self._locked_job(connection, lease.job_id)
            current = _database_clock(connection, now)
            self._assert_lease(row, lease, current)
            calls, normalized_usage = self._normalized_usage(row, usage)
            reserved = int(row.get("model_calls_reserved") or 0)
            self._settle_quota_counters(
                connection, quotas, reserved=reserved, actual=calls, now=current
            )
            self._insert_usage(
                connection,
                row,
                attempt_count=lease.attempt_count,
                llm_calls=calls,
                usage=normalized_usage,
                now=current,
            )
            source_revision = str(row.get("head_sha") or row["source_sha256"])
            for finding in normalized_findings:
                payload = json.dumps(
                    finding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                finding_id, content_hash, evidence_hash = self._finding_lineage(
                    finding,
                    str(row["organization_id"]),
                    str(row["repository_id"]),
                    source_revision,
                )
                fingerprint = str(finding.get("fingerprint") or content_hash)
                connection.execute(
                    text(
                        "INSERT INTO findings "
                        "(id, organization_id, repository_id, review_job_id, fingerprint, "
                        "content_sha256, evidence_sha256, source_revision, path, line, severity, "
                        "category, status, payload_json, created_at) VALUES "
                        "(:id, :org, :repo, :job, :fingerprint, :content, :evidence, :revision, "
                        ":path, :line, :severity, :category, 'pending_approval', :payload, "
                        ":created) ON CONFLICT DO NOTHING"
                    ),
                    {
                        "id": finding_id,
                        "org": row["organization_id"],
                        "repo": row["repository_id"],
                        "job": lease.job_id,
                        "fingerprint": fingerprint[:128],
                        "content": content_hash,
                        "evidence": evidence_hash,
                        "revision": source_revision,
                        "path": finding.get("path") or finding.get("file"),
                        "line": finding.get("line"),
                        "severity": finding.get("severity"),
                        "category": finding.get("category"),
                        "payload": payload,
                        "created": _iso(current),
                    },
                )
            final_current = _database_clock(connection, now)
            result = connection.execute(
                text(
                    "UPDATE review_jobs SET state=:target, completed_at=:completed, "
                    "review_json=:review, error_code=NULL, last_error_category=NULL, "
                    "lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, "
                    "heartbeat_at=NULL, model_calls_reserved=0, final_trace_key=:trace, "
                    "updated_at=:updated WHERE id=:id AND lease_owner=:owner "
                    "AND lease_token=:token AND attempt_count=:attempt "
                    "AND state IN ('leased','running') AND lease_expires_at>:now"
                ),
                {
                    "target": JobState.AWAITING_APPROVAL.value,
                    "completed": _iso(final_current),
                    "review": encoded,
                    "trace": trace_key,
                    "updated": final_current,
                    "id": lease.job_id,
                    "owner": lease.lease_owner,
                    "token": lease.lease_token,
                    "attempt": lease.attempt_count,
                    "now": final_current,
                },
            )
            if result.rowcount != 1:
                raise LeaseLost("job completion lost its fencing race")
            self._insert_job_audit(
                connection,
                row,
                action="review.awaiting_approval",
                decision="allow",
                reason_code=None,
                attempt_count=lease.attempt_count,
                now=final_current,
            )
            connection.execute(
                text(
                    "INSERT INTO metric_events "
                    "(id, organization_id, repository_id, review_job_id, finding_id, "
                    "approval_id, principal_id, event_type, subject_sha256, occurred_at) "
                    "VALUES (:id, :org, :repo, :job, NULL, NULL, :principal, :event, "
                    ":subject, :occurred)"
                ),
                {
                    "id": new_id(),
                    "org": row["organization_id"],
                    "repo": row["repository_id"],
                    "job": lease.job_id,
                    "principal": row["submitted_by"],
                    "event": "review.awaiting_approval",
                    "subject": hashlib.sha256(lease.job_id.encode("utf-8")).hexdigest(),
                    "occurred": _iso(final_current),
                },
            )
        self._delete_payload(lease.payload_key)

    def finish_failure(
        self,
        lease: JobLease,
        error_category: str,
        *,
        retryable: bool,
        trace_key: str | None,
        usage: Mapping[str, Any] | None = None,
        available_at: datetime | None = None,
        delay_seconds: float | None = None,
        now: datetime | None = None,
    ) -> FailureOutcome:
        if delay_seconds is not None:
            if (
                isinstance(delay_seconds, bool)
                or not isinstance(delay_seconds, (int, float))
                or not math.isfinite(delay_seconds)
                or not 0 <= delay_seconds <= MAX_RETRY_DELAY_SECONDS
            ):
                raise InvalidRequest("retry delay is outside the supported range")
        normalized_available: datetime | None = None
        if available_at is not None:
            if (
                not isinstance(available_at, datetime)
                or available_at.tzinfo is None
                or available_at.utcoffset() is None
            ):
                raise InvalidRequest("absolute retry availability must include a timezone")
            normalized_available = available_at.astimezone(timezone.utc)
        if trace_key is not None and (
            _TRACE_KEY.fullmatch(trace_key) is None
            or trace_key != self.trace_path_for_lease(lease).name
        ):
            raise InvalidRequest("failure trace key is invalid")
        if trace_key is not None and not self._trace_is_complete(trace_key):
            trace_key = None
        scope = self._scope(lease.job_id)
        terminal = False
        with self._transaction(immediate=True) as connection:
            current = _database_clock(connection, now)
            quotas = self._lock_quotas(
                connection,
                str(scope["organization_id"]),
                str(scope["repository_id"]),
                current,
            )
            row = self._locked_job(connection, lease.job_id)
            current = _database_clock(connection, now)
            self._assert_lease(row, lease, current)
            calls, normalized_usage = self._normalized_usage(row, usage)
            reserved = int(row.get("model_calls_reserved") or 0)
            self._settle_quota_counters(
                connection, quotas, reserved=reserved, actual=calls, now=current
            )
            for quota in quotas:
                if isinstance(quota, dict):
                    quota["monthly_model_calls_reserved"] = max(
                        0, int(quota["monthly_model_calls_reserved"]) - reserved
                    )
                    quota["monthly_model_calls_used"] = int(
                        quota["monthly_model_calls_used"]
                    ) + calls
            self._insert_usage(
                connection,
                row,
                attempt_count=lease.attempt_count,
                llm_calls=calls,
                usage=normalized_usage,
                now=current,
            )
            can_retry = retryable and lease.attempt_count < lease.max_attempts
            next_available = current + timedelta(seconds=delay_seconds or 0.0)
            if normalized_available is not None:
                next_available = max(next_available, normalized_available)
            next_available = min(
                next_available,
                current + timedelta(seconds=MAX_RETRY_DELAY_SECONDS),
            )
            next_reserved = 0
            if can_retry:
                next_reserved = int(row["model_call_limit"])
                if not self._reserve_attempt(
                    connection, quotas, next_reserved, current
                ):
                    can_retry = False
                    error_category = "budget_exhausted"
            if can_retry:
                target = JobState.QUEUED.value
                completed = None
                final_trace = None
            else:
                target = (
                    JobState.DEAD_LETTER.value
                    if retryable and lease.attempt_count >= lease.max_attempts
                    else JobState.FAILED.value
                )
                completed = _iso(current)
                final_trace = trace_key
                terminal = True
            final_current = _database_clock(connection, now)
            result = connection.execute(
                text(
                    "UPDATE review_jobs SET state=:target, available_at=:available, "
                    "completed_at=:completed, review_json=NULL, error_code=:error, "
                    "last_error_category=:error, lease_owner=NULL, lease_token=NULL, "
                    "lease_expires_at=NULL, heartbeat_at=NULL, "
                    "model_calls_reserved=:reserved, final_trace_key=:trace, "
                    "updated_at=:updated WHERE id=:id AND lease_owner=:owner "
                    "AND lease_token=:token AND attempt_count=:attempt "
                    "AND state IN ('leased','running') AND lease_expires_at>:now"
                ),
                {
                    "target": target,
                    "available": next_available,
                    "completed": completed,
                    "error": error_category[:64],
                    "reserved": next_reserved if can_retry else 0,
                    "trace": final_trace,
                    "updated": final_current,
                    "id": lease.job_id,
                    "owner": lease.lease_owner,
                    "token": lease.lease_token,
                    "attempt": lease.attempt_count,
                    "now": final_current,
                },
            )
            if result.rowcount != 1:
                raise LeaseLost("job failure lost its fencing race")
            self._insert_job_audit(
                connection,
                row,
                action=(
                    "review.retry_scheduled"
                    if can_retry
                    else f"review.{target}"
                ),
                decision="error",
                reason_code=error_category,
                attempt_count=lease.attempt_count,
                now=final_current,
            )
        if terminal:
            self._delete_payload(lease.payload_key)
        return FailureOutcome(
            state=target,
            retry_scheduled=can_retry,
            available_at=next_available if can_retry else None,
        )

    def _delete_payload(self, payload_key: str | None) -> None:
        if payload_key is None:
            return
        try:
            self._payload_path(payload_key).unlink()
        except FileNotFoundError:
            return
        except OSError:
            # Cleanup failure must not roll back a committed review.
            warnings.warn(
                "durable artifact cleanup failed",
                RuntimeWarning,
                stacklevel=2,
            )
            return

    @staticmethod
    def _unlink_orphan(path: Path) -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            warnings.warn(
                "durable artifact cleanup failed",
                RuntimeWarning,
                stacklevel=2,
            )
            return False
        return True

    def _artifact_batch(self, directory: Path, kind: str, limit: int) -> list[Path]:
        with self._cleanup_cursor_lock:
            offset = self._cleanup_offsets[kind]
            entries = list(islice(directory.iterdir(), offset, offset + limit))
            if not entries and offset:
                offset = 0
                entries = list(islice(directory.iterdir(), limit))
            self._cleanup_offsets[kind] = (
                offset + len(entries) if len(entries) == limit else 0
            )
        return entries

    def cleanup_orphans(
        self,
        *,
        limit: int = 64,
        temporary_min_age_seconds: float = DEFAULT_TEMP_ARTIFACT_MIN_AGE_SECONDS,
    ) -> int:
        """Delete a bounded set of artifacts not retained by database lineage."""

        if isinstance(limit, bool) or not 2 <= limit <= 1024:
            raise InvalidRequest("artifact cleanup limit is invalid")
        if (
            isinstance(temporary_min_age_seconds, bool)
            or not isinstance(temporary_min_age_seconds, (int, float))
            or not math.isfinite(temporary_min_age_seconds)
            or not 1 <= temporary_min_age_seconds <= 86400
        ):
            raise InvalidRequest("temporary artifact cleanup age is invalid")
        removed = 0
        payload_limit = (limit + 1) // 2
        trace_limit = limit - payload_limit
        try:
            with self.database.engine.connect() as connection:
                for path in self._artifact_batch(
                    self.job_data_dir, "payload", payload_limit
                ):
                    if _PAYLOAD_TEMP_KEY.fullmatch(path.name) is not None:
                        try:
                            age_seconds = time.time() - path.stat().st_mtime
                        except OSError:
                            continue
                        if (
                            age_seconds >= temporary_min_age_seconds
                            and self._unlink_orphan(path)
                        ):
                            removed += 1
                        continue
                    if _PAYLOAD_KEY.fullmatch(path.name) is None:
                        continue
                    job_id = path.name.removesuffix(".diff")
                    row = connection.execute(
                        text(
                            "SELECT state, payload_key FROM review_jobs WHERE id=:id"
                        ),
                        {"id": job_id},
                    ).mappings().first()
                    retained = (
                        row is not None
                        and row["payload_key"] == path.name
                        and row["state"] not in TERMINAL_JOB_STATES
                    )
                    if not retained and self._unlink_orphan(path):
                        removed += 1

                for path in self._artifact_batch(
                    self.trace_dir, "trace", trace_limit
                ):
                    if _TRACE_KEY.fullmatch(path.name) is None:
                        continue
                    job_id, attempt, token, _ = path.name.split(".")
                    row = connection.execute(
                        text(
                            "SELECT state, lease_token, attempt_count, final_trace_key "
                            "FROM review_jobs WHERE id=:id"
                        ),
                        {"id": job_id},
                    ).mappings().first()
                    retained = row is not None and (
                        row["final_trace_key"] == path.name
                        or (
                            row["state"] in {
                                JobState.LEASED.value,
                                JobState.RUNNING.value,
                            }
                            and row["lease_token"] == token
                            and int(row["attempt_count"]) == int(attempt)
                        )
                    )
                    if not retained and self._unlink_orphan(path):
                        removed += 1
        except OSError:
            warnings.warn(
                "durable artifact cleanup failed",
                RuntimeWarning,
                stacklevel=2,
            )
        return removed

    def trace_path(self, job_id: str, principal: Principal | None = None) -> Path:
        compatibility = self._compat_leases.get(job_id)
        if compatibility is not None:
            return self.trace_path_for_lease(compatibility)
        row = self._row(job_id, principal)
        key = row.get("final_trace_key")
        if not isinstance(key, str) or _TRACE_KEY.fullmatch(key) is None:
            raise InvalidRequest("trace is unavailable")
        return self.trace_dir / key

    def read_trace(self, job_id: str, principal: Principal | None = None) -> str:
        job = self.get(job_id, principal)
        if job["state"] not in TERMINAL_JOB_STATES:
            raise InvalidRequest("trace is not available until the review is terminal")
        path = self.trace_path(job_id, principal)
        if not self._trace_is_complete(path.name):
            raise InvalidRequest("trace is unavailable or malformed")
        trace_text = path.read_text(encoding="utf-8")
        return trace_text

    def reconcile_received(
        self,
        *,
        timeout_seconds: float,
        batch_size: int = RECEIVED_RECONCILE_BATCH_SIZE,
        now: datetime | None = None,
    ) -> int:
        if isinstance(batch_size, bool) or not 1 <= batch_size <= 256:
            raise InvalidRequest("received reconciliation batch size is invalid")
        with self.database.engine.connect() as connection:
            current = _database_clock(connection, now)
            cutoff = current - timedelta(seconds=timeout_seconds)
            rows = connection.execute(
                text(
                    "SELECT id, created_at, payload_key, source_sha256, source_bytes "
                    "FROM review_jobs WHERE state='received' AND created_at<=:cutoff "
                    "ORDER BY created_at, id LIMIT :batch_size"
                ),
                {"cutoff": _iso(cutoff), "batch_size": batch_size},
            ).all()
        reconciled = 0
        for row in rows:
            created = _coerce_datetime(row._mapping["created_at"])
            if created is None or created > cutoff:
                continue
            job_id = str(row._mapping["id"])
            payload_key = row._mapping["payload_key"]
            payload_valid = payload_key is None
            if payload_key is not None:
                try:
                    path = self._payload_path(str(payload_key))
                    expected_size = int(row._mapping["source_bytes"])
                    if not 0 <= expected_size <= MAX_PAYLOAD_BYTES:
                        raise InvalidRequest("job payload size is invalid")
                    if path.stat().st_size == expected_size:
                        encoded = path.read_bytes()
                        payload_valid = (
                            len(encoded) == expected_size
                            and hashlib.sha256(encoded).hexdigest()
                            == str(row._mapping["source_sha256"])
                        )
                except (OSError, InvalidRequest, TypeError, ValueError):
                    payload_valid = False
            if payload_valid:
                try:
                    self.finalize_received(
                        job_id,
                        payload_key=str(payload_key) if payload_key is not None else None,
                        now=current,
                    )
                except ServiceError:
                    failed = self.fail_received(
                        job_id, "payload_unavailable", now=current
                    )
                    if failed:
                        self._delete_payload(
                            str(payload_key) if payload_key is not None else None
                        )
            else:
                failed = self.fail_received(job_id, "payload_unavailable", now=current)
                if failed:
                    self._delete_payload(
                        str(payload_key) if payload_key is not None else None
                    )
            reconciled += 1
        return reconciled
