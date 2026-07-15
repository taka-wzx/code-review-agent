"""Regression tests for the P0 security/correctness fixes (interview-review
round): read_file secrets guard, skip-dir pruning, diff parsing beyond the
`b/`-prefix happy path, caller matching precision, argument-injection
rejection, and crash-free rendering of unvalidated findings.
"""
import argparse
import tempfile
import unittest
from pathlib import Path

from code_review_agent.agent import _git_diff_text
from code_review_agent.context import find_callers, parse_diff
from code_review_agent.render import render_markdown
from code_review_agent.tools import read_file, run_linter, search_repo


class RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)


class TestParseDiff(unittest.TestCase):
    def test_git_default_prefix(self):
        files, _ = parse_diff("--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n")
        self.assertEqual(files, ["pkg/mod.py"])

    def test_no_prefix(self):
        files, _ = parse_diff("--- pkg/mod.py\n+++ pkg/mod.py\n@@ -1 +1 @@\n")
        self.assertEqual(files, ["pkg/mod.py"])

    def test_tab_timestamp_suffix(self):
        # diff -u style: `+++ path<TAB>2026-07-14 10:00:00`
        files, _ = parse_diff("+++ b/pkg/mod.py\t2026-07-14 10:00:00\n")
        self.assertEqual(files, ["pkg/mod.py"])

    def test_dev_null_deleted_file(self):
        files, _ = parse_diff("--- a/gone.py\n+++ /dev/null\n")
        self.assertEqual(files, [])

    def test_crlf_diff(self):
        files, _ = parse_diff("+++ b/pkg/mod.py\r\n@@ -1 +1 @@\r\n")
        self.assertEqual(files, ["pkg/mod.py"])


class TestFindCallers(RepoCase):
    def test_async_def_is_not_its_own_caller(self):
        (self.repo / "m.py").write_text(
            "async def fetch(url):\n    return url\n", encoding="utf-8")
        self.assertEqual(find_callers(self.repo, "fetch", set()), [])

    def test_substring_symbol_does_not_match(self):
        (self.repo / "m.py").write_text(
            "x = overrun(3)\ny = rerun_all(4)\n", encoding="utf-8")
        self.assertEqual(find_callers(self.repo, "run", set()), [])

    def test_method_call_still_counts(self):
        (self.repo / "m.py").write_text(
            "job.run(now=True)\n", encoding="utf-8")
        callers = find_callers(self.repo, "run", set())
        self.assertEqual([rel for rel, _ in callers], ["m.py"])

    def test_uppercase_extension_still_scanned(self):
        # rglob("*.py") matched Foo.PY on Windows; the pruned walk must not
        # silently drop such callers (suffix is matched case-insensitively,
        # same as tools._iter_text_files).
        (self.repo / "Caller.PY").write_text(
            "job.run(now=True)\n", encoding="utf-8")
        callers = find_callers(self.repo, "run", set())
        self.assertEqual([rel for rel, _ in callers], ["Caller.PY"])


class TestReadFileGuards(RepoCase):
    def test_refuses_dotenv(self):
        (self.repo / ".env").write_text("API_KEY=sk-hunter2\n", encoding="utf-8")
        result = read_file(self.repo, ".env")
        self.assertTrue(result.startswith("Error:"))
        self.assertNotIn("sk-hunter2", result)

    def test_refuses_key_material(self):
        (self.repo / "server.pem").write_text("PRIVATE\n", encoding="utf-8")
        self.assertTrue(read_file(self.repo, "server.pem").startswith("Error:"))

    def test_refuses_skip_dirs(self):
        git = self.repo / ".git"
        git.mkdir()
        (git / "config").write_text("[core]\n", encoding="utf-8")
        result = read_file(self.repo, ".git/config")
        self.assertTrue(result.startswith("Error:"))
        self.assertIn("non-project", result)

    def test_linter_refuses_sensitive_paths(self):
        (self.repo / ".env").write_text("K=v\n", encoding="utf-8")
        self.assertTrue(run_linter(self.repo, ".env").startswith("Error:"))

    def test_normal_source_still_readable(self):
        (self.repo / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.assertEqual(read_file(self.repo, "app.py"), "VALUE = 3")


class TestSearchSkipsPrunedDirs(RepoCase):
    def test_venv_content_invisible(self):
        venv = self.repo / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "big.py").write_text("NEEDLE = 1\n", encoding="utf-8")
        (self.repo / "app.py").write_text("NEEDLE = 2\n", encoding="utf-8")
        hits = search_repo(self.repo, "NEEDLE")
        self.assertIn("app.py", hits)
        self.assertNotIn(".venv", hits)


class TestArgumentInjection(unittest.TestCase):
    @staticmethod
    def _args(**over):
        base = {"pr": None, "uncommitted": False, "commit": "HEAD", "repo": "."}
        base.update(over)
        return argparse.Namespace(**base)

    def test_option_like_commit_rejected(self):
        with self.assertRaises(SystemExit):
            _git_diff_text(self._args(commit="--output=owned"))

    def test_option_like_pr_rejected(self):
        with self.assertRaises(SystemExit):
            _git_diff_text(self._args(pr="--repo=evil/evil"))


class TestRenderRobustness(unittest.TestCase):
    def test_partial_findings_do_not_crash(self):
        review = {"summary": "s",
                  "findings": [],
                  "out_of_scope_findings": [{"issue": "no file key"}],
                  "dropped_findings": [{"file": "a.py"}]}
        md = render_markdown(review)
        self.assertIn("a.py:?", md)
        self.assertIn("?:?", md)

    def test_failed_open_banner(self):
        md = render_markdown({"summary": "s", "findings": [],
                              "verifier_status": "failed_open"})
        self.assertIn("Verifier unavailable", md)
        md_ok = render_markdown({"summary": "s", "findings": [],
                                 "verifier_status": "ok"})
        self.assertNotIn("Verifier unavailable", md_ok)


if __name__ == "__main__":
    unittest.main()
