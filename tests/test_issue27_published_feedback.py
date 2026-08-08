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


WEBHOOK_SECRET = "issue27-webhook-secret-value"


class FindingRunner:
    def __call__(self, request: ReviewRequest, trace_path: Path) -> dict:
        del request
        trace_path.write_text('{"trace":"redacted"}\n', encoding="utf-8")
        return {
            "summary": "issue27 review",
            "findings": [
                {
                    "fingerprint": "issue27-finding",
                    "path": "a.py",
                    "line": 7,
                    "severity": "high",
                    "category": "correctness",
                    "message": "published feedback binding must never leak this text",
                    "evidence": "source line evidence",
                }
            ],
        }


class Issue27PublishedFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        paths = {}
        for alias in ("owner/repo-a", "owner/repo-b"):
            root = self.root / alias.rsplit("/", 1)[1]
            root.mkdir()
            (root / ".git").mkdir()
            paths[alias] = str(root.resolve())
        self.registry = RepositoryRegistry.from_json(json.dumps(paths))
        self.store = JobStore(self.root / "state")
        self.database = self.store.database
        self.org_a = self.database.create_organization("issue27-a", "Issue 27 A")
        self.org_b = self.database.create_organization("issue27-b", "Issue 27 B")
        self.repo_a = self.database.register_repository(
            self.org_a["id"], "owner/repo-a", policy_version="policy/a"
        )
        self.repo_b = self.database.register_repository(
            self.org_b["id"], "owner/repo-b", policy_version="policy/b"
        )
        self.principals = {}
        for name, organization, role, repository_ids in (
            ("viewer_a", self.org_a, Role.VIEWER, (self.repo_a["id"],)),
            ("reviewer_a", self.org_a, Role.REVIEWER, (self.repo_a["id"],)),
            ("maintainer_a", self.org_a, Role.MAINTAINER, (self.repo_a["id"],)),
            ("reviewer_b", self.org_b, Role.REVIEWER, (self.repo_b["id"],)),
            ("maintainer_b", self.org_b, Role.MAINTAINER, (self.repo_b["id"],)),
        ):
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
        auth = {
            name.replace("_", "-"): principal
            for name, principal in self.principals.items()
        }
        self.client_context = TestClient(
            create_app(
                settings=settings,
                review_service=self.service,
                auth_backend=FakeAuthBackend(auth),
            )
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    @staticmethod
    def auth(name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {name.replace('_', '-')}"}

    def _submit_pr(
        self,
        *,
        actor: str = "reviewer_a",
        repository: str = "owner/repo-a",
        number: str = "27",
        head_sha: str = "a" * 40,
    ) -> tuple[str, dict]:
        response = self.client.post(
            "/v1/reviews/pr",
            headers=self.auth(actor),
            json={
                "repository": repository,
                "pull_request": number,
                "head_sha": head_sha,
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["review_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = self.client.get(f"/v1/reviews/{job_id}", headers=self.auth(actor))
            if current.status_code == 200 and current.json()["state"] == "awaiting_approval":
                findings = self.client.get(
                    f"/v1/reviews/{job_id}/findings", headers=self.auth(actor)
                )
                self.assertEqual(findings.status_code, 200, findings.text)
                return job_id, findings.json()["findings"][0]
            time.sleep(0.01)
        self.fail("review did not reach awaiting_approval")

    def _proposal(self, job_id: str, *, actor: str = "maintainer_a") -> dict:
        response = self.client.get(
            "/v1/reviews/pending-approval", headers=self.auth(actor)
        )
        self.assertEqual(response.status_code, 200, response.text)
        return next(
            item for item in response.json()["reviews"] if item["review_job_id"] == job_id
        )

    def _publish(self, job_id: str, *, actor: str = "maintainer_a") -> dict:
        proposal = self._proposal(job_id, actor=actor)
        approved = self.client.post(
            f"/v1/reviews/{job_id}/approve",
            headers=self.auth(actor),
            json={
                "payload_sha256": proposal["payload_sha256"],
                "nonce": proposal["nonce"],
            },
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["state"], "published")
        return approved.json()

    def test_feedback_requires_publication_then_persists_hash_only_binding(self) -> None:
        job_id, finding = self._submit_pr()
        unpublished = self.client.post(
            f"/v1/findings/{finding['id']}/feedback",
            headers=self.auth("reviewer_a"),
            json={
                "decision": "accepted",
                "finding_hash": finding["content_sha256"],
                "reason": "actionable",
            },
        )
        self.assertEqual(unpublished.status_code, 409, unpublished.text)
        self.assertEqual(unpublished.json()["error"]["code"], "feedback_conflict")

        published = self._publish(job_id)
        detail = self.client.get(
            f"/v1/findings/{finding['id']}", headers=self.auth("viewer_a")
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["status"], "published")
        feedback = self.client.post(
            f"/v1/findings/{finding['id']}/feedback",
            headers=self.auth("reviewer_a"),
            json={
                "decision": "accepted",
                "finding_hash": detail.json()["content_sha256"],
                "reason": "actionable",
            },
        )
        self.assertEqual(feedback.status_code, 201, feedback.text)
        body = feedback.json()
        self.assertEqual(body["finding_hash"], detail.json()["content_sha256"])
        self.assertEqual(body["publish_approval_id"], published["approval_id"])
        self.assertEqual(body["published_payload_sha256"], published["payload_sha256"])
        self.assertEqual(body["published_head_sha"], published["head_sha"])
        self.assertEqual(len(body["published_finding_sha256"]), 64)
        self.assertNotIn("published feedback binding must never leak this text", feedback.text)

        listed = self.client.get(
            f"/v1/findings/{finding['id']}/feedback", headers=self.auth("viewer_a")
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            listed.json()["feedback"][0]["published_finding_sha256"],
            body["published_finding_sha256"],
        )
        with self.database.engine.connect() as connection:
            metric = connection.execute(
                text(
                    "SELECT subject_sha256 FROM metric_events "
                    "WHERE event_type='finding.feedback.accepted'"
                )
            ).scalar_one()
        self.assertEqual(metric, detail.json()["content_sha256"])

    def test_stale_finding_hash_is_rejected_without_recording_feedback(self) -> None:
        job_id, finding = self._submit_pr(head_sha="b" * 40)
        self._publish(job_id)
        with self.database.engine.begin() as connection:
            connection.execute(
                text("UPDATE findings SET content_sha256=:hash WHERE id=:id"),
                {"hash": "c" * 64, "id": finding["id"]},
            )
        response = self.client.post(
            f"/v1/findings/{finding['id']}/feedback",
            headers=self.auth("reviewer_a"),
            json={
                "decision": "rejected",
                "finding_hash": finding["content_sha256"],
                "reason": "version drift",
            },
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "feedback_conflict")
        with self.database.engine.connect() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM finding_feedback WHERE finding_id=:id"),
                {"id": finding["id"]},
            ).scalar_one()
        self.assertEqual(count, 0)

    def test_cross_tenant_feedback_to_published_finding_is_not_found(self) -> None:
        job_id, finding = self._submit_pr(
            actor="reviewer_b",
            repository="owner/repo-b",
            number="28",
            head_sha="d" * 40,
        )
        self._publish(job_id, actor="maintainer_b")
        response = self.client.post(
            f"/v1/findings/{finding['id']}/feedback",
            headers=self.auth("reviewer_a"),
            json={
                "decision": "accepted",
                "finding_hash": finding["content_sha256"],
                "reason": "wrong tenant",
            },
        )
        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
