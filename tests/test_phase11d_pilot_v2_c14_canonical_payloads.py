import math
import unittest

from code_review_agent.approval_publish import canonical_json


class CanonicalPayloadRegressionTests(unittest.TestCase):
    def test_serializes_deterministically_as_ascii(self) -> None:
        self.assertEqual(b'{"a":"\\u6d4b\\u8bd5","z":1}', canonical_json({"z": 1, "a": "测试"}))

    def test_rejects_non_finite_numbers(self) -> None:
        with self.assertRaises(ValueError):
            canonical_json({"value": math.nan})


if __name__ == "__main__":
    unittest.main()
