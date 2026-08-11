import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C01Tests(unittest.TestCase):
    def test_ephemeral_finding_rejects_private_key_marker(self) -> None:
        marker = "-----BEGIN PRIVATE " + "KEY-----"
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "redaction_failure"):
            gate_b.EphemeralReviewFinding(
                pr_id="pr-104",
                finding_id="a" * 64,
                index=1,
                title="Leaked key material",
                severity="high",
                path="src/example.py",
                line=1,
                description=marker,
                response_sha256="b" * 64,
            )


if __name__ == "__main__":
    unittest.main()
