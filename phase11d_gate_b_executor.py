"""Default-closed controls for the Phase 11D Gate B executor.

This module deliberately separates three states:

* a hash-only authorization draft;
* a frozen authorization awaiting the owner's exact approval; and
* an approved authorization that a future transport may use.

Network-capable commands first require an active authorization with an exact owner
approval. Credential values are read only from explicitly named sources, remain in
memory, and are never returned, logged, persisted, or included in command output.
"""
from __future__ import annotations

import argparse
import base64
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import sys
import threading
import time
from decimal import Decimal, ROUND_CEILING
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import phase11d_human_pilot as gate_a


EXECUTOR_RUNTIME_SCHEMA_VERSION = "crag.phase11d.gate-b-executor-runtime/v1alpha1"
EXECUTOR_AUTHORIZATION_SCHEMA_VERSION = "crag.phase11d.gate-b-authorization/v1alpha1"
EXACT_APPROVAL_PREFIX = "PHASE11D_GATE_B_EXACT_APPROVAL_V1"
EXECUTOR_SOURCE_FILES = (
    "phase11d_human_pilot.py",
    "phase11d_gate_b_executor.py",
    "requirements.lock",
)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
STABLE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
REPOSITORY_PATH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,511}\Z")

_DRAFT_FIELDS = frozenset(
    {
        "business_claim_allowed",
        "exact_approval_text",
        "formal_quality_status",
        "gate_b_allowed",
        "generated_at_utc",
        "model_quality_status",
        "owner_approval",
        "permission_switches",
        "required_fields",
        "schema_version",
        "template_id",
        "template_status",
    }
)
_OWNER_APPROVAL_FIELDS = frozenset(
    {
        "actor_id",
        "approved_at_utc",
        "binding_sha256",
        "decision",
        "exact_approval_text_sha256",
    }
)
_RUNTIME_FIELDS = frozenset(
    {
        "authorization_id",
        "created_at_utc",
        "credential_input_mode",
        "executor_id",
        "execution_capability",
        "frozen_deployment_sha256",
        "frozen_executable_source_sha256",
        "frozen_runtime_identity_sha256",
        "frozen_runtime_image_sha256",
        "frozen_source_tree_sha256",
        "network_default",
        "real_operations_default_enabled",
        "runtime_sha256",
        "schema_version",
    }
)
_GITHUB_HOST = "api.github.com"
_ZHIPU_HOST = "open.bigmodel.cn"
_MAX_HTTP_RESPONSE_BYTES = 1_000_000
_MAX_GITHUB_PAGES = 10
_MAX_DIFF_BYTES = 500_000
_GITHUB_API_VERSION = "2022-11-28"
_INPUT_MICRO_CNY_PER_MILLION = 8_000_000
_OUTPUT_MICRO_CNY_PER_MILLION = 28_000_000
_CACHED_INPUT_MICRO_CNY_PER_MILLION = 2_000_000


class GateBExecutorError(RuntimeError):
    """Stable, redacted Gate B authorization error."""


@dataclass(frozen=True)
class AuthorizationStatus:
    authorization_id: str
    canonical_authorization_sha256: str
    gate_b_allowed: bool
    execution_capability: str
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorization_id": self.authorization_id,
            "canonical_authorization_sha256": self.canonical_authorization_sha256,
            "gate_b_allowed": self.gate_b_allowed,
            "execution_capability": self.execution_capability,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class JsonTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None,
        timeout_seconds: int,
    ) -> HttpResponse: ...


@dataclass(frozen=True)
class InstallationToken:
    value: str
    expires_at_utc: str
    app_id: int
    installation_id: int


@dataclass(frozen=True)
class PullRequestCandidate:
    number: int
    github_id: int
    base_branch: str
    base_sha: str
    head_sha: str
    updated_at_utc: str
    selection_rank_sha256: str

    @property
    def pr_id(self) -> str:
        return f"pr-{self.number}"

    def receipt_row(self) -> dict[str, Any]:
        return {
            "pr_id": self.pr_id,
            "github_pull_request_id": self.github_id,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "updated_at_utc": self.updated_at_utc,
            "snapshot_sha256": sha256_bytes(
                canonical_json(
                    {
                        "github_pull_request_id": self.github_id,
                        "number": self.number,
                        "base_sha": self.base_sha,
                        "head_sha": self.head_sha,
                        "updated_at_utc": self.updated_at_utc,
                    }
                )
            ),
            "selection_rank_sha256": self.selection_rank_sha256,
        }


@dataclass(frozen=True)
class ReviewOutcome:
    pr_id: str
    status: str
    terminal_category: str
    finding_ids: tuple[str, ...]
    feedback_eligible_finding_ids: tuple[str, ...]
    provider_call_count: int
    http_attempt_count: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    response_sha256: str

    def receipt_row(self) -> dict[str, Any]:
        return {
            "pr_id": self.pr_id,
            "status": self.status,
            "terminal_category": self.terminal_category,
            "finding_ids": list(self.finding_ids),
            "feedback_eligible_finding_ids": list(self.feedback_eligible_finding_ids),
            "provider_call_count": self.provider_call_count,
            "http_attempt_count": self.http_attempt_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "response_sha256": self.response_sha256,
        }


@dataclass(frozen=True)
class EphemeralReviewFinding:
    """Human-readable Review evidence that must never be serialized to disk."""

    pr_id: str
    finding_id: str
    index: int
    title: str
    severity: str
    path: str
    line: int
    description: str
    response_sha256: str

    def __post_init__(self) -> None:
        _require_stable_id("ephemeral_finding_pr_id", self.pr_id)
        _require_sha256("ephemeral_finding_id", self.finding_id)
        _require_int("ephemeral_finding_index", self.index, minimum=1)
        if self.severity not in _FINDING_SEVERITIES:
            raise GateBExecutorError("ephemeral_finding_severity_invalid")
        _require_repository_path("ephemeral_finding_path", self.path)
        _require_int("ephemeral_finding_line", self.line, minimum=1)
        _require_sha256("ephemeral_finding_response", self.response_sha256)
        for name, value, maximum in (
            ("title", self.title, 240),
            ("description", self.description, 4000),
        ):
            if not isinstance(value, str) or not value or len(value) > maximum:
                raise GateBExecutorError(f"ephemeral_finding_{name}_invalid")
        if _contains_secret_like_content(
            "\n".join((self.title, self.path, self.description))
        ):
            raise GateBExecutorError("redaction_failure")

    def operator_view(self) -> dict[str, Any]:
        return {
            "pr_id": self.pr_id,
            "finding_id": self.finding_id,
            "index": self.index,
            "title": self.title,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "description": self.description,
            "response_sha256": self.response_sha256,
        }


@dataclass
class ReviewBudget:
    max_logical_calls: int
    max_http_attempts: int
    max_input_tokens: int
    max_output_tokens: int
    max_cached_tokens: int
    max_micro_cny: int
    max_wall_clock_seconds: int
    started_monotonic: float = 0.0
    logical_calls: int = 0
    http_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    micro_cny: int = 0

    def __post_init__(self) -> None:
        for name in (
            "max_logical_calls",
            "max_http_attempts",
            "max_wall_clock_seconds",
        ):
            _require_int(f"review_budget_{name}", getattr(self, name), minimum=1)
        for name in (
            "max_input_tokens",
            "max_output_tokens",
            "max_cached_tokens",
            "max_micro_cny",
        ):
            _require_int(f"review_budget_{name}", getattr(self, name))
        # This clock is process-owned. Accepting a caller-provided origin would let a
        # direct caller extend the wall-clock budget by supplying a future value.
        self.started_monotonic = time.monotonic()

    def _check_wall_clock(self) -> None:
        if time.monotonic() - self.started_monotonic > self.max_wall_clock_seconds:
            raise GateBExecutorError("budget_wall_clock_exhausted")

    def reserve_http(self, count: int = 1) -> None:
        self._check_wall_clock()
        _require_int("review_http_reservation", count, minimum=1)
        if self.http_attempts + count > self.max_http_attempts:
            raise GateBExecutorError("budget_http_attempts_exhausted")
        self.http_attempts += count

    def reserve_call(self) -> None:
        self._check_wall_clock()
        if self.logical_calls + 1 > self.max_logical_calls:
            raise GateBExecutorError("budget_logical_calls_exhausted")
        self.logical_calls += 1

    def settle(self, outcome: ReviewOutcome) -> None:
        self._check_wall_clock()
        input_tokens = _require_int("review_outcome_input_tokens", outcome.input_tokens)
        output_tokens = _require_int("review_outcome_output_tokens", outcome.output_tokens)
        cached_tokens = _require_int("review_outcome_cached_tokens", outcome.cached_tokens)
        if cached_tokens > input_tokens:
            raise GateBExecutorError("provider_usage_ambiguity")
        next_input_tokens = self.input_tokens + input_tokens
        next_output_tokens = self.output_tokens + output_tokens
        next_cached_tokens = self.cached_tokens + cached_tokens
        if next_input_tokens > self.max_input_tokens:
            raise GateBExecutorError("budget_input_tokens_exhausted")
        if next_output_tokens > self.max_output_tokens:
            raise GateBExecutorError("budget_output_tokens_exhausted")
        if next_cached_tokens > self.max_cached_tokens:
            raise GateBExecutorError("budget_cached_tokens_exhausted")
        next_micro_cny = self.micro_cny + review_cost_micro_cny(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )
        if next_micro_cny > self.max_micro_cny:
            raise GateBExecutorError("budget_cost_exhausted")
        self.input_tokens = next_input_tokens
        self.output_tokens = next_output_tokens
        self.cached_tokens = next_cached_tokens
        self.micro_cny = next_micro_cny

    def restore(self, usage: Mapping[str, Any]) -> None:
        """Restore validated cumulative usage before a receipt-bound resume."""
        values = {
            "logical_calls": _require_int(
                "review_resume_logical_calls", usage.get("logical_calls")
            ),
            "http_attempts": _require_int(
                "review_resume_http_attempts", usage.get("http_attempts")
            ),
            "input_tokens": _require_int(
                "review_resume_input_tokens", usage.get("input_tokens")
            ),
            "output_tokens": _require_int(
                "review_resume_output_tokens", usage.get("output_tokens")
            ),
            "cached_tokens": _require_int(
                "review_resume_cached_tokens", usage.get("cached_tokens")
            ),
            "micro_cny": _require_int(
                "review_resume_micro_cny", usage.get("micro_cny")
            ),
        }
        limits = {
            "logical_calls": self.max_logical_calls,
            "http_attempts": self.max_http_attempts,
            "input_tokens": self.max_input_tokens,
            "output_tokens": self.max_output_tokens,
            "cached_tokens": self.max_cached_tokens,
            "micro_cny": self.max_micro_cny,
        }
        if any(values[name] > limit for name, limit in limits.items()):
            raise GateBExecutorError("review_resume_budget_exhausted")
        for name, value in values.items():
            setattr(self, name, value)

    def to_dict(self) -> dict[str, int]:
        return {
            "logical_calls": self.logical_calls,
            "http_attempts": self.http_attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "micro_cny": self.micro_cny,
        }


def review_cost_micro_cny(*, input_tokens: int, output_tokens: int, cached_tokens: int) -> int:
    """Apply the frozen integer GLM tariff without floating-point billing math."""
    _require_int("cost_input_tokens", input_tokens)
    _require_int("cost_output_tokens", output_tokens)
    _require_int("cost_cached_tokens", cached_tokens)
    if cached_tokens > input_tokens:
        raise GateBExecutorError("provider_usage_ambiguity")
    uncached = input_tokens - cached_tokens
    numerator = (
        uncached * _INPUT_MICRO_CNY_PER_MILLION
        + cached_tokens * _CACHED_INPUT_MICRO_CNY_PER_MILLION
        + output_tokens * _OUTPUT_MICRO_CNY_PER_MILLION
    )
    return int((Decimal(numerator) / Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateBExecutorError("duplicate_json_key")
        result[key] = value
    return result


def load_json(path: Path, *, maximum_bytes: int = 1_000_000) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GateBExecutorError("input_unavailable") from exc
    if len(raw) > maximum_bytes:
        raise GateBExecutorError("input_too_large")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, GateBExecutorError) as exc:
        raise GateBExecutorError("invalid_json") from exc
    if not isinstance(value, dict):
        raise GateBExecutorError("json_object_required")
    return value


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class StrictHttpsJsonTransport:
    """HTTPS-only, no-redirect JSON transport with bounded responses."""

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        allowed_urls: frozenset[str] | None = None,
        max_response_bytes: int = _MAX_HTTP_RESPONSE_BYTES,
    ) -> None:
        if not allowed_hosts or max_response_bytes < 1:
            raise ValueError("transport configuration is invalid")
        if allowed_urls is not None:
            for allowed_url in allowed_urls:
                parsed = urllib_parse.urlsplit(allowed_url)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname not in allowed_hosts
                    or parsed.port not in {None, 443}
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.fragment
                ):
                    raise ValueError("transport URL allowlist is invalid")
        self._allowed_hosts = allowed_hosts
        self._allowed_urls = allowed_urls
        self._max_response_bytes = max_response_bytes
        context = ssl.create_default_context()
        self._opener = urllib_request.build_opener(
            urllib_request.HTTPSHandler(context=context),
            _NoRedirect(),
        )

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None,
        timeout_seconds: int,
    ) -> HttpResponse:
        if method not in {"GET", "POST"} or timeout_seconds < 1:
            raise GateBExecutorError("http_request_invalid")
        parsed = urllib_parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self._allowed_hosts
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise GateBExecutorError("http_endpoint_denied")
        if self._allowed_urls is not None and url not in self._allowed_urls:
            raise GateBExecutorError("http_endpoint_denied")
        try:
            data = None if payload is None else canonical_json(payload)
        except (TypeError, ValueError) as exc:
            raise GateBExecutorError("http_payload_invalid") from exc
        request_headers = {str(name): str(value) for name, value in headers.items()}
        request_headers.setdefault("Accept", "application/vnd.github+json")
        request_headers.setdefault("Content-Type", "application/json")
        request_headers.setdefault("User-Agent", "crag-phase11d-gate-b-v1")
        request = urllib_request.Request(
            url,
            data=data,
            method=method,
            headers=request_headers,
        )
        try:
            with self._opener.open(request, timeout=float(timeout_seconds)) as response:
                raw = response.read(self._max_response_bytes + 1)
                if len(raw) > self._max_response_bytes:
                    raise GateBExecutorError("http_response_too_large")
                return HttpResponse(
                    status=int(response.status),
                    headers={str(key).lower(): str(value) for key, value in response.headers.items()},
                    body=raw,
                )
        except urllib_error.HTTPError as exc:
            raw = exc.read(self._max_response_bytes + 1)
            if len(raw) > self._max_response_bytes:
                raw = b""
            return HttpResponse(
                status=int(exc.code),
                headers={str(key).lower(): str(value) for key, value in (exc.headers or {}).items()},
                body=raw,
            )
        except (urllib_error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise GateBExecutorError("http_transport_failure") from exc


def _load_response_value(response: HttpResponse, *, context: str) -> Any:
    if not 200 <= response.status < 300:
        raise GateBExecutorError(f"{context}_http_{response.status}")
    try:
        value = json.loads(response.body.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, GateBExecutorError) as exc:
        raise GateBExecutorError(f"{context}_response_invalid") from exc
    return value


def _load_response_json(response: HttpResponse, *, context: str) -> Mapping[str, Any]:
    value = _load_response_value(response, context=context)
    if not isinstance(value, Mapping):
        raise GateBExecutorError(f"{context}_response_invalid")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def _require_exact_fields(name: str, value: Mapping[str, Any], expected: frozenset[str]) -> None:
    if set(value) != expected:
        raise GateBExecutorError(f"{name}_fields_invalid")


def _require_stable_id(name: str, value: Any) -> str:
    if not isinstance(value, str) or STABLE_ID_RE.fullmatch(value) is None:
        raise GateBExecutorError(f"{name}_invalid")
    return value


def _require_git_sha(name: str, value: Any) -> str:
    if not isinstance(value, str) or GIT_SHA_RE.fullmatch(value) is None:
        raise GateBExecutorError(f"{name}_invalid")
    return value


def _require_branch(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or BRANCH_RE.fullmatch(value) is None
        or ".." in value
        or "//" in value
        or value.startswith("/")
        or value.endswith("/")
        or value.endswith(".lock")
        or "/." in value
    ):
        raise GateBExecutorError(f"{name}_invalid")
    return value


def _require_repository_path(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or REPOSITORY_PATH_RE.fullmatch(value) is None
        or value.startswith("/")
        or value.endswith("/")
        or ".." in value
        or "//" in value
        or "/." in value
    ):
        raise GateBExecutorError(f"{name}_invalid")
    return value


def _require_sha256(name: str, value: Any) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise GateBExecutorError(f"{name}_invalid")
    return value


def _require_int(name: str, value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise GateBExecutorError(f"{name}_invalid")
    return value


def _require_bool(name: str, value: Any) -> bool:
    if type(value) is not bool:
        raise GateBExecutorError(f"{name}_invalid")
    return value


def _require_utc(name: str, value: Any) -> datetime:
    if not isinstance(value, str) or UTC_RE.fullmatch(value) is None:
        raise GateBExecutorError(f"{name}_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise GateBExecutorError(f"{name}_invalid") from exc


def _self_hash(value: Mapping[str, Any], field: str) -> str:
    if field not in value:
        raise GateBExecutorError("self_hash_field_missing")
    clone = dict(value)
    clone[field] = ""
    return sha256_bytes(canonical_json(clone))


def _source_set_sha256(root: Path, relative_paths: Sequence[str], *, label: str) -> str:
    entries: list[dict[str, str]] = []
    for relative in relative_paths:
        path = root / relative
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise GateBExecutorError("runtime_source_unavailable") from exc
        entries.append({"path": relative, "sha256": sha256_bytes(payload)})
    return sha256_bytes(canonical_json({"kind": label, "artifacts": entries}))


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _der_length(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise GateBExecutorError("github_private_key_invalid")
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    count = first & 0x7F
    if count == 0 or count > 4 or offset + count > len(data):
        raise GateBExecutorError("github_private_key_invalid")
    length = int.from_bytes(data[offset : offset + count], "big")
    if length < 0x80:
        raise GateBExecutorError("github_private_key_invalid")
    return length, offset + count


def _der_tlv(data: bytes, offset: int, expected_tag: int) -> tuple[bytes, int]:
    if offset >= len(data) or data[offset] != expected_tag:
        raise GateBExecutorError("github_private_key_invalid")
    length, start = _der_length(data, offset + 1)
    end = start + length
    if end > len(data):
        raise GateBExecutorError("github_private_key_invalid")
    return data[start:end], end


def _der_integer(data: bytes, offset: int) -> tuple[int, int]:
    raw, end = _der_tlv(data, offset, 0x02)
    if not raw or raw[0] & 0x80:
        raise GateBExecutorError("github_private_key_invalid")
    if len(raw) > 1 and raw[0] == 0 and not raw[1] & 0x80:
        raise GateBExecutorError("github_private_key_invalid")
    return int.from_bytes(raw, "big"), end


def _pkcs1_numbers(der: bytes) -> tuple[int, int, int]:
    sequence, end = _der_tlv(der, 0, 0x30)
    if end != len(der):
        raise GateBExecutorError("github_private_key_invalid")
    offset = 0
    version, offset = _der_integer(sequence, offset)
    if version not in {0, 1}:
        raise GateBExecutorError("github_private_key_invalid")
    modulus, offset = _der_integer(sequence, offset)
    exponent, offset = _der_integer(sequence, offset)
    private_exponent, offset = _der_integer(sequence, offset)
    if modulus.bit_length() < 2048 or exponent < 3 or private_exponent < 1:
        raise GateBExecutorError("github_private_key_invalid")
    return modulus, exponent, private_exponent


def _private_key_numbers(private_key: bytes) -> tuple[int, int, int]:
    try:
        text = private_key.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GateBExecutorError("github_private_key_invalid") from exc
    match = re.fullmatch(
        r"-----BEGIN (?:RSA )?PRIVATE KEY-----\s*([A-Za-z0-9+/=\s]+)-----END (?:RSA )?PRIVATE KEY-----\s*",
        text,
    )
    if match is None:
        raise GateBExecutorError("github_private_key_invalid")
    try:
        der = base64.b64decode(re.sub(r"\s+", "", match.group(1)), validate=True)
    except (ValueError, TypeError) as exc:
        raise GateBExecutorError("github_private_key_invalid") from exc
    if text.startswith("-----BEGIN RSA PRIVATE KEY-----"):
        return _pkcs1_numbers(der)
    outer, end = _der_tlv(der, 0, 0x30)
    if end != len(der):
        raise GateBExecutorError("github_private_key_invalid")
    offset = 0
    version, offset = _der_integer(outer, offset)
    if version != 0:
        raise GateBExecutorError("github_private_key_invalid")
    _algorithm, offset = _der_tlv(outer, offset, 0x30)
    pkcs1, offset = _der_tlv(outer, offset, 0x04)
    if offset != len(outer):
        raise GateBExecutorError("github_private_key_invalid")
    return _pkcs1_numbers(pkcs1)


def _rs256_signature(private_key: bytes, signing_input: bytes) -> bytes:
    modulus, _exponent, private_exponent = _private_key_numbers(private_key)
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(
        signing_input
    ).digest()
    width = (modulus.bit_length() + 7) // 8
    padding_length = width - len(digest_info) - 3
    if padding_length < 8:
        raise GateBExecutorError("github_private_key_invalid")
    encoded = b"\x00\x01" + (b"\xff" * padding_length) + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus)
    return signature.to_bytes(width, "big")


def build_github_app_jwt(
    *,
    app_id: int,
    private_key: bytes,
    issued_at_epoch: int | None = None,
) -> str:
    """Create a short-lived RS256 app JWT without exposing key material."""
    _require_int("github_app_id", app_id, minimum=1)
    now = int(time.time()) if issued_at_epoch is None else issued_at_epoch
    if type(now) is not int or now < 1:
        raise GateBExecutorError("github_jwt_time_invalid")
    header = _base64url(canonical_json({"alg": "RS256", "typ": "JWT"}))
    payload = _base64url(canonical_json({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}))
    signing_input = f"{header}.{payload}".encode("ascii")
    return f"{header}.{payload}.{_base64url(_rs256_signature(private_key, signing_input))}"


def _read_private_key_file(path: Path, *, expected_sha256: str) -> bytes:
    _require_sha256("github_private_key_fingerprint", expected_sha256)
    try:
        file_stat = path.lstat()
        if (
            stat.S_ISLNK(file_stat.st_mode)
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_size <= 0
            or file_stat.st_size > 65_536
        ):
            raise GateBExecutorError("github_private_key_file_invalid")
        value = path.read_bytes()
    except OSError as exc:
        raise GateBExecutorError("github_private_key_file_unavailable") from exc
    if sha256_bytes(value) != expected_sha256:
        raise GateBExecutorError("credential_fingerprint_mismatch")
    return value


class GitHubAppAuthenticator:
    """Mints one short-lived installation token after the full local gate passes."""

    def __init__(self, transport: JsonTransport, *, timeout_seconds: int = 20) -> None:
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    def mint_installation_token(
        self,
        *,
        app_id: int,
        installation_id: int,
        private_key: bytes,
    ) -> InstallationToken:
        _require_int("github_app_id", app_id, minimum=1)
        _require_int("github_installation_id", installation_id, minimum=1)
        jwt_value = build_github_app_jwt(app_id=app_id, private_key=private_key)
        response = self._transport.request(
            method="POST",
            url=f"https://{_GITHUB_HOST}/app/installations/{installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {jwt_value}",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            payload=None,
            timeout_seconds=self._timeout_seconds,
        )
        if response.status != 201:
            raise GateBExecutorError("github_installation_token_failed")
        body = _load_response_json(response, context="github_installation_token")
        token = body.get("token")
        expires_at = body.get("expires_at")
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise GateBExecutorError("github_installation_token_invalid")
        if not isinstance(expires_at, str):
            raise GateBExecutorError("github_installation_token_invalid")
        if _require_utc("github_installation_token_expiry", expires_at) <= datetime.now(timezone.utc):
            raise GateBExecutorError("github_installation_token_expired")
        return InstallationToken(
            value=token,
            expires_at_utc=expires_at,
            app_id=app_id,
            installation_id=installation_id,
        )


_REPOSITORY_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


def _repository_part(name: str, value: str) -> str:
    if not isinstance(value, str) or _REPOSITORY_PART_RE.fullmatch(value) is None:
        raise GateBExecutorError(f"{name}_invalid")
    return value


class GitHubRepositoryReader:
    """Read-only GitHub API surface for deterministic Gate B PR selection."""

    def __init__(
        self,
        transport: JsonTransport,
        *,
        token: InstallationToken,
        owner: str,
        repository: str,
        expected_repository_id: int,
        timeout_seconds: int = 20,
    ) -> None:
        self._transport = transport
        self._token = token
        self._owner = _repository_part("github_owner", owner)
        self._repository = _repository_part("github_repository", repository)
        self._expected_repository_id = _require_int(
            "github_repository_id", expected_repository_id, minimum=1
        )
        self._timeout_seconds = _require_int("github_timeout_seconds", timeout_seconds, minimum=1)

    @property
    def _base_url(self) -> str:
        return f"https://{_GITHUB_HOST}/repos/{self._owner}/{self._repository}"

    def _request(self, *, path: str, accept: str = "application/vnd.github+json") -> HttpResponse:
        list_pattern = r"/pulls\?state=open&base=[A-Za-z0-9_.-]+&sort=updated&direction=desc&per_page=100&page=[1-9][0-9]*"
        pull_pattern = r"/pulls/[1-9][0-9]*"
        if path not in {""} and re.fullmatch(list_pattern, path) is None and re.fullmatch(
            pull_pattern, path
        ) is None:
            raise GateBExecutorError("github_endpoint_denied")
        return self._transport.request(
            method="GET",
            url=self._base_url + path,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self._token.value}",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            payload=None,
            timeout_seconds=self._timeout_seconds,
        )

    def verify_repository(self) -> None:
        body = _load_response_json(self._request(path=""), context="github_repository")
        if body.get("id") != self._expected_repository_id:
            raise GateBExecutorError("github_repository_identity_mismatch")
        if body.get("archived") is True or body.get("disabled") is True:
            raise GateBExecutorError("github_repository_unavailable")

    def list_open_pull_requests(
        self,
        *,
        base_branch: str,
        selection_seed_sha256: str,
        window_start_utc: str,
        window_end_utc: str,
        selected_count: int,
    ) -> tuple[list[PullRequestCandidate], dict[str, int]]:
        _repository_part("base_branch", base_branch)
        _require_sha256("selection_seed_sha256", selection_seed_sha256)
        start = _require_utc("selection_window_start", window_start_utc)
        end = _require_utc("selection_window_end", window_end_utc)
        if start >= end:
            raise GateBExecutorError("selection_window_invalid")
        _require_int("selected_pr_count", selected_count, minimum=20)
        if selected_count > 30:
            raise GateBExecutorError("selected_pr_count_invalid")
        candidates: list[PullRequestCandidate] = []
        seen_numbers: set[int] = set()
        excluded = {"wrong_base": 0, "draft": 0, "outside_window": 0, "malformed": 0}
        for page in range(1, _MAX_GITHUB_PAGES + 1):
            query = urllib_parse.urlencode(
                {
                    "state": "open",
                    "base": base_branch,
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": "100",
                    "page": str(page),
                }
            )
            response = self._request(path=f"/pulls?{query}")
            if response.status != 200:
                raise GateBExecutorError("github_pull_list_failed")
            rows = _load_response_value(response, context="github_pull_list")
            if not isinstance(rows, list):
                raise GateBExecutorError("github_pull_list_response_invalid")
            if not rows:
                break
            for row in rows:
                if not isinstance(row, Mapping):
                    excluded["malformed"] += 1
                    continue
                try:
                    number = _require_int("github_pr_number", row.get("number"), minimum=1)
                    github_id = _require_int("github_pr_id", row.get("id"), minimum=1)
                    if number in seen_numbers:
                        raise GateBExecutorError("github_pr_duplicate")
                    seen_numbers.add(number)
                    base = row.get("base")
                    head = row.get("head")
                    if not isinstance(base, Mapping) or not isinstance(head, Mapping):
                        raise GateBExecutorError("github_pr_malformed")
                    if base.get("ref") != base_branch:
                        excluded["wrong_base"] += 1
                        continue
                    if row.get("draft") is True:
                        excluded["draft"] += 1
                        continue
                    updated_at = _require_utc("github_pr_updated_at", row.get("updated_at"))
                    if not start <= updated_at <= end:
                        excluded["outside_window"] += 1
                        continue
                    base_sha = base.get("sha")
                    head_sha = head.get("sha")
                    if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", base_sha):
                        raise GateBExecutorError("github_pr_malformed")
                    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
                        raise GateBExecutorError("github_pr_malformed")
                except GateBExecutorError as exc:
                    if str(exc) == "github_pr_duplicate":
                        raise
                    excluded["malformed"] += 1
                    continue
                candidates.append(
                    PullRequestCandidate(
                        number=number,
                        github_id=github_id,
                        base_branch=base_branch,
                        base_sha=base_sha,
                        head_sha=head_sha,
                        updated_at_utc=row["updated_at"],
                        selection_rank_sha256=sha256_text(
                            f"{selection_seed_sha256}\npr-{number}"
                        ),
                    )
                )
            if len(rows) < 100:
                break
        candidates.sort(key=lambda item: (item.selection_rank_sha256, item.number))
        if len(candidates) < selected_count:
            raise GateBExecutorError("selection_insufficient_eligible")
        return candidates[:selected_count], excluded

    def pull_request_diff(self, number: int) -> str:
        _require_int("github_pr_number", number, minimum=1)
        response = self._request(
            path=f"/pulls/{number}",
            accept="application/vnd.github.v3.diff",
        )
        if response.status != 200 or not response.body or len(response.body) > _MAX_DIFF_BYTES:
            raise GateBExecutorError("github_diff_unavailable")
        try:
            return response.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GateBExecutorError("github_diff_invalid") from exc

    def pull_request_candidate(
        self,
        number: int,
        *,
        selection_seed_sha256: str,
    ) -> PullRequestCandidate:
        """Read back one selected PR as a candidate for snapshot comparison."""
        _require_int("github_pr_number", number, minimum=1)
        _require_sha256("selection_seed_sha256", selection_seed_sha256)
        response = self._request(path=f"/pulls/{number}")
        if response.status != 200:
            raise GateBExecutorError("github_pull_read_failed")
        row = _load_response_json(response, context="github_pull")
        if row.get("state") != "open" or row.get("draft") is True:
            raise GateBExecutorError("selection_candidate_drift")
        base = row.get("base")
        head = row.get("head")
        if not isinstance(base, Mapping) or not isinstance(head, Mapping):
            raise GateBExecutorError("github_pr_malformed")
        base_ref = base.get("ref")
        if not isinstance(base_ref, str):
            raise GateBExecutorError("github_pr_malformed")
        base_branch = _repository_part("base_branch", base_ref)
        base_sha = _require_git_sha("github_pr_base_sha", base.get("sha"))
        head_sha = _require_git_sha("github_pr_head_sha", head.get("sha"))
        updated_at = row.get("updated_at")
        if not isinstance(updated_at, str):
            raise GateBExecutorError("github_pr_malformed")
        _require_utc("github_pr_updated_at", updated_at)
        return PullRequestCandidate(
            number=number,
            github_id=_require_int("github_pr_id", row.get("id"), minimum=1),
            base_branch=base_branch,
            base_sha=base_sha,
            head_sha=head_sha,
            updated_at_utc=updated_at,
            selection_rank_sha256=sha256_text(f"{selection_seed_sha256}\npr-{number}"),
        )


_REDACTION_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"\b(?:ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|"
        r"sk-[A-Za-z0-9_\-]{16,})\b"
    ),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
)
_FINDING_SEVERITIES = frozenset({"critical", "high", "medium", "low"})


