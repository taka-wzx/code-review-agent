import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import anyio
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import text

from code_review_agent.identity import (
    AuthenticationRequired,
    DatabaseAuthBackend,
    FakeAuthBackend,
    Role,
    VerifiedOIDCJWTAuthBackend,
)
from code_review_agent.mcp_server import create_mcp
from code_review_agent.service import HttpSettings, create_app, main as service_main
from code_review_agent.service_core import JobStore, RepositoryRegistry, ReviewRequest, ReviewService

from tests.test_week7_service_core import DIFF


WEBHOOK_SECRET = "phase9b-webhook-secret-value"


class FindingRunner:
    def __init__(self):
        self.calls: list[ReviewRequest] = []

    def __call__(self, request: ReviewRequest, trace_path: Path):
        self.calls.append(request)
        trace_path.write_text(json.dumps({"trace": "redacted"}) + "\n", encoding="utf-8")
        return {
            "summary": "ok",
            "findings": [
                {
                    "fingerprint": "stable-finding",
                    "path": "a.py",
                    "line": 1,
                    "severity": "high",
                    "category": "correctness",
                    "message": "bounded test finding",
                }
            ],
        }


class Phase9BIdentityRBACTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        aliases = ("owner/repo-a", "owner/repo-b", "owner/repo-c")
        paths = {}
        for alias in aliases:
            path = self.root / alias.rsplit("/", 1)[1]
            path.mkdir()
            (path / ".git").mkdir()
            paths[alias] = str(path.resolve())
        self.registry = RepositoryRegistry.from_json(json.dumps(paths))
        self.store = JobStore(self.root / "state")
        self.database = self.store.database

        self.org_a = self.database.create_organization("org-a", "Organization A")
        self.org_b = self.database.create_organization("org-b", "Organization B")
        self.repo_a = self.database.register_repository(
            self.org_a["id"], "owner/repo-a", policy_version="policy/a"
        )
        self.repo_b = self.database.register_repository(
            self.org_b["id"], "owner/repo-b", policy_version="policy/b"
        )
        self.members = {}
        for name, organization, role, repositories in (
            ("admin_a", self.org_a, Role.ORG_ADMIN, ()),
            ("viewer_a", self.org_a, Role.VIEWER, (self.repo_a["id"],)),
            ("reviewer_a", self.org_a, Role.REVIEWER, (self.repo_a["id"],)),
            ("maintainer_a", self.org_a, Role.MAINTAINER, (self.repo_a["id"],)),
            ("admin_b", self.org_b, Role.ORG_ADMIN, ()),
            ("reviewer_b", self.org_b, Role.REVIEWER, (self.repo_b["id"],)),
            ("maintainer_b", self.org_b, Role.MAINTAINER, (self.repo_b["id"],)),
        ):
            member = self.database.create_membership(
                organization["id"],
                subject=name,
                display_name=name,
                role=role,
                repository_ids=repositories,
            )
            principal = self.database.principal_for_user(
                organization["id"], member["user_id"]
            )
            self.assertIsNotNone(principal)
            self.members[name] = (member, principal)

        principals = {
            name.replace("_", "-"): principal
            for name, (_, principal) in self.members.items()
        }
        self.runner = FindingRunner()
        self.service = ReviewService(
            self.registry,
            self.store,
            runner=self.runner,
            local_mode=False,
        )
        settings = HttpSettings(
            service_token="",
            webhook_secret=WEBHOOK_SECRET,
            allowed_origins=frozenset({"http://localhost"}),
            allowed_hosts=frozenset({"testserver"}),
            local_token_enabled=False,
        )
        self.app = create_app(
            settings=settings,
            review_service=self.service,
            auth_backend=FakeAuthBackend(principals),
        )
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    @staticmethod
    def auth(name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {name.replace('_', '-')}"}

    def wait_terminal(self, job_id: str, actor: str) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = self.client.get(f"/v1/reviews/{job_id}", headers=self.auth(actor))
            if response.status_code == 200 and response.json()["state"] in {
                "awaiting_approval",
                "failed",
                "dead_letter",
            }:
                return response.json()
            time.sleep(0.01)
        self.fail("job did not become terminal")

    def submit_review(self, actor: str, repository: str) -> tuple[str, str]:
        response = self.client.post(
            "/v1/reviews/diff",
            headers=self.auth(actor),
            json={"repository": repository, "diff": DIFF},
        )
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["review_id"]
        self.wait_terminal(job_id, actor)
        findings = self.client.get(
            f"/v1/reviews/{job_id}/findings", headers=self.auth(actor)
        )
        self.assertEqual(findings.status_code, 200, findings.text)
        return job_id, findings.json()["findings"][0]["id"]

    def test_principal_admin_members_repositories_and_audit_are_org_scoped(self):
        principal = self.client.get("/v1/principal", headers=self.auth("admin_a"))
        self.assertEqual(principal.status_code, 200)
        self.assertEqual(principal.json()["organization_id"], self.org_a["id"])
        self.assertEqual(principal.json()["role"], "org_admin")

        members = self.client.get(
            f"/v1/organizations/{self.org_a['id']}/memberships",
            headers=self.auth("admin_a"),
        )
        self.assertEqual(members.status_code, 200)
        self.assertEqual(len(members.json()["memberships"]), 4)
        cross_org = self.client.get(
            f"/v1/organizations/{self.org_b['id']}/memberships",
            headers=self.auth("admin_a"),
        )
        self.assertEqual(cross_org.status_code, 404)

        registered = self.client.post(
            f"/v1/organizations/{self.org_a['id']}/repositories",
            headers={**self.auth("admin_a"), "X-Request-ID": "phase9b-admin-request"},
            json={
                "repository": "owner/repo-c",
                "mode": "shadow",
                "budget_microusd": 1000,
                "policy_version": "policy/c",
            },
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        duplicate = self.client.post(
            f"/v1/organizations/{self.org_a['id']}/repositories",
            headers=self.auth("admin_a"),
            json={"repository": "owner/repo-c", "policy_version": "policy/c"},
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["error"]["code"], "database_conflict")
        updated = self.client.patch(
            f"/v1/organizations/{self.org_a['id']}/repositories/"
            f"{registered.json()['id']}",
            headers=self.auth("admin_a"),
            json={
                "mode": "guarded_publish",
                "budget_microusd": 2000,
                "policy_version": "policy/c2",
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["budget_microusd"], 2000)
        repositories = self.client.get(
            f"/v1/organizations/{self.org_a['id']}/repositories",
            headers=self.auth("admin_a"),
        )
        self.assertEqual(
            {item["alias"] for item in repositories.json()["repositories"]},
            {"owner/repo-a", "owner/repo-c"},
        )
        audit = self.client.get("/v1/audit-events", headers=self.auth("admin_a"))
        self.assertEqual(audit.status_code, 200)
        self.assertTrue(audit.json()["audit_events"])
        self.assertTrue(
            all(
                item["organization_id"] == self.org_a["id"]
                for item in audit.json()["audit_events"]
            )
        )
        self.assertIn(
            "phase9b-admin-request",
            {item["correlation_id"] for item in audit.json()["audit_events"]},
        )

    def test_role_matrix_feedback_approval_and_self_role_negative(self):
        viewer_submit = self.client.post(
            "/v1/reviews/diff",
            headers=self.auth("viewer_a"),
            json={"repository": "owner/repo-a", "diff": DIFF},
        )
        self.assertEqual(viewer_submit.status_code, 403)

        _, finding_id = self.submit_review("reviewer_a", "owner/repo-a")
        finding = self.client.get(
            f"/v1/findings/{finding_id}", headers=self.auth("viewer_a")
        )
        self.assertEqual(finding.status_code, 200, finding.text)
        viewer_feedback = self.client.post(
            f"/v1/findings/{finding_id}/feedback",
            headers=self.auth("viewer_a"),
            json={
                "decision": "accepted",
                "finding_hash": finding.json()["content_sha256"],
            },
        )
        self.assertEqual(viewer_feedback.status_code, 403)
        feedback = self.client.post(
            f"/v1/findings/{finding_id}/feedback",
            headers=self.auth("reviewer_a"),
            json={
                "decision": "accepted",
                "finding_hash": finding.json()["content_sha256"],
                "reason": "actionable",
            },
        )
        self.assertEqual(feedback.status_code, 409, feedback.text)
        self.assertEqual(feedback.json()["error"]["code"], "feedback_conflict")
        reviewer_approval = self.client.post(
            f"/v1/findings/{finding_id}/decisions",
            headers=self.auth("reviewer_a"),
            json={"decision": "approved"},
        )
        self.assertEqual(reviewer_approval.status_code, 403)
        admin_approval = self.client.post(
            f"/v1/findings/{finding_id}/decisions",
            headers=self.auth("admin_a"),
            json={"decision": "approved"},
        )
        self.assertEqual(admin_approval.status_code, 403)
        approval = self.client.post(
            f"/v1/findings/{finding_id}/decisions",
            headers=self.auth("maintainer_a"),
            json={"decision": "approved"},
        )
        self.assertEqual(approval.status_code, 201, approval.text)

        reviewer_member = self.members["reviewer_a"][0]
        self_role = self.client.patch(
            f"/v1/organizations/{self.org_a['id']}/memberships/"
            f"{reviewer_member['membership_id']}",
            headers=self.auth("reviewer_a"),
            json={"role": "org_admin", "repository_ids": []},
        )
        self.assertEqual(self_role.status_code, 403)

        detail = self.client.get(
            f"/v1/findings/{finding_id}", headers=self.auth("viewer_a")
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["feedback"], [])
        self.assertEqual(detail.json()["approvals"][0]["decision"], "approved")

    def test_cross_tenant_job_trace_finding_feedback_and_approval_are_not_found(self):
        job_b, finding_b = self.submit_review("reviewer_b", "owner/repo-b")
        finding = self.client.get(
            f"/v1/findings/{finding_b}", headers=self.auth("reviewer_b")
        )
        self.assertEqual(finding.status_code, 200, finding.text)
        feedback = self.client.post(
            f"/v1/findings/{finding_b}/feedback",
            headers=self.auth("reviewer_b"),
            json={
                "decision": "rejected",
                "finding_hash": finding.json()["content_sha256"],
                "reason": "low_value",
            },
        )
        self.assertEqual(feedback.status_code, 409)
        approval = self.client.post(
            f"/v1/findings/{finding_b}/decisions",
            headers=self.auth("maintainer_b"),
            json={"decision": "rejected"},
        )
        self.assertEqual(approval.status_code, 201)

        for path in (
            f"/v1/reviews/{job_b}",
            f"/v1/reviews/{job_b}/trace",
            f"/v1/reviews/{job_b}/findings",
            f"/v1/findings/{finding_b}",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.auth("viewer_a"))
                self.assertEqual(response.status_code, 404, response.text)

    def test_mcp_uses_the_same_cross_tenant_authorization(self):
        reviewer_b = self.members["reviewer_b"][1]
        viewer_a = self.members["viewer_a"][1]
        submitted, _ = self.service.submit_pr(
            "owner/repo-b",
            "9",
            principal=reviewer_b,
            idempotency_key="phase9b-compatible-pr-9",
        )
        mcp = create_mcp(self.service, principal_provider=lambda: viewer_a)

        async def exercise():
            async with create_connected_server_and_client_session(mcp) as session:
                status = await session.call_tool(
                    "get_review_status", {"review_id": submitted["review_id"]}
                )
                self.assertTrue(status.isError)
                cross_submit = await session.call_tool(
                    "review_diff", {"repository": "owner/repo-b", "diff": DIFF}
                )
                self.assertTrue(cross_submit.isError)

        anyio.run(exercise)

    def test_database_token_is_hashed_and_revocation_is_immediate(self):
        reviewer = self.members["reviewer_a"][1]
        created = self.client.post(
            "/v1/credentials",
            headers=self.auth("reviewer_a"),
            json={"expires_in_seconds": 3600},
        )
        self.assertEqual(created.status_code, 201, created.text)
        record = created.json()
        raw_token = record["token"]
        backend = DatabaseAuthBackend(self.database)
        authenticated = backend.authenticate(f"Bearer {raw_token}")
        self.assertEqual(authenticated.user_id, reviewer.user_id)

        with self.database.engine.connect() as connection:
            stored = connection.execute(
                text(
                    "SELECT token_hash, token_prefix FROM access_credentials WHERE id=:id"
                ),
                {"id": record["credential_id"]},
            ).mappings().one()
            audit_text = json.dumps(
                [dict(row) for row in connection.execute(text("SELECT * FROM audit_events")).mappings()]
            )
        self.assertEqual(stored["token_hash"], hashlib.sha256(raw_token.encode()).hexdigest())
        self.assertNotEqual(stored["token_hash"], raw_token)
        self.assertNotIn(raw_token, audit_text)

        revoked = self.client.delete(
            f"/v1/credentials/{record['credential_id']}",
            headers=self.auth("reviewer_a"),
        )
        self.assertEqual(revoked.status_code, 204, revoked.text)
        with self.assertRaises(AuthenticationRequired):
            backend.authenticate(f"Bearer {raw_token}")

    def test_webhook_is_system_attributed_and_cannot_create_approval(self):
        payload = {
            "action": "opened",
            "repository": {"full_name": "owner/repo-a"},
            "pull_request": {"number": 8, "head": {"sha": "a" * 40}},
            "approval": {"decision": "approved", "principal_id": "spoofed"},
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        response = self.client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "phase9b-delivery",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.wait_terminal(response.json()["review_id"], "reviewer_a")
        with self.database.engine.connect() as connection:
            job = connection.execute(
                text("SELECT submitted_by FROM review_jobs WHERE id=:id"),
                {"id": response.json()["review_id"]},
            ).mappings().one()
            approval_count = connection.execute(
                text("SELECT COUNT(*) FROM approvals")
            ).scalar_one()
        self.assertEqual(job["submitted_by"], "github-webhook")
        self.assertEqual(approval_count, 0)

    def test_oidc_adapter_requires_verified_standard_claim_shape(self):
        reviewer = self.members["reviewer_a"][1]
        backend = VerifiedOIDCJWTAuthBackend(
            lambda token: {
                "iss": "https://issuer.example",
                "sub": "reviewer-a",
                "aud": "crag",
                "exp": 4_102_444_800,
                "token_marker": token,
            },
            lambda claims: reviewer if claims["sub"] == "reviewer-a" else None,
        )
        mapped = backend.authenticate("Bearer signed-jwt-placeholder")
        self.assertEqual(mapped.user_id, reviewer.user_id)
        missing_claim = VerifiedOIDCJWTAuthBackend(
            lambda token: {"sub": token}, lambda claims: reviewer
        )
        with self.assertRaises(AuthenticationRequired):
            missing_claim.authenticate("Bearer invalid")

    def test_local_static_token_is_opt_in_and_public_bind_is_rejected(self):
        with patch.dict(
            os.environ,
            {
                "CRAG_SERVICE_TOKEN": "ignored-unless-explicitly-enabled-token",
                "CRAG_WEBHOOK_SECRET": WEBHOOK_SECRET,
            },
            clear=True,
        ):
            default_settings = HttpSettings.from_env()
        self.assertFalse(default_settings.local_token_enabled)
        self.assertEqual(default_settings.service_token, "")

        local_settings = HttpSettings(
            service_token="explicit-local-token-that-is-long-enough",
            webhook_secret=WEBHOOK_SECRET,
        )
        with patch(
            "code_review_agent.service.HttpSettings.from_env",
            return_value=local_settings,
        ), patch("code_review_agent.service.uvicorn.run") as run:
            with self.assertRaisesRegex(SystemExit, "loopback"):
                service_main(["--host", "0.0.0.0"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
