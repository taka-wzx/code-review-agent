import math
import unittest

from code_review_agent.artifact_retention import ArtifactRetentionError, _timestamp


class RetentionTimestampRegressionTests(unittest.TestCase):
    def test_normalizes_integer_timestamp(self) -> None:
        self.assertEqual(123.0, _timestamp(123))

    def test_rejects_non_finite_and_non_numeric_values(self) -> None:
        for value in (True, "123", math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ArtifactRetentionError):
                _timestamp(value)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
