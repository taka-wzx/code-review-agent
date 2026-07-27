from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest

from code_review_agent.identity import Principal, Role
from code_review_agent.repair_publish import (
    DryRunDraftPrPublisher,
    FakeDraftPrPublisher,
    GitHubDraftPrPublisher,
)
from code_review_agent.repair_budget import BudgetLimits
from code_review_agent.repair_service import (
    CommitReceipt,
    ExecutionReceipt,
    OrganizationRepairPolicy,
    Phase10RepairService,
    Phase10RepairStore,
    PlanReceipt,
    ReflectionDecision,
    ReflectionReceipt,
    RepairAuthorizationError,
    RepairConflict,
    RepairJobState,
    RepairPlanArtifact,
    RepairServiceError,
    RepositorySnapshot,
    StartRepairRequest,
    TestEvidence,
    WorktreeBinding,
    _hash_text,
)
from code_review_agent.tracelog import Trace


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
COMMIT_SHA = "c" * 40
FINDING_SHA = "d" * 64
EMPTY_DIFF_SHA = _hash_text("")
PATCH_SENTINEL = "phase10-secret-patch-sentinel"
OUTPUT_SENTINEL = "phase10-secret-output-sentinel"


class ForcedCrash(BaseException):
    pass


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakePlanner:
    offline_only = True

    def __init__(self, policy: OrganizationRepairPolicy) -> None:
        self.policy = policy
        self.plan_calls: list[str] = []
        self.reflection_calls: list[str] = []
        self.plans: dict[str, PlanReceipt] = {}
        self.reflections: dict[str, ReflectionReceipt] = {}
        self.decisions: list[ReflectionDecision] = [ReflectionDecision.SUCCESS]
        self.crash_plan_after_store = False
        self.crash_reflection_after_store = False

    def create_plan(
        self,
        operation_id,
        request,
        *,
        revision,
        previous_test_sha256,
    ):
        del request, previous_test_sha256
        self.plan_calls.append(operation_id)
        patch = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-value = 'old'\n"
            f"+value = '{PATCH_SENTINEL}-{revision}'\n"
        )
        receipt = PlanReceipt(
            operation_id,
            RepairPlanArtifact(
                revision=revision,
                summary=f"repair revision {revision}",
                patch_text=patch,
                writable_paths=("app.py",),
                test_commands=self.policy.fixed_test_commands,
                commit_message=f"fix: synthetic repair {revision}",
                draft_pr_title="Draft synthetic repair",
                draft_pr_body="Offline Phase 10 Prep evidence only.",
                risks=("synthetic-only",),
            ),
            tokens=100,
        )
        self.plans[operation_id] = receipt
        if self.crash_plan_after_store:
            self.crash_plan_after_store = False
            raise ForcedCrash()
        return receipt

    def lookup_plan(self, operation_id):
        return self.plans.get(operation_id)

    def reflect(self, operation_id, plan, tests):
        del plan, tests
        self.reflection_calls.append(operation_id)
        decision = self.decisions.pop(0) if self.decisions else ReflectionDecision.SUCCESS
        receipt = ReflectionReceipt(operation_id, decision, tokens=20)
        self.reflections[operation_id] = receipt
        if self.crash_reflection_after_store:
            self.crash_reflection_after_store = False
            raise ForcedCrash()
        return receipt

    def lookup_reflection(self, operation_id):
        return self.reflections.get(operation_id)


