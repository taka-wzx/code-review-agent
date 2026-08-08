"""Phase 11C Gate B DIAGNOSTIC freeze mechanics, offline and fail-closed.

This module defines only canonical bindings and fake-only ordering checks.  It has no
provider transport, credential reader, cloud client, subprocess, dotenv loader, or
network import.  ``run-live`` is intentionally a zero-I/O blocked outcome.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


PHASE_ID = "phase11c-gateb-live-diagnostic-v1"
AUTHORIZATION_SCHEMA_VERSION = "phase11c-gateb-live-diagnostic-authorization/v1"
PREFLIGHT_SCHEMA_VERSION = "phase11c-gateb-live-diagnostic-preflight/v1"
RECEIPT_SCHEMA_VERSION = "phase11c-gateb-live-diagnostic-receipt/v1"
APPROVAL_SCHEMA_VERSION = "phase11c-gateb-live-diagnostic-approval/v1"
GATE_A_BASE_COMMIT = "72de06368672d4cc72f7750ee10cb88b6d8aee42"
GATE_A_RUNTIME_CONFIG_SHA256 = "921e87d89b7d4cde206228f8eab0fc755af871aec40ac39f5c1852e12e92b0c2"
GATE_A_PREFLIGHT_SHA256 = "0c66fbb6f1a4582a4e0ee8e01a334ad75c65e032d4fbd301f61f2f0ac977a385"
MAX_AGGREGATE_BUDGET_MICRO_CNY = 15_000_000
OWNER_ACCOUNT = "taka-wzx"
PENDING_FREEZE = "PENDING_FREEZE"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")

AUTHORIZATION_FIELDS = frozenset(
    {
        "schema_version",
        "phase_id",
        "stage",
        "authorization_status",
        "authorization_sha256",
        "gate_a_base_commit_sha",
        "gate_a_runtime_config_sha256",
        "gate_a_preflight_sha256",
        "executable_source_sha256",
        "source_tree_sha256",
        "image_sha256",
        "deployment_sha256",
        "runtime_identity_sha256",
        "cohort_sha256",
        "provider",
        "request_model_id",
        "api_surface",
        "endpoint_id",
        "endpoint_sha256",
        "provider_policy_evidence_sha256",
        "provider_policy_accepted",
        "provider_tariff_sha256",
        "tariff_effective_utc",
        "credential_delivery_mode",
        "credential_fingerprint_sha256",
        "owner_account",
        "owner_reconfirmed",
        "kill_switch_bound",
        "authorization_window_start_utc",
        "authorization_window_end_utc",
        "max_logical_calls",
        "max_http_attempts",
        "max_input_tokens",
        "max_output_tokens",
        "input_rate_microcny_per_million",
        "output_rate_microcny_per_million",
        "cached_input_rate_microcny_per_million",
        "diagnostic_budget_microcny",
        "sdk_retries",
        "transport_retries",
        "concurrency",
        "local_raw_retention",
        "live_execution_enabled",
    }
)

PREFLIGHT_FIELDS = frozenset(
    {
        "schema_version",
        "phase_id",
        "stage",
        "preflight_sha256",
        "authorization_sha256",
        "technical_bindings_valid",
        "provider_policy_accepted",
        "tariff_current",
        "authorization_window_valid",
        "credential_metadata_validated",
        "canary_allowed",
        "real_run_recommended_now",
        "blocking_reason_codes",
    }
)

APPROVAL_FIELDS = frozenset(
    {
        "schema_version",
        "phase_id",
        "stage",
        "approval_binding_sha256",
        "authorization_sha256",
        "preapproval_preflight_sha256",
    }
)

RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "phase_id",
        "stage",
        "receipt_sha256",
        "authorization_sha256",
        "approval_binding_sha256",
        "execution_status",
        "logical_call_count",
        "provider_call_count",
        "http_attempt_count",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_microcny",
        "credential_file_opened",
        "live_execution_enabled",
        "usage_known",
        "blocking_reason_codes",
    }
)

DRAFT_BLOCKING_REASON_CODES = (
    "provider_policy_evidence_missing",
    "provider_tariff_missing",
    "source_tree_binding_missing",
    "image_binding_missing",
    "deployment_binding_missing",
    "runtime_identity_binding_missing",
    "cohort_binding_missing",
    "authorization_window_missing",
    "owner_reconfirmation_missing",
    "kill_switch_binding_missing",
    "credential_fingerprint_missing",
    "diagnostic_human_approval_missing",
    "live_execution_not_implemented",
)

FINAL_PREFLIGHT_BLOCKING_REASON_CODES = (
    "credential_metadata_not_validated",
    "diagnostic_human_approval_missing",
    "live_execution_not_implemented",
)

FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "credential_file_path",
        "credential_value",
        "credential_bytes",
        "host_path",
        "hostname",
        "prompt",
        "messages",
        "provider_response",
        "response",
        "tool_arguments",
        "tool_results",
        "exception_message",
    }
)
FORBIDDEN_VALUE_FRAGMENTS = ("sk-", "bearer ", "api_key=", "-----begin", "/home/")


class GateBLiveDiagnosticError(ValueError):
    """Stable safe refusal for an invalid Gate B binding or fake execution."""


def _fail(code: str) -> None:
    raise GateBLiveDiagnosticError(code)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonicalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < 0:
            _fail("negative_integer")
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        _fail("floating_point")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _fail("non_string_json_key")
            result[key] = _canonicalize(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    _fail("unsupported_canonical_type")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _canonicalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def strict_json_loads(value: str | bytes) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                _fail("duplicate_json_key")
            result[key] = item
        return result

    try:
        parsed = json.loads(value, object_pairs_hook=reject_duplicate_keys)
    except GateBLiveDiagnosticError:
        raise
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise GateBLiveDiagnosticError("invalid_json") from exc
    return _canonicalize(parsed)


def source_sha256() -> str:
    """Hash this fixed source file only; callers cannot select another path."""

    normalized = Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return sha256_bytes(normalized.encode("utf-8"))


def _expect_mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code)
    return dict(value)


def _expect_exact_keys(value: Mapping[str, Any], expected: frozenset[str], code: str) -> None:
    if set(value) != expected:
        _fail(code)


def _expect_sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _fail(code)
    return value


def _expect_git_sha(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _GIT_SHA.fullmatch(value):
        _fail(code)
    return value


def _expect_nonnegative_int(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(code)
    return value


def _expect_bool(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        _fail(code)
    return value


def _seal(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = deepcopy(dict(value))
    if result.get(field) != "":
        _fail("invalid_unsealed_document")
    result[field] = sha256_bytes(canonical_json(result))
    return result


def _validate_seal(value: Mapping[str, Any], field: str, code: str) -> None:
    document = dict(value)
    observed = _expect_sha256(document.get(field), code)
    document[field] = ""
    if sha256_bytes(canonical_json(document)) != observed:
        _fail(code)


def contains_forbidden_content(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in FORBIDDEN_KEYS or contains_forbidden_content(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_forbidden_content(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return any(fragment in lowered for fragment in FORBIDDEN_VALUE_FRAGMENTS)
    return False


def _parse_utc(value: Any, code: str) -> datetime:
    if not isinstance(value, str):
        _fail(code)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise GateBLiveDiagnosticError(code) from exc
    return parsed.replace(tzinfo=timezone.utc)


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def worst_case_microcny(
    *, input_tokens: int, output_tokens: int, input_rate: int, output_rate: int
) -> int:
    for value in (input_tokens, output_tokens, input_rate, output_rate):
        _expect_nonnegative_int(value, "invalid_cost_component")
    return _ceil_div(input_tokens * input_rate, 1_000_000) + _ceil_div(
        output_tokens * output_rate, 1_000_000
    )


def build_draft_authorization(*, executable_source_sha256: str) -> dict[str, Any]:
    _expect_sha256(executable_source_sha256, "invalid_executable_source_sha256")
    return _seal(
        {
            "schema_version": AUTHORIZATION_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "stage": "DIAGNOSTIC",
            "authorization_status": "draft_incomplete",
            "authorization_sha256": "",
            "gate_a_base_commit_sha": GATE_A_BASE_COMMIT,
            "gate_a_runtime_config_sha256": GATE_A_RUNTIME_CONFIG_SHA256,
            "gate_a_preflight_sha256": GATE_A_PREFLIGHT_SHA256,
            "executable_source_sha256": executable_source_sha256,
            "source_tree_sha256": PENDING_FREEZE,
            "image_sha256": PENDING_FREEZE,
            "deployment_sha256": PENDING_FREEZE,
            "runtime_identity_sha256": PENDING_FREEZE,
            "cohort_sha256": PENDING_FREEZE,
            "provider": "glm",
            "request_model_id": "glm-5.2",
            "api_surface": "chat.completions.create",
            "endpoint_id": "glm_standard_v4",
            "endpoint_sha256": PENDING_FREEZE,
            "provider_policy_evidence_sha256": PENDING_FREEZE,
            "provider_policy_accepted": False,
            "provider_tariff_sha256": PENDING_FREEZE,
            "tariff_effective_utc": PENDING_FREEZE,
            "credential_delivery_mode": "local_one_time_secure_file",
            "credential_fingerprint_sha256": PENDING_FREEZE,
            "owner_account": OWNER_ACCOUNT,
            "owner_reconfirmed": False,
            "kill_switch_bound": False,
            "authorization_window_start_utc": PENDING_FREEZE,
            "authorization_window_end_utc": PENDING_FREEZE,
            "max_logical_calls": 0,
            "max_http_attempts": 0,
            "max_input_tokens": 0,
            "max_output_tokens": 0,
            "input_rate_microcny_per_million": 0,
            "output_rate_microcny_per_million": 0,
            "cached_input_rate_microcny_per_million": 0,
            "diagnostic_budget_microcny": 0,
            "sdk_retries": 0,
            "transport_retries": 0,
            "concurrency": 1,
            "local_raw_retention": False,
            "live_execution_enabled": False,
        },
        "authorization_sha256",
    )


def validate_draft_authorization(value: Any, *, executable_source_digest: str | None = None) -> dict[str, Any]:
    authorization = _expect_mapping(value, "invalid_authorization")
    _expect_exact_keys(authorization, AUTHORIZATION_FIELDS, "invalid_authorization_keys")
    if authorization["schema_version"] != AUTHORIZATION_SCHEMA_VERSION:
        _fail("authorization_schema_version_mismatch")
    if authorization["phase_id"] != PHASE_ID or authorization["stage"] != "DIAGNOSTIC":
        _fail("authorization_phase_mismatch")
    if authorization["authorization_status"] != "draft_incomplete":
        _fail("authorization_status_mismatch")
    if authorization["gate_a_base_commit_sha"] != GATE_A_BASE_COMMIT:
        _fail("gate_a_base_commit_mismatch")
    _expect_git_sha(authorization["gate_a_base_commit_sha"], "invalid_gate_a_base_commit_sha")
    for field, expected in (
        ("gate_a_runtime_config_sha256", GATE_A_RUNTIME_CONFIG_SHA256),
        ("gate_a_preflight_sha256", GATE_A_PREFLIGHT_SHA256),
    ):
        if authorization[field] != expected:
            _fail("gate_a_binding_mismatch")
        _expect_sha256(authorization[field], "invalid_gate_a_binding_sha256")
    source = _expect_sha256(authorization["executable_source_sha256"], "invalid_executable_source_sha256")
    if executable_source_digest is not None and source != executable_source_digest:
        _fail("executable_source_sha256_drift")
    for field in (
        "source_tree_sha256",
        "image_sha256",
        "deployment_sha256",
        "runtime_identity_sha256",
        "cohort_sha256",
        "endpoint_sha256",
        "provider_policy_evidence_sha256",
        "provider_tariff_sha256",
        "tariff_effective_utc",
        "credential_fingerprint_sha256",
        "authorization_window_start_utc",
        "authorization_window_end_utc",
    ):
        if authorization[field] != PENDING_FREEZE:
            _fail("draft_binding_not_pending")
    if authorization["provider"] != "glm" or authorization["request_model_id"] != "glm-5.2":
        _fail("provider_model_mismatch")
    if authorization["api_surface"] != "chat.completions.create" or authorization["endpoint_id"] != "glm_standard_v4":
        _fail("endpoint_surface_mismatch")
    if authorization["credential_delivery_mode"] != "local_one_time_secure_file":
        _fail("credential_delivery_mode_mismatch")
    if authorization["owner_account"] != OWNER_ACCOUNT or not _OWNER.fullmatch(authorization["owner_account"]):
        _fail("owner_account_mismatch")
    for field in ("provider_policy_accepted", "owner_reconfirmed", "kill_switch_bound", "live_execution_enabled"):
        if authorization[field] is not False:
            _fail("draft_flag_not_false")
    for field in (
        "max_logical_calls",
        "max_http_attempts",
        "max_input_tokens",
        "max_output_tokens",
        "input_rate_microcny_per_million",
        "output_rate_microcny_per_million",
        "cached_input_rate_microcny_per_million",
        "diagnostic_budget_microcny",
    ):
        if authorization[field] != 0:
            _fail("draft_numeric_not_zero")
    if authorization["sdk_retries"] != 0 or authorization["transport_retries"] != 0 or authorization["concurrency"] != 1:
        _fail("draft_transport_policy_mismatch")
    if authorization["local_raw_retention"] is not False:
        _fail("draft_raw_retention_denied")
    _validate_seal(authorization, "authorization_sha256", "authorization_sha256_mismatch")
    if contains_forbidden_content(authorization):
        _fail("authorization_contains_forbidden_content")
    return authorization


def validate_final_authorization(
    value: Any,
    *,
    executable_source_digest: str | None = None,
    expected_authorization_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate a final binding against the actual current UTC clock."""

    return _validate_final_authorization_at(
        value,
        executable_source_digest=executable_source_digest,
        expected_authorization_sha256=expected_authorization_sha256,
        now_utc=datetime.now(timezone.utc),
    )


