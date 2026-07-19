import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from code_review_agent.service_core import (
    DefaultReviewRunner,
    InvalidRequest,
    JobNotFound,
    JobState,
    JobStore,
    MAX_DIFF_BYTES,
    MAX_RESULT_BYTES,
    MAX_TRACE_BYTES,
    RepositoryRegistry,
    ReviewRequest,
    ReviewService,
    ServiceClosed,
    normalize_pr_ref,
    normalize_repository,
    validate_diff,
)


DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-old = 1
+new = 2
"""


class FakeRunner:
    def __init__(self, *, fail: bool = False, block: threading.Event | None = None):
        self.fail = fail
        self.block = block
        self.calls: list[ReviewRequest] = []
        self.started = threading.Event()

    def __call__(self, request: ReviewRequest, trace_path: Path):
        self.calls.append(request)
        self.started.set()
        if self.block is not None:
            self.block.wait(2)
        trace_path.write_text(json.dumps({"trace": "redacted"}) + "\n", encoding="utf-8")
        if self.fail:
            raise RuntimeError("SECRET provider detail /host/path")
        return {"summary": "ok", "findings": []}


class Week7ServiceCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.registry = RepositoryRegistry.from_json(
            json.dumps({"Owner/Repo": str(self.repo.resolve())})
        )

    def tearDown(self):
        self.temp.cleanup()

    def wait_terminal(self, service: ReviewService, job_id: str):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = service.get(job_id)
            if job["state"] in {JobState.SUCCEEDED.value, JobState.FAILED.value}:
                return job
            time.sleep(0.01)
        self.fail("job did not become terminal")

    def test_repository_registry_is_exact_and_resolves_paths(self):
        alias, path = self.registry.resolve("OWNER/REPO")
        self.assertEqual(alias, "owner/repo")
        self.assertEqual(path, self.repo.resolve())
        with self.assertRaisesRegex(InvalidRequest, "not registered"):
            self.registry.resolve("other/repo")
        for value in ("", "one", "../repo", "owner/repo/extra"):
            with self.subTest(value=value), self.assertRaises(InvalidRequest):
                normalize_repository(value)

    def test_registry_rejects_bad_config_and_non_checkout(self):
        cases = ["", "[]", "{", json.dumps({"owner/repo": "relative"})]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(InvalidRequest):
                RepositoryRegistry.from_json(raw)
        plain = self.root / "plain"
        plain.mkdir()
        with self.assertRaisesRegex(InvalidRequest, "not a Git checkout"):
            RepositoryRegistry.from_json(json.dumps({"owner/repo": str(plain.resolve())}))

    def test_pr_reference_is_repository_bound(self):
        self.assertEqual(normalize_pr_ref("owner/repo", 12), "12")
        self.assertEqual(
            normalize_pr_ref("owner/repo", "https://github.com/Owner/Repo/pull/007/"),
            "https://github.com/Owner/Repo/pull/7",
        )
        for value in ("0", "-1", "--repo=x", "https://github.com/other/repo/pull/1"):
            with self.subTest(value=value), self.assertRaises(InvalidRequest):
                normalize_pr_ref("owner/repo", value)

    def test_diff_validation_is_bounded(self):
        digest, size = validate_diff(DIFF)
        self.assertEqual(len(digest), 64)
        self.assertEqual(size, len(DIFF.encode()))
        with self.assertRaises(InvalidRequest):
            validate_diff("print('not a diff')")
        with self.assertRaisesRegex(InvalidRequest, "limit"):
            validate_diff("diff --git a/a b/a\n" + "x" * MAX_DIFF_BYTES)

    def test_store_state_machine_and_trace(self):
        store = JobStore(self.root / "state")
        job_id, duplicate = store.create(
            source_kind="diff",
            repository="owner/repo",
            source_ref="inline",
            source_sha256="0" * 64,
            source_bytes=4,
        )
        self.assertFalse(duplicate)
        self.assertEqual(store.get(job_id)["state"], "queued")
        with self.assertRaisesRegex(InvalidRequest, "terminal"):
            store.read_trace(job_id)
        store.mark_running(job_id)
        store.trace_path(job_id).write_text('{"ok":true}\n', encoding="utf-8")
        store.succeed(job_id, {"summary": "done"})
        job = store.get(job_id)
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["review"], {"summary": "done"})
        self.assertEqual(store.read_trace(job_id), '{"ok":true}\n')
        with self.assertRaises(RuntimeError):
            store.mark_running(job_id)
        with self.assertRaises(JobNotFound):
            store.get("not-a-job")

    def test_store_marks_abandoned_work_failed_on_restart(self):
        state = self.root / "state"
        store = JobStore(state)
        queued, _ = store.create(
            source_kind="diff", repository="owner/repo", source_ref="inline",
            source_sha256="0" * 64, source_bytes=1,
        )
        running, _ = store.create(
            source_kind="diff", repository="owner/repo", source_ref="inline",
            source_sha256="1" * 64, source_bytes=1,
        )
        store.mark_running(running)
        restarted = JobStore(state)
        for job_id in (queued, running):
            job = restarted.get(job_id)
            self.assertEqual(job["state"], "failed")
            self.assertEqual(job["error"]["code"], "service_restarted")

    def test_store_idempotent_delivery_and_result_limit(self):
        store = JobStore(self.root / "state")
        kwargs = dict(
            source_kind="pull_request", repository="owner/repo", source_ref="1",
            source_sha256="0" * 64, source_bytes=0, delivery_id="delivery-1",
        )
        first, duplicate = store.create(**kwargs)
        second, duplicate2 = store.create(**kwargs)
        self.assertFalse(duplicate)
        self.assertTrue(duplicate2)
        self.assertEqual(first, second)
        store.mark_running(first)
        store.succeed(first, {"value": "x" * MAX_RESULT_BYTES})
        self.assertEqual(store.get(first)["error"]["code"], "result_too_large")

    def test_review_service_success_failure_and_duplicate(self):
        runner = FakeRunner()
        service = ReviewService(self.registry, JobStore(self.root / "state"), runner=runner)
        try:
            submitted = service.submit_diff("owner/repo", DIFF)
            done = self.wait_terminal(service, submitted["review_id"])
            self.assertEqual(done["review"]["summary"], "ok")
            self.assertIn("redacted", service.get_trace(submitted["review_id"]))

            first, duplicate = service.submit_pr(
                "owner/repo", "3", delivery_id="delivery-2"
            )
            second, duplicate2 = service.submit_pr(
                "owner/repo", "3", delivery_id="delivery-2"
            )
            self.assertFalse(duplicate)
            self.assertTrue(duplicate2)
            self.assertEqual(first["review_id"], second["review_id"])
            self.wait_terminal(service, first["review_id"])
            self.assertEqual(len(runner.calls), 2)
        finally:
            service.shutdown()

        failing = ReviewService(
            self.registry, JobStore(self.root / "failure"), runner=FakeRunner(fail=True)
        )
        try:
            job = failing.submit_diff("owner/repo", DIFF)
            failed = self.wait_terminal(failing, job["review_id"])
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["error"]["code"], "internal")
            self.assertNotIn("SECRET", json.dumps(failed))
        finally:
            failing.shutdown()

    def test_shutdown_rejects_new_work(self):
        service = ReviewService(self.registry, JobStore(self.root / "state"), runner=FakeRunner())
        service.shutdown()
        with self.assertRaises(ServiceClosed):
            service.submit_diff("owner/repo", DIFF)

    def test_default_runner_fetches_pr_diff_with_safe_argv(self):
        calls = []

        def command(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, DIFF, "")

        runner = DefaultReviewRunner(client_factory=lambda: (object(), "model"), command_runner=command)
        request = ReviewRequest("0" * 32, "pull_request", "owner/repo", self.repo, "7")
        self.assertEqual(runner._pr_diff(request), DIFF)
        self.assertEqual(calls[0][0], ["gh", "pr", "diff", "7"])
        self.assertFalse(calls[0][1]["check"])

    def test_default_runner_closes_canonical_trace(self):
        request = ReviewRequest("0" * 32, "diff", "owner/repo", self.repo, "inline", DIFF)
        trace_path = self.root / "trace.jsonl"
        runner = DefaultReviewRunner(client_factory=lambda: (object(), "model"))
        with patch(
            "code_review_agent.service_core.run_review",
            return_value={"summary": "done", "findings": []},
        ) as review:
            result = runner(request, trace_path)
        self.assertEqual(result["summary"], "done")
        self.assertTrue(trace_path.is_file())
        self.assertIn("crag.service.schema", trace_path.read_text(encoding="utf-8"))
        review.assert_called_once()

    def test_pr_diff_command_failures_are_bounded(self):
        request = ReviewRequest("0" * 32, "pull_request", "owner/repo", self.repo, "7")
        for result in (
            subprocess.CompletedProcess([], 1, "", "SECRET"),
            subprocess.CompletedProcess([], 0, "", ""),
        ):
            runner = DefaultReviewRunner(command_runner=lambda *args, **kwargs: result)
            with self.subTest(returncode=result.returncode), self.assertRaisesRegex(
                RuntimeError, "GitHub diff command failed|empty or too large"
            ):
                runner._pr_diff(request)
        oversized = subprocess.CompletedProcess([], 0, "x" * (MAX_DIFF_BYTES + 1), "")
        with self.assertRaisesRegex(RuntimeError, "too large"):
            DefaultReviewRunner(command_runner=lambda *args, **kwargs: oversized)._pr_diff(request)

    def test_trace_reader_rejects_malformed_and_oversized_content(self):
        store = JobStore(self.root / "state")
        for index, content in enumerate(("not json\n", "x" * (MAX_TRACE_BYTES + 1))):
            job_id, _ = store.create(
                source_kind="diff", repository="owner/repo", source_ref="inline",
                source_sha256=str(index) * 64, source_bytes=1,
            )
            store.mark_running(job_id)
            store.trace_path(job_id).write_text(content, encoding="utf-8")
            store.fail(job_id, "internal")
            with self.subTest(index=index), self.assertRaises(InvalidRequest):
                store.read_trace(job_id)


if __name__ == "__main__":
    unittest.main()
