import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV4N03Tests(unittest.TestCase):
    def test_integer_validator_rejects_boolean(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "budget_invalid"):
            gate_b._require_int("budget", True)


if __name__ == "__main__":
    unittest.main()
