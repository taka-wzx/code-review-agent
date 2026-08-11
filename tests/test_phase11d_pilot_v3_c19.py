import base64
import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C19Tests(unittest.TestCase):
    def test_operator_patch_decodes_exact_bytes(self) -> None:
        content = b"value = 1\n"
        files = gate_b._operator_patch_files(
            [{
                "path": "src/example.py",
                "mode": "100644",
                "content_base64": base64.b64encode(content).decode("ascii"),
            }]
        )
        self.assertEqual(files[0].content, content)
        self.assertEqual(files[0].path, "src/example.py")


if __name__ == "__main__":
    unittest.main()

