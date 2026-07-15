"""Focused regression tests for proactive repository context retrieval."""
import tempfile
import unittest
from pathlib import Path

from code_review_agent.context import build_context


def _diff(path: str, body: str = "+def changed():\n+    return helper.VALUE\n") -> str:
    return (f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,2 @@\n"
            "-OLD = 1\n" + body)


class TestImportPrefetchLayouts(unittest.TestCase):
    def test_flat_layout_module_is_prefetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "mod.py").write_text(
                "import helper\ndef changed():\n    return helper.VALUE\n",
                encoding="utf-8",
            )
            (repo / "helper.py").write_text("VALUE = 2\n", encoding="utf-8")

            pack = build_context(_diff("mod.py"), repo)

        self.assertIn("## Imported module: helper.py (imported by mod.py)", pack)
        self.assertIn("VALUE = 2", pack)

    def test_src_layout_package_module_is_prefetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            package = repo / "src" / "pkg"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "mod.py").write_text(
                "from pkg.helper import VALUE\ndef changed():\n    return VALUE\n",
                encoding="utf-8",
            )
            (package / "helper.py").write_text("VALUE = 3\n", encoding="utf-8")

            pack = build_context(
                _diff("src/pkg/mod.py", "+def changed():\n+    return VALUE\n"),
                repo,
            )

        self.assertIn(
            "## Imported module: src/pkg/helper.py (imported by src/pkg/mod.py)",
            pack,
        )
        self.assertIn("VALUE = 3", pack)

    def test_src_layout_missing_internal_module_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            package = repo / "src" / "pkg"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "mod.py").write_text(
                "from pkg.missing import VALUE\ndef changed():\n    return VALUE\n",
                encoding="utf-8",
            )

            pack = build_context(
                _diff("src/pkg/mod.py", "+def changed():\n+    return VALUE\n"),
                repo,
            )

        self.assertIn("## Import note", pack)
        self.assertIn("`pkg.missing` is imported by src/pkg/mod.py", pack)
        self.assertIn("`src/pkg/missing.py` does not exist", pack)

    def test_external_import_does_not_create_missing_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            package = repo / "src" / "pkg"
            package.mkdir(parents=True)
            (package / "mod.py").write_text(
                "import definitely_external\ndef changed():\n    return 1\n",
                encoding="utf-8",
            )

            pack = build_context(
                _diff("src/pkg/mod.py", "+def changed():\n+    return 1\n"),
                repo,
            )

        self.assertNotIn("## Import note", pack)


if __name__ == "__main__":
    unittest.main()