def _validate_final_authorization_at(
    value: Any,
    *,
    executable_source_digest: str | None = None,
    expected_authorization_sha256: str | None = None,
    now_utc: datetime,
) -> dict[str, Any]:
    authorization = _expect_mapping(value, "invalid_final_authorization")
    _expect_exact_keys(authorization, AUTHORIZATION_FIELDS, "invalid_final_authorization_keys")
    if authorization["schema_version"] != AUTHORIZATION_SCHEMA_VERSION:
        _fail("authorization_schema_version_mismatch")
    if authorization["phase_id"] != PHASE_ID or authorization["stage"] != "DIAGNOSTIC":
        _fail("authorization_phase_mismatch")
    if authorization["authorization_status"] != "frozen_pending_approval":
        _fail("authorization_status_mismatch")
    if authorization["gate_a_base_commit_sha"] != GATE_A_BASE_COMMIT:
        _fail("gate_a_base_commit_mismatch")
    _expect_git_sha(authorization["gate_a_base_commit_sha"], "invalid_gate_a_base_commit_sha")
    for field, expected in (
        ("gate_a_runtime_config_sha256", GATE_A_RUNTIME_CONFIG_SHA256),
        ("gate_a_preflight_sha256", GATE_A_PREFLIGHT_SHA256),
    ):
        if authorization[field] != expected:
            _fail("gate_a_binding_mismatch")
        _expect_sha256(authorization[field], "invalid_gate_a_binding_sha256")
    source = _expect_sha256(authorization["executable_source_sha256"], "invalid_executable_source_sha256")
    if executable_source_digest is not None and source != executable_source_digest:
        _fail("executable_source_sha256_drift")
    for field in (
        "source_tree_sha256",
        "image_sha256",
        "deployment_sha256",
        "runtime_identity_sha256",
        "cohort_sha256",
        "endpoint_sha256",
        "provider_policy_evidence_sha256",
        "provider_tariff_sha256",
        "credential_fingerprint_sha256",
    ):
        _expect_sha256(authorization[field], f"invalid_{field}")
    if authorization["provider"] != "glm" or authorization["request_model_id"] != "glm-5.2":
        _fail("provider_model_mismatch")
    if authorization["api_surface"] != "chat.completions.create" or authorization["endpoint_id"] != "glm_standard_v4":
        _fail("endpoint_surface_mismatch")
    if authorization["credential_delivery_mode"] != "local_one_time_secure_file":
        _fail("credential_delivery_mode_mismatch")
    if authorization["provider_policy_accepted"] is not True:
        _fail("provider_policy_unaccepted")
    if authorization["owner_account"] != OWNER_ACCOUNT or not _OWNER.fullmatch(authorization["owner_account"]):
        _fail("owner_account_mismatch")
    if authorization["owner_reconfirmed"] is not True or authorization["kill_switch_bound"] is not True:
        _fail("owner_or_kill_switch_unbound")
    if authorization["local_raw_retention"] is not False or authorization["live_execution_enabled"] is not False:
        _fail("unsafe_retention_or_live_flag")
    if authorization["sdk_retries"] != 0 or authorization["transport_retries"] != 0 or authorization["concurrency"] != 1:
        _fail("transport_policy_mismatch")
    effective = _parse_utc(authorization["tariff_effective_utc"], "invalid_tariff_effective_utc")
    start = _parse_utc(authorization["authorization_window_start_utc"], "invalid_authorization_window")
    end = _parse_utc(authorization["authorization_window_end_utc"], "invalid_authorization_window")
    if start >= end or end - start > timedelta(minutes=30):
        _fail("authorization_window_invalid")
    if now_utc.tzinfo is None:
        _fail("invalid_now_utc")
    if start <= now_utc.astimezone(timezone.utc):
        _fail("authorization_window_expired")
    if effective > start:
        _fail("tariff_effective_after_window")
    max_logical = _expect_nonnegative_int(authorization["max_logical_calls"], "invalid_max_logical_calls")
    max_http = _expect_nonnegative_int(authorization["max_http_attempts"], "invalid_max_http_attempts")
    input_tokens = _expect_nonnegative_int(authorization["max_input_tokens"], "invalid_max_input_tokens")
    output_tokens = _expect_nonnegative_int(authorization["max_output_tokens"], "invalid_max_output_tokens")
    input_rate = _expect_nonnegative_int(
        authorization["input_rate_microcny_per_million"], "invalid_input_rate"
    )
    output_rate = _expect_nonnegative_int(
        authorization["output_rate_microcny_per_million"], "invalid_output_rate"
    )
    cached_rate = _expect_nonnegative_int(
        authorization["cached_input_rate_microcny_per_million"], "invalid_cached_input_rate"
    )
    budget = _expect_nonnegative_int(authorization["diagnostic_budget_microcny"], "invalid_diagnostic_budget")
    if max_logical != 1 or max_http != 1 or input_tokens < 1 or output_tokens < 1:
        _fail("diagnostic_call_or_token_cap_mismatch")
    if input_rate < 1 or output_rate < 1 or cached_rate > input_rate:
        _fail("tariff_rate_invalid")
    expected_budget = worst_case_microcny(
        input_tokens=input_tokens, output_tokens=output_tokens, input_rate=input_rate, output_rate=output_rate
    )
    if budget != expected_budget or budget > MAX_AGGREGATE_BUDGET_MICRO_CNY:
        _fail("diagnostic_budget_mismatch")
    _validate_seal(authorization, "authorization_sha256", "authorization_sha256_mismatch")
    if (
        expected_authorization_sha256 is not None
        and authorization["authorization_sha256"]
        != _expect_sha256(expected_authorization_sha256, "invalid_expected_authorization_sha256")
    ):
        _fail("authorization_binding_drift")
    if contains_forbidden_content(authorization):
        _fail("authorization_contains_forbidden_content")
    return authorization


