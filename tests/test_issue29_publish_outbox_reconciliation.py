from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

from alembic import command
from sqlalchemy import inspect, text

from code_review_agent.approval_publish import FakePublisher, PublishReceipt, PublishRequest
from code_review_agent.database import (
    _alembic_config,
    create_database_engine,
    current_revision,
    upgrade_database,
)
from code_review_agent.identity import Role
from code_review_agent.service_core import (
    JobStore,
    PublisherFailed,
    RepositoryRegistry,
    ReviewRequest,
    ReviewService,
)


class FindingRunner:
    def __call__(self, request: ReviewRequest, trace_path: Path) -> dict:
        del request
        trace_path.write_text('{"trace":"issue29"}\n', encoding="utf-8")
        return {
            "summary": "issue29 bounded review",
            "findings": [
                {
                    "fingerprint": "issue29-finding",
                    "path": "a.py",
                    "line": 1,
                    "severity": "high",
                    "category": "correctness",
                    "message": "bounded issue29 finding",
                    "evidence": "line appears in the changed source",
                }
            ],
        }


class CrashAfterPersistPublisher(FakePublisher):
    def __init__(self) -> None:
        super().__init__()
        self.crash_once = True

    def publish(self, request: PublishRequest) -> PublishReceipt:
        receipt = super().publish(request)
        if self.crash_once:
            self.crash_once = False
            raise SystemExit("synthetic publisher crash")
        return receipt


class AmbiguousWithoutReceiptPublisher(FakePublisher):
    def publish(self, request: PublishRequest) -> PublishReceipt:
        self.calls.append(request.idempotency_key)
        raise TimeoutError("synthetic publisher timeout")


