import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C08Tests(unittest.TestCase):
    def test_ephemeral_text_rejects_oversized_utf8_payload(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "pilot_plan_redaction_failure"):
            gate_b._hash_ephemeral_text("pilot_plan", "abcde", maximum_bytes=4)


if __name__ == "__main__":
    unittest.main()

