import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import text

from code_review_agent.approval_publish import FakePublisher
from code_review_agent.identity import FakeAuthBackend, Role
from code_review_agent.service import HttpSettings, create_app
from code_review_agent.service_core import (
    ApprovalConflict,
    JobStore,
    RepositoryRegistry,
    ReviewRequest,
    ReviewService,
)


WEBHOOK_SECRET = "phase9d-webhook-secret-value"


class FindingRunner:
    def __call__(self, request: ReviewRequest, trace_path: Path) -> dict:
        del request
        trace_path.write_text('{"trace":"redacted"}\n', encoding="utf-8")
        return {
            "summary": "bounded test review",
            "findings": [
                {
                    "fingerprint": "phase9d-finding",
                    "path": "a.py",
                    "line": 1,
                    "severity": "high",
                    "category": "correctness",
                    "message": "bounded test finding",
                    "evidence": "line appears in the changed source",
                }
            ],
        }


class Phase9DApprovalFeedbackTests(unittest.TestCase):
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
        self.org_a = self.database.create_organization("phase9d-a", "Phase 9D A")
        self.org_b = self.database.create_organization("phase9d-b", "Phase 9D B")
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
            ("admin_a", self.org_a, Role.ORG_ADMIN, ()),
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
        auth = {
            name.replace("_", "-"): principal
            for name, principal in self.principals.items()
        }
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

    def _submit_pr(self, number: str = "17", head_sha: str = "a" * 40) -> tuple[str, str]:
        response = self.client.post(
            "/v1/reviews/pr",
            headers=self.auth("reviewer_a"),
            json={"repository": "owner/repo-a", "pull_request": number, "head_sha": head_sha},
        )
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["review_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = self.client.get(
                f"/v1/reviews/{job_id}", headers=self.auth("reviewer_a")
            )
            if current.status_code == 200 and current.json()["state"] == "awaiting_approval":
                findings = self.client.get(
                    f"/v1/reviews/{job_id}/findings", headers=self.auth("reviewer_a")
                )
                self.assertEqual(findings.status_code, 200, findings.text)
                return job_id, findings.json()["findings"][0]["id"]
            time.sleep(0.01)
        self.fail("review did not reach awaiting_approval")

    def _proposal(self, job_id: str, actor: str = "maintainer_a") -> dict:
        response = self.client.get(
            "/v1/reviews/pending-approval", headers=self.auth(actor)
        )
        self.assertEqual(response.status_code, 200, response.text)
        proposal = next(
            item for item in response.json()["reviews"] if item["review_job_id"] == job_id
        )
        self.assertEqual(len(proposal["payload_sha256"]), 64)
        self.assertEqual(len(proposal["nonce"]), 64)
        return proposal

    @staticmethod
    def _decision_payload(proposal: dict) -> dict:
        return {
            "payload_sha256": proposal["payload_sha256"],
            "nonce": proposal["nonce"],
        }

    def test_guarded_approval_uses_exact_binding_and_publishes_once(self) -> None:
        job_id, finding_id = self._submit_pr()
        proposal = self._proposal(job_id)
        self.assertEqual(self.publisher.calls, [])

        for actor in ("viewer_a", "reviewer_a"):
            denied = self.client.post(
                f"/v1/reviews/{job_id}/approve",
                headers=self.auth(actor),
                json=self._decision_payload(proposal),
            )
            self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(self.publisher.calls, [])

        approved = self.client.post(
            f"/v1/reviews/{job_id}/approve",
            headers=self.auth("admin_a"),
            json=self._decision_payload(proposal),
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["state"], "published")
        self.assertEqual(len(self.publisher.calls), 1)

        replay = self.client.post(
            f"/v1/reviews/{job_id}/approve",
            headers=self.auth("maintainer_a"),
            json=self._decision_payload(proposal),
        )
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(len(self.publisher.calls), 1)

        approvals = self.client.get(
            f"/v1/reviews/{job_id}/approvals", headers=self.auth("viewer_a")
        )
        self.assertEqual(approvals.status_code, 200, approvals.text)
        self.assertEqual(approvals.json()["approvals"][0]["used_at"], approved.json()["used_at"])
        self.assertNotIn(proposal["nonce"], approvals.text)

        finding_detail = self.client.get(
            f"/v1/findings/{finding_id}", headers=self.auth("viewer_a")
        )
        self.assertEqual(finding_detail.status_code, 200, finding_detail.text)
        feedback = self.client.post(
            f"/v1/findings/{finding_id}/feedback",
            headers=self.auth("reviewer_a"),
            json={
                "decision": "fixed",
                "finding_hash": finding_detail.json()["content_sha256"],
                "rationale": "verified in a follow-up commit",
            },
        )
        self.assertEqual(feedback.status_code, 201, feedback.text)
        self.assertEqual(feedback.json()["principal_id"], self.principals["reviewer_a"].user_id)
        self.assertEqual(len(feedback.json()["finding_hash"]), 64)
        feedback_audit = self.client.get(
            f"/v1/findings/{finding_id}/feedback", headers=self.auth("viewer_a")
        )
        self.assertEqual(feedback_audit.status_code, 200, feedback_audit.text)
        self.assertEqual(feedback_audit.json()["feedback"][0]["decision"], "fixed")

        trace = self.client.get(
            f"/v1/reviews/{job_id}/trace", headers=self.auth("viewer_a")
        )
        self.assertEqual(trace.status_code, 200, trace.text)
        self.assertNotIn(proposal["nonce"], trace.text)
        self.assertNotIn("bounded test finding", trace.text)

    def test_payload_change_expiry_and_cross_organization_are_rejected(self) -> None:
        job_id, finding_id = self._submit_pr()
        proposal = self._proposal(job_id)
        with self.database.engine.begin() as connection:
            connection.execute(
                text("UPDATE findings SET content_sha256=:hash WHERE id=:id"),
                {"hash": "b" * 64, "id": finding_id},
            )
        changed = self.client.post(
            f"/v1/reviews/{job_id}/approve",
            headers=self.auth("maintainer_a"),
            json=self._decision_payload(proposal),
        )
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(self.publisher.calls, [])

        current = self._proposal(job_id)
        with self.database.engine.begin() as connection:
            connection.execute(
                text("UPDATE publish_proposals SET expires_at='2000-01-01T00:00:00Z' WHERE nonce=:nonce"),
                {"nonce": current["nonce"]},
            )
        expired = self.client.post(
            f"/v1/reviews/{job_id}/approve",
            headers=self.auth("maintainer_a"),
            json=self._decision_payload(current),
        )
        self.assertEqual(expired.status_code, 409, expired.text)
        cross_org = self.client.post(
            f"/v1/reviews/{job_id}/approve",
            headers=self.auth("maintainer_b"),
            json=self._decision_payload(current),
        )
        self.assertEqual(cross_org.status_code, 404, cross_org.text)
        self.assertEqual(self.publisher.calls, [])

    def test_timeout_receipt_reconciliation_and_concurrent_approval_do_not_duplicate(self) -> None:
        job_id, _ = self._submit_pr()
        proposal = self._proposal(job_id)
        self.publisher.timeout_after_persist = True
        response = self.client.post(
            f"/v1/reviews/{job_id}/approve",
            headers=self.auth("maintainer_a"),
            json=self._decision_payload(proposal),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "published")
        self.assertEqual(len(self.publisher.calls), 1)

        second_job, _ = self._submit_pr("18", "b" * 40)
        second = self._proposal(second_job)
        self.publisher.timeout_after_persist = False
        barrier = threading.Barrier(2)
        results: list[str] = []

        def approve() -> None:
            barrier.wait()
            try:
                self.service.decide_review_publication(
                    second_job,
                    decision="approved",
                    payload_sha256=second["payload_sha256"],
                    nonce=second["nonce"],
                    principal=self.principals["maintainer_a"],
                )
                results.append("approved")
            except ApprovalConflict:
                results.append("conflict")

        threads = [threading.Thread(target=approve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(sorted(results), ["approved", "conflict"])
        self.assertEqual(len(self.publisher.calls), 2)

    def test_pending_approval_survives_restart_and_reject_calls_no_publisher(self) -> None:
        job_id, _ = self._submit_pr()
        proposal = self._proposal(job_id)
        self.service.shutdown()
        restarted_store = JobStore(self.root / "state")
        restarted_service = ReviewService(
            self.registry,
            restarted_store,
            runner=FindingRunner(),
            local_mode=False,
            publisher=self.publisher,
        )
        try:
            restored = restarted_service.list_pending_approvals(
                principal=self.principals["maintainer_a"]
            )
            restored_proposal = next(
                item for item in restored if item["review_job_id"] == job_id
            )
            self.assertEqual(restored_proposal["nonce"], proposal["nonce"])
            rejected = restarted_service.decide_review_publication(
                job_id,
                decision="rejected",
                payload_sha256=restored_proposal["payload_sha256"],
                nonce=restored_proposal["nonce"],
                principal=self.principals["maintainer_a"],
            )
            self.assertEqual(rejected["decision"], "rejected")
            self.assertEqual(self.publisher.calls, [])
        finally:
            restarted_service.shutdown()


if __name__ == "__main__":
    unittest.main()
