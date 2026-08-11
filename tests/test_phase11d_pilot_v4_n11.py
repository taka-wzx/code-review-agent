import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV4N11Tests(unittest.TestCase):
    def test_ephemeral_text_rejects_empty_value(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "repair_plan_invalid"):
            gate_b._hash_ephemeral_text("repair_plan", "", maximum_bytes=64)


if __name__ == "__main__":
    unittest.main()
