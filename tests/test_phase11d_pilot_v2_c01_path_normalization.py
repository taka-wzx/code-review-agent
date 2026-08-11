import unittest

from code_review_agent.repair_approval import normalize_repo_paths


class PathNormalizationRegressionTests(unittest.TestCase):
    def test_normalizes_separators_and_sorts_paths(self) -> None:
        self.assertEqual(
            ("docs/Guide.md", "src/pkg/module.py"),
            normalize_repo_paths(["src\\pkg\\module.py", "docs/Guide.md"]),
        )

    def test_rejects_traversal_and_casefold_aliases(self) -> None:
        with self.assertRaises(ValueError):
            normalize_repo_paths(["../escape.py"])
        with self.assertRaises(ValueError):
            normalize_repo_paths(["src/a.py", "SRC/A.py"])


if __name__ == "__main__":
    unittest.main()
