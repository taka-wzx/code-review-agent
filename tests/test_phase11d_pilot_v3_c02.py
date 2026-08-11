import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C02Tests(unittest.TestCase):
    def test_operator_patch_rejects_invalid_base64(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "operator_patch_content_invalid"):
            gate_b._operator_patch_files(
                [{"path": "src/example.py", "mode": "100644", "content_base64": "%%%"}]
            )


if __name__ == "__main__":
    unittest.main()

