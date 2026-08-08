"""Offline tests for the Gate B DIAGNOSTIC freeze mechanics."""
from __future__ import annotations

import ast
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import inspect
import io
from pathlib import Path
import unittest

import phase11c_gateb_live_diagnostic as gate_b


ROOT = Path(__file__).parents[1]
EXECUTABLE_SOURCE_SHA256 = "a" * 64
PREAPPROVAL_ATTESTATIONS = gate_b.PreapprovalAttestations(
    technical_bindings_valid=True,
    provider_policy_accepted=True,
    tariff_current=True,
    authorization_window_valid=True,
)


def _reseal(value: dict[str, object], field: str) -> dict[str, object]:
    result = deepcopy(value)
    result[field] = ""
    return gate_b._seal(result, field)


def _final_authorization() -> dict[str, object]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    candidate = gate_b.build_draft_authorization(executable_source_sha256=EXECUTABLE_SOURCE_SHA256)
    candidate.update(
        {
            "authorization_status": "frozen_pending_approval",
            "source_tree_sha256": "b" * 64,
            "image_sha256": "c" * 64,
            "deployment_sha256": "d" * 64,
            "runtime_identity_sha256": "e" * 64,
            "cohort_sha256": "f" * 64,
            "endpoint_sha256": "1" * 64,
            "provider_policy_evidence_sha256": "2" * 64,
            "provider_policy_accepted": True,
            "provider_tariff_sha256": "3" * 64,
            "tariff_effective_utc": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "credential_fingerprint_sha256": "4" * 64,
            "owner_reconfirmed": True,
            "kill_switch_bound": True,
            "authorization_window_start_utc": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "authorization_window_end_utc": (now + timedelta(days=1, minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "max_logical_calls": 1,
            "max_http_attempts": 1,
            "max_input_tokens": 1_000,
            "max_output_tokens": 500,
            "input_rate_microcny_per_million": 1_000_000,
            "output_rate_microcny_per_million": 2_000_000,
            "cached_input_rate_microcny_per_million": 500_000,
            "diagnostic_budget_microcny": 2_000,
        }
    )
    return _reseal(candidate, "authorization_sha256")


def _secure_metadata() -> gate_b.CredentialFileMetadata:
    return gate_b.CredentialFileMetadata(
        platform="posix",
        exists=True,
        regular_file=True,
        symlink=False,
        ancestor_symlink=False,
        absolute_repository_external=True,
        owner_uid=0,
        mode_octal=0o600,
        link_count=1,
        size_bytes=64,
    )


def _preflight(authorization: dict[str, object]) -> dict[str, object]:
    return gate_b.build_preapproval_preflight(
        authorization,
        executable_source_digest=EXECUTABLE_SOURCE_SHA256,
        attestations=PREAPPROVAL_ATTESTATIONS,
    )


def _approval_binding(authorization: dict[str, object], preflight: dict[str, object]) -> dict[str, object]:
    return gate_b.build_approval_binding(
        authorization,
        preflight,
        executable_source_digest=EXECUTABLE_SOURCE_SHA256,
    )


class Phase11CGateBLiveDiagnosticTests(unittest.TestCase):
    def test_canonical_json_rejects_duplicate_keys_float_and_nonstring_keys(self) -> None:
        self.assertEqual(gate_b.canonical_json({"b": 2, "a": 1}), b'{"a":1,"b":2}')
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "duplicate_json_key"):
            gate_b.strict_json_loads('{"field":1,"field":2}')
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "floating_point"):
            gate_b.canonical_json({"cost": 0.1})
        for serialized, code in (
            ('{"cost":0.1}', "floating_point"),
            ('{"cost":NaN}', "floating_point"),
            ('{"count":-1}', "negative_integer"),
        ):
            with self.subTest(serialized=serialized), self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, code):
                gate_b.strict_json_loads(serialized)
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "non_string_json_key"):
            gate_b.canonical_json({1: "x"})

    def test_draft_is_deterministic_and_permanently_disabled(self) -> None:
        first = gate_b.build_draft_authorization(executable_source_sha256=EXECUTABLE_SOURCE_SHA256)
        second = gate_b.build_draft_authorization(executable_source_sha256=EXECUTABLE_SOURCE_SHA256)
        self.assertEqual(first, second)
        validated = gate_b.validate_draft_authorization(first, executable_source_digest=EXECUTABLE_SOURCE_SHA256)
        self.assertEqual(validated["authorization_status"], "draft_incomplete")
        self.assertEqual(validated["diagnostic_budget_microcny"], 0)
        self.assertFalse(validated["live_execution_enabled"])
        self.assertFalse(validated["provider_policy_accepted"])

    def test_draft_rejects_final_values_and_executable_drift(self) -> None:
        draft = gate_b.build_draft_authorization(executable_source_sha256=EXECUTABLE_SOURCE_SHA256)
        mutated = _reseal({**draft, "diagnostic_budget_microcny": 1}, "authorization_sha256")
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "draft_numeric_not_zero"):
            gate_b.validate_draft_authorization(mutated)
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "executable_source_sha256_drift"):
            gate_b.validate_draft_authorization(draft, executable_source_digest="b" * 64)

    def test_gate_a_binding_drift_is_rejected_for_draft_and_final_authorizations(self) -> None:
        draft = gate_b.build_draft_authorization(executable_source_sha256=EXECUTABLE_SOURCE_SHA256)
        for field, replacement in (
            ("gate_a_base_commit_sha", "0" * 40),
            ("gate_a_runtime_config_sha256", "0" * 64),
            ("gate_a_preflight_sha256", "0" * 64),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "gate_a"):
                gate_b.validate_draft_authorization(_reseal({**draft, field: replacement}, "authorization_sha256"))

        final = _final_authorization()
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "gate_a"):
            gate_b.validate_final_authorization(
                _reseal({**final, "gate_a_preflight_sha256": "0" * 64}, "authorization_sha256"),
                executable_source_digest=EXECUTABLE_SOURCE_SHA256,
            )

    def test_final_authorization_binds_every_technical_input_and_budget(self) -> None:
        authorization = _final_authorization()
        validated = gate_b.validate_final_authorization(
            authorization,
            executable_source_digest=EXECUTABLE_SOURCE_SHA256,
            expected_authorization_sha256=authorization["authorization_sha256"],
        )
        self.assertEqual(validated["diagnostic_budget_microcny"], 2_000)
        self.assertEqual(
            gate_b.worst_case_microcny(
                input_tokens=1_000, output_tokens=500, input_rate=1_000_000, output_rate=2_000_000
            ),
            2_000,
        )
        for field, replacement, code in (
            ("image_sha256", "9" * 64, "authorization_sha256_mismatch"),
            ("provider_policy_accepted", False, "provider_policy_unaccepted"),
            ("diagnostic_budget_microcny", 1_999, "diagnostic_budget_mismatch"),
            ("cached_input_rate_microcny_per_million", 1_000_001, "tariff_rate_invalid"),
            ("concurrency", 2, "transport_policy_mismatch"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, code):
                candidate = deepcopy(authorization)
                candidate[field] = replacement
                if field != "image_sha256":
                    candidate = _reseal(candidate, "authorization_sha256")
                gate_b.validate_final_authorization(
                    candidate,
                    executable_source_digest=EXECUTABLE_SOURCE_SHA256,
                    expected_authorization_sha256=authorization["authorization_sha256"],
                )

    def test_final_authorization_rejects_expired_or_invalid_window(self) -> None:
        authorization = _final_authorization()
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "authorization_window_expired"):
            gate_b._validate_final_authorization_at(
                authorization, now_utc=datetime(2032, 1, 1, tzinfo=timezone.utc)
            )
        invalid = deepcopy(authorization)
        invalid["authorization_window_end_utc"] = "2031-01-01T00:00:00Z"
        invalid = _reseal(invalid, "authorization_sha256")
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "authorization_window_invalid"):
            gate_b.validate_final_authorization(invalid)

    def test_resealed_final_binding_drift_is_rejected_against_the_frozen_authorization(self) -> None:
        authorization = _final_authorization()
        frozen_sha = authorization["authorization_sha256"]
        for field, replacement in (
            ("source_tree_sha256", "7" * 64),
            ("image_sha256", "8" * 64),
            ("deployment_sha256", "9" * 64),
            ("runtime_identity_sha256", "0" * 64),
            ("cohort_sha256", "a" * 64),
            ("endpoint_sha256", "b" * 64),
            ("provider_policy_evidence_sha256", "c" * 64),
            ("provider_tariff_sha256", "d" * 64),
            ("credential_fingerprint_sha256", "e" * 64),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                gate_b.GateBLiveDiagnosticError, "authorization_binding_drift"
            ):
                candidate = _reseal({**authorization, field: replacement}, "authorization_sha256")
                gate_b.validate_final_authorization(
                    candidate,
                    executable_source_digest=EXECUTABLE_SOURCE_SHA256,
                    expected_authorization_sha256=frozen_sha,
                )
        changed_tariff = deepcopy(authorization)
        changed_tariff["input_rate_microcny_per_million"] = 900_000
        changed_tariff["diagnostic_budget_microcny"] = gate_b.worst_case_microcny(
            input_tokens=changed_tariff["max_input_tokens"],
            output_tokens=changed_tariff["max_output_tokens"],
            input_rate=changed_tariff["input_rate_microcny_per_million"],
            output_rate=changed_tariff["output_rate_microcny_per_million"],
        )
        changed_tariff = _reseal(changed_tariff, "authorization_sha256")
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "authorization_binding_drift"):
            gate_b.validate_final_authorization(
                changed_tariff,
                executable_source_digest=EXECUTABLE_SOURCE_SHA256,
                expected_authorization_sha256=frozen_sha,
            )

        changed_window = deepcopy(authorization)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        changed_window["authorization_window_start_utc"] = (now + timedelta(days=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        changed_window["authorization_window_end_utc"] = (now + timedelta(days=2, minutes=15)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        changed_window = _reseal(changed_window, "authorization_sha256")
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "authorization_binding_drift"):
            gate_b.validate_final_authorization(
                changed_window,
                executable_source_digest=EXECUTABLE_SOURCE_SHA256,
                expected_authorization_sha256=frozen_sha,
            )

    def test_preflight_and_approval_binding_are_sealed_and_non_circular(self) -> None:
        authorization = _final_authorization()
        preflight = _preflight(authorization)
        self.assertFalse(preflight["credential_metadata_validated"])
        self.assertIn("credential_metadata_not_validated", preflight["blocking_reason_codes"])
        validated = gate_b.validate_preapproval_preflight(
            preflight, authorization_sha256=authorization["authorization_sha256"]
        )
        binding = _approval_binding(authorization, validated)
        self.assertEqual(
            gate_b.validate_approval_binding(binding)["authorization_sha256"], authorization["authorization_sha256"]
        )
        drifted = deepcopy(preflight)
        drifted["authorization_sha256"] = "9" * 64
        drifted = _reseal(drifted, "preflight_sha256")
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "preflight_authorization_binding_mismatch"):
            gate_b.validate_preapproval_preflight(
                drifted, authorization_sha256=authorization["authorization_sha256"]
            )

    def test_preapproval_attestations_are_explicit_and_fail_closed(self) -> None:
        authorization = _final_authorization()
        unverified = gate_b.PreapprovalAttestations(
            technical_bindings_valid=True,
            provider_policy_accepted=False,
            tariff_current=True,
            authorization_window_valid=True,
        )
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "preapproval_attestation_not_approval_eligible"):
            gate_b.build_preapproval_preflight(
                authorization,
                executable_source_digest=EXECUTABLE_SOURCE_SHA256,
                attestations=unverified,
            )

    def test_one_use_approval_wrong_text_and_duplicate_are_fail_closed(self) -> None:
        authorization = _final_authorization()
        binding = _approval_binding(authorization, _preflight(authorization))
        ledger = gate_b.OneUseApprovalLedger()
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "approval_text_mismatch"):
            ledger.consume("APPROVE PHASE11C DIAGNOSTIC wrong", binding)
        self.assertEqual(ledger.consumed, set())
        text = f"APPROVE PHASE11C DIAGNOSTIC {binding['approval_binding_sha256']}"
        ledger.consume(text, binding)
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "already_consumed"):
            ledger.consume(text, binding)

    def test_fake_execution_reserves_before_metadata_and_fake_transport(self) -> None:
        authorization = _final_authorization()
        preflight = _preflight(authorization)
        binding = _approval_binding(authorization, preflight)
        approval = f"APPROVE PHASE11C DIAGNOSTIC {binding['approval_binding_sha256']}"
        budget = gate_b.DurableFakeBudgetLedger(
            gate_b.BudgetLimits(logical_calls=1, http_attempts=1, input_tokens=1_000, output_tokens=500, microcny=2_000)
        )
        transport = gate_b.RecordingFakeTransport(budget)
        result = gate_b.run_fake_diagnostic(
            authorization,
            preflight,
            approval,
            _secure_metadata(),
            executable_source_digest=EXECUTABLE_SOURCE_SHA256,
            approval_ledger=gate_b.OneUseApprovalLedger(),
            budget_ledger=budget,
            transport=transport,
        )
        self.assertEqual(result["execution_status"], "fake_completed_no_provider")
        self.assertEqual(result["provider_call_count"], 0)
        self.assertEqual(result["http_attempt_count"], 1)
        self.assertFalse(result["usage_known"])
        self.assertEqual(transport.calls, 1)
        self.assertEqual(budget.microcny, 2_000)
        self.assertEqual(budget.credential_metadata_validated, {binding["approval_binding_sha256"]})

    def test_budget_never_rolls_back_and_transport_needs_http_record(self) -> None:
        ledger = gate_b.DurableFakeBudgetLedger(
            gate_b.BudgetLimits(logical_calls=1, http_attempts=1, input_tokens=1, output_tokens=1, microcny=1)
        )
        reservation = gate_b.BudgetReservation("r", 1, 1, 1)
        ledger.reserve(reservation)
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "transport_before_durable_http_attempt"):
            gate_b.RecordingFakeTransport(ledger).dispatch("r")
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "http_attempt_before_credential_metadata_validation"):
            ledger.record_http_attempt("r")
        ledger.http_recorded.add("r")
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "transport_before_credential_metadata_validation"):
            gate_b.RecordingFakeTransport(ledger).dispatch("r")
        ledger.http_recorded.clear()
        ledger.record_credential_metadata_validated("r")
        ledger.record_http_attempt("r")
        ledger.reconcile("r", usage_known=False)
        self.assertEqual((ledger.logical_calls, ledger.http_attempts, ledger.microcny), (1, 1, 1))
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "budget_hard_cap_exhausted"):
            ledger.reserve(gate_b.BudgetReservation("next", 1, 1, 1))

    def test_budget_limits_and_reservations_reject_negative_or_noninteger_values(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "invalid_budget_limit"):
            gate_b.BudgetLimits(logical_calls=1, http_attempts=1, input_tokens=1, output_tokens=1, microcny=-1)
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaisesRegex(
                gate_b.GateBLiveDiagnosticError, "invalid_budget_reservation"
            ):
                gate_b.BudgetReservation("r", value, 1, 1)

    def test_fake_execution_rejects_source_drift_before_consuming_approval_or_budget(self) -> None:
        authorization = _final_authorization()
        preflight = _preflight(authorization)
        binding = _approval_binding(authorization, preflight)
        approval = f"APPROVE PHASE11C DIAGNOSTIC {binding['approval_binding_sha256']}"
        approvals = gate_b.OneUseApprovalLedger()
        budget = gate_b.DurableFakeBudgetLedger(
            gate_b.BudgetLimits(logical_calls=1, http_attempts=1, input_tokens=1_000, output_tokens=500, microcny=2_000)
        )
        with self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, "executable_source_sha256_drift"):
            gate_b.run_fake_diagnostic(
                authorization,
                preflight,
                approval,
                _secure_metadata(),
                executable_source_digest="b" * 64,
                approval_ledger=approvals,
                budget_ledger=budget,
            )
        self.assertEqual(approvals.consumed, set())
        self.assertEqual((budget.logical_calls, budget.http_attempts, budget.microcny), (0, 0, 0))

    def test_credential_metadata_is_synthetic_linux_only_and_fail_closed(self) -> None:
        self.assertIsNone(gate_b.validate_credential_file_metadata(_secure_metadata()))
        for field, replacement, code in (
            ("platform", "windows", "platform_unsupported"),
            ("ancestor_symlink", True, "credential_file_denied"),
            ("owner_uid", 1000, "permissions_denied"),
            ("mode_octal", 0o644, "permissions_denied"),
            ("link_count", 2, "link_count_denied"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(gate_b.GateBLiveDiagnosticError, code):
                metadata = _secure_metadata()
                gate_b.validate_credential_file_metadata(
                    gate_b.CredentialFileMetadata(**{**metadata.__dict__, field: replacement})
                )
        for field, replacement in (
            ("owner_uid", False),
            ("mode_octal", 384.0),
            ("link_count", 1.0),
            ("size_bytes", 1.5),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                gate_b.GateBLiveDiagnosticError, "credential_file_metadata_type_invalid"
            ):
                metadata = _secure_metadata()
                gate_b.validate_credential_file_metadata(
                    gate_b.CredentialFileMetadata(**{**metadata.__dict__, field: replacement})
                )

    def test_blocked_receipt_never_records_a_call_or_credential_access(self) -> None:
        draft = gate_b.build_draft_authorization(executable_source_sha256="a" * 64)
        receipt = gate_b.validate_blocked_receipt(gate_b.build_blocked_receipt(draft))
        self.assertEqual(receipt["execution_status"], "not_run_gate_blocked")
        self.assertEqual(receipt["provider_call_count"], 0)
        self.assertEqual(receipt["http_attempt_count"], 0)
        self.assertFalse(receipt["credential_file_opened"])
        self.assertFalse(receipt["live_execution_enabled"])
        self.assertEqual(tuple(receipt["blocking_reason_codes"]), gate_b.DRAFT_BLOCKING_REASON_CODES)

    def test_schemas_have_exact_fields_and_fixed_safe_blockers(self) -> None:
        expected = (
            ("phase11c-gateb-live-diagnostic-authorization.schema.json", gate_b.AUTHORIZATION_FIELDS),
            ("phase11c-gateb-live-diagnostic-preflight.schema.json", gate_b.PREFLIGHT_FIELDS),
            ("phase11c-gateb-live-diagnostic-receipt.schema.json", gate_b.RECEIPT_FIELDS),
        )
        schemas: dict[str, dict[str, object]] = {}
        for name, fields in expected:
            schema = gate_b.strict_json_loads((ROOT / "schemas" / name).read_bytes())
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), fields)
            schemas[name] = schema
        self.assertEqual(
            schemas["phase11c-gateb-live-diagnostic-preflight.schema.json"]["properties"]["blocking_reason_codes"]["const"],
            list(gate_b.FINAL_PREFLIGHT_BLOCKING_REASON_CODES),
        )
        self.assertEqual(
            schemas["phase11c-gateb-live-diagnostic-receipt.schema.json"]["properties"]["blocking_reason_codes"]["const"],
            list(gate_b.DRAFT_BLOCKING_REASON_CODES),
        )

    def test_documents_do_not_contain_secret_or_path_content(self) -> None:
        draft = gate_b.build_draft_authorization(executable_source_sha256="a" * 64)
        receipt = gate_b.build_blocked_receipt(draft)
        self.assertFalse(gate_b.contains_forbidden_content(draft))
        self.assertFalse(gate_b.contains_forbidden_content(receipt))
        self.assertTrue(gate_b.contains_forbidden_content({"credential_file_path": "redacted"}))
        self.assertTrue(gate_b.contains_forbidden_content({"value": "sk-not-a-real-key"}))

    def test_source_has_no_network_cloud_or_credential_reader_import(self) -> None:
        tree = ast.parse((ROOT / "phase11c_gateb_live_diagnostic.py").read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        forbidden = {"openai", "http", "requests", "urllib", "socket", "ssl", "subprocess", "os", "dotenv", "aliyun"}
        self.assertFalse(forbidden & imports)
        self.assertEqual(tuple(inspect.signature(gate_b.source_sha256).parameters), ())

    def test_cli_run_live_is_always_offline_and_blocked(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(gate_b.main(["run-live"]), 2)
        self.assertIn('"execution_status":"not_run_gate_blocked"', output.getvalue())
        self.assertIn('"provider_call_count":0', output.getvalue())


if __name__ == "__main__":
    unittest.main()