class Issue29PublishOutboxReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo_root = self.root / "repo"
        self.repo_root.mkdir()
        (self.repo_root / ".git").mkdir()
        self.registry = RepositoryRegistry.from_json(
            json.dumps({"owner/repo": str(self.repo_root.resolve())})
        )
        self.store = JobStore(self.root / "state")
        self.database = self.store.database
        self.organization = self.database.create_organization("issue29", "Issue 29")
        self.repository = self.database.register_repository(
            self.organization["id"], "owner/repo", policy_version="policy/issue29"
        )
        self.principals = {}
        for name, role, repository_ids in (
            ("reviewer", Role.REVIEWER, (self.repository["id"],)),
            ("maintainer", Role.MAINTAINER, (self.repository["id"],)),
            ("admin", Role.ORG_ADMIN, ()),
        ):
            member = self.database.create_membership(
                self.organization["id"],
                subject=name,
                display_name=name,
                role=role,
                repository_ids=repository_ids,
            )
            principal = self.database.principal_for_user(
                self.organization["id"], member["user_id"]
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

    def tearDown(self) -> None:
        self.service.shutdown()
        self.temp.cleanup()

    def _submit_pr(self, number: str = "29", head_sha: str = "a" * 40) -> str:
        review, duplicate = self.service.submit_pr(
            "owner/repo",
            number,
            principal=self.principals["reviewer"],
            head_sha=head_sha,
        )
        self.assertFalse(duplicate)
        job_id = str(review["review_id"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = self.service.get(job_id, principal=self.principals["reviewer"])
            if current["state"] == "awaiting_approval":
                self.assertEqual(
                    len(
                        self.service.list_findings(
                            job_id, principal=self.principals["reviewer"]
                        )
                    ),
                    1,
                )
                return job_id
            time.sleep(0.01)
        self.fail("review did not reach awaiting_approval")

    def _proposal(self, job_id: str) -> dict:
        proposals = self.service.list_pending_approvals(
            principal=self.principals["maintainer"]
        )
        proposal = next(item for item in proposals if item["review_job_id"] == job_id)
        self.assertEqual(len(proposal["payload_sha256"]), 64)
        self.assertEqual(len(proposal["nonce"]), 64)
        return proposal

    def _attempt(self, job_id: str) -> dict:
        with self.database.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT status, receipt_sha256, error_code, idempotency_key, "
                    "publishing_started_at, reconcile_after "
                    "FROM publish_attempts WHERE review_job_id=:job"
                ),
                {"job": job_id},
            ).one()
        return dict(row._mapping)

    def _expire_reconciliation_grace(self, job_id: str) -> None:
        with self.database.engine.begin() as connection:
            updated = connection.execute(
                text(
                    "UPDATE publish_attempts SET reconcile_after='1970-01-01T00:00:00Z' "
                    "WHERE review_job_id=:job AND status='publishing'"
                ),
                {"job": job_id},
            )
        self.assertEqual(updated.rowcount, 1)

    def _job_state(self, job_id: str) -> str:
        with self.database.engine.connect() as connection:
            return str(
                connection.execute(
                    text("SELECT state FROM review_jobs WHERE id=:job"), {"job": job_id}
                ).scalar_one()
            )

    def _restart_with_publisher(self, publisher: FakePublisher) -> ReviewService:
        self.service.shutdown()
        self.store = JobStore(self.root / "state")
        self.database = self.store.database
        self.publisher = publisher
        self.service = ReviewService(
            self.registry,
            self.store,
            runner=None,
            local_mode=False,
            publisher=publisher,
        )
        return self.service

    def test_prepared_outbox_is_published_once_after_restart(self) -> None:
        job_id = self._submit_pr()
        proposal = self._proposal(job_id)
        self.database.decide_publish_review(
            self.principals["maintainer"],
            job_id,
            decision="approved",
            payload_sha256=proposal["payload_sha256"],
            nonce=proposal["nonce"],
        )
        self.assertEqual(self._attempt(job_id)["status"], "prepared")

        restarted = self._restart_with_publisher(self.publisher)
        reconciled = restarted.reconcile_publish_outbox(principal=self.principals["admin"])

        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["state"], "published")
        self.assertEqual(len(self.publisher.calls), 1)
        self.assertEqual(self.publisher.lookup_calls, [])
        self.assertEqual(self._attempt(job_id)["status"], "succeeded")
        self.assertEqual(self._job_state(job_id), "published")

        self.assertEqual(
            restarted.reconcile_publish_outbox(principal=self.principals["admin"]), []
        )
        self.assertEqual(len(self.publisher.calls), 1)

    def test_crash_after_remote_success_reconciles_without_duplicate_write(self) -> None:
        self.publisher = CrashAfterPersistPublisher()
        self.service.publisher = self.publisher
        job_id = self._submit_pr()
        proposal = self._proposal(job_id)

        with self.assertRaises(SystemExit):
            self.service.decide_review_publication(
                job_id,
                decision="approved",
                payload_sha256=proposal["payload_sha256"],
                nonce=proposal["nonce"],
                principal=self.principals["maintainer"],
            )
        attempt = self._attempt(job_id)
        self.assertEqual(attempt["status"], "publishing")
        self.assertEqual(self._job_state(job_id), "approved")
        self.assertEqual(self.publisher.calls, [attempt["idempotency_key"]])
        self.assertIsNotNone(attempt["publishing_started_at"])
        self.assertIsNotNone(attempt["reconcile_after"])

        restarted = self._restart_with_publisher(self.publisher)
        self.assertEqual(
            restarted.reconcile_publish_outbox(principal=self.principals["admin"]), []
        )
        self.assertEqual(self.publisher.calls, [attempt["idempotency_key"]])
        self.assertEqual(self.publisher.lookup_calls, [])

        self._expire_reconciliation_grace(job_id)
        reconciled = restarted.reconcile_publish_outbox(principal=self.principals["admin"])

        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["state"], "published")
        self.assertEqual(self.publisher.calls, [attempt["idempotency_key"]])
        self.assertEqual(self.publisher.lookup_calls, [attempt["idempotency_key"]])
        completed = self._attempt(job_id)
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(len(completed["receipt_sha256"]), 64)
        self.assertIsNone(completed["error_code"])

    def test_ambiguous_outcome_without_receipt_quarantines(self) -> None:
        self.publisher = AmbiguousWithoutReceiptPublisher()
        self.service.publisher = self.publisher
        job_id = self._submit_pr()
        proposal = self._proposal(job_id)

        with self.assertRaises(PublisherFailed):
            self.service.decide_review_publication(
                job_id,
                decision="approved",
                payload_sha256=proposal["payload_sha256"],
                nonce=proposal["nonce"],
                principal=self.principals["maintainer"],
            )

        attempt = self._attempt(job_id)
        self.assertEqual(attempt["status"], "quarantined")
        self.assertEqual(attempt["error_code"], "publisher_ambiguous")
        self.assertEqual(self._job_state(job_id), "failed")
        self.assertEqual(self.publisher.calls, [attempt["idempotency_key"]])
        self.assertEqual(self.publisher.lookup_calls, [attempt["idempotency_key"]])

        restarted = self._restart_with_publisher(self.publisher)
        self.assertEqual(
            restarted.reconcile_publish_outbox(principal=self.principals["admin"]), []
        )
        self.assertEqual(self.publisher.calls, [attempt["idempotency_key"]])

    def test_downgrade_maps_inflight_attempt_to_terminal_failure(self) -> None:
        job_id = self._submit_pr()
        proposal = self._proposal(job_id)
        record = self.database.decide_publish_review(
            self.principals["maintainer"],
            job_id,
            decision="approved",
            payload_sha256=proposal["payload_sha256"],
            nonce=proposal["nonce"],
        )
        publish = record["_publish"]
        self.database.start_publish_attempt(
            approval_id=str(record["approval_id"]),
            review_job_id=job_id,
            attempt_id=str(publish["attempt_id"]),
        )
        self.assertEqual(self._attempt(job_id)["status"], "publishing")

        self.service.shutdown()
        database_url = self.database.engine.url.render_as_string(hide_password=True)
        self.database.engine.dispose()
        command.downgrade(_alembic_config(database_url), "0008_phase11b_github_canary")
        self.assertEqual(current_revision(database_url), "0008_phase11b_github_canary")

        downgraded = create_database_engine(database_url)
        try:
            with downgraded.connect() as connection:
                attempt = connection.execute(
                    text(
                        "SELECT status, error_code, completed_at FROM publish_attempts "
                        "WHERE review_job_id=:job"
                    ),
                    {"job": job_id},
                ).one()
            self.assertEqual(attempt.status, "failed")
            self.assertEqual(attempt.error_code, "publisher_ambiguous")
            self.assertIsNotNone(attempt.completed_at)
            columns = {column["name"] for column in inspect(downgraded).get_columns("publish_attempts")}
            self.assertNotIn("publishing_started_at", columns)
            self.assertNotIn("reconcile_after", columns)
        finally:
            downgraded.dispose()
        upgrade_database(database_url)


if __name__ == "__main__":
    unittest.main()
