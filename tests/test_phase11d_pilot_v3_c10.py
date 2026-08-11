import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C10Tests(unittest.TestCase):
    def test_repository_path_rejects_parent_traversal(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "candidate_path_invalid"):
            gate_b._require_repository_path("candidate_path", "../secret.txt")


if __name__ == "__main__":
    unittest.main()

