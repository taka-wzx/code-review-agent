import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C04Tests(unittest.TestCase):
    def test_cached_tokens_cannot_exceed_input_tokens(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "provider_usage_ambiguity"):
            gate_b.review_cost_micro_cny(input_tokens=1, output_tokens=0, cached_tokens=2)


if __name__ == "__main__":
    unittest.main()

