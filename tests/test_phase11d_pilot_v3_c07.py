import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C07Tests(unittest.TestCase):
    def test_canonical_json_is_independent_of_mapping_order(self) -> None:
        left = gate_b.canonical_json({"b": 2, "a": 1})
        right = gate_b.canonical_json({"a": 1, "b": 2})
        self.assertEqual(left, right)
        self.assertEqual(left, b'{"a":1,"b":2}')


if __name__ == "__main__":
    unittest.main()