@dataclass(frozen=True)
class PreapprovalAttestations:
    """Injected non-secret assertions; this module never discovers external evidence."""

    technical_bindings_valid: bool
    provider_policy_accepted: bool
    tariff_current: bool
    authorization_window_valid: bool

    def require_approval_eligible(self) -> None:
        for value in (
            self.technical_bindings_valid,
            self.provider_policy_accepted,
            self.tariff_current,
            self.authorization_window_valid,
        ):
            if _expect_bool(value, "invalid_preapproval_attestation") is not True:
                _fail("preapproval_attestation_not_approval_eligible")


def build_preapproval_preflight(
    authorization: Mapping[str, Any],
    *,
    executable_source_digest: str,
    attestations: PreapprovalAttestations,
) -> dict[str, Any]:
    """Seal externally supplied, non-secret approval-stage assertions.

    The assertions are injected by a future evidence-verification task or by a test
    fake.  This module validates their shape but never reads policy, tariff, window,
    or credential evidence to decide them.
    """

    if not isinstance(attestations, PreapprovalAttestations):
        _fail("invalid_preapproval_attestation")
    validated = validate_final_authorization(
        authorization, executable_source_digest=executable_source_digest
    )
    attestations.require_approval_eligible()
    return _seal(
        {
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "stage": "DIAGNOSTIC",
            "preflight_sha256": "",
            "authorization_sha256": validated["authorization_sha256"],
            "technical_bindings_valid": attestations.technical_bindings_valid,
            "provider_policy_accepted": attestations.provider_policy_accepted,
            "tariff_current": attestations.tariff_current,
            "authorization_window_valid": attestations.authorization_window_valid,
            "credential_metadata_validated": False,
            "canary_allowed": False,
            "real_run_recommended_now": False,
            "blocking_reason_codes": list(FINAL_PREFLIGHT_BLOCKING_REASON_CODES),
        },
        "preflight_sha256",
    )


