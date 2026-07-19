"""Authenticated FastAPI and GitHub Webhook service for asynchronous reviews."""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field
import anyio
import uvicorn

from code_review_agent.mcp_server import create_mcp
from code_review_agent.service_core import (
    InvalidRequest,
    JobNotFound,
    MAX_DIFF_BYTES,
    ReviewService,
    SCHEMA_VERSION,
    ServiceClosed,
    ServiceError,
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
    ) -> None:
        if len(service_token.encode("utf-8")) < 32:
            raise InvalidRequest("CRAG_SERVICE_TOKEN must be at least 32 UTF-8 bytes")
        if len(webhook_secret.encode("utf-8")) < 16:
            raise InvalidRequest("CRAG_WEBHOOK_SECRET must be at least 16 UTF-8 bytes")
        if not allowed_origins or any(not item.startswith(("http://", "https://")) for item in allowed_origins):
            raise InvalidRequest("CRAG_ALLOWED_ORIGINS must contain exact HTTP origins")
        if not allowed_hosts or any("/" in item or " " in item for item in allowed_hosts):
            raise InvalidRequest("CRAG_ALLOWED_HOSTS must contain exact host values")
        self.service_token = service_token
        self.webhook_secret = webhook_secret.encode("utf-8")
        self.allowed_origins = allowed_origins
        self.allowed_hosts = allowed_hosts

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
        return cls(
            service_token=os.environ.get("CRAG_SERVICE_TOKEN", ""),
            webhook_secret=os.environ.get("CRAG_WEBHOOK_SECRET", ""),
            allowed_origins=origins,
            allowed_hosts=hosts,
        )


def _bearer(request: Request, settings: HttpSettings) -> None:
    header = request.headers.get("authorization", "")
    scheme, separator, token = header.partition(" ")
    if separator != " " or scheme.casefold() != "bearer" or not hmac.compare_digest(
        token.encode("utf-8"), settings.service_token.encode("utf-8")
    ):
        raise ServiceError("authentication required")


def _webhook_signature(body: bytes, header: str, secret: bytes) -> None:
    if not header.startswith("sha256=") or len(header) != 71:
        raise ServiceError("webhook authentication failed")
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        raise ServiceError("webhook authentication failed")


def _webhook_fields(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict) or payload.get("action") not in _PULL_REQUEST_ACTIONS:
        raise InvalidRequest("pull_request action is not reviewable")
    repository = payload.get("repository")
    pull_request = payload.get("pull_request")
    if not isinstance(repository, dict) or not isinstance(pull_request, dict):
        raise InvalidRequest("webhook payload is missing repository or pull_request")
    alias = repository.get("full_name")
    number = pull_request.get("number")
    if not isinstance(alias, str) or isinstance(number, bool) or not isinstance(number, int):
        raise InvalidRequest("webhook repository or pull_request identity is invalid")
    return alias, str(number)


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
) -> FastAPI:
    http = settings or HttpSettings.from_env()
    service = review_service or create_review_service_from_env()
    mcp = create_mcp(
        service,
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

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        del request
        status = (
            404
            if isinstance(exc, JobNotFound)
            else 503
            if isinstance(exc, ServiceClosed)
            else 413
            if isinstance(exc, PayloadTooLarge)
            else 400
        )
        if exc.code == "service_error":
            status = 401
        return JSONResponse(
            status_code=status,
            content={"schema_version": SCHEMA_VERSION, "error": {"code": exc.code}},
            headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
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

    @app.middleware("http")
    async def protocol_boundary(request: Request, call_next):
        path = request.url.path
        if path.startswith("/v1/") or path == "/mcp" or path.startswith("/mcp/"):
            try:
                _bearer(request, http)
                if path == "/mcp" or path.startswith("/mcp/"):
                    origin = request.headers.get("origin")
                    if origin is not None and origin.rstrip("/") not in http.allowed_origins:
                        return JSONResponse(status_code=403, content={"error": "invalid_origin"})
            except ServiceError as exc:
                return await service_error_handler(request, exc)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"schema_version": SCHEMA_VERSION, "status": "ok"}

    @app.post("/v1/reviews/diff", status_code=202)
    def submit_diff(body: DiffSubmission) -> dict[str, Any]:
        return service.submit_diff(body.repository, body.diff)

    @app.post("/v1/reviews/pr", status_code=202)
    def submit_pr(body: PullRequestSubmission) -> dict[str, Any]:
        job, duplicate = service.submit_pr(body.repository, body.pull_request)
        return {**job, "duplicate": duplicate}

    @app.get("/v1/reviews/{review_id}")
    def get_review(review_id: str) -> dict[str, Any]:
        return service.get(review_id)

    @app.get("/v1/reviews/{review_id}/trace", response_class=PlainTextResponse)
    def get_trace(review_id: str) -> PlainTextResponse:
        return PlainTextResponse(service.get_trace(review_id), media_type="application/x-ndjson")

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
        repository, pull_request = _webhook_fields(payload)
        job, duplicate = await anyio.to_thread.run_sync(
            lambda: service.submit_pr(repository, pull_request, delivery_id=delivery)
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
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