def _contains_secret_like_content(value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in _REDACTION_PATTERNS)


def _review_failure(pr_id: str, category: str, *, response_sha256: str = "") -> ReviewOutcome:
    return ReviewOutcome(
        pr_id=pr_id,
        status="failed",
        terminal_category=category,
        finding_ids=(),
        feedback_eligible_finding_ids=(),
        provider_call_count=0,
        http_attempt_count=0,
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        response_sha256=response_sha256 or sha256_text(category),
    )


class ZhipuReviewClient:
    """One-shot structured review client with no automatic retry or raw receipts."""

    endpoint = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    def __init__(
        self,
        transport: JsonTransport,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int = 120,
        finding_sink: Callable[[tuple[EphemeralReviewFinding, ...]], None] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not 1 <= len(api_key) <= 4096:
            raise GateBExecutorError("provider_key_invalid")
        if model != "glm-5.2":
            raise GateBExecutorError("provider_model_invalid")
        self._transport = transport
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = _require_int("provider_timeout_seconds", timeout_seconds, minimum=1)
        self._finding_sink = finding_sink

    @staticmethod
    def _tool_schema() -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "submit_review",
                "description": "Return the review findings in the required structure.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "findings": {
                            "type": "array",
                            "maxItems": 20,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "title": {"type": "string", "maxLength": 240},
                                    "severity": {"type": "string", "enum": sorted(_FINDING_SEVERITIES)},
                                    "path": {"type": "string", "maxLength": 512},
                                    "line": {"type": "integer", "minimum": 1},
                                    "description": {"type": "string", "maxLength": 4000},
                                },
                                "required": ["title", "severity", "path", "line", "description"],
                            },
                        }
                    },
                    "required": ["findings"],
                },
            },
        }

    def review(self, *, candidate: PullRequestCandidate, diff_text: str) -> ReviewOutcome:
        if len(diff_text.encode("utf-8")) > _MAX_DIFF_BYTES:
            return _review_failure(candidate.pr_id, "provider_input_too_large")
        if _contains_secret_like_content(diff_text):
            return _review_failure(candidate.pr_id, "redaction_failure")
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Review only the supplied pull-request diff. Return exactly one "
                        "submit_review tool call. Do not provide plain text."
                    ),
                },
                {"role": "user", "content": f"Review this diff:\n\n```diff\n{diff_text}\n```"},
            ],
            "tools": [self._tool_schema()],
            "tool_choice": "auto",
        }
        try:
            response = self._transport.request(
                method="POST",
                url=self.endpoint,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                payload=payload,
                timeout_seconds=self._timeout_seconds,
            )
        except GateBExecutorError:
            return ReviewOutcome(
                pr_id=candidate.pr_id,
                status="failed",
                terminal_category="provider_failure",
                finding_ids=(),
                feedback_eligible_finding_ids=(),
                provider_call_count=1,
                http_attempt_count=1,
                input_tokens=0,
                output_tokens=0,
                cached_tokens=0,
                response_sha256=sha256_text("provider_failure"),
            )
        response_sha = sha256_bytes(response.body)
        if response.status != 200:
            return ReviewOutcome(
                pr_id=candidate.pr_id,
                status="failed",
                terminal_category="provider_failure",
                finding_ids=(),
                feedback_eligible_finding_ids=(),
                provider_call_count=1,
                http_attempt_count=1,
                input_tokens=0,
                output_tokens=0,
                cached_tokens=0,
                response_sha256=response_sha,
            )
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        try:
            body = _load_response_json(response, context="provider")
            usage = body.get("usage")
            if not isinstance(usage, Mapping):
                raise GateBExecutorError("provider_usage_ambiguity")
            input_tokens = _require_int("provider_input_tokens", usage.get("prompt_tokens"))
            output_tokens = _require_int("provider_output_tokens", usage.get("completion_tokens"))
            cached_tokens = usage.get("prompt_cache_hit_tokens", 0)
            _require_int("provider_cached_tokens", cached_tokens)
            choices = body.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
                raise GateBExecutorError("provider_malformed_tool_response")
            message = choices[0].get("message")
            if not isinstance(message, Mapping):
                raise GateBExecutorError("provider_malformed_tool_response")
            calls = message.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                category = "provider_text_only_response" if message.get("content") else "provider_malformed_tool_response"
                return ReviewOutcome(
                    pr_id=candidate.pr_id,
                    status="failed",
                    terminal_category=category,
                    finding_ids=(),
                    feedback_eligible_finding_ids=(),
                    provider_call_count=1,
                    http_attempt_count=1,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    response_sha256=response_sha,
                )
            if len(calls) != 1 or not isinstance(calls[0], Mapping):
                raise GateBExecutorError("provider_tool_call_mismatch")
            function = calls[0].get("function")
            if not isinstance(function, Mapping) or function.get("name") != "submit_review":
                raise GateBExecutorError("provider_tool_call_mismatch")
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                raise GateBExecutorError("provider_malformed_tool_response")
            value = json.loads(arguments, object_pairs_hook=_reject_duplicate_keys)
            if not isinstance(value, Mapping) or set(value) != {"findings"}:
                raise GateBExecutorError("provider_schema_mismatch")
            findings = value["findings"]
            if not isinstance(findings, list) or len(findings) > 20:
                raise GateBExecutorError("provider_schema_mismatch")
            finding_ids: list[str] = []
            ephemeral_findings: list[EphemeralReviewFinding] = []
            for index, finding in enumerate(findings, 1):
                if not isinstance(finding, Mapping) or set(finding) != {
                    "title",
                    "severity",
                    "path",
                    "line",
                    "description",
                }:
                    raise GateBExecutorError("provider_schema_mismatch")
                if (
                    not isinstance(finding["title"], str)
                    or not isinstance(finding["description"], str)
                    or not isinstance(finding["path"], str)
                    or finding["severity"] not in _FINDING_SEVERITIES
                    or type(finding["line"]) is not int
                    or finding["line"] < 1
                ):
                    raise GateBExecutorError("provider_schema_mismatch")
                if any(len(str(finding[name])) > maximum for name, maximum in (("title", 240), ("path", 512), ("description", 4000))):
                    raise GateBExecutorError("provider_schema_mismatch")
                finding_id = sha256_bytes(
                    canonical_json(
                        {
                            "pr_id": candidate.pr_id,
                            "index": index,
                            "finding": dict(finding),
                        }
                    )
                )
                finding_ids.append(finding_id)
                ephemeral_findings.append(
                    EphemeralReviewFinding(
                        pr_id=candidate.pr_id,
                        finding_id=finding_id,
                        index=index,
                        title=finding["title"],
                        severity=finding["severity"],
                        path=finding["path"],
                        line=finding["line"],
                        description=finding["description"],
                        response_sha256=response_sha,
                    )
                )
        except GateBExecutorError as exc:
            category = str(exc)
            if category not in {
                "provider_usage_ambiguity",
                "provider_tool_call_mismatch",
                "provider_schema_mismatch",
                "provider_malformed_tool_response",
            }:
                category = "provider_malformed_tool_response"
            return ReviewOutcome(
                pr_id=candidate.pr_id,
                status="failed",
                terminal_category=category,
                finding_ids=(),
                feedback_eligible_finding_ids=(),
                provider_call_count=1,
                http_attempt_count=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                response_sha256=response_sha,
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return ReviewOutcome(
                pr_id=candidate.pr_id,
                status="failed",
                terminal_category="provider_malformed_tool_response",
                finding_ids=(),
                feedback_eligible_finding_ids=(),
                provider_call_count=1,
                http_attempt_count=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                response_sha256=response_sha,
            )
        outcome = ReviewOutcome(
            pr_id=candidate.pr_id,
            status="completed",
            terminal_category="completed",
            finding_ids=tuple(finding_ids),
            feedback_eligible_finding_ids=tuple(finding_ids),
            provider_call_count=1,
            http_attempt_count=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            response_sha256=response_sha,
        )
        if self._finding_sink is not None:
            self._finding_sink(tuple(ephemeral_findings))
        return outcome


def review_with_budget(
    *,
    client: ZhipuReviewClient,
    budget: ReviewBudget,
    candidate: PullRequestCandidate,
    diff_text: str,
) -> ReviewOutcome:
    """Run one review while reserving the logical/HTTP budget before transport."""
    if len(diff_text.encode("utf-8")) > _MAX_DIFF_BYTES or _contains_secret_like_content(diff_text):
        return client.review(candidate=candidate, diff_text=diff_text)
    budget.reserve_call()
    budget.reserve_http()
    outcome = client.review(candidate=candidate, diff_text=diff_text)
    if outcome.provider_call_count != 1 or outcome.http_attempt_count != 1:
        raise GateBExecutorError("provider_usage_ambiguity")
    budget.settle(outcome)
    return outcome


SELECTION_RECEIPT_SCHEMA_VERSION = "crag.phase11d.gate-b-selection-receipt/v1alpha1"
_SELECTION_RECEIPT_FIELDS = frozenset(
    {
        "authorization_id",
        "canonical_authorization_sha256",
        "excluded_counts",
        "repository_id",
        "selected_pr_count",
        "selected_prs",
        "selection_receipt_sha256",
        "selection_seed_sha256",
        "selection_window_end_utc",
        "selection_window_start_utc",
        "schema_version",
    }
)
_SELECTION_ROW_FIELDS = frozenset(
    {
        "base_sha",
        "github_pull_request_id",
        "head_sha",
        "pr_id",
        "selection_rank_sha256",
        "snapshot_sha256",
        "updated_at_utc",
    }
)


def build_selection_receipt(
    *,
    authorization: Mapping[str, Any],
    candidates: Sequence[PullRequestCandidate],
    excluded_counts: Mapping[str, int],
) -> dict[str, Any]:
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    _validate_required_values(required)
    selected_count = required["selected_pr_count"]
    if len(candidates) != selected_count:
        raise GateBExecutorError("selection_count_mismatch")
    rows = [candidate.receipt_row() for candidate in candidates]
    if len({row["pr_id"] for row in rows}) != len(rows):
        raise GateBExecutorError("selection_duplicate_pr")
    normalized_excluded: dict[str, int] = {}
    for key, value in excluded_counts.items():
        _require_stable_id("selection_exclusion_reason", key)
        normalized_excluded[key] = _require_int("selection_exclusion_count", value)
    receipt: dict[str, Any] = {
        "schema_version": SELECTION_RECEIPT_SCHEMA_VERSION,
        "authorization_id": required["authorization_id"],
        "canonical_authorization_sha256": required["canonical_authorization_sha256"],
        "repository_id": required["repository_allowlist"]["repository_ids"][0],
        "selection_window_start_utc": required["pr_selection_window_start_utc"],
        "selection_window_end_utc": required["pr_selection_window_end_utc"],
        "selection_seed_sha256": required["deterministic_selection_seed_sha256"],
        "selected_pr_count": selected_count,
        "selected_prs": rows,
        "excluded_counts": normalized_excluded,
        "selection_receipt_sha256": "",
    }
    receipt["selection_receipt_sha256"] = _self_hash(receipt, "selection_receipt_sha256")
    validate_selection_receipt(receipt)
    return receipt


def validate_selection_receipt(receipt: Mapping[str, Any]) -> None:
    _require_exact_fields("selection_receipt", receipt, _SELECTION_RECEIPT_FIELDS)
    if receipt["schema_version"] != SELECTION_RECEIPT_SCHEMA_VERSION:
        raise GateBExecutorError("selection_schema_invalid")
    _require_stable_id("selection_authorization_id", receipt["authorization_id"])
    for name in (
        "canonical_authorization_sha256",
        "selection_seed_sha256",
        "selection_receipt_sha256",
    ):
        _require_sha256(name, receipt[name])
    _require_stable_id("selection_repository_id", receipt["repository_id"])
    _require_utc("selection_window_start", receipt["selection_window_start_utc"])
    _require_utc("selection_window_end", receipt["selection_window_end_utc"])
    selected_count = _require_int("selection_selected_pr_count", receipt["selected_pr_count"], minimum=20)
    if selected_count > 30 or not isinstance(receipt["selected_prs"], list) or len(receipt["selected_prs"]) != selected_count:
        raise GateBExecutorError("selection_count_invalid")
    seen: set[str] = set()
    previous_rank: tuple[str, int] | None = None
    for row in receipt["selected_prs"]:
        if not isinstance(row, Mapping):
            raise GateBExecutorError("selection_row_invalid")
        _require_exact_fields("selection_row", row, _SELECTION_ROW_FIELDS)
        pr_id = _require_stable_id("selection_pr_id", row["pr_id"])
        if pr_id in seen:
            raise GateBExecutorError("selection_duplicate_pr")
        seen.add(pr_id)
        number_match = re.fullmatch(r"pr-([1-9][0-9]*)", pr_id)
        if number_match is None:
            raise GateBExecutorError("selection_pr_id_invalid")
        number = int(number_match.group(1))
        github_id = _require_int("selection_github_pull_request_id", row["github_pull_request_id"], minimum=1)
        for name in ("snapshot_sha256", "selection_rank_sha256"):
            _require_sha256(name, row[name])
        for name in ("base_sha", "head_sha"):
            if not isinstance(row[name], str) or re.fullmatch(r"[0-9a-f]{40}", row[name]) is None:
                raise GateBExecutorError("selection_git_sha_invalid")
        _require_utc("selection_updated_at", row["updated_at_utc"])
        expected_rank = sha256_text(f"{receipt['selection_seed_sha256']}\n{pr_id}")
        if row["selection_rank_sha256"] != expected_rank:
            raise GateBExecutorError("selection_rank_mismatch")
        expected_snapshot = sha256_bytes(
            canonical_json(
                {
                    "github_pull_request_id": github_id,
                    "number": number,
                    "base_sha": row["base_sha"],
                    "head_sha": row["head_sha"],
                    "updated_at_utc": row["updated_at_utc"],
                }
            )
        )
        if row["snapshot_sha256"] != expected_snapshot:
            raise GateBExecutorError("selection_snapshot_mismatch")
        rank_key = (expected_rank, number)
        if previous_rank is not None and rank_key < previous_rank:
            raise GateBExecutorError("selection_order_invalid")
        previous_rank = rank_key
    if not isinstance(receipt["excluded_counts"], Mapping):
        raise GateBExecutorError("selection_excluded_counts_invalid")
    for key, value in receipt["excluded_counts"].items():
        _require_stable_id("selection_exclusion_reason", key)
        _require_int("selection_exclusion_count", value)
    if _self_hash(receipt, "selection_receipt_sha256") != receipt["selection_receipt_sha256"]:
        raise GateBExecutorError("selection_receipt_hash_mismatch")


def _github_numeric_repository_id(repository_id: Any) -> int:
    if not isinstance(repository_id, str):
        raise GateBExecutorError("repository_id_invalid")
    match = re.fullmatch(r"repo-([1-9][0-9]*)", repository_id)
    if match is None:
        raise GateBExecutorError("repository_id_invalid")
    return int(match.group(1))


def select_authorized_pull_requests(
    *,
    authorization: Mapping[str, Any],
    participants: Mapping[str, Any],
    repository_authorization: Mapping[str, Any],
    credential_descriptor: Mapping[str, Any],
    runtime: Mapping[str, Any],
    source_root: Path,
    github_app_private_key_file: Path,
    provider_key_environment: str,
    owner: str,
    repository: str,
    now_utc: str | None = None,
    transport: JsonTransport | None = None,
) -> dict[str, Any]:
    """Perform the bounded, authenticated read that materializes the fixed cohort."""
    require_active_execution_authorization(
        authorization=authorization,
        participants=participants,
        repository=repository_authorization,
        credential_descriptor=credential_descriptor,
        runtime=runtime,
        source_root=source_root,
        now_utc=now_utc,
    )
    verify_credential_fingerprints(
        authorization=authorization,
        participants=participants,
        repository=repository_authorization,
        credential_descriptor=credential_descriptor,
        runtime=runtime,
        source_root=source_root,
        github_app_private_key_file=github_app_private_key_file,
        provider_key_environment=provider_key_environment,
        now_utc=now_utc,
    )
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    _validate_repository_authorization(required, repository_authorization)
    _validate_descriptor(required, credential_descriptor)
    normalized_locator = f"https://github.com/{_repository_part('github_owner', owner)}/{_repository_part('github_repository', repository)}"
    repository_rows = repository_authorization["repositories"]
    assert isinstance(repository_rows, list) and isinstance(repository_rows[0], Mapping)
    if sha256_text(normalized_locator) != repository_rows[0]["locator_sha256"]:
        raise GateBExecutorError("repository_locator_mismatch")
    private_key = _read_private_key_file(
        github_app_private_key_file,
        expected_sha256=credential_descriptor["github_app_private_key_fingerprint_sha256"],
    )
    active_transport = transport or StrictHttpsJsonTransport(allowed_hosts=frozenset({_GITHUB_HOST}))
    token = GitHubAppAuthenticator(active_transport).mint_installation_token(
        app_id=credential_descriptor["github_app_id"],
        installation_id=credential_descriptor["github_app_installation_id"],
        private_key=private_key,
    )
    del private_key
    reader = GitHubRepositoryReader(
        active_transport,
        token=token,
        owner=owner,
        repository=repository,
        expected_repository_id=_github_numeric_repository_id(required["repository_allowlist"]["repository_ids"][0]),
    )
    reader.verify_repository()
    candidates, excluded = reader.list_open_pull_requests(
        base_branch=required["allowed_base_branch_rule"]["base_branch"],
        selection_seed_sha256=required["deterministic_selection_seed_sha256"],
        window_start_utc=required["pr_selection_window_start_utc"],
        window_end_utc=required["pr_selection_window_end_utc"],
        selected_count=required["selected_pr_count"],
    )
    return build_selection_receipt(
        authorization=authorization,
        candidates=candidates,
        excluded_counts=excluded,
    )


def freeze_executor_runtime(
    *,
    source_root: Path,
    authorization_id: str,
    executor_id: str,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    """Create the five hash bindings for the actual default-closed executor."""
    root = source_root.resolve()
    if not root.is_dir():
        raise GateBExecutorError("runtime_source_root_unavailable")
    _require_stable_id("authorization_id", authorization_id)
    _require_stable_id("executor_id", executor_id)
    timestamp = created_at_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _require_utc("runtime_created_at", timestamp)

    source_tree = _source_set_sha256(root, EXECUTOR_SOURCE_FILES, label="phase11d_gate_b_source")
    try:
        executable = sha256_bytes((root / "phase11d_gate_b_executor.py").read_bytes())
        lock_sha256 = sha256_bytes((root / "requirements.lock").read_bytes())
    except OSError as exc:
        raise GateBExecutorError("runtime_source_unavailable") from exc
    runtime_image = sha256_bytes(
        canonical_json(
            {
                "runtime_kind": "python_venv",
                "python_implementation": sys.implementation.name,
                "python_version": ".".join(str(part) for part in sys.version_info[:3]),
                "requirements_lock_sha256": lock_sha256,
            }
        )
    )
    deployment = sha256_bytes(
        canonical_json(
            {
                "deployment_mode": "owner_local_cli",
                "network_default": "closed",
                "provider_transport": "authorization_gated",
                "github_transport": "authorization_gated",
                "credential_input_mode": "explicit_file_or_environment",
            }
        )
    )
    runtime_identity = sha256_bytes(
        canonical_json(
            {
                "runtime_identity_kind": "local_owner_cli",
                "authorization_id": authorization_id,
                "execution_capability": "authorization_gated_real_executor",
            }
        )
    )
    runtime: dict[str, Any] = {
        "schema_version": EXECUTOR_RUNTIME_SCHEMA_VERSION,
        "executor_id": executor_id,
        "authorization_id": authorization_id,
        "created_at_utc": timestamp,
        "frozen_source_tree_sha256": source_tree,
        "frozen_executable_source_sha256": executable,
        "frozen_runtime_image_sha256": runtime_image,
        "frozen_deployment_sha256": deployment,
        "frozen_runtime_identity_sha256": runtime_identity,
        "execution_capability": "authorization_gated_real_executor",
        "real_operations_default_enabled": False,
        "credential_input_mode": "explicit_file_or_environment",
        "network_default": "closed",
        "runtime_sha256": "",
    }
    runtime["runtime_sha256"] = _self_hash(runtime, "runtime_sha256")
    validate_executor_runtime(runtime)
    return runtime


def validate_executor_runtime(runtime: Mapping[str, Any]) -> None:
    _require_exact_fields("runtime", runtime, _RUNTIME_FIELDS)
    if runtime["schema_version"] != EXECUTOR_RUNTIME_SCHEMA_VERSION:
        raise GateBExecutorError("runtime_schema_invalid")
    _require_stable_id("runtime_executor_id", runtime["executor_id"])
    _require_stable_id("runtime_authorization_id", runtime["authorization_id"])
    _require_utc("runtime_created_at", runtime["created_at_utc"])
    for field in (
        "frozen_source_tree_sha256",
        "frozen_executable_source_sha256",
        "frozen_runtime_image_sha256",
        "frozen_deployment_sha256",
        "frozen_runtime_identity_sha256",
        "runtime_sha256",
    ):
        _require_sha256(field, runtime[field])
    if runtime["execution_capability"] != "authorization_gated_real_executor":
        raise GateBExecutorError("runtime_capability_invalid")
    if _require_bool("runtime_real_operations_default_enabled", runtime["real_operations_default_enabled"]):
        raise GateBExecutorError("runtime_default_open")
    if runtime["credential_input_mode"] != "explicit_file_or_environment":
        raise GateBExecutorError("runtime_credential_mode_invalid")
    if runtime["network_default"] != "closed":
        raise GateBExecutorError("runtime_network_default_open")
    if _self_hash(runtime, "runtime_sha256") != runtime["runtime_sha256"]:
        raise GateBExecutorError("runtime_hash_mismatch")


def validate_live_executor_runtime(*, source_root: Path, runtime: Mapping[str, Any]) -> None:
    """Recompute the frozen executor descriptors before credentials or transport use."""
    validate_executor_runtime(runtime)
    actual_root = Path(__file__).resolve().parent
    if source_root.resolve() != actual_root:
        raise GateBExecutorError("live_runtime_source_root_mismatch")
    observed = freeze_executor_runtime(
        source_root=source_root,
        authorization_id=runtime["authorization_id"],
        executor_id=runtime["executor_id"],
        created_at_utc=runtime["created_at_utc"],
    )
    for name in (
        "frozen_source_tree_sha256",
        "frozen_executable_source_sha256",
        "frozen_runtime_image_sha256",
        "frozen_deployment_sha256",
        "frozen_runtime_identity_sha256",
        "runtime_sha256",
    ):
        if observed[name] != runtime[name]:
            raise GateBExecutorError("live_runtime_drift")


def _validate_draft_shape(draft: Mapping[str, Any]) -> None:
    unknown = set(draft) - _DRAFT_FIELDS
    required = _DRAFT_FIELDS - {"owner_approval"}
    if unknown or not required.issubset(draft):
        raise GateBExecutorError("authorization_fields_invalid")
    if draft["schema_version"] != gate_a.AUTHORIZATION_SCHEMA_VERSION:
        raise GateBExecutorError("authorization_schema_invalid")
    _require_stable_id("authorization_template_id", draft["template_id"])
    _require_utc("authorization_generated_at", draft["generated_at_utc"])
    _require_bool("authorization_gate_b_allowed", draft["gate_b_allowed"])
    for name in ("business_claim_allowed",):
        if _require_bool(name, draft[name]):
            raise GateBExecutorError("business_claim_prohibited")
    if draft["model_quality_status"] != "not_measured":
        raise GateBExecutorError("model_quality_claim_prohibited")
    if draft["formal_quality_status"] != "incomplete":
        raise GateBExecutorError("formal_quality_claim_prohibited")
    if not isinstance(draft["permission_switches"], Mapping):
        raise GateBExecutorError("permission_switches_invalid")
    if not isinstance(draft["required_fields"], Mapping):
        raise GateBExecutorError("required_fields_invalid")
    required_fields = draft["required_fields"]
    if set(required_fields) != set(gate_a.GATE_B_REQUIRED_FIELDS):
        raise GateBExecutorError("required_fields_invalid")
    if set(draft["permission_switches"]) != set(gate_a.PERMISSION_FIELDS):
        raise GateBExecutorError("permission_switches_invalid")


def _authorization_projection(draft: Mapping[str, Any]) -> dict[str, Any]:
    _validate_draft_shape(draft)
    required = dict(draft["required_fields"])
    required["canonical_authorization_sha256"] = ""
    return {
        "schema_version": EXECUTOR_AUTHORIZATION_SCHEMA_VERSION,
        "template_id": draft["template_id"],
        "authorization_id": required["authorization_id"],
        "generated_at_utc": draft["generated_at_utc"],
        "required_fields": required,
        "permission_switches": dict(draft["permission_switches"]),
        "business_claim_allowed": draft["business_claim_allowed"],
        "model_quality_status": draft["model_quality_status"],
        "formal_quality_status": draft["formal_quality_status"],
    }


def canonical_authorization_sha256(draft: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json(_authorization_projection(draft)))


def _required_sha(required: Mapping[str, Any], name: str) -> str:
    return _require_sha256(name, required[name])


def _require_active_permissions(permissions: Mapping[str, Any]) -> None:
    if permissions["allow_real_provider_calls"] is not True:
        raise GateBExecutorError("authorization_permission_denied")
    repair_permissions = (
        permissions["allow_real_github_repair_branch_push"],
        permissions["allow_real_draft_repair_pr"],
    )
    if repair_permissions not in {(False, False), (True, True)}:
        raise GateBExecutorError("authorization_repair_permissions_inconsistent")
    for name in (
        "allow_comments_checks_labels_reviews",
        "allow_pilot_pr_ready",
        "allow_pilot_pr_merge",
        "allow_default_branch_mutation",
        "allow_auto_merge",
        "allow_agent_push_merge_master",
    ):
        if permissions[name] is not False:
            raise GateBExecutorError("authorization_prohibited_permission_enabled")


def _require_repair_permissions(permissions: Mapping[str, Any]) -> None:
    _require_active_permissions(permissions)
    if (
        permissions["allow_real_github_repair_branch_push"] is not True
        or permissions["allow_real_draft_repair_pr"] is not True
    ):
        raise GateBExecutorError("authorization_repair_permission_denied")


def _validate_required_values(required: Mapping[str, Any]) -> None:
    if set(required) != set(gate_a.GATE_B_REQUIRED_FIELDS):
        raise GateBExecutorError("authorization_required_fields_invalid")
    for name in gate_a.GATE_B_REQUIRED_FIELDS:
        if required[name] in (None, "", "PENDING", "PENDING_FREEZE"):
            raise GateBExecutorError("authorization_required_value_missing")
    _require_stable_id("authorization_id", required["authorization_id"])
    _require_sha256("canonical_authorization_sha256", required["canonical_authorization_sha256"])
    for name in (
        "frozen_source_tree_sha256",
        "frozen_executable_source_sha256",
        "frozen_runtime_image_sha256",
        "frozen_deployment_sha256",
        "frozen_runtime_identity_sha256",
        "participant_consent_receipt_sha256",
        "deterministic_selection_seed_sha256",
        "credential_fingerprint_sha256",
    ):
        _required_sha(required, name)
    participant_ids = required["participant_stable_ids"]
    if not isinstance(participant_ids, list) or not 3 <= len(participant_ids) <= 5:
        raise GateBExecutorError("participant_count_invalid")
    if len(set(participant_ids)) != len(participant_ids):
        raise GateBExecutorError("participant_ids_duplicate")
    for participant_id in participant_ids:
        _require_stable_id("participant_id", participant_id)
    roles = required["participant_roles"]
    if not isinstance(roles, Mapping) or set(roles) != set(participant_ids):
        raise GateBExecutorError("participant_roles_invalid")
    for role in roles.values():
        if role not in {"maintainer", "org_admin"}:
            raise GateBExecutorError("participant_role_invalid")
    _require_int("selected_pr_count", required["selected_pr_count"], minimum=20)
    if required["selected_pr_count"] > 30:
        raise GateBExecutorError("selected_pr_count_invalid")
    for name in (
        "max_repair_findings_per_pr",
        "max_repair_jobs",
        "max_real_branches",
        "max_real_commits",
        "max_real_pushes",
        "max_real_draft_repair_prs",
        "github_app_installation_id",
        "max_logical_calls",
        "max_http_attempts",
        "max_input_tokens",
        "max_output_tokens",
        "max_cached_tokens",
        "max_micro_cny",
        "max_wall_clock_seconds",
        "raw_content_retention_days",
        "metadata_retention_days",
        "feedback_retention_days",
        "human_approval_sla_seconds",
    ):
        _require_int(name, required[name])
    if any(required[name] != 1 for name in ("max_repair_jobs", "max_real_branches", "max_real_commits", "max_real_pushes", "max_real_draft_repair_prs")):
        raise GateBExecutorError("real_write_ceiling_invalid")
    if required["max_repair_findings_per_pr"] != 1:
        raise GateBExecutorError("repair_finding_ceiling_invalid")
    if required["raw_content_retention_days"] != 0:
        raise GateBExecutorError("raw_retention_not_zero")
    _require_stable_id("organization_id", required["organization_id"])
    _require_stable_id("incident_owner", required["incident_owner"])
    _require_stable_id("selection_rule", required["deterministic_selection_rule"])
    if (
        required["deterministic_selection_rule"]
        != "sha256_rank_lowest_eligible_pr_ids_no_replacement_after_failure"
    ):
        raise GateBExecutorError("selection_rule_invalid")
    _require_stable_id("data_classification", required["data_classification"])
    if required["data_classification"] != "restricted_source_code":
        raise GateBExecutorError("data_classification_invalid")
    _require_stable_id("provider_sendable_code_scope", required["provider_sendable_code_scope"])
    if (
        required["provider_sendable_code_scope"]
        != "selected_authorized_pr_diff_and_minimal_context_after_secret_scan"
    ):
        raise GateBExecutorError("provider_sendable_code_scope_invalid")
    _require_stable_id("credential_revoke_procedure", required["credential_revoke_procedure"])
    _require_utc("selection_window_start", required["pr_selection_window_start_utc"])
    _require_utc("selection_window_end", required["pr_selection_window_end_utc"])
    if _require_utc("selection_window_start", required["pr_selection_window_start_utc"]) >= _require_utc("selection_window_end", required["pr_selection_window_end_utc"]):
        raise GateBExecutorError("selection_window_invalid")
    if not isinstance(required["repository_allowlist"], Mapping):
        raise GateBExecutorError("repository_allowlist_invalid")
    repository_ids = required["repository_allowlist"].get("repository_ids")
    if not isinstance(repository_ids, list) or len(repository_ids) != 1:
        raise GateBExecutorError("repository_allowlist_invalid")
    _require_stable_id("repository_id", repository_ids[0])
    _require_sha256(
        "repository_authorization_sha256",
        required["repository_allowlist"].get("repository_authorization_sha256"),
    )
    base_rule = required["allowed_base_branch_rule"]
    if not isinstance(base_rule, Mapping):
        raise GateBExecutorError("base_branch_rule_invalid")
    _require_exact_fields(
        "base_branch_rule",
        base_rule,
        frozenset({"base_branch", "base_sha_rule", "protected_branch_mutation"}),
    )
    _require_branch("base_branch", base_rule["base_branch"])
    if (
        base_rule["base_sha_rule"] != "read_and_pin_at_selection"
        or base_rule["protected_branch_mutation"] is not False
    ):
        raise GateBExecutorError("base_branch_rule_invalid")
    scopes = required["github_repository_scopes"]
    if not isinstance(scopes, Mapping) or dict(scopes) != {
        "contents": "write",
        "metadata": "read",
        "pull_requests": "write",
    }:
        raise GateBExecutorError("github_repository_scopes_invalid")
    if not isinstance(required["provider_endpoint_allowlist"], list) or required["provider_endpoint_allowlist"] != [
        "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    ]:
        raise GateBExecutorError("provider_endpoint_allowlist_invalid")
    model = required["provider_model_snapshot"]
    if not isinstance(model, Mapping) or model != {
        "provider": "zhipu",
        "model": "glm-5.2",
        "api_family": "openai_compatible",
    }:
        raise GateBExecutorError("provider_model_invalid")
    if required["credential_delivery_mode"] != "local_secret_store_to_ephemeral_process_environment":
        raise GateBExecutorError("credential_delivery_mode_invalid")
    kill_switch = required["kill_switch"]
    if not isinstance(kill_switch, Mapping):
        raise GateBExecutorError("kill_switch_configuration_invalid")
    _require_exact_fields("kill_switch", kill_switch, frozenset({"activation", "owner"}))
    if (
        kill_switch["owner"] != required["incident_owner"]
        or kill_switch["activation"]
        != "stop_new_jobs_revoke_or_isolate_credentials_quarantine_unresolved"
    ):
        raise GateBExecutorError("kill_switch_configuration_invalid")
    deletion = required["deletion_owner_process"]
    if not isinstance(deletion, Mapping):
        raise GateBExecutorError("deletion_owner_process_invalid")
    _require_exact_fields("deletion_owner_process", deletion, frozenset({"owner", "process"}))
    if (
        deletion["owner"] != required["incident_owner"]
        or deletion["process"]
        != "purge_raw_content_at_retention_deadline_and_record_hash_only_receipt"
    ):
        raise GateBExecutorError("deletion_owner_process_invalid")
    business = required["business_success_thresholds"]
    if not isinstance(business, Mapping):
        raise GateBExecutorError("business_thresholds_invalid")
    _require_exact_fields(
        "business_success_thresholds",
        business,
        frozenset(
            {
                "adoption_rate_permille",
                "business_claim_requires_owner_signoff",
                "feedback_coverage_permille",
                "headline_completion_permille",
            }
        ),
    )
    for name in ("adoption_rate_permille", "feedback_coverage_permille", "headline_completion_permille"):
        if _require_int(f"business_{name}", business[name]) > 1000:
            raise GateBExecutorError("business_thresholds_invalid")
    if business["business_claim_requires_owner_signoff"] is not True:
        raise GateBExecutorError("business_thresholds_invalid")
    cost = required["cost_stop_thresholds"]
    if not isinstance(cost, Mapping):
        raise GateBExecutorError("cost_thresholds_invalid")
    _require_exact_fields(
        "cost_stop_thresholds",
        cost,
        frozenset({"max_http_attempts", "max_logical_calls", "max_micro_cny"}),
    )
    for name in ("max_http_attempts", "max_logical_calls", "max_micro_cny"):
        if cost[name] != required[name]:
            raise GateBExecutorError("cost_thresholds_invalid")
    safety = required["safety_stop_thresholds"]
    if not isinstance(safety, Mapping):
        raise GateBExecutorError("safety_thresholds_invalid")
    _require_exact_fields(
        "safety_stop_thresholds",
        safety,
        frozenset(
            {
                "max_duplicate_external_writes",
                "max_protected_branch_writes",
                "max_unauthorized_operations",
                "stop_on_credential_revoke_or_expiry",
                "stop_on_provider_text_only_response",
                "stop_on_publisher_ambiguous_result",
            }
        ),
    )
    if (
        safety["max_duplicate_external_writes"] != 0
        or safety["max_protected_branch_writes"] != 0
        or safety["max_unauthorized_operations"] != 0
        or safety["stop_on_credential_revoke_or_expiry"] is not True
        or safety["stop_on_provider_text_only_response"] is not True
        or safety["stop_on_publisher_ambiguous_result"] is not True
    ):
        raise GateBExecutorError("safety_thresholds_invalid")


def _participant_manifest_hash(participants: Mapping[str, Any]) -> str:
    if "manifest_sha256" not in participants:
        raise GateBExecutorError("participant_manifest_invalid")
    expected = _require_sha256("participant_manifest_sha256", participants["manifest_sha256"])
    if _self_hash(participants, "manifest_sha256") != expected:
        raise GateBExecutorError("participant_manifest_hash_mismatch")
    return expected


def _validate_participants(required: Mapping[str, Any], participants: Mapping[str, Any]) -> None:
    if participants.get("synthetic") is not False:
        raise GateBExecutorError("participant_manifest_synthetic")
    if participants.get("phase_id") != "phase11d-gate-b-human-pilot-v1":
        raise GateBExecutorError("participant_phase_invalid")
    _participant_manifest_hash(participants)
    rows = participants.get("participants")
    if not isinstance(rows, list):
        raise GateBExecutorError("participant_manifest_invalid")
    wanted = set(required["participant_stable_ids"])
    if len(rows) != len(wanted):
        raise GateBExecutorError("participant_count_invalid")
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise GateBExecutorError("participant_manifest_invalid")
        participant_id = _require_stable_id("participant_id", row.get("participant_id"))
        if participant_id in by_id:
            raise GateBExecutorError("participant_ids_duplicate")
        by_id[participant_id] = row
    if set(by_id) != wanted:
        raise GateBExecutorError("participant_manifest_mismatch")
    repository_id = required["repository_allowlist"]["repository_ids"][0]
    for participant_id, role in required["participant_roles"].items():
        row = by_id[participant_id]
        if row.get("confirmed_real") is not True or row.get("role") != role:
            raise GateBExecutorError("participant_manifest_mismatch")
        if repository_id not in row.get("repository_ids", []):
            raise GateBExecutorError("participant_repository_mismatch")
        if row.get("withdrawal_acknowledged") is not True:
            raise GateBExecutorError("participant_consent_invalid")
        expires = _require_utc("participant_consent_expires", row.get("consent_expires_at"))
        if expires < _require_utc("selection_window_end", required["pr_selection_window_end_utc"]):
            raise GateBExecutorError("participant_consent_expired")
    if required["participant_consent_receipt_sha256"] != participants["manifest_sha256"]:
        raise GateBExecutorError("participant_consent_hash_mismatch")


def _validate_repository_authorization(required: Mapping[str, Any], repository: Mapping[str, Any]) -> None:
    if repository.get("synthetic") is not False:
        raise GateBExecutorError("repository_authorization_synthetic")
    if repository.get("phase_id") != "phase11d-gate-b-human-pilot-v1":
        raise GateBExecutorError("repository_phase_invalid")
    if _self_hash(repository, "manifest_sha256") != _require_sha256(
        "repository_manifest_sha256", repository.get("manifest_sha256")
    ):
        raise GateBExecutorError("repository_manifest_hash_mismatch")
    rows = repository.get("repositories")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise GateBExecutorError("repository_authorization_invalid")
    row = rows[0]
    expected_id = required["repository_allowlist"]["repository_ids"][0]
    expected_hash = required["repository_allowlist"]["repository_authorization_sha256"]
    if row.get("repository_id") != expected_id or row.get("repository_sha256") != expected_hash:
        raise GateBExecutorError("repository_allowlist_mismatch")
    if row.get("real_github_api_authorized") is not True or row.get("publication_authorized") is not True:
        raise GateBExecutorError("repository_write_not_authorized")
    if row.get("raw_diff_read_authorized") is not True or row.get("publish_mode") != "publish":
        raise GateBExecutorError("repository_read_not_authorized")
    if _require_utc("repository_authorization_expires", row.get("authorization_expires_at")) < _require_utc(
        "selection_window_end", required["pr_selection_window_end_utc"]
    ):
        raise GateBExecutorError("repository_authorization_expired")


def _validate_descriptor(required: Mapping[str, Any], descriptor: Mapping[str, Any]) -> None:
    try:
        gate_a.validate_credential_descriptor(descriptor)
    except gate_a.Phase11DError as exc:
        raise GateBExecutorError("credential_descriptor_invalid") from exc
    if descriptor["authorization_id"] != required["authorization_id"]:
        raise GateBExecutorError("credential_authorization_mismatch")
    if descriptor["github_app_installation_id"] != required["github_app_installation_id"]:
        raise GateBExecutorError("credential_installation_mismatch")
    provider = required["provider_model_snapshot"]
    if descriptor["provider_id"] != provider["provider"] or descriptor["provider_model_snapshot"] != provider["model"]:
        raise GateBExecutorError("credential_provider_mismatch")
    if descriptor["credential_descriptor_sha256"] != required["credential_fingerprint_sha256"]:
        raise GateBExecutorError("credential_descriptor_hash_mismatch")


def _apply_runtime_hashes(draft: Mapping[str, Any], runtime: Mapping[str, Any]) -> dict[str, Any]:
    validate_executor_runtime(runtime)
    result = copy.deepcopy(dict(draft))
    required = result["required_fields"]
    assert isinstance(required, dict)
    if runtime["authorization_id"] != required["authorization_id"]:
        raise GateBExecutorError("runtime_authorization_mismatch")
    for name in (
        "frozen_source_tree_sha256",
        "frozen_executable_source_sha256",
        "frozen_runtime_image_sha256",
        "frozen_deployment_sha256",
        "frozen_runtime_identity_sha256",
    ):
        required[name] = runtime[name]
    return result


def build_exact_approval_text(draft: Mapping[str, Any]) -> str:
    """Create the exact owner text after all non-approval inputs are frozen."""
    projection = _authorization_projection(draft)
    required = projection["required_fields"]
    permissions = projection["permission_switches"]
    canonical_sha = canonical_authorization_sha256(draft)
    lines = (
        EXACT_APPROVAL_PREFIX,
        f"authorization_id={required['authorization_id']}",
        f"canonical_authorization_sha256={canonical_sha}",
        f"frozen_source_tree_sha256={required['frozen_source_tree_sha256']}",
        f"frozen_executable_source_sha256={required['frozen_executable_source_sha256']}",
        f"frozen_runtime_image_sha256={required['frozen_runtime_image_sha256']}",
        f"frozen_deployment_sha256={required['frozen_deployment_sha256']}",
        f"frozen_runtime_identity_sha256={required['frozen_runtime_identity_sha256']}",
        f"repository_id={required['repository_allowlist']['repository_ids'][0]}",
        f"selected_pr_count={required['selected_pr_count']}",
        f"max_logical_calls={required['max_logical_calls']}",
        f"max_http_attempts={required['max_http_attempts']}",
        f"max_micro_cny={required['max_micro_cny']}",
        f"max_wall_clock_seconds={required['max_wall_clock_seconds']}",
        f"allow_real_provider_calls={str(permissions['allow_real_provider_calls']).lower()}",
        "allow_real_github_repair_branch_push="
        f"{str(permissions['allow_real_github_repair_branch_push']).lower()}",
        "allow_real_draft_repair_pr="
        f"{str(permissions['allow_real_draft_repair_pr']).lower()}",
        "allow_comments_checks_labels_reviews=false",
        "allow_pilot_pr_ready=false",
        "allow_pilot_pr_merge=false",
        "allow_default_branch_mutation=false",
        "allow_auto_merge=false",
        "allow_agent_push_merge_master=false",
        "I approve exactly this Phase 11D Gate B authorization and no broader operation.",
    )
    return "\n".join(lines)


def freeze_authorization(
    *,
    draft: Mapping[str, Any],
    participants: Mapping[str, Any],
    repository: Mapping[str, Any],
    credential_descriptor: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze all known inputs but leave the real gate closed for owner approval."""
    _validate_draft_shape(draft)
    frozen = _apply_runtime_hashes(draft, runtime)
    required = frozen["required_fields"]
    assert isinstance(required, dict)
    required["canonical_authorization_sha256"] = canonical_authorization_sha256(frozen)
    frozen["exact_approval_text"] = build_exact_approval_text(frozen)
    frozen["owner_approval"] = {
        "actor_id": "PENDING",
        "approved_at_utc": "PENDING",
        "binding_sha256": "PENDING",
        "decision": "not_approved",
        "exact_approval_text_sha256": sha256_text(frozen["exact_approval_text"]),
    }
    frozen["gate_b_allowed"] = False
    frozen["template_status"] = "awaiting_owner_exact_approval"
    _validate_required_values(required)
    _require_active_permissions(frozen["permission_switches"])
    _validate_participants(required, participants)
    _validate_repository_authorization(required, repository)
    _validate_descriptor(required, credential_descriptor)
    return frozen


def _owner_approval_binding(authorization: Mapping[str, Any], actor_id: str) -> str:
    required = authorization["required_fields"]
    assert isinstance(required, Mapping)
    return sha256_bytes(
        canonical_json(
            {
                "authorization_id": required["authorization_id"],
                "canonical_authorization_sha256": required["canonical_authorization_sha256"],
                "actor_id": actor_id,
                "exact_approval_text_sha256": sha256_text(authorization["exact_approval_text"]),
            }
        )
    )


def approve_authorization(
    *,
    frozen: Mapping[str, Any],
    participants: Mapping[str, Any],
    actor_id: str,
    approved_at_utc: str,
    exact_approval_text: str,
) -> dict[str, Any]:
    """Apply an owner-provided exact approval without touching credentials or network."""
    _validate_draft_shape(frozen)
    if frozen.get("template_status") != "awaiting_owner_exact_approval":
        raise GateBExecutorError("authorization_not_awaiting_approval")
    required = frozen["required_fields"]
    assert isinstance(required, Mapping)
    _validate_required_values(required)
    _require_active_permissions(frozen["permission_switches"])
    _validate_participants(required, participants)
    expected_text = build_exact_approval_text(frozen)
    if exact_approval_text != expected_text or frozen["exact_approval_text"] != expected_text:
        raise GateBExecutorError("exact_approval_text_mismatch")
    _require_stable_id("owner_actor_id", actor_id)
    actor_rows = {
        row.get("participant_id"): row
        for row in participants.get("participants", [])
        if isinstance(row, Mapping)
    }
    actor = actor_rows.get(actor_id)
    if not isinstance(actor, Mapping) or actor.get("role") not in {"maintainer", "org_admin"}:
        raise GateBExecutorError("owner_role_denied")
    approved_at = _require_utc("owner_approved_at", approved_at_utc)
    start = _require_utc("selection_window_start", required["pr_selection_window_start_utc"])
    end = _require_utc("selection_window_end", required["pr_selection_window_end_utc"])
    if not start <= approved_at <= end:
        raise GateBExecutorError("owner_approval_window_invalid")
    approved = copy.deepcopy(dict(frozen))
    approved["owner_approval"] = {
        "actor_id": actor_id,
        "approved_at_utc": approved_at_utc,
        "binding_sha256": _owner_approval_binding(approved, actor_id),
        "decision": "approved",
        "exact_approval_text_sha256": sha256_text(expected_text),
    }
    approved["gate_b_allowed"] = True
    approved["template_status"] = "approved_ready_for_execution"
    return approved


def validate_execution_authorization(
    *,
    authorization: Mapping[str, Any],
    participants: Mapping[str, Any],
    repository: Mapping[str, Any],
    credential_descriptor: Mapping[str, Any],
    runtime: Mapping[str, Any],
    now_utc: str | None = None,
) -> AuthorizationStatus:
    """Validate all non-secret gates before a future transport can access a credential."""
    blockers: list[str] = []
    try:
        _validate_draft_shape(authorization)
        required = authorization["required_fields"]
        assert isinstance(required, Mapping)
        _validate_required_values(required)
        _require_active_permissions(authorization["permission_switches"])
        validate_executor_runtime(runtime)
        if runtime["authorization_id"] != required["authorization_id"]:
            raise GateBExecutorError("runtime_authorization_mismatch")
        for name in (
            "frozen_source_tree_sha256",
            "frozen_executable_source_sha256",
            "frozen_runtime_image_sha256",
            "frozen_deployment_sha256",
            "frozen_runtime_identity_sha256",
        ):
            if required[name] != runtime[name]:
                raise GateBExecutorError("runtime_hash_mismatch")
        canonical_sha = canonical_authorization_sha256(authorization)
        if required["canonical_authorization_sha256"] != canonical_sha:
            raise GateBExecutorError("canonical_authorization_hash_mismatch")
        expected_text = build_exact_approval_text(authorization)
        if authorization["exact_approval_text"] != expected_text:
            raise GateBExecutorError("exact_approval_text_mismatch")
        owner_approval = authorization.get("owner_approval")
        if not isinstance(owner_approval, Mapping):
            raise GateBExecutorError("owner_approval_missing")
        _require_exact_fields("owner_approval", owner_approval, _OWNER_APPROVAL_FIELDS)
        if owner_approval["decision"] != "approved":
            raise GateBExecutorError("owner_approval_missing")
        actor_id = _require_stable_id("owner_actor_id", owner_approval["actor_id"])
        if owner_approval["exact_approval_text_sha256"] != sha256_text(expected_text):
            raise GateBExecutorError("owner_approval_text_hash_mismatch")
        if owner_approval["binding_sha256"] != _owner_approval_binding(authorization, actor_id):
            raise GateBExecutorError("owner_approval_binding_mismatch")
        approved_at = _require_utc("owner_approved_at", owner_approval["approved_at_utc"])
        _validate_participants(required, participants)
        actor_rows = {
            row.get("participant_id"): row
            for row in participants.get("participants", [])
            if isinstance(row, Mapping)
        }
        actor = actor_rows.get(actor_id)
        if (
            not isinstance(actor, Mapping)
            or actor.get("confirmed_real") is not True
            or actor.get("role") not in {"maintainer", "org_admin"}
        ):
            raise GateBExecutorError("owner_role_denied")
        _validate_repository_authorization(required, repository)
        _validate_descriptor(required, credential_descriptor)
        now = _require_utc("now", now_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        start = _require_utc("selection_window_start", required["pr_selection_window_start_utc"])
        end = _require_utc("selection_window_end", required["pr_selection_window_end_utc"])
        if not start <= approved_at <= end:
            raise GateBExecutorError("owner_approval_window_invalid")
        if not start <= now <= end:
            raise GateBExecutorError("authorization_window_inactive")
        kill_switch = required["kill_switch"]
        if not isinstance(kill_switch, Mapping) or kill_switch.get("owner") != required["incident_owner"]:
            raise GateBExecutorError("kill_switch_configuration_invalid")
        if authorization["gate_b_allowed"] is not True or authorization["template_status"] != "approved_ready_for_execution":
            raise GateBExecutorError("gate_b_closed")
        return AuthorizationStatus(
            authorization_id=required["authorization_id"],
            canonical_authorization_sha256=canonical_sha,
            gate_b_allowed=True,
            execution_capability="authorization_gated_real_executor",
            blockers=(),
        )
    except GateBExecutorError as exc:
        authorization_id = "unknown"
        canonical_sha = ""
        required = authorization.get("required_fields") if isinstance(authorization, Mapping) else None
        if isinstance(required, Mapping):
            candidate_id = required.get("authorization_id")
            if isinstance(candidate_id, str):
                authorization_id = candidate_id
            candidate_hash = required.get("canonical_authorization_sha256")
            if isinstance(candidate_hash, str) and SHA256_RE.fullmatch(candidate_hash):
                canonical_sha = candidate_hash
        blockers.append(str(exc))
        return AuthorizationStatus(
            authorization_id=authorization_id,
            canonical_authorization_sha256=canonical_sha,
            gate_b_allowed=False,
            execution_capability="closed",
            blockers=tuple(blockers),
        )


def require_active_execution_authorization(
    *,
    authorization: Mapping[str, Any],
    participants: Mapping[str, Any],
    repository: Mapping[str, Any],
    credential_descriptor: Mapping[str, Any],
    runtime: Mapping[str, Any],
    source_root: Path | None = None,
    now_utc: str | None = None,
) -> AuthorizationStatus:
    """Raise before any credential or transport access when the full gate is not open."""
    status = validate_execution_authorization(
        authorization=authorization,
        participants=participants,
        repository=repository,
        credential_descriptor=credential_descriptor,
        runtime=runtime,
        now_utc=now_utc,
    )
    if not status.gate_b_allowed:
        blocker = status.blockers[0] if status.blockers else "gate_b_closed"
        raise GateBExecutorError(blocker)
    if source_root is not None:
        validate_live_executor_runtime(source_root=source_root, runtime=runtime)
    return status


def verify_credential_fingerprints(
    *,
    authorization: Mapping[str, Any],
    participants: Mapping[str, Any],
    repository: Mapping[str, Any],
    credential_descriptor: Mapping[str, Any],
    runtime: Mapping[str, Any],
    source_root: Path,
    github_app_private_key_file: Path,
    provider_key_environment: str,
    now_utc: str | None = None,
) -> dict[str, bool]:
    """Compare explicit credentials in memory only after all static gates passed."""
    require_active_execution_authorization(
        authorization=authorization,
        participants=participants,
        repository=repository,
        credential_descriptor=credential_descriptor,
        runtime=runtime,
        source_root=source_root,
        now_utc=now_utc,
    )
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    _validate_descriptor(required, credential_descriptor)
    private_key = _read_private_key_file(
        github_app_private_key_file,
        expected_sha256=credential_descriptor["github_app_private_key_fingerprint_sha256"],
    )
    del private_key
    try:
        provider_key = os.environ[provider_key_environment]
    except KeyError as exc:
        raise GateBExecutorError("provider_key_unavailable") from exc
    if not provider_key or len(provider_key.encode("utf-8")) > 4_096:
        raise GateBExecutorError("provider_key_invalid")
    github_match = True
    provider_match = sha256_text(provider_key) == credential_descriptor[
        "provider_api_key_fingerprint_sha256"
    ]
    if not github_match or not provider_match:
        raise GateBExecutorError("credential_fingerprint_mismatch")
    return {
        "github_app_private_key_fingerprint_matched": True,
        "provider_api_key_fingerprint_matched": True,
    }


REAL_REPAIR_SCHEMA_VERSION = "crag.phase11d.gate-b-repair/v1alpha1"
REAL_DRAFT_PR_RECEIPT_SCHEMA_VERSION = "crag.phase11d.gate-b-draft-pr-receipt/v1alpha1"
REPAIR_APPROVAL_KINDS = frozenset({"write", "draft_pr"})
PUBLISH_STATES = frozenset(
    {
        "intent_recorded",
        "branch_push_observed",
        "draft_pr_observed",
        "receipt_reconciled",
        "quarantined",
    }
)


def _hash_ephemeral_text(name: str, value: Any, *, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value:
        raise GateBExecutorError(f"{name}_invalid")
    encoded = value.encode("utf-8")
    if len(encoded) > maximum_bytes or _contains_secret_like_content(value):
        raise GateBExecutorError(f"{name}_redaction_failure")
    return sha256_bytes(encoded)


def _resolve_human_actor(participants: Mapping[str, Any], actor_id: Any) -> tuple[str, str]:
    actor = _require_stable_id("human_actor_id", actor_id)
    rows = participants.get("participants")
    if not isinstance(rows, list):
        raise GateBExecutorError("participant_manifest_invalid")
    for row in rows:
        if isinstance(row, Mapping) and row.get("participant_id") == actor:
            if row.get("confirmed_real") is not True:
                break
            role = row.get("role")
            if role in {"maintainer", "org_admin"}:
                return actor, role
            break
    raise GateBExecutorError("human_actor_role_denied")


@dataclass(frozen=True)
class FindingSelection:
    """Hash-only record of the human-selected finding that starts one repair job."""

    selection_id: str
    pr_id: str
    finding_id: str
    review_response_sha256: str
    selector_id: str
    selected_at_utc: str
    actor_method: str = "human"
    human_attested: bool = True

    def __post_init__(self) -> None:
        _require_stable_id("finding_selection_id", self.selection_id)
        _require_stable_id("finding_selection_pr_id", self.pr_id)
        _require_stable_id("finding_selection_finding_id", self.finding_id)
        _require_sha256("finding_selection_review_response", self.review_response_sha256)
        _require_stable_id("finding_selection_selector", self.selector_id)
        _require_utc("finding_selection_time", self.selected_at_utc)
        if self.actor_method != "human" or self.human_attested is not True:
            raise GateBExecutorError("human_finding_selection_invalid")

    @property
    def selection_sha256(self) -> str:
        return sha256_bytes(
            canonical_json(
                {
                    "selection_id": self.selection_id,
                    "pr_id": self.pr_id,
                    "finding_id": self.finding_id,
                    "review_response_sha256": self.review_response_sha256,
                    "selector_id": self.selector_id,
                    "selected_at_utc": self.selected_at_utc,
                    "actor_method": self.actor_method,
                    "human_attested": self.human_attested,
                }
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REAL_REPAIR_SCHEMA_VERSION,
            "selection_id": self.selection_id,
            "pr_id": self.pr_id,
            "finding_id": self.finding_id,
            "review_response_sha256": self.review_response_sha256,
            "selector_id": self.selector_id,
            "selected_at_utc": self.selected_at_utc,
            "actor_method": self.actor_method,
            "human_attested": self.human_attested,
            "selection_sha256": self.selection_sha256,
        }


def select_finding_for_repair(
    *,
    participants: Mapping[str, Any],
    candidate: PullRequestCandidate,
    review: ReviewOutcome,
    finding_id: str,
    selection_id: str,
    selector_id: str,
    selected_at_utc: str,
) -> FindingSelection:
    """Accept only an explicit human selection of an immutable review finding."""
    _resolve_human_actor(participants, selector_id)
    if review.status != "completed" or review.terminal_category != "completed":
        raise GateBExecutorError("finding_selection_review_not_completed")
    finding = _require_stable_id("finding_selection_finding_id", finding_id)
    if finding not in review.feedback_eligible_finding_ids:
        raise GateBExecutorError("finding_selection_not_feedback_eligible")
    if review.pr_id != candidate.pr_id:
        raise GateBExecutorError("finding_selection_pr_mismatch")
    return FindingSelection(
        selection_id=selection_id,
        pr_id=candidate.pr_id,
        finding_id=finding,
        review_response_sha256=review.response_sha256,
        selector_id=selector_id,
        selected_at_utc=selected_at_utc,
    )


@dataclass(frozen=True)
class RepairIntent:
    """Immutable, hash-only Repair request before any sandbox mutation."""

    repair_job_id: str
    pr_id: str
    finding_id: str
    selection_sha256: str
    base_branch: str
    base_sha: str
    head_sha: str
    plan_sha256: str
    requested_by: str
    requested_at_utc: str
    head_branch: str

    def __post_init__(self) -> None:
        _require_stable_id("repair_job_id", self.repair_job_id)
        _require_stable_id("repair_pr_id", self.pr_id)
        _require_stable_id("repair_finding_id", self.finding_id)
        _require_sha256("repair_selection_sha256", self.selection_sha256)
        _require_branch("repair_base_branch", self.base_branch)
        _require_git_sha("repair_base_sha", self.base_sha)
        _require_git_sha("repair_head_sha", self.head_sha)
        _require_sha256("repair_plan_sha256", self.plan_sha256)
        _require_stable_id("repair_requested_by", self.requested_by)
        _require_utc("repair_requested_at", self.requested_at_utc)
        _require_branch("repair_head_branch", self.head_branch)
        if not self.head_branch.startswith("crag/phase11d/") or self.head_branch == self.base_branch:
            raise GateBExecutorError("repair_branch_boundary_invalid")

    @property
    def write_binding_sha256(self) -> str:
        return _repair_binding(
            "write",
            {
                "repair_job_id": self.repair_job_id,
                "pr_id": self.pr_id,
                "finding_id": self.finding_id,
                "selection_sha256": self.selection_sha256,
                "base_branch": self.base_branch,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
                "plan_sha256": self.plan_sha256,
                "head_branch": self.head_branch,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REAL_REPAIR_SCHEMA_VERSION,
            "repair_job_id": self.repair_job_id,
            "pr_id": self.pr_id,
            "finding_id": self.finding_id,
            "selection_sha256": self.selection_sha256,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "plan_sha256": self.plan_sha256,
            "requested_by": self.requested_by,
            "requested_at_utc": self.requested_at_utc,
            "head_branch": self.head_branch,
            "write_binding_sha256": self.write_binding_sha256,
        }


def _repair_binding(kind: str, value: Mapping[str, Any]) -> str:
    if kind not in REPAIR_APPROVAL_KINDS:
        raise GateBExecutorError("repair_approval_kind_invalid")
    return sha256_bytes(canonical_json({"kind": kind, "binding": dict(value)}))


def create_repair_intent(
    *,
    participants: Mapping[str, Any],
    selection: FindingSelection,
    candidate: PullRequestCandidate,
    plan_text: str,
    repair_job_id: str,
    requested_by: str,
    requested_at_utc: str,
    base_branch: str,
) -> RepairIntent:
    """Create a repair intent without storing the plan or source content."""
    _resolve_human_actor(participants, requested_by)
    if selection.pr_id != candidate.pr_id or selection.selector_id == "":
        raise GateBExecutorError("repair_selection_mismatch")
    if selection.finding_id == "":
        raise GateBExecutorError("repair_finding_invalid")
    if base_branch != candidate.base_branch:
        raise GateBExecutorError("repair_base_branch_mismatch")
    branch = f"crag/phase11d/{_require_stable_id('repair_job_id', repair_job_id)}"
    return RepairIntent(
        repair_job_id=repair_job_id,
        pr_id=candidate.pr_id,
        finding_id=selection.finding_id,
        selection_sha256=selection.selection_sha256,
        base_branch=base_branch,
        base_sha=candidate.base_sha,
        head_sha=candidate.head_sha,
        plan_sha256=_hash_ephemeral_text("repair_plan", plan_text, maximum_bytes=64_000),
        requested_by=requested_by,
        requested_at_utc=requested_at_utc,
        head_branch=branch,
    )


@dataclass(frozen=True)
class HumanApproval:
    approval_id: str
    kind: str
    decision: str
    actor_id: str
    actor_role: str
    actor_method: str
    binding_sha256: str
    approved_at_utc: str
    consumed: bool

    def __post_init__(self) -> None:
        _require_stable_id("repair_approval_id", self.approval_id)
        if self.kind not in REPAIR_APPROVAL_KINDS:
            raise GateBExecutorError("repair_approval_kind_invalid")
        if self.decision not in {"approved", "declined"}:
            raise GateBExecutorError("repair_approval_decision_invalid")
        _require_stable_id("repair_approval_actor", self.actor_id)
        if self.actor_role not in {"maintainer", "org_admin"} or self.actor_method != "human":
            raise GateBExecutorError("repair_approval_actor_denied")
        _require_sha256("repair_approval_binding", self.binding_sha256)
        _require_utc("repair_approval_time", self.approved_at_utc)
        if self.consumed is not True:
            raise GateBExecutorError("repair_approval_not_consumed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "kind": self.kind,
            "decision": self.decision,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "actor_method": self.actor_method,
            "binding_sha256": self.binding_sha256,
            "approved_at_utc": self.approved_at_utc,
            "consumed": self.consumed,
        }


class OneUseApprovalLedger:
    """Thread-safe in-memory approval gate; callers may persist only its sanitized rows."""

    def __init__(self, *, participants: Mapping[str, Any], sla_seconds: int) -> None:
        self._participants = participants
        self._sla_seconds = _require_int("human_approval_sla_seconds", sla_seconds, minimum=1)
        self._requests: dict[str, tuple[str, str, str, datetime]] = {}
        self._consumed: set[str] = set()
        self._lock = threading.Lock()

    def register(
        self,
        *,
        approval_id: str,
        kind: str,
        binding_sha256: str,
        requested_at_utc: str,
    ) -> None:
        _require_stable_id("repair_approval_id", approval_id)
        if kind not in REPAIR_APPROVAL_KINDS:
            raise GateBExecutorError("repair_approval_kind_invalid")
        _require_sha256("repair_approval_binding", binding_sha256)
        requested_at = _require_utc("repair_approval_requested_at", requested_at_utc)
        expires_at = requested_at.timestamp() + self._sla_seconds
        with self._lock:
            if approval_id in self._requests or approval_id in self._consumed:
                raise GateBExecutorError("repair_approval_replay")
            self._requests[approval_id] = (
                kind,
                binding_sha256,
                requested_at_utc,
                datetime.fromtimestamp(expires_at, timezone.utc),
            )

    def decide(
        self,
        *,
        approval_id: str,
        actor_id: str,
        decision: str,
        approved_at_utc: str,
    ) -> HumanApproval:
        actor, role = _resolve_human_actor(self._participants, actor_id)
        if decision not in {"approved", "declined"}:
            raise GateBExecutorError("repair_approval_decision_invalid")
        approved_at = _require_utc("repair_approval_time", approved_at_utc)
        with self._lock:
            request = self._requests.get(approval_id)
            if request is None or approval_id in self._consumed:
                raise GateBExecutorError("repair_approval_replay")
            kind, binding, _requested_at, expires_at = request
            if not _require_utc("repair_approval_requested_at", _requested_at) <= approved_at <= expires_at:
                raise GateBExecutorError("repair_approval_expired")
            self._consumed.add(approval_id)
        return HumanApproval(
            approval_id=approval_id,
            kind=kind,
            decision=decision,
            actor_id=actor,
            actor_role=role,
            actor_method="human",
            binding_sha256=binding,
            approved_at_utc=approved_at_utc,
            consumed=True,
        )


@dataclass(frozen=True)
class SandboxPatchFile:
    path: str
    content: bytes
    mode: str = "100644"

    def __post_init__(self) -> None:
        _require_repository_path("sandbox_patch_path", self.path)
        if not isinstance(self.content, bytes) or not self.content or len(self.content) > 1_000_000:
            raise GateBExecutorError("sandbox_patch_content_invalid")
        if self.mode not in {"100644", "100755"}:
            raise GateBExecutorError("sandbox_patch_mode_invalid")

    @property
    def blob_sha(self) -> str:
        return hashlib.sha1(
            f"blob {len(self.content)}\0".encode("ascii") + self.content,
            usedforsecurity=False,
        ).hexdigest()


@dataclass(frozen=True)
class SandboxResult:
    repair_job_id: str
    worktree_receipt_sha256: str
    task_branch_sha256: str
    patch_sha256: str
    checkpoint_sha256: str
    test_sha256: str
    budget_sha256: str
    tests_passed: bool
    reflection_passed: bool
    exact_commit_sha: str
    expected_tree_sha: str
    patch_files: tuple[SandboxPatchFile, ...]
    network_mode: str = "none"
    docker: bool = True
    non_root: bool = True
    timeout_seconds: int = 900
    output_limit_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        _require_stable_id("sandbox_repair_job_id", self.repair_job_id)
        for name in (
            "worktree_receipt_sha256",
            "task_branch_sha256",
            "patch_sha256",
            "checkpoint_sha256",
            "test_sha256",
            "budget_sha256",
        ):
            _require_sha256(f"sandbox_{name}", getattr(self, name))
        _require_bool("sandbox_tests_passed", self.tests_passed)
        _require_bool("sandbox_reflection_passed", self.reflection_passed)
        if self.tests_passed and self.reflection_passed:
            _require_git_sha("sandbox_exact_commit_sha", self.exact_commit_sha)
            _require_git_sha("sandbox_expected_tree_sha", self.expected_tree_sha)
        if self.network_mode != "none" or self.docker is not True or self.non_root is not True:
            raise GateBExecutorError("sandbox_policy_invalid")
        _require_int("sandbox_timeout_seconds", self.timeout_seconds, minimum=1)
        _require_int("sandbox_output_limit_bytes", self.output_limit_bytes, minimum=1)
        if not self.patch_files or len(self.patch_files) > 64:
            raise GateBExecutorError("sandbox_patch_file_count_invalid")
        if len({item.path for item in self.patch_files}) != len(self.patch_files):
            raise GateBExecutorError("sandbox_patch_duplicate_path")
        if self.patch_manifest_sha256 != self.patch_sha256:
            raise GateBExecutorError("sandbox_patch_hash_mismatch")

    @property
    def patch_manifest_sha256(self) -> str:
        return sha256_bytes(
            canonical_json(
                [
                    {
                        "path": item.path,
                        "mode": item.mode,
                        "blob_sha": item.blob_sha,
                        "content_sha256": sha256_bytes(item.content),
                    }
                    for item in sorted(self.patch_files, key=lambda item: item.path)
                ]
            )
        )

    def receipt_projection(self) -> dict[str, Any]:
        return {
            "schema_version": REAL_REPAIR_SCHEMA_VERSION,
            "repair_job_id": self.repair_job_id,
            "worktree_receipt_sha256": self.worktree_receipt_sha256,
            "task_branch_sha256": self.task_branch_sha256,
            "patch_sha256": self.patch_sha256,
            "checkpoint_sha256": self.checkpoint_sha256,
            "test_sha256": self.test_sha256,
            "budget_sha256": self.budget_sha256,
            "tests_passed": self.tests_passed,
            "reflection_passed": self.reflection_passed,
            "exact_commit_sha": self.exact_commit_sha if self.tests_passed else "",
            "expected_tree_sha": self.expected_tree_sha if self.tests_passed else "",
            "network_mode": self.network_mode,
            "docker": self.docker,
            "non_root": self.non_root,
            "timeout_seconds": self.timeout_seconds,
            "output_limit_bytes": self.output_limit_bytes,
        }


def validate_sandbox_result(*, intent: RepairIntent, result: SandboxResult) -> None:
    if result.repair_job_id != intent.repair_job_id:
        raise GateBExecutorError("sandbox_repair_job_mismatch")
    if sha256_text(intent.head_branch) != result.task_branch_sha256:
        raise GateBExecutorError("sandbox_task_branch_mismatch")
    if not result.tests_passed:
        raise GateBExecutorError("sandbox_tests_failed")
    if not result.reflection_passed:
        raise GateBExecutorError("sandbox_reflection_failed")


@dataclass(frozen=True)
class DraftPublicationMaterial:
    """In-memory Git data prepared by the isolated sandbox before DRAFT_PR approval."""

    base_tree_sha: str
    expected_tree_sha: str
    exact_commit_sha: str
    commit_message: str
    commit_timestamp_utc: str
    patch_files: tuple[SandboxPatchFile, ...]

    def __post_init__(self) -> None:
        _require_git_sha("publication_base_tree_sha", self.base_tree_sha)
        _require_git_sha("publication_expected_tree_sha", self.expected_tree_sha)
        _require_git_sha("publication_exact_commit_sha", self.exact_commit_sha)
        _hash_ephemeral_text("publication_commit_message", self.commit_message, maximum_bytes=256)
        _require_utc("publication_commit_timestamp", self.commit_timestamp_utc)
        if not self.patch_files:
            raise GateBExecutorError("publication_patch_empty")

    @property
    def payload_sha256(self) -> str:
        return sha256_bytes(
            canonical_json(
                {
                    "base_tree_sha": self.base_tree_sha,
                    "expected_tree_sha": self.expected_tree_sha,
                    "exact_commit_sha": self.exact_commit_sha,
                    "commit_message_sha256": sha256_text(self.commit_message),
                    "commit_timestamp_utc": self.commit_timestamp_utc,
                    "patch": [
                        {
                            "path": item.path,
                            "mode": item.mode,
                            "blob_sha": item.blob_sha,
                            "content_sha256": sha256_bytes(item.content),
                        }
                        for item in sorted(self.patch_files, key=lambda item: item.path)
                    ],
                }
            )
        )


def build_draft_publication_material(
    *,
    intent: RepairIntent,
    sandbox: SandboxResult,
    base_tree_sha: str,
    commit_message: str,
    commit_timestamp_utc: str,
) -> DraftPublicationMaterial:
    validate_sandbox_result(intent=intent, result=sandbox)
    if sandbox.exact_commit_sha == "" or sandbox.expected_tree_sha == "":
        raise GateBExecutorError("publication_commit_missing")
    return DraftPublicationMaterial(
        base_tree_sha=base_tree_sha,
        expected_tree_sha=sandbox.expected_tree_sha,
        exact_commit_sha=sandbox.exact_commit_sha,
        commit_message=commit_message,
        commit_timestamp_utc=commit_timestamp_utc,
        patch_files=sandbox.patch_files,
    )


@dataclass(frozen=True)
class DraftPublication:
    """Sanitized publication description plus patch bytes retained only in memory."""

    authorization_id: str
    authorization_sha256: str
    repository_id: str
    repair_job_id: str
    pr_id: str
    base_branch: str
    base_sha: str
    source_head_sha: str
    head_branch: str
    base_tree_sha: str
    expected_tree_sha: str
    exact_commit_sha: str
    commit_message: str
    commit_timestamp_utc: str
    patch_files: tuple[SandboxPatchFile, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        _require_stable_id("publication_authorization_id", self.authorization_id)
        _require_sha256("publication_authorization_sha256", self.authorization_sha256)
        _require_stable_id("publication_repository_id", self.repository_id)
        _require_stable_id("publication_repair_job_id", self.repair_job_id)
        _require_stable_id("publication_pr_id", self.pr_id)
        _require_branch("publication_base_branch", self.base_branch)
        _require_git_sha("publication_base_sha", self.base_sha)
        _require_git_sha("publication_source_head_sha", self.source_head_sha)
        _require_branch("publication_head_branch", self.head_branch)
        if not self.head_branch.startswith("crag/phase11d/") or self.head_branch == self.base_branch:
            raise GateBExecutorError("publication_branch_boundary_invalid")
        _require_git_sha("publication_base_tree_sha", self.base_tree_sha)
        _require_git_sha("publication_expected_tree_sha", self.expected_tree_sha)
        _require_git_sha("publication_exact_commit_sha", self.exact_commit_sha)
        _hash_ephemeral_text("publication_commit_message", self.commit_message, maximum_bytes=256)
        _require_utc("publication_commit_timestamp", self.commit_timestamp_utc)
        _require_sha256("publication_payload_sha256", self.payload_sha256)
        if not self.patch_files:
            raise GateBExecutorError("publication_patch_empty")
        if self.payload_sha256 != self.computed_payload_sha256:
            raise GateBExecutorError("publication_payload_hash_mismatch")

    @property
    def title(self) -> str:
        return f"Phase 11D repair {self.repair_job_id}"

    @property
    def body_marker(self) -> str:
        return f"<!-- crag-phase11d:{sha256_text(self.repair_job_id)} -->"

    @property
    def body_marker_sha256(self) -> str:
        return sha256_text(self.body_marker)

    @property
    def computed_payload_sha256(self) -> str:
        material_payload_sha256 = sha256_bytes(
            canonical_json(
                {
                    "base_tree_sha": self.base_tree_sha,
                    "expected_tree_sha": self.expected_tree_sha,
                    "exact_commit_sha": self.exact_commit_sha,
                    "commit_message_sha256": sha256_text(self.commit_message),
                    "commit_timestamp_utc": self.commit_timestamp_utc,
                    "patch": [
                        {
                            "path": item.path,
                            "mode": item.mode,
                            "blob_sha": item.blob_sha,
                            "content_sha256": sha256_bytes(item.content),
                        }
                        for item in sorted(self.patch_files, key=lambda item: item.path)
                    ],
                }
            )
        )
        return sha256_bytes(
            canonical_json(
                {
                    "authorization_id": self.authorization_id,
                    "authorization_sha256": self.authorization_sha256,
                    "repository_id": self.repository_id,
                    "repair_job_id": self.repair_job_id,
                    "pr_id": self.pr_id,
                    "base_branch": self.base_branch,
                    "base_sha": self.base_sha,
                    "source_head_sha": self.source_head_sha,
                    "head_branch": self.head_branch,
                    "material_payload_sha256": material_payload_sha256,
                }
            )
        )


def build_draft_publication(
    *,
    authorization: Mapping[str, Any],
    intent: RepairIntent,
    material: DraftPublicationMaterial,
) -> DraftPublication:
    """Bind the exact sandbox commit before a DRAFT_PR approval is requested."""
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    _require_stable_id("publication_authorization_id", required.get("authorization_id"))
    canonical_sha = _require_sha256(
        "publication_authorization_sha256", required.get("canonical_authorization_sha256")
    )
    repository_allowlist = required.get("repository_allowlist")
    if not isinstance(repository_allowlist, Mapping):
        raise GateBExecutorError("repository_allowlist_invalid")
    repository_ids = repository_allowlist.get("repository_ids")
    if not isinstance(repository_ids, list) or len(repository_ids) != 1:
        raise GateBExecutorError("repository_allowlist_invalid")
    base_rule = required.get("allowed_base_branch_rule")
    if not isinstance(base_rule, Mapping) or base_rule.get("base_branch") != intent.base_branch:
        raise GateBExecutorError("publication_base_branch_mismatch")
    if intent.base_sha != _require_git_sha("publication_base_sha", intent.base_sha):
        raise GateBExecutorError("publication_base_sha_invalid")
    payload_sha = sha256_bytes(
        canonical_json(
            {
                "authorization_id": required["authorization_id"],
                "authorization_sha256": canonical_sha,
                "repository_id": repository_ids[0],
                "repair_job_id": intent.repair_job_id,
                "pr_id": intent.pr_id,
                "base_branch": intent.base_branch,
                "base_sha": intent.base_sha,
                "source_head_sha": intent.head_sha,
                "head_branch": intent.head_branch,
                "material_payload_sha256": material.payload_sha256,
            }
        )
    )
    return DraftPublication(
        authorization_id=required["authorization_id"],
        authorization_sha256=canonical_sha,
        repository_id=repository_ids[0],
        repair_job_id=intent.repair_job_id,
        pr_id=intent.pr_id,
        base_branch=intent.base_branch,
        base_sha=intent.base_sha,
        source_head_sha=intent.head_sha,
        head_branch=intent.head_branch,
        base_tree_sha=material.base_tree_sha,
        expected_tree_sha=material.expected_tree_sha,
        exact_commit_sha=material.exact_commit_sha,
        commit_message=material.commit_message,
        commit_timestamp_utc=material.commit_timestamp_utc,
        patch_files=material.patch_files,
        payload_sha256=payload_sha,
    )


LEGACY_JOURNAL_SCHEMA_VERSION = "crag.phase11d.gate-b-publication-journal/v1alpha1"
JOURNAL_SCHEMA_VERSION = "crag.phase11d.gate-b-publication-journal/v1alpha2"
_LEGACY_JOURNAL_FIELDS = frozenset(
    {
        "schema_version",
        "authorization_id",
        "authorization_sha256",
        "repository_id",
        "repair_job_id",
        "pr_id",
        "base_branch",
        "base_sha",
        "head_branch",
        "expected_tree_sha",
        "expected_commit_sha",
        "marker_sha256",
        "state",
        "remote_pr_number",
        "journal_sha256",
    }
)
_JOURNAL_FIELDS = _LEGACY_JOURNAL_FIELDS | {"source_head_sha"}


def _validate_journal(value: Mapping[str, Any]) -> None:
    schema_version = value.get("schema_version")
    if schema_version == LEGACY_JOURNAL_SCHEMA_VERSION:
        fields = _LEGACY_JOURNAL_FIELDS
    elif schema_version == JOURNAL_SCHEMA_VERSION:
        fields = _JOURNAL_FIELDS
    else:
        raise GateBExecutorError("publication_journal_schema_invalid")
    _require_exact_fields("publication_journal", value, fields)
    for name in ("authorization_id", "repository_id", "repair_job_id", "pr_id"):
        _require_stable_id(f"journal_{name}", value[name])
    _require_branch("journal_base_branch", value["base_branch"])
    _require_branch("journal_head_branch", value["head_branch"])
    _require_sha256("journal_authorization_sha256", value["authorization_sha256"])
    _require_git_sha("journal_base_sha", value["base_sha"])
    if schema_version == JOURNAL_SCHEMA_VERSION:
        _require_git_sha("journal_source_head_sha", value["source_head_sha"])
    _require_git_sha("journal_expected_tree_sha", value["expected_tree_sha"])
    _require_git_sha("journal_expected_commit_sha", value["expected_commit_sha"])
    _require_sha256("journal_marker_sha256", value["marker_sha256"])
    if value["state"] not in PUBLISH_STATES:
        raise GateBExecutorError("publication_journal_state_invalid")
    _require_int("journal_remote_pr_number", value["remote_pr_number"])
    _require_sha256("journal_sha256", value["journal_sha256"])
    if _self_hash(value, "journal_sha256") != value["journal_sha256"]:
        raise GateBExecutorError("publication_journal_hash_mismatch")


def _journal_row(publication: DraftPublication, *, state: str, remote_pr_number: int = 0) -> dict[str, Any]:
    if state not in PUBLISH_STATES:
        raise GateBExecutorError("publication_journal_state_invalid")
    row: dict[str, Any] = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "authorization_id": publication.authorization_id,
        "authorization_sha256": publication.authorization_sha256,
        "repository_id": publication.repository_id,
        "repair_job_id": publication.repair_job_id,
        "pr_id": publication.pr_id,
        "base_branch": publication.base_branch,
        "base_sha": publication.base_sha,
        "source_head_sha": publication.source_head_sha,
        "head_branch": publication.head_branch,
        "expected_tree_sha": publication.expected_tree_sha,
        "expected_commit_sha": publication.exact_commit_sha,
        "marker_sha256": publication.body_marker_sha256,
        "state": state,
        "remote_pr_number": _require_int("journal_remote_pr_number", remote_pr_number),
        "journal_sha256": "",
    }
    row["journal_sha256"] = _self_hash(row, "journal_sha256")
    _validate_journal(row)
    return row


class PublicationJournal:
    """Durable state containing hashes and IDs only; it must live outside the source tree."""

    def __init__(self, path: Path, *, repository_root: Path | None = None) -> None:
        resolved = path.resolve()
        if resolved.name in {"", ".", ".."} or resolved.suffix.lower() != ".json":
            raise GateBExecutorError("publication_journal_path_invalid")
        if repository_root is not None:
            root = repository_root.resolve()
            try:
                if os.path.commonpath((str(root), str(resolved))) == str(root):
                    raise GateBExecutorError("publication_journal_inside_source_tree")
            except ValueError as exc:
                raise GateBExecutorError("publication_journal_path_invalid") from exc
        self.path = resolved
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        value = load_json(self.path)
        _validate_journal(value)
        return value

    def save(self, value: Mapping[str, Any]) -> None:
        _validate_journal(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        with self._lock:
            try:
                temporary.write_bytes(canonical_json(value) + b"\n")
                os.replace(temporary, self.path)
            except OSError as exc:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise GateBExecutorError("publication_journal_unavailable") from exc


@dataclass(frozen=True)
class DraftPublicationReceipt:
    authorization_id: str
    authorization_sha256: str
    repository_id: str
    repair_job_id: str
    pr_id: str
    draft_pr_id: str
    head_branch: str
    base_branch: str
    commit_sha: str
    payload_sha256: str
    publisher_status: str
    state: str
    ready: bool = False
    merged: bool = False
    comments_checks_labels_reviews: int = 0
    draft: bool = True
    redaction_applied: bool = True

    def __post_init__(self) -> None:
        _require_stable_id("receipt_authorization_id", self.authorization_id)
        _require_sha256("receipt_authorization_sha256", self.authorization_sha256)
        _require_stable_id("receipt_repository_id", self.repository_id)
        _require_stable_id("receipt_repair_job_id", self.repair_job_id)
        _require_stable_id("receipt_pr_id", self.pr_id)
        _require_stable_id("receipt_draft_pr_id", self.draft_pr_id)
        _require_branch("receipt_head_branch", self.head_branch)
        _require_branch("receipt_base_branch", self.base_branch)
        _require_git_sha("receipt_commit_sha", self.commit_sha)
        _require_sha256("receipt_payload_sha256", self.payload_sha256)
        if self.publisher_status != "draft_published" or self.state != "receipt_reconciled":
            raise GateBExecutorError("publication_receipt_status_invalid")
        if self.draft is not True or self.ready is not False or self.merged is not False:
            raise GateBExecutorError("draft_pr_boundary_violation")
        if self.comments_checks_labels_reviews != 0 or self.redaction_applied is not True:
            raise GateBExecutorError("draft_pr_boundary_violation")

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "schema_version": REAL_DRAFT_PR_RECEIPT_SCHEMA_VERSION,
            "authorization_id": self.authorization_id,
            "authorization_sha256": self.authorization_sha256,
            "repository_id": self.repository_id,
            "repair_job_id": self.repair_job_id,
            "pr_id": self.pr_id,
            "draft_pr_id": self.draft_pr_id,
            "head_branch_sha256": sha256_text(self.head_branch),
            "base_branch": self.base_branch,
            "commit_sha": self.commit_sha,
            "draft": self.draft,
            "ready": self.ready,
            "merged": self.merged,
            "comments_checks_labels_reviews": self.comments_checks_labels_reviews,
            "publisher_status": self.publisher_status,
            "state": self.state,
            "payload_sha256": self.payload_sha256,
            "redaction_applied": self.redaction_applied,
            "receipt_sha256": "",
        }
        row["receipt_sha256"] = _self_hash(row, "receipt_sha256")
        return row


class GitHubDraftPublisher:
    """One-shot Git-data/Draft-PR publisher with no ready, comment, or merge routes."""

    def __init__(
        self,
        transport: JsonTransport,
        *,
        authorization: Mapping[str, Any],
        participants: Mapping[str, Any],
        repository_authorization: Mapping[str, Any],
        credential_descriptor: Mapping[str, Any],
        runtime: Mapping[str, Any],
        source_root: Path,
        github_app_private_key_file: Path,
        provider_key_environment: str,
        token: InstallationToken,
        owner: str,
        repository: str,
        expected_repository_id: int,
        journal: PublicationJournal,
        expected_app_id: int,
        expected_installation_id: int,
        timeout_seconds: int = 20,
    ) -> None:
        self._transport = transport
        self._authorization = authorization
        self._participants = participants
        self._repository_authorization = repository_authorization
        self._credential_descriptor = credential_descriptor
        self._runtime = runtime
        self._source_root = source_root
        self._github_app_private_key_file = github_app_private_key_file
        self._provider_key_environment = _require_stable_id(
            "publisher_provider_key_environment", provider_key_environment
        )
        self._token = token
        self._owner = _repository_part("publisher_owner", owner)
        self._repository = _repository_part("publisher_repository", repository)
        self._expected_repository_id = _require_int(
            "publisher_repository_id", expected_repository_id, minimum=1
        )
        self._expected_app_id = _require_int("publisher_app_id", expected_app_id, minimum=1)
        self._expected_installation_id = _require_int(
            "publisher_installation_id", expected_installation_id, minimum=1
        )
        if (
            token.app_id != self._expected_app_id
            or token.installation_id != self._expected_installation_id
        ):
            raise GateBExecutorError("github_installation_identity_mismatch")
        self._journal = journal
        self._timeout_seconds = _require_int("publisher_timeout_seconds", timeout_seconds, minimum=1)

    def _check_active(self, now_utc: str | None) -> None:
        status = require_active_execution_authorization(
            authorization=self._authorization,
            participants=self._participants,
            repository=self._repository_authorization,
            credential_descriptor=self._credential_descriptor,
            runtime=self._runtime,
            source_root=self._source_root,
            now_utc=now_utc,
        )
        required = self._authorization.get("required_fields")
        if not isinstance(required, Mapping):
            raise GateBExecutorError("authorization_required_fields_invalid")
        permissions = self._authorization.get("permission_switches")
        if not isinstance(permissions, Mapping):
            raise GateBExecutorError("permission_switches_invalid")
        _require_repair_permissions(permissions)
        if (
            status.authorization_id != required.get("authorization_id")
            or status.canonical_authorization_sha256
            != required.get("canonical_authorization_sha256")
        ):
            raise GateBExecutorError("publisher_authorization_mismatch")
        verify_credential_fingerprints(
            authorization=self._authorization,
            participants=self._participants,
            repository=self._repository_authorization,
            credential_descriptor=self._credential_descriptor,
            runtime=self._runtime,
            source_root=self._source_root,
            github_app_private_key_file=self._github_app_private_key_file,
            provider_key_environment=self._provider_key_environment,
            now_utc=now_utc,
        )

    @property
    def _base_path(self) -> str:
        return f"/repos/{self._owner}/{self._repository}"

    @property
    def _base_url(self) -> str:
        return f"https://{_GITHUB_HOST}"

    def _allowed_path(self, method: str, path: str) -> bool:
        base = re.escape(self._base_path)
        encoded_branch = r"[A-Za-z0-9._~%/-]+"
        git_sha = r"[0-9a-f]{40}"
        if method == "GET" and path == self._base_path:
            return True
        if method == "GET" and re.fullmatch(f"{base}/git/ref/heads/{encoded_branch}", path):
            return True
        if method == "GET" and re.fullmatch(f"{base}/git/commits/{git_sha}", path):
            return True
        if method == "GET" and re.fullmatch(
            rf"{base}/compare/{git_sha}\.\.\.{git_sha}", path
        ):
            return True
        if method == "GET" and re.fullmatch(
            f"{base}/pulls\\?state=open&head=[A-Za-z0-9._~%:-]+&base=[A-Za-z0-9._~%/-]+&per_page=10",
            path,
        ):
            return True
        if method == "POST" and path in {
            f"{self._base_path}/git/blobs",
            f"{self._base_path}/git/trees",
            f"{self._base_path}/git/commits",
            f"{self._base_path}/git/refs",
            f"{self._base_path}/pulls",
        }:
            return True
        return False

    def _request(
        self,
        *,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> HttpResponse:
        if not self._allowed_path(method, path):
            raise GateBExecutorError("github_endpoint_denied")
        if _require_utc("github_token_expiry", self._token.expires_at_utc) <= datetime.now(timezone.utc):
            raise GateBExecutorError("github_token_expired")
        return self._transport.request(
            method=method,
            url=self._base_url + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token.value}",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            payload=payload,
            timeout_seconds=self._timeout_seconds,
        )

    def _verify_repository(self) -> None:
        response = self._request(method="GET", path=self._base_path)
        body = _load_response_json(response, context="publisher_repository")
        if body.get("id") != self._expected_repository_id:
            raise GateBExecutorError("github_repository_identity_mismatch")
        if body.get("archived") is True or body.get("disabled") is True:
            raise GateBExecutorError("github_repository_unavailable")

    def _get_ref(self, branch: str) -> str | None:
        _require_branch("publisher_branch", branch)
        encoded = urllib_parse.quote(branch, safe="/")
        response = self._request(method="GET", path=f"{self._base_path}/git/ref/heads/{encoded}")
        if response.status == 404:
            return None
        body = _load_response_json(response, context="publisher_ref")
        obj = body.get("object")
        if not isinstance(obj, Mapping) or not isinstance(obj.get("sha"), str):
            raise GateBExecutorError("publisher_ref_invalid")
        return _require_git_sha("publisher_ref_sha", obj["sha"])

    def _verify_source_head(self, publication: DraftPublication) -> None:
        response = self._request(
            method="GET",
            path=f"{self._base_path}/git/commits/{publication.source_head_sha}",
        )
        body = _load_response_json(response, context="publisher_source_head")
        tree = body.get("tree")
        if (
            body.get("sha") != publication.source_head_sha
            or not isinstance(tree, Mapping)
            or tree.get("sha") != publication.base_tree_sha
        ):
            raise GateBExecutorError("publisher_source_head_drift")

    def _verify_forward_base(
        self, publication: DraftPublication, *, current_base_sha: str
    ) -> None:
        if current_base_sha == publication.base_sha:
            return
        response = self._request(
            method="GET",
            path=(
                f"{self._base_path}/compare/{publication.base_sha}"
                f"...{current_base_sha}"
            ),
        )
        body = _load_response_json(response, context="publisher_base_compare")
        base_commit = body.get("base_commit")
        merge_base_commit = body.get("merge_base_commit")
        if (
            body.get("status") != "ahead"
            or not isinstance(base_commit, Mapping)
            or base_commit.get("sha") != publication.base_sha
            or not isinstance(merge_base_commit, Mapping)
            or merge_base_commit.get("sha") != publication.base_sha
            or _require_int("publisher_base_ahead_by", body.get("ahead_by"), minimum=1)
            < 1
            or _require_int("publisher_base_behind_by", body.get("behind_by")) != 0
        ):
            raise GateBExecutorError("publisher_base_drift")

    def _post(self, *, path: str, payload: Mapping[str, Any], context: str, statuses: frozenset[int]) -> Mapping[str, Any]:
        response = self._request(method="POST", path=path, payload=payload)
        if response.status not in statuses:
            raise GateBExecutorError(f"{context}_failed")
        return _load_response_json(response, context=context)

    def _find_draft_pr(self, publication: DraftPublication) -> tuple[int, str] | None:
        query = urllib_parse.urlencode(
            {
                "state": "open",
                "head": f"{self._owner}:{publication.head_branch}",
                "base": publication.base_branch,
                "per_page": "10",
            }
        )
        response = self._request(method="GET", path=f"{self._base_path}/pulls?{query}")
        rows = _load_response_value(response, context="publisher_pull_list")
        if not isinstance(rows, list):
            raise GateBExecutorError("publisher_pull_list_invalid")
        matches: list[tuple[int, str]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise GateBExecutorError("publisher_pull_invalid")
            number = _require_int("publisher_pull_number", row.get("number"), minimum=1)
            if row.get("state") != "open" or row.get("draft") is not True or row.get("merged_at") is not None:
                raise GateBExecutorError("draft_pr_boundary_violation")
            base = row.get("base")
            head = row.get("head")
            body = row.get("body")
            if not isinstance(base, Mapping) or not isinstance(head, Mapping) or not isinstance(body, str):
                raise GateBExecutorError("publisher_pull_invalid")
            if (
                base.get("ref") != publication.base_branch
                or head.get("ref") != publication.head_branch
                or head.get("sha") != publication.exact_commit_sha
                or publication.body_marker not in body
            ):
                raise GateBExecutorError("publisher_pull_binding_mismatch")
            matches.append((number, body))
        if len(matches) > 1:
            raise GateBExecutorError("publisher_ambiguous_result")
        if not matches:
            return None
        return matches[0][0], matches[0][1]

    def _create_draft_pr(self, publication: DraftPublication) -> int:
        body = publication.body_marker
        response = self._request(
            method="POST",
            path=f"{self._base_path}/pulls",
            payload={
                "title": publication.title,
                "head": publication.head_branch,
                "base": publication.base_branch,
                "body": body,
                "draft": True,
            },
        )
        if response.status not in {201}:
            raise GateBExecutorError("publisher_draft_pr_failed")
        result = _load_response_json(response, context="publisher_draft_pr")
        number = _require_int("publisher_draft_pr_number", result.get("number"), minimum=1)
        if result.get("draft") is not True or result.get("merged") is True or result.get("state") != "open":
            raise GateBExecutorError("draft_pr_boundary_violation")
        read_back = self._find_draft_pr(publication)
        if read_back is None or read_back[0] != number:
            raise GateBExecutorError("publisher_draft_pr_receipt_mismatch")
        return number

    def _upload_git_objects(self, publication: DraftPublication) -> None:
        total_bytes = sum(len(item.content) for item in publication.patch_files)
        if total_bytes > _MAX_DIFF_BYTES * 4:
            raise GateBExecutorError("publisher_patch_too_large")
        for item in publication.patch_files:
            result = self._post(
                path=f"{self._base_path}/git/blobs",
                payload={
                    "content": base64.b64encode(item.content).decode("ascii"),
                    "encoding": "base64",
                },
                context="publisher_blob",
                statuses=frozenset({201}),
            )
            if result.get("sha") != item.blob_sha:
                raise GateBExecutorError("publisher_blob_hash_mismatch")
        tree_entries = [
            {"path": item.path, "mode": item.mode, "type": "blob", "sha": item.blob_sha}
            for item in publication.patch_files
        ]
        tree = self._post(
            path=f"{self._base_path}/git/trees",
            payload={"base_tree": publication.base_tree_sha, "tree": tree_entries},
            context="publisher_tree",
            statuses=frozenset({201}),
        )
        if tree.get("sha") != publication.expected_tree_sha:
            raise GateBExecutorError("publisher_tree_hash_mismatch")
        commit = self._post(
            path=f"{self._base_path}/git/commits",
            payload={
                "message": publication.commit_message,
                "tree": publication.expected_tree_sha,
                "parents": [publication.source_head_sha],
                "author": {
                    "name": "Phase 11D Pilot",
                    "email": "phase11d-pilot@users.noreply.github.com",
                    "date": publication.commit_timestamp_utc,
                },
                "committer": {
                    "name": "Phase 11D Pilot",
                    "email": "phase11d-pilot@users.noreply.github.com",
                    "date": publication.commit_timestamp_utc,
                },
            },
            context="publisher_commit",
            statuses=frozenset({201}),
        )
        if commit.get("sha") != publication.exact_commit_sha:
            raise GateBExecutorError("publisher_commit_hash_mismatch")
        ref = self._post(
            path=f"{self._base_path}/git/refs",
            payload={"ref": f"refs/heads/{publication.head_branch}", "sha": publication.exact_commit_sha},
            context="publisher_branch",
            statuses=frozenset({201}),
        )
        if ref.get("ref") != f"refs/heads/{publication.head_branch}":
            raise GateBExecutorError("publisher_branch_invalid")
        if self._get_ref(publication.head_branch) != publication.exact_commit_sha:
            raise GateBExecutorError("publisher_branch_receipt_mismatch")

    def _receipt(self, publication: DraftPublication, *, number: int) -> DraftPublicationReceipt:
        return DraftPublicationReceipt(
            authorization_id=publication.authorization_id,
            authorization_sha256=publication.authorization_sha256,
            repository_id=publication.repository_id,
            repair_job_id=publication.repair_job_id,
            pr_id=publication.pr_id,
            draft_pr_id=f"draft-pr-{number}",
            head_branch=publication.head_branch,
            base_branch=publication.base_branch,
            commit_sha=publication.exact_commit_sha,
            payload_sha256=publication.payload_sha256,
            publisher_status="draft_published",
            state="receipt_reconciled",
        )

    def reconcile(
        self, publication: DraftPublication, *, now_utc: str | None = None
    ) -> DraftPublicationReceipt | None:
        """Reconcile a previously started publication without issuing another mutation."""
        self._check_active(now_utc)
        if (
            publication.authorization_id
            != self._authorization["required_fields"]["authorization_id"]
            or publication.authorization_sha256
            != self._authorization["required_fields"]["canonical_authorization_sha256"]
        ):
            raise GateBExecutorError("publisher_authorization_mismatch")
        head_sha = self._get_ref(publication.head_branch)
        if head_sha is None:
            return None
        if head_sha != publication.exact_commit_sha:
            raise GateBExecutorError("publisher_ref_collision")
        found = self._find_draft_pr(publication)
        if found is None:
            return None
        return self._receipt(publication, number=found[0])

    def publish(
        self, publication: DraftPublication, *, now_utc: str | None = None
    ) -> DraftPublicationReceipt:
        """Publish exactly one branch and one Draft PR, or quarantine on uncertainty."""
        self._check_active(now_utc)
        required = self._authorization.get("required_fields")
        if not isinstance(required, Mapping) or (
            publication.authorization_id != required.get("authorization_id")
            or publication.authorization_sha256 != required.get("canonical_authorization_sha256")
        ):
            raise GateBExecutorError("publisher_authorization_mismatch")
        existing = self._journal.load()
        intent = _journal_row(publication, state="intent_recorded")
        if existing is not None:
            if existing.get("schema_version") != JOURNAL_SCHEMA_VERSION:
                raise GateBExecutorError("publisher_journal_upgrade_required")
            immutable = (
                "authorization_id",
                "authorization_sha256",
                "repository_id",
                "repair_job_id",
                "pr_id",
                "base_branch",
                "base_sha",
                "source_head_sha",
                "head_branch",
                "expected_tree_sha",
                "expected_commit_sha",
                "marker_sha256",
            )
            if any(existing[name] != intent[name] for name in immutable):
                raise GateBExecutorError("publisher_journal_binding_mismatch")
            if existing["state"] == "quarantined":
                raise GateBExecutorError("publisher_quarantined")
            if existing["state"] == "receipt_reconciled":
                reconciled = self.reconcile(publication, now_utc=now_utc)
                if reconciled is None:
                    raise GateBExecutorError("publisher_receipt_missing")
                return reconciled
        else:
            self._journal.save(intent)
        try:
            self._verify_repository()
            base_ref = self._get_ref(publication.base_branch)
            if base_ref is None:
                raise GateBExecutorError("publisher_base_drift")
            self._verify_forward_base(publication, current_base_sha=base_ref)
            self._verify_source_head(publication)
            head_ref = self._get_ref(publication.head_branch)
            if head_ref is None:
                self._upload_git_objects(publication)
            elif head_ref != publication.exact_commit_sha:
                raise GateBExecutorError("publisher_ref_collision")
            self._journal.save(_journal_row(publication, state="branch_push_observed"))
            found = self._find_draft_pr(publication)
            number = found[0] if found is not None else self._create_draft_pr(publication)
            self._journal.save(_journal_row(publication, state="draft_pr_observed", remote_pr_number=number))
            receipt = self._receipt(publication, number=number)
            self._journal.save(_journal_row(publication, state="receipt_reconciled", remote_pr_number=number))
            return receipt
        except GateBExecutorError as exc:
            try:
                reconciled = self.reconcile(publication, now_utc=now_utc)
            except GateBExecutorError:
                reconciled = None
            if reconciled is not None:
                try:
                    self._journal.save(
                        _journal_row(
                            publication,
                            state="receipt_reconciled",
                            remote_pr_number=int(reconciled.draft_pr_id.removeprefix("draft-pr-")),
                        )
                    )
                except GateBExecutorError:
                    pass
                return reconciled
            try:
                self._journal.save(_journal_row(publication, state="quarantined"))
            except GateBExecutorError:
                pass
            if str(exc) in {"http_transport_failure", "publisher_draft_pr_failed", "publisher_receipt_missing"}:
                raise GateBExecutorError("publisher_ambiguous_result") from exc
            raise


def validate_authorized_selected_candidate(
    *,
    authorization: Mapping[str, Any],
    receipt: Mapping[str, Any],
    candidate: PullRequestCandidate,
) -> None:
    """Require the repair candidate to be one of the immutable selected PR rows."""
    validate_selection_receipt(receipt)
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    repository_allowlist = required.get("repository_allowlist")
    if not isinstance(repository_allowlist, Mapping):
        raise GateBExecutorError("repository_allowlist_invalid")
    repository_ids = repository_allowlist.get("repository_ids")
    if not isinstance(repository_ids, list) or len(repository_ids) != 1:
        raise GateBExecutorError("repository_allowlist_invalid")
    if (
        receipt["authorization_id"] != required.get("authorization_id")
        or receipt["canonical_authorization_sha256"]
        != required.get("canonical_authorization_sha256")
        or receipt["repository_id"] != repository_ids[0]
    ):
        raise GateBExecutorError("selection_authorization_mismatch")
    for row in receipt["selected_prs"]:
        if not isinstance(row, Mapping) or row.get("pr_id") != candidate.pr_id:
            continue
        if (
            row.get("github_pull_request_id") != candidate.github_id
            or row.get("base_sha") != candidate.base_sha
            or row.get("head_sha") != candidate.head_sha
            or row.get("selection_rank_sha256") != candidate.selection_rank_sha256
        ):
            raise GateBExecutorError("selection_candidate_drift")
        return
    raise GateBExecutorError("selection_candidate_not_authorized")


REVIEW_COHORT_RECEIPT_SCHEMA_VERSION = "crag.phase11d.gate-b-review-cohort/v1alpha1"
RESUMED_REVIEW_COHORT_RECEIPT_SCHEMA_VERSION = (
    "crag.phase11d.gate-b-review-cohort/v1alpha2"
)
_REVIEW_COHORT_RECEIPT_FIELDS = frozenset(
    {
        "authorization_id",
        "budget_usage",
        "canonical_authorization_sha256",
        "created_at_utc",
        "repository_id",
        "review_cohort_receipt_sha256",
        "review_rows",
        "schema_version",
        "selected_pr_count",
        "selection_receipt_sha256",
        "stop_category",
    }
)
_RESUMED_REVIEW_COHORT_RECEIPT_FIELDS = _REVIEW_COHORT_RECEIPT_FIELDS | {
    "previous_review_cohort_receipt_sha256"
}
_RESUMABLE_REVIEW_STOP_CATEGORIES = frozenset({"http_transport_failure"})
_REVIEW_ROW_FIELDS = frozenset(
    {
        "cached_tokens",
        "feedback_eligible_finding_ids",
        "finding_ids",
        "http_attempt_count",
        "input_tokens",
        "output_tokens",
        "pr_id",
        "provider_call_count",
        "response_sha256",
        "status",
        "terminal_category",
    }
)
_REVIEW_BUDGET_FIELDS = frozenset(
    {
        "cached_tokens",
        "http_attempts",
        "input_tokens",
        "logical_calls",
        "micro_cny",
        "output_tokens",
    }
)


def _candidate_from_selection_row(
    row: Mapping[str, Any],
    *,
    base_branch: str,
) -> PullRequestCandidate:
    pr_id = _require_stable_id("selection_pr_id", row.get("pr_id"))
    match = re.fullmatch(r"pr-([1-9][0-9]*)", pr_id)
    if match is None:
        raise GateBExecutorError("selection_pr_id_invalid")
    updated_at = row.get("updated_at_utc")
    if not isinstance(updated_at, str):
        raise GateBExecutorError("selection_updated_at_invalid")
    _require_utc("selection_updated_at", updated_at)
    return PullRequestCandidate(
        number=int(match.group(1)),
        github_id=_require_int(
            "selection_github_pull_request_id",
            row.get("github_pull_request_id"),
            minimum=1,
        ),
        base_branch=base_branch,
        base_sha=_require_git_sha("selection_base_sha", row.get("base_sha")),
        head_sha=_require_git_sha("selection_head_sha", row.get("head_sha")),
        updated_at_utc=updated_at,
        selection_rank_sha256=_require_sha256(
            "selection_rank_sha256", row.get("selection_rank_sha256")
        ),
    )


def _review_budget_from_authorization(required: Mapping[str, Any]) -> ReviewBudget:
    return ReviewBudget(
        max_logical_calls=_require_int(
            "max_logical_calls", required.get("max_logical_calls"), minimum=1
        ),
        max_http_attempts=_require_int(
            "max_http_attempts", required.get("max_http_attempts"), minimum=1
        ),
        max_input_tokens=_require_int("max_input_tokens", required.get("max_input_tokens")),
        max_output_tokens=_require_int("max_output_tokens", required.get("max_output_tokens")),
        max_cached_tokens=_require_int("max_cached_tokens", required.get("max_cached_tokens")),
        max_micro_cny=_require_int("max_micro_cny", required.get("max_micro_cny")),
        max_wall_clock_seconds=_require_int(
            "max_wall_clock_seconds", required.get("max_wall_clock_seconds"), minimum=1
        ),
    )


def build_review_cohort_receipt(
    *,
    authorization: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    outcomes: Sequence[ReviewOutcome],
    budget: ReviewBudget,
    stop_category: str,
    created_at_utc: str,
    previous_review_cohort_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    validate_selection_receipt(selection_receipt)
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    repository_allowlist = required.get("repository_allowlist")
    if not isinstance(repository_allowlist, Mapping):
        raise GateBExecutorError("repository_allowlist_invalid")
    repository_ids = repository_allowlist.get("repository_ids")
    if not isinstance(repository_ids, list) or len(repository_ids) != 1:
        raise GateBExecutorError("repository_allowlist_invalid")
    if len(outcomes) != selection_receipt["selected_pr_count"]:
        raise GateBExecutorError("review_cohort_count_mismatch")
    _require_utc("review_cohort_created_at", created_at_utc)
    schema_version = REVIEW_COHORT_RECEIPT_SCHEMA_VERSION
    if previous_review_cohort_receipt_sha256 is not None:
        _require_sha256(
            "previous_review_cohort_receipt_sha256",
            previous_review_cohort_receipt_sha256,
        )
        schema_version = RESUMED_REVIEW_COHORT_RECEIPT_SCHEMA_VERSION
    receipt: dict[str, Any] = {
        "schema_version": schema_version,
        "authorization_id": required["authorization_id"],
        "canonical_authorization_sha256": required["canonical_authorization_sha256"],
        "repository_id": repository_ids[0],
        "selection_receipt_sha256": selection_receipt["selection_receipt_sha256"],
        "selected_pr_count": selection_receipt["selected_pr_count"],
        "created_at_utc": created_at_utc,
        "review_rows": [outcome.receipt_row() for outcome in outcomes],
        "budget_usage": budget.to_dict(),
        "stop_category": stop_category,
        "review_cohort_receipt_sha256": "",
    }
    if previous_review_cohort_receipt_sha256 is not None:
        receipt["previous_review_cohort_receipt_sha256"] = (
            previous_review_cohort_receipt_sha256
        )
    receipt["review_cohort_receipt_sha256"] = _self_hash(
        receipt, "review_cohort_receipt_sha256"
    )
    validate_review_cohort_receipt(
        receipt,
        authorization=authorization,
        selection_receipt=selection_receipt,
    )
    return receipt


def validate_review_cohort_receipt(
    receipt: Mapping[str, Any],
    *,
    authorization: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
) -> None:
    schema_version = receipt.get("schema_version")
    if schema_version == REVIEW_COHORT_RECEIPT_SCHEMA_VERSION:
        expected_fields = _REVIEW_COHORT_RECEIPT_FIELDS
    elif schema_version == RESUMED_REVIEW_COHORT_RECEIPT_SCHEMA_VERSION:
        expected_fields = _RESUMED_REVIEW_COHORT_RECEIPT_FIELDS
        _require_sha256(
            "previous_review_cohort_receipt_sha256",
            receipt.get("previous_review_cohort_receipt_sha256"),
        )
    else:
        raise GateBExecutorError("review_cohort_schema_invalid")
    _require_exact_fields("review_cohort_receipt", receipt, expected_fields)
    validate_selection_receipt(selection_receipt)
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    repository_allowlist = required.get("repository_allowlist")
    if not isinstance(repository_allowlist, Mapping):
        raise GateBExecutorError("repository_allowlist_invalid")
    repository_ids = repository_allowlist.get("repository_ids")
    if not isinstance(repository_ids, list) or len(repository_ids) != 1:
        raise GateBExecutorError("repository_allowlist_invalid")
    if (
        receipt["authorization_id"] != required.get("authorization_id")
        or receipt["canonical_authorization_sha256"]
        != required.get("canonical_authorization_sha256")
        or receipt["repository_id"] != repository_ids[0]
        or receipt["selection_receipt_sha256"]
        != selection_receipt["selection_receipt_sha256"]
        or receipt["selected_pr_count"] != selection_receipt["selected_pr_count"]
    ):
        raise GateBExecutorError("review_cohort_binding_mismatch")
    _require_utc("review_cohort_created_at", receipt["created_at_utc"])
    stop_category = _require_stable_id("review_stop_category", receipt["stop_category"])
    rows = receipt["review_rows"]
    selected_rows = selection_receipt["selected_prs"]
    if not isinstance(rows, list) or len(rows) != receipt["selected_pr_count"]:
        raise GateBExecutorError("review_cohort_count_mismatch")
    totals = {
        "logical_calls": 0,
        "http_attempts": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "micro_cny": 0,
    }
    failure_index: int | None = None
    for index, (row, selected) in enumerate(zip(rows, selected_rows)):
        if not isinstance(row, Mapping) or not isinstance(selected, Mapping):
            raise GateBExecutorError("review_row_invalid")
        _require_exact_fields("review_row", row, _REVIEW_ROW_FIELDS)
        if row["pr_id"] != selected.get("pr_id"):
            raise GateBExecutorError("review_pr_order_mismatch")
        status = row["status"]
        category = _require_stable_id("review_terminal_category", row["terminal_category"])
        if status not in {"completed", "failed"}:
            raise GateBExecutorError("review_status_invalid")
        if status == "completed" and category != "completed":
            raise GateBExecutorError("review_terminal_category_invalid")
        if status == "failed" and category == "completed":
            raise GateBExecutorError("review_terminal_category_invalid")
        if failure_index is None and status == "failed":
            failure_index = index
        if failure_index is not None and index > failure_index and category != "cohort_stopped":
            raise GateBExecutorError("review_stop_sequence_invalid")
        finding_ids = row["finding_ids"]
        feedback_ids = row["feedback_eligible_finding_ids"]
        if not isinstance(finding_ids, list) or not isinstance(feedback_ids, list):
            raise GateBExecutorError("review_findings_invalid")
        for finding_id in [*finding_ids, *feedback_ids]:
            _require_sha256("review_finding_id", finding_id)
        if not set(feedback_ids).issubset(finding_ids):
            raise GateBExecutorError("review_feedback_finding_invalid")
        _require_sha256("review_response_sha256", row["response_sha256"])
        provider_calls = _require_int("review_provider_call_count", row["provider_call_count"])
        http_attempts = _require_int("review_http_attempt_count", row["http_attempt_count"])
        if provider_calls not in {0, 1} or http_attempts != provider_calls:
            raise GateBExecutorError("provider_usage_ambiguity")
        input_tokens = _require_int("review_input_tokens", row["input_tokens"])
        output_tokens = _require_int("review_output_tokens", row["output_tokens"])
        cached_tokens = _require_int("review_cached_tokens", row["cached_tokens"])
        totals["logical_calls"] += provider_calls
        totals["http_attempts"] += http_attempts
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        totals["cached_tokens"] += cached_tokens
        totals["micro_cny"] += review_cost_micro_cny(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )
    expected_stop = "none" if failure_index is None else rows[failure_index]["terminal_category"]
    if stop_category != expected_stop:
        raise GateBExecutorError("review_stop_category_mismatch")
    budget_usage = receipt["budget_usage"]
    if not isinstance(budget_usage, Mapping):
        raise GateBExecutorError("review_budget_usage_invalid")
    _require_exact_fields("review_budget_usage", budget_usage, _REVIEW_BUDGET_FIELDS)
    for name, expected in totals.items():
        if _require_int(f"review_budget_{name}", budget_usage.get(name)) != expected:
            raise GateBExecutorError("review_budget_usage_mismatch")
    if _self_hash(receipt, "review_cohort_receipt_sha256") != receipt[
        "review_cohort_receipt_sha256"
    ]:
        raise GateBExecutorError("review_cohort_receipt_hash_mismatch")


def _review_outcome_from_receipt_row(row: Mapping[str, Any]) -> ReviewOutcome:
    return ReviewOutcome(
        pr_id=str(row["pr_id"]),
        status=str(row["status"]),
        terminal_category=str(row["terminal_category"]),
        finding_ids=tuple(str(value) for value in row["finding_ids"]),
        feedback_eligible_finding_ids=tuple(
            str(value) for value in row["feedback_eligible_finding_ids"]
        ),
        provider_call_count=int(row["provider_call_count"]),
        http_attempt_count=int(row["http_attempt_count"]),
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        cached_tokens=int(row["cached_tokens"]),
        response_sha256=str(row["response_sha256"]),
    )


def _review_selected_candidate(
    *,
    authorization: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    selected: Mapping[str, Any],
    expected: PullRequestCandidate,
    seed: str,
    reader: GitHubRepositoryReader,
    client: ZhipuReviewClient,
    budget: ReviewBudget,
) -> ReviewOutcome:
    try:
        current = reader.pull_request_candidate(
            expected.number,
            selection_seed_sha256=seed,
        )
        validate_authorized_selected_candidate(
            authorization=authorization,
            receipt=selection_receipt,
            candidate=current,
        )
        if current.receipt_row() != dict(selected):
            raise GateBExecutorError("selection_candidate_drift")
        diff_text = reader.pull_request_diff(expected.number)
    except GateBExecutorError as exc:
        category = str(exc)
        if STABLE_ID_RE.fullmatch(category) is None:
            category = "github_review_input_failure"
        return _review_failure(expected.pr_id, category)
    return review_with_budget(
        client=client,
        budget=budget,
        candidate=current,
        diff_text=diff_text,
    )


def run_review_cohort(
    *,
    authorization: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    reader: GitHubRepositoryReader,
    client: ZhipuReviewClient,
    created_at_utc: str,
) -> dict[str, Any]:
    """Run the immutable selected denominator with no retry or replacement."""
    validate_selection_receipt(selection_receipt)
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    if (
        selection_receipt["authorization_id"] != required.get("authorization_id")
        or selection_receipt["canonical_authorization_sha256"]
        != required.get("canonical_authorization_sha256")
    ):
        raise GateBExecutorError("selection_authorization_mismatch")
    base_rule = required.get("allowed_base_branch_rule")
    if not isinstance(base_rule, Mapping):
        raise GateBExecutorError("base_branch_rule_invalid")
    base_branch_value = base_rule.get("base_branch")
    if not isinstance(base_branch_value, str):
        raise GateBExecutorError("base_branch_rule_invalid")
    base_branch = _repository_part("base_branch", base_branch_value)
    seed = _require_sha256(
        "selection_seed_sha256", required.get("deterministic_selection_seed_sha256")
    )
    budget = _review_budget_from_authorization(required)
    outcomes: list[ReviewOutcome] = []
    stop_category = "none"
    for selected in selection_receipt["selected_prs"]:
        if not isinstance(selected, Mapping):
            raise GateBExecutorError("selection_row_invalid")
        expected = _candidate_from_selection_row(selected, base_branch=base_branch)
        if stop_category != "none":
            outcomes.append(_review_failure(expected.pr_id, "cohort_stopped"))
            continue
        outcome = _review_selected_candidate(
            authorization=authorization,
            selection_receipt=selection_receipt,
            selected=selected,
            expected=expected,
            seed=seed,
            reader=reader,
            client=client,
            budget=budget,
        )
        outcomes.append(outcome)
        if outcome.status != "completed":
            stop_category = outcome.terminal_category
    return build_review_cohort_receipt(
        authorization=authorization,
        selection_receipt=selection_receipt,
        outcomes=outcomes,
        budget=budget,
        stop_category=stop_category,
        created_at_utc=created_at_utc,
    )


def resume_review_cohort(
    *,
    authorization: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    previous_authorization: Mapping[str, Any],
    previous_selection_receipt: Mapping[str, Any],
    previous_review_receipt: Mapping[str, Any],
    reader: GitHubRepositoryReader,
    client: ZhipuReviewClient,
    created_at_utc: str,
) -> dict[str, Any]:
    """Resume only a zero-provider-call GitHub transport failure without replay."""
    validate_selection_receipt(selection_receipt)
    validate_review_cohort_receipt(
        previous_review_receipt,
        authorization=previous_authorization,
        selection_receipt=previous_selection_receipt,
    )
    if (
        selection_receipt["selected_pr_count"]
        != previous_selection_receipt["selected_pr_count"]
        or selection_receipt["selected_prs"] != previous_selection_receipt["selected_prs"]
        or selection_receipt["repository_id"] != previous_selection_receipt["repository_id"]
    ):
        raise GateBExecutorError("review_resume_selection_mismatch")
    previous_rows = previous_review_receipt["review_rows"]
    assert isinstance(previous_rows, list)
    failure_index = next(
        (index for index, row in enumerate(previous_rows) if row["status"] == "failed"),
        None,
    )
    if failure_index is None:
        raise GateBExecutorError("review_resume_not_required")
    failed_row = previous_rows[failure_index]
    if (
        previous_review_receipt["stop_category"]
        not in _RESUMABLE_REVIEW_STOP_CATEGORIES
        or failed_row["terminal_category"] != previous_review_receipt["stop_category"]
    ):
        raise GateBExecutorError("review_resume_category_denied")
    for row in previous_rows[failure_index:]:
        if any(
            row[name] != 0
            for name in (
                "provider_call_count",
                "http_attempt_count",
                "input_tokens",
                "output_tokens",
                "cached_tokens",
            )
        ):
            raise GateBExecutorError("review_resume_prior_usage_ambiguous")

    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    base_rule = required.get("allowed_base_branch_rule")
    if not isinstance(base_rule, Mapping) or not isinstance(base_rule.get("base_branch"), str):
        raise GateBExecutorError("base_branch_rule_invalid")
    base_branch = _repository_part("base_branch", base_rule["base_branch"])
    seed = _require_sha256(
        "selection_seed_sha256", required.get("deterministic_selection_seed_sha256")
    )
    budget = _review_budget_from_authorization(required)
    previous_usage = previous_review_receipt.get("budget_usage")
    if not isinstance(previous_usage, Mapping):
        raise GateBExecutorError("review_budget_usage_invalid")
    budget.restore(previous_usage)
    outcomes = [
        _review_outcome_from_receipt_row(row) for row in previous_rows[:failure_index]
    ]
    stop_category = "none"
    selected_rows = selection_receipt["selected_prs"]
    for selected in selected_rows[failure_index:]:
        if not isinstance(selected, Mapping):
            raise GateBExecutorError("selection_row_invalid")
        expected = _candidate_from_selection_row(selected, base_branch=base_branch)
        if stop_category != "none":
            outcomes.append(_review_failure(expected.pr_id, "cohort_stopped"))
            continue
        outcome = _review_selected_candidate(
            authorization=authorization,
            selection_receipt=selection_receipt,
            selected=selected,
            expected=expected,
            seed=seed,
            reader=reader,
            client=client,
            budget=budget,
        )
        outcomes.append(outcome)
        if outcome.status != "completed":
            stop_category = outcome.terminal_category
    return build_review_cohort_receipt(
        authorization=authorization,
        selection_receipt=selection_receipt,
        outcomes=outcomes,
        budget=budget,
        stop_category=stop_category,
        created_at_utc=created_at_utc,
        previous_review_cohort_receipt_sha256=previous_review_receipt[
            "review_cohort_receipt_sha256"
        ],
    )


def execute_authorized_reviews(
    *,
    authorization: Mapping[str, Any],
    participants: Mapping[str, Any],
    repository_authorization: Mapping[str, Any],
    credential_descriptor: Mapping[str, Any],
    runtime: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    source_root: Path,
    github_app_private_key_file: Path,
    provider_key_environment: str,
    owner: str,
    repository: str,
    now_utc: str,
    github_transport: JsonTransport | None = None,
    provider_transport: JsonTransport | None = None,
    finding_sink: Callable[[tuple[EphemeralReviewFinding, ...]], None] | None = None,
    previous_authorization: Mapping[str, Any] | None = None,
    previous_selection_receipt: Mapping[str, Any] | None = None,
    previous_review_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Open the two read/provider transports only after every frozen gate passes."""
    require_active_execution_authorization(
        authorization=authorization,
        participants=participants,
        repository=repository_authorization,
        credential_descriptor=credential_descriptor,
        runtime=runtime,
        source_root=source_root,
        now_utc=now_utc,
    )
    verify_credential_fingerprints(
        authorization=authorization,
        participants=participants,
        repository=repository_authorization,
        credential_descriptor=credential_descriptor,
        runtime=runtime,
        source_root=source_root,
        github_app_private_key_file=github_app_private_key_file,
        provider_key_environment=provider_key_environment,
        now_utc=now_utc,
    )
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    _validate_repository_authorization(required, repository_authorization)
    _validate_descriptor(required, credential_descriptor)
    normalized_locator = (
        f"https://github.com/{_repository_part('github_owner', owner)}/"
        f"{_repository_part('github_repository', repository)}"
    )
    repository_rows = repository_authorization["repositories"]
    assert isinstance(repository_rows, list) and isinstance(repository_rows[0], Mapping)
    if sha256_text(normalized_locator) != repository_rows[0]["locator_sha256"]:
        raise GateBExecutorError("repository_locator_mismatch")
    private_key = _read_private_key_file(
        github_app_private_key_file,
        expected_sha256=credential_descriptor["github_app_private_key_fingerprint_sha256"],
    )
    active_github_transport = github_transport or StrictHttpsJsonTransport(
        allowed_hosts=frozenset({_GITHUB_HOST})
    )
    token = GitHubAppAuthenticator(active_github_transport).mint_installation_token(
        app_id=credential_descriptor["github_app_id"],
        installation_id=credential_descriptor["github_app_installation_id"],
        private_key=private_key,
    )
    del private_key
    reader = GitHubRepositoryReader(
        active_github_transport,
        token=token,
        owner=owner,
        repository=repository,
        expected_repository_id=_github_numeric_repository_id(
            required["repository_allowlist"]["repository_ids"][0]
        ),
    )
    reader.verify_repository()
    try:
        provider_key = os.environ[provider_key_environment]
    except KeyError as exc:
        raise GateBExecutorError("provider_key_unavailable") from exc
    active_provider_transport = provider_transport or StrictHttpsJsonTransport(
        allowed_hosts=frozenset({_ZHIPU_HOST})
    )
    client = ZhipuReviewClient(
        active_provider_transport,
        api_key=provider_key,
        model=credential_descriptor["provider_model_snapshot"],
        finding_sink=finding_sink,
    )
    try:
        previous_inputs = (
            previous_authorization,
            previous_selection_receipt,
            previous_review_receipt,
        )
        if any(value is not None for value in previous_inputs):
            if not all(value is not None for value in previous_inputs):
                raise GateBExecutorError("review_resume_inputs_incomplete")
            assert previous_authorization is not None
            assert previous_selection_receipt is not None
            assert previous_review_receipt is not None
            return resume_review_cohort(
                authorization=authorization,
                selection_receipt=selection_receipt,
                previous_authorization=previous_authorization,
                previous_selection_receipt=previous_selection_receipt,
                previous_review_receipt=previous_review_receipt,
                reader=reader,
                client=client,
                created_at_utc=now_utc,
            )
        return run_review_cohort(
            authorization=authorization,
            selection_receipt=selection_receipt,
            reader=reader,
            client=client,
            created_at_utc=now_utc,
        )
    finally:
        del provider_key


class GateBRepairCoordinator:
    """Fail-closed human Review-to-Repair state machine for one authorized job."""

    def __init__(
        self,
        *,
        authorization: Mapping[str, Any],
        participants: Mapping[str, Any],
        repository: Mapping[str, Any],
        credential_descriptor: Mapping[str, Any],
        runtime: Mapping[str, Any],
        source_root: Path,
        selection_receipt: Mapping[str, Any],
        now_utc: str,
    ) -> None:
        self._authorization = authorization
        self._participants = participants
        self._repository = repository
        self._credential_descriptor = credential_descriptor
        self._runtime = runtime
        self._source_root = source_root
        self._selection_receipt = receipt = dict(selection_receipt)
        self._check_active(now_utc)
        validate_selection_receipt(receipt)
        required = authorization.get("required_fields")
        if not isinstance(required, Mapping):
            raise GateBExecutorError("authorization_required_fields_invalid")
        if required.get("max_repair_jobs") != 1 or required.get("max_repair_findings_per_pr") != 1:
            raise GateBExecutorError("repair_ceiling_invalid")
        self._required = required
        self._ledger = OneUseApprovalLedger(
            participants=participants,
            sla_seconds=_require_int(
                "human_approval_sla_seconds", required.get("human_approval_sla_seconds"), minimum=1
            ),
        )
        self._selection: FindingSelection | None = None
        self._selected_candidate: PullRequestCandidate | None = None
        self._intent: RepairIntent | None = None
        self._write_approval: HumanApproval | None = None
        self._sandbox: SandboxResult | None = None
        self._publication: DraftPublication | None = None
        self._draft_binding_sha256: str | None = None
        self._draft_approval: HumanApproval | None = None
        self._receipt: DraftPublicationReceipt | None = None

    def _check_active(self, now_utc: str) -> None:
        require_active_execution_authorization(
            authorization=self._authorization,
            participants=self._participants,
            repository=self._repository,
            credential_descriptor=self._credential_descriptor,
            runtime=self._runtime,
            source_root=self._source_root,
            now_utc=now_utc,
        )
        permissions = self._authorization.get("permission_switches")
        if not isinstance(permissions, Mapping):
            raise GateBExecutorError("permission_switches_invalid")
        _require_repair_permissions(permissions)

    def select_finding(
        self,
        *,
        candidate: PullRequestCandidate,
        review: ReviewOutcome,
        finding_id: str,
        selection_id: str,
        selector_id: str,
        selected_at_utc: str,
    ) -> FindingSelection:
        self._check_active(selected_at_utc)
        if self._selection is not None:
            raise GateBExecutorError("repair_job_already_selected")
        validate_authorized_selected_candidate(
            authorization=self._authorization,
            receipt=self._selection_receipt,
            candidate=candidate,
        )
        self._selection = select_finding_for_repair(
            participants=self._participants,
            candidate=candidate,
            review=review,
            finding_id=finding_id,
            selection_id=selection_id,
            selector_id=selector_id,
            selected_at_utc=selected_at_utc,
        )
        self._selected_candidate = candidate
        return self._selection

    def prepare_repair(
        self,
        *,
        candidate: PullRequestCandidate,
        plan_text: str,
        repair_job_id: str,
        requested_by: str,
        requested_at_utc: str,
    ) -> RepairIntent:
        self._check_active(requested_at_utc)
        if self._selection is None:
            raise GateBExecutorError("repair_finding_not_selected")
        if self._selected_candidate != candidate:
            raise GateBExecutorError("repair_candidate_drift")
        if self._intent is not None:
            raise GateBExecutorError("repair_job_already_prepared")
        base_rule = self._required.get("allowed_base_branch_rule")
        if not isinstance(base_rule, Mapping) or not isinstance(base_rule.get("base_branch"), str):
            raise GateBExecutorError("authorization_base_branch_invalid")
        self._intent = create_repair_intent(
            participants=self._participants,
            selection=self._selection,
            candidate=candidate,
            plan_text=plan_text,
            repair_job_id=repair_job_id,
            requested_by=requested_by,
            requested_at_utc=requested_at_utc,
            base_branch=base_rule["base_branch"],
        )
        return self._intent

    def request_write_approval(self, *, approval_id: str, requested_at_utc: str) -> str:
        self._check_active(requested_at_utc)
        if self._intent is None:
            raise GateBExecutorError("repair_not_prepared")
        self._ledger.register(
            approval_id=approval_id,
            kind="write",
            binding_sha256=self._intent.write_binding_sha256,
            requested_at_utc=requested_at_utc,
        )
        return self._intent.write_binding_sha256

    def decide_write_approval(
        self,
        *,
        approval_id: str,
        actor_id: str,
        decision: str,
        approved_at_utc: str,
    ) -> HumanApproval:
        self._check_active(approved_at_utc)
        if self._intent is None:
            raise GateBExecutorError("write_approval_state_invalid")
        if self._write_approval is not None:
            raise GateBExecutorError("repair_approval_replay")
        approval = self._ledger.decide(
            approval_id=approval_id,
            actor_id=actor_id,
            decision=decision,
            approved_at_utc=approved_at_utc,
        )
        if approval.kind != "write" or approval.binding_sha256 != self._intent.write_binding_sha256:
            raise GateBExecutorError("write_approval_binding_mismatch")
        self._write_approval = approval
        return approval

    def submit_sandbox_result(self, *, result: SandboxResult, observed_at_utc: str) -> SandboxResult:
        self._check_active(observed_at_utc)
        if self._intent is None or self._write_approval is None:
            raise GateBExecutorError("sandbox_write_approval_missing")
        if self._write_approval.decision != "approved":
            raise GateBExecutorError("sandbox_write_approval_declined")
        if self._sandbox is not None:
            raise GateBExecutorError("sandbox_result_replay")
        validate_sandbox_result(intent=self._intent, result=result)
        self._sandbox = result
        return result

    def request_draft_pr_approval(
        self,
        *,
        approval_id: str,
        material: DraftPublicationMaterial,
        requested_at_utc: str,
    ) -> str:
        self._check_active(requested_at_utc)
        if self._intent is None or self._sandbox is None:
            raise GateBExecutorError("draft_pr_sandbox_result_missing")
        if self._publication is not None:
            raise GateBExecutorError("draft_pr_already_prepared")
        if material.expected_tree_sha != self._sandbox.expected_tree_sha or material.exact_commit_sha != self._sandbox.exact_commit_sha:
            raise GateBExecutorError("draft_pr_commit_drift")
        if material.patch_files != self._sandbox.patch_files:
            raise GateBExecutorError("draft_pr_patch_drift")
        self._publication = build_draft_publication(
            authorization=self._authorization,
            intent=self._intent,
            material=material,
        )
        binding = _repair_binding(
            "draft_pr",
            {
                "write_approval_binding_sha256": self._intent.write_binding_sha256,
                "sandbox_patch_sha256": self._sandbox.patch_sha256,
                "sandbox_checkpoint_sha256": self._sandbox.checkpoint_sha256,
                "sandbox_test_sha256": self._sandbox.test_sha256,
                "sandbox_budget_sha256": self._sandbox.budget_sha256,
                "exact_commit_sha": self._publication.exact_commit_sha,
                "publisher_payload_sha256": self._publication.payload_sha256,
            },
        )
        self._ledger.register(
            approval_id=approval_id,
            kind="draft_pr",
            binding_sha256=binding,
            requested_at_utc=requested_at_utc,
        )
        self._draft_binding_sha256 = binding
        return binding

    def decide_draft_pr_approval(
        self,
        *,
        approval_id: str,
        actor_id: str,
        decision: str,
        approved_at_utc: str,
    ) -> HumanApproval:
        self._check_active(approved_at_utc)
        if self._publication is None or self._draft_approval is not None:
            raise GateBExecutorError("draft_pr_approval_state_invalid")
        approval = self._ledger.decide(
            approval_id=approval_id,
            actor_id=actor_id,
            decision=decision,
            approved_at_utc=approved_at_utc,
        )
        if approval.kind != "draft_pr" or approval.binding_sha256 != self._draft_binding_sha256:
            raise GateBExecutorError("draft_pr_approval_binding_mismatch")
        self._draft_approval = approval
        return approval

    def publish_draft_pr(
        self,
        *,
        publisher: GitHubDraftPublisher,
        published_at_utc: str,
    ) -> DraftPublicationReceipt:
        self._check_active(published_at_utc)
        if self._publication is None or self._draft_approval is None:
            raise GateBExecutorError("draft_pr_approval_missing")
        if self._draft_approval.decision != "approved":
            raise GateBExecutorError("draft_pr_approval_declined")
        if self._receipt is not None:
            return self._receipt
        self._receipt = publisher.publish(self._publication, now_utc=published_at_utc)
        return self._receipt

    def repair_receipt(self) -> dict[str, Any]:
        """Return the sanitized record suitable for an external restricted receipt store."""
        if self._intent is None:
            raise GateBExecutorError("repair_not_prepared")
        row: dict[str, Any] = self._intent.to_dict()
        row["selection"] = self._selection.to_dict() if self._selection is not None else None
        row["write_approval"] = self._write_approval.to_dict() if self._write_approval else None
        row["sandbox"] = self._sandbox.receipt_projection() if self._sandbox else None
        row["draft_pr_approval"] = self._draft_approval.to_dict() if self._draft_approval else None
        row["draft_pr_receipt_sha256"] = (
            self._receipt.to_dict()["receipt_sha256"] if self._receipt is not None else ""
        )
        return row


OPERATOR_SESSION_SCHEMA_VERSION = "crag.phase11d.operator-session/v1alpha1"
_OPERATOR_MAX_REQUEST_BYTES = 3_000_000
TIMEOUT_RECOVERY_CHECKPOINT_SCHEMA_VERSION = (
    "crag.phase11d.operator-timeout-recovery/v1alpha1"
)
_OPERATOR_SESSION_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "authorization_id",
        "canonical_authorization_sha256",
        "review_cohort_receipt_sha256",
        "selected_finding_id",
        "state",
        "terminal_category",
        "started_at_utc",
        "session_receipt_sha256",
    }
)
_TIMEOUT_RECOVERY_CHECKPOINT_FIELDS = frozenset(
    {
        "schema_version",
        "source_authorization_id",
        "source_authorization_sha256",
        "source_runtime_sha256",
        "selection_receipt_sha256",
        "review_cohort_receipt_sha256",
        "timeout_session_receipt_sha256",
        "selected_finding_id",
        "selected_review_response_sha256",
        "prior_selection_sha256",
        "prior_plan_sha256",
        "prior_write_binding_sha256",
        "prior_write_approval_id",
        "prior_write_approved_at_utc",
        "recovery_actor_id",
        "recovery_selection_id",
        "recovery_repair_job_id",
        "recovery_write_approval_id",
        "recovery_requested_at_utc",
        "checkpoint_sha256",
    }
)


def _require_complete_recovery_review(review_receipt: Mapping[str, Any]) -> None:
    review_rows = review_receipt.get("review_rows")
    if (
        review_receipt.get("stop_category") != "none"
        or not isinstance(review_rows, list)
        or not 20 <= len(review_rows) <= 30
        or any(
            not isinstance(row, Mapping)
            or row.get("status") != "completed"
            or row.get("terminal_category") != "completed"
            for row in review_rows
        )
    ):
        raise GateBExecutorError("recovery_review_incomplete")


def validate_operator_timeout_receipt(
    receipt: Mapping[str, Any],
    *,
    authorization: Mapping[str, Any],
    review_receipt: Mapping[str, Any],
) -> None:
    _require_exact_fields(
        "operator_session_receipt", receipt, _OPERATOR_SESSION_RECEIPT_FIELDS
    )
    if receipt["schema_version"] != OPERATOR_SESSION_SCHEMA_VERSION:
        raise GateBExecutorError("operator_session_schema_invalid")
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    if (
        receipt["authorization_id"] != required.get("authorization_id")
        or receipt["canonical_authorization_sha256"]
        != required.get("canonical_authorization_sha256")
        or receipt["review_cohort_receipt_sha256"]
        != review_receipt.get("review_cohort_receipt_sha256")
    ):
        raise GateBExecutorError("operator_session_binding_mismatch")
    _require_sha256("operator_selected_finding_id", receipt["selected_finding_id"])
    _require_utc("operator_session_started_at", receipt["started_at_utc"])
    if receipt["state"] != "expired" or receipt["terminal_category"] != "timeout":
        raise GateBExecutorError("operator_session_not_recoverable")
    receipt_sha = _require_sha256(
        "operator_session_receipt_sha256", receipt["session_receipt_sha256"]
    )
    if _self_hash(receipt, "session_receipt_sha256") != receipt_sha:
        raise GateBExecutorError("operator_session_receipt_hash_mismatch")


def build_timeout_recovery_checkpoint(
    *,
    source_authorization: Mapping[str, Any],
    source_runtime: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    review_receipt: Mapping[str, Any],
    timeout_receipt: Mapping[str, Any],
    selected_finding_id: str,
    prior_selection_sha256: str,
    prior_plan_sha256: str,
    prior_write_binding_sha256: str,
    prior_write_approval_id: str,
    prior_write_approved_at_utc: str,
    recovery_actor_id: str,
    recovery_selection_id: str,
    recovery_repair_job_id: str,
    recovery_write_approval_id: str,
    recovery_requested_at_utc: str,
) -> dict[str, Any]:
    validate_selection_receipt(selection_receipt)
    validate_review_cohort_receipt(
        review_receipt,
        authorization=source_authorization,
        selection_receipt=selection_receipt,
    )
    _require_complete_recovery_review(review_receipt)
    validate_operator_timeout_receipt(
        timeout_receipt,
        authorization=source_authorization,
        review_receipt=review_receipt,
    )
    finding_id = _require_sha256("recovery_selected_finding_id", selected_finding_id)
    if timeout_receipt["selected_finding_id"] != finding_id:
        raise GateBExecutorError("recovery_selected_finding_mismatch")
    matching_rows = [
        row
        for row in review_receipt["review_rows"]
        if finding_id in row.get("feedback_eligible_finding_ids", [])
    ]
    if len(matching_rows) != 1 or matching_rows[0].get("status") != "completed":
        raise GateBExecutorError("recovery_selected_finding_unavailable")
    source_required = source_authorization.get("required_fields")
    if not isinstance(source_required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    runtime_sha = _require_sha256(
        "recovery_source_runtime_sha256", source_runtime.get("runtime_sha256")
    )
    for name, value in (
        ("prior_selection_sha256", prior_selection_sha256),
        ("prior_plan_sha256", prior_plan_sha256),
        ("prior_write_binding_sha256", prior_write_binding_sha256),
    ):
        _require_sha256(name, value)
    for name, value in (
        ("prior_write_approval_id", prior_write_approval_id),
        ("recovery_actor_id", recovery_actor_id),
        ("recovery_selection_id", recovery_selection_id),
        ("recovery_repair_job_id", recovery_repair_job_id),
        ("recovery_write_approval_id", recovery_write_approval_id),
    ):
        _require_stable_id(name, value)
    _require_utc("prior_write_approved_at", prior_write_approved_at_utc)
    _require_utc("recovery_requested_at", recovery_requested_at_utc)
    checkpoint: dict[str, Any] = {
        "schema_version": TIMEOUT_RECOVERY_CHECKPOINT_SCHEMA_VERSION,
        "source_authorization_id": source_required["authorization_id"],
        "source_authorization_sha256": source_required["canonical_authorization_sha256"],
        "source_runtime_sha256": runtime_sha,
        "selection_receipt_sha256": selection_receipt["selection_receipt_sha256"],
        "review_cohort_receipt_sha256": review_receipt[
            "review_cohort_receipt_sha256"
        ],
        "timeout_session_receipt_sha256": timeout_receipt["session_receipt_sha256"],
        "selected_finding_id": finding_id,
        "selected_review_response_sha256": matching_rows[0]["response_sha256"],
        "prior_selection_sha256": prior_selection_sha256,
        "prior_plan_sha256": prior_plan_sha256,
        "prior_write_binding_sha256": prior_write_binding_sha256,
        "prior_write_approval_id": prior_write_approval_id,
        "prior_write_approved_at_utc": prior_write_approved_at_utc,
        "recovery_actor_id": recovery_actor_id,
        "recovery_selection_id": recovery_selection_id,
        "recovery_repair_job_id": recovery_repair_job_id,
        "recovery_write_approval_id": recovery_write_approval_id,
        "recovery_requested_at_utc": recovery_requested_at_utc,
        "checkpoint_sha256": "",
    }
    checkpoint["checkpoint_sha256"] = _self_hash(checkpoint, "checkpoint_sha256")
    validate_timeout_recovery_checkpoint(
        checkpoint,
        source_authorization=source_authorization,
        source_runtime=source_runtime,
        selection_receipt=selection_receipt,
        review_receipt=review_receipt,
        timeout_receipt=timeout_receipt,
    )
    return checkpoint


def validate_timeout_recovery_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    source_authorization: Mapping[str, Any],
    source_runtime: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    review_receipt: Mapping[str, Any],
    timeout_receipt: Mapping[str, Any],
) -> None:
    _require_exact_fields(
        "timeout_recovery_checkpoint", checkpoint, _TIMEOUT_RECOVERY_CHECKPOINT_FIELDS
    )
    if checkpoint["schema_version"] != TIMEOUT_RECOVERY_CHECKPOINT_SCHEMA_VERSION:
        raise GateBExecutorError("recovery_checkpoint_schema_invalid")
    source_required = source_authorization.get("required_fields")
    if not isinstance(source_required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    expected = {
        "source_authorization_id": source_required.get("authorization_id"),
        "source_authorization_sha256": source_required.get(
            "canonical_authorization_sha256"
        ),
        "source_runtime_sha256": source_runtime.get("runtime_sha256"),
        "selection_receipt_sha256": selection_receipt.get("selection_receipt_sha256"),
        "review_cohort_receipt_sha256": review_receipt.get(
            "review_cohort_receipt_sha256"
        ),
        "timeout_session_receipt_sha256": timeout_receipt.get(
            "session_receipt_sha256"
        ),
        "selected_finding_id": timeout_receipt.get("selected_finding_id"),
    }
    if any(checkpoint.get(name) != value for name, value in expected.items()):
        raise GateBExecutorError("recovery_checkpoint_binding_mismatch")
    for name in (
        "source_authorization_sha256",
        "source_runtime_sha256",
        "selection_receipt_sha256",
        "review_cohort_receipt_sha256",
        "timeout_session_receipt_sha256",
        "selected_finding_id",
        "selected_review_response_sha256",
        "prior_selection_sha256",
        "prior_plan_sha256",
        "prior_write_binding_sha256",
        "checkpoint_sha256",
    ):
        _require_sha256(f"recovery_{name}", checkpoint[name])
    for name in (
        "prior_write_approval_id",
        "recovery_actor_id",
        "recovery_selection_id",
        "recovery_repair_job_id",
        "recovery_write_approval_id",
    ):
        _require_stable_id(name, checkpoint[name])
    _require_utc("prior_write_approved_at", checkpoint["prior_write_approved_at_utc"])
    _require_utc("recovery_requested_at", checkpoint["recovery_requested_at_utc"])
    _require_complete_recovery_review(review_receipt)
    rows = [
        row
        for row in review_receipt.get("review_rows", [])
        if checkpoint["selected_finding_id"]
        in row.get("feedback_eligible_finding_ids", [])
    ]
    if (
        len(rows) != 1
        or rows[0].get("status") != "completed"
        or rows[0].get("response_sha256")
        != checkpoint["selected_review_response_sha256"]
    ):
        raise GateBExecutorError("recovery_selected_finding_unavailable")
    if _self_hash(checkpoint, "checkpoint_sha256") != checkpoint["checkpoint_sha256"]:
        raise GateBExecutorError("recovery_checkpoint_hash_mismatch")


def rebind_recovery_selection_receipt(
    *,
    authorization: Mapping[str, Any],
    source_selection_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    validate_selection_receipt(source_selection_receipt)
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    seed = _require_sha256(
        "recovery_selection_seed", required["deterministic_selection_seed_sha256"]
    )
    base_branch = required["allowed_base_branch_rule"]["base_branch"]
    candidates = []
    for row in source_selection_receipt["selected_prs"]:
        candidate = _candidate_from_selection_row(row, base_branch=base_branch)
        candidates.append(
            PullRequestCandidate(
                number=candidate.number,
                github_id=candidate.github_id,
                base_branch=candidate.base_branch,
                base_sha=candidate.base_sha,
                head_sha=candidate.head_sha,
                updated_at_utc=candidate.updated_at_utc,
                selection_rank_sha256=sha256_text(f"{seed}\n{candidate.pr_id}"),
            )
        )
    candidates.sort(key=lambda item: (item.selection_rank_sha256, item.number))
    return build_selection_receipt(
        authorization=authorization,
        candidates=candidates,
        excluded_counts=source_selection_receipt["excluded_counts"],
    )


def rebind_recovery_review_receipt(
    *,
    authorization: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    source_review_receipt: Mapping[str, Any],
    created_at_utc: str,
) -> dict[str, Any]:
    outcomes = {
        row["pr_id"]: _review_outcome_from_receipt_row(row)
        for row in source_review_receipt["review_rows"]
    }
    ordered = [outcomes[row["pr_id"]] for row in selection_receipt["selected_prs"]]
    budget_usage = source_review_receipt["budget_usage"]
    required = authorization["required_fields"]
    budget = ReviewBudget(
        max_logical_calls=required["max_logical_calls"],
        max_http_attempts=required["max_http_attempts"],
        max_input_tokens=required["max_input_tokens"],
        max_output_tokens=required["max_output_tokens"],
        max_cached_tokens=required["max_cached_tokens"],
        max_micro_cny=required["max_micro_cny"],
        max_wall_clock_seconds=required["max_wall_clock_seconds"],
    )
    budget.restore(budget_usage)
    if budget.to_dict() != budget_usage:
        raise GateBExecutorError("recovery_review_budget_mismatch")
    return build_review_cohort_receipt(
        authorization=authorization,
        selection_receipt=selection_receipt,
        outcomes=ordered,
        budget=budget,
        stop_category="none",
        created_at_utc=created_at_utc,
        previous_review_cohort_receipt_sha256=source_review_receipt[
            "review_cohort_receipt_sha256"
        ],
    )


def _operator_patch_files(value: Any) -> tuple[SandboxPatchFile, ...]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise GateBExecutorError("operator_patch_files_invalid")
    files: list[SandboxPatchFile] = []
    for row in value:
        if not isinstance(row, Mapping):
            raise GateBExecutorError("operator_patch_file_invalid")
        _require_exact_fields(
            "operator_patch_file",
            row,
            frozenset({"path", "mode", "content_base64"}),
        )
        encoded = row["content_base64"]
        if not isinstance(encoded, str) or len(encoded) > 1_400_000:
            raise GateBExecutorError("operator_patch_content_invalid")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise GateBExecutorError("operator_patch_content_invalid") from exc
        files.append(
            SandboxPatchFile(
                path=row["path"],
                mode=row["mode"],
                content=content,
            )
        )
    return tuple(files)


class ReviewRepairOperatorSession:
    """One in-memory, human-driven Review-to-Repair session for one repair job."""

    def __init__(
        self,
        *,
        coordinator: GateBRepairCoordinator,
        authorization: Mapping[str, Any],
        selection_receipt: Mapping[str, Any],
        review_receipt: Mapping[str, Any],
        findings: Sequence[EphemeralReviewFinding],
        receipt_directory: Path,
        source_root: Path,
        publisher_factory: Callable[[], GitHubDraftPublisher],
        started_at_utc: str,
        timeout_seconds: int,
    ) -> None:
        validate_review_cohort_receipt(
            review_receipt,
            authorization=authorization,
            selection_receipt=selection_receipt,
        )
        _require_utc("operator_started_at", started_at_utc)
        self._coordinator = coordinator
        self._authorization = authorization
        self._selection_receipt = dict(selection_receipt)
        self._review_receipt = dict(review_receipt)
        self._receipt_directory = receipt_directory.resolve()
        resolved_source = source_root.resolve()
        if self._receipt_directory == resolved_source or resolved_source in self._receipt_directory.parents:
            raise GateBExecutorError("operator_receipt_directory_inside_source_tree")
        self._publisher_factory = publisher_factory
        self._started_at_utc = started_at_utc
        self._timeout_seconds = _require_int(
            "operator_timeout_seconds", timeout_seconds, minimum=1
        )
        self._started_monotonic = time.monotonic()
        self._state = "awaiting_selection"
        self._lock = threading.RLock()
        self._selected_finding_id = ""
        self._intent: RepairIntent | None = None
        self._draft_binding_sha256 = ""
        self._receipt: DraftPublicationReceipt | None = None
        review_rows = review_receipt.get("review_rows")
        if not isinstance(review_rows, list):
            raise GateBExecutorError("review_rows_invalid")
        self._reviews = {
            str(row["pr_id"]): _review_outcome_from_receipt_row(row)
            for row in review_rows
            if isinstance(row, Mapping) and row.get("status") == "completed"
        }
        selected_rows = selection_receipt.get("selected_prs")
        if not isinstance(selected_rows, list):
            raise GateBExecutorError("selection_rows_invalid")
        required = authorization.get("required_fields")
        base_rule = required.get("allowed_base_branch_rule") if isinstance(required, Mapping) else None
        if not isinstance(base_rule, Mapping) or not isinstance(base_rule.get("base_branch"), str):
            raise GateBExecutorError("base_branch_rule_invalid")
        self._candidates = {
            str(row["pr_id"]): _candidate_from_selection_row(
                row,
                base_branch=base_rule["base_branch"],
            )
            for row in selected_rows
            if isinstance(row, Mapping)
        }
        finding_map: dict[str, EphemeralReviewFinding] = {}
        for finding in findings:
            review = self._reviews.get(finding.pr_id)
            if (
                review is None
                or finding.finding_id not in review.feedback_eligible_finding_ids
                or finding.response_sha256 != review.response_sha256
                or finding.finding_id in finding_map
            ):
                raise GateBExecutorError("operator_finding_binding_mismatch")
            finding_map[finding.finding_id] = finding
        expected_ids = {
            finding_id
            for review in self._reviews.values()
            for finding_id in review.feedback_eligible_finding_ids
        }
        if set(finding_map) != expected_ids:
            raise GateBExecutorError("operator_finding_set_incomplete")
        self._findings = finding_map

    @classmethod
    def from_write_recovery(
        cls,
        *,
        coordinator: GateBRepairCoordinator,
        authorization: Mapping[str, Any],
        selection_receipt: Mapping[str, Any],
        review_receipt: Mapping[str, Any],
        intent: RepairIntent,
        selected_finding_id: str,
        receipt_directory: Path,
        source_root: Path,
        publisher_factory: Callable[[], GitHubDraftPublisher],
        started_at_utc: str,
        timeout_seconds: int,
    ) -> ReviewRepairOperatorSession:
        validate_review_cohort_receipt(
            review_receipt,
            authorization=authorization,
            selection_receipt=selection_receipt,
        )
        if intent.finding_id != selected_finding_id:
            raise GateBExecutorError("recovery_selected_finding_mismatch")
        instance = cls.__new__(cls)
        instance._coordinator = coordinator
        instance._authorization = authorization
        instance._selection_receipt = dict(selection_receipt)
        instance._review_receipt = dict(review_receipt)
        instance._receipt_directory = receipt_directory.resolve()
        resolved_source = source_root.resolve()
        if (
            instance._receipt_directory == resolved_source
            or resolved_source in instance._receipt_directory.parents
        ):
            raise GateBExecutorError("operator_receipt_directory_inside_source_tree")
        instance._publisher_factory = publisher_factory
        instance._started_at_utc = started_at_utc
        _require_utc("operator_started_at", started_at_utc)
        instance._timeout_seconds = _require_int(
            "operator_timeout_seconds", timeout_seconds, minimum=1
        )
        instance._started_monotonic = time.monotonic()
        instance._state = "awaiting_write_approval"
        instance._lock = threading.RLock()
        instance._selected_finding_id = selected_finding_id
        instance._intent = intent
        instance._draft_binding_sha256 = ""
        instance._receipt = None
        instance._reviews = {}
        instance._candidates = {}
        instance._findings = {}
        return instance

    @property
    def terminal(self) -> bool:
        with self._lock:
            return self._state in {"published", "declined", "stopped", "expired"}

    def _check_live(self) -> None:
        if time.monotonic() - self._started_monotonic > self._timeout_seconds:
            self._state = "expired"
            self._clear_ephemeral()
            self._write_session_receipt("timeout")
            raise GateBExecutorError("operator_session_expired")
        if self.terminal:
            raise GateBExecutorError("operator_session_terminal")

    def _clear_ephemeral(self) -> None:
        self._findings.clear()

    def _write_receipt(self, name: str, value: Mapping[str, Any]) -> None:
        self._receipt_directory.mkdir(parents=True, exist_ok=True)
        _write_json(self._receipt_directory / name, value)

    def _write_session_receipt(self, terminal_category: str) -> None:
        required = self._authorization.get("required_fields")
        if not isinstance(required, Mapping):
            raise GateBExecutorError("authorization_required_fields_invalid")
        row: dict[str, Any] = {
            "schema_version": OPERATOR_SESSION_SCHEMA_VERSION,
            "authorization_id": required["authorization_id"],
            "canonical_authorization_sha256": required["canonical_authorization_sha256"],
            "review_cohort_receipt_sha256": self._review_receipt[
                "review_cohort_receipt_sha256"
            ],
            "selected_finding_id": self._selected_finding_id,
            "state": self._state,
            "terminal_category": terminal_category,
            "started_at_utc": self._started_at_utc,
            "session_receipt_sha256": "",
        }
        row["session_receipt_sha256"] = _self_hash(row, "session_receipt_sha256")
        self._write_receipt("operator-session-receipt.json", row)

    def status(self) -> dict[str, Any]:
        with self._lock:
            if not self.terminal:
                self._check_live()
            result: dict[str, Any] = {
                "state": self._state,
                "terminal": self.terminal,
                "selected_finding_id": self._selected_finding_id,
                "write_binding_sha256": (
                    self._intent.write_binding_sha256 if self._intent is not None else ""
                ),
                "draft_pr_binding_sha256": self._draft_binding_sha256,
                "draft_pr_id": self._receipt.draft_pr_id if self._receipt else "",
            }
            if self._state == "awaiting_selection":
                result["findings"] = [
                    item.operator_view()
                    for item in sorted(
                        self._findings.values(),
                        key=lambda item: (item.pr_id, item.index),
                    )
                ]
            return result

    def select_and_plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        fields = frozenset(
            {
                "finding_id",
                "selection_id",
                "selector_id",
                "selected_at_utc",
                "plan_text",
                "repair_job_id",
                "requested_by",
                "requested_at_utc",
                "write_approval_id",
                "write_requested_at_utc",
            }
        )
        _require_exact_fields("operator_select_and_plan", payload, fields)
        with self._lock:
            self._check_live()
            if self._state != "awaiting_selection":
                raise GateBExecutorError("operator_state_invalid")
            finding_id = _require_sha256("operator_finding_id", payload["finding_id"])
            finding = self._findings.get(finding_id)
            if finding is None:
                raise GateBExecutorError("operator_finding_unknown")
            candidate = self._candidates[finding.pr_id]
            review = self._reviews[finding.pr_id]
            self._coordinator.select_finding(
                candidate=candidate,
                review=review,
                finding_id=finding_id,
                selection_id=payload["selection_id"],
                selector_id=payload["selector_id"],
                selected_at_utc=payload["selected_at_utc"],
            )
            self._intent = self._coordinator.prepare_repair(
                candidate=candidate,
                plan_text=payload["plan_text"],
                repair_job_id=payload["repair_job_id"],
                requested_by=payload["requested_by"],
                requested_at_utc=payload["requested_at_utc"],
            )
            binding = self._coordinator.request_write_approval(
                approval_id=payload["write_approval_id"],
                requested_at_utc=payload["write_requested_at_utc"],
            )
            self._selected_finding_id = finding_id
            self._findings = {finding_id: finding}
            self._state = "awaiting_write_approval"
            return {
                "state": self._state,
                "selection_sha256": self._intent.selection_sha256,
                "plan_sha256": self._intent.plan_sha256,
                "write_binding_sha256": binding,
            }

    def decide_write(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        fields = frozenset({"approval_id", "actor_id", "decision", "approved_at_utc"})
        _require_exact_fields("operator_write_approval", payload, fields)
        with self._lock:
            self._check_live()
            if self._state != "awaiting_write_approval":
                raise GateBExecutorError("operator_state_invalid")
            approval = self._coordinator.decide_write_approval(
                approval_id=payload["approval_id"],
                actor_id=payload["actor_id"],
                decision=payload["decision"],
                approved_at_utc=payload["approved_at_utc"],
            )
            self._state = "awaiting_sandbox" if approval.decision == "approved" else "declined"
            if self._state == "declined":
                self._clear_ephemeral()
                self._write_session_receipt("write_declined")
            return {"state": self._state, "approval": approval.to_dict()}

    def submit_sandbox(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        fields = frozenset(
            {
                "repair_job_id",
                "worktree_receipt_sha256",
                "task_branch_sha256",
                "patch_sha256",
                "checkpoint_sha256",
                "test_sha256",
                "budget_sha256",
                "tests_passed",
                "reflection_passed",
                "exact_commit_sha",
                "expected_tree_sha",
                "patch_files",
                "base_tree_sha",
                "commit_message",
                "commit_timestamp_utc",
                "observed_at_utc",
                "draft_approval_id",
                "draft_requested_at_utc",
            }
        )
        _require_exact_fields("operator_sandbox", payload, fields)
        with self._lock:
            self._check_live()
            if self._state != "awaiting_sandbox" or self._intent is None:
                raise GateBExecutorError("operator_state_invalid")
            patch_files = _operator_patch_files(payload["patch_files"])
            result = SandboxResult(
                repair_job_id=payload["repair_job_id"],
                worktree_receipt_sha256=payload["worktree_receipt_sha256"],
                task_branch_sha256=payload["task_branch_sha256"],
                patch_sha256=payload["patch_sha256"],
                checkpoint_sha256=payload["checkpoint_sha256"],
                test_sha256=payload["test_sha256"],
                budget_sha256=payload["budget_sha256"],
                tests_passed=payload["tests_passed"],
                reflection_passed=payload["reflection_passed"],
                exact_commit_sha=payload["exact_commit_sha"],
                expected_tree_sha=payload["expected_tree_sha"],
                patch_files=patch_files,
            )
            self._coordinator.submit_sandbox_result(
                result=result,
                observed_at_utc=payload["observed_at_utc"],
            )
            material = build_draft_publication_material(
                intent=self._intent,
                sandbox=result,
                base_tree_sha=payload["base_tree_sha"],
                commit_message=payload["commit_message"],
                commit_timestamp_utc=payload["commit_timestamp_utc"],
            )
            self._draft_binding_sha256 = self._coordinator.request_draft_pr_approval(
                approval_id=payload["draft_approval_id"],
                material=material,
                requested_at_utc=payload["draft_requested_at_utc"],
            )
            self._state = "awaiting_draft_pr_approval"
            return {
                "state": self._state,
                "exact_commit_sha": result.exact_commit_sha,
                "patch_sha256": result.patch_sha256,
                "test_sha256": result.test_sha256,
                "draft_pr_binding_sha256": self._draft_binding_sha256,
            }

    def decide_draft_pr(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        fields = frozenset({"approval_id", "actor_id", "decision", "approved_at_utc"})
        _require_exact_fields("operator_draft_pr_approval", payload, fields)
        with self._lock:
            self._check_live()
            if self._state != "awaiting_draft_pr_approval":
                raise GateBExecutorError("operator_state_invalid")
            approval = self._coordinator.decide_draft_pr_approval(
                approval_id=payload["approval_id"],
                actor_id=payload["actor_id"],
                decision=payload["decision"],
                approved_at_utc=payload["approved_at_utc"],
            )
            self._state = "ready_to_publish" if approval.decision == "approved" else "declined"
            if self._state == "declined":
                self._clear_ephemeral()
                self._write_session_receipt("draft_pr_declined")
            return {"state": self._state, "approval": approval.to_dict()}

    def publish(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _require_exact_fields("operator_publish", payload, frozenset({"published_at_utc"}))
        with self._lock:
            self._check_live()
            if self._state != "ready_to_publish":
                raise GateBExecutorError("operator_state_invalid")
            self._receipt = self._coordinator.publish_draft_pr(
                publisher=self._publisher_factory(),
                published_at_utc=payload["published_at_utc"],
            )
            repair_receipt = self._coordinator.repair_receipt()
            repair_receipt["repair_receipt_sha256"] = _self_hash(
                {**repair_receipt, "repair_receipt_sha256": ""},
                "repair_receipt_sha256",
            )
            self._write_receipt("repair-receipt.json", repair_receipt)
            self._write_receipt("draft-pr-receipt.json", self._receipt.to_dict())
            self._state = "published"
            self._clear_ephemeral()
            self._write_session_receipt("draft_pr_published")
            return {"state": self._state, "draft_pr_receipt": self._receipt.to_dict()}

    def stop(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        fields = frozenset({"actor_id", "stopped_at_utc", "reason"})
        _require_exact_fields("operator_stop", payload, fields)
        with self._lock:
            self._check_live()
            _resolve_human_actor(self._coordinator._participants, payload["actor_id"])
            _require_utc("operator_stopped_at", payload["stopped_at_utc"])
            reason_sha256 = _hash_ephemeral_text(
                "operator_stop_reason", payload["reason"], maximum_bytes=4000
            )
            self._state = "stopped"
            self._clear_ephemeral()
            self._write_session_receipt("human_stop")
            return {"state": self._state, "reason_sha256": reason_sha256}

    def dispatch(
        self,
        *,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if method == "GET" and path == "/v1/status" and payload is None:
            return self.status()
        if method != "POST" or payload is None:
            raise GateBExecutorError("operator_endpoint_denied")
        routes: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            "/v1/select-and-plan": self.select_and_plan,
            "/v1/write-approval": self.decide_write,
            "/v1/sandbox": self.submit_sandbox,
            "/v1/draft-pr-approval": self.decide_draft_pr,
            "/v1/publish": self.publish,
            "/v1/shutdown": self.stop,
        }
        action = routes.get(path)
        if action is None:
            raise GateBExecutorError("operator_endpoint_denied")
        return action(payload)


class LoopbackReviewRepairServer:
    """Authenticated HTTP wrapper that can bind only to the IPv4 loopback address."""

    def __init__(
        self,
        session: ReviewRepairOperatorSession,
        *,
        port: int = 0,
        bearer_token: str | None = None,
    ) -> None:
        self.session = session
        self.bearer_token = bearer_token or secrets.token_urlsafe(32)
        if not isinstance(self.bearer_token, str) or len(self.bearer_token) < 32:
            raise GateBExecutorError("operator_bearer_token_invalid")
        _require_int("operator_port", port)
        if port > 65535:
            raise GateBExecutorError("operator_port_invalid")
        wrapper = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Phase11DLoopback/1"
            sys_version = ""

            def log_message(self, format: str, *args: Any) -> None:
                del format, args

            def _respond(self, status: HTTPStatus, value: Mapping[str, Any]) -> None:
                body = canonical_json(value)
                self.send_response(status.value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                if self.client_address[0] != "127.0.0.1":
                    return False
                supplied = self.headers.get("Authorization", "")
                expected = f"Bearer {wrapper.bearer_token}"
                return secrets.compare_digest(supplied, expected)

            def _handle(self, method: str) -> None:
                if not self._authorized():
                    self._respond(HTTPStatus.UNAUTHORIZED, {"error": "operator_unauthorized"})
                    return
                payload: Mapping[str, Any] | None = None
                if method == "POST":
                    try:
                        length = int(self.headers.get("Content-Length", ""))
                    except ValueError:
                        length = -1
                    if length < 0 or length > _OPERATOR_MAX_REQUEST_BYTES:
                        self._respond(
                            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            {"error": "operator_request_size_invalid"},
                        )
                        return
                    raw = self.rfile.read(length)
                    try:
                        loaded = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
                    except (json.JSONDecodeError, GateBExecutorError):
                        self._respond(HTTPStatus.BAD_REQUEST, {"error": "operator_json_invalid"})
                        return
                    if not isinstance(loaded, Mapping):
                        self._respond(HTTPStatus.BAD_REQUEST, {"error": "operator_json_invalid"})
                        return
                    payload = loaded
                try:
                    result = wrapper.session.dispatch(
                        method=method,
                        path=urllib_parse.urlsplit(self.path).path,
                        payload=payload,
                    )
                except GateBExecutorError as exc:
                    self._respond(HTTPStatus.CONFLICT, {"error": str(exc)})
                    return
                self._respond(HTTPStatus.OK, result)
                if wrapper.session.terminal:
                    threading.Thread(target=wrapper._server.shutdown, daemon=True).start()

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def serve_until_terminal(self) -> None:
        def expire_session() -> None:
            remaining = max(
                0.0,
                self.session._timeout_seconds
                - (time.monotonic() - self.session._started_monotonic),
            )
            threading.Event().wait(remaining)
            if not self.session.terminal:
                try:
                    self.session.status()
                except GateBExecutorError:
                    pass
                self._server.shutdown()

        threading.Thread(target=expire_session, daemon=True).start()
        self._server.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self._server.shutdown()

    def close(self) -> None:
        self._server.server_close()


def prepare_timeout_recovery_operator_session(
    *,
    authorization: Mapping[str, Any],
    source_authorization: Mapping[str, Any],
    participants: Mapping[str, Any],
    repository_authorization: Mapping[str, Any],
    credential_descriptor: Mapping[str, Any],
    source_credential_descriptor: Mapping[str, Any],
    runtime: Mapping[str, Any],
    source_runtime: Mapping[str, Any],
    source_selection_receipt: Mapping[str, Any],
    source_review_receipt: Mapping[str, Any],
    timeout_receipt: Mapping[str, Any],
    recovery_checkpoint: Mapping[str, Any],
    plan_text: str,
    source_root: Path,
    receipt_directory: Path,
    publisher_factory: Callable[[], GitHubDraftPublisher],
    now_utc: str,
) -> ReviewRepairOperatorSession:
    source_status = validate_execution_authorization(
        authorization=source_authorization,
        participants=participants,
        repository=repository_authorization,
        credential_descriptor=source_credential_descriptor,
        runtime=source_runtime,
        now_utc=recovery_checkpoint["prior_write_approved_at_utc"],
    )
    if not source_status.gate_b_allowed:
        blocker = source_status.blockers[0] if source_status.blockers else "gate_b_closed"
        raise GateBExecutorError(f"recovery_source_{blocker}")
    require_active_execution_authorization(
        authorization=authorization,
        participants=participants,
        repository=repository_authorization,
        credential_descriptor=credential_descriptor,
        runtime=runtime,
        source_root=source_root,
        now_utc=now_utc,
    )
    validate_review_cohort_receipt(
        source_review_receipt,
        authorization=source_authorization,
        selection_receipt=source_selection_receipt,
    )
    validate_operator_timeout_receipt(
        timeout_receipt,
        authorization=source_authorization,
        review_receipt=source_review_receipt,
    )
    validate_timeout_recovery_checkpoint(
        recovery_checkpoint,
        source_authorization=source_authorization,
        source_runtime=source_runtime,
        selection_receipt=source_selection_receipt,
        review_receipt=source_review_receipt,
        timeout_receipt=timeout_receipt,
    )
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    if (
        required.get("deterministic_selection_seed_sha256")
        != recovery_checkpoint["checkpoint_sha256"]
    ):
        raise GateBExecutorError("recovery_authorization_checkpoint_mismatch")
    recovery_actor_id, _recovery_actor_role = _resolve_human_actor(
        participants, recovery_checkpoint["recovery_actor_id"]
    )
    selection_receipt = rebind_recovery_selection_receipt(
        authorization=authorization,
        source_selection_receipt=source_selection_receipt,
    )
    review_receipt = rebind_recovery_review_receipt(
        authorization=authorization,
        selection_receipt=selection_receipt,
        source_review_receipt=source_review_receipt,
        created_at_utc=now_utc,
    )
    selected_finding_id = recovery_checkpoint["selected_finding_id"]
    source_review_row = next(
        row
        for row in source_review_receipt["review_rows"]
        if selected_finding_id in row["feedback_eligible_finding_ids"]
    )
    selected_pr_id = source_review_row["pr_id"]
    selected_row = next(
        row for row in selection_receipt["selected_prs"] if row["pr_id"] == selected_pr_id
    )
    candidate = _candidate_from_selection_row(
        selected_row,
        base_branch=required["allowed_base_branch_rule"]["base_branch"],
    )
    review = _review_outcome_from_receipt_row(
        next(row for row in review_receipt["review_rows"] if row["pr_id"] == selected_pr_id)
    )
    coordinator = GateBRepairCoordinator(
        authorization=authorization,
        participants=participants,
        repository=repository_authorization,
        credential_descriptor=credential_descriptor,
        runtime=runtime,
        source_root=source_root,
        selection_receipt=selection_receipt,
        now_utc=now_utc,
    )
    coordinator.select_finding(
        candidate=candidate,
        review=review,
        finding_id=selected_finding_id,
        selection_id=recovery_checkpoint["recovery_selection_id"],
        selector_id=recovery_actor_id,
        selected_at_utc=now_utc,
    )
    intent = coordinator.prepare_repair(
        candidate=candidate,
        plan_text=plan_text,
        repair_job_id=recovery_checkpoint["recovery_repair_job_id"],
        requested_by=recovery_actor_id,
        requested_at_utc=now_utc,
    )
    write_binding = coordinator.request_write_approval(
        approval_id=recovery_checkpoint["recovery_write_approval_id"],
        requested_at_utc=now_utc,
    )
    session = ReviewRepairOperatorSession.from_write_recovery(
        coordinator=coordinator,
        authorization=authorization,
        selection_receipt=selection_receipt,
        review_receipt=review_receipt,
        intent=intent,
        selected_finding_id=selected_finding_id,
        receipt_directory=receipt_directory,
        source_root=source_root,
        publisher_factory=publisher_factory,
        started_at_utc=now_utc,
        timeout_seconds=_require_int(
            "max_wall_clock_seconds", required.get("max_wall_clock_seconds"), minimum=1
        ),
    )
    recovery_receipt: dict[str, Any] = {
        "schema_version": "crag.phase11d.operator-timeout-recovery-receipt/v1alpha1",
        "authorization_id": required["authorization_id"],
        "canonical_authorization_sha256": required["canonical_authorization_sha256"],
        "checkpoint_sha256": recovery_checkpoint["checkpoint_sha256"],
        "source_review_cohort_receipt_sha256": source_review_receipt[
            "review_cohort_receipt_sha256"
        ],
        "source_timeout_session_receipt_sha256": timeout_receipt[
            "session_receipt_sha256"
        ],
        "selection_receipt_sha256": selection_receipt["selection_receipt_sha256"],
        "review_cohort_receipt_sha256": review_receipt[
            "review_cohort_receipt_sha256"
        ],
        "selected_finding_id": selected_finding_id,
        "prior_write_binding_sha256": recovery_checkpoint[
            "prior_write_binding_sha256"
        ],
        "write_binding_sha256": write_binding,
        "state": "awaiting_write_approval",
        "created_at_utc": now_utc,
        "recovery_receipt_sha256": "",
    }
    recovery_receipt["recovery_receipt_sha256"] = _self_hash(
        recovery_receipt, "recovery_receipt_sha256"
    )
    receipt_directory.mkdir(parents=True, exist_ok=True)
    _write_json(receipt_directory / "operator-timeout-recovery-receipt.json", recovery_receipt)
    _write_json(receipt_directory / "recovery-selection-receipt.json", selection_receipt)
    _write_json(receipt_directory / "recovery-review-cohort-receipt.json", review_receipt)
    return session


def run_timeout_recovery_operator(
    *,
    authorization: Mapping[str, Any],
    source_authorization: Mapping[str, Any],
    participants: Mapping[str, Any],
    repository_authorization: Mapping[str, Any],
    credential_descriptor: Mapping[str, Any],
    source_credential_descriptor: Mapping[str, Any],
    runtime: Mapping[str, Any],
    source_runtime: Mapping[str, Any],
    source_selection_receipt: Mapping[str, Any],
    source_review_receipt: Mapping[str, Any],
    timeout_receipt: Mapping[str, Any],
    recovery_checkpoint: Mapping[str, Any],
    plan_text: str,
    source_root: Path,
    github_app_private_key_file: Path,
    provider_key_environment: str,
    owner: str,
    repository: str,
    receipt_directory: Path,
    now_utc: str,
    port: int = 0,
) -> None:
    def publisher_factory() -> GitHubDraftPublisher:
        require_active_execution_authorization(
            authorization=authorization,
            participants=participants,
            repository=repository_authorization,
            credential_descriptor=credential_descriptor,
            runtime=runtime,
            source_root=source_root,
        )
        verify_credential_fingerprints(
            authorization=authorization,
            participants=participants,
            repository=repository_authorization,
            credential_descriptor=credential_descriptor,
            runtime=runtime,
            source_root=source_root,
            github_app_private_key_file=github_app_private_key_file,
            provider_key_environment=provider_key_environment,
        )
        private_key = _read_private_key_file(
            github_app_private_key_file,
            expected_sha256=credential_descriptor[
                "github_app_private_key_fingerprint_sha256"
            ],
        )
        transport = StrictHttpsJsonTransport(allowed_hosts=frozenset({_GITHUB_HOST}))
        token = GitHubAppAuthenticator(transport).mint_installation_token(
            app_id=credential_descriptor["github_app_id"],
            installation_id=credential_descriptor["github_app_installation_id"],
            private_key=private_key,
        )
        del private_key
        required = authorization["required_fields"]
        repository_ids = required["repository_allowlist"]["repository_ids"]
        return GitHubDraftPublisher(
            transport,
            authorization=authorization,
            participants=participants,
            repository_authorization=repository_authorization,
            credential_descriptor=credential_descriptor,
            runtime=runtime,
            source_root=source_root,
            github_app_private_key_file=github_app_private_key_file,
            provider_key_environment=provider_key_environment,
            token=token,
            owner=owner,
            repository=repository,
            expected_repository_id=_github_numeric_repository_id(repository_ids[0]),
            expected_app_id=credential_descriptor["github_app_id"],
            expected_installation_id=credential_descriptor["github_app_installation_id"],
            journal=PublicationJournal(receipt_directory / "publication-journal.json"),
        )

    session = prepare_timeout_recovery_operator_session(
        authorization=authorization,
        source_authorization=source_authorization,
        participants=participants,
        repository_authorization=repository_authorization,
        credential_descriptor=credential_descriptor,
        source_credential_descriptor=source_credential_descriptor,
        runtime=runtime,
        source_runtime=source_runtime,
        source_selection_receipt=source_selection_receipt,
        source_review_receipt=source_review_receipt,
        timeout_receipt=timeout_receipt,
        recovery_checkpoint=recovery_checkpoint,
        plan_text=plan_text,
        source_root=source_root,
        receipt_directory=receipt_directory,
        publisher_factory=publisher_factory,
        now_utc=now_utc,
    )
    server = LoopbackReviewRepairServer(session, port=port)
    try:
        host, actual_port = server.address
        print(
            json.dumps(
                {
                    "operator_url": f"http://{host}:{actual_port}",
                    "bearer_token": server.bearer_token,
                    "state": session.status()["state"],
                    "write_binding_sha256": session.status()["write_binding_sha256"],
                    "checkpoint_sha256": recovery_checkpoint["checkpoint_sha256"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        server.serve_until_terminal()
    finally:
        server.close()


def run_review_repair_operator(
    *,
    authorization: Mapping[str, Any],
    participants: Mapping[str, Any],
    repository_authorization: Mapping[str, Any],
    credential_descriptor: Mapping[str, Any],
    runtime: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    source_root: Path,
    github_app_private_key_file: Path,
    provider_key_environment: str,
    owner: str,
    repository: str,
    receipt_directory: Path,
    now_utc: str,
    port: int = 0,
) -> None:
    """Run a fresh Review cohort and retain its readable findings for one repair."""
    findings: list[EphemeralReviewFinding] = []

    def retain(rows: tuple[EphemeralReviewFinding, ...]) -> None:
        findings.extend(rows)

    review_receipt = execute_authorized_reviews(
        authorization=authorization,
        participants=participants,
        repository_authorization=repository_authorization,
        credential_descriptor=credential_descriptor,
        runtime=runtime,
        selection_receipt=selection_receipt,
        source_root=source_root,
        github_app_private_key_file=github_app_private_key_file,
        provider_key_environment=provider_key_environment,
        owner=owner,
        repository=repository,
        now_utc=now_utc,
        finding_sink=retain,
    )
    receipt_directory = receipt_directory.resolve()
    resolved_source = source_root.resolve()
    if receipt_directory == resolved_source or resolved_source in receipt_directory.parents:
        raise GateBExecutorError("operator_receipt_directory_inside_source_tree")
    receipt_directory.mkdir(parents=True, exist_ok=True)
    _write_json(receipt_directory / "review-cohort-receipt.json", review_receipt)
    if review_receipt["stop_category"] != "none":
        raise GateBExecutorError("operator_review_cohort_incomplete")
    if not findings:
        raise GateBExecutorError("operator_no_findings")
    coordinator = GateBRepairCoordinator(
        authorization=authorization,
        participants=participants,
        repository=repository_authorization,
        credential_descriptor=credential_descriptor,
        runtime=runtime,
        source_root=source_root,
        selection_receipt=selection_receipt,
        now_utc=now_utc,
    )

    def publisher_factory() -> GitHubDraftPublisher:
        require_active_execution_authorization(
            authorization=authorization,
            participants=participants,
            repository=repository_authorization,
            credential_descriptor=credential_descriptor,
            runtime=runtime,
            source_root=source_root,
        )
        verify_credential_fingerprints(
            authorization=authorization,
            participants=participants,
            repository=repository_authorization,
            credential_descriptor=credential_descriptor,
            runtime=runtime,
            source_root=source_root,
            github_app_private_key_file=github_app_private_key_file,
            provider_key_environment=provider_key_environment,
        )
        private_key = _read_private_key_file(
            github_app_private_key_file,
            expected_sha256=credential_descriptor[
                "github_app_private_key_fingerprint_sha256"
            ],
        )
        transport = StrictHttpsJsonTransport(allowed_hosts=frozenset({_GITHUB_HOST}))
        token = GitHubAppAuthenticator(transport).mint_installation_token(
            app_id=credential_descriptor["github_app_id"],
            installation_id=credential_descriptor["github_app_installation_id"],
            private_key=private_key,
        )
        del private_key
        required = authorization.get("required_fields")
        if not isinstance(required, Mapping):
            raise GateBExecutorError("authorization_required_fields_invalid")
        repository_allowlist = required.get("repository_allowlist")
        if not isinstance(repository_allowlist, Mapping):
            raise GateBExecutorError("repository_allowlist_invalid")
        repository_ids = repository_allowlist.get("repository_ids")
        if not isinstance(repository_ids, list) or len(repository_ids) != 1:
            raise GateBExecutorError("repository_allowlist_invalid")
        return GitHubDraftPublisher(
            transport,
            authorization=authorization,
            participants=participants,
            repository_authorization=repository_authorization,
            credential_descriptor=credential_descriptor,
            runtime=runtime,
            source_root=source_root,
            github_app_private_key_file=github_app_private_key_file,
            provider_key_environment=provider_key_environment,
            token=token,
            owner=owner,
            repository=repository,
            expected_repository_id=_github_numeric_repository_id(repository_ids[0]),
            expected_app_id=credential_descriptor["github_app_id"],
            expected_installation_id=credential_descriptor["github_app_installation_id"],
            journal=PublicationJournal(receipt_directory / "publication-journal.json"),
        )

    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    session = ReviewRepairOperatorSession(
        coordinator=coordinator,
        authorization=authorization,
        selection_receipt=selection_receipt,
        review_receipt=review_receipt,
        findings=findings,
        receipt_directory=receipt_directory,
        source_root=source_root,
        publisher_factory=publisher_factory,
        started_at_utc=now_utc,
        timeout_seconds=_require_int(
            "max_wall_clock_seconds", required.get("max_wall_clock_seconds"), minimum=1
        ),
    )
    server = LoopbackReviewRepairServer(session, port=port)
    try:
        host, actual_port = server.address
        print(
            json.dumps(
                {
                    "operator_url": f"http://{host}:{actual_port}",
                    "bearer_token": server.bearer_token,
                    "finding_count": len(findings),
                    "review_cohort_receipt_sha256": review_receipt[
                        "review_cohort_receipt_sha256"
                    ],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        server.serve_until_terminal()
    finally:
        findings.clear()
        server.close()


PILOT_FEEDBACK_SCHEMA_VERSION = "crag.phase11d.pilot-feedback/v1alpha1"
PILOT_TIME_COST_SCHEMA_VERSION = "crag.phase11d.pilot-time-cost/v1alpha1"
PILOT_BUSINESS_REPORT_SCHEMA_VERSION = "crag.phase11d.pilot-business-report/v1alpha1"
PILOT_CLAIM_DECISION_SCHEMA_VERSION = "crag.phase11d.pilot-claim-decision/v1alpha1"
PILOT_ACCEPTANCE_SCHEMA_VERSION = "crag.phase11d.pilot-final-acceptance/v1alpha1"
PILOT_MANIFEST_SCHEMA_VERSION = "crag.phase11d.pilot-canonical-manifest/v1alpha1"
PILOT_FINAL_APPROVAL_PREFIX = "PHASE11D_PILOT_FINAL_SIGNOFF_V1"
_PILOT_FEEDBACK_DECISIONS = frozenset({"accepted", "rejected", "deferred"})


def _validate_repair_receipt(value: Mapping[str, Any]) -> None:
    receipt_sha = _require_sha256(
        "repair_receipt_sha256", value.get("repair_receipt_sha256")
    )
    if _self_hash(value, "repair_receipt_sha256") != receipt_sha:
        raise GateBExecutorError("repair_receipt_hash_mismatch")
    _require_stable_id("repair_receipt_job_id", value.get("repair_job_id"))
    _require_stable_id("repair_receipt_pr_id", value.get("pr_id"))
    _require_sha256("repair_receipt_finding_id", value.get("finding_id"))
    draft_sha = _require_sha256(
        "repair_draft_pr_receipt_sha256", value.get("draft_pr_receipt_sha256")
    )
    if draft_sha == "0" * 64:
        raise GateBExecutorError("repair_draft_pr_receipt_missing")


def _validate_draft_pr_receipt(value: Mapping[str, Any]) -> None:
    receipt_sha = _require_sha256("draft_pr_receipt_sha256", value.get("receipt_sha256"))
    if _self_hash(value, "receipt_sha256") != receipt_sha:
        raise GateBExecutorError("draft_pr_receipt_hash_mismatch")
    if (
        value.get("draft") is not True
        or value.get("ready") is not False
        or value.get("merged") is not False
        or value.get("publisher_status") != "draft_published"
    ):
        raise GateBExecutorError("draft_pr_boundary_violation")


def prepare_pilot_closeout(
    *,
    authorization: Mapping[str, Any],
    participants: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    review_receipt: Mapping[str, Any],
    repair_receipt: Mapping[str, Any],
    draft_pr_receipt: Mapping[str, Any],
    feedback: Mapping[str, Any],
    output_directory: Path,
) -> dict[str, Any]:
    """Prepare post-publication evidence and the exact owner sign-off text."""
    required = authorization.get("required_fields")
    if not isinstance(required, Mapping):
        raise GateBExecutorError("authorization_required_fields_invalid")
    _validate_participants(required, participants)
    validate_review_cohort_receipt(
        review_receipt,
        authorization=authorization,
        selection_receipt=selection_receipt,
    )
    _validate_repair_receipt(repair_receipt)
    _validate_draft_pr_receipt(draft_pr_receipt)
    if review_receipt.get("stop_category") != "none":
        raise GateBExecutorError("closeout_review_incomplete")
    if draft_pr_receipt.get("repair_job_id") != repair_receipt.get("repair_job_id"):
        raise GateBExecutorError("closeout_repair_job_mismatch")
    if draft_pr_receipt.get("pr_id") != repair_receipt.get("pr_id"):
        raise GateBExecutorError("closeout_pr_mismatch")
    if draft_pr_receipt.get("receipt_sha256") != repair_receipt.get(
        "draft_pr_receipt_sha256"
    ):
        raise GateBExecutorError("closeout_draft_receipt_mismatch")
    feedback_fields = frozenset(
        {
            "actor_id",
            "finding_id",
            "decision",
            "repair_requested",
            "draft_pr_adopted",
            "active_review_seconds",
            "paused_review_seconds",
            "rationale_text",
            "submitted_at_utc",
        }
    )
    _require_exact_fields("pilot_feedback_input", feedback, feedback_fields)
    actor_id, actor_role = _resolve_human_actor(participants, feedback["actor_id"])
    finding_id = _require_sha256("pilot_feedback_finding_id", feedback["finding_id"])
    if finding_id != repair_receipt.get("finding_id"):
        raise GateBExecutorError("pilot_feedback_finding_mismatch")
    decision = feedback["decision"]
    if decision not in _PILOT_FEEDBACK_DECISIONS:
        raise GateBExecutorError("pilot_feedback_decision_invalid")
    repair_requested = _require_bool(
        "pilot_feedback_repair_requested", feedback["repair_requested"]
    )
    draft_pr_adopted = _require_bool(
        "pilot_feedback_draft_pr_adopted", feedback["draft_pr_adopted"]
    )
    active_seconds = _require_int(
        "pilot_feedback_active_review_seconds", feedback["active_review_seconds"]
    )
    paused_seconds = _require_int(
        "pilot_feedback_paused_review_seconds", feedback["paused_review_seconds"]
    )
    submitted_at = feedback["submitted_at_utc"]
    _require_utc("pilot_feedback_submitted_at", submitted_at)
    rationale_sha = _hash_ephemeral_text(
        "pilot_feedback_rationale", feedback["rationale_text"], maximum_bytes=16_000
    )
    feedback_row: dict[str, Any] = {
        "schema_version": PILOT_FEEDBACK_SCHEMA_VERSION,
        "repair_job_id": repair_receipt["repair_job_id"],
        "pr_id": repair_receipt["pr_id"],
        "finding_id": finding_id,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "actor_method": "human",
        "decision": decision,
        "repair_requested": repair_requested,
        "draft_pr_adopted": draft_pr_adopted,
        "rationale_sha256": rationale_sha,
        "submitted_at_utc": submitted_at,
        "human_attested": True,
        "feedback_receipt_sha256": "",
    }
    feedback_row["feedback_receipt_sha256"] = _self_hash(
        feedback_row, "feedback_receipt_sha256"
    )
    budget_usage = review_receipt.get("budget_usage")
    if not isinstance(budget_usage, Mapping):
        raise GateBExecutorError("review_budget_usage_invalid")
    time_cost: dict[str, Any] = {
        "schema_version": PILOT_TIME_COST_SCHEMA_VERSION,
        "repair_job_id": repair_receipt["repair_job_id"],
        "pr_id": repair_receipt["pr_id"],
        "active_review_seconds": active_seconds,
        "paused_review_seconds": paused_seconds,
        "review_cost_micro_cny": _require_int(
            "review_cost_micro_cny", budget_usage.get("micro_cny")
        ),
        "provider_call_count": _require_int(
            "review_provider_call_count", budget_usage.get("logical_calls")
        ),
        "recorded_at_utc": submitted_at,
        "human_attested": True,
        "time_cost_receipt_sha256": "",
    }
    time_cost["time_cost_receipt_sha256"] = _self_hash(
        time_cost, "time_cost_receipt_sha256"
    )
    review_rows = review_receipt.get("review_rows")
    assert isinstance(review_rows, list)
    completed = sum(row.get("status") == "completed" for row in review_rows if isinstance(row, Mapping))
    finding_count = sum(
        len(row.get("finding_ids", [])) for row in review_rows if isinstance(row, Mapping)
    )
    business: dict[str, Any] = {
        "schema_version": PILOT_BUSINESS_REPORT_SCHEMA_VERSION,
        "authorization_id": review_receipt["authorization_id"],
        "selected_pr_count": review_receipt["selected_pr_count"],
        "completed_review_count": completed,
        "finding_count": finding_count,
        "repair_count": 1,
        "draft_pr_count": 1,
        "feedback_count": 1,
        "draft_pr_adopted_count": int(draft_pr_adopted),
        "active_review_seconds": active_seconds,
        "review_cost_micro_cny": time_cost["review_cost_micro_cny"],
        "business_claim_allowed": False,
        "report_sha256": "",
    }
    business["report_sha256"] = _self_hash(business, "report_sha256")
    claim: dict[str, Any] = {
        "schema_version": PILOT_CLAIM_DECISION_SCHEMA_VERSION,
        "business_claim_allowed": False,
        "quality_claim_allowed": False,
        "model_quality_status": "not_measured",
        "formal_quality_status": "incomplete",
        "production_ready": False,
        "decision_reason": "pilot_evidence_is_not_formal_quality_or_production_evidence",
        "claim_decision_sha256": "",
    }
    claim["claim_decision_sha256"] = _self_hash(claim, "claim_decision_sha256")
    binding = sha256_bytes(
        canonical_json(
            {
                "authorization_sha256": review_receipt[
                    "canonical_authorization_sha256"
                ],
                "review_receipt_sha256": review_receipt[
                    "review_cohort_receipt_sha256"
                ],
                "repair_receipt_sha256": repair_receipt["repair_receipt_sha256"],
                "draft_pr_receipt_sha256": draft_pr_receipt["receipt_sha256"],
                "feedback_receipt_sha256": feedback_row["feedback_receipt_sha256"],
                "time_cost_receipt_sha256": time_cost["time_cost_receipt_sha256"],
                "business_report_sha256": business["report_sha256"],
                "claim_decision_sha256": claim["claim_decision_sha256"],
            }
        )
    )
    exact_text = "\n".join(
        (
            PILOT_FINAL_APPROVAL_PREFIX,
            f"authorization_id={review_receipt['authorization_id']}",
            f"closeout_binding_sha256={binding}",
            f"draft_pr_id={draft_pr_receipt['draft_pr_id']}",
            "phase11d_pilot_complete=true",
            "business_claim_allowed=false",
            "quality_claim_allowed=false",
            "production_ready=false",
            "model_quality_status=not_measured",
            "formal_quality_status=incomplete",
            "",
        )
    )
    acceptance_draft: dict[str, Any] = {
        "schema_version": PILOT_ACCEPTANCE_SCHEMA_VERSION,
        "closeout_binding_sha256": binding,
        "exact_approval_text_sha256": sha256_text(exact_text),
        "participant_manifest_sha256": participants["manifest_sha256"],
        "owner_signoff": None,
        "phase11d_pilot_complete": False,
        "business_claim_allowed": False,
        "quality_claim_allowed": False,
        "production_ready": False,
        "model_quality_status": "not_measured",
        "formal_quality_status": "incomplete",
        "final_project_complete": False,
        "acceptance_sha256": "",
    }
    acceptance_draft["acceptance_sha256"] = _self_hash(
        acceptance_draft, "acceptance_sha256"
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    outputs = {
        "feedback-receipt.json": feedback_row,
        "time-cost-receipt.json": time_cost,
        "business-report.json": business,
        "claim-decision-report.json": claim,
        "final-acceptance-draft.json": acceptance_draft,
    }
    for name, value in outputs.items():
        _write_json(output_directory / name, value)
    try:
        (output_directory / "final-acceptance-exact-approval.txt").write_text(
            exact_text, encoding="utf-8", newline="\n"
        )
    except OSError as exc:
        raise GateBExecutorError("closeout_exact_approval_unavailable") from exc
    return {
        "closeout_binding_sha256": binding,
        "exact_approval_text_sha256": sha256_text(exact_text),
        "exact_approval_text": exact_text,
    }


def approve_pilot_closeout(
    *,
    participants: Mapping[str, Any],
    output_directory: Path,
    actor_id: str,
    approved_at_utc: str,
    exact_approval_text: str,
) -> dict[str, Any]:
    """Apply one exact org-admin sign-off and freeze the closeout manifest."""
    actor, role = _resolve_human_actor(participants, actor_id)
    if role != "org_admin":
        raise GateBExecutorError("closeout_owner_role_denied")
    _require_utc("closeout_approved_at", approved_at_utc)
    draft = load_json(output_directory / "final-acceptance-draft.json")
    if _self_hash(draft, "acceptance_sha256") != draft.get("acceptance_sha256"):
        raise GateBExecutorError("closeout_acceptance_draft_hash_mismatch")
    if _participant_manifest_hash(participants) != draft.get("participant_manifest_sha256"):
        raise GateBExecutorError("closeout_participant_manifest_mismatch")
    if draft.get("owner_signoff") is not None or draft.get("phase11d_pilot_complete") is not False:
        raise GateBExecutorError("closeout_approval_replay")
    expected_text_path = output_directory / "final-acceptance-exact-approval.txt"
    try:
        expected_text = expected_text_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateBExecutorError("closeout_exact_approval_unavailable") from exc
    if (
        not secrets.compare_digest(exact_approval_text, expected_text)
        or sha256_text(exact_approval_text) != draft.get("exact_approval_text_sha256")
    ):
        raise GateBExecutorError("closeout_exact_approval_mismatch")
    final = dict(draft)
    final["owner_signoff"] = {
        "actor_id": actor,
        "actor_role": role,
        "actor_method": "human",
        "decision": "approved",
        "binding_sha256": draft["closeout_binding_sha256"],
        "approved_at_utc": approved_at_utc,
        "exact_approval_text_sha256": draft["exact_approval_text_sha256"],
    }
    final["phase11d_pilot_complete"] = True
    final["acceptance_sha256"] = ""
    final["acceptance_sha256"] = _self_hash(final, "acceptance_sha256")
    _write_json(output_directory / "final-acceptance-report.json", final)
    artifact_names = (
        "review-cohort-receipt.json",
        "repair-receipt.json",
        "draft-pr-receipt.json",
        "operator-session-receipt.json",
        "feedback-receipt.json",
        "time-cost-receipt.json",
        "business-report.json",
        "claim-decision-report.json",
        "final-acceptance-report.json",
        "final-acceptance-exact-approval.txt",
    )
    artifacts: list[dict[str, str]] = []
    for name in artifact_names:
        path = output_directory / name
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise GateBExecutorError("closeout_artifact_missing") from exc
        artifacts.append({"name": name, "sha256": sha256_bytes(content)})
    manifest: dict[str, Any] = {
        "schema_version": PILOT_MANIFEST_SCHEMA_VERSION,
        "closeout_binding_sha256": draft["closeout_binding_sha256"],
        "artifacts": artifacts,
        "model_quality_status": "not_measured",
        "formal_quality_status": "incomplete",
        "production_ready": False,
        "manifest_sha256": "",
    }
    manifest["manifest_sha256"] = _self_hash(manifest, "manifest_sha256")
    _write_json(output_directory / "canonical-manifest.json", manifest)
    return {
        "phase11d_pilot_complete": True,
        "acceptance_sha256": final["acceptance_sha256"],
        "manifest_sha256": manifest["manifest_sha256"],
    }


def _load_artifacts(args: argparse.Namespace) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    return (
        load_json(args.authorization),
        load_json(args.participants),
        load_json(args.repository_authorization),
        load_json(args.credential_descriptor),
        load_json(args.runtime),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 11D default-closed Gate B executor controls")
    commands = parser.add_subparsers(dest="command", required=True)

    runtime = commands.add_parser("freeze-runtime")
    runtime.add_argument("--source-root", type=Path, required=True)
    runtime.add_argument("--authorization-id", required=True)
    runtime.add_argument("--executor-id", required=True)
    runtime.add_argument("--output", type=Path, required=True)

    freeze = commands.add_parser("freeze-authorization")
    freeze.add_argument("--draft", type=Path, required=True)
    freeze.add_argument("--participants", type=Path, required=True)
    freeze.add_argument("--repository-authorization", type=Path, required=True)
    freeze.add_argument("--credential-descriptor", type=Path, required=True)
    freeze.add_argument("--runtime", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)

    approve = commands.add_parser("approve-authorization")
    approve.add_argument("--authorization", type=Path, required=True)
    approve.add_argument("--participants", type=Path, required=True)
    approve.add_argument("--actor-id", required=True)
    approve.add_argument("--approved-at-utc", required=True)
    approve.add_argument("--exact-approval-text-file", type=Path, required=True)
    approve.add_argument("--output", type=Path, required=True)

    validate = commands.add_parser("validate-authorization")
    validate.add_argument("--authorization", type=Path, required=True)
    validate.add_argument("--participants", type=Path, required=True)
    validate.add_argument("--repository-authorization", type=Path, required=True)
    validate.add_argument("--credential-descriptor", type=Path, required=True)
    validate.add_argument("--runtime", type=Path, required=True)
    validate.add_argument("--now-utc")

    verify = commands.add_parser("verify-credential-fingerprints")
    verify.add_argument("--authorization", type=Path, required=True)
    verify.add_argument("--participants", type=Path, required=True)
    verify.add_argument("--repository-authorization", type=Path, required=True)
    verify.add_argument("--credential-descriptor", type=Path, required=True)
    verify.add_argument("--runtime", type=Path, required=True)
    verify.add_argument("--source-root", type=Path, required=True)
    verify.add_argument("--github-app-private-key-file", type=Path, required=True)
    verify.add_argument("--provider-key-environment", required=True)

    selection = commands.add_parser("select-pull-requests")
    selection.add_argument("--authorization", type=Path, required=True)
    selection.add_argument("--participants", type=Path, required=True)
    selection.add_argument("--repository-authorization", type=Path, required=True)
    selection.add_argument("--credential-descriptor", type=Path, required=True)
    selection.add_argument("--runtime", type=Path, required=True)
    selection.add_argument("--source-root", type=Path, required=True)
    selection.add_argument("--github-app-private-key-file", type=Path, required=True)
    selection.add_argument("--provider-key-environment", required=True)
    selection.add_argument("--owner", required=True)
    selection.add_argument("--repository", required=True)
    selection.add_argument("--now-utc")
    selection.add_argument("--output", type=Path, required=True)

    review = commands.add_parser("review-selected-pull-requests")
    review.add_argument("--authorization", type=Path, required=True)
    review.add_argument("--participants", type=Path, required=True)
    review.add_argument("--repository-authorization", type=Path, required=True)
    review.add_argument("--credential-descriptor", type=Path, required=True)
    review.add_argument("--runtime", type=Path, required=True)
    review.add_argument("--selection-receipt", type=Path, required=True)
    review.add_argument("--source-root", type=Path, required=True)
    review.add_argument("--github-app-private-key-file", type=Path, required=True)
    review.add_argument("--provider-key-environment", required=True)
    review.add_argument("--owner", required=True)
    review.add_argument("--repository", required=True)
    review.add_argument("--now-utc")
    review.add_argument("--output", type=Path, required=True)

    resume = commands.add_parser("resume-selected-pull-requests")
    resume.add_argument("--authorization", type=Path, required=True)
    resume.add_argument("--participants", type=Path, required=True)
    resume.add_argument("--repository-authorization", type=Path, required=True)
    resume.add_argument("--credential-descriptor", type=Path, required=True)
    resume.add_argument("--runtime", type=Path, required=True)
    resume.add_argument("--selection-receipt", type=Path, required=True)
    resume.add_argument("--previous-authorization", type=Path, required=True)
    resume.add_argument("--previous-selection-receipt", type=Path, required=True)
    resume.add_argument("--previous-review-receipt", type=Path, required=True)
    resume.add_argument("--source-root", type=Path, required=True)
    resume.add_argument("--github-app-private-key-file", type=Path, required=True)
    resume.add_argument("--provider-key-environment", required=True)
    resume.add_argument("--owner", required=True)
    resume.add_argument("--repository", required=True)
    resume.add_argument("--now-utc")
    resume.add_argument("--output", type=Path, required=True)

    operator = commands.add_parser("run-review-repair-session")
    operator.add_argument("--authorization", type=Path, required=True)
    operator.add_argument("--participants", type=Path, required=True)
    operator.add_argument("--repository-authorization", type=Path, required=True)
    operator.add_argument("--credential-descriptor", type=Path, required=True)
    operator.add_argument("--runtime", type=Path, required=True)
    operator.add_argument("--selection-receipt", type=Path, required=True)
    operator.add_argument("--source-root", type=Path, required=True)
    operator.add_argument("--github-app-private-key-file", type=Path, required=True)
    operator.add_argument("--provider-key-environment", required=True)
    operator.add_argument("--owner", required=True)
    operator.add_argument("--repository", required=True)
    operator.add_argument("--receipt-directory", type=Path, required=True)
    operator.add_argument("--port", type=int, default=0)
    operator.add_argument("--now-utc")

    recovery_checkpoint = commands.add_parser("build-timeout-recovery-checkpoint")
    recovery_checkpoint.add_argument("--source-authorization", type=Path, required=True)
    recovery_checkpoint.add_argument("--participants", type=Path, required=True)
    recovery_checkpoint.add_argument(
        "--repository-authorization", type=Path, required=True
    )
    recovery_checkpoint.add_argument(
        "--source-credential-descriptor", type=Path, required=True
    )
    recovery_checkpoint.add_argument("--source-runtime", type=Path, required=True)
    recovery_checkpoint.add_argument("--selection-receipt", type=Path, required=True)
    recovery_checkpoint.add_argument("--review-receipt", type=Path, required=True)
    recovery_checkpoint.add_argument("--timeout-receipt", type=Path, required=True)
    recovery_checkpoint.add_argument("--selected-finding-id", required=True)
    recovery_checkpoint.add_argument("--prior-selection-sha256", required=True)
    recovery_checkpoint.add_argument("--prior-plan-sha256", required=True)
    recovery_checkpoint.add_argument("--prior-write-binding-sha256", required=True)
    recovery_checkpoint.add_argument("--prior-write-approval-id", required=True)
    recovery_checkpoint.add_argument("--prior-write-approved-at-utc", required=True)
    recovery_checkpoint.add_argument("--recovery-actor-id", required=True)
    recovery_checkpoint.add_argument("--recovery-selection-id", required=True)
    recovery_checkpoint.add_argument("--recovery-repair-job-id", required=True)
    recovery_checkpoint.add_argument("--recovery-write-approval-id", required=True)
    recovery_checkpoint.add_argument("--recovery-requested-at-utc", required=True)
    recovery_checkpoint.add_argument("--output", type=Path, required=True)

    recovery = commands.add_parser("resume-write-approved-repair-session")
    recovery.add_argument("--authorization", type=Path, required=True)
    recovery.add_argument("--source-authorization", type=Path, required=True)
    recovery.add_argument("--participants", type=Path, required=True)
    recovery.add_argument("--repository-authorization", type=Path, required=True)
    recovery.add_argument("--credential-descriptor", type=Path, required=True)
    recovery.add_argument("--source-credential-descriptor", type=Path, required=True)
    recovery.add_argument("--runtime", type=Path, required=True)
    recovery.add_argument("--source-runtime", type=Path, required=True)
    recovery.add_argument("--selection-receipt", type=Path, required=True)
    recovery.add_argument("--review-receipt", type=Path, required=True)
    recovery.add_argument("--timeout-receipt", type=Path, required=True)
    recovery.add_argument("--recovery-checkpoint", type=Path, required=True)
    recovery.add_argument("--plan-file", type=Path, required=True)
    recovery.add_argument("--source-root", type=Path, required=True)
    recovery.add_argument("--github-app-private-key-file", type=Path, required=True)
    recovery.add_argument("--provider-key-environment", required=True)
    recovery.add_argument("--owner", required=True)
    recovery.add_argument("--repository", required=True)
    recovery.add_argument("--receipt-directory", type=Path, required=True)
    recovery.add_argument("--port", type=int, default=0)
    recovery.add_argument("--now-utc")

    closeout = commands.add_parser("prepare-pilot-closeout")
    closeout.add_argument("--authorization", type=Path, required=True)
    closeout.add_argument("--participants", type=Path, required=True)
    closeout.add_argument("--selection-receipt", type=Path, required=True)
    closeout.add_argument("--review-receipt", type=Path, required=True)
    closeout.add_argument("--repair-receipt", type=Path, required=True)
    closeout.add_argument("--draft-pr-receipt", type=Path, required=True)
    closeout.add_argument("--feedback", type=Path, required=True)
    closeout.add_argument("--output-directory", type=Path, required=True)

    final = commands.add_parser("approve-pilot-closeout")
    final.add_argument("--participants", type=Path, required=True)
    final.add_argument("--output-directory", type=Path, required=True)
    final.add_argument("--actor-id", required=True)
    final.add_argument("--approved-at-utc", required=True)
    final.add_argument("--exact-approval-text-file", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "freeze-runtime":
            runtime = freeze_executor_runtime(
                source_root=args.source_root,
                authorization_id=args.authorization_id,
                executor_id=args.executor_id,
            )
            _write_json(args.output, runtime)
            print(
                json.dumps(
                    {
                        "execution_capability": runtime["execution_capability"],
                        "runtime_sha256": runtime["runtime_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "freeze-authorization":
            draft = load_json(args.draft)
            participants = load_json(args.participants)
            repository = load_json(args.repository_authorization)
            descriptor = load_json(args.credential_descriptor)
            runtime = load_json(args.runtime)
            frozen = freeze_authorization(
                draft=draft,
                participants=participants,
                repository=repository,
                credential_descriptor=descriptor,
                runtime=runtime,
            )
            _write_json(args.output, frozen)
            required = frozen["required_fields"]
            assert isinstance(required, Mapping)
            print(
                json.dumps(
                    {
                        "canonical_authorization_sha256": required[
                            "canonical_authorization_sha256"
                        ],
                        "exact_approval_text_sha256": sha256_text(frozen["exact_approval_text"]),
                        "gate_b_allowed": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "approve-authorization":
            authorization = load_json(args.authorization)
            participants = load_json(args.participants)
            try:
                exact_text = args.exact_approval_text_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise GateBExecutorError("exact_approval_text_unavailable") from exc
            approved = approve_authorization(
                frozen=authorization,
                participants=participants,
                actor_id=args.actor_id,
                approved_at_utc=args.approved_at_utc,
                exact_approval_text=exact_text,
            )
            _write_json(args.output, approved)
            required = approved["required_fields"]
            assert isinstance(required, Mapping)
            print(
                json.dumps(
                    {
                        "canonical_authorization_sha256": required[
                            "canonical_authorization_sha256"
                        ],
                        "gate_b_allowed": True,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "prepare-pilot-closeout":
            result = prepare_pilot_closeout(
                authorization=load_json(args.authorization),
                participants=load_json(args.participants),
                selection_receipt=load_json(args.selection_receipt),
                review_receipt=load_json(args.review_receipt),
                repair_receipt=load_json(args.repair_receipt),
                draft_pr_receipt=load_json(args.draft_pr_receipt),
                feedback=load_json(args.feedback),
                output_directory=args.output_directory,
            )
            print(
                json.dumps(
                    {
                        "closeout_binding_sha256": result["closeout_binding_sha256"],
                        "exact_approval_text_sha256": result[
                            "exact_approval_text_sha256"
                        ],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "approve-pilot-closeout":
            try:
                exact_text = args.exact_approval_text_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise GateBExecutorError("closeout_exact_approval_unavailable") from exc
            result = approve_pilot_closeout(
                participants=load_json(args.participants),
                output_directory=args.output_directory,
                actor_id=args.actor_id,
                approved_at_utc=args.approved_at_utc,
                exact_approval_text=exact_text,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "build-timeout-recovery-checkpoint":
            source_authorization = load_json(args.source_authorization)
            participants = load_json(args.participants)
            repository = load_json(args.repository_authorization)
            source_descriptor = load_json(args.source_credential_descriptor)
            source_runtime = load_json(args.source_runtime)
            status = validate_execution_authorization(
                authorization=source_authorization,
                participants=participants,
                repository=repository,
                credential_descriptor=source_descriptor,
                runtime=source_runtime,
                now_utc=args.prior_write_approved_at_utc,
            )
            if not status.gate_b_allowed:
                print(json.dumps(status.to_dict(), sort_keys=True))
                return 2
            checkpoint = build_timeout_recovery_checkpoint(
                source_authorization=source_authorization,
                source_runtime=source_runtime,
                selection_receipt=load_json(args.selection_receipt),
                review_receipt=load_json(args.review_receipt),
                timeout_receipt=load_json(args.timeout_receipt),
                selected_finding_id=args.selected_finding_id,
                prior_selection_sha256=args.prior_selection_sha256,
                prior_plan_sha256=args.prior_plan_sha256,
                prior_write_binding_sha256=args.prior_write_binding_sha256,
                prior_write_approval_id=args.prior_write_approval_id,
                prior_write_approved_at_utc=args.prior_write_approved_at_utc,
                recovery_actor_id=args.recovery_actor_id,
                recovery_selection_id=args.recovery_selection_id,
                recovery_repair_job_id=args.recovery_repair_job_id,
                recovery_write_approval_id=args.recovery_write_approval_id,
                recovery_requested_at_utc=args.recovery_requested_at_utc,
            )
            _write_json(args.output, checkpoint)
            print(
                json.dumps(
                    {
                        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
                        "selected_finding_id": checkpoint["selected_finding_id"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        authorization, participants, repository, descriptor, runtime = _load_artifacts(args)
        status = validate_execution_authorization(
            authorization=authorization,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=runtime,
            now_utc=getattr(args, "now_utc", None),
        )
        if args.command == "validate-authorization":
            print(json.dumps(status.to_dict(), sort_keys=True))
            return 0 if status.gate_b_allowed else 2
        if not status.gate_b_allowed:
            print(json.dumps(status.to_dict(), sort_keys=True))
            return 2
        if args.command == "resume-write-approved-repair-session":
            try:
                plan_bytes = args.plan_file.read_bytes()
            except OSError as exc:
                raise GateBExecutorError("recovery_plan_unavailable") from exc
            if not 1 <= len(plan_bytes) <= 64_000:
                raise GateBExecutorError("recovery_plan_invalid")
            try:
                plan_text = plan_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GateBExecutorError("recovery_plan_invalid") from exc
            now_utc = getattr(args, "now_utc", None) or datetime.now(
                timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            run_timeout_recovery_operator(
                authorization=authorization,
                source_authorization=load_json(args.source_authorization),
                participants=participants,
                repository_authorization=repository,
                credential_descriptor=descriptor,
                source_credential_descriptor=load_json(
                    args.source_credential_descriptor
                ),
                runtime=runtime,
                source_runtime=load_json(args.source_runtime),
                source_selection_receipt=load_json(args.selection_receipt),
                source_review_receipt=load_json(args.review_receipt),
                timeout_receipt=load_json(args.timeout_receipt),
                recovery_checkpoint=load_json(args.recovery_checkpoint),
                plan_text=plan_text,
                source_root=args.source_root,
                github_app_private_key_file=args.github_app_private_key_file,
                provider_key_environment=args.provider_key_environment,
                owner=args.owner,
                repository=args.repository,
                receipt_directory=args.receipt_directory,
                now_utc=now_utc,
                port=args.port,
            )
            return 0
        if args.command == "run-review-repair-session":
            now_utc = getattr(args, "now_utc", None) or datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            run_review_repair_operator(
                authorization=authorization,
                participants=participants,
                repository_authorization=repository,
                credential_descriptor=descriptor,
                runtime=runtime,
                selection_receipt=load_json(args.selection_receipt),
                source_root=args.source_root,
                github_app_private_key_file=args.github_app_private_key_file,
                provider_key_environment=args.provider_key_environment,
                owner=args.owner,
                repository=args.repository,
                receipt_directory=args.receipt_directory,
                now_utc=now_utc,
                port=args.port,
            )
            return 0
        if args.command == "select-pull-requests":
            selection = select_authorized_pull_requests(
                authorization=authorization,
                participants=participants,
                repository_authorization=repository,
                credential_descriptor=descriptor,
                runtime=runtime,
                source_root=args.source_root,
                github_app_private_key_file=args.github_app_private_key_file,
                provider_key_environment=args.provider_key_environment,
                owner=args.owner,
                repository=args.repository,
                now_utc=getattr(args, "now_utc", None),
            )
            _write_json(args.output, selection)
            print(
                json.dumps(
                    {
                        "selected_pr_count": selection["selected_pr_count"],
                        "selection_receipt_sha256": selection["selection_receipt_sha256"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command in {
            "review-selected-pull-requests",
            "resume-selected-pull-requests",
        }:
            now_utc = getattr(args, "now_utc", None) or datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            previous_authorization = None
            previous_selection_receipt = None
            previous_review_receipt = None
            if args.command == "resume-selected-pull-requests":
                previous_authorization = load_json(args.previous_authorization)
                previous_selection_receipt = load_json(args.previous_selection_receipt)
                previous_review_receipt = load_json(args.previous_review_receipt)
            review_receipt = execute_authorized_reviews(
                authorization=authorization,
                participants=participants,
                repository_authorization=repository,
                credential_descriptor=descriptor,
                runtime=runtime,
                selection_receipt=load_json(args.selection_receipt),
                source_root=args.source_root,
                github_app_private_key_file=args.github_app_private_key_file,
                provider_key_environment=args.provider_key_environment,
                owner=args.owner,
                repository=args.repository,
                now_utc=now_utc,
                previous_authorization=previous_authorization,
                previous_selection_receipt=previous_selection_receipt,
                previous_review_receipt=previous_review_receipt,
            )
            _write_json(args.output, review_receipt)
            print(
                json.dumps(
                    {
                        "review_cohort_receipt_sha256": review_receipt[
                            "review_cohort_receipt_sha256"
                        ],
                        "selected_pr_count": review_receipt["selected_pr_count"],
                        "stop_category": review_receipt["stop_category"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        result = verify_credential_fingerprints(
            authorization=authorization,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=runtime,
            source_root=args.source_root,
            github_app_private_key_file=args.github_app_private_key_file,
            provider_key_environment=args.provider_key_environment,
            now_utc=getattr(args, "now_utc", None),
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except GateBExecutorError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
