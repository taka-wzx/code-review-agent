import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import text

from code_review_agent.identity import FakeAuthBackend, Permission, Principal, Role
from code_review_agent.repair_service import create_synthetic_staging_repair_service
from code_review_agent.service import HttpSettings, create_app
from code_review_agent.service_core import AuthorizationDenied, JobStore, RepositoryRegistry, ReviewRequest, ReviewService
from code_review_agent.worker import ReviewWorker


DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old = 1
+new = 2
"""


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[ReviewRequest] = []

    def __call__(self, request: ReviewRequest, trace_path: Path) -> dict[str, object]:
        self.calls.append(request)
        trace_path.write_text('{"status":"ok"}\n', encoding="utf-8")
        return {"summary": "ok", "findings": []}


class Issue30RBACServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        aliases = ("owner/repo-a", "owner/repo-a-private", "owner/repo-b")
        paths: dict[str, str] = {}
        for alias in aliases:
            path = self.root / alias.rsplit("/", 1)[1]
            path.mkdir()
            (path / ".git").mkdir()
            paths[alias] = str(path.resolve())
        self.registry = RepositoryRegistry.from_json(json.dumps(paths))
        self.store = JobStore(self.root / "state")
        self.database = self.store.database
        self.org_a = self.database.create_organization("org-a", "A")
        self.org_b = self.database.create_organization("org-b", "B")
        self.repo_a = self.database.register_repository(
            self.org_a["id"], "owner/repo-a", policy_version="policy/a"
        )
        self.repo_a_private = self.database.register_repository(
            self.org_a["id"], "owner/repo-a-private", policy_version="policy/a-private"
        )
        self.repo_b = self.database.register_repository(
            self.org_b["id"], "owner/repo-b", policy_version="policy/b"
        )
        self.members: dict[str, Principal] = {}
        entries = (
            ("admin_a", self.org_a, Role.ORG_ADMIN, ()),
            ("viewer_a", self.org_a, Role.VIEWER, (self.repo_a["id"],)),
            ("reviewer_a", self.org_a, Role.REVIEWER, (self.repo_a["id"],)),
            ("maintainer_a", self.org_a, Role.MAINTAINER, (self.repo_a["id"],)),
            (
                "maintainer_a_private",
                self.org_a,
                Role.MAINTAINER,
                (self.repo_a_private["id"],),
            ),
            ("maintainer_b", self.org_b, Role.MAINTAINER, (self.repo_b["id"],)),
            ("reviewer_b", self.org_b, Role.REVIEWER, (self.repo_b["id"],)),
        )
        tokens: dict[str, Principal] = {}
        for name, organization, role, repository_ids in entries:
            member = self.database.create_membership(
                organization["id"],
                subject=name,
                display_name=name,
                role=role,
                repository_ids=repository_ids,
            )
            principal = self.database.principal_for_user(
                organization["id"], member["user_id"]
            )
            self.assertIsNotNone(principal)
            self.members[name] = principal
            tokens[name.replace("_", "-")] = principal
        self.service = ReviewService(
            self.registry, self.store, runner=None, local_mode=False
        )
        self.repair = create_synthetic_staging_repair_service(
            self.database,
            allow_sqlite_for_tests=True,
            validate_environment=False,
            metrics=self.service.metrics,
        )
        settings = HttpSettings(
            service_token="",
            webhook_secret="issue30-webhook-secret",
            allowed_origins=frozenset({"http://localhost"}),
            allowed_hosts=frozenset({"testserver"}),
            local_token_enabled=False,
        )
        self.client_context = TestClient(
            create_app(
                settings=settings,
                review_service=self.service,
                repair_service=self.repair,
                auth_backend=FakeAuthBackend(tokens),
            )
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    @staticmethod
    def auth(name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {name.replace('_', '-')}"}

    def test_role_fixture_has_explicit_non_monotonic_permissions(self) -> None:
        self.assertFalse(self.members["viewer_a"].allows(Permission.SUBMIT_REVIEW))
        self.assertTrue(self.members["reviewer_a"].allows(Permission.SUBMIT_REVIEW))
        self.assertTrue(self.members["maintainer_a"].allows(Permission.START_REPAIR))
        self.assertTrue(self.members["admin_a"].allows(Permission.MANAGE_POLICY))
        self.assertFalse(self.members["admin_a"].allows(Permission.SUBMIT_REVIEW))

    def test_cross_tenant_policy_and_quota_are_not_found_and_audited(self) -> None:
        policy = self.client.get(
            f"/v1/organizations/{self.org_b['id']}/policy",
            headers=self.auth("admin_a"),
        )
        self.assertEqual(policy.status_code, 404, policy.text)
        quota = self.client.get(
            f"/v1/organizations/{self.org_a['id']}/repositories/"
            f"{self.repo_b['id']}/service-quota",
            headers=self.auth("admin_a"),
        )
        self.assertEqual(quota.status_code, 404, quota.text)
        with self.database.engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        "SELECT action, decision, reason_code, repository_id "
                        "FROM audit_events WHERE principal_id=:principal"
                    ),
                    {"principal": self.members["admin_a"].principal_id},
                ).mappings()
            ]
        self.assertIn(
            ("organization.policy.read", "deny", "cross_organization", None),
            [
                (
                    row["action"],
                    row["decision"],
                    row["reason_code"],
                    row["repository_id"],
                )
                for row in rows
            ],
        )
        self.assertIn(
            ("service_quota.read", "deny", "not_found", None),
            [
                (
                    row["action"],
                    row["decision"],
                    row["reason_code"],
                    row["repository_id"],
                )
                for row in rows
            ],
        )

    def test_repair_requires_repository_access_with_same_not_found_shape(self) -> None:
        created = self.client.post(
            "/v1/repairs",
            headers=self.auth("maintainer_a"),
            json={
                "repository": "owner/repo-a",
                "finding_sha256": "a" * 64,
                "base_sha": "b" * 40,
                "head_sha": "c" * 40,
            },
        )
        self.assertEqual(created.status_code, 202, created.text)
        job_id = created.json()["job_id"]
        same_org_denied = self.client.get(
            f"/v1/repairs/{job_id}", headers=self.auth("maintainer_a_private")
        )
        cross_org_denied = self.client.get(
            f"/v1/repairs/{job_id}", headers=self.auth("maintainer_b")
        )
        self.assertEqual(same_org_denied.status_code, 404, same_org_denied.text)
        self.assertEqual(cross_org_denied.status_code, 404, cross_org_denied.text)
        start_cross_org = self.client.post(
            "/v1/repairs",
            headers=self.auth("maintainer_a"),
            json={
                "repository": "owner/repo-b",
                "finding_sha256": "a" * 64,
                "base_sha": "b" * 40,
                "head_sha": "c" * 40,
            },
        )
        self.assertEqual(start_cross_org.status_code, 404, start_cross_org.text)
        with self.database.engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        "SELECT action, decision, reason_code, repository_id "
                        "FROM audit_events WHERE action LIKE 'repair.%'"
                    )
                ).mappings()
            ]
        self.assertTrue(any(row["decision"] == "allow" for row in rows))
        self.assertTrue(
            any(
                row["decision"] == "deny"
                and row["reason_code"] == "repair_repository_not_found"
                for row in rows
            )
        )
        self.assertTrue(
            any(
                row["decision"] == "deny"
                and row["reason_code"] == "repair_cross_organization_denied"
                and row["repository_id"] is None
                for row in rows
            )
        )

    def test_queue_cannot_accept_forged_tenant_identity(self) -> None:
        with self.assertRaises(AuthorizationDenied):
            self.store.create(
                source_kind="diff",
                repository="owner/repo-b",
                source_ref="inline",
                source_sha256="d" * 64,
                source_bytes=1,
                organization_id=self.org_a["id"],
                repository_id=self.repo_b["id"],
                submitted_by="forged-user",
                principal=self.members["reviewer_a"],
            )
        with self.assertRaises(AuthorizationDenied):
            self.store.create(
                source_kind="diff",
                repository="owner/repo-b",
                source_ref="inline",
                source_sha256="e" * 64,
                source_bytes=1,
                organization_id=self.org_a["id"],
                repository_id=self.repo_a["id"],
                submitted_by="forged-user",
            )

    def test_worker_rechecks_repository_before_runner_execution(self) -> None:
        submitted = self.service.submit_diff(
            "owner/repo-a",
            DIFF,
            principal=self.members["reviewer_a"],
        )
        lease = self.store.claim("issue30-worker", lease_seconds=30)
        self.assertIsNotNone(lease)
        with self.database.engine.begin() as connection:
            connection.execute(
                text("UPDATE repositories SET active=:active WHERE id=:id"),
                {"active": False, "id": self.repo_a["id"]},
            )
        runner = RecordingRunner()
        worker = ReviewWorker(
            self.registry,
            self.store,
            runner=runner,
            worker_id="issue30-executor",
            concurrency=1,
            lease_seconds=30,
            heartbeat_seconds=5,
        )
        worker._start_claimed(lease)
        active = worker._active[lease.job_id]
        active.thread.join(3)
        self.assertFalse(active.thread.is_alive())
        self.assertEqual(runner.calls, [])
        record = self.store.get(submitted["review_id"])
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["error"]["code"], "repository_unavailable")


if __name__ == "__main__":
    unittest.main()
