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
from typing import Any, AsyncIterator
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
from code_review_agent.mcp_server import create_mcp
from code_review_agent.identity import (
    AuthBackend,
    AuthenticationRequired,
    DatabaseAuthBackend,
    LocalTokenAuthBackend,
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
_PULL_REQUEST_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})


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


class CredentialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expires_in_seconds: int = Field(default=3600, ge=60, le=86400)


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: str = Field(pattern="^(accepted|rejected|uncertain|fixed|duplicate)$")
    rationale: str | None = Field(default=None, max_length=512)
    reason: str | None = Field(default=None, max_length=512)


class PublicationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    payload_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    nonce: str = Field(pattern="^[0-9a-f]{64}$")


class FindingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: str = Field(pattern="^(approved|rejected)$")


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
        worker_stale_seconds: float = 30.0,
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
        if not 1 <= worker_stale_seconds <= 3600:
            raise InvalidRequest("CRAG_WORKER_STALE_SECONDS must be between 1 and 3600")
        self.service_token = service_token
        self.webhook_secret = webhook_secret.encode("utf-8")
        self.allowed_origins = allowed_origins
        self.allowed_hosts = allowed_hosts
        self.local_token_enabled = local_token_enabled
        self.worker_stale_seconds = worker_stale_seconds

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
            worker_stale_seconds=worker_stale_seconds,
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


def _webhook_fields(payload: Any) -> tuple[str, str, str]:
    if not isinstance(payload, dict) or payload.get("action") not in _PULL_REQUEST_ACTIONS:
        raise InvalidRequest("pull_request action is not reviewable")
    repository = payload.get("repository")
    pull_request = payload.get("pull_request")
    if not isinstance(repository, dict) or not isinstance(pull_request, dict):
        raise InvalidRequest("webhook payload is missing repository or pull_request")
    alias = repository.get("full_name")
    number = pull_request.get("number")
    head = pull_request.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(alias, str) or isinstance(number, bool) or not isinstance(number, int):
        raise InvalidRequest("webhook repository or pull_request identity is invalid")
    if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-fA-F]{40,64}", head_sha) is None:
        raise InvalidRequest("webhook pull_request head SHA is invalid")
    return alias, str(number), head_sha.casefold()


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
) -> FastAPI:
    http = settings or HttpSettings.from_env()
    service = review_service or create_review_service_from_env()
    if auth_backend is None:
        if http.local_token_enabled:
            local_principal = service.store.local_principal
            if local_principal is None:
                raise InvalidRequest("local token mode requires a local service principal")
            auth_backend = LocalTokenAuthBackend(http.service_token, local_principal)
        else:
            auth_backend = DatabaseAuthBackend(service.store.database)
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
        workers = (
            service.store.live_worker_count(stale_seconds=http.worker_stale_seconds)
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
            },
        )

    def request_principal(request: Request) -> Principal:
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal):
            raise AuthorizationDenied("authenticated principal is required")
        return principal

    def require_organization(request: Request, organization_id: str) -> Principal:
        principal = request_principal(request)
        if principal.organization_id != organization_id:
            raise JobNotFound("organization was not found")
        return principal

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
        principal = require_organization(request, organization_id)
        return {"memberships": service.list_members(principal)}

    @app.post("/v1/organizations/{organization_id}/memberships", status_code=201)
    def create_membership(
        request: Request, organization_id: str, body: MembershipCreate
    ) -> dict[str, Any]:
        principal = require_organization(request, organization_id)
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
        principal = require_organization(request, organization_id)
        return service.update_member(
            membership_id,
            role=body.role,
            repository_ids=body.repository_ids,
            principal=principal,
        )

    @app.get("/v1/organizations/{organization_id}/repositories")
    def list_repositories(request: Request, organization_id: str) -> dict[str, Any]:
        principal = require_organization(request, organization_id)
        return {"repositories": service.list_repositories(principal)}

    @app.post("/v1/organizations/{organization_id}/repositories", status_code=201)
    def register_repository(
        request: Request, organization_id: str, body: RepositoryCreate
    ) -> dict[str, Any]:
        principal = require_organization(request, organization_id)
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
        principal = require_organization(request, organization_id)
        return service.update_repository(
            repository_id,
            mode=body.mode,
            budget_microusd=body.budget_microusd,
            policy_version=body.policy_version,
            principal=principal,
        )

    @app.get("/v1/organizations/{organization_id}/service-quota")
    def get_organization_service_quota(
        request: Request, organization_id: str
    ) -> dict[str, Any]:
        principal = require_organization(request, organization_id)
        return service.get_service_quota(principal=principal)

    @app.patch("/v1/organizations/{organization_id}/service-quota")
    def update_organization_service_quota(
        request: Request, organization_id: str, body: ServiceQuotaUpdate
    ) -> dict[str, Any]:
        principal = require_organization(request, organization_id)
        return service.update_service_quota(
            principal=principal, **body.model_dump(exclude_unset=True)
        )

    @app.get(
        "/v1/organizations/{organization_id}/repositories/{repository_id}/service-quota"
    )
    def get_repository_service_quota(
        request: Request, organization_id: str, repository_id: str
    ) -> dict[str, Any]:
        principal = require_organization(request, organization_id)
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
        principal = require_organization(request, organization_id)
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
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > MAX_WEBHOOK_BYTES:
                    raise PayloadTooLarge("webhook body is too large")
            except ValueError as exc:
                raise InvalidRequest("content-length is invalid") from exc
        body = await _bounded_body(request, MAX_WEBHOOK_BYTES)
        _webhook_signature(body, request.headers.get("x-hub-signature-256", ""), http.webhook_secret)
        event = request.headers.get("x-github-event", "")
        delivery = request.headers.get("x-github-delivery", "")
        if event == "ping":
            return JSONResponse(
                status_code=200,
                content={"schema_version": SCHEMA_VERSION, "status": "pong"},
            )
        if event != "pull_request":
            return JSONResponse(
                status_code=202,
                content={"schema_version": SCHEMA_VERSION, "status": "ignored"},
            )
        if not delivery:
            raise InvalidRequest("webhook delivery ID is required")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidRequest("webhook body is not valid UTF-8 JSON") from exc
        repository, pull_request, head_sha = _webhook_fields(payload)
        job, duplicate = await anyio.to_thread.run_sync(
            lambda: service.submit_webhook_pr(
                repository,
                pull_request,
                delivery_id=delivery,
                correlation_id=delivery,
                head_sha=head_sha,
            )
        )
        return JSONResponse(status_code=202, content={**job, "duplicate": duplicate})

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
    if settings.local_token_enabled and args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("local token mode may only bind to a loopback host")
    app = create_app(settings=settings)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
