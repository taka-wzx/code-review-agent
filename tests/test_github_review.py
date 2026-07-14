"""Unit tests for the GitHub PR review payload builder (W17 B3, dry-run
scope): hunk line mapping, snapping, and payload degradation for findings
that fall outside the diff view."""
import unittest

from code_review_agent.github_review import (build_review_payload,
                                             commentable_lines,
                                             format_dry_run, snap_line)

DIFF = """\
diff --git a/pkg/mod.py b/pkg/mod.py
index 111..222 100644
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,4 +1,5 @@
 import os
-VALUE = 2
+VALUE = 3
+EXTRA = 4
 def f():
@@ -20,1 +21,2 @@ def g():
     x = 1
+    return x
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-old = 1
-old2 = 2
"""


class TestCommentableLines(unittest.TestCase):
    def test_added_and_context_lines_mapped(self):
        lines = commentable_lines(DIFF)
        # hunk 1: context(1) + added(2,3) + context(4); hunk 2: 21,22
        self.assertEqual(lines["pkg/mod.py"], {1, 2, 3, 4, 21, 22})

    def test_deleted_file_not_commentable(self):
        self.assertNotIn("/dev/null", commentable_lines(DIFF))
        self.assertNotIn("gone.py", commentable_lines(DIFF))

    def test_no_prefix_diff(self):
        lines = commentable_lines("--- x.py\n+++ x.py\n@@ -1 +1 @@\n+a = 1\n")
        self.assertEqual(lines["x.py"], {1})


class TestSnapLine(unittest.TestCase):
    LINES = {"a.py": {10, 11, 12, 30}}

    def test_exact(self):
        self.assertEqual(snap_line("a.py", 11, self.LINES), 11)

    def test_near_snaps(self):
        self.assertEqual(snap_line("a.py", 14, self.LINES), 12)

    def test_too_far_is_none(self):
        self.assertIsNone(snap_line("a.py", 50, self.LINES))

    def test_unknown_file_or_bad_line(self):
        self.assertIsNone(snap_line("b.py", 10, self.LINES))
        self.assertIsNone(snap_line("a.py", None, self.LINES))


class TestBuildPayload(unittest.TestCase):
    def _review(self, findings):
        return {"summary": "sum", "findings": findings,
                "verifier_status": "ok"}

    def test_placeable_finding_becomes_inline_comment(self):
        f = {"file": "pkg/mod.py", "line": 2, "severity": "high",
             "issue": "boom", "suggestion": "fix"}
        p = build_review_payload(self._review([f]), DIFF)
        self.assertEqual(p["event"], "COMMENT")
        self.assertEqual(len(p["comments"]), 1)
        c = p["comments"][0]
        self.assertEqual((c["path"], c["line"], c["side"]),
                         ("pkg/mod.py", 2, "RIGHT"))
        self.assertIn("boom", c["body"])
        self.assertIn("💡 fix", c["body"])

    def test_unplaceable_finding_degrades_to_body(self):
        f = {"file": "pkg/mod.py", "line": 400, "severity": "low",
             "issue": "far away", "suggestion": "s"}
        p = build_review_payload(self._review([f]), DIFF)
        self.assertEqual(p["comments"], [])
        self.assertIn("far away", p["body"])
        self.assertIn("outside the diff view", p["body"])

    def test_uncertain_marker_travels(self):
        f = {"file": "pkg/mod.py", "line": 21, "severity": "low",
             "issue": "i", "suggestion": "s", "verification": "uncertain",
             "dissent_reason": "pass B disagreed"}
        p = build_review_payload(self._review([f]), DIFF)
        self.assertIn("passes disagreed", p["comments"][0]["body"])
        self.assertIn("pass B disagreed", p["comments"][0]["body"])

    def test_failed_open_banner_and_empty_findings(self):
        p = build_review_payload({"summary": "s", "findings": [],
                                  "verifier_status": "failed_open"}, DIFF)
        self.assertIn("Verifier unavailable", p["body"])
        p2 = build_review_payload({"summary": "s", "findings": []}, DIFF)
        self.assertIn("No findings survived", p2["body"])

    def test_dry_run_mentions_gh_and_payload(self):
        p = build_review_payload(self._review([]), DIFF)
        text = format_dry_run("42", p)
        self.assertIn("gh api repos/{owner}/{repo}/pulls/42/reviews", text)
        self.assertIn('"event": "COMMENT"', text)


if __name__ == "__main__":
    unittest.main()
