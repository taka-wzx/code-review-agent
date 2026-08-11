import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C13Tests(unittest.TestCase):
    def test_review_budget_rejects_bool_as_integer(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "review_budget_max_logical_calls_invalid"):
            gate_b.ReviewBudget(
                max_logical_calls=True, max_http_attempts=1, max_input_tokens=1,
                max_output_tokens=1, max_cached_tokens=0, max_micro_cny=1,
                max_wall_clock_seconds=1,
            )


if __name__ == "__main__":
    unittest.main()