def validate_preapproval_preflight(value: Any, *, authorization_sha256: str | None = None) -> dict[str, Any]:
    preflight = _expect_mapping(value, "invalid_preflight")
    _expect_exact_keys(preflight, PREFLIGHT_FIELDS, "invalid_preflight_keys")
    if preflight["schema_version"] != PREFLIGHT_SCHEMA_VERSION or preflight["phase_id"] != PHASE_ID:
        _fail("preflight_identity_mismatch")
    if preflight["stage"] != "DIAGNOSTIC":
        _fail("preflight_stage_mismatch")
    observed_auth = _expect_sha256(preflight["authorization_sha256"], "invalid_preflight_authorization_sha256")
    if authorization_sha256 is not None and observed_auth != authorization_sha256:
        _fail("preflight_authorization_binding_mismatch")
    for field in ("technical_bindings_valid", "provider_policy_accepted", "tariff_current", "authorization_window_valid"):
        if preflight[field] is not True:
            _fail("preflight_technical_check_failed")
    for field in ("credential_metadata_validated", "canary_allowed", "real_run_recommended_now"):
        if preflight[field] is not False:
            _fail("preflight_unsafe_flag")
    if tuple(preflight["blocking_reason_codes"]) != FINAL_PREFLIGHT_BLOCKING_REASON_CODES:
        _fail("preflight_blocking_codes_mismatch")
    _validate_seal(preflight, "preflight_sha256", "preflight_sha256_mismatch")
    if contains_forbidden_content(preflight):
        _fail("preflight_contains_forbidden_content")
    return preflight


