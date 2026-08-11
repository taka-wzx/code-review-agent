import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV4N08Tests(unittest.TestCase):
    def test_sandbox_patch_file_rejects_empty_content(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "sandbox_patch_content_invalid"):
            gate_b.SandboxPatchFile(path="src/example.py", content=b"")


if __name__ == "__main__":
    unittest.main()
