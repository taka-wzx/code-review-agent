from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import text

from code_review_agent.identity import FakeAuthBackend, Principal, Role
from code_review_agent.database import Database, upgrade_database
from code_review_agent.repair_publish import FakeDraftPrPublisher
from code_review_agent.repair_service import (
    CommitReceipt,
    OrganizationRepairPolicy,
    PostgresRepairStore,
    RepairAuthorizationError,
    RepairConflict,
    RepairJobCheckpoint,
    RepairPlanArtifact,
    RepairServiceError,
    ReflectionDecision,
    ReflectionReceipt,
    StartRepairRequest,
    SyntheticRepairExecutor,
    SyntheticRepairPlanner,
    SyntheticRepairWorker,
    SyntheticStagingRepairService,
    TestEvidence,
    create_synthetic_staging_repair_service,
    synthetic_repair_policy,
    validate_synthetic_runtime_environment,
)
from code_review_agent.service import HttpSettings, create_app, main as service_main
from code_review_agent.service_core import InvalidRequest, RepositoryRegistry, ReviewService
from code_review_agent.service_queue import JobStore


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


class ForcedCrash(BaseException):
    pass


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RetryOncePlanner(SyntheticRepairPlanner):
    """Return one synthetic retry decision, then a synthetic success."""

    def reflect(
        self,
        operation_id: str,
        plan: RepairPlanArtifact,
        tests: tuple[TestEvidence, ...],
    ) -> ReflectionReceipt:
        receipt = super().reflect(operation_id, plan, tests)
        if self.reflection_calls == 1:
            return replace(receipt, decision=ReflectionDecision.RETRY)
        return receipt


