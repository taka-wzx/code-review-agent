import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C05Tests(unittest.TestCase):
    def test_sandbox_patch_rejects_unsupported_mode(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "sandbox_patch_mode_invalid"):
            gate_b.SandboxPatchFile(path="src/example.py", content=b"x = 1\n", mode="100600")


if __name__ == "__main__":
    unittest.main()

