"""Fail-closed GitHub sandbox Draft PR publisher for Phase 11B.

The default publisher is disabled.  The configured path uses only an injected
transport, hash-only durable state, and synthetic inputs.  No command shell, Git
credential helper, PAT fallback, model client, or merge operation exists here.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
import secrets
import socket
import ssl
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib import error, parse, request as urllib_request

from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from code_review_agent.identity import Principal, Role


AUTHORIZATION_SCHEMA_VERSION = "crag.github-sandbox-authorization/v1alpha1"
CANARY_ENVIRONMENT = "github_sandbox_canary"
CANARY_CASE_IDS = ("normal", "crash_after_branch", "crash_after_draft_pr")
PUBLISH_STATES = (
    "publish_intent_recorded",
    "branch_push_requested",
    "branch_push_observed",
    "draft_pr_requested",
    "draft_pr_observed",
    "receipt_reconciled",
    "quarantined",
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_BRANCH = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,253}[A-Za-z0-9])?\Z")
_PATH = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,253}[A-Za-z0-9])?\Z")
_UTC_SECOND = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_APPROVER_METHODS_DENIED = frozenset(
    {"anonymous", "agent", "finding", "github_webhook", "model", "system", "webhook"}
)
_FAILURE_CODES = frozenset(
    {
        "auth_401",
        "permission_403",
        "missing_404",
        "conflict_409",
        "validation_422",
        "rate_limited",
        "server_5xx",
        "timeout",
        "base_drift",
        "ref_collision",
        "branch_protected",
        "token_revoked",
        "token_expired",
        "ambiguous_result",
        "receipt_mismatch",
        "repository_mismatch",
        "installation_mismatch",
        "authorization_expired",
        "authorization_mismatch",
        "endpoint_denied",
        "redirect_denied",
        "budget_exhausted",
        "other",
    }
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _required(name: str, value: Any, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _digest(name: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
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


def _normalized_branch(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or _BRANCH.fullmatch(value) is None
        or ".." in value
        or "//" in value
        or value.endswith(".lock")
        or "/." in value
    ):
        raise ValueError(f"{name} must be a normalized branch")
    return value


def _synthetic_path(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or _PATH.fullmatch(value) is None
        or ".." in value
        or "//" in value
        or value.startswith("/")
        or "/." in value
    ):
        raise ValueError(f"{name} must be a normalized repository-relative path")
    return value


def _utc(value: str) -> datetime:
    if not isinstance(value, str) or _UTC_SECOND.fullmatch(value) is None:
        raise ValueError("authorization timestamps must be UTC whole-second values")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class GitHubFailure(str, Enum):
    AUTH_401 = "auth_401"
    PERMISSION_403 = "permission_403"
    MISSING_404 = "missing_404"
    CONFLICT_409 = "conflict_409"
    VALIDATION_422 = "validation_422"
    RATE_LIMITED = "rate_limited"
    SERVER_5XX = "server_5xx"
    TIMEOUT = "timeout"
    BASE_DRIFT = "base_drift"
    REF_COLLISION = "ref_collision"
    BRANCH_PROTECTED = "branch_protected"
    TOKEN_REVOKED = "token_revoked"
    TOKEN_EXPIRED = "token_expired"
    AMBIGUOUS_RESULT = "ambiguous_result"
    RECEIPT_MISMATCH = "receipt_mismatch"
    REPOSITORY_MISMATCH = "repository_mismatch"
    INSTALLATION_MISMATCH = "installation_mismatch"
    AUTHORIZATION_EXPIRED = "authorization_expired"
    AUTHORIZATION_MISMATCH = "authorization_mismatch"
    ENDPOINT_DENIED = "endpoint_denied"
    REDIRECT_DENIED = "redirect_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    OTHER = "other"


class GitHubSandboxPublicationError(RuntimeError):
    """Stable low-cardinality error with no provider or payload detail."""

    def __init__(self, code: str | GitHubFailure, *, status: int | None = None) -> None:
        normalized = code.value if isinstance(code, GitHubFailure) else code
        if normalized not in _FAILURE_CODES:
            normalized = GitHubFailure.OTHER.value
        self.code = normalized
        self.status = status if isinstance(status, int) and 100 <= status <= 599 else None
        super().__init__(self.code)


class GitHubSandboxApprovalConflict(GitHubSandboxPublicationError):
    pass


@dataclass(frozen=True)
class AuthorizationCase:
    case_id: str
    head_branch: str

    def __post_init__(self) -> None:
        if self.case_id not in CANARY_CASE_IDS:
            raise ValueError("unknown canary case")
        _normalized_branch("head_branch", self.head_branch)
        if not self.head_branch.startswith("crag-canary/"):
            raise ValueError("canary branch must use the frozen prefix")

    def to_dict(self) -> dict[str, str]:
        return {"case_id": self.case_id, "head_branch": self.head_branch}


@dataclass(frozen=True)
class GitHubSandboxAuthorization:
    schema_version: str
    authorization_id: str
    organization_id: str
    repository_owner: str
    repository_name: str
    repository_id: int
    github_app_id: int
    installation_id: int
    installation_account_id: int
    allowed_base_branch: str
    frozen_base_sha: str
    cases: tuple[AuthorizationCase, ...]
    max_denominator: int
    executable_code_sha: str
    runtime_config_sha256: str
    issued_at: str
    not_before: str
    expires_at: str
    max_requests: int
    max_mutations: int
    max_reads: int
    max_branches: int
    max_commits: int
    max_draft_prs: int
    cost_ceiling_micro_cny: int
    authorization_owner: str
    revocation_owner: str
    kill_switch_owner: str

    _FIELDS = frozenset(
        {
            "schema_version",
            "authorization_id",
            "organization_id",
            "repository_owner",
            "repository_name",
            "repository_id",
            "github_app_id",
            "installation_id",
            "installation_account_id",
            "allowed_base_branch",
            "frozen_base_sha",
            "cases",
            "max_denominator",
            "executable_code_sha",
            "runtime_config_sha256",
            "issued_at",
            "not_before",
            "expires_at",
            "max_requests",
            "max_mutations",
            "max_reads",
            "max_branches",
            "max_commits",
            "max_draft_prs",
            "cost_ceiling_micro_cny",
            "authorization_owner",
            "revocation_owner",
            "kill_switch_owner",
        }
    )

    def __post_init__(self) -> None:
        if self.schema_version != AUTHORIZATION_SCHEMA_VERSION:
            raise ValueError("unsupported authorization schema")
        _required("authorization_id", self.authorization_id, maximum=128)
        _required("organization_id", self.organization_id, maximum=96)
        if _OWNER.fullmatch(self.repository_owner) is None:
            raise ValueError("repository_owner is invalid")
        if _REPOSITORY.fullmatch(self.repository_name) is None:
            raise ValueError("repository_name is invalid")
        for name in ("repository_id", "github_app_id", "installation_id", "installation_account_id"):
            _positive_int(name, getattr(self, name))
        _normalized_branch("allowed_base_branch", self.allowed_base_branch)
        _git_sha("frozen_base_sha", self.frozen_base_sha)
        if len(self.cases) != 3 or tuple(case.case_id for case in self.cases) != CANARY_CASE_IDS:
            raise ValueError("authorization must freeze the three ordered canary cases")
        if len({case.head_branch for case in self.cases}) != 3:
            raise ValueError("authorization branches must be unique")
        if self.allowed_base_branch in {case.head_branch for case in self.cases}:
            raise ValueError("a canary head cannot be the base branch")
        if self.max_denominator != 3:
            raise ValueError("canary denominator must be exactly three")
        _git_sha("executable_code_sha", self.executable_code_sha)
        _digest("runtime_config_sha256", self.runtime_config_sha256)
        issued = _utc(self.issued_at)
        not_before = _utc(self.not_before)
        expires = _utc(self.expires_at)
        if not issued <= not_before < expires:
            raise ValueError("authorization time order is invalid")
        if (expires - not_before).total_seconds() > 3600:
            raise ValueError("authorization window exceeds one hour")
        _positive_int("max_requests", self.max_requests, maximum=100)
        _positive_int("max_mutations", self.max_mutations, maximum=30)
        _positive_int("max_reads", self.max_reads, maximum=100)
        _positive_int("max_branches", self.max_branches, maximum=3)
        _positive_int("max_commits", self.max_commits, maximum=3)
        _positive_int("max_draft_prs", self.max_draft_prs, maximum=3)
        if self.max_requests < self.max_mutations + self.max_reads:
            raise ValueError("total request budget is smaller than its partitions")
        if self.cost_ceiling_micro_cny != 0:
            raise ValueError("canary incremental cost ceiling must be zero")
        for name in ("authorization_owner", "revocation_owner", "kill_switch_owner"):
            _required(name, getattr(self, name), maximum=96)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GitHubSandboxAuthorization":
        if not isinstance(value, Mapping) or set(value) != cls._FIELDS:
            raise ValueError("authorization fields do not exactly match the schema")
        raw_cases = value["cases"]
        if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)):
            raise ValueError("authorization cases must be an array")
        cases: list[AuthorizationCase] = []
        for raw in raw_cases:
            if not isinstance(raw, Mapping) or set(raw) != {"case_id", "head_branch"}:
                raise ValueError("authorization case fields are invalid")
            cases.append(AuthorizationCase(case_id=raw["case_id"], head_branch=raw["head_branch"]))
        arguments = dict(value)
        arguments["cases"] = tuple(cases)
        return cls(**arguments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "authorization_id": self.authorization_id,
            "organization_id": self.organization_id,
            "repository_owner": self.repository_owner,
            "repository_name": self.repository_name,
            "repository_id": self.repository_id,
            "github_app_id": self.github_app_id,
            "installation_id": self.installation_id,
            "installation_account_id": self.installation_account_id,
            "allowed_base_branch": self.allowed_base_branch,
            "frozen_base_sha": self.frozen_base_sha,
            "cases": [case.to_dict() for case in self.cases],
            "max_denominator": self.max_denominator,
            "executable_code_sha": self.executable_code_sha,
            "runtime_config_sha256": self.runtime_config_sha256,
            "issued_at": self.issued_at,
            "not_before": self.not_before,
            "expires_at": self.expires_at,
            "max_requests": self.max_requests,
            "max_mutations": self.max_mutations,
            "max_reads": self.max_reads,
            "max_branches": self.max_branches,
            "max_commits": self.max_commits,
            "max_draft_prs": self.max_draft_prs,
            "cost_ceiling_micro_cny": self.cost_ceiling_micro_cny,
            "authorization_owner": self.authorization_owner,
            "revocation_owner": self.revocation_owner,
            "kill_switch_owner": self.kill_switch_owner,
        }

    @property
    def canonical_sha256(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))

    def case(self, case_id: str) -> AuthorizationCase:
        for item in self.cases:
            if item.case_id == case_id:
                return item
        raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)


@dataclass(frozen=True)
class GitBlob:
    path: str
    content: bytes
    blob_sha: str
    mode: str = "100644"

    def __post_init__(self) -> None:
        _synthetic_path("blob path", self.path)
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("blob content must be non-empty bytes")
        _git_sha("blob_sha", self.blob_sha)
        expected = hashlib.sha1(
            f"blob {len(self.content)}\0".encode("ascii") + self.content,
            usedforsecurity=False,
        ).hexdigest()
        if expected != self.blob_sha:
            raise ValueError("blob SHA does not match its content")
        if self.mode not in {"100644", "100755"}:
            raise ValueError("blob mode is not allowed")

    @property
    def content_sha256(self) -> str:
        return sha256_hex(self.content)

    def binding(self) -> dict[str, str]:
        return {
            "path_sha256": sha256_hex(self.path.encode("utf-8")),
            "content_sha256": self.content_sha256,
            "blob_sha": self.blob_sha,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class GitHubCanaryPublication:
    repair_job_id: str
    organization_id: str
    repair_repository_id: str
    repository_owner: str
    repository_name: str
    repository_id: int
    github_app_id: int
    installation_id: int
    installation_account_id: int
    base_branch: str
    base_sha: str
    base_tree_sha: str
    head_branch: str
    diff_sha256: str
    test_evidence_sha256: str
    durable_budget_sha256: str
    checkpoint_sha256: str
    exact_commit_sha: str
    commit_message: str
    commit_timestamp: str
    expected_tree_sha: str
    blobs: tuple[GitBlob, ...]
    title: str
    body: str
    title_marker_sha256: str
    body_marker_sha256: str
    publisher_payload_sha256: str
    authorization_id: str
    authorization_sha256: str
    app_idempotency_key: str
    canary_case_id: str
    executable_code_sha: str
    runtime_config_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "repair_job_id",
            "organization_id",
            "repair_repository_id",
            "authorization_id",
            "app_idempotency_key",
        ):
            _required(name, getattr(self, name), maximum=128)
        if _OWNER.fullmatch(self.repository_owner) is None or _REPOSITORY.fullmatch(self.repository_name) is None:
            raise ValueError("repository owner/name is invalid")
        for name in ("repository_id", "github_app_id", "installation_id", "installation_account_id"):
            _positive_int(name, getattr(self, name))
        _normalized_branch("base_branch", self.base_branch)
        _normalized_branch("head_branch", self.head_branch)
        if not self.head_branch.startswith("crag-canary/") or self.head_branch == self.base_branch:
            raise ValueError("head branch is outside the canary boundary")
        for name in (
            "base_sha",
            "base_tree_sha",
            "exact_commit_sha",
            "expected_tree_sha",
            "executable_code_sha",
        ):
            _git_sha(name, getattr(self, name))
        for name in (
            "diff_sha256",
            "test_evidence_sha256",
            "durable_budget_sha256",
            "checkpoint_sha256",
            "title_marker_sha256",
            "body_marker_sha256",
            "authorization_sha256",
            "runtime_config_sha256",
        ):
            _digest(name, getattr(self, name))
        _required("commit_message", self.commit_message, maximum=256)
        _utc(self.commit_timestamp)
        _required("title", self.title, maximum=256)
        _required("body", self.body, maximum=16_384)
        if not self.blobs or len(self.blobs) > 16 or len({blob.path for blob in self.blobs}) != len(self.blobs):
            raise ValueError("publication must contain one through sixteen unique blobs")
        if self.canary_case_id not in CANARY_CASE_IDS:
            raise ValueError("unknown canary case")
        if self.title_marker_sha256 != sha256_hex(self.title.encode("utf-8")):
            raise ValueError("title marker hash mismatch")
        marker_key = sha256_hex(self.app_idempotency_key.encode("utf-8"))
        marker = f"<!-- crag-canary:{marker_key} -->"
        if marker not in self.body:
            raise ValueError("body lacks the exact publisher marker")
        if self.body_marker_sha256 != sha256_hex(marker.encode("utf-8")):
            raise ValueError("body marker hash mismatch")
        if self.publisher_payload_sha256 == "auto":
            object.__setattr__(self, "publisher_payload_sha256", self.computed_payload_sha256)
        _digest("publisher_payload_sha256", self.publisher_payload_sha256)
        if self.publisher_payload_sha256 != self.computed_payload_sha256:
            raise ValueError("publisher payload hash mismatch")

    @property
    def commit_message_sha256(self) -> str:
        return sha256_hex(self.commit_message.encode("utf-8"))

    @property
    def body_sha256(self) -> str:
        return sha256_hex(self.body.encode("utf-8"))

    @property
    def payload_binding(self) -> dict[str, Any]:
        return {
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "base_tree_sha": self.base_tree_sha,
            "blobs": [blob.binding() for blob in self.blobs],
            "body_sha256": self.body_sha256,
            "commit_message_sha256": self.commit_message_sha256,
            "commit_timestamp": self.commit_timestamp,
            "diff_sha256": self.diff_sha256,
            "exact_commit_sha": self.exact_commit_sha,
            "expected_tree_sha": self.expected_tree_sha,
            "head_branch": self.head_branch,
            "repository_id": self.repository_id,
            "test_evidence_sha256": self.test_evidence_sha256,
            "title_marker_sha256": self.title_marker_sha256,
        }

    @property
    def computed_payload_sha256(self) -> str:
        return sha256_hex(canonical_json(self.payload_binding))

    @property
    def exact_binding(self) -> dict[str, Any]:
        return {
            "app_idempotency_key": self.app_idempotency_key,
            "authorization_id": self.authorization_id,
            "authorization_sha256": self.authorization_sha256,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "base_tree_sha": self.base_tree_sha,
            "body_marker_sha256": self.body_marker_sha256,
            "canary_case_id": self.canary_case_id,
            "checkpoint_sha256": self.checkpoint_sha256,
            "commit_message_sha256": self.commit_message_sha256,
            "commit_timestamp": self.commit_timestamp,
            "diff_sha256": self.diff_sha256,
            "durable_budget_sha256": self.durable_budget_sha256,
            "exact_commit_sha": self.exact_commit_sha,
            "executable_code_sha": self.executable_code_sha,
            "github_app_id": self.github_app_id,
            "head_branch": self.head_branch,
            "installation_account_id": self.installation_account_id,
            "installation_id": self.installation_id,
            "organization_id": self.organization_id,
            "publisher_payload_sha256": self.publisher_payload_sha256,
            "repair_job_id": self.repair_job_id,
            "repair_repository_id": self.repair_repository_id,
            "repository_id": self.repository_id,
            "repository_name": self.repository_name,
            "repository_owner": self.repository_owner,
            "runtime_config_sha256": self.runtime_config_sha256,
            "test_evidence_sha256": self.test_evidence_sha256,
            "title_marker_sha256": self.title_marker_sha256,
        }

    def approval_binding(self, kind: str) -> dict[str, Any]:
        if kind not in {"write", "draft_pr"}:
            raise ValueError("approval kind is invalid")
        return {"kind": kind, **self.exact_binding}

    def approval_binding_sha256(self, kind: str) -> str:
        return sha256_hex(canonical_json(self.approval_binding(kind)))


@dataclass(frozen=True)
class GitHubCanaryPublishRequest:
    publication: GitHubCanaryPublication
    write_approval_id: str
    write_approval_binding_sha256: str
    draft_pr_approval_id: str
    draft_pr_approval_binding_sha256: str

    def __post_init__(self) -> None:
        _required("write_approval_id", self.write_approval_id, maximum=128)
        _required("draft_pr_approval_id", self.draft_pr_approval_id, maximum=128)
        _digest("write_approval_binding_sha256", self.write_approval_binding_sha256)
        _digest("draft_pr_approval_binding_sha256", self.draft_pr_approval_binding_sha256)
        if self.write_approval_binding_sha256 != self.publication.approval_binding_sha256("write"):
            raise ValueError("WRITE approval binding mismatch")
        if self.draft_pr_approval_binding_sha256 != self.publication.approval_binding_sha256("draft_pr"):
            raise ValueError("DRAFT_PR approval binding mismatch")

    @property
    def binding_sha256(self) -> str:
        value = {
            **self.publication.exact_binding,
            "write_approval_id": self.write_approval_id,
            "write_approval_binding_sha256": self.write_approval_binding_sha256,
            "draft_pr_approval_id": self.draft_pr_approval_id,
            "draft_pr_approval_binding_sha256": self.draft_pr_approval_binding_sha256,
        }
        return sha256_hex(canonical_json(value))


@dataclass(frozen=True)
class GitHubCanaryReceipt:
    receipt_sha256: str
    request_sha256: str
    branch_sha256: str
    commit_sha: str
    draft_pr_sha256: str
    environment: str = CANARY_ENVIRONMENT
    synthetic_input_only: bool = True
    real_github_sandbox_writes: bool = False
    real_model_calls: bool = False
    real_business_repository_writes: bool = False
    business_claim_allowed: bool = False
    quality_claim_allowed: bool = False
    production_ready: bool = False

    def __post_init__(self) -> None:
        for name in ("receipt_sha256", "request_sha256", "branch_sha256", "draft_pr_sha256"):
            _digest(name, getattr(self, name))
        _git_sha("commit_sha", self.commit_sha)
        if self.environment != CANARY_ENVIRONMENT or self.synthetic_input_only is not True:
            raise ValueError("receipt environment boundary is invalid")
        if not isinstance(self.real_github_sandbox_writes, bool) or self.real_model_calls is not False:
            raise ValueError("receipt external-call boundary is invalid")
        if any(
            (
                self.real_business_repository_writes,
                self.business_claim_allowed,
                self.quality_claim_allowed,
                self.production_ready,
            )
        ):
            raise ValueError("receipt claim boundary is invalid")


@dataclass(frozen=True)
class InstallationToken:
    value: str
    app_id: int
    installation_id: int
    installation_account_id: int
    expires_at: datetime
    revoked: bool = False

    def __post_init__(self) -> None:
        _required("installation token", self.value, maximum=4096)
        for name in ("app_id", "installation_id", "installation_account_id"):
            _positive_int(name, getattr(self, name))
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise ValueError("token expiry must be timezone-aware")


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    headers: Mapping[str, str]
    body: Any


class GitHubTransport(Protocol):
    real_github_writes: bool

    def send(
        self,
        endpoint: str,
        parameters: Mapping[str, Any],
        *,
        body: Mapping[str, Any] | None,
        token: str,
        timeout_seconds: float,
    ) -> GitHubResponse: ...


_ENDPOINTS: Mapping[str, tuple[str, str, frozenset[str]]] = {
    "repository_read": ("GET", "/repos/{owner}/{repo}", frozenset({"owner", "repo"})),
    "ref_read": ("GET", "/repos/{owner}/{repo}/git/ref/heads/{branch}", frozenset({"owner", "repo", "branch"})),
    "blob_read": ("GET", "/repos/{owner}/{repo}/git/blobs/{sha}", frozenset({"owner", "repo", "sha"})),
    "tree_read": ("GET", "/repos/{owner}/{repo}/git/trees/{sha}", frozenset({"owner", "repo", "sha"})),
    "commit_read": ("GET", "/repos/{owner}/{repo}/git/commits/{sha}", frozenset({"owner", "repo", "sha"})),
    "blob_create": ("POST", "/repos/{owner}/{repo}/git/blobs", frozenset({"owner", "repo"})),
    "tree_create": ("POST", "/repos/{owner}/{repo}/git/trees", frozenset({"owner", "repo"})),
    "commit_create": ("POST", "/repos/{owner}/{repo}/git/commits", frozenset({"owner", "repo"})),
    "ref_create": ("POST", "/repos/{owner}/{repo}/git/refs", frozenset({"owner", "repo"})),
    "draft_pr_list": ("GET", "/repos/{owner}/{repo}/pulls", frozenset({"owner", "repo", "head", "base"})),
    "draft_pr_create": ("POST", "/repos/{owner}/{repo}/pulls", frozenset({"owner", "repo"})),
    "draft_pr_read": ("GET", "/repos/{owner}/{repo}/pulls/{number}", frozenset({"owner", "repo", "number"})),
}


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class StrictGitHubHttpsTransport:
    """Pinned HTTPS transport. Callers inject fakes in every ordinary test."""

    real_github_writes = True

    def __init__(
        self,
        *,
        opener: Any | None = None,
        max_response_bytes: int = 1_048_576,
    ) -> None:
        if isinstance(max_response_bytes, bool) or not 1024 <= max_response_bytes <= 4_194_304:
            raise ValueError("response-size limit is invalid")
        context = ssl.create_default_context()
        self._opener = opener or urllib_request.build_opener(
            urllib_request.HTTPSHandler(context=context), _NoRedirect()
        )
        self._max_response_bytes = max_response_bytes

    @staticmethod
    def _url(endpoint: str, parameters: Mapping[str, Any]) -> tuple[str, str]:
        definition = _ENDPOINTS.get(endpoint)
        if definition is None:
            raise GitHubSandboxPublicationError(GitHubFailure.ENDPOINT_DENIED)
        method, template, expected = definition
        if set(parameters) != expected:
            raise GitHubSandboxPublicationError(GitHubFailure.ENDPOINT_DENIED)
        values = {key: parse.quote(str(parameters[key]), safe="") for key in expected}
        query = ""
        if endpoint == "draft_pr_list":
            query = "?" + parse.urlencode(
                {
                    "head": f"{parameters['owner']}:{parameters['head']}",
                    "base": str(parameters["base"]),
                    "state": "open",
                    "per_page": "100",
                }
            )
            values = {"owner": values["owner"], "repo": values["repo"]}
        url = "https://api.github.com" + template.format(**values) + query
        parsed = parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "api.github.com" or parsed.port not in {None, 443}:
            raise GitHubSandboxPublicationError(GitHubFailure.ENDPOINT_DENIED)
        return method, url

    @staticmethod
    def _headers(values: Any) -> dict[str, str]:
        allowed = {"retry-after", "x-ratelimit-remaining", "x-ratelimit-reset"}
        result = {
            str(key).casefold(): str(value)[:128]
            for key, value in values.items()
            if str(key).casefold() in allowed
        }
        link = next(
            (str(value) for key, value in values.items() if str(key).casefold() == "link"),
            "",
        )
        if 'rel="next"' in link:
            result["has-next-page"] = "true"
        return result

    @staticmethod
    def _sanitize(endpoint: str, body: Any) -> Any:
        if endpoint in {"blob_read", "tree_read", "blob_create", "tree_create"}:
            return {"sha": body.get("sha")} if isinstance(body, Mapping) else None
        if endpoint == "repository_read" and isinstance(body, Mapping):
            owner = body.get("owner")
            return {
                "id": body.get("id"),
                "name": body.get("name"),
                "owner": {"login": owner.get("login")} if isinstance(owner, Mapping) else None,
                "default_branch": body.get("default_branch"),
            }
        if endpoint in {"ref_read", "ref_create"} and isinstance(body, Mapping):
            obj = body.get("object")
            return {
                "ref": body.get("ref"),
                "object": {"sha": obj.get("sha")} if isinstance(obj, Mapping) else None,
            }
        if endpoint in {"commit_read", "commit_create"} and isinstance(body, Mapping):
            tree = body.get("tree")
            return {
                "sha": body.get("sha"),
                "tree": {"sha": tree.get("sha")} if isinstance(tree, Mapping) else None,
            }

        def clean_pr(value: Any) -> Any:
            if not isinstance(value, Mapping):
                return None
            head = value.get("head")
            base = value.get("base")
            return {
                "number": value.get("number"),
                "draft": value.get("draft"),
                "title": value.get("title"),
                "body": value.get("body"),
                "head": {
                    "ref": head.get("ref"),
                    "sha": head.get("sha"),
                }
                if isinstance(head, Mapping)
                else None,
                "base": {"ref": base.get("ref")} if isinstance(base, Mapping) else None,
            }

        if endpoint == "draft_pr_list" and isinstance(body, list):
            return [item for value in body if (item := clean_pr(value)) is not None]
        if endpoint in {"draft_pr_create", "draft_pr_read"}:
            return clean_pr(body)
        return None

    def send(
        self,
        endpoint: str,
        parameters: Mapping[str, Any],
        *,
        body: Mapping[str, Any] | None,
        token: str,
        timeout_seconds: float,
    ) -> GitHubResponse:
        method, url = self._url(endpoint, parameters)
        if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise GitHubSandboxPublicationError(GitHubFailure.BUDGET_EXHAUSTED)
        _required("installation token", token, maximum=4096)
        data = None if body is None else canonical_json(body)
        req = urllib_request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "crag-github-sandbox-canary-v1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            response = self._opener.open(req, timeout=float(timeout_seconds))
            status = int(response.getcode())
            headers = self._headers(response.headers)
            raw = response.read(self._max_response_bytes + 1)
            close = getattr(response, "close", None)
            if callable(close):
                close()
        except error.HTTPError as exc:
            status = int(exc.code)
            headers = self._headers(exc.headers or {})
            raw = exc.read(self._max_response_bytes + 1)
            exc.close()
        except (TimeoutError, socket.timeout):
            raise GitHubSandboxPublicationError(GitHubFailure.TIMEOUT) from None
        except (error.URLError, OSError):
            raise GitHubSandboxPublicationError(GitHubFailure.OTHER) from None
        if len(raw) > self._max_response_bytes:
            raise GitHubSandboxPublicationError(GitHubFailure.OTHER, status=status)
        parsed_body: Any = None
        if raw:
            try:
                parsed_body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed_body = None
        return GitHubResponse(status=status, headers=headers, body=self._sanitize(endpoint, parsed_body))


class GitHubCanaryStore:
    """Hash-only approval, request-ledger, outbox, and receipt persistence."""

    def __init__(self, engine: Engine, *, clock: Callable[[], float] = time.time) -> None:
        self.engine = engine
        self.clock = clock

    def _job_lineage(self, connection: Any, publication: GitHubCanaryPublication) -> None:
        row = connection.execute(
            text(
                "SELECT organization_id, repository_id, base_sha, state, checkpoint_json, "
                "checkpoint_sha256, current_diff_sha256, budget_sha256 FROM repair_jobs WHERE id=:id"
            ),
            {"id": publication.repair_job_id},
        ).first()
        if row is None:
            raise GitHubSandboxApprovalConflict(GitHubFailure.AUTHORIZATION_MISMATCH)
        record = dict(row._mapping)
        try:
            checkpoint = json.loads(str(record["checkpoint_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise GitHubSandboxApprovalConflict(GitHubFailure.AUTHORIZATION_MISMATCH) from None
        if (
            record["organization_id"] != publication.organization_id
            or record["repository_id"] != publication.repair_repository_id
            or record["base_sha"] != publication.base_sha
            or record["checkpoint_sha256"] != publication.checkpoint_sha256
            or record["current_diff_sha256"] != publication.diff_sha256
            or record["budget_sha256"] != publication.durable_budget_sha256
            or checkpoint.get("tests_sha256") != publication.test_evidence_sha256
            or record["state"]
            not in {
                "awaiting_draft_pr_approval",
                "queued_publish",
                "publishing",
            }
        ):
            raise GitHubSandboxApprovalConflict(GitHubFailure.AUTHORIZATION_MISMATCH)

    def issue_approval(
        self,
        publication: GitHubCanaryPublication,
        *,
        kind: str,
        ttl_seconds: float,
    ) -> tuple[str, str]:
        if kind not in {"write", "draft_pr"}:
            raise ValueError("approval kind is invalid")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or not 0 < ttl_seconds <= 3600
        ):
            raise ValueError("approval TTL is invalid")
        approval_id = f"github-canary-{kind}-{secrets.token_hex(16)}"
        binding_sha256 = publication.approval_binding_sha256(kind)
        now = float(self.clock())
        with self.engine.begin() as connection:
            self._job_lineage(connection, publication)
            connection.execute(
                text(
                    "INSERT INTO github_canary_approvals "
                    "(id, repair_job_id, organization_id, kind, binding_sha256, status, "
                    "expires_at, approver_sha256, decided_at, created_at) VALUES "
                    "(:id, :job, :organization, :kind, :binding, 'issued', :expires, "
                    "NULL, NULL, :created)"
                ),
                {
                    "id": approval_id,
                    "job": publication.repair_job_id,
                    "organization": publication.organization_id,
                    "kind": kind,
                    "binding": binding_sha256,
                    "expires": now + float(ttl_seconds),
                    "created": now,
                },
            )
        return approval_id, binding_sha256

    def decide_approval(
        self,
        approval_id: str,
        publication: GitHubCanaryPublication,
        *,
        kind: str,
        actor: Principal,
        approved: bool,
    ) -> None:
        _required("approval_id", approval_id, maximum=128)
        if kind not in {"write", "draft_pr"}:
            raise ValueError("approval kind is invalid")
        if (
            actor.organization_id != publication.organization_id
            or actor.role not in {Role.MAINTAINER, Role.ORG_ADMIN}
            or actor.auth_method.casefold() in _APPROVER_METHODS_DENIED
        ):
            raise GitHubSandboxApprovalConflict(GitHubFailure.AUTHORIZATION_MISMATCH)
        now = float(self.clock())
        binding = publication.approval_binding_sha256(kind)
        with self.engine.begin() as connection:
            self._job_lineage(connection, publication)
            row = connection.execute(
                text(
                    "SELECT status, binding_sha256, expires_at FROM github_canary_approvals "
                    "WHERE id=:id AND repair_job_id=:job AND organization_id=:organization "
                    "AND kind=:kind"
                ),
                {
                    "id": approval_id,
                    "job": publication.repair_job_id,
                    "organization": publication.organization_id,
                    "kind": kind,
                },
            ).first()
            if row is None:
                raise GitHubSandboxApprovalConflict(GitHubFailure.AUTHORIZATION_MISMATCH)
            record = dict(row._mapping)
            if record["status"] != "issued":
                raise GitHubSandboxApprovalConflict(GitHubFailure.CONFLICT_409)
            if float(record["expires_at"]) < now or record["binding_sha256"] != binding:
                raise GitHubSandboxApprovalConflict(GitHubFailure.AUTHORIZATION_MISMATCH)
            result = connection.execute(
                text(
                    "UPDATE github_canary_approvals SET status=:status, approver_sha256=:actor, "
                    "decided_at=:decided WHERE id=:id AND status='issued'"
                ),
                {
                    "status": "consumed" if approved else "rejected",
                    "actor": sha256_hex(actor.principal_id.encode("utf-8")),
                    "decided": now,
                    "id": approval_id,
                },
            )
            if result.rowcount != 1:
                raise GitHubSandboxApprovalConflict(GitHubFailure.CONFLICT_409)

    def validate_approvals(self, request: GitHubCanaryPublishRequest) -> None:
        publication = request.publication
        with self.engine.connect() as connection:
            self._job_lineage(connection, publication)
            rows = connection.execute(
                text(
                    "SELECT id, kind, binding_sha256, status FROM github_canary_approvals "
                    "WHERE id IN (:write_id, :draft_id) AND repair_job_id=:job"
                ),
                {
                    "write_id": request.write_approval_id,
                    "draft_id": request.draft_pr_approval_id,
                    "job": publication.repair_job_id,
                },
            ).all()
        found = {str(row._mapping["kind"]): dict(row._mapping) for row in rows}
        expected = {
            "write": (request.write_approval_id, request.write_approval_binding_sha256),
            "draft_pr": (
                request.draft_pr_approval_id,
                request.draft_pr_approval_binding_sha256,
            ),
        }
        if set(found) != set(expected):
            raise GitHubSandboxApprovalConflict(GitHubFailure.AUTHORIZATION_MISMATCH)
        for kind, (approval_id, binding_sha256) in expected.items():
            record = found[kind]
            if (
                record["id"] != approval_id
                or record["binding_sha256"] != binding_sha256
                or record["status"] != "consumed"
            ):
                raise GitHubSandboxApprovalConflict(GitHubFailure.AUTHORIZATION_MISMATCH)

    def record_intent(
        self,
        request: GitHubCanaryPublishRequest,
        *,
        real_github_writes: bool,
    ) -> Mapping[str, Any]:
        publication = request.publication
        if not isinstance(real_github_writes, bool):
            raise ValueError("real_github_writes must be boolean")
        now = float(self.clock())
        with self.engine.begin() as connection:
            budget = connection.execute(
                text(
                    "SELECT authorization_sha256 FROM github_canary_authorization_budgets "
                    "WHERE authorization_id=:authorization"
                ),
                {"authorization": publication.authorization_id},
            ).first()
            if budget is None:
                connection.execute(
                    text(
                        "INSERT INTO github_canary_authorization_budgets "
                        "(authorization_id, authorization_sha256, request_count, mutation_count, "
                        "read_count, branch_count, commit_count, draft_pr_count, created_at, updated_at) "
                        "VALUES (:authorization, :sha, 0, 0, 0, 0, 0, 0, :now, :now)"
                    ),
                    {
                        "authorization": publication.authorization_id,
                        "sha": publication.authorization_sha256,
                        "now": now,
                    },
                )
            elif budget._mapping["authorization_sha256"] != publication.authorization_sha256:
                raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
            row = connection.execute(
                text(
                    "SELECT * FROM github_canary_publications WHERE idempotency_key=:key"
                ),
                {"key": publication.app_idempotency_key},
            ).first()
            if row is not None:
                record = dict(row._mapping)
                if (
                    record["repair_job_id"] != publication.repair_job_id
                    or record["authorization_id"] != publication.authorization_id
                    or record["canary_case_id"] != publication.canary_case_id
                    or record["authorization_sha256"] != publication.authorization_sha256
                    or record["binding_sha256"] != request.binding_sha256
                    or record["payload_sha256"] != publication.publisher_payload_sha256
                    or bool(record["real_github_writes"]) != real_github_writes
                ):
                    raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
                return record
            connection.execute(
                text(
                    "INSERT INTO github_canary_publications "
                    "(idempotency_key, repair_job_id, authorization_id, canary_case_id, authorization_sha256, "
                    "binding_sha256, payload_sha256, real_github_writes, state, failure_code, request_count, "
                    "mutation_count, read_count, branch_count, commit_count, draft_pr_count, "
                    "receipt_sha256, created_at, updated_at) VALUES "
                    "(:key, :job, :authorization, :case_id, :authorization_sha, :binding, :payload, :real_writes, "
                    "'publish_intent_recorded', NULL, 0, 0, 0, 0, 0, 0, NULL, :now, :now)"
                ),
                {
                    "key": publication.app_idempotency_key,
                    "job": publication.repair_job_id,
                    "authorization": publication.authorization_id,
                    "case_id": publication.canary_case_id,
                    "authorization_sha": publication.authorization_sha256,
                    "binding": request.binding_sha256,
                    "payload": publication.publisher_payload_sha256,
                    "real_writes": real_github_writes,
                    "now": now,
                },
            )
        return self.publication(publication.app_idempotency_key)

    def publication(self, idempotency_key: str) -> Mapping[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT * FROM github_canary_publications WHERE idempotency_key=:key"),
                {"key": idempotency_key},
            ).first()
        if row is None:
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
        return dict(row._mapping)

    def transition(self, idempotency_key: str, expected: str, target: str) -> None:
        if expected not in PUBLISH_STATES or target not in PUBLISH_STATES:
            raise ValueError("publication state is invalid")
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE github_canary_publications SET state=:target, updated_at=:now "
                    "WHERE idempotency_key=:key AND state=:expected"
                ),
                {
                    "target": target,
                    "now": float(self.clock()),
                    "key": idempotency_key,
                    "expected": expected,
                },
            )
            if result.rowcount != 1:
                row = connection.execute(
                    text(
                        "SELECT state FROM github_canary_publications WHERE idempotency_key=:key"
                    ),
                    {"key": idempotency_key},
                ).first()
                if row is None or row._mapping["state"] != target:
                    raise GitHubSandboxPublicationError(GitHubFailure.CONFLICT_409)

    def reserve_request(
        self,
        idempotency_key: str,
        authorization: GitHubSandboxAuthorization,
        *,
        endpoint: str,
        request_sha256: str,
        mutation_key: str | None = None,
    ) -> Mapping[str, Any]:
        if endpoint not in _ENDPOINTS:
            raise GitHubSandboxPublicationError(GitHubFailure.ENDPOINT_DENIED)
        _digest("request_sha256", request_sha256)
        is_mutation = _ENDPOINTS[endpoint][0] != "GET"
        if is_mutation != (mutation_key is not None):
            raise GitHubSandboxPublicationError(GitHubFailure.ENDPOINT_DENIED)
        now = float(self.clock())
        with self.engine.begin() as connection:
            if mutation_key is not None:
                prior = connection.execute(
                    text(
                        "SELECT * FROM github_canary_requests WHERE idempotency_key=:key "
                        "AND operation_key=:operation"
                    ),
                    {"key": idempotency_key, "operation": mutation_key},
                ).first()
                if prior is not None:
                    record = dict(prior._mapping)
                    if record["endpoint"] != endpoint or record["request_sha256"] != request_sha256:
                        raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
                    record["_reserved_new"] = False
                    return record
            row = connection.execute(
                text(
                    "SELECT request_count, mutation_count, read_count, branch_count, "
                    "commit_count, draft_pr_count, state, authorization_id "
                    "FROM github_canary_publications "
                    "WHERE idempotency_key=:key"
                ),
                {"key": idempotency_key},
            ).first()
            if row is None or row._mapping["state"] == "quarantined":
                raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
            counts = dict(row._mapping)
            request_count = int(counts["request_count"]) + 1
            mutation_count = int(counts["mutation_count"]) + int(is_mutation)
            read_count = int(counts["read_count"]) + int(not is_mutation)
            branch_count = int(counts["branch_count"]) + int(endpoint == "ref_create")
            commit_count = int(counts["commit_count"]) + int(endpoint == "commit_create")
            draft_pr_count = int(counts["draft_pr_count"]) + int(endpoint == "draft_pr_create")
            budget_result = connection.execute(
                text(
                    "UPDATE github_canary_authorization_budgets SET "
                    "request_count=request_count+1, mutation_count=mutation_count+:mutation, "
                    "read_count=read_count+:read, branch_count=branch_count+:branch, "
                    "commit_count=commit_count+:commit, draft_pr_count=draft_pr_count+:draft, "
                    "updated_at=:now WHERE authorization_id=:authorization AND "
                    "authorization_sha256=:authorization_sha AND request_count+1<=:max_requests "
                    "AND mutation_count+:mutation<=:max_mutations "
                    "AND read_count+:read<=:max_reads AND branch_count+:branch<=:max_branches "
                    "AND commit_count+:commit<=:max_commits "
                    "AND draft_pr_count+:draft<=:max_drafts"
                ),
                {
                    "mutation": int(is_mutation),
                    "read": int(not is_mutation),
                    "branch": int(endpoint == "ref_create"),
                    "commit": int(endpoint == "commit_create"),
                    "draft": int(endpoint == "draft_pr_create"),
                    "now": now,
                    "authorization": counts["authorization_id"],
                    "authorization_sha": authorization.canonical_sha256,
                    "max_requests": authorization.max_requests,
                    "max_mutations": authorization.max_mutations,
                    "max_reads": authorization.max_reads,
                    "max_branches": authorization.max_branches,
                    "max_commits": authorization.max_commits,
                    "max_drafts": authorization.max_draft_prs,
                },
            )
            if budget_result.rowcount != 1:
                raise GitHubSandboxPublicationError(GitHubFailure.BUDGET_EXHAUSTED)
            operation_key = mutation_key or f"read:{request_count}:{endpoint}"
            connection.execute(
                text(
                    "UPDATE github_canary_publications SET request_count=:requests, "
                    "mutation_count=:mutations, read_count=:reads, branch_count=:branches, "
                    "commit_count=:commits, draft_pr_count=:drafts, updated_at=:now "
                    "WHERE idempotency_key=:key"
                ),
                {
                    "requests": request_count,
                    "mutations": mutation_count,
                    "reads": read_count,
                    "branches": branch_count,
                    "commits": commit_count,
                    "drafts": draft_pr_count,
                    "now": now,
                    "key": idempotency_key,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO github_canary_requests "
                    "(idempotency_key, request_index, operation_key, endpoint, request_sha256, "
                    "is_mutation, status, failure_code, http_status, response_sha256, "
                    "created_at, updated_at) VALUES "
                    "(:key, :index, :operation, :endpoint, :request, :mutation, 'requested', "
                    "NULL, NULL, NULL, :now, :now)"
                ),
                {
                    "key": idempotency_key,
                    "index": request_count,
                    "operation": operation_key,
                    "endpoint": endpoint,
                    "request": request_sha256,
                    "mutation": is_mutation,
                    "now": now,
                },
            )
        record = dict(self.request_record(idempotency_key, operation_key))
        record["_reserved_new"] = True
        return record

    def request_record(self, idempotency_key: str, operation_key: str) -> Mapping[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM github_canary_requests WHERE idempotency_key=:key "
                    "AND operation_key=:operation"
                ),
                {"key": idempotency_key, "operation": operation_key},
            ).first()
        if row is None:
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
        return dict(row._mapping)

    def finish_request(
        self,
        idempotency_key: str,
        operation_key: str,
        *,
        status: str,
        response_sha256: str | None = None,
        failure_code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        if status not in {"observed", "ambiguous", "failed"}:
            raise ValueError("request status is invalid")
        if response_sha256 is not None:
            _digest("response_sha256", response_sha256)
        if failure_code is not None and failure_code not in _FAILURE_CODES:
            raise ValueError("failure code is invalid")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE github_canary_requests SET status=:status, failure_code=:failure, "
                    "http_status=:http_status, response_sha256=:response, updated_at=:now "
                    "WHERE idempotency_key=:key AND operation_key=:operation"
                ),
                {
                    "status": status,
                    "failure": failure_code,
                    "http_status": http_status,
                    "response": response_sha256,
                    "now": float(self.clock()),
                    "key": idempotency_key,
                    "operation": operation_key,
                },
            )

    def quarantine(self, idempotency_key: str, failure_code: str) -> None:
        if failure_code not in _FAILURE_CODES:
            failure_code = GitHubFailure.OTHER.value
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE github_canary_publications SET state='quarantined', "
                    "failure_code=:failure, updated_at=:now WHERE idempotency_key=:key "
                    "AND state!='receipt_reconciled'"
                ),
                {"failure": failure_code, "now": float(self.clock()), "key": idempotency_key},
            )

    def reconcile_receipt(self, idempotency_key: str, receipt: GitHubCanaryReceipt) -> None:
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE github_canary_publications SET state='receipt_reconciled', "
                    "receipt_sha256=:receipt, branch_sha256=:branch, commit_sha=:commit, "
                    "draft_pr_sha256=:draft, updated_at=:now WHERE idempotency_key=:key "
                    "AND state='draft_pr_observed'"
                ),
                {
                    "receipt": receipt.receipt_sha256,
                    "branch": receipt.branch_sha256,
                    "commit": receipt.commit_sha,
                    "draft": receipt.draft_pr_sha256,
                    "now": float(self.clock()),
                    "key": idempotency_key,
                },
            )
            if result.rowcount != 1:
                row = connection.execute(
                    text(
                        "SELECT state, receipt_sha256 FROM github_canary_publications "
                        "WHERE idempotency_key=:key"
                    ),
                    {"key": idempotency_key},
                ).first()
                if (
                    row is None
                    or row._mapping["state"] != "receipt_reconciled"
                    or row._mapping["receipt_sha256"] != receipt.receipt_sha256
                ):
                    raise GitHubSandboxPublicationError(GitHubFailure.RECEIPT_MISMATCH)


def classify_github_failure(status: int, headers: Mapping[str, str]) -> GitHubFailure | None:
    normalized = {str(key).casefold(): str(value) for key, value in headers.items()}
    if 200 <= status <= 299:
        return None
    if 300 <= status <= 399:
        return GitHubFailure.REDIRECT_DENIED
    if status == 401:
        return GitHubFailure.AUTH_401
    if status in {403, 429}:
        limited = (
            status == 429
            or "retry-after" in normalized
            or normalized.get("x-ratelimit-remaining") == "0"
        )
        return GitHubFailure.RATE_LIMITED if limited else GitHubFailure.PERMISSION_403
    if status == 404:
        return GitHubFailure.MISSING_404
    if status == 409:
        return GitHubFailure.CONFLICT_409
    if status == 422:
        return GitHubFailure.VALIDATION_422
    if 500 <= status <= 599:
        return GitHubFailure.SERVER_5XX
    return GitHubFailure.OTHER


class GitHubDraftPrPublisher:
    """Default-disabled, authorization-bound GitHub sandbox publisher."""

    def __init__(
        self,
        *,
        feature_enabled: bool = False,
        real_github_writes_enabled: bool = False,
        authorization: GitHubSandboxAuthorization | None = None,
        authorization_sha256: str | None = None,
        executable_code_sha: str | None = None,
        runtime_config_sha256: str | None = None,
        repository_allowlist: frozenset[tuple[str, str, int]] = frozenset(),
        protected_branches: frozenset[str] = frozenset({"main", "master"}),
        store: GitHubCanaryStore | None = None,
        transport: GitHubTransport | None = None,
        token_provider: Callable[[], InstallationToken] | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], float] = time.time,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self._enabled = False
        self._real_github_writes = False
        self.authorization = authorization
        self.store = store
        self.transport = transport
        self.token_provider = token_provider
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.fault = fault or (lambda _name: None)
        self.protected_branches = frozenset(protected_branches)
        if not feature_enabled:
            return
        if (
            authorization is None
            or store is None
            or transport is None
            or token_provider is None
            or authorization_sha256 is None
            or executable_code_sha is None
            or runtime_config_sha256 is None
        ):
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
        _digest("authorization_sha256", authorization_sha256)
        _git_sha("executable_code_sha", executable_code_sha)
        _digest("runtime_config_sha256", runtime_config_sha256)
        if (
            authorization.canonical_sha256 != authorization_sha256
            or authorization.executable_code_sha != executable_code_sha
            or authorization.runtime_config_sha256 != runtime_config_sha256
            or (
                authorization.repository_owner,
                authorization.repository_name,
                authorization.repository_id,
            )
            not in repository_allowlist
        ):
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
        transport_is_real = getattr(transport, "real_github_writes", None)
        if not isinstance(real_github_writes_enabled, bool) or transport_is_real is not real_github_writes_enabled:
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30
        ):
            raise GitHubSandboxPublicationError(GitHubFailure.BUDGET_EXHAUSTED)
        self._enabled = True
        self._real_github_writes = real_github_writes_enabled

    @staticmethod
    def _request_hash(endpoint: str, parameters: Mapping[str, Any], body: Any) -> str:
        safe_body = body
        if endpoint == "blob_create" and isinstance(body, Mapping):
            safe_body = {
                "encoding": body.get("encoding"),
                "content_sha256": sha256_hex(str(body.get("content", "")).encode("ascii")),
            }
        elif endpoint in {"draft_pr_create"} and isinstance(body, Mapping):
            safe_body = {
                **{key: body.get(key) for key in ("head", "base", "draft", "maintainer_can_modify")},
                "title_sha256": sha256_hex(str(body.get("title", "")).encode("utf-8")),
                "body_sha256": sha256_hex(str(body.get("body", "")).encode("utf-8")),
            }
        return sha256_hex(canonical_json({"endpoint": endpoint, "parameters": dict(parameters), "body": safe_body}))

    def _validate_authorization(self, publication: GitHubCanaryPublication) -> None:
        if not self._enabled or self.authorization is None:
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
        authorization = self.authorization
        now = datetime.fromtimestamp(float(self.clock()), tz=timezone.utc)
        if now < _utc(authorization.not_before):
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
        if now >= _utc(authorization.expires_at):
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_EXPIRED)
        case = authorization.case(publication.canary_case_id)
        checks = (
            publication.authorization_id == authorization.authorization_id,
            publication.authorization_sha256 == authorization.canonical_sha256,
            publication.organization_id == authorization.organization_id,
            publication.repository_owner == authorization.repository_owner,
            publication.repository_name == authorization.repository_name,
            publication.repository_id == authorization.repository_id,
            publication.github_app_id == authorization.github_app_id,
            publication.installation_id == authorization.installation_id,
            publication.installation_account_id == authorization.installation_account_id,
            publication.base_branch == authorization.allowed_base_branch,
            publication.base_sha == authorization.frozen_base_sha,
            publication.head_branch == case.head_branch,
            publication.executable_code_sha == authorization.executable_code_sha,
            publication.runtime_config_sha256 == authorization.runtime_config_sha256,
        )
        if not all(checks):
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)

    def _token(self) -> InstallationToken:
        if self.authorization is None or self.token_provider is None:
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
        now = datetime.fromtimestamp(float(self.clock()), tz=timezone.utc)
        if now < _utc(self.authorization.not_before):
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
        if now >= _utc(self.authorization.expires_at):
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_EXPIRED)
        token = self.token_provider()
        if token.revoked:
            raise GitHubSandboxPublicationError(GitHubFailure.TOKEN_REVOKED)
        if token.expires_at.astimezone(timezone.utc) <= now:
            raise GitHubSandboxPublicationError(GitHubFailure.TOKEN_EXPIRED)
        if (
            token.app_id != self.authorization.github_app_id
            or token.installation_id != self.authorization.installation_id
            or token.installation_account_id != self.authorization.installation_account_id
        ):
            raise GitHubSandboxPublicationError(GitHubFailure.INSTALLATION_MISMATCH)
        return token

    def _read(
        self,
        publication: GitHubCanaryPublication,
        endpoint: str,
        parameters: Mapping[str, Any],
    ) -> GitHubResponse:
        assert self.authorization is not None and self.store is not None and self.transport is not None
        request_sha = self._request_hash(endpoint, parameters, None)
        record = self.store.reserve_request(
            publication.app_idempotency_key,
            self.authorization,
            endpoint=endpoint,
            request_sha256=request_sha,
        )
        operation = str(record["operation_key"])
        try:
            response = self.transport.send(
                endpoint,
                parameters,
                body=None,
                token=self._token().value,
                timeout_seconds=float(self.timeout_seconds),
            )
        except GitHubSandboxPublicationError as exc:
            self.store.finish_request(
                publication.app_idempotency_key,
                operation,
                status="failed",
                failure_code=exc.code,
                http_status=exc.status,
            )
            raise
        failure = classify_github_failure(response.status, response.headers)
        if failure is not None:
            self.store.finish_request(
                publication.app_idempotency_key,
                operation,
                status="failed",
                failure_code=failure.value,
                http_status=response.status,
            )
            raise GitHubSandboxPublicationError(failure, status=response.status)
        response_sha = sha256_hex(canonical_json(response.body))
        self.store.finish_request(
            publication.app_idempotency_key,
            operation,
            status="observed",
            response_sha256=response_sha,
            http_status=response.status,
        )
        return response

    def _mutate(
        self,
        publication: GitHubCanaryPublication,
        endpoint: str,
        parameters: Mapping[str, Any],
        body: Mapping[str, Any],
        operation_key: str,
    ) -> tuple[GitHubResponse | None, Mapping[str, Any]]:
        assert self.authorization is not None and self.store is not None and self.transport is not None
        request_sha = self._request_hash(endpoint, parameters, body)
        record = self.store.reserve_request(
            publication.app_idempotency_key,
            self.authorization,
            endpoint=endpoint,
            request_sha256=request_sha,
            mutation_key=operation_key,
        )
        if record["status"] != "requested" or record.get("_reserved_new") is not True:
            return None, record
        try:
            response = self.transport.send(
                endpoint,
                parameters,
                body=body,
                token=self._token().value,
                timeout_seconds=float(self.timeout_seconds),
            )
        except GitHubSandboxPublicationError as exc:
            ambiguous = exc.code in {
                GitHubFailure.TIMEOUT.value,
                GitHubFailure.SERVER_5XX.value,
                GitHubFailure.RATE_LIMITED.value,
                GitHubFailure.OTHER.value,
            }
            self.store.finish_request(
                publication.app_idempotency_key,
                operation_key,
                status="ambiguous" if ambiguous else "failed",
                failure_code=exc.code,
                http_status=exc.status,
            )
            if not ambiguous:
                self.store.quarantine(publication.app_idempotency_key, exc.code)
            return None, self.store.request_record(publication.app_idempotency_key, operation_key)
        failure = classify_github_failure(response.status, response.headers)
        if failure is not None:
            reconcilable_conflict = endpoint in {"ref_create", "draft_pr_create"} and failure in {
                GitHubFailure.CONFLICT_409,
                GitHubFailure.VALIDATION_422,
            }
            ambiguous = reconcilable_conflict or failure in {
                GitHubFailure.SERVER_5XX,
                GitHubFailure.RATE_LIMITED,
                GitHubFailure.OTHER,
            }
            self.store.finish_request(
                publication.app_idempotency_key,
                operation_key,
                status="ambiguous" if ambiguous else "failed",
                failure_code=failure.value,
                http_status=response.status,
            )
            if not ambiguous:
                self.store.quarantine(publication.app_idempotency_key, failure.value)
            return None, self.store.request_record(publication.app_idempotency_key, operation_key)
        self.fault(f"after_{endpoint}_before_receipt")
        return response, record

    @staticmethod
    def _sha_body(response: GitHubResponse, expected_sha: str) -> bool:
        return isinstance(response.body, Mapping) and response.body.get("sha") == expected_sha

    def _observe_object(
        self,
        publication: GitHubCanaryPublication,
        *,
        create_endpoint: str,
        read_endpoint: str,
        expected_sha: str,
        create_body: Mapping[str, Any],
        operation_key: str,
    ) -> None:
        assert self.store is not None
        parameters = {"owner": publication.repository_owner, "repo": publication.repository_name}
        response, record = self._mutate(
            publication, create_endpoint, parameters, create_body, operation_key
        )
        if response is None:
            if record["status"] == "observed":
                return
            if record["status"] == "failed":
                raise GitHubSandboxPublicationError(str(record.get("failure_code") or "other"))
            try:
                response = self._read(
                    publication,
                    read_endpoint,
                    {**parameters, "sha": expected_sha},
                )
            except GitHubSandboxPublicationError:
                self.store.quarantine(
                    publication.app_idempotency_key, GitHubFailure.AMBIGUOUS_RESULT.value
                )
                raise GitHubSandboxPublicationError(GitHubFailure.AMBIGUOUS_RESULT) from None
        if not self._sha_body(response, expected_sha):
            self.store.finish_request(
                publication.app_idempotency_key,
                operation_key,
                status="failed",
                failure_code=GitHubFailure.RECEIPT_MISMATCH.value,
                http_status=response.status,
            )
            self.store.quarantine(
                publication.app_idempotency_key, GitHubFailure.RECEIPT_MISMATCH.value
            )
            raise GitHubSandboxPublicationError(GitHubFailure.RECEIPT_MISMATCH)
        self.store.finish_request(
            publication.app_idempotency_key,
            operation_key,
            status="observed",
            response_sha256=sha256_hex(canonical_json(response.body)),
            http_status=response.status,
        )

    def _preflight(self, publication: GitHubCanaryPublication) -> None:
        parameters = {"owner": publication.repository_owner, "repo": publication.repository_name}
        repository = self._read(publication, "repository_read", parameters)
        body = repository.body
        owner = body.get("owner") if isinstance(body, Mapping) else None
        if (
            not isinstance(body, Mapping)
            or body.get("id") != publication.repository_id
            or body.get("name") != publication.repository_name
            or not isinstance(owner, Mapping)
            or owner.get("login") != publication.repository_owner
        ):
            raise GitHubSandboxPublicationError(GitHubFailure.REPOSITORY_MISMATCH)
        default_branch = body.get("default_branch")
        if publication.head_branch == default_branch or publication.head_branch in self.protected_branches:
            raise GitHubSandboxPublicationError(GitHubFailure.BRANCH_PROTECTED)
        base = self._read(
            publication,
            "ref_read",
            {**parameters, "branch": publication.base_branch},
        )
        obj = base.body.get("object") if isinstance(base.body, Mapping) else None
        if not isinstance(obj, Mapping) or obj.get("sha") != publication.base_sha:
            raise GitHubSandboxPublicationError(GitHubFailure.BASE_DRIFT)
        commit = self._read(
            publication,
            "commit_read",
            {**parameters, "sha": publication.base_sha},
        )
        tree = commit.body.get("tree") if isinstance(commit.body, Mapping) else None
        if not isinstance(tree, Mapping) or tree.get("sha") != publication.base_tree_sha:
            raise GitHubSandboxPublicationError(GitHubFailure.BASE_DRIFT)

    def _branch(self, publication: GitHubCanaryPublication) -> None:
        assert self.store is not None
        parameters = {"owner": publication.repository_owner, "repo": publication.repository_name}
        for blob in publication.blobs:
            self._observe_object(
                publication,
                create_endpoint="blob_create",
                read_endpoint="blob_read",
                expected_sha=blob.blob_sha,
                create_body={
                    "content": base64.b64encode(blob.content).decode("ascii"),
                    "encoding": "base64",
                },
                operation_key=f"blob:{blob.blob_sha}",
            )
        self._observe_object(
            publication,
            create_endpoint="tree_create",
            read_endpoint="tree_read",
            expected_sha=publication.expected_tree_sha,
            create_body={
                "base_tree": publication.base_tree_sha,
                "tree": [
                    {"path": blob.path, "mode": blob.mode, "type": "blob", "sha": blob.blob_sha}
                    for blob in publication.blobs
                ],
            },
            operation_key=f"tree:{publication.expected_tree_sha}",
        )
        actor = {
            "name": "CRAG Sandbox Canary",
            "email": "crag-canary@invalid.example",
            "date": publication.commit_timestamp,
        }
        self._observe_object(
            publication,
            create_endpoint="commit_create",
            read_endpoint="commit_read",
            expected_sha=publication.exact_commit_sha,
            create_body={
                "message": publication.commit_message,
                "tree": publication.expected_tree_sha,
                "parents": [publication.base_sha],
                "author": actor,
                "committer": actor,
            },
            operation_key=f"commit:{publication.exact_commit_sha}",
        )
        operation_key = f"ref:{sha256_hex(publication.head_branch.encode('utf-8'))}"
        response, record = self._mutate(
            publication,
            "ref_create",
            parameters,
            {"ref": f"refs/heads/{publication.head_branch}", "sha": publication.exact_commit_sha},
            operation_key,
        )
        if response is None:
            if record["status"] == "observed":
                return
            if record["status"] == "failed":
                raise GitHubSandboxPublicationError(str(record.get("failure_code") or "other"))
            try:
                response = self._read(
                    publication,
                    "ref_read",
                    {**parameters, "branch": publication.head_branch},
                )
            except GitHubSandboxPublicationError:
                self.store.quarantine(
                    publication.app_idempotency_key, GitHubFailure.AMBIGUOUS_RESULT.value
                )
                raise GitHubSandboxPublicationError(GitHubFailure.AMBIGUOUS_RESULT) from None
        obj = response.body.get("object") if isinstance(response.body, Mapping) else None
        if not isinstance(obj, Mapping) or obj.get("sha") != publication.exact_commit_sha:
            self.store.quarantine(publication.app_idempotency_key, GitHubFailure.REF_COLLISION.value)
            raise GitHubSandboxPublicationError(GitHubFailure.REF_COLLISION)
        self.store.finish_request(
            publication.app_idempotency_key,
            operation_key,
            status="observed",
            response_sha256=sha256_hex(canonical_json(response.body)),
            http_status=response.status,
        )

    @staticmethod
    def _matching_pr(publication: GitHubCanaryPublication, body: Any) -> bool:
        if not isinstance(body, Mapping):
            return False
        head = body.get("head")
        base = body.get("base")
        return (
            body.get("draft") is True
            and isinstance(head, Mapping)
            and head.get("ref") == publication.head_branch
            and head.get("sha") == publication.exact_commit_sha
            and isinstance(base, Mapping)
            and base.get("ref") == publication.base_branch
            and sha256_hex(str(body.get("title", "")).encode("utf-8"))
            == publication.title_marker_sha256
            and sha256_hex(str(body.get("body", "")).encode("utf-8")) == publication.body_sha256
        )

    def _draft_pr(self, publication: GitHubCanaryPublication) -> str:
        assert self.store is not None
        parameters = {"owner": publication.repository_owner, "repo": publication.repository_name}
        operation_key = f"draft-pr:{publication.publisher_payload_sha256}"
        response, record = self._mutate(
            publication,
            "draft_pr_create",
            parameters,
            {
                "title": publication.title,
                "body": publication.body,
                "head": publication.head_branch,
                "base": publication.base_branch,
                "draft": True,
                "maintainer_can_modify": False,
            },
            operation_key,
        )
        if response is None:
            if record["status"] == "observed" and record.get("response_sha256"):
                return str(record["response_sha256"])
            if record["status"] == "failed":
                raise GitHubSandboxPublicationError(str(record.get("failure_code") or "other"))
            try:
                listed = self._read(
                    publication,
                    "draft_pr_list",
                    {
                        **parameters,
                        "head": publication.head_branch,
                        "base": publication.base_branch,
                    },
                )
            except GitHubSandboxPublicationError:
                self.store.quarantine(
                    publication.app_idempotency_key, GitHubFailure.AMBIGUOUS_RESULT.value
                )
                raise GitHubSandboxPublicationError(GitHubFailure.AMBIGUOUS_RESULT) from None
            if listed.headers.get("has-next-page") == "true":
                self.store.quarantine(
                    publication.app_idempotency_key, GitHubFailure.RECEIPT_MISMATCH.value
                )
                raise GitHubSandboxPublicationError(GitHubFailure.RECEIPT_MISMATCH)
            candidates = [item for item in listed.body if self._matching_pr(publication, item)] if isinstance(listed.body, list) else []
            if len(candidates) != 1:
                failure = (
                    GitHubFailure.RECEIPT_MISMATCH
                    if len(candidates) > 1
                    else GitHubFailure.AMBIGUOUS_RESULT
                )
                self.store.quarantine(publication.app_idempotency_key, failure.value)
                raise GitHubSandboxPublicationError(failure)
            response = GitHubResponse(status=200, headers={}, body=candidates[0])
        if not self._matching_pr(publication, response.body):
            self.store.quarantine(
                publication.app_idempotency_key, GitHubFailure.RECEIPT_MISMATCH.value
            )
            raise GitHubSandboxPublicationError(GitHubFailure.RECEIPT_MISMATCH)
        response_sha = sha256_hex(canonical_json(response.body))
        self.store.finish_request(
            publication.app_idempotency_key,
            operation_key,
            status="observed",
            response_sha256=response_sha,
            http_status=response.status,
        )
        return response_sha

    def _receipt(self, publication: GitHubCanaryPublication, draft_pr_sha256: str) -> GitHubCanaryReceipt:
        request_sha = publication.publisher_payload_sha256
        branch_sha = sha256_hex(publication.head_branch.encode("utf-8"))
        body = {
            "environment": CANARY_ENVIRONMENT,
            "synthetic_input_only": True,
            "real_github_sandbox_writes": self._real_github_writes,
            "real_model_calls": False,
            "real_business_repository_writes": False,
            "business_claim_allowed": False,
            "quality_claim_allowed": False,
            "production_ready": False,
            "request_sha256": request_sha,
            "branch_sha256": branch_sha,
            "commit_sha": publication.exact_commit_sha,
            "draft_pr_sha256": draft_pr_sha256,
        }
        return GitHubCanaryReceipt(
            receipt_sha256=sha256_hex(canonical_json(body)),
            request_sha256=request_sha,
            branch_sha256=branch_sha,
            commit_sha=publication.exact_commit_sha,
            draft_pr_sha256=draft_pr_sha256,
            real_github_sandbox_writes=self._real_github_writes,
        )

    def publish(self, request: Any) -> GitHubCanaryReceipt:
        if not isinstance(request, GitHubCanaryPublishRequest):
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
        if self.store is None:
            raise GitHubSandboxPublicationError(GitHubFailure.AUTHORIZATION_MISMATCH)
        publication = request.publication
        self._validate_authorization(publication)
        self.store.validate_approvals(request)
        try:
            row = self.store.record_intent(
                request,
                real_github_writes=self._real_github_writes,
            )
        except IntegrityError:
            raise GitHubSandboxPublicationError(GitHubFailure.CONFLICT_409) from None
        if row["state"] == "quarantined":
            raise GitHubSandboxPublicationError(str(row["failure_code"] or "other"))
        operation_key = f"draft-pr:{publication.publisher_payload_sha256}"
        if row["state"] == "receipt_reconciled":
            observed = self.store.request_record(publication.app_idempotency_key, operation_key)
            receipt = self._receipt(publication, str(observed["response_sha256"]))
            if receipt.receipt_sha256 != row["receipt_sha256"]:
                raise GitHubSandboxPublicationError(GitHubFailure.RECEIPT_MISMATCH)
            return receipt
        try:
            self._preflight(publication)
        except GitHubSandboxPublicationError as exc:
            self.store.quarantine(publication.app_idempotency_key, exc.code)
            raise
        row = self.store.publication(publication.app_idempotency_key)
        if row["state"] == "publish_intent_recorded":
            self.store.transition(
                publication.app_idempotency_key,
                "publish_intent_recorded",
                "branch_push_requested",
            )
        if self.store.publication(publication.app_idempotency_key)["state"] == "branch_push_requested":
            try:
                self._branch(publication)
            except GitHubSandboxPublicationError as exc:
                self.store.quarantine(publication.app_idempotency_key, exc.code)
                raise
            self.store.transition(
                publication.app_idempotency_key,
                "branch_push_requested",
                "branch_push_observed",
            )
        if self.store.publication(publication.app_idempotency_key)["state"] == "branch_push_observed":
            self.store.transition(
                publication.app_idempotency_key,
                "branch_push_observed",
                "draft_pr_requested",
            )
        if self.store.publication(publication.app_idempotency_key)["state"] == "draft_pr_requested":
            try:
                draft_pr_sha = self._draft_pr(publication)
            except GitHubSandboxPublicationError as exc:
                self.store.quarantine(publication.app_idempotency_key, exc.code)
                raise
            self.store.transition(
                publication.app_idempotency_key,
                "draft_pr_requested",
                "draft_pr_observed",
            )
        else:
            record = self.store.request_record(publication.app_idempotency_key, operation_key)
            draft_pr_sha = str(record["response_sha256"])
        receipt = self._receipt(publication, draft_pr_sha)
        self.store.reconcile_receipt(publication.app_idempotency_key, receipt)
        return receipt

    def lookup(self, idempotency_key: str) -> GitHubCanaryReceipt | None:
        if not self._enabled or self.store is None:
            return None
        try:
            row = self.store.publication(idempotency_key)
        except GitHubSandboxPublicationError:
            return None
        if row["state"] != "receipt_reconciled":
            return None
        try:
            return GitHubCanaryReceipt(
                receipt_sha256=str(row["receipt_sha256"]),
                request_sha256=str(row["payload_sha256"]),
                branch_sha256=str(row["branch_sha256"]),
                commit_sha=str(row["commit_sha"]),
                draft_pr_sha256=str(row["draft_pr_sha256"]),
                real_github_sandbox_writes=bool(row["real_github_writes"]),
            )
        except ValueError:
            raise GitHubSandboxPublicationError(GitHubFailure.RECEIPT_MISMATCH) from None


__all__ = [
    "AUTHORIZATION_SCHEMA_VERSION",
    "CANARY_CASE_IDS",
    "CANARY_ENVIRONMENT",
    "AuthorizationCase",
    "GitBlob",
    "GitHubSandboxApprovalConflict",
    "GitHubCanaryPublication",
    "GitHubCanaryPublishRequest",
    "GitHubCanaryReceipt",
    "GitHubCanaryStore",
    "GitHubDraftPrPublisher",
    "GitHubFailure",
    "GitHubResponse",
    "GitHubSandboxAuthorization",
    "GitHubSandboxPublicationError",
    "GitHubTransport",
    "InstallationToken",
    "StrictGitHubHttpsTransport",
    "canonical_json",
    "classify_github_failure",
    "sha256_hex",
]
