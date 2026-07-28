"""Fail-closed Draft PR publisher boundary for offline Phase 10 Prep."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Protocol


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_BRANCH = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,253}[A-Za-z0-9])?\Z")


def _required(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _digest(name: str, value: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _branch(name: str, value: str) -> str:
    if (
        not isinstance(value, str)
        or _BRANCH.fullmatch(value) is None
        or ".." in value
        or "//" in value
        or value.endswith(".lock")
        or "/." in value
    ):
        raise ValueError(f"{name} must be a normalized Git branch name")
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class DraftPrPublicationError(RuntimeError):
    """Stable publisher failure that carries no payload or provider message."""

    def __init__(self, code: str) -> None:
        self.code = _required("publication error code", code)
        super().__init__(self.code)


@dataclass(frozen=True)
class DraftPrRequest:
    organization_id: str
    repository_id: str
    repair_job_id: str
    head_branch: str
    base_branch: str
    base_sha: str
    head_sha: str
    commit_sha: str
    title: str
    body: str
    diff_sha256: str
    test_sha256: str
    budget_sha256: str
    payload_sha256: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for name in (
            "organization_id",
            "repository_id",
            "repair_job_id",
            "title",
            "body",
            "idempotency_key",
        ):
            _required(name, getattr(self, name))
        _branch("head_branch", self.head_branch)
        _branch("base_branch", self.base_branch)
        for name in ("base_sha", "head_sha", "commit_sha"):
            value = getattr(self, name)
            if not isinstance(value, str) or _OBJECT_ID.fullmatch(value) is None:
                raise ValueError(f"{name} must be a lowercase Git object id")
        for name in (
            "diff_sha256",
            "test_sha256",
            "budget_sha256",
            "payload_sha256",
        ):
            _digest(name, getattr(self, name))
        if self.payload_sha256 != self.computed_payload_sha256:
            raise ValueError("publisher payload hash does not match the request")

    @property
    def canonical_payload(self) -> dict[str, Any]:
        return {
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "body": self.body,
            "budget_sha256": self.budget_sha256,
            "commit_sha": self.commit_sha,
            "diff_sha256": self.diff_sha256,
            "head_branch": self.head_branch,
            "head_sha": self.head_sha,
            "organization_id": self.organization_id,
            "repair_job_id": self.repair_job_id,
            "repository_id": self.repository_id,
            "test_sha256": self.test_sha256,
            "title": self.title,
        }

    @property
    def computed_payload_sha256(self) -> str:
        return _sha256(_canonical(self.canonical_payload))


@dataclass(frozen=True)
class DraftPrReceipt:
    receipt_id: str
    request_sha256: str
    synthetic: bool = True

    def __post_init__(self) -> None:
        _required("receipt_id", self.receipt_id)
        _digest("request_sha256", self.request_sha256)
        if self.synthetic is not True:
            raise ValueError("Phase 10 Prep receipts must remain synthetic")


class DraftPrPublisher(Protocol):
    """Idempotent Draft PR boundary. No merge operation exists."""

    def publish(self, request: DraftPrRequest) -> DraftPrReceipt: ...

    def lookup(self, idempotency_key: str) -> DraftPrReceipt | None: ...


class FakeDraftPrPublisher:
    """Deterministic effect-recording fake with no network or filesystem I/O."""

    def __init__(
        self,
        *,
        fail: bool = False,
        timeout_after_persist: bool = False,
    ) -> None:
        self.fail = fail
        self.timeout_after_persist = timeout_after_persist
        self.calls: list[str] = []
        self._receipts: dict[str, DraftPrReceipt] = {}

    def publish(self, request: DraftPrRequest) -> DraftPrReceipt:
        prior = self.lookup(request.idempotency_key)
        if prior is not None:
            return prior
        if self.fail:
            raise DraftPrPublicationError("draft_pr_publisher_failed")
        receipt = DraftPrReceipt(
            receipt_id=f"fake-draft:{request.idempotency_key[:24]}",
            request_sha256=request.payload_sha256,
        )
        self._receipts[request.idempotency_key] = receipt
        self.calls.append(request.idempotency_key)
        if self.timeout_after_persist:
            raise TimeoutError("draft_pr_publisher_timeout")
        return receipt

    def lookup(self, idempotency_key: str) -> DraftPrReceipt | None:
        return self._receipts.get(idempotency_key)


class DryRunDraftPrPublisher(FakeDraftPrPublisher):
    """Default offline publisher; returns only a synthetic hash-bound receipt."""

    def publish(self, request: DraftPrRequest) -> DraftPrReceipt:
        prior = self.lookup(request.idempotency_key)
        if prior is not None:
            return prior
        receipt = DraftPrReceipt(
            receipt_id=f"dry-run-draft:{request.idempotency_key[:24]}",
            request_sha256=request.payload_sha256,
        )
        self._receipts[request.idempotency_key] = receipt
        self.calls.append(request.idempotency_key)
        return receipt


from code_review_agent.github_sandbox_publish import (  # noqa: E402
    GitHubDraftPrPublisher as _GitHubDraftPrPublisher,
)


class GitHubDraftPrPublisher(_GitHubDraftPrPublisher):
    """Phase 10-compatible facade over the Phase 11B configured adapter."""

    def publish(self, request: Any) -> Any:
        if not self._enabled:
            raise DraftPrPublicationError("github_draft_pr_publisher_disabled")
        return super().publish(request)
