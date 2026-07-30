"""Offline regression tests for the independent Phase 11C Gate A executor."""
from __future__ import annotations

import ast
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import phase11c_provider_canary as canary


ROOT = Path(__file__).parents[1]
GATE_A_ARTIFACTS = "phase11c_provider_canary/examples/gate_a"


class Phase11CProviderCanaryTests(unittest.TestCase):
    def test_canonical_json_is_deterministic_and_rejects_unsafe_forms(self) -> None:
        self.assertEqual(
            canary.canonical_json({"b": 2, "a": 1}),
            b'{"a":1,"b":2}',
        )
        with self.assertRaisesRegex(canary.CanaryValidationError, "floating_point"):
            canary.canonical_json({"value": 0.5})
        with self.assertRaisesRegex(canary.CanaryValidationError, "duplicate_json_key"):
            canary.strict_json_loads('{"field":1,"field":2}')

    def test_phase11b_acceptance_verifier_checks_only_hash_and_status(self) -> None:
        accepted = b'{"status":"accepted"}'
        with patch.object(
            canary,
            "PHASE11B_ACCEPTANCE_REPORT_SHA256",
            canary.sha256_bytes(accepted),
        ):
            self.assertEqual(
                canary.validate_phase11b_acceptance_report_bytes(accepted),
                {"sha256": canary.sha256_bytes(accepted), "status": "accepted"},
            )
        failed = b'{"status":"failed"}'
        with patch.object(
            canary,
            "PHASE11B_ACCEPTANCE_REPORT_SHA256",
            canary.sha256_bytes(failed),
        ):
            with self.assertRaisesRegex(canary.CanaryValidationError, "status_mismatch"):
                canary.validate_phase11b_acceptance_report_bytes(failed)
        with self.assertRaisesRegex(canary.CanaryValidationError, "sha256_mismatch"):
            canary.validate_phase11b_acceptance_report_bytes(accepted)

    def test_source_hash_normalizes_platform_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lf = root / "lf.py"
            crlf = root / "crlf.py"
            lf.write_bytes(b"line_one\nline_two\n")
            crlf.write_bytes(b"line_one\r\nline_two\r\n")
            self.assertEqual(canary.source_sha256(lf), canary.source_sha256(crlf))

    def test_lockfile_hash_normalizes_platform_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lf = root / "requirements-lf.lock"
            crlf = root / "requirements-crlf.lock"
            lf.write_bytes(b"package==1.0\npackage-two==2.0\n")
            crlf.write_bytes(b"package==1.0\r\npackage-two==2.0\r\n")
            self.assertEqual(canary.lockfile_sha256(lf), canary.lockfile_sha256(crlf))

    def test_candidate_bundle_is_deterministic_and_gate_b_remains_blocked(self) -> None:
        digest = "a" * 64
        first = canary.build_gate_a_candidates(executable_source_digest=digest)
        second = canary.build_gate_a_candidates(executable_source_digest=digest)
        self.assertEqual(first, second)
        validated = canary.validate_gate_a_candidates(first, executable_source_digest=digest)
        self.assertFalse(validated["canary_allowed"])
        self.assertFalse(validated["real_run_recommended_now"])
        self.assertIn("provider_policy_unaccepted", validated["blocking_reason_codes"])
        self.assertEqual(first["runtime_config"]["sdk_retries"], 0)
        self.assertEqual(first["runtime_config"]["transport_retries"], 0)
        self.assertEqual(first["runtime_config"]["concurrency"], 1)
        self.assertEqual(first["runtime_config"]["openai_sdk_version"], "2.46.0")
        self.assertEqual(first["runtime_config"]["sdk_package_set_sha256"], canary.lockfile_sha256())
        self.assertFalse(first["runtime_config"]["real_provider_calls_enabled"])
        self.assertFalse(first["runtime_config"]["paid_calls_enabled"])
        self.assertFalse(first["runtime_config"]["local_persistence"]["raw_provider_response_retention"])
        self.assertFalse(first["runtime_config"]["local_persistence"]["credential_value_retention"])
        self.assertTrue(
            first["runtime_config"]["local_persistence"][
                "safe_hash_enum_boolean_count_receipt_retention"
            ]
        )
        self.assertFalse(first["synthetic_cohort"]["diagnostic_in_headline_denominator"])
        self.assertEqual(first["synthetic_cohort"]["proposed_headline_denominator"], 3)
        self.assertEqual(len(first["synthetic_cohort"]["targets"]), 5)
        self.assertEqual(
            first["tariff"]["provider_data_use_policy_url"],
            canary.PENDING_CURRENT_REVIEW,
        )
        self.assertEqual(
            first["authorization"]["gate_b_bindings"]["immutable_image_digest"],
            canary.PENDING_FREEZE,
        )

    def test_artifact_hash_and_source_drift_fail_closed(self) -> None:
        candidates = canary.build_gate_a_candidates(executable_source_digest="b" * 64)
        image_drift = deepcopy(candidates)
        image_drift["runtime_config"]["immutable_image_digest"] = "c" * 64
        with self.assertRaisesRegex(canary.CanaryValidationError, "runtime_frozen_literal_mismatch"):
            canary.validate_gate_a_candidates(image_drift, executable_source_digest="b" * 64)
        with self.assertRaisesRegex(canary.CanaryValidationError, "source_sha256_drift"):
            canary.validate_gate_a_candidates(candidates, executable_source_digest="c" * 64)

    def test_persisted_candidates_round_trip_and_are_content_safe(self) -> None:
        candidates = canary.load_gate_a_artifacts(GATE_A_ARTIFACTS)
        result = canary.validate_gate_a_candidates(
            candidates,
            executable_source_digest=canary.source_sha256(),
        )
        self.assertFalse(result["canary_allowed"])
        for document in candidates.values():
            self.assertFalse(canary.contains_forbidden_content(document))

    def test_schemas_have_strict_field_sets(self) -> None:
        schema_names = (
            "phase11c-provider-canary-authorization.schema.json",
            "phase11c-provider-canary-runtime.schema.json",
            "phase11c-provider-canary-cohort.schema.json",
            "phase11c-provider-canary-tariff.schema.json",
            "phase11c-provider-canary-preflight.schema.json",
        )
        for name in schema_names:
            schema = canary.strict_json_loads((ROOT / "schemas" / name).read_bytes())
            self.assertFalse(schema["additionalProperties"])
            self.assertTrue(schema["required"])

    def test_safe_telemetry_matrix_has_fixed_stages_and_no_mixed_failure(self) -> None:
        matrix = canary.run_fake_compatibility_matrix()
        self.assertEqual({item["scenario"] for item in matrix}, set(canary.FAKE_SCENARIOS))
        self.assertIn("normal_submit", canary.FAKE_SCENARIOS)
        self.assertIn("provider_schema_mismatch", canary.FAKE_SCENARIOS)
        self.assertIn("ambiguous_result", canary.FAKE_SCENARIOS)
        for item in matrix:
            self.assertIn(item["pipeline_stage"], canary.PIPELINE_STAGES)
            self.assertTrue(item["redaction_applied"])
            self.assertNotEqual(item["stable_failure_code"], "provider_or_pipeline_RuntimeError")
        normal = canary.fake_protocol_terminal("normal_submit")
        self.assertEqual(normal["terminal_status"], "completed")
        self.assertIsNone(normal["telemetry"]["stable_failure_code"])
        repeated = canary.fake_protocol_terminal("repeated_empty_response")
        self.assertEqual(repeated["telemetry"]["stable_failure_code"], "repeated_empty_response")
        self.assertEqual(repeated["telemetry"]["empty_response_count"], 2)

    def test_terminal_status_validation_rejects_completed_failure(self) -> None:
        terminal = canary.fake_protocol_terminal("normal_submit")
        terminal["telemetry"]["stable_failure_code"] = "other"
        with self.assertRaisesRegex(canary.CanaryValidationError, "completed_with_failure_code"):
            canary.validate_terminal_receipt(terminal)

    def test_budget_reservation_is_monotonic_across_unknown_usage_and_restart(self) -> None:
        ledger = canary.DurableBudgetLedger(canary.BudgetLimits(2, 2, 10, 10, 100))
        reservation = canary.BudgetReservation("diagnostic-1", 10, 10, 100)
        ledger.reserve(reservation)
        ledger.record_http_attempt("diagnostic-1")
        ledger.reconcile("diagnostic-1", usage_known=False)
        self.assertEqual(ledger.logical_calls, 1)
        self.assertEqual(ledger.http_attempts, 1)
        self.assertEqual(ledger.input_tokens, 10)
        self.assertEqual(ledger.output_tokens, 10)
        self.assertEqual(ledger.micro_cny, 100)
        restarted = canary.DurableBudgetLedger.from_snapshot(ledger.snapshot())
        with self.assertRaises(canary.BudgetExhausted):
            restarted.reserve(canary.BudgetReservation("headline-1", 1, 1, 1))

    def test_attempt_is_durably_recorded_before_fake_transport(self) -> None:
        ledger = canary.DurableBudgetLedger(canary.BudgetLimits(1, 1, 10, 10, 10))
        transport = canary.RecordingFakeTransport(ledger)
        receipt = canary.execute_fake_attempt(
            "normal_submit",
            ledger=ledger,
            reservation=canary.BudgetReservation("fake-1", 10, 10, 10),
            transport=transport,
        )
        self.assertEqual(transport.calls, 1)
        self.assertEqual(receipt["logical_call_count"], 1)
        self.assertEqual(receipt["http_attempt_count"], 1)

    def test_budget_exhaustion_happens_before_fake_transport(self) -> None:
        ledger = canary.DurableBudgetLedger(canary.BudgetLimits(0, 0, 0, 0, 0))
        transport = canary.RecordingFakeTransport(ledger)
        with self.assertRaises(canary.BudgetExhausted):
            canary.execute_fake_attempt(
                "normal_submit",
                ledger=ledger,
                reservation=canary.BudgetReservation("fake-1", 1, 1, 1),
                transport=transport,
            )
        self.assertEqual(transport.calls, 0)
        self.assertEqual(ledger.http_attempts, 0)

    def test_credential_metadata_check_never_reads_a_credential(self) -> None:
        secure = canary.CredentialFileMetadata(True, False, True, 0o600, False, False)
        self.assertIsNone(canary.validate_credential_file_metadata(secure))
        self.assertEqual(
            canary.validate_credential_file_metadata(
                canary.CredentialFileMetadata(True, True, True, 0o600, False, False)
            ),
            "authorization_mismatch",
        )
        self.assertEqual(
            canary.validate_credential_file_metadata(
                canary.CredentialFileMetadata(True, False, True, 0o600, True, False)
            ),
            "credential_expired",
        )
        self.assertEqual(
            canary.validate_credential_file_metadata(
                canary.CredentialFileMetadata(True, False, True, 0o600, False, True)
            ),
            "credential_revoked",
        )

    def test_real_run_is_a_zero_io_gate_blocked_receipt(self) -> None:
        receipt = canary.run_real_gate_blocked()
        self.assertEqual(receipt["execution_status"], "not_run")
        self.assertEqual(receipt["protocol_canary_status"], "not_run")
        self.assertEqual(receipt["terminal_status"], "not_run_gate_blocked")
        self.assertEqual(receipt["provider_call_count"], 0)
        self.assertEqual(receipt["http_attempt_count"], 0)
        self.assertEqual(receipt["telemetry"]["stable_failure_code"], "authorization_mismatch")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(canary.main(["run-real"]), 2)
        self.assertIn('"provider_call_count":0', output.getvalue())

    def test_executor_has_no_provider_or_http_client_import(self) -> None:
        tree = ast.parse((ROOT / "phase11c_provider_canary.py").read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertFalse({"openai", "http", "requests", "urllib"} & imports)

    def test_absolute_artifact_output_path_is_denied(self) -> None:
        for output_directory in (
            "/outside-repository",
            "C:/outside-repository",
            r"C:\outside-repository",
            r"\\server\share\outside-repository",
            r"\outside-repository",
            "C:outside-repository",
        ):
            with self.subTest(output_directory=output_directory):
                with self.assertRaisesRegex(canary.CanaryValidationError, "absolute_output_path_denied"):
                    canary.write_gate_a_artifacts(output_directory)

    def test_relative_artifact_output_path_escape_is_denied(self) -> None:
        for output_directory in (
            "../outside-repository",
            "nested/../../outside-repository",
            r"..\outside-repository",
            r"nested\..\..\outside-repository",
        ):
            with self.subTest(output_directory=output_directory):
                with self.assertRaisesRegex(canary.CanaryValidationError, "output_path_escape_denied"):
                    canary.write_gate_a_artifacts(output_directory)


if __name__ == "__main__":
    unittest.main()
