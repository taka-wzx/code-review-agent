import unittest

from code_review_agent.github_installation import _positive_id


class InstallationIdRegressionTests(unittest.TestCase):
    def test_accepts_positive_integer(self) -> None:
        self.assertEqual(42, _positive_id("installation_id", 42))

    def test_rejects_non_positive_and_bool_values(self) -> None:
        for value in (True, False, 0, -1, "42"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _positive_id("installation_id", value)


if __name__ == "__main__":
    unittest.main()
