import hashlib
import unittest

from code_review_agent.feedback_rules import FeedbackRuleValidationError, normalize_rules


def _rule(rule_id: str) -> dict[str, str]:
    return {
        "rule_id": rule_id,
        "category": " SECURITY ",
        "action": " SUPPRESS ",
        "condition": "  generated file  ",
        "rationale": "  reviewed exception  ",
    }


class FeedbackNormalizationRegressionTests(unittest.TestCase):
    def test_normalizes_and_hashes_canonical_rules(self) -> None:
        normalized, canonical, digest = normalize_rules([_rule("rule-01")])
        self.assertEqual("security", normalized[0]["category"])
        self.assertEqual("suppress", normalized[0]["action"])
        self.assertEqual("generated file", normalized[0]["condition"])
        self.assertEqual(hashlib.sha256(canonical.encode("utf-8")).hexdigest(), digest)

    def test_rejects_casefold_duplicate_ids(self) -> None:
        with self.assertRaises(FeedbackRuleValidationError):
            normalize_rules([_rule("Rule-01"), _rule("rule-01")])


if __name__ == "__main__":
    unittest.main()
