import unittest

from code_review_agent.tracelog import _span_token


class TraceSpanTokenRegressionTests(unittest.TestCase):
    def test_sanitizes_and_bounds_span_tokens(self) -> None:
        token = _span_token("../unsafe path/$segment" + "x" * 120)
        self.assertLessEqual(len(token), 96)
        self.assertNotIn(" ", token)
        self.assertNotIn("$", token)
        self.assertIn("/", token)
        self.assertFalse(token.startswith("."))

    def test_uses_fallback_when_nothing_remains(self) -> None:
        self.assertEqual("fallback", _span_token("...", "fallback"))


if __name__ == "__main__":
    unittest.main()
