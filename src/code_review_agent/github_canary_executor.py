"""Operationally gated Phase 11B GitHub sandbox canary executor.

The module deliberately keeps credential minting, migrations, arbitrary HTTP
transports, repository discovery, and human identity selection outside the CLI.
Ordinary tests inject an effect-recording transport and synthetic token provider;
the script entry point always selects the strict HTTPS transport for ``run``.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import text

from code_review_agent.database import (
    Database,
    current_revision,
    database_url_from_env,
)
from code_review_agent.github_sandbox_publish import (
    CANARY_CASE_IDS,
    CANARY_ENVIRONMENT,
    GitBlob,
    GitHubCanaryPublication,
    GitHubCanaryPublishRequest,
    GitHubCanaryReceipt,
    GitHubCanaryStore,
    GitHubDraftPrPublisher,
    GitHubFailure,
    GitHubSandboxApprovalConflict,
    GitHubSandboxAuthorization,
    GitHubSandboxPublicationError,
    GitHubTransport,
    InstallationToken,
    StrictGitHubHttpsTransport,
    canonical_json,
    sha256_hex,
)
from code_review_agent.identity import (
    AuthenticationRequired,
    DatabaseAuthBackend,
)


RUNTIME_SCHEMA_VERSION = "crag.github-sandbox-runtime/v1alpha1"
WORKSHEET_SCHEMA_VERSION = "crag.github-sandbox-approval-worksheet/v1alpha1"
PHASE11B_SCHEMA_HEAD = "0008_phase11b_github_canary"
TOKEN_INJECTION_MODE = "secure_explicit_json_file"
RUNTIME_ENVIRONMENT = "aliyun_ecs_cn_hangzhou"
GITHUB_API_URL = "https://api.github.com"
EGRESS_ALLOWLIST = ("api.github.com:443",)
COMMIT_ACTOR_NAME = "CRAG Sandbox Canary"
COMMIT_ACTOR_EMAIL = "crag-canary@invalid.example"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_BRANCH = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,253}[A-Za-z0-9])?\Z")
_TOP_LEVEL_PATH = re.compile(r"[A-Za-z0-9._-]{1,255}\Z")
_UTC_SECOND = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_TREE_TYPES = {
    "100644": "blob",
    "100755": "blob",
    "120000": "blob",
    "040000": "tree",
    "160000": "commit",
}
_CASE_FIELDS = frozenset(
    {
        "case_id",
        "repair_job_id",
        "repair_repository_id",
        "repair_base_sha",
        "repair_diff_sha256",
        "head_branch",
        "app_idempotency_key",
        "synthetic_file_path",
        "synthetic_content_base64",
        "file_mode",
        "blob_sha",
        "commit_message",
        "commit_timestamp",
        "expected_tree_sha",
        "exact_commit_sha",
        "diff_sha256",
        "test_evidence_sha256",
        "durable_budget_sha256",
        "checkpoint_sha256",
        "title",
        "body_prefix",
        "title_marker_sha256",
        "body_marker_sha256",
        "publisher_payload_sha256",
    }
)
_RUNTIME_FIELDS = frozenset(
    {
        "schema_version",
        "environment",
        "synthetic_input_only",
        "real_github_writes_enabled",
        "real_model_calls",
        "real_business_repository_writes",
        "business_claim_allowed",
        "quality_claim_allowed",
        "production_ready",
        "executable_code_sha",
        "image_id",
        "source_archive_sha256",
        "deployment_config_sha256",
        "runtime_host_sha256",
        "runtime_environment",
        "github_api_url",
        "egress_allowlist",
        "tls_verify",
        "follow_redirects",
        "credential_injection_mode",
        "request_timeout_seconds",
        "canary_window_seconds",
        "approval_ttl_seconds",
        "max_retries",
        "backoff_seconds",
        "max_requests",
        "max_mutations",
        "max_reads",
        "max_branches",
        "max_commits",
        "max_draft_prs",
        "cost_ceiling_micro_cny",
        "repository_owner",
        "repository_name",
        "repository_id",
        "github_app_id",
        "installation_id",
        "installation_account_id",
        "base_branch",
        "base_sha",
        "base_tree_sha",
        "base_tree_manifest_complete",
        "base_tree_entries",
        "cases",
    }
)
_TOKEN_FIELDS = frozenset(
    {
        "token",
        "github_app_id",
        "installation_id",
        "installation_account_id",
        "expires_at",
        "revoked",
    }
)
_ERROR_CODES = frozenset(
    {
        "approval_ambiguous",
        "approval_conflict",
        "authorization_mismatch",
        "authorization_window_closed",
        "database_unavailable",
        "executor_failed",
        "git_object_mismatch",
        "human_authentication_failed",
        "later_case_blocked",
        "runtime_config_invalid",
        "schema_mismatch",
        "secret_file_denied",
        "secret_file_invalid",
        "token_expired",
        "token_identity_mismatch",
        "token_revoked",
    }
)


class CanaryExecutorError(RuntimeError):
    """Stable, redacted executor failure."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CODES else "executor_failed"
        super().__init__(self.code)


class ExpectedCanaryRestart(RuntimeError):
    """The selected crash case reached its single deterministic stop point."""

    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        super().__init__("expected_process_restart")


