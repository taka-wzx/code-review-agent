from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from code_review_agent.identity import (
    AuthenticationRequired,
    OIDCConfiguration,
    OIDCJWTAuthBackend,
    Role,
)
from code_review_agent.service import HttpSettings, create_app
from code_review_agent.service_core import InvalidRequest, JobStore, RepositoryRegistry, ReviewService


ISSUER = "https://issuer.example/tenant"
AUDIENCE = "code-review-agent"
JWKS_URL = "https://issuer.example/tenant/keys"


def _base64url(value: int) -> str:
    length = max(1, (value.bit_length() + 7) // 8)
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")


def _jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    public = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _base64url(public.n),
        "e": _base64url(public.e),
    }


class MutableJWKFetcher:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout_seconds: float) -> dict[str, object]:
        self.calls.append((url, timeout_seconds))
        return self.payload


class Issue25OIDCJWKTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_one = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_two = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.fetcher = MutableJWKFetcher({"keys": [_jwk(self.private_one, "one")]})
        self.clock = [100.0]
        self.failures: list[str] = []
        self.subjects = {("org-1", "reviewer-1"): "principal-1"}
        self.configuration = OIDCConfiguration(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_url=JWKS_URL,
            jwks_cache_seconds=60,
        )

        class Resolver:
            def __init__(self, subjects: dict[tuple[str, str], str]) -> None:
                self.subjects = subjects

            def principal_for_subject(self, organization_id: str, subject: str, *, auth_method: str):
                from code_review_agent.identity import Principal, Role

                principal_id = self.subjects.get((organization_id, subject))
                if principal_id is None:
                    return None
                return Principal(
                    principal_id=principal_id,
                    user_id=principal_id,
                    organization_id=organization_id,
                    role=Role.REVIEWER,
                    auth_method=auth_method,
                )

        self.backend = OIDCJWTAuthBackend(
            self.configuration,
            Resolver(self.subjects),
            fetcher=self.fetcher,
            clock=lambda: self.clock[0],
            failure_recorder=self.failures.append,
        )

    def token(
        self,
        private_key: rsa.RSAPrivateKey,
        kid: str,
        **overrides: object,
    ) -> str:
        now = datetime.now(timezone.utc)
        claims: dict[str, object] = {
            "iss": ISSUER,
            "sub": "reviewer-1",
            "aud": AUDIENCE,
            "organization_id": "org-1",
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5),
        }
        claims.update(overrides)
        return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})

    def authenticate(self, token: str):
        return self.backend.authenticate(f"Bearer {token}")

    def test_cache_reuse_rotated_kid_refresh_and_expiry(self) -> None:
        first = self.authenticate(self.token(self.private_one, "one"))
        self.assertEqual(first.principal_id, "principal-1")
        self.authenticate(self.token(self.private_one, "one"))
        self.assertEqual(len(self.fetcher.calls), 1)

        self.fetcher.payload = {"keys": [_jwk(self.private_two, "two")]}
        rotated = self.authenticate(self.token(self.private_two, "two"))
        self.assertEqual(rotated.auth_method, "oidc")
        self.assertEqual(len(self.fetcher.calls), 2)

        self.clock[0] += 61
        self.authenticate(self.token(self.private_two, "two"))
        self.assertEqual(len(self.fetcher.calls), 3)

    def test_invalid_tokens_are_rejected_with_bounded_failure_codes(self) -> None:
        invalid_cases = (
            (
                self.token(
                    self.private_one,
                    "one",
                    exp=datetime.now(timezone.utc) - timedelta(minutes=2),
                ),
                "expired",
            ),
            (self.token(self.private_one, "one", aud="different"), "audience"),
            (self.token(self.private_one, "one", iss="https://issuer.example/wrong"), "issuer"),
            (self.token(self.private_one, "one", nbf=datetime.now(timezone.utc) + timedelta(minutes=2)), "not_yet_valid"),
        )
        for token, reason in invalid_cases:
            with self.subTest(reason=reason), self.assertRaises(AuthenticationRequired):
                self.authenticate(token)
            self.assertEqual(self.failures[-1], reason)

        with self.assertRaises(AuthenticationRequired):
            self.authenticate(self.token(self.private_two, "one"))
        self.assertEqual(self.failures[-1], "signature")

        token = jwt.encode(
            {
                "iss": ISSUER,
                "sub": "reviewer-1",
                "aud": AUDIENCE,
                "organization_id": "org-1",
                "iat": datetime.now(timezone.utc),
                "nbf": datetime.now(timezone.utc),
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            },
            "not-an-accepted-oidc-key-with-at-least-thirty-two-bytes",
            algorithm="HS256",
            headers={"kid": "symmetric"},
        )
        with self.assertRaises(AuthenticationRequired):
            self.authenticate(token)
        self.assertEqual(self.failures[-1], "algorithm")

    def test_unknown_subject_and_unknown_kid_fail_closed_without_repeated_refresh(self) -> None:
        unknown_subject = self.token(self.private_one, "one", sub="not-enrolled")
        with self.assertRaises(AuthenticationRequired):
            self.authenticate(unknown_subject)
        self.assertEqual(self.failures[-1], "unmapped_subject")

        unknown_kid = self.token(self.private_two, "not-present")
        with self.assertRaises(AuthenticationRequired):
            self.authenticate(unknown_kid)
        self.assertEqual(self.failures[-1], "unknown_kid")
        self.assertEqual(len(self.fetcher.calls), 2)
        with self.assertRaises(AuthenticationRequired):
            self.authenticate(unknown_kid)
        self.assertEqual(len(self.fetcher.calls), 2)

    def test_database_lookup_is_tenant_bound(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = JobStore(Path(root) / "state")
            try:
                database = store.database
                org_one = database.create_organization("org-one", "Organization One")
                org_two = database.create_organization("org-two", "Organization Two")
                member = database.create_membership(
                    org_one["id"],
                    subject="oidc-reviewer",
                    display_name="OIDC Reviewer",
                    role=Role.REVIEWER,
                )
                principal = database.principal_for_subject(
                    org_one["id"], "oidc-reviewer", auth_method="oidc"
                )
                self.assertIsNotNone(principal)
                self.assertEqual(principal.user_id, member["user_id"])
                self.assertIsNone(
                    database.principal_for_subject(
                        org_two["id"], "oidc-reviewer", auth_method="oidc"
                    )
                )
            finally:
                store.close()

    def test_oidc_environment_requires_complete_explicit_mode(self) -> None:
        environment = {
            "CRAG_AUTH_MODE": "oidc",
            "CRAG_WEBHOOK_SECRET": "a-valid-webhook-secret",
            "CRAG_OIDC_ISSUER": ISSUER,
            "CRAG_OIDC_AUDIENCE": AUDIENCE,
            "CRAG_OIDC_JWKS_URL": JWKS_URL,
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = HttpSettings.from_env()
        self.assertEqual(settings.auth_mode, "oidc")
        self.assertFalse(settings.local_token_enabled)
        self.assertEqual(settings.oidc_configuration.issuer, ISSUER)

        incomplete = dict(environment)
        incomplete.pop("CRAG_OIDC_JWKS_URL")
        with patch.dict(os.environ, incomplete, clear=True), self.assertRaisesRegex(
            InvalidRequest, "CRAG_OIDC_JWKS_URL"
        ):
            HttpSettings.from_env()

    def test_service_factory_selects_oidc_backend_only_in_explicit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            repository_path = root_path / "repository"
            repository_path.mkdir()
            (repository_path / ".git").mkdir()
            store = JobStore(root_path / "state")
            service: ReviewService | None = None
            try:
                service = ReviewService(
                    RepositoryRegistry.from_json(
                        json.dumps({"owner/repository": str(repository_path)})
                    ),
                    store,
                    local_mode=False,
                )
                settings = HttpSettings(
                    service_token="",
                    webhook_secret="a-valid-webhook-secret",
                    allowed_origins=frozenset({"http://localhost"}),
                    allowed_hosts=frozenset({"testserver"}),
                    local_token_enabled=False,
                    auth_mode="oidc",
                    oidc_configuration=self.configuration,
                )
                app = create_app(settings=settings, review_service=service)
                self.assertIsInstance(app.state.auth_backend, OIDCJWTAuthBackend)
            finally:
                if service is None:
                    store.close()
                else:
                    service.shutdown()


if __name__ == "__main__":
    unittest.main()
