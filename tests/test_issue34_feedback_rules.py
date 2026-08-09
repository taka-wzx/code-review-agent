import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from code_review_agent.agent import build_review_input
from code_review_agent.feedback_rules import FeedbackRuleStore, normalize_rules
from code_review_agent.identity import FakeAuthBackend, Principal, Role
from code_review_agent.service import HttpSettings, create_app
from code_review_agent.service_core import (
    DefaultReviewRunner,
    JobStore,
    RepositoryRegistry,
    ReviewRequest,
    ReviewService,
)


DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old = 1
+new = 2
"""


def rule(rule_id: str, action: str = "prioritize") -> dict[str, str]:
    return {
        "rule_id": rule_id,
        "category": "correctness",
        "action": action,
        "condition": "Finding concerns changed authentication logic",
        "rationale": "Repository maintainers confirmed this feedback pattern",
    }


class Issue34FeedbackRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        checkout = self.root / "repo"
        checkout.mkdir()
        (checkout / ".git").mkdir()
        self.registry = RepositoryRegistry.from_json(
            json.dumps({"owner/repo": str(checkout.resolve())})
        )
        self.store = JobStore(self.root / "state")
        self.database = self.store.database
        organization = self.database.create_organization("org-a", "Organization A")
        repository = self.database.register_repository(
            organization["id"], "owner/repo", policy_version="policy/a"
        )
        self.organization_id = str(organization["id"])
        self.repository_id = str(repository["id"])
        self.principals: dict[str, Principal] = {}
        tokens: dict[str, Principal] = {}
        for name, role, repository_ids in (
            ("admin", Role.ORG_ADMIN, ()),
            ("reviewer", Role.REVIEWER, (self.repository_id,)),
            ("viewer", Role.VIEWER, (self.repository_id,)),
        ):
            membership = self.database.create_membership(
                self.organization_id,
                subject=name,
                display_name=name,
                role=role,
                repository_ids=repository_ids,
            )
            principal = self.database.principal_for_user(
                self.organization_id, membership["user_id"]
            )
            assert isinstance(principal, Principal)
            self.principals[name] = principal
            tokens[f"{name}-token"] = principal
        self.rules = FeedbackRuleStore(self.database)
        self.service = ReviewService(
            self.registry, self.store, runner=None, local_mode=False
        )
        self.client_context = TestClient(
            create_app(
                settings=HttpSettings(
                    service_token="",
                    webhook_secret="issue34-webhook-secret",
                    allowed_origins=frozenset({"http://localhost"}),
                    allowed_hosts=frozenset({"testserver"}),
                    local_token_enabled=False,
                ),
                review_service=self.service,
                auth_backend=FakeAuthBackend(tokens),
            )
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    @staticmethod
    def auth(name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {name}-token"}

    def endpoint(self, suffix: str = "") -> str:
        base = (
            f"/v1/organizations/{self.organization_id}/repositories/"
            f"{self.repository_id}/feedback-rules"
        )
        return base + suffix

    def create_version(self, version: str, rule_id: str) -> dict[str, object]:
        response = self.client.post(
            self.endpoint(),
            headers=self.auth("admin"),
            json={
                "version": version,
                "rules": [rule(rule_id)],
                "reason": f"create {version}",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def activate(self, version: str, action: str = "activate") -> dict[str, object]:
        response = self.client.post(
            self.endpoint(f"/{version}/{action}"),
            headers=self.auth("admin"),
            json={"reason": f"{action} {version}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_migration_creates_version_active_receipt_and_binding_tables(self) -> None:
        tables = set(inspect(self.database.engine).get_table_names())
        self.assertTrue(
            {
                "repository_feedback_rule_versions",
                "repository_feedback_rule_active",
                "repository_feedback_rule_receipts",
                "review_feedback_rule_bindings",
            }.issubset(tables)
        )
        with self.database.engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version"))
            self.assertEqual(revision.scalar_one(), "0011_issue34_feedback_rules")

    def test_versions_are_immutable_and_rollback_receipt_is_hash_bound(self) -> None:
        first = self.create_version("v1", "auth-1")
        duplicate = self.create_version("v1", "auth-1")
        self.assertEqual(first["rules_sha256"], duplicate["rules_sha256"])
        conflict = self.client.post(
            self.endpoint(),
            headers=self.auth("admin"),
            json={
                "version": "v1",
                "rules": [rule("auth-2")],
                "reason": "attempt mutation",
            },
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"], "feedback_rule_conflict")

        self.create_version("v2", "auth-2")
        with patch.object(
            FeedbackRuleStore,
            "active",
            side_effect=AssertionError("transition response must not re-read active state"),
        ):
            activated_v1 = self.activate("v1")
        active_response = self.client.get(
            self.endpoint("/active"), headers=self.auth("viewer")
        )
        self.assertEqual(active_response.status_code, 200, active_response.text)
        self.assertEqual(active_response.json()["active"], activated_v1["active"])
        activated_v2 = self.activate("v2")
        rolled_back = self.activate("v1", action="rollback")
        self.assertEqual(activated_v1["active"]["generation"], 1)
        self.assertEqual(activated_v2["active"]["generation"], 2)
        self.assertEqual(rolled_back["active"]["generation"], 3)
        self.assertEqual(rolled_back["receipt"]["action"], "rollback")
        self.assertEqual(rolled_back["receipt"]["from_version"], "v2")
        self.assertEqual(rolled_back["receipt"]["to_version"], "v1")

        with self.database.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT receipt_json, receipt_sha256 FROM "
                    "repository_feedback_rule_receipts ORDER BY generation"
                )
            ).all()
        self.assertEqual(len(rows), 3)
        for row in rows:
            receipt_json = str(row._mapping["receipt_json"])
            self.assertEqual(
                hashlib.sha256(receipt_json.encode("utf-8")).hexdigest(),
                row._mapping["receipt_sha256"],
            )
        receipts = self.client.get(
            self.endpoint().replace("feedback-rules", "feedback-rule-receipts"),
            headers=self.auth("viewer"),
        )
        self.assertEqual(receipts.status_code, 200, receipts.text)
        self.assertEqual(len(receipts.json()["receipts"]), 3)

    def test_active_version_is_bound_to_job_and_does_not_change_in_flight(self) -> None:
        self.create_version("v1", "auth-1")
        self.create_version("v2", "auth-2")
        self.activate("v1")
        first = self.service.submit_diff(
            "owner/repo", DIFF, principal=self.principals["reviewer"]
        )
        self.assertEqual(first["feedback_rule"]["version"], "v1")
        self.assertEqual(first["feedback_rule"]["generation"], 1)

        self.activate("v2")
        unchanged = self.service.get(
            first["review_id"], principal=self.principals["reviewer"]
        )
        self.assertEqual(unchanged["feedback_rule"]["version"], "v1")
        lease = self.store.claim("issue34-worker", lease_seconds=30)
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertEqual(lease.feedback_rule["version"], "v1")
        self.assertEqual(lease.feedback_rule["rules_sha256"], first["feedback_rule"]["rules_sha256"])

        second = self.service.submit_diff(
            "owner/repo", DIFF, principal=self.principals["reviewer"]
        )
        self.assertNotEqual(second["review_id"], first["review_id"])
        self.assertEqual(second["feedback_rule"]["version"], "v2")
        prompt = build_review_input(
            DIFF,
            self.root / "repo",
            use_context=False,
            feedback_rule=lease.feedback_rule,
        )
        self.assertIn("Bound repository feedback rules", prompt)
        self.assertIn("version=v1", prompt)
        self.assertIn("auth-1", prompt)

    def test_api_enforces_validation_rbac_and_tenant_scope(self) -> None:
        invalid = self.client.post(
            self.endpoint(),
            headers=self.auth("admin"),
            json={
                "version": "v1",
                "rules": [{**rule("auth-1"), "action": "execute"}],
                "reason": "invalid action",
            },
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)
        denied = self.client.post(
            self.endpoint(),
            headers=self.auth("viewer"),
            json={"version": "v1", "rules": [rule("auth-1")], "reason": "deny"},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        cross_org = self.client.get(
            self.endpoint().replace(self.organization_id, "org-other"),
            headers=self.auth("admin"),
        )
        self.assertEqual(cross_org.status_code, 404, cross_org.text)

        self.create_version("v1", "auth-1")
        never_active = self.client.post(
            self.endpoint("/v1/rollback"),
            headers=self.auth("admin"),
            json={"reason": "not yet active"},
        )
        self.assertEqual(never_active.status_code, 409, never_active.text)
        readable = self.client.get(self.endpoint(), headers=self.auth("viewer"))
        self.assertEqual(readable.status_code, 200, readable.text)
        self.assertEqual(readable.json()["versions"][0]["version"], "v1")

    def test_default_runner_records_and_forwards_bound_rule_identity(self) -> None:
        _, canonical, rules_sha256 = normalize_rules([rule("auth-1")])
        binding = {
            "version_id": "a" * 64,
            "version": "v1",
            "generation": 4,
            "rules": [rule("auth-1")],
            "rules_json": canonical,
            "rules_sha256": rules_sha256,
            "bound_at": "2026-08-09T00:00:00Z",
        }
        request = ReviewRequest(
            job_id="0" * 32,
            source_kind="diff",
            repository="owner/repo",
            repo_root=self.root / "repo",
            source_ref="inline",
            diff=DIFF,
            organization_id=self.organization_id,
            repository_id=self.repository_id,
            principal_id=self.principals["reviewer"].principal_id,
            feedback_rule=binding,
        )
        trace_path = self.root / "bound-rule-trace.jsonl"
        runner = DefaultReviewRunner(client_factory=lambda: (object(), "model"))
        with patch(
            "code_review_agent.service_core.run_review",
            return_value={"summary": "done", "findings": []},
        ) as review:
            runner(request, trace_path)
        self.assertEqual(review.call_args.kwargs["feedback_rule"], binding)
        trace = trace_path.read_text(encoding="utf-8")
        self.assertIn("crag.feedback_rule.version", trace)
        self.assertIn(rules_sha256, trace)


if __name__ == "__main__":
    unittest.main()
