import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV4N06Tests(unittest.TestCase):
    def test_branch_rejects_double_slash(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "branch_invalid"):
            gate_b._require_branch("branch", "crag/phase11d//repair")


if __name__ == "__main__":
    unittest.main()
