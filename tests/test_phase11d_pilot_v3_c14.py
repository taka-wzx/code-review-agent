import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C14Tests(unittest.TestCase):
    def test_secret_detector_matches_github_pat_shape(self) -> None:
        value = "github_pat-" + "A" * 24
        self.assertTrue(gate_b._contains_secret_like_content(value))
        self.assertFalse(gate_b._contains_secret_like_content("ordinary review text"))


if __name__ == "__main__":
    unittest.main()

