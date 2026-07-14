"""Unit tests for repeat_eval's aggregation statistics (previously untested:
the code that adjudicates "variance or real effect" had no coverage)."""
import unittest

from repeat_eval import bootstrap_ci, bug_level_recall_ci, f1, stat_block


class TestF1(unittest.TestCase):
    def test_undefined_slices_are_none(self):
        self.assertIsNone(f1(None, 0.5))
        self.assertIsNone(f1(0.5, None))

    def test_zero_and_normal(self):
        self.assertEqual(f1(0.0, 0.9), 0.0)
        self.assertEqual(f1(1.0, 1.0), 1.0)
        self.assertEqual(f1(0.8, 0.5), round(2 * 0.8 * 0.5 / 1.3, 3))


class TestBootstrapCI(unittest.TestCase):
    def test_deterministic_for_seed(self):
        vals = [0.8, 0.9, 0.85]
        self.assertEqual(bootstrap_ci(vals, seed=0), bootstrap_ci(vals, seed=0))

    def test_needs_two_values(self):
        self.assertIsNone(bootstrap_ci([]))
        self.assertIsNone(bootstrap_ci([0.5]))
        self.assertIsNone(bootstrap_ci([None, 0.5]))

    def test_interval_brackets_the_data(self):
        lo, hi = bootstrap_ci([0.8, 0.9, 0.85], seed=1)
        self.assertGreaterEqual(lo, 0.8)
        self.assertLessEqual(hi, 0.9)
        self.assertLessEqual(lo, hi)

    def test_constant_data_gives_point_interval(self):
        self.assertEqual(bootstrap_ci([0.5, 0.5, 0.5]), (0.5, 0.5))


class TestStatBlock(unittest.TestCase):
    def test_empty_is_all_none(self):
        self.assertEqual(stat_block([None, None]),
                         {"mean": None, "min": None, "max": None,
                          "stdev": None, "ci95": None})

    def test_single_run_has_no_spread(self):
        s = stat_block([0.8])
        self.assertEqual(s["mean"], 0.8)
        self.assertIsNone(s["stdev"])
        self.assertIsNone(s["ci95"])

    def test_multi_run(self):
        s = stat_block([0.8, 0.9])
        self.assertEqual(s["mean"], 0.85)
        self.assertEqual((s["min"], s["max"]), (0.8, 0.9))
        self.assertAlmostEqual(s["stdev"], 0.071, places=3)
        self.assertIsNotNone(s["ci95"])


class TestBugLevelRecallCI(unittest.TestCase):
    def test_resamples_bugs_not_runs(self):
        # 4 bugs x 3 runs; per-bug rates 1, 1, 0, 1/3 -> mean recall .583
        hits = {"b1": [True] * 3, "b2": [True] * 3,
                "b3": [False] * 3, "b4": [True, False, False]}
        lo, hi = bug_level_recall_ci(hits, seed=0)
        self.assertLess(lo, 0.583)
        self.assertGreater(hi, 0.583)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(hi, 1.0)

    def test_too_few_bugs_is_none(self):
        self.assertIsNone(bug_level_recall_ci({"b1": [True]}))
        self.assertIsNone(bug_level_recall_ci({}))


if __name__ == "__main__":
    unittest.main()
