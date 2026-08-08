"""Replaceable authentication and organization-scoped RBAC primitives."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import json
import logging
from threading import RLock
import time
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

import jwt


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
    START_REPAIR = "start_repair"
    DECIDE_REPAIR = "decide_repair"
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
            Permission.START_REPAIR,
            Permission.DECIDE_REPAIR,
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
            Permission.START_REPAIR,
            Permission.DECIDE_REPAIR,
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


class OIDCPrincipalResolver(Protocol):
    def principal_for_subject(
        self, organization_id: str, subject: str, *, auth_method: str
    ) -> Principal | None: ...


JWKSetFetcher = Callable[[str, float], Mapping[str, object]]
OIDCFailureRecorder = Callable[[str], None]

_LOGGER = logging.getLogger(__name__)
_OIDC_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"})
_OIDC_REASONS = frozenset(
    {
        "algorithm",
        "audience",
        "claims",
        "expired",
        "issuer",
        "jwks_invalid",
        "jwks_unavailable",
        "malformed_authorization",
        "malformed_token",
        "not_yet_valid",
        "signature",
        "temporal",
        "unknown_kid",
        "unmapped_subject",
        "verification",
    }
)
_MAX_JWKS_BYTES = 256 * 1024


class _OIDCValidationError(RuntimeError):
    """Internal, bounded OIDC failure reason that is safe to record."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason if reason in _OIDC_REASONS else "verification"


