"""Offline coverage for provider setup, repo tools, rendering, and traces."""
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from code_review_agent import llm
from code_review_agent.render import render_markdown
from code_review_agent.tools import ToolSession, read_file, run_linter, search_repo
from code_review_agent.tracelog import Trace, force_utf8, iter_events, tev


class RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)


class TestDotenvAndClient(RepoCase):
    def test_load_dotenv_parses_supported_lines_without_overwriting_env(self):
        (self.repo / ".env").write_text(
            "\ufeff# comment\nKEEP=file\nQUOTED=\"hello\"\nSINGLE='world'\n"
            "SPACED = value \nBROKEN\n",
            encoding="utf-8",
        )
        keys = ("KEEP", "QUOTED", "SINGLE", "SPACED")
        old = {key: os.environ.get(key) for key in keys}
        self.addCleanup(self._restore_env, old)
        for key in keys:
            os.environ.pop(key, None)
        os.environ["KEEP"] = "process"

        with mock.patch.object(llm.Path, "cwd", return_value=self.repo):
            llm.load_dotenv()

        self.assertEqual(os.environ["KEEP"], "process")
        self.assertEqual(os.environ["QUOTED"], "hello")
        self.assertEqual(os.environ["SINGLE"], "world")
        self.assertEqual(os.environ["SPACED"], "value")

    @staticmethod
    def _restore_env(old):
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_load_dotenv_missing_file_is_noop(self):
        with mock.patch.object(llm.Path, "cwd", return_value=self.repo):
            llm.load_dotenv()

    def test_make_client_uses_provider_and_model_override(self):
        fake_client = object()
        env = {
            "LLM_PROVIDER": "GLM",
            "ZHIPUAI_API_KEY": "secret",
            "LLM_MODEL": "glm-pinned",
        }
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(llm, "load_dotenv"), \
                mock.patch.object(llm, "OpenAI", return_value=fake_client) as ctor:
            client, model = llm.make_client()
        self.assertIs(client, fake_client)
        self.assertEqual(model, "glm-pinned")
        ctor.assert_called_once_with(
            api_key="secret",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            timeout=llm.REQUEST_TIMEOUT,
            max_retries=2,
        )

    def test_make_client_default_and_actionable_errors(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "key"}, clear=True), \
                mock.patch.object(llm, "load_dotenv"), \
                mock.patch.object(llm, "OpenAI", return_value=object()):
            _, model = llm.make_client()
        self.assertEqual(model, llm.PROVIDERS["deepseek"]["model"])

        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "other"}, clear=True), \
                mock.patch.object(llm, "load_dotenv"), \
                self.assertRaisesRegex(SystemExit, "Unknown LLM_PROVIDER"):
            llm.make_client()
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "glm"}, clear=True), \
                mock.patch.object(llm, "load_dotenv"), \
                self.assertRaisesRegex(SystemExit, "GLM_API_KEY or ZHIPUAI_API_KEY"):
            llm.make_client()


