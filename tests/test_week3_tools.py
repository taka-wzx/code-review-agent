"""Offline safety tests for the Week 3 sandbox and worktree lifecycle."""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from code_review_agent.repair import (
    DockerWorktreeBackend,
    OriginalCheckoutChanged,
    RepairWorktreeManager,
    RepositorySnapshot,
    WorktreePolicyError,
    WorktreeProvisionError,
)
from code_review_agent.repair_approval import ApprovalMismatch, issue_write_approval
from code_review_agent.repair_budget import BudgetManager
from code_review_agent.repair_tools import (
    APPLY_CHECK_COMMAND,
    APPLY_COMMAND,
    GIT_PREFIX,
    REVERSE_CHECK_COMMAND,
    REVERSE_COMMAND,
    DiffScope,
    GitToolError,
    ManifestState,
    PatchDocument,
    PatchManifest,
    PatchRejected,
    PatchScopeError,
    RepairTools,
    SnapshotMismatch,
    ToolPersistenceError,
    ToolQuarantined,
    build_repair_sandbox,
    build_commit_sandbox,
    parse_patch,
    parse_porcelain_v1_z,
)
from code_review_agent.sandbox import (
    BoundedProcessExecutor,
    CommandPolicy,
    DockerSandboxRunner,
    HostProcessResult,
    SandboxCleanupError,
    SandboxPolicyError,
    SandboxResult,
    SandboxTimeout,
    SandboxUnavailable,
    WritableMount,
)


