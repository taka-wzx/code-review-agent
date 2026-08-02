from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import phase11c_gateb_freeze as freeze
import phase11c_gateb_headline_cohort_executor as protocol


NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
SEAL_NOW = NOW - timedelta(minutes=2)
ROOT = Path(__file__).resolve().parents[1]


def _sha(character: str) -> str:
    return character * 64


def _freeze_values() -> dict[str, object]:
    return {
        "authorization_id": "p11c-gateb-0123456789abcdef0123456789abcdef",
        "execution_freeze_sha256": _sha("f"),
        "executable_commit_sha": "d" * 40,
        "source_tree_sha256": _sha("1"),
        "source_archive_sha256": _sha("b"),
        "dockerfile_sha256": _sha("2"),
        "compose_sha256": _sha("3"),
        "image_sha256": _sha("4"),
        "deployment_sha256": _sha("5"),
        "runtime_config_sha256": _sha("c"),
        "runtime_identity_sha256": _sha("6"),
        "aliyun_runtime_identity_sha256": _sha("6"),
        "provider_policy_evidence_sha256": _sha("8"),
        "provider_tariff_evidence_sha256": _sha("9"),
        "provider_tariff_manifest_sha256": _sha("e"),
        "preflight_verdict_sha256": _sha("a"),
        "credential_fingerprint_sha256": hashlib.sha256(b"fake-key").hexdigest(),
        "authorization_window_start_utc": (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "authorization_window_end_utc": (NOW + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _diagnostic_candidate() -> dict[str, object]:
    candidate = protocol.build_diagnostic_authorization_template()
    candidate.update(_freeze_values())
    return candidate


def _diagnostic_authorization() -> dict[str, object]:
    return protocol.seal_diagnostic_authorization(_diagnostic_candidate(), now_utc=SEAL_NOW)


def _headline_candidate() -> dict[str, object]:
    candidate = protocol.build_authorization_template()
    candidate.update(_freeze_values())
    candidate.update(
        {
            "diagnostic_receipt_sha256": _sha("7"),
            "diagnostic_authorization_sha256": _sha("a"),
            "diagnostic_approval_binding_sha256": _sha("b"),
            "auth004_nonoverlap_evidence_sha256": _sha("c"),
        }
    )
    return candidate


def _headline_authorization() -> dict[str, object]:
    return protocol.seal_authorization(_headline_candidate(), now_utc=SEAL_NOW)


class FakeCredential:
    def __init__(self, value: str = "fake-key") -> None:
        self.calls = 0
        self.value = value

    def read(self, expected_fingerprint: str, on_opened: object) -> str:
        self.calls += 1
        assert expected_fingerprint == hashlib.sha256(b"fake-key").hexdigest()
        assert callable(on_opened)
        on_opened()
        return self.value


class FailingCredential:
    def read(self, expected_fingerprint: str, on_opened: object) -> str:
        assert callable(on_opened)
        on_opened()
        raise protocol.HeadlineCohortError("credential_fingerprint_mismatch")


class MissingCallbackCredential:
    def read(self, expected_fingerprint: str, on_opened: object) -> str:
        return "fake-key"


class FakeTransport:
    def __init__(self, results: list[protocol.HttpResult | Exception]) -> None:
        self.results = list(results)
        self.calls: list[bytes] = []

    def dispatch(self, api_key: str, request_body: bytes) -> protocol.HttpResult:
        assert api_key == "fake-key"
        self.calls.append(request_body)
        if not self.results:
            raise AssertionError("unexpected transport call")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _tool_response(
    name: str,
    outcome: str,
    target: dict[str, str],
    call_id: str,
    *,
    usage: bool = True,
    finish_reason: str = "tool_calls",
    arguments: str | None = None,
) -> protocol.HttpResult:
    args = arguments or json.dumps(
        {"outcome": outcome, "payload_sha256": target["payload_sha256"], "target_id": target["stable_id"]},
        sort_keys=True,
        separators=(",", ":"),
    )
    payload: dict[str, object] = {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}]},
            }
        ]
    }
    if usage:
        payload["usage"] = {"prompt_tokens": 12, "completion_tokens": 7}
    return protocol.HttpResult(200, json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _diagnostic_response(content: str = protocol.DIAGNOSTIC_TERMINAL_TOKEN, *, usage: bool = True) -> protocol.HttpResult:
    payload: dict[str, object] = {"choices": [{"message": {"content": content}}]}
    if usage:
        payload["usage"] = {"prompt_tokens": 12, "completion_tokens": 7}
    return protocol.HttpResult(200, json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _completed_diagnostic() -> tuple[dict[str, object], dict[str, object]]:
    authorization = _diagnostic_authorization()
    binding = protocol.diagnostic_approval_binding_sha256(authorization)
    receipt = protocol.execute_diagnostic(
        authorization,
        protocol.expected_diagnostic_approval_text(binding),
        store=protocol.InMemoryDiagnosticStateStore(),
        credential_reader=FakeCredential(),
        transport=FakeTransport([_diagnostic_response()]),
        now_utc=NOW,
    )
    return authorization, receipt


def _lineage() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    diagnostic_authorization, diagnostic_receipt = _completed_diagnostic()
    candidate = _headline_candidate()
    candidate.update(
        {
            "diagnostic_receipt_sha256": diagnostic_receipt["receipt_sha256"],
            "diagnostic_authorization_sha256": diagnostic_authorization["authorization_sha256"],
            "diagnostic_approval_binding_sha256": diagnostic_receipt["approval_binding_sha256"],
        }
    )
    return protocol.seal_authorization(candidate, now_utc=SEAL_NOW), diagnostic_authorization, diagnostic_receipt


class CanonicalAndAuthorizationTests(unittest.TestCase):
    def test_live_control_chain_requires_exact_freeze_and_preflight(self) -> None:
        materials = freeze.FreezeMaterials(
            executable_source_sha256=protocol.source_sha256(),
            executable_commit_sha="d" * 40,
            source_tree_sha256=_sha("1"),
            source_archive_sha256=_sha("2"),
            dockerfile_sha256=_sha("3"),
            compose_sha256=_sha("4"),
            image_sha256=_sha("5"),
            deployment_sha256=_sha("6"),
            runtime_identity_sha256=_sha("7"),
            provider_policy_evidence_sha256=_sha("8"),
            provider_tariff_evidence_sha256=_sha("9"),
            credential_fingerprint_sha256=hashlib.sha256(b"fake-key").hexdigest(),
        )
        result = freeze.freeze_diagnostic(
            materials=materials,
            window_start_utc=(NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            window_end_utc=(NOW + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            policy_url=freeze.POLICY_URL,
            retention_policy_url=freeze.RETENTION_POLICY_URL,
            policy_reviewed_at_utc="2030-01-02T03:00:00Z",
            tariff_observed_at_utc="2030-01-02T03:00:00Z",
            tariff_effective_date="2030-01-02",
            now_utc=SEAL_NOW,
        )
        authorization = result["authorization"]
        controls = [
            protocol.canonical_json(result["execution_freeze"]),
            protocol.canonical_json(result["preflight"]),
        ]
        with patch.object(protocol, "_read_fixed_control_file", side_effect=controls):
            protocol._validate_live_control_chain(
                authorization,
                stage="DIAGNOSTIC",
                execution_freeze_path=protocol.DIAGNOSTIC_EXECUTION_FREEZE_PATH,
                preflight_path=protocol.DIAGNOSTIC_PREFLIGHT_PATH,
            )
        tampered = dict(result["preflight"])
        tampered["canary_allowed"] = False
        with patch.object(
            protocol,
            "_read_fixed_control_file",
            side_effect=[protocol.canonical_json(result["execution_freeze"]), protocol.canonical_json(tampered)],
        ):
            with self.assertRaisesRegex(protocol.HeadlineCohortError, "preflight_sha256_mismatch"):
                protocol._validate_live_control_chain(
                    authorization,
                    stage="DIAGNOSTIC",
                    execution_freeze_path=protocol.DIAGNOSTIC_EXECUTION_FREEZE_PATH,
                    preflight_path=protocol.DIAGNOSTIC_PREFLIGHT_PATH,
                )

    def test_live_path_requires_stage_control_documents(self) -> None:
        reads: list[Path] = []

        def fake_read(path: Path, *, maximum_bytes: int, exact_mode: int) -> bytes:
            reads.append(path)
            return b"{}"

        with patch.object(protocol, "_read_fixed_control_file", side_effect=fake_read):
            with self.assertRaisesRegex(protocol.HeadlineCohortError, "control_document_keys_invalid"):
                protocol._validate_live_control_chain(
                    {},
                    stage="DIAGNOSTIC",
                    execution_freeze_path=protocol.DIAGNOSTIC_EXECUTION_FREEZE_PATH,
                    preflight_path=protocol.DIAGNOSTIC_PREFLIGHT_PATH,
                )
        self.assertEqual(
            reads,
            [protocol.DIAGNOSTIC_EXECUTION_FREEZE_PATH, protocol.DIAGNOSTIC_PREFLIGHT_PATH],
        )

    def test_strict_json_rejects_duplicate_keys_and_floats(self) -> None:
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "duplicate_json_key"):
            protocol.strict_json_loads('{"a":1,"a":2}')
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "floating_point"):
            protocol.canonical_json({"a": 1.5})

    def test_headline_template_seals_with_three_deterministic_targets(self) -> None:
        authorization = _headline_authorization()
        self.assertEqual(authorization["exact_headline_denominator"], 3)
        self.assertEqual(authorization["headline_http_attempt_cap"], 6)
        self.assertEqual(authorization["headline_budget_microcny"], 117504)
        self.assertEqual(protocol.validate_authorization(authorization, now_utc=NOW), authorization)
        self.assertEqual(protocol.cohort_manifest()["parent_gate_a_cohort_manifest_sha256"], protocol.GATE_A_COHORT_MANIFEST_SHA256)
        for ordinal, target in enumerate(protocol.HEADLINE_TARGETS, start=1):
            self.assertEqual(protocol._validate_reconstructed_target(target, ordinal), target)

    def test_seal_requires_a_future_window_and_exact_approval(self) -> None:
        candidate = _headline_candidate()
        candidate["authorization_window_start_utc"] = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "authorization_window_must_be_future"):
            protocol.seal_authorization(candidate, now_utc=NOW)
        authorization = _headline_authorization()
        binding = protocol.approval_binding_sha256(authorization)
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "headline_approval_text_mismatch"):
            protocol.validate_approval_text("APPROVE PHASE11C HEADLINE_COHORT bad", binding)
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "authorization_window_not_active"):
            protocol.validate_authorization(authorization, now_utc=NOW + timedelta(minutes=11))

    def test_nonoverlap_and_binding_drift_are_fail_closed(self) -> None:
        candidate = _headline_candidate()
        candidate["auth004_intersection_count"] = 1
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "auth004_nonoverlap_mismatch"):
            protocol.seal_authorization(candidate, now_utc=SEAL_NOW)
        authorization, diagnostic_authorization, diagnostic_receipt = _lineage()
        authorization["image_sha256"] = _sha("f")
        authorization["authorization_sha256"] = ""
        changed = protocol.seal_authorization(authorization, now_utc=SEAL_NOW)
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "diagnostic_freeze_binding_mismatch"):
            protocol.validate_headline_diagnostic_lineage(changed, diagnostic_authorization, diagnostic_receipt, now_utc=NOW)


