"""Offline tests for the Phase 11C Gate B local-credential preparation tool."""
from __future__ import annotations

import ast
from copy import deepcopy
from contextlib import redirect_stdout
import inspect
import io
from pathlib import Path
import unittest

import phase11c_gateb_local_diagnostic as gate_b


ROOT = Path(__file__).parents[1]


class Phase11CGateBLocalDiagnosticTests(unittest.TestCase):
    def test_canonical_json_is_stable_and_rejects_unsafe_json(self) -> None:
        self.assertEqual(gate_b.canonical_json({"b": 2, "a": 1}), b'{"a":1,"b":2}')
        with self.assertRaisesRegex(gate_b.GateBPreparationError, "duplicate_json_key"):
            gate_b.strict_json_loads('{"field":1,"field":2}')
        with self.assertRaisesRegex(gate_b.GateBPreparationError, "floating_point"):
            gate_b.canonical_json({"cost": 0.1})

    def test_draft_is_deterministic_and_remains_blocked(self) -> None:
        digest = "a" * 64
        first = gate_b.build_draft_authorization(executable_source_sha256=digest)
        second = gate_b.build_draft_authorization(executable_source_sha256=digest)
        self.assertEqual(first, second)
        validated = gate_b.validate_draft_authorization(first, executable_source_digest=digest)
        self.assertEqual(validated["gate_a_base_commit_sha"], gate_b.GATE_A_BASE_COMMIT)
        self.assertEqual(validated["credential_delivery_mode"], "local_one_time_secure_file")
        self.assertEqual(validated["aggregate_budget_ceiling_micro_cny"], 15_000_000)
        self.assertEqual(validated["diagnostic_budget_micro_cny"], 0)
        self.assertFalse(validated["credential_bytes_read"])
        self.assertFalse(validated["live_execution_enabled"])

    def test_draft_rejects_unapproved_budget_and_source_drift(self) -> None:
        candidate = gate_b.build_draft_authorization(executable_source_sha256="b" * 64)
        bad_budget = deepcopy(candidate)
        bad_budget["diagnostic_budget_micro_cny"] = 1
        with self.assertRaisesRegex(gate_b.GateBPreparationError, "diagnostic_budget_not_zero"):
            gate_b.validate_draft_authorization(bad_budget, executable_source_digest="b" * 64)
        with self.assertRaisesRegex(gate_b.GateBPreparationError, "executable_source_sha256_drift"):
            gate_b.validate_draft_authorization(candidate, executable_source_digest="c" * 64)

    def test_metadata_validation_is_pure_and_fails_closed(self) -> None:
        secure_posix = gate_b.LocalCredentialFileMetadata(
            exists=True,
            regular_file=True,
            symlink=False,
            ancestor_symlink=False,
            absolute_repository_external=True,
            size_bytes=64,
            platform="posix",
            posix_mode=0o600,
            owner_uid=0,
            link_count=1,
            windows_acl_proven=False,
        )
        self.assertIsNone(gate_b.validate_local_credential_file_metadata(secure_posix))
        insecure_mode = gate_b.LocalCredentialFileMetadata(
            **{**secure_posix.__dict__, "posix_mode": 0o644}
        )
        with self.assertRaisesRegex(gate_b.GateBPreparationError, "permissions_denied"):
            gate_b.validate_local_credential_file_metadata(insecure_mode)
        claimed_secure_windows_acl = gate_b.LocalCredentialFileMetadata(
            **{
                **secure_posix.__dict__,
                "platform": "windows",
                "posix_mode": None,
                "windows_acl_proven": True,
            }
        )
        with self.assertRaisesRegex(gate_b.GateBPreparationError, "platform_unsupported"):
            gate_b.validate_local_credential_file_metadata(claimed_secure_windows_acl)

    def test_metadata_requires_the_linux_root_owned_external_file_invariants(self) -> None:
        secure_posix = gate_b.LocalCredentialFileMetadata(
            exists=True,
            regular_file=True,
            symlink=False,
            ancestor_symlink=False,
            absolute_repository_external=True,
            size_bytes=64,
            platform="posix",
            posix_mode=0o600,
            owner_uid=0,
            link_count=1,
            windows_acl_proven=False,
        )
        rejected_cases = (
            ("absolute_repository_external", False, "location_denied"),
            ("ancestor_symlink", True, "file_denied"),
            ("owner_uid", 1000, "owner_denied"),
            ("link_count", 2, "link_count_denied"),
        )
        for field, replacement, code in rejected_cases:
            with self.subTest(field=field), self.assertRaisesRegex(gate_b.GateBPreparationError, code):
                gate_b.validate_local_credential_file_metadata(
                    gate_b.LocalCredentialFileMetadata(
                        **{**secure_posix.__dict__, field: replacement}
                    )
                )

    def test_blocked_receipt_never_records_or_opens_a_credential(self) -> None:
        authorization = gate_b.build_draft_authorization(executable_source_sha256="d" * 64)
        receipt = gate_b.validate_blocked_receipt(gate_b.build_blocked_receipt(authorization))
        self.assertEqual(receipt["execution_status"], "not_run_gate_blocked")
        self.assertEqual(receipt["provider_call_count"], 0)
        self.assertEqual(receipt["http_attempt_count"], 0)
        self.assertFalse(receipt["credential_file_opened"])
        self.assertFalse(receipt["credential_bytes_retained"])
        self.assertEqual(tuple(receipt["blocking_reason_codes"]), gate_b.BLOCKING_REASON_CODES)

    def test_documents_contain_no_secret_or_path_fields(self) -> None:
        draft = gate_b.build_draft_authorization(executable_source_sha256="e" * 64)
        receipt = gate_b.build_blocked_receipt(draft)
        self.assertFalse(gate_b.contains_forbidden_content(draft))
        self.assertFalse(gate_b.contains_forbidden_content(receipt))
        self.assertTrue(gate_b.contains_forbidden_content({"credential_file_path": "C:/secret"}))
        self.assertTrue(gate_b.contains_forbidden_content({"value": "sk-not-a-real-key"}))

    def test_schemas_have_exact_field_sets(self) -> None:
        for name, fields in (
            ("phase11c-gateb-local-authorization.schema.json", gate_b.AUTHORIZATION_FIELDS),
            ("phase11c-gateb-local-receipt.schema.json", gate_b.RECEIPT_FIELDS),
        ):
            schema = gate_b.strict_json_loads((ROOT / "schemas" / name).read_bytes())
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), fields)
        receipt_schema = gate_b.strict_json_loads(
            (ROOT / "schemas" / "phase11c-gateb-local-receipt.schema.json").read_bytes()
        )
        self.assertEqual(
            receipt_schema["properties"]["blocking_reason_codes"]["const"],
            list(gate_b.BLOCKING_REASON_CODES),
        )

    def test_executable_has_no_provider_transport_or_cloud_import(self) -> None:
        tree = ast.parse((ROOT / "phase11c_gateb_local_diagnostic.py").read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertFalse({"openai", "http", "requests", "urllib", "subprocess", "dotenv", "aliyun"} & imports)

    def test_source_hash_has_no_caller_supplied_file_path(self) -> None:
        self.assertEqual(tuple(inspect.signature(gate_b.source_sha256).parameters), ())

    def test_run_diagnostic_is_an_offline_blocked_receipt(self) -> None:
        receipt = gate_b.run_diagnostic_gate_blocked()
        self.assertEqual(receipt["provider_call_count"], 0)
        self.assertEqual(receipt["http_attempt_count"], 0)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(gate_b.main(["run-diagnostic"]), 2)
        self.assertIn('"execution_status":"not_run_gate_blocked"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