BASE_SHA = "a" * 40
PATCH_TEXT = (
    "diff --git a/app.py b/app.py\n"
    "index 78981922613b2afb6025042ff6bd878ac1994e85..61780798228d17af2d34fce4cfbdf35556832472 100644\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


class FakeExecutor:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def execute(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if not self.outcomes:
            raise AssertionError("unexpected process execution")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(argv, kwargs)
        return outcome


def process_result(
    returncode=0,
    *,
    stdout="ok",
    stderr="",
    timed_out=False,
    truncated=False,
):
    return HostProcessResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.25,
        timed_out=timed_out,
        output_truncated=truncated,
    )


class TestDockerSandbox(unittest.TestCase):
    def test_bounded_process_executor_captures_stdin_and_caps_combined_output(self):
        executor = BoundedProcessExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            result = executor.execute(
                (
                    sys.executable,
                    "-c",
                    "import sys; data=sys.stdin.buffer.read(); "
                    "sys.stdout.buffer.write(data + b'x' * 4096)",
                ),
                cwd=Path(tmp),
                env={},
                timeout_seconds=5.0,
                output_limit_bytes=64,
                stdin_bytes=b"approved-input",
            )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertTrue(result.output_truncated)
        self.assertLessEqual(len((result.stdout + result.stderr).encode()), 64)
        self.assertIn("approved-input", result.stdout)

    def test_bounded_process_executor_kills_a_timed_out_process(self):
        executor = BoundedProcessExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            result = executor.execute(
                (sys.executable, "-c", "import time; time.sleep(10)"),
                cwd=Path(tmp),
                env={},
                timeout_seconds=0.05,
                output_limit_bytes=128,
                stdin_bytes=None,
            )
        self.assertIsNone(result.returncode)
        self.assertTrue(result.timed_out)

    def test_bounded_process_executor_reaps_cli_on_keyboard_interrupt(self):
        class InterruptedProcess:
            def __init__(self):
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()
                self.stdin = None
                self.returncode = None
                self.wait_calls = 0
                self.kill_calls = 0

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise KeyboardInterrupt
                self.returncode = -9
                return self.returncode

            def poll(self):
                return self.returncode

            def kill(self):
                self.kill_calls += 1
                self.returncode = -9

        process = InterruptedProcess()
        executor = BoundedProcessExecutor()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "code_review_agent.sandbox.subprocess.Popen", return_value=process
        ):
            with self.assertRaises(KeyboardInterrupt):
                executor.execute(
                    ("docker", "run", "image"),
                    cwd=Path(tmp),
                    env={},
                    timeout_seconds=5,
                    output_limit_bytes=128,
                    stdin_bytes=None,
                )
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_calls, 2)

    def make_runner(self, root, executor, *, commands=None):
        docker = Path(os.path.abspath(Path(root) / "docker.exe"))
        policy = CommandPolicy(
            frozenset(commands or {("python", "-m", "unittest")}),
            max_seconds=20,
            max_output_bytes=1234,
        )
        return DockerSandboxRunner(
            worktree=Path(root),
            image="crag-repair:test",
            policy=policy,
            docker_path=docker,
            executor=executor,
        )

    def test_hardened_docker_invocation_and_scrubbed_environment(self):
        executor = FakeExecutor(
            process_result(stdout="24.0"),
            process_result(stdout="tests passed", truncated=True),
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "secret", "SSH_AUTH_SOCK": "secret-sock"},
            clear=False,
        ):
            runner = self.make_runner(tmp, executor)
            result = runner.run(("python", "-m", "unittest"), timeout_seconds=10)

        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.output_truncated)
        probe, command = executor.calls
        self.assertEqual(probe[0][1:3], ("version", "--format"))
        docker_argv = command[0]
        for expected in (
            ("--network", "none"),
            ("--user", "65532:65532"),
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges"),
            ("--workdir", "/workspace"),
            ("--entrypoint", "python"),
        ):
            self.assertIn(expected, tuple(zip(docker_argv, docker_argv[1:])))
        self.assertIn("--read-only", docker_argv)
        self.assertIn("/tmp:rw,noexec,nosuid,size=64m", docker_argv)
        self.assertIn("--pull", docker_argv)
        mounts = [docker_argv[index + 1] for index, item in enumerate(docker_argv) if item == "--mount"]
        self.assertEqual(len(mounts), 1)
        self.assertIn("target=/workspace", mounts[0])
        self.assertNotIn("DEEPSEEK_API_KEY", command[1]["env"])
        self.assertNotIn("SSH_AUTH_SOCK", command[1]["env"])
        self.assertEqual(command[1]["cwd"], Path(tmp).resolve())
        self.assertEqual(command[1]["output_limit_bytes"], 1234)

    def test_unlisted_command_and_excessive_timeout_are_rejected_before_docker(self):
        executor = FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.make_runner(tmp, executor)
            with self.assertRaises(SandboxPolicyError):
                runner.run(("sh", "-c", "rm -rf /"))
            with self.assertRaises(SandboxPolicyError):
                runner.run(("python", "-m", "unittest"), timeout_seconds=21)
            self.assertEqual(executor.calls, [])

    def test_control_plane_write_mount_is_explicit_and_target_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "task"
            metadata = root / "metadata"
            worktree.mkdir()
            metadata.mkdir()
            executor = FakeExecutor(process_result(stdout="24.0"), process_result())
            runner = DockerSandboxRunner(
                worktree=worktree,
                image="repair:test",
                policy=CommandPolicy(frozenset({("git", "status")})),
                docker_path=Path(os.path.abspath(root / "docker.exe")),
                executor=executor,
                writable_mounts=(WritableMount(metadata, "/git-control"),),
            )
            runner.run(("git", "status"))
            mounts = [
                executor.calls[1][0][index + 1]
                for index, item in enumerate(executor.calls[1][0])
                if item == "--mount"
            ]
            self.assertTrue(mounts[-1].endswith("target=/git-control"))
            self.assertNotIn("readonly", mounts[-1])

    def test_shells_and_inline_interpreter_snippets_cannot_be_allowlisted(self):
        for command in (
            ("sh", "-c", "echo unsafe"),
            ("pwsh", "-Command", "Write-Output unsafe"),
            ("python", "-c", "print('unsafe')"),
            ("node", "--eval", "process.exit()"),
        ):
            with self.subTest(command=command), self.assertRaises(SandboxPolicyError):
                CommandPolicy(frozenset({command}))

    def test_missing_or_failed_docker_probe_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "code_review_agent.sandbox.shutil.which", return_value=None
        ):
            policy = CommandPolicy(frozenset({("python", "-V")}))
            runner = DockerSandboxRunner(
                worktree=Path(tmp), image="repair:test", policy=policy
            )
            with self.assertRaisesRegex(SandboxUnavailable, "not installed"):
                runner.run(("python", "-V"))

        executor = FakeExecutor(process_result(returncode=1, stdout="", stderr="offline"))
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.make_runner(tmp, executor)
            with self.assertRaisesRegex(SandboxUnavailable, "offline"):
                runner.run(("python", "-m", "unittest"))

    def test_timeout_removes_named_container_before_failing(self):
        executor = FakeExecutor(
            process_result(stdout="24.0"),
            process_result(returncode=None, timed_out=True, stdout="partial"),
            process_result(stdout="removed"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.make_runner(tmp, executor)
            with self.assertRaises(SandboxTimeout) as caught:
                runner.run(("python", "-m", "unittest"))
        run_argv = executor.calls[1][0]
        cleanup_argv = executor.calls[2][0]
        container_name = run_argv[run_argv.index("--name") + 1]
        self.assertEqual(cleanup_argv[-3:], ("rm", "-f", container_name))
        self.assertEqual(caught.exception.stdout, "partial")

    def test_keyboard_interrupt_removes_named_container_before_propagating(self):
        def interrupt(_argv, _kwargs):
            raise KeyboardInterrupt

        executor = FakeExecutor(
            process_result(stdout="24.0"),
            interrupt,
            process_result(stdout="removed"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.make_runner(tmp, executor)
            with self.assertRaises(KeyboardInterrupt):
                runner.run(("python", "-m", "unittest"))
        run_argv = executor.calls[1][0]
        cleanup_argv = executor.calls[2][0]
        container_name = run_argv[run_argv.index("--name") + 1]
        self.assertEqual(cleanup_argv[-3:], ("rm", "-f", container_name))

    def test_unproven_timeout_cleanup_is_a_hard_failure(self):
        executor = FakeExecutor(
            process_result(stdout="24.0"),
            process_result(returncode=None, timed_out=True),
            process_result(returncode=1, stdout="", stderr="permission denied"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.make_runner(tmp, executor)
            with self.assertRaises(SandboxCleanupError):
                runner.run(("python", "-m", "unittest"))

    def test_ambiguous_mount_and_image_references_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="repair,mount-") as tmp:
            with self.assertRaises(SandboxPolicyError):
                self.make_runner(tmp, FakeExecutor())
        with tempfile.TemporaryDirectory() as tmp:
            policy = CommandPolicy(frozenset({("python", "-V")}))
            with self.assertRaises(SandboxPolicyError):
                DockerSandboxRunner(
                    worktree=Path(tmp),
                    image="--privileged",
                    policy=policy,
                    docker_path=Path(os.path.abspath(Path(tmp) / "docker.exe")),
                )


class FakeWorktreeBackend:
    def __init__(self, original, *, original_snapshot=None):
        self.original = Path(original).resolve()
        self.original_snapshot = original_snapshot or RepositorySnapshot("master", BASE_SHA)
        self.original_after = self.original_snapshot
        self.task_snapshots = {}
        self.existing_branches = set()
        self.has_commit = True
        self.create_error = None

    def snapshot(self, checkout):
        path = Path(checkout).resolve()
        if path == self.original:
            if self.task_snapshots:
                return self.original_after
            return self.original_snapshot
        return self.task_snapshots[path]

    def contains_commit(self, _checkout, _object_id):
        return self.has_commit

    def branch_exists(self, _checkout, branch):
        return branch in self.existing_branches

    def create_worktree(self, *, checkout, target, branch, base_sha):
        self.last_create = (checkout, target, branch, base_sha)
        Path(target).mkdir(parents=False)
        if self.create_error is not None:
            raise self.create_error
        self.task_snapshots[Path(target).resolve()] = RepositorySnapshot(branch, base_sha)


class TestRepairWorktreeLifecycle(unittest.TestCase):
    def test_docker_worktree_backend_uses_exact_containerized_git_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            repair_root = root / "repairs"
            (original / ".git").mkdir(parents=True)
            repair_root.mkdir()

            def create_worktree(_argv, _kwargs):
                target = repair_root / "issue-run"
                metadata = original / ".git" / "worktrees" / "issue-run"
                target.mkdir()
                metadata.mkdir(parents=True)
                (target / ".git").write_text(
                    "gitdir: /workspace/.git/worktrees/issue-run\n",
                    encoding="utf-8",
                )
                return process_result(returncode=0, stdout="created")

            executor = FakeExecutor(
                process_result(stdout="24.0"),
                process_result(stdout="master\n"),
                process_result(stdout=BASE_SHA + "\n"),
                process_result(stdout=" M app.py\x00?? new.py\x00"),
                process_result(stdout="24.0"),
                process_result(returncode=0, stdout=""),
                process_result(stdout="24.0"),
                process_result(returncode=1, stdout=""),
                process_result(stdout="24.0"),
                create_worktree,
            )
            budget = BudgetManager()
            backend = DockerWorktreeBackend(
                worktree_root=repair_root,
                image="repair:test",
                budget=budget,
                docker_path=Path(os.path.abspath(root / "docker.exe")),
                executor=executor,
            )
            snapshot = backend.snapshot(original)
            self.assertTrue(backend.contains_commit(original, BASE_SHA))
            self.assertFalse(backend.branch_exists(original, "repair/issue-run"))
            backend.create_worktree(
                checkout=original,
                target=repair_root / "issue-run",
                branch="repair/issue-run",
                base_sha=BASE_SHA,
            )
            rewritten_marker = (repair_root / "issue-run" / ".git").read_text(
                encoding="utf-8"
            )

        self.assertEqual(snapshot.branch, "master")
        self.assertEqual(snapshot.tracked, ("app.py",))
        self.assertEqual(snapshot.untracked, ("new.py",))
        create_argv = executor.calls[-1][0]
        self.assertIn("/repairs/issue-run", create_argv)
        self.assertIn("--network", create_argv)
        self.assertEqual(budget.usage.commands, 6)
        self.assertEqual(
            rewritten_marker,
            f"gitdir: {(original / '.git' / 'worktrees' / 'issue-run').as_posix()}\n",
        )

    def test_docker_worktree_marker_rewrite_rejects_metadata_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            repair_root = root / "repairs"
            (original / ".git" / "worktrees" / "issue-run").mkdir(parents=True)
            repair_root.mkdir()

            def create_escaped_worktree(_argv, _kwargs):
                target = repair_root / "issue-run"
                target.mkdir()
                (target / ".git").write_text(
                    "gitdir: /workspace/.git/worktrees/../../escape\n",
                    encoding="utf-8",
                )
                return process_result(returncode=0, stdout="created")

            executor = FakeExecutor(
                process_result(stdout="24.0"),
                create_escaped_worktree,
            )
            backend = DockerWorktreeBackend(
                worktree_root=repair_root,
                image="repair:test",
                budget=BudgetManager(),
                docker_path=Path(os.path.abspath(root / "docker.exe")),
                executor=executor,
            )

            with self.assertRaisesRegex(WorktreeProvisionError, "unsafe"):
                backend.create_worktree(
                    checkout=original,
                    target=repair_root / "issue-run",
                    branch="repair/issue-run",
                    base_sha=BASE_SHA,
                )

    def make_case(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        original = root / "original"
        original.mkdir()
        worktrees = root / "repair-runs"
        backend = FakeWorktreeBackend(original)
        manager = RepairWorktreeManager(
            original_checkout=original,
            worktree_root=worktrees,
            backend=backend,
        )
        return original, worktrees, backend, manager

    def test_unique_clean_worktree_from_exact_base_and_original_unchanged(self):
        original, worktrees, backend, manager = self.make_case()
        handle = manager.create(issue_slug="issue-42", run_id="run-abc", base_sha=BASE_SHA)
        self.assertEqual(handle.branch, "repair/issue-42-run-abc")
        self.assertEqual(handle.path, (worktrees / "issue-42-run-abc").resolve())
        self.assertEqual(handle.original_checkout, original.resolve())
        self.assertTrue(handle.original_snapshot.clean)
        handle.assert_original_unchanged(backend)
        self.assertEqual(backend.last_create[3], BASE_SHA)

    def test_backend_is_mandatory_and_worktree_root_cannot_be_in_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "repo"
            original.mkdir()
            with self.assertRaisesRegex(WorktreePolicyError, "backend is required"):
                RepairWorktreeManager(
                    original_checkout=original,
                    worktree_root=Path(tmp) / "runs",
                    backend=None,
                )
            with self.assertRaisesRegex(WorktreePolicyError, "outside"):
                RepairWorktreeManager(
                    original_checkout=original,
                    worktree_root=original / "runs",
                    backend=FakeWorktreeBackend(original),
                )
            self.assertFalse((original / "runs").exists())

    def test_invalid_identity_existing_branch_and_wrong_base_fail_before_creation(self):
        _original, _worktrees, backend, manager = self.make_case()
        for values in (
            {"issue_slug": "../escape", "run_id": "run-1", "base_sha": BASE_SHA},
            {"issue_slug": "issue", "run_id": "Run-1", "base_sha": BASE_SHA},
            {"issue_slug": "issue", "run_id": "run-1", "base_sha": "HEAD"},
        ):
            with self.subTest(values=values), self.assertRaises(WorktreePolicyError):
                manager.create(**values)
        backend.original_snapshot = RepositorySnapshot("master", "b" * 40)
        with self.assertRaisesRegex(WorktreePolicyError, "HEAD"):
            manager.create(issue_slug="issue", run_id="run-1", base_sha=BASE_SHA)

        backend.original_snapshot = RepositorySnapshot("master", BASE_SHA)
        backend.existing_branches.add("repair/issue-run-1")
        with self.assertRaisesRegex(WorktreePolicyError, "already exists"):
            manager.create(issue_slug="issue", run_id="run-1", base_sha=BASE_SHA)

    def test_partial_creation_is_quarantined_without_broad_cleanup(self):
        _original, worktrees, backend, manager = self.make_case()
        backend.create_error = RuntimeError("simulated interruption")
        with self.assertRaises(WorktreeProvisionError) as caught:
            manager.create(issue_slug="issue", run_id="run-1", base_sha=BASE_SHA)
        target = worktrees / "issue-run-1"
        self.assertEqual(caught.exception.quarantine_path, target)
        self.assertTrue(target.is_dir())

    def test_original_mutation_and_dirty_or_wrong_task_are_detected(self):
        _original, _worktrees, backend, manager = self.make_case()
        backend.original_after = RepositorySnapshot("master", BASE_SHA, tracked=("changed.py",))
        with self.assertRaises(OriginalCheckoutChanged):
            manager.create(issue_slug="issue", run_id="run-1", base_sha=BASE_SHA)

        _original, _worktrees, backend, manager = self.make_case()
        original_create = backend.create_worktree

        def create_dirty(**kwargs):
            original_create(**kwargs)
            target = Path(kwargs["target"]).resolve()
            backend.task_snapshots[target] = RepositorySnapshot(
                kwargs["branch"], kwargs["base_sha"], untracked=("leak.txt",)
            )

        backend.create_worktree = create_dirty
        with self.assertRaisesRegex(WorktreeProvisionError, "not clean"):
            manager.create(issue_slug="issue", run_id="run-2", base_sha=BASE_SHA)


class ScriptedSandbox:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def run(self, argv, **kwargs):
        command = tuple(argv)
        self.calls.append((command, kwargs))
        if not self.outcomes:
            raise AssertionError(f"unexpected sandbox command: {command}")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, SandboxResult):
            return outcome
        values = {"exit_code": 0, "stdout": "", "stderr": ""}
        values.update(outcome)
        return sandbox_result(command, **values)


def sandbox_result(
    argv,
    *,
    exit_code=0,
    stdout="",
    stderr="",
    truncated=False,
    operation_id=None,
):
    return SandboxResult(
        operation_id=operation_id or f"op-{hashlib.sha256(repr(argv).encode()).hexdigest()[:8]}",
        argv=tuple(argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        output_truncated=truncated,
    )


def repository_snapshot_hash(status_text, base_diff, untracked=()):
    entries = parse_porcelain_v1_z(status_text)
    payload = {
        "status": [
            {
                "index": entry.index_status,
                "worktree": entry.worktree_status,
                "path": entry.path,
                "original_path": entry.original_path,
            }
            for entry in entries
        ],
        "base_diff": base_diff,
        "untracked_diffs": list(untracked),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class TestGitParsingAndDockerContext(unittest.TestCase):
    def test_porcelain_parser_preserves_rename_and_rejects_unsafe_paths(self):
        entries = parse_porcelain_v1_z("R  new.py\x00old.py\x00?? fresh.py\x00")
        self.assertEqual(entries[0].path, "new.py")
        self.assertEqual(entries[0].original_path, "old.py")
        self.assertEqual(entries[1].path, "fresh.py")
        with self.assertRaises(GitToolError):
            parse_porcelain_v1_z("?? unsafe\npath.py\x00")
        with self.assertRaises(GitToolError):
            parse_porcelain_v1_z("?? missing-terminator.py")

    def test_patch_parser_checks_every_header_and_rejects_special_files(self):
        document = parse_patch(PATCH_TEXT)
        self.assertEqual(document.paths, ("app.py",))
        self.assertEqual(document.sha256, hashlib.sha256(PATCH_TEXT.encode()).hexdigest())
        mismatched = PATCH_TEXT.replace("+++ b/app.py", "+++ b/../escape.py")
        with self.assertRaises(PatchRejected):
            parse_patch(mismatched)
        for marker in ("new file mode 120000\n", "GIT binary patch\n"):
            with self.subTest(marker=marker), self.assertRaises(PatchRejected):
                parse_patch(PATCH_TEXT.replace("index ", marker + "index "))

    def test_patch_parser_rejects_bare_hunk_lines_before_human_approval(self):
        malformed = PATCH_TEXT.replace("+new", "def test_missing_prefix():")

        with self.assertRaisesRegex(PatchRejected, "patch prefix"):
            parse_patch(malformed)

    def test_patch_parser_requires_a_well_formed_hunk(self):
        without_hunk = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
"""
        malformed_header = PATCH_TEXT.replace("@@ -1 +1 @@", "@@ one two @@")

        with self.assertRaisesRegex(PatchRejected, "unified diff hunk"):
            parse_patch(without_hunk)
        with self.assertRaisesRegex(PatchRejected, "malformed hunk header"):
            parse_patch(malformed_header)

    def test_patch_parser_accepts_a_scoped_classic_unified_diff(self):
        classic = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+new
"""

        document = parse_patch(classic)

        self.assertEqual(document.paths, ("app.py",))

    def test_patch_snapshots_reject_non_string_fields(self):
        document = parse_patch(PATCH_TEXT)
        document_data = document.to_dict()
        document_data["text"] = 123
        with self.assertRaisesRegex(ValueError, "must be strings"):
            PatchDocument.from_dict(document_data)

        manifest = PatchManifest(
            manifest_id="manifest-1",
            run_id="run-1",
            state=ManifestState.INTENT,
            patch=document,
            before_snapshot_hash="0" * 64,
            rollback_token="token-1",
        )
        manifest_data = manifest.to_dict()
        manifest_data["run_id"] = 123
        with self.assertRaisesRegex(ValueError, "must be strings"):
            PatchManifest.from_dict(manifest_data)

    def test_git_layout_is_read_only_mounted_with_fixed_environment_and_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = root / "common.git"
            git_dir = common / "worktrees" / "repair-run"
            git_dir.mkdir(parents=True)
            (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
            worktree = root / "task"
            worktree.mkdir()
            (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            executor = FakeExecutor(
                process_result(stdout="24.0"),
                process_result(stdout=""),
            )
            runner = build_repair_sandbox(
                worktree=worktree,
                image="repair:test",
                base_sha=BASE_SHA,
                writable_paths=("app.py",),
                docker_path=Path(os.path.abspath(root / "docker.exe")),
                executor=executor,
            )
            runner.run(APPLY_CHECK_COMMAND, stdin_bytes=PATCH_TEXT.encode())

        docker_argv, kwargs = executor.calls[1]
        self.assertIn("-i", docker_argv)
        mounts = [docker_argv[index + 1] for index, item in enumerate(docker_argv) if item == "--mount"]
        self.assertEqual(len(mounts), 3)
        self.assertTrue(mounts[1].endswith("target=/git-common,readonly"))
        self.assertTrue(mounts[2].endswith("target=/workspace/.git,readonly"))
        env_values = [docker_argv[index + 1] for index, item in enumerate(docker_argv) if item == "--env"]
        self.assertIn("GIT_WORK_TREE=/workspace", env_values)
        self.assertIn("GIT_DIR=/git-common/worktrees/repair-run", env_values)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", env_values)
        self.assertIn("PYTEST_ADDOPTS=-p no:cacheprovider", env_values)
        self.assertEqual(kwargs["stdin_bytes"], PATCH_TEXT.encode())

    def test_commit_sandbox_is_separate_and_only_git_metadata_is_extra_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = root / "common.git"
            git_dir = common / "worktrees" / "repair-run"
            git_dir.mkdir(parents=True)
            (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
            worktree = root / "task"
            worktree.mkdir()
            (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            executor = FakeExecutor(process_result(stdout="24.0"), process_result())
            command = GIT_PREFIX + ("rev-parse", "HEAD")
            runner = build_commit_sandbox(
                worktree=worktree,
                image="repair:test",
                allowed_commands=(command,),
                docker_path=Path(os.path.abspath(root / "docker.exe")),
                executor=executor,
            )
            runner.run(command)

        docker_argv = executor.calls[1][0]
        mounts = [
            docker_argv[index + 1]
            for index, item in enumerate(docker_argv)
            if item == "--mount"
        ]
        self.assertEqual(len(mounts), 3)
        self.assertTrue(mounts[1].endswith("target=/workspace/.git,readonly"))
        self.assertTrue(mounts[2].endswith("target=/git-common"))
        self.assertNotIn("readonly", mounts[2])


class TestRepairMutationTools(unittest.TestCase):
    def make_tools(self, sandbox, approval_receipt="approval-ok", manifest_receipt="manifest-ok"):
        approvals = []
        manifests = []

        def persist_approval(record):
            approvals.append(record)
            return approval_receipt

        def persist_manifest(manifest):
            manifests.append(manifest)
            return manifest_receipt

        tools = RepairTools(
            run_id="run-1",
            base_sha=BASE_SHA,
            writable_paths=("app.py",),
            sandbox=sandbox,
            budget=BudgetManager(),
            persist_approval=persist_approval,
            persist_manifest=persist_manifest,
            persist_budget=lambda _event: None,
        )
        return tools, approvals, manifests

    def test_budget_is_persisted_before_and_after_an_interrupted_command(self):
        events = []

        class InterruptingSandbox:
            def __init__(self):
                self.calls = []

            def run(self, argv, *, timeout_seconds=None, stdin_bytes=None):
                self.calls.append((tuple(argv), timeout_seconds, stdin_bytes))
                raise KeyboardInterrupt

        sandbox = InterruptingSandbox()
        budget = BudgetManager()

        def persist_budget(event):
            events.append(
                (
                    event,
                    budget.usage.tool_calls,
                    budget.usage.commands,
                    len(sandbox.calls),
                )
            )

        tools = RepairTools(
            run_id="run-1",
            base_sha=BASE_SHA,
            writable_paths=("app.py",),
            sandbox=sandbox,
            budget=budget,
            persist_approval=lambda _approval: "approval-ok",
            persist_manifest=lambda _manifest: "manifest-ok",
            persist_budget=persist_budget,
        )

        with self.assertRaises(KeyboardInterrupt):
            tools.git_status()

        self.assertEqual(
            events,
            [
                ("tool_git_status_consumed", 1, 0, 0),
                ("command_consumed", 1, 1, 0),
                ("command_interrupted", 1, 1, 1),
                ("tool_git_status_interrupted", 1, 1, 1),
            ],
        )

    @staticmethod
    def approval(diff_hash):
        return issue_write_approval(
            run_id="run-1",
            checkpoint_id="cp-1",
            base_sha=BASE_SHA,
            diff_hash=diff_hash,
            plan_hash="plan-hash",
            patch_hash=hashlib.sha256(PATCH_TEXT.encode("utf-8")).hexdigest(),
            writable_paths=("app.py",),
            patch_attempt=1,
            ttl_seconds=60,
            now=100,
            nonce="human-nonce",
        )

    def successful_apply(self):
        before_hash = repository_snapshot_hash("", "")
        after_status = " M app.py\x00"
        after_hash = repository_snapshot_hash(after_status, PATCH_TEXT)
        sandbox = ScriptedSandbox(
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": after_status},
            {"stdout": PATCH_TEXT},
        )
        tools, approvals, manifests = self.make_tools(sandbox)
        consumed, manifest = tools.apply_patch(
            PATCH_TEXT,
            approval=self.approval(before_hash),
            checkpoint_id="cp-1",
            plan_hash="plan-hash",
            patch_attempt=1,
            now=110,
        )
        self.assertIsNotNone(consumed.consumed_at)
        self.assertEqual(manifest.state, ManifestState.APPLIED)
        self.assertEqual(manifest.before_snapshot_hash, before_hash)
        self.assertEqual(manifest.after_snapshot_hash, after_hash)
        self.assertEqual(type(manifest).from_dict(manifest.to_dict()), manifest)
        self.assertEqual(sandbox.calls[2][0], APPLY_CHECK_COMMAND)
        self.assertEqual(sandbox.calls[5][0], APPLY_COMMAND)
        self.assertEqual(len(approvals), 1)
        self.assertEqual([item.state for item in manifests], [ManifestState.INTENT, ManifestState.APPLIED])
        self.assertEqual(tools.budget.usage.tool_calls, 1)
        self.assertEqual(tools.budget.usage.commands, 8)
        return tools, sandbox, manifest, manifests

    def test_apply_consumes_and_persists_approval_before_mutation(self):
        self.successful_apply()

    def test_apply_rejects_a_different_patch_with_the_same_snapshot_approval(self):
        before_hash = repository_snapshot_hash("", "")
        sandbox = ScriptedSandbox({"stdout": ""}, {"stdout": ""})
        tools, approvals, manifests = self.make_tools(sandbox)
        changed_patch = PATCH_TEXT.replace("+new", "+unapproved")

        with self.assertRaises(ApprovalMismatch):
            tools.apply_patch(
                changed_patch,
                approval=self.approval(before_hash),
                checkpoint_id="cp-1",
                plan_hash="plan-hash",
                patch_attempt=1,
                now=110,
            )

        self.assertEqual(approvals, [])
        self.assertEqual(manifests, [])
        self.assertNotIn(APPLY_COMMAND, [call[0] for call in sandbox.calls])

    def test_source_context_is_exactly_scoped_and_budgeted(self):
        sandbox = ScriptedSandbox({"stdout": "print('base')\n"})
        tools, _approvals, _manifests = self.make_tools(sandbox)

        sources = tools.read_approved_sources()

        self.assertEqual(sources, {"app.py": "print('base')\n"})
        self.assertEqual(
            sandbox.calls[0][0], GIT_PREFIX + ("show", f"{BASE_SHA}:app.py")
        )
        self.assertEqual(tools.budget.usage.tool_calls, 1)
        self.assertEqual(tools.budget.usage.commands, 1)

        denied = ScriptedSandbox()
        tools, _approvals, _manifests = self.make_tools(denied)
        with self.assertRaises(PatchScopeError):
            tools.read_approved_sources(paths=("other.py",))
        self.assertEqual(denied.calls, [])

    def test_source_context_rejects_truncated_binary_or_oversized_output(self):
        cases = (
            ({"stdout": "partial", "truncated": True}, {}),
            ({"stdout": "text\x00binary"}, {}),
            ({"stdout": "four"}, {"max_file_bytes": 3}),
        )
        for outcome, limits in cases:
            with self.subTest(outcome=outcome, limits=limits):
                sandbox = ScriptedSandbox(outcome)
                tools, _approvals, _manifests = self.make_tools(sandbox)
                with self.assertRaises(GitToolError):
                    tools.read_approved_sources(**limits)

    def test_large_source_context_returns_a_bounded_line_aligned_tail(self):
        source = "first α\n" + "middle\n" * 20 + "target = 42\nlast\n"
        sandbox = ScriptedSandbox({"stdout": source})
        tools, _approvals, _manifests = self.make_tools(sandbox)

        window = tools.read_approved_sources(
            max_file_bytes=128, max_total_bytes=128
        )["app.py"]

        self.assertLessEqual(len(window.encode("utf-8")), 128)
        self.assertTrue(window.startswith("[TRUNCATED BASE SOURCE app.py:"))
        self.assertIn("next source line is", window)
        self.assertIn("target = 42\nlast\n", window)
        self.assertNotIn("first α", window)

    def test_scope_or_persistence_failure_stops_before_patch_command(self):
        sandbox = ScriptedSandbox()
        tools, _approvals, _manifests = self.make_tools(sandbox)
        outside = PATCH_TEXT.replace("app.py", "other.py")
        with self.assertRaises(PatchScopeError):
            tools.apply_patch(
                outside,
                approval=self.approval("x" * 64),
                checkpoint_id="cp-1",
                plan_hash="plan-hash",
                patch_attempt=1,
            )
        self.assertEqual(sandbox.calls, [])

        before_hash = repository_snapshot_hash("", "")
        sandbox = ScriptedSandbox({"stdout": ""}, {"stdout": ""})
        tools, _approvals, _manifests = self.make_tools(sandbox, approval_receipt="")
        with self.assertRaises(ToolPersistenceError):
            tools.apply_patch(
                PATCH_TEXT,
                approval=self.approval(before_hash),
                checkpoint_id="cp-1",
                plan_hash="plan-hash",
                patch_attempt=1,
                now=110,
            )
        self.assertEqual(len(sandbox.calls), 2)

    def test_preflight_failure_is_recorded_without_mutation(self):
        before_hash = repository_snapshot_hash("", "")
        sandbox = ScriptedSandbox(
            {"stdout": ""},
            {"stdout": ""},
            {"exit_code": 1, "stderr": "does not apply"},
        )
        tools, _approvals, manifests = self.make_tools(sandbox)
        with self.assertRaisesRegex(PatchRejected, "does not apply"):
            tools.apply_patch(
                PATCH_TEXT,
                approval=self.approval(before_hash),
                checkpoint_id="cp-1",
                plan_hash="plan-hash",
                patch_attempt=1,
                now=110,
            )
        self.assertEqual(manifests[-1].state, ManifestState.REJECTED)
        self.assertNotIn(APPLY_COMMAND, [call[0] for call in sandbox.calls])

    def test_read_only_preflight_runs_before_approval_and_never_mutates(self):
        sandbox = ScriptedSandbox(
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": ""},
        )
        tools, approvals, manifests = self.make_tools(sandbox)

        result = tools.preflight_patch(PATCH_TEXT)

        self.assertEqual(result.patch.sha256, hashlib.sha256(PATCH_TEXT.encode()).hexdigest())
        self.assertEqual(result.snapshot_hash, repository_snapshot_hash("", ""))
        self.assertRegex(result.operation_id, r"^op-")
        self.assertEqual(approvals, [])
        self.assertEqual(manifests, [])
        self.assertEqual(sandbox.calls[2][0], APPLY_CHECK_COMMAND)
        self.assertNotIn(APPLY_COMMAND, [call[0] for call in sandbox.calls])

        rejected = ScriptedSandbox(
            {"stdout": ""},
            {"stdout": ""},
            {"exit_code": 1, "stderr": "stale patch"},
            {"stdout": ""},
            {"stdout": ""},
        )
        tools, approvals, manifests = self.make_tools(rejected)
        with self.assertRaisesRegex(PatchRejected, "stale patch"):
            tools.preflight_patch(PATCH_TEXT)
        self.assertEqual(approvals, [])
        self.assertEqual(manifests, [])
        self.assertNotIn(APPLY_COMMAND, [call[0] for call in rejected.calls])

    def test_rollback_requires_exact_snapshot_and_one_manifest_token(self):
        tools, sandbox, manifest, manifests = self.successful_apply()
        after_status = " M app.py\x00"
        sandbox.outcomes.extend(
            [
                {"stdout": after_status},
                {"stdout": PATCH_TEXT},
                {"stdout": ""},
                {"stdout": ""},
                {"stdout": ""},
                {"stdout": ""},
            ]
        )
        rolled_back = tools.rollback(manifest, rollback_token=manifest.rollback_token)
        self.assertEqual(rolled_back.state, ManifestState.ROLLED_BACK)
        self.assertEqual(rolled_back.rollback_token, "")
        self.assertIn(REVERSE_CHECK_COMMAND, [call[0] for call in sandbox.calls])
        self.assertIn(REVERSE_COMMAND, [call[0] for call in sandbox.calls])
        self.assertEqual(manifests[-1].state, ManifestState.ROLLED_BACK)
        with self.assertRaises(PatchRejected):
            tools.rollback(rolled_back, rollback_token=manifest.rollback_token)

    def test_rollback_refuses_a_changed_snapshot(self):
        tools, sandbox, manifest, _manifests = self.successful_apply()
        sandbox.outcomes.extend(
            [
                {"stdout": " M app.py\x00"},
                {"stdout": PATCH_TEXT + "# changed\n"},
            ]
        )
        with self.assertRaises(SnapshotMismatch):
            tools.rollback(manifest, rollback_token=manifest.rollback_token)

    def test_git_diff_truncation_and_test_timeout_preserve_failure(self):
        sandbox = ScriptedSandbox({"stdout": "diff", "truncated": True})
        tools, _approvals, _manifests = self.make_tools(sandbox)
        with self.assertRaises(GitToolError):
            tools.git_diff(DiffScope.BASE)

        timeout = SandboxTimeout("timeout-op", "partial", "deadline")
        sandbox = ScriptedSandbox(
            {"stdout": ""},
            {"stdout": ""},
            timeout,
            {"stdout": ""},
            {"stdout": ""},
        )
        tools, _approvals, _manifests = self.make_tools(sandbox)
        results = tools.run_tests((("python", "-m", "unittest"),), timeout_seconds=5)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].timed_out)
        self.assertEqual(results[0].operation_id, "timeout-op")
        self.assertEqual(results[0].stdout, "partial")

    def test_test_command_mutation_is_quarantined(self):
        sandbox = ScriptedSandbox(
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": "tests passed"},
            {"stdout": " M app.py\x00"},
            {"stdout": PATCH_TEXT},
        )
        tools, _approvals, _manifests = self.make_tools(sandbox)
        with self.assertRaisesRegex(ToolQuarantined, "test command mutated"):
            tools.run_tests((("python", "-m", "unittest"),))

    def test_test_command_mutation_of_ignored_file_is_quarantined(self):
        ignored_status = "!! cache/result.bin\x00"
        sandbox = ScriptedSandbox(
            {"stdout": ignored_status},
            {"stdout": ""},
            {"stdout": "1" * 40 + "\n"},
            {"stdout": "tests passed"},
            {"stdout": ignored_status},
            {"stdout": ""},
            {"stdout": "2" * 40 + "\n"},
        )
        tools, _approvals, _manifests = self.make_tools(sandbox)
        with self.assertRaisesRegex(ToolQuarantined, "test command mutated"):
            tools.run_tests((("python", "-m", "unittest"),))
        hash_calls = [call for call in sandbox.calls if call[0][-2:] == ("hash-object", "--stdin-paths")]
        self.assertEqual(len(hash_calls), 2)
        self.assertEqual(hash_calls[0][1]["stdin_bytes"], b"cache/result.bin\n")

    def test_out_of_scope_post_apply_state_is_persisted_as_quarantined(self):
        before_hash = repository_snapshot_hash("", "")
        outside_status = " M other.py\x00"
        sandbox = ScriptedSandbox(
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": ""},
            {"stdout": outside_status},
            {"stdout": PATCH_TEXT},
        )
        tools, _approvals, manifests = self.make_tools(sandbox)
        with self.assertRaisesRegex(ToolQuarantined, "outside"):
            tools.apply_patch(
                PATCH_TEXT,
                approval=self.approval(before_hash),
                checkpoint_id="cp-1",
                plan_hash="plan-hash",
                patch_attempt=1,
                now=110,
            )
        self.assertEqual(manifests[-1].state, ManifestState.QUARANTINED)


if __name__ == "__main__":
    unittest.main()
