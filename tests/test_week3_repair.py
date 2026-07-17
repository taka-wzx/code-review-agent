"""End-to-end fake-model tests for the durable repair orchestrator."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import code_review_agent.repair as repair_module

from code_review_agent.repair import (
    CommitInspection,
    CommitApprovalRequest,
    CommitOutcome,
    MeteredModelProtocolError,
    ModelCallLimits,
    ModelCallResult,
    OpenAIRepairModel,
    Reflection,
    ReflectionDecision,
    RepairOrchestrator,
    RepairPlan,
    RepairRunResult,
    RepairWorktree,
    RepositorySnapshot,
    SandboxedGitCommitControl,
    TTYApprovalProvider,
    WriteApprovalRequest,
    WorktreePolicyError,
)
from code_review_agent.repair_approval import (
    ApprovalError,
    issue_commit_approval,
    issue_write_approval,
)
from code_review_agent.repair_budget import (
    BudgetAccountingError,
    BudgetLimits,
    BudgetManager,
    CohortCostLedger,
)
from code_review_agent.repair_checkpoint import CheckpointStore, RepairCheckpoint
from code_review_agent.repair_state import RepairState
from code_review_agent.repair_tools import (
    APPLY_CHECK_COMMAND,
    APPLY_COMMAND,
    GIT_PREFIX,
    IGNORED_HASH_COMMAND,
    REVERSE_CHECK_COMMAND,
    REVERSE_COMMAND,
    STATUS_COMMAND,
    GitStatusResult,
    ManifestState,
    PatchManifest,
    RepairRepositorySnapshot,
    SnapshotMismatch,
    ToolQuarantined,
    parse_patch,
)
from code_review_agent.sandbox import SandboxResult


BASE_SHA = "a" * 40
COMMIT_SHA = "b" * 40
BASE_TREE_OID = "c" * 40
APPROVED_TREE_OID = "d" * 40


def runtime_client():
    return SimpleNamespace(
        with_options=lambda **_options: SimpleNamespace(max_retries=0)
    )


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True
TASK_BRANCH = "repair/issue-1-run-1"
TEST_COMMAND = ("python", "-m", "unittest")


def patch_for(attempt):
    return (
        "diff --git a/app.py b/app.py\n"
        f"--- a/app.py\n+++ b/app.py\n@@ -{attempt} +{attempt} @@\n"
        f"-old-{attempt}\n+new-{attempt}\n"
    )


class StatefulSandbox:
    def __init__(self, test_exit_codes=()):
        self.patches = []
        self.test_exit_codes = list(test_exit_codes)
        self.committed = False
        self.calls = []
        self._operation = 0

    def run(self, argv, *, timeout_seconds=None, stdin_bytes=None):
        command = tuple(argv)
        self.calls.append((command, stdin_bytes))
        self._operation += 1
        stdout, stderr, exit_code = "", "", 0
        if command == STATUS_COMMAND:
            if self.patches and not self.committed:
                stdout = " M app.py\x00"
        elif command == APPLY_CHECK_COMMAND:
            if stdin_bytes is None:
                exit_code, stderr = 1, "missing patch"
        elif command == APPLY_COMMAND:
            self.patches.append(stdin_bytes.decode("utf-8"))
        elif command == REVERSE_CHECK_COMMAND:
            text = "" if stdin_bytes is None else stdin_bytes.decode("utf-8")
            if not self.patches or self.patches[-1] != text:
                exit_code, stderr = 1, "reverse patch does not apply"
        elif command == REVERSE_COMMAND:
            text = "" if stdin_bytes is None else stdin_bytes.decode("utf-8")
            if not self.patches or self.patches[-1] != text:
                exit_code, stderr = 1, "reverse patch does not apply"
            else:
                self.patches.pop()
        elif command == TEST_COMMAND:
            exit_code = self.test_exit_codes.pop(0) if self.test_exit_codes else 0
            stdout = "ok" if exit_code == 0 else "failed"
        elif command[: len(GIT_PREFIX) + 1] == GIT_PREFIX + ("show",):
            stdout = "old-1\n"
        elif command[: len(GIT_PREFIX)] == GIT_PREFIX and "diff" in command:
            stdout = "".join(self.patches)
        else:
            raise AssertionError(f"unexpected sandbox command: {command}")
        return SandboxResult(
            operation_id=f"op-{self._operation}",
            argv=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0.01,
            output_truncated=False,
        )


class InterruptAfterPatchSandbox(StatefulSandbox):
    """Simulate a process interruption after git apply changed the worktree."""

    def __init__(self):
        super().__init__()
        self.interrupt_apply = True

    def run(self, argv, *, timeout_seconds=None, stdin_bytes=None):
        command = tuple(argv)
        if command == APPLY_COMMAND and self.interrupt_apply:
            self.calls.append((command, stdin_bytes))
            self._operation += 1
            self.patches.append(stdin_bytes.decode("utf-8"))
            self.interrupt_apply = False
            raise KeyboardInterrupt
        return super().run(
            argv,
            timeout_seconds=timeout_seconds,
            stdin_bytes=stdin_bytes,
        )


class RejectFirstPatchSandbox(StatefulSandbox):
    """Reject one preflight without mutating the worktree."""

    def __init__(self, reject_preflights=1):
        super().__init__()
        self.reject_preflights = reject_preflights

    def run(self, argv, *, timeout_seconds=None, stdin_bytes=None):
        command = tuple(argv)
        if command == APPLY_CHECK_COMMAND and self.reject_preflights:
            self.calls.append((command, stdin_bytes))
            self._operation += 1
            self.reject_preflights -= 1
            return SandboxResult(
                operation_id=f"op-{self._operation}",
                argv=command,
                exit_code=1,
                stdout="",
                stderr="patch does not apply: stale model context",
                duration_seconds=0.01,
                output_truncated=False,
            )
        return super().run(
            argv,
            timeout_seconds=timeout_seconds,
            stdin_bytes=stdin_bytes,
        )


class CacheMutatingTestSandbox(StatefulSandbox):
    """Model a test runner that leaves an ignored cache directory behind."""

    def __init__(self):
        super().__init__()
        self.cache_created = False

    def run(self, argv, *, timeout_seconds=None, stdin_bytes=None):
        command = tuple(argv)
        if command == IGNORED_HASH_COMMAND:
            self.calls.append((command, stdin_bytes))
            self._operation += 1
            return SandboxResult(
                operation_id=f"op-{self._operation}",
                argv=command,
                exit_code=0,
                stdout="f" * 40 + "\n",
                stderr="",
                duration_seconds=0.01,
                output_truncated=False,
            )
        result = super().run(
            argv,
            timeout_seconds=timeout_seconds,
            stdin_bytes=stdin_bytes,
        )
        if command == TEST_COMMAND:
            self.cache_created = True
        if command == STATUS_COMMAND and self.cache_created:
            result = SandboxResult(
                operation_id=result.operation_id,
                argv=result.argv,
                exit_code=result.exit_code,
                stdout=result.stdout + "!! .pytest_cache/\x00",
                stderr=result.stderr,
                duration_seconds=result.duration_seconds,
                output_truncated=result.output_truncated,
            )
        return result


class PreflightMutatingSandbox(StatefulSandbox):
    """Model an abnormal read-only preflight that leaves ignored state behind."""

    def __init__(self):
        super().__init__()
        self.cache_created = False

    def run(self, argv, *, timeout_seconds=None, stdin_bytes=None):
        command = tuple(argv)
        if command == IGNORED_HASH_COMMAND:
            self.calls.append((command, stdin_bytes))
            self._operation += 1
            return SandboxResult(
                operation_id=f"op-{self._operation}",
                argv=command,
                exit_code=0,
                stdout="e" * 40 + "\n",
                stderr="",
                duration_seconds=0.01,
                output_truncated=False,
            )
        result = super().run(
            argv,
            timeout_seconds=timeout_seconds,
            stdin_bytes=stdin_bytes,
        )
        if command == APPLY_CHECK_COMMAND:
            self.cache_created = True
        if command == STATUS_COMMAND and self.cache_created:
            return SandboxResult(
                operation_id=result.operation_id,
                argv=result.argv,
                exit_code=result.exit_code,
                stdout=result.stdout + "!! .pytest_cache/\x00",
                stderr=result.stderr,
                duration_seconds=result.duration_seconds,
                output_truncated=result.output_truncated,
            )
        return result


class InterruptFirstPreflightSandbox(StatefulSandbox):
    """Interrupt one read-only preflight without changing repository state."""

    def __init__(self):
        super().__init__()
        self.interrupt_preflight = True

    def run(self, argv, *, timeout_seconds=None, stdin_bytes=None):
        command = tuple(argv)
        if command == APPLY_CHECK_COMMAND and self.interrupt_preflight:
            self.calls.append((command, stdin_bytes))
            self._operation += 1
            self.interrupt_preflight = False
            raise KeyboardInterrupt("patch preflight interrupted")
        return super().run(
            argv,
            timeout_seconds=timeout_seconds,
            stdin_bytes=stdin_bytes,
        )


class InterruptingMutatingPreflightSandbox(PreflightMutatingSandbox):
    """Interrupt a nominally read-only preflight after an ignored side effect."""

    def run(self, argv, *, timeout_seconds=None, stdin_bytes=None):
        command = tuple(argv)
        if command == APPLY_CHECK_COMMAND and not self.cache_created:
            self.calls.append((command, stdin_bytes))
            self._operation += 1
            self.cache_created = True
            raise KeyboardInterrupt("preflight interrupted after side effect")
        return super().run(
            argv,
            timeout_seconds=timeout_seconds,
            stdin_bytes=stdin_bytes,
        )


class InterruptFirstRollbackSandbox(StatefulSandbox):
    """Interrupt after one reverse mutation to exercise manifest recovery."""

    def __init__(self, patches):
        super().__init__()
        self.patches = list(patches)
        self.interrupt_rollback = True

    def run(self, argv, *, timeout_seconds=None, stdin_bytes=None):
        command = tuple(argv)
        if command == REVERSE_COMMAND and self.interrupt_rollback:
            self.calls.append((command, stdin_bytes))
            self._operation += 1
            text = "" if stdin_bytes is None else stdin_bytes.decode("utf-8")
            if not self.patches or self.patches[-1] != text:
                raise AssertionError("rollback patch order is invalid")
            self.patches.pop()
            self.interrupt_rollback = False
            raise KeyboardInterrupt("rollback interrupted after mutation")
        return super().run(
            argv,
            timeout_seconds=timeout_seconds,
            stdin_bytes=stdin_bytes,
        )


def commit_result(command, *, exit_code=0, stdout="", stderr=""):
    return SandboxResult(
        operation_id=f"commit-{len(command)}",
        argv=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
        output_truncated=False,
    )


class CommitSandbox:
    def __init__(self, *, fail_commit=False, base_parents=("c" * 40,)):
        self.fail_commit = fail_commit
        self.base_parents = base_parents
        self.head = BASE_SHA
        self.staged = False
        self.message = "base commit"
        self.tree_oid = BASE_TREE_OID
        self.calls = []

    def run(self, argv, *, timeout_seconds=None, stdin_bytes=None):
        command = tuple(argv)
        self.calls.append((command, stdin_bytes))
        if command == GIT_PREFIX + ("branch", "--show-current"):
            return commit_result(command, stdout=TASK_BRANCH)
        if command == GIT_PREFIX + ("rev-parse", "HEAD"):
            return commit_result(command, stdout=self.head)
        if command == STATUS_COMMAND:
            stdout = "" if self.head == COMMIT_SHA else " M app.py\x00"
            return commit_result(command, stdout=stdout)
        if command == GIT_PREFIX + ("show", "-s", "--format=%P", "HEAD"):
            parent = BASE_SHA if self.head == COMMIT_SHA else " ".join(self.base_parents)
            return commit_result(command, stdout=parent)
        if command == GIT_PREFIX + ("show", "-s", "--format=%B", "HEAD"):
            return commit_result(command, stdout=self.message + "\n")
        if command == GIT_PREFIX + ("show", "-s", "--format=%T", "HEAD"):
            return commit_result(command, stdout=self.tree_oid + "\n")
        if command == GIT_PREFIX + ("write-tree",):
            tree_oid = APPROVED_TREE_OID if self.staged else self.tree_oid
            return commit_result(command, stdout=tree_oid + "\n")
        if command == GIT_PREFIX + ("apply", "--cached", "--whitespace=error-all", "-"):
            self.staged = True
            return commit_result(command)
        if command == GIT_PREFIX + (
            "apply",
            "--cached",
            "--reverse",
            "--whitespace=error-all",
            "-",
        ):
            self.staged = False
            return commit_result(command)
        if "commit" in command:
            self.assert_commit_identity(command)
            if self.fail_commit:
                return commit_result(command, exit_code=1, stderr="commit failed")
            self.head = COMMIT_SHA
            self.message = command[-1]
            self.tree_oid = APPROVED_TREE_OID
            self.staged = False
            return commit_result(command)
        raise AssertionError(f"unexpected commit command: {command}")

    @staticmethod
    def assert_commit_identity(command):
        name = command.index("user.name=code-review-agent")
        email = command.index("user.email=code-review-agent@localhost")
        commit = command.index("commit")
        if not (
            command[name - 1] == "-c"
            and command[email - 1] == "-c"
            and name < email < commit
        ):
            raise AssertionError("commit command lacks its fixed sandbox identity")


class FakeModel:
    def __init__(self):
        self.plan_calls = []
        self.patch_attempts = []
        self.reflections = []

    def limits_for(self, operation):
        if operation not in {"plan", "patch", "reflect"}:
            raise AssertionError(operation)
        return ModelCallLimits(max_tokens=100, max_cost_usd=0.01)

    def make_plan(self, issue_ref, *, previous_plan, evidence):
        self.plan_calls.append((issue_ref, previous_plan, evidence))
        revision = 1 if previous_plan is None else previous_plan.revision + 1
        return ModelCallResult(
            RepairPlan(
                summary=f"repair attempt {revision}",
                writable_paths=("app.py",),
                test_commands=(TEST_COMMAND,),
                risks=("small test-only repair",),
                rollback_boundary="app.py",
                commit_message="fix: resolve issue 1",
                revision=revision,
            ),
            actual_tokens=10,
            actual_cost_usd=0.001,
        )

    def make_patch(self, plan, *, patch_attempt, evidence):
        self.patch_attempts.append((plan.revision, patch_attempt, evidence))
        return ModelCallResult(
            patch_for(patch_attempt), actual_tokens=10, actual_cost_usd=0.001
        )

    def reflect(self, plan, *, patch_attempt, test_results):
        passed = bool(test_results) and all(item.exit_code == 0 for item in test_results)
        decision = ReflectionDecision.SUCCESS if passed else ReflectionDecision.RETRY
        reflection = Reflection(decision, "tests passed" if passed else "tests failed")
        self.reflections.append((plan.revision, patch_attempt, reflection))
        return ModelCallResult(reflection, actual_tokens=10, actual_cost_usd=0.001)


class FakeApprovals:
    def __init__(
        self,
        *,
        reject_write=False,
        reject_write_at=None,
        reject_commit=False,
        now=100.0,
    ):
        self.reject_write = reject_write
        self.reject_write_at = reject_write_at
        self.reject_commit = reject_commit
        self.now = now
        self.write_requests = []
        self.commit_requests = []

    def request_write(self, request):
        self.write_requests.append(request)
        if self.reject_write or len(self.write_requests) == self.reject_write_at:
            return None
        return issue_write_approval(
            run_id=request.run_id,
            checkpoint_id=request.checkpoint_id,
            base_sha=request.base_sha,
            diff_hash=request.diff_hash,
            plan_hash=request.plan.sha256,
            patch_hash=request.patch_hash,
            writable_paths=request.plan.writable_paths,
            patch_attempt=request.patch_attempt,
            ttl_seconds=600,
            now=self.now,
            nonce=f"write-{len(self.write_requests)}",
        )

    def request_commit(self, request):
        self.commit_requests.append(request)
        if self.reject_commit:
            return None
        return issue_commit_approval(
            run_id=request.run_id,
            checkpoint_id=request.checkpoint_id,
            base_sha=request.base_sha,
            diff_hash=request.diff_hash,
            test_result_hash=request.test_result_hash,
            commit_message=request.commit_message,
            expected_tree_oid=request.expected_tree_oid,
            ttl_seconds=600,
            now=self.now,
            nonce=f"commit-{len(self.commit_requests)}",
        )


class FakeCommitControl:
    def __init__(self, sandbox, *, fail=False, committed_tree=APPROVED_TREE_OID):
        self.sandbox = sandbox
        self.fail = fail
        self.calls = []
        self.head = BASE_SHA
        self.parent = ""
        self.message = ""
        self.tree_oid = BASE_TREE_OID
        self.committed_tree = committed_tree

    def inspect(self):
        return CommitInspection(
            branch=TASK_BRANCH,
            head=self.head,
            clean=self.sandbox.committed or not self.sandbox.patches,
            parent=self.parent,
            message=self.message,
            tree_oid=self.tree_oid,
        )

    def expected_tree(self, *, patch_text, writable_paths):
        if not patch_text or writable_paths != ("app.py",):
            raise AssertionError("tree preview did not receive the approved patch scope")
        return APPROVED_TREE_OID

    def commit(self, message, *, patch_text, writable_paths, expected_tree_oid):
        self.calls.append(message)
        if not patch_text or writable_paths != ("app.py",):
            raise AssertionError("commit did not receive the approved patch scope")
        if expected_tree_oid != APPROVED_TREE_OID:
            raise AssertionError("commit did not receive the approved tree")
        if self.fail:
            return CommitOutcome(False, error="simulated commit failure")
        self.parent = self.head
        self.head = COMMIT_SHA
        self.message = message
        self.tree_oid = self.committed_tree
        self.sandbox.committed = True
        return CommitOutcome(True, COMMIT_SHA)

    def restore_index(self, patch_text):
        return bool(patch_text)


def snapshot_hash(patches=(), *, committed=False):
    entries = [] if committed or not patches else [
        {"index": " ", "worktree": "M", "path": "app.py", "original_path": ""}
    ]
    payload = {
        "status": entries,
        "base_diff": "".join(patches),
        "untracked_diffs": [],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class OrchestratorCase(unittest.TestCase):
    def make_checkpoint(self, worktree, **overrides):
        values = {
            "run_id": "run-1",
            "repository_id": "repo-id",
            "base_sha": BASE_SHA,
            "task_branch": TASK_BRANCH,
            "worktree": str(worktree),
            "state": RepairState.DISCOVER,
            "state_history": (RepairState.DISCOVER,),
            "sequence": 0,
            "issue_ref": "https://github.com/example/repo/issues/1",
            "original_snapshot": {"branch": "master", "head": BASE_SHA},
            "writable_paths": ("app.py",),
            "budget": BudgetManager().to_dict(),
            "updated_at": 100.0,
        }
        values.update(overrides)
        return RepairCheckpoint(**values)

    def make_orchestrator(
        self,
        root,
        *,
        sandbox=None,
        model=None,
        approvals=None,
        commit_control=None,
        checkpoint=None,
        expected_limits=None,
        cohort_ledger=None,
        preflight=None,
    ):
        worktree = Path(root) / "task"
        worktree.mkdir(exist_ok=True)
        sandbox = sandbox or StatefulSandbox()
        model = model or FakeModel()
        approvals = approvals or FakeApprovals(now=100)
        commit_control = commit_control or FakeCommitControl(sandbox)
        checkpoint = checkpoint or self.make_checkpoint(worktree)
        store = CheckpointStore(Path(root) / "state", clock=lambda: 100.0)
        store.save(checkpoint)
        orchestrator = RepairOrchestrator(
            checkpoint=checkpoint,
            store=store,
            sandbox=sandbox,
            model=model,
            approvals=approvals,
            commit_control=commit_control,
            expected_limits=expected_limits,
            cohort_ledger=cohort_ledger,
            preflight=preflight,
            clock=lambda: 100.0,
        )
        return orchestrator, store, sandbox, model, approvals, commit_control


class TestRepairOrchestrator(OrchestratorCase):
    def test_preapproval_preflight_side_effect_is_quarantined_without_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = PreflightMutatingSandbox()
            orchestrator, store, _sandbox, _model, approvals, commit = (
                self.make_orchestrator(tmp, sandbox=sandbox)
            )

            result = orchestrator.run()
            durable = store.load("run-1")

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "tool_failed:ToolQuarantined")
        self.assertEqual(approvals.write_requests, [])
        self.assertEqual(commit.calls, [])
        self.assertEqual(sandbox.patches, [])
        self.assertEqual(
            durable.in_progress_operation["cleanup_status"], "quarantined"
        )
        self.assertIn("cleanup_snapshot_hash", durable.in_progress_operation)
        self.assertFalse(
            any(item.get("kind") == "patch_manifest" for item in durable.tool_ledger)
        )

    def test_interrupted_preflight_side_effect_is_durably_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = InterruptingMutatingPreflightSandbox()
            orchestrator, store, _sandbox, model, approvals, commit = (
                self.make_orchestrator(tmp, sandbox=sandbox)
            )

            result = orchestrator.run()
            durable = store.load("run-1")
            resumed = RepairOrchestrator(
                checkpoint=durable,
                store=store,
                sandbox=sandbox,
                model=model,
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            ).run()

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "tool_failed:ToolQuarantined")
        self.assertEqual(resumed.state, RepairState.FAILED)
        self.assertEqual(resumed.reason, result.reason)
        self.assertEqual(
            durable.in_progress_operation["cleanup_status"], "quarantined"
        )
        self.assertFalse(
            any(
                item.get("kind") == "llm_call"
                and item.get("status") == "completed"
                for item in durable.tool_ledger
            )
        )
        self.assertEqual(approvals.write_requests, [])
        self.assertEqual(commit.calls, [])

    def test_completed_patch_output_recovers_before_candidate_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = FakeModel()
            orchestrator, store, sandbox, _model, approvals, commit = (
                self.make_orchestrator(tmp, model=model)
            )
            with mock.patch.object(
                repair_module,
                "parse_patch",
                side_effect=KeyboardInterrupt("before candidate persistence"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    orchestrator.run()
            interrupted = store.load("run-1")
            completed = [
                item
                for item in interrupted.tool_ledger
                if item.get("kind") == "llm_call"
                and item.get("operation") == "patch"
                and item.get("status") == "completed"
            ]
            self.assertIsNone(interrupted.in_progress_operation)
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["result"]["text"], patch_for(1))

            resumed = RepairOrchestrator(
                checkpoint=interrupted,
                store=store,
                sandbox=sandbox,
                model=model,
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            ).run()

        self.assertEqual(resumed.state, RepairState.SUBMIT)
        self.assertEqual(len(model.patch_attempts), 1)
        self.assertEqual(len(approvals.write_requests), 1)
        self.assertEqual(len(sandbox.patches), 1)

    def test_oversized_patch_output_is_bounded_and_fails_without_approval(self):
        class OversizedPatchModel(FakeModel):
            def make_patch(self, plan, *, patch_attempt, evidence):
                self.patch_attempts.append((plan.revision, patch_attempt, evidence))
                return ModelCallResult(
                    "x" * (1024 * 1024 + 1),
                    actual_tokens=10,
                    actual_cost_usd=0.001,
                )

        with tempfile.TemporaryDirectory() as tmp:
            model = OversizedPatchModel()
            orchestrator, store, sandbox, _model, approvals, commit = (
                self.make_orchestrator(tmp, model=model)
            )

            result = orchestrator.run()
            durable = store.load("run-1")

        patch_call = next(
            item
            for item in durable.tool_ledger
            if item.get("kind") == "llm_call"
            and item.get("operation") == "patch"
        )
        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "tool_failed:PatchRejected")
        self.assertEqual(patch_call["status"], "consumed")
        self.assertEqual(patch_call["result"]["kind"], "patch_output_rejected")
        self.assertNotIn("text", patch_call["result"])
        self.assertEqual(approvals.write_requests, [])
        self.assertEqual(sandbox.patches, [])
        self.assertEqual(commit.calls, [])

    def test_interrupted_first_preflight_resumes_durable_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = InterruptFirstPreflightSandbox()
            model = FakeModel()
            orchestrator, store, sandbox, _model, approvals, commit = (
                self.make_orchestrator(tmp, sandbox=sandbox, model=model)
            )
            with self.assertRaises(KeyboardInterrupt):
                orchestrator.run()
            interrupted = store.load("run-1")
            candidate = interrupted.in_progress_operation

            resumed = RepairOrchestrator(
                checkpoint=interrupted,
                store=store,
                sandbox=sandbox,
                model=model,
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            ).run()

        self.assertEqual(candidate["kind"], "patch_candidate")
        self.assertEqual(candidate["status"], "pending_preflight")
        self.assertEqual(candidate["patch"]["text"], patch_for(1))
        self.assertEqual(resumed.state, RepairState.SUBMIT)
        self.assertEqual(len(model.patch_attempts), 1)
        self.assertEqual(len(approvals.write_requests), 1)
        self.assertEqual(len(sandbox.patches), 1)

    def test_waiting_write_approval_resumes_exact_candidate_without_model_replay(self):
        class InterruptOnceApprovals(FakeApprovals):
            def __init__(self):
                super().__init__(now=100)
                self.interrupt = True

            def request_write(self, request):
                if self.interrupt:
                    self.write_requests.append(request)
                    self.interrupt = False
                    raise KeyboardInterrupt("approval input interrupted")
                return super().request_write(request)

        with tempfile.TemporaryDirectory() as tmp:
            approvals = InterruptOnceApprovals()
            model = FakeModel()
            orchestrator, store, sandbox, _model, _approvals, commit = (
                self.make_orchestrator(
                    tmp,
                    model=model,
                    approvals=approvals,
                )
            )
            with self.assertRaises(KeyboardInterrupt):
                orchestrator.run()
            interrupted = store.load("run-1")
            candidate = interrupted.in_progress_operation
            resumed = RepairOrchestrator(
                checkpoint=interrupted,
                store=store,
                sandbox=sandbox,
                model=model,
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            ).run()

        self.assertEqual(candidate["kind"], "patch_candidate")
        self.assertEqual(candidate["patch"]["text"], patch_for(1))
        self.assertEqual(candidate["patch_attempt"], 1)
        self.assertEqual(resumed.state, RepairState.SUBMIT)
        self.assertEqual(len(model.plan_calls), 1)
        self.assertEqual(len(model.patch_attempts), 1)
        self.assertEqual(len(approvals.write_requests), 2)
        self.assertNotEqual(
            approvals.write_requests[0].checkpoint_id,
            approvals.write_requests[1].checkpoint_id,
        )
        self.assertEqual(len(sandbox.patches), 1)

    def test_consumed_write_before_manifest_resumes_with_new_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            approvals = FakeApprovals(now=100)
            model = FakeModel()
            orchestrator, store, sandbox, _model, _approvals, commit = (
                self.make_orchestrator(
                    tmp,
                    model=model,
                    approvals=approvals,
                )
            )
            original_persist_manifest = orchestrator._persist_manifest
            interrupted_once = True

            def interrupt_manifest(manifest):
                nonlocal interrupted_once
                if interrupted_once:
                    interrupted_once = False
                    raise KeyboardInterrupt("before manifest intent persistence")
                return original_persist_manifest(manifest)

            orchestrator._persist_manifest = interrupt_manifest
            with self.assertRaises(KeyboardInterrupt):
                orchestrator.run()
            interrupted = store.load("run-1")
            self.assertEqual(interrupted.state, RepairState.PATCH)
            self.assertEqual(
                interrupted.in_progress_operation["kind"], "patch_candidate"
            )
            write_approvals = [
                item
                for item in interrupted.approvals
                if item["binding"]["kind"] == "write"
            ]
            self.assertEqual(len(write_approvals), 1)
            self.assertIsNotNone(write_approvals[0]["consumed_at"])

            resumed = RepairOrchestrator(
                checkpoint=interrupted,
                store=store,
                sandbox=sandbox,
                model=model,
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            ).run()
            durable = store.load("run-1")

        self.assertEqual(resumed.state, RepairState.SUBMIT)
        self.assertEqual(len(model.patch_attempts), 1)
        self.assertEqual(len(approvals.write_requests), 2)
        write_approvals = [
            item for item in durable.approvals if item["binding"]["kind"] == "write"
        ]
        self.assertEqual(len(write_approvals), 2)
        self.assertTrue(all(item["consumed_at"] is not None for item in write_approvals))
        self.assertNotEqual(
            write_approvals[0]["binding"]["nonce"],
            write_approvals[1]["binding"]["nonce"],
        )

    def test_resume_rejects_reused_write_approval_nonce(self):
        class FixedNonceApprovals(FakeApprovals):
            def request_write(self, request):
                self.write_requests.append(request)
                return issue_write_approval(
                    run_id=request.run_id,
                    checkpoint_id=request.checkpoint_id,
                    base_sha=request.base_sha,
                    diff_hash=request.diff_hash,
                    plan_hash=request.plan.sha256,
                    patch_hash=request.patch_hash,
                    writable_paths=request.plan.writable_paths,
                    patch_attempt=request.patch_attempt,
                    ttl_seconds=600,
                    now=self.now,
                    nonce="fixed-write-nonce",
                )

        with tempfile.TemporaryDirectory() as tmp:
            approvals = FixedNonceApprovals(now=100)
            model = FakeModel()
            orchestrator, store, sandbox, _model, _approvals, commit = (
                self.make_orchestrator(tmp, model=model, approvals=approvals)
            )
            original_persist_manifest = orchestrator._persist_manifest
            interrupted_once = True

            def interrupt_manifest(manifest):
                nonlocal interrupted_once
                if interrupted_once:
                    interrupted_once = False
                    raise KeyboardInterrupt("before manifest intent persistence")
                return original_persist_manifest(manifest)

            orchestrator._persist_manifest = interrupt_manifest
            with self.assertRaises(KeyboardInterrupt):
                orchestrator.run()
            interrupted = store.load("run-1")

            result = RepairOrchestrator(
                checkpoint=interrupted,
                store=store,
                sandbox=sandbox,
                model=model,
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            ).run()
            durable = store.load("run-1")

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "tool_failed:RepairToolError")
        self.assertEqual(len(model.patch_attempts), 1)
        self.assertEqual(len(approvals.write_requests), 2)
        self.assertEqual(sandbox.patches, [])
        self.assertFalse(
            any(
                item.get("kind") == "llm_call"
                and item.get("status") == "completed"
                for item in durable.tool_ledger
            )
        )

    def test_test_side_effect_is_durably_quarantined_instead_of_escaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = CacheMutatingTestSandbox()
            orchestrator, store, _sandbox, _model, _approvals, _commit = (
                self.make_orchestrator(tmp, sandbox=sandbox)
            )

            result = orchestrator.run()
            durable = store.load("run-1")

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "tool_failed:ToolQuarantined")
        self.assertEqual(durable.state, RepairState.FAILED)
        self.assertEqual(durable.in_progress_operation["kind"], "failure")
        self.assertEqual(
            durable.in_progress_operation["cleanup_status"], "quarantined"
        )
        self.assertIn("mutated the repair worktree", durable.in_progress_operation["tool_error"])
        self.assertIn("patch manifest", durable.in_progress_operation["cleanup_error"])
        self.assertEqual(len(sandbox.patches), 1)
        self.assertNotIn(REVERSE_COMMAND, [call[0] for call in sandbox.calls])
        failures = [
            item for item in durable.tool_ledger if item.get("kind") == "tool_failure"
        ]
        self.assertEqual([item["phase"] for item in failures], ["run", "cleanup"])

    def test_tool_failure_evidence_survives_successful_cleanup_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, store, sandbox, _model, approvals, commit = (
                self.make_orchestrator(tmp)
            )

            first = orchestrator._enter_tool_failure(
                ToolQuarantined("sandbox produced an unverifiable result")
            )
            durable = store.load("run-1")
            resumed = RepairOrchestrator(
                checkpoint=durable,
                store=store,
                sandbox=sandbox,
                model=FakeModel(),
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            ).run()

        self.assertEqual(first.state, RepairState.FAILED)
        self.assertIsNone(durable.in_progress_operation)
        failures = [
            item for item in durable.tool_ledger if item.get("kind") == "tool_failure"
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["error_type"], "ToolQuarantined")
        self.assertIn("unverifiable", failures[0]["detail"])
        self.assertEqual(resumed.state, RepairState.FAILED)
        self.assertEqual(resumed.reason, "terminal")

    def test_tool_error_during_existing_failure_keeps_original_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "task"
            worktree.mkdir()
            checkpoint = self.make_checkpoint(
                worktree,
                state=RepairState.FAILED,
                state_history=(RepairState.DISCOVER, RepairState.FAILED),
                in_progress_operation={
                    "kind": "failure",
                    "reason": "original_failure",
                    "cleanup_status": "pending",
                },
            )
            orchestrator, store, _sandbox, _model, _approvals, _commit = (
                self.make_orchestrator(tmp, checkpoint=checkpoint)
            )
            with mock.patch.object(
                orchestrator,
                "_complete_failure_cleanup",
                side_effect=SnapshotMismatch("cleanup still mismatched"),
            ):
                result = orchestrator._enter_tool_failure(
                    ToolQuarantined("first cleanup tool error")
                )
            durable = store.load("run-1")

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "original_failure")
        self.assertEqual(durable.in_progress_operation["reason"], "original_failure")
        self.assertEqual(
            durable.in_progress_operation["cleanup_status"], "quarantined"
        )
        self.assertIn("still mismatched", durable.in_progress_operation["cleanup_error"])

    def test_uncertain_llm_call_is_never_replayed_after_resume(self):
        class InterruptingModel(FakeModel):
            def make_plan(self, issue_ref, *, previous_plan, evidence):
                self.plan_calls.append((issue_ref, previous_plan, evidence))
                raise KeyboardInterrupt("provider outcome is unknown")

        with tempfile.TemporaryDirectory() as tmp:
            model = InterruptingModel()
            orchestrator, store, sandbox, _model, approvals, commit = (
                self.make_orchestrator(tmp, model=model)
            )
            with self.assertRaises(KeyboardInterrupt):
                orchestrator.run()
            interrupted = store.load("run-1")
            resumed = RepairOrchestrator(
                checkpoint=interrupted,
                store=store,
                sandbox=sandbox,
                model=model,
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            )
            with self.assertRaisesRegex(WorktreePolicyError, "automatic replay"):
                resumed.run()

        self.assertEqual(len(model.plan_calls), 1)
        calls = [
            item for item in interrupted.tool_ledger if item.get("kind") == "llm_call"
        ]
        self.assertEqual([item["status"] for item in calls], ["uncertain"])

    def test_metered_protocol_error_reconciles_usage_and_can_resume(self):
        class ProtocolErrorModel(FakeModel):
            def make_plan(self, issue_ref, *, previous_plan, evidence):
                self.plan_calls.append((issue_ref, previous_plan, evidence))
                raw = ModelCallResult(
                    None,
                    actual_tokens=10,
                    actual_cost_usd=0.001,
                )
                raise MeteredModelProtocolError("invalid provider JSON", raw)

        with tempfile.TemporaryDirectory() as tmp:
            model = ProtocolErrorModel()
            orchestrator, store, sandbox, _model, approvals, commit = (
                self.make_orchestrator(tmp, model=model)
            )
            with self.assertRaisesRegex(
                MeteredModelProtocolError, "invalid provider JSON"
            ):
                orchestrator.run()
            interrupted = store.load("run-1")
            durable_budget = BudgetManager.from_dict(interrupted.budget)
            calls = [
                item
                for item in interrupted.tool_ledger
                if item.get("kind") == "llm_call"
            ]
            resumed = RepairOrchestrator(
                checkpoint=interrupted,
                store=store,
                sandbox=sandbox,
                model=FakeModel(),
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            ).run()

        self.assertEqual([item["status"] for item in calls], ["protocol_error"])
        self.assertEqual(durable_budget.usage.tokens, 10)
        self.assertEqual(durable_budget.usage.cost_usd, 0.001)
        self.assertEqual(durable_budget.to_dict()["reservations"], [])
        self.assertEqual(resumed.state, RepairState.SUBMIT)

    def test_completed_but_unconsumed_llm_call_is_never_replayed(self):
        class OutOfScopeModel(FakeModel):
            def make_plan(self, issue_ref, *, previous_plan, evidence):
                self.plan_calls.append((issue_ref, previous_plan, evidence))
                return ModelCallResult(
                    RepairPlan(
                        summary="unsafe",
                        writable_paths=("other.py",),
                        test_commands=(TEST_COMMAND,),
                        risks=("out of scope",),
                        rollback_boundary="other.py",
                        commit_message="fix: unsafe",
                    ),
                    actual_tokens=10,
                    actual_cost_usd=0.001,
                )

        with tempfile.TemporaryDirectory() as tmp:
            model = OutOfScopeModel()
            orchestrator, store, sandbox, _model, approvals, commit = (
                self.make_orchestrator(tmp, model=model)
            )
            with self.assertRaisesRegex(WorktreePolicyError, "writable path"):
                orchestrator.run()
            interrupted = store.load("run-1")
            resumed = RepairOrchestrator(
                checkpoint=interrupted,
                store=store,
                sandbox=sandbox,
                model=model,
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            )
            with self.assertRaisesRegex(WorktreePolicyError, "automatic replay"):
                resumed.run()

        self.assertEqual(len(model.plan_calls), 1)
        calls = [
            item for item in interrupted.tool_ledger if item.get("kind") == "llm_call"
        ]
        self.assertEqual([item["status"] for item in calls], ["completed"])

    def test_paid_model_calls_share_the_durable_cohort_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CohortCostLedger(Path(tmp) / "cohort", "week3-test", 10.0)
            orchestrator, store, _sandbox, model, _approvals, _commit = (
                self.make_orchestrator(tmp, cohort_ledger=ledger)
            )

            result = orchestrator.run()
            cohort = ledger.snapshot()
            durable = BudgetManager.from_dict(store.load("run-1").budget)

        self.assertEqual(result.state, RepairState.SUBMIT)
        self.assertEqual(len(model.plan_calls), 1)
        self.assertEqual(len(model.patch_attempts), 1)
        self.assertEqual(len(model.reflections), 1)
        self.assertEqual(cohort.spent_microusd, 3000)
        self.assertEqual(len(cohort.finalized), 3)
        self.assertEqual(cohort.reservations, ())
        self.assertEqual(durable.usage.cost_usd, 0.003)

    def test_cohort_refusal_cancels_task_reservation_before_model_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CohortCostLedger(Path(tmp) / "cohort", "week3-test", 0.005)
            orchestrator, store, _sandbox, model, _approvals, _commit = (
                self.make_orchestrator(tmp, cohort_ledger=ledger)
            )

            result = orchestrator.run()
            durable = BudgetManager.from_dict(store.load("run-1").budget)
            cohort = ledger.snapshot()

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "budget_exceeded")
        self.assertEqual(model.plan_calls, [])
        self.assertEqual(durable.usage.tokens, 0)
        self.assertEqual(durable.usage.cost_usd, 0.0)
        self.assertEqual(durable.to_dict()["reservations"], [])
        self.assertEqual(cohort.spent_microusd, 0)
        self.assertEqual(cohort.reservations, ())

    def test_actual_over_reservation_is_saved_to_both_ledgers_before_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CohortCostLedger(Path(tmp) / "cohort", "week3-test", 10.0)
            model = FakeModel()
            model.limits_for = lambda _operation: ModelCallLimits(100, 0.0005)
            orchestrator, store, _sandbox, model, _approvals, _commit = (
                self.make_orchestrator(tmp, model=model, cohort_ledger=ledger)
            )

            with self.assertRaises(BudgetAccountingError):
                orchestrator.run()
            checkpoint = store.load("run-1")
            cohort = ledger.snapshot()

        self.assertEqual(len(model.plan_calls), 1)
        self.assertEqual(checkpoint.budget["usage"]["cost_usd"], 0.001)
        self.assertEqual(
            checkpoint.budget["accounting_failure"],
            "actual LLM usage exceeded its pre-call reservation",
        )
        self.assertEqual(cohort.spent_microusd, 1000)
        self.assertIsNotNone(cohort.accounting_failure)

    def test_interrupted_mutation_keeps_budget_and_is_not_replayed_after_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = InterruptAfterPatchSandbox()
            orchestrator, store, sandbox, _model, approvals, commit = self.make_orchestrator(
                tmp, sandbox=sandbox
            )

            with self.assertRaises(KeyboardInterrupt):
                orchestrator.run()

            interrupted = store.load("run-1")
            interrupted_budget = BudgetManager.from_dict(interrupted.budget)
            interrupted_commands = interrupted_budget.usage.commands
            interrupted_tool_calls = interrupted_budget.usage.tool_calls
            operation = interrupted.in_progress_operation
            self.assertGreater(interrupted_commands, 0)
            self.assertGreater(interrupted_tool_calls, 0)
            self.assertEqual(operation["kind"], "patch_manifest")
            self.assertEqual(operation["manifest"]["state"], ManifestState.INTENT.value)
            self.assertEqual(
                sum(1 for command, _stdin in sandbox.calls if command == APPLY_COMMAND),
                1,
            )

            resumed = RepairOrchestrator(
                checkpoint=interrupted,
                store=store,
                sandbox=sandbox,
                model=FakeModel(),
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            ).run()

            durable_budget = BudgetManager.from_dict(store.load("run-1").budget)

        self.assertEqual(resumed.state, RepairState.SUBMIT)
        self.assertEqual(resumed.reason, "completed")
        self.assertGreater(durable_budget.usage.commands, interrupted_commands)
        self.assertGreaterEqual(durable_budget.usage.tool_calls, interrupted_tool_calls)
        self.assertEqual(
            sum(1 for command, _stdin in sandbox.calls if command == APPLY_COMMAND),
            1,
        )

    def test_repair_cli_start_builds_a_durable_run_without_approval_bypass_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            original.mkdir()
            contract = {
                "run_id": "run-1",
                "issue_slug": "issue-1",
                "issue_ref": "https://github.com/example/repo/issues/1",
                "issue_context": "issue body and approved context",
                "repository_id": "example/repo",
                "base_sha": BASE_SHA,
                "original_checkout": str(original),
                "worktree_root": str(root / "worktrees"),
                "state_root": str(root / "state"),
                "docker_image": "repair:test",
                "llm_provider": "deepseek",
                "llm_model": "model",
                "llm_thinking": "disabled",
                "pricing_id": "test-pricing-v1",
                "cohort_id": "week3-test",
                "cohort_cost_limit_usd": 10.0,
                "writable_paths": ["app.py"],
                "test_commands": [list(TEST_COMMAND)],
                "max_total_tokens_per_call": 1000,
                "max_output_tokens_per_call": 200,
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 2.0,
                "task_total_seconds": 900.0,
                "task_total_tokens": 500000,
                "task_total_cost_usd": 1.0,
                "task_tool_calls": 80,
                "task_command_seconds": 240.0,
                "task_command_output_bytes": 524288,
                "task_repair_attempts": 2,
            }
            contract_path = root / "contract.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            task_path = root / "worktrees" / "issue-1-run-1"
            task_path.mkdir(parents=True)
            task = RepairWorktree(
                run_id="run-1",
                issue_slug="issue-1",
                branch=TASK_BRANCH,
                base_sha=BASE_SHA,
                original_checkout=original.resolve(),
                path=task_path.resolve(),
                original_snapshot=RepositorySnapshot("master", BASE_SHA),
            )
            result = RepairRunResult(RepairState.CANCELLED, "write_approval_rejected")
            with (
                mock.patch.object(repair_module, "DockerWorktreeBackend") as backend,
                mock.patch.object(repair_module, "RepairWorktreeManager") as manager,
                mock.patch.object(repair_module, "build_repair_sandbox", return_value=StatefulSandbox()),
                mock.patch.object(repair_module, "OpenAIRepairModel"),
                mock.patch.object(repair_module, "TTYApprovalProvider"),
                mock.patch.object(repair_module, "SandboxedGitCommitControl"),
                mock.patch.object(repair_module, "RepairOrchestrator") as orchestrator,
                mock.patch(
                    "code_review_agent.llm.make_client",
                    return_value=(runtime_client(), "model"),
                ),
                mock.patch.dict(repair_module.os.environ, {"LLM_PROVIDER": "deepseek"}),
            ):
                manager.return_value.create.return_value = task
                backend.return_value.snapshot.return_value = task.original_snapshot
                orchestrator.return_value.run.return_value = result
                observed = repair_module._run_repair_contract(contract_path, resume=False)
            checkpoint = CheckpointStore(root / "state").load("run-1")

        self.assertEqual(observed, result)
        self.assertEqual(checkpoint.task_branch, TASK_BRANCH)
        self.assertEqual(len(checkpoint.original_snapshot["contract_hash"]), 64)
        expected_limits = orchestrator.call_args.kwargs["expected_limits"]
        self.assertEqual(expected_limits.total_tokens, 500000)
        self.assertEqual(expected_limits.total_cost_usd, 1.0)
        self.assertEqual(expected_limits.tool_calls, 80)
        self.assertEqual(expected_limits.repair_attempts, 2)
        backend.return_value.snapshot.assert_called_once_with(original.resolve())
        with self.assertRaises(SystemExit):
            repair_module.repair_cli_main(["start", "--yes", str(contract_path)])

    def test_contract_rejects_state_root_overlap_before_store_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            worktree_root = root / "worktrees"
            original.mkdir()
            worktree_root.mkdir()
            base_contract = {
                "run_id": "run-1",
                "issue_slug": "issue-1",
                "issue_ref": "https://github.com/example/repo/issues/1",
                "issue_context": "issue body and approved context",
                "repository_id": "example/repo",
                "base_sha": BASE_SHA,
                "original_checkout": str(original),
                "worktree_root": str(worktree_root),
                "docker_image": "repair:test",
                "llm_provider": "deepseek",
                "llm_model": "model",
                "llm_thinking": "disabled",
                "pricing_id": "test-pricing-v1",
                "cohort_id": "week3-test",
                "cohort_cost_limit_usd": 10.0,
                "writable_paths": ["app.py"],
                "test_commands": [list(TEST_COMMAND)],
                "max_total_tokens_per_call": 1000,
                "max_output_tokens_per_call": 200,
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 2.0,
            }
            cases = (
                ("original", original / "state", "original checkout"),
                ("worktrees", worktree_root / "state", "repair worktree root"),
                ("ancestor", root, "original checkout"),
            )
            for resume in (False, True):
                for name, state_root, expected in cases:
                    with self.subTest(resume=resume, state_root=name):
                        contract = base_contract | {"state_root": str(state_root)}
                        contract_path = root / f"contract-{resume}-{name}.json"
                        contract_path.write_text(json.dumps(contract), encoding="utf-8")
                        with mock.patch.object(
                            repair_module, "CheckpointStore"
                        ) as checkpoint_store:
                            with self.assertRaisesRegex(WorktreePolicyError, expected):
                                repair_module._run_repair_contract(
                                    contract_path, resume=resume
                                )
                        checkpoint_store.assert_not_called()

    def test_contract_rejects_state_root_symlink_alias_before_store_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            worktree_root = root / "worktrees"
            alias = root / "original-alias"
            original.mkdir()
            worktree_root.mkdir()
            try:
                alias.symlink_to(original, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            contract = {
                "run_id": "run-1",
                "issue_slug": "issue-1",
                "issue_ref": "issue",
                "issue_context": "context",
                "repository_id": "example/repo",
                "base_sha": BASE_SHA,
                "original_checkout": str(original),
                "worktree_root": str(worktree_root),
                "state_root": str(alias / "state"),
                "docker_image": "repair:test",
                "llm_provider": "deepseek",
                "llm_model": "model",
                "llm_thinking": "disabled",
                "pricing_id": "test-pricing-v1",
                "cohort_id": "week3-test",
                "cohort_cost_limit_usd": 10.0,
                "writable_paths": ["app.py"],
                "test_commands": [list(TEST_COMMAND)],
                "max_total_tokens_per_call": 1000,
                "max_output_tokens_per_call": 200,
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 2.0,
            }
            contract_path = root / "contract-alias.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            with mock.patch.object(
                repair_module, "CheckpointStore"
            ) as checkpoint_store:
                with self.assertRaisesRegex(
                    WorktreePolicyError, "symlink or junction aliases"
                ):
                    repair_module._run_repair_contract(contract_path, resume=False)
            checkpoint_store.assert_not_called()

    def test_resume_rejects_state_root_containing_task_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            worktree_root = root / "worktrees"
            state_root = root / "state"
            task_worktree = state_root / "run-1" / "task"
            original.mkdir()
            worktree_root.mkdir()
            task_worktree.mkdir(parents=True)
            contract = {
                "run_id": "run-1",
                "issue_slug": "issue-1",
                "issue_ref": "issue",
                "issue_context": "context",
                "repository_id": "example/repo",
                "base_sha": BASE_SHA,
                "original_checkout": str(original),
                "worktree_root": str(worktree_root),
                "state_root": str(state_root),
                "docker_image": "repair:test",
                "llm_provider": "deepseek",
                "llm_model": "model",
                "llm_thinking": "disabled",
                "pricing_id": "test-pricing-v1",
                "cohort_id": "week3-test",
                "cohort_cost_limit_usd": 10.0,
                "writable_paths": ["app.py"],
                "test_commands": [list(TEST_COMMAND)],
                "max_total_tokens_per_call": 1000,
                "max_output_tokens_per_call": 200,
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 2.0,
            }
            contract_path = root / "contract-resume-overlap.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            contract_hash = hashlib.sha256(
                json.dumps(
                    contract,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            checkpoint = self.make_checkpoint(
                task_worktree,
                original_snapshot={
                    "branch": "master",
                    "head": BASE_SHA,
                    "staged": [],
                    "tracked": [],
                    "untracked": [],
                    "contract_hash": contract_hash,
                },
            )
            store = CheckpointStore(state_root)
            store.save(checkpoint)
            snapshot_before = store.snapshot_path("run-1").read_bytes()
            journal_before = store.journal_path("run-1").read_bytes()

            with mock.patch.object(repair_module, "DockerWorktreeBackend") as backend:
                with self.assertRaisesRegex(WorktreePolicyError, "task worktree"):
                    repair_module._run_repair_contract(contract_path, resume=True)

            backend.assert_not_called()
            self.assertEqual(store.snapshot_path("run-1").read_bytes(), snapshot_before)
            self.assertEqual(store.journal_path("run-1").read_bytes(), journal_before)

    def test_repair_cli_resume_reuses_the_durable_budget_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            worktree = root / "task"
            worktree_root = root / "worktrees"
            original.mkdir()
            worktree.mkdir()
            worktree_root.mkdir()
            contract = {
                "run_id": "run-1",
                "issue_slug": "issue-1",
                "issue_ref": "https://github.com/example/repo/issues/1",
                "issue_context": "issue body and approved context",
                "repository_id": "example/repo",
                "base_sha": BASE_SHA,
                "original_checkout": str(original),
                "worktree_root": str(worktree_root),
                "state_root": str(root / "state"),
                "docker_image": "repair:test",
                "llm_provider": "deepseek",
                "llm_model": "model",
                "llm_thinking": "disabled",
                "pricing_id": "test-pricing-v1",
                "cohort_id": "week3-test",
                "cohort_cost_limit_usd": 10.0,
                "writable_paths": ["app.py"],
                "test_commands": [list(TEST_COMMAND)],
                "max_total_tokens_per_call": 1000,
                "max_output_tokens_per_call": 200,
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 2.0,
            }
            contract_path = root / "contract.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            contract_hash = hashlib.sha256(
                json.dumps(
                    contract,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            durable_budget = BudgetManager()
            reservation = durable_budget.reserve_llm(40, 0.01)
            durable_budget.reconcile_llm(reservation.reservation_id, 30, 0.008)
            durable_budget.consume_tool_call(3)
            durable_budget.consume_command(12)
            original_snapshot = RepositorySnapshot("master", BASE_SHA)
            checkpoint = self.make_checkpoint(
                worktree,
                original_snapshot={
                    "branch": original_snapshot.branch,
                    "head": original_snapshot.head,
                    "staged": [],
                    "tracked": [],
                    "untracked": [],
                    "contract_hash": contract_hash,
                },
                budget=durable_budget.to_dict(),
            )
            CheckpointStore(root / "state").save(checkpoint)
            result = RepairRunResult(RepairState.CANCELLED, "write_approval_rejected")
            with (
                mock.patch.object(repair_module, "DockerWorktreeBackend") as backend,
                mock.patch.object(
                    repair_module,
                    "build_repair_sandbox",
                    return_value=StatefulSandbox(),
                ),
                mock.patch.object(repair_module, "OpenAIRepairModel"),
                mock.patch.object(repair_module, "TTYApprovalProvider"),
                mock.patch.object(
                    repair_module,
                    "SandboxedGitCommitControl",
                ) as commit_control,
                mock.patch.object(repair_module, "RepairOrchestrator") as orchestrator,
                mock.patch(
                    "code_review_agent.llm.make_client",
                    return_value=(runtime_client(), "model"),
                ),
                mock.patch.dict(repair_module.os.environ, {"LLM_PROVIDER": "deepseek"}),
            ):
                backend.return_value.snapshot.return_value = original_snapshot
                orchestrator.return_value.run.return_value = result
                observed = repair_module._run_repair_contract(contract_path, resume=True)

            injected = orchestrator.call_args.kwargs["budget_manager"]
            self.assertEqual(observed, result)
            self.assertEqual(injected.to_dict(), durable_budget.to_dict())
            self.assertEqual(injected.usage.commands, 12)
            self.assertIs(backend.call_args.kwargs["budget"], injected)
            self.assertIs(commit_control.call_args.kwargs["budget"], injected)

    def test_terminal_original_snapshot_validation_fails_closed(self):
        checkpoint = self.make_checkpoint(
            Path("task"),
            original_snapshot={
                "branch": "master",
                "head": BASE_SHA,
                "staged": [],
                "tracked": [],
                "untracked": [],
            },
        )
        expected = repair_module._checkpoint_original_snapshot(checkpoint)
        self.assertEqual(expected, RepositorySnapshot("master", BASE_SHA))

        backend = mock.Mock()
        backend.snapshot.return_value = RepositorySnapshot("master", "b" * 40)
        with self.assertRaises(repair_module.OriginalCheckoutChanged):
            repair_module._assert_original_checkout_unchanged(
                backend, Path("original"), expected
            )

        checkpoint.original_snapshot.pop("tracked")
        with self.assertRaisesRegex(WorktreePolicyError, "status"):
            repair_module._checkpoint_original_snapshot(checkpoint)

    def test_resume_rejects_a_changed_contract_before_runtime_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = CheckpointStore(root / "state")
            checkpoint = self.make_checkpoint(root / "task")
            checkpoint.original_snapshot["contract_hash"] = "0" * 64
            store.save(checkpoint)
            data = {
                "run_id": "run-1", "issue_slug": "issue-1", "issue_ref": "issue",
                "issue_context": "changed", "repository_id": "repo-id", "base_sha": BASE_SHA,
                "original_checkout": str(root / "original"),
                "worktree_root": str(root / "worktrees"), "state_root": str(root / "state"),
                "docker_image": "repair:test", "writable_paths": ["app.py"],
                "llm_provider": "deepseek", "llm_model": "model",
                "llm_thinking": "disabled",
                "pricing_id": "test-pricing-v1",
                "cohort_id": "week3-test", "cohort_cost_limit_usd": 10.0,
                "test_commands": [list(TEST_COMMAND)], "max_total_tokens_per_call": 1000,
                "max_output_tokens_per_call": 200, "input_cost_per_million": 1.0,
                "output_cost_per_million": 2.0,
            }
            path = root / "changed.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(WorktreePolicyError, "does not match"):
                repair_module._run_repair_contract(path, resume=True)

    def test_contract_requires_reviewed_llm_identity_and_pricing_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = {
                "run_id": "run-1",
                "issue_slug": "issue-1",
                "issue_ref": "issue",
                "issue_context": "context",
                "repository_id": "repo-id",
                "base_sha": BASE_SHA,
                "original_checkout": str(root / "original"),
                "worktree_root": str(root / "worktrees"),
                "state_root": str(root / "state"),
                "docker_image": "repair:test",
                "llm_provider": "deepseek",
                "llm_model": "model",
                "llm_thinking": "disabled",
                "pricing_id": "test-pricing-v1",
                "cohort_id": "week3-test",
                "cohort_cost_limit_usd": 10.0,
                "writable_paths": ["app.py"],
                "test_commands": [list(TEST_COMMAND)],
                "max_total_tokens_per_call": 1000,
                "max_output_tokens_per_call": 200,
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 2.0,
            }
            for missing in (
                "llm_provider",
                "llm_model",
                "llm_thinking",
                "pricing_id",
                "cohort_id",
                "cohort_cost_limit_usd",
            ):
                with self.subTest(missing=missing):
                    contract = dict(base)
                    contract.pop(missing)
                    path = root / f"missing-{missing}.json"
                    path.write_text(json.dumps(contract), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, missing):
                        repair_module._run_repair_contract(path, resume=False)
            excessive = base | {"cohort_cost_limit_usd": 10.000001}
            excessive_path = root / "excessive-cohort.json"
            excessive_path.write_text(json.dumps(excessive), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "USD 10"):
                repair_module._run_repair_contract(excessive_path, resume=False)
            duplicate_path = root / "duplicate.json"
            duplicate_path.write_text('{"run_id":"a","run_id":"b"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                repair_module._run_repair_contract(duplicate_path, resume=False)

    def test_contract_rejects_runtime_provider_or_model_mismatch_before_task_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            original.mkdir()
            base = {
                "run_id": "run-1",
                "issue_slug": "issue-1",
                "issue_ref": "issue",
                "issue_context": "context",
                "repository_id": "repo-id",
                "base_sha": BASE_SHA,
                "original_checkout": str(original),
                "worktree_root": str(root / "worktrees"),
                "state_root": str(root / "state"),
                "docker_image": "repair:test",
                "llm_provider": "glm",
                "llm_model": "glm-5.2",
                "llm_thinking": "disabled",
                "pricing_id": "human-reviewed-2026-07-16",
                "cohort_id": "week3-test",
                "cohort_cost_limit_usd": 10.0,
                "writable_paths": ["app.py"],
                "test_commands": [list(TEST_COMMAND)],
                "max_total_tokens_per_call": 1000,
                "max_output_tokens_per_call": 200,
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 2.0,
            }
            cases = (
                ("provider", "deepseek", "glm-5.2", "provider"),
                ("model", "glm", "glm-4.6", "model"),
            )
            for name, provider, runtime_model, expected in cases:
                with self.subTest(name=name):
                    path = root / f"mismatch-{name}.json"
                    path.write_text(json.dumps(base), encoding="utf-8")
                    with (
                        mock.patch(
                            "code_review_agent.llm.make_client",
                            return_value=(object(), runtime_model),
                        ) as make_client,
                        mock.patch.dict(
                            repair_module.os.environ,
                            {"LLM_PROVIDER": provider},
                        ),
                        mock.patch.object(
                            repair_module, "DockerWorktreeBackend"
                        ) as backend,
                        self.assertRaisesRegex(WorktreePolicyError, expected),
                    ):
                        repair_module._run_repair_contract(path, resume=False)
                    make_client.assert_called_once_with()
                    backend.assert_not_called()

    def test_openai_repair_adapter_returns_strict_metered_values(self):
        responses = [
            json.dumps(
                {
                    "summary": "small repair",
                    "writable_paths": ["app.py"],
                    "test_commands": [["python", "-m", "unittest"]],
                    "risks": ["small"],
                    "rollback_boundary": "app.py",
                    "commit_message": "fix: issue",
                    "revision": 1,
                }
            ),
            json.dumps({"patch": patch_for(1)}),
            json.dumps({"decision": "success", "reason": "tests pass"}),
        ]
        requests = []

        def create(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=responses.pop(0)))],
                usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5),
            )

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        model = OpenAIRepairModel(
            client=client,
            model="test-model",
            issue_context="Issue body and approved source context",
            max_total_tokens=2000,
            max_output_tokens=200,
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
        )
        plan_result = model.make_plan("issue", previous_plan=None, evidence={})
        patch_result = model.make_patch(plan_result.value, patch_attempt=1, evidence={})
        reflection_result = model.reflect(
            plan_result.value,
            patch_attempt=1,
            test_results=(
                SimpleNamespace(
                    argv=TEST_COMMAND,
                    operation_id="test-1",
                    exit_code=0,
                    stdout="ok",
                    stderr="",
                    duration_seconds=0.1,
                    timed_out=False,
                    output_truncated=False,
                ),
            ),
        )
        self.assertEqual(plan_result.actual_tokens, 25)
        self.assertEqual(patch_result.value, patch_for(1))
        self.assertEqual(reflection_result.value.decision, ReflectionDecision.SUCCESS)
        self.assertAlmostEqual(plan_result.actual_cost_usd, 0.00003)
        self.assertEqual(model.limits_for("plan").max_cost_usd, 0.004)
        patch_prompt = json.loads(requests[1]["messages"][1]["content"])["task"]
        self.assertIn("end the patch string with one newline", patch_prompt)
        self.assertIn("function-local import is not a module attribute", patch_prompt)
        self.assertTrue(
            all(
                request["response_format"] == {"type": "json_object"}
                for request in requests
            )
        )

    def test_openai_repair_adapter_normalizes_structured_reflection_reason(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "decision": "retry",
                                "reason": ["focused tests failed", "revise the patch"],
                            }
                        )
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5),
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=mock.Mock(return_value=response))
            )
        )
        model = OpenAIRepairModel(
            client=client,
            model="test-model",
            issue_context="context",
            max_total_tokens=2000,
            max_output_tokens=200,
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
        )
        plan = RepairPlan(
            summary="small repair",
            writable_paths=("app.py",),
            test_commands=(TEST_COMMAND,),
            risks=("small",),
            rollback_boundary="app.py",
            commit_message="fix: issue",
        )

        result = model.reflect(
            plan,
            patch_attempt=1,
            test_results=(
                SimpleNamespace(
                    argv=TEST_COMMAND,
                    operation_id="test-1",
                    exit_code=1,
                    stdout="failed",
                    stderr="",
                    duration_seconds=0.1,
                    timed_out=False,
                    output_truncated=False,
                ),
            ),
        )

        self.assertEqual(result.value.decision, ReflectionDecision.RETRY)
        self.assertEqual(
            result.value.reason,
            '["focused tests failed", "revise the patch"]',
        )

    def test_openai_repair_adapter_normalizes_transport_defects_in_patch(self):
        malformed = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,9 +1,9 @@\n"
            "-old\n"
            "new\n"
        )
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps({"patch": malformed}))
                )
            ],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5),
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=mock.Mock(return_value=response))
            )
        )
        model = OpenAIRepairModel(
            client=client,
            model="test-model",
            issue_context="context",
            max_total_tokens=2000,
            max_output_tokens=200,
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
        )
        plan = RepairPlan(
            summary="small repair",
            writable_paths=("app.py",),
            test_commands=(TEST_COMMAND,),
            risks=("small",),
            rollback_boundary="app.py",
            commit_message="fix: issue",
        )

        result = model.make_patch(plan, patch_attempt=1, evidence={})

        self.assertEqual(
            result.value,
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n",
        )

    def test_openai_repair_adapter_rejects_oversized_payload_before_client_call(self):
        create = mock.Mock()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        model = OpenAIRepairModel(
            client=client,
            model="test-model",
            issue_context="context",
            max_total_tokens=100,
            max_output_tokens=20,
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
        )
        plan = RepairPlan(
            summary="x" * 200,
            writable_paths=("app.py",),
            test_commands=(TEST_COMMAND,),
            risks=("small",),
            rollback_boundary="app.py",
            commit_message="fix: issue",
            revision=1,
        )

        with self.assertRaisesRegex(WorktreePolicyError, "payload exceeds"):
            model.make_patch(plan, patch_attempt=1, evidence={})

        create.assert_not_called()

    def test_openai_repair_adapter_rounds_cost_up_to_integer_micro_usd(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
        create = mock.Mock(return_value=response)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        model = OpenAIRepairModel(
            client=client,
            model="test-model",
            issue_context="context",
            max_total_tokens=1000,
            max_output_tokens=200,
            input_cost_per_million=0.1,
            output_cost_per_million=0.2000001,
            disable_thinking=True,
        )

        result = model._chat("patch", {"small": True})
        limit = model.limits_for("patch")

        self.assertEqual(result.actual_cost_usd, 0.000001)
        self.assertEqual(limit.max_cost_usd, 0.000201)
        self.assertEqual(result.actual_cost_usd * 1_000_000, 1)
        self.assertEqual(limit.max_cost_usd * 1_000_000, 201)
        self.assertEqual(
            create.call_args.kwargs["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_openai_adapter_and_contract_parsing_fail_closed_on_invalid_data(self):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace()))
        base = {
            "client": client,
            "model": "model",
            "issue_context": "context",
            "max_total_tokens": 100,
            "max_output_tokens": 20,
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 1.0,
        }
        for overrides in (
            {"issue_context": ""},
            {"issue_context": "x" * (64 * 1024 + 1)},
            {"max_total_tokens": True},
            {"max_output_tokens": 100},
            {"input_cost_per_million": -1},
            {"input_cost_per_million": 0},
            {"input_cost_per_million": float("nan")},
            {"output_cost_per_million": float("inf")},
            {"output_cost_per_million": float("-inf")},
            {"output_cost_per_million": 10**1000},
            {"disable_thinking": "yes"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                OpenAIRepairModel(**(base | overrides))
        model = OpenAIRepairModel(**base)
        with self.assertRaisesRegex(ValueError, "unknown"):
            model.limits_for("unknown")
        for value in (True, 0, "1"):
            with self.subTest(integer=value), self.assertRaises(ValueError):
                repair_module._positive_contract_int({"field": value}, "field")
        for value in (
            True,
            -1,
            0,
            float("nan"),
            float("inf"),
            float("-inf"),
            10**1000,
            "1",
        ):
            with self.subTest(number=value), self.assertRaises(ValueError):
                repair_module._contract_number({"field": value}, "field")
        with self.assertRaises(ValueError):
            repair_module._contract_text({}, "field")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contract.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                repair_module._json_file(path)
        with self.assertRaisesRegex(WorktreePolicyError, "invalid JSON"):
            repair_module._json_object("not-json")
        with self.assertRaisesRegex(WorktreePolicyError, "must be an object"):
            repair_module._json_object("[]")
        self.assertEqual(
            repair_module._json_object('```json\n{"patch":"line 1\nline 2"}\n```'),
            {"patch": "line 1\nline 2"},
        )

    def test_sandboxed_commit_control_stages_exact_patch_and_restores_on_failure(self):
        budget = BudgetManager()
        sandbox = CommitSandbox()
        allowed = []

        def factory(commands):
            allowed.append(commands)
            return sandbox

        control = SandboxedGitCommitControl(sandbox_factory=factory, budget=budget)
        persisted = []
        control.bind_budget_persister(persisted.append)
        before = control.inspect()
        expected_tree_oid = control.expected_tree(
            patch_text=patch_for(1), writable_paths=("app.py",)
        )
        self.assertEqual(expected_tree_oid, APPROVED_TREE_OID)
        self.assertFalse(sandbox.staged)
        outcome = control.commit(
            "fix: resolve issue 1",
            patch_text=patch_for(1),
            writable_paths=("app.py",),
            expected_tree_oid=expected_tree_oid,
        )
        after = control.inspect()
        self.assertFalse(before.clean)
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.commit_sha, COMMIT_SHA)
        self.assertTrue(after.clean)
        self.assertEqual(after.parent, BASE_SHA)
        self.assertEqual(after.message, "fix: resolve issue 1")
        self.assertEqual(after.tree_oid, APPROVED_TREE_OID)
        self.assertTrue(all(isinstance(command, tuple) for group in allowed for command in group))

        mismatch_sandbox = CommitSandbox()
        mismatch = SandboxedGitCommitControl(
            sandbox_factory=lambda _commands: mismatch_sandbox,
            budget=BudgetManager(),
        )
        mismatch.bind_budget_persister(lambda _event: None)
        with self.assertRaisesRegex(WorktreePolicyError, "human-approved tree"):
            mismatch.commit(
                "fix: resolve issue 1",
                patch_text=patch_for(1),
                writable_paths=("app.py",),
                expected_tree_oid="e" * 40,
            )
        self.assertEqual(mismatch_sandbox.head, BASE_SHA)
        self.assertFalse(mismatch_sandbox.staged)

        merge_control = SandboxedGitCommitControl(
            sandbox_factory=lambda _commands: CommitSandbox(
                base_parents=("c" * 40, "d" * 40)
            ),
            budget=BudgetManager(),
        )
        merge_control.bind_budget_persister(lambda _event: None)
        merge_base = merge_control.inspect()
        self.assertEqual(merge_base.head, BASE_SHA)
        self.assertEqual(merge_base.parent, "")

        failed_sandbox = CommitSandbox(fail_commit=True)
        failed_control = SandboxedGitCommitControl(
            sandbox_factory=lambda _commands: failed_sandbox,
            budget=BudgetManager(),
        )
        failed_control.bind_budget_persister(lambda _event: None)
        failed = failed_control.commit(
            "fix: resolve issue 1",
            patch_text=patch_for(1),
            writable_paths=("app.py",),
            expected_tree_oid=APPROVED_TREE_OID,
        )
        self.assertFalse(failed.success)
        self.assertFalse(failed_sandbox.staged)
        self.assertGreater(len(persisted), budget.usage.commands)

    def test_snapshot_diff_text_stays_parseable_across_tracked_and_new_files(self):
        # A repair that edits a tracked file and adds a new file yields a
        # base diff plus a per-file untracked diff. The combined commit-gate
        # patch must remain a single valid multi-file unified diff: joining the
        # sections with "\n" injects a blank line inside the previous hunk and
        # makes parse_patch (and therefore the commit scope check) reject it.
        base_diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\n"
        )
        new_file_diff = (
            "diff --git a/tests/test_new.py b/tests/test_new.py\n"
            "new file mode 100644\n"
            "index 0000000000000000000000000000000000000000.."
            "1111111111111111111111111111111111111111\n"
            "--- /dev/null\n"
            "+++ b/tests/test_new.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def test_new():\n"
            "+    assert True\n"
        )
        snapshot = RepairRepositorySnapshot(
            status=GitStatusResult(operation_id="op-1", entries=(), ignored_paths=()),
            base_diff=base_diff,
            untracked_diffs=(("tests/test_new.py", new_file_diff),),
            sha256="0" * 64,
        )

        diff_text = repair_module._snapshot_diff_text(snapshot)

        self.assertNotIn("\n\n", diff_text)
        document = parse_patch(diff_text)
        self.assertEqual(document.paths, ("app.py", "tests/test_new.py"))
        # The commit gate (expected_tree and commit) validates the same text.
        SandboxedGitCommitControl._validate_patch_scope(
            diff_text, ("app.py", "tests/test_new.py")
        )

        # Two new files with no tracked edit must also stay parseable.
        second_new = new_file_diff.replace("test_new", "test_two")
        untracked_only = RepairRepositorySnapshot(
            status=GitStatusResult(operation_id="op-2", entries=(), ignored_paths=()),
            base_diff="",
            untracked_diffs=(
                ("tests/test_new.py", new_file_diff),
                ("tests/test_two.py", second_new),
            ),
            sha256="1" * 64,
        )
        multi_new = repair_module._snapshot_diff_text(untracked_only)
        self.assertNotIn("\n\n", multi_new)
        self.assertEqual(
            parse_patch(multi_new).paths,
            ("tests/test_new.py", "tests/test_two.py"),
        )

    def test_commit_command_budget_is_persisted_before_and_after_interrupt(self):
        budget = BudgetManager()
        sandbox = mock.Mock()
        sandbox.run.side_effect = KeyboardInterrupt("docker interrupted")
        snapshots = []
        control = SandboxedGitCommitControl(
            sandbox_factory=lambda _commands: sandbox,
            budget=budget,
        )
        control.bind_budget_persister(
            lambda event: snapshots.append((event, budget.to_dict()))
        )

        with self.assertRaises(KeyboardInterrupt):
            control.inspect()

        self.assertEqual(
            [event for event, _snapshot in snapshots],
            ["commit_command_consumed", "commit_command_interrupted"],
        )
        restored = BudgetManager.from_dict(snapshots[-1][1])
        self.assertEqual(restored.usage.commands, 1)

    def test_repair_client_disables_sdk_retries_or_refuses_to_start(self):
        configured = SimpleNamespace(max_retries=0)
        with_options = mock.Mock(return_value=configured)
        base = SimpleNamespace(with_options=with_options)

        self.assertIs(repair_module._repair_client_without_retries(base), configured)
        with_options.assert_called_once_with(max_retries=0)
        for client in (
            object(),
            SimpleNamespace(with_options=lambda **_options: SimpleNamespace(max_retries=2)),
        ):
            with self.subTest(client=client), self.assertRaisesRegex(
                WorktreePolicyError, "retries"
            ):
                repair_module._repair_client_without_retries(client)

    def test_tty_provider_requires_exact_human_challenge_for_each_gate(self):
        plan = FakeModel().make_plan("issue", previous_plan=None, evidence={}).value
        output = TTYBuffer()
        provider = TTYApprovalProvider(
            input_stream=TTYBuffer("APPROVE WRITE fixed-nonce\n"),
            output_stream=output,
            clock=lambda: 100.0,
            nonce_factory=lambda: "fixed-nonce",
        )
        write = provider.request_write(
            WriteApprovalRequest(
                run_id="run-1",
                checkpoint_id="cp-1",
                base_sha=BASE_SHA,
                diff_hash="d" * 64,
                patch_hash=hashlib.sha256(patch_for(1).encode("utf-8")).hexdigest(),
                patch_text=patch_for(1),
                plan=plan,
                patch_attempt=1,
            )
        )
        self.assertIsNotNone(write)
        self.assertIn('"plan"', output.getvalue())
        self.assertIn(f'"patch_hash": "{write.binding.patch_hash}"', output.getvalue())
        self.assertIn(json.dumps(patch_for(1))[1:-1], output.getvalue())

        provider = TTYApprovalProvider(
            input_stream=TTYBuffer("reject\n"),
            output_stream=TTYBuffer(),
            clock=lambda: 100.0,
            nonce_factory=lambda: "commit-nonce",
        )
        commit = provider.request_commit(
            CommitApprovalRequest(
                run_id="run-1",
                checkpoint_id="cp-2",
                base_sha=BASE_SHA,
                diff_hash="e" * 64,
                test_result_hash="t" * 64,
                commit_message="fix: resolve issue 1",
                expected_tree_oid=APPROVED_TREE_OID,
                diff_text="diff --git a/app.py b/app.py",
            )
        )
        self.assertIsNone(commit)

        provider = TTYApprovalProvider(
            input_stream=io.StringIO("APPROVE WRITE fixed-nonce\n"),
            output_stream=TTYBuffer(),
        )
        with self.assertRaises(ApprovalError):
            provider.request_write(
                WriteApprovalRequest(
                    run_id="run-1",
                    checkpoint_id="cp-1",
                    base_sha=BASE_SHA,
                    diff_hash="d" * 64,
                    patch_hash=hashlib.sha256(patch_for(1).encode("utf-8")).hexdigest(),
                    patch_text=patch_for(1),
                    plan=plan,
                    patch_attempt=1,
                )
            )

    def test_happy_path_persists_both_approvals_and_one_verified_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, store, sandbox, model, approvals, commit = self.make_orchestrator(tmp)
            result = orchestrator.run()
            loaded = store.load("run-1")

        self.assertEqual(result.state, RepairState.SUBMIT)
        self.assertEqual(result.commit_sha, COMMIT_SHA)
        self.assertEqual(commit.calls, ["fix: resolve issue 1"])
        self.assertEqual(len(approvals.write_requests), 1)
        self.assertEqual(len(approvals.commit_requests), 1)
        self.assertEqual(len(loaded.approvals), 2)
        self.assertTrue(all(item["consumed_at"] is not None for item in loaded.approvals))
        self.assertEqual(loaded.state, RepairState.SUBMIT)
        self.assertEqual(loaded.state_history[0], RepairState.DISCOVER)
        self.assertEqual(loaded.state_history[-1], RepairState.SUBMIT)
        self.assertTrue(sandbox.committed)
        self.assertEqual(model.patch_attempts[0][1], 1)
        self.assertEqual(
            model.plan_calls[0][2]["approved_sources_at_base"],
            {"app.py": "old-1\n"},
        )
        self.assertEqual(
            model.patch_attempts[0][2]["approved_sources_at_base"],
            {"app.py": "old-1\n"},
        )
        self.assertEqual(model.patch_attempts[0][2]["current_diff"], "")
        self.assertEqual(orchestrator.budget.usage.tokens, 30)
        self.assertAlmostEqual(orchestrator.budget.usage.cost_usd, 0.003)
        self.assertEqual(orchestrator.budget.to_dict()["reservations"], [])

    def test_rejected_patch_preflight_is_bounded_before_write_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = RejectFirstPatchSandbox()
            orchestrator, store, sandbox, model, approvals, commit = (
                self.make_orchestrator(tmp, sandbox=sandbox)
            )
            result = orchestrator.run()
            loaded = store.load("run-1")
            event_kinds = [item["kind"] for item in store.events("run-1")]

        self.assertEqual(result.state, RepairState.SUBMIT)
        self.assertEqual(commit.calls, ["fix: resolve issue 1"])
        self.assertEqual(len(approvals.write_requests), 1)
        self.assertEqual(approvals.write_requests[0].patch_attempt, 1)
        self.assertEqual([item[1] for item in model.patch_attempts], [1, 1])
        rejection_evidence = model.patch_attempts[1][2]["patch_rejections"]
        self.assertEqual(len(rejection_evidence), 1)
        self.assertEqual(rejection_evidence[0]["attempt"], 1)
        self.assertEqual(
            rejection_evidence[0]["patch_hash"],
            hashlib.sha256(patch_for(1).encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            rejection_evidence[0]["reason"],
            "patch does not apply: stale model context",
        )
        self.assertEqual(rejection_evidence[0]["patch_excerpt"], patch_for(1))
        self.assertEqual(rejection_evidence[0]["paths"], ["app.py"])
        self.assertEqual(
            approvals.write_requests[0].patch_hash,
            hashlib.sha256(patch_for(1).encode("utf-8")).hexdigest(),
        )
        self.assertEqual(loaded.budget["usage"]["repair_attempts"], 0)
        rejections = [
            item
            for item in loaded.tool_ledger
            if item.get("kind") == "patch_rejection"
        ]
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["status"], "candidate_retry_consumed")
        self.assertTrue(all(item["consumed_at"] is not None for item in loaded.approvals))
        write_nonces = [
            item["binding"]["nonce"]
            for item in loaded.approvals
            if item["binding"]["kind"] == "write"
        ]
        self.assertEqual(len(write_nonces), 1)
        self.assertEqual(len(sandbox.patches), 1)
        passed = [
            item for item in loaded.tool_ledger if item.get("kind") == "patch_preflight"
        ]
        self.assertEqual(len(passed), 1)
        self.assertEqual(passed[0]["patch_hash"], approvals.write_requests[0].patch_hash)

        retry_event = event_kinds.index("patch_candidate_retry_scheduled")
        second_reservation = event_kinds.index(
            "llm_patch_reserved",
            event_kinds.index("llm_patch_reserved") + 1,
        )
        self.assertLess(retry_event, second_reservation)

    def test_out_of_plan_patch_is_repaired_before_write_approval(self):
        class FirstPatchOutOfPlan(FakeModel):
            def make_patch(self, plan, *, patch_attempt, evidence):
                self.patch_attempts.append((plan.revision, patch_attempt, evidence))
                patch = patch_for(patch_attempt)
                if len(self.patch_attempts) == 1:
                    patch = patch.replace("app.py", "outside.py")
                return ModelCallResult(
                    patch,
                    actual_tokens=10,
                    actual_cost_usd=0.001,
                )

        with tempfile.TemporaryDirectory() as tmp:
            model = FirstPatchOutOfPlan()
            orchestrator, store, sandbox, _model, approvals, commit = (
                self.make_orchestrator(tmp, model=model)
            )

            result = orchestrator.run()
            durable = store.load("run-1")

        self.assertEqual(result.state, RepairState.SUBMIT)
        self.assertEqual([item[1] for item in model.patch_attempts], [1, 1])
        self.assertEqual(len(approvals.write_requests), 1)
        self.assertEqual(approvals.write_requests[0].patch_attempt, 1)
        rejection = next(
            item
            for item in durable.tool_ledger
            if item.get("kind") == "patch_rejection"
        )
        self.assertEqual(rejection["paths"], ["outside.py"])
        self.assertIn("outside the current plan", rejection["reason"])
        self.assertEqual(len(sandbox.patches), 1)
        self.assertEqual(len(commit.calls), 1)

    def test_candidate_retry_does_not_consume_post_test_self_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = RejectFirstPatchSandbox()
            sandbox.test_exit_codes = [1, 0]
            orchestrator, store, sandbox, model, approvals, commit = (
                self.make_orchestrator(tmp, sandbox=sandbox)
            )

            result = orchestrator.run()
            loaded = store.load("run-1")

        self.assertEqual(result.state, RepairState.SUBMIT)
        self.assertEqual([item[1] for item in model.patch_attempts], [1, 1, 2])
        self.assertEqual(
            [request.patch_attempt for request in approvals.write_requests], [1, 2]
        )
        self.assertEqual(loaded.budget["usage"]["repair_attempts"], 1)
        self.assertEqual(len(loaded.test_results), 2)
        self.assertEqual(commit.calls, ["fix: resolve issue 1"])

    def test_repeated_patch_rejections_exhaust_candidate_budget_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = RejectFirstPatchSandbox(reject_preflights=3)
            orchestrator, store, sandbox, model, approvals, commit = (
                self.make_orchestrator(tmp, sandbox=sandbox)
            )
            result = orchestrator.run()
            loaded = store.load("run-1")

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "patch_candidate_retry_budget_exhausted")
        self.assertEqual([item[1] for item in model.patch_attempts], [1, 1, 1])
        self.assertEqual(len(approvals.write_requests), 0)
        self.assertEqual(loaded.budget["usage"]["repair_attempts"], 0)
        rejection_statuses = [
            item["status"]
            for item in loaded.tool_ledger
            if item.get("kind") == "patch_rejection"
        ]
        self.assertEqual(
            rejection_statuses,
            [
                "candidate_retry_consumed",
                "candidate_retry_consumed",
                "candidate_retry_budget_exhausted",
            ],
        )
        self.assertEqual(sandbox.patches, [])
        self.assertEqual(commit.calls, [])
        self.assertTrue(all(item["consumed_at"] is not None for item in loaded.approvals))

    def test_model_reservation_budget_refusal_becomes_durable_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            limits = BudgetLimits(total_tokens=50)
            worktree = Path(tmp) / "task"
            worktree.mkdir()
            checkpoint = self.make_checkpoint(
                worktree,
                budget=BudgetManager(limits).to_dict(),
            )
            model = FakeModel()
            orchestrator, store, sandbox, model, approvals, commit = (
                self.make_orchestrator(
                    tmp,
                    model=model,
                    checkpoint=checkpoint,
                    expected_limits=limits,
                )
            )
            result = orchestrator.run()
            loaded = store.load("run-1")

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "budget_exceeded")
        self.assertEqual(model.plan_calls, [])
        self.assertEqual(model.patch_attempts, [])
        self.assertEqual(approvals.write_requests, [])
        self.assertEqual(commit.calls, [])
        self.assertEqual(sandbox.patches, [])
        self.assertEqual(loaded.state, RepairState.FAILED)
        self.assertIsNone(loaded.in_progress_operation)

    def test_elapsed_budget_failure_uses_emergency_terminal_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            limits = BudgetLimits(total_seconds=1.0)
            worktree = Path(tmp) / "task"
            worktree.mkdir()
            checkpoint = self.make_checkpoint(
                worktree,
                updated_at=98.0,
                budget=BudgetManager(limits).to_dict(),
            )
            model = FakeModel()
            orchestrator, store, _sandbox, model, approvals, commit = (
                self.make_orchestrator(
                    tmp,
                    model=model,
                    checkpoint=checkpoint,
                    expected_limits=limits,
                )
            )
            result = orchestrator.run()
            loaded = store.load("run-1")

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "budget_exceeded")
        self.assertEqual(model.plan_calls, [])
        self.assertEqual(approvals.write_requests, [])
        self.assertEqual(commit.calls, [])
        self.assertEqual(loaded.state, RepairState.FAILED)
        self.assertTrue(
            any(item.get("kind") == "budget_failure" for item in loaded.tool_ledger)
        )

    def test_preflight_tool_budget_failure_is_durably_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            limits = BudgetLimits(tool_calls=1)
            budget = BudgetManager(limits)
            budget.consume_tool_call()
            worktree = Path(tmp) / "task"
            worktree.mkdir()
            checkpoint = self.make_checkpoint(worktree, budget=budget.to_dict())
            model = FakeModel()
            orchestrator, store, _sandbox, model, approvals, commit = (
                self.make_orchestrator(
                    tmp,
                    model=model,
                    checkpoint=checkpoint,
                    expected_limits=limits,
                    preflight=lambda: None,
                )
            )
            orchestrator._preflight = lambda: orchestrator.budget.consume_tool_call()
            result = orchestrator.run()
            loaded = store.load("run-1")

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "budget_exceeded")
        self.assertEqual(model.plan_calls, [])
        self.assertEqual(approvals.write_requests, [])
        self.assertEqual(commit.calls, [])
        self.assertEqual(loaded.state, RepairState.FAILED)
        self.assertIsNone(loaded.in_progress_operation)

    def test_exhausted_tool_budget_quarantines_required_failure_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            limits = BudgetLimits(tool_calls=1)
            budget = BudgetManager(limits)
            budget.consume_tool_call()
            worktree = Path(tmp) / "task"
            worktree.mkdir()
            patch_text = patch_for(1)
            manifest = PatchManifest(
                manifest_id="manifest-budget-cleanup",
                run_id="run-1",
                state=ManifestState.APPLIED,
                patch=parse_patch(patch_text),
                before_snapshot_hash=snapshot_hash(),
                after_snapshot_hash=snapshot_hash((patch_text,)),
                approval_receipt="approval-budget-cleanup",
                rollback_token="rollback-budget-cleanup",
            )
            checkpoint = self.make_checkpoint(
                worktree,
                state=RepairState.FAILED,
                state_history=(
                    RepairState.DISCOVER,
                    RepairState.PLAN,
                    RepairState.PATCH,
                    RepairState.FAILED,
                ),
                budget=budget.to_dict(),
                diff_hash=manifest.after_snapshot_hash,
                in_progress_operation={
                    "kind": "failure",
                    "reason": "budget_exceeded",
                    "cleanup_status": "pending",
                },
                tool_ledger=[
                    {
                        "kind": "patch_manifest",
                        "manifest_id": manifest.manifest_id,
                        "manifest": manifest.to_dict(),
                    }
                ],
            )
            sandbox = StatefulSandbox()
            sandbox.patches.append(patch_text)
            model = FakeModel()
            orchestrator, store, sandbox, model, approvals, commit = (
                self.make_orchestrator(
                    tmp,
                    sandbox=sandbox,
                    model=model,
                    checkpoint=checkpoint,
                    expected_limits=limits,
                )
            )
            result = orchestrator.run()
            loaded = store.load("run-1")

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "budget_exceeded")
        self.assertEqual(model.patch_attempts, [])
        self.assertEqual(approvals.write_requests, [])
        self.assertEqual(commit.calls, [])
        self.assertEqual(sandbox.patches, [patch_text])
        self.assertEqual(loaded.in_progress_operation["kind"], "failure")
        self.assertEqual(
            loaded.in_progress_operation["cleanup_status"], "quarantined"
        )

    def test_failure_transition_intent_recovers_before_any_model_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "task"
            worktree.mkdir()
            budget = BudgetManager()
            budget.consume_repair_attempt()
            budget.consume_repair_attempt()
            checkpoint = self.make_checkpoint(
                worktree,
                state=RepairState.PATCH,
                state_history=(RepairState.DISCOVER, RepairState.PLAN, RepairState.PATCH),
                sequence=7,
                budget=budget.to_dict(),
                in_progress_operation={
                    "kind": "transition",
                    "from": "PATCH",
                    "to": "FAILED",
                    "preserve": {
                        "kind": "failure",
                        "reason": "repair_attempt_budget_exhausted",
                        "cleanup_status": "pending",
                    },
                },
                tool_ledger=[
                    {
                        "kind": "llm_call",
                        "operation": "patch",
                        "reservation_id": "exhausted-call",
                        "status": "consumed",
                    }
                ],
            )
            model = FakeModel()
            orchestrator, store, _sandbox, model, approvals, commit = (
                self.make_orchestrator(tmp, model=model, checkpoint=checkpoint)
            )
            result = orchestrator.run()
            loaded = store.load("run-1")

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "repair_attempt_budget_exhausted")
        self.assertEqual(model.patch_attempts, [])
        self.assertEqual(model.plan_calls, [])
        self.assertEqual(approvals.write_requests, [])
        self.assertEqual(commit.calls, [])
        self.assertEqual(loaded.state, RepairState.FAILED)
        self.assertIsNone(loaded.in_progress_operation)
        self.assertEqual(loaded.budget["usage"]["repair_attempts"], 2)

    def test_failure_intent_survives_interrupted_multi_manifest_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "task"
            worktree.mkdir()
            first_patch = patch_for(1)
            second_patch = patch_for(2)
            first = PatchManifest(
                manifest_id="manifest-first",
                run_id="run-1",
                state=ManifestState.APPLIED,
                patch=parse_patch(first_patch),
                before_snapshot_hash=snapshot_hash(),
                after_snapshot_hash=snapshot_hash((first_patch,)),
                approval_receipt="approval-first",
                rollback_token="rollback-first",
            )
            second = PatchManifest(
                manifest_id="manifest-second",
                run_id="run-1",
                state=ManifestState.APPLIED,
                patch=parse_patch(second_patch),
                before_snapshot_hash=snapshot_hash((first_patch,)),
                after_snapshot_hash=snapshot_hash((first_patch, second_patch)),
                approval_receipt="approval-second",
                rollback_token="rollback-second",
            )
            checkpoint = self.make_checkpoint(
                worktree,
                state=RepairState.FAILED,
                state_history=(
                    RepairState.DISCOVER,
                    RepairState.PLAN,
                    RepairState.PATCH,
                    RepairState.FAILED,
                ),
                sequence=7,
                diff_hash=second.after_snapshot_hash,
                in_progress_operation={
                    "kind": "failure",
                    "reason": "repair_attempt_budget_exhausted",
                    "cleanup_status": "pending",
                },
                tool_ledger=[
                    {
                        "kind": "patch_manifest",
                        "manifest_id": first.manifest_id,
                        "manifest": first.to_dict(),
                    },
                    {
                        "kind": "patch_manifest",
                        "manifest_id": second.manifest_id,
                        "manifest": second.to_dict(),
                    },
                ],
            )
            sandbox = InterruptFirstRollbackSandbox((first_patch, second_patch))
            orchestrator, store, sandbox, _model, _approvals, _commit = (
                self.make_orchestrator(
                    tmp,
                    sandbox=sandbox,
                    checkpoint=checkpoint,
                )
            )
            with self.assertRaises(KeyboardInterrupt):
                orchestrator.run()
            interrupted = store.load("run-1")
            self.assertEqual(interrupted.in_progress_operation["kind"], "failure")

            resumed = RepairOrchestrator(
                checkpoint=interrupted,
                store=store,
                sandbox=sandbox,
                model=FakeModel(),
                approvals=FakeApprovals(now=100),
                commit_control=FakeCommitControl(sandbox),
                clock=lambda: 100.0,
            ).run()
            loaded = store.load("run-1")

        self.assertEqual(resumed.state, RepairState.FAILED)
        self.assertEqual(resumed.reason, "repair_attempt_budget_exhausted")
        self.assertEqual(sandbox.patches, [])
        self.assertIsNone(loaded.in_progress_operation)
        manifests = [
            item["manifest"]
            for item in loaded.tool_ledger
            if item.get("kind") == "patch_manifest"
        ]
        self.assertTrue(all(item["state"] == "rolled_back" for item in manifests))

    def test_preflighted_repaired_patch_requires_a_fresh_human_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = RejectFirstPatchSandbox()
            approvals = FakeApprovals(reject_write_at=1)
            orchestrator, store, sandbox, model, approvals, commit = (
                self.make_orchestrator(
                    tmp,
                    sandbox=sandbox,
                    approvals=approvals,
                )
            )
            result = orchestrator.run()
            loaded = store.load("run-1")

        self.assertEqual(result.state, RepairState.CANCELLED)
        self.assertEqual(result.reason, "write_approval_rejected")
        self.assertEqual([item[1] for item in model.patch_attempts], [1, 1])
        self.assertEqual(len(approvals.write_requests), 1)
        self.assertEqual(approvals.write_requests[0].patch_attempt, 1)
        self.assertEqual(loaded.approvals, [])
        self.assertEqual(sandbox.patches, [])
        self.assertEqual(commit.calls, [])

    def test_resume_preflight_is_locked_and_persists_downtime_before_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint = self.make_checkpoint(root / "task")
            checkpoint.updated_at = 90.0
            calls = []
            orchestrator, store, _sandbox, _model, _approvals, _commit = (
                self.make_orchestrator(
                    tmp,
                    checkpoint=checkpoint,
                    preflight=lambda: calls.append("checked"),
                )
            )

            result = orchestrator.run()
            loaded = store.load("run-1")
            event_kinds = [item["kind"] for item in store.events("run-1")]

        self.assertEqual(result.state, RepairState.SUBMIT)
        self.assertEqual(calls, ["checked"])
        self.assertEqual(loaded.budget["usage"]["elapsed_seconds"], 10.0)
        self.assertIn("resume_preflight_completed", event_kinds)

    def test_stale_checkpoint_is_rejected_after_the_run_lock_is_acquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, store, _sandbox, _model, _approvals, _commit = (
                self.make_orchestrator(tmp)
            )
            newer = store.load("run-1")
            newer.sequence += 1
            store.save(newer)

            with self.assertRaisesRegex(WorktreePolicyError, "changed before"):
                orchestrator.run()

    def test_write_or_commit_rejection_never_calls_the_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            approvals = FakeApprovals(reject_write=True)
            orchestrator, _store, sandbox, _model, _approvals, commit = self.make_orchestrator(
                tmp, approvals=approvals
            )
            result = orchestrator.run()
            self.assertEqual(result.state, RepairState.CANCELLED)
            self.assertEqual(sandbox.patches, [])
            self.assertEqual(commit.calls, [])

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = StatefulSandbox()
            approvals = FakeApprovals(reject_commit=True)
            commit = FakeCommitControl(sandbox)
            orchestrator, _store, sandbox, _model, _approvals, commit = self.make_orchestrator(
                tmp, sandbox=sandbox, approvals=approvals, commit_control=commit
            )
            result = orchestrator.run()
            self.assertEqual(result.state, RepairState.CANCELLED)
            self.assertEqual(len(sandbox.patches), 1)
            self.assertEqual(commit.calls, [])

    def test_commit_failure_consumes_approval_and_returns_to_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = StatefulSandbox()
            commit = FakeCommitControl(sandbox, fail=True)
            orchestrator, store, _sandbox, _model, _approvals, _commit = self.make_orchestrator(
                tmp, sandbox=sandbox, commit_control=commit
            )
            result = orchestrator.run()
            loaded = store.load("run-1")
        self.assertEqual(result.state, RepairState.WAIT_APPROVAL)
        self.assertEqual(len(commit.calls), 1)
        self.assertIsNotNone(loaded.approvals[-1]["consumed_at"])
        self.assertEqual(loaded.state, RepairState.WAIT_APPROVAL)
        failure = next(
            item for item in loaded.tool_ledger if item.get("kind") == "commit_failure"
        )
        self.assertEqual(failure["error"], "simulated commit failure")
        self.assertEqual(failure["expected_tree_oid"], APPROVED_TREE_OID)

    def test_normal_submit_rejects_same_parent_and_message_with_another_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = StatefulSandbox()
            commit = FakeCommitControl(sandbox, committed_tree="e" * 40)
            orchestrator, _store, _sandbox, _model, _approvals, _commit = (
                self.make_orchestrator(
                    tmp, sandbox=sandbox, commit_control=commit
                )
            )
            result = orchestrator.run()

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "commit_result_cannot_be_verified")

    def test_submit_recovery_rejects_same_parent_and_message_with_another_tree(self):
        class CrashAfterCommit(FakeCommitControl):
            def commit(self, message, **kwargs):
                super().commit(message, **kwargs)
                raise RuntimeError("simulated process interruption")

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = StatefulSandbox()
            commit = CrashAfterCommit(sandbox)
            orchestrator, store, _sandbox, _model, approvals, _commit = (
                self.make_orchestrator(
                    tmp, sandbox=sandbox, commit_control=commit
                )
            )
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                orchestrator.run()
            checkpoint = store.load("run-1")
            self.assertEqual(
                checkpoint.in_progress_operation["expected_tree_oid"],
                APPROVED_TREE_OID,
            )
            commit.tree_oid = "e" * 40
            resumed = RepairOrchestrator(
                checkpoint=checkpoint,
                store=store,
                sandbox=sandbox,
                model=FakeModel(),
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            )
            result = resumed.run()

        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "ambiguous_commit_state")

    def test_two_repairs_are_bounded_and_persistent_failure_rolls_everything_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = StatefulSandbox(test_exit_codes=(1, 1, 1))
            orchestrator, store, sandbox, model, approvals, commit = self.make_orchestrator(
                tmp, sandbox=sandbox
            )
            result = orchestrator.run()
            loaded = store.load("run-1")
        self.assertEqual(result.state, RepairState.FAILED)
        self.assertEqual(result.reason, "repair_attempt_budget_exhausted")
        self.assertEqual(sandbox.patches, [])
        self.assertEqual(commit.calls, [])
        self.assertEqual(len(approvals.write_requests), 3)
        self.assertEqual(len(model.plan_calls), 3)
        self.assertEqual(loaded.budget["usage"]["repair_attempts"], 2)
        self.assertEqual(loaded.budget["usage"]["tokens"], 90)
        manifests = [
            entry["manifest"] for entry in loaded.tool_ledger
            if entry.get("kind") == "patch_manifest"
        ]
        self.assertTrue(manifests)
        self.assertTrue(all(item["state"] == "rolled_back" for item in manifests))

    def test_interrupted_applied_patch_is_reconciled_without_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "task"
            worktree.mkdir()
            sandbox = StatefulSandbox()
            sandbox.patches.append(patch_for(1))
            plan = FakeModel().make_plan("issue", previous_plan=None, evidence={}).value
            document = parse_patch(patch_for(1))
            manifest = PatchManifest(
                manifest_id="manifest-1",
                run_id="run-1",
                state=ManifestState.INTENT,
                patch=document,
                before_snapshot_hash=snapshot_hash(),
                approval_receipt="approval-receipt",
                rollback_token="rollback-secret",
            )
            checkpoint = self.make_checkpoint(
                worktree,
                state=RepairState.PATCH,
                state_history=(RepairState.DISCOVER, RepairState.PLAN, RepairState.PATCH),
                sequence=7,
                plan=plan.to_dict(),
                plan_hash=plan.sha256,
                diff_hash=snapshot_hash(),
                in_progress_operation={"kind": "patch_manifest", "manifest": manifest.to_dict()},
                tool_ledger=[
                    {
                        "kind": "patch_manifest",
                        "manifest_id": manifest.manifest_id,
                        "manifest": manifest.to_dict(),
                    }
                ],
            )
            model = FakeModel()
            approvals = FakeApprovals(now=100)
            commit = FakeCommitControl(sandbox)
            orchestrator, _store, sandbox, model, approvals, commit = self.make_orchestrator(
                tmp,
                sandbox=sandbox,
                model=model,
                approvals=approvals,
                commit_control=commit,
                checkpoint=checkpoint,
            )
            result = orchestrator.run()
        self.assertEqual(result.state, RepairState.SUBMIT)
        self.assertEqual(len(sandbox.patches), 1)
        self.assertEqual(model.patch_attempts, [])
        self.assertEqual(len(approvals.write_requests), 0)
        self.assertEqual(len(commit.calls), 1)

    def test_interrupted_rejected_patch_schedules_one_metered_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "task"
            worktree.mkdir()
            sandbox = StatefulSandbox()
            plan = FakeModel().make_plan("issue", previous_plan=None, evidence={}).value
            document = parse_patch(patch_for(1))
            manifest = PatchManifest(
                manifest_id="manifest-rejected",
                run_id="run-1",
                state=ManifestState.REJECTED,
                patch=document,
                before_snapshot_hash=snapshot_hash(),
                approval_receipt="approval-receipt",
                rollback_token="rollback-secret",
                preflight_operation_id="op-rejected",
            )
            checkpoint = self.make_checkpoint(
                worktree,
                state=RepairState.PATCH,
                state_history=(RepairState.DISCOVER, RepairState.PLAN, RepairState.PATCH),
                sequence=7,
                plan=plan.to_dict(),
                plan_hash=plan.sha256,
                diff_hash=snapshot_hash(),
                in_progress_operation={
                    "kind": "patch_manifest",
                    "manifest": manifest.to_dict(),
                },
                tool_ledger=[
                    {
                        "kind": "llm_call",
                        "operation": "patch",
                        "reservation_id": "rejected-call",
                        "status": "completed",
                        "result": {
                            "kind": "patch_output",
                            "text": document.text,
                            "sha256": document.sha256,
                        },
                    },
                    {
                        "kind": "patch_manifest",
                        "manifest_id": manifest.manifest_id,
                        "manifest": manifest.to_dict(),
                    },
                ],
            )
            model = FakeModel()
            approvals = FakeApprovals(now=100)
            commit = FakeCommitControl(sandbox)
            orchestrator, store, sandbox, model, approvals, commit = (
                self.make_orchestrator(
                    tmp,
                    sandbox=sandbox,
                    model=model,
                    approvals=approvals,
                    commit_control=commit,
                    checkpoint=checkpoint,
                )
            )
            result = orchestrator.run()
            loaded = store.load("run-1")

        self.assertEqual(result.state, RepairState.SUBMIT)
        self.assertEqual([item[1] for item in model.patch_attempts], [1])
        self.assertEqual(len(approvals.write_requests), 1)
        self.assertEqual(approvals.write_requests[0].patch_attempt, 1)
        self.assertEqual(loaded.budget["usage"]["repair_attempts"], 0)
        self.assertEqual(
            model.patch_attempts[0][2]["patch_rejections"][0]["manifest_id"],
            "manifest-rejected",
        )
        self.assertEqual(
            model.patch_attempts[0][2]["patch_rejections"][0][
                "preflight_operation_id"
            ],
            "op-rejected",
        )
        old_call = next(
            item
            for item in loaded.tool_ledger
            if item.get("reservation_id") == "rejected-call"
        )
        self.assertEqual(old_call["status"], "consumed")
        self.assertEqual(len(sandbox.patches), 1)
        self.assertEqual(len(commit.calls), 1)

    def test_completed_run_resume_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator, store, sandbox, _model, _approvals, commit = self.make_orchestrator(tmp)
            first = orchestrator.run()
            checkpoint = store.load("run-1")
            approvals = FakeApprovals(now=100)
            resumed = RepairOrchestrator(
                checkpoint=checkpoint,
                store=store,
                sandbox=sandbox,
                model=FakeModel(),
                approvals=approvals,
                commit_control=commit,
                clock=lambda: 100.0,
            ).run()
        self.assertEqual(first.commit_sha, COMMIT_SHA)
        self.assertEqual(resumed.reason, "already_completed")
        self.assertEqual(commit.calls, ["fix: resolve issue 1"])
        self.assertEqual(approvals.write_requests, [])
        self.assertEqual(approvals.commit_requests, [])

    def test_resume_rejects_raised_budget_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "task"
            worktree.mkdir()
            budget = BudgetManager(BudgetLimits(total_tokens=90_000)).to_dict()
            checkpoint = self.make_checkpoint(worktree, budget=budget)
            with self.assertRaisesRegex(WorktreePolicyError, "budgets"):
                self.make_orchestrator(tmp, checkpoint=checkpoint)


if __name__ == "__main__":
    unittest.main()
