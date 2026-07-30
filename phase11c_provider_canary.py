"""Phase 11C deterministic synthetic provider-protocol canary, Gate A only.

This module deliberately has no HTTP client, provider SDK, credential reader, dotenv
loader, subprocess, or production-runtime import.  It generates and validates only
safe, canonical Gate A artifacts and executes an in-memory response-shape matrix.
The ``run-real`` command is intentionally a zero-I/O fail-closed receipt; a future
Gate B needs a separately approved implementation and complete frozen bindings.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence


PHASE_ID = "phase11c-provider-canary-v1"
AUTHORIZATION_SCHEMA_VERSION = "phase11c-provider-canary-authorization/v1"
RUNTIME_SCHEMA_VERSION = "phase11c-provider-canary-runtime/v1"
COHORT_SCHEMA_VERSION = "phase11c-provider-canary-cohort/v1"
TARIFF_SCHEMA_VERSION = "phase11c-provider-canary-tariff/v1"
PREFLIGHT_SCHEMA_VERSION = "phase11c-provider-canary-preflight/v1"
BUDGET_SCHEMA_VERSION = "phase11c-provider-canary-budget/v1"

BASELINE_MASTER_SHA = "4af4b2756e8d2de6764d08e17a6e12040e24975e"
BASELINE_CI_RUN = 30_451_250_259
BASELINE_CI_ATTEMPT = 1
BASELINE_CI_CONCLUSION = "success"
PHASE11B_ACCEPTANCE_REPORT_SHA256 = (
    "354398234ee34773f26b1811ece62a5ccc7ed9fd18472adb11e1907bec25c6f7"
)
PHASE11B_AUTHORIZATION_SHA256 = (
    "73c8367ce00ce4ad77798dbd1bcbf0f3995528096b18924f2a198ba290796745"
)
PHASE11B_RUNTIME_CONFIG_SHA256 = (
    "e1a3d3adadc78ab0b11e8d28b60ba05552c503edf7b91661e895d24cb5ea8bdc"
)

PENDING_FREEZE = "PENDING_FREEZE"
PENDING_CURRENT_REVIEW = "PENDING_CURRENT_REVIEW"
PENDING_EXPLICIT_APPROVAL = "PENDING_EXPLICIT_APPROVAL"
PENDING_EXECUTABLE_COMMIT = "PENDING_EXECUTABLE_COMMIT"

PIPELINE_STAGES = frozenset(
    {
        "preflight",
        "authorization",
        "credential",
        "budget_reservation",
        "provider_transport",
        "response_decode",
        "finder",
        "verifier",
        "submit",
        "receipt_reconcile",
        "cleanup",
    }
)
TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "inconclusive",
        "not_run_gate_blocked",
        "quarantined",
    }
)
FAILURE_CODES = frozenset(
    {
        "empty_response",
        "repeated_empty_response",
        "text_only_response",
        "malformed_tool_call",
        "invalid_submit_limit",
        "finder_step_cap",
        "verifier_step_cap",
        "tool_call_loop",
        "finish_reason_length",
        "finish_reason_other",
        "output_token_exhaustion",
        "provider_schema_mismatch",
        "provider_timeout",
        "provider_auth",
        "provider_rate_limit",
        "provider_server_error",
        "provider_connection_error",
        "local_budget_exhausted",
        "local_deadline",
        "credential_revoked",
        "credential_expired",
        "authorization_expired",
        "authorization_mismatch",
        "endpoint_denied",
        "redirect_denied",
        "ambiguous_result",
        "receipt_mismatch",
        "other",
    }
)
FINISH_REASON_CATEGORIES = frozenset(
    {"tool_calls", "stop", "length", "other", "not_observed"}
)
RESPONSE_SHAPE_CATEGORIES = frozenset(
    {
        "tool_call",
        "empty",
        "text_only",
        "malformed_tool_call",
        "schema_mismatch",
        "not_observed",
    }
)
PROVIDER_EXCEPTION_TYPES = frozenset(
    {
        "none",
        "auth",
        "rate_limit",
        "timeout",
        "server_error",
        "connection_error",
        "schema_mismatch",
        "other",
        "unknown",
    }
)

TELEMETRY_KEYS = frozenset(
    {
        "pipeline_stage",
        "stable_failure_code",
        "finish_reason_category",
        "response_shape_category",
        "tool_call_present",
        "submit_attempt_count",
        "empty_response_count",
        "step_count",
        "output_limit_reached",
        "usage_known",
        "provider_exception_type",
        "redaction_applied",
    }
)

PHASE9H_PLANNING_CEILING = {
    "logical_calls": 80,
    "http_attempts": 80,
    "input_tokens": 1_500_000,
    "output_tokens": 163_840,
    "micro_cny": 15_000_000,
}

GATE_A_BLOCKING_REASON_CODES = (
    "provider_policy_unaccepted",
    "tariff_current_review_pending",
    "gate_b_authorization_pending",
    "runtime_image_pending",
    "source_archive_pending",
    "deployment_config_pending",
    "runtime_identity_pending",
    "authorization_window_pending",
    "owners_pending",
    "credential_handoff_pending",
    "kill_switch_pending",
    "diagnostic_human_approval_missing",
    "headline_human_approval_missing",
    "auth004_nonoverlap_metadata_pending",
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "content",
        "credential",
        "credentials",
        "diff",
        "exception",
        "exception_message",
        "message",
        "messages",
        "prompt",
        "response",
        "secret",
        "token",
        "tool_arguments",
        "tool_result",
        "tool_results",
    }
)
_FORBIDDEN_STRING_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"^(?:[A-Za-z]:[\\/]|/opt/|\\\\)"),
)


class CanaryValidationError(ValueError):
    """Stable, content-free validation failure."""


class BudgetExhausted(CanaryValidationError):
    """A pre-call reservation would exceed a frozen hard cap."""


def _fail(code: str) -> None:
    raise CanaryValidationError(code)


def _reject_floats(value: Any) -> None:
    if isinstance(value, float):
        _fail("floating_point_values_forbidden")
    if isinstance(value, Mapping):
        for child in value.values():
            _reject_floats(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_floats(child)


def canonical_json(value: Any) -> bytes:
    """Serialize JSON deterministically and reject non-integer accounting values."""

    _reject_floats(value)
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanaryValidationError("noncanonical_json_value") from exc


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def strict_json_loads(raw: str | bytes) -> Any:
    """Parse JSON while rejecting duplicate keys and non-standard constants."""

    def reject_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                _fail("duplicate_json_key")
            output[key] = value
        return output

    def reject_constant(_: str) -> None:
        _fail("nonfinite_json_constant")

    try:
        return json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except CanaryValidationError:
        raise
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanaryValidationError("invalid_json") from exc


def _expect_mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(code)
    return value


def _expect_exact_keys(value: Mapping[str, Any], expected: Iterable[str], code: str) -> None:
    if set(value) != set(expected):
        _fail(code)


def _expect_sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
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


def _expect_enum(value: Any, allowed: Iterable[str], code: str) -> str:
    if not isinstance(value, str) or value not in set(allowed):
        _fail(code)
    return value


def _canonical_sha256(document: Mapping[str, Any], field: str) -> str:
    if field not in document:
        _fail("missing_canonical_hash")
    candidate = deepcopy(dict(document))
    candidate[field] = ""
    return sha256_value(candidate)


def _seal(document: Mapping[str, Any], field: str) -> dict[str, Any]:
    sealed = deepcopy(dict(document))
    sealed[field] = ""
    sealed[field] = _canonical_sha256(sealed, field)
    return sealed


def _validate_sealed(document: Mapping[str, Any], field: str, code: str) -> None:
    actual = _expect_sha256(document.get(field), code)
    if actual != _canonical_sha256(document, field):
        _fail(code)


def contains_forbidden_content(value: Any) -> bool:
    """Defensive scan for raw data, credentials, and host paths in safe artifacts."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or key.casefold() in _FORBIDDEN_KEYS:
                return True
            if contains_forbidden_content(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(contains_forbidden_content(child) for child in value)
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _FORBIDDEN_STRING_PATTERNS)
    return False


def _validate_safe_document(document: Mapping[str, Any]) -> None:
    _reject_floats(document)
    if contains_forbidden_content(document):
        _fail("forbidden_content_in_safe_artifact")


