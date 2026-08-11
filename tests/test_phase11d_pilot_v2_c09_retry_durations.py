import unittest

from code_review_agent.worker import _duration


class RetryDurationRegressionTests(unittest.TestCase):
    def test_converts_supported_units(self) -> None:
        self.assertEqual(0.25, _duration("250ms"))
        self.assertEqual(2.5, _duration(" 2.5S "))
        self.assertEqual(120.0, _duration("2m"))
        self.assertEqual(3600.0, _duration("1h"))

    def test_rejects_malformed_durations(self) -> None:
        for value in ("-1s", "1m30s", "soon", ""):
            with self.subTest(value=value):
                self.assertIsNone(_duration(value))


if __name__ == "__main__":
    unittest.main()
