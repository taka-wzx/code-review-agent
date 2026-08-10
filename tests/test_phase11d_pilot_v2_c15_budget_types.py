import math
import unittest

from code_review_agent.repair_budget import BudgetLimits


class BudgetTypeRegressionTests(unittest.TestCase):
    def test_defaults_are_valid(self) -> None:
        self.assertGreater(BudgetLimits().total_tokens, 0)

    def test_rejects_bool_and_non_finite_limits(self) -> None:
        for kwargs in (
            {"total_tokens": True},
            {"repair_attempts": False},
            {"total_seconds": math.inf},
            {"command_output_bytes": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BudgetLimits(**kwargs)


if __name__ == "__main__":
    unittest.main()
