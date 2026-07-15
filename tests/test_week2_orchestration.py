"""Offline tests for Week 2 parallel lanes and the shared soft deadline."""
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from code_review_agent import agent, verifier
from code_review_agent.agentloop import LoopResult, run_submit_loop
from code_review_agent.orchestration import (
    CallOutcome,
    Deadline,
    run_parallel_pair,
)
from code_review_agent.tracelog import Trace, iter_events

from fakes import FakeClient, FakeTrace, response, tool_call


DIFF = ("--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n"
        "-VALUE = 2\n+VALUE = 3\n")
FINDING = {"file": "mod.py", "line": 1, "severity": "high",
           "issue": "wrong value", "suggestion": "restore it"}


def _serial_pair(first, second, **_kwargs):
    outcomes = []
    for call in (first, second):
        try:
            outcomes.append(CallOutcome(value=call()))
        except Exception as exc:
            outcomes.append(CallOutcome(error=exc))
    return tuple(outcomes)


class TestDeadline(unittest.TestCase):
    def test_monotonic_remaining_and_request_cap(self):
        now = [10.0]
        deadline = Deadline.after(5.0, clock=lambda: now[0])
        self.assertEqual(deadline.timeout_seconds, 5.0)
        self.assertEqual(deadline.remaining(), 5.0)
        self.assertEqual(deadline.request_timeout(120.0), 5.0)
        now[0] = 13.5
        self.assertEqual(deadline.remaining(), 1.5)
        self.assertEqual(deadline.request_timeout(1.0), 1.0)
        now[0] = 15.0
        self.assertTrue(deadline.expired())
        self.assertEqual(deadline.request_timeout(120.0), 0.0)

    def test_rejects_invalid_budgets(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Deadline.after(value)

    def test_loop_stops_before_starting_request_after_deadline(self):
        now = [0.0]
        deadline = Deadline.after(5.0, clock=lambda: now[0])
        explore = {"type": "function", "function": {
            "name": "read_file", "parameters": {"type": "object"}}}
        submit = {"type": "function", "function": {
            "name": "submit_x", "parameters": {"type": "object"}}}
        client = FakeClient([
            response([tool_call("r1", "read_file", {"path": "mod.py"})]),
        ])
        trace = FakeTrace()

        class AdvancingSession:
            @staticmethod
            def execute(_name, _arguments):
                now[0] = 6.0
                return "VALUE = 3"

        result = run_submit_loop(
            client, "m", [{"role": "user", "content": "u"}],
            explore_tools=[explore], submit_tool=submit,
            parse=lambda raw: (raw, []), session=AdvancingSession(),
            max_steps=3, max_submit_attempts=2, max_tokens=100,
            budget_msg="submit", reject_msg=lambda problems: str(problems),
            trace=trace, component="finder", deadline=deadline,
        )
        self.assertEqual(result.reason, "deadline")
        self.assertEqual(result.steps, 1)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0]["timeout"], 5.0)
        self.assertIn({"kind": "deadline_exhausted", "component": "finder",
                       "completed_steps": 1, "budget_seconds": 5.0},
                      trace.events)


class TestParallelPair(unittest.TestCase):
    def test_lanes_reach_barrier_concurrently_and_emit_timing(self):
        barrier = threading.Barrier(2)
        trace = FakeTrace()

        def lane(value):
            barrier.wait(timeout=2.0)
            return value

        first, second = run_parallel_pair(
            lambda: lane("a"), lambda: lane("b"),
            stage="finder", trace=trace,
        )
        self.assertEqual((first.value, second.value), ("a", "b"))
        self.assertEqual(trace.events[0], {
            "kind": "parallel_stage_started", "stage": "finder", "lanes": 2,
        })
        finished = trace.events[-1]
        self.assertEqual(finished["kind"], "parallel_stage_finished")
        self.assertEqual(finished["errors"], [None, None])
        self.assertGreaterEqual(finished["elapsed_ms"], 0.0)

    def test_lane_exception_is_returned_without_losing_peer_result(self):
        def fail():
            raise ValueError("boom")

        first, second = run_parallel_pair(fail, lambda: 7, stage="verifier")
        self.assertIsInstance(first.error, ValueError)
        self.assertEqual(second.value, 7)


