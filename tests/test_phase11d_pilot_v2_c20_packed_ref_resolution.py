import tempfile
import unittest
from pathlib import Path

from code_review_agent.context_memory import repository_source_sha


class PackedRefResolutionRegressionTests(unittest.TestCase):
    def test_resolves_worktree_pointer_from_packed_refs(self) -> None:
        expected = "A" * 40
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            checkout = root / "checkout"
            metadata = root / "metadata"
            checkout.mkdir()
            metadata.mkdir()
            (checkout / ".git").write_text("gitdir: ../metadata\n", encoding="utf-8")
            (metadata / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
            (metadata / "packed-refs").write_text(
                f"{expected} refs/heads/main\n", encoding="ascii"
            )
            self.assertEqual(expected.casefold(), repository_source_sha(checkout))

            (metadata / "packed-refs").write_text("", encoding="ascii")
            self.assertIsNone(repository_source_sha(checkout))


if __name__ == "__main__":
    unittest.main()
