import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C03Tests(unittest.TestCase):
    def test_loopback_server_rejects_short_bearer_token(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "operator_bearer_token_invalid"):
            gate_b.LoopbackReviewRepairServer(object(), bearer_token="too-short")


if __name__ == "__main__":
    unittest.main()

