import unittest

from code_review_agent.github_review import pr_api_path


class PrUrlRoutingRegressionTests(unittest.TestCase):
    def test_routes_numeric_and_explicit_urls(self) -> None:
        self.assertEqual("repos/{owner}/{repo}/pulls/7/reviews", pr_api_path("7"))
        self.assertEqual(
            "repos/acme/widget/pulls/7/reviews",
            pr_api_path("https://github.com/acme/widget/pull/7"),
        )

    def test_rejects_lookalike_hosts_and_non_pr_paths(self) -> None:
        for value in (
            "https://github.com.evil/acme/widget/pull/7",
            "https://github.com/acme/widget/issues/7",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pr_api_path(value)


if __name__ == "__main__":
    unittest.main()
