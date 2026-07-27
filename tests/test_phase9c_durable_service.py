from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import warnings

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from scripts import phase9c_container_test as container_test_module
from code_review_agent import database as database_module
from code_review_agent import llm as llm_module
from code_review_agent import service_core as service_core_module
from code_review_agent.service import HttpSettings, create_app
from code_review_agent.service_core import (
    AuthorizationDenied,
    ExternalCommandError,
    IdempotencyConflict,
    InvalidRequest,
    JobState,
    LeaseLost,
    ModelBudgetExhausted,
    QueueFull,
    RepositoryRegistry,
    ReviewRequest,
    ReviewService,
    SubmissionRateLimited,
)
from code_review_agent.service_queue import JobStore
from code_review_agent import worker as worker_module
from code_review_agent.worker import (
    FakeReviewRunner,
    ReviewWorker,
    WorkerSettings,
    classify_failure,
    create_worker_from_env,
)


TOKEN = "phase9c-local-token-that-is-at-least-32-bytes"
SECRET = "phase9c-webhook-secret-value"
HEAD_SHA = "a" * 40


def diff_for(index: int) -> str:
    return (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        f"-old = {index}\n"
        f"+new = {index}\n"
    )


class RecordingRunner:
    def __init__(self, *, gate: threading.Event | None = None, fail: BaseException | None = None):
        self.gate = gate
        self.fail = fail
        self.started = threading.Event()
        self.calls: list[ReviewRequest] = []
        self._lock = threading.Lock()

    def __call__(self, request: ReviewRequest, trace_path: Path):
        with self._lock:
            self.calls.append(request)
        self.started.set()
        if self.gate is not None:
            self.gate.wait(3)
        trace_path.write_text('{"trace":"redacted"}\n', encoding="utf-8")
        if self.fail is not None:
            raise self.fail
        return {"summary": "ok", "findings": []}


