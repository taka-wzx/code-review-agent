import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C09Tests(unittest.TestCase):
    def test_branch_rejects_parent_traversal(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "candidate_branch_invalid"):
            gate_b._require_branch("candidate_branch", "feature/../master")


if __name__ == "__main__":
    unittest.main()

