from __future__ import annotations

import ast
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from typing import Any, Callable
import unittest
from unittest.mock import patch

import phase11c_gateb_one_shot_executor as gateb


NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _candidate(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    value = gateb.build_authorization_template()
    value.update(
        {
            "source_tree_sha256": "1" * 64,
            "dockerfile_sha256": "2" * 64,
            "compose_sha256": "3" * 64,
            "image_sha256": "4" * 64,
            "deployment_sha256": "5" * 64,
            "runtime_identity_sha256": "6" * 64,
            "provider_policy_evidence_sha256": "7" * 64,
            "authorization_window_start_utc": (start or NOW - timedelta(minutes=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "authorization_window_end_utc": (end or NOW + timedelta(minutes=10)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
    )
    return value


def _authorization(**kwargs: Any) -> dict[str, Any]:
    return gateb.seal_authorization(
        _candidate(**kwargs),
        executable_source_digest=gateb.source_sha256(),
        now_utc=NOW,
    )


def _provider_body(
    content: str = gateb.TERMINAL_TOKEN,
    *,
    prompt_tokens: int | None = 12,
    completion_tokens: int | None = 3,
) -> bytes:
    value: dict[str, Any] = {"choices": [{"message": {"content": content}}]}
    if prompt_tokens is not None and completion_tokens is not None:
        value["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class FakeCredentialReader:
    def __init__(self, events: list[str], *, failure: str | None = None, call_opened: bool = True) -> None:
        self.events = events
        self.failure = failure
        self.call_opened = call_opened

    def read(self, on_opened: Callable[[], None]) -> str:
        self.events.append("credential_read_entered")
        if self.call_opened:
            on_opened()
            self.events.append("credential_open_recorded")
        if self.failure is not None:
            raise gateb.GateBOneShotError(self.failure)
        self.events.append("credential_validated_by_reader")
        return "offline-fake-credential"


class FakeTransport:
    def __init__(
        self,
        store: gateb.InMemoryStateStore,
        events: list[str],
        *,
        status_code: int = 200,
        body: bytes | None = None,
        failure: str | None = None,
    ) -> None:
        self.store = store
        self.events = events
        self.status_code = status_code
        self.body = body if body is not None else _provider_body()
        self.failure = failure
        self.calls = 0

    def dispatch(self, api_key: str) -> gateb.HttpResult:
        state = self.store.state
        if state is None or state["http_attempt_recorded"] is not True:
            raise AssertionError("transport ran before durable attempt")
        if state["credential_validated"] is not True:
            raise AssertionError("transport ran before credential validation")
        if api_key != "offline-fake-credential":
            raise AssertionError("unexpected fake credential")
        self.calls += 1
        self.events.append("transport_dispatched")
        if self.failure is not None:
            raise gateb.GateBOneShotError(self.failure)
        return gateb.HttpResult(self.status_code, self.body)


def _execute(
    *,
    authorization: dict[str, Any] | None = None,
    approval_text: str | None = None,
    credential_failure: str | None = None,
    credential_calls_opened: bool = True,
    transport_status: int = 200,
    transport_body: bytes | None = None,
    transport_failure: str | None = None,
) -> tuple[dict[str, Any], gateb.InMemoryStateStore, FakeTransport, list[str]]:
    authorization_value = authorization or _authorization()
    binding = gateb.approval_binding_sha256(authorization_value)
    store = gateb.InMemoryStateStore()
    events: list[str] = []
    transport = FakeTransport(
        store,
        events,
        status_code=transport_status,
        body=transport_body,
        failure=transport_failure,
    )
    receipt = gateb.execute_one_shot(
        authorization_value,
        approval_text or gateb.expected_approval_text(binding),
        store=store,
        credential_reader=FakeCredentialReader(
            events,
            failure=credential_failure,
            call_opened=credential_calls_opened,
        ),
        transport=transport,
        now_utc=NOW,
        executable_source_digest=gateb.source_sha256(),
    )
    return receipt, store, transport, events


class CanonicalAndAuthorizationTests(unittest.TestCase):
    def test_canonical_json_rejects_float_negative_nonstring_and_duplicate(self) -> None:
        with self.assertRaisesRegex(gateb.GateBOneShotError, "floating_point"):
            gateb.canonical_json({"value": 0.01})
        with self.assertRaisesRegex(gateb.GateBOneShotError, "negative_integer"):
            gateb.canonical_json({"value": -1})
        with self.assertRaisesRegex(gateb.GateBOneShotError, "non_string_json_key"):
            gateb.canonical_json({1: "value"})
        with self.assertRaisesRegex(gateb.GateBOneShotError, "duplicate_json_key"):
            gateb.strict_json_loads('{"a":1,"a":2}')

    def test_request_endpoint_and_cohort_are_fixed(self) -> None:
        request = json.loads(gateb.REQUEST_BODY)
        self.assertEqual(request["model"], "glm-5.2")
        self.assertEqual(request["max_tokens"], 128)
        self.assertFalse(request["stream"])
        self.assertEqual(request["thinking"], {"type": "disabled"})
        self.assertEqual(request["temperature"], 0.01)
        self.assertIn(gateb.TERMINAL_TOKEN, request["messages"][0]["content"])
        self.assertRegex(gateb.request_sha256(), r"^[0-9a-f]{64}$")
        self.assertRegex(gateb.endpoint_sha256(), r"^[0-9a-f]{64}$")
        self.assertRegex(gateb.cohort_sha256(), r"^[0-9a-f]{64}$")

    def test_template_exposes_only_pending_external_bindings(self) -> None:
        template = gateb.build_authorization_template()
        self.assertEqual(set(template), gateb.AUTHORIZATION_FIELDS)
        self.assertEqual(template["executable_source_sha256"], gateb.source_sha256())
        self.assertEqual(template["source_tree_sha256"], gateb.PENDING_FREEZE)
        self.assertEqual(template["provider_policy_evidence_sha256"], gateb.PENDING_FREEZE)
        self.assertEqual(template["diagnostic_budget_microcny"], 19_584)
        self.assertEqual(template["aggregate_budget_microcny"], 15_000_000)

    def test_seal_and_validate_bind_every_fixed_value(self) -> None:
        authorization = _authorization()
        validated = gateb.validate_authorization(
            authorization,
            executable_source_digest=gateb.source_sha256(),
            now_utc=NOW,
        )
        self.assertEqual(validated, authorization)
        self.assertEqual(
            gateb.worst_case_microcny(
                input_tokens=2_000,
                output_tokens=128,
                input_rate=8_000_000,
                output_rate=28_000_000,
            ),
            19_584,
        )

    def test_authorization_tamper_and_source_drift_are_refused(self) -> None:
        authorization = _authorization()
        tampered = deepcopy(authorization)
        tampered["image_sha256"] = "8" * 64
        with self.assertRaisesRegex(gateb.GateBOneShotError, "authorization_sha256_mismatch"):
            gateb.validate_authorization(tampered, now_utc=NOW)
        with self.assertRaisesRegex(gateb.GateBOneShotError, "executable_source_sha256_drift"):
            gateb.validate_authorization(
                authorization,
                executable_source_digest="9" * 64,
                now_utc=NOW,
            )

    def test_bool_float_and_negative_accounting_are_refused(self) -> None:
        for field, bad_value in (
            ("max_input_tokens", True),
            ("max_output_tokens", 128.0),
            ("diagnostic_budget_microcny", -1),
        ):
            candidate = _candidate()
            candidate[field] = bad_value
            with self.subTest(field=field):
                with self.assertRaises(gateb.GateBOneShotError):
                    gateb.seal_authorization(
                        candidate,
                        executable_source_digest=gateb.source_sha256(),
                        now_utc=NOW,
                    )

    def test_window_must_be_active_and_no_longer_than_thirty_minutes(self) -> None:
        future = _authorization(start=NOW + timedelta(minutes=1), end=NOW + timedelta(minutes=5))
        with self.assertRaisesRegex(gateb.GateBOneShotError, "authorization_window_not_active"):
            gateb.validate_authorization(future, now_utc=NOW)
        gateb.validate_authorization(future, now_utc=NOW, require_active_window=False)
        with self.assertRaisesRegex(gateb.GateBOneShotError, "authorization_window_invalid"):
            gateb.seal_authorization(
                _candidate(start=NOW, end=NOW + timedelta(minutes=31)),
                executable_source_digest=gateb.source_sha256(),
                now_utc=NOW,
            )
        with self.assertRaisesRegex(gateb.GateBOneShotError, "authorization_window_expired"):
            gateb.seal_authorization(
                _candidate(start=NOW - timedelta(minutes=10), end=NOW),
                executable_source_digest=gateb.source_sha256(),
                now_utc=NOW,
            )

    def test_exact_approval_has_no_whitespace_tolerance(self) -> None:
        binding = gateb.approval_binding_sha256(_authorization())
        expected = gateb.expected_approval_text(binding)
        gateb.validate_approval_text(expected, binding)
        for bad in (expected + "\n", " " + expected, expected.lower(), expected[:-1] + "0"):
            with self.subTest(bad=bad[-5:]):
                with self.assertRaisesRegex(gateb.GateBOneShotError, "approval_text_mismatch"):
                    gateb.validate_approval_text(bad, binding)


class ExecutionTests(unittest.TestCase):
    def test_success_orders_irreversible_steps_and_writes_safe_receipt(self) -> None:
        receipt, store, transport, events = _execute()
        self.assertEqual(receipt["execution_status"], "completed")
        self.assertEqual(receipt["terminal_category"], "provider_terminal_match")
        self.assertTrue(receipt["terminal_match"])
        self.assertEqual(receipt["http_attempt_count"], 1)
        self.assertEqual(receipt["reserved_microcny"], 19_584)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(
            store.events,
            [
                "approval_consumed",
                "budget_reserved",
                "credential_opened",
                "credential_validated",
                "http_attempted",
                "terminal",
                "receipt_written",
            ],
        )
        self.assertEqual(
            events,
            [
                "credential_read_entered",
                "credential_open_recorded",
                "credential_validated_by_reader",
                "transport_dispatched",
            ],
        )
        self.assertEqual(store.receipt, receipt)
        self.assertEqual(gateb.validate_receipt(receipt), receipt)

    def test_wrong_approval_consumes_nothing(self) -> None:
        authorization = _authorization()
        store = gateb.InMemoryStateStore()
        events: list[str] = []
        with self.assertRaisesRegex(gateb.GateBOneShotError, "approval_text_mismatch"):
            gateb.execute_one_shot(
                authorization,
                "APPROVE SOMETHING ELSE",
                store=store,
                credential_reader=FakeCredentialReader(events),
                transport=FakeTransport(store, events),
                now_utc=NOW,
                executable_source_digest=gateb.source_sha256(),
            )
        self.assertIsNone(store.state)
        self.assertEqual(events, [])

    def test_credential_failure_keeps_approval_and_budget_and_never_dispatches(self) -> None:
        receipt, store, transport, _ = _execute(credential_failure="credential_fingerprint_mismatch")
        self.assertEqual(receipt["execution_status"], "failed")
        self.assertEqual(receipt["terminal_category"], "credential_validation_failed")
        self.assertTrue(receipt["credential_file_opened"])
        self.assertFalse(receipt["credential_validated"])
        self.assertEqual(receipt["reserved_microcny"], 19_584)
        self.assertEqual(receipt["http_attempt_count"], 0)
        self.assertEqual(transport.calls, 0)
        authorization = _authorization()
        binding = gateb.approval_binding_sha256(authorization)
        with self.assertRaisesRegex(gateb.GateBOneShotError, "one_shot_already_consumed"):
            gateb.execute_one_shot(
                authorization,
                gateb.expected_approval_text(binding),
                store=store,
                credential_reader=FakeCredentialReader([]),
                transport=transport,
                now_utc=NOW,
                executable_source_digest=gateb.source_sha256(),
            )

    def test_transport_failure_is_one_attempt_and_not_retried(self) -> None:
        receipt, _, transport, _ = _execute(transport_failure="provider_transport_failure")
        self.assertEqual(receipt["terminal_category"], "provider_transport_failure")
        self.assertEqual(receipt["http_attempt_count"], 1)
        self.assertEqual(receipt["provider_call_count"], 1)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(receipt["retry_count"], 0)

    def test_executor_rechecks_fake_transport_response_bound(self) -> None:
        receipt, _, transport, _ = _execute(
            transport_body=b"x" * (gateb.MAX_PROVIDER_RESPONSE_BYTES + 1)
        )
        self.assertEqual(receipt["terminal_category"], "provider_response_too_large")
        self.assertEqual(receipt["provider_response_sha256"], gateb.ZERO_SHA256)
        self.assertEqual(transport.calls, 1)

    def test_redirect_and_http_error_are_never_followed(self) -> None:
        for status, category, status_class in (
            (302, "redirect_refused", "3xx"),
            (401, "http_status_failure", "4xx"),
            (503, "http_status_failure", "5xx"),
        ):
            with self.subTest(status=status):
                receipt, _, transport, _ = _execute(
                    transport_status=status,
                    transport_body=b"provider body deliberately discarded",
                )
                self.assertEqual(receipt["terminal_category"], category)
                self.assertEqual(receipt["http_status_class"], status_class)
                self.assertEqual(transport.calls, 1)
                self.assertNotIn("provider body", json.dumps(receipt))

    def test_terminal_mismatch_is_inconclusive(self) -> None:
        receipt, _, _, _ = _execute(transport_body=_provider_body("not-the-terminal"))
        self.assertEqual(receipt["execution_status"], "inconclusive")
        self.assertEqual(receipt["terminal_category"], "provider_terminal_mismatch")
        self.assertFalse(receipt["terminal_match"])

    def test_surrounding_whitespace_is_allowed_only_in_provider_content(self) -> None:
        receipt, _, _, _ = _execute(transport_body=_provider_body(" \nPHASE11C_GATEB_OK\t"))
        self.assertEqual(receipt["execution_status"], "completed")

    def test_missing_usage_keeps_reservation_and_marks_usage_unknown(self) -> None:
        receipt, _, _, _ = _execute(
            transport_body=_provider_body(prompt_tokens=None, completion_tokens=None)
        )
        self.assertFalse(receipt["usage_known"])
        self.assertEqual(receipt["input_tokens_used"], 0)
        self.assertEqual(receipt["output_tokens_used"], 0)
        self.assertEqual(receipt["estimated_microcny"], 19_584)
        self.assertEqual(receipt["reserved_microcny"], 19_584)

    def test_usage_cap_excess_is_failed_but_recorded(self) -> None:
        receipt, _, _, _ = _execute(transport_body=_provider_body(prompt_tokens=2_001))
        self.assertEqual(receipt["terminal_category"], "provider_usage_cap_exceeded")
        self.assertTrue(receipt["usage_known"])
        self.assertEqual(receipt["input_tokens_used"], 2_001)
        self.assertGreater(receipt["estimated_microcny"], 0)

    def test_invalid_provider_json_and_schema_map_to_stable_categories(self) -> None:
        cases = (
            (b"not-json", "provider_response_invalid_json"),
            (b'{"choices":[],"choices":[]}', "provider_response_invalid_json"),
            (b'{"choices":[]}', "provider_response_schema_invalid"),
            (_provider_body(prompt_tokens=-1), "provider_usage_schema_invalid"),
        )
        for body, expected in cases:
            with self.subTest(expected=expected):
                receipt, _, _, _ = _execute(transport_body=body)
                self.assertEqual(receipt["execution_status"], "failed")
                self.assertEqual(receipt["terminal_category"], expected)

    def test_receipt_contains_no_key_prompt_response_path_or_exception(self) -> None:
        receipt, _, _, _ = _execute(transport_body=_provider_body("sensitive raw output"))
        serialized = gateb.canonical_json(receipt).decode("ascii").lower()
        for fragment in (
            "offline-fake-credential",
            "sensitive raw output",
            "deterministic synthetic protocol canary",
            "authorization: bearer",
            "/run/",
            "open.bigmodel.cn",
            "exception",
        ):
            self.assertNotIn(fragment, serialized)

    def test_receipt_validator_rejects_resealed_accounting_or_order_drift(self) -> None:
        receipt, _, _, _ = _execute()
        cases = (
            ("reserved_microcny", 19_583, "receipt_reservation_invalid"),
            ("http_attempt_count", 0, "receipt_attempt_count_invalid"),
            ("credential_validated", False, "receipt_http_order_invalid"),
        )
        for field, value, expected in cases:
            tampered = deepcopy(receipt)
            tampered[field] = value
            tampered["receipt_sha256"] = ""
            resealed = gateb._seal(tampered, "receipt_sha256")
            with self.subTest(field=field):
                with self.assertRaisesRegex(gateb.GateBOneShotError, expected):
                    gateb.validate_receipt(resealed)


class CredentialAndPersistenceTests(unittest.TestCase):
    def test_synthetic_credential_metadata_is_strict(self) -> None:
        good = gateb.CredentialMetadata(True, 0, 0o600, 1, 49, 8, 9)
        gateb.validate_credential_metadata(good)
        bad_values = (
            gateb.CredentialMetadata(False, 0, 0o600, 1, 49, 8, 9),
            gateb.CredentialMetadata(True, 1000, 0o600, 1, 49, 8, 9),
            gateb.CredentialMetadata(True, 0, 0o640, 1, 49, 8, 9),
            gateb.CredentialMetadata(True, 0, 0o600, 2, 49, 8, 9),
            gateb.CredentialMetadata(True, 0, 0o600, 1, 0, 8, 9),
            gateb.CredentialMetadata(True, 0, 0o600, 1, 4097, 8, 9),
        )
        for metadata in bad_values:
            with self.subTest(metadata=metadata):
                with self.assertRaises(gateb.GateBOneShotError):
                    gateb.validate_credential_metadata(metadata)

    def test_state_rejects_rollback_and_boolean_counters(self) -> None:
        state = gateb._new_state("1" * 64, "2" * 64)
        reserved = gateb._transition_state(
            state,
            execution_status="budget_reserved",
            budget_reserved=True,
            reserved_input_tokens=2_000,
            reserved_output_tokens=128,
            reserved_microcny=19_584,
        )
        with self.assertRaisesRegex(gateb.GateBOneShotError, "state_transition_rollback"):
            gateb._transition_state(reserved, execution_status="approval_consumed")
        tampered = deepcopy(reserved)
        tampered["logical_call_count"] = True
        tampered["state_sha256"] = ""
        resealed = gateb._seal(tampered, "state_sha256")
        with self.assertRaisesRegex(gateb.GateBOneShotError, "invalid_state_logical_call_count"):
            gateb.validate_state(resealed)

    def test_source_contains_required_linux_durability_primitives(self) -> None:
        source = (ROOT / "phase11c_gateb_one_shot_executor.py").read_text(encoding="utf-8")
        for required in (
            'getattr(os, "O_NOFOLLOW", 0)',
            'getattr(os, "O_CLOEXEC", 0)',
            'getattr(os, "fchmod", None)',
            "os.fstat",
            "os.lstat",
            "fcntl.flock",
            "os.fsync",
            "os.replace",
            "http.client.HTTPSConnection",
            "ssl.create_default_context",
        ):
            self.assertIn(required, source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("os.getenv", source)


class TransportAndArtifactTests(unittest.TestCase):
    def test_https_transport_uses_one_fixed_request_and_does_not_follow_redirect(self) -> None:
        calls: list[tuple[Any, ...]] = []

        class FakeResponse:
            status = 302

            def read(self, maximum: int) -> bytes:
                calls.append(("read", maximum))
                return b"redirect"

        class FakeConnection:
            def __init__(self, host: str, port: int, *, timeout: int, context: object) -> None:
                calls.append(("connect", host, port, timeout, context))

            def request(
                self,
                method: str,
                path: str,
                *,
                body: bytes,
                headers: dict[str, str],
            ) -> None:
                calls.append(("request", method, path, body, set(headers)))

            def getresponse(self) -> FakeResponse:
                calls.append(("getresponse",))
                return FakeResponse()

            def close(self) -> None:
                calls.append(("close",))

        tls_context = object()
        with patch.object(gateb.ssl, "create_default_context", return_value=tls_context), patch.object(
            gateb.http.client, "HTTPSConnection", FakeConnection
        ):
            result = gateb.FixedHTTPSProviderTransport().dispatch("fake")
        self.assertEqual(result.status_code, 302)
        request_calls = [item for item in calls if item[0] == "request"]
        self.assertEqual(len(request_calls), 1)
        self.assertEqual(request_calls[0][1:3], ("POST", gateb.ENDPOINT_PATH))
        self.assertEqual(request_calls[0][3], gateb.REQUEST_BODY)
        self.assertEqual(calls[-1], ("close",))

    def test_https_transport_refuses_oversized_body(self) -> None:
        class FakeResponse:
            status = 200

            def read(self, maximum: int) -> bytes:
                return b"x" * maximum

        class FakeConnection:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def request(self, *args: Any, **kwargs: Any) -> None:
                pass

            def getresponse(self) -> FakeResponse:
                return FakeResponse()

            def close(self) -> None:
                pass

        with patch.object(gateb.http.client, "HTTPSConnection", FakeConnection):
            with self.assertRaisesRegex(gateb.GateBOneShotError, "provider_response_too_large"):
                gateb.FixedHTTPSProviderTransport().dispatch("fake")

    def test_ast_has_no_sdk_proxy_subprocess_or_dynamic_network_import(self) -> None:
        source = (ROOT / "phase11c_gateb_one_shot_executor.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertIn("http.client", imported)
        self.assertIn("ssl", imported)
        self.assertTrue({"requests", "urllib", "urllib.request", "socket", "subprocess", "openai"}.isdisjoint(imported))

    def test_schemas_are_exact_and_match_runtime_fields(self) -> None:
        authorization_schema = json.loads(
            (ROOT / "schemas/phase11c-gateb-one-shot-authorization.schema.json").read_text(
                encoding="utf-8"
            )
        )
        receipt_schema = json.loads(
            (ROOT / "schemas/phase11c-gateb-one-shot-receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(authorization_schema["additionalProperties"])
        self.assertFalse(receipt_schema["additionalProperties"])
        self.assertEqual(set(authorization_schema["required"]), gateb.AUTHORIZATION_FIELDS)
        self.assertEqual(set(receipt_schema["required"]), gateb.RECEIPT_FIELDS)
        self.assertEqual(
            authorization_schema["properties"]["diagnostic_budget_microcny"]["const"],
            gateb.DIAGNOSTIC_BUDGET_MICROCNY,
        )
        self.assertEqual(
            set(receipt_schema["properties"]["terminal_category"]["enum"]),
            gateb.TERMINAL_CATEGORIES - {"none"},
        )

    def test_container_artifacts_enforce_fixed_mounts_and_hardening(self) -> None:
        dockerfile = (ROOT / "Dockerfile.phase11c-gateb").read_text(encoding="utf-8")
        compose = (ROOT / "compose.phase11c-gateb.yml").read_text(encoding="utf-8")
        self.assertIn("phase11c_gateb_one_shot_executor.py", dockerfile)
        self.assertNotIn("requirements", dockerfile.lower())
        for required in (
            "read_only: true",
            "pull_policy: never",
            "cap_drop:",
            "- ALL",
            "no-new-privileges:true",
            "/run/crag-gateb/glm_api_key",
            "/run/crag-gateb/authorization.json",
            "/run/crag-gateb/approval.txt",
            "/var/lib/crag-gateb",
            "restart: \"no\"",
        ):
            self.assertIn(required, compose)
        self.assertNotIn("docker.sock", compose)

    def test_print_template_cli_is_offline_and_safe(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = gateb.main(["print-template"])
        self.assertEqual(result, 0)
        document = json.loads(output.getvalue())
        self.assertEqual(set(document), gateb.AUTHORIZATION_FIELDS)
        self.assertNotIn("api_key", output.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
