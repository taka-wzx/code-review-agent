import unittest

from code_review_agent.repair_checkpoint import _require_valid_run_id, secrets_compare


class CheckpointIdentifierRegressionTests(unittest.TestCase):
    def test_accepts_portable_run_id(self) -> None:
        self.assertEqual("run-01.part_a", _require_valid_run_id("run-01.part_a"))

    def test_rejects_windows_aliases_and_trailing_dot(self) -> None:
        for value in ("con", "aux.log", "run."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _require_valid_run_id(value)

    def test_checksum_comparison_distinguishes_values(self) -> None:
        self.assertTrue(secrets_compare("a" * 64, "a" * 64))
        self.assertFalse(secrets_compare("a" * 64, "b" * 64))


if __name__ == "__main__":
    unittest.main()
