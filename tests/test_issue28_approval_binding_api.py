import json
from pathlib import Path
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import text

from code_review_agent.approval_publish import FakePublisher
from code_review_agent.identity import FakeAuthBackend, Role
from code_review_agent.service import HttpSettings, create_app
from code_review_agent.service_core import (
    JobStore,
    RepositoryRegistry,
    ReviewRequest,
    ReviewService,
)


WEBHOOK_SECRET = "issue28-webhook-secret-value"


class FindingRunner:
    def __call__(self, request: ReviewRequest, trace_path: Path) -> dict:
        del request
        trace_path.write_text('{"trace":"redacted"}\n', encoding="utf-8")
        return {
            "summary": "issue28 review",
            "findings": [
                {
                    "fingerprint": "issue28-finding",
                    "path": "approval.py",
                    "line": 12,
                    "severity": "high",
                    "category": "correctness",
                    "message": "approval binding raw finding text must not leak",
                    "evidence": "line evidence",
                }
            ],
        }


class Issue28ApprovalBindingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        repo_root = self.root / "repo-a"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        self.registry = RepositoryRegistry.from_json(
            json.dumps({"owner/repo-a": str(repo_root.resolve())})
        )
        self.store = JobStore(self.root / "state")
        self.database = self.store.database
        self.org = self.database.create_organization("issue28-org", "Issue 28")
        self.repo = self.database.register_repository(
            self.org["id"], "owner/repo-a", policy_version="policy/issue28"
        )
        self.principals = {}
        for name, role in (
            ("viewer", Role.VIEWER),
            ("reviewer", Role.REVIEWER),
            ("maintainer", Role.MAINTAINER),
        ):
            member = self.database.create_membership(
                self.org["id"],
                subject=name,
                display_name=name,
                role=role,
                repository_ids=(self.repo["id"],),
            )
            principal = self.database.principal_for_user(self.org["id"], member["user_id"])
            self.assertIsNotNone(principal)
            self.principals[name] = principal
        self.publisher = FakePublisher()
        self.service = ReviewService(
            self.registry,
            self.store,
            runner=FindingRunner(),
            local_mode=False,
            publisher=self.publisher,
        )
        settings = HttpSettings(
            service_token="",
            webhook_secret=WEBHOOK_SECRET,
            allowed_origins=frozenset({"http://localhost"}),
            allowed_hosts=frozenset({"testserver"}),
            local_token_enabled=False,
        )
        self.client_context = TestClient(
            create_app(
                settings=settings,
                review_service=self.service,
                auth_backend=FakeAuthBackend(self.principals),
            )
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    @staticmethod
    def auth(name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {name}"}

    def _submit_pr(self, *, number: str = "28", head_sha: str = "a" * 40) -> str:
        response = self.client.post(
            "/v1/reviews/pr",
            headers=self.auth("reviewer"),
            json={
                "repository": "owner/repo-a",
                "pull_request": number,
                "head_sha": head_sha,
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["review_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = self.client.get(f"/v1/reviews/{job_id}", headers=self.auth("reviewer"))
            if current.status_code == 200 and current.json()["state"] == "awaiting_approval":
                return job_id
            time.sleep(0.01)
        self.fail("review did not reach awaiting_approval")

    def _proposal(self, job_id: str) -> dict:
        response = self.client.get(
            "/v1/reviews/pending-approval", headers=self.auth("maintainer")
        )
        self.assertEqual(response.status_code, 200, response.text)
        return next(
            item for item in response.json()["reviews"] if item["review_job_id"] == job_id
        )

    @staticmethod
    def _approval_payload(proposal: dict) -> dict:
        return {
            "payload_sha256": proposal["payload_sha256"],
            "nonce": proposal["nonce"],
        }

    def test_approval_api_exposes_binding_and_hides_consumed_nonce(self) -> None:
        job_id = self._submit_pr(number="28", head_sha="a" * 40)
        proposal = self._proposal(job_id)
        self.assertEqual(proposal["repository"], "owner/repo-a")
        self.assertEqual(proposal["pull_request"], "28")
        self.assertEqual(proposal["head_sha"], "a" * 40)
        self.assertEqual(len(proposal["payload_sha256"]), 64)
        self.assertEqual(len(proposal["finding_set_sha256"]), 64)
        self.assertEqual(len(proposal["nonce"]), 64)
        self.assertTrue(proposal["expires_at"])
        self.assertNotIn("approval binding raw finding text", json.dumps(proposal))

        for actor in ("viewer", "reviewer"):
            denied = self.client.post(
                f"/v1/reviews/{job_id}/approve",
                headers=self.auth(actor),
                json=self._approval_payload(proposal),
            )
            self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(self.publisher.calls, [])

        approved = self.client.post(
            f"/v1/reviews/{job_id}/approve",
            headers=self.auth("maintainer"),
            json=self._approval_payload(proposal),
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        approved_body = approved.json()
        self.assertEqual(approved_body["state"], "published")
        self.assertEqual(approved_body["repository"], "owner/repo-a")
        self.assertEqual(approved_body["pull_request"], "28")
        self.assertEqual(approved_body["approver_id"], self.principals["maintainer"].user_id)
        self.assertEqual(approved_body["finding_set_sha256"], proposal["finding_set_sha256"])
        self.assertEqual(approved_body["expires_at"], proposal["expires_at"])
        self.assertNotIn(proposal["nonce"], approved.text)

        approvals = self.client.get(
            f"/v1/reviews/{job_id}/approvals", headers=self.auth("viewer")
        )
        self.assertEqual(approvals.status_code, 200, approvals.text)
        approval = approvals.json()["approvals"][0]
        self.assertEqual(approval["repository"], "owner/repo-a")
        self.assertEqual(approval["pull_request"], "28")
        self.assertEqual(approval["approver_id"], self.principals["maintainer"].user_id)
        self.assertEqual(approval["finding_set_sha256"], proposal["finding_set_sha256"])
        self.assertEqual(approval["expires_at"], proposal["expires_at"])
        self.assertNotIn(proposal["nonce"], approvals.text)

        replay = self.client.post(
            f"/v1/reviews/{job_id}/approve",
            headers=self.auth("maintainer"),
            json=self._approval_payload(proposal),
        )
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(len(self.publisher.calls), 1)

    def test_stale_head_sha_invalidates_prior_nonce_binding(self) -> None:
        job_id = self._submit_pr(number="29", head_sha="b" * 40)
        proposal = self._proposal(job_id)
        with self.database.engine.begin() as connection:
            connection.execute(
                text("UPDATE review_jobs SET head_sha=:head WHERE id=:job"),
                {"head": "c" * 40, "job": job_id},
            )

        stale = self.client.post(
            f"/v1/reviews/{job_id}/approve",
            headers=self.auth("maintainer"),
            json=self._approval_payload(proposal),
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.publisher.calls, [])

        refreshed = self._proposal(job_id)
        self.assertEqual(refreshed["head_sha"], "c" * 40)
        self.assertNotEqual(refreshed["nonce"], proposal["nonce"])
        self.assertNotEqual(refreshed["payload_sha256"], proposal["payload_sha256"])


if __name__ == "__main__":
    unittest.main()
