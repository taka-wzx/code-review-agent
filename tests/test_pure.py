"""Unit tests for the pure validation/merge/metrics functions that the
docstrings advertise as offline-testable. Locks the eval-side behavior the
refactor must not disturb (judge.py's loop itself is out of scope).
"""
import unittest

from agent import validate_review
from judge import compute_metrics, validate_verdict
from verifier import apply_verdicts, merge_verdicts, validate_verdicts


def finding(**over):
    f = {"file": "a.py", "line": 1, "severity": "low",
         "issue": "i", "suggestion": "s"}
    f.update(over)
    return f


class TestValidateReview(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_review(
            {"summary": "s", "findings": [finding()]}), [])

    def test_not_an_object(self):
        self.assertEqual(validate_review("nope"),
                         ["review is not a JSON object"])

    def test_missing_summary_and_findings(self):
        self.assertEqual(validate_review({"summary": "  ", "findings": []}),
                         ["'summary' missing or empty"])
        self.assertEqual(validate_review({"summary": "s"}),
                         ["'findings' missing or not a list"])

    def test_bad_finding_fields(self):
        self.assertEqual(validate_review(
            {"summary": "s", "findings": ["x"]}),
            ["finding 0: not an object"])
        self.assertEqual(validate_review(
            {"summary": "s", "findings": [finding(line=True)]}),
            ["finding 0: 'line' must be an integer, got True"])
        self.assertEqual(validate_review(
            {"summary": "s", "findings": [finding(severity="worst")]}),
            ["finding 0: 'severity' must be one of "
             "['high', 'medium', 'low'], got 'worst'"])


def verdict(i, v, reason="r"):
    return {"finding_index": i, "verdict": v, "reason": reason}


class TestValidateVerdicts(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_verdicts(
            [verdict(0, "keep"), verdict(1, "drop")], 2), [])

    def test_not_a_list(self):
        self.assertEqual(validate_verdicts(None, 1),
                         ["'verdicts' missing or not a list"])

    def test_out_of_range_and_missing(self):
        self.assertEqual(validate_verdicts([verdict(5, "keep")], 2),
                         ["finding index 5 out of range 0..1",
                          "findings never judged: [0, 1]"])

    def test_duplicate_and_bad_verdict(self):
        self.assertEqual(validate_verdicts(
            [verdict(0, "keep"), verdict(0, "drop")], 1),
            ["finding 0 judged twice"])
        self.assertEqual(validate_verdicts([verdict(0, "maybe")], 1),
                         ["finding 0: bad verdict 'maybe'"])


class TestApplyMergeVerdicts(unittest.TestCase):
    def test_apply(self):
        f = [finding(line=1), finding(line=2)]
        kept, dropped = apply_verdicts(
            f, [verdict(0, "keep"), verdict(1, "drop", "why")])
        self.assertEqual(kept, [f[0]])
        self.assertEqual(dropped, [{**f[1], "drop_reason": "why"}])

    def test_merge_all_combinations(self):
        f = [finding(line=n) for n in range(4)]
        va = [verdict(0, "keep", "a0"), verdict(1, "drop", "a1"),
              verdict(2, "keep", "a2"), verdict(3, "drop", "a3")]
        vb = [verdict(0, "keep", "b0"), verdict(1, "drop", "b1"),
              verdict(2, "drop", "b2"), verdict(3, "keep", "b3")]
        kept, dropped = merge_verdicts(f, va, vb)
        self.assertEqual(kept, [
            {**f[0], "verification": "confirmed"},
            {**f[2], "verification": "uncertain", "dissent_reason": "b2"},
            {**f[3], "verification": "uncertain", "dissent_reason": "a3"},
        ])
        self.assertEqual(dropped, [{**f[1], "drop_reason": "2/2: a1"}])


def bug_verdict(bug_id, hit, idxs, reason="r"):
    return {"id": bug_id, "hit": hit, "matched_finding_indices": idxs,
            "reason": reason}


def unmatched(i, cls, reason="r"):
    return {"finding_index": i, "classification": cls, "reason": reason}


BUGS = [{"id": "b1"}, {"id": "b2"}]
THREE_FINDINGS = [finding(line=n) for n in range(3)]


class TestJudgeValidateVerdict(unittest.TestCase):
    def test_valid(self):
        v = {"bugs": [bug_verdict("b1", True, [0]),
                      bug_verdict("b2", False, [])],
             "unmatched_findings": [unmatched(1, "noise"),
                                    unmatched(2, "false_positive")]}
        self.assertEqual(validate_verdict(v, BUGS, THREE_FINDINGS), [])

    def test_id_mismatch(self):
        v = {"bugs": [bug_verdict("b1", False, []),
                      bug_verdict("bX", False, [])],
             "unmatched_findings": [unmatched(i, "noise") for i in range(3)]}
        self.assertEqual(validate_verdict(v, BUGS, THREE_FINDINGS),
                         ["bug ids mismatch: expected ['b1', 'b2'], "
                          "got ['b1', 'bX']"])

    def test_hit_flag_vs_indices(self):
        v = {"bugs": [bug_verdict("b1", True, []),
                      bug_verdict("b2", False, [0])],
             "unmatched_findings": [unmatched(1, "noise"),
                                    unmatched(2, "noise")]}
        self.assertEqual(validate_verdict(v, BUGS, THREE_FINDINGS), [
            "bug b1: hit=true but no matched findings",
            "bug b2: hit=false but has matched findings",
        ])

    def test_coverage_problems(self):
        v = {"bugs": [bug_verdict("b1", True, [0]),
                      bug_verdict("b2", False, [])],
             "unmatched_findings": [unmatched(0, "noise"),
                                    unmatched(1, "irrelevant")]}
        self.assertEqual(validate_verdict(v, BUGS, THREE_FINDINGS), [
            "finding 0 is both matched to a bug and listed as unmatched",
            "finding 1: bad classification 'irrelevant'",
            "findings never mentioned: [2]",
        ])


class TestComputeMetrics(unittest.TestCase):
    def test_totals_and_unique_matching(self):
        # finding 1 hits both bugs -> matched counts it once
        verdicts = {"d1": {"verdict": {
            "bugs": [bug_verdict("b1", True, [0, 1]),
                     bug_verdict("b2", True, [1])],
            "unmatched_findings": [unmatched(2, "false_positive"),
                                   unmatched(3, "noise")],
        }, "n_findings": 4}}
        t = compute_metrics(verdicts)["total"]
        self.assertEqual((t["bugs"], t["hits"], t["findings"], t["matched"]),
                         (2, 2, 4, 2))
        self.assertEqual((t["false_positives"], t["noise"]), (1, 1))
        self.assertEqual(t["recall"], 1.0)
        self.assertEqual(t["precision"], 0.5)

    def test_empty_slice_gives_none(self):
        verdicts = {"d2": {"verdict": {"bugs": [], "unmatched_findings": []},
                           "n_findings": 0}}
        t = compute_metrics(verdicts)["total"]
        self.assertIsNone(t["recall"])
        self.assertIsNone(t["precision"])


if __name__ == "__main__":
    unittest.main()
