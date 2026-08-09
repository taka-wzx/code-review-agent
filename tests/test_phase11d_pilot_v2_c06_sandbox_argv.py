import unittest

from code_review_agent.sandbox import SandboxPolicyError, _validate_argv


class SandboxArgvRegressionTests(unittest.TestCase):
    def test_accepts_safe_argument_sequence(self) -> None:
        self.assertEqual(("python", "-m", "unittest"), _validate_argv(["python", "-m", "unittest"]))

    def test_rejects_shells_inline_code_and_malformed_values(self) -> None:
        for value, error in (
            ("python -m unittest", ValueError),
            (["python", "bad\x00arg"], ValueError),
            (["pwsh", "-File", "test.ps1"], SandboxPolicyError),
            (["python", "-c", "print(1)"], SandboxPolicyError),
        ):
            with self.subTest(value=value), self.assertRaises(error):
                _validate_argv(value)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
