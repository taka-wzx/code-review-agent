"""Phase 11C Gate B local-credential DIAGNOSTIC preparation, offline only.

This module deliberately has no provider transport, cloud client, credential reader,
dotenv loader, subprocess, or production-runtime import.  It only creates and
validates non-secret draft bindings and a fail-closed receipt.  A later task needs a
separate exact authorization before it may read a credential or make a request.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


PHASE_ID = "phase11c-gateb-local-diagnostic-v1"
AUTHORIZATION_SCHEMA_VERSION = "phase11c-gateb-local-authorization/v1"
RECEIPT_SCHEMA_VERSION = "phase11c-gateb-local-receipt/v1"
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
        "credential_delivery_mode",
        "credential_path_retention",
        "credential_bytes_read",
        "aggregate_budget_ceiling_micro_cny",
        "diagnostic_budget_micro_cny",
        "owner_account",
        "owner_reconfirmed",
        "provider_policy_evidence_sha256",
        "provider_tariff_sha256",
        "source_tree_sha256",
        "image_sha256",
        "deployment_sha256",
        "runtime_identity_sha256",
        "cohort_sha256",
        "gate_b_preflight_sha256",
        "authorization_window_start_utc",
        "authorization_window_end_utc",
        "kill_switch_bound",
        "credential_file_path_supplied",
        "credential_metadata_validated",
        "diagnostic_human_approved",
        "live_execution_enabled",
    }
)

RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "phase_id",
        "stage",
        "receipt_sha256",
        "authorization_sha256",
        "execution_status",
        "provider_call_count",
        "http_attempt_count",
        "credential_file_opened",
        "credential_bytes_retained",
        "live_execution_enabled",
        "blocking_reason_codes",
    }
)

BLOCKING_REASON_CODES = (
    "provider_policy_evidence_missing",
    "provider_tariff_missing",
    "source_tree_binding_missing",
    "image_binding_missing",
    "deployment_binding_missing",
    "runtime_identity_binding_missing",
    "cohort_binding_missing",
    "gate_b_preflight_missing",
    "authorization_window_missing",
    "owner_reconfirmation_missing",
    "kill_switch_binding_missing",
    "credential_file_path_not_supplied",
    "credential_metadata_not_validated",
    "diagnostic_human_approval_missing",
)

FORBIDDEN_KEYS = frozenset(
    {
        "credential_file_path",
        "credential_value",
        "credential_bytes",
        "provider_response",
        "prompt",
        "messages",
        "tool_arguments",
        "tool_results",
        "exception_message",
        "host_path",
    }
)
FORBIDDEN_VALUE_FRAGMENTS = ("sk-", "bearer ", "api_key=", "-----begin")


class GateBPreparationError(ValueError):
    """Stable, safe error for a rejected Gate B preparation input."""


def _fail(code: str) -> None:
    raise GateBPreparationError(code)


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
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    _fail("unsupported_canonical_type")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
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
        return json.loads(value, object_pairs_hook=reject_duplicate_keys)
    except GateBPreparationError:
        raise
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise GateBPreparationError("invalid_json") from exc


def source_sha256() -> str:
    """Hash only this fixed module source; no caller can supply another path."""

    path = Path(__file__)
    normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
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


def _expect_non_negative_int(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
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
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS or contains_forbidden_content(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(contains_forbidden_content(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return any(fragment in lowered for fragment in FORBIDDEN_VALUE_FRAGMENTS)
    return False


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
            "credential_delivery_mode": "local_one_time_secure_file",
            "credential_path_retention": False,
            "credential_bytes_read": False,
            "aggregate_budget_ceiling_micro_cny": MAX_AGGREGATE_BUDGET_MICRO_CNY,
            "diagnostic_budget_micro_cny": 0,
            "owner_account": OWNER_ACCOUNT,
            "owner_reconfirmed": False,
            "provider_policy_evidence_sha256": PENDING_FREEZE,
            "provider_tariff_sha256": PENDING_FREEZE,
            "source_tree_sha256": PENDING_FREEZE,
            "image_sha256": PENDING_FREEZE,
            "deployment_sha256": PENDING_FREEZE,
            "runtime_identity_sha256": PENDING_FREEZE,
            "cohort_sha256": PENDING_FREEZE,
            "gate_b_preflight_sha256": PENDING_FREEZE,
            "authorization_window_start_utc": PENDING_FREEZE,
            "authorization_window_end_utc": PENDING_FREEZE,
            "kill_switch_bound": False,
            "credential_file_path_supplied": False,
            "credential_metadata_validated": False,
            "diagnostic_human_approved": False,
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
    executable_source = _expect_sha256(
        authorization["executable_source_sha256"], "invalid_executable_source_sha256"
    )
    if executable_source_digest is not None and executable_source != executable_source_digest:
        _fail("executable_source_sha256_drift")
    if authorization["credential_delivery_mode"] != "local_one_time_secure_file":
        _fail("credential_delivery_mode_mismatch")
    for field in ("credential_path_retention", "credential_bytes_read", "live_execution_enabled"):
        if authorization[field] is not False:
            _fail("unsafe_credential_or_execution_flag")
    if authorization["aggregate_budget_ceiling_micro_cny"] != MAX_AGGREGATE_BUDGET_MICRO_CNY:
        _fail("aggregate_budget_ceiling_mismatch")
    if authorization["diagnostic_budget_micro_cny"] != 0:
        _fail("diagnostic_budget_not_zero")
    if not isinstance(authorization["owner_account"], str) or not _OWNER.fullmatch(
        authorization["owner_account"]
    ):
        _fail("invalid_owner_account")
    if authorization["owner_account"] != OWNER_ACCOUNT:
        _fail("owner_account_mismatch")
    for field in (
        "provider_policy_evidence_sha256",
        "provider_tariff_sha256",
        "source_tree_sha256",
        "image_sha256",
        "deployment_sha256",
        "runtime_identity_sha256",
        "cohort_sha256",
        "gate_b_preflight_sha256",
        "authorization_window_start_utc",
        "authorization_window_end_utc",
    ):
        if authorization[field] != PENDING_FREEZE:
            _fail("draft_binding_not_pending")
    for field in (
        "owner_reconfirmed",
        "kill_switch_bound",
        "credential_file_path_supplied",
        "credential_metadata_validated",
        "diagnostic_human_approved",
    ):
        if authorization[field] is not False:
            _fail("draft_approval_flag_not_false")
    _validate_seal(authorization, "authorization_sha256", "authorization_sha256_mismatch")
    if contains_forbidden_content(authorization):
        _fail("authorization_contains_forbidden_content")
    return authorization


@dataclass(frozen=True)
class LocalCredentialFileMetadata:
    """Synthetic metadata only; no field contains a path or credential value."""

    exists: bool
    regular_file: bool
    symlink: bool
    ancestor_symlink: bool
    absolute_repository_external: bool
    size_bytes: int
    platform: str
    posix_mode: int | None
    owner_uid: int | None
    link_count: int
    windows_acl_proven: bool


def validate_local_credential_file_metadata(metadata: LocalCredentialFileMetadata) -> None:
    """Validate injected metadata without touching a file system object."""

    if metadata.platform == "windows":
        _fail("credential_platform_unsupported")
    if metadata.platform != "posix":
        _fail("credential_file_platform_unsupported")
    if (
        metadata.exists is not True
        or metadata.regular_file is not True
        or metadata.symlink is not False
        or metadata.ancestor_symlink is not False
    ):
        _fail("credential_file_denied")
    if (
        isinstance(metadata.size_bytes, bool)
        or not isinstance(metadata.size_bytes, int)
        or not 1 <= metadata.size_bytes <= 4096
    ):
        _fail("credential_file_size_invalid")
    if metadata.absolute_repository_external is not True:
        _fail("credential_file_location_denied")
    if metadata.owner_uid != 0:
        _fail("credential_file_owner_denied")
    if metadata.posix_mode != 0o600:
        _fail("credential_file_permissions_denied")
    if (
        isinstance(metadata.link_count, bool)
        or not isinstance(metadata.link_count, int)
        or metadata.link_count != 1
    ):
        _fail("credential_file_link_count_denied")


def build_blocked_receipt(authorization: Mapping[str, Any]) -> dict[str, Any]:
    validated = validate_draft_authorization(authorization)
    return _seal(
        {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "stage": "DIAGNOSTIC",
            "receipt_sha256": "",
            "authorization_sha256": validated["authorization_sha256"],
            "execution_status": "not_run_gate_blocked",
            "provider_call_count": 0,
            "http_attempt_count": 0,
            "credential_file_opened": False,
            "credential_bytes_retained": False,
            "live_execution_enabled": False,
            "blocking_reason_codes": list(BLOCKING_REASON_CODES),
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
    for field in ("provider_call_count", "http_attempt_count"):
        if _expect_non_negative_int(receipt[field], "invalid_receipt_count") != 0:
            _fail("receipt_call_count_not_zero")
    for field in ("credential_file_opened", "credential_bytes_retained", "live_execution_enabled"):
        if receipt[field] is not False:
            _fail("receipt_unsafe_flag")
    if tuple(receipt["blocking_reason_codes"]) != BLOCKING_REASON_CODES:
        _fail("receipt_blocking_reason_codes_mismatch")
    _validate_seal(receipt, "receipt_sha256", "receipt_sha256_mismatch")
    if contains_forbidden_content(receipt):
        _fail("receipt_contains_forbidden_content")
    return receipt


def run_diagnostic_gate_blocked() -> dict[str, Any]:
    """Return the only allowed outcome for this preparation-only executable."""

    authorization = build_draft_authorization(executable_source_sha256=source_sha256())
    return validate_blocked_receipt(build_blocked_receipt(authorization))


def _print_safe_json(value: Mapping[str, Any]) -> None:
    print(canonical_json(value).decode("utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("print-draft")
    commands.add_parser("validate-draft")
    commands.add_parser("run-diagnostic")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    authorization = build_draft_authorization(executable_source_sha256=source_sha256())
    if args.command == "print-draft":
        _print_safe_json(validate_draft_authorization(authorization, executable_source_digest=source_sha256()))
        return 0
    if args.command == "validate-draft":
        _print_safe_json(build_blocked_receipt(validate_draft_authorization(authorization, executable_source_digest=source_sha256())))
        return 0
    _print_safe_json(run_diagnostic_gate_blocked())
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
