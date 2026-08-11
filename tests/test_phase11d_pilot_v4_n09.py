import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV4N09Tests(unittest.TestCase):
    def test_sandbox_patch_file_rejects_symlink_mode(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "sandbox_patch_mode_invalid"):
            gate_b.SandboxPatchFile(path="src/example.py", content=b"target\n", mode="120000")


if __name__ == "__main__":
    unittest.main()
