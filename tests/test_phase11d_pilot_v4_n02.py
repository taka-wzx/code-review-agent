import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV4N02Tests(unittest.TestCase):
    def test_canonical_json_rejects_nan(self) -> None:
        with self.assertRaises(ValueError):
            gate_b.canonical_json({"value": float("nan")})


if __name__ == "__main__":
    unittest.main()