class DiagnosticTests(unittest.TestCase):
    def test_same_image_diagnostic_is_eligible_and_redacted(self) -> None:
        authorization, receipt = _completed_diagnostic()
        self.assertEqual(receipt["execution_status"], "completed")
        self.assertEqual(receipt["authorization_sha256"], authorization["authorization_sha256"])
        self.assertEqual(receipt["reserved_microcny"], 19584)
        self.assertTrue(receipt["redaction_applied"])
        self.assertEqual(protocol.validate_completed_diagnostic_receipt(receipt), receipt)

    def test_diagnostic_usage_or_terminal_mismatch_is_not_eligible(self) -> None:
        authorization = _diagnostic_authorization()
        binding = protocol.diagnostic_approval_binding_sha256(authorization)
        receipt = protocol.execute_diagnostic(
            authorization,
            protocol.expected_diagnostic_approval_text(binding),
            store=protocol.InMemoryDiagnosticStateStore(),
            credential_reader=FakeCredential(),
            transport=FakeTransport([_diagnostic_response("wrong")]),
            now_utc=NOW,
        )
        self.assertEqual(receipt["execution_status"], "inconclusive")
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "diagnostic_receipt_not_eligible"):
            protocol.validate_completed_diagnostic_receipt(receipt)

    def test_diagnostic_credential_failure_has_zero_actual_calls(self) -> None:
        authorization = _diagnostic_authorization()
        binding = protocol.diagnostic_approval_binding_sha256(authorization)
        receipt = protocol.execute_diagnostic(
            authorization,
            protocol.expected_diagnostic_approval_text(binding),
            store=protocol.InMemoryDiagnosticStateStore(),
            credential_reader=FailingCredential(),
            transport=FakeTransport([]),
            now_utc=NOW,
        )
        self.assertEqual((receipt["logical_call_count"], receipt["provider_call_count"], receipt["http_attempt_count"]), (0, 0, 0))
        self.assertEqual(receipt["terminal_category"], "credential_validation_failed")
        self.assertEqual(protocol.validate_diagnostic_receipt(receipt), receipt)

    def test_offline_diagnostic_reconciliation_seals_counted_dispatch_as_quarantine(self) -> None:
        authorization = _diagnostic_authorization()
        binding = protocol.diagnostic_approval_binding_sha256(authorization)
        state = protocol._new_diagnostic_state(authorization["authorization_sha256"], binding)
        state = protocol._transition_diagnostic_state(state, execution_status="budget_reserved", budget_reserved=True)
        state = protocol._transition_diagnostic_state(state, execution_status="credential_opened", credential_file_opened=True)
        state = protocol._transition_diagnostic_state(state, execution_status="credential_validated", credential_validated=True)
        state = protocol._transition_diagnostic_state(state, execution_status="http_attempted", http_attempt_count=1)
        receipt = protocol.reconcile_interrupted_diagnostic_attempt(authorization, state, now_utc=NOW)
        self.assertEqual(receipt["execution_status"], "quarantined")
        self.assertEqual(receipt["http_attempt_count"], 1)
        self.assertEqual(receipt["estimated_microcny"], 19584)
        self.assertEqual(protocol.validate_diagnostic_receipt(receipt), receipt)


