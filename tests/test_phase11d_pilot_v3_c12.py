import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C12Tests(unittest.TestCase):
    def test_candidate_receipt_is_stable_and_hash_bound(self) -> None:
        candidate = gate_b.PullRequestCandidate(
            number=104, github_id=500104, base_branch="master", base_sha="a" * 40,
            head_sha="b" * 40, updated_at_utc="2026-08-11T04:15:00Z",
            selection_rank_sha256="c" * 64,
        )
        first = candidate.receipt_row()
        second = candidate.receipt_row()
        self.assertEqual(first, second)
        self.assertEqual(len(first["snapshot_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()

