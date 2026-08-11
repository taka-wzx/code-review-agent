import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C16Tests(unittest.TestCase):
    def test_operator_view_preserves_validated_fields(self) -> None:
        finding = gate_b.EphemeralReviewFinding(
            pr_id="pr-104", finding_id="a" * 64, index=2, title="Boundary defect",
            severity="medium", path="src/example.py", line=7,
            description="The branch omits a required validation.", response_sha256="b" * 64,
        )
        view = finding.operator_view()
        self.assertEqual(view["finding_id"], finding.finding_id)
        self.assertEqual(view["line"], 7)
        self.assertEqual(view["severity"], "medium")


if __name__ == "__main__":
    unittest.main()