class HeadlineExecutionTests(unittest.TestCase):
    def test_three_successful_targets_use_exactly_six_requests_and_seal_ledger(self) -> None:
        authorization, diagnostic_authorization, diagnostic_receipt = _lineage()
        binding = protocol.approval_binding_sha256(authorization)
        responses: list[protocol.HttpResult] = []
        for ordinal, target in enumerate(protocol.HEADLINE_TARGETS, start=1):
            responses.extend([_tool_response("probe_canary", "probe", target, f"probe-{ordinal}"), _tool_response("submit_canary", "submit", target, f"submit-{ordinal}")])
        transport = FakeTransport(responses)
        store = protocol.InMemoryCohortStateStore()
        receipt = protocol.execute_headline_cohort(
            authorization,
            protocol.expected_approval_text(binding),
            diagnostic_authorization,
            diagnostic_receipt,
            store=store,
            credential_reader=FakeCredential(),
            transport=transport,
            now_utc=NOW,
        )
        self.assertEqual(receipt["execution_status"], "completed")
        self.assertEqual(receipt["http_attempt_count"], 6)
        self.assertEqual(len(transport.calls), 6)
        self.assertEqual([item["execution_status"] for item in store.target_receipts], ["completed"] * 3)
        self.assertEqual(protocol.validate_cohort_receipt(receipt), receipt)
        self.assertIsNotNone(store.ledger)
        self.assertEqual(protocol.validate_ledger(store.ledger), store.ledger)
        encoded = protocol.canonical_json({"receipt": receipt, "ledger": store.ledger}).decode("ascii")
        self.assertNotIn("fake-key", encoded)
        self.assertNotIn("synthetic_probe_ok", encoded)

    def test_text_response_stops_and_writes_full_denominator(self) -> None:
        authorization, diagnostic_authorization, diagnostic_receipt = _lineage()
        binding = protocol.approval_binding_sha256(authorization)
        text = protocol.HttpResult(200, json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "text"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}, separators=(",", ":")).encode("utf-8"))
        transport = FakeTransport([text])
        store = protocol.InMemoryCohortStateStore()
        receipt = protocol.execute_headline_cohort(
            authorization, protocol.expected_approval_text(binding), diagnostic_authorization, diagnostic_receipt,
            store=store, credential_reader=FakeCredential(), transport=transport, now_utc=NOW,
        )
        self.assertEqual(receipt["execution_status"], "inconclusive")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual([item["execution_status"] for item in store.target_receipts], ["inconclusive", "not_run_gate_blocked", "not_run_gate_blocked"])

    def test_credential_failure_preserves_three_target_denominator_without_transport(self) -> None:
        authorization, diagnostic_authorization, diagnostic_receipt = _lineage()
        binding = protocol.approval_binding_sha256(authorization)
        transport = FakeTransport([])
        store = protocol.InMemoryCohortStateStore()
        receipt = protocol.execute_headline_cohort(
            authorization, protocol.expected_approval_text(binding), diagnostic_authorization, diagnostic_receipt,
            store=store, credential_reader=FailingCredential(), transport=transport, now_utc=NOW,
        )
        self.assertEqual(receipt["execution_status"], "failed")
        self.assertEqual(len(transport.calls), 0)
        self.assertEqual(len(store.target_receipts), 3)
        self.assertEqual(store.target_receipts[0]["terminal_category"], "credential_validation_failed")
        self.assertEqual([item["execution_status"] for item in store.target_receipts[1:]], ["not_run_gate_blocked", "not_run_gate_blocked"])

    def test_lineage_hash_drift_and_missing_credential_callback_fail_before_transport(self) -> None:
        authorization, diagnostic_authorization, diagnostic_receipt = _lineage()
        binding = protocol.approval_binding_sha256(authorization)
        changed_authorization = dict(authorization)
        changed_authorization["diagnostic_receipt_sha256"] = _sha("e")
        changed_authorization["authorization_sha256"] = ""
        changed_authorization = protocol.seal_authorization(changed_authorization, now_utc=SEAL_NOW)
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "diagnostic_receipt_binding_mismatch"):
            protocol.execute_headline_cohort(
                changed_authorization, protocol.expected_approval_text(protocol.approval_binding_sha256(changed_authorization)), diagnostic_authorization, diagnostic_receipt,
                store=protocol.InMemoryCohortStateStore(), credential_reader=FakeCredential(), transport=FakeTransport([]), now_utc=NOW,
            )
        store = protocol.InMemoryCohortStateStore()
        receipt = protocol.execute_headline_cohort(
            authorization, protocol.expected_approval_text(binding), diagnostic_authorization, diagnostic_receipt,
            store=store, credential_reader=MissingCallbackCredential(), transport=FakeTransport([]), now_utc=NOW,
        )
        self.assertEqual(receipt["execution_status"], "failed")
        self.assertEqual(receipt["http_attempt_count"], 0)

    def test_tool_target_mismatch_and_unknown_usage_stop_immediately(self) -> None:
        authorization, diagnostic_authorization, diagnostic_receipt = _lineage()
        binding = protocol.approval_binding_sha256(authorization)
        wrong_target = dict(protocol.HEADLINE_TARGETS[0])
        wrong_target["payload_sha256"] = _sha("f")
        wrong = _tool_response("probe_canary", "probe", wrong_target, "probe-1")
        store = protocol.InMemoryCohortStateStore()
        receipt = protocol.execute_headline_cohort(
            authorization, protocol.expected_approval_text(binding), diagnostic_authorization, diagnostic_receipt,
            store=store, credential_reader=FakeCredential(), transport=FakeTransport([wrong]), now_utc=NOW,
        )
        self.assertEqual(receipt["execution_status"], "failed")
        self.assertEqual(store.target_receipts[0]["terminal_category"], "tool_target_mismatch")
        authorization, diagnostic_authorization, diagnostic_receipt = _lineage()
        binding = protocol.approval_binding_sha256(authorization)
        no_usage = _tool_response("probe_canary", "probe", protocol.HEADLINE_TARGETS[0], "probe-1", usage=False)
        receipt = protocol.execute_headline_cohort(
            authorization, protocol.expected_approval_text(binding), diagnostic_authorization, diagnostic_receipt,
            store=protocol.InMemoryCohortStateStore(), credential_reader=FakeCredential(), transport=FakeTransport([no_usage]), now_utc=NOW,
        )
        self.assertEqual(receipt["execution_status"], "inconclusive")

    def test_unknown_provider_response_fields_fail_closed_before_submit(self) -> None:
        authorization, diagnostic_authorization, diagnostic_receipt = _lineage()
        binding = protocol.approval_binding_sha256(authorization)
        target = protocol.HEADLINE_TARGETS[0]
        body = json.loads(_tool_response("probe_canary", "probe", target, "probe-1").body.decode("utf-8"))
        body["unfrozen_provider_extension"] = {"opaque": True}
        transport = FakeTransport([protocol.HttpResult(200, json.dumps(body, separators=(",", ":")).encode("utf-8"))])
        store = protocol.InMemoryCohortStateStore()
        receipt = protocol.execute_headline_cohort(
            authorization,
            protocol.expected_approval_text(binding),
            diagnostic_authorization,
            diagnostic_receipt,
            store=store,
            credential_reader=FakeCredential(),
            transport=transport,
            now_utc=NOW,
        )
        self.assertEqual(receipt["execution_status"], "failed")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(store.target_receipts[0]["terminal_category"], "provider_response_schema_invalid")


