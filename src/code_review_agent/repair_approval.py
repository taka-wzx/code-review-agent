"""One-use human approvals bound to an exact repair snapshot."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
from pathlib import PurePosixPath
import secrets
import time
from typing import Any


class ApprovalKind(str, Enum):
    WRITE = "write"
    COMMIT = "commit"


class ApprovalError(RuntimeError):
    pass


class ApprovalExpired(ApprovalError):
    pass


class ApprovalConsumed(ApprovalError):
    pass


class ApprovalMismatch(ApprovalError):
    pass


def _required(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _normalize_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    if not paths:
        raise ValueError("write approval needs at least one writable path")
    normalized = []
    for raw in paths:
        _required("writable path", raw)
        path = PurePosixPath(raw.replace("\\", "/"))
        if (
            not path.parts
            or path.is_absolute()
            or ":" in path.parts[0]
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise ValueError(f"writable path must be a normalized repo-relative path: {raw!r}")
        if path.parts[0].lower() == ".git":
            raise ValueError(".git cannot be approved as a writable path")
        normalized.append(path.as_posix())
    if len(set(normalized)) != len(normalized):
        raise ValueError("writable paths must be unique")
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class ApprovalBinding:
    kind: ApprovalKind
    run_id: str
    checkpoint_id: str
    base_sha: str
    diff_hash: str
    nonce: str
    plan_hash: str = ""
    writable_paths: tuple[str, ...] = ()
    patch_attempt: int = 0
    test_result_hash: str = ""
    commit_message: str = ""

    def __post_init__(self) -> None:
        for name in ("run_id", "checkpoint_id", "base_sha", "diff_hash", "nonce"):
            _required(name, getattr(self, name))
        if self.kind is ApprovalKind.WRITE:
            _required("plan_hash", self.plan_hash)
            if (
                isinstance(self.patch_attempt, bool)
                or not isinstance(self.patch_attempt, int)
                or self.patch_attempt <= 0
            ):
                raise ValueError("patch_attempt must be a positive integer")
            object.__setattr__(self, "writable_paths", _normalize_paths(self.writable_paths))
            if self.test_result_hash or self.commit_message:
                raise ValueError("write approval cannot contain commit-only fields")
        elif self.kind is ApprovalKind.COMMIT:
            _required("test_result_hash", self.test_result_hash)
            _required("commit_message", self.commit_message)
            if self.plan_hash or self.writable_paths or self.patch_attempt:
                raise ValueError("commit approval cannot contain write-only fields")
        else:  # defensive when instantiated from untyped checkpoint data
            raise ValueError(f"unsupported approval kind: {self.kind!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "run_id": self.run_id,
            "checkpoint_id": self.checkpoint_id,
            "base_sha": self.base_sha,
            "diff_hash": self.diff_hash,
            "nonce": self.nonce,
            "plan_hash": self.plan_hash,
            "writable_paths": list(self.writable_paths),
            "patch_attempt": self.patch_attempt,
            "test_result_hash": self.test_result_hash,
            "commit_message": self.commit_message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalBinding":
        try:
            writable_paths = data.get("writable_paths", [])
            if not isinstance(writable_paths, (list, tuple)) or not all(
                isinstance(path, str) for path in writable_paths
            ):
                raise ValueError("writable_paths must be a list of strings")
            return cls(
                kind=ApprovalKind(data["kind"]),
                run_id=data["run_id"],
                checkpoint_id=data["checkpoint_id"],
                base_sha=data["base_sha"],
                diff_hash=data["diff_hash"],
                nonce=data["nonce"],
                plan_hash=data.get("plan_hash", ""),
                writable_paths=tuple(writable_paths),
                patch_attempt=data.get("patch_attempt", 0),
                test_result_hash=data.get("test_result_hash", ""),
                commit_message=data.get("commit_message", ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid approval binding: {exc}") from exc


@dataclass(frozen=True)
class ApprovalRecord:
    binding: ApprovalBinding
    issued_at: float
    expires_at: float
    consumed_at: float | None = None

    def __post_init__(self) -> None:
        for name in ("issued_at", "expires_at"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative timestamp")
        if self.expires_at <= self.issued_at:
            raise ValueError("approval expiry must be after issuance")
        if self.consumed_at is not None:
            if not math.isfinite(float(self.consumed_at)) or self.consumed_at < self.issued_at:
                raise ValueError("consumed_at must be a valid timestamp after issuance")
            if self.consumed_at >= self.expires_at:
                raise ValueError("consumed_at must be before approval expiry")

    def consume(self, expected: ApprovalBinding, *, now: float | None = None) -> "ApprovalRecord":
        at = time.time() if now is None else float(now)
        if self.consumed_at is not None:
            raise ApprovalConsumed("approval has already been consumed")
        if at >= self.expires_at:
            raise ApprovalExpired("approval has expired")
        if self.binding != expected:
            raise ApprovalMismatch("approval is not bound to this operation snapshot")
        return replace(self, consumed_at=at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "consumed_at": self.consumed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRecord":
        try:
            return cls(
                binding=ApprovalBinding.from_dict(data["binding"]),
                issued_at=float(data["issued_at"]),
                expires_at=float(data["expires_at"]),
                consumed_at=(
                    None if data.get("consumed_at") is None else float(data["consumed_at"])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid approval record: {exc}") from exc


def issue_write_approval(
    *,
    run_id: str,
    checkpoint_id: str,
    base_sha: str,
    diff_hash: str,
    plan_hash: str,
    writable_paths: tuple[str, ...],
    patch_attempt: int,
    ttl_seconds: float,
    now: float | None = None,
    nonce: str | None = None,
) -> ApprovalRecord:
    issued = time.time() if now is None else float(now)
    if not math.isfinite(float(ttl_seconds)) or ttl_seconds <= 0:
        raise ValueError("approval ttl_seconds must be finite and positive")
    binding = ApprovalBinding(
        kind=ApprovalKind.WRITE,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        base_sha=base_sha,
        diff_hash=diff_hash,
        nonce=nonce or secrets.token_urlsafe(24),
        plan_hash=plan_hash,
        writable_paths=writable_paths,
        patch_attempt=patch_attempt,
    )
    return ApprovalRecord(binding, issued, issued + float(ttl_seconds))


def issue_commit_approval(
    *,
    run_id: str,
    checkpoint_id: str,
    base_sha: str,
    diff_hash: str,
    test_result_hash: str,
    commit_message: str,
    ttl_seconds: float,
    now: float | None = None,
    nonce: str | None = None,
) -> ApprovalRecord:
    issued = time.time() if now is None else float(now)
    if not math.isfinite(float(ttl_seconds)) or ttl_seconds <= 0:
        raise ValueError("approval ttl_seconds must be finite and positive")
    binding = ApprovalBinding(
        kind=ApprovalKind.COMMIT,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        base_sha=base_sha,
        diff_hash=diff_hash,
        nonce=nonce or secrets.token_urlsafe(24),
        test_result_hash=test_result_hash,
        commit_message=commit_message,
    )
    return ApprovalRecord(binding, issued, issued + float(ttl_seconds))
