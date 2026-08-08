from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from code_review_agent.github_installation import (
    DatabaseLifecycleAuditSink,
    GitHubInstallationApiError,
    GitHubInstallationValidator,
    GitHubRepositoryRegistration,
    InstallationCredential,
    LifecycleState,
)
from code_review_agent.identity import Role
from code_review_agent.service_core import JobStore


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
TOKEN = "installation-token-that-must-never-enter-a-receipt"


class FakeGitHubClient:
    def __init__(self) -> None:
        self.installation: object = {
            "id": 149747930,
            "app_id": 4421400,
            "account": {"id": 20105},
            "suspended_at": None,
        }
        self.repository: object = {
            "id": 987654,
            "full_name": "taka-wzx/code-review-agent",
            "owner": {"login": "taka-wzx"},
        }
        self.calls: list[tuple[str, object, str]] = []

    def get_installation(self, installation_id: int, token: str):
        self.calls.append(("installation", installation_id, token))
        if isinstance(self.installation, Exception):
            raise self.installation
        return self.installation

    def get_repository(self, owner: str, name: str, token: str):
        self.calls.append(("repository", f"{owner}/{name}", token))
        if isinstance(self.repository, Exception):
            raise self.repository
        return self.repository


class RecordingSink:
    def __init__(self) -> None:
        self.receipts = []

    def record(self, receipt) -> None:
        self.receipts.append(receipt)


def registration() -> GitHubRepositoryRegistration:
    return GitHubRepositoryRegistration(
        organization_id="org-1",
        registered_repository_id="repo-registration-1",
        owner="taka-wzx",
        name="code-review-agent",
        github_repository_id=987654,
        github_app_id=4421400,
        installation_id=149747930,
        installation_account_id=20105,
    )


def credential(**overrides: object) -> InstallationCredential:
    values = {
        "value": TOKEN,
        "app_id": 4421400,
        "installation_id": 149747930,
        "installation_account_id": 20105,
        "expires_at": NOW + timedelta(minutes=20),
        "revoked": False,
    }
    values.update(overrides)
    return InstallationCredential(**values)


class Issue26GitHubInstallationTests(unittest.TestCase):
    def test_active_installation_returns_sanitized_receipt_and_calls_both_reads(self) -> None:
        client = FakeGitHubClient()
        sink = RecordingSink()
        validator = GitHubInstallationValidator(
            client,
            audit_sink=sink,
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt-issue26-1",
        )

        result = validator.validate(registration(), credential())

        self.assertTrue(result.accepted)
        self.assertEqual(result.receipt.state, LifecycleState.ACTIVE)
        self.assertEqual(result.receipt.reason, "active")
        self.assertEqual([item[0] for item in client.calls], ["installation", "repository"])
        self.assertEqual(sink.receipts[0].receipt_sha256, result.receipt.receipt_sha256)
        serialized = json.dumps(result.receipt.to_dict(), sort_keys=True)
        self.assertNotIn(TOKEN, serialized)
        self.assertNotIn("taka-wzx/code-review-agent", serialized)

    def test_identity_mismatch_stops_before_repository_read(self) -> None:
        client = FakeGitHubClient()
        client.installation = {**client.installation, "app_id": 99}
        result = GitHubInstallationValidator(client, clock=lambda: NOW).validate(
            registration(), credential()
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.receipt.reason, "app_id_mismatch")
        self.assertEqual([item[0] for item in client.calls], ["installation"])

        client = FakeGitHubClient()
        client.repository = {**client.repository, "id": 123}
        result = GitHubInstallationValidator(client, clock=lambda: NOW).validate(
            registration(), credential()
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.receipt.reason, "repository_id_mismatch")

    def test_suspension_and_deletion_are_distinct_fail_closed_states(self) -> None:
        client = FakeGitHubClient()
        client.installation = {**client.installation, "suspended_at": "2026-08-07T11:00:00Z"}
        result = GitHubInstallationValidator(client, clock=lambda: NOW).validate(
            registration(), credential()
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.receipt.state, LifecycleState.SUSPENDED)
        self.assertEqual(result.receipt.reason, "installation_suspended")

        client = FakeGitHubClient()
        client.repository = GitHubInstallationApiError(404)
        result = GitHubInstallationValidator(client, clock=lambda: NOW).validate(
            registration(), credential()
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.receipt.state, LifecycleState.DELETED)
        self.assertEqual(result.receipt.reason, "repository_deleted")

    def test_credential_expiry_revocation_and_identity_are_checked_before_api(self) -> None:
        for supplied, reason in (
            (credential(expires_at=NOW), "token_expired"),
            (credential(revoked=True), "token_revoked"),
            (credential(installation_id=12), "credential_identity_mismatch"),
        ):
            client = FakeGitHubClient()
            result = GitHubInstallationValidator(client, clock=lambda: NOW).validate(
                registration(), supplied
            )
            with self.subTest(reason=reason):
                self.assertFalse(result.accepted)
                self.assertEqual(result.receipt.reason, reason)
                self.assertEqual(client.calls, [])

    def test_api_errors_and_malformed_responses_are_redacted(self) -> None:
        client = FakeGitHubClient()
        client.installation = GitHubInstallationApiError(503)
        result = GitHubInstallationValidator(client, clock=lambda: NOW).validate(
            registration(), credential()
        )
        self.assertEqual(result.receipt.reason, "api_unavailable")
        self.assertEqual(result.receipt.state, LifecycleState.ERROR)

        client = FakeGitHubClient()
        client.installation = {"id": "not-an-int"}
        result = GitHubInstallationValidator(client, clock=lambda: NOW).validate(
            registration(), credential()
        )
        self.assertEqual(result.receipt.reason, "response_invalid")
        self.assertNotIn(TOKEN, repr(result.receipt))

    def test_database_audit_sink_persists_only_stable_projection(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = JobStore(Path(root) / "state")
            try:
                database = store.database
                organization = database.create_organization("org-1", "Organization")
                repository = database.register_repository(
                    organization["id"], "taka-wzx/code-review-agent"
                )
                member = database.create_membership(
                    organization["id"],
                    subject="operator",
                    display_name="Operator",
                    role=Role.ORG_ADMIN,
                    repository_ids=(repository["id"],),
                )
                principal = database.principal_for_user(
                    organization["id"], member["user_id"], auth_method="api_token"
                )
                self.assertIsNotNone(principal)
                sink = DatabaseLifecycleAuditSink(database, principal)
                client = FakeGitHubClient()
                validator = GitHubInstallationValidator(
                    client,
                    audit_sink=sink,
                    clock=lambda: NOW,
                    receipt_id_factory=lambda: "receipt-db-1",
                )
                result = validator.validate(
                    GitHubRepositoryRegistration(
                        **{**registration().__dict__, "registered_repository_id": repository["id"]}
                    ),
                    credential(),
                )
                self.assertTrue(result.accepted)
                events = database.list_audit_events(organization["id"])
                self.assertEqual(len(events), 1)
                serialized = json.dumps(events[0], sort_keys=True)
                self.assertNotIn(TOKEN, serialized)
                self.assertNotIn("taka-wzx/code-review-agent", serialized)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
