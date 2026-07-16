"""Sandboxed Git, patch, test, and rollback tools for repair runs."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
from threading import RLock
from typing import Callable, Protocol, Sequence, TypeVar
from uuid import uuid4

from code_review_agent.repair_approval import (
    ApprovalBinding,
    ApprovalKind,
    ApprovalRecord,
    normalize_repo_paths,
)
from code_review_agent.repair_budget import BudgetManager
from code_review_agent.sandbox import (
    CommandPolicy,
    DockerSandboxRunner,
    ProcessExecutor,
    ReadOnlyMount,
    SandboxResult,
    SandboxTimeout,
    WritableMount,
)


GIT_PREFIX = (
    "git",
    "-c",
    "safe.directory=/workspace",
    "-c",
    "core.hooksPath=/dev/null",
)
STATUS_COMMAND = GIT_PREFIX + (
    "status",
    "--porcelain=v1",
    "-z",
    "--untracked-files=all",
    "--ignored=traditional",
)
IGNORED_HASH_COMMAND = GIT_PREFIX + ("hash-object", "--stdin-paths")
APPLY_CHECK_COMMAND = GIT_PREFIX + (
    "apply",
    "--check",
    "--recount",
    "--whitespace=error-all",
    "-",
)
APPLY_COMMAND = GIT_PREFIX + (
    "apply",
    "--recount",
    "--whitespace=error-all",
    "-",
)
REVERSE_CHECK_COMMAND = GIT_PREFIX + (
    "apply",
    "--check",
    "--reverse",
    "--recount",
    "--whitespace=error-all",
    "-",
)
REVERSE_COMMAND = GIT_PREFIX + (
    "apply",
    "--reverse",
    "--recount",
    "--whitespace=error-all",
    "-",
)


class RepairToolError(RuntimeError):
    pass


class GitToolError(RepairToolError):
    pass


class PatchRejected(RepairToolError):
    pass


class PatchScopeError(RepairToolError):
    pass


class SnapshotMismatch(RepairToolError):
    pass


class ToolPersistenceError(RepairToolError):
    pass


class ToolQuarantined(RepairToolError):
    pass


class DiffScope(str, Enum):
    BASE = "base"
    STAGED = "staged"
    WORKTREE = "worktree"


class ManifestState(str, Enum):
    INTENT = "intent"
    APPLIED = "applied"
    REJECTED = "rejected"
    ROLLBACK_INTENT = "rollback_intent"
    ROLLED_BACK = "rolled_back"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class StatusEntry:
    index_status: str
    worktree_status: str
    path: str
    original_path: str = ""

    def __post_init__(self) -> None:
        if len(self.index_status) != 1 or len(self.worktree_status) != 1:
            raise ValueError("Git status codes must be one character")
        normalized = normalize_repo_paths((self.path,))[0]
        object.__setattr__(self, "path", normalized)
        if self.original_path:
            original = normalize_repo_paths((self.original_path,))[0]
            object.__setattr__(self, "original_path", original)


@dataclass(frozen=True)
class GitStatusResult:
    operation_id: str
    entries: tuple[StatusEntry, ...]
    ignored_paths: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.entries

    @property
    def paths(self) -> tuple[str, ...]:
        paths = set()
        for entry in self.entries:
            paths.add(entry.path)
            if entry.original_path:
                paths.add(entry.original_path)
        return tuple(sorted(paths))

    @property
    def untracked_paths(self) -> tuple[str, ...]:
        return tuple(sorted(entry.path for entry in self.entries if _is_untracked(entry)))


@dataclass(frozen=True)
class GitDiffResult:
    operation_id: str
    scope: DiffScope
    paths: tuple[str, ...]
    text: str = field(repr=False)
    sha256: str


@dataclass(frozen=True)
class RepairRepositorySnapshot:
    status: GitStatusResult
    base_diff: str = field(repr=False)
    untracked_diffs: tuple[tuple[str, str], ...] = field(repr=False)
    sha256: str


@dataclass(frozen=True)
class PatchDocument:
    text: str = field(repr=False)
    sha256: str
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("patch document text must be non-empty")
        if self.sha256 != hashlib.sha256(self.text.encode("utf-8")).hexdigest():
            raise ValueError("patch document hash does not match its text")
        normalized = normalize_repo_paths(self.paths)
        if not normalized:
            raise ValueError("patch document must contain at least one path")
        object.__setattr__(self, "paths", normalized)

    def to_dict(self) -> dict[str, object]:
        return {"text": self.text, "sha256": self.sha256, "paths": list(self.paths)}

    @classmethod
    def from_dict(cls, data: object) -> "PatchDocument":
        if not isinstance(data, dict):
            raise ValueError("patch document must be an object")
        try:
            text = data["text"]
            sha256 = data["sha256"]
            paths = data["paths"]
            if not isinstance(text, str) or not isinstance(sha256, str):
                raise ValueError("patch document text and sha256 must be strings")
            if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
                raise ValueError("patch document paths must be a list of strings")
            return cls(text, sha256, tuple(paths))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid patch document: {exc}") from exc


@dataclass(frozen=True)
class PatchPreflightResult:
    patch: PatchDocument = field(repr=False)
    snapshot_hash: str
    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.patch, PatchDocument):
            raise ValueError("preflight patch must be a PatchDocument")
        if not _is_sha256(self.snapshot_hash):
            raise ValueError("preflight snapshot_hash must be SHA-256")
        if not isinstance(self.operation_id, str) or not self.operation_id:
            raise ValueError("preflight operation_id must be non-empty")


@dataclass(frozen=True)
class PatchManifest:
    manifest_id: str
    run_id: str
    state: ManifestState
    patch: PatchDocument = field(repr=False)
    before_snapshot_hash: str
    after_snapshot_hash: str = ""
    approval_receipt: str = ""
    persistence_receipt: str = ""
    preflight_operation_id: str = ""
    mutation_operation_id: str = ""
    rollback_token: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.manifest_id, str) or not self.manifest_id:
            raise ValueError("manifest_id must be non-empty")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("manifest run_id must be non-empty")
        object.__setattr__(self, "state", ManifestState(self.state))
        if not isinstance(self.patch, PatchDocument):
            raise ValueError("manifest patch must be a PatchDocument")
        if not _is_sha256(self.before_snapshot_hash):
            raise ValueError("manifest before_snapshot_hash must be SHA-256")
        if self.after_snapshot_hash and not _is_sha256(self.after_snapshot_hash):
            raise ValueError("manifest after_snapshot_hash must be SHA-256")
        if self.state in {
            ManifestState.APPLIED,
            ManifestState.ROLLBACK_INTENT,
            ManifestState.ROLLED_BACK,
        } and not self.after_snapshot_hash:
            raise ValueError("manifest state requires after_snapshot_hash")
        if self.state in {ManifestState.INTENT, ManifestState.APPLIED} and not self.rollback_token:
            raise ValueError("active mutation manifest requires a rollback token")

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_id": self.manifest_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "patch": self.patch.to_dict(),
            "before_snapshot_hash": self.before_snapshot_hash,
            "after_snapshot_hash": self.after_snapshot_hash,
            "approval_receipt": self.approval_receipt,
            "persistence_receipt": self.persistence_receipt,
            "preflight_operation_id": self.preflight_operation_id,
            "mutation_operation_id": self.mutation_operation_id,
            "rollback_token": self.rollback_token,
        }

    @classmethod
    def from_dict(cls, data: object) -> "PatchManifest":
        if not isinstance(data, dict):
            raise ValueError("patch manifest must be an object")
        try:
            required_text = ("manifest_id", "run_id", "state", "before_snapshot_hash")
            optional_text = (
                "after_snapshot_hash",
                "approval_receipt",
                "persistence_receipt",
                "preflight_operation_id",
                "mutation_operation_id",
                "rollback_token",
            )
            text_fields = {name: data[name] for name in required_text}
            text_fields.update({name: data.get(name, "") for name in optional_text})
            if any(not isinstance(value, str) for value in text_fields.values()):
                raise ValueError("patch manifest text fields must be strings")
            return cls(
                manifest_id=text_fields["manifest_id"],
                run_id=text_fields["run_id"],
                state=ManifestState(text_fields["state"]),
                patch=PatchDocument.from_dict(data["patch"]),
                before_snapshot_hash=text_fields["before_snapshot_hash"],
                after_snapshot_hash=text_fields["after_snapshot_hash"],
                approval_receipt=text_fields["approval_receipt"],
                persistence_receipt=text_fields["persistence_receipt"],
                preflight_operation_id=text_fields["preflight_operation_id"],
                mutation_operation_id=text_fields["mutation_operation_id"],
                rollback_token=text_fields["rollback_token"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid patch manifest: {exc}") from exc


@dataclass(frozen=True)
class TestCommandResult:
    argv: tuple[str, ...]
    operation_id: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    output_truncated: bool


class ToolSandbox(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        stdin_bytes: bytes | None = None,
    ) -> SandboxResult: ...


ApprovalPersister = Callable[[ApprovalRecord], str]
ManifestPersister = Callable[[PatchManifest], str]
BudgetPersister = Callable[[str], object]
T = TypeVar("T")


class RepairTools:
    """One-run tool facade with budget, approval, and snapshot enforcement."""

    def __init__(
        self,
        *,
        run_id: str,
        base_sha: str,
        writable_paths: Sequence[str],
        sandbox: ToolSandbox,
        budget: BudgetManager,
        persist_approval: ApprovalPersister,
        persist_manifest: ManifestPersister,
        persist_budget: BudgetPersister,
    ):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be non-empty")
        if not _is_object_id(base_sha):
            raise ValueError("base_sha must be a hexadecimal object id")
        self.run_id = run_id
        self.base_sha = base_sha.lower()
        self.writable_paths = normalize_repo_paths(writable_paths)
        if not self.writable_paths:
            raise ValueError("repair tools require at least one writable path")
        if (
            not callable(persist_approval)
            or not callable(persist_manifest)
            or not callable(persist_budget)
        ):
            raise ValueError("durable approval, manifest, and budget persisters are required")
        self.sandbox = sandbox
        self.budget = budget
        self._persist_approval = persist_approval
        self._persist_manifest = persist_manifest
        self._persist_budget = persist_budget
        self._mutation_lock = RLock()

    def git_status(self) -> GitStatusResult:
        return self._invoke_tool("git_status", self._git_status)

    def repository_snapshot(self) -> RepairRepositorySnapshot:
        """Control-plane snapshot for checkpoint/recovery; not model-callable."""
        return self._snapshot()

    def git_diff(
        self,
        scope: DiffScope = DiffScope.BASE,
        *,
        paths: Sequence[str] = (),
    ) -> GitDiffResult:
        return self._invoke_tool(
            "git_diff", lambda: self._git_diff(scope, paths=paths)
        )

    def read_approved_sources(
        self,
        *,
        paths: Sequence[str] = (),
        max_file_bytes: int = 32 * 1024,
        max_total_bytes: int = 64 * 1024,
    ) -> dict[str, str]:
        """Read bounded base-revision text only from the approved path set."""
        return self._invoke_tool(
            "read_approved_sources",
            lambda: self._read_approved_sources(
                paths=paths,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
            ),
        )

    def _read_approved_sources(
        self,
        *,
        paths: Sequence[str],
        max_file_bytes: int,
        max_total_bytes: int,
    ) -> dict[str, str]:
        selected = normalize_repo_paths(paths) if paths else self.writable_paths
        if not set(selected).issubset(self.writable_paths):
            raise PatchScopeError("source path is outside the approved writable set")
        for name, value in (
            ("per-file source limit", max_file_bytes),
            ("total source limit", max_total_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if max_file_bytes > max_total_bytes:
            raise ValueError("per-file source limit cannot exceed total source limit")

        sources: dict[str, str] = {}
        total_bytes = 0
        for path in selected:
            result = self._run(_source_command(self.base_sha, path))
            if result.exit_code != 0:
                raise GitToolError(
                    result.stderr or result.stdout or f"cannot read base source: {path}"
                )
            if result.output_truncated:
                raise GitToolError("source output exceeded the sandbox safety limit")
            if "\x00" in result.stdout:
                raise GitToolError(f"base source is not UTF-8 text: {path}")
            source = _bounded_source_tail(result.stdout, path, max_file_bytes)
            size = len(source.encode("utf-8"))
            total_bytes += size
            if total_bytes > max_total_bytes:
                raise GitToolError("approved base sources exceed the total context limit")
            sources[path] = source
        return sources

    def run_command(
        self, argv: Sequence[str], *, timeout_seconds: float | None = None
    ) -> SandboxResult:
        return self._invoke_tool(
            "run_command",
            lambda: self._run_command(argv, timeout_seconds=timeout_seconds),
        )

    def _run_command(
        self, argv: Sequence[str], *, timeout_seconds: float | None
    ) -> SandboxResult:
        before = self._snapshot()
        try:
            result = self._run(argv, timeout_seconds=timeout_seconds)
        except Exception as exc:
            self._assert_unchanged_after_command(before, "failed run_command", exc)
            raise
        self._assert_unchanged_after_command(before, "run_command")
        return result

    def run_tests(
        self,
        commands: Sequence[Sequence[str]],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[TestCommandResult, ...]:
        return self._invoke_tool(
            "run_tests",
            lambda: self._run_tests(commands, timeout_seconds=timeout_seconds),
        )

    def _run_tests(
        self,
        commands: Sequence[Sequence[str]],
        *,
        timeout_seconds: float | None,
    ) -> tuple[TestCommandResult, ...]:
        if isinstance(commands, (str, bytes)) or not commands:
            raise ValueError("test commands must be a non-empty sequence")
        results = []
        before = self._snapshot()
        for argv in commands:
            command = tuple(argv)
            try:
                result = self._run(command, timeout_seconds=timeout_seconds)
            except SandboxTimeout as exc:
                results.append(
                    TestCommandResult(
                        argv=command,
                        operation_id=exc.operation_id,
                        exit_code=None,
                        stdout=exc.stdout,
                        stderr=exc.stderr,
                        duration_seconds=timeout_seconds or 0.0,
                        timed_out=True,
                        output_truncated=False,
                    )
                )
                self._assert_unchanged_after_command(before, "timed-out test command", exc)
                break
            results.append(_test_result(result))
            self._assert_unchanged_after_command(before, "test command")
            if result.exit_code != 0:
                break
        return tuple(results)

    def apply_patch(
        self,
        patch_text: str,
        *,
        approval: ApprovalRecord,
        checkpoint_id: str,
        plan_hash: str,
        patch_attempt: int,
        now: float | None = None,
    ) -> tuple[ApprovalRecord, PatchManifest]:
        return self._invoke_tool(
            "apply_patch",
            lambda: self._apply_patch(
                patch_text,
                approval=approval,
                checkpoint_id=checkpoint_id,
                plan_hash=plan_hash,
                patch_attempt=patch_attempt,
                now=now,
            ),
        )

    def preflight_patch(self, patch_text: str) -> PatchPreflightResult:
        """Validate an exact candidate without consuming approval or writing files."""
        return self._invoke_tool(
            "preflight_patch", lambda: self._preflight_patch(patch_text)
        )

    def _preflight_patch(self, patch_text: str) -> PatchPreflightResult:
        document = parse_patch(patch_text)
        if not set(document.paths).issubset(self.writable_paths):
            raise PatchScopeError("patch touches a path outside the approved writable set")
        before = self._snapshot()
        self._assert_status_scope(before.status)
        try:
            result = self._run(
                APPLY_CHECK_COMMAND, stdin_bytes=patch_text.encode("utf-8")
            )
        except BaseException as exc:
            self._assert_unchanged_after_command(
                before, "failed patch preflight", exc
            )
            raise
        self._assert_unchanged_after_command(before, "patch preflight")
        if result.exit_code != 0:
            raise PatchRejected(
                result.stderr or result.stdout or "patch preflight failed"
            )
        return PatchPreflightResult(document, before.sha256, result.operation_id)

    def _apply_patch(
        self,
        patch_text: str,
        *,
        approval: ApprovalRecord,
        checkpoint_id: str,
        plan_hash: str,
        patch_attempt: int,
        now: float | None,
    ) -> tuple[ApprovalRecord, PatchManifest]:
        document = parse_patch(patch_text)
        if not set(document.paths).issubset(self.writable_paths):
            raise PatchScopeError("patch touches a path outside the approved writable set")
        with self._mutation_lock:
            before = self._snapshot()
            self._assert_status_scope(before.status)
            expected = ApprovalBinding(
                kind=ApprovalKind.WRITE,
                run_id=self.run_id,
                checkpoint_id=checkpoint_id,
                base_sha=self.base_sha,
                diff_hash=before.sha256,
                nonce=approval.binding.nonce,
                plan_hash=plan_hash,
                patch_hash=document.sha256,
                writable_paths=self.writable_paths,
                patch_attempt=patch_attempt,
            )
            consumed = approval.consume(expected, now=now)
            approval_receipt = _require_receipt(
                self._persist_approval(consumed), "consumed approval"
            )
            intent = PatchManifest(
                manifest_id=uuid4().hex,
                run_id=self.run_id,
                state=ManifestState.INTENT,
                patch=document,
                before_snapshot_hash=before.sha256,
                approval_receipt=approval_receipt,
                rollback_token=secrets.token_urlsafe(24),
            )
            intent = self._persisted(intent)
            patch_bytes = patch_text.encode("utf-8")
            preflight = self._run(APPLY_CHECK_COMMAND, stdin_bytes=patch_bytes)
            if preflight.exit_code != 0:
                rejected = replace(
                    intent,
                    state=ManifestState.REJECTED,
                    preflight_operation_id=preflight.operation_id,
                )
                self._persisted(rejected)
                raise PatchRejected(preflight.stderr or preflight.stdout or "patch preflight failed")
            after_preflight = self._snapshot()
            if after_preflight.sha256 != before.sha256:
                quarantined = replace(
                    intent,
                    state=ManifestState.QUARANTINED,
                    after_snapshot_hash=after_preflight.sha256,
                    preflight_operation_id=preflight.operation_id,
                )
                self._persisted(quarantined)
                raise ToolQuarantined("repository changed during patch preflight")
            try:
                mutation = self._run(APPLY_COMMAND, stdin_bytes=patch_bytes)
            except Exception as exc:
                self._reconcile_failed_mutation(
                    intent,
                    before,
                    preflight_operation_id=preflight.operation_id,
                    unchanged_state=ManifestState.REJECTED,
                    cause=exc,
                )
                raise
            try:
                after = self._snapshot()
            except Exception as exc:
                quarantined = replace(
                    intent,
                    state=ManifestState.QUARANTINED,
                    preflight_operation_id=preflight.operation_id,
                    mutation_operation_id=mutation.operation_id,
                )
                try:
                    self._persisted(quarantined)
                except Exception:
                    pass
                raise ToolQuarantined(
                    "patch command completed but repository state cannot be verified"
                ) from exc
            if mutation.exit_code != 0:
                state = (
                    ManifestState.REJECTED
                    if after.sha256 == before.sha256
                    else ManifestState.QUARANTINED
                )
                failed = replace(
                    intent,
                    state=state,
                    after_snapshot_hash=after.sha256,
                    preflight_operation_id=preflight.operation_id,
                    mutation_operation_id=mutation.operation_id,
                )
                self._persisted(failed)
                if state is ManifestState.QUARANTINED:
                    raise ToolQuarantined("failed patch command changed the repository")
                raise PatchRejected(mutation.stderr or mutation.stdout or "patch apply failed")
            try:
                self._assert_status_scope(after.status)
            except PatchScopeError as exc:
                quarantined = replace(
                    intent,
                    state=ManifestState.QUARANTINED,
                    after_snapshot_hash=after.sha256,
                    preflight_operation_id=preflight.operation_id,
                    mutation_operation_id=mutation.operation_id,
                )
                self._persisted(quarantined)
                raise ToolQuarantined("patch changed a path outside its approval") from exc
            applied = replace(
                intent,
                state=ManifestState.APPLIED,
                after_snapshot_hash=after.sha256,
                preflight_operation_id=preflight.operation_id,
                mutation_operation_id=mutation.operation_id,
            )
            try:
                applied = self._persisted(applied)
            except Exception as exc:
                raise ToolQuarantined(
                    "patch applied but its completed manifest was not persisted"
                ) from exc
            return consumed, applied

    def rollback(self, manifest: PatchManifest, *, rollback_token: str) -> PatchManifest:
        return self._invoke_tool(
            "rollback",
            lambda: self._rollback(manifest, rollback_token=rollback_token),
        )

    def _rollback(self, manifest: PatchManifest, *, rollback_token: str) -> PatchManifest:
        if manifest.run_id != self.run_id:
            raise PatchScopeError("rollback manifest belongs to another run")
        if manifest.state is not ManifestState.APPLIED:
            raise PatchRejected("only an applied, unconsumed manifest can be rolled back")
        if not secrets.compare_digest(manifest.rollback_token, rollback_token):
            raise PatchRejected("rollback token does not match the mutation manifest")
        with self._mutation_lock:
            current = self._snapshot()
            if current.sha256 != manifest.after_snapshot_hash:
                raise SnapshotMismatch("repository no longer matches the patch manifest")
            rollback_intent = self._persisted(
                replace(manifest, state=ManifestState.ROLLBACK_INTENT)
            )
            patch_bytes = manifest.patch.text.encode("utf-8")
            preflight = self._run(REVERSE_CHECK_COMMAND, stdin_bytes=patch_bytes)
            if preflight.exit_code != 0:
                applied_again = replace(rollback_intent, state=ManifestState.APPLIED)
                self._persisted(applied_again)
                raise PatchRejected(
                    preflight.stderr or preflight.stdout or "rollback preflight failed"
                )
            try:
                mutation = self._run(REVERSE_COMMAND, stdin_bytes=patch_bytes)
            except Exception as exc:
                self._reconcile_failed_mutation(
                    rollback_intent,
                    current,
                    preflight_operation_id=preflight.operation_id,
                    unchanged_state=ManifestState.APPLIED,
                    cause=exc,
                )
                raise
            after = self._snapshot()
            if mutation.exit_code != 0 or after.sha256 != manifest.before_snapshot_hash:
                quarantined = replace(
                    rollback_intent,
                    state=ManifestState.QUARANTINED,
                    after_snapshot_hash=after.sha256,
                    preflight_operation_id=preflight.operation_id,
                    mutation_operation_id=mutation.operation_id,
                )
                self._persisted(quarantined)
                raise ToolQuarantined("rollback could not restore the exact pre-patch snapshot")
            rolled_back = replace(
                rollback_intent,
                state=ManifestState.ROLLED_BACK,
                after_snapshot_hash=after.sha256,
                preflight_operation_id=preflight.operation_id,
                mutation_operation_id=mutation.operation_id,
                rollback_token="",
            )
            return self._persisted(rolled_back)

    def reconcile_manifest(self, manifest: PatchManifest) -> PatchManifest:
        """Reconcile an interrupted patch/rollback without replaying a write."""
        if manifest.run_id != self.run_id:
            raise PatchScopeError("manifest belongs to another repair run")
        with self._mutation_lock:
            current = self._snapshot()
            if manifest.state is ManifestState.INTENT:
                if current.sha256 == manifest.before_snapshot_hash:
                    return self._persisted(
                        replace(
                            manifest,
                            state=ManifestState.REJECTED,
                            after_snapshot_hash=current.sha256,
                        )
                    )
                if not set(current.status.paths).issubset(manifest.patch.paths):
                    return self._persisted(
                        replace(
                            manifest,
                            state=ManifestState.QUARANTINED,
                            after_snapshot_hash=current.sha256,
                        )
                    )
                reverse_check = self._run(
                    REVERSE_CHECK_COMMAND,
                    stdin_bytes=manifest.patch.text.encode("utf-8"),
                )
                state = (
                    ManifestState.APPLIED
                    if reverse_check.exit_code == 0
                    else ManifestState.QUARANTINED
                )
                return self._persisted(
                    replace(
                        manifest,
                        state=state,
                        after_snapshot_hash=current.sha256,
                        preflight_operation_id=reverse_check.operation_id,
                    )
                )
            if manifest.state is ManifestState.ROLLBACK_INTENT:
                if current.sha256 == manifest.before_snapshot_hash:
                    state = ManifestState.ROLLED_BACK
                    token = ""
                elif current.sha256 == manifest.after_snapshot_hash:
                    state = ManifestState.APPLIED
                    token = manifest.rollback_token
                else:
                    state = ManifestState.QUARANTINED
                    token = manifest.rollback_token
                return self._persisted(
                    replace(manifest, state=state, rollback_token=token)
                )
            return manifest

    def _git_status(self) -> GitStatusResult:
        result = self._run(STATUS_COMMAND)
        if result.exit_code != 0:
            raise GitToolError(result.stderr or result.stdout or "git status failed")
        if result.output_truncated:
            raise GitToolError("git status output exceeded its safety limit")
        parsed = parse_porcelain_v1_z(result.stdout)
        ignored_paths = tuple(sorted(entry.path for entry in parsed if _is_ignored(entry)))
        visible = tuple(entry for entry in parsed if not _is_ignored(entry))
        return GitStatusResult(result.operation_id, visible, ignored_paths)

    def _git_diff(
        self, scope: DiffScope, *, paths: Sequence[str] = ()
    ) -> GitDiffResult:
        selected = normalize_repo_paths(paths)
        if selected and not set(selected).issubset(self.writable_paths):
            raise PatchScopeError("git_diff path is outside the approved writable set")
        scope = DiffScope(scope)
        command = _diff_command(scope, self.base_sha, selected)
        result = self._run(command)
        if result.exit_code != 0:
            raise GitToolError(result.stderr or result.stdout or "git diff failed")
        if result.output_truncated:
            raise GitToolError("git diff output exceeded its safety limit")
        return GitDiffResult(
            operation_id=result.operation_id,
            scope=scope,
            paths=selected,
            text=result.stdout,
            sha256=_sha256_text(result.stdout),
        )

    def _snapshot(self) -> RepairRepositorySnapshot:
        status = self._git_status()
        base = self._git_diff(DiffScope.BASE)
        untracked = []
        for path in status.untracked_paths:
            if path not in self.writable_paths:
                continue
            result = self._run(_untracked_diff_command(path))
            if result.exit_code not in (0, 1):
                raise GitToolError(result.stderr or result.stdout or "untracked diff failed")
            if result.output_truncated:
                raise GitToolError("untracked diff output exceeded its safety limit")
            untracked.append((path, result.stdout))
        ignored_hashes = self._ignored_hashes(status.ignored_paths)
        payload = {
            "status": [
                {
                    "index": entry.index_status,
                    "worktree": entry.worktree_status,
                    "path": entry.path,
                    "original_path": entry.original_path,
                }
                for entry in status.entries
            ],
            "base_diff": base.text,
            "untracked_diffs": untracked,
        }
        if ignored_hashes:
            payload["ignored_hashes"] = ignored_hashes
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return RepairRepositorySnapshot(status, base.text, tuple(untracked), digest)

    def _ignored_hashes(self, paths: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        if not paths:
            return ()
        stdin_bytes = "".join(f"{path}\n" for path in paths).encode("utf-8")
        result = self._run(IGNORED_HASH_COMMAND, stdin_bytes=stdin_bytes)
        if result.exit_code != 0:
            raise GitToolError(result.stderr or result.stdout or "ignored file hash failed")
        if result.output_truncated:
            raise GitToolError("ignored file hashes exceeded the sandbox safety limit")
        hashes = result.stdout.splitlines()
        if len(hashes) != len(paths) or any(not _is_object_id(item) for item in hashes):
            raise GitToolError("ignored file hash output is malformed")
        return tuple((path, digest.lower()) for path, digest in zip(paths, hashes))

    def _assert_status_scope(self, status: GitStatusResult) -> None:
        outside = sorted(set(status.paths) - set(self.writable_paths))
        if outside:
            raise PatchScopeError(
                "repository contains changes outside the approved writable set: "
                + ", ".join(outside)
            )

    def _persisted(self, manifest: PatchManifest) -> PatchManifest:
        receipt = _require_receipt(self._persist_manifest(manifest), "patch manifest")
        return replace(manifest, persistence_receipt=receipt)

    def _assert_unchanged_after_command(
        self,
        before: RepairRepositorySnapshot,
        label: str,
        cause: BaseException | None = None,
    ) -> None:
        try:
            after = self._snapshot()
        except Exception as exc:
            raise ToolQuarantined(f"{label} left repository state unverifiable") from (
                cause or exc
            )
        if after.sha256 != before.sha256:
            raise ToolQuarantined(f"{label} mutated the repair worktree") from cause

    def _reconcile_failed_mutation(
        self,
        manifest: PatchManifest,
        before: RepairRepositorySnapshot,
        *,
        preflight_operation_id: str,
        unchanged_state: ManifestState,
        cause: Exception,
    ) -> None:
        try:
            after = self._snapshot()
        except Exception as exc:
            raise ToolQuarantined("failed mutation left repository state unverifiable") from exc
        state = (
            unchanged_state
            if after.sha256 == before.sha256
            else ManifestState.QUARANTINED
        )
        reconciled = replace(
            manifest,
            state=state,
            after_snapshot_hash=after.sha256,
            preflight_operation_id=preflight_operation_id,
        )
        self._persisted(reconciled)
        if state is ManifestState.QUARANTINED:
            raise ToolQuarantined("failed mutation changed the repair worktree") from cause

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        stdin_bytes: bytes | None = None,
    ) -> SandboxResult:
        self.budget.consume_command()
        self._persist_budget("command_consumed")
        try:
            result = self.sandbox.run(
                argv,
                timeout_seconds=timeout_seconds,
                stdin_bytes=stdin_bytes,
            )
        except BaseException:
            self._persist_budget("command_interrupted")
            raise
        self._persist_budget("command_completed")
        return result

    def _invoke_tool(self, name: str, invoke: Callable[[], T]) -> T:
        self.budget.consume_tool_call()
        self._persist_budget(f"tool_{name}_consumed")
        try:
            result = invoke()
        except BaseException:
            self._persist_budget(f"tool_{name}_interrupted")
            raise
        self._persist_budget(f"tool_{name}_completed")
        return result


@dataclass(frozen=True)
class GitLayout:
    worktree: Path
    git_dir: Path
    common_dir: Path
    git_dir_relative_to_common: str

    @classmethod
    def discover(cls, worktree: Path) -> "GitLayout":
        root = _canonical_directory(worktree, "repair worktree")
        marker = root / ".git"
        if not marker.is_file():
            raise GitToolError("repair worktree must use a linked-worktree .git file")
        line = _read_small_text(marker, ".git file").strip()
        if not line.startswith("gitdir: "):
            raise GitToolError("linked-worktree .git file is malformed")
        raw_git_dir = Path(line[8:])
        if not raw_git_dir.is_absolute():
            raw_git_dir = marker.parent / raw_git_dir
        git_dir = _canonical_directory(raw_git_dir, "worktree Git directory")
        commondir_file = git_dir / "commondir"
        if commondir_file.is_file():
            raw_common = Path(_read_small_text(commondir_file, "commondir file").strip())
            if not raw_common.is_absolute():
                raw_common = git_dir / raw_common
            common = _canonical_directory(raw_common, "common Git directory")
        else:
            common = git_dir
        if common.is_relative_to(root):
            raise GitToolError("common Git directory must be outside the repair worktree")
        try:
            relative = git_dir.relative_to(common).as_posix()
        except ValueError as exc:
            raise GitToolError("worktree Git directory escapes the common Git directory") from exc
        return cls(root, git_dir, common, relative or ".")


def build_repair_sandbox(
    *,
    worktree: Path,
    image: str,
    base_sha: str,
    writable_paths: Sequence[str],
    test_commands: Sequence[Sequence[str]] = (),
    command_allowlist: Sequence[Sequence[str]] = (),
    docker_path: Path | None = None,
    executor: ProcessExecutor | None = None,
    max_seconds: float = 300.0,
    max_output_bytes: int = 1024 * 1024,
    max_input_bytes: int = 1024 * 1024,
) -> DockerSandboxRunner:
    if not _is_object_id(base_sha):
        raise ValueError("base_sha must be a hexadecimal object id")
    paths = normalize_repo_paths(writable_paths)
    if not paths:
        raise ValueError("at least one writable path is required")
    layout = GitLayout.discover(worktree)
    path_sets = {(), paths, *((path,) for path in paths)}
    commands = {
        STATUS_COMMAND,
        IGNORED_HASH_COMMAND,
        APPLY_CHECK_COMMAND,
        APPLY_COMMAND,
        REVERSE_CHECK_COMMAND,
        REVERSE_COMMAND,
    }
    for selected in path_sets:
        for scope in DiffScope:
            commands.add(_diff_command(scope, base_sha.lower(), selected))
    for path in paths:
        commands.add(_source_command(base_sha.lower(), path))
        commands.add(_untracked_diff_command(path))
    for command in (*test_commands, *command_allowlist):
        commands.add(tuple(command))
    policy = CommandPolicy(
        frozenset(commands),
        max_seconds=max_seconds,
        max_output_bytes=max_output_bytes,
        max_input_bytes=max_input_bytes,
    )
    git_dir = PurePosixPath("/git-common") / layout.git_dir_relative_to_common
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_DIR": git_dir.as_posix(),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_WORK_TREE": "/workspace",
        "HOME": "/nonexistent",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
    }
    return DockerSandboxRunner(
        worktree=layout.worktree,
        image=image,
        policy=policy,
        docker_path=docker_path,
        executor=executor,
        read_only_mounts=(
            ReadOnlyMount(layout.common_dir, "/git-common"),
            ReadOnlyMount(layout.worktree / ".git", "/workspace/.git"),
        ),
        container_environment=environment,
    )


def build_commit_sandbox(
    *,
    worktree: Path,
    image: str,
    allowed_commands: Sequence[Sequence[str]],
    docker_path: Path | None = None,
    executor: ProcessExecutor | None = None,
    max_seconds: float = 300.0,
    max_output_bytes: int = 1024 * 1024,
    max_input_bytes: int = 1024 * 1024,
) -> DockerSandboxRunner:
    """Build the approval-only Git runner with the linked metadata write mount."""
    commands = frozenset(tuple(command) for command in allowed_commands)
    if not commands:
        raise ValueError("commit sandbox needs at least one exact command")
    layout = GitLayout.discover(worktree)
    git_dir = PurePosixPath("/git-common") / layout.git_dir_relative_to_common
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_DIR": git_dir.as_posix(),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_WORK_TREE": "/workspace",
        "HOME": "/nonexistent",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return DockerSandboxRunner(
        worktree=layout.worktree,
        image=image,
        policy=CommandPolicy(
            commands,
            max_seconds=max_seconds,
            max_output_bytes=max_output_bytes,
            max_input_bytes=max_input_bytes,
        ),
        docker_path=docker_path,
        executor=executor,
        read_only_mounts=(ReadOnlyMount(layout.worktree / ".git", "/workspace/.git"),),
        writable_mounts=(WritableMount(layout.common_dir, "/git-common"),),
        container_environment=environment,
    )


def parse_porcelain_v1_z(text: str) -> tuple[StatusEntry, ...]:
    if not isinstance(text, str):
        raise GitToolError("git status output must be text")
    if not text:
        return ()
    if not text.endswith("\x00"):
        raise GitToolError("git status output is truncated or malformed")
    fields = text[:-1].split("\x00")
    entries = []
    index = 0
    while index < len(fields):
        field = fields[index]
        if len(field) < 4 or field[2] != " ":
            raise GitToolError("git status entry is malformed")
        x, y, path = field[0], field[1], field[3:]
        original = ""
        if x in "RC" or y in "RC":
            index += 1
            if index >= len(fields) or not fields[index]:
                raise GitToolError("git rename/copy status is missing its source path")
            original = fields[index]
        try:
            entries.append(StatusEntry(x, y, path, original))
        except ValueError as exc:
            raise GitToolError(f"git status contains an unsafe path: {exc}") from exc
        index += 1
    return tuple(entries)


def parse_patch(text: str, *, max_bytes: int = 1024 * 1024) -> PatchDocument:
    if not isinstance(text, str) or not text:
        raise PatchRejected("patch must be non-empty text")
    if "\x00" in text or "\r" in text:
        raise PatchRejected("patch contains unsupported control characters")
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        raise PatchRejected("patch exceeds the input safety limit")
    if not text.endswith("\n"):
        raise PatchRejected("patch must end with a newline")
    forbidden = (
        "GIT binary patch",
        "Binary files ",
        "new file mode 120000",
        "new mode 120000",
        "new file mode 160000",
        "new mode 160000",
    )
    if any(marker in text for marker in forbidden):
        raise PatchRejected("binary, symlink, and submodule patches are prohibited")
    lines = text.splitlines()
    file_header_starts = {
        index
        for index in range(len(lines) - 1)
        if lines[index].startswith("--- ")
        and lines[index + 1].startswith("+++ ")
    }
    file_header_continuations = {index + 1 for index in file_header_starts}
    paths = set()
    file_header_pairs = 0
    hunk_headers = 0
    in_hunk = False
    current_file = False
    file_needs_hunk = False
    for index, line in enumerate(lines):
        if line.startswith("diff --git "):
            if file_needs_hunk:
                raise PatchRejected("every paired file header must have a hunk")
            in_hunk = False
            current_file = False
            try:
                parts = shlex.split(line, posix=True)
            except ValueError as exc:
                raise PatchRejected(f"malformed diff header: {exc}") from exc
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise PatchRejected("diff header must contain exactly two paths")
            for raw, prefix in zip(parts[2:], ("a/", "b/")):
                paths.add(_patch_path(raw, prefix))
        elif index in file_header_starts:
            if file_needs_hunk:
                raise PatchRejected("every paired file header must have a hunk")
            in_hunk = False
            current_file = True
            file_needs_hunk = True
            file_header_pairs += 1
            for header, prefix in ((line, "a/"), (lines[index + 1], "b/")):
                try:
                    parts = shlex.split(header, posix=True)
                except ValueError as exc:
                    raise PatchRejected(f"malformed file header: {exc}") from exc
                if len(parts) != 2:
                    raise PatchRejected("file header must contain exactly one path")
                if parts[1] != "/dev/null":
                    paths.add(_patch_path(parts[1], prefix))
        elif index in file_header_continuations:
            continue
        elif line.startswith("@@"):
            if not current_file:
                raise PatchRejected("patch hunk must follow a paired file header")
            if not re.fullmatch(
                r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?: .*)?", line
            ):
                raise PatchRejected("patch contains a malformed hunk header")
            hunk_headers += 1
            in_hunk = True
            file_needs_hunk = False
        elif in_hunk:
            if not line.startswith((" ", "+", "-", "\\")):
                raise PatchRejected(
                    "every unified diff hunk line must have a patch prefix"
                )
            if line.startswith("\\") and line != "\\ No newline at end of file":
                raise PatchRejected("patch contains an invalid hunk marker")
        elif line.startswith(("--- ", "+++ ")):
            raise PatchRejected("file headers must be a paired ---/+++ block before a hunk")
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            raw = line.split(" ", 2)[2]
            paths.add(_patch_path(raw, ""))
    if file_header_pairs == 0 or not paths:
        raise PatchRejected("patch must contain at least one paired file header")
    if hunk_headers == 0 or file_needs_hunk:
        raise PatchRejected("every paired file header must have a unified diff hunk")
    try:
        normalized = normalize_repo_paths(paths)
    except ValueError as exc:
        raise PatchRejected(f"patch contains an unsafe path: {exc}") from exc
    return PatchDocument(text, hashlib.sha256(encoded).hexdigest(), normalized)


def _patch_path(raw: str, prefix: str) -> str:
    if raw == "/dev/null":
        return ""
    if prefix and not raw.startswith(prefix):
        raise PatchRejected(f"patch path lacks expected {prefix!r} prefix")
    value = raw[len(prefix) :] if prefix else raw
    if not value:
        raise PatchRejected("patch path cannot be empty")
    return value


def _diff_command(scope: DiffScope, base_sha: str, paths: Sequence[str]) -> tuple[str, ...]:
    common = ("diff", "--no-ext-diff", "--no-color", "--binary", "--full-index")
    scope = DiffScope(scope)
    args: tuple[str, ...]
    if scope is DiffScope.BASE:
        args = (*common, base_sha)
    elif scope is DiffScope.STAGED:
        args = (*common, "--cached")
    else:
        args = common
    return GIT_PREFIX + args + ("--", *tuple(paths))


def _source_command(base_sha: str, path: str) -> tuple[str, ...]:
    return GIT_PREFIX + ("show", f"{base_sha}:{path}")


def _bounded_source_tail(text: str, path: str, max_bytes: int) -> str:
    """Return exact source or a line-aligned, explicitly labelled tail window."""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    lines = text.splitlines(keepends=True)
    selected: list[str] = []
    selected_bytes = 0
    start_index = len(lines)
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        line_bytes = len(line.encode("utf-8"))
        marker = (
            f"[TRUNCATED BASE SOURCE {path}: lines 1-{index} omitted; "
            f"next source line is {index + 1}]\n"
        )
        if selected_bytes + line_bytes + len(marker.encode("utf-8")) > max_bytes:
            break
        selected.append(line)
        selected_bytes += line_bytes
        start_index = index
    if not selected:
        raise GitToolError(f"base source has no complete line within the limit: {path}")
    marker = (
        f"[TRUNCATED BASE SOURCE {path}: lines 1-{start_index} omitted; "
        f"next source line is {start_index + 1}]\n"
    )
    window = marker + "".join(reversed(selected))
    if len(window.encode("utf-8")) > max_bytes:
        raise GitToolError(f"bounded source window exceeds the limit: {path}")
    return window


def _untracked_diff_command(path: str) -> tuple[str, ...]:
    return GIT_PREFIX + (
        "diff",
        "--no-index",
        "--no-color",
        "--binary",
        "--full-index",
        "--",
        "/dev/null",
        path,
    )


def _is_untracked(entry: StatusEntry) -> bool:
    return entry.index_status == "?" and entry.worktree_status == "?"


def _is_ignored(entry: StatusEntry) -> bool:
    return entry.index_status == "!" and entry.worktree_status == "!"


def _test_result(result: SandboxResult) -> TestCommandResult:
    return TestCommandResult(
        argv=result.argv,
        operation_id=result.operation_id,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_seconds=result.duration_seconds,
        timed_out=False,
        output_truncated=result.output_truncated,
    )


def _require_receipt(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolPersistenceError(f"{label} was not durably persisted")
    return value


def _is_object_id(value: str) -> bool:
    return isinstance(value, str) and len(value) in (40, 64) and all(
        char in "0123456789abcdefABCDEF" for char in value
    )


def _is_sha256(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_directory(path: Path, label: str) -> Path:
    raw = Path(path)
    absolute = Path(os.path.abspath(raw))
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise GitToolError(f"{label} cannot be resolved: {exc}") from exc
    if not resolved.is_dir():
        raise GitToolError(f"{label} must be a directory")
    if os.path.normcase(str(absolute)) != os.path.normcase(str(resolved)):
        raise GitToolError(f"{label} must not use symlink or junction aliases")
    return resolved


def _read_small_text(path: Path, label: str) -> str:
    try:
        if path.stat().st_size > 4096:
            raise GitToolError(f"{label} exceeds its safety limit")
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GitToolError(f"cannot read {label}: {exc}") from exc
