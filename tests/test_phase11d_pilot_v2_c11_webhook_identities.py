import unittest

from code_review_agent.github_webhook import _delivery_id, _event, _positive_int
from code_review_agent.service_core import InvalidRequest


class WebhookIdentityRegressionTests(unittest.TestCase):
    def test_accepts_canonical_values(self) -> None:
        self.assertEqual("pull_request", _event("pull_request"))
        self.assertEqual("delivery-01", _delivery_id("delivery-01"))
        self.assertEqual(7, _positive_int(7, "installation ID"))

    def test_rejects_malformed_values(self) -> None:
        for callback in (
            lambda: _event("pull request"),
            lambda: _delivery_id(" delivery-01"),
            lambda: _positive_int(True, "installation ID"),
        ):
            with self.assertRaises(InvalidRequest):
                callback()


if __name__ == "__main__":
    unittest.main()
