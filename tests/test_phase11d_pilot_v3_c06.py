import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C06Tests(unittest.TestCase):
    def test_draft_receipt_rejects_ready_state(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "draft_pr_boundary_violation"):
            gate_b.DraftPublicationReceipt(
                authorization_id="auth-007", authorization_sha256="a" * 64,
                repository_id="repo-1", repair_job_id="repair-1", pr_id="pr-104",
                draft_pr_id="draft-pr-104", head_branch="crag/phase11d/repair-1",
                base_branch="master", commit_sha="b" * 40, payload_sha256="c" * 64,
                publisher_status="draft_published", state="receipt_reconciled", ready=True,
            )


if __name__ == "__main__":
    unittest.main()