class FakeExecutor:
    offline_only = True

    def __init__(self) -> None:
        self.binding: WorktreeBinding | None = None
        self.current_diff = ""
        self.execute_calls: list[str] = []
        self.commit_calls: list[str] = []
        self.rollback_calls: list[str] = []
        self.executions: dict[str, ExecutionReceipt] = {}
        self.commits: dict[str, CommitReceipt] = {}
        self.test_exit_codes: list[int] = [0]
        self.crash_execute_after_store = False
        self.crash_commit_after_store = False
        self.network_mode = "none"
        self.provisioned_branch_override: str | None = None
        self.rollback_ok = True

    def provision(
        self,
        *,
        job_id,
        task_branch,
        repository_id,
        base_sha,
        head_sha,
    ):
        self.binding = WorktreeBinding(
            worktree_id=f"worktree-{job_id}",
            task_branch=self.provisioned_branch_override or task_branch,
            repository_id=repository_id,
            base_sha=base_sha,
            head_sha=head_sha,
            original_checkout_unchanged=True,
        )
        return self.binding

    def inspect(self, binding):
        return RepositorySnapshot(
            worktree_id=binding.worktree_id,
            task_branch=binding.task_branch,
            repository_id=binding.repository_id,
            base_sha=binding.base_sha,
            head_sha=binding.head_sha,
            diff_sha256=_hash_text(self.current_diff),
            original_checkout_unchanged=True,
        )

    def execute(self, operation_id, binding, plan, policy):
        self.execute_calls.append(operation_id)
        self.current_diff = plan.patch_text
        exit_code = self.test_exit_codes.pop(0) if self.test_exit_codes else 0
        snapshot = self.inspect(binding)
        receipt = ExecutionReceipt(
            operation_id=operation_id,
            snapshot=snapshot,
            full_diff=self.current_diff,
            tests=(
                TestEvidence(
                    argv=policy.fixed_test_commands[0],
                    exit_code=exit_code,
                    duration_seconds=0.25,
                ),
            ),
            docker=True,
            network_mode=self.network_mode,
            non_root=True,
            timeout_seconds=policy.command_timeout_seconds,
            output_limit_bytes=policy.command_output_bytes,
            elapsed_seconds=1.0,
            tool_calls=2,
        )
        self.executions[operation_id] = receipt
        if self.crash_execute_after_store:
            self.crash_execute_after_store = False
            raise ForcedCrash()
        return receipt

    def lookup_execution(self, operation_id):
        return self.executions.get(operation_id)

    def commit(
        self,
        operation_id,
        binding,
        *,
        diff_sha256,
        commit_message,
    ):
        del binding
        self.commit_calls.append(operation_id)
        receipt = CommitReceipt(
            operation_id=operation_id,
            commit_sha=COMMIT_SHA,
            parent_sha=BASE_SHA,
            diff_sha256=diff_sha256,
            message_sha256=_hash_text(commit_message),
            original_checkout_unchanged=True,
        )
        self.commits[operation_id] = receipt
        if self.crash_commit_after_store:
            self.crash_commit_after_store = False
            raise ForcedCrash()
        return receipt

    def lookup_commit(self, operation_id):
        return self.commits.get(operation_id)

    def rollback(self, binding, operation_id):
        del binding
        self.rollback_calls.append(operation_id)
        if self.rollback_ok:
            self.current_diff = ""
        return self.rollback_ok


class RecordingMetrics:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, str]]] = []

    def increment(self, name, labels=None):
        self.records.append((name, dict(labels or {})))


def principal(role: Role, *, method: str = "fake") -> Principal:
    return Principal(
        principal_id=f"principal-{role.value}-{method}",
        user_id=f"user-{role.value}",
        organization_id="org-1",
        role=role,
        auth_method=method,
    )


