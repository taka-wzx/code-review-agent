"""One-use human approvals bound to an exact repair snapshot."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
import math
from pathlib import PurePosixPath
import re
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


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _required(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _timestamp(name: str, value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a finite {qualifier} number")
    return number


# Reserved Win32 device basenames: writing to e.g. "nul" or "con.txt" inside a
# worktree addresses a device, not a file, on Windows hosts.
WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{digit}" for digit in range(1, 10)}
    | {f"LPT{digit}" for digit in range(1, 10)}
)


def _validate_path_part(raw: str, part: str) -> None:
    # ":" anywhere covers drive letters and NTFS alternate data streams
    # ("src/mod.py:stream"); trailing dots/spaces alias a different file on
    # Windows ("src/mod.py." opens "src/mod.py"), silently widening the scope.
    if (
        part in (".", "..")
        or ":" in part
        or part != part.rstrip(". ")
        or any(ord(char) < 32 for char in part)
    ):
        raise ValueError(f"writable path must be a normalized repo-relative path: {raw!r}")
    if part.lower() == ".git":
        raise ValueError(f".git cannot appear in a writable path: {raw!r}")
    if part.split(".")[0].rstrip(" ").upper() in WINDOWS_RESERVED_DEVICE_NAMES:
        raise ValueError(f"writable path contains a Windows reserved device name: {raw!r}")


def normalize_repo_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Normalize repo-relative POSIX paths, rejecting every escape or alias vector.

    Shared by approval bindings and checkpoint payloads so both enforce the
    same path scope. Accepts an empty iterable; callers that require at least
    one path check that themselves.
    """
    if isinstance(paths, (str, bytes)):
        raise ValueError("writable paths must be a sequence of path strings, not one string")
    normalized = []
    for raw in paths:
        _required("writable path", raw)
        path = PurePosixPath(raw.replace("\\", "/"))
        if not path.parts or path.is_absolute():
            raise ValueError(f"writable path must be a normalized repo-relative path: {raw!r}")
        for part in path.parts:
            _validate_path_part(raw, part)
        normalized.append(path.as_posix())
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise ValueError("writable paths must be unique (case-insensitively)")
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
    patch_hash: str = ""
    writable_paths: tuple[str, ...] = ()
    patch_attempt: int = 0
    test_result_hash: str = ""
    commit_message: str = ""
    expected_tree_oid: str = ""

    def __post_init__(self) -> None:
        for name in ("run_id", "checkpoint_id", "base_sha", "diff_hash", "nonce"):
            _required(name, getattr(self, name))
        if self.kind is ApprovalKind.WRITE:
            _required("plan_hash", self.plan_hash)
            if not isinstance(self.patch_hash, str) or not _SHA256.fullmatch(
                self.patch_hash
            ):
                raise ValueError("patch_hash must be a lowercase SHA-256 digest")
            if (
                isinstance(self.patch_attempt, bool)
                or not isinstance(self.patch_attempt, int)
                or self.patch_attempt <= 0
            ):
                raise ValueError("patch_attempt must be a positive integer")
            if not self.writable_paths:
                raise ValueError("write approval needs at least one writable path")
            object.__setattr__(
                self, "writable_paths", normalize_repo_paths(self.writable_paths)
            )
            if self.test_result_hash or self.commit_message or self.expected_tree_oid:
                raise ValueError("write approval cannot contain commit-only fields")
        elif self.kind is ApprovalKind.COMMIT:
            _required("test_result_hash", self.test_result_hash)
            _required("commit_message", self.commit_message)
            if not isinstance(self.expected_tree_oid, str) or not _OBJECT_ID.fullmatch(
                self.expected_tree_oid
            ):
                raise ValueError("expected_tree_oid must be a lowercase Git object id")
            if self.plan_hash or self.patch_hash or self.writable_paths or self.patch_attempt:
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
            "patch_hash": self.patch_hash,
            "writable_paths": list(self.writable_paths),
            "patch_attempt": self.patch_attempt,
            "test_result_hash": self.test_result_hash,
            "commit_message": self.commit_message,
            "expected_tree_oid": self.expected_tree_oid,
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
                patch_hash=data.get("patch_hash", ""),
                writable_paths=tuple(writable_paths),
                patch_attempt=data.get("patch_attempt", 0),
                test_result_hash=data.get("test_result_hash", ""),
                commit_message=data.get("commit_message", ""),
                expected_tree_oid=data.get("expected_tree_oid", ""),
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
            _timestamp(name, getattr(self, name))
        if self.expires_at <= self.issued_at:
            raise ValueError("approval expiry must be after issuance")
        if self.consumed_at is not None:
            consumed = _timestamp("consumed_at", self.consumed_at)
            if consumed < self.issued_at:
                raise ValueError("consumed_at must be a valid timestamp after issuance")
            if self.consumed_at >= self.expires_at:
                raise ValueError("consumed_at must be before approval expiry")

    def consume(self, expected: ApprovalBinding, *, now: float | None = None) -> "ApprovalRecord":
        at = time.time() if now is None else _timestamp("now", now)
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
                issued_at=_timestamp("issued_at", data["issued_at"]),
                expires_at=_timestamp("expires_at", data["expires_at"]),
                consumed_at=(
                    None
                    if data.get("consumed_at") is None
                    else _timestamp("consumed_at", data["consumed_at"])
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
    patch_hash: str,
    writable_paths: tuple[str, ...],
    patch_attempt: int,
    ttl_seconds: float,
    now: float | None = None,
    nonce: str | None = None,
) -> ApprovalRecord:
    issued = time.time() if now is None else _timestamp("now", now)
    ttl = _timestamp("approval ttl_seconds", ttl_seconds, positive=True)
    binding = ApprovalBinding(
        kind=ApprovalKind.WRITE,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        base_sha=base_sha,
        diff_hash=diff_hash,
        nonce=nonce or secrets.token_urlsafe(24),
        plan_hash=plan_hash,
        patch_hash=patch_hash,
        writable_paths=writable_paths,
        patch_attempt=patch_attempt,
    )
    return ApprovalRecord(binding, issued, issued + ttl)


def issue_commit_approval(
    *,
    run_id: str,
    checkpoint_id: str,
    base_sha: str,
    diff_hash: str,
    test_result_hash: str,
    commit_message: str,
    expected_tree_oid: str,
    ttl_seconds: float,
    now: float | None = None,
    nonce: str | None = None,
) -> ApprovalRecord:
    issued = time.time() if now is None else _timestamp("now", now)
    ttl = _timestamp("approval ttl_seconds", ttl_seconds, positive=True)
    binding = ApprovalBinding(
        kind=ApprovalKind.COMMIT,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        base_sha=base_sha,
        diff_hash=diff_hash,
        nonce=nonce or secrets.token_urlsafe(24),
        test_result_hash=test_result_hash,
        commit_message=commit_message,
        expected_tree_oid=expected_tree_oid,
    )
    return ApprovalRecord(binding, issued, issued + ttl)
