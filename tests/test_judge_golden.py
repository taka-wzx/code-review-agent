"""Golden tests for the judge's LLM loop (W16 C-1): the scorer's retry /
text-answer / feedback protocol was the biggest untested surface in the
repo -- everything downstream (recall/precision, gate verdicts) flows
through judge_one. Same FakeClient technique as test_golden.py: pin the
exact request sequence and trace events.

Also covers scores_is_stale, the resume-integrity check (W16 C-3).
"""
import json
import tempfile
import unittest
from pathlib import Path

from judge import (JUDGE_SYSTEM, SCORE_TOOL, judge_one, scores_is_stale,
                   truth_sha256)

from fakes import FakeClient, FakeTrace, response, tool_call

BUGS = [{"id": "b1", "severity": "high",
         "description": "off-by-one in window; hit = names the boundary"}]
FINDINGS = [{"index": 0, "file": "a.py", "line": 3, "severity": "high",
             "issue": "window boundary off by one", "suggestion": "fix"}]
GOOD = {"bugs": [{"id": "b1", "hit": True,
                  "matched_finding_indices": [0], "reason": "names it"}],
        "unmatched_findings": []}
# Wrong bug id -> "bug ids mismatch" plus finding 0 never mentioned.
BAD = {"bugs": [{"id": "zzz", "hit": False,
                 "matched_finding_indices": [], "reason": "?"}],
       "unmatched_findings": []}

USER_MSG = "Diff: d1\n\n" + json.dumps(
    {"planted_bugs": BUGS, "agent_findings": FINDINGS},
    ensure_ascii=False, indent=2)


class TestJudgeGolden(unittest.TestCase):
    def test_happy_path_request_and_trace(self):
        client = FakeClient([
            response([tool_call("j1", "submit_scores", GOOD)]),
        ])
        trace = FakeTrace()
        verdict = judge_one(client, "test-model", "d1", BUGS, FINDINGS,
                            trace=trace)
        self.assertEqual(verdict, GOOD)
        self.assertEqual(len(client.requests), 1)
        r = client.requests[0]
        self.assertEqual(r["model"], "test-model")
        self.assertEqual(r["max_tokens"], 4000)
        self.assertEqual(r["temperature"], 0.0)
        self.assertEqual(r["tool_choice"], "auto")
        self.assertEqual(r["tools"], [SCORE_TOOL])
        self.assertEqual(r["messages"], [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": USER_MSG},
        ])
        self.assertEqual(trace.events, [
            {"kind": "llm_response", "component": "judge", "diff": "d1",
             "step": 1, "tool_calls": ["submit_scores"],
             "tokens_in": 100, "tokens_out": 20},
            {"kind": "judge_verdict", "diff": "d1", "attempts": 1,
             "hits": 1, "n_findings": 1},
        ])

    def test_invalid_verdict_feeds_problems_back(self):
        client = FakeClient([
            response([tool_call("j1", "submit_scores", BAD)]),
            response([tool_call("j2", "submit_scores", GOOD)]),
        ])
        trace = FakeTrace()
        verdict = judge_one(client, "test-model", "d1", BUGS, FINDINGS,
                            trace=trace)
        self.assertEqual(verdict, GOOD)
        # the retry request carries the rejected call + the tool feedback
        tail = client.requests[1]["messages"][-2:]
        self.assertEqual(tail[0]["tool_calls"][0]["function"]["name"],
                         "submit_scores")
        self.assertEqual(tail[1]["role"], "tool")
        self.assertEqual(tail[1]["tool_call_id"], "j1")
        self.assertTrue(tail[1]["content"].startswith(
            "Verdict rejected: bug ids mismatch"))
        self.assertIn("judge_rejected",
                      [e["kind"] for e in trace.events])

    def test_text_answer_gets_user_nudge(self):
        client = FakeClient([
            response(content="I think bug b1 was found."),
            response([tool_call("j2", "submit_scores", GOOD)]),
        ])
        verdict = judge_one(client, "test-model", "d1", BUGS, FINDINGS)
        self.assertEqual(verdict, GOOD)
        tail = client.requests[1]["messages"][-2:]
        self.assertEqual(tail[0], {"role": "assistant",
                                   "content": "I think bug b1 was found."})
        self.assertEqual(tail[1]["role"], "user")
        self.assertTrue(tail[1]["content"].startswith(
            "You must call submit_scores."))

    def test_two_failures_raise(self):
        client = FakeClient([
            response([tool_call("j1", "submit_scores", BAD)]),
            response([tool_call("j2", "submit_scores", BAD)]),
        ])
        with self.assertRaises(RuntimeError) as cm:
            judge_one(client, "test-model", "d1", BUGS, FINDINGS)
        self.assertIn("judge failed after 2 attempts", str(cm.exception))
        self.assertEqual(len(client.requests), 2)


class TestScoresIsStale(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.truth = self.dir / "truth.json"
        self.truth.write_text('{"d1": []}', encoding="utf-8")
        self.scores = self.dir / "scores.json"

    def _write_scores(self, obj):
        self.scores.write_text(json.dumps(obj), encoding="utf-8")

    def test_legacy_scores_without_meta_are_trusted(self):
        self._write_scores({"metrics": {}, "verdicts": {}})
        self.assertFalse(scores_is_stale(self.scores, self.truth))

    def test_matching_hash_is_fresh(self):
        self._write_scores({"meta": {"truth_sha256": truth_sha256(self.truth)},
                            "metrics": {}, "verdicts": {}})
        self.assertFalse(scores_is_stale(self.scores, self.truth))

    def test_truth_change_marks_stale(self):
        self._write_scores({"meta": {"truth_sha256": truth_sha256(self.truth)},
                            "metrics": {}, "verdicts": {}})
        self.truth.write_text('{"d1": [], "d2": []}', encoding="utf-8")
        self.assertTrue(scores_is_stale(self.scores, self.truth))

    def test_corrupt_scores_marks_stale(self):
        self.scores.write_text("{not json", encoding="utf-8")
        self.assertTrue(scores_is_stale(self.scores, self.truth))


if __name__ == "__main__":
    unittest.main()