class ReceiptAndStateTests(unittest.TestCase):
    def test_offline_reconciliation_seals_interrupted_target_without_replay(self) -> None:
        authorization, _, _ = _lineage()
        binding = protocol.approval_binding_sha256(authorization)
        state = protocol._new_state(authorization["authorization_sha256"], binding)
        state = protocol._transition_state(
            state,
            execution_status="budget_reserved",
            budget_reserved=True,
            reserved_input_tokens=12000,
            reserved_output_tokens=768,
            reserved_microcny=117504,
        )
        state = protocol._transition_state(state, execution_status="credential_opened", credential_file_opened=True)
        state = protocol._transition_state(state, execution_status="credential_validated", credential_validated=True)
        state = protocol._transition_state(
            state,
            execution_status="running",
            current_target_ordinal=1,
            logical_call_count=1,
            provider_call_count=1,
            http_attempt_count=1,
        )
        receipts, envelope = protocol.reconcile_interrupted_headline_attempt(
            authorization, state, [], now_utc=NOW
        )
        self.assertEqual(
            [item["execution_status"] for item in receipts],
            ["quarantined", "not_run_gate_blocked", "not_run_gate_blocked"],
        )
        self.assertEqual(receipts[0]["http_attempt_count"], 1)
        self.assertEqual(envelope["cohort_receipt"]["execution_status"], "quarantined")
        self.assertEqual(envelope["ledger"]["http_attempt_count"], 1)
        self.assertEqual(protocol.validate_terminal_envelope(envelope), envelope)

    def test_offline_reconciliation_preserves_completed_prefix_and_attempt_count(self) -> None:
        authorization, _, _ = _lineage()
        binding = protocol.approval_binding_sha256(authorization)
        first = protocol._target_receipt(
            authorization=authorization,
            binding_sha=binding,
            ordinal=1,
            target=protocol.HEADLINE_TARGETS[0],
            execution_status="completed",
            terminal_category="provider_tool_submit",
            finish_reason_category="tool_calls",
            response_shape_category="tool_call",
            tool_call_present=True,
            submit_attempt_count=1,
            logical_call_count=2,
            provider_call_count=2,
            http_attempt_count=2,
            usage_known=True,
            input_tokens_used=1,
            output_tokens_used=1,
            estimated_microcny=36,
            http_status_class="2xx",
            provider_response_sha256=_sha("d"),
            tool_call_sha256=_sha("e"),
        )
        state = protocol._new_state(authorization["authorization_sha256"], binding)
        state = protocol._transition_state(
            state,
            execution_status="budget_reserved",
            budget_reserved=True,
            reserved_input_tokens=12000,
            reserved_output_tokens=768,
            reserved_microcny=117504,
        )
        state = protocol._transition_state(state, execution_status="credential_opened", credential_file_opened=True)
        state = protocol._transition_state(state, execution_status="credential_validated", credential_validated=True)
        state = protocol._transition_state(
            state,
            execution_status="running",
            current_target_ordinal=2,
            next_target_ordinal=2,
            logical_call_count=3,
            provider_call_count=3,
            http_attempt_count=3,
        )
        receipts, envelope = protocol.reconcile_interrupted_headline_attempt(
            authorization, state, [first], now_utc=NOW
        )
        self.assertEqual([item["execution_status"] for item in receipts], ["completed", "quarantined", "not_run_gate_blocked"])
        self.assertEqual(receipts[1]["http_attempt_count"], 1)
        self.assertEqual(envelope["ledger"]["http_attempt_count"], 3)

    def test_target_and_ledger_tampering_are_detected(self) -> None:
        authorization, diagnostic_authorization, diagnostic_receipt = _lineage()
        binding = protocol.approval_binding_sha256(authorization)
        target = protocol._target_receipt(
            authorization=authorization, binding_sha=binding, ordinal=1, target=protocol.HEADLINE_TARGETS[0],
            execution_status="not_run_gate_blocked", terminal_category="not_run_gate_blocked", estimated_microcny=0,
        )
        target["raw_retained"] = True
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "target_receipt_retention_or_retry_invalid"):
            protocol.validate_target_receipt(target)
        successful = []
        for ordinal, item in enumerate(protocol.HEADLINE_TARGETS, start=1):
            successful.append(protocol._target_receipt(
                authorization=authorization, binding_sha=binding, ordinal=ordinal, target=item,
                execution_status="completed", terminal_category="provider_tool_submit", finish_reason_category="tool_calls",
                response_shape_category="tool_call", tool_call_present=True, submit_attempt_count=1,
                logical_call_count=2, provider_call_count=2, http_attempt_count=2, usage_known=True,
                input_tokens_used=1, output_tokens_used=1, estimated_microcny=36,
                http_status_class="2xx", provider_response_sha256=_sha(chr(96 + ordinal)), tool_call_sha256=_sha(chr(99 + ordinal)),
            ))
        cohort = protocol.build_cohort_receipt(
            authorization=authorization, binding_sha=binding, target_receipts=successful, execution_status="completed", stopped_after_ordinal=3,
        )
        ledger = protocol.build_ledger(cohort)
        ledger["retry_count"] = 1
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "ledger_accounting_or_redaction_invalid"):
            protocol.validate_ledger(ledger)

    def test_state_requires_target_start_and_monotonicity(self) -> None:
        state = protocol._new_state(_sha("a"), _sha("b"))
        state = protocol._transition_state(
            state,
            execution_status="budget_reserved",
            budget_reserved=True,
            reserved_input_tokens=12000,
            reserved_output_tokens=768,
            reserved_microcny=117504,
        )
        state = protocol._transition_state(state, execution_status="credential_opened", credential_file_opened=True)
        state = protocol._transition_state(state, execution_status="credential_validated", credential_validated=True)
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "state_target_start_invariant_failed"):
            protocol._transition_state(state, execution_status="running")
        state = protocol._transition_state(state, execution_status="running", current_target_ordinal=1)
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "state_transition_rollback"):
            protocol._transition_state(state, next_target_ordinal=0)

    def test_credential_metadata_and_value_validation(self) -> None:
        metadata = protocol.CredentialMetadata(True, 0, 0o600, 1, 8, 1, 1)
        protocol.validate_credential_metadata(metadata)
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "credential_permissions_denied"):
            protocol.validate_credential_metadata(protocol.CredentialMetadata(True, 0, 0o644, 1, 8, 1, 1))
        self.assertEqual(protocol.validate_live_credential("fake-key"), "fake-key")
        with self.assertRaisesRegex(protocol.HeadlineCohortError, "credential_format_invalid"):
            protocol.validate_live_credential("bad key")


