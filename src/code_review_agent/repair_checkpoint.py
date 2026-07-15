"""Atomic checkpoint snapshots and an append-only repair event journal."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
from threading import RLock
import time
from typing import Any, Callable
from uuid import uuid4

from code_review_agent.repair_state import RepairState


SCHEMA_VERSION = 1
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class CheckpointError(RuntimeError):
    pass


class CheckpointCorrupt(CheckpointError):
    pass


class CheckpointVersionError(CheckpointError):
    pass


class CheckpointMismatch(CheckpointError):
    def __init__(self, fields: list[str]):
        self.fields = fields
        super().__init__("checkpoint does not match runtime: " + ", ".join(fields))


def _canonical(data: Any) -> bytes:
    try:
        text = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"checkpoint contains non-JSON data: {exc}") from exc
    return text.encode("utf-8")


def _checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync after replace; unsupported on some platforms."""
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


def _required_text(data: dict[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CheckpointCorrupt(f"checkpoint field {name!r} must be a non-empty string")
    return value


def _text(data: dict[str, Any], name: str) -> str:
    value = data.get(name, "")
    if not isinstance(value, str):
        raise CheckpointCorrupt(f"checkpoint field {name!r} must be a string")
    return value


def _mapping(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise CheckpointCorrupt(f"checkpoint field {name!r} must be an object")
    return deepcopy(value)


def _records(data: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = data.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CheckpointCorrupt(f"checkpoint field {name!r} must be a list of objects")
    return deepcopy(value)


def _paths(data: dict[str, Any]) -> tuple[str, ...]:
    value = data.get("writable_paths", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CheckpointCorrupt("checkpoint field 'writable_paths' must be a list of strings")
    return tuple(value)


def _optional_mapping(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    value = data.get(name)
    if value is not None and not isinstance(value, dict):
        raise CheckpointCorrupt(f"checkpoint field {name!r} must be an object or null")
    return deepcopy(value)


@dataclass
class RepairCheckpoint:
    run_id: str
    repository_id: str
    base_sha: str
    task_branch: str
    worktree: str
    state: RepairState = RepairState.DISCOVER
    sequence: int = 0
    issue_ref: str = ""
    original_snapshot: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    writable_paths: tuple[str, ...] = ()
    plan_hash: str = ""
    status_summary: dict[str, Any] = field(default_factory=dict)
    diff_hash: str = ""
    tool_ledger: list[dict[str, Any]] = field(default_factory=list)
    test_results: list[dict[str, Any]] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    last_transition: dict[str, Any] = field(default_factory=dict)
    in_progress_operation: dict[str, Any] | None = None
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("run_id must contain only letters, digits, dot, underscore, or dash")
        for name in ("repository_id", "base_sha", "task_branch", "worktree"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        if not _finite_nonnegative(self.updated_at):
            raise ValueError("updated_at must be finite and non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "repository_id": self.repository_id,
            "base_sha": self.base_sha,
            "task_branch": self.task_branch,
            "worktree": self.worktree,
            "state": self.state.value,
            "sequence": self.sequence,
            "issue_ref": self.issue_ref,
            "original_snapshot": deepcopy(self.original_snapshot),
            "plan": deepcopy(self.plan),
            "writable_paths": list(self.writable_paths),
            "plan_hash": self.plan_hash,
            "status_summary": deepcopy(self.status_summary),
            "diff_hash": self.diff_hash,
            "tool_ledger": deepcopy(self.tool_ledger),
            "test_results": deepcopy(self.test_results),
            "budget": deepcopy(self.budget),
            "approvals": deepcopy(self.approvals),
            "last_transition": deepcopy(self.last_transition),
            "in_progress_operation": deepcopy(self.in_progress_operation),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepairCheckpoint":
        if not isinstance(data, dict):
            raise CheckpointCorrupt("checkpoint payload must be a JSON object")
        try:
            state = RepairState(data.get("state"))
        except (TypeError, ValueError) as exc:
            raise CheckpointCorrupt(f"invalid repair state: {data.get('state')!r}") from exc
        try:
            return cls(
                run_id=_required_text(data, "run_id"),
                repository_id=_required_text(data, "repository_id"),
                base_sha=_required_text(data, "base_sha"),
                task_branch=_required_text(data, "task_branch"),
                worktree=_required_text(data, "worktree"),
                state=state,
                sequence=data.get("sequence", 0),
                issue_ref=_text(data, "issue_ref"),
                original_snapshot=_mapping(data, "original_snapshot"),
                plan=_mapping(data, "plan"),
                writable_paths=_paths(data),
                plan_hash=_text(data, "plan_hash"),
                status_summary=_mapping(data, "status_summary"),
                diff_hash=_text(data, "diff_hash"),
                tool_ledger=_records(data, "tool_ledger"),
                test_results=_records(data, "test_results"),
                budget=_mapping(data, "budget"),
                approvals=_records(data, "approvals"),
                last_transition=_mapping(data, "last_transition"),
                in_progress_operation=_optional_mapping(data, "in_progress_operation"),
                updated_at=float(data.get("updated_at", 0.0)),
            )
        except (TypeError, ValueError) as exc:
            raise CheckpointCorrupt(f"invalid checkpoint payload: {exc}") from exc

    def assert_matches(
        self,
        *,
        repository_id: str,
        base_sha: str,
        task_branch: str,
        worktree: str,
        status_summary: dict[str, Any],
        diff_hash: str,
    ) -> None:
        mismatches = []
        expected = {
            "repository_id": repository_id,
            "base_sha": base_sha,
            "task_branch": task_branch,
            "worktree": os.path.normcase(os.path.abspath(worktree)),
            "status_summary": status_summary,
            "diff_hash": diff_hash,
        }
        actual = {
            "repository_id": self.repository_id,
            "base_sha": self.base_sha,
            "task_branch": self.task_branch,
            "worktree": os.path.normcase(os.path.abspath(self.worktree)),
            "status_summary": self.status_summary,
            "diff_hash": self.diff_hash,
        }
        for name, expected_value in expected.items():
            if actual[name] != expected_value:
                mismatches.append(name)
        if mismatches:
            raise CheckpointMismatch(mismatches)


class CheckpointStore:
    def __init__(self, state_root: Path, *, clock: Callable[[], float] = time.time):
        self.state_root = Path(state_root)
        self._clock = clock
        self._lock = RLock()

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run_id")

    def _run_dir(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        return self.state_root / run_id

    def snapshot_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "checkpoint.json"

    def journal_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "events.jsonl"

    def save(self, checkpoint: RepairCheckpoint) -> str:
        with self._lock:
            run_dir = self._run_dir(checkpoint.run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            payload = checkpoint.to_dict()
            checksum = _checksum(payload)
            envelope = {
                "schema_version": SCHEMA_VERSION,
                "checksum": checksum,
                "checkpoint": payload,
            }
            target = self.snapshot_path(checkpoint.run_id)
            temporary = run_dir / f".checkpoint.{uuid4().hex}.tmp"
            data = json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                _fsync_directory(run_dir)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            self.append_event(
                checkpoint.run_id,
                "checkpoint_saved",
                {
                    "sequence": checkpoint.sequence,
                    "state": checkpoint.state.value,
                    "checksum": checksum,
                },
            )
            return checksum

    def load(self, run_id: str) -> RepairCheckpoint:
        with self._lock:
            return self._load_unlocked(run_id)

    def _load_unlocked(self, run_id: str) -> RepairCheckpoint:
        path = self.snapshot_path(run_id)
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CheckpointError(f"checkpoint not found for run {run_id!r}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointCorrupt(f"cannot read checkpoint: {exc}") from exc
        if not isinstance(envelope, dict):
            raise CheckpointCorrupt("checkpoint envelope must be a JSON object")
        version = envelope.get("schema_version")
        if version != SCHEMA_VERSION:
            raise CheckpointVersionError(
                f"unsupported checkpoint schema {version!r}; expected {SCHEMA_VERSION}"
            )
        payload = envelope.get("checkpoint")
        checksum = envelope.get("checksum")
        if not isinstance(payload, dict) or not isinstance(checksum, str):
            raise CheckpointCorrupt("checkpoint envelope is missing payload or checksum")
        try:
            actual_checksum = _checksum(payload)
        except ValueError as exc:
            raise CheckpointCorrupt(f"checkpoint payload is not canonical JSON: {exc}") from exc
        if not secrets_compare(checksum, actual_checksum):
            raise CheckpointCorrupt("checkpoint checksum mismatch")
        checkpoint = RepairCheckpoint.from_dict(payload)
        if checkpoint.run_id != run_id:
            raise CheckpointCorrupt("checkpoint run_id does not match its state directory")
        return checkpoint

    def append_event(self, run_id: str, kind: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._validate_run_id(run_id)
            if not isinstance(kind, str) or not kind.strip():
                raise ValueError("event kind must be a non-empty string")
            run_dir = self._run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            record = {"t": float(self._clock()), "kind": kind, "data": deepcopy(data)}
            line = _canonical(record).decode("utf-8") + "\n"
            with self.journal_path(run_id).open(
                "a", encoding="utf-8", newline="\n"
            ) as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())

    def events(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            path = self.journal_path(run_id)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
            events = []
            for line_number, line in enumerate(lines, 1):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CheckpointCorrupt(
                        f"invalid journal JSON at line {line_number}: {exc}"
                    ) from exc
                if not isinstance(event, dict):
                    raise CheckpointCorrupt(f"journal line {line_number} is not an object")
                events.append(event)
            return events


def secrets_compare(left: str, right: str) -> bool:
    """Constant-time checksum comparison without storing any secret material."""
    return hmac.compare_digest(left, right)


def _finite_nonnegative(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0
