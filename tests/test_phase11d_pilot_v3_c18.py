import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C18Tests(unittest.TestCase):
    def test_operator_patch_set_cannot_be_empty(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "operator_patch_files_invalid"):
            gate_b._operator_patch_files([])

    def test_operator_patch_set_requires_json_array(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "operator_patch_files_invalid"):
            gate_b._operator_patch_files(())


if __name__ == "__main__":
    unittest.main()