class ArtifactAndSourceGuardTests(unittest.TestCase):
    def test_declared_schemas_are_valid_json_and_match_runtime_field_sets(self) -> None:
        schemas = {
            "phase11c-gateb-protocol-diagnostic-authorization.schema.json": protocol.DIAGNOSTIC_AUTHORIZATION_FIELDS,
            "phase11c-gateb-protocol-diagnostic-receipt.schema.json": protocol.DIAGNOSTIC_RECEIPT_FIELDS,
            "phase11c-gateb-headline-cohort-authorization.schema.json": protocol.AUTHORIZATION_FIELDS,
            "phase11c-gateb-headline-cohort-target-receipt.schema.json": protocol.TARGET_RECEIPT_FIELDS,
            "phase11c-gateb-headline-cohort-receipt.schema.json": protocol.COHORT_RECEIPT_FIELDS,
            "phase11c-gateb-headline-cohort-ledger.schema.json": protocol.LEDGER_FIELDS,
        }
        for name, fields in schemas.items():
            document = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
            self.assertFalse(document["additionalProperties"])
            self.assertEqual(set(document["required"]), set(fields))
            if name == "phase11c-gateb-headline-cohort-target-receipt.schema.json":
                self.assertIn("nonzeroSha256", document["$defs"])

    def test_compose_is_exact_image_no_build_and_docker_is_minimal(self) -> None:
        compose = (ROOT / "compose.phase11c-gateb-headline.yml").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile.phase11c-gateb-headline").read_text(encoding="utf-8")
        self.assertIn("pull_policy: never", compose)
        self.assertNotIn("build:", compose)
        self.assertIn("read_only: true", compose)
        self.assertIn("cap_drop:", compose)
        diagnostic = compose.split("gateb-protocol-diagnostic:", 1)[1].split("gateb-protocol-headline:", 1)[0]
        self.assertIn("run-diagnostic", diagnostic)
        self.assertNotIn("/run/crag-gateb-headline/", diagnostic)
        headline = compose.split("gateb-protocol-headline:", 1)[1].split("gateb-protocol-recovery:", 1)[0]
        self.assertIn("run-headline", headline)
        self.assertNotIn("/run/crag-gateb-diagnostic/approval.txt", headline)
        self.assertIn("/run/crag-gateb-diagnostic/execution-freeze.json", headline)
        self.assertIn("/run/crag-gateb-diagnostic/preflight.json", headline)
        self.assertIn("gateb-protocol-recovery:", compose)
        recovery = compose.split("gateb-protocol-recovery:", 1)[1]
        self.assertIn("network_mode: none", recovery)
        self.assertNotIn("glm_api_key", recovery)
        self.assertNotIn("approval.txt", recovery)
        diagnostic_recovery = compose.split("gateb-protocol-diagnostic-recovery:", 1)[1]
        self.assertIn("network_mode: none", diagnostic_recovery)
        self.assertNotIn("glm_api_key", diagnostic_recovery)
        self.assertNotIn("approval.txt", diagnostic_recovery)
        self.assertIn("phase11c_gateb_headline_cohort_executor.py", dockerfile)

    def test_source_has_no_sdk_proxy_or_publisher_and_no_runtime_raw_persistence(self) -> None:
        source = Path(protocol.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertTrue({"requests", "openai", "subprocess"}.isdisjoint(imported))
        for forbidden in ("HTTP_PROXY", "HTTPS_PROXY", "github", "os.environ"):
            self.assertNotIn(forbidden, source)
        self.assertIn("TLSv1_2", source)
        self.assertIn("CERT_REQUIRED", source)
        continuation = protocol.continuation_body_for(protocol.HEADLINE_TARGETS[0], "probe_1").decode("utf-8")
        self.assertRegex(protocol.request_protocol_sha256(), "^[0-9a-f]{64}$")
        self.assertIn("synthetic_probe_ok", continuation)

    def test_offline_recovery_entrypoints_cannot_construct_credential_or_transport(self) -> None:
        for entrypoint in (protocol.recover_diagnostic_from_fixed_files, protocol.recover_headline_from_fixed_files):
            tree = ast.parse(inspect.getsource(entrypoint))
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            self.assertTrue({"FixedCredentialReader", "FixedHTTPSProviderTransport"}.isdisjoint(names))


if __name__ == "__main__":
    unittest.main()
