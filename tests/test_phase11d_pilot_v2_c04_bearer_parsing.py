import unittest

from code_review_agent.identity import AuthenticationRequired, _bearer_token


class BearerParsingRegressionTests(unittest.TestCase):
    def test_scheme_is_case_insensitive(self) -> None:
        self.assertEqual("opaque-test-token", _bearer_token("bEaReR opaque-test-token"))

    def test_missing_or_wrong_scheme_fails_closed(self) -> None:
        for value in (None, "Bearer", "Basic opaque-test-token"):
            with self.subTest(value=value), self.assertRaisesRegex(
                AuthenticationRequired, "authentication required"
            ):
                _bearer_token(value)


if __name__ == "__main__":
    unittest.main()
