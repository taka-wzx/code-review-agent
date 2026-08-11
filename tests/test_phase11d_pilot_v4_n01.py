import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV4N01Tests(unittest.TestCase):
    def test_sha256_text_uses_utf8_for_unicode(self) -> None:
        self.assertEqual(
            gate_b.sha256_text("caf\u00e9"),
            "850f7dc43910ff890f8879c0ed26fe697c93a067ad93a7d50f466a7028a9bf4e",
        )


if __name__ == "__main__":
    unittest.main()
