import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV4N07Tests(unittest.TestCase):
    def test_repository_path_rejects_absolute_path(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "patch_path_invalid"):
            gate_b._require_repository_path("patch_path", "/tmp/change.py")


if __name__ == "__main__":
    unittest.main()
