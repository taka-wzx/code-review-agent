import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C14Tests(unittest.TestCase):
    def test_secret_detector_matches_github_pat_shape(self) -> None:
        fine_grained = "github_pat_" + "A" * 24
        classic = "ghp_" + "B" * 36
        self.assertTrue(gate_b._contains_secret_like_content(fine_grained))
        self.assertTrue(gate_b._contains_secret_like_content(classic))
        self.assertFalse(gate_b._contains_secret_like_content("ordinary review text"))

    def test_secret_detector_allows_plain_sha256_digest(self) -> None:
        self.assertFalse(gate_b._contains_secret_like_content("a" * 64))


if __name__ == "__main__":
    unittest.main()

