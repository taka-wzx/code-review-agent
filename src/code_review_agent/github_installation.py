"""Fail-closed GitHub App installation lifecycle validation."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
from typing import Callable, Protocol

from code_review_agent.identity import Principal


SCHEMA_VERSION = "crag.github-installation-lifecycle/v1alpha1"
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_MAX_AGE = timedelta(hours=1)

_REASONS = frozenset(
    {
        "active",
        "api_error",
        "api_unavailable",
        "app_id_mismatch",
        "audit_unavailable",
        "credential_identity_mismatch",
        "installation_account_mismatch",
        "installation_deleted",
        "installation_id_mismatch",
        "installation_suspended",
        "repository_deleted",
        "repository_id_mismatch",
        "repository_name_mismatch",
        "response_invalid",
        "token_expired",
        "token_invalid",
        "token_revoked",
    }
)


class GitHubInstallationValidationError(RuntimeError):
    """Stable validation failure without provider response details."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _REASONS else "api_error"
        super().__init__(self.code)


class GitHubInstallationApiError(RuntimeError):
    """Fake/real client boundary error carrying only an HTTP status."""

    def __init__(self, status: int) -> None:
        self.status = status if isinstance(status, int) and 100 <= status <= 599 else 500
        super().__init__("github_api_error")


class LifecycleState(str):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"
    MISMATCH = "mismatch"
    ERROR = "error"


class LifecycleDecision(str):
    ALLOW = "allow"
    DENY = "deny"
    ERROR = "error"


def _positive_id(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bounded_id(name: str, value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class GitHubRepositoryRegistration:
    """Immutable external identity bound when a repository is registered."""

    organization_id: str
    registered_repository_id: str
    owner: str
    name: str
    github_repository_id: int
    github_app_id: int
    installation_id: int
    installation_account_id: int

    def __post_init__(self) -> None:
        _bounded_id("organization_id", self.organization_id)
        _bounded_id("registered_repository_id", self.registered_repository_id)
        if _OWNER.fullmatch(self.owner) is None:
            raise ValueError("repository owner is invalid")
        if _NAME.fullmatch(self.name) is None:
            raise ValueError("repository name is invalid")
        for field_name in (
            "github_repository_id",
            "github_app_id",
            "installation_id",
            "installation_account_id",
        ):
            _positive_id(field_name, getattr(self, field_name))

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def identity_sha256(self) -> str:
        return _sha256(
            _canonical_json(
                {"name": self.name.casefold(), "owner": self.owner.casefold()}
            )
        )


@dataclass(frozen=True)
class InstallationCredential:
    """Short-lived token kept in memory; repr intentionally omits its value."""

    value: str = field(repr=False)
    app_id: int
    installation_id: int
    installation_account_id: int
    expires_at: datetime
    revoked: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, str)
            or not 20 <= len(self.value.encode("utf-8")) <= 4096
            or any(character.isspace() for character in self.value)
        ):
            raise ValueError("installation token is invalid")
        for field_name in ("app_id", "installation_id", "installation_account_id"):
            _positive_id(field_name, getattr(self, field_name))
        _utc(self.expires_at)
        if not isinstance(self.revoked, bool):
            raise ValueError("revoked must be boolean")


class GitHubInstallationClient(Protocol):
    def get_installation(self, installation_id: int, token: str) -> Mapping[str, object]: ...

    def get_repository(
        self, owner: str, name: str, token: str
    ) -> Mapping[str, object]: ...


class LifecycleAuditSink(Protocol):
    def record(self, receipt: "GitHubLifecycleReceipt") -> None: ...