def _https_url(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError(f"{field} must be a bounded HTTPS URL")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{field} must be a bounded HTTPS URL")
    return value


def _claim_name(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("OIDC organization claim must be a bounded string")
    if not value[0].isalpha() or any(not (char.isalnum() or char in "_.-") for char in value):
        raise ValueError("OIDC organization claim contains unsupported characters")
    return value


@dataclass(frozen=True)
class OIDCConfiguration:
    """Static operator configuration for one OIDC issuer and JWKS endpoint."""

    issuer: str
    audience: str
    jwks_url: str
    organization_claim: str = "organization_id"
    jwks_cache_seconds: float = 300.0
    jwks_timeout_seconds: float = 5.0
    leeway_seconds: int = 30
    algorithms: tuple[str, ...] = ("RS256",)

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer", _https_url(self.issuer, "OIDC issuer"))
        object.__setattr__(self, "jwks_url", _https_url(self.jwks_url, "OIDC JWKS URL"))
        if not isinstance(self.audience, str) or not self.audience or len(self.audience) > 256:
            raise ValueError("OIDC audience must be a bounded non-empty string")
        object.__setattr__(self, "organization_claim", _claim_name(self.organization_claim))
        if not isinstance(self.jwks_cache_seconds, (int, float)) or isinstance(
            self.jwks_cache_seconds, bool
        ):
            raise ValueError("OIDC JWKS cache seconds must be numeric")
        if not 30 <= float(self.jwks_cache_seconds) <= 3600:
            raise ValueError("OIDC JWKS cache seconds must be between 30 and 3600")
        if not isinstance(self.jwks_timeout_seconds, (int, float)) or isinstance(
            self.jwks_timeout_seconds, bool
        ):
            raise ValueError("OIDC JWKS timeout seconds must be numeric")
        if not 1 <= float(self.jwks_timeout_seconds) <= 30:
            raise ValueError("OIDC JWKS timeout seconds must be between 1 and 30")
        if (
            isinstance(self.leeway_seconds, bool)
            or not isinstance(self.leeway_seconds, int)
            or not 0 <= self.leeway_seconds <= 120
        ):
            raise ValueError("OIDC leeway seconds must be an integer between 0 and 120")
        algorithms = tuple(self.algorithms)
        if not algorithms or len(algorithms) > len(_OIDC_ALGORITHMS):
            raise ValueError("OIDC algorithms must be a non-empty bounded sequence")
        if len(set(algorithms)) != len(algorithms) or any(item not in _OIDC_ALGORITHMS for item in algorithms):
            raise ValueError("OIDC algorithms must be approved asymmetric algorithms")
        object.__setattr__(self, "algorithms", algorithms)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "OIDCConfiguration":
        required = {
            "issuer": environment.get("CRAG_OIDC_ISSUER", "").strip(),
            "audience": environment.get("CRAG_OIDC_AUDIENCE", "").strip(),
            "jwks_url": environment.get("CRAG_OIDC_JWKS_URL", "").strip(),
        }
        if not all(required.values()):
            raise ValueError(
                "OIDC mode requires CRAG_OIDC_ISSUER, CRAG_OIDC_AUDIENCE, and CRAG_OIDC_JWKS_URL"
            )
        raw_algorithms = environment.get("CRAG_OIDC_ALGORITHMS", "RS256")
        algorithms = tuple(item.strip() for item in raw_algorithms.split(",") if item.strip())
        try:
            cache_seconds = float(environment.get("CRAG_OIDC_JWKS_CACHE_SECONDS", "300"))
            timeout_seconds = float(environment.get("CRAG_OIDC_JWKS_TIMEOUT_SECONDS", "5"))
            leeway_seconds = int(environment.get("CRAG_OIDC_LEEWAY_SECONDS", "30"))
        except ValueError as exc:
            raise ValueError("OIDC timing settings must be numeric") from exc
        return cls(
            **required,
            organization_claim=environment.get("CRAG_OIDC_ORGANIZATION_CLAIM", "organization_id"),
            jwks_cache_seconds=cache_seconds,
            jwks_timeout_seconds=timeout_seconds,
            leeway_seconds=leeway_seconds,
            algorithms=algorithms,
        )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


def fetch_jwk_set(url: str, timeout_seconds: float) -> Mapping[str, object]:
    """Fetch one bounded JWKS document from the operator-configured HTTPS endpoint."""

    request = Request(url, headers={"Accept": "application/json"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout_seconds) as response:
            payload = response.read(_MAX_JWKS_BYTES + 1)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise _OIDCValidationError("jwks_unavailable") from exc
    if len(payload) > _MAX_JWKS_BYTES:
        raise _OIDCValidationError("jwks_invalid")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _OIDCValidationError("jwks_invalid") from exc
    if not isinstance(decoded, dict):
        raise _OIDCValidationError("jwks_invalid")
    return decoded


class JWKSetCache:
    """Thread-safe cache that permits one rotation refresh per cached key set."""

    def __init__(
        self,
        configuration: OIDCConfiguration,
        *,
        fetcher: JWKSetFetcher | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._configuration = configuration
        self._fetcher = fetcher or fetch_jwk_set
        self._clock = clock or time.monotonic
        self._keys: dict[str, jwt.PyJWK] | None = None
        self._expires_at = 0.0
        self._generation = 0
        self._miss_refresh_generation: int | None = None
        self._lock = RLock()

    def signing_key(self, kid: str, algorithm: str) -> jwt.PyJWK:
        with self._lock:
            keys, refreshed = self._current_keys()
            key = keys.get(kid)
            if key is not None:
                if key.algorithm_name != algorithm:
                    raise _OIDCValidationError("algorithm")
                return key
            if not refreshed and self._miss_refresh_generation != self._generation:
                self._miss_refresh_generation = self._generation
                keys = self._refresh()
                self._miss_refresh_generation = self._generation
                key = keys.get(kid)
                if key is not None:
                    if key.algorithm_name != algorithm:
                        raise _OIDCValidationError("algorithm")
                    return key
            raise _OIDCValidationError("unknown_kid")

    def _current_keys(self) -> tuple[dict[str, jwt.PyJWK], bool]:
        if self._keys is None or self._clock() >= self._expires_at:
            return self._refresh(), True
        return self._keys, False

    def _refresh(self) -> dict[str, jwt.PyJWK]:
        try:
            payload = self._fetcher(
                self._configuration.jwks_url, float(self._configuration.jwks_timeout_seconds)
            )
            if not isinstance(payload, Mapping):
                raise _OIDCValidationError("jwks_invalid")
            jwks = jwt.PyJWKSet.from_dict(dict(payload))
        except _OIDCValidationError:
            raise
        except (TypeError, ValueError, jwt.PyJWTError) as exc:
            raise _OIDCValidationError("jwks_invalid") from exc
        keys: dict[str, jwt.PyJWK] = {}
        for key in jwks.keys:
            if (
                not isinstance(key.key_id, str)
                or not key.key_id
                or len(key.key_id) > 256
                or key.public_key_use not in {None, "sig"}
                or key.algorithm_name not in self._configuration.algorithms
            ):
                continue
            if key.key_id in keys:
                raise _OIDCValidationError("jwks_invalid")
            keys[key.key_id] = key
        if not keys:
            raise _OIDCValidationError("jwks_invalid")
        self._keys = keys
        self._expires_at = self._clock() + float(self._configuration.jwks_cache_seconds)
        self._generation += 1
        self._miss_refresh_generation = None
        return keys


class OIDCJWTVerifier:
    """Verify configured OIDC JWTs without trusting token-controlled JWK URLs."""

    def __init__(
        self,
        configuration: OIDCConfiguration,
        *,
        fetcher: JWKSetFetcher | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._configuration = configuration
        self._cache = JWKSetCache(configuration, fetcher=fetcher, clock=clock)

    def verify(self, token: str) -> Mapping[str, object]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise _OIDCValidationError("malformed_token") from exc
        algorithm = header.get("alg")
        kid = header.get("kid")
        if not isinstance(algorithm, str) or algorithm not in self._configuration.algorithms:
            raise _OIDCValidationError("algorithm")
        if not isinstance(kid, str) or not kid or len(kid) > 256:
            raise _OIDCValidationError("malformed_token")
        key = self._cache.signing_key(kid, algorithm)
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=list(self._configuration.algorithms),
                audience=self._configuration.audience,
                issuer=self._configuration.issuer,
                leeway=self._configuration.leeway_seconds,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise _OIDCValidationError("expired") from exc
        except jwt.InvalidIssuerError as exc:
            raise _OIDCValidationError("issuer") from exc
        except jwt.InvalidAudienceError as exc:
            raise _OIDCValidationError("audience") from exc
        except jwt.ImmatureSignatureError as exc:
            raise _OIDCValidationError("not_yet_valid") from exc
        except jwt.InvalidSignatureError as exc:
            raise _OIDCValidationError("signature") from exc
        except jwt.PyJWTError as exc:
            raise _OIDCValidationError("verification") from exc
        if not isinstance(claims, Mapping):
            raise _OIDCValidationError("claims")
        subject = claims.get("sub")
        organization_id = claims.get(self._configuration.organization_claim)
        if (
            not isinstance(subject, str)
            or not subject
            or len(subject) > 256
            or not isinstance(organization_id, str)
            or not organization_id
            or len(organization_id) > 128
        ):
            raise _OIDCValidationError("claims")
        return claims


class OIDCJWTAuthBackend:
    """Map verified configured OIDC tokens to existing organization memberships."""

    def __init__(
        self,
        configuration: OIDCConfiguration,
        resolver: OIDCPrincipalResolver,
        *,
        fetcher: JWKSetFetcher | None = None,
        clock: Callable[[], float] | None = None,
        failure_recorder: OIDCFailureRecorder | None = None,
    ) -> None:
        self._configuration = configuration
        self._resolver = resolver
        self._verifier = OIDCJWTVerifier(configuration, fetcher=fetcher, clock=clock)
        self._failure_recorder = failure_recorder or self._log_failure

    @staticmethod
    def _log_failure(reason: str) -> None:
        _LOGGER.warning("oidc_authentication_failed reason=%s", reason)

    def _record_failure(self, reason: str) -> None:
        try:
            self._failure_recorder(reason if reason in _OIDC_REASONS else "verification")
        except Exception:
            # Telemetry must not turn an authentication denial into a service failure.
            pass

    def authenticate(self, authorization: str | None) -> Principal:
        try:
            token = _bearer_token(authorization)
        except AuthenticationRequired:
            self._record_failure("malformed_authorization")
            raise
        try:
            claims = self._verifier.verify(token)
            principal = self._resolver.principal_for_subject(
                str(claims[self._configuration.organization_claim]),
                str(claims["sub"]),
                auth_method="oidc",
            )
        except _OIDCValidationError as exc:
            self._record_failure(exc.reason)
            raise AuthenticationRequired("authentication required") from None
        if principal is None:
            self._record_failure("unmapped_subject")
            raise AuthenticationRequired("authentication required")
        return principal


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
