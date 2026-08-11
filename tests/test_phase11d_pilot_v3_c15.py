import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C15Tests(unittest.TestCase):
    def test_review_failure_hashes_terminal_category(self) -> None:
        outcome = gate_b._review_failure("pr-104", "cohort_stopped")
        self.assertEqual(outcome.response_sha256, gate_b.sha256_text("cohort_stopped"))
        self.assertEqual(outcome.provider_call_count, 0)
        self.assertEqual(outcome.http_attempt_count, 0)


if __name__ == "__main__":
    unittest.main()

