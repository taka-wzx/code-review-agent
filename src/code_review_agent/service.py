"""Authenticated FastAPI and GitHub Webhook service for asynchronous reviews."""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
from typing import Any, AsyncIterator, Callable
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field
import anyio
import uvicorn
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from code_review_agent.database import DatabaseError
from code_review_agent.github_webhook import GitHubWebhookProcessor
from code_review_agent.mcp_server import create_mcp
from code_review_agent.production_metrics import CONTENT_TYPE
from code_review_agent.repair_service import (
    RepairAuthorizationError,
    RepairConflict,
    RepairServiceError,
    SyntheticStagingRepairService,
    create_synthetic_staging_repair_service,
)
from code_review_agent.identity import (
    AuthBackend,
    AuthenticationRequired,
    DatabaseAuthBackend,
    LocalTokenAuthBackend,
    OIDCConfiguration,
    OIDCJWTAuthBackend,
    Principal,
    Role,
    bind_correlation_id,
    bind_principal,
    current_principal,
    reset_correlation_id,
    reset_principal,
)
from code_review_agent.service_core import (
    AuthorizationDenied,
    IdempotencyConflict,
    InvalidRequest,
    JobNotFound,
    MAX_DIFF_BYTES,
    PublisherFailed,
    ReviewService,
    SCHEMA_VERSION,
    ServiceClosed,
    ServiceError,
    QuotaExceeded,
    create_review_service_from_env,
)


MAX_WEBHOOK_BYTES = 1024 * 1024


class DiffSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    repository: str = Field(min_length=3, max_length=128)
    diff: str = Field(min_length=1, max_length=MAX_DIFF_BYTES)


class PullRequestSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    repository: str = Field(min_length=3, max_length=128)
    pull_request: str = Field(min_length=1, max_length=256)
    head_sha: str | None = Field(default=None, pattern="^[0-9a-fA-F]{40,64}$")


class ServiceQuotaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_queued_jobs: int | None = Field(default=None, ge=1, le=100000)
    max_concurrent_jobs: int | None = Field(default=None, ge=1, le=64)
    submission_rate_limit: int | None = Field(default=None, ge=1, le=100000)
    submission_window_seconds: int | None = Field(default=None, ge=1, le=86400)
    monthly_model_call_budget: int | None = Field(
        default=None, ge=1, le=1000000000
    )
    model_call_limit_per_job: int | None = Field(default=None, ge=1, le=256)


class OrganizationPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: str = Field(min_length=1, max_length=128)
    severity_levels: list[str] = Field(min_length=1, max_length=16)
    forbidden_operations: list[str] = Field(default_factory=list, max_length=64)
    allowed_tools: list[str] = Field(default_factory=list, max_length=64)
    approval_threshold: int = Field(ge=1, le=100)
    retention_days: int = Field(ge=1, le=3650)
    cost_budget_microusd: int = Field(ge=0, le=10_000_000_000)
    source_sha: str = Field(pattern="^[0-9a-fA-F]{7,64}$")
    reason: str = Field(min_length=1, max_length=512)


class MembershipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    subject: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=256)
    role: Role
    repository_ids: list[str] = Field(default_factory=list, max_length=256)


class MembershipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    role: Role
    repository_ids: list[str] = Field(default_factory=list, max_length=256)


class RepositoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    repository: str = Field(min_length=3, max_length=128)
    mode: str = Field(default="shadow", pattern="^(shadow|guarded_publish)$")
    budget_microusd: int | None = Field(default=None, ge=0)
    policy_version: str = Field(default="rbac/v1", min_length=1, max_length=128)


class RepositoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    mode: str = Field(default="shadow", pattern="^(shadow|guarded_publish)$")
    budget_microusd: int | None = Field(default=None, ge=0)
    policy_version: str = Field(min_length=1, max_length=128)


class FeedbackRuleItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    rule_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
    category: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    action: str = Field(pattern=r"^(prioritize|suppress|require_verification)$")
    condition: str = Field(min_length=1, max_length=256)
    rationale: str = Field(min_length=1, max_length=512)


class FeedbackRuleVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
    rules: list[FeedbackRuleItem] = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=512)


class FeedbackRuleTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=512)


class CredentialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expires_in_seconds: int = Field(default=3600, ge=60, le=86400)


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: str = Field(pattern="^(accepted|rejected|uncertain|fixed|duplicate)$")
    finding_hash: str = Field(pattern="^[0-9a-f]{64}$")
    rationale: str | None = Field(default=None, max_length=512)
    reason: str | None = Field(default=None, max_length=512)


class PublicationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    payload_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    nonce: str = Field(pattern="^[0-9a-f]{64}$")


class FindingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: str = Field(pattern="^(approved|rejected)$")


class SyntheticRepairCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    repository: str = Field(min_length=1, max_length=128)
    finding_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    base_sha: str = Field(pattern="^[0-9a-f]{40,64}$")
    head_sha: str = Field(pattern="^[0-9a-f]{40,64}$")


class SyntheticRepairDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    approval_id: str = Field(pattern="^approval-[0-9a-f]{32}$")
    checkpoint_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class PayloadTooLarge(ServiceError):
    code = "payload_too_large"


class HttpSettings:
    def __init__(
        self,
        *,
        service_token: str,
        webhook_secret: str,
        allowed_origins: frozenset[str] = frozenset(
            {"http://127.0.0.1", "http://localhost"}
        ),
        allowed_hosts: frozenset[str] = frozenset(
            {"127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"}
        ),
        local_token_enabled: bool = True,
        local_token_behind_loopback_publish: bool = False,
        worker_stale_seconds: float = 30.0,
        auth_mode: str = "database",
        oidc_configuration: OIDCConfiguration | None = None,
    ) -> None:
        if service_token and len(service_token.encode("utf-8")) < 32:
            raise InvalidRequest("CRAG_SERVICE_TOKEN must be at least 32 UTF-8 bytes")
        if local_token_enabled and not service_token:
            raise InvalidRequest("local token mode requires CRAG_SERVICE_TOKEN")
        if len(webhook_secret.encode("utf-8")) < 16:
            raise InvalidRequest("CRAG_WEBHOOK_SECRET must be at least 16 UTF-8 bytes")
        if not allowed_origins or any(not item.startswith(("http://", "https://")) for item in allowed_origins):
            raise InvalidRequest("CRAG_ALLOWED_ORIGINS must contain exact HTTP origins")
        if not allowed_hosts or any("/" in item or " " in item for item in allowed_hosts):
            raise InvalidRequest("CRAG_ALLOWED_HOSTS must contain exact host values")
        if local_token_behind_loopback_publish:
            if not local_token_enabled:
                raise InvalidRequest(
                    "CRAG_LOCAL_TOKEN_BEHIND_LOOPBACK_PUBLISH requires local token mode"
                )
            loopback_origins = frozenset({"http://127.0.0.1", "http://localhost"})
            loopback_hosts = frozenset(
                {"127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"}
            )
            if not allowed_origins.issubset(loopback_origins):
                raise InvalidRequest(
                    "trusted loopback publication requires loopback-only origins"
                )
            if not allowed_hosts.issubset(loopback_hosts):
                raise InvalidRequest(
                    "trusted loopback publication requires loopback-only hosts"
                )
        if not 1 <= worker_stale_seconds <= 3600:
            raise InvalidRequest("CRAG_WORKER_STALE_SECONDS must be between 1 and 3600")
        if auth_mode not in {"database", "oidc"}:
            raise InvalidRequest("CRAG_AUTH_MODE must be database or oidc")
        if auth_mode == "oidc":
            if local_token_enabled:
                raise InvalidRequest("OIDC mode cannot enable the local token backend")
            if oidc_configuration is None:
                raise InvalidRequest("OIDC mode requires complete OIDC configuration")
        elif oidc_configuration is not None:
            raise InvalidRequest("OIDC configuration requires CRAG_AUTH_MODE=oidc")
        self.service_token = service_token
        self.webhook_secret = webhook_secret.encode("utf-8")
        self.allowed_origins = allowed_origins
        self.allowed_hosts = allowed_hosts
        self.local_token_enabled = local_token_enabled
        self.local_token_behind_loopback_publish = local_token_behind_loopback_publish
        self.worker_stale_seconds = worker_stale_seconds
        self.auth_mode = auth_mode
        self.oidc_configuration = oidc_configuration

    @classmethod
    def from_env(cls) -> "HttpSettings":
        raw_origins = os.environ.get(
            "CRAG_ALLOWED_ORIGINS", "http://127.0.0.1,http://localhost"
        )
        origins = frozenset(value.strip().rstrip("/") for value in raw_origins.split(",") if value.strip())
        raw_hosts = os.environ.get(
            "CRAG_ALLOWED_HOSTS", "127.0.0.1,127.0.0.1:*,localhost,localhost:*"
        )
        hosts = frozenset(value.strip() for value in raw_hosts.split(",") if value.strip())
        local_token_enabled = os.environ.get("CRAG_ALLOW_LOCAL_TOKEN", "").casefold() in {
            "1",
            "true",
            "yes",
        }
        raw_loopback_publish = os.environ.get(
            "CRAG_LOCAL_TOKEN_BEHIND_LOOPBACK_PUBLISH", ""
        ).casefold()
        if raw_loopback_publish not in {"", "0", "false", "no", "1", "true", "yes"}:
            raise InvalidRequest(
                "CRAG_LOCAL_TOKEN_BEHIND_LOOPBACK_PUBLISH must be boolean"
            )
        loopback_publish = raw_loopback_publish in {"1", "true", "yes"}
        auth_mode = os.environ.get("CRAG_AUTH_MODE", "database").casefold()
        oidc_variables = (
            "CRAG_OIDC_ISSUER",
            "CRAG_OIDC_AUDIENCE",
            "CRAG_OIDC_JWKS_URL",
            "CRAG_OIDC_ORGANIZATION_CLAIM",
            "CRAG_OIDC_JWKS_CACHE_SECONDS",
            "CRAG_OIDC_JWKS_TIMEOUT_SECONDS",
            "CRAG_OIDC_LEEWAY_SECONDS",
            "CRAG_OIDC_ALGORITHMS",
        )
        if auth_mode != "oidc" and any(name in os.environ for name in oidc_variables):
            raise InvalidRequest("OIDC settings require CRAG_AUTH_MODE=oidc")
        try:
            oidc_configuration = (
                OIDCConfiguration.from_environment(os.environ) if auth_mode == "oidc" else None
            )
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        try:
            worker_stale_seconds = float(
                os.environ.get("CRAG_WORKER_STALE_SECONDS", "30")
            )
        except ValueError as exc:
            raise InvalidRequest("CRAG_WORKER_STALE_SECONDS must be numeric") from exc
        return cls(
            service_token=(
                _secret_setting("CRAG_SERVICE_TOKEN") if local_token_enabled else ""
            ),
            webhook_secret=_secret_setting("CRAG_WEBHOOK_SECRET"),
            allowed_origins=origins,
            allowed_hosts=hosts,
            local_token_enabled=local_token_enabled,
            local_token_behind_loopback_publish=loopback_publish,
            worker_stale_seconds=worker_stale_seconds,
            auth_mode=auth_mode,
            oidc_configuration=oidc_configuration,
        )


