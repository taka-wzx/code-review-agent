import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV4N05Tests(unittest.TestCase):
    def test_stable_id_rejects_whitespace(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "receipt_id_invalid"):
            gate_b._require_stable_id("receipt_id", "receipt 01")


if __name__ == "__main__":
    unittest.main()