def _required(name: str, value: Any, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded non-empty string")
    if "\x00" in value or "\r" in value:
        raise ValueError(f"{name} contains a prohibited character")
    return value


def _digest(name: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _git_sha(name: str, value: Any) -> str:
    if not isinstance(value, str) or _GIT_SHA.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase Git SHA-1")
    return value


def _positive_int(name: str, value: Any, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} exceeds its fixed maximum")
    return value


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or _UTC_SECOND.fullmatch(value) is None:
        raise ValueError("timestamp must be a UTC whole-second value")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _top_level_path(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or _TOP_LEVEL_PATH.fullmatch(value) is None
        or value in {".", ".."}
        or value.endswith(".lock")
    ):
        raise ValueError(f"{name} must be a bounded top-level Git path")
    return value


def _normalized_branch(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or _BRANCH.fullmatch(value) is None
        or ".." in value
        or "//" in value
        or value.endswith(".lock")
        or "/." in value
    ):
        raise ValueError(f"{name} must be a normalized Git branch")
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _parse_json_object(raw: bytes, *, expected_fields: frozenset[str]) -> Mapping[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("JSON object is invalid") from exc
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ValueError("JSON fields do not exactly match the schema")
    return value


def _load_json_file(
    path: Path,
    *,
    expected_fields: frozenset[str],
    maximum: int,
    error_code: str,
) -> Mapping[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise CanaryExecutorError(error_code) from exc
    if not raw or len(raw) > maximum:
        raise CanaryExecutorError(error_code)
    try:
        return _parse_json_object(raw, expected_fields=expected_fields)
    except ValueError as exc:
        raise CanaryExecutorError(error_code) from exc


@dataclass(frozen=True)
class GitTreeEntry:
    path: str
    mode: str
    object_type: str
    sha: str

    _FIELDS = frozenset({"path", "mode", "type", "sha"})

    def __post_init__(self) -> None:
        _top_level_path("tree path", self.path)
        if self.mode not in _TREE_TYPES or _TREE_TYPES[self.mode] != self.object_type:
            raise ValueError("tree mode/type pair is invalid")
        _git_sha("tree entry SHA", self.sha)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GitTreeEntry":
        if not isinstance(value, Mapping) or set(value) != cls._FIELDS:
            raise ValueError("tree entry fields are invalid")
        return cls(
            path=value["path"],
            mode=value["mode"],
            object_type=value["type"],
            sha=value["sha"],
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "mode": self.mode,
            "type": self.object_type,
            "sha": self.sha,
        }


def git_object_sha(object_type: str, content: bytes) -> str:
    if object_type not in {"blob", "tree", "commit"} or not isinstance(content, bytes):
        raise ValueError("Git object input is invalid")
    import hashlib

    header = f"{object_type} {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def git_tree_sha(entries: Sequence[GitTreeEntry]) -> str:
    if not entries or len({entry.path for entry in entries}) != len(entries):
        raise ValueError("tree entries must be non-empty and unique")

    def sort_key(entry: GitTreeEntry) -> bytes:
        suffix = b"/" if entry.object_type == "tree" else b""
        return entry.path.encode("utf-8") + suffix

    raw = bytearray()
    for entry in sorted(entries, key=sort_key):
        wire_mode = entry.mode.lstrip("0") or "0"
        raw.extend(f"{wire_mode} {entry.path}\0".encode("utf-8"))
        raw.extend(bytes.fromhex(entry.sha))
    return git_object_sha("tree", bytes(raw))


def git_commit_sha(
    *,
    tree_sha: str,
    parent_sha: str,
    message: str,
    timestamp: str,
) -> str:
    _git_sha("commit tree SHA", tree_sha)
    _git_sha("commit parent SHA", parent_sha)
    _required("commit message", message, maximum=256)
    if "\n" in message or message.endswith("\n"):
        raise ValueError("commit message must be a single line without a trailing newline")
    moment = _utc(timestamp)
    epoch = int(moment.timestamp())
    content = (
        f"tree {tree_sha}\n"
        f"parent {parent_sha}\n"
        f"author {COMMIT_ACTOR_NAME} <{COMMIT_ACTOR_EMAIL}> {epoch} +0000\n"
        f"committer {COMMIT_ACTOR_NAME} <{COMMIT_ACTOR_EMAIL}> {epoch} +0000\n"
        f"\n{message}\n"
    ).encode("utf-8")
    return git_object_sha("commit", content)


@dataclass(frozen=True)
class RuntimeCase:
    case_id: str
    repair_job_id: str
    repair_repository_id: str
    repair_base_sha: str
    repair_diff_sha256: str
    head_branch: str
    app_idempotency_key: str
    synthetic_file_path: str
    synthetic_content_base64: str
    file_mode: str
    blob_sha: str
    commit_message: str
    commit_timestamp: str
    expected_tree_sha: str
    exact_commit_sha: str
    diff_sha256: str
    test_evidence_sha256: str
    durable_budget_sha256: str
    checkpoint_sha256: str
    title: str
    body_prefix: str
    title_marker_sha256: str
    body_marker_sha256: str
    publisher_payload_sha256: str

    def __post_init__(self) -> None:
        if self.case_id not in CANARY_CASE_IDS:
            raise ValueError("runtime case is unknown")
        for name in ("repair_job_id", "repair_repository_id", "app_idempotency_key"):
            _required(name, getattr(self, name), maximum=128)
        _git_sha("repair_base_sha", self.repair_base_sha)
        _normalized_branch("head_branch", self.head_branch)
        if not self.head_branch.startswith("crag-canary/"):
            raise ValueError("runtime case branch is outside the canary prefix")
        path = _top_level_path("synthetic_file_path", self.synthetic_file_path)
        if not path.startswith("crag-canary-"):
            raise ValueError("synthetic file path is outside the canary prefix")
        if self.file_mode not in {"100644", "100755"}:
            raise ValueError("synthetic file mode is invalid")
        try:
            content = base64.b64decode(self.synthetic_content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("synthetic content is not canonical base64") from exc
        if not content or len(content) > 4096 or base64.b64encode(content).decode("ascii") != self.synthetic_content_base64:
            raise ValueError("synthetic content is empty, oversized, or non-canonical")
        _git_sha("blob_sha", self.blob_sha)
        if git_object_sha("blob", content) != self.blob_sha:
            raise ValueError("synthetic blob SHA mismatch")
        _required("commit_message", self.commit_message, maximum=256)
        _utc(self.commit_timestamp)
        _git_sha("expected_tree_sha", self.expected_tree_sha)
        _git_sha("exact_commit_sha", self.exact_commit_sha)
        for name in (
            "repair_diff_sha256",
            "diff_sha256",
            "test_evidence_sha256",
            "durable_budget_sha256",
            "checkpoint_sha256",
            "title_marker_sha256",
            "body_marker_sha256",
            "publisher_payload_sha256",
        ):
            _digest(name, getattr(self, name))
        _required("title", self.title, maximum=256)
        _required("body_prefix", self.body_prefix, maximum=1024)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RuntimeCase":
        if not isinstance(value, Mapping) or set(value) != _CASE_FIELDS:
            raise ValueError("runtime case fields do not exactly match the schema")
        return cls(**dict(value))

    @property
    def content(self) -> bytes:
        return base64.b64decode(self.synthetic_content_base64, validate=True)

    @property
    def marker(self) -> str:
        key = sha256_hex(self.app_idempotency_key.encode("utf-8"))
        return f"<!-- crag-canary:{key} -->"

    @property
    def body(self) -> str:
        return f"{self.body_prefix}\n\n{self.marker}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "repair_job_id": self.repair_job_id,
            "repair_repository_id": self.repair_repository_id,
            "repair_base_sha": self.repair_base_sha,
            "repair_diff_sha256": self.repair_diff_sha256,
            "head_branch": self.head_branch,
            "app_idempotency_key": self.app_idempotency_key,
            "synthetic_file_path": self.synthetic_file_path,
            "synthetic_content_base64": self.synthetic_content_base64,
            "file_mode": self.file_mode,
            "blob_sha": self.blob_sha,
            "commit_message": self.commit_message,
            "commit_timestamp": self.commit_timestamp,
            "expected_tree_sha": self.expected_tree_sha,
            "exact_commit_sha": self.exact_commit_sha,
            "diff_sha256": self.diff_sha256,
            "test_evidence_sha256": self.test_evidence_sha256,
            "durable_budget_sha256": self.durable_budget_sha256,
            "checkpoint_sha256": self.checkpoint_sha256,
            "title": self.title,
            "body_prefix": self.body_prefix,
            "title_marker_sha256": self.title_marker_sha256,
            "body_marker_sha256": self.body_marker_sha256,
            "publisher_payload_sha256": self.publisher_payload_sha256,
        }


def freeze_runtime_case(
    *,
    case_id: str,
    repair_job_id: str,
    repair_repository_id: str,
    repair_base_sha: str,
    repair_diff_sha256: str,
    head_branch: str,
    app_idempotency_key: str,
    synthetic_file_path: str,
    synthetic_content: bytes,
    file_mode: str,
    commit_message: str,
    commit_timestamp: str,
    test_evidence_sha256: str,
    durable_budget_sha256: str,
    checkpoint_sha256: str,
    title: str,
    body_prefix: str,
    repository_id: int,
    base_branch: str,
    base_sha: str,
    base_tree_sha: str,
    base_tree_entries: Sequence[GitTreeEntry],
) -> RuntimeCase:
    """Freeze every derived object/payload hash from exact synthetic material."""

    if git_tree_sha(base_tree_entries) != base_tree_sha:
        raise ValueError("base tree manifest does not match base_tree_sha")
    _normalized_branch("base_branch", base_branch)
    _git_sha("base_sha", base_sha)
    _positive_int("repository_id", repository_id)
    content = bytes(synthetic_content)
    blob_sha = git_object_sha("blob", content)
    blob = GitBlob(synthetic_file_path, content, blob_sha, file_mode)
    entries = (*base_tree_entries, GitTreeEntry(synthetic_file_path, file_mode, "blob", blob_sha))
    expected_tree_sha = git_tree_sha(entries)
    exact_commit_sha = git_commit_sha(
        tree_sha=expected_tree_sha,
        parent_sha=base_sha,
        message=commit_message,
        timestamp=commit_timestamp,
    )
    diff_sha256 = sha256_hex(
        canonical_json(
            {
                "base_sha": base_sha,
                "blob_sha": blob_sha,
                "mode": file_mode,
                "path": synthetic_file_path,
                "schema_version": "crag.synthetic-diff-binding/v1",
            }
        )
    )
    marker_key = sha256_hex(app_idempotency_key.encode("utf-8"))
    marker = f"<!-- crag-canary:{marker_key} -->"
    body = f"{body_prefix}\n\n{marker}"
    title_marker_sha256 = sha256_hex(title.encode("utf-8"))
    body_marker_sha256 = sha256_hex(marker.encode("utf-8"))
    payload_binding = {
        "base_branch": base_branch,
        "base_sha": base_sha,
        "base_tree_sha": base_tree_sha,
        "blobs": [blob.binding()],
        "body_sha256": sha256_hex(body.encode("utf-8")),
        "commit_message_sha256": sha256_hex(commit_message.encode("utf-8")),
        "commit_timestamp": commit_timestamp,
        "diff_sha256": diff_sha256,
        "exact_commit_sha": exact_commit_sha,
        "expected_tree_sha": expected_tree_sha,
        "head_branch": head_branch,
        "repository_id": repository_id,
        "test_evidence_sha256": test_evidence_sha256,
        "title_marker_sha256": title_marker_sha256,
    }
    return RuntimeCase(
        case_id=case_id,
        repair_job_id=repair_job_id,
        repair_repository_id=repair_repository_id,
        repair_base_sha=repair_base_sha,
        repair_diff_sha256=repair_diff_sha256,
        head_branch=head_branch,
        app_idempotency_key=app_idempotency_key,
        synthetic_file_path=synthetic_file_path,
        synthetic_content_base64=base64.b64encode(content).decode("ascii"),
        file_mode=file_mode,
        blob_sha=blob_sha,
        commit_message=commit_message,
        commit_timestamp=commit_timestamp,
        expected_tree_sha=expected_tree_sha,
        exact_commit_sha=exact_commit_sha,
        diff_sha256=diff_sha256,
        test_evidence_sha256=test_evidence_sha256,
        durable_budget_sha256=durable_budget_sha256,
        checkpoint_sha256=checkpoint_sha256,
        title=title,
        body_prefix=body_prefix,
        title_marker_sha256=title_marker_sha256,
        body_marker_sha256=body_marker_sha256,
        publisher_payload_sha256=sha256_hex(canonical_json(payload_binding)),
    )


@dataclass(frozen=True)
class GitHubCanaryRuntimeConfig:
    schema_version: str
    environment: str
    synthetic_input_only: bool
    real_github_writes_enabled: bool
    real_model_calls: bool
    real_business_repository_writes: bool
    business_claim_allowed: bool
    quality_claim_allowed: bool
    production_ready: bool
    executable_code_sha: str
    image_id: str
    source_archive_sha256: str
    deployment_config_sha256: str
    runtime_host_sha256: str
    runtime_environment: str
    github_api_url: str
    egress_allowlist: tuple[str, ...]
    tls_verify: bool
    follow_redirects: bool
    credential_injection_mode: str
    request_timeout_seconds: int
    canary_window_seconds: int
    approval_ttl_seconds: int
    max_retries: int
    backoff_seconds: int
    max_requests: int
    max_mutations: int
    max_reads: int
    max_branches: int
    max_commits: int
    max_draft_prs: int
    cost_ceiling_micro_cny: int
    repository_owner: str
    repository_name: str
    repository_id: int
    github_app_id: int
    installation_id: int
    installation_account_id: int
    base_branch: str
    base_sha: str
    base_tree_sha: str
    base_tree_manifest_complete: bool
    base_tree_entries: tuple[GitTreeEntry, ...]
    cases: tuple[RuntimeCase, ...]

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported runtime schema")
        if (
            self.environment != CANARY_ENVIRONMENT
            or self.synthetic_input_only is not True
            or self.real_github_writes_enabled is not True
            or self.real_model_calls is not False
            or self.real_business_repository_writes is not False
            or self.business_claim_allowed is not False
            or self.quality_claim_allowed is not False
            or self.production_ready is not False
        ):
            raise ValueError("runtime claim boundary is invalid")
        _git_sha("executable_code_sha", self.executable_code_sha)
        if not isinstance(self.image_id, str) or _IMAGE_ID.fullmatch(self.image_id) is None:
            raise ValueError("runtime image ID must be immutable")
        for name in ("source_archive_sha256", "deployment_config_sha256", "runtime_host_sha256"):
            _digest(name, getattr(self, name))
        if (
            self.runtime_environment != RUNTIME_ENVIRONMENT
            or self.github_api_url != GITHUB_API_URL
            or self.egress_allowlist != EGRESS_ALLOWLIST
            or self.tls_verify is not True
            or self.follow_redirects is not False
            or self.credential_injection_mode != TOKEN_INJECTION_MODE
        ):
            raise ValueError("runtime network or credential boundary is invalid")
        _positive_int("request_timeout_seconds", self.request_timeout_seconds, maximum=10)
        _positive_int("canary_window_seconds", self.canary_window_seconds, maximum=2700)
        _positive_int("approval_ttl_seconds", self.approval_ttl_seconds, maximum=1800)
        if self.approval_ttl_seconds >= self.canary_window_seconds:
            raise ValueError("approval TTL must be shorter than the canary window")
        if self.max_retries != 0 or self.backoff_seconds != 0:
            raise ValueError("automatic retry/backoff must remain disabled")
        _positive_int("max_requests", self.max_requests, maximum=40)
        _positive_int("max_mutations", self.max_mutations, maximum=15)
        _positive_int("max_reads", self.max_reads, maximum=25)
        _positive_int("max_branches", self.max_branches, maximum=3)
        _positive_int("max_commits", self.max_commits, maximum=3)
        _positive_int("max_draft_prs", self.max_draft_prs, maximum=3)
        if (
            self.max_requests,
            self.max_mutations,
            self.max_reads,
            self.max_branches,
            self.max_commits,
            self.max_draft_prs,
        ) != (40, 15, 25, 3, 3, 3):
            raise ValueError("runtime canary budgets must match the frozen profile")
        if self.max_requests < self.max_mutations + self.max_reads:
            raise ValueError("runtime request budget is smaller than its partitions")
        if self.cost_ceiling_micro_cny != 0:
            raise ValueError("runtime cost ceiling must be zero")
        if _OWNER.fullmatch(self.repository_owner) is None or _REPOSITORY.fullmatch(self.repository_name) is None:
            raise ValueError("runtime repository identity is invalid")
        for name in ("repository_id", "github_app_id", "installation_id", "installation_account_id"):
            _positive_int(name, getattr(self, name))
        _normalized_branch("base_branch", self.base_branch)
        _git_sha("base_sha", self.base_sha)
        _git_sha("base_tree_sha", self.base_tree_sha)
        if self.base_tree_manifest_complete is not True:
            raise ValueError("base tree manifest is incomplete")
        if not self.base_tree_entries or len(self.base_tree_entries) > 1024:
            raise ValueError("base tree manifest size is invalid")
        if git_tree_sha(self.base_tree_entries) != self.base_tree_sha:
            raise ValueError("base tree manifest does not match base_tree_sha")
        if len(self.cases) != 3 or tuple(case.case_id for case in self.cases) != CANARY_CASE_IDS:
            raise ValueError("runtime must contain the three ordered canary cases")
        if len({case.head_branch for case in self.cases}) != 3:
            raise ValueError("runtime case branches must be unique")
        if len({case.repair_job_id for case in self.cases}) != 3:
            raise ValueError("runtime case Repair jobs must be unique")
        if len({case.app_idempotency_key for case in self.cases}) != 3:
            raise ValueError("runtime case idempotency keys must be unique")
        base_paths = {entry.path for entry in self.base_tree_entries}
        for case in self.cases:
            if case.synthetic_file_path in base_paths:
                raise ValueError("synthetic file collides with the frozen base tree")
            self._validate_case(case)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GitHubCanaryRuntimeConfig":
        if not isinstance(value, Mapping) or set(value) != _RUNTIME_FIELDS:
            raise ValueError("runtime fields do not exactly match the schema")
        raw_entries = value["base_tree_entries"]
        raw_cases = value["cases"]
        if (
            not isinstance(raw_entries, Sequence)
            or isinstance(raw_entries, (str, bytes))
            or not isinstance(raw_cases, Sequence)
            or isinstance(raw_cases, (str, bytes))
        ):
            raise ValueError("runtime entries/cases must be arrays")
        arguments = dict(value)
        arguments["egress_allowlist"] = tuple(value["egress_allowlist"])
        arguments["base_tree_entries"] = tuple(GitTreeEntry.from_dict(item) for item in raw_entries)
        arguments["cases"] = tuple(RuntimeCase.from_dict(item) for item in raw_cases)
        return cls(**arguments)

    @property
    def canonical_sha256(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))

    def case(self, case_id: str) -> RuntimeCase:
        for case in self.cases:
            if case.case_id == case_id:
                return case
        raise CanaryExecutorError("runtime_config_invalid")

    def _tree_entries_for(self, case: RuntimeCase) -> tuple[GitTreeEntry, ...]:
        return (
            *self.base_tree_entries,
            GitTreeEntry(case.synthetic_file_path, case.file_mode, "blob", case.blob_sha),
        )

    def _payload_binding(self, case: RuntimeCase) -> dict[str, Any]:
        blob = GitBlob(case.synthetic_file_path, case.content, case.blob_sha, case.file_mode)
        return {
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "base_tree_sha": self.base_tree_sha,
            "blobs": [blob.binding()],
            "body_sha256": sha256_hex(case.body.encode("utf-8")),
            "commit_message_sha256": sha256_hex(case.commit_message.encode("utf-8")),
            "commit_timestamp": case.commit_timestamp,
            "diff_sha256": case.diff_sha256,
            "exact_commit_sha": case.exact_commit_sha,
            "expected_tree_sha": case.expected_tree_sha,
            "head_branch": case.head_branch,
            "repository_id": self.repository_id,
            "test_evidence_sha256": case.test_evidence_sha256,
            "title_marker_sha256": case.title_marker_sha256,
        }

    def _validate_case(self, case: RuntimeCase) -> None:
        expected_tree = git_tree_sha(self._tree_entries_for(case))
        expected_commit = git_commit_sha(
            tree_sha=expected_tree,
            parent_sha=self.base_sha,
            message=case.commit_message,
            timestamp=case.commit_timestamp,
        )
        expected_diff = sha256_hex(
            canonical_json(
                {
                    "base_sha": self.base_sha,
                    "blob_sha": case.blob_sha,
                    "mode": case.file_mode,
                    "path": case.synthetic_file_path,
                    "schema_version": "crag.synthetic-diff-binding/v1",
                }
            )
        )
        if (
            expected_tree != case.expected_tree_sha
            or expected_commit != case.exact_commit_sha
            or expected_diff != case.diff_sha256
            or sha256_hex(case.title.encode("utf-8")) != case.title_marker_sha256
            or sha256_hex(case.marker.encode("utf-8")) != case.body_marker_sha256
            or sha256_hex(canonical_json(self._payload_binding(case)))
            != case.publisher_payload_sha256
        ):
            raise ValueError("runtime case Git/payload material does not match its hashes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "environment": self.environment,
            "synthetic_input_only": self.synthetic_input_only,
            "real_github_writes_enabled": self.real_github_writes_enabled,
            "real_model_calls": self.real_model_calls,
            "real_business_repository_writes": self.real_business_repository_writes,
            "business_claim_allowed": self.business_claim_allowed,
            "quality_claim_allowed": self.quality_claim_allowed,
            "production_ready": self.production_ready,
            "executable_code_sha": self.executable_code_sha,
            "image_id": self.image_id,
            "source_archive_sha256": self.source_archive_sha256,
            "deployment_config_sha256": self.deployment_config_sha256,
            "runtime_host_sha256": self.runtime_host_sha256,
            "runtime_environment": self.runtime_environment,
            "github_api_url": self.github_api_url,
            "egress_allowlist": list(self.egress_allowlist),
            "tls_verify": self.tls_verify,
            "follow_redirects": self.follow_redirects,
            "credential_injection_mode": self.credential_injection_mode,
            "request_timeout_seconds": self.request_timeout_seconds,
            "canary_window_seconds": self.canary_window_seconds,
            "approval_ttl_seconds": self.approval_ttl_seconds,
            "max_retries": self.max_retries,
            "backoff_seconds": self.backoff_seconds,
            "max_requests": self.max_requests,
            "max_mutations": self.max_mutations,
            "max_reads": self.max_reads,
            "max_branches": self.max_branches,
            "max_commits": self.max_commits,
            "max_draft_prs": self.max_draft_prs,
            "cost_ceiling_micro_cny": self.cost_ceiling_micro_cny,
            "repository_owner": self.repository_owner,
            "repository_name": self.repository_name,
            "repository_id": self.repository_id,
            "github_app_id": self.github_app_id,
            "installation_id": self.installation_id,
            "installation_account_id": self.installation_account_id,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "base_tree_sha": self.base_tree_sha,
            "base_tree_manifest_complete": self.base_tree_manifest_complete,
            "base_tree_entries": [entry.to_dict() for entry in self.base_tree_entries],
            "cases": [case.to_dict() for case in self.cases],
        }

    def publication(
        self,
        authorization: GitHubSandboxAuthorization,
        case_id: str,
    ) -> GitHubCanaryPublication:
        case = self.case(case_id)
        return GitHubCanaryPublication(
            repair_job_id=case.repair_job_id,
            organization_id=authorization.organization_id,
            repair_repository_id=case.repair_repository_id,
            repair_base_sha=case.repair_base_sha,
            repair_diff_sha256=case.repair_diff_sha256,
            repository_owner=self.repository_owner,
            repository_name=self.repository_name,
            repository_id=self.repository_id,
            github_app_id=self.github_app_id,
            installation_id=self.installation_id,
            installation_account_id=self.installation_account_id,
            base_branch=self.base_branch,
            base_sha=self.base_sha,
            base_tree_sha=self.base_tree_sha,
            head_branch=case.head_branch,
            diff_sha256=case.diff_sha256,
            test_evidence_sha256=case.test_evidence_sha256,
            durable_budget_sha256=case.durable_budget_sha256,
            checkpoint_sha256=case.checkpoint_sha256,
            exact_commit_sha=case.exact_commit_sha,
            commit_message=case.commit_message,
            commit_timestamp=case.commit_timestamp,
            expected_tree_sha=case.expected_tree_sha,
            blobs=(GitBlob(case.synthetic_file_path, case.content, case.blob_sha, case.file_mode),),
            title=case.title,
            body=case.body,
            title_marker_sha256=case.title_marker_sha256,
            body_marker_sha256=case.body_marker_sha256,
            publisher_payload_sha256=case.publisher_payload_sha256,
            authorization_id=authorization.authorization_id,
            authorization_sha256=authorization.canonical_sha256,
            app_idempotency_key=case.app_idempotency_key,
            canary_case_id=case.case_id,
            executable_code_sha=self.executable_code_sha,
            runtime_config_sha256=self.canonical_sha256,
        )


def load_runtime_config(path: Path) -> GitHubCanaryRuntimeConfig:
    value = _load_json_file(
        path,
        expected_fields=_RUNTIME_FIELDS,
        maximum=1_048_576,
        error_code="runtime_config_invalid",
    )
    try:
        return GitHubCanaryRuntimeConfig.from_dict(value)
    except (TypeError, ValueError, KeyError) as exc:
        raise CanaryExecutorError("runtime_config_invalid") from exc


def load_authorization(path: Path) -> GitHubSandboxAuthorization:
    value = _load_json_file(
        path,
        expected_fields=GitHubSandboxAuthorization._FIELDS,
        maximum=131_072,
        error_code="authorization_mismatch",
    )
    try:
        return GitHubSandboxAuthorization.from_dict(value)
    except (TypeError, ValueError, KeyError) as exc:
        raise CanaryExecutorError("authorization_mismatch") from exc


def validate_secret_metadata(
    *,
    mode: int,
    owner_uid: int,
    current_uid: int,
    is_symlink: bool,
    is_regular: bool,
) -> None:
    """Pure policy helper used by cross-platform tests and the Linux reader."""

    if (
        is_symlink
        or not is_regular
        or stat.S_IMODE(mode) != 0o600
        or owner_uid not in {0, current_uid}
    ):
        raise CanaryExecutorError("secret_file_denied")


def read_secure_secret_file(path: Path, *, maximum: int = 16_384) -> bytes:
    """Read one explicit Linux secret file without following a symlink."""

    candidate = Path(path)
    if not candidate.is_absolute() or os.name != "posix":
        raise CanaryExecutorError("secret_file_denied")
    geteuid = getattr(os, "geteuid", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not callable(geteuid) or nofollow is None:
        raise CanaryExecutorError("secret_file_denied")
    try:
        before = os.lstat(candidate)
        validate_secret_metadata(
            mode=before.st_mode,
            owner_uid=before.st_uid,
            current_uid=int(geteuid()),
            is_symlink=stat.S_ISLNK(before.st_mode),
            is_regular=stat.S_ISREG(before.st_mode),
        )
        descriptor = os.open(candidate, os.O_RDONLY | cloexec | int(nofollow))
    except CanaryExecutorError:
        raise
    except OSError as exc:
        raise CanaryExecutorError("secret_file_denied") from exc
    try:
        after = os.fstat(descriptor)
        validate_secret_metadata(
            mode=after.st_mode,
            owner_uid=after.st_uid,
            current_uid=int(geteuid()),
            is_symlink=False,
            is_regular=stat.S_ISREG(after.st_mode),
        )
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise CanaryExecutorError("secret_file_denied")
        chunks: list[bytes] = []
        size = 0
        while size <= maximum:
            chunk = os.read(descriptor, min(4096, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if not raw or len(raw) > maximum:
        raise CanaryExecutorError("secret_file_invalid")
    return raw


class SecureBearerTokenFileProvider:
    """Load a CRAG bearer value; never returns identity or file metadata."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def __call__(self) -> str:
        raw = read_secure_secret_file(self._path, maximum=4096)
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise CanaryExecutorError("secret_file_invalid") from exc
        if not 32 <= len(value.encode("utf-8")) <= 4096 or any(character.isspace() for character in value):
            raise CanaryExecutorError("secret_file_invalid")
        return value


class SecureInstallationTokenFileProvider:
    """Reload and revalidate a short-lived installation token before each request."""

    def __init__(
        self,
        path: Path,
        *,
        runtime: GitHubCanaryRuntimeConfig,
        authorization: GitHubSandboxAuthorization,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._runtime = runtime
        self._authorization = authorization
        self._clock = clock

    def __call__(self) -> InstallationToken:
        raw = read_secure_secret_file(self._path)
        try:
            value = _parse_json_object(raw, expected_fields=_TOKEN_FIELDS)
            token_value = _required("installation token", value["token"], maximum=4096)
            if len(token_value.encode("utf-8")) < 20 or any(character.isspace() for character in token_value):
                raise ValueError("installation token format is invalid")
            expires = _utc(value["expires_at"])
            revoked = value["revoked"]
            if not isinstance(revoked, bool):
                raise ValueError("token revocation field is invalid")
            token = InstallationToken(
                value=token_value,
                app_id=value["github_app_id"],
                installation_id=value["installation_id"],
                installation_account_id=value["installation_account_id"],
                expires_at=expires,
                revoked=revoked,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CanaryExecutorError("secret_file_invalid") from exc
        if token.revoked:
            raise CanaryExecutorError("token_revoked")
        now = datetime.fromtimestamp(float(self._clock()), tz=timezone.utc)
        if token.expires_at <= now or token.expires_at > now.replace(microsecond=0) + _ONE_HOUR:
            raise CanaryExecutorError("token_expired")
        if (
            token.app_id != self._runtime.github_app_id
            or token.installation_id != self._runtime.installation_id
            or token.installation_account_id != self._runtime.installation_account_id
        ):
            raise CanaryExecutorError("token_identity_mismatch")
        return token


_ONE_HOUR = timedelta(hours=1)


def validate_runtime_authorization(
    runtime: GitHubCanaryRuntimeConfig,
    authorization: GitHubSandboxAuthorization,
) -> None:
    window = (_utc(authorization.expires_at) - _utc(authorization.not_before)).total_seconds()
    checks = (
        authorization.runtime_config_sha256 == runtime.canonical_sha256,
        authorization.executable_code_sha == runtime.executable_code_sha,
        authorization.repository_owner == runtime.repository_owner,
        authorization.repository_name == runtime.repository_name,
        authorization.repository_id == runtime.repository_id,
        authorization.github_app_id == runtime.github_app_id,
        authorization.installation_id == runtime.installation_id,
        authorization.installation_account_id == runtime.installation_account_id,
        authorization.allowed_base_branch == runtime.base_branch,
        authorization.frozen_base_sha == runtime.base_sha,
        tuple((item.case_id, item.head_branch) for item in authorization.cases)
        == tuple((item.case_id, item.head_branch) for item in runtime.cases),
        authorization.max_denominator == 3,
        authorization.max_requests == runtime.max_requests,
        authorization.max_mutations == runtime.max_mutations,
        authorization.max_reads == runtime.max_reads,
        authorization.max_branches == runtime.max_branches,
        authorization.max_commits == runtime.max_commits,
        authorization.max_draft_prs == runtime.max_draft_prs,
        authorization.cost_ceiling_micro_cny == runtime.cost_ceiling_micro_cny,
        0 < window <= runtime.canary_window_seconds,
    )
    if not all(checks):
        raise CanaryExecutorError("authorization_mismatch")


class GitHubCanaryExecutor:
    """Prepare, approve, and run exact cases against one already-migrated database."""

    def __init__(
        self,
        database: Database,
        runtime: GitHubCanaryRuntimeConfig,
        authorization: GitHubSandboxAuthorization,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        validate_runtime_authorization(runtime, authorization)
        self.database = database
        self.runtime = runtime
        self.authorization = authorization
        self.clock = clock
        self.store = GitHubCanaryStore(database.engine, clock=clock)

    def _require_window(self, *, require_approval_ttl: bool = False) -> None:
        now = datetime.fromtimestamp(float(self.clock()), tz=timezone.utc)
        not_before = _utc(self.authorization.not_before)
        expires = _utc(self.authorization.expires_at)
        if now < not_before or now >= expires:
            raise CanaryExecutorError("authorization_window_closed")
        if require_approval_ttl and (expires - now).total_seconds() < self.runtime.approval_ttl_seconds:
            raise CanaryExecutorError("authorization_window_closed")

    def _approval_rows(self, publication: GitHubCanaryPublication, kind: str) -> list[dict[str, Any]]:
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, binding_sha256, status FROM github_canary_approvals "
                    "WHERE repair_job_id=:job AND organization_id=:organization AND kind=:kind "
                    "ORDER BY created_at, id"
                ),
                {
                    "job": publication.repair_job_id,
                    "organization": publication.organization_id,
                    "kind": kind,
                },
            ).all()
        return [dict(row._mapping) for row in rows]

    def _prepare_approval(self, publication: GitHubCanaryPublication, kind: str) -> tuple[str, str]:
        binding = publication.approval_binding_sha256(kind)
        rows = self._approval_rows(publication, kind)
        if not rows:
            return self.store.issue_approval(
                publication,
                kind=kind,
                ttl_seconds=self.runtime.approval_ttl_seconds,
            )
        if len(rows) != 1 or rows[0]["binding_sha256"] != binding or rows[0]["status"] not in {"issued", "consumed"}:
            raise CanaryExecutorError("approval_ambiguous")
        return str(rows[0]["id"]), binding

    def prepare(self) -> Mapping[str, Any]:
        self._require_window(require_approval_ttl=True)
        cases: list[dict[str, Any]] = []
        for case_id in CANARY_CASE_IDS:
            publication = self.runtime.publication(self.authorization, case_id)
            write_id, write_binding = self._prepare_approval(publication, "write")
            draft_id, draft_binding = self._prepare_approval(publication, "draft_pr")
            cases.append(
                {
                    "case_id": case_id,
                    "publication_binding_sha256": sha256_hex(canonical_json(publication.exact_binding)),
                    "write": {"approval_id": write_id, "binding_sha256": write_binding},
                    "draft_pr": {"approval_id": draft_id, "binding_sha256": draft_binding},
                }
            )
        worksheet = {
            "schema_version": WORKSHEET_SCHEMA_VERSION,
            "authorization_id": self.authorization.authorization_id,
            "authorization_sha256": self.authorization.canonical_sha256,
            "runtime_config_sha256": self.runtime.canonical_sha256,
            "planned": 3,
            "denominator": 3,
            "cases": cases,
        }
        return {**worksheet, "worksheet_sha256": sha256_hex(canonical_json(worksheet))}

    def decide_approval(
        self,
        *,
        case_id: str,
        kind: str,
        approval_id: str,
        approved: bool,
        bearer_provider: Callable[[], str],
    ) -> Mapping[str, Any]:
        self._require_window()
        if kind not in {"write", "draft_pr"} or not isinstance(approved, bool):
            raise CanaryExecutorError("approval_conflict")
        publication = self.runtime.publication(self.authorization, case_id)
        rows = self._approval_rows(publication, kind)
        binding = publication.approval_binding_sha256(kind)
        if (
            len(rows) != 1
            or rows[0]["id"] != approval_id
            or rows[0]["binding_sha256"] != binding
            or rows[0]["status"] != "issued"
        ):
            raise CanaryExecutorError("approval_conflict")
        try:
            principal = DatabaseAuthBackend(self.database).authenticate(
                f"Bearer {bearer_provider()}"
            )
        except (AuthenticationRequired, CanaryExecutorError) as exc:
            raise CanaryExecutorError("human_authentication_failed") from exc
        try:
            self.store.decide_approval(
                approval_id,
                publication,
                kind=kind,
                actor=principal,
                approved=approved,
            )
        except GitHubSandboxApprovalConflict as exc:
            raise CanaryExecutorError("approval_conflict") from exc
        return {
            "case_id": case_id,
            "kind": kind,
            "approval_id": approval_id,
            "binding_sha256": binding,
            "status": "consumed" if approved else "rejected",
        }

    def _approved_request(self, publication: GitHubCanaryPublication) -> GitHubCanaryPublishRequest:
        found: dict[str, dict[str, Any]] = {}
        for kind in ("write", "draft_pr"):
            rows = self._approval_rows(publication, kind)
            binding = publication.approval_binding_sha256(kind)
            if len(rows) != 1 or rows[0]["binding_sha256"] != binding or rows[0]["status"] != "consumed":
                raise CanaryExecutorError("approval_conflict")
            found[kind] = rows[0]
        return GitHubCanaryPublishRequest(
            publication=publication,
            write_approval_id=str(found["write"]["id"]),
            write_approval_binding_sha256=str(found["write"]["binding_sha256"]),
            draft_pr_approval_id=str(found["draft_pr"]["id"]),
            draft_pr_approval_binding_sha256=str(found["draft_pr"]["binding_sha256"]),
        )

    def _require_prior_cases(self, case_id: str) -> None:
        index = CANARY_CASE_IDS.index(case_id)
        for prior_id in CANARY_CASE_IDS[:index]:
            prior = self.runtime.case(prior_id)
            with self.database.engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT state FROM github_canary_publications "
                        "WHERE authorization_id=:authorization AND canary_case_id=:case_id "
                        "AND idempotency_key=:key"
                    ),
                    {
                        "authorization": self.authorization.authorization_id,
                        "case_id": prior_id,
                        "key": prior.app_idempotency_key,
                    },
                ).first()
            if row is None or row._mapping["state"] != "receipt_reconciled":
                raise CanaryExecutorError("later_case_blocked")

    def run_case(
        self,
        case_id: str,
        *,
        token_provider: Callable[[], InstallationToken],
        transport: GitHubTransport,
    ) -> GitHubCanaryReceipt:
        self._require_window()
        if case_id not in CANARY_CASE_IDS:
            raise CanaryExecutorError("runtime_config_invalid")
        self._require_prior_cases(case_id)
        publication = self.runtime.publication(self.authorization, case_id)
        request = self._approved_request(publication)
        token_provider()

        def checked_token_provider() -> InstallationToken:
            try:
                return token_provider()
            except CanaryExecutorError as exc:
                failure = {
                    "token_revoked": GitHubFailure.TOKEN_REVOKED,
                    "token_expired": GitHubFailure.TOKEN_EXPIRED,
                    "token_identity_mismatch": GitHubFailure.INSTALLATION_MISMATCH,
                    "secret_file_denied": GitHubFailure.AUTH_401,
                    "secret_file_invalid": GitHubFailure.AUTH_401,
                }.get(exc.code, GitHubFailure.OTHER)
                raise GitHubSandboxPublicationError(failure) from None

        stop_point = {
            "normal": None,
            "crash_after_branch": "after_ref_create_before_receipt",
            "crash_after_draft_pr": "after_draft_pr_create_before_receipt",
        }[case_id]

        def fault(point: str) -> None:
            if point == stop_point:
                raise ExpectedCanaryRestart(case_id)

        publisher = GitHubDraftPrPublisher(
            feature_enabled=True,
            real_github_writes_enabled=bool(transport.real_github_writes),
            authorization=self.authorization,
            authorization_sha256=self.authorization.canonical_sha256,
            executable_code_sha=self.runtime.executable_code_sha,
            runtime_config_sha256=self.runtime.canonical_sha256,
            repository_allowlist=frozenset(
                {(self.runtime.repository_owner, self.runtime.repository_name, self.runtime.repository_id)}
            ),
            protected_branches=frozenset({"main", "master", self.runtime.base_branch}),
            store=self.store,
            transport=transport,
            token_provider=checked_token_provider,
            timeout_seconds=self.runtime.request_timeout_seconds,
            clock=self.clock,
            fault=fault,
        )
        try:
            return publisher.publish(request)
        except ExpectedCanaryRestart:
            raise
        except GitHubSandboxPublicationError:
            raise
        except Exception as exc:
            raise CanaryExecutorError("executor_failed") from exc


def _database() -> Database:
    try:
        database_url = database_url_from_env()
        if current_revision(database_url) != PHASE11B_SCHEMA_HEAD:
            raise CanaryExecutorError("schema_mismatch")
        return Database(database_url, check_schema=False)
    except CanaryExecutorError:
        raise
    except Exception as exc:
        raise CanaryExecutorError("database_unavailable") from exc


def _output(value: Mapping[str, Any], *, stream: Any = sys.stdout) -> None:
    stream.write(canonical_json(value).decode("utf-8") + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CRAG Phase 11B gated sandbox canary")
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="validate exact offline bindings")
    commands.add_parser("prepare", help="issue or reuse six exact approval envelopes")
    approve = commands.add_parser("approve", help="record one human approval decision")
    approve.add_argument("--case-id", choices=CANARY_CASE_IDS, required=True)
    approve.add_argument("--kind", choices=("write", "draft_pr"), required=True)
    approve.add_argument("--approval-id", required=True)
    approve.add_argument("--decision", choices=("approve", "reject"), required=True)
    approve.add_argument("--crag-token-file", type=Path, required=True)
    run = commands.add_parser("run", help="run or reconcile one exact case")
    run.add_argument("--case-id", choices=CANARY_CASE_IDS, required=True)
    run.add_argument("--github-token-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database: Database | None = None
    try:
        runtime = load_runtime_config(args.runtime_config)
        authorization = load_authorization(args.authorization)
        validate_runtime_authorization(runtime, authorization)
        if args.command == "validate":
            _output(
                {
                    "authorization_id": authorization.authorization_id,
                    "authorization_sha256": authorization.canonical_sha256,
                    "runtime_config_sha256": runtime.canonical_sha256,
                    "status": "valid",
                }
            )
            return 0
        database = _database()
        executor = GitHubCanaryExecutor(database, runtime, authorization)
        if args.command == "prepare":
            _output(executor.prepare())
            return 0
        if args.command == "approve":
            bearer_provider = SecureBearerTokenFileProvider(args.crag_token_file)
            _output(
                executor.decide_approval(
                    case_id=args.case_id,
                    kind=args.kind,
                    approval_id=args.approval_id,
                    approved=args.decision == "approve",
                    bearer_provider=bearer_provider,
                )
            )
            return 0
        installation_provider = SecureInstallationTokenFileProvider(
            args.github_token_file,
            runtime=runtime,
            authorization=authorization,
        )
        receipt = executor.run_case(
            args.case_id,
            token_provider=installation_provider,
            transport=StrictGitHubHttpsTransport(),
        )
        _output(
            {
                "branch_sha256": receipt.branch_sha256,
                "case_id": args.case_id,
                "commit_sha": receipt.commit_sha,
                "draft_pr_sha256": receipt.draft_pr_sha256,
                "environment": receipt.environment,
                "receipt_sha256": receipt.receipt_sha256,
                "real_github_sandbox_writes": receipt.real_github_sandbox_writes,
                "real_model_calls": receipt.real_model_calls,
                "real_business_repository_writes": receipt.real_business_repository_writes,
                "business_claim_allowed": receipt.business_claim_allowed,
                "quality_claim_allowed": receipt.quality_claim_allowed,
                "production_ready": receipt.production_ready,
                "status": "succeeded",
            }
        )
        return 0
    except ExpectedCanaryRestart as exc:
        _output(
            {
                "case_id": exc.case_id,
                "restart_required": True,
                "status": "expected_process_restart",
            }
        )
        return 75
    except (CanaryExecutorError, GitHubSandboxPublicationError) as exc:
        code = getattr(exc, "code", "executor_failed")
        _output({"code": code, "status": "failed"}, stream=sys.stderr)
        return 2
    finally:
        if database is not None:
            database.close()


__all__ = [
    "CANARY_CASE_IDS",
    "CanaryExecutorError",
    "ExpectedCanaryRestart",
    "GitHubCanaryExecutor",
    "GitHubCanaryRuntimeConfig",
    "GitTreeEntry",
    "RuntimeCase",
    "SecureBearerTokenFileProvider",
    "SecureInstallationTokenFileProvider",
    "freeze_runtime_case",
    "git_commit_sha",
    "git_object_sha",
    "git_tree_sha",
    "load_authorization",
    "load_runtime_config",
    "main",
    "validate_runtime_authorization",
    "validate_secret_metadata",
]


if __name__ == "__main__":
    raise SystemExit(main())
