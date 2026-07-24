"""Replaceable authentication and organization-scoped RBAC primitives."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
from typing import Callable, Mapping, Protocol


class IdentityError(RuntimeError):
    """Base identity failure with no credential-bearing detail."""


class AuthenticationRequired(IdentityError):
    """The request did not carry a valid, active credential."""


class PermissionDenied(IdentityError):
    """The authenticated principal is not allowed to perform an operation."""


class Role(str, Enum):
    ORG_ADMIN = "org_admin"
    MAINTAINER = "maintainer"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class Permission(str, Enum):
    READ = "read"
    SUBMIT_REVIEW = "submit_review"
    SUBMIT_FEEDBACK = "submit_feedback"
    DECIDE_FINDING = "decide_finding"
    APPROVE_PUBLICATION = "approve_publication"
    MANAGE_MEMBERS = "manage_members"
    MANAGE_REPOSITORIES = "manage_repositories"
    MANAGE_CREDENTIALS = "manage_credentials"
    MANAGE_POLICY = "manage_policy"
    READ_AUDIT = "read_audit"


_ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.READ}),
    Role.REVIEWER: frozenset(
        {Permission.READ, Permission.SUBMIT_REVIEW, Permission.SUBMIT_FEEDBACK}
    ),
    Role.MAINTAINER: frozenset(
        {
            Permission.READ,
            Permission.SUBMIT_REVIEW,
            Permission.SUBMIT_FEEDBACK,
            Permission.DECIDE_FINDING,
            Permission.APPROVE_PUBLICATION,
        }
    ),
    Role.ORG_ADMIN: frozenset(
        {
            Permission.READ,
            Permission.MANAGE_MEMBERS,
            Permission.MANAGE_REPOSITORIES,
            Permission.MANAGE_CREDENTIALS,
            Permission.MANAGE_POLICY,
            Permission.READ_AUDIT,
            Permission.APPROVE_PUBLICATION,
        }
    ),
}


@dataclass(frozen=True)
class Principal:
    principal_id: str
    user_id: str
    organization_id: str
    role: Role
    auth_method: str
    credential_id: str | None = None

    def allows(self, permission: Permission) -> bool:
        return permission in _ROLE_PERMISSIONS[self.role]

    def require(self, permission: Permission) -> None:
        if not self.allows(permission):
            raise PermissionDenied("operation is not permitted")


class AuthBackend(Protocol):
    def authenticate(self, authorization: str | None) -> Principal: ...


class CredentialResolver(Protocol):
    def authenticate_token(self, token: str) -> Principal | None: ...


def token_digest(token: str) -> str:
    """Return the irreversible lookup digest for a high-entropy API token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bearer_token(authorization: str | None) -> str:
    if not isinstance(authorization, str):
        raise AuthenticationRequired("authentication required")
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.casefold() != "bearer" or not token:
        raise AuthenticationRequired("authentication required")
    return token


class DatabaseAuthBackend:
    """Authenticate high-entropy bearer credentials against current DB state."""

    def __init__(self, resolver: CredentialResolver) -> None:
        self._resolver = resolver

    def authenticate(self, authorization: str | None) -> Principal:
        token = _bearer_token(authorization)
        principal = self._resolver.authenticate_token(token)
        if principal is None:
            raise AuthenticationRequired("authentication required")
        return principal


class LocalTokenAuthBackend:
    """Explicit in-memory compatibility backend for loopback development only."""

    def __init__(self, token: str, principal: Principal) -> None:
        if len(token.encode("utf-8")) < 32:
            raise ValueError("local development token must be at least 32 UTF-8 bytes")
        self._digest = token_digest(token)
        self._principal = principal

    def authenticate(self, authorization: str | None) -> Principal:
        token = _bearer_token(authorization)
        if not hmac.compare_digest(token_digest(token), self._digest):
            raise AuthenticationRequired("authentication required")
        return self._principal


class FakeAuthBackend:
    """Deterministic authentication backend for offline tests."""

    def __init__(self, principals: Mapping[str, Principal]) -> None:
        if not principals:
            raise ValueError("at least one fake credential is required")
        self._principals = dict(principals)

    def authenticate(self, authorization: str | None) -> Principal:
        token = _bearer_token(authorization)
        principal = self._principals.get(token)
        if principal is None:
            raise AuthenticationRequired("authentication required")
        return principal


class VerifiedOIDCJWTAuthBackend:
    """Adapt deployment-verified OIDC/JWT claims to the service principal.

    The injected verifier owns signature, issuer, audience, expiry, and key
    rotation checks. This class performs no discovery or network access and
    never persists the bearer value or claims.
    """

    def __init__(
        self,
        verifier: Callable[[str], Mapping[str, object]],
        mapper: Callable[[Mapping[str, object]], Principal | None],
    ) -> None:
        self._verifier = verifier
        self._mapper = mapper

    def authenticate(self, authorization: str | None) -> Principal:
        token = _bearer_token(authorization)
        try:
            claims = self._verifier(token)
            if not all(name in claims for name in ("iss", "sub", "aud", "exp")):
                raise AuthenticationRequired("authentication required")
            principal = self._mapper(claims)
        except AuthenticationRequired:
            raise
        except Exception:
            raise AuthenticationRequired("authentication required") from None
        if principal is None:
            raise AuthenticationRequired("authentication required")
        return principal


_CURRENT_PRINCIPAL: ContextVar[Principal | None] = ContextVar(
    "crag_current_principal", default=None
)
_CORRELATION_ID: ContextVar[str | None] = ContextVar("crag_correlation_id", default=None)


def bind_principal(principal: Principal) -> Token[Principal | None]:
    return _CURRENT_PRINCIPAL.set(principal)


def reset_principal(token: Token[Principal | None]) -> None:
    _CURRENT_PRINCIPAL.reset(token)


def current_principal() -> Principal:
    principal = _CURRENT_PRINCIPAL.get()
    if principal is None:
        raise AuthenticationRequired("authentication required")
    return principal


def bind_correlation_id(correlation_id: str) -> Token[str | None]:
    return _CORRELATION_ID.set(correlation_id)


def reset_correlation_id(token: Token[str | None]) -> None:
    _CORRELATION_ID.reset(token)


def current_correlation_id(default: str) -> str:
    return _CORRELATION_ID.get() or default
