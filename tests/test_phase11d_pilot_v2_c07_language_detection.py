import unittest

from code_review_agent.context import _languages


class LanguageDetectionRegressionTests(unittest.TestCase):
    def test_detects_supported_suffixes_case_insensitively(self) -> None:
        self.assertEqual(
            ("python", "typescript"),
            _languages(["src/A.PY", "ui/view.TsX", "src/b.py", "README.md"]),
        )

    def test_empty_input_has_no_languages(self) -> None:
        self.assertEqual((), _languages([]))


if __name__ == "__main__":
    unittest.main()
