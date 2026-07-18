from __future__ import annotations

import math
from pathlib import Path
import unittest

from code_review_agent.redaction import (
    MAX_ARRAY_ITEMS,
    MAX_ATTRIBUTES,
    MAX_NESTED_DEPTH,
    MAX_STRING_CHARACTERS,
    contains_forbidden_content,
    sanitize_attributes,
    sanitize_value,
)


class ExplosiveString:
    def __str__(self):
        raise AssertionError("redaction must not stringify unknown objects")


class TestRedaction(unittest.TestCase):
    def test_sensitive_keys_and_values_are_removed_before_serialization(self):
        result = sanitize_attributes(
            {
                "safe": "ok",
                "api_key": "not-retained",
                "args": {"path": ".env"},
                "nested": {
                    "authorization": "Bearer super-secret-value",
                    "detail": "W6_CANARY_case-1",
                },
                "absolute": r"C:\Users\person\.ssh\id_rsa",
                "relative": Path("src/module.py"),
            }
        )
        self.assertEqual(result.value["safe"], "ok")
        self.assertNotIn("api_key", result.value)
        self.assertNotIn("args", result.value)
        self.assertEqual(result.value["nested"], "[REDACTED:split-secret]")
        self.assertEqual(result.value["absolute"], "[OMITTED:absolute-path]")
        self.assertEqual(result.value["relative"], "src/module.py")
        self.assertGreaterEqual(result.redaction_count, 4)
        self.assertFalse(contains_forbidden_content(result.value))

    def test_content_fields_are_hard_disabled(self):
        result = sanitize_attributes(
            {
                "gen_ai.input.messages": ["do not retain"],
                "gen_ai.output.messages": ["do not retain"],
                "gen_ai.system_instructions": "do not retain",
                "gen_ai.tool.call.arguments": "{}",
                "gen_ai.tool.call.result": "value",
                "gen_ai.request.model": "model-v1",
            }
        )
        self.assertEqual(result.value, {"gen_ai.request.model": "model-v1"})
        self.assertEqual(result.redaction_count, 5)

    def test_limits_are_deterministic_and_audited(self):
        result = sanitize_attributes(
            {f"k{i}": i for i in range(MAX_ATTRIBUTES + 5)}
        )
        self.assertEqual(len(result.value), MAX_ATTRIBUTES)
        self.assertEqual(result.omitted_count, 5)
        self.assertTrue(result.truncated)

        long_text = sanitize_value("x" * (MAX_STRING_CHARACTERS + 10))
        self.assertEqual(len(long_text.value), MAX_STRING_CHARACTERS)
        self.assertTrue(long_text.truncated)

        long_array = sanitize_value(list(range(MAX_ARRAY_ITEMS + 3)))
        self.assertEqual(len(long_array.value), MAX_ARRAY_ITEMS)
        self.assertEqual(long_array.omitted_count, 3)
        self.assertTrue(long_array.truncated)

    def test_depth_controls_and_control_characters(self):
        value = "leaf"
        for _ in range(MAX_NESTED_DEPTH + 2):
            value = {"child": value}
        result = sanitize_value(value)
        self.assertIn("[OMITTED:max-depth]", repr(result.value))
        self.assertTrue(result.truncated)
        self.assertGreater(result.omitted_count, 0)
        normalized = sanitize_value("line1\r\nline2\t\x00")
        self.assertEqual(normalized.value, "line1 line2 \ufffd")
        self.assertEqual(sanitize_value("safe\u200btext").value, "safe\ufffdtext")

    def test_unknown_nonfinite_bytes_and_sets_fail_to_metadata(self):
        self.assertEqual(sanitize_attributes({"unknown": None}).value, {})
        self.assertEqual(sanitize_value(ExplosiveString()).value, "[OMITTED:ExplosiveString]")
        self.assertEqual(sanitize_value(b"secret").value, "[OMITTED:bytes:6]")
        self.assertEqual(sanitize_value({3, 1, 2}).value, "[OMITTED:set]")
        self.assertEqual(sanitize_value(math.inf).value, "[OMITTED:non-finite]")

    def test_secret_shapes_are_not_retained(self):
        samples = [
            "Bearer abcdefghijklmnop",
            "sk-example-token-123",
            "ghp_abcdefghijklmnopqrstuvwxyz",
            "AKIAABCDEFGHIJKLMNOP",
            "password=hunter2",
            "https://user:password@example.invalid/path",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "s\u200bk-example-token-123",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(sanitize_value(sample).value, "[REDACTED]")

    def test_split_secrets_and_sensitive_paths_are_omitted(self):
        split_list = sanitize_value(["sk-example-", "token-123"])
        split_mapping = sanitize_value({"one": "Bearer ", "two": "abcdefghijkl"})
        split_attributes = sanitize_attributes(
            {"one": "sk-example-", "two": "token-123"}
        )
        self.assertEqual(split_list.value, "[REDACTED:split-secret]")
        self.assertEqual(split_mapping.value, "[REDACTED:split-secret]")
        self.assertEqual(split_attributes.value, {})

        for path in (".env", "config/.env.production", ".ssh/id_rsa"):
            with self.subTest(path=path):
                self.assertEqual(
                    sanitize_value(path).value,
                    "[OMITTED:sensitive-path]",
                )
        self.assertEqual(sanitize_value(".env.example").value, ".env.example")

    def test_all_posix_absolute_path_shapes_are_omitted_and_rejected(self):
        paths = (
            "/usr/lib/python3.12/site-packages",
            "/bin/bash",
            "/sbin/init",
            "/lib64/ld-linux-x86-64.so.2",
            "/run/media/person/disk",
            "/media/person/disk",
            "/snap/package/current",
            "/boot/config",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(
                    sanitize_value(path).value,
                    "[OMITTED:absolute-path]",
                )
                self.assertTrue(contains_forbidden_content({"path_value": path}))


if __name__ == "__main__":
    unittest.main()
