"""Unit tests for the pure validation/merge/metrics functions that the
docstrings advertise as offline-testable. Locks the eval-side behavior the
refactor must not disturb (judge.py's loop itself is out of scope).
"""
import json
import tempfile
import unittest
from pathlib import Path

from code_review_agent.agent import validate_review
from cost_report import billed_cost, collect, iter_trace_files, run_stats
from code_review_agent.findings import dedup_union, is_duplicate, similarity, split_by_scope
from judge import compute_metrics, validate_verdict
from replay_verifier import reconstruct_candidates
from code_review_agent.sentinels import classify_drop, rescue_forbidden_drops
from code_review_agent.verifier import (apply_verdicts, merge_verdicts,
                                        validate_verdicts)


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
        self.assertEqual(classify_drop(f), "dead-path-dismissed")

    def test_scaffold_reason_rescues_config_disabled_dead_path(self):
        # bench round 1: the live drop dropped the modal phrasing for
        # "intentional scaffolding, not wired yet"
        f = finding(issue="PREDICT_FROZEN=True and FREEZE_ON_COMMIT=False so "
                          "draw_predicted is unreachable dead code",
                    drop_reason="2/2: intentional design; the freeze wiring "
                                "isn't wired yet, normal scaffolding")
        self.assertEqual(classify_drop(f), "dead-path-dismissed")

    def test_reverse_rule_no_callers_stays_dropped(self):
        # the rule's own legitimate direction: new code gets wired up later.
        # The issue is pure no-callers (no config VALUE), so the tightened
        # issue gate keeps it dropped even though the reason matches.
        f = finding(issue="function has no callers anywhere, dead code",
                    drop_reason="2/2: new code gets wired up later; no "
                                "existing definitions preclude a future caller")
        self.assertIsNone(classify_drop(f))

    def test_scaffold_reason_on_pure_no_callers_stays_dropped(self):
        # the exact live-phrasing danger: scaffolding reason on a no-callers
        # issue must NOT rescue (the issue gate is the discriminator)
        f = finding(issue="draw_helper has zero callers anywhere; dead code "
                          "that will never execute",
                    drop_reason="2/2: newly added function, intentional "
                                "scaffolding, not wired yet")
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

    def test_speculative_robustness_rescues_numeric(self):
        # bench round 1: live drop said "speculative robustness / no concrete
        # defect" rather than "generic robustness / no concrete failure"
        f = finding(issue="the covariance can lose symmetry and positive "
                          "definiteness over repeated updates",
                    drop_reason="2/2: speculative robustness advice; no "
                                "concrete defect identified")
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

    def test_tuning_dismissal_rescues_documented_condition(self):
        # W14 full r3-d5: the caller DOCUMENTS the short-segment regime; the
        # drop calls it a tuning observation and praises the guard
        f = finding(issue="BOOTSTRAP_WINDOW = 5, but the caller in "
                          "tracker/ingest.py:22-23 documents that serve-toss "
                          "segments frequently end after only 2-4 samples, so "
                          "bootstrap_velocity returns None for the common case",
                    drop_reason="2/2: This is a parameter-tuning observation, "
                                "not a concrete defect. The code handles short "
                                "segments gracefully by returning None")
        self.assertEqual(classify_drop(f), "doc-condition-dismissed")

    def test_no_evidence_dismissal_rescues_documented_condition(self):
        # W14 slice r1-d6: "no evidence the scenario occurs" against an issue
        # that cites the constant's own comment asserting the scenario
        f = finding(issue="record_bounce accepts asr but discards it. The "
                          "constant ACQUIRE_MIN_STATE = 4 (line 3) documents "
                          "that asr >= 4 means the ball is confirmed in "
                          "flight; without the gate low-asr detections are "
                          "recorded as bounces",
                    drop_reason="Speculative: no evidence that the detector "
                                "actually produces low-asr bounce events; a "
                                "robustness suggestion rather than a concrete "
                                "defect")
        self.assertEqual(classify_drop(f), "doc-condition-dismissed")

    def test_correctly_returns_rescues_caller_comment_citation(self):
        # w11r3-d5: "correctly returns None" locally-correct reasoning against
        # a caller-comment citation ("notes that ...")
        f = finding(issue="BOOTSTRAP_WINDOW=5 but the caller comment in "
                          "ingest.py:24 notes that 'serve-toss segments "
                          "frequently end after only 2-4 samples' -- most "
                          "segments get None and no initial velocity is set",
                    drop_reason="2/2: This is a parameter tuning suggestion, "
                                "not a concrete defect. The code correctly "
                                "returns None when samples are insufficient")
        self.assertEqual(classify_drop(f), "doc-condition-dismissed")

    def test_future_proofing_dismissal_rescues_constant_comment(self):
        # w10r3-d6: quoted constant comment in the issue; the drop hand-waves
        # "no evidence / speculative future-proofing / not a defect"
        f = finding(issue='ACQUIRE_MIN_STATE = 4 with comment "asr >= 4 means '
                          'the ball is confirmed in flight" strongly suggests '
                          "asr carries confidence downstream consumers need",
                    drop_reason="2/2: no evidence that discarding it causes "
                                "any concrete failure. Speculative "
                                "future-proofing advice, not a defect")
        self.assertEqual(classify_drop(f), "doc-condition-dismissed")

    def test_missing_doc_nit_stays_dropped(self):
        # nearest lookalike: docstring-nit issues say docs are ABSENT
        # ("does not specify"); the gate needs a positive citation
        f = finding(issue="the docstring does not specify the expected format "
                          "of pred_polyline",
                    drop_reason="2/2: docstring omission, not a concrete "
                                "defect")
        self.assertIsNone(classify_drop(f))

    def test_robustness_without_citation_stays_dropped(self):
        # legit speculative-robustness kill: no documentation cited at all
        f = finding(issue="No None guard for pred_polyline; if a caller "
                          "passes None the slice expression crashes",
                    drop_reason="2/2: Speculative robustness advice with no "
                                "evidence that None is ever passed")
        self.assertIsNone(classify_drop(f))

    def test_substantive_doc_rebuttal_stays_dropped(self):
        # a REAL rebuttal may itself cite docs in the reason; without the
        # forbidden dismissal motifs it must stay dropped
        f = finding(issue="the guard treats missing actual_y as possible, "
                          "but the comment in report.py states every throw "
                          "record carries both keys",
                    drop_reason="2/2: the schema documented in report.py "
                                "guarantees both keys are present, so the "
                                "guarded scenario cannot occur; the guard is "
                                "merely redundant")
        self.assertIsNone(classify_drop(f))

    def test_negated_citation_stays_dropped(self):
        # sweep iteration 2 (w14r3-d11): "does not document that X" is a
        # missing-doc nit wearing citation clothing -- must not rescue
        f = finding(issue="The fit_cor docstring states the fit is 'forced "
                          "through the origin' but does not document that "
                          "the returned value is COR = -slope",
                    drop_reason="2/2: Documentation style nit: a clarity "
                                "improvement, not a defect. The math is "
                                "correct")
        self.assertIsNone(classify_drop(f))

    def test_doc_condition_duplicate_guard(self):
        f = finding(issue="the caller comment in ingest.py notes that "
                          "segments are 2-4 samples, so the guard swallows "
                          "the common case",
                    drop_reason="2/2: duplicate of finding 0; also just a "
                                "tuning suggestion, not a concrete defect")
        self.assertEqual(classify_drop(f), "duplicate-guard")

    def test_rescued_shape_and_partition(self):
        target = finding(line=5, origin="finder2",
                         issue="the FROZEN flag is False so the guarded path "
                               "never runs",
                         drop_reason="callers may supply a different value")
        junk = finding(line=9, issue="missing docstring",
                       drop_reason="style nit")
        rescued, still = rescue_forbidden_drops([target, junk])
        self.assertEqual(still, [junk])
        self.assertEqual(rescued, [{
            **{k: v for k, v in target.items() if k != "drop_reason"},
            "verification": "uncertain",
            "dissent_reason": "[sentinel:dead-path-dismissed] "
                              "callers may supply a different value",
            "rescue": "dead-path-dismissed",
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


class TestBilledCost(unittest.TestCase):
    def test_no_prices_means_no_cost(self):
        self.assertIsNone(billed_cost(100, 10, 50, None, None))
        self.assertIsNone(billed_cost(100, 10, 50, 1.0, None))

    def test_price_hit_defaults_to_no_discount(self):
        # hits billed like misses -> identical to the pre-W14 formula
        self.assertAlmostEqual(
            billed_cost(1_000_000, 0, 500_000, 2.0, 8.0), 2.0)

    def test_hit_discount(self):
        # 90% hit at 1/10 price -> effective input factor 0.19
        self.assertAlmostEqual(
            billed_cost(1_000_000, 0, 900_000, 1.0, 4.0, price_hit=0.1),
            0.19)

    def test_output_never_discounted(self):
        self.assertAlmostEqual(
            billed_cost(0, 1_000_000, 0, 1.0, 4.0, price_hit=0.1), 4.0)

    def test_all_hit_and_zero_hit_edges(self):
        self.assertAlmostEqual(
            billed_cost(1_000_000, 0, 1_000_000, 1.0, 4.0, price_hit=0.1),
            0.1)
        self.assertAlmostEqual(
            billed_cost(1_000_000, 0, 0, 1.0, 4.0, price_hit=0.1), 1.0)


class TestCostReportCacheAggregation(unittest.TestCase):
    """Traces mixing W13+ events (cache fields) with older ones (none):
    only recorded hits may earn the discount; old events bill as all-miss."""

    EVENTS = [
        {"kind": "llm_response", "component": "finder", "step": 1,
         "tokens_in": 100, "tokens_out": 10, "cache_hit": 80, "cache_miss": 20},
        {"kind": "llm_response", "component": "finder", "step": 2,
         "tokens_in": 50, "tokens_out": 5},          # pre-W13 event
        {"kind": "llm_response", "component": "finder", "step": 3,
         "tokens_in": 40, "tokens_out": 4, "cache_hit": 40, "cache_miss": 0},
        {"kind": "tool", "component": "finder", "tool": "read_file",
         "result_chars": 1000},
    ]

    def write_trace(self, tmp):
        (Path(tmp) / "d1_demo.jsonl").write_text(
            "\n".join(json.dumps(e) for e in self.EVENTS) + "\n",
            encoding="utf-8")

    def test_run_stats_splits_hit_and_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_trace(tmp)
            d = run_stats(tmp)["finder"]
        self.assertEqual(d["tokens_in"], 190)
        self.assertEqual(d["cache_hit"], 120)
        self.assertEqual(d["cache_seen_in"], 140)    # only events with fields
        # billed miss volume = 190-120 = 70: the 20 recorded misses plus
        # the 50 old-style tokens billed conservatively as misses
        self.assertAlmostEqual(
            billed_cost(d["tokens_in"], d["tokens_out"], d["cache_hit"],
                        1.0, 0.0, price_hit=0.0), 70 / 1e6)

    def test_collect_accumulates_cache_hit_per_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_trace(tmp)
            per_diff = collect(iter_trace_files([tmp]))
        d = per_diff["d1_demo"]["finder"]
        self.assertEqual(d["tokens_in"], 190)
        self.assertEqual(d["cache_hit"], 120)
        self.assertEqual(d["calls"], 3)


if __name__ == "__main__":
    unittest.main()