def validate_phase11b_acceptance_report_bytes(raw: bytes) -> dict[str, str]:
    """Verify only the permitted SHA and terminal status of the supplied report."""

    report_sha256 = sha256_bytes(raw)
    if report_sha256 != PHASE11B_ACCEPTANCE_REPORT_SHA256:
        _fail("phase11b_acceptance_sha256_mismatch")
    report = _expect_mapping(strict_json_loads(raw), "phase11b_acceptance_not_object")
    if report.get("status") != "accepted":
        _fail("phase11b_acceptance_status_mismatch")
    return {"sha256": report_sha256, "status": "accepted"}


def source_sha256(source_path: Path | None = None) -> str:
    """Hash executable source after fixed UTF-8/LF normalization.

    The candidate is intended to bind source semantics rather than the checkout's
    platform-specific line-ending conversion.  The final Gate B source-tree/archive
    hashes remain separately frozen and are deliberately still pending here.
    """

    path = source_path or Path(__file__)
    normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return sha256_bytes(normalized.encode("utf-8"))


def lockfile_sha256(lockfile_path: Path | None = None) -> str:
    """Freeze the exact installed package set by hash, without modifying the lockfile."""

    path = lockfile_path or (Path(__file__).resolve().parent / "requirements.lock")
    return sha256_bytes(path.read_bytes())


def _synthetic_targets() -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    for slot in range(1, 6):
        stable_digest = hashlib.sha256(
            f"{PHASE_ID}:deterministic-synthetic:stable-id:{slot}".encode("ascii")
        ).hexdigest()
        payload_digest = hashlib.sha256(
            f"{PHASE_ID}:deterministic-synthetic:payload:{slot}".encode("ascii")
        ).hexdigest()
        targets.append(
            {
                "stable_id": f"p11c-{stable_digest[:32]}",
                "payload_sha256": payload_digest,
            }
        )
    return targets


def _baseline_record() -> dict[str, Any]:
    return {
        "master_sha": BASELINE_MASTER_SHA,
        "ci_run_id": BASELINE_CI_RUN,
        "ci_attempt": BASELINE_CI_ATTEMPT,
        "ci_conclusion": BASELINE_CI_CONCLUSION,
        "phase11b_acceptance_report_sha256": PHASE11B_ACCEPTANCE_REPORT_SHA256,
        "phase11b_acceptance_status": "accepted",
        "phase11b_authorization_sha256": PHASE11B_AUTHORIZATION_SHA256,
        "phase11b_runtime_config_sha256": PHASE11B_RUNTIME_CONFIG_SHA256,
    }


def build_runtime_config(
    *, executable_source_sha256: str, sdk_package_set_sha256: str | None = None
) -> dict[str, Any]:
    _expect_sha256(executable_source_sha256, "invalid_executable_source_sha256")
    lock_digest = sdk_package_set_sha256 or lockfile_sha256()
    _expect_sha256(lock_digest, "invalid_sdk_package_set_sha256")
    return _seal(
        {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "runtime_config_sha256": "",
            "provider": "glm",
            "request_model_id": "glm-5.2",
            "snapshot_immutability": False,
            "api_surface": "chat.completions.create",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "egress_allowlist": "open.bigmodel.cn:443",
            "tls_certificate_verification": True,
            "redirect_policy": "deny",
            "sdk_retries": 0,
            "transport_retries": 0,
            "concurrency": 1,
            "proxy_policy": "deny_implicit_inheritance",
            "openai_sdk_version": "2.46.0",
            "sdk_package_set_sha256": lock_digest,
            "publisher_mode": "fake_dry_run",
            "real_provider_calls_enabled": False,
            "paid_calls_enabled": False,
            "local_persistence": {
                "rendered_raw_prompt_retention": False,
                "rendered_synthetic_diff_retention": False,
                "raw_provider_response_retention": False,
                "raw_tool_args_results_retention": False,
                "exception_message_retention": False,
                "credential_value_retention": False,
                "deterministic_synthetic_source_manifest_retention": True,
                "safe_hash_enum_boolean_count_receipt_retention": True,
            },
            "executable_code_commit_sha": PENDING_EXECUTABLE_COMMIT,
            "executable_source_sha256": executable_source_sha256,
            "immutable_image_digest": PENDING_FREEZE,
            "source_archive_sha256": PENDING_FREEZE,
            "rendered_deployment_config_sha256": PENDING_FREEZE,
            "aliyun_runtime_identity_sha256": PENDING_FREEZE,
        },
        "runtime_config_sha256",
    )


def build_synthetic_cohort() -> dict[str, Any]:
    return _seal(
        {
            "schema_version": COHORT_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "cohort_manifest_sha256": "",
            "source_kind": "deterministic_synthetic",
            "target_limit": 5,
            "proposed_headline_denominator": 3,
            "exact_headline_denominator": PENDING_FREEZE,
            "diagnostic_in_headline_denominator": False,
            "targets": _synthetic_targets(),
            "auth004_nonoverlap_status": "pending_sanitized_metadata_freeze",
            "auth004_sanitized_metadata_sha256": PENDING_FREEZE,
        },
        "cohort_manifest_sha256",
    )


def build_tariff_manifest() -> dict[str, Any]:
    return _seal(
        {
            "schema_version": TARIFF_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "tariff_manifest_sha256": "",
            "provider": "glm",
            "request_model_id": "glm-5.2",
            "currency": "micro-CNY",
            "integer_accounting_only": True,
            "tariff_review_state": "pending_current_review",
            "effective_date_state": "pending_current_review",
            "input_uncached_tariff_state": "pending_current_review",
            "input_cached_tariff_state": "pending_current_review",
            "output_tariff_state": "pending_current_review",
            "provider_data_use_policy_url": PENDING_CURRENT_REVIEW,
            "provider_retention_policy_url": PENDING_CURRENT_REVIEW,
            "policy_reviewed_at": PENDING_CURRENT_REVIEW,
            "policy_evidence_sha256": PENDING_CURRENT_REVIEW,
            "owner_policy_acceptance": PENDING_EXPLICIT_APPROVAL,
        },
        "tariff_manifest_sha256",
    )


def build_authorization_candidate() -> dict[str, Any]:
    return _seal(
        {
            "schema_version": AUTHORIZATION_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "gate": "gate_b",
            "authorization_id": PENDING_FREEZE,
            "authorization_sha256": "",
            "baseline": _baseline_record(),
            "binding_state": "pending_freeze",
            "real_provider_calls_enabled": False,
            "paid_calls_enabled": False,
            "authorization_window": PENDING_FREEZE,
            "owners": PENDING_FREEZE,
            "gate_b_bindings": {
                "runtime_config_sha256": PENDING_FREEZE,
                "executable_code_commit_sha": PENDING_FREEZE,
                "executable_source_tree_sha256": PENDING_FREEZE,
                "immutable_image_digest": PENDING_FREEZE,
                "source_archive_sha256": PENDING_FREEZE,
                "rendered_deployment_config_sha256": PENDING_FREEZE,
                "synthetic_cohort_manifest_sha256": PENDING_FREEZE,
                "preflight_verdict_sha256": PENDING_FREEZE,
                "provider_tariff_manifest_sha256": PENDING_FREEZE,
                "aliyun_runtime_identity_sha256": PENDING_FREEZE,
            },
            "human_approvals": {
                "diagnostic": PENDING_EXPLICIT_APPROVAL,
                "headline_cohort": PENDING_EXPLICIT_APPROVAL,
            },
        },
        "authorization_sha256",
    )


def build_budget_proposal(
    *, cohort_manifest_sha256: str, tariff_manifest_sha256: str
) -> dict[str, Any]:
    _expect_sha256(cohort_manifest_sha256, "invalid_cohort_manifest_sha256")
    _expect_sha256(tariff_manifest_sha256, "invalid_tariff_manifest_sha256")
    diagnostic = {
        "logical_calls": 2,
        "http_attempts": 2,
        "input_tokens": 16_000,
        "output_tokens": 2_048,
        "micro_cny": 600_000,
    }
    headline = {
        "logical_calls": 15,
        "http_attempts": 15,
        "input_tokens": 120_000,
        "output_tokens": 15_360,
        "micro_cny": 4_500_000,
    }
    total = {
        name: diagnostic[name] + headline[name]
        for name in ("logical_calls", "http_attempts", "input_tokens", "output_tokens", "micro_cny")
    }
    return _seal(
        {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "budget_proposal_sha256": "",
            "cohort_manifest_sha256": cohort_manifest_sha256,
            "tariff_manifest_sha256": tariff_manifest_sha256,
            "diagnostic_sub_cap": diagnostic,
            "headline_hard_cap": headline,
            "phase_hard_cap": total,
            "cached_input_rule": "reserve_uncached_count_once_when_verified",
            "per_request_connect_timeout_seconds": 10,
            "per_request_read_timeout_seconds": 90,
            "per_request_total_timeout_seconds": 100,
            "phase_wall_clock_hard_cap_seconds": 900,
            "max_steps": 6,
            "max_output_tokens_per_request": 1_024,
            "temperature_profile": "deterministic_zero",
        },
        "budget_proposal_sha256",
    )