class AuditDatabase(Protocol):
    def audit(
        self,
        *,
        principal: Principal,
        action: str,
        resource_type: str,
        resource_id: str,
        decision: str,
        correlation_id: str,
        policy_version: str = "rbac/v1",
        repository_id: str | None = None,
        reason_code: str | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class GitHubLifecycleReceipt:
    schema_version: str
    receipt_id: str
    organization_id: str
    registered_repository_id: str
    github_repository_id: int
    github_app_id: int
    installation_id: int
    installation_account_id: int
    repository_identity_sha256: str
    state: str
    decision: str
    reason: str
    observed_at_utc: str
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("receipt schema is invalid")
        _bounded_id("receipt_id", self.receipt_id)
        _bounded_id("organization_id", self.organization_id)
        _bounded_id("registered_repository_id", self.registered_repository_id)
        for field_name in (
            "github_repository_id",
            "github_app_id",
            "installation_id",
            "installation_account_id",
        ):
            _positive_id(field_name, getattr(self, field_name))
        if _SHA256.fullmatch(self.repository_identity_sha256) is None:
            raise ValueError("repository identity digest is invalid")
        if self.state not in {
            LifecycleState.ACTIVE,
            LifecycleState.SUSPENDED,
            LifecycleState.DELETED,
            LifecycleState.MISMATCH,
            LifecycleState.ERROR,
        }:
            raise ValueError("lifecycle state is invalid")
        if self.decision not in {
            LifecycleDecision.ALLOW,
            LifecycleDecision.DENY,
            LifecycleDecision.ERROR,
        }:
            raise ValueError("lifecycle decision is invalid")
        if self.reason not in _REASONS:
            raise ValueError("lifecycle reason is invalid")
        datetime.fromisoformat(self.observed_at_utc.replace("Z", "+00:00"))
        object.__setattr__(self, "receipt_sha256", _sha256(_canonical_json(self.to_dict(False))))

    def to_dict(self, include_hash: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "organization_id": self.organization_id,
            "registered_repository_id": self.registered_repository_id,
            "github_repository_id": self.github_repository_id,
            "github_app_id": self.github_app_id,
            "installation_id": self.installation_id,
            "installation_account_id": self.installation_account_id,
            "repository_identity_sha256": self.repository_identity_sha256,
            "state": self.state,
            "decision": self.decision,
            "reason": self.reason,
            "observed_at_utc": self.observed_at_utc,
        }
        if include_hash:
            value["receipt_sha256"] = self.receipt_sha256
        return value


@dataclass(frozen=True)
class GitHubLifecycleValidation:
    accepted: bool
    receipt: GitHubLifecycleReceipt


class DatabaseLifecycleAuditSink:
    """Persist only the stable audit projection of a lifecycle receipt."""

    def __init__(
        self,
        database: AuditDatabase,
        principal: Principal,
        *,
        policy_version: str = "rbac/v1",
    ) -> None:
        self._database = database
        self._principal = principal
        self._policy_version = policy_version

    def record(self, receipt: GitHubLifecycleReceipt) -> None:
        self._database.audit(
            principal=self._principal,
            action="github.installation.lifecycle",
            resource_type="github_installation",
            resource_id=receipt.registered_repository_id,
            decision=receipt.decision,
            correlation_id=receipt.receipt_id,
            policy_version=self._policy_version,
            repository_id=receipt.registered_repository_id,
            reason_code=receipt.reason,
        )


class _LifecycleFailure(Exception):
    def __init__(self, state: str, reason: str) -> None:
        self.state = state
        self.reason = reason if reason in _REASONS else "api_error"
        super().__init__(self.reason)


class GitHubInstallationValidator:
    """Validate one registration and emit one receipt per attempt."""

    def __init__(
        self,
        client: GitHubInstallationClient,
        *,
        audit_sink: LifecycleAuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
        receipt_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._client = client
        self._audit_sink = audit_sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._receipt_id_factory = receipt_id_factory or (lambda: secrets.token_hex(16))

    def validate(
        self,
        registration: GitHubRepositoryRegistration,
        credential: InstallationCredential,
    ) -> GitHubLifecycleValidation:
        observed_at = _utc(self._clock())
        state = LifecycleState.ERROR
        decision = LifecycleDecision.DENY
        reason = "api_error"
        api_stage = "installation"
        try:
            self._validate_credential(registration, credential, observed_at)
            installation = self._client.get_installation(
                registration.installation_id, credential.value
            )
            self._validate_installation(registration, installation)
            api_stage = "repository"
            repository = self._client.get_repository(
                registration.owner, registration.name, credential.value
            )
            self._validate_repository(registration, repository)
            state = LifecycleState.ACTIVE
            decision = LifecycleDecision.ALLOW
            reason = "active"
        except _LifecycleFailure as failure:
            state = failure.state
            reason = failure.reason
        except GitHubInstallationApiError as failure:
            state, reason = self._api_failure(failure.status, api_stage)
        except (OSError, TimeoutError):
            state, reason = LifecycleState.ERROR, "api_unavailable"
        except Exception:
            state, reason = LifecycleState.ERROR, "response_invalid"

        receipt = GitHubLifecycleReceipt(
            schema_version=SCHEMA_VERSION,
            receipt_id=self._receipt_id_factory(),
            organization_id=registration.organization_id,
            registered_repository_id=registration.registered_repository_id,
            github_repository_id=registration.github_repository_id,
            github_app_id=registration.github_app_id,
            installation_id=registration.installation_id,
            installation_account_id=registration.installation_account_id,
            repository_identity_sha256=registration.identity_sha256,
            state=state,
            decision=decision,
            reason=reason,
            observed_at_utc=_utc_text(observed_at),
        )
        if self._audit_sink is not None:
            try:
                self._audit_sink.record(receipt)
            except Exception:
                raise GitHubInstallationValidationError("audit_unavailable") from None
        return GitHubLifecycleValidation(decision == LifecycleDecision.ALLOW, receipt)

    def require_valid(
        self,
        registration: GitHubRepositoryRegistration,
        credential: InstallationCredential,
    ) -> GitHubLifecycleReceipt:
        result = self.validate(registration, credential)
        if not result.accepted:
            raise GitHubInstallationValidationError(result.receipt.reason)
        return result.receipt

    @staticmethod
    def _validate_credential(
        registration: GitHubRepositoryRegistration,
        credential: InstallationCredential,
        now: datetime,
    ) -> None:
        if credential.revoked:
            raise _LifecycleFailure(LifecycleState.ERROR, "token_revoked")
        if credential.expires_at <= now:
            raise _LifecycleFailure(LifecycleState.ERROR, "token_expired")
        if credential.expires_at > now + _TOKEN_MAX_AGE:
            raise _LifecycleFailure(LifecycleState.ERROR, "token_invalid")
        if (
            credential.app_id != registration.github_app_id
            or credential.installation_id != registration.installation_id
            or credential.installation_account_id != registration.installation_account_id
        ):
            raise _LifecycleFailure(LifecycleState.MISMATCH, "credential_identity_mismatch")

    @staticmethod
    def _validate_installation(
        registration: GitHubRepositoryRegistration, payload: Mapping[str, object]
    ) -> None:
        if not isinstance(payload, Mapping):
            raise _LifecycleFailure(LifecycleState.ERROR, "response_invalid")
        if payload.get("deleted") is True:
            raise _LifecycleFailure(LifecycleState.DELETED, "installation_deleted")
        suspended = payload.get("suspended_at")
        if payload.get("suspended") is True or (isinstance(suspended, str) and suspended):
            raise _LifecycleFailure(LifecycleState.SUSPENDED, "installation_suspended")
        if payload.get("suspended") not in {None, False}:
            raise _LifecycleFailure(LifecycleState.ERROR, "response_invalid")
        if _positive_id("installation.id", payload.get("id")) != registration.installation_id:
            raise _LifecycleFailure(LifecycleState.MISMATCH, "installation_id_mismatch")
        if _positive_id("installation.app_id", payload.get("app_id")) != registration.github_app_id:
            raise _LifecycleFailure(LifecycleState.MISMATCH, "app_id_mismatch")
        account = payload.get("account")
        if not isinstance(account, Mapping):
            raise _LifecycleFailure(LifecycleState.ERROR, "response_invalid")
        if _positive_id("installation.account.id", account.get("id")) != registration.installation_account_id:
            raise _LifecycleFailure(
                LifecycleState.MISMATCH, "installation_account_mismatch"
            )

    @staticmethod
    def _validate_repository(
        registration: GitHubRepositoryRegistration, payload: Mapping[str, object]
    ) -> None:
        if not isinstance(payload, Mapping):
            raise _LifecycleFailure(LifecycleState.ERROR, "response_invalid")
        if payload.get("deleted") is True:
            raise _LifecycleFailure(LifecycleState.DELETED, "repository_deleted")
        if _positive_id("repository.id", payload.get("id")) != registration.github_repository_id:
            raise _LifecycleFailure(LifecycleState.MISMATCH, "repository_id_mismatch")
        full_name = payload.get("full_name")
        owner = payload.get("owner")
        if not isinstance(full_name, str) or not isinstance(owner, Mapping):
            raise _LifecycleFailure(LifecycleState.ERROR, "response_invalid")
        if full_name.casefold() != registration.full_name.casefold():
            raise _LifecycleFailure(LifecycleState.MISMATCH, "repository_name_mismatch")
        observed_owner = owner.get("login")
        if not isinstance(observed_owner, str) or observed_owner.casefold() != registration.owner.casefold():
            raise _LifecycleFailure(LifecycleState.MISMATCH, "repository_name_mismatch")

    @staticmethod
    def _api_failure(status: int, stage: str) -> tuple[str, str]:
        if status == 404:
            return (
                LifecycleState.DELETED,
                "installation_deleted" if stage == "installation" else "repository_deleted",
            )
        if status in {401, 403}:
            return LifecycleState.ERROR, "token_invalid"
        if status == 429 or status >= 500:
            return LifecycleState.ERROR, "api_unavailable"
        return LifecycleState.ERROR, "api_error"
