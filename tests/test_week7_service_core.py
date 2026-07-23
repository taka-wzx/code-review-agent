import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from code_review_agent.service_core import (
    DefaultReviewRunner,
    ExternalCommandError,
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


class FakeProcess:
    def __init__(self, output, data: bytes, *, returncode: int = 0, running: bool = False):
        output.write(data)
        output.flush()
        self.returncode = None if running else returncode
        self.final_returncode = returncode
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = self.final_returncode

    def wait(self):
        return self.returncode


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
        self.stores: list[JobStore] = []

    def tearDown(self):
        for store in reversed(self.stores):
            store.close()
        self.temp.cleanup()

    def make_store(self, path: Path) -> JobStore:
        store = JobStore(path)
        self.stores.append(store)
        return store

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
            "7",
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
        store = self.make_store(self.root / "state")
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
        self.assertEqual(job["state"], "awaiting_approval")
        self.assertEqual(job["review"], {"summary": "done"})
        self.assertEqual(store.read_trace(job_id), '{"ok":true}\n')
        with self.assertRaises(RuntimeError):
            store.mark_running(job_id)
        with self.assertRaises(JobNotFound):
            store.get("not-a-job")

    def test_store_leaves_abandoned_work_for_lease_recovery(self):
        state = self.root / "state"
        store = self.make_store(state)
        running, _ = store.create(
            source_kind="diff", repository="owner/repo", source_ref="inline",
            source_sha256="0" * 64, source_bytes=1,
        )
        queued, _ = store.create(
            source_kind="diff", repository="owner/repo", source_ref="inline",
            source_sha256="1" * 64, source_bytes=1,
        )
        start = datetime.now(timezone.utc) + timedelta(seconds=1)
        lease = store.claim("restart-worker", lease_seconds=1, now=start)
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertEqual(lease.job_id, running)
        store.mark_running(lease, now=start)
        store.close()
        restarted = self.make_store(state)
        self.assertEqual(restarted.get(queued)["state"], "queued")
        self.assertEqual(restarted.get(running)["state"], "running")
        recovered = restarted.claim(
            "recovery-worker", lease_seconds=10, now=start + timedelta(seconds=2)
        )
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.job_id, running)
        self.assertEqual(recovered.attempt_count, 2)

    def test_compatibility_mark_running_claims_the_requested_job(self):
        store = self.make_store(self.root / "compat-requested")
        first, _ = store.create(
            source_kind="diff",
            repository="owner/repo",
            source_ref="inline",
            source_sha256="2" * 64,
            source_bytes=1,
        )
        second, _ = store.create(
            source_kind="diff",
            repository="owner/repo",
            source_ref="inline",
            source_sha256="3" * 64,
            source_bytes=1,
        )
        store.mark_running(second)
        self.assertEqual(store.get(first)["state"], "queued")
        self.assertEqual(store.get(second)["state"], "running")
        store.fail(second, "compatibility_test")

    def test_store_allows_a_second_process_without_sweeping_live_jobs(self):
        state = self.root / "state"
        store = self.make_store(state)
        queued, _ = store.create(
            source_kind="diff", repository="owner/repo", source_ref="inline",
            source_sha256="0" * 64, source_bytes=1,
        )
        second = JobStore(state, database_url=store.database_url, auto_migrate=False)
        self.stores.append(second)
        self.assertEqual(store.get(queued)["state"], "queued")
        self.assertFalse((state / ".service.lock").exists())

    def test_store_idempotent_delivery_and_result_limit(self):
        store = self.make_store(self.root / "state")
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
        self.assertEqual(store.get(first)["error"]["code"], "schema_policy")

    def test_review_service_success_failure_and_duplicate(self):
        runner = FakeRunner()
        service = ReviewService(self.registry, self.make_store(self.root / "state"), runner=runner)
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
            self.registry, self.make_store(self.root / "failure"), runner=FakeRunner(fail=True)
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
        service = ReviewService(
            self.registry, self.make_store(self.root / "state"), runner=FakeRunner()
        )
        service.shutdown()
        with self.assertRaises(ServiceClosed):
            service.submit_diff("owner/repo", DIFF)

    def test_default_runner_fetches_pr_diff_with_safe_argv(self):
        calls = []

        def process(argv, **kwargs):
            calls.append((argv, kwargs))
            return FakeProcess(kwargs["stdout"], DIFF.encode())

        runner = DefaultReviewRunner(
            client_factory=lambda: (object(), "model"), process_factory=process
        )
        request = ReviewRequest("0" * 32, "pull_request", "owner/repo", self.repo, "7")
        self.assertEqual(runner._pr_diff(request), DIFF)
        self.assertEqual(calls[0][0], ["gh", "pr", "diff", "7"])
        self.assertEqual(calls[0][1]["stderr"], subprocess.DEVNULL)
        self.assertIn("env", calls[0][1])

    def test_default_runner_fences_pr_diff_to_submitted_head(self):
        calls = []
        responses = iter([(HEAD := "a" * 40).encode(), DIFF.encode(), HEAD.encode()])

        def process(argv, **kwargs):
            calls.append((argv, kwargs))
            return FakeProcess(kwargs["stdout"], next(responses))

        request = ReviewRequest(
            "0" * 32,
            "pull_request",
            "owner/repo",
            self.repo,
            "7",
            head_sha=HEAD,
        )
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "provider-secret",
                "CRAG_DATABASE_URL": "postgresql://secret@db/reviews",
                "GH_TOKEN": "github-required-token",
            },
            clear=False,
        ):
            self.assertEqual(DefaultReviewRunner(process_factory=process)._pr_diff(request), DIFF)
        self.assertEqual(
            [call[0][1:3] for call in calls],
            [["pr", "view"], ["pr", "diff"], ["pr", "view"]],
        )
        for _, kwargs in calls:
            self.assertNotIn("DEEPSEEK_API_KEY", kwargs["env"])
            self.assertNotIn("CRAG_DATABASE_URL", kwargs["env"])
            self.assertEqual(kwargs["env"]["GH_TOKEN"], "github-required-token")

        mismatch = DefaultReviewRunner(
            process_factory=lambda *args, **kwargs: FakeProcess(
                kwargs["stdout"], ("b" * 40).encode()
            )
        )
        with self.assertRaisesRegex(InvalidRequest, "head no longer matches"):
            mismatch._pr_diff(request)

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

    def test_default_runner_disables_sdk_retries_before_budgeting(self):
        class Client:
            def __init__(self, max_retries=2):
                self.max_retries = max_retries
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_: None))

            def with_options(self, *, max_retries):
                return Client(max_retries=max_retries)

        request = ReviewRequest("0" * 32, "diff", "owner/repo", self.repo, "inline", DIFF)
        runner = DefaultReviewRunner(client_factory=lambda: (Client(), "model"))
        with patch(
            "code_review_agent.service_core.run_review",
            return_value={"summary": "done", "findings": []},
        ) as review:
            runner(request, self.root / "no-retries.jsonl")
        budgeted = review.call_args.args[0]
        self.assertEqual(budgeted._target.max_retries, 0)

    def test_pr_diff_command_failures_are_bounded(self):
        request = ReviewRequest("0" * 32, "pull_request", "owner/repo", self.repo, "7")
        for returncode, data in (
            (1, b"SECRET"),
            (0, b""),
        ):
            runner = DefaultReviewRunner(
                process_factory=lambda *args, _data=data, _code=returncode, **kwargs: FakeProcess(
                    kwargs["stdout"], _data, returncode=_code
                )
            )
            with self.subTest(returncode=returncode), self.assertRaisesRegex(
                ExternalCommandError, "GitHub command failed|empty or too large"
            ):
                runner._pr_diff(request)
        processes = []

        def oversized(*args, **kwargs):
            process = FakeProcess(kwargs["stdout"], b"x" * (MAX_DIFF_BYTES + 1), running=True)
            processes.append(process)
            return process

        with self.assertRaisesRegex(ExternalCommandError, "too large"):
            DefaultReviewRunner(process_factory=oversized)._pr_diff(request)
        self.assertTrue(processes[0].killed)

    def test_external_command_failure_has_stable_category(self):
        def fail(request, trace_path):
            del request, trace_path
            raise ExternalCommandError("SECRET command detail")

        service = ReviewService(
            self.registry, self.make_store(self.root / "external"), runner=fail
        )
        try:
            job = service.submit_diff("owner/repo", DIFF)
            failed = self.wait_terminal(service, job["review_id"])
            self.assertEqual(failed["error"]["code"], "external_command")
            self.assertNotIn("SECRET", json.dumps(failed))
        finally:
            service.shutdown()

    def test_submission_does_not_depend_on_an_executor(self):
        service = ReviewService(
            self.registry, self.make_store(self.root / "race")
        )
        try:
            job, duplicate = service.submit_pr(
                "owner/repo", "9", delivery_id="delivery-race"
            )
            self.assertFalse(duplicate)
            self.assertEqual(job["state"], "queued")
            replay, duplicate2 = service.submit_pr(
                "owner/repo", "9", delivery_id="delivery-race"
            )
            self.assertTrue(duplicate2)
            self.assertEqual(job["review_id"], replay["review_id"])
        finally:
            service.shutdown()

    def test_concurrent_delivery_submission_queues_once(self):
        gate = threading.Event()
        runner = FakeRunner(block=gate)
        service = ReviewService(
            self.registry, self.make_store(self.root / "concurrent"), runner=runner
        )
        results = []

        def submit():
            results.append(
                service.submit_pr("owner/repo", "10", delivery_id="delivery-concurrent")
            )

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        gate.set()
        try:
            self.assertEqual(sorted(duplicate for _, duplicate in results), [False, True])
            self.assertEqual({job["review_id"] for job, _ in results}, {results[0][0]["review_id"]})
            self.wait_terminal(service, results[0][0]["review_id"])
            self.assertEqual(len(runner.calls), 1)
        finally:
            service.shutdown()

    def test_trace_reader_rejects_malformed_and_oversized_content(self):
        store = self.make_store(self.root / "state")
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