class Phase11ASyntheticStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = FakeClock()
        self.review_store = JobStore(self.root / "review-state")
        self.database = self.review_store.database
        self.organization = self.database.create_organization("phase11a", "Phase 11A")
        self.repository = self.database.register_repository(
            self.organization["id"], "owner/synthetic", policy_version="synthetic/v1"
        )
        self.other_organization = self.database.create_organization("phase11b", "Other")
        self.other_repository = self.database.register_repository(
            self.other_organization["id"], "owner/other", policy_version="other/v1"
        )
        self.maintainer = self._member(
            self.organization["id"], "maintainer", Role.MAINTAINER, (self.repository["id"],)
        )
        self.admin = self._member(self.organization["id"], "admin", Role.ORG_ADMIN, ())
        self.viewer = self._member(
            self.organization["id"], "viewer", Role.VIEWER, (self.repository["id"],)
        )
        self.reviewer = self._member(
            self.organization["id"], "reviewer", Role.REVIEWER, (self.repository["id"],)
        )
        self.other_maintainer = self._member(
            self.other_organization["id"],
            "other-maintainer",
            Role.MAINTAINER,
            (self.other_repository["id"],),
        )
        self.repair_store = PostgresRepairStore(
            self.database,
            clock=self.clock,
            allow_sqlite_for_tests=True,
        )
        self.planner = SyntheticRepairPlanner()
        self.executor = SyntheticRepairExecutor()
        self.publisher = FakeDraftPrPublisher()
        self.service = self._new_service()

    def tearDown(self) -> None:
        self.review_store.close()
        self.temp.cleanup()

    def _member(
        self,
        organization_id: str,
        subject: str,
        role: Role,
        repository_ids: tuple[str, ...],
    ) -> Principal:
        member = self.database.create_membership(
            organization_id,
            subject=subject,
            display_name=subject,
            role=role,
            repository_ids=repository_ids,
        )
        principal = self.database.principal_for_user(organization_id, member["user_id"])
        self.assertIsNotNone(principal)
        assert principal is not None
        return principal

    def _new_service(
        self, *, fault=None, fresh_adapters: bool = False
    ) -> SyntheticStagingRepairService:
        planner = SyntheticRepairPlanner() if fresh_adapters else self.planner
        executor = SyntheticRepairExecutor() if fresh_adapters else self.executor
        publisher = FakeDraftPrPublisher() if fresh_adapters else self.publisher
        return SyntheticStagingRepairService(
            self.repair_store,
            planner=planner,
            executor=executor,
            publisher=publisher,
            clock=self.clock,
            fault=fault,
        )

    @staticmethod
    def _finding(index: int) -> str:
        return hashlib.sha256(f"synthetic-finding-{index}".encode("utf-8")).hexdigest()

    def _start(self, index: int = 0) -> dict:
        return self.service.start_synthetic_repair(
            organization_id=self.organization["id"],
            repository_id=self.repository["id"],
            finding_sha256=self._finding(index),
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            actor=self.maintainer,
        )

    def _plan(self, job_id: str) -> dict:
        return self.service.run_worker_once(job_id, worker_id="repair-worker-a")

    def _approve_write(self, job_id: str) -> dict:
        view = self.service.write_approval_view(job_id, actor=self.maintainer)
        return self.service.decide_write(
            job_id,
            actor=self.maintainer,
            checkpoint_sha256=view["checkpoint_sha256"],
            approval_id=view["approval_id"],
            approved=True,
        )

    def _approve_draft(self, job_id: str) -> dict:
        view = self.service.draft_pr_approval_view(job_id, actor=self.maintainer)
        return self.service.decide_draft_pr(
            job_id,
            actor=self.maintainer,
            checkpoint_sha256=view["checkpoint_sha256"],
            approval_id=view["approval_id"],
            approved=True,
        )

    def _complete(self, index: int) -> dict:
        started = self._start(index)
        job_id = started["job_id"]
        self.assertEqual("awaiting_write_approval", self._plan(job_id)["state"])
        self.assertEqual("queued_execution", self._approve_write(job_id)["state"])
        self.assertEqual("awaiting_draft_pr_approval", self._plan(job_id)["state"])
        self.assertEqual("queued_publish", self._approve_draft(job_id)["state"])
        return self._plan(job_id)

    def test_postgres_store_rejects_sqlite_outside_test_mode(self) -> None:
        with self.assertRaisesRegex(Exception, "synthetic_staging_postgres_required"):
            PostgresRepairStore(self.database, clock=self.clock)

    def test_thirty_synthetic_repairs_complete_without_real_effects(self) -> None:
        results = [self._complete(index) for index in range(30)]
        self.assertTrue(all(result["state"] == "draft_published" for result in results))
        self.assertTrue(all(result["synthetic_only"] for result in results))
        self.assertTrue(all(not result["real_writes_enabled"] for result in results))
        self.assertEqual(30, self.planner.plan_calls)
        self.assertEqual(30, self.planner.reflection_calls)
        self.assertEqual(30, self.executor.execution_calls)
        self.assertEqual(30, self.executor.commit_calls)
        self.assertEqual(30, len(self.publisher.calls))
        self.assertTrue(self.repair_store.backup_restore_consistent())

    def test_write_approval_is_one_use_and_concurrent(self) -> None:
        job_id = self._start()["job_id"]
        self._plan(job_id)
        view = self.service.write_approval_view(job_id, actor=self.maintainer)
        outcomes: list[str] = []

        def decide() -> None:
            try:
                value = self.service.decide_write(
                    job_id,
                    actor=self.maintainer,
                    checkpoint_sha256=view["checkpoint_sha256"],
                    approval_id=view["approval_id"],
                    approved=True,
                )
                outcomes.append(value["state"])
            except RepairConflict:
                outcomes.append("conflict")

        threads = [threading.Thread(target=decide), threading.Thread(target=decide)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(["conflict", "queued_execution"], sorted(outcomes))
        with self.assertRaises(RepairConflict):
            self.service.decide_write(
                job_id,
                actor=self.maintainer,
                checkpoint_sha256=view["checkpoint_sha256"],
                approval_id=view["approval_id"],
                approved=True,
            )

    def test_approval_expiry_and_checkpoint_mismatch_fail_closed(self) -> None:
        job_id = self._start()["job_id"]
        self._plan(job_id)
        view = self.service.write_approval_view(job_id, actor=self.maintainer)
        with self.assertRaises(RepairConflict):
            self.service.decide_write(
                job_id,
                actor=self.maintainer,
                checkpoint_sha256="0" * 64,
                approval_id=view["approval_id"],
                approved=True,
            )
        self.clock.advance(301)
        with self.assertRaisesRegex(RepairConflict, "expired"):
            self.service.decide_write(
                job_id,
                actor=self.maintainer,
                checkpoint_sha256=view["checkpoint_sha256"],
                approval_id=view["approval_id"],
                approved=True,
            )
        self.assertEqual("awaiting_write_approval", self.service.get_repair(job_id, actor=self.maintainer)["state"])

    def test_restart_after_plan_receipt_has_no_duplicate_model_call(self) -> None:
        job_id = self._start()["job_id"]

        def fault(point: str) -> None:
            if point == "after_plan_result_persisted":
                raise ForcedCrash()

        crashing = self._new_service(fault=fault)
        with self.assertRaises(ForcedCrash):
            crashing.run_worker_once(job_id, worker_id="repair-worker-a")
        self.assertEqual(1, self.planner.plan_calls)
        restarted = self._new_service(fresh_adapters=True)
        self.assertEqual(
            "awaiting_write_approval",
            restarted.run_worker_once(job_id, worker_id="repair-worker-b")["state"],
        )
        self.assertEqual(0, restarted.planner.plan_calls)

    def test_restart_after_mutation_intent_quarantines_without_replay(self) -> None:
        job_id = self._start()["job_id"]
        self._plan(job_id)
        self._approve_write(job_id)

        def fault(point: str) -> None:
            if point == "after_execution_intent_persisted":
                raise ForcedCrash()

        crashing = self._new_service(fault=fault)
        with self.assertRaises(ForcedCrash):
            crashing.run_worker_once(job_id, worker_id="repair-worker-a")
        self.assertEqual(0, self.executor.execution_calls)
        self.clock.advance(31)
        restarted = self._new_service(fresh_adapters=True)
        result = restarted.run_worker_once(job_id, worker_id="repair-worker-b")
        self.assertEqual("quarantined", result["state"])
        self.assertEqual(0, restarted.executor.execution_calls)

    def test_restart_after_publish_intent_quarantines_without_republication(self) -> None:
        job_id = self._start()["job_id"]
        self._plan(job_id)
        self._approve_write(job_id)
        self._plan(job_id)
        self._approve_draft(job_id)

        def fault(point: str) -> None:
            if point == "after_publish_intent_persisted":
                raise ForcedCrash()

        crashing = self._new_service(fault=fault)
        with self.assertRaises(ForcedCrash):
            crashing.run_worker_once(job_id, worker_id="repair-worker-a")
        self.assertEqual(0, len(self.publisher.calls))
        self.clock.advance(31)
        restarted = self._new_service(fresh_adapters=True)
        result = restarted.run_worker_once(job_id, worker_id="repair-worker-b")
        self.assertEqual("quarantined", result["state"])
        self.assertEqual(0, len(restarted.publisher.calls))

    def test_roles_cross_organization_and_redaction(self) -> None:
        job_id = self._start()["job_id"]
        self.assertEqual("queued_plan", self.service.get_repair(job_id, actor=self.viewer)["state"])
        with self.assertRaises(RepairAuthorizationError):
            self.service.get_repair(job_id, actor=self.other_maintainer)
        for principal in (self.viewer, self.reviewer):
            with self.assertRaises(RepairAuthorizationError):
                self.service.start_synthetic_repair(
                    organization_id=self.organization["id"],
                    repository_id=self.repository["id"],
                    finding_sha256=self._finding(99),
                    base_sha=BASE_SHA,
                    head_sha=HEAD_SHA,
                    actor=principal,
                )
        self._plan(job_id)
        receipt = self.service.redacted_receipt(job_id, actor=self.maintainer)
        serialized = json.dumps(receipt, sort_keys=True)
        self.assertNotIn("synthetic staging repair", serialized)
        self.assertNotIn("diff --git", serialized)
        self.assertFalse(receipt["business_claim_allowed"])
        self.assertFalse(receipt["quality_claim_allowed"])
        self.assertFalse(receipt["production_ready"])

    def test_worker_heartbeat_and_api_rbac(self) -> None:
        repository_root = self.root / "api-repository"
        repository_root.mkdir()
        (repository_root / ".git").mkdir()
        registry = RepositoryRegistry.from_json(
            json.dumps({"owner/synthetic": str(repository_root)})
        )
        review_service = ReviewService(registry, self.review_store, local_mode=False)
        settings = HttpSettings(
            service_token="",
            webhook_secret="phase11a-webhook-secret-value",
            allowed_origins=frozenset({"http://localhost"}),
            allowed_hosts=frozenset({"testserver"}),
            local_token_enabled=False,
        )
        auth = FakeAuthBackend(
            {"maintainer": self.maintainer, "viewer": self.viewer, "reviewer": self.reviewer}
        )
        worker = SyntheticRepairWorker(self.service, worker_id="api-repair-worker")
        worker.run_once()
        with TestClient(
            create_app(
                settings=settings,
                review_service=review_service,
                auth_backend=auth,
                repair_service=self.service,
            )
        ) as client:
            body = {
                "repository": self.repository["id"],
                "finding_sha256": self._finding(5),
                "base_sha": BASE_SHA,
                "head_sha": HEAD_SHA,
            }
            unauthorized = client.post("/v1/repairs", json=body, headers={"Authorization": "Bearer viewer"})
            self.assertEqual(403, unauthorized.status_code)
            created = client.post(
                "/v1/repairs", json=body, headers={"Authorization": "Bearer maintainer"}
            )
            self.assertEqual(202, created.status_code)
            job_id = created.json()["job_id"]
            self.service.run_worker_once(job_id, worker_id="api-repair-worker")
            view = client.get(
                f"/v1/repairs/{job_id}/write-approval",
                headers={"Authorization": "Bearer maintainer"},
            )
            self.assertEqual(200, view.status_code)
            self.assertIn("plan", view.json())
            denied_view = client.get(
                f"/v1/repairs/{job_id}/write-approval",
                headers={"Authorization": "Bearer reviewer"},
            )
            self.assertEqual(403, denied_view.status_code)

    def test_synthetic_runtime_rejects_real_provider_or_writer(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "CRAG_REPAIR_RUNTIME": "synthetic",
                "CRAG_REPAIR_PROVIDER": "real",
                "CRAG_REPAIR_PUBLISHER": "dry_run",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(Exception, "real_provider_denied"):
                validate_synthetic_runtime_environment()
        with patch.dict(
            "os.environ",
            {
                "CRAG_REPAIR_RUNTIME": "synthetic",
                "CRAG_REPAIR_PROVIDER": "synthetic",
                "CRAG_REPAIR_PUBLISHER": "github",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(Exception, "github_writer_denied"):
                validate_synthetic_runtime_environment()

    def test_compose_and_sandbox_declare_synthetic_no_egress_boundaries(self) -> None:
        root = Path(__file__).resolve().parents[1]
        compose = (root / "compose.service.yml").read_text(encoding="utf-8")
        service_dockerfile = (root / "Dockerfile.service").read_text(encoding="utf-8")
        repair_dockerfile = (root / "Dockerfile.repair").read_text(encoding="utf-8")
        self.assertIn("repair-worker:", compose)
        self.assertIn("CRAG_REPAIR_RUNTIME: synthetic", compose)
        self.assertIn("internal: true", compose)
        self.assertNotIn("provider_api_key", compose)
        self.assertNotIn("CRAG_REPOSITORY_ROOT", compose)
        self.assertNotIn("/repositories", compose)
        self.assertIn("synthetic/repository", compose)
        self.assertIn(
            "DEBIAN_MIRROR: ${CRAG_DEBIAN_MIRROR:-https://deb.debian.org}", compose
        )
        self.assertIn(
            "PYPI_INDEX_URL: ${CRAG_PYPI_INDEX_URL:-https://pypi.org/simple}", compose
        )
        self.assertIn(
            "CRAG_LOCAL_TOKEN_BEHIND_LOOPBACK_PUBLISH: "
            "${CRAG_LOCAL_TOKEN_BEHIND_LOOPBACK_PUBLISH:-false}",
            compose,
        )
        self.assertIn('"127.0.0.1:${CRAG_PUBLISHED_PORT:-8000}:8000"', compose)
        self.assertEqual(2, compose.count("timeout: 30s"))
        for dockerfile in (service_dockerfile, repair_dockerfile):
            self.assertIn("ARG DEBIAN_MIRROR=https://deb.debian.org", dockerfile)
            self.assertIn("https://deb.debian.org|https://mirrors.aliyun.com", dockerfile)
            self.assertIn('echo "unsupported DEBIAN_MIRROR" >&2; exit 64', dockerfile)
            self.assertIn("ARG PYPI_INDEX_URL=https://pypi.org/simple", dockerfile)
            self.assertIn(
                "https://pypi.org/simple|https://mirrors.aliyun.com/pypi/simple/", dockerfile
            )
            self.assertIn('echo "unsupported PYPI_INDEX_URL" >&2; exit 64', dockerfile)
            self.assertIn('--index-url "$PYPI_INDEX_URL"', dockerfile)
        self.assertIn("USER 65532:65532", repair_dockerfile)
        self.assertIn("--no-create-home", repair_dockerfile)

    def test_local_token_container_bind_requires_trusted_loopback_publication(self) -> None:
        untrusted = HttpSettings(
            service_token="s" * 32,
            webhook_secret="phase11a-webhook-secret-value",
        )
        with patch(
            "code_review_agent.service.HttpSettings.from_env", return_value=untrusted
        ):
            with self.assertRaisesRegex(SystemExit, "trusted loopback publication"):
                service_main(["--host", "0.0.0.0"])

        trusted = HttpSettings(
            service_token="s" * 32,
            webhook_secret="phase11a-webhook-secret-value",
            local_token_behind_loopback_publish=True,
        )
        with (
            patch("code_review_agent.service.HttpSettings.from_env", return_value=trusted),
            patch("code_review_agent.service.create_app", return_value=object()),
            patch("code_review_agent.service.uvicorn.run") as uvicorn_run,
        ):
            service_main(["--host", "0.0.0.0"])
        uvicorn_run.assert_called_once()

        with self.assertRaisesRegex(InvalidRequest, "loopback-only hosts"):
            HttpSettings(
                service_token="s" * 32,
                webhook_secret="phase11a-webhook-secret-value",
                allowed_hosts=frozenset({"staging.example.com"}),
                local_token_behind_loopback_publish=True,
            )

    def test_unsafe_repair_values_fail_closed_at_the_schema_boundary(self) -> None:
        policy = synthetic_repair_policy()
        request = self._synthetic_request(policy)
        plan = self.planner.create_plan(
            "plan-validation", request, revision=1, previous_test_sha256=""
        ).plan
        binding = self.executor.provision(
            job_id="repair-validation",
            task_branch="repair/repair-validation",
            repository_id=self.repository["id"],
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
        )
        receipt = self.executor.execute("execution-validation", binding, plan, policy)

        invalid_values = (
            lambda: replace(policy, fixed_test_commands=()),
            lambda: replace(policy, writable_paths=()),
            lambda: replace(policy, max_retries=True),
            lambda: replace(policy, command_timeout_seconds=301.0),
            lambda: replace(policy, command_output_bytes=65_537),
            lambda: OrganizationRepairPolicy.from_dict({"version": "incomplete"}),
            lambda: replace(request, finding_sha256="not-a-digest"),
            lambda: replace(plan, revision=0),
            lambda: replace(plan, patch_text=""),
            lambda: replace(binding, original_checkout_unchanged=False),
            lambda: TestEvidence(argv=(), exit_code=0, duration_seconds=0.0),
            lambda: TestEvidence(argv=("python",), exit_code=True, duration_seconds=0.0),
            lambda: replace(receipt, full_diff="not-the-recorded-diff"),
            lambda: replace(receipt, tests=()),
            lambda: replace(receipt, output_limit_bytes=0),
            lambda: CommitReceipt(
                operation_id="commit-validation",
                commit_sha=BASE_SHA,
                parent_sha=BASE_SHA,
                diff_sha256=receipt.snapshot.diff_sha256,
                message_sha256="c" * 64,
                original_checkout_unchanged=False,
            ),
            lambda: RepairJobCheckpoint.from_dict({"job_id": "invalid"}),
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaises((ValueError, RepairServiceError)):
                    invalid()

    def test_store_rejects_invalid_audit_and_budget_data(self) -> None:
        job_id = self._start(101)["job_id"]
        with self.assertRaisesRegex(Exception, "repair_event_fields_denied"):
            self.repair_store.append_event(job_id, "unsafe", {"diff": "forbidden"})
        with self.assertRaisesRegex(Exception, "repair_event_invalid"):
            self.repair_store.append_event(
                job_id,
                "unsafe",
                {
                    "state": "queued_plan",
                    "outcome": "none",
                    "approval_kind": "none",
                    "attempt": -1,
                    "failure_code": "none",
                },
            )
        self.repair_store.append_event(
            job_id,
            "safe",
            {
                "state": "queued_plan",
                "outcome": "none",
                "approval_kind": "none",
                "attempt": 1,
                "failure_code": "none",
            },
        )

        job, _checksum = self.repair_store.load(job_id)
        job.budget = {"reservations": "not-a-list"}
        with self.assertRaisesRegex(Exception, "repair_budget_reservations_invalid"):
            self.repair_store.save(job)
        job, _checksum = self.repair_store.load(job_id)
        job.budget = {
            "reservations": [
                {"reservation_id": "invalid", "tokens": -1, "cost_usd": 0.0}
            ]
        }
        with self.assertRaisesRegex(Exception, "repair_budget_reservations_invalid"):
            self.repair_store.save(job)
        job, _checksum = self.repair_store.load(job_id)
        job.budget = {
            "reservations": [
                {"reservation_id": "valid", "tokens": 1, "cost_usd": 0.0}
            ]
        }
        self.repair_store.save(job)
        self.assertTrue(self.repair_store.backup_restore_consistent())

    def test_store_approval_validation_and_worker_heartbeats_fail_closed(self) -> None:
        job_id = self._start(102)["job_id"]
        self._plan(job_id)
        view = self.service.write_approval_view(job_id, actor=self.maintainer)
        job, checksum = self.repair_store.load(job_id)
        binding = self.service._write_binding(job, checksum)
        self.repair_store.validate_issued_approval(
            job,
            kind="write",
            approval_id=view["approval_id"],
            checkpoint_sha256=checksum,
            binding=binding,
            now=self.clock(),
        )
        with self.assertRaisesRegex(RepairConflict, "kind_invalid"):
            self.repair_store.validate_issued_approval(
                job,
                kind="unknown",
                approval_id=view["approval_id"],
                checkpoint_sha256=checksum,
                binding=binding,
                now=self.clock(),
            )
        with self.assertRaisesRegex(RepairConflict, "not_found"):
            self.repair_store.validate_issued_approval(
                job,
                kind="write",
                approval_id="approval-missing",
                checkpoint_sha256=checksum,
                binding=binding,
                now=self.clock(),
            )
        changed_binding = dict(binding)
        changed_binding["attempt"] = 99
        with self.assertRaisesRegex(RepairConflict, "binding_mismatch"):
            self.repair_store.validate_issued_approval(
                job,
                kind="write",
                approval_id=view["approval_id"],
                checkpoint_sha256=checksum,
                binding=changed_binding,
                now=self.clock(),
            )
        with self.assertRaisesRegex(RepairConflict, "id_required"):
            self.service.decide_write(
                job_id,
                actor=self.maintainer,
                checkpoint_sha256=checksum,
                approved=True,
            )
        self.service.decide_write(
            job_id,
            actor=self.maintainer,
            checkpoint_sha256=checksum,
            approval_id=view["approval_id"],
            approved=True,
        )
        with self.assertRaisesRegex(RepairConflict, "replayed"):
            self.repair_store.validate_issued_approval(
                job,
                kind="write",
                approval_id=view["approval_id"],
                checkpoint_sha256=checksum,
                binding=binding,
                now=self.clock(),
            )

        self.assertEqual(job_id, self.repair_store.next_claimable_job_id())
        self.repair_store.worker_heartbeat("worker-heartbeat", status="ready", capacity=1)
        self.assertEqual(1, self.repair_store.live_worker_count(stale_seconds=1.0))
        self.repair_store.worker_heartbeat("worker-heartbeat", status="draining", capacity=2)
        self.assertEqual(0, self.repair_store.live_worker_count(stale_seconds=1.0))
        with self.assertRaisesRegex(Exception, "repair_worker_status_invalid"):
            self.repair_store.worker_heartbeat("worker-invalid", status="unknown", capacity=1)
        with self.assertRaisesRegex(Exception, "repair_worker_capacity_invalid"):
            self.repair_store.worker_heartbeat("worker-invalid", status="ready", capacity=0)

    def test_rejections_are_durable_and_release_worker_leases(self) -> None:
        write_job_id = self._start(103)["job_id"]
        self._plan(write_job_id)
        write = self.service.write_approval_view(write_job_id, actor=self.maintainer)
        declined_write = self.service.decide_write(
            write_job_id,
            actor=self.maintainer,
            checkpoint_sha256=write["checkpoint_sha256"],
            approval_id=write["approval_id"],
            approved=False,
        )
        self.assertEqual("declined", declined_write["state"])
        self.assertFalse(declined_write["lease_active"])
        self.assertEqual(0, self.executor.execution_calls)

        draft_job_id = self._start(104)["job_id"]
        self._plan(draft_job_id)
        self._approve_write(draft_job_id)
        self._plan(draft_job_id)
        draft = self.service.draft_pr_approval_view(draft_job_id, actor=self.maintainer)
        declined_draft = self.service.decide_draft_pr(
            draft_job_id,
            actor=self.maintainer,
            checkpoint_sha256=draft["checkpoint_sha256"],
            approval_id=draft["approval_id"],
            approved=False,
        )
        self.assertEqual("declined", declined_draft["state"])
        self.assertFalse(declined_draft["lease_active"])
        self.assertEqual(0, len(self.publisher.calls))
        self.assertTrue(self.repair_store.backup_restore_consistent())

    def test_retry_creates_a_new_plan_and_new_write_approval(self) -> None:
        planner = RetryOncePlanner()
        service = SyntheticStagingRepairService(
            self.repair_store,
            planner=planner,
            executor=self.executor,
            publisher=self.publisher,
            clock=self.clock,
        )
        started = service.start_synthetic_repair(
            organization_id=self.organization["id"],
            repository_id=self.repository["id"],
            finding_sha256=self._finding(105),
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            actor=self.maintainer,
        )
        job_id = started["job_id"]
        service.run_worker_once(job_id, worker_id="retry-worker")
        first_write = service.write_approval_view(job_id, actor=self.maintainer)
        service.decide_write(
            job_id,
            actor=self.maintainer,
            checkpoint_sha256=first_write["checkpoint_sha256"],
            approval_id=first_write["approval_id"],
            approved=True,
        )
        retry = service.run_worker_once(job_id, worker_id="retry-worker")
        self.assertEqual("queued_plan", retry["state"])
        self.assertEqual(2, retry["attempt"])
        self.assertFalse(retry["lease_active"])
        second_plan = service.run_worker_once(job_id, worker_id="retry-worker")
        self.assertEqual("awaiting_write_approval", second_plan["state"])
        second_write = service.write_approval_view(job_id, actor=self.maintainer)
        self.assertNotEqual(first_write["approval_id"], second_write["approval_id"])
        self.assertEqual(2, second_write["plan"]["revision"])
        self.assertEqual(2, planner.plan_calls)
        self.assertEqual(1, planner.reflection_calls)

    def test_publisher_failures_remain_quarantined_and_timeouts_reconcile(self) -> None:
        failing_publisher = FakeDraftPrPublisher(fail=True)
        failing_service = SyntheticStagingRepairService(
            self.repair_store,
            planner=SyntheticRepairPlanner(),
            executor=SyntheticRepairExecutor(),
            publisher=failing_publisher,
            clock=self.clock,
        )
        failed_job_id = self._complete_with(failing_service, 106)
        failed = failing_service.run_worker_once(failed_job_id, worker_id="publisher-failure")
        self.assertEqual("quarantined", failed["state"])
        self.assertEqual("draft_pr_publisher_failed_after_commit", failed["failure_code"])
        self.assertEqual([], failing_publisher.calls)

        timeout_publisher = FakeDraftPrPublisher(timeout_after_persist=True)
        timeout_service = SyntheticStagingRepairService(
            self.repair_store,
            planner=SyntheticRepairPlanner(),
            executor=SyntheticRepairExecutor(),
            publisher=timeout_publisher,
            clock=self.clock,
        )
        timeout_job_id = self._complete_with(timeout_service, 107)
        recovered = timeout_service.run_worker_once(timeout_job_id, worker_id="publisher-timeout")
        self.assertEqual("draft_published", recovered["state"])
        self.assertEqual(1, len(timeout_publisher.calls))

    def test_worker_shutdown_and_runtime_credentials_fail_closed(self) -> None:
        validate_synthetic_runtime_environment(
            {
                "CRAG_REPAIR_RUNTIME": "synthetic",
                "CRAG_REPAIR_PROVIDER": "fake",
                "CRAG_REPAIR_PUBLISHER": "fake",
            }
        )
        with self.assertRaisesRegex(Exception, "real_credential_denied"):
            validate_synthetic_runtime_environment(
                {
                    "CRAG_REPAIR_RUNTIME": "synthetic",
                    "CRAG_REPAIR_PROVIDER": "fake",
                    "CRAG_REPAIR_PUBLISHER": "fake",
                    "GITHUB_TOKEN": "not-accepted",
                }
            )
        with self.assertRaisesRegex(Exception, "publisher_denied"):
            SyntheticStagingRepairService(
                self.repair_store,
                publisher=object(),  # type: ignore[arg-type]
                clock=self.clock,
            )
        created = create_synthetic_staging_repair_service(
            self.database,
            allow_sqlite_for_tests=True,
            clock=self.clock,
            validate_environment=False,
        )
        self.assertTrue(created.synthetic_only)

        worker = SyntheticRepairWorker(self.service, worker_id="shutdown-worker", poll_seconds=0.01)
        self.assertIsNone(worker.run_once())
        worker.request_shutdown()
        worker.run_forever()
        self.assertEqual(0, self.repair_store.live_worker_count(stale_seconds=1.0))

    def _synthetic_request(self, policy: OrganizationRepairPolicy) -> StartRepairRequest:
        return StartRepairRequest(
            organization_id=self.organization["id"],
            repository_id=self.repository["id"],
            finding_sha256=self._finding(999),
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            policy=policy,
        )

    def _complete_with(self, service: SyntheticStagingRepairService, index: int) -> str:
        started = service.start_synthetic_repair(
            organization_id=self.organization["id"],
            repository_id=self.repository["id"],
            finding_sha256=self._finding(index),
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            actor=self.maintainer,
        )
        job_id = started["job_id"]
        self.assertEqual(
            "awaiting_write_approval",
            service.run_worker_once(job_id, worker_id="flow")["state"],
        )
        write = service.write_approval_view(job_id, actor=self.maintainer)
        service.decide_write(
            job_id,
            actor=self.maintainer,
            checkpoint_sha256=write["checkpoint_sha256"],
            approval_id=write["approval_id"],
            approved=True,
        )
        self.assertEqual(
            "awaiting_draft_pr_approval",
            service.run_worker_once(job_id, worker_id="flow")["state"],
        )
        draft = service.draft_pr_approval_view(job_id, actor=self.maintainer)
        service.decide_draft_pr(
            job_id,
            actor=self.maintainer,
            checkpoint_sha256=draft["checkpoint_sha256"],
            approval_id=draft["approval_id"],
            approved=True,
        )
        return job_id

    @unittest.skipUnless(
        os.environ.get("CRAG_PHASE11A_POSTGRES_URL"),
        "set CRAG_PHASE11A_POSTGRES_URL for the local Postgres integration gate",
    )
    def test_postgres_transaction_cas_outbox_and_recovery(self) -> None:
        database_url = os.environ["CRAG_PHASE11A_POSTGRES_URL"]
        upgrade_database(database_url)
        database = Database(database_url)
        try:
            suffix = uuid.uuid4().hex[:12]
            organization = database.create_organization(
                f"phase11a-pg-{suffix}", "Phase 11A Postgres"
            )
            repository = database.register_repository(
                organization["id"], f"owner/postgres-{suffix}", policy_version="synthetic/v1"
            )
            member = database.create_membership(
                organization["id"],
                subject=f"maintainer-{suffix}",
                display_name="Postgres maintainer",
                role=Role.MAINTAINER,
                repository_ids=(repository["id"],),
            )
            principal = database.principal_for_user(organization["id"], member["user_id"])
            self.assertIsNotNone(principal)
            assert principal is not None
            postgres_store = PostgresRepairStore(database)
            service = SyntheticStagingRepairService(postgres_store)
            job = service.start_synthetic_repair(
                organization_id=organization["id"],
                repository_id=repository["id"],
                finding_sha256=self._finding(777),
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
                actor=principal,
            )
            job_id = job["job_id"]
            self.assertEqual(
                "awaiting_write_approval",
                service.run_worker_once(job_id, worker_id="postgres-worker")["state"],
            )
            write = service.write_approval_view(job_id, actor=principal)
            service.decide_write(
                job_id,
                actor=principal,
                checkpoint_sha256=write["checkpoint_sha256"],
                approval_id=write["approval_id"],
                approved=True,
            )
            self.assertEqual(
                "awaiting_draft_pr_approval",
                service.run_worker_once(job_id, worker_id="postgres-worker")["state"],
            )
            draft = service.draft_pr_approval_view(job_id, actor=principal)
            service.decide_draft_pr(
                job_id,
                actor=principal,
                checkpoint_sha256=draft["checkpoint_sha256"],
                approval_id=draft["approval_id"],
                approved=True,
            )
            self.assertEqual(
                "draft_published",
                service.run_worker_once(job_id, worker_id="postgres-worker")["state"],
            )
            self.assertTrue(postgres_store.backup_restore_consistent())
            with database.engine.connect() as connection:
                outbox = connection.execute(
                    text("SELECT status FROM repair_outbox WHERE repair_job_id=:job_id"),
                    {"job_id": job_id},
                ).scalar_one()
            self.assertEqual("succeeded", outbox)
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
