import unittest

from code_review_agent.redaction import contains_forbidden_content


class RedactionHostPathRegressionTests(unittest.TestCase):
    def test_detects_nested_credentials_and_synthetic_host_paths(self) -> None:
        self.assertTrue(contains_forbidden_content({"nested": {"api_key": "synthetic"}}))
        self.assertTrue(contains_forbidden_content({"path": "C:\\synthetic-user\\private"}))
        self.assertTrue(contains_forbidden_content({"path": "/home/synthetic-user/private"}))

    def test_allows_bounded_identifiers(self) -> None:
        self.assertFalse(
            contains_forbidden_content({"organization_id": "org-01", "reason_code": "timeout"})
        )


if __name__ == "__main__":
    unittest.main()