def build_approval_binding(
    authorization: Mapping[str, Any],
    preflight: Mapping[str, Any],
    *,
    executable_source_digest: str,
) -> dict[str, Any]:
    validated_preflight = validate_preapproval_preflight(preflight)
    validated_auth = validate_final_authorization(
        authorization,
        executable_source_digest=executable_source_digest,
        expected_authorization_sha256=validated_preflight["authorization_sha256"],
    )
    return _seal(
        {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "stage": "DIAGNOSTIC",
            "approval_binding_sha256": "",
            "authorization_sha256": validated_auth["authorization_sha256"],
            "preapproval_preflight_sha256": validated_preflight["preflight_sha256"],
        },
        "approval_binding_sha256",
    )


def validate_approval_binding(value: Any) -> dict[str, Any]:
    binding = _expect_mapping(value, "invalid_approval_binding")
    _expect_exact_keys(binding, APPROVAL_FIELDS, "invalid_approval_binding_keys")
    if (
        binding["schema_version"] != APPROVAL_SCHEMA_VERSION
        or binding["phase_id"] != PHASE_ID
        or binding["stage"] != "DIAGNOSTIC"
    ):
        _fail("approval_binding_identity_mismatch")
    _expect_sha256(binding["authorization_sha256"], "invalid_approval_binding_authorization_sha256")
    _expect_sha256(binding["preapproval_preflight_sha256"], "invalid_approval_binding_preflight_sha256")
    _validate_seal(binding, "approval_binding_sha256", "approval_binding_sha256_mismatch")
    if contains_forbidden_content(binding):
        _fail("approval_binding_contains_forbidden_content")
    return binding