class Phase9CDurableServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.registry = RepositoryRegistry.from_json(
            json.dumps({"owner/repo": str(self.repo.resolve())})
        )
        self.stores: list[JobStore] = []
        self.services: list[ReviewService] = []
        self.workers: list[ReviewWorker] = []

    def tearDown(self):
        for worker in reversed(self.workers):
            worker.shutdown()
        for service in reversed(self.services):
            service.shutdown()
        for store in reversed(self.stores):
            store.close()
        self.temp.cleanup()

    def make_store(self, name: str = "state", *, auto_migrate: bool = True) -> JobStore:
        store = JobStore(self.root / name, auto_migrate=auto_migrate)
        self.stores.append(store)
        return store

    def make_service(
        self, *, store: JobStore | None = None, runner=None
    ) -> ReviewService:
        service = ReviewService(
            self.registry,
            store or self.make_store(),
            runner=runner,
        )
        self.services.append(service)
        return service

    @staticmethod
    def wait_state(store: JobStore, job_id: str, states: set[str], timeout: float = 3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = store.get(job_id)
            if job["state"] in states:
                return job
            time.sleep(0.01)
        raise AssertionError(f"job did not enter {states}: {store.get(job_id)}")

    def test_api_only_submission_is_durable_and_does_not_execute(self):
        store = self.make_store()
        service = self.make_service(store=store)
        before = time.monotonic()
        submitted = service.submit_diff("owner/repo", diff_for(1))
        elapsed = time.monotonic() - before
        self.assertLess(elapsed, 0.5)
        self.assertEqual(submitted["state"], JobState.QUEUED.value)
        self.assertFalse(submitted["duplicate"])
        self.assertEqual(list(store.job_data_dir.glob("*.diff")), [
            store.job_data_dir / f"{submitted['review_id']}.diff"
        ])
        time.sleep(0.05)
        self.assertEqual(store.get(submitted["review_id"])["state"], "queued")

    def test_fifty_concurrent_submissions_have_no_loss_or_duplicate(self):
        store = self.make_store()
        service = self.make_service(store=store)

        def submit(index: int):
            return service.submit_diff(
                "owner/repo",
                diff_for(index),
                idempotency_key=f"submission-{index}",
            )

        with ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(submit, range(50)))
        ids = {item["review_id"] for item in results}
        self.assertEqual(len(ids), 50)
        with store.database.engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM review_jobs")).scalar_one()
            events = connection.execute(text("SELECT COUNT(*) FROM submission_events")).scalar_one()
        self.assertEqual(count, 50)
        self.assertEqual(events, 50)
        self.assertEqual(len(list(store.job_data_dir.glob("*.diff"))), 50)

        with ThreadPoolExecutor(max_workers=16) as executor:
            replay = list(executor.map(submit, range(50)))
        self.assertEqual({item["review_id"] for item in replay}, ids)
        self.assertTrue(all(item["duplicate"] for item in replay))
        with store.database.engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SELECT COUNT(*) FROM review_jobs")).scalar_one(),
                50,
            )

    def test_idempotency_conflict_is_stable(self):
        service = self.make_service()
        first = service.submit_diff(
            "owner/repo", diff_for(1), idempotency_key="same-key"
        )
        duplicate = service.submit_diff(
            "owner/repo", diff_for(1), idempotency_key="same-key"
        )
        self.assertEqual(first["review_id"], duplicate["review_id"])
        self.assertTrue(duplicate["duplicate"])
        with self.assertRaises(IdempotencyConflict):
            service.submit_diff(
                "owner/repo", diff_for(2), idempotency_key="same-key"
            )

        principal = service.store.local_principal
        self.assertIsNotNone(principal)
        assert principal is not None
        repository = service.store.database.authorized_repository(
            principal, "owner/repo"
        )
        self.assertIsNotNone(repository)
        assert repository is not None
        service.store.database.update_repository(
            principal.organization_id,
            str(repository["id"]),
            mode="shadow",
            budget_microusd=None,
            policy_version="local/v2",
        )
        with self.assertRaises(IdempotencyConflict):
            service.submit_diff(
                "owner/repo", diff_for(1), idempotency_key="same-key"
            )
        updated_policy = service.submit_diff(
            "owner/repo", diff_for(1), idempotency_key="policy-v2-key"
        )
        self.assertNotEqual(first["review_id"], updated_policy["review_id"])
        self.assertFalse(updated_policy["duplicate"])

    def test_late_and_multiple_idempotency_keys_remain_bound(self):
        service = self.make_service()
        first = service.submit_diff("owner/repo", diff_for(1))
        for key in ("late-key-a", "late-key-b"):
            replay = service.submit_diff(
                "owner/repo", diff_for(1), idempotency_key=key
            )
            self.assertEqual(replay["review_id"], first["review_id"])
            self.assertTrue(replay["duplicate"])
            with self.assertRaises(IdempotencyConflict):
                service.submit_diff(
                    "owner/repo", diff_for(2), idempotency_key=key
                )

    def test_pr_number_and_exact_url_share_one_logical_job(self):
        service = self.make_service()
        first, first_duplicate = service.submit_pr(
            "owner/repo", "7", head_sha=HEAD_SHA
        )
        replay, replay_duplicate = service.submit_pr(
            "owner/repo",
            "https://github.com/Owner/Repo/pull/007/",
            head_sha=HEAD_SHA,
        )
        self.assertFalse(first_duplicate)
        self.assertTrue(replay_duplicate)
        self.assertEqual(first["review_id"], replay["review_id"])

    def test_terminal_replay_does_not_recreate_cleaned_payload(self):
        store = self.make_store()
        service = self.make_service(store=store)
        first = service.submit_diff(
            "owner/repo", diff_for(3), idempotency_key="terminal-replay"
        )
        lease = store.claim("terminal-worker", lease_seconds=10)
        self.assertIsNotNone(lease)
        assert lease is not None
        store.mark_running(lease)
        trace = store.trace_path_for_lease(lease)
        trace.write_text('{"trace":"winner"}\n', encoding="utf-8")
        store.complete(
            lease,
            {"summary": "winner", "findings": []},
            trace_key=trace.name,
            usage={"llm_calls": 0},
        )
        with store.database.engine.connect() as connection:
            transition = connection.execute(
                text(
                    "SELECT action, decision FROM audit_events "
                    "WHERE resource_id=:job AND action='review.awaiting_approval'"
                ),
                {"job": first["review_id"]},
            ).one()
        self.assertEqual(tuple(transition), ("review.awaiting_approval", "allow"))
        self.assertFalse(list(store.job_data_dir.glob("*.diff")))
        replay = service.submit_diff(
            "owner/repo", diff_for(3), idempotency_key="terminal-replay"
        )
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["review_id"], first["review_id"])
        self.assertFalse(list(store.job_data_dir.glob("*.diff")))

    def test_received_reconciler_rejects_corrupt_payload(self):
        store = self.make_store()
        service = self.make_service(store=store)
        submitted = service.submit_diff("owner/repo", diff_for(4))
        payload = store.job_data_dir / f"{submitted['review_id']}.diff"
        payload.write_text("corrupt", encoding="utf-8")
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        with store.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE review_jobs SET state='received', created_at=:old "
                    "WHERE id=:job"
                ),
                {"old": old, "job": submitted["review_id"]},
            )
        self.assertEqual(store.reconcile_received(timeout_seconds=1), 1)
        failed = store.get(submitted["review_id"])
        self.assertEqual(failed["state"], JobState.FAILED.value)
        self.assertEqual(failed["error"]["code"], "payload_unavailable")
        self.assertFalse(payload.exists())
        with store.database.engine.connect() as connection:
            transition = connection.execute(
                text(
                    "SELECT action, reason_code FROM audit_events "
                    "WHERE resource_id=:job AND action='review.failed'"
                ),
                {"job": submitted["review_id"]},
            ).one()
        self.assertEqual(tuple(transition), ("review.failed", "payload_unavailable"))

    def test_received_reconciler_does_not_delete_payload_after_losing_failure_cas(self):
        store = self.make_store()
        service = self.make_service(store=store)
        submitted = service.submit_diff("owner/repo", diff_for(41))
        job_id = submitted["review_id"]
        payload = store.job_data_dir / f"{job_id}.diff"
        original_payload = payload.read_bytes()
        payload.write_bytes(b"corrupt")
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        with store.database.engine.begin() as connection:
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
        original_fail_received = store.fail_received

        def delayed_fail_received(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("concurrent finalizer did not release reconciler")
            result = original_fail_received(*args, **kwargs)
            failure_results.append(result)
            return result

        with patch.object(store, "fail_received", side_effect=delayed_fail_received):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(store.reconcile_received, timeout_seconds=1)
                try:
                    self.assertTrue(entered.wait(2))
                    payload.write_bytes(original_payload)
                    store.finalize_received(job_id, payload_key=payload.name)
                finally:
                    release.set()
                self.assertEqual(future.result(), 1)

        self.assertEqual(failure_results, [False])
        self.assertEqual(store.get(job_id)["state"], JobState.QUEUED.value)
        self.assertEqual(payload.read_bytes(), original_payload)

    def test_orphan_cleanup_is_bounded_and_preserves_database_lineage(self):
        store = self.make_store()
        service = self.make_service(store=store)
        submitted = service.submit_diff("owner/repo", diff_for(90))
        retained = store.job_data_dir / f"{submitted['review_id']}.diff"
        self.assertTrue(retained.is_file())
        orphan_payload = store.job_data_dir / f"{'e' * 32}.diff"
        orphan_payload.write_text("orphan", encoding="utf-8")
        old_temporary = (
            store.job_data_dir / f".{'d' * 32}.diff.{'1' * 32}.tmp"
        )
        old_temporary.write_text("abandoned", encoding="utf-8")
        old_timestamp = time.time() - 600
        os.utime(old_temporary, (old_timestamp, old_timestamp))
        fresh_temporary = (
            store.job_data_dir / f".{'c' * 32}.diff.{'2' * 32}.tmp"
        )
        fresh_temporary.write_text("active", encoding="utf-8")
        orphan_trace = store.trace_dir / f"{'f' * 32}.1.{'a' * 32}.jsonl"
        orphan_trace.write_text('{}\n', encoding="utf-8")
        self.assertEqual(store.cleanup_orphans(limit=64), 3)
        self.assertTrue(retained.is_file())
        self.assertFalse(orphan_payload.exists())
        self.assertFalse(old_temporary.exists())
        self.assertTrue(fresh_temporary.exists())
        self.assertFalse(orphan_trace.exists())
        with self.assertRaises(Exception):
            store.cleanup_orphans(limit=0)

        orphan_payload.write_text("orphan", encoding="utf-8")
        with patch.object(Path, "unlink", side_effect=OSError("disk")), warnings.catch_warnings(
            record=True
        ) as caught:
            warnings.simplefilter("always")
            store._delete_payload(orphan_payload.name)
        self.assertEqual(str(caught[0].message), "durable artifact cleanup failed")
        self.assertNotIn(str(orphan_payload), str(caught[0].message))

    def test_two_stores_do_not_use_a_state_directory_lock_and_claim_once(self):
        first = self.make_store()
        service = self.make_service(store=first)
        job_id = service.submit_diff("owner/repo", diff_for(1))["review_id"]
        second = JobStore(
            self.root / "state",
            database_url=first.database_url,
            auto_migrate=False,
        )
        self.stores.append(second)
        barrier = threading.Barrier(2)
        leases = []
        lock = threading.Lock()

        def claim(store: JobStore, owner: str):
            barrier.wait()
            lease = store.claim(owner, lease_seconds=10)
            with lock:
                leases.append(lease)

        threads = [
            threading.Thread(target=claim, args=(first, "worker-a")),
            threading.Thread(target=claim, args=(second, "worker-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        claimed = [lease for lease in leases if lease is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].job_id, job_id)
        self.assertFalse((first.state_dir / ".service.lock").exists())

    def test_expired_lease_is_recovered_and_old_token_is_fenced(self):
        store = self.make_store()
        service = self.make_service(store=store)
        job_id = service.submit_diff("owner/repo", diff_for(1))["review_id"]
        start = datetime.now(timezone.utc) + timedelta(seconds=1)
        first = store.claim("worker-a", lease_seconds=1, now=start)
        self.assertIsNotNone(first)
        assert first is not None
        store.mark_running(first, now=start)
        store.trace_path_for_lease(first).write_text(
            '{"trace":"stale"}\n', encoding="utf-8"
        )

        recovered = store.claim(
            "worker-b", lease_seconds=10, now=start + timedelta(seconds=2)
        )
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.job_id, first.job_id)
        self.assertNotEqual(recovered.lease_token, first.lease_token)
        self.assertEqual(recovered.attempt_count, 2)
        with self.assertRaises(LeaseLost):
            store.complete(
                first,
                {"summary": "stale", "findings": []},
                trace_key=store.trace_path_for_lease(first).name,
                now=start + timedelta(seconds=2),
            )
        store.mark_running(recovered, now=start + timedelta(seconds=2))
        trace = store.trace_path_for_lease(recovered)
        trace.write_text('{"trace":"winner"}\n', encoding="utf-8")
        store.complete(
            recovered,
            {"summary": "winner", "findings": []},
            trace_key=trace.name,
            usage={"llm_calls": 0},
            now=start + timedelta(seconds=3),
        )
        job = store.get(job_id)
        self.assertEqual(job["state"], JobState.AWAITING_APPROVAL.value)
        self.assertEqual(job["review"]["summary"], "winner")
        with store.database.engine.connect() as connection:
            transitions = connection.execute(
                text(
                    "SELECT action, reason_code FROM audit_events "
                    "WHERE resource_id=:job AND auth_method='durable_worker' "
                    "ORDER BY occurred_at_utc, id"
                ),
                {"job": job_id},
            ).all()
        self.assertEqual(
            [tuple(item) for item in transitions],
            [
                ("review.lease_expired", "lease_expired"),
                ("review.awaiting_approval", None),
            ],
        )

    def test_full_concurrency_does_not_leak_attempt_reservations(self):
        store = self.make_store()
        service = self.make_service(store=store)
        first = service.submit_diff("owner/repo", diff_for(1))
        second = service.submit_diff("owner/repo", diff_for(2))
        org_id = first["organization_id"]
        repo_id = first["repository_id"]
        store.configure_quota(org_id, max_concurrent_jobs=1)
        store.configure_quota(
            org_id, repository_id=repo_id, max_concurrent_jobs=1
        )
        active = store.claim("capacity-owner", lease_seconds=10)
        self.assertIsNotNone(active)
        assert active is not None
        store.mark_running(active)
        queued_id = (
            second["review_id"]
            if active.job_id == first["review_id"]
            else first["review_id"]
        )
        with store.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE review_jobs SET model_calls_reserved=0 WHERE id=:job"
                ),
                {"job": queued_id},
            )
            connection.execute(
                text(
                    "UPDATE service_quotas SET monthly_model_calls_reserved=64 "
                    "WHERE organization_id=:org"
                ),
                {"org": org_id},
            )
        for _ in range(2):
            self.assertIsNone(store.claim("capacity-waiter", lease_seconds=10))
        with store.database.engine.connect() as connection:
            reserved = connection.execute(
                text(
                    "SELECT scope_kind, monthly_model_calls_reserved "
                    "FROM service_quotas WHERE organization_id=:org"
                ),
                {"org": org_id},
            ).all()
            queued = connection.execute(
                text(
                    "SELECT state, attempt_count, model_calls_reserved "
                    "FROM review_jobs WHERE id=:job"
                ),
                {"job": queued_id},
            ).one()
        self.assertEqual({int(row[1]) for row in reserved}, {64})
        self.assertEqual(tuple(queued), ("queued", 0, 0))

    def test_month_rollover_carries_active_reservations(self):
        store = self.make_store()
        service = self.make_service(store=store)
        first = service.submit_diff("owner/repo", diff_for(1))
        org_id = first["organization_id"]
        repo_id = first["repository_id"]
        store.configure_quota(org_id, monthly_model_call_budget=64)
        store.configure_quota(
            org_id,
            repository_id=repo_id,
            monthly_model_call_budget=64,
        )
        with store.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE service_quotas SET model_call_month='1999-01' "
                    "WHERE organization_id=:org"
                ),
                {"org": org_id},
            )
        with self.assertRaises(ModelBudgetExhausted):
            service.submit_diff("owner/repo", diff_for(2))
        with store.database.engine.connect() as connection:
            reservations = connection.execute(
                text(
                    "SELECT monthly_model_calls_reserved FROM service_quotas "
                    "WHERE organization_id=:org"
                ),
                {"org": org_id},
            ).scalars().all()
        self.assertEqual(set(map(int, reservations)), {64})

    def test_claim_batch_is_fair_across_repository_scopes(self):
        repo_a = self.root / "repo-a"
        repo_b = self.root / "repo-b"
        for repo in (repo_a, repo_b):
            repo.mkdir()
            (repo / ".git").mkdir()
        registry = RepositoryRegistry.from_json(
            json.dumps(
                {
                    "owner/a": str(repo_a.resolve()),
                    "owner/b": str(repo_b.resolve()),
                }
            )
        )
        store = self.make_store("fair")
        service = ReviewService(registry, store)
        self.services.append(service)
        jobs_a = [service.submit_diff("owner/a", diff_for(i)) for i in range(33)]
        org_id = jobs_a[0]["organization_id"]
        repo_a_id = jobs_a[0]["repository_id"]
        store.configure_quota(
            org_id, repository_id=repo_a_id, max_concurrent_jobs=1
        )
        active = store.claim("fair-owner", lease_seconds=10)
        self.assertIsNotNone(active)
        assert active is not None
        store.mark_running(active)
        job_b = service.submit_diff("owner/b", diff_for(1000))
        claimed = store.claim("fair-other", lease_seconds=10)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.job_id, job_b["review_id"])

    def test_registered_repository_has_default_quota_before_first_submission(self):
        store = self.make_store()
        self.make_service(store=store)
        principal = store.local_principal
        self.assertIsNotNone(principal)
        assert principal is not None
        repository = store.database.authorized_repository(principal, "owner/repo")
        self.assertIsNotNone(repository)
        assert repository is not None
        organization_quota = store.get_quota(principal.organization_id)
        repository_quota = store.get_quota(
            principal.organization_id, repository_id=str(repository["id"])
        )
        self.assertEqual(organization_quota["max_queued_jobs"], 1000)
        self.assertEqual(repository_quota["max_queued_jobs"], 100)
        with store.database.engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SELECT COUNT(*) FROM review_jobs")).scalar_one(),
                0,
            )

    def test_empty_organization_quota_can_be_updated(self):
        store = self.make_store()
        organization = store.database.create_organization(
            "empty-organization", "Empty Organization"
        )
        updated = store.configure_quota(
            str(organization["id"]), max_queued_jobs=7
        )
        self.assertEqual(updated["max_queued_jobs"], 7)
        self.assertEqual(
            store.get_quota(str(organization["id"]))["max_queued_jobs"], 7
        )

    def test_queue_rate_and_model_budget_return_distinct_errors(self):
        store = self.make_store()
        service = self.make_service(store=store)
        first = service.submit_diff("owner/repo", diff_for(1))
        repo_id = first["repository_id"]
        org_id = first["organization_id"]
        store.configure_quota(
            org_id,
            repository_id=repo_id,
            max_queued_jobs=1,
            submission_rate_limit=100,
            monthly_model_call_budget=10000,
        )
        with self.assertRaises(QueueFull):
            service.submit_diff("owner/repo", diff_for(2))

        org_store = self.make_store("org-queue")
        org_service = self.make_service(store=org_store)
        org_seed = org_service.submit_diff("owner/repo", diff_for(5))
        org_store.configure_quota(
            org_seed["organization_id"], max_queued_jobs=1
        )
        org_store.configure_quota(
            org_seed["organization_id"],
            repository_id=org_seed["repository_id"],
            max_queued_jobs=10,
        )
        with self.assertRaises(QueueFull):
            org_service.submit_diff("owner/repo", diff_for(6))

        # A fresh store isolates fixed-window and budget counters.
        rate_store = self.make_store("rate")
        rate_service = self.make_service(store=rate_store)
        seeded = rate_service.submit_diff("owner/repo", diff_for(10))
        rate_store.configure_quota(
            seeded["organization_id"],
            repository_id=seeded["repository_id"],
            max_queued_jobs=10,
            submission_rate_limit=1,
        )
        with self.assertRaises(SubmissionRateLimited) as limited:
            rate_service.submit_diff("owner/repo", diff_for(11))
        self.assertIsNotNone(limited.exception.retry_after)

        budget_store = self.make_store("budget")
        budget_service = self.make_service(store=budget_store)
        seed = budget_service.submit_diff("owner/repo", diff_for(20))
        budget_store.configure_quota(
            seed["organization_id"],
            repository_id=seed["repository_id"],
            max_queued_jobs=10,
            submission_rate_limit=100,
            monthly_model_call_budget=64,
        )
        with self.assertRaises(ModelBudgetExhausted):
            budget_service.submit_diff("owner/repo", diff_for(21))

    def test_store_validation_and_health_error_branches_are_stable(self):
        store = self.make_store()
        service = self.make_service(store=store)
        seed = service.submit_diff("owner/repo", diff_for(30))
        org_id = seed["organization_id"]
        repo_id = seed["repository_id"]
        with self.assertRaises(Exception):
            store.get("not-a-job-id")
        with self.assertRaises(Exception):
            store.get_quota("missing-organization")
        for values in (
            {"unknown_quota": 1},
            {"max_queued_jobs": True},
            {"max_concurrent_jobs": 0},
        ):
            with self.subTest(values=values), self.assertRaises(Exception):
                store.configure_quota(org_id, repository_id=repo_id, **values)
        unlimited = store.configure_quota(
            org_id, repository_id=repo_id, monthly_model_call_budget=None
        )
        self.assertIsNone(unlimited["monthly_model_call_budget"])

        common = {
            "source_kind": "pull_request",
            "repository": "owner/repo",
            "source_ref": "31",
            "source_sha256": "a" * 64,
            "source_bytes": 0,
            "organization_id": org_id,
            "repository_id": repo_id,
            "submitted_by": "test-user",
        }
        for values in (
            {"submission_key": "invalid"},
            {"idempotency_key_hash": "invalid"},
            {"max_attempts": 0},
        ):
            with self.subTest(values=values), self.assertRaises(Exception):
                store.create(**common, **values)
        with self.assertRaises(Exception):
            store.claim("bad worker", lease_seconds=10)
        with self.assertRaises(Exception):
            store.claim("worker", lease_seconds=0)
        with self.assertRaises(Exception):
            store.worker_heartbeat("worker", status="bad", capacity=1)
        self.assertFalse(store.worker_is_live("missing", stale_seconds=1))
        with self.assertRaises(Exception):
            store.trace_path(seed["review_id"])
        with self.assertRaises(Exception):
            store.read_trace(seed["review_id"])
        with self.assertRaises(Exception):
            store.finalize_received("0" * 32, payload_key=None)
        with self.assertRaises(Exception):
            store._payload_path("../escape.diff")
        with self.assertRaises(Exception):
            store._write_payload(seed["review_id"], "wrong", "0" * 64)
        store.close()
        store.close()

    def test_payload_loading_trace_binding_and_oversized_result_fail_closed(self):
        store = self.make_store()
        service = self.make_service(store=store)
        submitted = service.submit_diff("owner/repo", diff_for(32))
        lease = store.claim("payload-worker", lease_seconds=10)
        self.assertIsNotNone(lease)
        assert lease is not None
        with self.assertRaises(Exception):
            store.trace_path_for_lease(replace(lease, lease_token="bad token"))
        payload_path = store.job_data_dir / str(lease.payload_key)
        original = payload_path.read_bytes()
        payload_path.unlink()
        with self.assertRaises(Exception):
            store.load_payload(lease)
        payload_path.write_bytes(b"changed")
        with self.assertRaises(Exception):
            store.load_payload(lease)
        payload_path.write_bytes(b"\xff")
        with self.assertRaises(Exception):
            store.load_payload(
                replace(
                    lease,
                    source_sha256=hashlib.sha256(b"\xff").hexdigest(),
                )
            )
        payload_path.write_bytes(original)
        store.mark_running(lease)
        trace = store.trace_path_for_lease(lease)
        with self.assertRaises(InvalidRequest):
            store.complete(
                lease,
                {"summary": "missing trace", "findings": []},
                trace_key=trace.name,
            )
        trace.write_text("partial-json", encoding="utf-8")
        with self.assertRaises(InvalidRequest):
            store.complete(
                lease,
                {"summary": "malformed trace", "findings": []},
                trace_key=trace.name,
            )
        trace.write_text('{"trace":"bounded"}\n', encoding="utf-8")
        with self.assertRaises(Exception):
            store.complete(
                lease,
                {"summary": "bad trace", "findings": []},
                trace_key="0" * 32 + ".1." + "0" * 32 + ".jsonl",
            )
        store.complete(
            lease,
            {"summary": "x" * (2 * 1024 * 1024), "findings": []},
            trace_key=trace.name,
            usage={"llm_calls": 0},
        )
        failed = store.get(submitted["review_id"])
        self.assertEqual(failed["state"], JobState.FAILED.value)
        self.assertEqual(failed["error"]["code"], "schema_policy")

        malformed_job = service.submit_diff("owner/repo", diff_for(319))
        malformed_lease = store.claim("malformed-trace-worker", lease_seconds=10)
        self.assertIsNotNone(malformed_lease)
        assert malformed_lease is not None
        store.mark_running(malformed_lease)
        malformed_trace = store.trace_path_for_lease(malformed_lease)
        malformed_trace.write_text("partial-json", encoding="utf-8")
        store.finish_failure(
            malformed_lease,
            "internal",
            retryable=False,
            trace_key=malformed_trace.name,
            usage={"llm_calls": 0},
        )
        with store.database.engine.connect() as connection:
            final_trace_key = connection.execute(
                text("SELECT final_trace_key FROM review_jobs WHERE id=:job"),
                {"job": malformed_job["review_id"]},
            ).scalar_one()
        self.assertIsNone(final_trace_key)

    def test_invalid_result_schema_fails_atomically_as_schema_policy(self):
        store = self.make_store()
        service = self.make_service(store=store)
        invalid_results = (
            {"summary": "missing findings"},
            {"summary": "bad", "findings": "not-a-list"},
            {"summary": "bad", "findings": [], "extra": object()},
            {"summary": "bad", "findings": [{"file": "a.py"}]},
            {
                "summary": "bad",
                "findings": [
                    {
                        "path": "a" * 513,
                        "line": 1,
                        "severity": "high",
                        "message": "too long",
                    }
                ],
            },
            {
                "summary": "bad",
                "findings": [
                    {
                        "path": "a.py",
                        "line": 1,
                        "severity": [],
                        "message": "unhashable enum",
                    }
                ],
            },
            {
                "summary": "bad",
                "findings": [
                    {
                        "path": "a.py\x00tail",
                        "line": 1,
                        "severity": "high",
                        "message": "nul",
                    }
                ],
            },
            {
                "summary": "bad",
                "findings": [
                    {
                        "path": "a.py",
                        "line": 1,
                        "severity": "critical",
                        "message": "enum",
                    }
                ],
            },
            {
                "summary": "bad",
                "findings": [
                    {
                        "path": "a.py",
                        "line": 2**31,
                        "severity": "high",
                        "message": "out of range",
                    }
                ],
            },
            {
                "summary": "bad",
                "findings": [
                    {
                        "path": "a.py",
                        "line": 1,
                        "severity": "high",
                        "category": "x" * 129,
                        "message": "too long",
                    }
                ],
            },
        )
        for index, review in enumerate(invalid_results):
            submitted = service.submit_diff("owner/repo", diff_for(320 + index))
            lease = store.claim(f"schema-worker-{index}", lease_seconds=10)
            self.assertIsNotNone(lease)
            assert lease is not None
            store.mark_running(lease)
            trace = store.trace_path_for_lease(lease)
            trace.write_text('{"trace":"invalid-result"}\n', encoding="utf-8")
            store.complete(lease, review, trace_key=trace.name, usage={"llm_calls": 0})
            failed = store.get(submitted["review_id"])
            self.assertEqual(failed["state"], JobState.FAILED.value)
            self.assertEqual(failed["error"]["code"], "schema_policy")
        with store.database.engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SELECT COUNT(*) FROM findings")).scalar_one(),
                0,
            )
            transitions = connection.execute(
                text(
                    "SELECT action, reason_code FROM audit_events "
                    "WHERE action='review.failed' ORDER BY occurred_at_utc, id"
                )
            ).all()
        self.assertEqual(
            [tuple(item) for item in transitions],
            [("review.failed", "schema_policy")] * len(invalid_results),
        )

    def test_claim_terminal_paths_release_budget_and_clean_payload(self):
        store = self.make_store()
        service = self.make_service(store=store)
        expired_job = service.submit_diff("owner/repo", diff_for(33))
        with store.database.engine.begin() as connection:
            connection.execute(
                text("UPDATE review_jobs SET max_attempts=1 WHERE id=:job"),
                {"job": expired_job["review_id"]},
            )
        start = datetime.now(timezone.utc) + timedelta(seconds=1)
        lease = store.claim("expires-once", lease_seconds=1, now=start)
        self.assertIsNotNone(lease)
        assert lease is not None
        store.mark_running(lease, now=start)
        expired_trace = store.trace_path_for_lease(lease)
        expired_trace.write_text('{"trace":"still-owned-by-old-worker"}\n', encoding="utf-8")
        self.assertIsNone(
            store.claim(
                "recovery-after-limit",
                lease_seconds=10,
                now=start + timedelta(seconds=2),
            )
        )
        terminal = store.get(expired_job["review_id"])
        self.assertEqual(terminal["state"], JobState.DEAD_LETTER.value)
        self.assertFalse(
            (store.job_data_dir / f"{expired_job['review_id']}.diff").exists()
        )

        budget_job = service.submit_diff("owner/repo", diff_for(34))
        with store.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE review_jobs SET model_calls_reserved=0 WHERE id=:job"
                ),
                {"job": budget_job["review_id"]},
            )
            connection.execute(
                text(
                    "UPDATE service_quotas SET monthly_model_call_budget=1, "
                    "monthly_model_calls_used=1, monthly_model_calls_reserved=0 "
                    "WHERE organization_id=:org"
                ),
                {"org": budget_job["organization_id"]},
            )
        self.assertIsNone(store.claim("budget-claim", lease_seconds=10))
        budget_terminal = store.get(budget_job["review_id"])
        self.assertEqual(budget_terminal["state"], JobState.FAILED.value)
        self.assertEqual(budget_terminal["error"]["code"], "budget_exhausted")
        self.assertFalse(
            (store.job_data_dir / f"{budget_job['review_id']}.diff").exists()
        )
        with store.database.engine.connect() as connection:
            expired_final_trace = connection.execute(
                text("SELECT final_trace_key FROM review_jobs WHERE id=:job"),
                {"job": expired_job["review_id"]},
            ).scalar_one()
            expired_transitions = connection.execute(
                text(
                    "SELECT action, reason_code FROM audit_events "
                    "WHERE resource_id=:job AND auth_method='durable_worker' "
                    "ORDER BY occurred_at_utc, id"
                ),
                {"job": expired_job["review_id"]},
            ).all()
            budget_transition = connection.execute(
                text(
                    "SELECT action, reason_code FROM audit_events "
                    "WHERE resource_id=:job AND action='review.failed'"
                ),
                {"job": budget_job["review_id"]},
            ).one()
        self.assertIsNone(expired_final_trace)
        self.assertCountEqual(
            [tuple(item) for item in expired_transitions],
            [
                ("review.lease_expired", "lease_expired"),
                ("review.dead_letter", "lease_expired"),
            ],
        )
        self.assertEqual(tuple(budget_transition), ("review.failed", "budget_exhausted"))

    def test_worker_executes_to_awaiting_approval_and_cleans_payload(self):
        gate = threading.Event()
        runner = RecordingRunner(gate=gate)
        store = self.make_store()
        service = self.make_service(store=store)
        worker = ReviewWorker(
            self.registry,
            store,
            runner=runner,
            worker_id="worker-runtime",
            concurrency=1,
            lease_seconds=5,
            heartbeat_seconds=0.25,
            poll_seconds=0.05,
            shutdown_grace_seconds=2,
        )
        self.workers.append(worker)
        worker.start()
        worker.start()
        submitted = service.submit_diff("owner/repo", diff_for(1))
        self.assertTrue(runner.started.wait(2))
        self.assertIn(store.get(submitted["review_id"])["state"], {"leased", "running"})
        self.assertGreater(store.live_worker_count(stale_seconds=2), 0)
        gate.set()
        done = self.wait_state(
            store,
            submitted["review_id"],
            {JobState.AWAITING_APPROVAL.value},
        )
        self.assertEqual(done["attempt_count"], 1)
        self.assertFalse(list(store.job_data_dir.glob("*.diff")))

    def test_worker_reaps_short_jobs_at_poll_cadence(self):
        store = self.make_store()
        service = self.make_service(store=store)
        worker = ReviewWorker(
            self.registry,
            store,
            runner=FakeReviewRunner(),
            worker_id="short-job-worker",
            concurrency=2,
            lease_seconds=30,
            heartbeat_seconds=10,
            poll_seconds=0.05,
            shutdown_grace_seconds=2,
        )
        self.workers.append(worker)
        submitted = [
            service.submit_diff("owner/repo", diff_for(200 + index))
            for index in range(3)
        ]
        worker.start()
        deadline = time.monotonic() + 2
        states: list[str] = []
        while time.monotonic() < deadline:
            states = [store.get(item["review_id"])["state"] for item in submitted]
            if all(state == JobState.AWAITING_APPROVAL.value for state in states):
                break
            time.sleep(0.05)
        self.assertEqual(states, [JobState.AWAITING_APPROVAL.value] * 3)

    def test_worker_heartbeats_are_not_delayed_by_poll_interval(self):
        store = Mock()
        store.claim.return_value = None
        worker = ReviewWorker(
            self.registry,
            store,
            worker_id="heartbeat-scheduler",
            concurrency=1,
            lease_seconds=10,
            heartbeat_seconds=0.1,
            poll_seconds=1.5,
            shutdown_grace_seconds=1,
        )

        def stop_after_wait(timeout):
            self.assertAlmostEqual(timeout, 0.08)
            worker.request_shutdown()

        with patch.object(
            worker_module.time,
            "monotonic",
            side_effect=[100.0, 100.02, 100.02],
        ), patch.object(
            worker, "_start_received_reconciliation"
        ), patch.object(
            worker._stop, "wait", side_effect=stop_after_wait
        ) as wait:
            worker.run_forever()

        wait.assert_called_once()
        store.claim.assert_called_once()

    def test_worker_graceful_shutdown_drains_without_new_claims(self):
        gate = threading.Event()
        runner = RecordingRunner(gate=gate)
        store = self.make_store()
        service = self.make_service(store=store)
        worker = ReviewWorker(
            self.registry,
            store,
            runner=runner,
            worker_id="draining-worker",
            concurrency=1,
            lease_seconds=5,
            heartbeat_seconds=0.25,
            poll_seconds=0.05,
            shutdown_grace_seconds=2,
        )
        self.workers.append(worker)
        worker.start()
        first = service.submit_diff("owner/repo", diff_for(70))
        self.assertTrue(runner.started.wait(2))
        worker.request_shutdown()
        time.sleep(0.1)
        second = service.submit_diff("owner/repo", diff_for(71))
        gate.set()
        worker.shutdown()
        self.assertEqual(
            store.get(first["review_id"])["state"],
            JobState.AWAITING_APPROVAL.value,
        )
        self.assertEqual(store.get(second["review_id"])["state"], JobState.QUEUED.value)
        self.assertFalse(
            store.worker_is_live("draining-worker", stale_seconds=10)
        )

    def test_worker_does_not_claim_after_stop_during_heartbeat(self):
        store = self.make_store()
        worker = ReviewWorker(
            self.registry,
            store,
            runner=FakeReviewRunner(),
            worker_id="stop-during-heartbeat",
            concurrency=1,
            lease_seconds=5,
            heartbeat_seconds=0.25,
            poll_seconds=0.05,
            shutdown_grace_seconds=1,
        )
        original_heartbeat = store.worker_heartbeat
        ready_heartbeats = 0

        def heartbeat(*args, **kwargs):
            nonlocal ready_heartbeats
            original_heartbeat(*args, **kwargs)
            if kwargs.get("status") == "ready":
                ready_heartbeats += 1
                if ready_heartbeats == 2:
                    worker.request_shutdown()

        with patch.object(
            store, "worker_heartbeat", side_effect=heartbeat
        ), patch.object(store, "claim", wraps=store.claim) as claim:
            worker.run_forever()
        claim.assert_not_called()

    def test_http_429_and_readiness_shapes_are_stable(self):
        store = self.make_store()
        service = self.make_service(store=store)
        settings = HttpSettings(
            service_token=TOKEN,
            webhook_secret=SECRET,
            worker_stale_seconds=1,
        )
        app = create_app(settings=settings, review_service=service)
        with TestClient(app) as client:
            auth = {"Authorization": f"Bearer {TOKEN}"}
            live = client.get("/healthz")
            self.assertEqual(live.status_code, 200)
            not_ready = client.get("/readyz")
            self.assertEqual(not_ready.status_code, 503)
            unkeyed_pr = client.post(
                "/v1/reviews/pr",
                headers=auth,
                json={"repository": "owner/repo", "pull_request": "7"},
            )
            self.assertEqual(unkeyed_pr.status_code, 400)
            self.assertEqual(unkeyed_pr.json()["error"]["code"], "invalid_request")
            store.worker_heartbeat(
                "stale-worker",
                status="ready",
                capacity=1,
                now=datetime.now(timezone.utc) - timedelta(seconds=5),
            )
            self.assertEqual(client.get("/readyz").status_code, 503)
            first = client.post(
                "/v1/reviews/diff",
                headers={**auth, "Idempotency-Key": "http-key"},
                json={"repository": "owner/repo", "diff": diff_for(1)},
            )
            self.assertEqual(first.status_code, 202, first.text)
            store.configure_quota(
                first.json()["organization_id"],
                repository_id=first.json()["repository_id"],
                max_queued_jobs=1,
            )
            full = client.post(
                "/v1/reviews/diff",
                headers=auth,
                json={"repository": "owner/repo", "diff": diff_for(2)},
            )
            self.assertEqual(full.status_code, 429)
            self.assertEqual(full.json()["error"]["code"], "queue_full")
            self.assertEqual(full.headers["Retry-After"], "1")
            self.assertLess(len(full.content), 256)

            conflict = client.post(
                "/v1/reviews/diff",
                headers={**auth, "Idempotency-Key": "http-key"},
                json={"repository": "owner/repo", "diff": diff_for(3)},
            )
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(
                conflict.json()["error"]["code"], "idempotency_conflict"
            )

            store.worker_heartbeat(
                "ready-worker", status="ready", capacity=1
            )
            ready = client.get("/readyz")
            self.assertEqual(ready.status_code, 200, ready.text)
            service.shutdown(wait=False)
            self.assertEqual(client.get("/readyz").status_code, 503)
            self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertTrue(store._closed)

    def test_http_rate_and_budget_429_codes_are_distinct(self):
        for name, quota, expected in (
            ("http-rate", {"submission_rate_limit": 1}, "submission_rate_limited"),
            (
                "http-budget",
                {"monthly_model_call_budget": 64},
                "model_budget_exhausted",
            ),
        ):
            with self.subTest(expected=expected):
                store = self.make_store(name)
                service = self.make_service(store=store)
                app = create_app(
                    settings=HttpSettings(
                        service_token=TOKEN, webhook_secret=SECRET
                    ),
                    review_service=service,
                )
                with TestClient(app) as client:
                    auth = {"Authorization": f"Bearer {TOKEN}"}
                    seed = client.post(
                        "/v1/reviews/diff",
                        headers=auth,
                        json={"repository": "owner/repo", "diff": diff_for(10)},
                    )
                    self.assertEqual(seed.status_code, 202, seed.text)
                    store.configure_quota(
                        seed.json()["organization_id"],
                        repository_id=seed.json()["repository_id"],
                        max_queued_jobs=10,
                        **quota,
                    )
                    limited = client.post(
                        "/v1/reviews/diff",
                        headers=auth,
                        json={"repository": "owner/repo", "diff": diff_for(11)},
                    )
                    self.assertEqual(limited.status_code, 429, limited.text)
                    self.assertEqual(limited.json()["error"]["code"], expected)
                    if expected == "submission_rate_limited":
                        self.assertIn("Retry-After", limited.headers)

    def test_retry_categories_and_retry_after(self):
        class Response:
            status_code = 429
            headers = {"Retry-After": "17"}

        class RateLimitError(Exception):
            response = Response()

        rate = classify_failure(
            RateLimitError(), job_id="0" * 32, attempt_count=1
        )
        self.assertEqual(rate.category, "rate_limit")
        self.assertTrue(rate.retryable)
        self.assertGreaterEqual(rate.delay_seconds, 17)

        class AuthenticationError(Exception):
            status_code = 401

        auth = classify_failure(
            AuthenticationError(), job_id="0" * 32, attempt_count=1
        )
        self.assertEqual(auth.category, "authentication")
        self.assertFalse(auth.retryable)

        class ProviderError(Exception):
            status_code = 503

        provider = classify_failure(
            ProviderError(), job_id="0" * 32, attempt_count=2
        )
        self.assertEqual(provider.category, "provider_5xx")
        self.assertTrue(provider.retryable)

        budget = classify_failure(
            ModelBudgetExhausted("budget"), job_id="0" * 32, attempt_count=1
        )
        self.assertEqual(budget.category, "budget_exhausted")
        self.assertFalse(budget.retryable)

    def test_worker_settings_validate_all_bounds_and_environment(self):
        invalid = (
            {"worker_id": "bad worker"},
            {"worker_id": "ok", "concurrency": 0},
            {"worker_id": "ok", "lease_seconds": 0},
            {"worker_id": "ok", "heartbeat_seconds": 0},
            {"worker_id": "ok", "lease_seconds": 2, "heartbeat_seconds": 1},
            {"worker_id": "ok", "poll_seconds": 0},
            {"worker_id": "ok", "stale_seconds": 0},
            {"worker_id": "ok", "shutdown_grace_seconds": -1},
            {"worker_id": "ok", "received_timeout_seconds": 0},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(Exception):
                WorkerSettings(**values)

        environment = {
            "CRAG_WORKER_CONCURRENCY": "3",
            "CRAG_JOB_LEASE_SECONDS": "20",
            "CRAG_JOB_HEARTBEAT_SECONDS": "3",
            "CRAG_WORKER_POLL_SECONDS": "0.25",
            "CRAG_WORKER_STALE_SECONDS": "12",
            "CRAG_SHUTDOWN_GRACE_SECONDS": "4",
            "CRAG_RECEIVED_TIMEOUT_SECONDS": "15",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            worker_module.socket, "gethostname", return_value="host name!"
        ):
            settings = WorkerSettings.from_env()
        self.assertEqual(settings.worker_id, "worker-host-name-")
        self.assertEqual(settings.concurrency, 3)
        self.assertEqual(settings.lease_seconds, 20)
        self.assertEqual(settings.heartbeat_seconds, 3)
        with patch.dict(
            os.environ, {"CRAG_WORKER_CONCURRENCY": "not-an-int"}, clear=True
        ), self.assertRaises(Exception):
            WorkerSettings.from_env()

    def test_retry_headers_and_failure_categories_cover_edge_cases(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        class HeaderError(Exception):
            status_code = 429

            def __init__(self, headers):
                self.headers = headers

        date_limited = classify_failure(
            HeaderError({"Retry-After": "Thu, 01 Jan 2026 00:00:30 GMT"}),
            job_id="1" * 32,
            attempt_count=1,
            now=now,
        )
        self.assertGreater(date_limited.delay_seconds, 0)
        self.assertEqual(
            date_limited.not_before,
            datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc),
        )
        epoch_limited = classify_failure(
            HeaderError(
                {
                    "X-RateLimit-Reset": str(int(now.timestamp()) + 20),
                    "X-RateLimit-Reset-Requests": "2m",
                    "X-RateLimit-Reset-Tokens": "999999999h",
                }
            ),
            job_id="2" * 32,
            attempt_count=2,
            now=now,
        )
        self.assertEqual(epoch_limited.delay_seconds, 7 * 86400)
        self.assertEqual(
            epoch_limited.not_before,
            datetime.fromtimestamp(int(now.timestamp()) + 20, timezone.utc),
        )
        invalid_limited = classify_failure(
            HeaderError({"Retry-After": "not-a-date", "X-RateLimit-Reset": "bad"}),
            job_id="3" * 32,
            attempt_count=1,
            now=now,
        )
        self.assertGreater(invalid_limited.delay_seconds, 0)

        class StatusError(Exception):
            def __init__(self, status_code):
                self.status_code = status_code

        cases = (
            (SystemExit(), "authentication", False),
            (AuthorizationDenied("denied"), "authorization", False),
            (StatusError(403), "authorization", False),
            (StatusError(400), "schema_policy", False),
            (InvalidRequest("bad"), "schema_policy", False),
            (TimeoutError(), "transient_network", True),
            (ConnectionError(), "transient_network", True),
            (ExternalCommandError("command"), "external_command", False),
            (RuntimeError("unknown"), "internal", False),
        )
        for error, category, retryable in cases:
            with self.subTest(error=type(error).__name__):
                decision = classify_failure(
                    error, job_id="4" * 32, attempt_count=1, now=now
                )
                self.assertEqual(decision.category, category)
                self.assertEqual(decision.retryable, retryable)

    def test_absolute_rate_limit_reset_uses_database_clock_and_backoff_floor(self):
        store = self.make_store()
        service = self.make_service(store=store)
        submitted = service.submit_diff("owner/repo", diff_for(39))
        database_now = datetime.now(timezone.utc) + timedelta(seconds=1)
        not_before = database_now + timedelta(seconds=60)
        lease = store.claim("rate-reset-worker", lease_seconds=30, now=database_now)
        self.assertIsNotNone(lease)
        assert lease is not None
        store.mark_running(lease, now=database_now)
        outcome = store.finish_failure(
            lease,
            "rate_limit",
            retryable=True,
            trace_key=None,
            usage={"llm_calls": 0},
            delay_seconds=3,
            available_at=not_before,
            now=database_now,
        )
        self.assertTrue(outcome.retry_scheduled)
        self.assertEqual(outcome.available_at, not_before)
        self.assertEqual(store.get(submitted["review_id"])["state"], "queued")

    def test_retry_lifecycle_exhausts_into_dead_letter(self):
        store = self.make_store()
        service = self.make_service(store=store)
        submitted = service.submit_diff("owner/repo", diff_for(40))
        base = datetime.now(timezone.utc) + timedelta(seconds=1)
        for attempt in range(1, 4):
            current = base + timedelta(seconds=attempt)
            lease = store.claim(
                f"retry-worker-{attempt}", lease_seconds=10, now=current
            )
            self.assertIsNotNone(lease)
            assert lease is not None
            self.assertEqual(lease.attempt_count, attempt)
            store.mark_running(lease, now=current)
            trace = store.trace_path_for_lease(lease)
            trace.write_text(
                json.dumps({"attempt": attempt}) + "\n", encoding="utf-8"
            )
            outcome = store.finish_failure(
                lease,
                "provider_5xx",
                retryable=True,
                trace_key=trace.name,
                usage={"llm_calls": 0},
                available_at=current,
                now=current,
            )
            self.assertEqual(outcome.retry_scheduled, attempt < 3)
        terminal = store.get(submitted["review_id"])
        self.assertEqual(terminal["state"], JobState.DEAD_LETTER.value)
        self.assertEqual(terminal["error"]["code"], "provider_5xx")
        self.assertFalse(list(store.job_data_dir.glob("*.diff")))
        with store.database.engine.connect() as connection:
            attempts = connection.execute(
                text(
                    "SELECT attempt_count FROM provider_usage "
                    "WHERE review_job_id=:job ORDER BY attempt_count"
                ),
                {"job": submitted["review_id"]},
            ).scalars().all()
            transitions = connection.execute(
                text(
                    "SELECT action, reason_code FROM audit_events "
                    "WHERE resource_id=:job AND auth_method='durable_worker' "
                    "ORDER BY occurred_at_utc, id"
                ),
                {"job": submitted["review_id"]},
            ).all()
        self.assertEqual(list(map(int, attempts)), [1, 2, 3])
        self.assertEqual(
            [tuple(item) for item in transitions],
            [
                ("review.retry_scheduled", "provider_5xx"),
                ("review.retry_scheduled", "provider_5xx"),
                ("review.dead_letter", "provider_5xx"),
            ],
        )

    def test_fake_runner_is_exclusive_and_trace_usage_is_bounded(self):
        request = ReviewRequest(
            job_id="5" * 32,
            source_kind="diff",
            repository="owner/repo",
            repo_root=self.repo,
            source_ref="inline",
            diff=diff_for(5),
        )
        trace_path = self.root / "fake-trace.jsonl"
        with patch.object(worker_module.time, "sleep") as sleep:
            result = FakeReviewRunner(delay_seconds=0.1)(request, trace_path)
        sleep.assert_called_once_with(0.1)
        self.assertEqual(result, {"summary": "fake-run", "findings": []})
        self.assertIn("fake_review_completed", trace_path.read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            FakeReviewRunner()(request, trace_path)

        with patch.object(
            worker_module, "load_span_records", return_value=[]
        ), patch.object(
            worker_module,
            "aggregate_trace",
            return_value={
                "input_tokens": 3,
                "output_tokens": 4,
                "cost_microusd": 5,
                "llm_calls": 2,
            },
        ), patch.dict(os.environ, {"LLM_PROVIDER": "fake", "LLM_MODEL": "model"}):
            usage = worker_module._trace_usage(trace_path)
        self.assertEqual(usage["llm_calls"], 2)
        self.assertEqual(usage["provider"], "fake")
        with patch.object(
            worker_module, "load_span_records", side_effect=ValueError("bad trace")
        ):
            self.assertIsNone(worker_module._trace_usage(trace_path))

    def test_worker_secret_files_and_environment_factory(self):
        state = self.root / "worker-env"
        bootstrap = JobStore(state)
        database_url = bootstrap.database_url
        bootstrap.close()
        secret = self.root / "provider-secret"
        secret.write_text("provider-value\n", encoding="utf-8")
        base = {
            "CRAG_STATE_DIR": str(state),
            "CRAG_DATABASE_URL": database_url,
            "CRAG_REPOSITORIES_JSON": json.dumps(
                {"owner/repo": str(self.repo.resolve())}
            ),
            "CRAG_WORKER_ID": "environment-worker",
            "CRAG_JOB_LEASE_SECONDS": "10",
            "CRAG_JOB_HEARTBEAT_SECONDS": "1",
        }
        with patch.dict(
            os.environ,
            {**base, "CRAG_WORKER_RUNNER": "fake", "CRAG_FAKE_RUN_SECONDS": "0.2"},
            clear=True,
        ):
            fake_worker = create_worker_from_env()
        self.assertIsInstance(fake_worker.runner, FakeReviewRunner)
        self.assertEqual(fake_worker.worker_id, "environment-worker")
        fake_worker.store.close()

        with patch.dict(
            os.environ,
            {
                **base,
                "CRAG_WORKER_RUNNER": "real",
                "DEEPSEEK_API_KEY_FILE": str(secret),
            },
            clear=True,
        ):
            self.assertEqual(
                llm_module._api_key_from_environment(("DEEPSEEK_API_KEY",)),
                "provider-value",
            )
            real_worker = create_worker_from_env()
            self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
            self.assertNotIn("DEEPSEEK_API_KEY_FILE", os.environ)
            self.assertNotIn("CRAG_DATABASE_URL", os.environ)
        self.assertIsInstance(real_worker.runner, worker_module.DefaultReviewRunner)
        real_worker.store.close()

        for extra in (
            {"CRAG_WORKER_RUNNER": "unknown"},
            {"CRAG_WORKER_RUNNER": "fake", "CRAG_FAKE_RUN_SECONDS": "bad"},
        ):
            with patch.dict(os.environ, {**base, **extra}, clear=True), self.assertRaises(
                Exception
            ):
                create_worker_from_env()

        missing = self.root / "missing-secret"
        empty = self.root / "empty-secret"
        empty.write_text("", encoding="utf-8")
        for path in (missing, empty):
            with patch.dict(
                os.environ, {"DEEPSEEK_API_KEY_FILE": str(path)}, clear=True
            ), self.assertRaises(Exception):
                worker_module._validate_provider_credentials()
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "already-present", "DEEPSEEK_API_KEY_FILE": str(missing)},
            clear=True,
        ), self.assertRaises(Exception):
            worker_module._validate_provider_credentials()

    def test_explicit_state_does_not_evaluate_unavailable_home(self):
        state = self.root / "explicit-state"
        bootstrap = JobStore(state)
        database_url = bootstrap.database_url
        bootstrap.close()
        environment = {
            "CRAG_STATE_DIR": str(state),
            "CRAG_DATABASE_URL": database_url,
            "CRAG_REPOSITORIES_JSON": json.dumps(
                {"owner/repo": str(self.repo.resolve())}
            ),
            "CRAG_ALLOW_LOCAL_TOKEN": "true",
        }
        with patch.dict(
            os.environ, {"CRAG_DATABASE_URL": database_url}, clear=True
        ), patch.object(
            database_module.Path,
            "home",
            side_effect=AssertionError("home must not be evaluated"),
        ):
            self.assertEqual(database_module._default_database_url(), database_url)
        with patch.dict(os.environ, environment, clear=True), patch.object(
            database_module.Path,
            "home",
            side_effect=AssertionError("home must not be evaluated"),
        ):
            self.assertEqual(database_module._default_database_url(), database_url)
            service = service_core_module.create_review_service_from_env(
                runner=FakeReviewRunner()
            )
        service.shutdown()

    def test_service_factory_rejects_startup_auto_migration(self):
        environment = {
            "CRAG_STATE_DIR": str(self.root / "state"),
            "CRAG_REPOSITORIES_JSON": json.dumps(
                {"owner/repo": str(self.repo.resolve())}
            ),
            "CRAG_ALLOW_LOCAL_TOKEN": "true",
            "CRAG_AUTO_MIGRATE": "true",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            service_core_module, "JobStore"
        ) as job_store, self.assertRaisesRegex(
            InvalidRequest, "run crag-db upgrade explicitly"
        ):
            service_core_module.create_review_service_from_env()
        job_store.assert_not_called()

    def test_worker_cli_check_and_signal_paths_close_the_store(self):
        for ready, exit_code in ((True, 0), (False, 1)):
            store = Mock()
            store.database_ready.return_value = ready
            store.worker_is_live.return_value = ready
            worker = SimpleNamespace(
                store=store,
                worker_id="cli-worker",
                settings=SimpleNamespace(stale_seconds=2),
            )
            with patch.object(worker_module, "create_worker_from_env", return_value=worker):
                with self.assertRaises(SystemExit) as stopped:
                    worker_module.main(["--check"])
            self.assertEqual(stopped.exception.code, exit_code)
            store.close.assert_called_once()

        store = Mock()
        worker = SimpleNamespace(
            store=store,
            worker_id="cli-worker",
            settings=SimpleNamespace(stale_seconds=2),
            request_shutdown=Mock(),
            run_forever=Mock(),
        )
        handlers = []

        def register(signum, callback):
            handlers.append((signum, callback))

        with patch.object(
            worker_module, "create_worker_from_env", return_value=worker
        ), patch.object(worker_module.signal, "signal", side_effect=register):
            worker_module.main([])
        self.assertEqual(len(handlers), 2)
        handlers[0][1](handlers[0][0], None)
        worker.request_shutdown.assert_called_once()
        worker.run_forever.assert_called_once()
        store.close.assert_called_once()

    def test_worker_retains_lease_lost_job_until_execution_finishes(self):
        store = self.make_store()
        service = self.make_service(store=store)
        submitted = service.submit_diff("owner/repo", diff_for(88))
        lease = store.claim("heartbeat-loss", lease_seconds=10)
        self.assertIsNotNone(lease)
        assert lease is not None
        worker = ReviewWorker(
            self.registry,
            store,
            runner=FakeReviewRunner(),
            worker_id="heartbeat-loss",
            concurrency=1,
            lease_seconds=10,
            heartbeat_seconds=1,
        )
        done = threading.Event()
        active = worker_module._ActiveJob(
            lease=lease, thread=threading.Thread(), done=done
        )
        worker._active[submitted["review_id"]] = active
        with patch.object(
            store, "heartbeat", side_effect=LeaseLost("lost")
        ) as heartbeat:
            worker._heartbeat_active()
            worker._heartbeat_active()
        heartbeat.assert_called_once()
        self.assertTrue(active.lease_lost)
        self.assertEqual(worker._active_count(), 1)
        done.set()
        worker._heartbeat_active()
        self.assertEqual(worker._active_count(), 0)

    def test_worker_leaves_uncertain_completion_for_lease_recovery(self):
        store = self.make_store()
        service = self.make_service(store=store)
        submitted = service.submit_diff("owner/repo", diff_for(89))
        lease = store.claim("uncertain-completion", lease_seconds=10)
        self.assertIsNotNone(lease)
        assert lease is not None
        worker = ReviewWorker(
            self.registry,
            store,
            runner=FakeReviewRunner(),
            worker_id="uncertain-completion",
            concurrency=1,
            lease_seconds=10,
            heartbeat_seconds=1,
        )
        active = worker_module._ActiveJob(
            lease=lease,
            thread=threading.Thread(),
            done=threading.Event(),
        )
        with patch.object(
            store, "complete", side_effect=SQLAlchemyError("connection lost")
        ), patch.object(store, "finish_failure") as finish_failure:
            worker._execute(active)
        self.assertTrue(active.done.is_set())
        finish_failure.assert_not_called()
        self.assertEqual(store.get(submitted["review_id"])["state"], "running")

    def test_received_reconciliation_does_not_block_worker_heartbeat_loop(self):
        entered = threading.Event()
        release = threading.Event()
        store = Mock()

        def reconcile(**kwargs):
            self.assertEqual(kwargs["batch_size"], 8)
            entered.set()
            release.wait(2)

        store.reconcile_received.side_effect = reconcile
        worker = ReviewWorker(
            self.registry,
            store,
            runner=FakeReviewRunner(),
            worker_id="maintenance-worker",
            concurrency=1,
            lease_seconds=10,
            heartbeat_seconds=1,
        )
        started = time.monotonic()
        worker._start_received_reconciliation()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(entered.wait(1))
        worker._start_received_reconciliation()
        store.reconcile_received.assert_called_once()
        release.set()
        assert worker._maintenance is not None
        worker._maintenance.join(2)

    def test_webhook_different_deliveries_same_head_have_one_logical_job(self):
        service = self.make_service()
        first, duplicate = service.submit_webhook_pr(
            "owner/repo", "8", delivery_id="delivery-a", head_sha=HEAD_SHA
        )
        second, duplicate2 = service.submit_webhook_pr(
            "owner/repo", "8", delivery_id="delivery-b", head_sha=HEAD_SHA
        )
        self.assertFalse(duplicate)
        self.assertTrue(duplicate2)
        self.assertEqual(first["review_id"], second["review_id"])

    def test_http_webhook_ack_does_not_wait_for_blocked_review(self):
        gate = threading.Event()
        runner = RecordingRunner(gate=gate)
        store = self.make_store()
        service = self.make_service(store=store, runner=runner)
        app = create_app(
            settings=HttpSettings(service_token=TOKEN, webhook_secret=SECRET),
            review_service=service,
        )
        payload = {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 8, "head": {"sha": HEAD_SHA}},
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = "sha256=" + hmac.new(
            SECRET.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        try:
            with TestClient(app) as client:
                started = time.monotonic()
                first = client.post(
                    "/webhooks/github",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "http-delivery-a",
                        "X-Hub-Signature-256": signature,
                    },
                )
                elapsed = time.monotonic() - started
                self.assertEqual(first.status_code, 202, first.text)
                self.assertLess(elapsed, 1.0)
                self.assertTrue(runner.started.wait(2))
                replay = client.post(
                    "/webhooks/github",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Event": "pull_request",
                        "X-GitHub-Delivery": "http-delivery-b",
                        "X-Hub-Signature-256": signature,
                    },
                )
                self.assertEqual(replay.status_code, 202, replay.text)
                self.assertTrue(replay.json()["duplicate"])
                self.assertEqual(
                    replay.json()["review_id"], first.json()["review_id"]
                )
                gate.set()
        finally:
            gate.set()


