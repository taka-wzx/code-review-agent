import math
import unittest

from code_review_agent.production_metrics import _histogram_lines


class MetricsHistogramRegressionTests(unittest.TestCase):
    def test_filters_invalid_samples_and_renders_totals(self) -> None:
        lines = _histogram_lines("latency", [0.5, -1.0, math.nan, math.inf, 2.0], [1.0])
        self.assertEqual(
            [
                'latency_bucket{le="1"} 1',
                'latency_bucket{le="+Inf"} 2',
                "latency_sum 2.5",
                "latency_count 2",
            ],
            lines,
        )


if __name__ == "__main__":
    unittest.main()
