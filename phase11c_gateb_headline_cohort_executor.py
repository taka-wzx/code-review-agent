"""Phase 11C Gate B deterministic-synthetic protocol executor.

The program exposes two deliberately narrow live paths: a one-call ``DIAGNOSTIC``
and a three-target ``HEADLINE_COHORT``.  The latter sends at most a probe and a
submit request for each frozen target, in order.  Both paths require separately
sealed, one-use approvals.  Raw prompt, response, tool arguments, credential,
exception detail, and host-path content exist only transiently in memory and are
never persisted in state, receipts, ledger, image output, or normal stdout.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
from typing import Any, Callable, Mapping, NoReturn, Protocol, Sequence

try:  # Keep the module importable for offline Windows tests.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None  # type: ignore[assignment]

fcntl: Any = _fcntl


PHASE_ID = "phase11c-gateb-headline-cohort-executor-v1"
AUTHORIZATION_SCHEMA_VERSION = "phase11c-gateb-headline-cohort-authorization/v1"
TARGET_RECEIPT_SCHEMA_VERSION = "phase11c-gateb-headline-cohort-target-receipt/v1"
COHORT_RECEIPT_SCHEMA_VERSION = "phase11c-gateb-headline-cohort-receipt/v1"
LEDGER_SCHEMA_VERSION = "phase11c-gateb-headline-cohort-ledger/v1"
STATE_SCHEMA_VERSION = "phase11c-gateb-headline-cohort-state/v1"
APPROVAL_BINDING_SCHEMA_VERSION = "phase11c-gateb-headline-cohort-approval-binding/v1"

OWNER_ACCOUNT = "taka-wzx"
PROVIDER = "glm"
REQUEST_MODEL_ID = "glm-5.2"
API_SURFACE = "chat.completions.create"
ENDPOINT_ID = "glm_standard_v4"
ENDPOINT_HOST = "open.bigmodel.cn"
ENDPOINT_PORT = 443
ENDPOINT_PATH = "/api/paas/v4/chat/completions"

# Historical evidence only.  A headline authorization must bind a fresh receipt
# produced by this exact final executable and image.
GATE_A_PHASE_ID = "phase11c-provider-canary-v1"
GATE_A_COHORT_MANIFEST_SHA256 = "ddcbc3d16c57cbc9c02268bccb01eee223f3aa3f502cdfa7e449e8171a759e1a"
HISTORICAL_DIAGNOSTIC_RECEIPT_SHA256 = "6ff641016ca966b933f7c12e8d968b09c3962dc0b959b79434be929530055959"
INPUT_RATE_MICROCNY_PER_MILLION = 8_000_000
CACHED_INPUT_RATE_MICROCNY_PER_MILLION = 2_000_000
OUTPUT_RATE_MICROCNY_PER_MILLION = 28_000_000
MAX_INPUT_TOKENS_PER_REQUEST = 2_000
MAX_OUTPUT_TOKENS_PER_REQUEST = 128
HEADLINE_TARGET_COUNT = 3
REQUESTS_PER_TARGET = 2
HEADLINE_REQUEST_COUNT = HEADLINE_TARGET_COUNT * REQUESTS_PER_TARGET
# This is the conservative reservation for one request, not one two-request
# target.  The explicit names below prevent a later freeze from double-counting.
PER_REQUEST_BUDGET_MICROCNY = 19_584
DIAGNOSTIC_BUDGET_MICROCNY = PER_REQUEST_BUDGET_MICROCNY
PER_TARGET_BUDGET_MICROCNY = REQUESTS_PER_TARGET * PER_REQUEST_BUDGET_MICROCNY
HEADLINE_BUDGET_MICROCNY = HEADLINE_REQUEST_COUNT * PER_REQUEST_BUDGET_MICROCNY
AGGREGATE_CEILING_MICROCNY = 15_000_000
HISTORICAL_DIAGNOSTIC_RESERVATION_MICROCNY = 19_584
AGGREGATE_BEFORE_SAME_IMAGE_DIAGNOSTIC_MICROCNY = (
    AGGREGATE_CEILING_MICROCNY - HISTORICAL_DIAGNOSTIC_RESERVATION_MICROCNY
)
AGGREGATE_BEFORE_HEADLINE_MICROCNY = (
    AGGREGATE_BEFORE_SAME_IMAGE_DIAGNOSTIC_MICROCNY - DIAGNOSTIC_BUDGET_MICROCNY
)
AGGREGATE_AFTER_HEADLINE_MICROCNY = AGGREGATE_BEFORE_HEADLINE_MICROCNY - HEADLINE_BUDGET_MICROCNY
HTTP_TIMEOUT_SECONDS = 60
MAX_PROVIDER_RESPONSE_BYTES = 262_144
MAX_CONTROL_FILE_BYTES = 65_536
MAX_CREDENTIAL_BYTES = 4_096
MAX_PROVIDER_USAGE_COUNTER = 1_000_000
STOP_POLICY = "stop_on_first_noncompleted_or_unknown_or_ambiguous"
MAX_TOOL_CALL_ID_BYTES = 128
MAX_TOOL_ARGUMENT_BYTES = 512

CREDENTIAL_PATH = Path("/run/crag-gateb-protocol/glm_api_key")
AUTHORIZATION_PATH = Path("/run/crag-gateb-headline/authorization.json")
APPROVAL_PATH = Path("/run/crag-gateb-headline/approval.txt")
DIAGNOSTIC_AUTHORIZATION_PATH = Path("/run/crag-gateb-diagnostic/authorization.json")
DIAGNOSTIC_APPROVAL_PATH = Path("/run/crag-gateb-diagnostic/approval.txt")
STATE_DIRECTORY = Path("/var/lib/crag-gateb-headline")
STATE_PATH = STATE_DIRECTORY / "state.json"
COHORT_RECEIPT_PATH = STATE_DIRECTORY / "cohort-receipt.json"
LEDGER_PATH = STATE_DIRECTORY / "ledger.json"
LOCK_PATH = STATE_DIRECTORY / "state.lock"

PENDING_FREEZE = "PENDING_FREEZE"
ZERO_SHA256 = "0" * 64
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_TOOL_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_FCHMOD = getattr(os, "fchmod", None)

# These identifiers and payload hashes are the first three manifest entries generated
# by the Gate A deterministic cohort.  They are safe metadata, not source material.
HEADLINE_TARGETS: tuple[dict[str, str], ...] = (
    {
        "stable_id": "p11c-1207dbad4b888c12bfc703b51dc0463c",
        "payload_sha256": "0af27835bb7a909a85e38e17404915b8806cdaef07e85ad09d751ba5875fed5d",
    },
    {
        "stable_id": "p11c-bb843bda5e0c283e00534906c2733c3f",
        "payload_sha256": "ce6f18888fb6e78b7fd03e932ec9b7ed15e1662cd7264025991f0251bda43151",
    },
    {
        "stable_id": "p11c-f041de09e0de6b8915f3a42c0c7d561a",
        "payload_sha256": "f48265776787985705ebfb66f94bc89dbde7136c05b0dcb106e2568cc883e3ab",
    },
)


class HeadlineCohortError(ValueError):
    """Stable, secret-free refusal or execution category."""


def _fail(code: str) -> NoReturn:
    raise HeadlineCohortError(code)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < 0:
            _fail("negative_integer")
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
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                _fail("duplicate_json_key")
            result[key] = item
        return result

    try:
        return _canonicalize(json.loads(value, object_pairs_hook=reject_duplicates))
    except HeadlineCohortError:
        raise
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise HeadlineCohortError("invalid_json") from exc


def _seal(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = deepcopy(dict(value))
    if result.get(field) != "":
        _fail("invalid_unsealed_document")
    result[field] = sha256_bytes(canonical_json(result))
    return result


def _validate_seal(value: Mapping[str, Any], field: str, code: str) -> None:
    document = dict(value)
    observed = _expect_sha256(document.get(field), code, allow_zero=False)
    document[field] = ""
    if sha256_bytes(canonical_json(document)) != observed:
        _fail(code)


def _expect_mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code)
    return dict(value)


def _expect_exact_keys(value: Mapping[str, Any], expected: frozenset[str], code: str) -> None:
    if set(value) != expected:
        _fail(code)


def _expect_sha256(value: Any, code: str, *, allow_zero: bool = True) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _fail(code)
    if not allow_zero and value == ZERO_SHA256:
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


def _parse_utc(value: Any, code: str) -> datetime:
    if not isinstance(value, str):
        _fail(code)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HeadlineCohortError(code) from exc


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def worst_case_microcny(*, input_tokens: int, output_tokens: int) -> int:
    for value in (input_tokens, output_tokens):
        _expect_nonnegative_int(value, "invalid_cost_component")
    return _ceil_div(input_tokens * INPUT_RATE_MICROCNY_PER_MILLION, 1_000_000) + _ceil_div(
        output_tokens * OUTPUT_RATE_MICROCNY_PER_MILLION, 1_000_000
    )


def source_sha256() -> str:
    normalized = Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return sha256_bytes(normalized.encode("utf-8"))


def endpoint_sha256() -> str:
    return sha256_bytes(
        canonical_json(
            {"host": ENDPOINT_HOST, "method": "POST", "path": ENDPOINT_PATH, "port": ENDPOINT_PORT, "tls": True}
        )
    )


def _gate_a_target_for_ordinal(ordinal: int) -> dict[str, str]:
    """Rebuild one Gate A deterministic target without opening its source artifacts."""

    if not 1 <= ordinal <= HEADLINE_TARGET_COUNT:
        _fail("headline_target_ordinal_invalid")
    stable_digest = sha256_bytes(
        f"{GATE_A_PHASE_ID}:deterministic-synthetic:stable-id:{ordinal}".encode("ascii")
    )
    payload_digest = sha256_bytes(
        f"{GATE_A_PHASE_ID}:deterministic-synthetic:payload:{ordinal}".encode("ascii")
    )
    return {"stable_id": f"p11c-{stable_digest[:32]}", "payload_sha256": payload_digest}


def _validate_reconstructed_target(target: Mapping[str, str], ordinal: int) -> dict[str, str]:
    expected = _gate_a_target_for_ordinal(ordinal)
    if dict(target) != expected:
        _fail("headline_target_reconstruction_mismatch")
    return expected


def _ordinal_for_target(target: Any) -> int:
    if not isinstance(target, Mapping):
        _fail("invalid_target")
    candidate = dict(target)
    for ordinal, expected in enumerate(HEADLINE_TARGETS, start=1):
        if candidate == expected:
            return ordinal
    _fail("invalid_target")


def cohort_manifest() -> dict[str, Any]:
    return {
        "schema_version": "phase11c-headline-cohort-manifest/v1",
        "phase_id": PHASE_ID,
        "source_kind": "deterministic_synthetic",
        "parent_gate_a_cohort_manifest_sha256": GATE_A_COHORT_MANIFEST_SHA256,
        "auth004_intersection_count": 0,
        "exact_headline_denominator": HEADLINE_TARGET_COUNT,
        "diagnostic_in_headline_denominator": False,
        "targets": [dict(target) for target in HEADLINE_TARGETS],
    }


def cohort_manifest_sha256() -> str:
    return sha256_bytes(canonical_json(cohort_manifest()))


def tool_schema(name: str, outcome: str) -> dict[str, Any]:
    if (name, outcome) not in {("probe_canary", "probe"), ("submit_canary", "submit")}:
        _fail("invalid_tool_schema")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Execute a deterministic synthetic canary action locally.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["outcome", "payload_sha256", "target_id"],
                "properties": {
                    "outcome": {"type": "string", "enum": [outcome]},
                    "payload_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "target_id": {"type": "string", "pattern": "^p11c-[0-9a-f]{32}$"},
                },
            },
        },
    }


def _tool_schema_sha256() -> str:
    return sha256_bytes(
        canonical_json(
            {
                "probe": tool_schema("probe_canary", "probe"),
                "submit": tool_schema("submit_canary", "submit"),
            }
        )
    )


SYNTHETIC_TOOL_RESULT_BYTES = b'{"result":"synthetic_probe_ok"}'


def synthetic_tool_result_sha256() -> str:
    return sha256_bytes(SYNTHETIC_TOOL_RESULT_BYTES)


def _canonical_tool_arguments_for(target: Mapping[str, str], outcome: str) -> str:
    if outcome not in {"probe", "submit"}:
        _fail("invalid_tool_outcome")
    return canonical_json(
        {
            "outcome": outcome,
            "payload_sha256": target["payload_sha256"],
            "target_id": target["stable_id"],
        }
    ).decode("ascii")


def initial_request_template_sha256() -> str:
    return sha256_bytes(
        canonical_json(
            {
                "api_surface": API_SURFACE,
                "max_tokens": MAX_OUTPUT_TOKENS_PER_REQUEST,
                "model": REQUEST_MODEL_ID,
                "prompt_contract": "deterministic_synthetic_probe_only",
                "stream": False,
                "temperature": 0,
                "thinking": {"type": "disabled"},
                "tool_choice": {"type": "function", "function": {"name": "probe_canary"}},
                "tool_schema_sha256": _tool_schema_sha256(),
            }
        )
    )


def stop_policy_sha256() -> str:
    return sha256_bytes(
        canonical_json(
            {
                "policy": STOP_POLICY,
                "on_noncompleted": "write_remaining_not_run_gate_blocked",
                "on_ambiguous_or_unknown": "quarantine_no_further_network",
                "retry_count": 0,
            }
        )
    )


def request_protocol_sha256() -> str:
    return sha256_bytes(
        canonical_json(
            {
                "api_surface": API_SURFACE,
                "endpoint_sha256": endpoint_sha256(),
                "max_tokens": MAX_OUTPUT_TOKENS_PER_REQUEST,
                "model": REQUEST_MODEL_ID,
                "stream": False,
                "temperature": 0,
                "thinking": {"type": "disabled"},
                "initial_request_template_sha256": initial_request_template_sha256(),
                "tool_schema_sha256": _tool_schema_sha256(),
                "synthetic_tool_result_sha256": synthetic_tool_result_sha256(),
            }
        )
    )


def request_body_for(target: Mapping[str, str]) -> bytes:
    target = _validate_reconstructed_target(target, _ordinal_for_target(target))
    stable_id = target["stable_id"]
    payload_sha = target["payload_sha256"]
    return canonical_json(
        {
            "max_tokens": MAX_OUTPUT_TOKENS_PER_REQUEST,
            "messages": [
                {
                    "content": (
                        "This is a deterministic synthetic protocol canary. "
                        f"For target {stable_id} and payload hash {payload_sha}, call probe_canary exactly once "
                        "with outcome probe. Do not produce a text answer."
                    ),
                    "role": "user",
                }
            ],
            "model": REQUEST_MODEL_ID,
            "stream": False,
            "temperature": 0,
            "thinking": {"type": "disabled"},
            "tool_choice": {"type": "function", "function": {"name": "probe_canary"}},
            "tools": [tool_schema("probe_canary", "probe")],
        }
    )


def continuation_body_for(target: Mapping[str, str], tool_call_id: str) -> bytes:
    target = _validate_reconstructed_target(target, _ordinal_for_target(target))
    stable_id = target["stable_id"]
    payload_sha = target["payload_sha256"]
    if not isinstance(tool_call_id, str) or not _TOOL_CALL_ID.fullmatch(tool_call_id):
        _fail("probe_tool_call_id_invalid")
    canonical_arguments = _canonical_tool_arguments_for(target, "probe")
    return canonical_json(
        {
            "max_tokens": MAX_OUTPUT_TOKENS_PER_REQUEST,
            "messages": [
                {
                    "content": (
                        "This is a deterministic synthetic protocol canary. "
                        f"For target {stable_id} and payload hash {payload_sha}, call probe_canary exactly once "
                        "with outcome probe. Do not produce a text answer."
                    ),
                    "role": "user",
                },
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {"name": "probe_canary", "arguments": canonical_arguments},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": tool_call_id, "content": SYNTHETIC_TOOL_RESULT_BYTES.decode("ascii")},
            ],
            "model": REQUEST_MODEL_ID,
            "stream": False,
            "temperature": 0,
            "thinking": {"type": "disabled"},
            "tool_choice": {"type": "function", "function": {"name": "submit_canary"}},
            "tools": [tool_schema("submit_canary", "submit")],
        }
    )


DIAGNOSTIC_TERMINAL_TOKEN = "PHASE11C_PROTOCOL_DIAGNOSTIC_OK"
DIAGNOSTIC_REQUEST_BODY = canonical_json(
    {
        "max_tokens": MAX_OUTPUT_TOKENS_PER_REQUEST,
        "messages": [
            {
                "role": "user",
                "content": "This is a deterministic synthetic protocol canary. Return exactly PHASE11C_PROTOCOL_DIAGNOSTIC_OK and no other text.",
            }
        ],
        "model": REQUEST_MODEL_ID,
        "stream": False,
        "temperature": 0,
        "thinking": {"type": "disabled"},
    }
)


def diagnostic_request_sha256() -> str:
    return sha256_bytes(DIAGNOSTIC_REQUEST_BODY)


SAME_IMAGE_FREEZE_FIELDS = (
    "executable_source_sha256",
    "source_tree_sha256",
    "dockerfile_sha256",
    "compose_sha256",
    "image_sha256",
    "deployment_sha256",
    "runtime_identity_sha256",
    "provider",
    "request_model_id",
    "api_surface",
    "endpoint_id",
    "endpoint_sha256",
    "provider_policy_evidence_sha256",
    "provider_tariff_evidence_sha256",
    "credential_delivery_mode",
    "credential_fingerprint_sha256",
    "owner_account",
)


AUTHORIZATION_FIELDS = frozenset(
    {
        "schema_version", "phase_id", "stage", "authorization_status", "authorization_sha256",
        "executable_source_sha256", "source_tree_sha256", "dockerfile_sha256", "compose_sha256",
        "image_sha256", "deployment_sha256", "runtime_identity_sha256", "diagnostic_receipt_sha256",
        "diagnostic_authorization_sha256", "diagnostic_approval_binding_sha256",
        "cohort_manifest_sha256", "parent_gate_a_cohort_manifest_sha256", "auth004_nonoverlap_evidence_sha256",
        "auth004_intersection_count", "exact_headline_denominator", "target_bindings", "provider",
        "request_model_id", "api_surface", "endpoint_id", "endpoint_sha256", "request_protocol_sha256",
        "initial_request_template_sha256", "tool_schema_sha256", "synthetic_tool_result_sha256",
        "provider_policy_evidence_sha256", "provider_policy_accepted", "provider_tariff_evidence_sha256", "credential_delivery_mode",
        "credential_fingerprint_sha256", "owner_account", "owner_reconfirmed", "kill_switch_bound",
        "authorization_window_start_utc", "authorization_window_end_utc", "headline_logical_call_cap",
        "headline_http_attempt_cap", "headline_input_token_cap", "headline_output_token_cap",
        "input_rate_microcny_per_million", "cached_input_rate_microcny_per_million",
        "output_rate_microcny_per_million", "headline_budget_microcny", "aggregate_remaining_microcny",
        "aggregate_remaining_after_reservation_microcny",
        "sdk_retries", "transport_retries", "concurrency", "stop_policy", "local_raw_retention",
        "stop_policy_sha256", "live_execution_enabled",
    }
)


def build_authorization_template() -> dict[str, Any]:
    return {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "phase_id": PHASE_ID,
        "stage": "HEADLINE_COHORT",
        "authorization_status": "frozen_pending_exact_approval",
        "authorization_sha256": "",
        "executable_source_sha256": source_sha256(),
        "source_tree_sha256": PENDING_FREEZE,
        "dockerfile_sha256": PENDING_FREEZE,
        "compose_sha256": PENDING_FREEZE,
        "image_sha256": PENDING_FREEZE,
        "deployment_sha256": PENDING_FREEZE,
        "runtime_identity_sha256": PENDING_FREEZE,
        "diagnostic_receipt_sha256": PENDING_FREEZE,
        "diagnostic_authorization_sha256": PENDING_FREEZE,
        "diagnostic_approval_binding_sha256": PENDING_FREEZE,
        "cohort_manifest_sha256": cohort_manifest_sha256(),
        "parent_gate_a_cohort_manifest_sha256": GATE_A_COHORT_MANIFEST_SHA256,
        "auth004_nonoverlap_evidence_sha256": PENDING_FREEZE,
        "auth004_intersection_count": 0,
        "exact_headline_denominator": HEADLINE_TARGET_COUNT,
        "target_bindings": [dict(target) for target in HEADLINE_TARGETS],
        "provider": PROVIDER,
        "request_model_id": REQUEST_MODEL_ID,
        "api_surface": API_SURFACE,
        "endpoint_id": ENDPOINT_ID,
        "endpoint_sha256": endpoint_sha256(),
        "request_protocol_sha256": request_protocol_sha256(),
        "initial_request_template_sha256": initial_request_template_sha256(),
        "tool_schema_sha256": _tool_schema_sha256(),
        "synthetic_tool_result_sha256": synthetic_tool_result_sha256(),
        "provider_policy_evidence_sha256": PENDING_FREEZE,
        "provider_policy_accepted": True,
        "provider_tariff_evidence_sha256": PENDING_FREEZE,
        "credential_delivery_mode": "fixed_linux_ecs_one_time_file",
        "credential_fingerprint_sha256": PENDING_FREEZE,
        "owner_account": OWNER_ACCOUNT,
        "owner_reconfirmed": True,
        "kill_switch_bound": True,
        "authorization_window_start_utc": PENDING_FREEZE,
        "authorization_window_end_utc": PENDING_FREEZE,
        "headline_logical_call_cap": HEADLINE_REQUEST_COUNT,
        "headline_http_attempt_cap": HEADLINE_REQUEST_COUNT,
        "headline_input_token_cap": HEADLINE_REQUEST_COUNT * MAX_INPUT_TOKENS_PER_REQUEST,
        "headline_output_token_cap": HEADLINE_REQUEST_COUNT * MAX_OUTPUT_TOKENS_PER_REQUEST,
        "input_rate_microcny_per_million": INPUT_RATE_MICROCNY_PER_MILLION,
        "cached_input_rate_microcny_per_million": CACHED_INPUT_RATE_MICROCNY_PER_MILLION,
        "output_rate_microcny_per_million": OUTPUT_RATE_MICROCNY_PER_MILLION,
        "headline_budget_microcny": HEADLINE_BUDGET_MICROCNY,
        "aggregate_remaining_microcny": AGGREGATE_BEFORE_HEADLINE_MICROCNY,
        "aggregate_remaining_after_reservation_microcny": AGGREGATE_AFTER_HEADLINE_MICROCNY,
        "sdk_retries": 0,
        "transport_retries": 0,
        "concurrency": 1,
        "stop_policy": STOP_POLICY,
        "stop_policy_sha256": stop_policy_sha256(),
        "local_raw_retention": False,
        "live_execution_enabled": True,
    }


def _validate_target_bindings(value: Any) -> None:
    if value != [dict(target) for target in HEADLINE_TARGETS]:
        _fail("target_bindings_mismatch")


def _validate_authorization_common(
    value: Any, *, executable_source_digest: str, now_utc: datetime, sealed: bool, require_active_window: bool
) -> dict[str, Any]:
    authorization = _expect_mapping(value, "invalid_authorization")
    _expect_exact_keys(authorization, AUTHORIZATION_FIELDS, "invalid_authorization_keys")
    if authorization["schema_version"] != AUTHORIZATION_SCHEMA_VERSION or authorization["phase_id"] != PHASE_ID:
        _fail("authorization_identity_mismatch")
    if authorization["stage"] != "HEADLINE_COHORT" or authorization["authorization_status"] != "frozen_pending_exact_approval":
        _fail("authorization_status_mismatch")
    if sealed:
        _validate_seal(authorization, "authorization_sha256", "authorization_sha256_mismatch")
    elif authorization["authorization_sha256"] != "":
        _fail("authorization_not_unsealed")
    if authorization["executable_source_sha256"] != _expect_sha256(executable_source_digest, "invalid_source", allow_zero=False):
        _fail("executable_source_sha256_drift")
    for field in (
        "source_tree_sha256", "dockerfile_sha256", "compose_sha256", "image_sha256", "deployment_sha256",
        "runtime_identity_sha256", "provider_policy_evidence_sha256", "provider_tariff_evidence_sha256",
        "credential_fingerprint_sha256", "diagnostic_receipt_sha256", "diagnostic_authorization_sha256",
        "diagnostic_approval_binding_sha256", "auth004_nonoverlap_evidence_sha256",
    ):
        _expect_sha256(authorization[field], f"invalid_{field}", allow_zero=False)
    expected = {
        "cohort_manifest_sha256": cohort_manifest_sha256(),
        "parent_gate_a_cohort_manifest_sha256": GATE_A_COHORT_MANIFEST_SHA256,
        "provider": PROVIDER,
        "request_model_id": REQUEST_MODEL_ID,
        "api_surface": API_SURFACE,
        "endpoint_id": ENDPOINT_ID,
        "endpoint_sha256": endpoint_sha256(),
        "request_protocol_sha256": request_protocol_sha256(),
        "initial_request_template_sha256": initial_request_template_sha256(),
        "tool_schema_sha256": _tool_schema_sha256(),
        "synthetic_tool_result_sha256": synthetic_tool_result_sha256(),
        "credential_delivery_mode": "fixed_linux_ecs_one_time_file",
        "owner_account": OWNER_ACCOUNT,
        "stop_policy": STOP_POLICY,
        "stop_policy_sha256": stop_policy_sha256(),
    }
    for field, expected_value in expected.items():
        if authorization[field] != expected_value:
            _fail(f"{field}_mismatch")
    _validate_target_bindings(authorization["target_bindings"])
    if _expect_nonnegative_int(authorization["auth004_intersection_count"], "invalid_auth004_intersection_count") != 0:
        _fail("auth004_nonoverlap_mismatch")
    if authorization["exact_headline_denominator"] != HEADLINE_TARGET_COUNT:
        _fail("headline_denominator_mismatch")
    if not _OWNER.fullmatch(authorization["owner_account"]):
        _fail("invalid_owner_account")
    for field in ("owner_reconfirmed", "kill_switch_bound", "live_execution_enabled", "provider_policy_accepted"):
        if _expect_bool(authorization[field], f"invalid_{field}") is not True:
            _fail(f"{field}_not_true")
    if _expect_bool(authorization["local_raw_retention"], "invalid_local_raw_retention") is not False:
        _fail("local_raw_retention_forbidden")
    expected_ints = {
        "headline_logical_call_cap": HEADLINE_REQUEST_COUNT,
        "headline_http_attempt_cap": HEADLINE_REQUEST_COUNT,
        "headline_input_token_cap": HEADLINE_REQUEST_COUNT * MAX_INPUT_TOKENS_PER_REQUEST,
        "headline_output_token_cap": HEADLINE_REQUEST_COUNT * MAX_OUTPUT_TOKENS_PER_REQUEST,
        "input_rate_microcny_per_million": INPUT_RATE_MICROCNY_PER_MILLION,
        "cached_input_rate_microcny_per_million": CACHED_INPUT_RATE_MICROCNY_PER_MILLION,
        "output_rate_microcny_per_million": OUTPUT_RATE_MICROCNY_PER_MILLION,
        "headline_budget_microcny": HEADLINE_BUDGET_MICROCNY,
        "aggregate_remaining_microcny": AGGREGATE_BEFORE_HEADLINE_MICROCNY,
        "aggregate_remaining_after_reservation_microcny": AGGREGATE_AFTER_HEADLINE_MICROCNY,
        "sdk_retries": 0,
        "transport_retries": 0,
        "concurrency": 1,
    }
    for field, expected_integer in expected_ints.items():
        if _expect_nonnegative_int(authorization[field], f"invalid_{field}") != expected_integer:
            _fail(f"{field}_mismatch")
    if worst_case_microcny(input_tokens=MAX_INPUT_TOKENS_PER_REQUEST, output_tokens=MAX_OUTPUT_TOKENS_PER_REQUEST) != PER_REQUEST_BUDGET_MICROCNY:
        _fail("internal_budget_constant_mismatch")
    start = _parse_utc(authorization["authorization_window_start_utc"], "invalid_window_start_utc")
    end = _parse_utc(authorization["authorization_window_end_utc"], "invalid_window_end_utc")
    now = now_utc.astimezone(timezone.utc)
    if start >= end or end - start > timedelta(minutes=30):
        _fail("authorization_window_invalid")
    if not sealed and start <= now:
        _fail("authorization_window_must_be_future")
    if require_active_window and not start <= now < end:
        _fail("authorization_window_not_active")
    if not require_active_window and now >= end:
        _fail("authorization_window_expired")
    return authorization


def seal_authorization(value: Any, *, executable_source_digest: str | None = None, now_utc: datetime | None = None) -> dict[str, Any]:
    candidate = _validate_authorization_common(
        value,
        executable_source_digest=executable_source_digest or source_sha256(),
        now_utc=now_utc or datetime.now(timezone.utc),
        sealed=False,
        require_active_window=False,
    )
    return _seal(candidate, "authorization_sha256")


def validate_authorization(
    value: Any, *, executable_source_digest: str | None = None, now_utc: datetime | None = None, require_active_window: bool = True
) -> dict[str, Any]:
    return _validate_authorization_common(
        value,
        executable_source_digest=executable_source_digest or source_sha256(),
        now_utc=now_utc or datetime.now(timezone.utc),
        sealed=True,
        require_active_window=require_active_window,
    )


def approval_binding_sha256(authorization: Mapping[str, Any]) -> str:
    """Create an explicit, human-reviewable binding for the one-use approval.

    The authorization seal is already a cryptographic commitment to every field,
    but repeating the live-risk fields here makes the approval object auditable
    without relying on an implicit transitive interpretation of that seal.
    """

    _expect_sha256(authorization.get("authorization_sha256"), "invalid_authorization_sha256", allow_zero=False)
    return sha256_bytes(
        canonical_json(
            {
                "authorization_sha256": authorization["authorization_sha256"],
                "authorization_window_end_utc": authorization["authorization_window_end_utc"],
                "authorization_window_start_utc": authorization["authorization_window_start_utc"],
                "cohort_manifest_sha256": authorization["cohort_manifest_sha256"],
                "credential_fingerprint_sha256": authorization["credential_fingerprint_sha256"],
                "diagnostic_receipt_sha256": authorization["diagnostic_receipt_sha256"],
                "diagnostic_authorization_sha256": authorization["diagnostic_authorization_sha256"],
                "diagnostic_approval_binding_sha256": authorization["diagnostic_approval_binding_sha256"],
                "endpoint_sha256": authorization["endpoint_sha256"],
                "exact_headline_denominator": authorization["exact_headline_denominator"],
                "executable_source_sha256": authorization["executable_source_sha256"],
                "headline_budget_microcny": authorization["headline_budget_microcny"],
                "image_sha256": authorization["image_sha256"],
                "phase_id": PHASE_ID,
                "provider_policy_evidence_sha256": authorization["provider_policy_evidence_sha256"],
                "provider_tariff_evidence_sha256": authorization["provider_tariff_evidence_sha256"],
                "request_model_id": authorization["request_model_id"],
                "schema_version": APPROVAL_BINDING_SCHEMA_VERSION,
                "stage": "HEADLINE_COHORT",
                "stop_policy_sha256": authorization["stop_policy_sha256"],
                "target_bindings": authorization["target_bindings"],
            }
        )
    )


def expected_approval_text(binding_sha256: str) -> str:
    return f"APPROVE PHASE11C HEADLINE_COHORT {_expect_sha256(binding_sha256, 'invalid_approval_binding', allow_zero=False)}"


def validate_approval_text(value: Any, binding_sha256: str) -> None:
    if not isinstance(value, str) or value != expected_approval_text(binding_sha256):
        _fail("headline_approval_text_mismatch")


DIAGNOSTIC_AUTHORIZATION_FIELDS = frozenset(
    {
        "schema_version", "phase_id", "stage", "authorization_status", "authorization_sha256",
        "executable_source_sha256", "source_tree_sha256", "dockerfile_sha256", "compose_sha256",
        "image_sha256", "deployment_sha256", "runtime_identity_sha256", "provider", "request_model_id",
        "api_surface", "endpoint_id", "endpoint_sha256", "diagnostic_request_sha256",
        "provider_policy_evidence_sha256", "provider_policy_accepted", "provider_tariff_evidence_sha256", "credential_delivery_mode",
        "credential_fingerprint_sha256", "owner_account", "owner_reconfirmed", "kill_switch_bound",
        "authorization_window_start_utc", "authorization_window_end_utc", "max_logical_calls",
        "max_http_attempts", "max_input_tokens", "max_output_tokens", "input_rate_microcny_per_million",
        "cached_input_rate_microcny_per_million", "output_rate_microcny_per_million", "diagnostic_budget_microcny",
        "aggregate_remaining_microcny", "aggregate_remaining_after_reservation_microcny", "sdk_retries",
        "transport_retries", "concurrency", "local_raw_retention", "live_execution_enabled",
    }
)


def build_diagnostic_authorization_template() -> dict[str, Any]:
    return {
        "schema_version": "phase11c-gateb-protocol-diagnostic-authorization/v1",
        "phase_id": PHASE_ID,
        "stage": "DIAGNOSTIC",
        "authorization_status": "frozen_pending_exact_approval",
        "authorization_sha256": "",
        "executable_source_sha256": source_sha256(),
        "source_tree_sha256": PENDING_FREEZE,
        "dockerfile_sha256": PENDING_FREEZE,
        "compose_sha256": PENDING_FREEZE,
        "image_sha256": PENDING_FREEZE,
        "deployment_sha256": PENDING_FREEZE,
        "runtime_identity_sha256": PENDING_FREEZE,
        "provider": PROVIDER,
        "request_model_id": REQUEST_MODEL_ID,
        "api_surface": API_SURFACE,
        "endpoint_id": ENDPOINT_ID,
        "endpoint_sha256": endpoint_sha256(),
        "diagnostic_request_sha256": diagnostic_request_sha256(),
        "provider_policy_evidence_sha256": PENDING_FREEZE,
        "provider_policy_accepted": True,
        "provider_tariff_evidence_sha256": PENDING_FREEZE,
        "credential_delivery_mode": "fixed_linux_ecs_one_time_file",
        "credential_fingerprint_sha256": PENDING_FREEZE,
        "owner_account": OWNER_ACCOUNT,
        "owner_reconfirmed": True,
        "kill_switch_bound": True,
        "authorization_window_start_utc": PENDING_FREEZE,
        "authorization_window_end_utc": PENDING_FREEZE,
        "max_logical_calls": 1,
        "max_http_attempts": 1,
        "max_input_tokens": MAX_INPUT_TOKENS_PER_REQUEST,
        "max_output_tokens": MAX_OUTPUT_TOKENS_PER_REQUEST,
        "input_rate_microcny_per_million": INPUT_RATE_MICROCNY_PER_MILLION,
        "cached_input_rate_microcny_per_million": CACHED_INPUT_RATE_MICROCNY_PER_MILLION,
        "output_rate_microcny_per_million": OUTPUT_RATE_MICROCNY_PER_MILLION,
        "diagnostic_budget_microcny": DIAGNOSTIC_BUDGET_MICROCNY,
        "aggregate_remaining_microcny": AGGREGATE_BEFORE_SAME_IMAGE_DIAGNOSTIC_MICROCNY,
        "aggregate_remaining_after_reservation_microcny": AGGREGATE_BEFORE_HEADLINE_MICROCNY,
        "sdk_retries": 0,
        "transport_retries": 0,
        "concurrency": 1,
        "local_raw_retention": False,
        "live_execution_enabled": True,
    }


def _validate_diagnostic_authorization_common(
    value: Any,
    *,
    executable_source_digest: str,
    now_utc: datetime,
    sealed: bool,
    require_active_window: bool,
    allow_expired_window: bool,
) -> dict[str, Any]:
    authorization = _expect_mapping(value, "invalid_diagnostic_authorization")
    _expect_exact_keys(authorization, DIAGNOSTIC_AUTHORIZATION_FIELDS, "invalid_diagnostic_authorization_keys")
    if (
        authorization["schema_version"] != "phase11c-gateb-protocol-diagnostic-authorization/v1"
        or authorization["phase_id"] != PHASE_ID
        or authorization["stage"] != "DIAGNOSTIC"
        or authorization["authorization_status"] != "frozen_pending_exact_approval"
    ):
        _fail("diagnostic_authorization_identity_mismatch")
    if sealed:
        _validate_seal(authorization, "authorization_sha256", "diagnostic_authorization_sha256_mismatch")
    elif authorization["authorization_sha256"] != "":
        _fail("diagnostic_authorization_not_unsealed")
    if authorization["executable_source_sha256"] != _expect_sha256(executable_source_digest, "invalid_source", allow_zero=False):
        _fail("diagnostic_executable_source_sha256_drift")
    for field in (
        "source_tree_sha256", "dockerfile_sha256", "compose_sha256", "image_sha256", "deployment_sha256",
        "runtime_identity_sha256", "provider_policy_evidence_sha256", "provider_tariff_evidence_sha256",
        "credential_fingerprint_sha256",
    ):
        _expect_sha256(authorization[field], f"invalid_diagnostic_{field}", allow_zero=False)
    expected = {
        "provider": PROVIDER,
        "request_model_id": REQUEST_MODEL_ID,
        "api_surface": API_SURFACE,
        "endpoint_id": ENDPOINT_ID,
        "endpoint_sha256": endpoint_sha256(),
        "diagnostic_request_sha256": diagnostic_request_sha256(),
        "credential_delivery_mode": "fixed_linux_ecs_one_time_file",
        "owner_account": OWNER_ACCOUNT,
    }
    for field, expected_value in expected.items():
        if authorization[field] != expected_value:
            _fail(f"diagnostic_{field}_mismatch")
    for field in ("owner_reconfirmed", "kill_switch_bound", "live_execution_enabled", "provider_policy_accepted"):
        if _expect_bool(authorization[field], f"invalid_diagnostic_{field}") is not True:
            _fail(f"diagnostic_{field}_not_true")
    if _expect_bool(authorization["local_raw_retention"], "invalid_diagnostic_local_raw_retention") is not False:
        _fail("diagnostic_local_raw_retention_forbidden")
    expected_ints = {
        "max_logical_calls": 1,
        "max_http_attempts": 1,
        "max_input_tokens": MAX_INPUT_TOKENS_PER_REQUEST,
        "max_output_tokens": MAX_OUTPUT_TOKENS_PER_REQUEST,
        "input_rate_microcny_per_million": INPUT_RATE_MICROCNY_PER_MILLION,
        "cached_input_rate_microcny_per_million": CACHED_INPUT_RATE_MICROCNY_PER_MILLION,
        "output_rate_microcny_per_million": OUTPUT_RATE_MICROCNY_PER_MILLION,
        "diagnostic_budget_microcny": DIAGNOSTIC_BUDGET_MICROCNY,
        "aggregate_remaining_microcny": AGGREGATE_BEFORE_SAME_IMAGE_DIAGNOSTIC_MICROCNY,
        "aggregate_remaining_after_reservation_microcny": AGGREGATE_BEFORE_HEADLINE_MICROCNY,
        "sdk_retries": 0,
        "transport_retries": 0,
        "concurrency": 1,
    }
    for field, expected_integer in expected_ints.items():
        if _expect_nonnegative_int(authorization[field], f"invalid_diagnostic_{field}") != expected_integer:
            _fail(f"diagnostic_{field}_mismatch")
    start = _parse_utc(authorization["authorization_window_start_utc"], "invalid_diagnostic_window_start_utc")
    end = _parse_utc(authorization["authorization_window_end_utc"], "invalid_diagnostic_window_end_utc")
    now = now_utc.astimezone(timezone.utc)
    if start >= end or end - start > timedelta(minutes=30):
        _fail("diagnostic_authorization_window_invalid")
    if not sealed and start <= now:
        _fail("diagnostic_authorization_window_must_be_future")
    if require_active_window and not start <= now < end:
        _fail("diagnostic_authorization_window_not_active")
    if not require_active_window and not allow_expired_window and now >= end:
        _fail("diagnostic_authorization_window_expired")
    return authorization


def seal_diagnostic_authorization(
    value: Any, *, executable_source_digest: str | None = None, now_utc: datetime | None = None
) -> dict[str, Any]:
    candidate = _validate_diagnostic_authorization_common(
        value,
        executable_source_digest=executable_source_digest or source_sha256(),
        now_utc=now_utc or datetime.now(timezone.utc),
        sealed=False,
        require_active_window=False,
        allow_expired_window=False,
    )
    return _seal(candidate, "authorization_sha256")


def validate_diagnostic_authorization(
    value: Any,
    *,
    executable_source_digest: str | None = None,
    now_utc: datetime | None = None,
    require_active_window: bool = True,
    allow_expired_window: bool = False,
) -> dict[str, Any]:
    return _validate_diagnostic_authorization_common(
        value,
        executable_source_digest=executable_source_digest or source_sha256(),
        now_utc=now_utc or datetime.now(timezone.utc),
        sealed=True,
        require_active_window=require_active_window,
        allow_expired_window=allow_expired_window,
    )


def diagnostic_approval_binding_sha256(authorization: Mapping[str, Any]) -> str:
    _expect_sha256(authorization.get("authorization_sha256"), "invalid_diagnostic_authorization_sha256", allow_zero=False)
    return sha256_bytes(
        canonical_json(
            {
                "authorization_sha256": authorization["authorization_sha256"],
                "phase_id": PHASE_ID,
                "schema_version": "phase11c-gateb-protocol-diagnostic-approval-binding/v1",
                "stage": "DIAGNOSTIC",
            }
        )
    )


def expected_diagnostic_approval_text(binding_sha256: str) -> str:
    return f"APPROVE PHASE11C DIAGNOSTIC {_expect_sha256(binding_sha256, 'invalid_diagnostic_approval_binding', allow_zero=False)}"


def validate_diagnostic_approval_text(value: Any, binding_sha256: str) -> None:
    if not isinstance(value, str) or value != expected_diagnostic_approval_text(binding_sha256):
        _fail("diagnostic_approval_text_mismatch")


@dataclass(frozen=True)
class CredentialMetadata:
    regular_file: bool
    owner_uid: int
    mode: int
    link_count: int
    size_bytes: int
    device: int
    inode: int


def _require_linux(code: str) -> None:
    if (
        not sys.platform.startswith("linux")
        or fcntl is None
        or not isinstance(_O_CLOEXEC, int)
        or _O_CLOEXEC <= 0
        or not isinstance(_O_NOFOLLOW, int)
        or _O_NOFOLLOW <= 0
    ):
        _fail(code)


def _metadata_from_stat(value: os.stat_result) -> CredentialMetadata:
    return CredentialMetadata(
        regular_file=stat.S_ISREG(value.st_mode),
        owner_uid=value.st_uid,
        mode=stat.S_IMODE(value.st_mode),
        link_count=value.st_nlink,
        size_bytes=value.st_size,
        device=value.st_dev,
        inode=value.st_ino,
    )


def validate_credential_metadata(value: CredentialMetadata) -> None:
    if not isinstance(value, CredentialMetadata):
        _fail("credential_metadata_invalid")
    for item in (value.owner_uid, value.mode, value.link_count, value.size_bytes, value.device, value.inode):
        _expect_nonnegative_int(item, "credential_metadata_invalid")
    if not value.regular_file:
        _fail("credential_not_regular_file")
    if value.owner_uid != 0 or value.mode != 0o600:
        _fail("credential_permissions_denied")
    if value.link_count != 1:
        _fail("credential_link_count_denied")
    if not 1 <= value.size_bytes <= MAX_CREDENTIAL_BYTES:
        _fail("credential_size_invalid")


def _assert_absolute_no_symlinks(path: Path, code: str) -> None:
    if not path.is_absolute():
        _fail(code)
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current = current / part
            if stat.S_ISLNK(os.lstat(current).st_mode):
                _fail(code)
    except HeadlineCohortError:
        raise
    except OSError as exc:
        raise HeadlineCohortError(code) from exc


class CredentialReader(Protocol):
    def read(self, expected_fingerprint: str, on_opened: Callable[[], None]) -> str: ...


class FixedCredentialReader:
    """Open only the frozen headline credential path with strict TOCTOU checks."""

    def read(self, expected_fingerprint: str, on_opened: Callable[[], None]) -> str:
        _require_linux("credential_platform_unsupported")
        _expect_sha256(expected_fingerprint, "invalid_credential_fingerprint", allow_zero=False)
        if CREDENTIAL_PATH != Path("/run/crag-gateb-protocol/glm_api_key"):
            _fail("credential_path_drift")
        _assert_absolute_no_symlinks(CREDENTIAL_PATH, "credential_symlink_or_path_denied")
        try:
            descriptor = os.open(CREDENTIAL_PATH, os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW)
        except OSError as exc:
            raise HeadlineCohortError("credential_open_failed") from exc
        raw = bytearray()
        try:
            on_opened()
            before = _metadata_from_stat(os.fstat(descriptor))
            validate_credential_metadata(before)
            while len(raw) <= MAX_CREDENTIAL_BYTES:
                chunk = os.read(descriptor, min(512, MAX_CREDENTIAL_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > MAX_CREDENTIAL_BYTES:
                _fail("credential_size_invalid")
            after = _metadata_from_stat(os.fstat(descriptor))
            validate_credential_metadata(after)
            _assert_absolute_no_symlinks(CREDENTIAL_PATH, "credential_symlink_or_path_denied")
            path_after = _metadata_from_stat(os.lstat(CREDENTIAL_PATH))
            validate_credential_metadata(path_after)
            identity_before = (before.device, before.inode, before.size_bytes)
            identity_after = (after.device, after.inode, after.size_bytes)
            identity_path = (path_after.device, path_after.inode, path_after.size_bytes)
            if identity_before != identity_after or identity_after != identity_path:
                _fail("credential_identity_changed")
            if sha256_bytes(bytes(raw)) != expected_fingerprint:
                _fail("credential_fingerprint_mismatch")
            try:
                key = bytes(raw).decode("ascii").strip("\r\n")
            except UnicodeDecodeError as exc:
                raise HeadlineCohortError("credential_encoding_invalid") from exc
            if not key or key != key.strip() or any(character.isspace() for character in key):
                _fail("credential_format_invalid")
            return key
        finally:
            for index in range(len(raw)):
                raw[index] = 0
            os.close(descriptor)


def validate_live_credential(value: Any) -> str:
    """Defensively validate the value supplied by any CredentialReader implementation."""

    if not isinstance(value, str) or not value or value != value.strip() or len(value.encode("ascii", "ignore")) != len(value):
        _fail("credential_format_invalid")
    if len(value.encode("ascii")) > MAX_CREDENTIAL_BYTES or any(not 33 <= ord(character) <= 126 for character in value):
        _fail("credential_format_invalid")
    return value


@dataclass(frozen=True)
class HttpResult:
    status_code: int
    body: bytes


class ProviderTransport(Protocol):
    def dispatch(self, api_key: str, request_body: bytes) -> HttpResult: ...


class FixedHTTPSProviderTransport:
    """Direct TLS transport with no proxy, redirect, retry, or target override."""

    def dispatch(self, api_key: str, request_body: bytes) -> HttpResult:
        api_key = validate_live_credential(api_key)
        if not isinstance(request_body, bytes) or not request_body:
            _fail("request_body_invalid")
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        connection = http.client.HTTPSConnection(
            ENDPOINT_HOST, ENDPOINT_PORT, timeout=HTTP_TIMEOUT_SECONDS, context=context
        )
        try:
            connection.request(
                "POST",
                ENDPOINT_PATH,
                body=request_body,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            response_body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_PROVIDER_RESPONSE_BYTES:
                _fail("provider_response_too_large")
            return HttpResult(status_code=response.status, body=response_body)
        except HeadlineCohortError:
            raise
        except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
            raise HeadlineCohortError("provider_transport_failure") from exc
        finally:
            connection.close()


@dataclass(frozen=True)
class ParsedToolResponse:
    finish_reason_category: str
    response_shape_category: str
    tool_call_present: bool
    submit_attempt_count: int
    usage_known: bool
    input_tokens_used: int
    output_tokens_used: int
    tool_call_sha256: str
    valid_tool_call: bool
    tool_call_id: str
    canonical_arguments: str


def _provider_json_loads(body: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                _fail("provider_response_duplicate_key")
            result[key] = item
        return result

    def reject_constant(_: str) -> None:
        _fail("provider_response_invalid_json")

    try:
        return json.loads(body, object_pairs_hook=reject_duplicates, parse_constant=reject_constant)
    except HeadlineCohortError:
        raise
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise HeadlineCohortError("provider_response_invalid_json") from exc


def _usage_from_payload(payload: Mapping[str, Any]) -> tuple[bool, int, int]:
    usage = payload.get("usage")
    if usage is None:
        return False, 0, 0
    if not isinstance(usage, Mapping):
        _fail("provider_usage_schema_invalid")
    input_tokens = _expect_nonnegative_int(usage.get("prompt_tokens"), "provider_usage_schema_invalid")
    output_tokens = _expect_nonnegative_int(usage.get("completion_tokens"), "provider_usage_schema_invalid")
    if input_tokens > MAX_PROVIDER_USAGE_COUNTER or output_tokens > MAX_PROVIDER_USAGE_COUNTER:
        _fail("provider_usage_schema_invalid")
    return True, input_tokens, output_tokens


def parse_tool_response(
    body: bytes, target: Mapping[str, str], *, expected_name: str, expected_outcome: str
) -> ParsedToolResponse:
    if (expected_name, expected_outcome) not in {("probe_canary", "probe"), ("submit_canary", "submit")}:
        _fail("invalid_expected_tool")
    payload = _provider_json_loads(body)
    if not isinstance(payload, Mapping):
        _fail("provider_response_schema_invalid")
    usage_known, input_tokens, output_tokens = _usage_from_payload(payload)
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        _fail("provider_response_schema_invalid")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, Mapping):
        _fail("provider_response_schema_invalid")
    raw_finish = choice.get("finish_reason")
    if raw_finish == "tool_calls":
        finish = "tool_calls"
    elif raw_finish == "length":
        finish = "length"
    elif raw_finish == "stop":
        finish = "stop"
    else:
        finish = "other"
    calls = message.get("tool_calls")
    if calls is None:
        content = message.get("content")
        if content is None or content == "":
            return ParsedToolResponse(finish, "empty", False, 0, usage_known, input_tokens, output_tokens, ZERO_SHA256, False, "", "")
        if isinstance(content, str):
            return ParsedToolResponse(finish, "text_only", False, 0, usage_known, input_tokens, output_tokens, ZERO_SHA256, False, "", "")
        _fail("provider_response_schema_invalid")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], Mapping):
        return ParsedToolResponse(finish, "malformed_tool_call", False, 0, usage_known, input_tokens, output_tokens, ZERO_SHA256, False, "", "")
    if message.get("content") not in (None, ""):
        return ParsedToolResponse(finish, "malformed_tool_call", True, 0, usage_known, input_tokens, output_tokens, ZERO_SHA256, False, "", "")
    call = calls[0]
    function = call.get("function")
    call_id = call.get("id")
    if (
        call.get("type") != "function"
        or not isinstance(function, Mapping)
        or function.get("name") != expected_name
        or not isinstance(call_id, str)
        or not _TOOL_CALL_ID.fullmatch(call_id)
    ):
        return ParsedToolResponse(finish, "malformed_tool_call", True, 0, usage_known, input_tokens, output_tokens, ZERO_SHA256, False, "", "")
    arguments = function.get("arguments")
    if not isinstance(arguments, str) or len(arguments.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
        return ParsedToolResponse(finish, "malformed_tool_call", True, 0, usage_known, input_tokens, output_tokens, ZERO_SHA256, False, "", "")
    call_sha = sha256_bytes(arguments.encode("utf-8"))
    try:
        decoded = strict_json_loads(arguments)
    except HeadlineCohortError:
        return ParsedToolResponse(finish, "malformed_tool_call", True, 0, usage_known, input_tokens, output_tokens, call_sha, False, "", "")
    if not isinstance(decoded, Mapping) or set(decoded) != {"outcome", "payload_sha256", "target_id"}:
        return ParsedToolResponse(finish, "malformed_tool_call", True, 0, usage_known, input_tokens, output_tokens, call_sha, False, "", "")
    valid = (
        decoded.get("outcome") == expected_outcome
        and decoded.get("target_id") == target.get("stable_id")
        and decoded.get("payload_sha256") == target.get("payload_sha256")
    )
    return ParsedToolResponse(
        finish,
        "tool_call",
        True,
        1 if valid and expected_name == "submit_canary" else 0,
        usage_known,
        input_tokens,
        output_tokens,
        call_sha,
        valid,
        call_id if valid else "",
        _canonical_tool_arguments_for(target, expected_outcome) if valid else "",
    )


def _status_class(status_code: int) -> str:
    if isinstance(status_code, bool) or not isinstance(status_code, int) or status_code < 100:
        return "other"
    if 200 <= status_code <= 299:
        return "2xx"
    if 300 <= status_code <= 399:
        return "3xx"
    if 400 <= status_code <= 499:
        return "4xx"
    if 500 <= status_code <= 599:
        return "5xx"
    return "other"


TARGET_TERMINAL_CATEGORIES = frozenset(
    {
        "provider_tool_submit",
        "provider_transport_failure",
        "provider_response_too_large",
        "provider_response_invalid_json",
        "provider_response_schema_invalid",
        "provider_usage_schema_invalid",
        "provider_usage_cap_exceeded",
        "http_status_failure",
        "redirect_refused",
        "empty_response",
        "text_only_response",
        "malformed_tool_call",
        "finish_reason_invalid",
        "tool_target_mismatch",
        "usage_unknown",
        "not_run_gate_blocked",
        "quarantined",
        "credential_validation_failed",
        "internal_failure",
    }
)
EXECUTION_STATUSES = frozenset({"completed", "failed", "inconclusive", "not_run_gate_blocked", "quarantined"})

TARGET_RECEIPT_FIELDS = frozenset(
    {
        "schema_version", "phase_id", "stage", "receipt_sha256", "authorization_sha256",
        "approval_binding_sha256", "diagnostic_receipt_sha256", "target_ordinal", "target_stable_id",
        "payload_sha256", "execution_status", "terminal_category", "finish_reason_category",
        "response_shape_category", "tool_call_present", "submit_attempt_count", "logical_call_count",
        "provider_call_count", "http_attempt_count", "reserved_input_tokens", "reserved_output_tokens",
        "reserved_microcny", "usage_known", "input_tokens_used", "output_tokens_used",
        "estimated_microcny", "http_status_class", "provider_response_sha256", "tool_call_sha256",
        "raw_retained", "redaction_applied", "retry_count",
    }
)


def _target_receipt(
    *,
    authorization: Mapping[str, Any],
    binding_sha: str,
    ordinal: int,
    target: Mapping[str, str],
    execution_status: str,
    terminal_category: str,
    finish_reason_category: str = "not_observed",
    response_shape_category: str = "not_observed",
    tool_call_present: bool = False,
    submit_attempt_count: int = 0,
    logical_call_count: int = 0,
    provider_call_count: int = 0,
    http_attempt_count: int = 0,
    usage_known: bool = False,
    input_tokens_used: int = 0,
    output_tokens_used: int = 0,
    estimated_microcny: int = PER_TARGET_BUDGET_MICROCNY,
    http_status_class: str = "none",
    provider_response_sha256: str = ZERO_SHA256,
    tool_call_sha256: str = ZERO_SHA256,
) -> dict[str, Any]:
    if execution_status not in EXECUTION_STATUSES or terminal_category not in TARGET_TERMINAL_CATEGORIES:
        _fail("target_terminal_invalid")
    if not 1 <= ordinal <= HEADLINE_TARGET_COUNT:
        _fail("target_ordinal_invalid")
    return validate_target_receipt(_seal(
        {
            "schema_version": TARGET_RECEIPT_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "stage": "HEADLINE_COHORT",
            "receipt_sha256": "",
            "authorization_sha256": authorization["authorization_sha256"],
            "approval_binding_sha256": binding_sha,
            "diagnostic_receipt_sha256": authorization["diagnostic_receipt_sha256"],
            "target_ordinal": ordinal,
            "target_stable_id": target["stable_id"],
            "payload_sha256": target["payload_sha256"],
            "execution_status": execution_status,
            "terminal_category": terminal_category,
            "finish_reason_category": finish_reason_category,
            "response_shape_category": response_shape_category,
            "tool_call_present": tool_call_present,
            "submit_attempt_count": submit_attempt_count,
            "logical_call_count": logical_call_count,
            "provider_call_count": provider_call_count,
            "http_attempt_count": http_attempt_count,
            "reserved_input_tokens": MAX_INPUT_TOKENS_PER_REQUEST * REQUESTS_PER_TARGET,
            "reserved_output_tokens": MAX_OUTPUT_TOKENS_PER_REQUEST * REQUESTS_PER_TARGET,
            "reserved_microcny": PER_TARGET_BUDGET_MICROCNY,
            "usage_known": usage_known,
            "input_tokens_used": input_tokens_used,
            "output_tokens_used": output_tokens_used,
            "estimated_microcny": estimated_microcny,
            "http_status_class": http_status_class,
            "provider_response_sha256": provider_response_sha256,
            "tool_call_sha256": tool_call_sha256,
            "raw_retained": False,
            "redaction_applied": True,
            "retry_count": 0,
        },
        "receipt_sha256",
    ))


def validate_target_receipt(value: Any) -> dict[str, Any]:
    receipt = _expect_mapping(value, "invalid_target_receipt")
    _expect_exact_keys(receipt, TARGET_RECEIPT_FIELDS, "invalid_target_receipt_keys")
    if receipt["schema_version"] != TARGET_RECEIPT_SCHEMA_VERSION or receipt["phase_id"] != PHASE_ID:
        _fail("target_receipt_identity_mismatch")
    if receipt["stage"] != "HEADLINE_COHORT":
        _fail("target_receipt_stage_mismatch")
    if receipt["execution_status"] not in EXECUTION_STATUSES or receipt["terminal_category"] not in TARGET_TERMINAL_CATEGORIES:
        _fail("target_receipt_terminal_invalid")
    if receipt["finish_reason_category"] not in {"tool_calls", "stop", "length", "other", "not_observed"}:
        _fail("target_receipt_finish_reason_invalid")
    if receipt["response_shape_category"] not in {"tool_call", "empty", "text_only", "malformed_tool_call", "not_observed"}:
        _fail("target_receipt_shape_invalid")
    for field in ("receipt_sha256", "authorization_sha256", "approval_binding_sha256", "diagnostic_receipt_sha256", "payload_sha256"):
        _expect_sha256(receipt[field], f"invalid_target_receipt_{field}", allow_zero=False)
    for field in ("provider_response_sha256", "tool_call_sha256"):
        _expect_sha256(receipt[field], f"invalid_target_receipt_{field}")
    if not isinstance(receipt["target_stable_id"], str) or not _OWNER.fullmatch(receipt["target_stable_id"].replace("p11c-", "x")):
        # The exact binding check below is the authoritative target-ID validation.
        _fail("target_receipt_target_invalid")
    ordinal = _expect_nonnegative_int(receipt["target_ordinal"], "target_receipt_ordinal_invalid")
    if not 1 <= ordinal <= HEADLINE_TARGET_COUNT:
        _fail("target_receipt_ordinal_invalid")
    expected_target = HEADLINE_TARGETS[ordinal - 1]
    if receipt["target_stable_id"] != expected_target["stable_id"] or receipt["payload_sha256"] != expected_target["payload_sha256"]:
        _fail("target_receipt_target_mismatch")
    for field in ("tool_call_present", "usage_known", "raw_retained", "redaction_applied"):
        _expect_bool(receipt[field], f"invalid_target_receipt_{field}")
    for field in (
        "submit_attempt_count", "logical_call_count", "provider_call_count", "http_attempt_count",
        "reserved_input_tokens", "reserved_output_tokens", "reserved_microcny", "input_tokens_used",
        "output_tokens_used", "estimated_microcny", "retry_count",
    ):
        _expect_nonnegative_int(receipt[field], f"invalid_target_receipt_{field}")
    if receipt["raw_retained"] is not False or receipt["redaction_applied"] is not True or receipt["retry_count"] != 0:
        _fail("target_receipt_retention_or_retry_invalid")
    if (
        receipt["reserved_input_tokens"] != MAX_INPUT_TOKENS_PER_REQUEST * REQUESTS_PER_TARGET
        or receipt["reserved_output_tokens"] != MAX_OUTPUT_TOKENS_PER_REQUEST * REQUESTS_PER_TARGET
        or receipt["reserved_microcny"] != PER_TARGET_BUDGET_MICROCNY
    ):
        _fail("target_receipt_reservation_invalid")
    if receipt["usage_known"] is False and (receipt["input_tokens_used"] != 0 or receipt["output_tokens_used"] != 0):
        _fail("target_receipt_unknown_usage_invalid")
    if receipt["logical_call_count"] != receipt["provider_call_count"] or receipt["provider_call_count"] != receipt["http_attempt_count"]:
        _fail("target_receipt_attempt_count_invalid")
    if receipt["http_attempt_count"] > REQUESTS_PER_TARGET or receipt["submit_attempt_count"] > 1:
        _fail("target_receipt_attempt_cap_exceeded")
    if receipt["http_status_class"] not in {"none", "2xx", "3xx", "4xx", "5xx", "other"}:
        _fail("target_receipt_http_status_invalid")
    if receipt["usage_known"] is True and receipt["estimated_microcny"] != worst_case_microcny(
        input_tokens=receipt["input_tokens_used"], output_tokens=receipt["output_tokens_used"]
    ):
        _fail("target_receipt_estimate_invalid")
    if receipt["http_attempt_count"] == 0:
        if not (
            receipt["http_status_class"] == "none"
            and receipt["provider_response_sha256"] == ZERO_SHA256
            and receipt["tool_call_sha256"] == ZERO_SHA256
            and receipt["estimated_microcny"] == 0
        ):
            _fail("target_receipt_zero_attempt_invariant_failed")
    elif receipt["usage_known"] is False and receipt["estimated_microcny"] != PER_TARGET_BUDGET_MICROCNY:
        _fail("target_receipt_unknown_usage_reservation_invalid")
    if receipt["execution_status"] == "completed":
        if not (
            receipt["terminal_category"] == "provider_tool_submit"
            and receipt["finish_reason_category"] == "tool_calls"
            and receipt["response_shape_category"] == "tool_call"
            and receipt["tool_call_present"] is True
            and receipt["submit_attempt_count"] == 1
            and receipt["logical_call_count"] == REQUESTS_PER_TARGET
            and receipt["provider_call_count"] == REQUESTS_PER_TARGET
            and receipt["http_attempt_count"] == REQUESTS_PER_TARGET
            and receipt["usage_known"] is True
            and receipt["http_status_class"] == "2xx"
            and receipt["provider_response_sha256"] != ZERO_SHA256
            and receipt["tool_call_sha256"] != ZERO_SHA256
            and receipt["input_tokens_used"] <= REQUESTS_PER_TARGET * MAX_INPUT_TOKENS_PER_REQUEST
            and receipt["output_tokens_used"] <= REQUESTS_PER_TARGET * MAX_OUTPUT_TOKENS_PER_REQUEST
            and receipt["estimated_microcny"] <= PER_TARGET_BUDGET_MICROCNY
        ):
            _fail("target_receipt_completed_invariant_failed")
    elif receipt["execution_status"] == "not_run_gate_blocked":
        if not (
            receipt["terminal_category"] == "not_run_gate_blocked"
            and receipt["logical_call_count"] == 0
            and receipt["tool_call_present"] is False
            and receipt["submit_attempt_count"] == 0
        ):
            _fail("target_receipt_not_run_invariant_failed")
    elif receipt["terminal_category"] == "not_run_gate_blocked":
        _fail("target_receipt_terminal_status_mismatch")
    elif receipt["terminal_category"] == "provider_tool_submit":
        _fail("target_receipt_terminal_status_mismatch")
    _validate_seal(receipt, "receipt_sha256", "target_receipt_sha256_mismatch")
    return receipt


COHORT_RECEIPT_FIELDS = frozenset(
    {
        "schema_version", "phase_id", "stage", "receipt_sha256", "authorization_sha256",
        "approval_binding_sha256", "diagnostic_receipt_sha256", "cohort_manifest_sha256",
        "execution_status", "stop_policy", "stop_policy_sha256", "stopped_after_ordinal", "target_receipts",
        "headline_target_count", "completed_target_count", "logical_call_count", "provider_call_count",
        "http_attempt_count", "reserved_input_tokens", "reserved_output_tokens", "reserved_microcny",
        "aggregate_remaining_microcny", "aggregate_remaining_after_reservation_microcny", "usage_known",
        "input_tokens_used", "output_tokens_used", "estimated_microcny",
        "raw_retained", "redaction_applied", "retry_count",
    }
)


def build_cohort_receipt(
    *, authorization: Mapping[str, Any], binding_sha: str, target_receipts: Sequence[Mapping[str, Any]], execution_status: str, stopped_after_ordinal: int
) -> dict[str, Any]:
    validated = [validate_target_receipt(receipt) for receipt in target_receipts]
    if execution_status not in EXECUTION_STATUSES:
        _fail("cohort_execution_status_invalid")
    if len(validated) != HEADLINE_TARGET_COUNT or not 1 <= stopped_after_ordinal <= HEADLINE_TARGET_COUNT:
        _fail("cohort_stop_ordinal_invalid")
    for ordinal, receipt in enumerate(validated, start=1):
        if (
            receipt["target_ordinal"] != ordinal
            or receipt["authorization_sha256"] != authorization["authorization_sha256"]
            or receipt["approval_binding_sha256"] != binding_sha
            or receipt["diagnostic_receipt_sha256"] != authorization["diagnostic_receipt_sha256"]
        ):
            _fail("cohort_target_receipt_order_invalid")
    first_noncompleted = next((receipt for receipt in validated if receipt["execution_status"] != "completed"), None)
    if first_noncompleted is None:
        if execution_status != "completed" or stopped_after_ordinal != HEADLINE_TARGET_COUNT:
            _fail("cohort_completed_status_invalid")
    elif (
        execution_status != first_noncompleted["execution_status"]
        or stopped_after_ordinal != first_noncompleted["target_ordinal"]
    ):
        _fail("cohort_stop_status_invalid")
    total_logical = sum(item["logical_call_count"] for item in validated)
    total_provider = sum(item["provider_call_count"] for item in validated)
    total_attempts = sum(item["http_attempt_count"] for item in validated)
    usage_known = all(item["usage_known"] for item in validated) and len(validated) == HEADLINE_TARGET_COUNT
    estimated = sum(item["estimated_microcny"] for item in validated)
    input_tokens_used = sum(item["input_tokens_used"] for item in validated) if usage_known else 0
    output_tokens_used = sum(item["output_tokens_used"] for item in validated) if usage_known else 0
    return validate_cohort_receipt(_seal(
        {
            "schema_version": COHORT_RECEIPT_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "stage": "HEADLINE_COHORT",
            "receipt_sha256": "",
            "authorization_sha256": authorization["authorization_sha256"],
            "approval_binding_sha256": binding_sha,
            "diagnostic_receipt_sha256": authorization["diagnostic_receipt_sha256"],
            "cohort_manifest_sha256": authorization["cohort_manifest_sha256"],
            "execution_status": execution_status,
            "stop_policy": STOP_POLICY,
            "stop_policy_sha256": stop_policy_sha256(),
            "stopped_after_ordinal": stopped_after_ordinal,
            "target_receipts": [
                {
                    "target_ordinal": item["target_ordinal"],
                    "target_stable_id": item["target_stable_id"],
                    "payload_sha256": item["payload_sha256"],
                    "receipt_sha256": item["receipt_sha256"],
                    "execution_status": item["execution_status"],
                    "terminal_category": item["terminal_category"],
                }
                for item in validated
            ],
            "headline_target_count": HEADLINE_TARGET_COUNT,
            "completed_target_count": sum(item["execution_status"] == "completed" for item in validated),
            "logical_call_count": total_logical,
            "provider_call_count": total_provider,
            "http_attempt_count": total_attempts,
            "reserved_input_tokens": HEADLINE_REQUEST_COUNT * MAX_INPUT_TOKENS_PER_REQUEST,
            "reserved_output_tokens": HEADLINE_REQUEST_COUNT * MAX_OUTPUT_TOKENS_PER_REQUEST,
            "reserved_microcny": HEADLINE_BUDGET_MICROCNY,
            "aggregate_remaining_microcny": authorization["aggregate_remaining_microcny"],
            "aggregate_remaining_after_reservation_microcny": authorization[
                "aggregate_remaining_after_reservation_microcny"
            ],
            "usage_known": usage_known,
            "input_tokens_used": input_tokens_used,
            "output_tokens_used": output_tokens_used,
            "estimated_microcny": estimated if usage_known else HEADLINE_BUDGET_MICROCNY,
            "raw_retained": False,
            "redaction_applied": True,
            "retry_count": 0,
        },
        "receipt_sha256",
    ))


COHORT_TARGET_SUMMARY_FIELDS = frozenset(
    {
        "target_ordinal",
        "target_stable_id",
        "payload_sha256",
        "receipt_sha256",
        "execution_status",
        "terminal_category",
    }
)


def validate_cohort_receipt(value: Any) -> dict[str, Any]:
    receipt = _expect_mapping(value, "invalid_cohort_receipt")
    _expect_exact_keys(receipt, COHORT_RECEIPT_FIELDS, "invalid_cohort_receipt_keys")
    if (
        receipt["schema_version"] != COHORT_RECEIPT_SCHEMA_VERSION
        or receipt["phase_id"] != PHASE_ID
        or receipt["stage"] != "HEADLINE_COHORT"
        or receipt["stop_policy"] != STOP_POLICY
        or receipt["stop_policy_sha256"] != stop_policy_sha256()
    ):
        _fail("cohort_receipt_identity_mismatch")
    for field in (
        "receipt_sha256",
        "authorization_sha256",
        "approval_binding_sha256",
        "diagnostic_receipt_sha256",
        "cohort_manifest_sha256",
        "stop_policy_sha256",
    ):
        _expect_sha256(receipt[field], f"invalid_cohort_receipt_{field}", allow_zero=False)
    if receipt["execution_status"] not in EXECUTION_STATUSES:
        _fail("cohort_receipt_status_invalid")
    stopped = _expect_nonnegative_int(receipt["stopped_after_ordinal"], "cohort_receipt_stop_invalid")
    if not 1 <= stopped <= HEADLINE_TARGET_COUNT:
        _fail("cohort_receipt_stop_invalid")
    summaries = receipt["target_receipts"]
    if not isinstance(summaries, list) or len(summaries) != HEADLINE_TARGET_COUNT:
        _fail("cohort_receipt_target_count_invalid")
    completed = 0
    first_noncompleted: dict[str, Any] | None = None
    target_receipt_hashes: set[str] = set()
    for ordinal, summary_value in enumerate(summaries, start=1):
        summary = _expect_mapping(summary_value, "cohort_receipt_target_summary_invalid")
        _expect_exact_keys(summary, COHORT_TARGET_SUMMARY_FIELDS, "cohort_receipt_target_summary_keys")
        target = HEADLINE_TARGETS[ordinal - 1]
        if (
            summary["target_ordinal"] != ordinal
            or summary["target_stable_id"] != target["stable_id"]
            or summary["payload_sha256"] != target["payload_sha256"]
            or summary["execution_status"] not in EXECUTION_STATUSES
            or summary["terminal_category"] not in TARGET_TERMINAL_CATEGORIES
        ):
            _fail("cohort_receipt_target_summary_mismatch")
        _expect_sha256(summary["receipt_sha256"], "cohort_receipt_target_receipt_sha_invalid", allow_zero=False)
        if summary["receipt_sha256"] in target_receipt_hashes:
            _fail("cohort_receipt_duplicate_target_receipt")
        target_receipt_hashes.add(summary["receipt_sha256"])
        if first_noncompleted is not None:
            if (
                summary["execution_status"] != "not_run_gate_blocked"
                or summary["terminal_category"] != "not_run_gate_blocked"
            ):
                _fail("cohort_receipt_stop_policy_mismatch")
        elif summary["execution_status"] == "completed":
            completed += 1
        else:
            first_noncompleted = summary
    if receipt["headline_target_count"] != HEADLINE_TARGET_COUNT or receipt["completed_target_count"] != completed:
        _fail("cohort_receipt_completion_count_invalid")
    if first_noncompleted is None:
        if receipt["execution_status"] != "completed" or stopped != HEADLINE_TARGET_COUNT:
            _fail("cohort_receipt_completed_invariant_failed")
    elif receipt["execution_status"] != first_noncompleted["execution_status"] or stopped != first_noncompleted["target_ordinal"]:
        _fail("cohort_receipt_stop_invariant_failed")
    for field in (
        "logical_call_count", "provider_call_count", "http_attempt_count", "reserved_input_tokens",
        "reserved_output_tokens", "reserved_microcny", "input_tokens_used", "output_tokens_used",
        "estimated_microcny", "aggregate_remaining_microcny", "aggregate_remaining_after_reservation_microcny", "retry_count",
    ):
        _expect_nonnegative_int(receipt[field], f"invalid_cohort_receipt_{field}")
    for field in ("usage_known", "raw_retained", "redaction_applied"):
        _expect_bool(receipt[field], f"invalid_cohort_receipt_{field}")
    if (
        receipt["logical_call_count"] != receipt["provider_call_count"]
        or receipt["provider_call_count"] != receipt["http_attempt_count"]
        or receipt["http_attempt_count"] > HEADLINE_REQUEST_COUNT
        or receipt["reserved_input_tokens"] != HEADLINE_REQUEST_COUNT * MAX_INPUT_TOKENS_PER_REQUEST
        or receipt["reserved_output_tokens"] != HEADLINE_REQUEST_COUNT * MAX_OUTPUT_TOKENS_PER_REQUEST
        or receipt["reserved_microcny"] != HEADLINE_BUDGET_MICROCNY
        or receipt["aggregate_remaining_microcny"] != AGGREGATE_BEFORE_HEADLINE_MICROCNY
        or receipt["aggregate_remaining_after_reservation_microcny"] != AGGREGATE_AFTER_HEADLINE_MICROCNY
        or receipt["raw_retained"] is not False
        or receipt["redaction_applied"] is not True
        or receipt["retry_count"] != 0
    ):
        _fail("cohort_receipt_accounting_or_redaction_invalid")
    if receipt["usage_known"] is False:
        if (
            receipt["input_tokens_used"] != 0
            or receipt["output_tokens_used"] != 0
            or receipt["estimated_microcny"] != HEADLINE_BUDGET_MICROCNY
        ):
            _fail("cohort_receipt_unknown_usage_invalid")
    elif receipt["estimated_microcny"] != worst_case_microcny(
        input_tokens=receipt["input_tokens_used"], output_tokens=receipt["output_tokens_used"]
    ):
        _fail("cohort_receipt_estimate_invalid")
    if receipt["execution_status"] == "completed" and (
        receipt["input_tokens_used"] > HEADLINE_REQUEST_COUNT * MAX_INPUT_TOKENS_PER_REQUEST
        or receipt["output_tokens_used"] > HEADLINE_REQUEST_COUNT * MAX_OUTPUT_TOKENS_PER_REQUEST
        or receipt["estimated_microcny"] > HEADLINE_BUDGET_MICROCNY
    ):
        _fail("cohort_receipt_completed_budget_invalid")
    _validate_seal(receipt, "receipt_sha256", "cohort_receipt_sha256_mismatch")
    return receipt


LEDGER_FIELDS = frozenset(
    {
        "schema_version",
        "phase_id",
        "ledger_sha256",
        "run_id_sha256",
        "cohort_receipt_sha256",
        "authorization_sha256",
        "approval_binding_sha256",
        "diagnostic_receipt_sha256",
        "target_receipt_sha256s",
        "execution_status",
        "logical_call_count",
        "provider_call_count",
        "http_attempt_count",
        "reserved_microcny",
        "estimated_microcny",
        "aggregate_remaining_microcny",
        "aggregate_remaining_after_reservation_microcny",
        "raw_retained",
        "redaction_applied",
        "retry_count",
    }
)


def _run_id_sha256(cohort: Mapping[str, Any]) -> str:
    return sha256_bytes(
        canonical_json(
            {
                "authorization_sha256": cohort["authorization_sha256"],
                "approval_binding_sha256": cohort["approval_binding_sha256"],
                "diagnostic_receipt_sha256": cohort["diagnostic_receipt_sha256"],
                "phase_id": PHASE_ID,
            }
        )
    )


def build_ledger(cohort_receipt: Mapping[str, Any]) -> dict[str, Any]:
    cohort = validate_cohort_receipt(cohort_receipt)
    return validate_ledger(_seal(
        {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "ledger_sha256": "",
            "run_id_sha256": _run_id_sha256(cohort),
            "cohort_receipt_sha256": cohort["receipt_sha256"],
            "authorization_sha256": cohort["authorization_sha256"],
            "approval_binding_sha256": cohort["approval_binding_sha256"],
            "diagnostic_receipt_sha256": cohort["diagnostic_receipt_sha256"],
            "target_receipt_sha256s": [item["receipt_sha256"] for item in cohort["target_receipts"]],
            "execution_status": cohort["execution_status"],
            "logical_call_count": cohort["logical_call_count"],
            "provider_call_count": cohort["provider_call_count"],
            "http_attempt_count": cohort["http_attempt_count"],
            "reserved_microcny": cohort["reserved_microcny"],
            "estimated_microcny": cohort["estimated_microcny"],
            "aggregate_remaining_microcny": cohort["aggregate_remaining_microcny"],
            "aggregate_remaining_after_reservation_microcny": cohort[
                "aggregate_remaining_after_reservation_microcny"
            ],
            "raw_retained": False,
            "redaction_applied": True,
            "retry_count": 0,
        },
        "ledger_sha256",
    ))


def validate_ledger(value: Any) -> dict[str, Any]:
    ledger = _expect_mapping(value, "invalid_ledger")
    _expect_exact_keys(ledger, LEDGER_FIELDS, "invalid_ledger_keys")
    if (
        ledger["schema_version"] != LEDGER_SCHEMA_VERSION
        or ledger["phase_id"] != PHASE_ID
        or ledger["execution_status"] not in EXECUTION_STATUSES
    ):
        _fail("ledger_identity_mismatch")
    for field in (
        "ledger_sha256",
        "run_id_sha256",
        "cohort_receipt_sha256",
        "authorization_sha256",
        "approval_binding_sha256",
        "diagnostic_receipt_sha256",
    ):
        _expect_sha256(ledger[field], f"invalid_ledger_{field}", allow_zero=False)
    target_hashes = ledger["target_receipt_sha256s"]
    if (
        not isinstance(target_hashes, list)
        or len(target_hashes) != HEADLINE_TARGET_COUNT
        or len(set(target_hashes)) != HEADLINE_TARGET_COUNT
    ):
        _fail("ledger_target_receipts_invalid")
    for item in target_hashes:
        _expect_sha256(item, "ledger_target_receipt_sha_invalid", allow_zero=False)
    if ledger["run_id_sha256"] != sha256_bytes(
        canonical_json(
            {
                "authorization_sha256": ledger["authorization_sha256"],
                "approval_binding_sha256": ledger["approval_binding_sha256"],
                "diagnostic_receipt_sha256": ledger["diagnostic_receipt_sha256"],
                "phase_id": PHASE_ID,
            }
        )
    ):
        _fail("ledger_run_id_mismatch")
    for field in (
        "logical_call_count",
        "provider_call_count",
        "http_attempt_count",
        "reserved_microcny",
        "estimated_microcny",
        "aggregate_remaining_microcny",
        "aggregate_remaining_after_reservation_microcny",
        "retry_count",
    ):
        _expect_nonnegative_int(ledger[field], f"invalid_ledger_{field}")
    for field in ("raw_retained", "redaction_applied"):
        _expect_bool(ledger[field], f"invalid_ledger_{field}")
    if (
        ledger["logical_call_count"] != ledger["provider_call_count"]
        or ledger["provider_call_count"] != ledger["http_attempt_count"]
        or ledger["http_attempt_count"] > HEADLINE_REQUEST_COUNT
        or ledger["reserved_microcny"] != HEADLINE_BUDGET_MICROCNY
        or ledger["aggregate_remaining_microcny"] != AGGREGATE_BEFORE_HEADLINE_MICROCNY
        or ledger["aggregate_remaining_after_reservation_microcny"] != AGGREGATE_AFTER_HEADLINE_MICROCNY
        or ledger["raw_retained"] is not False
        or ledger["redaction_applied"] is not True
        or ledger["retry_count"] != 0
    ):
        _fail("ledger_accounting_or_redaction_invalid")
    if ledger["execution_status"] == "completed" and ledger["estimated_microcny"] > HEADLINE_BUDGET_MICROCNY:
        _fail("ledger_completed_budget_invalid")
    _validate_seal(ledger, "ledger_sha256", "ledger_sha256_mismatch")
    return ledger


STATE_FIELDS = frozenset(
    {
        "schema_version", "phase_id", "state_sha256", "authorization_sha256", "approval_binding_sha256",
        "execution_status", "approval_consumed", "budget_reserved", "credential_file_opened",
        "credential_validated", "next_target_ordinal", "current_target_ordinal", "logical_call_count", "provider_call_count",
        "http_attempt_count", "reserved_input_tokens", "reserved_output_tokens", "reserved_microcny",
    }
)
STATE_ORDER = {
    "approval_consumed": 0,
    "budget_reserved": 1,
    "credential_opened": 2,
    "credential_validated": 3,
    "running": 4,
    "terminal": 5,
}


def _new_state(authorization_sha: str, binding_sha: str) -> dict[str, Any]:
    return _seal(
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "state_sha256": "",
            "authorization_sha256": authorization_sha,
            "approval_binding_sha256": binding_sha,
            "execution_status": "approval_consumed",
            "approval_consumed": True,
            "budget_reserved": False,
            "credential_file_opened": False,
            "credential_validated": False,
            "next_target_ordinal": 1,
            "current_target_ordinal": 0,
            "logical_call_count": 0,
            "provider_call_count": 0,
            "http_attempt_count": 0,
            "reserved_input_tokens": 0,
            "reserved_output_tokens": 0,
            "reserved_microcny": 0,
        },
        "state_sha256",
    )


def validate_state(value: Any) -> dict[str, Any]:
    state_value = _expect_mapping(value, "invalid_state")
    _expect_exact_keys(state_value, STATE_FIELDS, "invalid_state_keys")
    if state_value["schema_version"] != STATE_SCHEMA_VERSION or state_value["phase_id"] != PHASE_ID:
        _fail("state_identity_mismatch")
    if state_value["execution_status"] not in STATE_ORDER:
        _fail("state_status_invalid")
    for field in ("authorization_sha256", "approval_binding_sha256"):
        _expect_sha256(state_value[field], f"invalid_state_{field}", allow_zero=False)
    for field in ("approval_consumed", "budget_reserved", "credential_file_opened", "credential_validated"):
        _expect_bool(state_value[field], f"invalid_state_{field}")
    for field in (
        "next_target_ordinal", "current_target_ordinal", "logical_call_count", "provider_call_count", "http_attempt_count",
        "reserved_input_tokens", "reserved_output_tokens", "reserved_microcny",
    ):
        _expect_nonnegative_int(state_value[field], f"invalid_state_{field}")
    if state_value["approval_consumed"] is not True or not 1 <= state_value["next_target_ordinal"] <= HEADLINE_TARGET_COUNT + 1:
        _fail("state_approval_invariant_failed")
    if not 0 <= state_value["current_target_ordinal"] <= HEADLINE_TARGET_COUNT:
        _fail("state_current_target_invalid")
    status = state_value["execution_status"]
    if status in {"budget_reserved", "credential_opened", "credential_validated", "running", "terminal"} and state_value["budget_reserved"] is not True:
        _fail("state_budget_order_invalid")
    if status in {"credential_opened", "credential_validated", "running"} and state_value["credential_file_opened"] is not True:
        _fail("state_credential_open_order_invalid")
    if status in {"credential_validated", "running"} and state_value["credential_validated"] is not True:
        _fail("state_credential_validation_order_invalid")
    if status == "running" and not 1 <= state_value["current_target_ordinal"] <= HEADLINE_TARGET_COUNT:
        _fail("state_target_start_invariant_failed")
    if status != "running" and state_value["current_target_ordinal"] != 0:
        _fail("state_target_not_running_invariant_failed")
    if state_value["credential_validated"] and not state_value["credential_file_opened"]:
        _fail("state_credential_order_invalid")
    if state_value["provider_call_count"] != state_value["http_attempt_count"] or state_value["logical_call_count"] != state_value["http_attempt_count"]:
        _fail("state_attempt_count_invalid")
    if state_value["http_attempt_count"] > HEADLINE_REQUEST_COUNT:
        _fail("state_attempt_cap_exceeded")
    if state_value["budget_reserved"]:
        if (
            state_value["reserved_input_tokens"] != HEADLINE_REQUEST_COUNT * MAX_INPUT_TOKENS_PER_REQUEST
            or state_value["reserved_output_tokens"] != HEADLINE_REQUEST_COUNT * MAX_OUTPUT_TOKENS_PER_REQUEST
            or state_value["reserved_microcny"] != HEADLINE_BUDGET_MICROCNY
        ):
            _fail("state_budget_invariant_failed")
    elif any(state_value[field] != 0 for field in ("reserved_input_tokens", "reserved_output_tokens", "reserved_microcny")):
        _fail("state_unreserved_budget_nonzero")
    _validate_seal(state_value, "state_sha256", "state_sha256_mismatch")
    return state_value


def _transition_state(current: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    previous = validate_state(current)
    if set(changes) - STATE_FIELDS or "state_sha256" in changes:
        _fail("state_transition_keys_invalid")
    updated = dict(previous)
    updated.update(changes)
    if STATE_ORDER[updated["execution_status"]] < STATE_ORDER[previous["execution_status"]]:
        _fail("state_transition_rollback")
    for field in ("approval_consumed", "budget_reserved", "credential_file_opened", "credential_validated"):
        if previous[field] is True and updated[field] is not True:
            _fail("state_transition_rollback")
    for field in ("logical_call_count", "provider_call_count", "http_attempt_count", "next_target_ordinal"):
        if _expect_nonnegative_int(updated[field], "state_transition_counter_invalid") < previous[field]:
            _fail("state_transition_rollback")
    updated["state_sha256"] = ""
    return validate_state(_seal(updated, "state_sha256"))


class CohortStateStore(Protocol):
    @property
    def state(self) -> dict[str, Any] | None: ...

    def begin(self, authorization_sha: str, binding_sha: str) -> None: ...

    def transition(self, **changes: Any) -> None: ...

    def write_target(self, receipt: Mapping[str, Any]) -> None: ...

    def write_cohort(self, receipt: Mapping[str, Any]) -> None: ...


class InMemoryCohortStateStore:
    def __init__(self) -> None:
        self._state: dict[str, Any] | None = None
        self.target_receipts: list[dict[str, Any]] = []
        self.cohort_receipt: dict[str, Any] | None = None
        self.ledger: dict[str, Any] | None = None
        self.events: list[str] = []

    @property
    def state(self) -> dict[str, Any] | None:
        return deepcopy(self._state)

    def begin(self, authorization_sha: str, binding_sha: str) -> None:
        if self._state is not None:
            _fail("cohort_already_consumed")
        self._state = _new_state(authorization_sha, binding_sha)
        self.events.append("approval_consumed")

    def transition(self, **changes: Any) -> None:
        if self._state is None:
            _fail("state_not_started")
        self._state = _transition_state(self._state, **changes)
        self.events.append(self._state["execution_status"])

    def write_target(self, receipt: Mapping[str, Any]) -> None:
        validated = validate_target_receipt(receipt)
        if self.cohort_receipt is not None or len(self.target_receipts) + 1 != validated["target_ordinal"]:
            _fail("target_receipt_order_invalid")
        self.target_receipts.append(validated)
        self.events.append(f"target_{validated['target_ordinal']}_written")

    def write_cohort(self, receipt: Mapping[str, Any]) -> None:
        if self.cohort_receipt is not None:
            _fail("cohort_receipt_already_written")
        validated = validate_cohort_receipt(receipt)
        if len(self.target_receipts) != HEADLINE_TARGET_COUNT or [
            item["receipt_sha256"] for item in self.target_receipts
        ] != [item["receipt_sha256"] for item in validated["target_receipts"]]:
            _fail("cohort_receipt_target_linkage_invalid")
        self.cohort_receipt = validated
        self.ledger = build_ledger(validated)
        self.events.append("cohort_receipt_written")


def _path_entry_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise HeadlineCohortError("state_persistence_failure") from exc
    return True


class FileCohortStateStore:
    """Dedicated fsync-backed, no-replay state store for headline execution."""

    def __init__(self) -> None:
        self._state: dict[str, Any] | None = None
        self._lock_descriptor: int | None = None
        self._target_receipts: list[dict[str, Any]] = []

    @property
    def state(self) -> dict[str, Any] | None:
        return deepcopy(self._state)

    def __enter__(self) -> FileCohortStateStore:
        _require_linux("state_store_platform_unsupported")
        try:
            STATE_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
            _assert_absolute_no_symlinks(STATE_DIRECTORY, "state_directory_symlink_denied")
            directory_stat = os.lstat(STATE_DIRECTORY)
            if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != 0:
                _fail("state_directory_metadata_denied")
            os.chmod(STATE_DIRECTORY, 0o700)
            self._lock_descriptor = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | _O_CLOEXEC | _O_NOFOLLOW, 0o600)
            if _FCHMOD is None:
                _fail("state_store_platform_unsupported")
            _FCHMOD(self._lock_descriptor, 0o600)
            fcntl.flock(self._lock_descriptor, fcntl.LOCK_EX)
            target_artifacts = list(STATE_DIRECTORY.glob("target-*.json"))
            if _path_entry_exists(STATE_PATH) or _path_entry_exists(COHORT_RECEIPT_PATH) or _path_entry_exists(LEDGER_PATH) or target_artifacts:
                _fail("cohort_quarantined")
            return self
        except HeadlineCohortError:
            self._close_lock()
            raise
        except OSError as exc:
            self._close_lock()
            raise HeadlineCohortError("state_persistence_failure") from exc

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._close_lock()

    def _close_lock(self) -> None:
        if self._lock_descriptor is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_descriptor)
                self._lock_descriptor = None

    def _atomic_write(self, destination: Path, value: Mapping[str, Any]) -> None:
        if self._lock_descriptor is None:
            _fail("state_lock_not_held")
        payload = canonical_json(value) + b"\n"
        temporary = STATE_DIRECTORY / f".{destination.name}.{os.getpid()}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC | _O_NOFOLLOW, 0o600)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    _fail("state_persistence_failure")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, destination)
            directory_descriptor = os.open(STATE_DIRECTORY, os.O_RDONLY | _O_CLOEXEC)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except HeadlineCohortError:
            raise
        except OSError as exc:
            raise HeadlineCohortError("state_persistence_failure") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

    def begin(self, authorization_sha: str, binding_sha: str) -> None:
        if self._state is not None or _path_entry_exists(STATE_PATH):
            _fail("cohort_already_consumed")
        self._state = _new_state(authorization_sha, binding_sha)
        self._atomic_write(STATE_PATH, self._state)

    def transition(self, **changes: Any) -> None:
        if self._state is None:
            _fail("state_not_started")
        self._state = _transition_state(self._state, **changes)
        self._atomic_write(STATE_PATH, self._state)

    def write_target(self, receipt: Mapping[str, Any]) -> None:
        validated = validate_target_receipt(receipt)
        destination = STATE_DIRECTORY / f"target-{validated['target_ordinal']:02d}.json"
        if _path_entry_exists(destination) or len(self._target_receipts) + 1 != validated["target_ordinal"]:
            _fail("target_receipt_already_written")
        self._atomic_write(destination, validated)
        self._target_receipts.append(validated)

    def write_cohort(self, receipt: Mapping[str, Any]) -> None:
        if _path_entry_exists(COHORT_RECEIPT_PATH) or _path_entry_exists(LEDGER_PATH):
            _fail("cohort_receipt_already_written")
        validated = validate_cohort_receipt(receipt)
        if len(self._target_receipts) != HEADLINE_TARGET_COUNT or [
            item["receipt_sha256"] for item in self._target_receipts
        ] != [item["receipt_sha256"] for item in validated["target_receipts"]]:
            _fail("cohort_receipt_target_linkage_invalid")
        self._atomic_write(COHORT_RECEIPT_PATH, validated)
        ledger = build_ledger(validated)
        self._atomic_write(LEDGER_PATH, ledger)


DIAGNOSTIC_RECEIPT_PATH = STATE_DIRECTORY / "diagnostic-receipt.json"
DIAGNOSTIC_STATE_PATH = STATE_DIRECTORY / "diagnostic-state.json"
DIAGNOSTIC_LOCK_PATH = STATE_DIRECTORY / "diagnostic.lock"
DIAGNOSTIC_RECEIPT_FIELDS = frozenset(
    {
        "schema_version", "phase_id", "stage", "receipt_sha256", "authorization_sha256",
        "approval_binding_sha256", "execution_status", "terminal_category", "logical_call_count",
        "provider_call_count", "http_attempt_count", "reserved_input_tokens", "reserved_output_tokens",
        "reserved_microcny", "credential_file_opened", "credential_validated", "usage_known",
        "input_tokens_used", "output_tokens_used", "estimated_microcny", "http_status_class",
        "provider_response_sha256", "assistant_content_sha256", "terminal_match", "raw_retained",
        "redaction_applied", "retry_count",
    }
)

DIAGNOSTIC_TERMINAL_CATEGORIES = frozenset(
    {
        "provider_terminal_match",
        "provider_terminal_mismatch",
        "credential_validation_failed",
        "provider_transport_failure",
        "provider_response_too_large",
        "redirect_refused",
        "http_status_failure",
        "provider_response_invalid_json",
        "provider_response_schema_invalid",
        "provider_usage_schema_invalid",
        "provider_usage_cap_exceeded",
        "usage_unknown",
        "internal_failure",
        "quarantined",
    }
)


def validate_diagnostic_receipt(value: Any) -> dict[str, Any]:
    receipt = _expect_mapping(value, "invalid_diagnostic_receipt")
    _expect_exact_keys(receipt, DIAGNOSTIC_RECEIPT_FIELDS, "invalid_diagnostic_receipt_keys")
    if (
        receipt["schema_version"] != "phase11c-gateb-protocol-diagnostic-receipt/v1"
        or receipt["phase_id"] != PHASE_ID
        or receipt["stage"] != "DIAGNOSTIC"
        or receipt["execution_status"] not in {"completed", "failed", "inconclusive", "quarantined"}
        or receipt["terminal_category"] not in DIAGNOSTIC_TERMINAL_CATEGORIES
    ):
        _fail("diagnostic_receipt_identity_mismatch")
    for field in ("receipt_sha256", "authorization_sha256", "approval_binding_sha256"):
        _expect_sha256(receipt[field], f"invalid_diagnostic_receipt_{field}", allow_zero=False)
    for field in ("provider_response_sha256", "assistant_content_sha256"):
        _expect_sha256(receipt[field], f"invalid_diagnostic_receipt_{field}")
    for field in ("credential_file_opened", "credential_validated", "usage_known", "terminal_match", "raw_retained", "redaction_applied"):
        _expect_bool(receipt[field], f"invalid_diagnostic_receipt_{field}")
    for field in (
        "logical_call_count", "provider_call_count", "http_attempt_count", "reserved_input_tokens",
        "reserved_output_tokens", "reserved_microcny", "input_tokens_used", "output_tokens_used",
        "estimated_microcny", "retry_count",
    ):
        _expect_nonnegative_int(receipt[field], f"invalid_diagnostic_receipt_{field}")
    if (
        receipt["logical_call_count"] != receipt["provider_call_count"]
        or receipt["provider_call_count"] != receipt["http_attempt_count"]
        or receipt["http_attempt_count"] > 1
        or receipt["reserved_input_tokens"] != MAX_INPUT_TOKENS_PER_REQUEST
        or receipt["reserved_output_tokens"] != MAX_OUTPUT_TOKENS_PER_REQUEST
        or receipt["reserved_microcny"] != DIAGNOSTIC_BUDGET_MICROCNY
        or receipt["raw_retained"] is not False
        or receipt["redaction_applied"] is not True
        or receipt["retry_count"] != 0
        or receipt["http_status_class"] not in {"none", "2xx", "3xx", "4xx", "5xx", "other"}
    ):
        _fail("diagnostic_receipt_accounting_or_redaction_invalid")
    if receipt["credential_validated"] is True and receipt["credential_file_opened"] is not True:
        _fail("diagnostic_receipt_credential_order_invalid")
    if receipt["usage_known"] is False:
        if receipt["input_tokens_used"] != 0 or receipt["output_tokens_used"] != 0:
            _fail("diagnostic_receipt_unknown_usage_invalid")
        expected_estimate = DIAGNOSTIC_BUDGET_MICROCNY if receipt["http_attempt_count"] == 1 else 0
        if receipt["estimated_microcny"] != expected_estimate:
            _fail("diagnostic_receipt_unknown_usage_estimate_invalid")
    elif receipt["estimated_microcny"] != worst_case_microcny(
        input_tokens=receipt["input_tokens_used"], output_tokens=receipt["output_tokens_used"]
    ):
        _fail("diagnostic_receipt_estimate_invalid")
    if receipt["http_attempt_count"] == 0 and not (
        receipt["http_status_class"] == "none"
        and receipt["provider_response_sha256"] == ZERO_SHA256
        and receipt["assistant_content_sha256"] == ZERO_SHA256
        and receipt["terminal_match"] is False
    ):
        _fail("diagnostic_receipt_zero_attempt_invariant_failed")
    _validate_seal(receipt, "receipt_sha256", "diagnostic_receipt_sha256_mismatch")
    return receipt


def validate_completed_diagnostic_receipt(value: Any) -> dict[str, Any]:
    """Validate the completed same-image proof eligible for headline binding."""

    receipt = validate_diagnostic_receipt(value)
    if not (
        receipt["execution_status"] == "completed"
        and receipt["terminal_category"] == "provider_terminal_match"
        and receipt["logical_call_count"] == 1
        and receipt["provider_call_count"] == 1
        and receipt["http_attempt_count"] == 1
        and receipt["usage_known"] is True
        and receipt["http_status_class"] == "2xx"
        and receipt["terminal_match"] is True
        and receipt["raw_retained"] is False
        and receipt["redaction_applied"] is True
        and receipt["retry_count"] == 0
        and receipt["provider_response_sha256"] != ZERO_SHA256
        and receipt["assistant_content_sha256"] != ZERO_SHA256
        and receipt["input_tokens_used"] <= MAX_INPUT_TOKENS_PER_REQUEST
        and receipt["output_tokens_used"] <= MAX_OUTPUT_TOKENS_PER_REQUEST
        and receipt["estimated_microcny"] <= DIAGNOSTIC_BUDGET_MICROCNY
    ):
        _fail("diagnostic_receipt_not_eligible")
    return receipt


class DiagnosticStateStore(Protocol):
    def begin(self, authorization_sha: str, binding_sha: str) -> None: ...

    def transition(self, **changes: Any) -> None: ...

    def write_receipt(self, receipt: Mapping[str, Any]) -> None: ...


def _new_diagnostic_state(authorization_sha: str, binding_sha: str) -> dict[str, Any]:
    return _seal(
        {
            "schema_version": "phase11c-gateb-protocol-diagnostic-state/v1",
            "phase_id": PHASE_ID,
            "state_sha256": "",
            "authorization_sha256": authorization_sha,
            "approval_binding_sha256": binding_sha,
            "execution_status": "approval_consumed",
            "budget_reserved": False,
            "credential_file_opened": False,
            "credential_validated": False,
            "http_attempt_count": 0,
        },
        "state_sha256",
    )


def _validate_diagnostic_state(value: Any) -> dict[str, Any]:
    state_value = _expect_mapping(value, "invalid_diagnostic_state")
    expected = {
        "schema_version", "phase_id", "state_sha256", "authorization_sha256", "approval_binding_sha256",
        "execution_status", "budget_reserved", "credential_file_opened", "credential_validated", "http_attempt_count",
    }
    if set(state_value) != expected:
        _fail("invalid_diagnostic_state_keys")
    if state_value["schema_version"] != "phase11c-gateb-protocol-diagnostic-state/v1" or state_value["phase_id"] != PHASE_ID:
        _fail("diagnostic_state_identity_mismatch")
    if state_value["execution_status"] not in {"approval_consumed", "budget_reserved", "credential_opened", "credential_validated", "http_attempted", "terminal"}:
        _fail("diagnostic_state_status_invalid")
    for field in ("authorization_sha256", "approval_binding_sha256"):
        _expect_sha256(state_value[field], f"invalid_diagnostic_state_{field}", allow_zero=False)
    for field in ("budget_reserved", "credential_file_opened", "credential_validated"):
        _expect_bool(state_value[field], f"invalid_diagnostic_state_{field}")
    attempts = _expect_nonnegative_int(state_value["http_attempt_count"], "invalid_diagnostic_state_attempt_count")
    status = state_value["execution_status"]
    if status in {"budget_reserved", "credential_opened", "credential_validated", "http_attempted"} and state_value["budget_reserved"] is not True:
        _fail("diagnostic_state_budget_order_invalid")
    if status in {"credential_opened", "credential_validated", "http_attempted"} and state_value["credential_file_opened"] is not True:
        _fail("diagnostic_state_credential_open_order_invalid")
    if status in {"credential_validated", "http_attempted"} and state_value["credential_validated"] is not True:
        _fail("diagnostic_state_credential_validation_order_invalid")
    if attempts > 1 or (status == "http_attempted" and attempts != 1):
        _fail("diagnostic_state_attempt_invalid")
    _validate_seal(state_value, "state_sha256", "diagnostic_state_sha256_mismatch")
    return state_value


def _transition_diagnostic_state(current: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    previous = _validate_diagnostic_state(current)
    if set(changes) - set(previous) or "state_sha256" in changes:
        _fail("diagnostic_state_transition_keys_invalid")
    updated = dict(previous)
    updated.update(changes)
    order = {
        "approval_consumed": 0,
        "budget_reserved": 1,
        "credential_opened": 2,
        "credential_validated": 3,
        "http_attempted": 4,
        "terminal": 5,
    }
    if order[updated["execution_status"]] < order[previous["execution_status"]]:
        _fail("diagnostic_state_transition_rollback")
    for field in ("budget_reserved", "credential_file_opened", "credential_validated"):
        if previous[field] is True and updated[field] is not True:
            _fail("diagnostic_state_transition_rollback")
    if updated["http_attempt_count"] < previous["http_attempt_count"]:
        _fail("diagnostic_state_transition_rollback")
    updated["state_sha256"] = ""
    return _validate_diagnostic_state(_seal(updated, "state_sha256"))


class InMemoryDiagnosticStateStore:
    def __init__(self) -> None:
        self.state: dict[str, Any] | None = None
        self.receipt: dict[str, Any] | None = None
        self.events: list[str] = []

    def begin(self, authorization_sha: str, binding_sha: str) -> None:
        if self.state is not None:
            _fail("diagnostic_already_consumed")
        self.state = _new_diagnostic_state(authorization_sha, binding_sha)
        self.events.append("approval_consumed")

    def transition(self, **changes: Any) -> None:
        if self.state is None:
            _fail("diagnostic_state_not_started")
        self.state = _transition_diagnostic_state(self.state, **changes)
        self.events.append(self.state["execution_status"])

    def write_receipt(self, receipt: Mapping[str, Any]) -> None:
        if self.receipt is not None:
            _fail("diagnostic_receipt_already_written")
        self.receipt = validate_diagnostic_receipt(receipt)
        self.events.append("receipt_written")


class FileDiagnosticStateStore:
    def __init__(self) -> None:
        self.state: dict[str, Any] | None = None
        self._lock_descriptor: int | None = None

    def __enter__(self) -> FileDiagnosticStateStore:
        _require_linux("diagnostic_state_store_platform_unsupported")
        try:
            STATE_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
            _assert_absolute_no_symlinks(STATE_DIRECTORY, "diagnostic_state_directory_symlink_denied")
            metadata = os.lstat(STATE_DIRECTORY)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0:
                _fail("diagnostic_state_directory_metadata_denied")
            os.chmod(STATE_DIRECTORY, 0o700)
            self._lock_descriptor = os.open(DIAGNOSTIC_LOCK_PATH, os.O_RDWR | os.O_CREAT | _O_CLOEXEC | _O_NOFOLLOW, 0o600)
            if _FCHMOD is None:
                _fail("diagnostic_state_store_platform_unsupported")
            _FCHMOD(self._lock_descriptor, 0o600)
            fcntl.flock(self._lock_descriptor, fcntl.LOCK_EX)
            if _path_entry_exists(DIAGNOSTIC_STATE_PATH) or _path_entry_exists(DIAGNOSTIC_RECEIPT_PATH):
                _fail("diagnostic_quarantined")
            return self
        except HeadlineCohortError:
            self._close()
            raise
        except OSError as exc:
            self._close()
            raise HeadlineCohortError("diagnostic_state_persistence_failure") from exc

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._close()

    def _close(self) -> None:
        if self._lock_descriptor is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_descriptor)
                self._lock_descriptor = None

    def _write(self, destination: Path, value: Mapping[str, Any]) -> None:
        if self._lock_descriptor is None:
            _fail("diagnostic_state_lock_not_held")
        temporary = STATE_DIRECTORY / f".{destination.name}.{os.getpid()}.tmp"
        payload = canonical_json(value) + b"\n"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC | _O_NOFOLLOW, 0o600)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    _fail("diagnostic_state_persistence_failure")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, destination)
            directory_descriptor = os.open(STATE_DIRECTORY, os.O_RDONLY | _O_CLOEXEC)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except HeadlineCohortError:
            raise
        except OSError as exc:
            raise HeadlineCohortError("diagnostic_state_persistence_failure") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

    def begin(self, authorization_sha: str, binding_sha: str) -> None:
        self.state = _new_diagnostic_state(authorization_sha, binding_sha)
        self._write(DIAGNOSTIC_STATE_PATH, self.state)

    def transition(self, **changes: Any) -> None:
        if self.state is None:
            _fail("diagnostic_state_not_started")
        self.state = _transition_diagnostic_state(self.state, **changes)
        self._write(DIAGNOSTIC_STATE_PATH, self.state)

    def write_receipt(self, receipt: Mapping[str, Any]) -> None:
        if _path_entry_exists(DIAGNOSTIC_RECEIPT_PATH):
            _fail("diagnostic_receipt_already_written")
        self._write(DIAGNOSTIC_RECEIPT_PATH, validate_diagnostic_receipt(receipt))


def _failure_category(code: str) -> str:
    if code.startswith("credential_"):
        return "credential_validation_failed"
    if code == "provider_transport_failure":
        return code
    if code == "provider_response_too_large":
        return code
    if code.startswith("provider_response"):
        return "provider_response_schema_invalid"
    if code.startswith("provider_usage"):
        return "provider_usage_schema_invalid"
    return "internal_failure"


def _not_run_target(authorization: Mapping[str, Any], binding_sha: str, ordinal: int) -> dict[str, Any]:
    return _target_receipt(
        authorization=authorization,
        binding_sha=binding_sha,
        ordinal=ordinal,
        target=HEADLINE_TARGETS[ordinal - 1],
        execution_status="not_run_gate_blocked",
        terminal_category="not_run_gate_blocked",
        estimated_microcny=0,
    )


def _response_pair_sha256(first: str, second: str = ZERO_SHA256) -> str:
    return sha256_bytes(canonical_json({"probe": first, "submit": second}))


def _tool_pair_sha256(first: str, second: str = ZERO_SHA256) -> str:
    return sha256_bytes(canonical_json({"probe": first, "submit": second}))


def _attempt_target(
    *,
    authorization: Mapping[str, Any],
    binding_sha: str,
    ordinal: int,
    api_key: str,
    store: CohortStateStore,
    transport: ProviderTransport,
) -> dict[str, Any]:
    target = HEADLINE_TARGETS[ordinal - 1]
    _validate_reconstructed_target(target, ordinal)
    # Mark the specific target durably before the first request can be attempted.
    store.transition(execution_status="running", current_target_ordinal=ordinal)

    def mark_attempt() -> None:
        state = store.state
        if state is None:
            _fail("state_not_started")
        next_count = state["http_attempt_count"] + 1
        store.transition(
            execution_status="running",
            logical_call_count=next_count,
            provider_call_count=next_count,
            http_attempt_count=next_count,
        )

    # The first request is durably counted before any network I/O.
    mark_attempt()
    try:
        first = transport.dispatch(api_key, request_body_for(target))
    except HeadlineCohortError as exc:
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category=_failure_category(str(exc)), logical_call_count=1,
            provider_call_count=1, http_attempt_count=1,
        )
    except Exception:
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category="internal_failure", logical_call_count=1,
            provider_call_count=1, http_attempt_count=1,
        )
    if (
        not isinstance(first, HttpResult)
        or isinstance(first.status_code, bool)
        or not isinstance(first.status_code, int)
        or not isinstance(first.body, bytes)
    ):
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category="internal_failure", logical_call_count=1,
            provider_call_count=1, http_attempt_count=1,
        )
    first_sha = sha256_bytes(first.body)
    first_class = _status_class(first.status_code)
    if len(first.body) > MAX_PROVIDER_RESPONSE_BYTES:
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category="provider_response_too_large", logical_call_count=1,
            provider_call_count=1, http_attempt_count=1, http_status_class=first_class, provider_response_sha256=_response_pair_sha256(first_sha),
        )
    if first_class == "3xx":
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category="redirect_refused", logical_call_count=1,
            provider_call_count=1, http_attempt_count=1, http_status_class=first_class, provider_response_sha256=_response_pair_sha256(first_sha),
        )
    if first_class != "2xx":
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category="http_status_failure", logical_call_count=1,
            provider_call_count=1, http_attempt_count=1, http_status_class=first_class, provider_response_sha256=_response_pair_sha256(first_sha),
        )
    try:
        probe = parse_tool_response(first.body, target, expected_name="probe_canary", expected_outcome="probe")
    except HeadlineCohortError as exc:
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category=_failure_category(str(exc)), logical_call_count=1,
            provider_call_count=1, http_attempt_count=1, http_status_class=first_class, provider_response_sha256=_response_pair_sha256(first_sha),
        )
    if not probe.usage_known:
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="inconclusive", terminal_category="usage_unknown", finish_reason_category=probe.finish_reason_category,
            response_shape_category=probe.response_shape_category, tool_call_present=probe.tool_call_present,
            logical_call_count=1, provider_call_count=1, http_attempt_count=1, http_status_class=first_class,
            provider_response_sha256=_response_pair_sha256(first_sha), tool_call_sha256=_tool_pair_sha256(probe.tool_call_sha256),
        )
    if probe.input_tokens_used > MAX_INPUT_TOKENS_PER_REQUEST or probe.output_tokens_used > MAX_OUTPUT_TOKENS_PER_REQUEST:
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category="provider_usage_cap_exceeded", finish_reason_category=probe.finish_reason_category,
            response_shape_category=probe.response_shape_category, tool_call_present=probe.tool_call_present,
            logical_call_count=1, provider_call_count=1, http_attempt_count=1, usage_known=True,
            input_tokens_used=probe.input_tokens_used, output_tokens_used=probe.output_tokens_used,
            estimated_microcny=worst_case_microcny(input_tokens=probe.input_tokens_used, output_tokens=probe.output_tokens_used),
            http_status_class=first_class, provider_response_sha256=_response_pair_sha256(first_sha), tool_call_sha256=_tool_pair_sha256(probe.tool_call_sha256),
        )
    if not (probe.finish_reason_category == "tool_calls" and probe.response_shape_category == "tool_call" and probe.valid_tool_call):
        if probe.response_shape_category == "empty":
            category = "empty"
        elif probe.response_shape_category == "text_only":
            category = "text_only"
        elif probe.finish_reason_category != "tool_calls":
            category = "finish_reason_invalid"
        elif probe.response_shape_category == "tool_call":
            category = "tool_target_mismatch"
        else:
            category = probe.response_shape_category
        status = "inconclusive" if category in {"empty", "text_only"} else "failed"
        if category == "text_only":
            category = "text_only_response"
        if category == "empty":
            category = "empty_response"
        if category not in TARGET_TERMINAL_CATEGORIES:
            category = "malformed_tool_call"
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status=status, terminal_category=category, finish_reason_category=probe.finish_reason_category,
            response_shape_category=probe.response_shape_category, tool_call_present=probe.tool_call_present,
            logical_call_count=1, provider_call_count=1, http_attempt_count=1, usage_known=True,
            input_tokens_used=probe.input_tokens_used, output_tokens_used=probe.output_tokens_used,
            estimated_microcny=worst_case_microcny(input_tokens=probe.input_tokens_used, output_tokens=probe.output_tokens_used),
            http_status_class=first_class, provider_response_sha256=_response_pair_sha256(first_sha), tool_call_sha256=_tool_pair_sha256(probe.tool_call_sha256),
        )

    # Only a validated probe may cause the second and final request for this target.
    mark_attempt()
    try:
        second = transport.dispatch(api_key, continuation_body_for(target, probe.tool_call_id))
    except HeadlineCohortError as exc:
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category=_failure_category(str(exc)), logical_call_count=2,
            provider_call_count=2, http_attempt_count=2, http_status_class=first_class,
            provider_response_sha256=_response_pair_sha256(first_sha), tool_call_sha256=_tool_pair_sha256(probe.tool_call_sha256),
        )
    except Exception:
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category="internal_failure", logical_call_count=2,
            provider_call_count=2, http_attempt_count=2, http_status_class=first_class,
            provider_response_sha256=_response_pair_sha256(first_sha), tool_call_sha256=_tool_pair_sha256(probe.tool_call_sha256),
        )
    if (
        not isinstance(second, HttpResult)
        or isinstance(second.status_code, bool)
        or not isinstance(second.status_code, int)
        or not isinstance(second.body, bytes)
    ):
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category="internal_failure", logical_call_count=2,
            provider_call_count=2, http_attempt_count=2,
        )
    second_sha = sha256_bytes(second.body)
    second_class = _status_class(second.status_code)
    pair_sha = _response_pair_sha256(first_sha, second_sha)
    if len(second.body) > MAX_PROVIDER_RESPONSE_BYTES:
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category="provider_response_too_large", logical_call_count=2,
            provider_call_count=2, http_attempt_count=2, http_status_class=second_class, provider_response_sha256=pair_sha,
            tool_call_sha256=_tool_pair_sha256(probe.tool_call_sha256),
        )
    if second_class == "3xx" or second_class != "2xx":
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category="redirect_refused" if second_class == "3xx" else "http_status_failure",
            logical_call_count=2, provider_call_count=2, http_attempt_count=2, http_status_class=second_class,
            provider_response_sha256=pair_sha, tool_call_sha256=_tool_pair_sha256(probe.tool_call_sha256),
        )
    try:
        submit = parse_tool_response(second.body, target, expected_name="submit_canary", expected_outcome="submit")
    except HeadlineCohortError as exc:
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category=_failure_category(str(exc)), logical_call_count=2,
            provider_call_count=2, http_attempt_count=2, http_status_class=second_class, provider_response_sha256=pair_sha,
            tool_call_sha256=_tool_pair_sha256(probe.tool_call_sha256),
        )
    tool_pair = _tool_pair_sha256(probe.tool_call_sha256, submit.tool_call_sha256)
    input_tokens = probe.input_tokens_used + submit.input_tokens_used
    output_tokens = probe.output_tokens_used + submit.output_tokens_used
    if not submit.usage_known:
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="inconclusive", terminal_category="usage_unknown", finish_reason_category=submit.finish_reason_category,
            response_shape_category=submit.response_shape_category, tool_call_present=submit.tool_call_present,
            logical_call_count=2, provider_call_count=2, http_attempt_count=2, http_status_class=second_class,
            provider_response_sha256=pair_sha, tool_call_sha256=tool_pair,
        )
    if (
        submit.input_tokens_used > MAX_INPUT_TOKENS_PER_REQUEST
        or submit.output_tokens_used > MAX_OUTPUT_TOKENS_PER_REQUEST
        or input_tokens > REQUESTS_PER_TARGET * MAX_INPUT_TOKENS_PER_REQUEST
        or output_tokens > REQUESTS_PER_TARGET * MAX_OUTPUT_TOKENS_PER_REQUEST
    ):
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="failed", terminal_category="provider_usage_cap_exceeded", finish_reason_category=submit.finish_reason_category,
            response_shape_category=submit.response_shape_category, tool_call_present=submit.tool_call_present,
            submit_attempt_count=submit.submit_attempt_count, logical_call_count=2, provider_call_count=2,
            http_attempt_count=2, usage_known=True, input_tokens_used=input_tokens, output_tokens_used=output_tokens,
            estimated_microcny=worst_case_microcny(input_tokens=input_tokens, output_tokens=output_tokens),
            http_status_class=second_class, provider_response_sha256=pair_sha, tool_call_sha256=tool_pair,
        )
    if not (submit.finish_reason_category == "tool_calls" and submit.response_shape_category == "tool_call" and submit.valid_tool_call):
        category = "finish_reason_invalid" if submit.finish_reason_category != "tool_calls" else "tool_target_mismatch"
        if submit.response_shape_category == "empty":
            category = "empty_response"
        elif submit.response_shape_category == "text_only":
            category = "text_only_response"
        elif submit.response_shape_category == "malformed_tool_call":
            category = "malformed_tool_call"
        return _target_receipt(
            authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
            execution_status="inconclusive" if category in {"empty_response", "text_only_response"} else "failed",
            terminal_category=category, finish_reason_category=submit.finish_reason_category,
            response_shape_category=submit.response_shape_category, tool_call_present=submit.tool_call_present,
            submit_attempt_count=submit.submit_attempt_count, logical_call_count=2, provider_call_count=2,
            http_attempt_count=2, usage_known=True, input_tokens_used=input_tokens, output_tokens_used=output_tokens,
            estimated_microcny=worst_case_microcny(input_tokens=input_tokens, output_tokens=output_tokens),
            http_status_class=second_class, provider_response_sha256=pair_sha, tool_call_sha256=tool_pair,
        )
    return _target_receipt(
        authorization=authorization, binding_sha=binding_sha, ordinal=ordinal, target=target,
        execution_status="completed", terminal_category="provider_tool_submit", finish_reason_category=submit.finish_reason_category,
        response_shape_category=submit.response_shape_category, tool_call_present=True, submit_attempt_count=1,
        logical_call_count=2, provider_call_count=2, http_attempt_count=2, usage_known=True,
        input_tokens_used=input_tokens, output_tokens_used=output_tokens,
        estimated_microcny=worst_case_microcny(input_tokens=input_tokens, output_tokens=output_tokens),
        http_status_class=second_class, provider_response_sha256=pair_sha, tool_call_sha256=tool_pair,
    )


def validate_headline_diagnostic_lineage(
    authorization: Mapping[str, Any],
    diagnostic_authorization: Mapping[str, Any],
    diagnostic_receipt: Mapping[str, Any],
    *,
    now_utc: datetime,
) -> dict[str, Any]:
    """Prove that a headline authorization follows the same-image DIAGNOSTIC."""

    diagnostic_authorization_validated = validate_diagnostic_authorization(
        diagnostic_authorization,
        now_utc=now_utc,
        require_active_window=False,
        allow_expired_window=True,
    )
    diagnostic = validate_completed_diagnostic_receipt(diagnostic_receipt)
    if (
        diagnostic["receipt_sha256"] != authorization["diagnostic_receipt_sha256"]
        or diagnostic["authorization_sha256"] != authorization["diagnostic_authorization_sha256"]
        or diagnostic["approval_binding_sha256"] != authorization["diagnostic_approval_binding_sha256"]
    ):
        _fail("diagnostic_receipt_binding_mismatch")
    if any(diagnostic_authorization_validated[field] != authorization[field] for field in SAME_IMAGE_FREEZE_FIELDS):
        _fail("diagnostic_freeze_binding_mismatch")
    if diagnostic_authorization_validated["authorization_sha256"] != diagnostic["authorization_sha256"]:
        _fail("diagnostic_authorization_receipt_mismatch")
    if diagnostic_approval_binding_sha256(diagnostic_authorization_validated) != diagnostic["approval_binding_sha256"]:
        _fail("diagnostic_approval_receipt_mismatch")
    return diagnostic


def execute_headline_cohort(
    authorization: Mapping[str, Any],
    approval_text: str,
    diagnostic_authorization: Mapping[str, Any],
    diagnostic_receipt: Mapping[str, Any],
    *,
    store: CohortStateStore,
    credential_reader: CredentialReader,
    transport: ProviderTransport,
    now_utc: datetime | None = None,
    executable_source_digest: str | None = None,
) -> dict[str, Any]:
    validated = validate_authorization(
        authorization,
        executable_source_digest=executable_source_digest or source_sha256(),
        now_utc=now_utc or datetime.now(timezone.utc),
        require_active_window=True,
    )
    validate_headline_diagnostic_lineage(
        validated,
        diagnostic_authorization,
        diagnostic_receipt,
        now_utc=now_utc or datetime.now(timezone.utc),
    )
    binding_sha = approval_binding_sha256(validated)
    validate_approval_text(approval_text, binding_sha)
    store.begin(validated["authorization_sha256"], binding_sha)
    store.transition(
        execution_status="budget_reserved",
        budget_reserved=True,
        reserved_input_tokens=HEADLINE_REQUEST_COUNT * MAX_INPUT_TOKENS_PER_REQUEST,
        reserved_output_tokens=HEADLINE_REQUEST_COUNT * MAX_OUTPUT_TOKENS_PER_REQUEST,
        reserved_microcny=HEADLINE_BUDGET_MICROCNY,
    )

    def record_open() -> None:
        state = store.state
        if state is None or state["credential_file_opened"]:
            _fail("credential_open_callback_invalid")
        store.transition(execution_status="credential_opened", credential_file_opened=True)

    def finish_before_target(category: str) -> dict[str, Any]:
        receipts: list[dict[str, Any]] = []
        first = _target_receipt(
            authorization=validated,
            binding_sha=binding_sha,
            ordinal=1,
            target=HEADLINE_TARGETS[0],
            execution_status="failed",
            terminal_category=category,
            estimated_microcny=0,
        )
        store.write_target(first)
        receipts.append(first)
        for blocked_ordinal in range(2, HEADLINE_TARGET_COUNT + 1):
            blocked = _not_run_target(validated, binding_sha, blocked_ordinal)
            store.write_target(blocked)
            receipts.append(blocked)
        store.transition(
            execution_status="terminal",
            next_target_ordinal=HEADLINE_TARGET_COUNT + 1,
            current_target_ordinal=0,
        )
        cohort = build_cohort_receipt(
            authorization=validated,
            binding_sha=binding_sha,
            target_receipts=receipts,
            execution_status="failed",
            stopped_after_ordinal=1,
        )
        store.write_cohort(cohort)
        return cohort

    try:
        api_key = validate_live_credential(
            credential_reader.read(validated["credential_fingerprint_sha256"], record_open)
        )
        state = store.state
        if state is None or state["credential_file_opened"] is not True:
            _fail("credential_open_callback_missing")
        store.transition(execution_status="credential_validated", credential_validated=True)
    except HeadlineCohortError as exc:
        return finish_before_target(_failure_category(str(exc)))
    except Exception:
        return finish_before_target("internal_failure")

    receipts: list[dict[str, Any]] = []
    for ordinal in range(1, HEADLINE_TARGET_COUNT + 1):
        receipt = _attempt_target(
            authorization=validated, binding_sha=binding_sha, ordinal=ordinal, api_key=api_key, store=store, transport=transport
        )
        store.write_target(receipt)
        receipts.append(receipt)
        if receipt["execution_status"] != "completed":
            for blocked_ordinal in range(ordinal + 1, HEADLINE_TARGET_COUNT + 1):
                blocked = _not_run_target(validated, binding_sha, blocked_ordinal)
                store.write_target(blocked)
                receipts.append(blocked)
            store.transition(
                execution_status="terminal",
                next_target_ordinal=HEADLINE_TARGET_COUNT + 1,
                current_target_ordinal=0,
            )
            cohort = build_cohort_receipt(
                authorization=validated,
                binding_sha=binding_sha,
                target_receipts=receipts,
                execution_status=receipt["execution_status"],
                stopped_after_ordinal=ordinal,
            )
            store.write_cohort(cohort)
            return cohort
        store.transition(next_target_ordinal=ordinal + 1)
    store.transition(execution_status="terminal", current_target_ordinal=0)
    cohort = build_cohort_receipt(
        authorization=validated,
        binding_sha=binding_sha,
        target_receipts=receipts,
        execution_status="completed",
        stopped_after_ordinal=HEADLINE_TARGET_COUNT,
    )
    store.write_cohort(cohort)
    return cohort


def _diagnostic_receipt(
    *,
    authorization: Mapping[str, Any],
    binding_sha: str,
    execution_status: str,
    terminal_category: str,
    logical_call_count: int,
    provider_call_count: int,
    http_attempt_count: int,
    usage_known: bool,
    http_status_class: str,
    terminal_match: bool,
    credential_file_opened: bool,
    credential_validated: bool,
    input_tokens_used: int = 0,
    output_tokens_used: int = 0,
    estimated_microcny: int | None = None,
    provider_response_sha256: str = ZERO_SHA256,
    assistant_content_sha256: str = ZERO_SHA256,
) -> dict[str, Any]:
    if execution_status not in {"completed", "failed", "inconclusive", "quarantined"}:
        _fail("diagnostic_receipt_execution_status_invalid")
    if terminal_category not in DIAGNOSTIC_TERMINAL_CATEGORIES:
        _fail("diagnostic_receipt_terminal_category_invalid")
    if estimated_microcny is None:
        if usage_known:
            estimated_microcny = worst_case_microcny(
                input_tokens=input_tokens_used, output_tokens=output_tokens_used
            )
        else:
            estimated_microcny = DIAGNOSTIC_BUDGET_MICROCNY if http_attempt_count else 0
    return validate_diagnostic_receipt(_seal(
        {
            "schema_version": "phase11c-gateb-protocol-diagnostic-receipt/v1",
            "phase_id": PHASE_ID,
            "stage": "DIAGNOSTIC",
            "receipt_sha256": "",
            "authorization_sha256": authorization["authorization_sha256"],
            "approval_binding_sha256": binding_sha,
            "execution_status": execution_status,
            "terminal_category": terminal_category,
            "logical_call_count": logical_call_count,
            "provider_call_count": provider_call_count,
            "http_attempt_count": http_attempt_count,
            "reserved_input_tokens": MAX_INPUT_TOKENS_PER_REQUEST,
            "reserved_output_tokens": MAX_OUTPUT_TOKENS_PER_REQUEST,
            "reserved_microcny": DIAGNOSTIC_BUDGET_MICROCNY,
            "credential_file_opened": credential_file_opened,
            "credential_validated": credential_validated,
            "usage_known": usage_known,
            "input_tokens_used": input_tokens_used,
            "output_tokens_used": output_tokens_used,
            "estimated_microcny": estimated_microcny,
            "http_status_class": http_status_class,
            "provider_response_sha256": provider_response_sha256,
            "assistant_content_sha256": assistant_content_sha256,
            "terminal_match": terminal_match,
            "raw_retained": False,
            "redaction_applied": True,
            "retry_count": 0,
        },
        "receipt_sha256",
    ))


def _parse_diagnostic_response(body: bytes) -> tuple[bool, bool, int, int, str]:
    payload = _provider_json_loads(body)
    if not isinstance(payload, Mapping):
        _fail("provider_response_schema_invalid")
    usage_known, input_tokens, output_tokens = _usage_from_payload(payload)
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        _fail("provider_response_schema_invalid")
    message = choices[0].get("message")
    if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
        _fail("provider_response_schema_invalid")
    assistant_content = message["content"].strip()
    return (
        assistant_content == DIAGNOSTIC_TERMINAL_TOKEN,
        usage_known,
        input_tokens,
        output_tokens,
        sha256_bytes(assistant_content.encode("utf-8")),
    )


def execute_diagnostic(
    authorization: Mapping[str, Any],
    approval_text: str,
    *,
    store: DiagnosticStateStore,
    credential_reader: CredentialReader,
    transport: ProviderTransport,
    now_utc: datetime | None = None,
    executable_source_digest: str | None = None,
) -> dict[str, Any]:
    now = now_utc or datetime.now(timezone.utc)
    validated = validate_diagnostic_authorization(
        authorization,
        executable_source_digest=executable_source_digest or source_sha256(),
        now_utc=now,
        require_active_window=True,
    )
    binding_sha = diagnostic_approval_binding_sha256(validated)
    validate_diagnostic_approval_text(approval_text, binding_sha)
    store.begin(validated["authorization_sha256"], binding_sha)
    store.transition(execution_status="budget_reserved", budget_reserved=True)
    credential_file_opened = False
    credential_validated = False

    def finish(
        execution_status: str,
        terminal_category: str,
        *,
        logical_call_count: int,
        provider_call_count: int,
        http_attempt_count: int,
        usage_known: bool,
        http_status_class: str,
        terminal_match: bool,
        input_tokens_used: int = 0,
        output_tokens_used: int = 0,
        provider_response_sha256: str = ZERO_SHA256,
        assistant_content_sha256: str = ZERO_SHA256,
    ) -> dict[str, Any]:
        store.transition(execution_status="terminal")
        receipt = _diagnostic_receipt(
            authorization=validated,
            binding_sha=binding_sha,
            execution_status=execution_status,
            terminal_category=terminal_category,
            logical_call_count=logical_call_count,
            provider_call_count=provider_call_count,
            http_attempt_count=http_attempt_count,
            usage_known=usage_known,
            http_status_class=http_status_class,
            terminal_match=terminal_match,
            credential_file_opened=credential_file_opened,
            credential_validated=credential_validated,
            input_tokens_used=input_tokens_used,
            output_tokens_used=output_tokens_used,
            provider_response_sha256=provider_response_sha256,
            assistant_content_sha256=assistant_content_sha256,
        )
        store.write_receipt(receipt)
        return receipt

    def record_open() -> None:
        nonlocal credential_file_opened
        if credential_file_opened:
            _fail("credential_open_callback_invalid")
        credential_file_opened = True
        store.transition(execution_status="credential_opened", credential_file_opened=True)

    try:
        api_key = validate_live_credential(
            credential_reader.read(validated["credential_fingerprint_sha256"], record_open)
        )
        credential_validated = True
        store.transition(execution_status="credential_validated", credential_validated=True)
    except HeadlineCohortError as exc:
        return finish(
            "failed", _failure_category(str(exc)), logical_call_count=0, provider_call_count=0,
            http_attempt_count=0, usage_known=False, http_status_class="none", terminal_match=False,
        )
    except Exception:
        return finish(
            "failed", "internal_failure", logical_call_count=0, provider_call_count=0,
            http_attempt_count=0, usage_known=False, http_status_class="none", terminal_match=False,
        )

    store.transition(execution_status="http_attempted", http_attempt_count=1)
    try:
        result = transport.dispatch(api_key, DIAGNOSTIC_REQUEST_BODY)
    except HeadlineCohortError as exc:
        return finish(
            "failed", _failure_category(str(exc)), logical_call_count=1, provider_call_count=1,
            http_attempt_count=1, usage_known=False, http_status_class="none", terminal_match=False,
        )
    except Exception:
        return finish(
            "failed", "internal_failure", logical_call_count=1, provider_call_count=1,
            http_attempt_count=1, usage_known=False, http_status_class="none", terminal_match=False,
        )
    if (
        not isinstance(result, HttpResult)
        or isinstance(result.status_code, bool)
        or not isinstance(result.status_code, int)
        or not isinstance(result.body, bytes)
    ):
        return finish(
            "failed", "internal_failure", logical_call_count=1, provider_call_count=1,
            http_attempt_count=1, usage_known=False, http_status_class="other", terminal_match=False,
        )
    status_class = _status_class(result.status_code)
    response_sha = sha256_bytes(result.body)
    if len(result.body) > MAX_PROVIDER_RESPONSE_BYTES or status_class != "2xx":
        return finish(
            "failed",
            "provider_response_too_large" if len(result.body) > MAX_PROVIDER_RESPONSE_BYTES else ("redirect_refused" if status_class == "3xx" else "http_status_failure"),
            logical_call_count=1, provider_call_count=1, http_attempt_count=1, usage_known=False,
            http_status_class=status_class, terminal_match=False, provider_response_sha256=response_sha,
        )
    try:
        terminal_match, usage_known, input_tokens, output_tokens, content_sha = _parse_diagnostic_response(result.body)
    except HeadlineCohortError as exc:
        return finish(
            "failed", _failure_category(str(exc)), logical_call_count=1, provider_call_count=1,
            http_attempt_count=1, usage_known=False, http_status_class=status_class, terminal_match=False,
            provider_response_sha256=response_sha,
        )
    if not usage_known:
        status, category = "inconclusive", "usage_unknown"
    elif input_tokens > MAX_INPUT_TOKENS_PER_REQUEST or output_tokens > MAX_OUTPUT_TOKENS_PER_REQUEST:
        status, category = "failed", "provider_usage_cap_exceeded"
    elif terminal_match:
        status, category = "completed", "provider_terminal_match"
    else:
        status, category = "inconclusive", "provider_terminal_mismatch"
    return finish(
        status, category,
        logical_call_count=1, provider_call_count=1, http_attempt_count=1, usage_known=usage_known,
        http_status_class=status_class, terminal_match=terminal_match, input_tokens_used=input_tokens,
        output_tokens_used=output_tokens, provider_response_sha256=response_sha,
        assistant_content_sha256=content_sha,
    )


def _read_fixed_control_file(path: Path, *, maximum_bytes: int, exact_mode: int) -> bytes:
    _require_linux("control_file_platform_unsupported")
    _assert_absolute_no_symlinks(path, "control_file_symlink_or_path_denied")
    try:
        descriptor = os.open(path, os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW)
    except OSError as exc:
        raise HeadlineCohortError("control_file_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or stat.S_IMODE(before.st_mode) != exact_mode
            or before.st_nlink != 1
            or not 1 <= before.st_size <= maximum_bytes
        ):
            _fail("control_file_metadata_denied")
        payload = bytearray()
        while len(payload) <= maximum_bytes:
            chunk = os.read(descriptor, min(4096, maximum_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > maximum_bytes:
            _fail("control_file_size_invalid")
        after = os.fstat(descriptor)
        path_after = os.lstat(path)
        _assert_absolute_no_symlinks(path, "control_file_symlink_or_path_denied")
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size) or (
            after.st_dev, after.st_ino, after.st_size
        ) != (path_after.st_dev, path_after.st_ino, path_after.st_size):
            _fail("control_file_identity_changed")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _read_fixed_state_file(path: Path) -> bytes:
    return _read_fixed_control_file(path, maximum_bytes=MAX_CONTROL_FILE_BYTES, exact_mode=0o600)


def read_fixed_diagnostic_authorization(
    *, require_active_window: bool, allow_expired_window: bool = False
) -> dict[str, Any]:
    return validate_diagnostic_authorization(
        strict_json_loads(_read_fixed_control_file(DIAGNOSTIC_AUTHORIZATION_PATH, maximum_bytes=MAX_CONTROL_FILE_BYTES, exact_mode=0o400)),
        require_active_window=require_active_window,
        allow_expired_window=allow_expired_window,
    )


def read_fixed_headline_authorization(*, require_active_window: bool) -> dict[str, Any]:
    return validate_authorization(
        strict_json_loads(_read_fixed_control_file(AUTHORIZATION_PATH, maximum_bytes=MAX_CONTROL_FILE_BYTES, exact_mode=0o400)),
        require_active_window=require_active_window,
    )


def _read_ascii_approval(path: Path, code: str) -> str:
    try:
        return _read_fixed_control_file(path, maximum_bytes=256, exact_mode=0o400).decode("ascii")
    except UnicodeDecodeError as exc:
        raise HeadlineCohortError(code) from exc


def run_diagnostic_from_fixed_files() -> dict[str, Any]:
    authorization = read_fixed_diagnostic_authorization(require_active_window=True)
    approval = _read_ascii_approval(DIAGNOSTIC_APPROVAL_PATH, "diagnostic_approval_encoding_invalid")
    with FileDiagnosticStateStore() as store:
        return execute_diagnostic(
            authorization,
            approval,
            store=store,
            credential_reader=FixedCredentialReader(),
            transport=FixedHTTPSProviderTransport(),
        )


def run_headline_from_fixed_files() -> dict[str, Any]:
    authorization = read_fixed_headline_authorization(require_active_window=True)
    approval = _read_ascii_approval(APPROVAL_PATH, "headline_approval_encoding_invalid")
    diagnostic_authorization = read_fixed_diagnostic_authorization(
        require_active_window=False, allow_expired_window=True
    )
    diagnostic = validate_completed_diagnostic_receipt(strict_json_loads(_read_fixed_state_file(DIAGNOSTIC_RECEIPT_PATH)))
    with FileCohortStateStore() as store:
        return execute_headline_cohort(
            authorization,
            approval,
            diagnostic_authorization,
            diagnostic,
            store=store,
            credential_reader=FixedCredentialReader(),
            transport=FixedHTTPSProviderTransport(),
        )


def _read_stdin_bounded() -> bytes:
    payload = sys.stdin.buffer.read(MAX_CONTROL_FILE_BYTES + 1)
    if len(payload) > MAX_CONTROL_FILE_BYTES:
        _fail("stdin_document_too_large")
    return payload


def _print_safe_json(value: Mapping[str, Any]) -> None:
    print(canonical_json(value).decode("ascii"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("print-template")
    commands.add_parser("print-diagnostic-template")
    commands.add_parser("seal-diagnostic-authorization")
    commands.add_parser("print-diagnostic-approval-binding")
    commands.add_parser("run-diagnostic")
    commands.add_parser("print-headline-template")
    commands.add_parser("seal-headline-authorization")
    commands.add_parser("print-headline-approval-binding")
    commands.add_parser("print-diagnostic-receipt")
    commands.add_parser("run-headline")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "print-template":
            _print_safe_json(
                {
                    "diagnostic_authorization_template": build_diagnostic_authorization_template(),
                    "headline_authorization_template": build_authorization_template(),
                    "phase_id": PHASE_ID,
                }
            )
            return 0
        if args.command == "print-diagnostic-template":
            _print_safe_json(build_diagnostic_authorization_template())
            return 0
        if args.command == "seal-diagnostic-authorization":
            _print_safe_json(seal_diagnostic_authorization(strict_json_loads(_read_stdin_bounded())))
            return 0
        if args.command == "print-diagnostic-approval-binding":
            authorization = read_fixed_diagnostic_authorization(require_active_window=False)
            binding = diagnostic_approval_binding_sha256(authorization)
            print(expected_diagnostic_approval_text(binding))
            return 0
        if args.command == "run-diagnostic":
            receipt = run_diagnostic_from_fixed_files()
            _print_safe_json(receipt)
            return 0 if receipt["execution_status"] == "completed" else 3
        if args.command == "print-headline-template":
            _print_safe_json(build_authorization_template())
            return 0
        if args.command == "seal-headline-authorization":
            _print_safe_json(seal_authorization(strict_json_loads(_read_stdin_bounded())))
            return 0
        if args.command == "print-headline-approval-binding":
            authorization = read_fixed_headline_authorization(require_active_window=False)
            diagnostic_authorization = read_fixed_diagnostic_authorization(
                require_active_window=False, allow_expired_window=True
            )
            diagnostic_receipt = validate_completed_diagnostic_receipt(
                strict_json_loads(_read_fixed_state_file(DIAGNOSTIC_RECEIPT_PATH))
            )
            validate_headline_diagnostic_lineage(
                authorization,
                diagnostic_authorization,
                diagnostic_receipt,
                now_utc=datetime.now(timezone.utc),
            )
            binding = approval_binding_sha256(authorization)
            print(expected_approval_text(binding))
            return 0
        if args.command == "print-diagnostic-receipt":
            _print_safe_json(validate_completed_diagnostic_receipt(strict_json_loads(_read_fixed_state_file(DIAGNOSTIC_RECEIPT_PATH))))
            return 0
        receipt = run_headline_from_fixed_files()
        _print_safe_json(receipt)
        return 0 if receipt["execution_status"] == "completed" else 3
    except HeadlineCohortError as exc:
        _print_safe_json({"error_code": str(exc)})
        return 2
    except Exception:
        _print_safe_json({"error_code": "internal_failure"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