class Phase9CBuildContextTests(unittest.TestCase):
    def _make_source_tree(self, root: Path) -> tuple[str, ...]:
        tracked = (
            "migrations/env.py",
            "src/code_review_agent/__init__.py",
        )
        for relative in (
            *container_test_module.BUILD_FILES,
            *container_test_module.PHASE9C_BUILD_FILES,
            *tracked,
        ):
            path = root.joinpath(*relative.replace("\\", "/").split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture for {relative}\n", encoding="utf-8")
        return tracked

    def test_filtered_build_context_excludes_untracked_and_bytecode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            tracked = self._make_source_tree(root)
            untracked = root / "src" / "code_review_agent" / "local_secret.py"
            untracked.write_text("secret = 'must-not-copy'\n", encoding="utf-8")
            bytecode = (
                root
                / "src"
                / "code_review_agent"
                / "__pycache__"
                / "worker.cpython-313.pyc"
            )
            bytecode.parent.mkdir()
            bytecode.write_bytes(str(root).encode("utf-8"))
            destination = Path(temporary) / "context"

            with patch.object(container_test_module, "ROOT", root), patch.object(
                container_test_module, "_tracked_build_files", return_value=tracked
            ):
                container_test_module.prepare_context(destination)

            copied = {
                path.relative_to(destination).as_posix()
                for path in destination.rglob("*")
                if path.is_file()
            }
            expected = set(container_test_module.BUILD_FILES) | set(
                container_test_module.PHASE9C_BUILD_FILES
            ) | set(tracked)
            self.assertEqual(copied, expected)
            self.assertFalse((destination / untracked.relative_to(root)).exists())
            self.assertFalse((destination / bytecode.relative_to(root)).exists())

    def test_filtered_build_context_rejects_host_path_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            tracked = self._make_source_tree(root)
            (root / "README.md").write_text(str(root), encoding="utf-8")
            destination = Path(temporary) / "context"

            with patch.object(container_test_module, "ROOT", root), patch.object(
                container_test_module, "_tracked_build_files", return_value=tracked
            ), self.assertRaisesRegex(
                container_test_module.HarnessError, "repository host path"
            ):
                container_test_module.prepare_context(destination)

    def test_container_command_start_failure_does_not_disclose_host_path(self):
        executable = r"C:\Program Files\Docker\docker.exe"
        with patch.object(
            container_test_module.subprocess,
            "run",
            side_effect=OSError("launch failed"),
        ), self.assertRaises(container_test_module.HarnessError) as raised:
            container_test_module._run((executable, "version"))
        self.assertEqual(
            str(raised.exception), "container command could not run: docker.exe"
        )
        self.assertNotIn("Program Files", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
