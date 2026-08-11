import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV4N10Tests(unittest.TestCase):
    def test_single_input_token_uses_frozen_tariff(self) -> None:
        self.assertEqual(
            gate_b.review_cost_micro_cny(input_tokens=1, output_tokens=0, cached_tokens=0),
            8,
        )


if __name__ == "__main__":
    unittest.main()
