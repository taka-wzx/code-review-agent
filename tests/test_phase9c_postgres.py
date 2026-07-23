from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from sqlalchemy import text

from code_review_agent.database import upgrade_database
from code_review_agent.service_core import LeaseLost, RepositoryRegistry, ReviewService
from code_review_agent.service_queue import JobStore
from code_review_agent.worker import classify_failure

from tests.test_phase9c_durable_service import diff_for


POSTGRES_URL = os.environ.get("CRAG_TEST_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "CRAG_TEST_POSTGRES_URL is not configured")
class Phase9CPostgresTests(unittest.TestCase):
    def setUp(self):
        assert POSTGRES_URL is not None
        if os.environ.get("CRAG_TEST_POSTGRES_RESET") != "1":
            self.skipTest("CRAG_TEST_POSTGRES_RESET=1 is required for destructive isolation")
        upgrade_database(POSTGRES_URL)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        self.registry = RepositoryRegistry.from_json(
            json.dumps({"owner/postgres": str(repo.resolve())})
        )
        bootstrap = JobStore(
            self.root / "state",
            database_url=POSTGRES_URL,
            auto_migrate=False,
        )
        with bootstrap.database.engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE submission_events, worker_instances, "
                    "review_idempotency_keys, service_quotas, "
                    "provider_usage, webhook_deliveries, audit_events, approvals, "
                    "finding_feedback, findings, review_sessions, review_jobs, "
                    "access_credentials, repository_access, memberships, users, "
                    "repositories, organizations RESTART IDENTITY CASCADE"
                )
            )
        bootstrap.bootstrap_local(("owner/postgres",))
        self.store = bootstrap
        self.service = ReviewService(self.registry, self.store, local_mode=True)
        self.extra_stores: list[JobStore] = []

    def tearDown(self):
        for store in reversed(self.extra_stores):
            store.close()
        self.service.shutdown()
        self.temp.cleanup()

    def test_fifty_concurrent_durable_submissions_and_replay(self):
        start = threading.Event()

        def submit(index: int):
            start.wait()
            return self.service.submit_diff(
                "owner/postgres",
                diff_for(index),
                idempotency_key=f"pg-{index}",
            )

        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = [executor.submit(submit, index) for index in range(50)]
            start.set()
            first = [future.result() for future in futures]
        self.assertEqual(len({job["review_id"] for job in first}), 50)
        start.clear()
        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = [executor.submit(submit, index) for index in range(50)]
            start.set()
            replay = [future.result() for future in futures]
        self.assertTrue(all(job["duplicate"] for job in replay))
        with self.store.database.engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SELECT COUNT(*) FROM review_jobs")).scalar_one(),
                50,
            )
            self.assertEqual(
                connection.execute(text("SELECT COUNT(*) FROM submission_events")).scalar_one(),
                50,
            )

    def test_concurrent_webhook_replay_has_one_delivery_and_logical_job(self):
        def submit(_: int):
            return self.service.submit_webhook_pr(
                "owner/postgres",
                "42",
                delivery_id="postgres-delivery",
                head_sha="b" * 40,
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(submit, range(24)))
        self.assertEqual(
            len({record[0]["review_id"] for record in results}), 1
        )
        self.assertEqual(sum(1 for _, duplicate in results if not duplicate), 1)
        with self.store.database.engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SELECT COUNT(*) FROM review_jobs")).scalar_one(),
                1,
            )
            self.assertEqual(
                connection.execute(
                    text("SELECT COUNT(*) FROM webhook_deliveries")
                ).scalar_one(),
                1,
            )

    def test_skip_locked_claim_and_lease_recovery_are_fenced(self):
        submitted = self.service.submit_diff("owner/postgres", diff_for(999))
        second = JobStore(
            self.root / "state",
            database_url=POSTGRES_URL,
            auto_migrate=False,
        )
        self.extra_stores.append(second)
        barrier = threading.Barrier(2)
        leases = []
        lock = threading.Lock()

        def claim(store: JobStore, worker: str):
            barrier.wait()
            lease = store.claim(worker, lease_seconds=2)
            with lock:
                leases.append(lease)

        threads = [
            threading.Thread(target=claim, args=(self.store, "pg-worker-a")),
            threading.Thread(target=claim, args=(second, "pg-worker-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        claimed = [lease for lease in leases if lease is not None]
        self.assertEqual(len(claimed), 1)
        first = claimed[0]
        self.assertEqual(first.job_id, submitted["review_id"])
        self.store.mark_running(first)

        self.assertIsNone(
            second.claim(
                "clock-skewed-worker",
                lease_seconds=10,
                now=datetime.now(timezone.utc) + timedelta(days=1),
            )
        )
        with self.store.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE review_jobs SET lease_expires_at="
                    "clock_timestamp() - INTERVAL '1 second' WHERE id=:job"
                ),
                {"job": first.job_id},
            )

        recovered = second.claim(
            "pg-worker-recovery",
            lease_seconds=10,
        )
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.job_id, first.job_id)
        self.assertNotEqual(recovered.lease_token, first.lease_token)
        self.store.trace_path_for_lease(first).write_text(
            '{"trace":"stale"}\n', encoding="utf-8"
        )
        with self.assertRaises(LeaseLost):
            self.store.complete(
                first,
                {"summary": "stale", "findings": []},
                trace_key=self.store.trace_path_for_lease(first).name,
            )

    def test_heartbeat_holds_quota_admission_before_waiting_for_job(self):
        first_job = self.service.submit_diff("owner/postgres", diff_for(2001))
        self.service.submit_diff("owner/postgres", diff_for(2002))
        organization_id = first_job["organization_id"]
        repository_id = first_job["repository_id"]
        self.store.configure_quota(organization_id, max_concurrent_jobs=1)
        self.store.configure_quota(
            organization_id,
            repository_id=repository_id,
            max_concurrent_jobs=1,
        )
        lease = self.store.claim("heartbeat-owner", lease_seconds=30)
        self.assertIsNotNone(lease)
        assert lease is not None
        self.store.mark_running(lease)

        second = JobStore(
            self.root / "state",
            database_url=POSTGRES_URL,
            auto_migrate=False,
        )
        self.extra_stores.append(second)
        blocker = self.store.database.engine.connect()
        blocking_transaction = blocker.begin()
        blocker.execute(
            text("SELECT id FROM review_jobs WHERE id=:job FOR UPDATE"),
            {"job": lease.job_id},
        ).one()

        heartbeat_entered_job_lock = threading.Event()
        claim_finished = threading.Event()
        errors: list[BaseException] = []
        claimed = []
        original_locked_job = self.store._locked_job

        def observed_locked_job(connection, job_id, *, skip_locked=False):
            heartbeat_entered_job_lock.set()
            return original_locked_job(connection, job_id, skip_locked=skip_locked)

        def renew() -> None:
            try:
                self.store.heartbeat(lease, lease_seconds=30)
            except BaseException as exc:
                errors.append(exc)

        def claim_waiting_job() -> None:
            try:
                claimed.append(second.claim("quota-waiter", lease_seconds=30))
            except BaseException as exc:
                errors.append(exc)
            finally:
                claim_finished.set()

        heartbeat_thread = threading.Thread(target=renew)
        claim_thread = threading.Thread(target=claim_waiting_job)
        heartbeat_reached_lock = False
        claim_completed_early = False
        try:
            with patch.object(
                self.store, "_locked_job", side_effect=observed_locked_job
            ):
                heartbeat_thread.start()
                heartbeat_reached_lock = heartbeat_entered_job_lock.wait(5)
                if heartbeat_reached_lock:
                    claim_thread.start()
                    claim_completed_early = claim_finished.wait(0.25)
                blocking_transaction.commit()
                heartbeat_thread.join(5)
                if claim_thread.ident is not None:
                    claim_thread.join(5)
        finally:
            if blocking_transaction.is_active:
                blocking_transaction.rollback()
            blocker.close()
            heartbeat_thread.join(5)
            if claim_thread.ident is not None:
                claim_thread.join(5)

        self.assertTrue(heartbeat_reached_lock)
        self.assertFalse(claim_completed_early)
        self.assertFalse(heartbeat_thread.is_alive())
        self.assertFalse(claim_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(claimed, [None])

    def test_concurrent_received_finalizers_are_idempotent(self):
        submitted = self.service.submit_diff("owner/postgres", diff_for(2100))
        job_id = submitted["review_id"]
        payload_key = f"{job_id}.diff"
        second = JobStore(
            self.root / "state",
            database_url=POSTGRES_URL,
            auto_migrate=False,
        )
        self.extra_stores.append(second)
        with self.store.database.engine.begin() as connection:
            connection.execute(
                text("UPDATE review_jobs SET state='received' WHERE id=:job"),
                {"job": job_id},
            )

        blocker = self.store.database.engine.connect()
        blocking_transaction = blocker.begin()
        blocker.execute(
            text("SELECT id FROM review_jobs WHERE id=:job FOR UPDATE"),
            {"job": job_id},
        ).one()
        start = threading.Barrier(3)
        errors: list[BaseException] = []
        error_lock = threading.Lock()

        def finalize(store: JobStore) -> None:
            start.wait()
            try:
                store.finalize_received(job_id, payload_key=payload_key)
            except BaseException as exc:
                with error_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=finalize, args=(self.store,)),
            threading.Thread(target=finalize, args=(second,)),
        ]
        try:
            for thread in threads:
                thread.start()
            start.wait()
            time.sleep(0.25)
            blocking_transaction.commit()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
        finally:
            if blocking_transaction.is_active:
                blocking_transaction.rollback()
            blocker.close()

        self.assertEqual(errors, [])
        self.assertEqual(self.store.get(job_id)["state"], "queued")

    def test_retry_delay_uses_database_clock(self):
        submitted = self.service.submit_diff("owner/postgres", diff_for(2200))
        lease = self.store.claim("retry-clock", lease_seconds=30)
        self.assertIsNotNone(lease)
        assert lease is not None
        self.store.mark_running(lease)
        trace = self.store.trace_path_for_lease(lease)
        trace.write_text('{"trace":"retry-clock"}\n', encoding="utf-8")
        self.store.finish_failure(
            lease,
            "rate_limit",
            retryable=True,
            trace_key=trace.name,
            usage={"llm_calls": 0},
            delay_seconds=60,
            now=datetime.now(timezone.utc) + timedelta(days=1),
        )
        with self.store.database.engine.connect() as connection:
            window = connection.execute(
                text(
                    "SELECT available_at>clock_timestamp() AS future, "
                    "available_at<clock_timestamp()+INTERVAL '90 seconds' AS bounded "
                    "FROM review_jobs WHERE id=:job"
                ),
                {"job": submitted["review_id"]},
            ).one()
        self.assertTrue(window._mapping["future"])
        self.assertTrue(window._mapping["bounded"])
        self.assertIsNone(
            self.store.claim(
                "skewed-retry-claimer",
                lease_seconds=30,
                now=datetime.now(timezone.utc) + timedelta(days=2),
            )
        )

    def test_absolute_rate_reset_is_independent_of_worker_clock_skew(self):
        target = (
            datetime.now(timezone.utc) + timedelta(seconds=60)
        ).replace(microsecond=0)

        class RateLimitError(Exception):
            status_code = 429
            headers = {
                "Retry-After": target.strftime("%a, %d %b %Y %H:%M:%S GMT")
            }

        decision = classify_failure(
            RateLimitError(),
            job_id="d" * 32,
            attempt_count=1,
            now=datetime.now(timezone.utc) + timedelta(days=1),
        )
        self.assertEqual(decision.not_before, target)

        submitted = self.service.submit_diff("owner/postgres", diff_for(2250))
        lease = self.store.claim("absolute-reset", lease_seconds=30)
        self.assertIsNotNone(lease)
        assert lease is not None
        self.store.mark_running(lease)
        outcome = self.store.finish_failure(
            lease,
            decision.category,
            retryable=decision.retryable,
            trace_key=None,
            usage={"llm_calls": 0},
            delay_seconds=decision.delay_seconds,
            available_at=decision.not_before,
            now=datetime.now(timezone.utc) + timedelta(days=1),
        )
        self.assertTrue(outcome.retry_scheduled)
        with self.store.database.engine.connect() as connection:
            aligned = connection.execute(
                text(
                    "SELECT ABS(EXTRACT(EPOCH FROM (available_at-:target)))<1 "
                    "FROM review_jobs WHERE id=:job"
                ),
                {"target": target, "job": submitted["review_id"]},
            ).scalar_one()
        self.assertTrue(aligned)

    def test_invalid_result_is_schema_policy_without_postgres_write_error(self):
        submitted = self.service.submit_diff("owner/postgres", diff_for(2275))
        lease = self.store.claim("invalid-result", lease_seconds=30)
        self.assertIsNotNone(lease)
        assert lease is not None
        self.store.mark_running(lease)
        trace = self.store.trace_path_for_lease(lease)
        trace.write_text('{"trace":"invalid-result"}\n', encoding="utf-8")
        self.store.complete(
            lease,
            {
                "summary": "invalid",
                "findings": [
                    {
                        "path": "a.py\x00tail",
                        "line": 1,
                        "severity": "high",
                        "message": "nul",
                    }
                ],
            },
            trace_key=trace.name,
            usage={"llm_calls": 0},
        )
        terminal = self.store.get(submitted["review_id"])
        self.assertEqual(terminal["state"], "failed")
        self.assertEqual(terminal["error"]["code"], "schema_policy")
        self.assertEqual(terminal["attempt_count"], 1)
        with self.store.database.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text("SELECT COUNT(*) FROM findings WHERE review_job_id=:job"),
                    {"job": submitted["review_id"]},
                ).scalar_one(),
                0,
            )

    def test_received_reconciliation_is_bounded(self):
        submitted = [
            self.service.submit_diff("owner/postgres", diff_for(2300 + index))
            for index in range(3)
        ]
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace(
            "+00:00", "Z"
        )
        with self.store.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE review_jobs SET state='received', created_at=:old "
                    "WHERE id IN (:first, :second, :third)"
                ),
                {
                    "old": old,
                    "first": submitted[0]["review_id"],
                    "second": submitted[1]["review_id"],
                    "third": submitted[2]["review_id"],
                },
            )
        self.assertEqual(
            self.store.reconcile_received(timeout_seconds=1, batch_size=2), 2
        )
        with self.store.database.engine.connect() as connection:
            remaining = connection.execute(
                text("SELECT COUNT(*) FROM review_jobs WHERE state='received'")
            ).scalar_one()
        self.assertEqual(remaining, 1)
        self.assertEqual(
            self.store.reconcile_received(timeout_seconds=1, batch_size=2), 1
        )

    def test_received_reconciler_losing_failure_cas_preserves_queued_payload(self):
        submitted = self.service.submit_diff("owner/postgres", diff_for(2350))
        job_id = submitted["review_id"]
        with self.store.database.engine.connect() as connection:
            payload_key = str(
                connection.execute(
                    text("SELECT payload_key FROM review_jobs WHERE id=:job"),
                    {"job": job_id},
                ).scalar_one()
            )
        payload = self.store.job_data_dir / payload_key
        original_payload = payload.read_bytes()
        payload.write_bytes(b"corrupt")
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace(
            "+00:00", "Z"
        )
        with self.store.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE review_jobs SET state='received', created_at=:old "
                    "WHERE id=:job"
                ),
                {"old": old, "job": job_id},
            )

        entered = threading.Event()
        release = threading.Event()
        failure_results: list[bool] = []
        original_fail_received = self.store.fail_received

        def delayed_fail_received(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("concurrent finalizer did not release reconciler")
            result = original_fail_received(*args, **kwargs)
            failure_results.append(result)
            return result

        with patch.object(
            self.store, "fail_received", side_effect=delayed_fail_received
        ):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self.store.reconcile_received, timeout_seconds=1
                )
                try:
                    self.assertTrue(entered.wait(5))
                    payload.write_bytes(original_payload)
                    self.store.finalize_received(job_id, payload_key=payload_key)
                finally:
                    release.set()
                self.assertEqual(future.result(), 1)

        self.assertEqual(failure_results, [False])
        self.assertEqual(self.store.get(job_id)["state"], "queued")
        self.assertEqual(payload.read_bytes(), original_payload)

    def test_claim_cursor_rotates_past_a_bounded_blocked_scope_page(self):
        aliases = tuple(f"owner/page-{index:02d}" for index in range(33))
        principal = self.store.bootstrap_local(aliases)
        records = []
        for alias in aliases:
            record = self.store.database.authorized_repository(principal, alias)
            self.assertIsNotNone(record)
            assert record is not None
            records.append(record)

        self.store.configure_quota(
            principal.organization_id,
            max_concurrent_jobs=64,
        )
        for record in records:
            self.store.configure_quota(
                principal.organization_id,
                repository_id=str(record["id"]),
                max_concurrent_jobs=1,
            )

        def create_job(alias: str, repository_id: str, ordinal: int) -> str:
            digest = hashlib.sha256(f"{alias}:{ordinal}".encode()).hexdigest()
            job_id, duplicate = self.store.create(
                source_kind="pull_request",
                repository=alias,
                source_ref=f"https://github.com/{alias}/pull/{ordinal}",
                source_sha256=digest,
                source_bytes=0,
                organization_id=principal.organization_id,
                repository_id=repository_id,
                submitted_by=principal.user_id,
                correlation_id=f"page-{ordinal}",
                submission_key=hashlib.sha256(
                    f"submission:{alias}:{ordinal}".encode()
                ).hexdigest(),
                head_sha=digest[:40],
            )
            self.assertFalse(duplicate)
            return job_id

        active_jobs = []
        for index, (alias, record) in enumerate(zip(aliases[:32], records[:32])):
            repository_id = str(record["id"])
            active_jobs.append(create_job(alias, repository_id, index * 2))
            create_job(alias, repository_id, index * 2 + 1)

        with self.store.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE review_jobs SET state='running', lease_owner='page-setup', "
                    "lease_token=:token, attempt_count=1, heartbeat_at=clock_timestamp(), "
                    "lease_expires_at=clock_timestamp()+INTERVAL '5 minutes' "
                    "WHERE id=:job"
                ),
                [
                    {
                        "token": hashlib.sha256(job_id.encode()).hexdigest()[:32],
                        "job": job_id,
                    }
                    for job_id in active_jobs
                ],
            )

        target = create_job(aliases[32], str(records[32]["id"]), 9999)
        with patch.object(self.store, "CLAIM_MAX_PAGES", 1):
            self.assertIsNone(
                self.store.claim("blocked-page-worker", lease_seconds=30)
            )
            lease = self.store.claim("second-page-worker", lease_seconds=30)
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertEqual(lease.job_id, target)


if __name__ == "__main__":
    unittest.main()
