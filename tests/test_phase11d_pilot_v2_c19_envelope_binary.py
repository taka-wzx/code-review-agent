import unittest

from code_review_agent.artifact_store import (
    ArtifactStoreError,
    _decode_binary,
    _require_object_id,
)


class EnvelopeBinaryRegressionTests(unittest.TestCase):
    def test_decodes_canonical_base64(self) -> None:
        self.assertEqual(b"binary", _decode_binary("YmluYXJ5", "ciphertext"))

    def test_rejects_malformed_binary_and_object_ids(self) -> None:
        for value in ("", "***", "测试"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _decode_binary(value, "ciphertext")
        with self.assertRaises(ArtifactStoreError):
            _require_object_id("../escape")


if __name__ == "__main__":
    unittest.main()