@dataclass(frozen=True)
class CredentialFileMetadata:
    """Injected synthetic metadata; this task never opens a credential file."""

    platform: str
    exists: bool
    regular_file: bool
    symlink: bool
    ancestor_symlink: bool
    absolute_repository_external: bool
    owner_uid: int | None
    mode_octal: int | None
    link_count: int
    size_bytes: int


def validate_credential_file_metadata(metadata: CredentialFileMetadata) -> None:
    if metadata.platform == "windows":
        _fail("credential_platform_unsupported")
    if metadata.platform != "posix":
        _fail("credential_file_platform_unsupported")
    if (
        metadata.exists is not True
        or metadata.regular_file is not True
        or metadata.symlink is not False
        or metadata.ancestor_symlink is not False
        or metadata.absolute_repository_external is not True
    ):
        _fail("credential_file_denied")
    owner_uid = _expect_nonnegative_int(metadata.owner_uid, "credential_file_metadata_type_invalid")
    mode_octal = _expect_nonnegative_int(metadata.mode_octal, "credential_file_metadata_type_invalid")
    link_count = _expect_nonnegative_int(metadata.link_count, "credential_file_metadata_type_invalid")
    size_bytes = _expect_nonnegative_int(metadata.size_bytes, "credential_file_metadata_type_invalid")
    if owner_uid != 0 or mode_octal != 0o600:
        _fail("credential_file_permissions_denied")
    if link_count != 1:
        _fail("credential_file_link_count_denied")
    if not 1 <= size_bytes <= 4096:
        _fail("credential_file_size_invalid")


@dataclass(frozen=True)
class BudgetLimits:
    logical_calls: int
    http_attempts: int
    input_tokens: int
    output_tokens: int
    microcny: int

    def __post_init__(self) -> None:
        for value in (
            self.logical_calls,
            self.http_attempts,
            self.input_tokens,
            self.output_tokens,
            self.microcny,
        ):
            _expect_nonnegative_int(value, "invalid_budget_limit")


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    input_tokens: int
    output_tokens: int
    microcny: int

    def __post_init__(self) -> None:
        if not isinstance(self.reservation_id, str) or not self.reservation_id:
            _fail("invalid_budget_reservation_id")
        for value in (self.input_tokens, self.output_tokens, self.microcny):
            _expect_nonnegative_int(value, "invalid_budget_reservation")