class TestReviewOrchestration(unittest.TestCase):
    def test_expired_anchor_deadline_is_fatal_without_api_request(self):
        now = [0.0]
        expired = Deadline.after(1.0, clock=lambda: now[0])
        now[0] = 2.0
        client = FakeClient([])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(agent.Deadline, "after",
                                  return_value=expired), \
                self.assertRaisesRegex(RuntimeError,
                                       "deadline exhausted.*anchor"):
            agent.run_review(client, DIFF, Path(tmp), "m",
                             use_context=False, use_verify=False)
        self.assertEqual(client.requests, [])

    def test_finder2_deadline_degrades_to_anchor(self):
        trace = FakeTrace()

        def finder_pass(_client, _model, _user, _repo, *, component,
                        **_kwargs):
            if component == "finder2":
                return LoopResult(reason="deadline")
            return LoopResult(
                payload={"summary": "anchor", "findings": [dict(FINDING)]},
                steps=1,
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                reason="ok",
            )

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(agent, "_run_pair",
                                  side_effect=_serial_pair), \
                mock.patch.object(agent, "_finder_pass",
                                  side_effect=finder_pass):
            review = agent.run_review(object(), DIFF, Path(tmp), "m",
                                      use_context=False, use_verify=False,
                                      trace=trace)
        self.assertEqual(review["findings"], [FINDING])
        self.assertIn({"kind": "finder2_failed", "reason": "deadline"},
                      trace.events)

    def test_finder_lanes_overlap_under_run_review(self):
        barrier = threading.Barrier(2)
        components = []
        lock = threading.Lock()

        def finder_pass(_client, _model, _user, _repo, *, component,
                        deadline, **_kwargs):
            with lock:
                components.append(component)
            barrier.wait(timeout=2.0)
            finding = dict(FINDING)
            if component == "finder2":
                finding.update(line=2, issue="second defect")
            return LoopResult(
                payload={"summary": component, "findings": [finding]},
                steps=1,
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
                reason="ok",
            )

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(agent, "_finder_pass",
                                  side_effect=finder_pass):
            review = agent.run_review(object(), DIFF, Path(tmp), "m",
                                      use_context=False, use_verify=False)
        self.assertCountEqual(components, ["finder", "finder2"])
        self.assertEqual(len(review["findings"]), 2)

    def test_verifier_lanes_overlap_under_public_entry_point(self):
        barrier = threading.Barrier(2)
        passes = []
        lock = threading.Lock()

        def verify_pass(_client, _model, _review_input, _findings, _repo,
                        *, pass_id, deadline, **_kwargs):
            with lock:
                passes.append(pass_id)
            barrier.wait(timeout=2.0)
            return [{"finding_index": 0, "verdict": "keep",
                     "reason": pass_id}]

        with mock.patch.object(verifier, "_verify_pass",
                               side_effect=verify_pass):
            kept, dropped, status = verifier.verify_findings(
                object(), "m", "review", [FINDING], Path("."),
            )
        self.assertCountEqual(passes, ["A", "B"])
        self.assertEqual(status, "ok")
        self.assertEqual(dropped, [])
        self.assertEqual(kept[0]["verification"], "confirmed")

    def test_expired_verifier_deadline_fails_open_without_api_request(self):
        now = [0.0]
        deadline = Deadline.after(1.0, clock=lambda: now[0])
        now[0] = 2.0
        client = FakeClient([])
        trace = FakeTrace()
        kept, dropped, status = verifier._verify_findings(
            client, "m", "review", [FINDING], Path("."), trace=trace,
            deadline=deadline,
        )
        self.assertEqual((kept, dropped, status),
                         ([FINDING], [], "failed_open"))
        self.assertEqual(client.requests, [])
        self.assertEqual(
            sum(event["kind"] == "deadline_exhausted"
                for event in trace.events),
            2,
        )
        self.assertIn({"kind": "verifier_fail_open", "n_findings": 1},
                      trace.events)

    def test_run_review_shares_one_deadline_with_all_stages(self):
        seen = []

        def finder_pass(_client, _model, _user, _repo, *, component,
                        deadline, **_kwargs):
            seen.append((component, deadline))
            return LoopResult(
                payload={"summary": component, "findings": [dict(FINDING)]},
                steps=1,
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                reason="ok",
            )

        def verify_findings(_client, _model, _user, findings, _repo, *,
                            deadline, **_kwargs):
            seen.append(("verifier", deadline))
            return findings, [], "ok"

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(agent, "_run_pair",
                                  side_effect=_serial_pair), \
                mock.patch.object(agent, "_finder_pass",
                                  side_effect=finder_pass), \
                mock.patch.object(agent, "_verify_findings",
                                  side_effect=verify_findings):
            agent.run_review(object(), DIFF, Path(tmp), "m",
                             use_context=False, use_verify=True)

        deadlines = [deadline for _component, deadline in seen]
        self.assertEqual(len(deadlines), 3)
        self.assertTrue(all(deadline is deadlines[0] for deadline in deadlines))
        self.assertEqual(deadlines[0].timeout_seconds,
                         agent.REVIEW_TIMEOUT_SECONDS)


class TestConcurrentTrace(unittest.TestCase):
    def test_jsonl_records_remain_intact_across_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            trace = Trace(path)

            def write(worker):
                for index in range(100):
                    trace.event("worker", worker=worker, index=index)

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(write, range(4)))
            trace.close()
            events = list(iter_events(path))

        self.assertEqual(len(events), 400)
        self.assertEqual(
            {(event["worker"], event["index"]) for event in events},
            {(worker, index) for worker in range(4) for index in range(100)},
        )


if __name__ == "__main__":
    unittest.main()
