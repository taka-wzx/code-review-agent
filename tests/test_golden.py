"""Golden tests pinning the finder/verifier LLM protocol.

Written against the pre-refactor code and required to pass UNCHANGED after
the agentloop refactor: they assert the exact request sequence sent to the
API (model, tools, messages -- budget-exhausted nudges, rejection feedback,
assistant tool_call reconstruction) and the trace event stream. stderr
wording is deliberately not asserted (allowed to drift); anything the API
or the traces see is contract.

Run from the repo root:  python -m unittest discover -s tests
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
from agent import SUBMIT_TOOL, SYSTEM, run_review
from context import build_context
from tools import READ_FILE_TOOL, RUN_LINTER_TOOL, SEARCH_REPO_TOOL
from verifier import VERDICT_TOOL, VERIFIER_SYSTEM, verify_findings

from fakes import FakeClient, FakeTrace, response, tool_call

DIFF = ("--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n"
        "-VALUE = 2\n+VALUE = 3\n")
USER_MSG = f"Review this diff:\n\n```diff\n{DIFF}\n```"

VALID_REVIEW = {"summary": "ok", "findings": [
    {"file": "mod.py", "line": 1, "severity": "low",
     "issue": "i", "suggestion": "s"}]}
# What the second (sampling) finder run submits: a near-duplicate of the
# anchor finding, so the union collapses back to the anchor's copy.
DUP_REVIEW = {"summary": "ok2", "findings": [
    {"file": "mod.py", "line": 2, "severity": "low",
     "issue": "i", "suggestion": "s2"}]}
# Missing severity -> exactly one validation problem.
BAD_REVIEW = {"summary": "ok", "findings": [
    {"file": "mod.py", "line": 1, "issue": "i", "suggestion": "s"}]}
BAD_REVIEW_PROBLEM = ("finding 0: 'severity' must be one of "
                      "['high', 'medium', 'low'], got None")


def reviewed(review, out_of_scope=()):
    """A finder payload as run_review returns it post-W12: the
    out_of_scope_findings field is always present."""
    return {**review, "out_of_scope_findings": list(out_of_scope)}

EXPLORE_AND_SUBMIT = [READ_FILE_TOOL, SEARCH_REPO_TOOL, RUN_LINTER_TOOL,
                      SUBMIT_TOOL]


def assistant_msg(call_id, name, arguments) -> dict:
    """The assistant message the loop reconstructs for one tool call."""
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(arguments)}}]}


class RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        (self.repo / "mod.py").write_text("VALUE = 3\n", encoding="utf-8")


class TestFinderGolden(RepoCase):
    def run_finder(self, client, trace=None):
        return run_review(client, DIFF, self.repo, "test-model",
                          use_context=False, use_verify=False, trace=trace)

    def test_happy_path(self):
        client = FakeClient([
            # run 1 (anchor, temperature 0)
            response([tool_call("c1", "read_file", {"path": "mod.py"})]),
            response([tool_call("c2", "submit_review", VALID_REVIEW)],
                     tokens_in=200, tokens_out=30),
            # run 2 (sampling): submits a near-duplicate immediately
            response([tool_call("c3", "submit_review", DUP_REVIEW)]),
        ])
        trace = FakeTrace()
        review = self.run_finder(client, trace)
        self.assertEqual(review, reviewed(VALID_REVIEW))
        self.assertEqual(len(client.requests), 3)

        r1 = client.requests[0]
        self.assertEqual(r1["model"], "test-model")
        self.assertEqual(r1["max_tokens"], 8000)
        self.assertEqual(r1["temperature"], 0.0)
        self.assertEqual(r1["tool_choice"], "auto")
        self.assertEqual(r1["tools"], EXPLORE_AND_SUBMIT)
        self.assertEqual(r1["messages"], [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER_MSG},
        ])
        r2 = client.requests[1]
        self.assertEqual(r2["tools"], EXPLORE_AND_SUBMIT)
        self.assertEqual(r2["messages"], r1["messages"] + [
            assistant_msg("c1", "read_file", {"path": "mod.py"}),
            {"role": "tool", "tool_call_id": "c1", "content": "VALUE = 3"},
        ])
        # run 2 is a fresh conversation at FINDER2_TEMPERATURE, same tools
        r3 = client.requests[2]
        self.assertEqual(r3["temperature"], agent.FINDER2_TEMPERATURE)
        self.assertEqual(r3["max_tokens"], 8000)
        self.assertEqual(r3["tools"], EXPLORE_AND_SUBMIT)
        self.assertEqual(r3["messages"], [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER_MSG},
        ])
        self.assertEqual(trace.events, [
            {"kind": "llm_response", "component": "finder", "step": 1,
             "tool_calls": ["read_file"], "tokens_in": 100, "tokens_out": 20},
            {"kind": "tool", "component": "finder", "tool": "read_file",
             "args": {"path": "mod.py"}, "repeat": False,
             "result_chars": 9, "error": False},
            {"kind": "llm_response", "component": "finder", "step": 2,
             "tool_calls": ["submit_review"], "tokens_in": 200,
             "tokens_out": 30},
            {"kind": "llm_response", "component": "finder2", "step": 1,
             "tool_calls": ["submit_review"], "tokens_in": 100,
             "tokens_out": 20},
            {"kind": "finder_union", "n_run1": 1, "n_run2": 1,
             "n_merged": 1, "n_union": 1},
            {"kind": "review", "steps": 3, "findings": 1, "dropped": 0,
             "out_of_scope": 0},
        ])

    def test_invalid_submit_feeds_problems_back(self):
        client = FakeClient([
            response([tool_call("c1", "submit_review", BAD_REVIEW)]),
            response([tool_call("c2", "submit_review", VALID_REVIEW)]),
            response([tool_call("c3", "submit_review", DUP_REVIEW)]),
        ])
        trace = FakeTrace()
        review = self.run_finder(client, trace)
        self.assertEqual(review, reviewed(VALID_REVIEW))
        self.assertEqual(client.requests[1]["messages"][-2:], [
            assistant_msg("c1", "submit_review", BAD_REVIEW),
            {"role": "tool", "tool_call_id": "c1",
             "content": "Review rejected -- fix these problems and call "
                        "submit_review again: " + BAD_REVIEW_PROBLEM},
        ])
        self.assertIn({"kind": "submit_rejected", "component": "finder",
                       "problems": [BAD_REVIEW_PROBLEM]}, trace.events)

    def test_two_bad_submits_raise(self):
        client = FakeClient([
            response([tool_call("c1", "submit_review", BAD_REVIEW)]),
            response([tool_call("c2", "submit_review", BAD_REVIEW)]),
        ])
        with self.assertRaises(RuntimeError) as cm:
            self.run_finder(client)
        self.assertIn("submit_review still invalid after 2 attempts",
                      str(cm.exception))
        # a fatal anchor run never launches the sampling run
        self.assertEqual(len(client.requests), 2)

    def test_budget_exhaustion_withdraws_tools_and_nudges(self):
        client = FakeClient([
            response([tool_call("c1", "read_file", {"path": "mod.py"})]),
            response([tool_call("c2", "submit_review", VALID_REVIEW)]),
            response([tool_call("c3", "submit_review", DUP_REVIEW)]),
        ])
        with mock.patch.object(agent, "MAX_STEPS", 2):
            review = self.run_finder(client)
        self.assertEqual(review, reviewed(VALID_REVIEW))
        r2 = client.requests[1]
        self.assertEqual(r2["tools"], [SUBMIT_TOOL])
        self.assertEqual(r2["messages"][-1], {
            "role": "user",
            "content": "Step budget exhausted. Call submit_review NOW with "
                       "the findings you have established so far."})
        # run 2 submitted at its step 1 (not final), so full tools again
        self.assertEqual(client.requests[2]["tools"], EXPLORE_AND_SUBMIT)

    def test_step_cap_raises(self):
        client = FakeClient([
            response([tool_call("c1", "read_file", {"path": "mod.py"})]),
        ])
        with mock.patch.object(agent, "MAX_STEPS", 1):
            with self.assertRaises(RuntimeError) as cm:
                self.run_finder(client)
        self.assertIn("agent did not finish within 1 steps",
                      str(cm.exception))
        # Step 1 is already the final step: submit-only tools + nudge.
        self.assertEqual(client.requests[0]["tools"], [SUBMIT_TOOL])
        self.assertEqual(len(client.requests), 1)

    def test_text_answer_raises(self):
        client = FakeClient([response(content="looks fine to me")])
        with self.assertRaises(RuntimeError) as cm:
            self.run_finder(client)
        self.assertIn("model stopped without calling submit_review",
                      str(cm.exception))
        self.assertEqual(len(client.requests), 1)

    def test_run2_bad_submits_degrade_to_anchor(self):
        client = FakeClient([
            response([tool_call("c1", "submit_review", VALID_REVIEW)]),
            response([tool_call("c2", "submit_review", BAD_REVIEW)]),
            response([tool_call("c3", "submit_review", BAD_REVIEW)]),
        ])
        trace = FakeTrace()
        review = self.run_finder(client, trace)
        self.assertEqual(review, reviewed(VALID_REVIEW))
        self.assertIn({"kind": "finder2_failed", "reason": "bad_submits"},
                      trace.events)
        self.assertIn({"kind": "finder_union", "n_run1": 1, "n_run2": 0,
                       "n_merged": 0, "n_union": 1}, trace.events)

    def test_run2_text_answer_degrades_to_anchor(self):
        client = FakeClient([
            response([tool_call("c1", "submit_review", VALID_REVIEW)]),
            response(content="all good"),
        ])
        trace = FakeTrace()
        review = self.run_finder(client, trace)
        self.assertEqual(review, reviewed(VALID_REVIEW))
        self.assertIn({"kind": "finder2_failed", "reason": "text_answer"},
                      trace.events)

    def test_union_dedup_and_scope(self):
        # run 2 contributes one duplicate (merged away) and one new finding
        # on a file the diff does not touch (demoted, never verified).
        out_finding = {"file": "other.py", "line": 9, "severity": "high",
                       "issue": "leaks handle", "suggestion": "close it"}
        run2 = {"summary": "ok2", "findings": [
            DUP_REVIEW["findings"][0], out_finding]}
        client = FakeClient([
            response([tool_call("c1", "submit_review", VALID_REVIEW)]),
            response([tool_call("c2", "submit_review", run2)]),
            # verifier passes A and B: keep the single in-scope finding
            response([tool_call("v1", "submit_verdicts", verdicts_payload(
                (0, "keep", "a0")))]),
            response([tool_call("v2", "submit_verdicts", verdicts_payload(
                (0, "keep", "b0")))]),
        ])
        trace = FakeTrace()
        review = run_review(client, DIFF, self.repo, "test-model",
                            use_context=False, use_verify=True, trace=trace)
        anchor = VALID_REVIEW["findings"][0]
        self.assertEqual(review["findings"],
                         [{**anchor, "verification": "confirmed"}])
        self.assertEqual(review["dropped_findings"], [])
        self.assertEqual(review["out_of_scope_findings"],
                         [{**out_finding, "origin": "finder2"}])
        # W13: the verifier's exact input is persisted for replays,
        # unmutated by the verdicts applied afterwards.
        self.assertEqual(review["candidate_findings"], [anchor])
        # the verifier saw ONLY the in-scope finding
        va_user = client.requests[2]["messages"][1]["content"]
        self.assertIn(USER_MSG, va_user)
        self.assertIn(json.dumps([{"index": 0, **anchor}],
                                 ensure_ascii=False, indent=2), va_user)
        self.assertNotIn("other.py", va_user)
        self.assertIn({"kind": "finder_union", "n_run1": 1, "n_run2": 2,
                       "n_merged": 1, "n_union": 2}, trace.events)
        self.assertIn({"kind": "review", "steps": 2, "findings": 1,
                       "dropped": 0, "out_of_scope": 1}, trace.events)


FINDINGS = [
    {"file": "mod.py", "line": 1, "severity": "high",
     "issue": "i0", "suggestion": "s0"},
    {"file": "mod.py", "line": 2, "severity": "low",
     "issue": "i1", "suggestion": "s1"},
    {"file": "mod.py", "line": 3, "severity": "low",
     "issue": "i2", "suggestion": "s2"},
]
REVIEW_INPUT = "Review this diff:\n\n```diff\n(diff body)\n```"


def verdicts_payload(*triples):
    return {"verdicts": [{"finding_index": i, "verdict": v, "reason": r}
                         for i, v, r in triples]}


def verifier_user_msg(findings, reverse=False) -> str:
    numbered = [{"index": i, **f} for i, f in enumerate(findings)]
    if reverse:
        numbered = list(reversed(numbered))
    return (REVIEW_INPUT + "\n\nCandidate findings to verify:\n\n"
            + json.dumps(numbered, ensure_ascii=False, indent=2))


class TestVerifierGolden(RepoCase):
    def test_two_pass_merge(self):
        client = FakeClient([
            # pass A: one exploration round, then verdicts
            response([tool_call("v1", "read_file", {"path": "mod.py"})]),
            response([tool_call("v2", "submit_verdicts", verdicts_payload(
                (0, "keep", "a0"), (1, "keep", "a1"), (2, "drop", "a2")))]),
            # pass B: verdicts immediately
            response([tool_call("v3", "submit_verdicts", verdicts_payload(
                (0, "keep", "b0"), (1, "drop", "b1"), (2, "drop", "b2")))]),
        ])
        trace = FakeTrace()
        kept, dropped = verify_findings(client, "test-model", REVIEW_INPUT,
                                        FINDINGS, self.repo, trace=trace)
        self.assertEqual(kept, [
            {**FINDINGS[0], "verification": "confirmed"},
            {**FINDINGS[1], "verification": "uncertain",
             "dissent_reason": "b1"},
        ])
        self.assertEqual(dropped, [{**FINDINGS[2], "drop_reason": "2/2: a2"}])

        self.assertEqual(len(client.requests), 3)
        r1 = client.requests[0]
        self.assertEqual(r1["model"], "test-model")
        self.assertEqual(r1["max_tokens"], 4000)
        self.assertEqual(r1["temperature"], 0.0)
        self.assertEqual(r1["tool_choice"], "auto")
        self.assertEqual(r1["tools"], [READ_FILE_TOOL, SEARCH_REPO_TOOL,
                                       RUN_LINTER_TOOL, VERDICT_TOOL])
        self.assertEqual(r1["messages"], [
            {"role": "system", "content": VERIFIER_SYSTEM},
            {"role": "user", "content": verifier_user_msg(FINDINGS)},
        ])
        r2 = client.requests[1]
        self.assertEqual(r2["messages"], r1["messages"] + [
            assistant_msg("v1", "read_file", {"path": "mod.py"}),
            {"role": "tool", "tool_call_id": "v1", "content": "VALUE = 3"},
        ])
        # pass B sees the findings back-to-front
        r3 = client.requests[2]
        self.assertEqual(r3["messages"], [
            {"role": "system", "content": VERIFIER_SYSTEM},
            {"role": "user",
             "content": verifier_user_msg(FINDINGS, reverse=True)},
        ])
        self.assertEqual(trace.events, [
            {"kind": "llm_response", "component": "verifierA", "step": 1,
             "tool_calls": ["read_file"], "tokens_in": 100, "tokens_out": 20},
            {"kind": "tool", "component": "verifierA", "tool": "read_file",
             "args": {"path": "mod.py"}, "repeat": False,
             "result_chars": 9, "error": False},
            {"kind": "llm_response", "component": "verifierA", "step": 2,
             "tool_calls": ["submit_verdicts"], "tokens_in": 100,
             "tokens_out": 20},
            {"kind": "verifier_pass", "pass_id": "A", "steps": 2, "drops": 1},
            {"kind": "llm_response", "component": "verifierB", "step": 1,
             "tool_calls": ["submit_verdicts"], "tokens_in": 100,
             "tokens_out": 20},
            {"kind": "verifier_pass", "pass_id": "B", "steps": 1, "drops": 2},
            {"kind": "verdicts", "kept": 2, "dropped": 1,
             "confirmed": 1, "uncertain": 1},
        ])

    def test_failed_pass_degrades_to_single(self):
        two = FINDINGS[:2]
        bad = verdicts_payload((0, "keep", "r"))   # finding 1 never judged
        client = FakeClient([
            response([tool_call("v1", "submit_verdicts", bad)]),
            response([tool_call("v2", "submit_verdicts", bad)]),
            response([tool_call("v3", "submit_verdicts", verdicts_payload(
                (0, "keep", "b0"), (1, "drop", "b1")))]),
        ])
        trace = FakeTrace()
        kept, dropped = verify_findings(client, "test-model", REVIEW_INPUT,
                                        two, self.repo, trace=trace)
        self.assertEqual(kept, [two[0]])   # single-pass: no verification tag
        self.assertEqual(dropped, [{**two[1], "drop_reason": "b1"}])
        self.assertEqual(client.requests[1]["messages"][-2:], [
            assistant_msg("v1", "submit_verdicts", bad),
            {"role": "tool", "tool_call_id": "v1",
             "content": "Verdicts rejected: findings never judged: [1]"},
        ])
        self.assertIn({"kind": "verifier_pass_failed", "pass_id": "A",
                       "problems": ["findings never judged: [1]"]},
                      trace.events)
        self.assertIn({"kind": "verdicts", "kept": 1, "dropped": 1,
                       "degraded": True, "failed_pass": "A"}, trace.events)

    def test_text_answers_fail_open(self):
        client = FakeClient([response(content=f"t{i}") for i in range(4)])
        trace = FakeTrace()
        kept, dropped = verify_findings(client, "test-model", REVIEW_INPUT,
                                        FINDINGS, self.repo, trace=trace)
        self.assertEqual(kept, FINDINGS)
        self.assertEqual(dropped, [])
        # a text answer gets the fixed nudge and counts as a bad submit
        self.assertEqual(client.requests[1]["messages"][-2:], [
            {"role": "assistant", "content": "t0"},
            {"role": "user",
             "content": "You must call submit_verdicts covering every "
                        "finding index exactly once."},
        ])
        self.assertIn({"kind": "verifier_fail_open", "n_findings": 3},
                      trace.events)

    def test_no_findings_short_circuits(self):
        client = FakeClient([])
        self.assertEqual(verify_findings(client, "test-model", REVIEW_INPUT,
                                         [], self.repo), ([], []))
        self.assertEqual(client.requests, [])

    # W13 sentinel: a 2/2 drop whose reason uses forbidden reasoning is
    # demoted to the uncertain channel instead of dying.
    FLAG_FINDING = {"file": "mod.py", "line": 5, "severity": "medium",
                    "issue": "dead path: PREDICT flag is False so the "
                             "branch never runs",
                    "suggestion": "s"}

    def test_sentinel_rescues_forbidden_two_pass_drop(self):
        client = FakeClient([
            response([tool_call("v1", "submit_verdicts", verdicts_payload(
                (0, "drop", "a future caller could pass frozen=True")))]),
            response([tool_call("v2", "submit_verdicts", verdicts_payload(
                (0, "drop", "callers may supply a different value")))]),
        ])
        trace = FakeTrace()
        kept, dropped = verify_findings(client, "test-model", REVIEW_INPUT,
                                        [self.FLAG_FINDING], self.repo,
                                        trace=trace)
        self.assertEqual(dropped, [])
        self.assertEqual(kept, [{
            **self.FLAG_FINDING,
            "verification": "uncertain",
            "dissent_reason": "[sentinel:dead-path-dismissed] "
                              "2/2: a future caller could pass frozen=True",
            "rescue": "dead-path-dismissed",
        }])
        self.assertIn({"kind": "sentinel_rescue", "n": 1,
                       "items": [{"file": "mod.py", "line": 5,
                                  "tag": "dead-path-dismissed"}]},
                      trace.events)
        self.assertIn({"kind": "verdicts", "kept": 1, "dropped": 0,
                       "confirmed": 0, "uncertain": 1}, trace.events)

    def test_sentinel_on_degraded_single_pass(self):
        bad = verdicts_payload((5, "keep", "r"))   # out of range -> invalid
        client = FakeClient([
            response([tool_call("v1", "submit_verdicts", bad)]),
            response([tool_call("v2", "submit_verdicts", bad)]),
            response([tool_call("v3", "submit_verdicts", verdicts_payload(
                (0, "drop", "callers might set the flag to True")))]),
        ])
        trace = FakeTrace()
        kept, dropped = verify_findings(client, "test-model", REVIEW_INPUT,
                                        [self.FLAG_FINDING], self.repo,
                                        trace=trace)
        self.assertEqual(dropped, [])
        self.assertEqual(kept, [{
            **self.FLAG_FINDING,
            "verification": "uncertain",
            "dissent_reason": "[sentinel:dead-path-dismissed] "
                              "callers might set the flag to True",
            "rescue": "dead-path-dismissed",
        }])
        self.assertIn({"kind": "verdicts", "kept": 1, "dropped": 0,
                       "degraded": True, "failed_pass": "A"}, trace.events)


class TestCacheAccounting(unittest.TestCase):
    """W13: provider cache fields land in llm_response events only when the
    SDK usage object carries them (DeepSeek); other providers unchanged."""

    SUBMIT = {"type": "function",
              "function": {"name": "submit_x", "parameters": {}}}

    def loop(self, resp):
        from agentloop import run_submit_loop
        client = FakeClient([resp])
        trace = FakeTrace()
        result = run_submit_loop(
            client, "m", [{"role": "user", "content": "u"}],
            explore_tools=[], submit_tool=self.SUBMIT,
            parse=lambda raw: (json.loads(raw), []),
            session=None, max_steps=3, max_submit_attempts=2,
            max_tokens=100, budget_msg="b", reject_msg=lambda p: "r",
            trace=trace, component="finder")
        self.assertEqual(result.reason, "ok")
        return trace.events[0]

    def test_cache_fields_recorded_when_present(self):
        resp = response([tool_call("c1", "submit_x", {"ok": True})])
        resp.usage.prompt_cache_hit_tokens = 60
        resp.usage.prompt_cache_miss_tokens = 40
        ev = self.loop(resp)
        self.assertEqual(ev["cache_hit"], 60)
        self.assertEqual(ev["cache_miss"], 40)

    def test_cache_fields_absent_when_provider_lacks_them(self):
        ev = self.loop(response([tool_call("c1", "submit_x", {"ok": True})]))
        self.assertNotIn("cache_hit", ev)
        self.assertNotIn("cache_miss", ev)


class TestBuildContextSmoke(unittest.TestCase):
    """Guards the SKIP_DIRS import move; loose contains-checks only."""

    def test_pack_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "CLAUDE.md").write_text("conventions here",
                                            encoding="utf-8")
            (repo / "mod.py").write_text(
                "import helper\ndef f(x):\n    return x + helper.H\n",
                encoding="utf-8")
            (repo / "helper.py").write_text("H = 1\n", encoding="utf-8")
            (repo / "caller.py").write_text("from mod import f\ny = f(2)\n",
                                            encoding="utf-8")
            diff = ("--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,3 @@\n"
                    " import helper\n-def g(x):\n-    return x\n"
                    "+def f(x):\n+    return x + helper.H\n")
            pack = build_context(diff, repo)
        self.assertIn("## Project conventions (CLAUDE.md)", pack)
        self.assertIn("## Changed file: mod.py", pack)
        self.assertIn("## Imported module: helper.py (imported by mod.py)",
                      pack)
        self.assertIn("## Caller of f() in caller.py", pack)


if __name__ == "__main__":
    unittest.main()
