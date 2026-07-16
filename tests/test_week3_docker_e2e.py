"""Opt-in real-Docker end-to-end coverage for the repair orchestrator."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from code_review_agent.repair import (
    DockerWorktreeBackend,
    ModelCallLimits,
    ModelCallResult,
    Reflection,
    ReflectionDecision,
    RepairOrchestrator,
    RepairPlan,
    RepairWorktreeManager,
    SandboxedGitCommitControl,
)
from code_review_agent.repair_approval import (
    ApprovalKind,
    issue_commit_approval,
    issue_write_approval,
)
from code_review_agent.repair_budget import BudgetLimits, BudgetManager
from code_review_agent.repair_checkpoint import CheckpointStore, RepairCheckpoint
from code_review_agent.repair_state import RepairState
from code_review_agent.repair_tools import build_commit_sandbox, build_repair_sandbox
from code_review_agent.sandbox import CommandPolicy, DockerSandboxRunner, SandboxTimeout


IMAGE_ENV = "CRAG_REPAIR_DOCKER_IMAGE"
RUN_ENV = "CRAG_RUN_DOCKER_E2E"
DEFAULT_IMAGE = "code-review-agent-repair:week3"
RUN_ID = "docker-e2e"
ISSUE_SLUG = "issue-1"
PATCH_TEXT = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1 +1 @@\n"
    "-VALUE = 1\n"
    "+VALUE = 2\n"
)
TEST_COMMAND = (
    "python",
    "-m",
    "unittest",
    "-v",
    "test_app.py",
)


class FakeModel:
    """Deterministic model boundary; every repository operation remains real."""

    def __init__(self) -> None:
        self.plan_calls = 0
        self.patch_calls = 0
        self.reflection_calls = 0

    def limits_for(self, operation: str) -> ModelCallLimits:
        if operation not in {"plan", "patch", "reflect"}:
            raise AssertionError(f"unexpected model operation: {operation}")
        return ModelCallLimits(max_tokens=32, max_cost_usd=0.0)

    def make_plan(self, issue_ref, *, previous_plan, evidence):
        self.plan_calls += 1
        if previous_plan is not None:
            raise AssertionError("the passing E2E should not revise its plan")
        approved_sources = evidence["approved_sources_at_base"]
        if approved_sources != {"app.py": "VALUE = 1\n"}:
            raise AssertionError(
                f"the model did not receive the approved base source: {approved_sources!r}"
            )
        return ModelCallResult(
            RepairPlan(
                summary="change the fixture value",
                writable_paths=("app.py",),
                test_commands=(TEST_COMMAND,),
                risks=("fixture-only one-line change",),
                rollback_boundary="app.py",
                commit_message="fix: update fixture value",
            ),
            actual_tokens=3,
            actual_cost_usd=0.0,
        )

    def make_patch(self, plan, *, patch_attempt, evidence):
        self.patch_calls += 1
        if patch_attempt != 1 or evidence["current_diff"]:
            raise AssertionError("the first patch attempt did not start from a clean diff")
        return ModelCallResult(PATCH_TEXT, actual_tokens=3, actual_cost_usd=0.0)

    def reflect(self, plan, *, patch_attempt, test_results):
        self.reflection_calls += 1
        passed = bool(test_results) and all(result.exit_code == 0 for result in test_results)
        return ModelCallResult(
            Reflection(
                ReflectionDecision.SUCCESS if passed else ReflectionDecision.FAIL,
                "real Docker test passed" if passed else "real Docker test failed",
            ),
            actual_tokens=3,
            actual_cost_usd=0.0,
        )


class RecordingApprovals:
    """Explicitly issue both human-gate records while retaining their evidence."""

    def __init__(self) -> None:
        self.write_requests = []
        self.commit_requests = []

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
            now=time.time(),
            nonce="docker-write-approval",
        )

    def request_commit(self, request):
        self.commit_requests.append(request)
        return issue_commit_approval(
            run_id=request.run_id,
            checkpoint_id=request.checkpoint_id,
            base_sha=request.base_sha,
            diff_hash=request.diff_hash,
            test_result_hash=request.test_result_hash,
            commit_message=request.commit_message,
            expected_tree_oid=request.expected_tree_oid,
            ttl_seconds=600,
            now=time.time(),
            nonce="docker-commit-approval",
        )


@unittest.skipUnless(
    os.environ.get(RUN_ENV) == "1",
    f"set {RUN_ENV}=1 to run the real Docker repair E2E",
)
class TestDockerRepairE2E(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.docker = shutil.which("docker")
        if cls.docker is None:
            raise AssertionError("Docker CLI is required when the Docker E2E is enabled")
        cls.image = os.environ.get(IMAGE_ENV, DEFAULT_IMAGE)
        inspected = subprocess.run(
            (cls.docker, "image", "inspect", cls.image),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if inspected.returncode != 0:
            raise AssertionError(
                f"Docker image {cls.image!r} is unavailable; build Dockerfile.repair first: "
                f"{inspected.stderr.strip()}"
            )

    def _docker_git(self, checkout: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        command = (
            self.docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=bind,source={checkout.resolve()},target=/workspace",
            "--env",
            "GIT_CONFIG_GLOBAL=/dev/null",
            "--env",
            "GIT_CONFIG_NOSYSTEM=1",
            "--env",
            "HOME=/tmp",
            "--workdir",
            "/workspace",
            "--entrypoint",
            "git",
            self.image,
            "-c",
            "safe.directory=/workspace",
            "-c",
            "core.hooksPath=/dev/null",
            *arguments,
        )
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            self.fail(
                f"Docker Git failed ({' '.join(arguments)}):\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def _initialize_repository(self, checkout: Path) -> str:
        checkout.mkdir()
        self._docker_git(checkout, "init", "--initial-branch=master", ".")
        (checkout / "app.py").write_text(
            "VALUE = 1\n", encoding="utf-8", newline="\n"
        )
        (checkout / "test_app.py").write_text(
            "import unittest\n\n"
            "import app\n\n\n"
            "class TestValue(unittest.TestCase):\n"
            "    def test_repaired_value(self):\n"
            "        self.assertEqual(app.VALUE, 2)\n",
            encoding="utf-8",
        )
        try:
            (checkout / "app.py").chmod(0o666)
        except OSError:
            pass
        self._docker_git(checkout, "config", "user.name", "Repair E2E")
        self._docker_git(checkout, "config", "user.email", "repair-e2e@example.invalid")
        self._docker_git(checkout, "add", "--", "app.py", "test_app.py")
        self._docker_git(checkout, "commit", "--no-gpg-sign", "--message", "base")
        return self._docker_git(checkout, "rev-parse", "HEAD").stdout.strip().lower()

    def _sandbox(self, worktree: Path, base_sha: str):
        return build_repair_sandbox(
            worktree=worktree,
            image=self.image,
            base_sha=base_sha,
            writable_paths=("app.py",),
            test_commands=(TEST_COMMAND,),
        )

    def _commit_control(self, worktree: Path, budget: BudgetManager):
        def factory(allowed_commands):
            return build_commit_sandbox(
                worktree=worktree,
                image=self.image,
                allowed_commands=allowed_commands,
            )

        return SandboxedGitCommitControl(sandbox_factory=factory, budget=budget)

    def test_live_security_boundary_and_timeout_cleanup(self):
        slow_command = ("python", "-m", "pytest", "-q", "test_slow.py")
        commands = frozenset(
            {
                ("id", "-u"),
                ("git", "init", "workspace-created"),
                ("git", "init", "/escape"),
                (
                    "git",
                    "ls-remote",
                    "https://github.com/pallets/click.git",
                    "HEAD",
                ),
                slow_command,
            }
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            (root / "test_slow.py").write_text(
                "import time\n\n\ndef test_slow():\n    time.sleep(30)\n",
                encoding="utf-8",
            )
            runner = DockerSandboxRunner(
                worktree=root,
                image=self.image,
                policy=CommandPolicy(commands, max_seconds=30),
            )

            identity = runner.run(("id", "-u"), timeout_seconds=10)
            self.assertEqual(identity.exit_code, 0)
            self.assertEqual(identity.stdout.strip(), "65532")

            workspace_write = runner.run(
                ("git", "init", "workspace-created"), timeout_seconds=10
            )
            self.assertEqual(workspace_write.exit_code, 0)
            self.assertTrue((root / "workspace-created" / ".git").is_dir())

            root_write = runner.run(("git", "init", "/escape"), timeout_seconds=10)
            self.assertNotEqual(root_write.exit_code, 0)
            self.assertIn("read-only file system", root_write.stderr.casefold())

            network = runner.run(
                (
                    "git",
                    "ls-remote",
                    "https://github.com/pallets/click.git",
                    "HEAD",
                ),
                timeout_seconds=15,
            )
            self.assertNotEqual(network.exit_code, 0)

            with self.assertRaises(SandboxTimeout) as caught:
                runner.run(slow_command, timeout_seconds=1)
            container_name = f"crag-repair-{caught.exception.operation_id}"
            inspected = subprocess.run(
                (self.docker, "container", "inspect", container_name),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(
                inspected.returncode,
                0,
                f"timed-out container still exists: {container_name}",
            )

    def test_real_docker_repair_commit_checkpoint_and_completed_resume(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            original = root / "original"
            worktree_root = root / "repair-worktrees"
            state_root = root / "repair-state"
            worktree_root.mkdir()
            base_sha = self._initialize_repository(original)
            limits = BudgetLimits()
            budget = BudgetManager(limits)
            backend = DockerWorktreeBackend(
                worktree_root=worktree_root,
                image=self.image,
                budget=budget,
            )
            task = RepairWorktreeManager(
                original_checkout=original,
                worktree_root=worktree_root,
                backend=backend,
            ).create(issue_slug=ISSUE_SLUG, run_id=RUN_ID, base_sha=base_sha)

            self.assertNotEqual(task.path, original)
            self.assertEqual(task.original_snapshot.branch, "master")
            self.assertEqual(task.original_snapshot.head, base_sha)
            self.assertTrue(task.original_snapshot.clean)
            checkpoint = RepairCheckpoint(
                run_id=RUN_ID,
                repository_id="fixture/docker-e2e",
                base_sha=base_sha,
                task_branch=task.branch,
                worktree=str(task.path),
                issue_ref="https://github.com/example/fixture/issues/1",
                original_snapshot={
                    "branch": task.original_snapshot.branch,
                    "head": task.original_snapshot.head,
                    "staged": list(task.original_snapshot.staged),
                    "tracked": list(task.original_snapshot.tracked),
                    "untracked": list(task.original_snapshot.untracked),
                },
                writable_paths=("app.py",),
                budget=budget.to_dict(),
                updated_at=time.time(),
            )
            store = CheckpointStore(state_root)
            store.save(checkpoint)
            model = FakeModel()
            approvals = RecordingApprovals()
            result = RepairOrchestrator(
                checkpoint=checkpoint,
                store=store,
                sandbox=self._sandbox(task.path, base_sha),
                model=model,
                approvals=approvals,
                commit_control=self._commit_control(task.path, budget),
                expected_limits=limits,
                budget_manager=budget,
            ).run()

            self.assertEqual(result.state, RepairState.SUBMIT)
            self.assertRegex(result.commit_sha, r"^[0-9a-f]{40}$")
            self.assertNotEqual(result.commit_sha, base_sha)
            self.assertEqual(model.plan_calls, 1)
            self.assertEqual(model.patch_calls, 1)
            self.assertEqual(model.reflection_calls, 1)
            self.assertEqual(len(approvals.write_requests), 1)
            self.assertEqual(len(approvals.commit_requests), 1)
            self.assertEqual(
                approvals.write_requests[0].patch_hash,
                hashlib.sha256(PATCH_TEXT.encode("utf-8")).hexdigest(),
            )
            self.assertRegex(
                approvals.commit_requests[0].expected_tree_oid,
                r"^[0-9a-f]{40}$",
            )

            durable = store.load(RUN_ID)
            self.assertEqual(durable.state, RepairState.SUBMIT)
            self.assertEqual(
                durable.state_history,
                (
                    RepairState.DISCOVER,
                    RepairState.PLAN,
                    RepairState.PATCH,
                    RepairState.TEST,
                    RepairState.REFLECT,
                    RepairState.WAIT_APPROVAL,
                    RepairState.SUBMIT,
                ),
            )
            self.assertEqual([item["binding"]["kind"] for item in durable.approvals],
                             [ApprovalKind.WRITE.value, ApprovalKind.COMMIT.value])
            self.assertTrue(all(item["consumed_at"] is not None for item in durable.approvals))
            self.assertEqual(durable.test_results[-1]["results"][0]["exit_code"], 0)
            self.assertTrue(
                any(item.get("kind") == "patch_manifest" for item in durable.tool_ledger)
            )
            completed = [
                item for item in durable.tool_ledger if item.get("kind") == "commit_completed"
            ]
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["commit_sha"], result.commit_sha)
            self.assertEqual(
                completed[0]["tree_oid"],
                approvals.commit_requests[0].expected_tree_oid,
            )
            self.assertEqual((original / "app.py").read_text(encoding="utf-8"), "VALUE = 1\n")
            self.assertEqual((task.path / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n")

            verification_backend = DockerWorktreeBackend(
                worktree_root=worktree_root,
                image=self.image,
                budget=BudgetManager(BudgetLimits(tool_calls=1_000)),
            )
            self.assertEqual(
                verification_backend.snapshot(original), task.original_snapshot
            )

            resumed_checkpoint = store.load(RUN_ID)
            resumed_budget = BudgetManager.from_dict(resumed_checkpoint.budget)
            resumed_approvals = RecordingApprovals()
            resumed = RepairOrchestrator(
                checkpoint=resumed_checkpoint,
                store=store,
                sandbox=self._sandbox(task.path, base_sha),
                model=FakeModel(),
                approvals=resumed_approvals,
                commit_control=self._commit_control(task.path, resumed_budget),
                expected_limits=limits,
                budget_manager=resumed_budget,
                preflight=lambda: self.assertEqual(
                    verification_backend.snapshot(original), task.original_snapshot
                ),
            ).run()

            self.assertEqual(resumed.state, RepairState.SUBMIT)
            self.assertEqual(resumed.reason, "already_completed")
            self.assertEqual(resumed.commit_sha, result.commit_sha)
            self.assertEqual(resumed_approvals.write_requests, [])
            self.assertEqual(resumed_approvals.commit_requests, [])
            self.assertEqual(
                verification_backend.snapshot(original), task.original_snapshot
            )
            self.assertEqual(
                self._docker_git(
                    original,
                    "rev-list",
                    "--count",
                    base_sha + ".." + task.branch,
                )
                .stdout.strip(),
                "1",
            )


if __name__ == "__main__":
    unittest.main()