def _candidate_hashes(candidates: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    return {
        "authorization_sha256": str(candidates["authorization"]["authorization_sha256"]),
        "runtime_config_sha256": str(candidates["runtime_config"]["runtime_config_sha256"]),
        "cohort_manifest_sha256": str(candidates["synthetic_cohort"]["cohort_manifest_sha256"]),
        "tariff_manifest_sha256": str(candidates["tariff"]["tariff_manifest_sha256"]),
        "budget_proposal_sha256": str(candidates["budget_proposal"]["budget_proposal_sha256"]),
        "executable_source_sha256": str(candidates["runtime_config"]["executable_source_sha256"]),
        "sdk_package_set_sha256": str(candidates["runtime_config"]["sdk_package_set_sha256"]),
    }


def build_preflight_verdict(candidates: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    compatibility = run_fake_compatibility_matrix()
    if not all(item["redaction_applied"] for item in compatibility):
        _fail("offline_matrix_redaction_failure")
    return _seal(
        {
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "preflight_verdict_sha256": "",
            "candidate_artifact_hashes": _candidate_hashes(candidates),
            "checks": {
                "phase11b_acceptance_report_match": True,
                "baseline_master_match": True,
                "baseline_ci_match": True,
                "offline_compatibility_matrix_passed": True,
                "every_fixture_has_stable_pipeline_stage": True,
                "mixed_provider_pipeline_failure_code_absent": True,
                "terminal_statuses_split": True,
                "safe_telemetry_complete": True,
                "redaction_applied": True,
                "sdk_retries_zero": True,
                "transport_retries_zero": True,
                "concurrency_one": True,
                "redirect_denied": True,
                "endpoint_tls_policy_frozen": True,
                "budget_reservation_restart_validated": True,
                "raw_content_retention_denied": True,
                "publisher_fake_dry_run": True,
                "candidate_artifact_hashes_consistent": True,
                "auth004_nonoverlap_confirmed": False,
                "provider_policy_accepted": False,
                "credential_file_security_confirmed": False,
                "authorization_window_valid": False,
                "kill_switch_available": False,
                "final_gate_b_bindings_complete": False,
            },
            "canary_allowed": False,
            "real_run_recommended_now": False,
            "blocking_reason_codes": list(GATE_A_BLOCKING_REASON_CODES),
            "execution_status": "not_run",
            "protocol_canary_status": "not_run",
            "redaction_applied": True,
        },
        "preflight_verdict_sha256",
    )


def build_gate_a_candidates(*, executable_source_digest: str | None = None) -> dict[str, dict[str, Any]]:
    """Build deterministic candidate artifacts without reading a credential or network."""

    source_digest = executable_source_digest or source_sha256()
    runtime_config = build_runtime_config(executable_source_sha256=source_digest)
    synthetic_cohort = build_synthetic_cohort()
    tariff = build_tariff_manifest()
    authorization = build_authorization_candidate()
    budget_proposal = build_budget_proposal(
        cohort_manifest_sha256=synthetic_cohort["cohort_manifest_sha256"],
        tariff_manifest_sha256=tariff["tariff_manifest_sha256"],
    )
    candidates: dict[str, dict[str, Any]] = {
        "authorization": authorization,
        "runtime_config": runtime_config,
        "synthetic_cohort": synthetic_cohort,
        "tariff": tariff,
        "budget_proposal": budget_proposal,
    }
    candidates["preflight_verdict"] = build_preflight_verdict(candidates)
    validate_gate_a_candidates(candidates, executable_source_digest=source_digest)
    return candidates


def _validate_baseline(value: Any) -> dict[str, Any]:
    baseline = _expect_mapping(value, "invalid_baseline")
    _expect_exact_keys(
        baseline,
        {
            "master_sha",
            "ci_run_id",
            "ci_attempt",
            "ci_conclusion",
            "phase11b_acceptance_report_sha256",
            "phase11b_acceptance_status",
            "phase11b_authorization_sha256",
            "phase11b_runtime_config_sha256",
        },
        "invalid_baseline_keys",
    )
    if baseline != _baseline_record():
        _fail("baseline_binding_mismatch")
    return baseline


def validate_runtime_config(value: Any) -> dict[str, Any]:
    runtime = _expect_mapping(value, "invalid_runtime_config")
    _expect_exact_keys(
        runtime,
        {
            "schema_version",
            "phase_id",
            "runtime_config_sha256",
            "provider",
            "request_model_id",
            "snapshot_immutability",
            "api_surface",
            "base_url",
            "egress_allowlist",
            "tls_certificate_verification",
            "redirect_policy",
            "sdk_retries",
            "transport_retries",
            "concurrency",
            "proxy_policy",
            "openai_sdk_version",
            "sdk_package_set_sha256",
            "publisher_mode",
            "real_provider_calls_enabled",
            "paid_calls_enabled",
            "local_persistence",
            "executable_code_commit_sha",
            "executable_source_sha256",
            "immutable_image_digest",
            "source_archive_sha256",
            "rendered_deployment_config_sha256",
            "aliyun_runtime_identity_sha256",
        },
        "invalid_runtime_config_keys",
    )
    if runtime["schema_version"] != RUNTIME_SCHEMA_VERSION or runtime["phase_id"] != PHASE_ID:
        _fail("runtime_schema_or_phase_mismatch")
    if runtime["provider"] != "glm" or runtime["request_model_id"] != "glm-5.2":
        _fail("runtime_provider_model_mismatch")
    expected_literals = {
        "api_surface": "chat.completions.create",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "egress_allowlist": "open.bigmodel.cn:443",
        "redirect_policy": "deny",
        "proxy_policy": "deny_implicit_inheritance",
        "openai_sdk_version": "2.46.0",
        "publisher_mode": "fake_dry_run",
        "executable_code_commit_sha": PENDING_EXECUTABLE_COMMIT,
        "immutable_image_digest": PENDING_FREEZE,
        "source_archive_sha256": PENDING_FREEZE,
        "rendered_deployment_config_sha256": PENDING_FREEZE,
        "aliyun_runtime_identity_sha256": PENDING_FREEZE,
    }
    for key, expected in expected_literals.items():
        if runtime[key] != expected:
            _fail("runtime_frozen_literal_mismatch")
    if runtime["snapshot_immutability"] is not False:
        _fail("runtime_snapshot_immutability_mismatch")
    for key in ("tls_certificate_verification",):
        if runtime[key] is not True:
            _fail("runtime_tls_mismatch")
    for key in ("real_provider_calls_enabled", "paid_calls_enabled"):
        if runtime[key] is not False:
            _fail("runtime_gate_opened")
    local_persistence = _expect_mapping(runtime["local_persistence"], "invalid_local_persistence")
    _expect_exact_keys(
        local_persistence,
        {
            "rendered_raw_prompt_retention",
            "rendered_synthetic_diff_retention",
            "raw_provider_response_retention",
            "raw_tool_args_results_retention",
            "exception_message_retention",
            "credential_value_retention",
            "deterministic_synthetic_source_manifest_retention",
            "safe_hash_enum_boolean_count_receipt_retention",
        },
        "invalid_local_persistence_keys",
    )
    for key in (
        "rendered_raw_prompt_retention",
        "rendered_synthetic_diff_retention",
        "raw_provider_response_retention",
        "raw_tool_args_results_retention",
        "exception_message_retention",
        "credential_value_retention",
    ):
        if local_persistence[key] is not False:
            _fail("unsafe_local_retention_enabled")
    for key in (
        "deterministic_synthetic_source_manifest_retention",
        "safe_hash_enum_boolean_count_receipt_retention",
    ):
        if local_persistence[key] is not True:
            _fail("required_safe_retention_disabled")
    for key in ("sdk_retries", "transport_retries"):
        if runtime[key] != 0:
            _fail("runtime_retry_mismatch")
    if runtime["concurrency"] != 1:
        _fail("runtime_concurrency_mismatch")
    _expect_sha256(runtime["executable_source_sha256"], "invalid_executable_source_sha256")
    _expect_sha256(runtime["sdk_package_set_sha256"], "invalid_sdk_package_set_sha256")
    _validate_sealed(runtime, "runtime_config_sha256", "runtime_config_sha256_mismatch")
    _validate_safe_document(runtime)
    return runtime


def validate_synthetic_cohort(value: Any) -> dict[str, Any]:
    cohort = _expect_mapping(value, "invalid_synthetic_cohort")
    _expect_exact_keys(
        cohort,
        {
            "schema_version",
            "phase_id",
            "cohort_manifest_sha256",
            "source_kind",
            "target_limit",
            "proposed_headline_denominator",
            "exact_headline_denominator",
            "diagnostic_in_headline_denominator",
            "targets",
            "auth004_nonoverlap_status",
            "auth004_sanitized_metadata_sha256",
        },
        "invalid_synthetic_cohort_keys",
    )
    if cohort["schema_version"] != COHORT_SCHEMA_VERSION or cohort["phase_id"] != PHASE_ID:
        _fail("cohort_schema_or_phase_mismatch")
    if cohort["source_kind"] != "deterministic_synthetic":
        _fail("cohort_source_kind_mismatch")
    if cohort["target_limit"] != 5 or cohort["proposed_headline_denominator"] != 3:
        _fail("cohort_target_or_denominator_mismatch")
    if cohort["exact_headline_denominator"] != PENDING_FREEZE:
        _fail("cohort_exact_denominator_mismatch")
    if cohort["diagnostic_in_headline_denominator"] is not False:
        _fail("cohort_diagnostic_denominator_mismatch")
    if cohort["auth004_nonoverlap_status"] != "pending_sanitized_metadata_freeze":
        _fail("cohort_nonoverlap_state_mismatch")
    if cohort["auth004_sanitized_metadata_sha256"] != PENDING_FREEZE:
        _fail("cohort_nonoverlap_hash_mismatch")
    targets = cohort["targets"]
    if not isinstance(targets, list) or len(targets) != cohort["target_limit"]:
        _fail("cohort_target_count_mismatch")
    stable_ids: set[str] = set()
    for target in targets:
        item = _expect_mapping(target, "invalid_synthetic_target")
        _expect_exact_keys(item, {"stable_id", "payload_sha256"}, "invalid_synthetic_target_keys")
        stable_id = item["stable_id"]
        if not isinstance(stable_id, str) or not stable_id.startswith("p11c-"):
            _fail("invalid_synthetic_stable_id")
        _expect_sha256(item["payload_sha256"], "invalid_synthetic_payload_sha256")
        stable_ids.add(stable_id)
    if len(stable_ids) != len(targets):
        _fail("duplicate_synthetic_stable_id")
    _validate_sealed(cohort, "cohort_manifest_sha256", "cohort_manifest_sha256_mismatch")
    _validate_safe_document(cohort)
    return cohort


def validate_tariff_manifest(value: Any) -> dict[str, Any]:
    tariff = _expect_mapping(value, "invalid_tariff_manifest")
    _expect_exact_keys(
        tariff,
        {
            "schema_version",
            "phase_id",
            "tariff_manifest_sha256",
            "provider",
            "request_model_id",
            "currency",
            "integer_accounting_only",
            "tariff_review_state",
            "effective_date_state",
            "input_uncached_tariff_state",
            "input_cached_tariff_state",
            "output_tariff_state",
            "provider_data_use_policy_url",
            "provider_retention_policy_url",
            "policy_reviewed_at",
            "policy_evidence_sha256",
            "owner_policy_acceptance",
        },
        "invalid_tariff_manifest_keys",
    )
    if tariff["schema_version"] != TARIFF_SCHEMA_VERSION or tariff["phase_id"] != PHASE_ID:
        _fail("tariff_schema_or_phase_mismatch")
    if tariff["provider"] != "glm" or tariff["request_model_id"] != "glm-5.2":
        _fail("tariff_provider_model_mismatch")
    if tariff["currency"] != "micro-CNY" or tariff["integer_accounting_only"] is not True:
        _fail("tariff_currency_or_integer_mismatch")
    for key in (
        "tariff_review_state",
        "effective_date_state",
        "input_uncached_tariff_state",
        "input_cached_tariff_state",
        "output_tariff_state",
    ):
        if tariff[key] != "pending_current_review":
            _fail("tariff_review_state_mismatch")
    for key in (
        "provider_data_use_policy_url",
        "provider_retention_policy_url",
        "policy_reviewed_at",
        "policy_evidence_sha256",
    ):
        if tariff[key] != PENDING_CURRENT_REVIEW:
            _fail("tariff_policy_pending_state_mismatch")
    if tariff["owner_policy_acceptance"] != PENDING_EXPLICIT_APPROVAL:
        _fail("tariff_owner_policy_acceptance_mismatch")
    _validate_sealed(tariff, "tariff_manifest_sha256", "tariff_manifest_sha256_mismatch")
    _validate_safe_document(tariff)
    return tariff


def validate_authorization_candidate(value: Any) -> dict[str, Any]:
    authorization = _expect_mapping(value, "invalid_authorization_candidate")
    _expect_exact_keys(
        authorization,
        {
            "schema_version",
            "phase_id",
            "gate",
            "authorization_id",
            "authorization_sha256",
            "baseline",
            "binding_state",
            "real_provider_calls_enabled",
            "paid_calls_enabled",
            "authorization_window",
            "owners",
            "gate_b_bindings",
            "human_approvals",
        },
        "invalid_authorization_candidate_keys",
    )
    if authorization["schema_version"] != AUTHORIZATION_SCHEMA_VERSION:
        _fail("authorization_schema_mismatch")
    if authorization["phase_id"] != PHASE_ID or authorization["gate"] != "gate_b":
        _fail("authorization_phase_or_gate_mismatch")
    if authorization["authorization_id"] != PENDING_FREEZE:
        _fail("authorization_id_mismatch")
    _validate_baseline(authorization["baseline"])
    if authorization["binding_state"] != "pending_freeze":
        _fail("authorization_binding_state_mismatch")
    if authorization["real_provider_calls_enabled"] is not False:
        _fail("authorization_real_provider_gate_opened")
    if authorization["paid_calls_enabled"] is not False:
        _fail("authorization_paid_gate_opened")
    if authorization["authorization_window"] != PENDING_FREEZE or authorization["owners"] != PENDING_FREEZE:
        _fail("authorization_pending_binding_mismatch")
    bindings = _expect_mapping(authorization["gate_b_bindings"], "invalid_gate_b_bindings")
    _expect_exact_keys(
        bindings,
        {
            "runtime_config_sha256",
            "executable_code_commit_sha",
            "executable_source_tree_sha256",
            "immutable_image_digest",
            "source_archive_sha256",
            "rendered_deployment_config_sha256",
            "synthetic_cohort_manifest_sha256",
            "preflight_verdict_sha256",
            "provider_tariff_manifest_sha256",
            "aliyun_runtime_identity_sha256",
        },
        "invalid_gate_b_binding_keys",
    )
    if set(bindings.values()) != {PENDING_FREEZE}:
        _fail("authorization_gate_b_binding_state_mismatch")
    approvals = _expect_mapping(authorization["human_approvals"], "invalid_human_approvals")
    _expect_exact_keys(approvals, {"diagnostic", "headline_cohort"}, "invalid_human_approval_keys")
    if set(approvals.values()) != {PENDING_EXPLICIT_APPROVAL}:
        _fail("authorization_human_approval_state_mismatch")
    _validate_sealed(authorization, "authorization_sha256", "authorization_sha256_mismatch")
    _validate_safe_document(authorization)
    return authorization


def _validate_budget_section(value: Any, code: str) -> dict[str, int]:
    section = _expect_mapping(value, code)
    _expect_exact_keys(
        section,
        {"logical_calls", "http_attempts", "input_tokens", "output_tokens", "micro_cny"},
        code,
    )
    return {name: _expect_nonnegative_int(section[name], code) for name in section}


def validate_budget_proposal(value: Any) -> dict[str, Any]:
    budget = _expect_mapping(value, "invalid_budget_proposal")
    _expect_exact_keys(
        budget,
        {
            "schema_version",
            "phase_id",
            "budget_proposal_sha256",
            "cohort_manifest_sha256",
            "tariff_manifest_sha256",
            "diagnostic_sub_cap",
            "headline_hard_cap",
            "phase_hard_cap",
            "cached_input_rule",
            "per_request_connect_timeout_seconds",
            "per_request_read_timeout_seconds",
            "per_request_total_timeout_seconds",
            "phase_wall_clock_hard_cap_seconds",
            "max_steps",
            "max_output_tokens_per_request",
            "temperature_profile",
        },
        "invalid_budget_proposal_keys",
    )
    if budget["schema_version"] != BUDGET_SCHEMA_VERSION or budget["phase_id"] != PHASE_ID:
        _fail("budget_schema_or_phase_mismatch")
    _expect_sha256(budget["cohort_manifest_sha256"], "invalid_budget_cohort_sha256")
    _expect_sha256(budget["tariff_manifest_sha256"], "invalid_budget_tariff_sha256")
    diagnostic = _validate_budget_section(budget["diagnostic_sub_cap"], "invalid_diagnostic_sub_cap")
    headline = _validate_budget_section(budget["headline_hard_cap"], "invalid_headline_hard_cap")
    phase = _validate_budget_section(budget["phase_hard_cap"], "invalid_phase_hard_cap")
    for name, ceiling in PHASE9H_PLANNING_CEILING.items():
        if phase[name] > ceiling:
            _fail("budget_exceeds_phase9h_planning_ceiling")
        if phase[name] != diagnostic[name] + headline[name]:
            _fail("budget_phase_total_mismatch")
    if budget["cached_input_rule"] != "reserve_uncached_count_once_when_verified":
        _fail("cached_input_rule_mismatch")
    if budget["temperature_profile"] != "deterministic_zero":
        _fail("temperature_profile_mismatch")
    for key in (
        "per_request_connect_timeout_seconds",
        "per_request_read_timeout_seconds",
        "per_request_total_timeout_seconds",
        "phase_wall_clock_hard_cap_seconds",
        "max_steps",
        "max_output_tokens_per_request",
    ):
        if _expect_nonnegative_int(budget[key], "invalid_budget_integer") <= 0:
            _fail("nonpositive_budget_limit")
    _validate_sealed(budget, "budget_proposal_sha256", "budget_proposal_sha256_mismatch")
    _validate_safe_document(budget)
    return budget


def validate_preflight_verdict(value: Any) -> dict[str, Any]:
    verdict = _expect_mapping(value, "invalid_preflight_verdict")
    _expect_exact_keys(
        verdict,
        {
            "schema_version",
            "phase_id",
            "preflight_verdict_sha256",
            "candidate_artifact_hashes",
            "checks",
            "canary_allowed",
            "real_run_recommended_now",
            "blocking_reason_codes",
            "execution_status",
            "protocol_canary_status",
            "redaction_applied",
        },
        "invalid_preflight_verdict_keys",
    )
    if verdict["schema_version"] != PREFLIGHT_SCHEMA_VERSION or verdict["phase_id"] != PHASE_ID:
        _fail("preflight_schema_or_phase_mismatch")
    hashes = _expect_mapping(verdict["candidate_artifact_hashes"], "invalid_preflight_hashes")
    _expect_exact_keys(
        hashes,
        {
            "authorization_sha256",
            "runtime_config_sha256",
            "cohort_manifest_sha256",
            "tariff_manifest_sha256",
            "budget_proposal_sha256",
            "executable_source_sha256",
            "sdk_package_set_sha256",
        },
        "invalid_preflight_hash_keys",
    )
    for item in hashes.values():
        _expect_sha256(item, "invalid_preflight_hash")
    checks = _expect_mapping(verdict["checks"], "invalid_preflight_checks")
    required_checks = {
        "phase11b_acceptance_report_match",
        "baseline_master_match",
        "baseline_ci_match",
        "offline_compatibility_matrix_passed",
        "every_fixture_has_stable_pipeline_stage",
        "mixed_provider_pipeline_failure_code_absent",
        "terminal_statuses_split",
        "safe_telemetry_complete",
        "redaction_applied",
        "sdk_retries_zero",
        "transport_retries_zero",
        "concurrency_one",
        "redirect_denied",
        "endpoint_tls_policy_frozen",
        "budget_reservation_restart_validated",
        "raw_content_retention_denied",
        "publisher_fake_dry_run",
        "candidate_artifact_hashes_consistent",
        "auth004_nonoverlap_confirmed",
        "provider_policy_accepted",
        "credential_file_security_confirmed",
        "authorization_window_valid",
        "kill_switch_available",
        "final_gate_b_bindings_complete",
    }
    _expect_exact_keys(checks, required_checks, "invalid_preflight_check_keys")
    for key, item in checks.items():
        _expect_bool(item, "invalid_preflight_check")
        if key in {
            "auth004_nonoverlap_confirmed",
            "provider_policy_accepted",
            "credential_file_security_confirmed",
            "authorization_window_valid",
            "kill_switch_available",
            "final_gate_b_bindings_complete",
        } and item is not False:
            _fail("gate_b_check_opened")
    if verdict["canary_allowed"] is not False or verdict["real_run_recommended_now"] is not False:
        _fail("gate_a_preflight_opened_real_canary")
    if verdict["execution_status"] != "not_run" or verdict["protocol_canary_status"] != "not_run":
        _fail("preflight_status_mismatch")
    if verdict["redaction_applied"] is not True:
        _fail("preflight_redaction_mismatch")
    blockers = verdict["blocking_reason_codes"]
    if not isinstance(blockers, list) or tuple(blockers) != GATE_A_BLOCKING_REASON_CODES:
        _fail("preflight_blocking_codes_mismatch")
    _validate_sealed(verdict, "preflight_verdict_sha256", "preflight_verdict_sha256_mismatch")
    _validate_safe_document(verdict)
    return verdict


def validate_gate_a_candidates(
    candidates: Mapping[str, Any], *, executable_source_digest: str | None = None
) -> dict[str, Any]:
    """Validate all candidate artifacts and their non-circular Gate A bindings."""

    _expect_exact_keys(
        candidates,
        {
            "authorization",
            "runtime_config",
            "synthetic_cohort",
            "tariff",
            "budget_proposal",
            "preflight_verdict",
        },
        "invalid_gate_a_candidate_set",
    )
    authorization = validate_authorization_candidate(candidates["authorization"])
    runtime = validate_runtime_config(candidates["runtime_config"])
    cohort = validate_synthetic_cohort(candidates["synthetic_cohort"])
    tariff = validate_tariff_manifest(candidates["tariff"])
    budget = validate_budget_proposal(candidates["budget_proposal"])
    verdict = validate_preflight_verdict(candidates["preflight_verdict"])
    if budget["cohort_manifest_sha256"] != cohort["cohort_manifest_sha256"]:
        _fail("budget_cohort_binding_mismatch")
    if budget["tariff_manifest_sha256"] != tariff["tariff_manifest_sha256"]:
        _fail("budget_tariff_binding_mismatch")
    expected_hashes = {
        "authorization_sha256": authorization["authorization_sha256"],
        "runtime_config_sha256": runtime["runtime_config_sha256"],
        "cohort_manifest_sha256": cohort["cohort_manifest_sha256"],
        "tariff_manifest_sha256": tariff["tariff_manifest_sha256"],
        "budget_proposal_sha256": budget["budget_proposal_sha256"],
        "executable_source_sha256": runtime["executable_source_sha256"],
        "sdk_package_set_sha256": runtime["sdk_package_set_sha256"],
    }
    if verdict["candidate_artifact_hashes"] != expected_hashes:
        _fail("preflight_candidate_hash_binding_mismatch")
    if executable_source_digest is not None and runtime["executable_source_sha256"] != executable_source_digest:
        _fail("executable_source_sha256_drift")
    if runtime["sdk_package_set_sha256"] != lockfile_sha256():
        _fail("sdk_package_set_sha256_drift")
    return {
        "candidate_artifact_hashes": expected_hashes,
        "canary_allowed": False,
        "real_run_recommended_now": False,
        "blocking_reason_codes": list(GATE_A_BLOCKING_REASON_CODES),
    }


def validate_safe_telemetry(value: Any) -> dict[str, Any]:
    telemetry = _expect_mapping(value, "invalid_safe_telemetry")
    _expect_exact_keys(telemetry, TELEMETRY_KEYS, "invalid_safe_telemetry_keys")
    _expect_enum(telemetry["pipeline_stage"], PIPELINE_STAGES, "invalid_pipeline_stage")
    failure = telemetry["stable_failure_code"]
    if failure is not None:
        _expect_enum(failure, FAILURE_CODES, "invalid_stable_failure_code")
    _expect_enum(
        telemetry["finish_reason_category"],
        FINISH_REASON_CATEGORIES,
        "invalid_finish_reason_category",
    )
    _expect_enum(
        telemetry["response_shape_category"],
        RESPONSE_SHAPE_CATEGORIES,
        "invalid_response_shape_category",
    )
    _expect_enum(
        telemetry["provider_exception_type"],
        PROVIDER_EXCEPTION_TYPES,
        "invalid_provider_exception_type",
    )
    for key in ("submit_attempt_count", "empty_response_count", "step_count"):
        _expect_nonnegative_int(telemetry[key], "invalid_safe_telemetry_count")
    for key in ("tool_call_present", "output_limit_reached", "usage_known", "redaction_applied"):
        _expect_bool(telemetry[key], "invalid_safe_telemetry_boolean")
    if telemetry["redaction_applied"] is not True:
        _fail("safe_telemetry_redaction_false")
    _validate_safe_document(telemetry)
    return telemetry


def build_safe_telemetry(
    *,
    pipeline_stage: str,
    stable_failure_code: str | None,
    finish_reason_category: str,
    response_shape_category: str,
    tool_call_present: bool,
    submit_attempt_count: int,
    empty_response_count: int,
    step_count: int,
    output_limit_reached: bool,
    usage_known: bool,
    provider_exception_type: str,
) -> dict[str, Any]:
    return validate_safe_telemetry(
        {
            "pipeline_stage": pipeline_stage,
            "stable_failure_code": stable_failure_code,
            "finish_reason_category": finish_reason_category,
            "response_shape_category": response_shape_category,
            "tool_call_present": tool_call_present,
            "submit_attempt_count": submit_attempt_count,
            "empty_response_count": empty_response_count,
            "step_count": step_count,
            "output_limit_reached": output_limit_reached,
            "usage_known": usage_known,
            "provider_exception_type": provider_exception_type,
            "redaction_applied": True,
        }
    )


def validate_terminal_receipt(value: Any) -> dict[str, Any]:
    """Validate a redacted terminal-state projection without retaining raw data."""

    receipt = _expect_mapping(value, "invalid_terminal_receipt")
    _expect_exact_keys(
        receipt,
        {"terminal_status", "telemetry", "redaction_applied"},
        "invalid_terminal_receipt_keys",
    )
    status = _expect_enum(receipt["terminal_status"], TERMINAL_STATUSES, "invalid_terminal_status")
    telemetry = validate_safe_telemetry(receipt["telemetry"])
    if receipt["redaction_applied"] is not True or telemetry["redaction_applied"] is not True:
        _fail("terminal_receipt_redaction_false")
    if status == "completed" and telemetry["stable_failure_code"] is not None:
        _fail("completed_with_failure_code")
    if status != "completed" and telemetry["stable_failure_code"] is None:
        _fail("noncompleted_without_failure_code")
    return receipt


def _fake_telemetry_for_scenario(scenario: str) -> tuple[str, dict[str, Any]]:
    """Return only fixed categories; no synthetic prompt or provider body exists."""

    normal = {
        "pipeline_stage": "submit",
        "stable_failure_code": None,
        "finish_reason_category": "tool_calls",
        "response_shape_category": "tool_call",
        "tool_call_present": True,
        "submit_attempt_count": 1,
        "empty_response_count": 0,
        "step_count": 1,
        "output_limit_reached": False,
        "usage_known": True,
        "provider_exception_type": "none",
    }
    cases: dict[str, tuple[str, dict[str, Any]]] = {
        "normal_submit": ("completed", normal),
        "empty_response": (
            "failed",
            {
                **normal,
                "pipeline_stage": "response_decode",
                "stable_failure_code": "empty_response",
                "finish_reason_category": "stop",
                "response_shape_category": "empty",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "empty_response_count": 1,
            },
        ),
        "repeated_empty_response": (
            "failed",
            {
                **normal,
                "pipeline_stage": "response_decode",
                "stable_failure_code": "repeated_empty_response",
                "finish_reason_category": "stop",
                "response_shape_category": "empty",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "empty_response_count": 2,
                "step_count": 2,
            },
        ),
        "text_only_response": (
            "failed",
            {
                **normal,
                "pipeline_stage": "response_decode",
                "stable_failure_code": "text_only_response",
                "finish_reason_category": "stop",
                "response_shape_category": "text_only",
                "tool_call_present": False,
                "submit_attempt_count": 0,
            },
        ),
        "malformed_tool_call": (
            "failed",
            {
                **normal,
                "pipeline_stage": "response_decode",
                "stable_failure_code": "malformed_tool_call",
                "response_shape_category": "malformed_tool_call",
                "submit_attempt_count": 0,
            },
        ),
        "finish_reason_length": (
            "failed",
            {
                **normal,
                "pipeline_stage": "response_decode",
                "stable_failure_code": "finish_reason_length",
                "finish_reason_category": "length",
                "response_shape_category": "not_observed",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "output_limit_reached": True,
            },
        ),
        "finish_reason_other": (
            "failed",
            {
                **normal,
                "pipeline_stage": "response_decode",
                "stable_failure_code": "finish_reason_other",
                "finish_reason_category": "other",
                "response_shape_category": "not_observed",
                "tool_call_present": False,
                "submit_attempt_count": 0,
            },
        ),
        "finder_step_cap": (
            "failed",
            {
                **normal,
                "pipeline_stage": "finder",
                "stable_failure_code": "finder_step_cap",
                "finish_reason_category": "not_observed",
                "response_shape_category": "not_observed",
                "submit_attempt_count": 0,
                "step_count": 6,
            },
        ),
        "verifier_step_cap": (
            "failed",
            {
                **normal,
                "pipeline_stage": "verifier",
                "stable_failure_code": "verifier_step_cap",
                "finish_reason_category": "not_observed",
                "response_shape_category": "not_observed",
                "submit_attempt_count": 0,
                "step_count": 6,
            },
        ),
        "tool_call_loop": (
            "failed",
            {
                **normal,
                "pipeline_stage": "finder",
                "stable_failure_code": "tool_call_loop",
                "submit_attempt_count": 0,
                "step_count": 6,
            },
        ),
        "invalid_submit_limit": (
            "failed",
            {
                **normal,
                "pipeline_stage": "verifier",
                "stable_failure_code": "invalid_submit_limit",
                "submit_attempt_count": 2,
                "step_count": 2,
            },
        ),
        "provider_schema_mismatch": (
            "failed",
            {
                **normal,
                "pipeline_stage": "response_decode",
                "stable_failure_code": "provider_schema_mismatch",
                "response_shape_category": "schema_mismatch",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "provider_exception_type": "schema_mismatch",
            },
        ),
        "provider_timeout": (
            "failed",
            {
                **normal,
                "pipeline_stage": "provider_transport",
                "stable_failure_code": "provider_timeout",
                "finish_reason_category": "not_observed",
                "response_shape_category": "not_observed",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "provider_exception_type": "timeout",
                "usage_known": False,
            },
        ),
        "provider_auth": (
            "failed",
            {
                **normal,
                "pipeline_stage": "provider_transport",
                "stable_failure_code": "provider_auth",
                "finish_reason_category": "not_observed",
                "response_shape_category": "not_observed",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "provider_exception_type": "auth",
                "usage_known": False,
            },
        ),
        "provider_rate_limit": (
            "failed",
            {
                **normal,
                "pipeline_stage": "provider_transport",
                "stable_failure_code": "provider_rate_limit",
                "finish_reason_category": "not_observed",
                "response_shape_category": "not_observed",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "provider_exception_type": "rate_limit",
                "usage_known": False,
            },
        ),
        "provider_server_error": (
            "failed",
            {
                **normal,
                "pipeline_stage": "provider_transport",
                "stable_failure_code": "provider_server_error",
                "finish_reason_category": "not_observed",
                "response_shape_category": "not_observed",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "provider_exception_type": "server_error",
                "usage_known": False,
            },
        ),
        "provider_connection_error": (
            "failed",
            {
                **normal,
                "pipeline_stage": "provider_transport",
                "stable_failure_code": "provider_connection_error",
                "finish_reason_category": "not_observed",
                "response_shape_category": "not_observed",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "provider_exception_type": "connection_error",
                "usage_known": False,
            },
        ),
        "local_budget_exhausted": (
            "failed",
            {
                **normal,
                "pipeline_stage": "budget_reservation",
                "stable_failure_code": "local_budget_exhausted",
                "finish_reason_category": "not_observed",
                "response_shape_category": "not_observed",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "usage_known": False,
            },
        ),
        "local_deadline": (
            "failed",
            {
                **normal,
                "pipeline_stage": "preflight",
                "stable_failure_code": "local_deadline",
                "finish_reason_category": "not_observed",
                "response_shape_category": "not_observed",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "usage_known": False,
            },
        ),
        "ambiguous_result": (
            "quarantined",
            {
                **normal,
                "pipeline_stage": "receipt_reconcile",
                "stable_failure_code": "ambiguous_result",
                "finish_reason_category": "not_observed",
                "response_shape_category": "not_observed",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "usage_known": False,
            },
        ),
        "usage_unknown": (
            "inconclusive",
            {
                **normal,
                "pipeline_stage": "receipt_reconcile",
                "stable_failure_code": "other",
                "finish_reason_category": "not_observed",
                "response_shape_category": "not_observed",
                "tool_call_present": False,
                "submit_attempt_count": 0,
                "usage_known": False,
            },
        ),
    }
    try:
        return cases[scenario]
    except KeyError as exc:
        raise CanaryValidationError("unknown_fake_scenario") from exc


FAKE_SCENARIOS = tuple(
    sorted(
        {
            "normal_submit",
            "empty_response",
            "repeated_empty_response",
            "text_only_response",
            "malformed_tool_call",
            "finish_reason_length",
            "finish_reason_other",
            "finder_step_cap",
            "verifier_step_cap",
            "tool_call_loop",
            "invalid_submit_limit",
            "provider_schema_mismatch",
            "provider_timeout",
            "provider_auth",
            "provider_rate_limit",
            "provider_server_error",
            "provider_connection_error",
            "local_budget_exhausted",
            "local_deadline",
            "ambiguous_result",
            "usage_unknown",
        }
    )
)


def fake_protocol_terminal(scenario: str) -> dict[str, Any]:
    terminal_status, fields = _fake_telemetry_for_scenario(scenario)
    telemetry = build_safe_telemetry(**fields)
    return validate_terminal_receipt(
        {
            "terminal_status": terminal_status,
            "telemetry": telemetry,
            "redaction_applied": True,
        }
    )


def run_fake_compatibility_matrix() -> list[dict[str, Any]]:
    """Exercise every required response shape using only fixed synthetic categories."""

    results: list[dict[str, Any]] = []
    for scenario in FAKE_SCENARIOS:
        terminal = fake_protocol_terminal(scenario)
        telemetry = terminal["telemetry"]
        results.append(
            {
                "scenario": scenario,
                "terminal_status": terminal["terminal_status"],
                "pipeline_stage": telemetry["pipeline_stage"],
                "stable_failure_code": telemetry["stable_failure_code"],
                "redaction_applied": terminal["redaction_applied"],
            }
        )
    if any(item["pipeline_stage"] not in PIPELINE_STAGES for item in results):
        _fail("fake_matrix_pipeline_stage_mismatch")
    return results


@dataclass(frozen=True)
class BudgetLimits:
    logical_calls: int
    http_attempts: int
    input_tokens: int
    output_tokens: int
    micro_cny: int

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            _expect_nonnegative_int(value, "invalid_budget_limit")


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    input_tokens: int
    output_tokens: int
    micro_cny: int

    def __post_init__(self) -> None:
        if not isinstance(self.reservation_id, str) or not self.reservation_id:
            _fail("invalid_reservation_id")
        for value in (self.input_tokens, self.output_tokens, self.micro_cny):
            _expect_nonnegative_int(value, "invalid_reservation_amount")


@dataclass
class DurableBudgetLedger:
    """Serializable monotonic accounting for Gate B-compatible fakes.

    Reservations immediately consume hard-cap capacity.  Reconciliation can add detail
    but can never reduce accumulated logical-call, HTTP-attempt, token, or cost totals.
    """

    limits: BudgetLimits
    logical_calls: int = 0
    http_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    micro_cny: int = 0
    reservations: dict[str, BudgetReservation] | None = None
    http_recorded: set[str] | None = None
    reconciled: set[str] | None = None

    def __post_init__(self) -> None:
        self.reservations = dict(self.reservations or {})
        self.http_recorded = set(self.http_recorded or set())
        self.reconciled = set(self.reconciled or set())
        for value in (
            self.logical_calls,
            self.http_attempts,
            self.input_tokens,
            self.output_tokens,
            self.micro_cny,
        ):
            _expect_nonnegative_int(value, "invalid_ledger_value")
        self._assert_within_limits()

    def _assert_within_limits(self) -> None:
        for name in ("logical_calls", "http_attempts", "input_tokens", "output_tokens", "micro_cny"):
            if getattr(self, name) > getattr(self.limits, name):
                raise BudgetExhausted("budget_hard_cap_exhausted")

    def reserve(self, reservation: BudgetReservation) -> None:
        if reservation.reservation_id in self.reservations or reservation.reservation_id in self.reconciled:
            _fail("duplicate_budget_reservation")
        proposed = {
            "logical_calls": self.logical_calls + 1,
            "input_tokens": self.input_tokens + reservation.input_tokens,
            "output_tokens": self.output_tokens + reservation.output_tokens,
            "micro_cny": self.micro_cny + reservation.micro_cny,
        }
        for name, value in proposed.items():
            if value > getattr(self.limits, name):
                raise BudgetExhausted("budget_hard_cap_exhausted")
        self.logical_calls = proposed["logical_calls"]
        self.input_tokens = proposed["input_tokens"]
        self.output_tokens = proposed["output_tokens"]
        self.micro_cny = proposed["micro_cny"]
        self.reservations[reservation.reservation_id] = reservation

    def record_http_attempt(self, reservation_id: str) -> None:
        if reservation_id not in self.reservations:
            _fail("http_attempt_without_reservation")
        if reservation_id in self.http_recorded:
            _fail("duplicate_http_attempt")
        if self.http_attempts + 1 > self.limits.http_attempts:
            raise BudgetExhausted("budget_hard_cap_exhausted")
        self.http_attempts += 1
        self.http_recorded.add(reservation_id)

    def reconcile(
        self,
        reservation_id: str,
        *,
        usage_known: bool,
        actual_input_tokens: int | None = None,
        actual_output_tokens: int | None = None,
        actual_micro_cny: int | None = None,
    ) -> None:
        if reservation_id not in self.reservations:
            _fail("unknown_budget_reservation")
        reservation = self.reservations[reservation_id]
        if usage_known:
            actuals = (actual_input_tokens, actual_output_tokens, actual_micro_cny)
            if any(value is None for value in actuals):
                _fail("known_usage_missing_values")
            amounts = tuple(
                _expect_nonnegative_int(value, "invalid_actual_usage") for value in actuals
            )
            caps = (reservation.input_tokens, reservation.output_tokens, reservation.micro_cny)
            if any(actual > cap for actual, cap in zip(amounts, caps)):
                _fail("actual_usage_exceeds_reservation")
        del self.reservations[reservation_id]
        self.reconciled.add(reservation_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "limits": asdict(self.limits),
            "logical_calls": self.logical_calls,
            "http_attempts": self.http_attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "micro_cny": self.micro_cny,
            "reservations": [
                asdict(self.reservations[key]) for key in sorted(self.reservations)
            ],
            "http_recorded": sorted(self.http_recorded),
            "reconciled": sorted(self.reconciled),
        }

    @classmethod
    def from_snapshot(cls, value: Mapping[str, Any]) -> "DurableBudgetLedger":
        snapshot = _expect_mapping(dict(value), "invalid_budget_snapshot")
        _expect_exact_keys(
            snapshot,
            {
                "limits",
                "logical_calls",
                "http_attempts",
                "input_tokens",
                "output_tokens",
                "micro_cny",
                "reservations",
                "http_recorded",
                "reconciled",
            },
            "invalid_budget_snapshot_keys",
        )
        limits = _expect_mapping(snapshot["limits"], "invalid_snapshot_limits")
        _expect_exact_keys(limits, PHASE9H_PLANNING_CEILING, "invalid_snapshot_limit_keys")
        reservations_value = snapshot["reservations"]
        if not isinstance(reservations_value, list):
            _fail("invalid_snapshot_reservations")
        reservations = {
            item.reservation_id: item
            for item in (
                BudgetReservation(**_expect_mapping(record, "invalid_snapshot_reservation"))
                for record in reservations_value
            )
        }
        for name in ("http_recorded", "reconciled"):
            if not isinstance(snapshot[name], list) or not all(
                isinstance(item, str) for item in snapshot[name]
            ):
                _fail("invalid_snapshot_set")
        return cls(
            limits=BudgetLimits(**limits),
            logical_calls=_expect_nonnegative_int(snapshot["logical_calls"], "invalid_snapshot_count"),
            http_attempts=_expect_nonnegative_int(snapshot["http_attempts"], "invalid_snapshot_count"),
            input_tokens=_expect_nonnegative_int(snapshot["input_tokens"], "invalid_snapshot_count"),
            output_tokens=_expect_nonnegative_int(snapshot["output_tokens"], "invalid_snapshot_count"),
            micro_cny=_expect_nonnegative_int(snapshot["micro_cny"], "invalid_snapshot_count"),
            reservations=reservations,
            http_recorded=set(snapshot["http_recorded"]),
            reconciled=set(snapshot["reconciled"]),
        )


@dataclass(frozen=True)
class CredentialFileMetadata:
    """Non-secret fixture for Gate B file-security validation logic."""

    is_regular_file: bool
    is_symlink: bool
    owner_is_root: bool
    mode_octal: int
    expired: bool
    revoked: bool


def validate_credential_file_metadata(value: CredentialFileMetadata) -> str | None:
    """Return a stable refusal code without opening or reading a credential file."""

    if value.is_symlink or not value.is_regular_file:
        return "authorization_mismatch"
    if not value.owner_is_root or value.mode_octal != 0o600:
        return "authorization_mismatch"
    if value.revoked:
        return "credential_revoked"
    if value.expired:
        return "credential_expired"
    return None


class RecordingFakeTransport:
    """In-memory fake that proves the ledger records an attempt before dispatch."""

    def __init__(self, ledger: DurableBudgetLedger) -> None:
        self._ledger = ledger
        self.calls = 0

    def dispatch(self, scenario: str, reservation_id: str) -> dict[str, Any]:
        if reservation_id not in self._ledger.http_recorded:
            _fail("transport_before_durable_http_attempt")
        self.calls += 1
        return fake_protocol_terminal(scenario)


def execute_fake_attempt(
    scenario: str,
    *,
    ledger: DurableBudgetLedger,
    reservation: BudgetReservation,
    transport: RecordingFakeTransport | None = None,
) -> dict[str, Any]:
    """Execute one fake protocol attempt with Gate B-compatible ordering only."""

    ledger.reserve(reservation)
    ledger.record_http_attempt(reservation.reservation_id)
    fake_transport = transport or RecordingFakeTransport(ledger)
    terminal = fake_transport.dispatch(scenario, reservation.reservation_id)
    telemetry = terminal["telemetry"]
    ledger.reconcile(
        reservation.reservation_id,
        usage_known=bool(telemetry["usage_known"]),
        actual_input_tokens=0 if telemetry["usage_known"] else None,
        actual_output_tokens=0 if telemetry["usage_known"] else None,
        actual_micro_cny=0 if telemetry["usage_known"] else None,
    )
    return {
        "terminal_status": terminal["terminal_status"],
        "telemetry": telemetry,
        "logical_call_count": ledger.logical_calls,
        "http_attempt_count": ledger.http_attempts,
        "redaction_applied": True,
    }


def run_real_gate_blocked() -> dict[str, Any]:
    """Produce a safe zero-I/O receipt for an intentionally closed Gate B."""

    telemetry = build_safe_telemetry(
        pipeline_stage="authorization",
        stable_failure_code="authorization_mismatch",
        finish_reason_category="not_observed",
        response_shape_category="not_observed",
        tool_call_present=False,
        submit_attempt_count=0,
        empty_response_count=0,
        step_count=0,
        output_limit_reached=False,
        usage_known=False,
        provider_exception_type="none",
    )
    return {
        "execution_status": "not_run",
        "protocol_canary_status": "not_run",
        "terminal_status": "not_run_gate_blocked",
        "telemetry": telemetry,
        "provider_call_count": 0,
        "http_attempt_count": 0,
        "redaction_applied": True,
    }


ARTIFACT_FILENAMES = {
    "authorization": "authorization.candidate.json",
    "runtime_config": "runtime-config.candidate.json",
    "synthetic_cohort": "synthetic-cohort.candidate.json",
    "tariff": "tariff.candidate.json",
    "budget_proposal": "budget-proposal.candidate.json",
    "preflight_verdict": "preflight-verdict.candidate.json",
}


def _safe_output_directory(value: str | Path) -> Path:
    supplied = Path(value)
    if supplied.is_absolute():
        _fail("absolute_output_path_denied")
    root = Path(__file__).resolve().parent
    resolved = (root / supplied).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CanaryValidationError("output_path_escape_denied") from exc
    return resolved


def write_gate_a_artifacts(
    output_directory: str | Path,
    *,
    executable_source_digest: str | None = None,
    replace_existing: bool = False,
) -> dict[str, dict[str, Any]]:
    """Create deterministic candidate files without silently replacing drifted evidence."""

    destination = _safe_output_directory(output_directory)
    candidates = build_gate_a_candidates(executable_source_digest=executable_source_digest)
    destination.mkdir(parents=True, exist_ok=True)
    for name, filename in ARTIFACT_FILENAMES.items():
        path = destination / filename
        encoded = canonical_json(candidates[name]) + b"\n"
        if path.exists() and path.read_bytes() != encoded and not replace_existing:
            _fail("candidate_artifact_drift")
        path.write_bytes(encoded)
    return candidates


def load_gate_a_artifacts(output_directory: str | Path) -> dict[str, dict[str, Any]]:
    directory = _safe_output_directory(output_directory)
    candidates: dict[str, dict[str, Any]] = {}
    for name, filename in ARTIFACT_FILENAMES.items():
        try:
            loaded = strict_json_loads((directory / filename).read_bytes())
        except OSError as exc:
            raise CanaryValidationError("missing_gate_a_artifact") from exc
        candidates[name] = _expect_mapping(loaded, "invalid_gate_a_artifact")
    return candidates


def _print_safe_json(value: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> None:
    print(canonical_json(value).decode("utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate-gate-a")
    generate.add_argument(
        "--output",
        default="phase11c_provider_canary/examples/gate_a",
        help="repository-relative candidate artifact directory",
    )
    generate.add_argument(
        "--replace",
        action="store_true",
        help="explicitly replace pre-commit Gate A candidates after an executable change",
    )
    validate = commands.add_parser("validate-gate-a")
    validate.add_argument(
        "--output",
        default="phase11c_provider_canary/examples/gate_a",
        help="repository-relative candidate artifact directory",
    )
    fake = commands.add_parser("run-fake")
    fake.add_argument("--scenario", choices=FAKE_SCENARIOS, default="normal_submit")
    commands.add_parser("run-real")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate-gate-a":
        candidates = write_gate_a_artifacts(args.output, replace_existing=args.replace)
        _print_safe_json(
            {
                "gate_a_status": "generated",
                "candidate_artifact_hashes": _candidate_hashes(candidates),
                "canary_allowed": False,
                "real_run_recommended_now": False,
            }
        )
        return 0
    if args.command == "validate-gate-a":
        candidates = load_gate_a_artifacts(args.output)
        validated = validate_gate_a_candidates(candidates, executable_source_digest=source_sha256())
        _print_safe_json({"gate_a_status": "valid", **validated})
        return 0
    if args.command == "run-fake":
        limits = BudgetLimits(1, 1, 1_024, 1_024, 1_024)
        receipt = execute_fake_attempt(
            args.scenario,
            ledger=DurableBudgetLedger(limits),
            reservation=BudgetReservation("fake-attempt-1", 1_024, 1_024, 1_024),
        )
        _print_safe_json(receipt)
        return 0
    if args.command == "run-real":
        _print_safe_json(run_real_gate_blocked())
        return 2
    raise AssertionError("unreachable parser command")


if __name__ == "__main__":
    raise SystemExit(main())
