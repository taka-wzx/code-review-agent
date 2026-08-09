import unittest

from code_review_agent.notification_routing import (
    InvalidNotificationInput,
    _require_identifier,
    _require_int,
    _require_reason_code,
)


class NotificationInputRegressionTests(unittest.TestCase):
    def test_accepts_canonical_values(self) -> None:
        self.assertEqual("org-01", _require_identifier("org-01", "organization_id"))
        self.assertEqual("delivery_timeout", _require_reason_code("delivery_timeout"))
        self.assertEqual(2, _require_int(2, "attempt", 1, 3))

    def test_rejects_malformed_values(self) -> None:
        for callback in (
            lambda: _require_identifier("org 01", "organization_id"),
            lambda: _require_reason_code("delivery.timeout"),
            lambda: _require_int(True, "attempt", 1, 3),
        ):
            with self.assertRaises(InvalidNotificationInput):
                callback()


if __name__ == "__main__":
    unittest.main()
