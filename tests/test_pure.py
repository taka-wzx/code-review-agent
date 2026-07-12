"""Unit tests for the pure validation/merge/metrics functions that the
docstrings advertise as offline-testable. Locks the eval-side behavior the
refactor must not disturb (judge.py's loop itself is out of scope).
"""
import unittest

from agent import validate_review
from findings import dedup_union, is_duplicate, similarity, split_by_scope
from judge import compute_metrics, validate_verdict
from replay_verifier import reconstruct_candidates
from verifier import (apply_verdicts, classify_drop, merge_verdicts,
                      rescue_forbidden_drops, validate_verdicts)


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

    def test_out_of_scope_passthrough_and_backward_compat(self):
        base = {"verdict": {"bugs": [], "unmatched_findings": []},
                "n_findings": 0}
        # d_old: pre-W12 scores entry without n_out_of_scope -> counts as 0
        verdicts = {"d_new": {**base, "n_out_of_scope": 3}, "d_old": base}
        m = compute_metrics(verdicts)
        self.assertEqual(m["per_diff"]["d_new"]["out_of_scope"], 3)
        self.assertEqual(m["per_diff"]["d_old"]["out_of_scope"], 0)
        self.assertEqual(m["total"]["out_of_scope"], 3)


class TestDedupUnion(unittest.TestCase):
    """W12 dual-run union. Jaccard boundaries pinned with synthetic token
    issues: "t1 t2 t3 x1 x2" vs "t1 t2 t3 t4 t5" = 3/7 ~ .43 (far tier),
    "t1 t2 x1 x2 x3" = 2/8 = .25 (near tier), "t1 x1 x2 x3 x4" = 1/9 (never).
    """

    def test_similarity_is_token_set_jaccard(self):
        self.assertEqual(similarity("t1 t2 t3 t4 t5", "t1 t2 t3 x1 x2"), 3 / 7)
        self.assertEqual(similarity("", "t1"), 0.0)
        # case-insensitive, punctuation ignored
        self.assertEqual(similarity("Foo(bar)", "foo bar"), 1.0)

    def test_far_tier_merges_at_any_line_distance(self):
        a = finding(line=1, issue="t1 t2 t3 t4 t5")
        b = finding(line=200, issue="t1 t2 t3 x1 x2")
        self.assertTrue(is_duplicate(a, b))

    def test_near_tier_needs_line_proximity(self):
        a = finding(line=10, issue="t1 t2 t3 t4 t5")
        b = finding(line=14, issue="t1 t2 x1 x2 x3")   # jac .25, delta 4
        far = finding(line=15, issue="t1 t2 x1 x2 x3")  # jac .25, delta 5
        self.assertTrue(is_duplicate(a, b))
        self.assertFalse(is_duplicate(a, far))

    def test_distinct_bugs_same_line_do_not_merge(self):
        # the measured d7 pattern: distinct bugs on one line score ~.22
        a = finding(line=15, issue="t1 t2 a1 a2 a3")
        b = finding(line=15, issue="t1 t2 b1 b2 b3 b4")  # jac 2/9 ~ .22
        self.assertFalse(is_duplicate(a, b))

    def test_different_file_never_merges(self):
        a = finding(file="a.py", issue="t1 t2 t3")
        b = finding(file="b.py", issue="t1 t2 t3")
        self.assertFalse(is_duplicate(a, b))

    def test_realistic_same_bug_pair(self):
        a = finding(line=24, issue="division by zero when window is empty "
                                   "in bootstrap_stats")
        b = finding(line=31, issue="bootstrap_stats divides by zero for an "
                                   "empty window")
        self.assertTrue(is_duplicate(a, b))

    def test_union_anchor_verbatim_and_origin_tag(self):
        a = finding(line=1, issue="t1 t2 t3 t4 t5")
        dup = finding(line=2, issue="t1 t2 t3 x1 x2")
        new = finding(line=50, issue="y1 y2 y3 y4 y5")
        union, n_merged = dedup_union([a], [dup, new])
        self.assertEqual(n_merged, 1)
        self.assertEqual(union, [a, {**new, "origin": "finder2"}])
        self.assertNotIn("origin", union[0])

    def test_union_empty_extra_is_identity(self):
        a = finding()
        self.assertEqual(dedup_union([a], []), ([a], 0))

    def test_extra_internal_duplicates_collapse(self):
        a = finding(file="z.py", issue="unrelated anchor finding")
        b1 = finding(line=5, issue="t1 t2 t3 t4 t5")
        b2 = finding(line=6, issue="t1 t2 t3 x1 x2")   # dups b1, not anchor
        union, n_merged = dedup_union([a], [b1, b2])
        self.assertEqual(union, [a, {**b1, "origin": "finder2"}])
        self.assertEqual(n_merged, 1)

    def test_threshold_override(self):
        a = finding(line=1, issue="t1 t2 t3 t4 t5")
        dup = finding(line=200, issue="t1 t2 t3 x1 x2")   # jac ~.43
        union, n_merged = dedup_union([a], [dup], sim_far=0.5)
        self.assertEqual(n_merged, 0)
        self.assertEqual(union, [a, {**dup, "origin": "finder2"}])