def _secret_setting(name: str) -> str:
    configured = os.environ.get(name)
    if configured:
        return configured
    path_value = os.environ.get(f"{name}_FILE")
    if not path_value:
        return ""
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
    return value


def _webhook_signature(body: bytes, header: str, secret: bytes) -> None:
    if not header.startswith("sha256=") or len(header) != 71:
        raise ServiceError("webhook authentication failed")
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        raise ServiceError("webhook authentication failed")


async def _bounded_body(request: Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise PayloadTooLarge("request body is too large")
        body.extend(chunk)
    return bytes(body)


def create_app(
    *,
    settings: HttpSettings | None = None,
    review_service: ReviewService | None = None,
    auth_backend: AuthBackend | None = None,
    metrics_clock: Callable[[], float] | None = None,
    repair_service: SyntheticStagingRepairService | None = None,
) -> FastAPI:
    http = settings or HttpSettings.from_env()
    service = review_service or create_review_service_from_env()
    if repair_service is None and os.environ.get("CRAG_REPAIR_RUNTIME", ""):
        repair_service = create_synthetic_staging_repair_service(
            service.store.database,
            metrics=service.metrics,
        )
    monotonic = metrics_clock or time.monotonic
    if auth_backend is None:
        if http.auth_mode == "oidc":
            if http.oidc_configuration is None:
                raise InvalidRequest("OIDC mode requires complete OIDC configuration")
            auth_backend = OIDCJWTAuthBackend(http.oidc_configuration, service.store.database)
        elif http.local_token_enabled:
            local_principal = service.store.local_principal
            if local_principal is None:
                raise InvalidRequest("local token mode requires a local service principal")
            auth_backend = LocalTokenAuthBackend(http.service_token, local_principal)
        else:
            auth_backend = DatabaseAuthBackend(service.store.database)
    github_webhooks = GitHubWebhookProcessor(
        service.store.database,
        submit_pull_request=service.submit_webhook_pr,
        get_job=service.store.get,
    )
    mcp = create_mcp(
        service,
        principal_provider=current_principal,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(http.allowed_hosts),
            allowed_origins=sorted(http.allowed_origins),
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            service.shutdown()

    app = FastAPI(
        title="code-review-agent service",
        version="1alpha1",
        lifespan=lifespan,
    )
    app.state.review_service = service
    app.state.http_settings = http
    app.state.auth_backend = auth_backend
    app.state.github_webhooks = github_webhooks
    app.state.repair_service = repair_service

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        del request
        status = (
            404
            if isinstance(exc, JobNotFound)
            else 429
            if isinstance(exc, QuotaExceeded)
            else 409
            if isinstance(exc, IdempotencyConflict)
            else 403
            if isinstance(exc, AuthorizationDenied)
            else 503
            if isinstance(exc, ServiceClosed)
            else 413
            if isinstance(exc, PayloadTooLarge)
            else 503
            if isinstance(exc, PublisherFailed)
            else 400
        )
        if exc.code == "service_error":
            status = 401
        headers: dict[str, str] | None = None
        if status == 401:
            headers = {"WWW-Authenticate": "Bearer"}
        elif isinstance(exc, QuotaExceeded) and exc.retry_after is not None:
            headers = {"Retry-After": str(exc.retry_after)}
        return JSONResponse(
            status_code=status,
            content={"schema_version": SCHEMA_VERSION, "error": {"code": exc.code}},
            headers=headers,
        )

    @app.exception_handler(RepairServiceError)
    async def repair_service_error_handler(
        request: Request, exc: RepairServiceError
    ) -> JSONResponse:
        del request
        status = (
            404
            if isinstance(exc, RepairAuthorizationError)
            and exc.code
            in {"repair_cross_organization_denied", "repair_repository_not_found"}
            else 403
            if isinstance(exc, RepairAuthorizationError)
            else 409
            if isinstance(exc, RepairConflict)
            else 503
        )
        return JSONResponse(
            status_code=status,
            content={"schema_version": SCHEMA_VERSION, "error": {"code": exc.code}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=422,
            content={
                "schema_version": SCHEMA_VERSION,
                "error": {"code": "validation_error"},
            },
        )

    @app.exception_handler(DatabaseError)
    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(
        request: Request, exc: DatabaseError | SQLAlchemyError
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=409 if isinstance(exc, IntegrityError) else 503,
            content={
                "schema_version": SCHEMA_VERSION,
                "error": {
                    "code": "database_conflict"
                    if isinstance(exc, IntegrityError)
                    else "database_unavailable"
                },
            },
        )

    @app.middleware("http")
    async def protocol_boundary(request: Request, call_next):
        path = request.url.path
        supplied_correlation = request.headers.get("x-request-id", "")
        correlation_id = (
            supplied_correlation
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", supplied_correlation)
            else uuid.uuid4().hex
        )
        correlation_token = bind_correlation_id(correlation_id)
        principal_token = None
        try:
            if path.startswith("/v1/") or path == "/mcp" or path.startswith("/mcp/"):
                try:
                    principal = auth_backend.authenticate(
                        request.headers.get("authorization")
                    )
                    request.state.principal = principal
                    request.state.correlation_id = correlation_id
                    principal_token = bind_principal(principal)
                    if path == "/mcp" or path.startswith("/mcp/"):
                        origin = request.headers.get("origin")
                        if (
                            origin is not None
                            and origin.rstrip("/") not in http.allowed_origins
                        ):
                            return JSONResponse(
                                status_code=403, content={"error": "invalid_origin"}
                            )
                except AuthenticationRequired:
                    return JSONResponse(
                        status_code=401,
                        content={
                            "schema_version": SCHEMA_VERSION,
                            "error": {"code": "service_error"},
                        },
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                except (DatabaseError, SQLAlchemyError):
                    return JSONResponse(
                        status_code=503,
                        content={
                            "schema_version": SCHEMA_VERSION,
                            "error": {"code": "authentication_unavailable"},
                        },
                    )
            return await call_next(request)
        finally:
            if principal_token is not None:
                reset_principal(principal_token)
            reset_correlation_id(correlation_token)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"schema_version": SCHEMA_VERSION, "status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        database_ready = service.store.database_ready()
        if repair_service is None:
            workers = (
                service.store.live_worker_count(stale_seconds=http.worker_stale_seconds)
                if database_ready
                else 0
            )
        else:
            workers = (
                repair_service.postgres_store.live_worker_count(
                    stale_seconds=http.worker_stale_seconds
                )
                if database_ready
                else 0
            )
        ready = database_ready and workers > 0 and service.accepting
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "schema_version": SCHEMA_VERSION,
                "status": "ready" if ready else "not_ready",
                "database": "ready" if database_ready else "unavailable",
                "worker": "ready" if workers > 0 else "unavailable",
                "migration": "ready" if database_ready else "unavailable",
            },
        )

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            service.metrics.render(),
            media_type=CONTENT_TYPE,
        )

    def request_principal(request: Request) -> Principal:
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal):
            raise AuthorizationDenied("authenticated principal is required")
        return principal

    def require_organization(
        request: Request,
        organization_id: str,
        *,
        action: str = "organization.scope",
    ) -> Principal:
        principal = request_principal(request)
        if principal.organization_id != organization_id:
            service._audit(
                principal,
                action,
                "organization",
                "redacted",
                "deny",
                reason_code="cross_organization",
            )
            raise JobNotFound("organization was not found")
        return principal

    def require_repair_service() -> SyntheticStagingRepairService:
        if repair_service is None:
            raise RepairServiceError("synthetic_staging_repair_disabled")
        return repair_service

    def authorized_repair_repository(
        principal: Principal, identity: str
    ) -> dict[str, Any]:
        repository = service.store.database.authorized_repository(principal, identity)
        if repository is None:
            service._audit(
                principal,
                "repair.start",
                "repository",
                "redacted",
                "deny",
                reason_code="not_found",
            )
            raise JobNotFound("repository was not found")
        return repository

    @app.get("/v1/principal")
    def get_principal(request: Request) -> dict[str, Any]:
        return service.principal_record(request_principal(request))

    @app.post("/v1/reviews/diff", status_code=202)
    def submit_diff(request: Request, body: DiffSubmission) -> dict[str, Any]:
        return service.submit_diff(
            body.repository,
            body.diff,
            principal=request_principal(request),
            correlation_id=request.state.correlation_id,
            idempotency_key=request.headers.get("idempotency-key"),
        )

    @app.post("/v1/reviews/pr", status_code=202)
    def submit_pr(request: Request, body: PullRequestSubmission) -> dict[str, Any]:
        job, duplicate = service.submit_pr(
            body.repository,
            body.pull_request,
            principal=request_principal(request),
            correlation_id=request.state.correlation_id,
            idempotency_key=request.headers.get("idempotency-key"),
            head_sha=body.head_sha,
        )
        return {**job, "duplicate": duplicate}

    @app.post("/v1/repairs", status_code=202)
    def create_synthetic_repair(
        request: Request, body: SyntheticRepairCreate
    ) -> dict[str, Any]:
        principal = request_principal(request)
        repository = authorized_repair_repository(principal, body.repository)
        return require_repair_service().start_synthetic_repair(
            organization_id=principal.organization_id,
            repository_id=str(repository["id"]),
            finding_sha256=body.finding_sha256,
            base_sha=body.base_sha,
            head_sha=body.head_sha,
            actor=principal,
        )

    @app.get("/v1/repairs/{repair_job_id}")
    def get_synthetic_repair(request: Request, repair_job_id: str) -> dict[str, Any]:
        return require_repair_service().get_repair(
            repair_job_id, actor=request_principal(request)
        )

    @app.get("/v1/repairs/{repair_job_id}/write-approval")
    def get_write_approval_view(
        request: Request, repair_job_id: str
    ) -> dict[str, Any]:
        return require_repair_service().write_approval_view(
            repair_job_id, actor=request_principal(request)
        )

    @app.post("/v1/repairs/{repair_job_id}/write-approval/approve")
    def approve_write(
        request: Request, repair_job_id: str, body: SyntheticRepairDecision
    ) -> dict[str, Any]:
        return require_repair_service().decide_write(
            repair_job_id,
            actor=request_principal(request),
            checkpoint_sha256=body.checkpoint_sha256,
            approval_id=body.approval_id,
            approved=True,
        )

    @app.post("/v1/repairs/{repair_job_id}/write-approval/reject")
    def reject_write(
        request: Request, repair_job_id: str, body: SyntheticRepairDecision
    ) -> dict[str, Any]:
        return require_repair_service().decide_write(
            repair_job_id,
            actor=request_principal(request),
            checkpoint_sha256=body.checkpoint_sha256,
            approval_id=body.approval_id,
            approved=False,
        )

    @app.get("/v1/repairs/{repair_job_id}/draft-pr-approval")
    def get_draft_pr_approval_view(
        request: Request, repair_job_id: str
    ) -> dict[str, Any]:
        return require_repair_service().draft_pr_approval_view(
            repair_job_id, actor=request_principal(request)
        )

    @app.post("/v1/repairs/{repair_job_id}/draft-pr-approval/approve")
    def approve_draft_pr(
        request: Request, repair_job_id: str, body: SyntheticRepairDecision
    ) -> dict[str, Any]:
        return require_repair_service().decide_draft_pr(
            repair_job_id,
            actor=request_principal(request),
            checkpoint_sha256=body.checkpoint_sha256,
            approval_id=body.approval_id,
            approved=True,
        )

    @app.post("/v1/repairs/{repair_job_id}/draft-pr-approval/reject")
    def reject_draft_pr(
        request: Request, repair_job_id: str, body: SyntheticRepairDecision
    ) -> dict[str, Any]:
        return require_repair_service().decide_draft_pr(
            repair_job_id,
            actor=request_principal(request),
            checkpoint_sha256=body.checkpoint_sha256,
            approval_id=body.approval_id,
            approved=False,
        )

    @app.get("/v1/repairs/{repair_job_id}/receipt")
    def get_repair_receipt(request: Request, repair_job_id: str) -> dict[str, Any]:
        return require_repair_service().redacted_receipt(
            repair_job_id, actor=request_principal(request)
        )

    @app.get("/v1/reviews/pending-approval")
    def list_pending_approval_reviews(request: Request) -> dict[str, Any]:
        return {
            "reviews": service.list_pending_approvals(
                principal=request_principal(request)
            )
        }

    @app.post("/v1/reviews/{review_id}/approve")
    def approve_review(
        request: Request, review_id: str, body: PublicationDecision
    ) -> dict[str, Any]:
        return service.decide_review_publication(
            review_id,
            decision="approved",
            payload_sha256=body.payload_sha256,
            nonce=body.nonce,
            principal=request_principal(request),
        )

    @app.post("/v1/reviews/{review_id}/reject")
    def reject_review(
        request: Request, review_id: str, body: PublicationDecision
    ) -> dict[str, Any]:
        return service.decide_review_publication(
            review_id,
            decision="rejected",
            payload_sha256=body.payload_sha256,
            nonce=body.nonce,
            principal=request_principal(request),
        )

    @app.get("/v1/reviews/{review_id}")
    def get_review(request: Request, review_id: str) -> dict[str, Any]:
        return service.get(review_id, principal=request_principal(request))

    @app.get("/v1/reviews/{review_id}/trace", response_class=PlainTextResponse)
    def get_trace(request: Request, review_id: str) -> PlainTextResponse:
        return PlainTextResponse(
            service.get_trace(review_id, principal=request_principal(request)),
            media_type="application/x-ndjson",
        )

    @app.get("/v1/reviews/{review_id}/findings")
    def list_findings(request: Request, review_id: str) -> dict[str, Any]:
        return {
            "findings": service.list_findings(
                review_id, principal=request_principal(request)
            )
        }

    @app.get("/v1/findings/{finding_id}")
    def get_finding(request: Request, finding_id: str) -> dict[str, Any]:
        return service.get_finding(finding_id, principal=request_principal(request))

    @app.get("/v1/reviews/{review_id}/approvals")
    def list_review_approvals(request: Request, review_id: str) -> dict[str, Any]:
        return {
            "approvals": service.list_review_approvals(
                review_id, principal=request_principal(request)
            )
        }

    @app.get("/v1/findings/{finding_id}/feedback")
    def list_finding_feedback(request: Request, finding_id: str) -> dict[str, Any]:
        return {
            "feedback": service.list_finding_feedback(
                finding_id, principal=request_principal(request)
            )
        }

    @app.get("/v1/organizations/{organization_id}/memberships")
    def list_memberships(request: Request, organization_id: str) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="membership.list"
        )
        return {"memberships": service.list_members(principal)}

    @app.post("/v1/organizations/{organization_id}/memberships", status_code=201)
    def create_membership(
        request: Request, organization_id: str, body: MembershipCreate
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="membership.create"
        )
        return service.create_member(
            subject=body.subject,
            display_name=body.display_name,
            role=body.role,
            repository_ids=body.repository_ids,
            principal=principal,
        )

    @app.patch("/v1/organizations/{organization_id}/memberships/{membership_id}")
    def update_membership(
        request: Request,
        organization_id: str,
        membership_id: str,
        body: MembershipUpdate,
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="membership.update"
        )
        return service.update_member(
            membership_id,
            role=body.role,
            repository_ids=body.repository_ids,
            principal=principal,
        )

    @app.get("/v1/organizations/{organization_id}/repositories")
    def list_repositories(request: Request, organization_id: str) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="repository.list"
        )
        return {"repositories": service.list_repositories(principal)}

    @app.post("/v1/organizations/{organization_id}/repositories", status_code=201)
    def register_repository(
        request: Request, organization_id: str, body: RepositoryCreate
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="repository.create"
        )
        return service.register_repository(
            body.repository,
            mode=body.mode,
            budget_microusd=body.budget_microusd,
            policy_version=body.policy_version,
            principal=principal,
        )

    @app.patch(
        "/v1/organizations/{organization_id}/repositories/{repository_id}"
    )
    def update_repository(
        request: Request,
        organization_id: str,
        repository_id: str,
        body: RepositoryUpdate,
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="repository.update"
        )
        return service.update_repository(
            repository_id,
            mode=body.mode,
            budget_microusd=body.budget_microusd,
            policy_version=body.policy_version,
            principal=principal,
        )

    @app.get(
        "/v1/organizations/{organization_id}/repositories/{repository_id}/feedback-rules"
    )
    def list_feedback_rules(
        request: Request, organization_id: str, repository_id: str
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="feedback_rule.version.list"
        )
        return {
            "versions": service.list_feedback_rule_versions(
                repository_id, principal=principal
            )
        }

    @app.post(
        "/v1/organizations/{organization_id}/repositories/{repository_id}/feedback-rules",
        status_code=201,
    )
    def create_feedback_rule(
        request: Request,
        organization_id: str,
        repository_id: str,
        body: FeedbackRuleVersionCreate,
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="feedback_rule.version.create"
        )
        return service.create_feedback_rule_version(
            repository_id,
            version=body.version,
            rules=[rule.model_dump() for rule in body.rules],
            reason=body.reason,
            principal=principal,
        )

    @app.get(
        "/v1/organizations/{organization_id}/repositories/{repository_id}/feedback-rules/active"
    )
    def get_active_feedback_rule(
        request: Request, organization_id: str, repository_id: str
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="feedback_rule.active.read"
        )
        return {
            "active": service.get_active_feedback_rule(
                repository_id, principal=principal
            )
        }

    @app.post(
        "/v1/organizations/{organization_id}/repositories/{repository_id}/"
        "feedback-rules/{version}/activate"
    )
    def activate_feedback_rule(
        request: Request,
        organization_id: str,
        repository_id: str,
        version: str,
        body: FeedbackRuleTransition,
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="feedback_rule.activate"
        )
        return service.transition_feedback_rule(
            repository_id,
            version,
            action="activate",
            reason=body.reason,
            principal=principal,
        )

    @app.post(
        "/v1/organizations/{organization_id}/repositories/{repository_id}/"
        "feedback-rules/{version}/rollback"
    )
    def rollback_feedback_rule(
        request: Request,
        organization_id: str,
        repository_id: str,
        version: str,
        body: FeedbackRuleTransition,
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="feedback_rule.rollback"
        )
        return service.transition_feedback_rule(
            repository_id,
            version,
            action="rollback",
            reason=body.reason,
            principal=principal,
        )

    @app.get(
        "/v1/organizations/{organization_id}/repositories/{repository_id}/"
        "feedback-rule-receipts"
    )
    def list_feedback_rule_receipts(
        request: Request, organization_id: str, repository_id: str
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="feedback_rule.receipt.list"
        )
        return {
            "receipts": service.list_feedback_rule_receipts(
                repository_id, principal=principal
            )
        }

    @app.get("/v1/organizations/{organization_id}/policy")
    def get_organization_policy(request: Request, organization_id: str) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="organization.policy.read"
        )
        policy = service.get_organization_policy(principal=principal)
        return {"policy": policy}

    @app.put("/v1/organizations/{organization_id}/policy")
    def put_organization_policy(
        request: Request, organization_id: str, body: OrganizationPolicyUpdate
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="organization.policy.write"
        )
        return {
            "policy": service.put_organization_policy(
                principal=principal, **body.model_dump()
            )
        }

    @app.post(
        "/v1/organizations/{organization_id}/policy/{version}/invalidate",
        status_code=204,
    )
    def invalidate_organization_policy(
        request: Request, organization_id: str, version: str
    ) -> None:
        principal = require_organization(
            request, organization_id, action="organization.policy.invalidate"
        )
        service.invalidate_organization_policy(version, principal=principal)

    @app.get("/v1/organizations/{organization_id}/service-quota")
    def get_organization_service_quota(
        request: Request, organization_id: str
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="service_quota.read"
        )
        return service.get_service_quota(principal=principal)

    @app.patch("/v1/organizations/{organization_id}/service-quota")
    def update_organization_service_quota(
        request: Request, organization_id: str, body: ServiceQuotaUpdate
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="service_quota.update"
        )
        return service.update_service_quota(
            principal=principal, **body.model_dump(exclude_unset=True)
        )

    @app.get(
        "/v1/organizations/{organization_id}/repositories/{repository_id}/service-quota"
    )
    def get_repository_service_quota(
        request: Request, organization_id: str, repository_id: str
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="service_quota.read"
        )
        return service.get_service_quota(
            repository_id=repository_id, principal=principal
        )

    @app.patch(
        "/v1/organizations/{organization_id}/repositories/{repository_id}/service-quota"
    )
    def update_repository_service_quota(
        request: Request,
        organization_id: str,
        repository_id: str,
        body: ServiceQuotaUpdate,
    ) -> dict[str, Any]:
        principal = require_organization(
            request, organization_id, action="service_quota.update"
        )
        return service.update_service_quota(
            repository_id=repository_id,
            principal=principal,
            **body.model_dump(exclude_unset=True),
        )

    @app.post("/v1/credentials", status_code=201)
    def create_credential(request: Request, body: CredentialCreate) -> dict[str, Any]:
        return service.create_credential(
            expires_in_seconds=body.expires_in_seconds,
            principal=request_principal(request),
        )

    @app.delete("/v1/credentials/{credential_id}", status_code=204)
    def revoke_credential(request: Request, credential_id: str) -> None:
        service.revoke_credential(credential_id, principal=request_principal(request))

    @app.get("/v1/audit-events")
    def list_audit_events(request: Request, limit: int = 100) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise InvalidRequest("audit limit must be between 1 and 500")
        return {
            "audit_events": service.list_audit(
                limit=limit, principal=request_principal(request)
            )
        }

    @app.post("/v1/findings/{finding_id}/feedback", status_code=201)
    def submit_finding_feedback(
        request: Request, finding_id: str, body: FeedbackCreate
    ) -> dict[str, Any]:
        return service.submit_feedback(
            finding_id,
            decision=body.decision,
            finding_hash=body.finding_hash,
            reason=body.reason,
            rationale=body.rationale,
            principal=request_principal(request),
        )

    @app.post("/v1/findings/{finding_id}/decisions", status_code=201)
    def decide_finding(
        request: Request, finding_id: str, body: FindingDecision
    ) -> dict[str, Any]:
        return service.decide_finding(
            finding_id,
            decision=body.decision,
            principal=request_principal(request),
        )

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(request: Request) -> JSONResponse:
        started = monotonic()
        verified = False
        try:
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) > MAX_WEBHOOK_BYTES:
                        raise PayloadTooLarge("webhook body is too large")
                except ValueError as exc:
                    raise InvalidRequest("content-length is invalid") from exc
            body = await _bounded_body(request, MAX_WEBHOOK_BYTES)
            _webhook_signature(
                body,
                request.headers.get("x-hub-signature-256", ""),
                http.webhook_secret,
            )
            verified = True
            event = request.headers.get("x-github-event", "")
            delivery = request.headers.get("x-github-delivery", "")
            if not delivery:
                raise InvalidRequest("webhook delivery ID is required")
            payload: Any = None
            if event in {"installation", "pull_request"}:
                try:
                    payload = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise InvalidRequest("webhook body is not valid UTF-8 JSON") from exc
            acknowledgement = await anyio.to_thread.run_sync(
                lambda: github_webhooks.acknowledge(
                    event=event,
                    delivery_id=delivery,
                    body=body,
                    payload=payload,
                )
            )
            return JSONResponse(
                status_code=acknowledgement.status_code,
                content=acknowledgement.body,
            )
        finally:
            if verified:
                try:
                    service.metrics.observe_webhook_ack(max(0.0, monotonic() - started))
                except (SQLAlchemyError, ValueError):
                    # Metrics degradation cannot change webhook authority or response semantics.
                    pass

    app.mount("/mcp", mcp.streamable_http_app())
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the code-review-agent HTTP/MCP service")
    parser.add_argument("--host", default=os.environ.get("CRAG_SERVICE_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=_port,
        default=os.environ.get("CRAG_SERVICE_PORT", "8000"),
    )
    parser.add_argument("--log-level", choices=["critical", "error", "warning", "info"], default="info")
    return parser


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = HttpSettings.from_env()
    loopback_bind = args.host in {"127.0.0.1", "localhost", "::1"}
    trusted_container_bind = (
        args.host == "0.0.0.0" and settings.local_token_behind_loopback_publish
    )
    if settings.local_token_enabled and not (loopback_bind or trusted_container_bind):
        raise SystemExit(
            "local token mode requires loopback binding or trusted loopback publication"
        )
    app = create_app(settings=settings)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