class Phase10RepairServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = FakeClock()
        self.policy = OrganizationRepairPolicy(
            version="policy-v1",
            fixed_test_commands=(("python", "-m", "unittest", "tests.test_fix"),),
            writable_paths=("app.py",),
            draft_pr_base="master",
            lease_seconds=10,
        )
        self.request = StartRepairRequest(
            organization_id="org-1",
            repository_id="repo-1",
            finding_sha256=FINDING_SHA,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            policy=self.policy,
        )
        self.planner = FakePlanner(self.policy)
        self.executor = FakeExecutor()
        self.publisher = FakeDraftPrPublisher()
        self.metrics = RecordingMetrics()
        self.trace_path = self.root / "trace.jsonl"
        self.trace = Trace(self.trace_path, run_id="phase10-test")
        self.store = Phase10RepairStore(self.root / "state", clock=self.clock)
        self.service = Phase10RepairService(
            store=self.store,
            planner=self.planner,
            executor=self.executor,
            publisher=self.publisher,
            metrics=self.metrics,
            trace=self.trace,
            clock=self.clock,
        )
        self.maintainer = principal(Role.MAINTAINER)
        self.admin = principal(Role.ORG_ADMIN)

    def tearDown(self) -> None:
        self.trace.close(status="ok")
        self.temp.cleanup()

    def start_and_plan(self):
        started = self.service.start_repair(self.request, actor=self.maintainer)
        planned = self.service.run_worker_once(started["job_id"], worker_id="worker-1")
        self.assertEqual(planned["state"], RepairJobState.AWAITING_WRITE_APPROVAL.value)
        self.assertFalse(planned["lease_active"])
        return planned

    def approve_write(self, job_id):
        view = self.service.write_approval_view(job_id, actor=self.maintainer)
        return self.service.decide_write(
            job_id,
            actor=self.maintainer,
            checkpoint_sha256=view["checkpoint_sha256"],
            approved=True,
        )

    def execute_to_draft_approval(self, job_id):
        self.approve_write(job_id)
        result = self.service.run_worker_once(job_id, worker_id="worker-1")
        self.assertEqual(
            result["state"], RepairJobState.AWAITING_DRAFT_PR_APPROVAL.value
        )
        self.assertFalse(result["lease_active"])
        return result

    def approve_draft(self, job_id):
        view = self.service.draft_pr_approval_view(job_id, actor=self.maintainer)
        result = self.service.decide_draft_pr(
            job_id,
            actor=self.maintainer,
            checkpoint_sha256=view["checkpoint_sha256"],
            approved=True,
        )
        return view, result

    def test_fake_end_to_end_review_repair_to_synthetic_draft_pr(self):
        planned = self.start_and_plan()
        write_view = self.service.write_approval_view(
            planned["job_id"], actor=self.maintainer
        )
        self.assertIn(PATCH_SENTINEL, write_view["plan"]["patch_text"])
        self.execute_to_draft_approval(planned["job_id"])
        self.assertEqual(self.executor.commit_calls, [])
        self.assertEqual(self.publisher.calls, [])
        draft_view, queued = self.approve_draft(planned["job_id"])
        self.assertIn(PATCH_SENTINEL, draft_view["full_diff"])
        self.assertEqual(draft_view["target_base"], "master")
        self.assertTrue(draft_view["tests"])
        self.assertTrue(draft_view["budget"])
        self.assertEqual(queued["state"], RepairJobState.QUEUED_PUBLISH.value)
        completed = self.service.run_worker_once(
            planned["job_id"], worker_id="worker-2"
        )
        self.assertEqual(completed["state"], RepairJobState.DRAFT_PUBLISHED.value)
        self.assertEqual(len(self.executor.commit_calls), 1)
        self.assertEqual(len(self.publisher.calls), 1)
        self.assertTrue(completed["synthetic_only"])
        self.assertFalse(completed["real_writes_enabled"])
        self.assertFalse(completed["business_claim_allowed"])
        self.assertFalse(completed["quality_claim_allowed"])

    def test_only_maintainer_or_admin_with_human_control_auth_can_operate(self):
        for role in (Role.VIEWER, Role.REVIEWER):
            with self.subTest(role=role), self.assertRaises(RepairAuthorizationError):
                self.service.start_repair(self.request, actor=principal(role))
        for method in ("model", "finding", "github_webhook", "webhook"):
            with self.subTest(method=method), self.assertRaises(RepairAuthorizationError):
                self.service.start_repair(
                    self.request, actor=principal(Role.MAINTAINER, method=method)
                )
        planned = self.start_and_plan()
        view = self.service.write_approval_view(planned["job_id"], actor=self.admin)
        approved = self.service.decide_write(
            planned["job_id"],
            actor=self.admin,
            checkpoint_sha256=view["checkpoint_sha256"],
            approved=True,
        )
        self.assertEqual(approved["state"], RepairJobState.QUEUED_EXECUTION.value)
        self.assertTrue(
            any(
                name == "unauthorized_operations_total"
                and labels in ({"operation": "other"}, {"operation": "approval"})
                for name, labels in self.metrics.records
            )
        )

    def test_stale_and_double_write_approvals_have_one_winner(self):
        planned = self.start_and_plan()
        view = self.service.write_approval_view(planned["job_id"], actor=self.maintainer)
        job, _ = self.store.load(planned["job_id"])
        self.store.save(job)
        with self.assertRaises(RepairConflict):
            self.service.decide_write(
                planned["job_id"],
                actor=self.maintainer,
                checkpoint_sha256=view["checkpoint_sha256"],
                approved=True,
            )
        self.assertIn(
            ("approval_validation_failures_total", {"reason": "mismatch"}),
            self.metrics.records,
        )
        fresh = self.service.write_approval_view(planned["job_id"], actor=self.maintainer)
        outcomes = []

        def approve():
            try:
                value = self.service.decide_write(
                    planned["job_id"],
                    actor=self.maintainer,
                    checkpoint_sha256=fresh["checkpoint_sha256"],
                    approved=True,
                )
                outcomes.append(("ok", value["state"]))
            except Exception as exc:
                outcomes.append(("error", type(exc).__name__))

        threads = [threading.Thread(target=approve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(sum(item[0] == "ok" for item in outcomes), 1)
        self.assertEqual(
            self.store.load(planned["job_id"])[0].state,
            RepairJobState.QUEUED_EXECUTION,
        )

    def test_rejected_approvals_create_no_commit_or_publication(self):
        planned = self.start_and_plan()
        view = self.service.write_approval_view(planned["job_id"], actor=self.maintainer)
        declined = self.service.decide_write(
            planned["job_id"],
            actor=self.maintainer,
            checkpoint_sha256=view["checkpoint_sha256"],
            approved=False,
        )
        self.assertEqual(declined["state"], RepairJobState.DECLINED.value)
        self.assertEqual(self.executor.execute_calls, [])
        self.assertEqual(self.executor.commit_calls, [])
        self.assertEqual(self.publisher.calls, [])

        second = self.start_and_plan()
        self.execute_to_draft_approval(second["job_id"])
        draft = self.service.draft_pr_approval_view(
            second["job_id"], actor=self.maintainer
        )
        declined2 = self.service.decide_draft_pr(
            second["job_id"],
            actor=self.maintainer,
            checkpoint_sha256=draft["checkpoint_sha256"],
            approved=False,
        )
        self.assertEqual(declined2["state"], RepairJobState.DECLINED.value)
        self.assertEqual(self.executor.commit_calls, [])
        self.assertEqual(self.publisher.calls, [])

    def test_test_failure_rolls_back_and_requires_new_plan_and_approval(self):
        self.executor.test_exit_codes = [1, 0]
        self.planner.decisions = [ReflectionDecision.RETRY, ReflectionDecision.SUCCESS]
        planned = self.start_and_plan()
        self.approve_write(planned["job_id"])
        retry = self.service.run_worker_once(planned["job_id"], worker_id="worker-1")
        self.assertEqual(retry["state"], RepairJobState.QUEUED_PLAN.value)
        self.assertEqual(retry["attempt"], 2)
        self.assertEqual(len(self.executor.rollback_calls), 1)
        replanned = self.service.run_worker_once(
            planned["job_id"], worker_id="worker-2"
        )
        self.assertEqual(
            replanned["state"], RepairJobState.AWAITING_WRITE_APPROVAL.value
        )
        self.assertEqual(len(self.planner.plan_calls), 2)
        self.approve_write(planned["job_id"])
        passed = self.service.run_worker_once(planned["job_id"], worker_id="worker-2")
        self.assertEqual(
            passed["state"], RepairJobState.AWAITING_DRAFT_PR_APPROVAL.value
        )
        self.assertEqual(self.executor.commit_calls, [])

    def test_invalid_sandbox_receipt_fails_closed(self):
        self.executor.network_mode = "bridge"
        planned = self.start_and_plan()
        self.approve_write(planned["job_id"])
        failed = self.service.run_worker_once(planned["job_id"], worker_id="worker-1")
        self.assertEqual(failed["state"], RepairJobState.FAILED.value)
        self.assertEqual(failed["failure_code"], "sandbox_policy_receipt_invalid")
        self.assertEqual(self.executor.commit_calls, [])
        self.assertEqual(self.publisher.calls, [])

    def test_budget_exhaustion_rolls_back_without_commit_or_publication(self):
        policy = replace(
            self.policy,
            budget_limits=BudgetLimits(tool_calls=1),
        )
        request = replace(self.request, policy=policy)
        planner = FakePlanner(policy)
        executor = FakeExecutor()
        publisher = FakeDraftPrPublisher()
        service = Phase10RepairService(
            store=self.store,
            planner=planner,
            executor=executor,
            publisher=publisher,
            clock=self.clock,
        )
        started = service.start_repair(request, actor=self.maintainer)
        service.run_worker_once(started["job_id"], worker_id="w")
        write = service.write_approval_view(started["job_id"], actor=self.maintainer)
        service.decide_write(
            started["job_id"],
            actor=self.maintainer,
            checkpoint_sha256=write["checkpoint_sha256"],
            approved=True,
        )
        failed = service.run_worker_once(started["job_id"], worker_id="w")
        self.assertEqual(failed["state"], RepairJobState.FAILED.value)
        self.assertEqual(failed["failure_code"], "repair_budget_exhausted")
        self.assertTrue(executor.rollback_calls)
        self.assertEqual(executor.commit_calls, [])
        self.assertEqual(publisher.calls, [])

    def test_patch_change_after_write_approval_invalidates_before_mutation(self):
        planned = self.start_and_plan()
        self.approve_write(planned["job_id"])
        job, _ = self.store.load(planned["job_id"])
        job.plan["patch_text"] += "\n# changed after approval\n"
        self.store.save(job)
        result = self.service.run_worker_once(planned["job_id"], worker_id="worker-1")
        self.assertEqual(result["state"], RepairJobState.FAILED.value)
        self.assertEqual(self.executor.execute_calls, [])
        self.assertEqual(self.executor.commit_calls, [])

    def test_repository_or_evidence_change_invalidates_draft_approval(self):
        planned = self.start_and_plan()
        self.execute_to_draft_approval(planned["job_id"])
        self.approve_draft(planned["job_id"])
        self.executor.current_diff += "\nexternal drift\n"
        result = self.service.run_worker_once(planned["job_id"], worker_id="worker-2")
        self.assertEqual(result["state"], RepairJobState.QUARANTINED.value)
        self.assertEqual(self.executor.commit_calls, [])
        self.assertEqual(self.publisher.calls, [])

    def test_changed_test_or_budget_hash_invalidates_draft_approval(self):
        for field_name, mutate in (
            ("tests", lambda job: setattr(job, "tests_sha256", "e" * 64)),
            (
                "budget",
                lambda job: job.budget["usage"].__setitem__("tool_calls", 99),
            ),
        ):
            with self.subTest(field=field_name):
                executor = FakeExecutor()
                planner = FakePlanner(self.policy)
                publisher = FakeDraftPrPublisher()
                service = Phase10RepairService(
                    store=self.store,
                    planner=planner,
                    executor=executor,
                    publisher=publisher,
                    clock=self.clock,
                )
                started = service.start_repair(self.request, actor=self.maintainer)
                service.run_worker_once(started["job_id"], worker_id="w")
                write = service.write_approval_view(started["job_id"], actor=self.maintainer)
                service.decide_write(
                    started["job_id"],
                    actor=self.maintainer,
                    checkpoint_sha256=write["checkpoint_sha256"],
                    approved=True,
                )
                service.run_worker_once(started["job_id"], worker_id="w")
                draft = service.draft_pr_approval_view(
                    started["job_id"], actor=self.maintainer
                )
                service.decide_draft_pr(
                    started["job_id"],
                    actor=self.maintainer,
                    checkpoint_sha256=draft["checkpoint_sha256"],
                    approved=True,
                )
                job, _ = self.store.load(started["job_id"])
                mutate(job)
                self.store.save(job)
                result = service.run_worker_once(started["job_id"], worker_id="w")
                self.assertEqual(result["state"], RepairJobState.QUARANTINED.value)
                self.assertEqual(executor.commit_calls, [])
                self.assertEqual(publisher.calls, [])

    def test_plan_crash_recovers_from_receipt_without_duplicate_model_call(self):
        started = self.service.start_repair(self.request, actor=self.maintainer)
        self.planner.crash_plan_after_store = True
        with self.assertRaises(ForcedCrash):
            self.service.run_worker_once(started["job_id"], worker_id="worker-a")
        self.assertEqual(len(self.planner.plan_calls), 1)
        durable, _ = self.store.load(started["job_id"])
        self.assertEqual(durable.budget["usage"]["tokens"], 0)
        self.assertEqual(len(durable.budget["reservations"]), 1)
        self.clock.advance(11)
        recovered = self.service.run_worker_once(
            started["job_id"], worker_id="worker-b"
        )
        self.assertEqual(
            recovered["state"], RepairJobState.AWAITING_WRITE_APPROVAL.value
        )
        self.assertEqual(len(self.planner.plan_calls), 1)
        durable, _ = self.store.load(started["job_id"])
        self.assertEqual(durable.budget["usage"]["tokens"], 100)
        self.assertEqual(durable.budget["reservations"], [])

    def test_execution_crash_recovers_without_duplicate_mutation(self):
        planned = self.start_and_plan()
        self.approve_write(planned["job_id"])
        self.executor.crash_execute_after_store = True
        with self.assertRaises(ForcedCrash):
            self.service.run_worker_once(planned["job_id"], worker_id="worker-a")
        self.assertEqual(len(self.executor.execute_calls), 1)
        self.clock.advance(11)
        recovered = self.service.run_worker_once(
            planned["job_id"], worker_id="worker-b"
        )
        self.assertEqual(
            recovered["state"], RepairJobState.AWAITING_DRAFT_PR_APPROVAL.value
        )
        self.assertEqual(len(self.executor.execute_calls), 1)

    def test_unreceipted_interrupted_operation_is_quarantined_not_replayed(self):
        started = self.service.start_repair(self.request, actor=self.maintainer)
        self.service.run_worker_once(started["job_id"], worker_id="worker-a")
        job, _ = self.store.load(started["job_id"])
        job.state = RepairJobState.PLANNING
        job.lease_owner = "dead-worker"
        job.lease_token = "dead-token"
        job.lease_expires_at = self.clock() - 1
        job.in_progress = {
            "kind": "plan",
            "operation_id": "missing-plan-receipt",
            "reservation_id": next(iter(job.budget["reservations"]))["reservation_id"]
            if job.budget["reservations"]
            else "missing-reservation",
        }
        self.store.save(job)
        before = len(self.planner.plan_calls)
        result = self.service.run_worker_once(started["job_id"], worker_id="worker-b")
        self.assertEqual(result["state"], RepairJobState.QUARANTINED.value)
        self.assertEqual(len(self.planner.plan_calls), before)

    def test_publisher_timeout_after_persist_is_reconciled_once(self):
        publisher = FakeDraftPrPublisher(timeout_after_persist=True)
        self.service.publisher = publisher
        planned = self.start_and_plan()
        self.execute_to_draft_approval(planned["job_id"])
        self.approve_draft(planned["job_id"])
        result = self.service.run_worker_once(planned["job_id"], worker_id="worker-2")
        self.assertEqual(result["state"], RepairJobState.DRAFT_PUBLISHED.value)
        self.assertEqual(len(publisher.calls), 1)

    def test_dry_run_publisher_returns_only_synthetic_receipt(self):
        publisher = DryRunDraftPrPublisher()
        self.service.publisher = publisher
        planned = self.start_and_plan()
        self.execute_to_draft_approval(planned["job_id"])
        self.approve_draft(planned["job_id"])
        result = self.service.run_worker_once(planned["job_id"], worker_id="worker-2")
        self.assertEqual(result["state"], RepairJobState.DRAFT_PUBLISHED.value)
        durable, _ = self.store.load(planned["job_id"])
        self.assertTrue(durable.publication["receipt_id"].startswith("dry-run-draft:"))
        self.assertTrue(durable.publication["synthetic"])

    def test_commit_crash_recovers_without_duplicate_commit_or_publish(self):
        planned = self.start_and_plan()
        self.execute_to_draft_approval(planned["job_id"])
        self.approve_draft(planned["job_id"])
        self.executor.crash_commit_after_store = True
        with self.assertRaises(ForcedCrash):
            self.service.run_worker_once(planned["job_id"], worker_id="worker-a")
        self.assertEqual(len(self.executor.commit_calls), 1)
        self.assertEqual(self.publisher.calls, [])
        self.clock.advance(11)
        recovered = self.service.run_worker_once(
            planned["job_id"], worker_id="worker-b"
        )
        self.assertEqual(recovered["state"], RepairJobState.DRAFT_PUBLISHED.value)
        self.assertEqual(len(self.executor.commit_calls), 1)
        self.assertEqual(len(self.publisher.calls), 1)

    def test_disabled_or_failed_publisher_never_reports_success(self):
        for publisher in (FakeDraftPrPublisher(fail=True), GitHubDraftPrPublisher()):
            with self.subTest(publisher=type(publisher).__name__):
                executor = FakeExecutor()
                planner = FakePlanner(self.policy)
                service = Phase10RepairService(
                    store=self.store,
                    planner=planner,
                    executor=executor,
                    publisher=publisher,
                    clock=self.clock,
                )
                started = service.start_repair(self.request, actor=self.maintainer)
                service.run_worker_once(started["job_id"], worker_id="w")
                write = service.write_approval_view(started["job_id"], actor=self.maintainer)
                service.decide_write(
                    started["job_id"],
                    actor=self.maintainer,
                    checkpoint_sha256=write["checkpoint_sha256"],
                    approved=True,
                )
                service.run_worker_once(started["job_id"], worker_id="w")
                draft = service.draft_pr_approval_view(
                    started["job_id"], actor=self.maintainer
                )
                service.decide_draft_pr(
                    started["job_id"],
                    actor=self.maintainer,
                    checkpoint_sha256=draft["checkpoint_sha256"],
                    approved=True,
                )
                result = service.run_worker_once(started["job_id"], worker_id="w")
                self.assertEqual(result["state"], RepairJobState.QUARANTINED.value)

    def test_protected_or_mismatched_task_branch_is_rejected(self):
        self.executor.provisioned_branch_override = "master"
        with self.assertRaises(RepairServiceError):
            self.service.start_repair(self.request, actor=self.maintainer)

    def test_non_offline_planner_or_executor_is_rejected_and_no_merge_api_exists(self):
        class OnlinePlanner(FakePlanner):
            offline_only = False

        class OnlineExecutor(FakeExecutor):
            offline_only = False

        with self.assertRaises(ValueError):
            Phase10RepairService(
                store=self.store,
                planner=OnlinePlanner(self.policy),
                executor=self.executor,
            )
        with self.assertRaises(ValueError):
            Phase10RepairService(
                store=self.store,
                planner=self.planner,
                executor=OnlineExecutor(),
            )
        self.assertFalse(hasattr(self.service, "merge"))

    def test_policy_rejects_unsafe_target_and_caps_above_durable_budget(self):
        with self.assertRaises(ValueError):
            replace(self.policy, draft_pr_base="../master")
        with self.assertRaises(ValueError):
            replace(
                self.policy,
                command_timeout_seconds=301,
                budget_limits=BudgetLimits(command_seconds=300),
            )
        with self.assertRaises(ValueError):
            replace(
                self.policy,
                command_output_bytes=1025,
                budget_limits=BudgetLimits(command_output_bytes=1024),
            )

    def test_checkpoint_checksum_tampering_fails_closed(self):
        started = self.service.start_repair(self.request, actor=self.maintainer)
        path = self.store.snapshot_path(started["job_id"])
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["checkpoint"]["state"] = RepairJobState.DRAFT_PUBLISHED.value
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaises(RepairServiceError):
            self.store.load(started["job_id"])

    def test_trace_journal_and_receipt_exclude_raw_content_output_and_paths(self):
        planned = self.start_and_plan()
        self.execute_to_draft_approval(planned["job_id"])
        self.approve_draft(planned["job_id"])
        self.service.run_worker_once(planned["job_id"], worker_id="worker-2")
        self.trace.close(status="ok")
        journal = (
            self.store.state_root
            / planned["job_id"]
            / "phase10-events.jsonl"
        ).read_text(encoding="utf-8")
        trace = self.trace_path.read_text(encoding="utf-8")
        receipt = self.store.load(planned["job_id"])[0].publication
        combined = journal + trace + json.dumps(receipt)
        self.assertNotIn(PATCH_SENTINEL, combined)
        self.assertNotIn(OUTPUT_SENTINEL, combined)
        self.assertNotIn(str(self.root), combined)
        self.assertNotIn("principal-maintainer", combined)


if __name__ == "__main__":
    unittest.main()