@dataclass
class DurableFakeBudgetLedger:
    """In-memory model of monotonic durable reservation state used only by tests."""

    limits: BudgetLimits
    logical_calls: int = 0
    http_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    microcny: int = 0
    reservations: dict[str, BudgetReservation] = dataclass_field(default_factory=dict)
    credential_metadata_validated: set[str] = dataclass_field(default_factory=set)
    http_recorded: set[str] = dataclass_field(default_factory=set)

    def __post_init__(self) -> None:
        if not isinstance(self.limits, BudgetLimits):
            _fail("invalid_budget_limit")
        for value in (
            self.logical_calls,
            self.http_attempts,
            self.input_tokens,
            self.output_tokens,
            self.microcny,
        ):
            _expect_nonnegative_int(value, "invalid_budget_ledger_state")

    def reserve(self, reservation: BudgetReservation) -> None:
        if reservation.reservation_id in self.reservations:
            _fail("duplicate_budget_reservation")
        for value in (reservation.input_tokens, reservation.output_tokens, reservation.microcny):
            _expect_nonnegative_int(value, "invalid_budget_reservation")
        candidate = (
            self.logical_calls + 1,
            self.input_tokens + reservation.input_tokens,
            self.output_tokens + reservation.output_tokens,
            self.microcny + reservation.microcny,
        )
        limits = (
            self.limits.logical_calls,
            self.limits.input_tokens,
            self.limits.output_tokens,
            self.limits.microcny,
        )
        if any(observed > maximum for observed, maximum in zip(candidate, limits)):
            _fail("budget_hard_cap_exhausted")
        self.logical_calls, self.input_tokens, self.output_tokens, self.microcny = candidate
        self.reservations[reservation.reservation_id] = reservation

    def record_credential_metadata_validated(self, reservation_id: str) -> None:
        if reservation_id not in self.reservations:
            _fail("credential_metadata_without_reservation")
        if reservation_id in self.credential_metadata_validated:
            _fail("duplicate_credential_metadata_validation")
        if reservation_id in self.http_recorded:
            _fail("credential_metadata_after_http_attempt")
        self.credential_metadata_validated.add(reservation_id)

    def record_http_attempt(self, reservation_id: str) -> None:
        if reservation_id not in self.reservations:
            _fail("http_attempt_without_reservation")
        if reservation_id not in self.credential_metadata_validated:
            _fail("http_attempt_before_credential_metadata_validation")
        if reservation_id in self.http_recorded:
            _fail("duplicate_http_attempt")
        if self.http_attempts >= self.limits.http_attempts:
            _fail("budget_hard_cap_exhausted")
        self.http_attempts += 1
        self.http_recorded.add(reservation_id)

    def reconcile(self, reservation_id: str, *, usage_known: bool) -> None:
        if reservation_id not in self.http_recorded:
            _fail("reconcile_before_http_attempt")
        if not isinstance(usage_known, bool):
            _fail("invalid_usage_known")
        # Reservations intentionally remain charged even when usage is known or unknown.


@dataclass
class OneUseApprovalLedger:
    consumed: set[str] = dataclass_field(default_factory=set)

    def consume(self, approval_text: str, binding: Mapping[str, Any]) -> None:
        validated = validate_approval_binding(binding)
        binding_sha = validated["approval_binding_sha256"]
        expected = f"APPROVE PHASE11C DIAGNOSTIC {binding_sha}"
        if approval_text != expected:
            _fail("diagnostic_approval_text_mismatch")
        if binding_sha in self.consumed:
            _fail("diagnostic_approval_already_consumed")
        self.consumed.add(binding_sha)


@dataclass
class RecordingFakeTransport:
    ledger: DurableFakeBudgetLedger
    calls: int = 0

    def dispatch(self, reservation_id: str) -> str:
        if reservation_id not in self.ledger.http_recorded:
            _fail("transport_before_durable_http_attempt")
        if reservation_id not in self.ledger.credential_metadata_validated:
            _fail("transport_before_credential_metadata_validation")
        self.calls += 1
        return "synthetic_protocol_terminal"


