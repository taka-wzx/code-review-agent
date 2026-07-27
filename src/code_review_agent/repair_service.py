"""Offline durable Review-to-Repair service control plane for Phase 10 Prep.

The module has no HTTP client, GitHub SDK, subprocess, or deployment dependency.
Mutable work is delegated to an executor that must return a strict sandbox receipt;
real Draft PR publication remains disabled by :mod:`repair_publish`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from code_review_agent.identity import Principal, Role
from code_review_agent.repair_approval import normalize_repo_paths
from code_review_agent.repair_budget import (
    BudgetAccountingError,
    BudgetExceeded,
    BudgetLimits,
    BudgetManager,
)
from code_review_agent.repair_checkpoint import RunFileLock
from code_review_agent.repair_publish import (
    DraftPrPublicationError,
    DraftPrPublisher,
    DraftPrReceipt,
    DraftPrRequest,
    DryRunDraftPrPublisher,
)
from code_review_agent.repair_tools import PatchRejected, parse_patch
from code_review_agent.tracelog import tev, tspan


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_JOB_ID = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?\Z")
_FORBIDDEN_APPROVER_METHODS = frozenset(
    {"anonymous", "finding", "github_webhook", "model", "webhook"}
)
_SCHEMA_VERSION = "crag.phase10.repair-job/v1alpha1"
_BRANCH = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,253}[A-Za-z0-9])?\Z")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _hash_value(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _sha256(name: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _object_id(name: str, value: Any) -> str:
    if not isinstance(value, str) or _OBJECT_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase Git object id")
    return value


def _finite(name: str, value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'non-negative'}")
    return number


def _branch(name: str, value: Any) -> str:
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


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class RepairServiceError(RuntimeError):
    """Redacted service failure with a stable code."""

    def __init__(self, code: str) -> None:
        self.code = _required("repair service error code", code)
        super().__init__(self.code)


class RepairAuthorizationError(RepairServiceError):
    pass


class RepairConflict(RepairServiceError):
    pass


class RepairQuarantined(RepairServiceError):
    pass


class RepairJobState(str, Enum):
    QUEUED_PLAN = "queued_plan"
    PLANNING = "planning"
    AWAITING_WRITE_APPROVAL = "awaiting_write_approval"
    QUEUED_EXECUTION = "queued_execution"
    EXECUTING = "executing"
    AWAITING_DRAFT_PR_APPROVAL = "awaiting_draft_pr_approval"
    QUEUED_PUBLISH = "queued_publish"
    PUBLISHING = "publishing"
    DRAFT_PUBLISHED = "draft_published"
    DECLINED = "declined"
    FAILED = "failed"
    QUARANTINED = "quarantined"


WAITING_STATES = frozenset(
    {
        RepairJobState.AWAITING_WRITE_APPROVAL,
        RepairJobState.AWAITING_DRAFT_PR_APPROVAL,
    }
)
ACTIVE_STATES = frozenset(
    {
        RepairJobState.PLANNING,
        RepairJobState.EXECUTING,
        RepairJobState.PUBLISHING,
    }
)
TERMINAL_STATES = frozenset(
    {
        RepairJobState.DRAFT_PUBLISHED,
        RepairJobState.DECLINED,
        RepairJobState.FAILED,
        RepairJobState.QUARANTINED,
    }
)


class ReflectionDecision(str, Enum):
    SUCCESS = "success"
    RETRY = "retry"
    FAIL = "fail"


@dataclass(frozen=True)
class OrganizationRepairPolicy:
    version: str
    fixed_test_commands: tuple[tuple[str, ...], ...]
    writable_paths: tuple[str, ...]
    draft_pr_base: str
    protected_branches: tuple[str, ...] = ("master", "main")
    max_retries: int = 2
    lease_seconds: float = 30.0
    command_timeout_seconds: float = 300.0
    command_output_bytes: int = 1024 * 1024
    plan_token_reservation: int = 20_000
    reflection_token_reservation: int = 4_000
    budget_limits: BudgetLimits = field(default_factory=BudgetLimits)

    def __post_init__(self) -> None:
        _required("policy version", self.version)
        _branch("draft_pr_base", self.draft_pr_base)
        if not self.fixed_test_commands:
            raise ValueError("organization policy needs fixed test commands")
        for command in self.fixed_test_commands:
            if not command or any(not isinstance(arg, str) or not arg for arg in command):
                raise ValueError("test commands must be non-empty argv arrays")
        object.__setattr__(
            self, "fixed_test_commands", tuple(tuple(item) for item in self.fixed_test_commands)
        )
        paths = normalize_repo_paths(self.writable_paths)
        if not paths:
            raise ValueError("organization policy needs writable paths")
        object.__setattr__(self, "writable_paths", paths)
        protected = tuple(sorted({_required("protected branch", item) for item in self.protected_branches}))
        object.__setattr__(self, "protected_branches", protected)
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or not 0 <= self.max_retries <= 8
        ):
            raise ValueError("max_retries must be an integer from zero through eight")
        _finite("lease_seconds", self.lease_seconds, positive=True)
        _finite("command_timeout_seconds", self.command_timeout_seconds, positive=True)
        if self.command_timeout_seconds > self.budget_limits.command_seconds:
            raise ValueError("command timeout exceeds the durable budget limit")
        for name in (
            "command_output_bytes",
            "plan_token_reservation",
            "reflection_token_reservation",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.command_output_bytes > self.budget_limits.command_output_bytes:
            raise ValueError("command output cap exceeds the durable budget limit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "fixed_test_commands": [list(item) for item in self.fixed_test_commands],
            "writable_paths": list(self.writable_paths),
            "draft_pr_base": self.draft_pr_base,
            "protected_branches": list(self.protected_branches),
            "max_retries": self.max_retries,
            "lease_seconds": self.lease_seconds,
            "command_timeout_seconds": self.command_timeout_seconds,
            "command_output_bytes": self.command_output_bytes,
            "plan_token_reservation": self.plan_token_reservation,
            "reflection_token_reservation": self.reflection_token_reservation,
            "budget_limits": asdict(self.budget_limits),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OrganizationRepairPolicy":
        try:
            return cls(
                version=data["version"],
                fixed_test_commands=tuple(
                    tuple(command) for command in data["fixed_test_commands"]
                ),
                writable_paths=tuple(data["writable_paths"]),
                draft_pr_base=data["draft_pr_base"],
                protected_branches=tuple(data.get("protected_branches", ("master", "main"))),
                max_retries=data.get("max_retries", 2),
                lease_seconds=data.get("lease_seconds", 30.0),
                command_timeout_seconds=data.get("command_timeout_seconds", 300.0),
                command_output_bytes=data.get("command_output_bytes", 1024 * 1024),
                plan_token_reservation=data.get("plan_token_reservation", 20_000),
                reflection_token_reservation=data.get("reflection_token_reservation", 4_000),
                budget_limits=BudgetLimits(**data.get("budget_limits", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid organization repair policy") from exc

    @property
    def sha256(self) -> str:
        return _hash_value(self.to_dict())


@dataclass(frozen=True)
class StartRepairRequest:
    organization_id: str
    repository_id: str
    finding_sha256: str
    base_sha: str
    head_sha: str
    policy: OrganizationRepairPolicy

    def __post_init__(self) -> None:
        _required("organization_id", self.organization_id)
        _required("repository_id", self.repository_id)
        _sha256("finding_sha256", self.finding_sha256)
        _object_id("base_sha", self.base_sha)
        _object_id("head_sha", self.head_sha)


@dataclass(frozen=True)
class RepairPlanArtifact:
    revision: int
    summary: str
    patch_text: str
    writable_paths: tuple[str, ...]
    test_commands: tuple[tuple[str, ...], ...]
    commit_message: str
    draft_pr_title: str
    draft_pr_body: str
    risks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision <= 0:
            raise ValueError("plan revision must be a positive integer")
        for name in (
            "summary",
            "patch_text",
            "commit_message",
            "draft_pr_title",
            "draft_pr_body",
        ):
            _required(name, getattr(self, name))
        object.__setattr__(self, "writable_paths", normalize_repo_paths(self.writable_paths))
        object.__setattr__(self, "test_commands", tuple(tuple(item) for item in self.test_commands))
        if not self.test_commands:
            raise ValueError("repair plan needs test commands")
        if any(not command or any(not arg for arg in command) for command in self.test_commands):
            raise ValueError("repair plan test commands must be non-empty argv arrays")
        object.__setattr__(self, "risks", tuple(_required("risk", item) for item in self.risks))

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "summary": self.summary,
            "patch_text": self.patch_text,
            "writable_paths": list(self.writable_paths),
            "test_commands": [list(item) for item in self.test_commands],
            "commit_message": self.commit_message,
            "draft_pr_title": self.draft_pr_title,
            "draft_pr_body": self.draft_pr_body,
            "risks": list(self.risks),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RepairPlanArtifact":
        return cls(
            revision=data["revision"],
            summary=data["summary"],
            patch_text=data["patch_text"],
            writable_paths=tuple(data["writable_paths"]),
            test_commands=tuple(tuple(item) for item in data["test_commands"]),
            commit_message=data["commit_message"],
            draft_pr_title=data["draft_pr_title"],
            draft_pr_body=data["draft_pr_body"],
            risks=tuple(data.get("risks", ())),
        )

    @property
    def sha256(self) -> str:
        return _hash_value(self.to_dict())

    @property
    def patch_sha256(self) -> str:
        return _hash_text(self.patch_text)


@dataclass(frozen=True)
class PlanReceipt:
    operation_id: str
    plan: RepairPlanArtifact
    tokens: int = 0
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        _required("operation_id", self.operation_id)
        if isinstance(self.tokens, bool) or not isinstance(self.tokens, int) or self.tokens < 0:
            raise ValueError("plan tokens must be non-negative")
        _finite("plan cost_usd", self.cost_usd)


@dataclass(frozen=True)
class ReflectionReceipt:
    operation_id: str
    decision: ReflectionDecision
    tokens: int = 0
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        _required("operation_id", self.operation_id)
        if isinstance(self.tokens, bool) or not isinstance(self.tokens, int) or self.tokens < 0:
            raise ValueError("reflection tokens must be non-negative")
        _finite("reflection cost_usd", self.cost_usd)


class RepairPlanner(Protocol):
    offline_only: bool

    def create_plan(
        self,
        operation_id: str,
        request: StartRepairRequest,
        *,
        revision: int,
        previous_test_sha256: str,
    ) -> PlanReceipt: ...

    def lookup_plan(self, operation_id: str) -> PlanReceipt | None: ...

    def reflect(
        self,
        operation_id: str,
        plan: RepairPlanArtifact,
        tests: tuple["TestEvidence", ...],
    ) -> ReflectionReceipt: ...

    def lookup_reflection(self, operation_id: str) -> ReflectionReceipt | None: ...


@dataclass(frozen=True)
class WorktreeBinding:
    worktree_id: str
    task_branch: str
    repository_id: str
    base_sha: str
    head_sha: str
    original_checkout_unchanged: bool

    def __post_init__(self) -> None:
        for name in ("worktree_id", "task_branch", "repository_id"):
            _required(name, getattr(self, name))
        _object_id("base_sha", self.base_sha)
        _object_id("head_sha", self.head_sha)
        if self.original_checkout_unchanged is not True:
            raise ValueError("original checkout must remain unchanged")


@dataclass(frozen=True)
class RepositorySnapshot:
    worktree_id: str
    task_branch: str
    repository_id: str
    base_sha: str
    head_sha: str
    diff_sha256: str
    original_checkout_unchanged: bool

    def __post_init__(self) -> None:
        for name in ("worktree_id", "task_branch", "repository_id"):
            _required(name, getattr(self, name))
        _object_id("base_sha", self.base_sha)
        _object_id("head_sha", self.head_sha)
        _sha256("diff_sha256", self.diff_sha256)
        if self.original_checkout_unchanged is not True:
            raise ValueError("original checkout must remain unchanged")


@dataclass(frozen=True)
class TestEvidence:
    argv: tuple[str, ...]
    exit_code: int
    duration_seconds: float
    timed_out: bool = False
    output_truncated: bool = False

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("test evidence needs a non-empty argv array")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise ValueError("test exit_code must be an integer")
        _finite("test duration_seconds", self.duration_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out,
            "output_truncated": self.output_truncated,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TestEvidence":
        return cls(
            argv=tuple(data["argv"]),
            exit_code=data["exit_code"],
            duration_seconds=data["duration_seconds"],
            timed_out=bool(data.get("timed_out", False)),
            output_truncated=bool(data.get("output_truncated", False)),
        )


@dataclass(frozen=True)
class ExecutionReceipt:
    operation_id: str
    snapshot: RepositorySnapshot
    full_diff: str
    tests: tuple[TestEvidence, ...]
    docker: bool
    network_mode: str
    non_root: bool
    timeout_seconds: float
    output_limit_bytes: int
    elapsed_seconds: float
    tool_calls: int

    def __post_init__(self) -> None:
        _required("operation_id", self.operation_id)
        if not isinstance(self.full_diff, str) or not self.full_diff:
            raise ValueError("execution receipt needs the full diff")
        if self.snapshot.diff_sha256 != _hash_text(self.full_diff):
            raise ValueError("execution diff hash does not match full diff")
        if not self.tests:
            raise ValueError("execution receipt needs test evidence")
        _required("network_mode", self.network_mode)
        _finite("timeout_seconds", self.timeout_seconds, positive=True)
        _finite("elapsed_seconds", self.elapsed_seconds)
        for name in ("output_limit_bytes", "tool_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    @property
    def tests_sha256(self) -> str:
        return _hash_value([item.to_dict() for item in self.tests])


@dataclass(frozen=True)
class CommitReceipt:
    operation_id: str
    commit_sha: str
    parent_sha: str
    diff_sha256: str
    message_sha256: str
    original_checkout_unchanged: bool

    def __post_init__(self) -> None:
        _required("operation_id", self.operation_id)
        _object_id("commit_sha", self.commit_sha)
        _object_id("parent_sha", self.parent_sha)
        _sha256("diff_sha256", self.diff_sha256)
        _sha256("message_sha256", self.message_sha256)
        if self.original_checkout_unchanged is not True:
            raise ValueError("commit changed the original checkout")


class RepairExecutor(Protocol):
    offline_only: bool

    def provision(
        self,
        *,
        job_id: str,
        task_branch: str,
        repository_id: str,
        base_sha: str,
        head_sha: str,
    ) -> WorktreeBinding: ...

    def inspect(self, binding: WorktreeBinding) -> RepositorySnapshot: ...

    def execute(
        self,
        operation_id: str,
        binding: WorktreeBinding,
        plan: RepairPlanArtifact,
        policy: OrganizationRepairPolicy,
    ) -> ExecutionReceipt: ...

    def lookup_execution(self, operation_id: str) -> ExecutionReceipt | None: ...

    def commit(
        self,
        operation_id: str,
        binding: WorktreeBinding,
        *,
        diff_sha256: str,
        commit_message: str,
    ) -> CommitReceipt: ...

    def lookup_commit(self, operation_id: str) -> CommitReceipt | None: ...

    def rollback(self, binding: WorktreeBinding, operation_id: str) -> bool: ...


class RepairMetricsSink(Protocol):
    def increment(self, name: str, labels: Mapping[str, str] | None = None) -> None: ...


@dataclass
class RepairJobCheckpoint:
    job_id: str
    organization_id: str
    repository_id: str
    finding_sha256: str
    base_sha: str
    head_sha: str
    requested_by: str
    policy: dict[str, Any]
    policy_sha256: str
    worktree: dict[str, Any]
    state: RepairJobState = RepairJobState.QUEUED_PLAN
    sequence: int = 0
    attempt: int = 1
    plan: dict[str, Any] = field(default_factory=dict)
    plan_sha256: str = ""
    current_diff_sha256: str = ""
    full_diff: str = ""
    tests: list[dict[str, Any]] = field(default_factory=list)
    tests_sha256: str = ""
    budget: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    write_approval: dict[str, Any] = field(default_factory=dict)
    draft_approval: dict[str, Any] = field(default_factory=dict)
    commit: dict[str, Any] = field(default_factory=dict)
    publication: dict[str, Any] = field(default_factory=dict)
    in_progress: dict[str, Any] | None = None
    lease_owner: str = ""
    lease_token: str = ""
    lease_expires_at: float = 0.0
    failure_code: str = ""
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if _JOB_ID.fullmatch(self.job_id) is None:
            raise ValueError("invalid repair job id")
        for name in ("organization_id", "repository_id", "requested_by"):
            _required(name, getattr(self, name))
        _sha256("finding_sha256", self.finding_sha256)
        _object_id("base_sha", self.base_sha)
        _object_id("head_sha", self.head_sha)
        _sha256("policy_sha256", self.policy_sha256)
        self.state = RepairJobState(self.state)
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt <= 0:
            raise ValueError("attempt must be positive")
        _finite("lease_expires_at", self.lease_expires_at)
        _finite("updated_at", self.updated_at)
        if self.state in WAITING_STATES and (self.lease_owner or self.lease_token):
            raise ValueError("approval-wait states cannot hold a worker lease")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["state"] = self.state.value
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RepairJobCheckpoint":
        try:
            return cls(**dict(data))
        except (TypeError, ValueError) as exc:
            raise RepairServiceError("repair_checkpoint_invalid") from exc

    @property
    def binding(self) -> WorktreeBinding:
        try:
            return WorktreeBinding(**self.worktree)
        except (TypeError, ValueError) as exc:
            raise RepairServiceError("worktree_binding_invalid") from exc

    @property
    def policy_object(self) -> OrganizationRepairPolicy:
        policy = OrganizationRepairPolicy.from_dict(self.policy)
        if policy.sha256 != self.policy_sha256:
            raise RepairServiceError("organization_policy_mismatch")
        return policy

    @property
    def plan_object(self) -> RepairPlanArtifact:
        try:
            plan = RepairPlanArtifact.from_dict(self.plan)
        except (KeyError, TypeError, ValueError) as exc:
            raise RepairServiceError("repair_plan_invalid") from exc
        if plan.sha256 != self.plan_sha256:
            raise RepairServiceError("repair_plan_mismatch")
        return plan


class Phase10RepairStore:
    """Checksum-bound atomic snapshots plus a redacted append-only journal."""

    def __init__(self, state_root: Path, *, clock: Callable[[], float] = time.time):
        self.state_root = Path(state_root)
        self.clock = clock

    def _directory(self, job_id: str) -> Path:
        if _JOB_ID.fullmatch(job_id) is None:
            raise RepairServiceError("repair_job_id_invalid")
        return self.state_root / job_id

    def snapshot_path(self, job_id: str) -> Path:
        return self._directory(job_id) / "phase10-checkpoint.json"

    def lock(self, job_id: str) -> RunFileLock:
        return RunFileLock(self._directory(job_id) / "phase10.lock")

    def save(self, checkpoint: RepairJobCheckpoint) -> str:
        directory = self._directory(checkpoint.job_id)
        directory.mkdir(parents=True, exist_ok=True)
        checkpoint.sequence += 1
        checkpoint.updated_at = float(self.clock())
        payload = checkpoint.to_dict()
        checksum = _hash_value(payload)
        envelope = {
            "schema_version": _SCHEMA_VERSION,
            "checksum": checksum,
            "checkpoint": payload,
        }
        temporary = directory / f".phase10.{uuid4().hex}.tmp"
        target = self.snapshot_path(checkpoint.job_id)
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(envelope, sort_keys=True, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _fsync_directory(directory)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return checksum

    def load(self, job_id: str) -> tuple[RepairJobCheckpoint, str]:
        try:
            envelope = json.loads(self.snapshot_path(job_id).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise RepairServiceError("repair_checkpoint_unavailable") from exc
        if not isinstance(envelope, dict) or envelope.get("schema_version") != _SCHEMA_VERSION:
            raise RepairServiceError("repair_checkpoint_version_invalid")
        payload = envelope.get("checkpoint")
        checksum = envelope.get("checksum")
        if not isinstance(payload, dict) or not isinstance(checksum, str):
            raise RepairServiceError("repair_checkpoint_invalid")
        if not secrets.compare_digest(_hash_value(payload), checksum):
            raise RepairServiceError("repair_checkpoint_checksum_mismatch")
        checkpoint = RepairJobCheckpoint.from_dict(payload)
        if checkpoint.job_id != job_id:
            raise RepairServiceError("repair_checkpoint_job_mismatch")
        return checkpoint, checksum

    def append_event(self, job_id: str, kind: str, data: Mapping[str, Any]) -> None:
        directory = self._directory(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        record = {"t": float(self.clock()), "kind": _required("event kind", kind), "data": dict(data)}
        with (directory / "phase10-events.jsonl").open(
            "a", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write(_canonical(record).decode("utf-8") + "\n")
            stream.flush()
            os.fsync(stream.fileno())


class Phase10RepairService:
    """Durable offline service with remote approvals and synthetic publication."""

    def __init__(
        self,
        *,
        store: Phase10RepairStore,
        planner: RepairPlanner,
        executor: RepairExecutor,
        publisher: DraftPrPublisher | None = None,
        metrics: RepairMetricsSink | None = None,
        trace: Any = None,
        clock: Callable[[], float] = time.time,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.planner = planner
        self.executor = executor
        self.publisher = publisher or DryRunDraftPrPublisher()
        self.metrics = metrics
        self.trace = trace
        self.clock = clock
        self.fault = fault
        self.real_writes_enabled = False
        if getattr(planner, "offline_only", False) is not True:
            raise ValueError("Phase 10 Prep requires an offline-only planner")
        if getattr(executor, "offline_only", False) is not True:
            raise ValueError("Phase 10 Prep requires an offline-only executor")

    def _require_operator(
        self,
        actor: Principal,
        organization_id: str,
        *,
        operation: str = "other",
    ) -> None:
        if actor.organization_id != organization_id:
            self._metric("unauthorized_operations_total", {"operation": operation})
            raise RepairAuthorizationError("repair_cross_organization_denied")
        if actor.role not in {Role.MAINTAINER, Role.ORG_ADMIN}:
            self._metric("unauthorized_operations_total", {"operation": operation})
            raise RepairAuthorizationError("repair_operator_required")
        if actor.auth_method.casefold() in _FORBIDDEN_APPROVER_METHODS:
            self._metric("unauthorized_operations_total", {"operation": operation})
            raise RepairAuthorizationError("repair_actor_type_denied")

    def _require_reader(self, actor: Principal, organization_id: str) -> None:
        if actor.organization_id != organization_id:
            self._metric("unauthorized_operations_total", {"operation": "other"})
            raise RepairAuthorizationError("repair_cross_organization_denied")

    def _metric(self, name: str, labels: Mapping[str, str]) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.increment(name, labels)
        except Exception:
            tev(
                self.trace,
                "degraded",
                operation="phase10_metrics",
                decision="disabled",
                reason_code="metrics_sink_failed",
            )

    def _event(
        self,
        job: RepairJobCheckpoint,
        kind: str,
        *,
        outcome: str = "none",
        approval_kind: str = "none",
    ) -> None:
        data = {
            "state": job.state.value,
            "outcome": outcome,
            "approval_kind": approval_kind,
            "attempt": job.attempt,
            "failure_code": job.failure_code or "none",
        }
        self.store.append_event(job.job_id, kind, data)
        tev(
            self.trace,
            "policy",
            operation="phase10_repair",
            decision=outcome,
            state=job.state.value,
            approval_kind=approval_kind,
            attempt=job.attempt,
            failure_code=job.failure_code or "none",
        )

    def _fault(self, point: str) -> None:
        if self.fault is not None:
            self.fault(point)

    def start_repair(
        self, request: StartRepairRequest, *, actor: Principal
    ) -> dict[str, Any]:
        self._require_operator(actor, request.organization_id, operation="other")
        job_id = f"repair-{secrets.token_hex(12)}"
        task_branch = f"repair/{job_id}"
        if task_branch.casefold() in {
            item.casefold() for item in request.policy.protected_branches
        }:
            raise RepairServiceError("protected_task_branch_denied")
        binding = self.executor.provision(
            job_id=job_id,
            task_branch=task_branch,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            head_sha=request.head_sha,
        )
        self._validate_binding(binding, request, task_branch)
        snapshot = self.executor.inspect(binding)
        self._validate_snapshot_fields(
            snapshot,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            head_sha=request.head_sha,
            binding=binding,
        )
        budget = BudgetManager(request.policy.budget_limits)
        checkpoint = RepairJobCheckpoint(
            job_id=job_id,
            organization_id=request.organization_id,
            repository_id=request.repository_id,
            finding_sha256=request.finding_sha256,
            base_sha=request.base_sha,
            head_sha=request.head_sha,
            requested_by=actor.principal_id,
            policy=request.policy.to_dict(),
            policy_sha256=request.policy.sha256,
            worktree=asdict(binding),
            current_diff_sha256=snapshot.diff_sha256,
            budget=budget.to_dict(),
        )
        with self.store.lock(job_id):
            checksum = self.store.save(checkpoint)
            self._event(checkpoint, "repair_started", outcome="accepted")
        return self._public(checkpoint, checksum)

    def get_repair(self, job_id: str, *, actor: Principal) -> dict[str, Any]:
        job, checksum = self.store.load(job_id)
        self._require_reader(actor, job.organization_id)
        return self._public(job, checksum)

    def write_approval_view(self, job_id: str, *, actor: Principal) -> dict[str, Any]:
        job, checksum = self.store.load(job_id)
        self._require_operator(actor, job.organization_id, operation="approval")
        if job.state is not RepairJobState.AWAITING_WRITE_APPROVAL:
            raise RepairConflict("write_approval_not_pending")
        plan = job.plan_object
        return {
            "job_id": job.job_id,
            "checkpoint_sha256": checksum,
            "attempt": job.attempt,
            "plan": plan.to_dict(),
            "plan_sha256": job.plan_sha256,
            "patch_sha256": plan.patch_sha256,
            "current_diff_sha256": job.current_diff_sha256,
            "repository_id": job.repository_id,
            "base_sha": job.base_sha,
            "head_sha": job.head_sha,
            "policy_sha256": job.policy_sha256,
            "task_branch": job.binding.task_branch,
        }

    def decide_write(
        self,
        job_id: str,
        *,
        actor: Principal,
        checkpoint_sha256: str,
        approved: bool,
        now: float | None = None,
    ) -> dict[str, Any]:
        _sha256("checkpoint_sha256", checkpoint_sha256)
        at = float(self.clock() if now is None else now)
        with self.store.lock(job_id):
            job, checksum = self.store.load(job_id)
            self._require_operator(actor, job.organization_id, operation="approval")
            if job.state is not RepairJobState.AWAITING_WRITE_APPROVAL:
                self._approval_failure("replay")
                raise RepairConflict("write_approval_not_pending")
            if not secrets.compare_digest(checksum, checkpoint_sha256):
                self._approval_failure("mismatch")
                raise RepairConflict("write_approval_checkpoint_mismatch")
            if not approved:
                job.state = RepairJobState.DECLINED
                job.failure_code = "write_approval_rejected"
                self._clear_lease(job)
                new_checksum = self.store.save(job)
                self._event(job, "write_approval", outcome="rejected", approval_kind="write")
                return self._public(job, new_checksum)
            binding = self._write_binding(job, checksum)
            job.write_approval = {
                "kind": "write",
                "binding": binding,
                "binding_sha256": _hash_value(binding),
                "nonce_sha256": _hash_text(secrets.token_urlsafe(24)),
                "approver_sha256": _hash_text(actor.principal_id),
                "issued_at": at,
                "consumed_at": at,
                "status": "consumed",
            }
            job.state = RepairJobState.QUEUED_EXECUTION
            self._clear_lease(job)
            new_checksum = self.store.save(job)
            self._event(job, "write_approval", outcome="approved", approval_kind="write")
            self._fault("after_write_approval_persisted")
            return self._public(job, new_checksum)

    def draft_pr_approval_view(
        self, job_id: str, *, actor: Principal
    ) -> dict[str, Any]:
        job, checksum = self.store.load(job_id)
        self._require_operator(actor, job.organization_id, operation="approval")
        if job.state is not RepairJobState.AWAITING_DRAFT_PR_APPROVAL:
            raise RepairConflict("draft_pr_approval_not_pending")
        plan = job.plan_object
        budget_hash = _hash_value(job.budget)
        draft_hash = self._draft_content_hash(job, plan, budget_hash)
        return {
            "job_id": job.job_id,
            "checkpoint_sha256": checksum,
            "full_diff": job.full_diff,
            "diff_sha256": job.current_diff_sha256,
            "tests": list(job.tests),
            "tests_sha256": job.tests_sha256,
            "budget": dict(job.budget),
            "budget_sha256": budget_hash,
            "commit_message": plan.commit_message,
            "head_branch": job.binding.task_branch,
            "target_base": job.policy_object.draft_pr_base,
            "base_sha": job.base_sha,
            "head_sha": job.head_sha,
            "draft_content_sha256": draft_hash,
        }

    def decide_draft_pr(
        self,
        job_id: str,
        *,
        actor: Principal,
        checkpoint_sha256: str,
        approved: bool,
        now: float | None = None,
    ) -> dict[str, Any]:
        _sha256("checkpoint_sha256", checkpoint_sha256)
        at = float(self.clock() if now is None else now)
        with self.store.lock(job_id):
            job, checksum = self.store.load(job_id)
            self._require_operator(actor, job.organization_id, operation="approval")
            if job.state is not RepairJobState.AWAITING_DRAFT_PR_APPROVAL:
                self._approval_failure("replay")
                raise RepairConflict("draft_pr_approval_not_pending")
            if not secrets.compare_digest(checksum, checkpoint_sha256):
                self._approval_failure("mismatch")
                raise RepairConflict("draft_pr_approval_checkpoint_mismatch")
            if not approved:
                rollback_ok = self.executor.rollback(job.binding, f"decline-{job.attempt}")
                job.state = (
                    RepairJobState.DECLINED if rollback_ok else RepairJobState.QUARANTINED
                )
                job.failure_code = (
                    "draft_pr_approval_rejected"
                    if rollback_ok
                    else "draft_pr_rejection_rollback_unverified"
                )
                self._clear_lease(job)
                new_checksum = self.store.save(job)
                self._event(job, "draft_pr_approval", outcome="rejected", approval_kind="draft_pr")
                return self._public(job, new_checksum)
            plan = job.plan_object
            budget_hash = _hash_value(job.budget)
            binding = self._draft_binding(job, checksum, plan, budget_hash)
            job.draft_approval = {
                "kind": "draft_pr",
                "binding": binding,
                "binding_sha256": _hash_value(binding),
                "nonce_sha256": _hash_text(secrets.token_urlsafe(24)),
                "approver_sha256": _hash_text(actor.principal_id),
                "issued_at": at,
                "consumed_at": at,
                "status": "consumed",
            }
            job.state = RepairJobState.QUEUED_PUBLISH
            self._clear_lease(job)
            new_checksum = self.store.save(job)
            self._event(job, "draft_pr_approval", outcome="approved", approval_kind="draft_pr")
            self._fault("after_draft_approval_persisted")
            return self._public(job, new_checksum)

    def run_worker_once(self, job_id: str, *, worker_id: str) -> dict[str, Any]:
        _required("worker_id", worker_id)
        with self.store.lock(job_id), tspan(
            self.trace,
            "crag.stage phase10_repair",
            operation="agent.stage",
            attributes={"crag.stage.name": "phase10_repair"},
        ):
            job, _checksum = self.store.load(job_id)
            if job.state in WAITING_STATES or job.state in TERMINAL_STATES:
                return self._public(job, self.store.load(job_id)[1])
            newly_claimed = self._claim(job, worker_id)
            try:
                if job.state is RepairJobState.PLANNING:
                    self._process_plan(job, newly_claimed=newly_claimed)
                elif job.state is RepairJobState.EXECUTING:
                    self._process_execution(job, newly_claimed=newly_claimed)
                elif job.state is RepairJobState.PUBLISHING:
                    self._process_publish(job, newly_claimed=newly_claimed)
                else:
                    raise RepairServiceError("repair_job_state_invalid")
            except (BudgetExceeded, BudgetAccountingError):
                self._fail_with_rollback(job, "repair_budget_exhausted")
            except RepairConflict as exc:
                self._quarantine(job, exc.code)
            except RepairServiceError as exc:
                self._fail_with_rollback(job, exc.code)
            checksum = self.store.load(job_id)[1]
            return self._public(job, checksum)

    def _claim(self, job: RepairJobCheckpoint, worker_id: str) -> bool:
        mapping = {
            RepairJobState.QUEUED_PLAN: RepairJobState.PLANNING,
            RepairJobState.QUEUED_EXECUTION: RepairJobState.EXECUTING,
            RepairJobState.QUEUED_PUBLISH: RepairJobState.PUBLISHING,
        }
        now = float(self.clock())
        if job.state in mapping:
            job.state = mapping[job.state]
            job.lease_owner = worker_id
            job.lease_token = secrets.token_hex(16)
            job.lease_expires_at = now + job.policy_object.lease_seconds
            self.store.save(job)
            self._event(job, "worker_claimed", outcome="accepted")
            return True
        if job.state not in ACTIVE_STATES:
            raise RepairConflict("repair_job_not_claimable")
        if job.lease_owner == worker_id and job.lease_expires_at > now:
            return False
        if job.lease_expires_at > now:
            raise RepairConflict("repair_job_lease_active")
        job.lease_owner = worker_id
        job.lease_token = secrets.token_hex(16)
        job.lease_expires_at = now + job.policy_object.lease_seconds
        self.store.save(job)
        self._event(job, "worker_recovered", outcome="accepted")
        return False

    def _process_plan(self, job: RepairJobCheckpoint, *, newly_claimed: bool) -> None:
        policy = job.policy_object
        budget = BudgetManager.from_dict(job.budget)
        operation = job.in_progress
        if operation is None:
            if not newly_claimed:
                self._quarantine(job, "plan_intent_missing")
                return
            reservation = budget.reserve_llm(policy.plan_token_reservation, 0.0)
            operation_id = f"plan-{job.job_id}-{job.attempt}-{secrets.token_hex(8)}"
            operation = {
                "kind": "plan",
                "operation_id": operation_id,
                "reservation_id": reservation.reservation_id,
            }
            job.in_progress = operation
            job.budget = budget.to_dict()
            self.store.save(job)
            receipt = self.planner.lookup_plan(operation_id)
            if receipt is None:
                request = StartRepairRequest(
                    organization_id=job.organization_id,
                    repository_id=job.repository_id,
                    finding_sha256=job.finding_sha256,
                    base_sha=job.base_sha,
                    head_sha=job.head_sha,
                    policy=policy,
                )
                receipt = self.planner.create_plan(
                    operation_id,
                    request,
                    revision=job.attempt,
                    previous_test_sha256=job.tests_sha256,
                )
        else:
            if operation.get("kind") != "plan":
                self._quarantine(job, "plan_intent_invalid")
                return
            operation_id = str(operation.get("operation_id", ""))
            receipt = self.planner.lookup_plan(operation_id)
            if receipt is None:
                self._quarantine(job, "plan_receipt_unavailable")
                return
        if receipt.operation_id != operation_id:
            self._quarantine(job, "plan_receipt_mismatch")
            return
        budget.reconcile_llm(
            str(operation["reservation_id"]), receipt.tokens, receipt.cost_usd
        )
        self._validate_plan(receipt.plan, policy, job.attempt)
        job.plan = receipt.plan.to_dict()
        job.plan_sha256 = receipt.plan.sha256
        job.budget = budget.to_dict()
        job.in_progress = None
        job.state = RepairJobState.AWAITING_WRITE_APPROVAL
        self._clear_lease(job)
        self.store.save(job)
        self._event(job, "plan_completed", outcome="completed")
        self._fault("after_plan_result_persisted")

    def _process_execution(self, job: RepairJobCheckpoint, *, newly_claimed: bool) -> None:
        policy = job.policy_object
        plan = job.plan_object
        self._validate_consumed_write(job)
        operation = job.in_progress
        if not job.execution:
            if operation is None:
                if not newly_claimed:
                    self._quarantine(job, "execution_intent_missing")
                    return
                self._revalidate_repository(job, expected_diff=job.current_diff_sha256)
                operation_id = f"execute-{job.job_id}-{job.attempt}-{secrets.token_hex(8)}"
                operation = {"kind": "execute", "operation_id": operation_id}
                job.in_progress = operation
                self.store.save(job)
                receipt = self.executor.lookup_execution(operation_id)
                if receipt is None:
                    receipt = self.executor.execute(
                        operation_id, job.binding, plan, policy
                    )
            else:
                if operation.get("kind") != "execute":
                    self._quarantine(job, "execution_intent_invalid")
                    return
                operation_id = str(operation.get("operation_id", ""))
                receipt = self.executor.lookup_execution(operation_id)
                if receipt is None:
                    self._quarantine(job, "execution_receipt_unavailable")
                    return
            self._validate_execution(job, receipt, policy)
            live_snapshot = self.executor.inspect(job.binding)
            self._validate_snapshot(job, live_snapshot)
            if live_snapshot.diff_sha256 != receipt.snapshot.diff_sha256:
                self._quarantine(job, "execution_receipt_repository_mismatch")
                return
            budget = BudgetManager.from_dict(job.budget)
            budget.consume_elapsed(receipt.elapsed_seconds)
            budget.consume_tool_call(receipt.tool_calls, command=True)
            job.budget = budget.to_dict()
            job.execution = self._execution_to_dict(receipt)
            job.current_diff_sha256 = receipt.snapshot.diff_sha256
            job.full_diff = receipt.full_diff
            job.tests = [item.to_dict() for item in receipt.tests]
            job.tests_sha256 = receipt.tests_sha256
            job.budget = budget.to_dict()
            job.in_progress = None
            self.store.save(job)
            self._fault("after_execution_result_persisted")
        else:
            self._revalidate_repository(job, expected_diff=job.current_diff_sha256)
        operation = job.in_progress
        reflection_new = False
        if operation is None and job.execution:
            budget = BudgetManager.from_dict(job.budget)
            reflection_reservation = budget.reserve_llm(
                policy.reflection_token_reservation, 0.0
            )
            reflection_id = f"reflect-{job.job_id}-{job.attempt}-{secrets.token_hex(8)}"
            operation = {
                "kind": "reflect",
                "operation_id": reflection_id,
                "reservation_id": reflection_reservation.reservation_id,
            }
            job.budget = budget.to_dict()
            job.in_progress = operation
            self.store.save(job)
            reflection_new = True
        if not isinstance(operation, dict) or operation.get("kind") != "reflect":
            self._quarantine(job, "reflection_intent_invalid")
            return
        reflection_id = str(operation.get("operation_id", ""))
        reflection = self.planner.lookup_reflection(reflection_id)
        if reflection is None:
            if not reflection_new:
                self._quarantine(job, "reflection_receipt_unavailable")
                return
            tests = tuple(TestEvidence.from_dict(item) for item in job.tests)
            reflection = self.planner.reflect(reflection_id, plan, tests)
        if reflection.operation_id != reflection_id:
            self._quarantine(job, "reflection_receipt_mismatch")
            return
        budget = BudgetManager.from_dict(job.budget)
        budget.reconcile_llm(
            str(operation["reservation_id"]), reflection.tokens, reflection.cost_usd
        )
        job.budget = budget.to_dict()
        tests = tuple(TestEvidence.from_dict(item) for item in job.tests)
        passed = all(
            item.exit_code == 0 and not item.timed_out and not item.output_truncated
            for item in tests
        )
        if passed and reflection.decision is ReflectionDecision.SUCCESS:
            job.state = RepairJobState.AWAITING_DRAFT_PR_APPROVAL
            job.in_progress = None
            self._clear_lease(job)
            self.store.save(job)
            self._event(job, "execution_completed", outcome="completed")
            return
        if reflection.decision is ReflectionDecision.RETRY and job.attempt <= policy.max_retries:
            rollback_ok = self.executor.rollback(
                job.binding, f"retry-{job.attempt}-{secrets.token_hex(4)}"
            )
            if not rollback_ok:
                self._quarantine(job, "retry_rollback_unverified")
                return
            budget.consume_repair_attempt()
            job.budget = budget.to_dict()
            job.attempt += 1
            job.plan = {}
            job.plan_sha256 = ""
            job.execution = {}
            job.write_approval = {}
            job.full_diff = ""
            job.in_progress = None
            snapshot = self.executor.inspect(job.binding)
            self._validate_snapshot(job, snapshot)
            job.current_diff_sha256 = snapshot.diff_sha256
            job.state = RepairJobState.QUEUED_PLAN
            self._clear_lease(job)
            self.store.save(job)
            self._event(job, "retry_queued", outcome="retry")
            return
        self._fail_with_rollback(job, "tests_or_reflection_failed")

    def _process_publish(self, job: RepairJobCheckpoint, *, newly_claimed: bool) -> None:
        del newly_claimed
        policy = job.policy_object
        plan = job.plan_object
        self._validate_consumed_draft(job)
        self._revalidate_repository(job, expected_diff=job.current_diff_sha256)
        operation = job.in_progress
        if not job.commit:
            if operation is None:
                commit_id = f"commit-{job.job_id}-{secrets.token_hex(8)}"
                job.in_progress = {"kind": "commit", "operation_id": commit_id}
                self.store.save(job)
                commit_receipt = self.executor.lookup_commit(commit_id)
                if commit_receipt is None:
                    commit_receipt = self.executor.commit(
                        commit_id,
                        job.binding,
                        diff_sha256=job.current_diff_sha256,
                        commit_message=plan.commit_message,
                    )
            else:
                if operation.get("kind") != "commit":
                    self._quarantine(job, "commit_intent_invalid")
                    return
                commit_id = str(operation.get("operation_id", ""))
                commit_receipt = self.executor.lookup_commit(commit_id)
                if commit_receipt is None:
                    self._quarantine(job, "commit_receipt_unavailable")
                    return
            try:
                self._validate_commit(job, plan, commit_receipt)
            except RepairServiceError:
                self._quarantine(job, "commit_receipt_mismatch")
                return
            job.commit = asdict(commit_receipt)
            job.in_progress = None
            self.store.save(job)
            self._fault("after_commit_result_persisted")
        commit = CommitReceipt(**job.commit)
        request = self._publisher_request(job, plan, policy, commit)
        operation = job.in_progress
        if operation is None:
            job.in_progress = {
                "kind": "publish",
                "idempotency_key": request.idempotency_key,
                "payload_sha256": request.payload_sha256,
            }
            self.store.save(job)
        elif (
            operation.get("kind") != "publish"
            or operation.get("idempotency_key") != request.idempotency_key
            or operation.get("payload_sha256") != request.payload_sha256
        ):
            self._quarantine(job, "publisher_intent_mismatch")
            return
        publish_receipt: DraftPrReceipt | None = self.publisher.lookup(
            request.idempotency_key
        )
        if publish_receipt is None:
            try:
                publish_receipt = self.publisher.publish(request)
            except (TimeoutError, DraftPrPublicationError):
                publish_receipt = self.publisher.lookup(request.idempotency_key)
        if publish_receipt is None:
            job.state = RepairJobState.QUARANTINED
            job.failure_code = "draft_pr_publisher_failed_after_commit"
            job.in_progress = None
            self._clear_lease(job)
            self.store.save(job)
            self._event(job, "publication_failed", outcome="quarantined")
            return
        if (
            publish_receipt.synthetic is not True
            or publish_receipt.request_sha256 != request.payload_sha256
        ):
            self._quarantine(job, "publisher_receipt_invalid")
            return
        job.publication = {
            "receipt_id": publish_receipt.receipt_id,
            "request_sha256": publish_receipt.request_sha256,
            "synthetic": True,
        }
        job.in_progress = None
        job.state = RepairJobState.DRAFT_PUBLISHED
        self._clear_lease(job)
        self.store.save(job)
        self._event(job, "draft_pr_published", outcome="synthetic_success")

    def _fail_with_rollback(self, job: RepairJobCheckpoint, code: str) -> None:
        rollback_ok = self.executor.rollback(job.binding, f"failure-{secrets.token_hex(8)}")
        job.state = RepairJobState.FAILED if rollback_ok else RepairJobState.QUARANTINED
        job.failure_code = code if rollback_ok else "failure_rollback_unverified"
        job.in_progress = None
        self._clear_lease(job)
        self.store.save(job)
        self._event(job, "repair_failed", outcome="failed")

    def _quarantine(self, job: RepairJobCheckpoint, code: str) -> None:
        job.state = RepairJobState.QUARANTINED
        job.failure_code = code
        job.in_progress = None
        self._clear_lease(job)
        self.store.save(job)
        self._event(job, "repair_quarantined", outcome="quarantined")

    @staticmethod
    def _clear_lease(job: RepairJobCheckpoint) -> None:
        job.lease_owner = ""
        job.lease_token = ""
        job.lease_expires_at = 0.0

    def _approval_failure(self, reason: str) -> None:
        bounded = reason if reason in {"replay", "mismatch", "expired", "consumed"} else "other"
        self._metric("approval_validation_failures_total", {"reason": bounded})

    @staticmethod
    def _validate_binding(
        binding: WorktreeBinding,
        request: StartRepairRequest,
        task_branch: str,
    ) -> None:
        if (
            binding.task_branch != task_branch
            or binding.repository_id != request.repository_id
            or binding.base_sha != request.base_sha
            or binding.head_sha != request.head_sha
            or not binding.original_checkout_unchanged
        ):
            raise RepairServiceError("worktree_binding_mismatch")
        if binding.task_branch.casefold() in {
            item.casefold() for item in request.policy.protected_branches
        }:
            raise RepairServiceError("protected_task_branch_denied")

    @staticmethod
    def _validate_snapshot_fields(
        snapshot: RepositorySnapshot,
        *,
        repository_id: str,
        base_sha: str,
        head_sha: str,
        binding: WorktreeBinding,
    ) -> None:
        if (
            snapshot.repository_id != repository_id
            or snapshot.base_sha != base_sha
            or snapshot.head_sha != head_sha
            or snapshot.worktree_id != binding.worktree_id
            or snapshot.task_branch != binding.task_branch
            or not snapshot.original_checkout_unchanged
        ):
            raise RepairServiceError("repository_snapshot_mismatch")

    def _validate_snapshot(
        self, job: RepairJobCheckpoint, snapshot: RepositorySnapshot
    ) -> None:
        self._validate_snapshot_fields(
            snapshot,
            repository_id=job.repository_id,
            base_sha=job.base_sha,
            head_sha=job.head_sha,
            binding=job.binding,
        )

    def _revalidate_repository(
        self, job: RepairJobCheckpoint, *, expected_diff: str
    ) -> RepositorySnapshot:
        snapshot = self.executor.inspect(job.binding)
        self._validate_snapshot(job, snapshot)
        if snapshot.diff_sha256 != expected_diff:
            raise RepairConflict("repair_repository_diff_mismatch")
        return snapshot

    @staticmethod
    def _validate_plan(
        plan: RepairPlanArtifact,
        policy: OrganizationRepairPolicy,
        attempt: int,
    ) -> None:
        if plan.revision != attempt:
            raise RepairServiceError("repair_plan_revision_mismatch")
        if not set(plan.writable_paths).issubset(policy.writable_paths):
            raise RepairServiceError("repair_plan_path_scope_denied")
        if plan.test_commands != policy.fixed_test_commands:
            raise RepairServiceError("repair_plan_test_commands_mismatch")
        try:
            patch = parse_patch(plan.patch_text)
        except PatchRejected as exc:
            raise RepairServiceError("repair_plan_patch_invalid") from exc
        if not set(patch.paths).issubset(plan.writable_paths):
            raise RepairServiceError("repair_plan_patch_scope_denied")

    def _validate_execution(
        self,
        job: RepairJobCheckpoint,
        receipt: ExecutionReceipt,
        policy: OrganizationRepairPolicy,
    ) -> None:
        self._validate_snapshot(job, receipt.snapshot)
        if (
            receipt.docker is not True
            or receipt.non_root is not True
            or receipt.network_mode != "none"
            or receipt.timeout_seconds > policy.command_timeout_seconds
            or receipt.output_limit_bytes > policy.command_output_bytes
        ):
            raise RepairServiceError("sandbox_policy_receipt_invalid")
        if tuple(item.argv for item in receipt.tests) != policy.fixed_test_commands:
            raise RepairServiceError("test_evidence_commands_mismatch")

    @staticmethod
    def _execution_to_dict(receipt: ExecutionReceipt) -> dict[str, Any]:
        return {
            "operation_id": receipt.operation_id,
            "snapshot": asdict(receipt.snapshot),
            "diff_sha256": receipt.snapshot.diff_sha256,
            "tests_sha256": receipt.tests_sha256,
            "docker": receipt.docker,
            "network_mode": receipt.network_mode,
            "non_root": receipt.non_root,
            "timeout_seconds": receipt.timeout_seconds,
            "output_limit_bytes": receipt.output_limit_bytes,
            "elapsed_seconds": receipt.elapsed_seconds,
            "tool_calls": receipt.tool_calls,
        }

    def _write_binding(
        self, job: RepairJobCheckpoint, checkpoint_sha256: str
    ) -> dict[str, Any]:
        plan = job.plan_object
        return {
            "job_id": job.job_id,
            "checkpoint_sha256": checkpoint_sha256,
            "attempt": job.attempt,
            "organization_id": job.organization_id,
            "repository_id": job.repository_id,
            "finding_sha256": job.finding_sha256,
            "base_sha": job.base_sha,
            "head_sha": job.head_sha,
            "policy_sha256": job.policy_sha256,
            "plan_sha256": job.plan_sha256,
            "patch_sha256": plan.patch_sha256,
            "current_diff_sha256": job.current_diff_sha256,
            "writable_paths": list(plan.writable_paths),
        }

    def _validate_consumed_write(self, job: RepairJobCheckpoint) -> None:
        record = job.write_approval
        binding = record.get("binding") if isinstance(record, dict) else None
        if (
            not isinstance(binding, dict)
            or record.get("status") != "consumed"
            or record.get("binding_sha256") != _hash_value(binding)
            or binding.get("attempt") != job.attempt
            or binding.get("plan_sha256") != job.plan_sha256
            or binding.get("patch_sha256") != job.plan_object.patch_sha256
            or binding.get("current_diff_sha256") != job.current_diff_sha256
            or binding.get("base_sha") != job.base_sha
            or binding.get("head_sha") != job.head_sha
            or binding.get("policy_sha256") != job.policy_sha256
        ):
            self._approval_failure("mismatch")
            raise RepairConflict("write_approval_binding_mismatch")

    def _draft_binding(
        self,
        job: RepairJobCheckpoint,
        checkpoint_sha256: str,
        plan: RepairPlanArtifact,
        budget_sha256: str,
    ) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "checkpoint_sha256": checkpoint_sha256,
            "organization_id": job.organization_id,
            "repository_id": job.repository_id,
            "finding_sha256": job.finding_sha256,
            "base_sha": job.base_sha,
            "head_sha": job.head_sha,
            "policy_sha256": job.policy_sha256,
            "diff_sha256": job.current_diff_sha256,
            "tests_sha256": job.tests_sha256,
            "budget_sha256": budget_sha256,
            "commit_message_sha256": _hash_text(plan.commit_message),
            "head_branch": job.binding.task_branch,
            "target_base": job.policy_object.draft_pr_base,
            "draft_content_sha256": self._draft_content_hash(job, plan, budget_sha256),
        }

    def _validate_consumed_draft(self, job: RepairJobCheckpoint) -> None:
        record = job.draft_approval
        binding = record.get("binding") if isinstance(record, dict) else None
        if not isinstance(binding, dict) or record.get("status") != "consumed":
            self._approval_failure("mismatch")
            raise RepairConflict("draft_pr_approval_binding_mismatch")
        plan = job.plan_object
        budget_hash = _hash_value(job.budget)
        checks = {
            "binding_sha256": record.get("binding_sha256") == _hash_value(binding),
            "base_sha": binding.get("base_sha") == job.base_sha,
            "head_sha": binding.get("head_sha") == job.head_sha,
            "policy": binding.get("policy_sha256") == job.policy_sha256,
            "diff": binding.get("diff_sha256") == job.current_diff_sha256,
            "tests": binding.get("tests_sha256") == job.tests_sha256,
            "budget": binding.get("budget_sha256") == budget_hash,
            "message": binding.get("commit_message_sha256") == _hash_text(plan.commit_message),
            "branch": binding.get("head_branch") == job.binding.task_branch,
            "target": binding.get("target_base") == job.policy_object.draft_pr_base,
            "draft": binding.get("draft_content_sha256")
            == self._draft_content_hash(job, plan, budget_hash),
        }
        if not all(checks.values()):
            self._approval_failure("mismatch")
            raise RepairConflict("draft_pr_approval_binding_mismatch")

    @staticmethod
    def _draft_content_hash(
        job: RepairJobCheckpoint,
        plan: RepairPlanArtifact,
        budget_sha256: str,
    ) -> str:
        return _hash_value(
            {
                "organization_id": job.organization_id,
                "repository_id": job.repository_id,
                "job_id": job.job_id,
                "head_branch": job.binding.task_branch,
                "target_base": job.policy_object.draft_pr_base,
                "base_sha": job.base_sha,
                "head_sha": job.head_sha,
                "diff_sha256": job.current_diff_sha256,
                "tests_sha256": job.tests_sha256,
                "budget_sha256": budget_sha256,
                "commit_message": plan.commit_message,
                "title": plan.draft_pr_title,
                "body": plan.draft_pr_body,
            }
        )

    @staticmethod
    def _validate_commit(
        job: RepairJobCheckpoint,
        plan: RepairPlanArtifact,
        receipt: CommitReceipt,
    ) -> None:
        if (
            receipt.parent_sha != job.base_sha
            or receipt.diff_sha256 != job.current_diff_sha256
            or receipt.message_sha256 != _hash_text(plan.commit_message)
            or not receipt.original_checkout_unchanged
        ):
            raise RepairServiceError("commit_receipt_mismatch")

    def _publisher_request(
        self,
        job: RepairJobCheckpoint,
        plan: RepairPlanArtifact,
        policy: OrganizationRepairPolicy,
        commit: CommitReceipt,
    ) -> DraftPrRequest:
        budget_hash = _hash_value(job.budget)
        payload = {
            "base_branch": policy.draft_pr_base,
            "base_sha": job.base_sha,
            "body": plan.draft_pr_body,
            "budget_sha256": budget_hash,
            "commit_sha": commit.commit_sha,
            "diff_sha256": job.current_diff_sha256,
            "head_branch": job.binding.task_branch,
            "head_sha": commit.commit_sha,
            "organization_id": job.organization_id,
            "repair_job_id": job.job_id,
            "repository_id": job.repository_id,
            "test_sha256": job.tests_sha256,
            "title": plan.draft_pr_title,
        }
        payload_hash = _hash_value(payload)
        return DraftPrRequest(
            organization_id=job.organization_id,
            repository_id=job.repository_id,
            repair_job_id=job.job_id,
            head_branch=job.binding.task_branch,
            base_branch=policy.draft_pr_base,
            base_sha=job.base_sha,
            head_sha=commit.commit_sha,
            commit_sha=commit.commit_sha,
            title=plan.draft_pr_title,
            body=plan.draft_pr_body,
            diff_sha256=job.current_diff_sha256,
            test_sha256=job.tests_sha256,
            budget_sha256=budget_hash,
            payload_sha256=payload_hash,
            idempotency_key=_hash_value(
                {
                    "job_id": job.job_id,
                    "approval": job.draft_approval.get("binding_sha256"),
                    "payload": payload_hash,
                }
            ),
        )

    @staticmethod
    def _public(job: RepairJobCheckpoint, checksum: str) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "job_id": job.job_id,
            "state": job.state.value,
            "sequence": job.sequence,
            "attempt": job.attempt,
            "checkpoint_sha256": checksum,
            "organization_id": job.organization_id,
            "repository_id": job.repository_id,
            "finding_sha256": job.finding_sha256,
            "base_sha": job.base_sha,
            "head_sha": job.head_sha,
            "policy_sha256": job.policy_sha256,
            "task_branch": job.binding.task_branch,
            "failure_code": job.failure_code or None,
            "lease_active": bool(job.lease_owner and job.lease_token),
            "synthetic_only": True,
            "real_writes_enabled": False,
            "business_claim_allowed": False,
            "quality_claim_allowed": False,
        }
