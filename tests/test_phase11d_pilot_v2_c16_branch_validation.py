import unittest

from code_review_agent.repair_publish import _branch


class BranchValidationRegressionTests(unittest.TestCase):
    def test_accepts_namespaced_repair_branch(self) -> None:
        self.assertEqual("repair/run-01", _branch("head_branch", "repair/run-01"))

    def test_rejects_ambiguous_branch_names(self) -> None:
        for value in ("repair..run", "repair//run", "repair/.hidden", "repair.lock"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _branch("head_branch", value)


if __name__ == "__main__":
    unittest.main()
