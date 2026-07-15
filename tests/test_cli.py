"""Offline CLI and git-diff source tests; provider calls are fully mocked."""
import argparse
import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from code_review_agent import agent


DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
REVIEW = {
    "summary": "checked",
    "findings": [{"file": "app.py", "line": 1, "severity": "high",
                  "issue": "broken", "suggestion": "fix it"}],
}


class TestGitDiffSources(unittest.TestCase):
    @staticmethod
    def args(**values):
        defaults = {"pr": None, "uncommitted": False, "commit": "HEAD", "repo": "."}
        defaults.update(values)
        return argparse.Namespace(**defaults)

    @mock.patch("code_review_agent.agent.subprocess.run")
    def test_commit_uncommitted_and_pr_commands(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout=DIFF, stderr="")
        self.assertEqual(agent._git_diff_text(self.args()), DIFF)
        self.assertEqual(run.call_args.args[0][:3], ["git", "show", "HEAD"])

        self.assertEqual(agent._git_diff_text(self.args(uncommitted=True)), DIFF)
        self.assertEqual(run.call_args.args[0],
                         ["git", "diff", "HEAD", "--no-color", "--unified=3"])

        self.assertEqual(agent._git_diff_text(self.args(pr="12")), DIFF)
        self.assertEqual(run.call_args.args[0], ["gh", "pr", "diff", "12"])

    @mock.patch("code_review_agent.agent.subprocess.run")
    def test_non_head_warning_and_subprocess_failures(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout=DIFF, stderr="")
        err = io.StringIO()
        with redirect_stderr(err):
            agent._git_diff_text(self.args(commit="abc123"))
        self.assertIn("check out that commit", err.getvalue())

        run.return_value = SimpleNamespace(returncode=2, stdout="", stderr="bad revision")
        with self.assertRaisesRegex(SystemExit, "bad revision"):
            agent._git_diff_text(self.args())
        run.return_value = SimpleNamespace(returncode=0, stdout=" \n", stderr="")
        with self.assertRaisesRegex(SystemExit, "empty diff"):
            agent._git_diff_text(self.args())
        run.side_effect = FileNotFoundError
        with self.assertRaisesRegex(SystemExit, "not found on PATH"):
            agent._git_diff_text(self.args(pr="9"))


class TestMain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.diff = self.root / "change.diff"
        self.diff.write_text(DIFF, encoding="utf-8")

    def run_main(self, argv, review=REVIEW):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["crag", *argv]), \
                mock.patch.object(agent, "make_client", return_value=(object(), "model")), \
                mock.patch.object(agent, "run_review", return_value=review) as run_review, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            agent.main()
        return stdout.getvalue(), stderr.getvalue(), run_review

    def test_json_diff_file_and_out_file(self):
        out = self.root / "review.json"
        stdout, stderr, run_review = self.run_main([
            str(self.diff), "--repo", str(self.root), "--no-context", "--no-verify",
            "--out", str(out),
        ])
        self.assertEqual(json.loads(stdout), REVIEW)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), REVIEW)
        self.assertIn("review written", stderr)
        self.assertFalse(run_review.call_args.kwargs["use_context"])
        self.assertFalse(run_review.call_args.kwargs["use_verify"])

    def test_markdown_pr_dry_run(self):
        with mock.patch.object(agent, "_git_diff_text", return_value=DIFF):
            stdout, _, _ = self.run_main([
                "--pr", "12", "--repo", str(self.root), "--format", "md",
                "--post-dry-run",
            ])
        self.assertIn("Code review: PR #12", stdout)
        self.assertIn("[dry-run] would run: gh api", stdout)
        self.assertIn('"comments"', stdout)

    def test_live_post_uses_payload_on_stdin_and_closes_trace(self):
        trace = SimpleNamespace(closed=False)
        trace.event = lambda kind, **data: None
        trace.close = lambda: setattr(trace, "closed", True)
        posted = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(agent, "_git_diff_text", return_value=DIFF), \
                mock.patch.object(agent, "Trace", return_value=trace), \
                mock.patch.object(agent.subprocess, "run", return_value=posted) as run, \
                mock.patch.object(sys, "argv", [
                    "crag", "--pr", "12", "--repo", str(self.root),
                    "--post", "--trace", str(self.root / "trace.jsonl"),
                ]), \
                mock.patch.object(agent, "make_client", return_value=(object(), "model")), \
                mock.patch.object(agent, "run_review", return_value=REVIEW), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            agent.main()
        self.assertTrue(trace.closed)
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.root))
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["event"], "COMMENT")

    def test_post_failure_is_actionable(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
        with mock.patch.object(agent, "_git_diff_text", return_value=DIFF), \
                mock.patch.object(agent.subprocess, "run", return_value=failed), \
                mock.patch.object(sys, "argv", [
                    "crag", "--pr", "12", "--repo", str(self.root), "--post",
                ]), \
                mock.patch.object(agent, "make_client", return_value=(object(), "model")), \
                mock.patch.object(agent, "run_review", return_value=REVIEW), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(SystemExit, "permission denied"):
            agent.main()

    def test_parser_rejects_ambiguous_sources_and_post_without_pr(self):
        for argv in (["crag"], ["crag", str(self.diff), "--commit"],
                     ["crag", str(self.diff), "--post"]):
            with mock.patch.object(sys, "argv", argv), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                agent.main()

    def test_module_entrypoint_delegates_to_main(self):
        with mock.patch.object(agent, "main") as main:
            runpy.run_module("code_review_agent.__main__", run_name="__main__")
        main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