class TestRepoTools(RepoCase):
    def test_read_file_recovery_continuation_and_bounds(self):
        pkg = self.repo / "pkg"
        pkg.mkdir()
        (pkg / "same.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
        (self.repo / "other").mkdir()
        (self.repo / "other" / "same.py").write_text("x\n", encoding="utf-8")

        self.assertIn("path escapes repo root", read_file(self.repo, "../outside.py"))
        self.assertIn("is a directory", read_file(self.repo, "pkg"))
        self.assertIn("did you mean", read_file(self.repo, "missing/same.py"))
        self.assertIn("exists anywhere", read_file(self.repo, "never.py"))
        self.assertIn("past the end", read_file(self.repo, "pkg/same.py", 9))
        self.assertEqual(
            read_file(self.repo, "pkg/same.py", 2),
            "[pkg/same.py from line 2]\ntwo\nthree",
        )
        with mock.patch("code_review_agent.tools.READ_CAP", 5):
            result = read_file(self.repo, "pkg/same.py")
        self.assertIn("truncated", result)
        self.assertIn("start_line=2", result)

    def test_search_results_limits_and_recovery_messages(self):
        (self.repo / "a.py").write_text("NEEDLE\nNEEDLE twice\n", encoding="utf-8")
        self.assertEqual(search_repo(self.repo, "  "), "Error: empty search pattern.")
        self.assertIn("does not exist", search_repo(self.repo, "plain-miss"))
        self.assertIn("literal, not regex", search_repo(self.repo, "^regex$"))
        with mock.patch("code_review_agent.tools.SEARCH_MAX_HITS", 1):
            result = search_repo(self.repo, "NEEDLE")
        self.assertIn("a.py:1", result)
        self.assertIn("stopped at 1 hits", result)

    @mock.patch("code_review_agent.tools.subprocess.run")
    def test_linter_success_errors_and_output_cap(self, run):
        source = self.repo / "app.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "notes.txt").write_text("text\n", encoding="utf-8")
        self.assertIn("path escapes", run_linter(self.repo, "../x.py"))
        self.assertIn("file not found", run_linter(self.repo, "missing.py"))
        self.assertIn("only lints Python", run_linter(self.repo, "notes.txt"))

        run.return_value = SimpleNamespace(stdout="", stderr="")
        self.assertEqual(run_linter(self.repo, "app.py"), "No lint findings in app.py.")
        run.return_value = SimpleNamespace(stdout="", stderr="No module named pyflakes")
        self.assertIn("pyflakes is not installed", run_linter(self.repo, "app.py"))
        run.return_value = SimpleNamespace(stdout=str(source) + ":1: issue" + "x" * 5000,
                                           stderr="")
        result = run_linter(self.repo, "app.py")
        self.assertNotIn(str(source), result)
        self.assertIn("...[truncated]", result)
        run.side_effect = subprocess.TimeoutExpired("pyflakes", 30)
        self.assertIn("timed out", run_linter(self.repo, "app.py"))

    def test_tool_session_protocol_repeat_and_miss_nudge(self):
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        trace = SimpleNamespace(events=[])
        trace.event = lambda kind, **data: trace.events.append({"kind": kind, **data})
        session = ToolSession(self.repo, trace=trace, component="finder")

        self.assertIn("malformed", session.execute("read_file", "[1]"))
        first = session.execute("read_file", '{"path": "app.py"}')
        self.assertEqual(first, "VALUE = 1")
        self.assertIn("repeat call", session.execute("read_file", '{"path": "app.py"}'))
        self.assertIn("unknown tool", session.execute("nope", "{}"))
        for i in range(2):
            self.assertNotIn("consecutive misses", session.execute(
                "search_repo", json.dumps({"pattern": f"missing-{i}"})))
        third = session.execute("search_repo", '{"pattern": "missing-2"}')
        self.assertIn("3 consecutive misses", third)
        regex_miss = session.execute("search_repo", '{"pattern": "^missing$"}')
        self.assertIn("literal, not regex", regex_miss)
        self.assertEqual(session._miss_streak, 0)
        session.execute("read_file", '{"path": "app.py", "start_line": 1}')
        self.assertEqual(session._miss_streak, 0)
        self.assertTrue(any(event.get("repeat") for event in trace.events))

    def test_tool_session_converts_tool_exception_to_error(self):
        session = ToolSession(self.repo)
        with mock.patch("code_review_agent.tools.read_file", side_effect=OSError("boom")):
            self.assertEqual(
                session.execute("read_file", '{"path": "app.py"}'),
                "Error: boom",
            )


class TestTraceAndRender(RepoCase):
    def test_trace_round_trip_and_optional_emission(self):
        path = self.repo / "nested" / "run.jsonl"
        trace = Trace(path)
        trace.event("start", value=Path("x"))
        tev(trace, "done", count=2)
        tev(None, "ignored")
        trace.close()
        events = list(iter_events(path))
        self.assertEqual([event["kind"] for event in events], ["start", "done"])
        self.assertEqual(events[0]["value"], "x")
        self.assertIsInstance(events[0]["t"], float)

    def test_force_utf8_reconfigures_supported_streams(self):
        class Stream(io.StringIO):
            def __init__(self):
                super().__init__()
                self.settings = None

            def reconfigure(self, **settings):
                self.settings = settings

        stdout, stderr = Stream(), Stream()
        with mock.patch("code_review_agent.tracelog.sys.stdout", stdout), \
                mock.patch("code_review_agent.tracelog.sys.stderr", stderr):
            force_utf8()
        self.assertEqual(stdout.settings, {"encoding": "utf-8", "errors": "replace"})
        self.assertEqual(stderr.settings, stdout.settings)

    def test_render_all_audit_channels(self):
        review = {
            "summary": "summary",
            "verifier_status": "ok",
            "findings": [
                {"file": "a.py", "line": 2, "severity": "low",
                 "issue": "low", "suggestion": "fix low"},
                {"file": "a.py", "line": 1, "severity": "high",
                 "issue": "high", "suggestion": ""},
                {"file": "b.py", "line": 3, "severity": "custom",
                 "issue": "uncertain", "suggestion": "inspect",
                 "verification": "uncertain", "dissent_reason": "evidence differs"},
            ],
            "out_of_scope_findings": [{"file": "old.py", "line": 8,
                                       "issue": "existing"}],
            "dropped_findings": [{"file": "c.py", "line": 4,
                                  "issue": "candidate", "drop_reason": "refuted"}],
        }
        rendered = render_markdown(review, title="Audit")
        self.assertLess(rendered.index("high"), rendered.index("low"))
        self.assertIn("Unverified findings (1)", rendered)
        self.assertIn("evidence differs", rendered)
        self.assertIn("out-of-diff finding", rendered)
        self.assertIn("drop reason: refuted", rendered)
        self.assertIn("fix low", rendered)


if __name__ == "__main__":
    unittest.main()