class TestSplitByScope(unittest.TestCase):
    def test_partition_by_changed_files(self):
        fin = finding(file="tracker/summary.py")
        fout = finding(file="tracker/ingest.py")
        in_scope, out = split_by_scope([fin, fout], ["tracker/summary.py"])
        self.assertEqual(in_scope, [fin])
        self.assertEqual(out, [fout])

    def test_path_normalization(self):
        fin = finding(file="./tracker/summary.py")
        fwin = finding(file="tracker\\summary.py")
        in_scope, out = split_by_scope([fin, fwin], ["./tracker/summary.py"])
        self.assertEqual(in_scope, [fin, fwin])
        self.assertEqual(out, [])

    def test_empty_changed_files_fails_open(self):
        f = finding(file="anywhere.py")
        in_scope, out = split_by_scope([f], [])
        self.assertEqual(in_scope, [f])
        self.assertEqual(out, [])

    def test_d16_missing_module_finding_stays_in_scope(self):
        # The missing-dep probe cites the changed importing file, never the
        # phantom module -- file-level scope must not demote it.
        f = finding(file="tracker/summary.py",
                    issue="imports tracker/timeutil.py which does not exist")
        in_scope, out = split_by_scope([f], ["tracker/summary.py"])
        self.assertEqual(in_scope, [f])
        self.assertEqual(out, [])


