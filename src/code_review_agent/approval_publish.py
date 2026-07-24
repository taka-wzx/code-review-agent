"""Offline guarded-publish primitives for the Phase 9D control plane.

The module deliberately has no HTTP, subprocess, GitHub SDK, or credential
dependency. A production GitHub adapter is represented only by a fail-closed
interface until a separately approved integration phase exists.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Protocol


def canonical_json(value: Mapping[str, Any]) -> bytes:
    """Encode an exact, deterministic publisher payload."""
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class PublicationError(RuntimeError):
    """A redacted, stable publication-control failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PublishRequest:
    organization_id: str
    repository_id: str
    review_job_id: str
    repository_alias: str
    pull_request: str
    head_sha: str
    payload: Mapping[str, Any]
    payload_sha256: str
    idempotency_key: str


@dataclass(frozen=True)
class PublishReceipt:
    receipt_id: str


class Publisher(Protocol):
    """Idempotent publisher boundary; implementations must never log payloads."""

    def publish(self, request: PublishRequest) -> PublishReceipt: ...

    def lookup(self, idempotency_key: str) -> PublishReceipt | None: ...


class FakePublisher:
    """Deterministic in-memory fake used by offline tests only."""

    def __init__(self, *, timeout_after_persist: bool = False) -> None:
        self.timeout_after_persist = timeout_after_persist
        self.calls: list[str] = []
        self._receipts: dict[str, PublishReceipt] = {}

    def publish(self, request: PublishRequest) -> PublishReceipt:
        prior = self._receipts.get(request.idempotency_key)
        if prior is not None:
            return prior
        receipt = PublishReceipt(receipt_id=f"fake:{request.idempotency_key[:24]}")
        self._receipts[request.idempotency_key] = receipt
        self.calls.append(request.idempotency_key)
        if self.timeout_after_persist:
            raise TimeoutError("publisher_timeout")
        return receipt

    def lookup(self, idempotency_key: str) -> PublishReceipt | None:
        return self._receipts.get(idempotency_key)


class DryRunPublisher(FakePublisher):
    """Default publisher. It records only synthetic receipts and has no I/O."""

    def publish(self, request: PublishRequest) -> PublishReceipt:
        prior = self._receipts.get(request.idempotency_key)
        if prior is not None:
            return prior
        receipt = PublishReceipt(receipt_id=f"dry-run:{request.idempotency_key[:24]}")
        self._receipts[request.idempotency_key] = receipt
        self.calls.append(request.idempotency_key)
        return receipt


class GitHubPublisher:
    """A deliberately disabled real-publisher interface for this phase."""

    def publish(self, request: PublishRequest) -> PublishReceipt:
        del request
        raise PublicationError("github_publisher_disabled")

    def lookup(self, idempotency_key: str) -> PublishReceipt | None:
        del idempotency_key
        return None