def run_fake_diagnostic(
    authorization: Mapping[str, Any],
    preflight: Mapping[str, Any],
    approval_text: str,
    credential_metadata: CredentialFileMetadata,
    *,
    executable_source_digest: str,
    approval_ledger: OneUseApprovalLedger,
    budget_ledger: DurableFakeBudgetLedger,
    transport: RecordingFakeTransport | None = None,
) -> dict[str, Any]:
    """Test-only fake ordering model.  It cannot call a provider or read a key."""

    validated_preflight = validate_preapproval_preflight(preflight)
    validated_auth = validate_final_authorization(
        authorization,
        executable_source_digest=executable_source_digest,
        expected_authorization_sha256=validated_preflight["authorization_sha256"],
    )
    binding = build_approval_binding(
        validated_auth,
        validated_preflight,
        executable_source_digest=executable_source_digest,
    )
    approval_ledger.consume(approval_text, binding)
    reservation = BudgetReservation(
        reservation_id=binding["approval_binding_sha256"],
        input_tokens=validated_auth["max_input_tokens"],
        output_tokens=validated_auth["max_output_tokens"],
        microcny=validated_auth["diagnostic_budget_microcny"],
    )
    budget_ledger.reserve(reservation)
    validate_credential_file_metadata(credential_metadata)
    budget_ledger.record_credential_metadata_validated(reservation.reservation_id)
    budget_ledger.record_http_attempt(reservation.reservation_id)
    fake_transport = transport or RecordingFakeTransport(budget_ledger)
    terminal_category = fake_transport.dispatch(reservation.reservation_id)
    budget_ledger.reconcile(reservation.reservation_id, usage_known=False)
    return {
        "approval_binding_sha256": binding["approval_binding_sha256"],
        "execution_status": "fake_completed_no_provider",
        "terminal_category": terminal_category,
        "logical_call_count": budget_ledger.logical_calls,
        "provider_call_count": 0,
        "http_attempt_count": budget_ledger.http_attempts,
        "reserved_microcny": budget_ledger.microcny,
        "usage_known": False,
    }


def build_blocked_receipt(authorization: Mapping[str, Any]) -> dict[str, Any]:
    draft = validate_draft_authorization(authorization)
    return _seal(
        {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "stage": "DIAGNOSTIC",
            "receipt_sha256": "",
            "authorization_sha256": draft["authorization_sha256"],
            "approval_binding_sha256": "0" * 64,
            "execution_status": "not_run_gate_blocked",
            "logical_call_count": 0,
            "provider_call_count": 0,
            "http_attempt_count": 0,
            "reserved_input_tokens": 0,
            "reserved_output_tokens": 0,
            "reserved_microcny": 0,
            "credential_file_opened": False,
            "live_execution_enabled": False,
            "usage_known": False,
            "blocking_reason_codes": list(DRAFT_BLOCKING_REASON_CODES),
        },
        "receipt_sha256",
    )


def validate_blocked_receipt(value: Any) -> dict[str, Any]:
    receipt = _expect_mapping(value, "invalid_receipt")
    _expect_exact_keys(receipt, RECEIPT_FIELDS, "invalid_receipt_keys")
    if (
        receipt["schema_version"] != RECEIPT_SCHEMA_VERSION
        or receipt["phase_id"] != PHASE_ID
        or receipt["stage"] != "DIAGNOSTIC"
        or receipt["execution_status"] != "not_run_gate_blocked"
    ):
        _fail("receipt_identity_mismatch")
    _expect_sha256(receipt["authorization_sha256"], "invalid_receipt_authorization_sha256")
    _expect_sha256(receipt["approval_binding_sha256"], "invalid_receipt_approval_binding_sha256")
    for field in (
        "logical_call_count",
        "provider_call_count",
        "http_attempt_count",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_microcny",
    ):
        if _expect_nonnegative_int(receipt[field], "invalid_receipt_count") != 0:
            _fail("receipt_count_not_zero")
    for field in ("credential_file_opened", "live_execution_enabled", "usage_known"):
        if receipt[field] is not False:
            _fail("receipt_unsafe_flag")
    if tuple(receipt["blocking_reason_codes"]) != DRAFT_BLOCKING_REASON_CODES:
        _fail("receipt_blocking_codes_mismatch")
    _validate_seal(receipt, "receipt_sha256", "receipt_sha256_mismatch")
    if contains_forbidden_content(receipt):
        _fail("receipt_contains_forbidden_content")
    return receipt


def run_live_gate_blocked() -> dict[str, Any]:
    """The only CLI live outcome in this task: no credential or network access."""

    draft = build_draft_authorization(executable_source_sha256=source_sha256())
    return validate_blocked_receipt(build_blocked_receipt(draft))


def _print_safe_json(value: Mapping[str, Any]) -> None:
    print(canonical_json(value).decode("utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("print-draft")
    commands.add_parser("validate-draft")
    commands.add_parser("run-live")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    draft = build_draft_authorization(executable_source_sha256=source_sha256())
    if args.command == "print-draft":
        _print_safe_json(validate_draft_authorization(draft, executable_source_digest=source_sha256()))
        return 0
    if args.command == "validate-draft":
        _print_safe_json(build_blocked_receipt(validate_draft_authorization(draft, executable_source_digest=source_sha256())))
        return 0
    _print_safe_json(run_live_gate_blocked())
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