class TestRescueForbiddenDrops(unittest.TestCase):
    """W13 sentinel: each pattern facet has a rescue case and the nearest
    legitimate-drop lookalike that must NOT be rescued."""

    def test_future_caller_modal_rescues_dead_path(self):
        f = finding(issue="dead path: ENABLE_X flag is False so the branch "
                          "never runs",
                    drop_reason="2/2: speculative -- any future caller could "
                                "pass enable=True directly")
        self.assertEqual(classify_drop(f), "dead-path-future-caller")

    def test_reverse_rule_no_callers_stays_dropped(self):
        # the rule's own legitimate direction: new code gets wired up later
        f = finding(issue="function has no callers anywhere, dead code",
                    drop_reason="2/2: new code gets wired up later; no "
                                "existing definitions preclude a future caller")
        self.assertIsNone(classify_drop(f))

    def test_duplicate_guard_blocks_rescue(self):
        f = finding(issue="dead path: flag is False so branch never runs",
                    drop_reason="2/2: duplicate of finding 1 -- any caller "
                                "could pass frozen=True")
        self.assertEqual(classify_drop(f), "duplicate-guard")
        rescued, still = rescue_forbidden_drops([f])
        self.assertEqual((rescued, still), ([], [f]))

    def test_named_invariant_rescues_numeric(self):
        f = finding(issue="covariance update can lose symmetry and positive "
                          "semi-definiteness across repeated updates",
                    drop_reason="2/2: generic numerical best-practice advice "
                                "about the Joseph form. No concrete failure "
                                "identified in this code")
        self.assertEqual(classify_drop(f), "numeric-invariant")

    def test_invariant_as_context_not_claim_stays_dropped(self):
        # the inv-vs-solve shape: invariant vocabulary as mere context, no
        # claim that anything is lost -- the "X is more robust than Y" DROP
        # is legitimate per the rule text itself (sweep iteration 1)
        f = finding(issue="np.linalg.inv(S) is numerically less stable than "
                          "solve; for the covariance S (positive definite) "
                          "this can amplify floating-point errors",
                    drop_reason="2/2: generic best-practice advice, textbook "
                                "recommendation, no concrete failure")
        self.assertIsNone(classify_drop(f))

    def test_missing_term_rescues_without_loss_verb(self):
        f = finding(issue="the estimator is missing a term: missing term "
                          "for measurement noise in the update",
                    drop_reason="2/2: textbook advice, no concrete failure")
        self.assertEqual(classify_drop(f), "numeric-invariant")

    def test_accumulation_without_invariant_stays_dropped(self):
        # the d16 fsum probe shape: generic-dismissal phrasing but the drop
        # legitimately refutes the mechanism; issue names no invariant
        f = finding(issue="accumulates total as a plain float and could "
                          "accumulate floating-point error",
                    drop_reason="2/2: generic best-practice advice; float64 "
                                "keeps sub-microsecond precision here")
        self.assertIsNone(classify_drop(f))

    def test_division_class_stays_dropped(self):
        # div-zero findings are protected by the docstring evidence rule
        # already; a generic-dismissal drop of one is not sentinel business
        f = finding(issue="division by zero when the list is empty",
                    drop_reason="2/2: generic robustness advice, the list is "
                                "never empty here")
        self.assertIsNone(classify_drop(f))

    def test_rescued_shape_and_partition(self):
        target = finding(line=5, origin="finder2",
                         issue="flag makes the guarded path unreachable",
                         drop_reason="callers may supply a different value")
        junk = finding(line=9, issue="missing docstring",
                       drop_reason="style nit")
        rescued, still = rescue_forbidden_drops([target, junk])
        self.assertEqual(still, [junk])
        self.assertEqual(rescued, [{
            **{k: v for k, v in target.items() if k != "drop_reason"},
            "verification": "uncertain",
            "dissent_reason": "[sentinel:dead-path-future-caller] "
                              "callers may supply a different value",
            "rescue": "dead-path-future-caller",
        }])

    def test_empty_input(self):
        self.assertEqual(rescue_forbidden_drops([]), ([], []))


class TestReconstructCandidates(unittest.TestCase):
    """W13 replay: recover the verifier's exact input from a recorded
    review -- kept-then-dropped with verifier output keys stripped, or the
    candidate_findings recording verbatim when present."""

    def test_strips_verifier_keys_kept_then_dropped(self):
        kept = finding(line=1, verification="confirmed")
        unc = finding(line=2, verification="uncertain",
                      dissent_reason="b1", origin="finder2")
        dropped = finding(line=3, drop_reason="2/2: junk")
        result = {"findings": [kept, unc], "dropped_findings": [dropped]}
        self.assertEqual(reconstruct_candidates(result), [
            finding(line=1),
            finding(line=2, origin="finder2"),   # origin passes through
            finding(line=3),
        ])

    def test_degraded_and_failopen_shapes(self):
        # degraded single-pass: bare drop reasons, no verification keys
        result = {"findings": [finding(line=1)],
                  "dropped_findings": [finding(line=2, drop_reason="why")]}
        self.assertEqual(reconstruct_candidates(result),
                         [finding(line=1), finding(line=2)])
        # fail-open: everything kept, nothing to strip
        result = {"findings": [finding(line=1)], "dropped_findings": []}
        self.assertEqual(reconstruct_candidates(result), [finding(line=1)])

    def test_rescue_key_stripped(self):
        rescued = finding(line=4, verification="uncertain",
                          dissent_reason="[sentinel:x] 2/2: r", rescue="x")
        result = {"findings": [rescued], "dropped_findings": []}
        self.assertEqual(reconstruct_candidates(result), [finding(line=4)])

    def test_candidate_findings_recording_wins_and_is_copied(self):
        recorded = [finding(line=9, origin="finder2")]
        result = {"candidate_findings": recorded,
                  "findings": [finding(line=1, verification="confirmed")],
                  "dropped_findings": [finding(line=2, drop_reason="r")]}
        out = reconstruct_candidates(result)
        self.assertEqual(out, recorded)
        self.assertIsNot(out[0], recorded[0])   # copies, not aliases


if __name__ == "__main__":
    unittest.main()
