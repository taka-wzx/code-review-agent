import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV4N04Tests(unittest.TestCase):
    def test_utc_validator_rejects_numeric_offset(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "timestamp_invalid"):
            gate_b._require_utc("timestamp", "2026-08-11T13:30:00+08:00")


if __name__ == "__main__":
    unittest.main()
