"""Offline preparation and integrity gates for the Phase 9G real pilot.

The module intentionally uses only the Python standard library.  It never
starts a subprocess, opens a network connection, invokes a provider SDK, or
reads an input whose resolved path contains an ``eval`` or ``holdout``
component.  Real executors consume the validated artifacts; this module is not
an executor.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics
import sys
from typing import Any, Mapping, NoReturn, Sequence


SCHEMA_VERSION = 1
PHASE_ID = "phase9g-real-pilot-v1"
REPORT_VERSION = "phase9g-business-v1"
SELECTION_SEED_DOMAIN = b"phase9g-pilot-selection-v1\0"
FORBIDDEN_PATH_PARTS = frozenset({"eval", "holdout"})
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,199}$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)

AUTHORIZATION_KEYS = {
    "schema_version",
    "phase_id",
    "authorization_id",
    "approved_by",
    "approved_at",
    "expires_at",
    "business_pilot",
    "model",
    "formal_quality",
    "external_operations",
    "synthetic",
    "authorization_sha256",
}
BUSINESS_AUTH_KEYS = {
    "participant_ids",
    "participants_confirmed_real",
    "repository_ids",
    "pr_count",
    "pr_selection_rule",
    "mode",
    "real_github_publish",
    "publish_approver_id",
    "data_retention_days",
    "feedback_retention_days",
}
MODEL_AUTH_KEYS = {
    "provider",
    "exact_model_snapshot",
    "temperature",
    "max_logical_calls",
    "max_http_attempts",
    "max_input_tokens",
    "max_output_tokens",
    "max_cost_microcny",
    "real_paid_calls",
    "read_raw_diff",
    "raw_trace_retention_days",
}
FORMAL_AUTH_KEYS = {
    "execute",
    "annotator_a_id",
    "annotator_b_id",
    "adjudicator_c_id",
    "humans_confirmed_distinct",
    "gold_freeze_custodian_id",
    "reporting_results_no_tuning",
}
EXTERNAL_AUTH_KEYS = {
    "staging_deploy",
    "deployment_target",
    "real_github_api",
    "create_comments_or_checks",
    "local_commit",
    "push_task_branch",
    "create_pr",
    "merge_master",
}

PARTICIPANT_MANIFEST_KEYS = {
    "schema_version",
    "phase_id",
    "pilot_id",
    "identity_custodian_id",
    "consent_version",
    "generated_at",
    "participants",
    "synthetic",
    "manifest_sha256",
}
PARTICIPANT_KEYS = {
    "participant_id",
    "confirmed_real",
    "role",
    "consented_at",
    "consent_expires_at",
    "consent_scope",
    "repository_ids",
    "feedback_retention_days",
    "withdrawal_acknowledged",
}
REPOSITORY_MANIFEST_KEYS = {
    "schema_version",
    "phase_id",
    "pilot_id",
    "generated_at",
    "repositories",
    "synthetic",
    "manifest_sha256",
}
REPOSITORY_KEYS = {
    "repository_id",
    "locator_sha256",
    "authorized_by",
    "authorized_at",
    "authorization_expires_at",
    "allowed_tracks",
    "raw_diff_read_authorized",
    "real_github_api_authorized",
    "publish_mode",
    "publication_authorized",
    "data_retention_days",
    "repository_sha256",
}

SELECTION_PLAN_KEYS = {
    "schema_version",
    "phase_id",
    "pilot_id",
    "seed",
    "seed_derivation",
    "selection_window",
    "groups",
    "generated_at",
    "synthetic",
    "plan_sha256",
}
SELECTION_SEED_DERIVATION_KEYS = {"method", "source_commit"}
SELECTION_WINDOW_KEYS = {"start", "end"}
SELECTION_GROUP_KEYS = {
    "track",
    "role",
    "repository_id",
    "target_prs",
    "exclusion_reasons",
}
SELECTION_ROW_KEYS = {
    "schema_version",
    "pilot_id",
    "track",
    "role",
    "repository_id",
    "pr_id",
    "merged_at",
    "eligible",
    "exclusion_reason",
    "selected",
    "rank_sha256",
    "snapshot_sha256",
    "diff_sha256",
    "synthetic",
    "row_sha256",
}
COHORT_KEYS = {
    "schema_version",
    "phase_id",
    "pilot_id",
    "materialized_at",
    "selection_plan_sha256",
    "selection_log_sha256",
    "entries",
    "synthetic",
    "cohort_sha256",
}
COHORT_ENTRY_KEYS = {
    "track",
    "role",
    "repository_id",
    "pr_id",
    "snapshot_sha256",
    "diff_sha256",
    "selected_at",
}

TRACK_ROLES = {
    ("business", "pilot"),
    ("formal", "calibration"),
    ("formal", "reporting"),
}
REPOSITORY_TRACKS = {"business", "formal_calibration", "formal_reporting"}


class ValidationError(ValueError):
    """A stable, content-free Phase 9G artifact failure."""


def _fail(message: str) -> NoReturn:
    raise ValidationError(message)


def _expect_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{where} must be an object")
    return value


def _expect_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{where} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], where: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing keys {missing}")
        if unknown:
            details.append(f"unknown keys {unknown}")
        _fail(f"{where}: {'; '.join(details)}")


def _expect_str(value: Any, where: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{where} must be a non-empty string")
    if len(value.encode("utf-8")) > maximum:
        _fail(f"{where} exceeds {maximum} UTF-8 bytes")
    return value


def _expect_nullable_str(value: Any, where: str, *, maximum: int = 4096) -> str | None:
    if value is None:
        return None
    return _expect_str(value, where, maximum=maximum)


def _expect_identifier(value: Any, where: str) -> str:
    result = _expect_str(value, where, maximum=200)
    if not IDENTIFIER_RE.fullmatch(result):
        _fail(f"{where} is not a stable opaque identifier")
    return result


def _expect_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{where} must be an explicit boolean")
    return value


def _expect_int(value: Any, where: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{where} must be an integer >= {minimum}")
    return value


def _expect_finite_number(value: Any, where: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{where} must be a finite number >= {minimum}")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        _fail(f"{where} must be a finite number >= {minimum}")
    return result


def _expect_enum(value: Any, allowed: set[str], where: str) -> str:
    result = _expect_str(value, where)
    if result not in allowed:
        _fail(f"{where} must be one of {sorted(allowed)}")
    return result


def _expect_sha(value: Any, where: str, *, length: int = 64) -> str:
    result = _expect_str(value, where, maximum=64)
    pattern = HEX40_RE if length == 40 else HEX64_RE
    if not pattern.fullmatch(result):
        _fail(f"{where} must be a lowercase {length}-character SHA-256/Git digest")
    return result


def parse_timestamp(value: Any, where: str) -> datetime:
    text = _expect_str(value, where, maximum=20)
    if not TIMESTAMP_RE.fullmatch(text):
        _fail(f"{where} must use YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ValidationError(f"{where} is not a valid UTC timestamp") from exc
    return parsed


def canonical_bytes(value: Any) -> bytes:
    """Encode canonical UTF-8 JSON with no non-finite numbers."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("artifact is not canonical-JSON encodable") from exc


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def with_artifact_hash(value: Mapping[str, Any], hash_field: str) -> dict[str, Any]:
    result = dict(value)
    if hash_field not in result:
        _fail(f"artifact is missing hash field {hash_field!r}")
    result[hash_field] = ""
    result[hash_field] = sha256_value(result)
    return result


def validate_artifact_hash(value: Mapping[str, Any], hash_field: str, where: str) -> None:
    declared = _expect_sha(value.get(hash_field), f"{where}.{hash_field}")
    unhashed = dict(value)
    unhashed[hash_field] = ""
    if declared != sha256_value(unhashed):
        _fail(f"{where}.{hash_field} does not match canonical content")


def _reject_forbidden_path(path: str | Path) -> Path:
    resolved = Path(path).resolve()
    if any(part.casefold() in FORBIDDEN_PATH_PARTS for part in resolved.parts):
        _fail("protected evaluation paths are forbidden")
    return resolved


def load_json(path: str | Path) -> Any:
    resolved = _reject_forbidden_path(path)
    try:
        raw = resolved.read_bytes()
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("cannot load the requested UTF-8 JSON artifact") from exc


def load_jsonl(path: str | Path) -> list[Any]:
    resolved = _reject_forbidden_path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError("cannot load the requested UTF-8 JSONL artifact") from exc
    rows = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"JSONL line {line_number} is malformed") from exc
    return rows


def _write_json(path: str | Path, value: Any) -> None:
    resolved = _reject_forbidden_path(path)
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ValidationError("cannot write the requested JSON artifact") from exc


def _write_jsonl(path: str | Path, rows: Sequence[Mapping[str, Any]]) -> None:
    resolved = _reject_forbidden_path(path)
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
                for row in rows
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        raise ValidationError("cannot write the requested JSONL artifact") from exc


def _unique_identifiers(values: Any, where: str, *, minimum: int = 0) -> list[str]:
    rows = _expect_list(values, where)
    result = [_expect_identifier(value, f"{where}[{index}]") for index, value in enumerate(rows)]
    if len(result) < minimum:
        _fail(f"{where} requires at least {minimum} entries")
    if len(result) != len(set(result)):
        _fail(f"{where} contains duplicate identities")
    return result


def validate_authorization(
    raw: Any,
    *,
    at: datetime | None = None,
    require_hash: bool = True,
) -> dict[str, Any]:
    """Validate completeness and integrity without turning denial into authority."""

    authorization = _expect_dict(raw, "authorization")
    _exact_keys(authorization, AUTHORIZATION_KEYS, "authorization")
    if _expect_int(authorization["schema_version"], "authorization.schema_version") != 1:
        _fail("authorization schema_version must be 1")
    if authorization["phase_id"] != PHASE_ID:
        _fail("authorization phase_id is invalid")
    _expect_identifier(authorization["authorization_id"], "authorization.authorization_id")
    _expect_identifier(authorization["approved_by"], "authorization.approved_by")
    approved_at = parse_timestamp(authorization["approved_at"], "authorization.approved_at")
    expires_at = parse_timestamp(authorization["expires_at"], "authorization.expires_at")
    if expires_at <= approved_at:
        _fail("authorization expires_at must follow approved_at")

    business = _expect_dict(authorization["business_pilot"], "authorization.business_pilot")
    _exact_keys(business, BUSINESS_AUTH_KEYS, "authorization.business_pilot")
    participants = _unique_identifiers(
        business["participant_ids"], "authorization.business_pilot.participant_ids"
    )
    _expect_bool(
        business["participants_confirmed_real"],
        "authorization.business_pilot.participants_confirmed_real",
    )
    repositories = _unique_identifiers(
        business["repository_ids"],
        "authorization.business_pilot.repository_ids",
        minimum=1,
    )
    _expect_int(business["pr_count"], "authorization.business_pilot.pr_count")
    _expect_str(
        business["pr_selection_rule"],
        "authorization.business_pilot.pr_selection_rule",
        maximum=2000,
    )
    mode = _expect_enum(
        business["mode"], {"shadow", "publish"}, "authorization.business_pilot.mode"
    )
    publish = _expect_bool(
        business["real_github_publish"],
        "authorization.business_pilot.real_github_publish",
    )
    approver = _expect_nullable_str(
        business["publish_approver_id"],
        "authorization.business_pilot.publish_approver_id",
        maximum=200,
    )
    if approver is not None:
        _expect_identifier(approver, "authorization.business_pilot.publish_approver_id")
    if publish and approver is None:
        _fail("real GitHub publication requires a stable publish approver")
    if mode == "shadow" and publish:
        _fail("shadow mode cannot authorize real GitHub publication")
    _expect_int(
        business["data_retention_days"],
        "authorization.business_pilot.data_retention_days",
        minimum=1,
    )
    _expect_int(
        business["feedback_retention_days"],
        "authorization.business_pilot.feedback_retention_days",
        minimum=1,
    )

    model = _expect_dict(authorization["model"], "authorization.model")
    _exact_keys(model, MODEL_AUTH_KEYS, "authorization.model")
    _expect_identifier(model["provider"], "authorization.model.provider")
    _expect_identifier(
        model["exact_model_snapshot"], "authorization.model.exact_model_snapshot"
    )
    _expect_finite_number(model["temperature"], "authorization.model.temperature")
    _expect_int(model["max_logical_calls"], "authorization.model.max_logical_calls")
    _expect_int(model["max_http_attempts"], "authorization.model.max_http_attempts")
    _expect_int(model["max_input_tokens"], "authorization.model.max_input_tokens")
    _expect_int(model["max_output_tokens"], "authorization.model.max_output_tokens")
    _expect_int(model["max_cost_microcny"], "authorization.model.max_cost_microcny")
    _expect_bool(model["real_paid_calls"], "authorization.model.real_paid_calls")
    _expect_bool(model["read_raw_diff"], "authorization.model.read_raw_diff")
    _expect_int(
        model["raw_trace_retention_days"],
        "authorization.model.raw_trace_retention_days",
        minimum=1,
    )
    if model["max_http_attempts"] < model["max_logical_calls"]:
        _fail("authorized HTTP attempts cannot be lower than logical calls")

    formal = _expect_dict(authorization["formal_quality"], "authorization.formal_quality")
    _exact_keys(formal, FORMAL_AUTH_KEYS, "authorization.formal_quality")
    execute_formal = _expect_bool(formal["execute"], "authorization.formal_quality.execute")
    human_ids = [
        _expect_identifier(formal[key], f"authorization.formal_quality.{key}")
        for key in ("annotator_a_id", "annotator_b_id", "adjudicator_c_id")
    ]
    confirmed_distinct = _expect_bool(
        formal["humans_confirmed_distinct"],
        "authorization.formal_quality.humans_confirmed_distinct",
    )
    _expect_identifier(
        formal["gold_freeze_custodian_id"],
        "authorization.formal_quality.gold_freeze_custodian_id",
    )
    _expect_bool(
        formal["reporting_results_no_tuning"],
        "authorization.formal_quality.reporting_results_no_tuning",
    )
    if execute_formal and (len(set(human_ids)) != 3 or not confirmed_distinct):
        _fail("formal quality requires three confirmed-distinct stable human IDs")

    external = _expect_dict(
        authorization["external_operations"], "authorization.external_operations"
    )
    _exact_keys(external, EXTERNAL_AUTH_KEYS, "authorization.external_operations")
    staging = _expect_bool(
        external["staging_deploy"], "authorization.external_operations.staging_deploy"
    )
    target = _expect_nullable_str(
        external["deployment_target"],
        "authorization.external_operations.deployment_target",
        maximum=256,
    )
    if staging and target is None:
        _fail("staging deployment requires an explicit target")
    for key in (
        "real_github_api",
        "create_comments_or_checks",
        "local_commit",
        "push_task_branch",
        "create_pr",
        "merge_master",
    ):
        _expect_bool(external[key], f"authorization.external_operations.{key}")
    if publish and (
        not external["real_github_api"] or not external["create_comments_or_checks"]
    ):
        _fail("publication authority requires GitHub API and comment/Check authority")
    _expect_bool(authorization["synthetic"], "authorization.synthetic")
    if require_hash:
        validate_artifact_hash(authorization, "authorization_sha256", "authorization")
    elif authorization["authorization_sha256"] not in {"", None}:
        _expect_sha(
            authorization["authorization_sha256"],
            "authorization.authorization_sha256",
        )

    checked_at = at or datetime.now(timezone.utc)
    return {
        **authorization,
        "_participant_ids": participants,
        "_repository_ids": repositories,
        "_not_yet_effective": checked_at < approved_at,
        "_expired": checked_at >= expires_at,
    }


def authorization_readiness(authorization: Mapping[str, Any]) -> dict[str, Any]:
    """Return fail-closed scope decisions; a valid table can still deny work."""

    auth = validate_authorization(authorization)
    business = auth["business_pilot"]
    model = auth["model"]
    formal = auth["formal_quality"]
    external = auth["external_operations"]
    common: list[str] = []
    if auth["synthetic"]:
        common.append("synthetic_authorization")
    if auth["_not_yet_effective"]:
        common.append("authorization_not_yet_effective")
    if auth["_expired"]:
        common.append("authorization_expired")

    business_reasons = list(common)
    if not 3 <= len(auth["_participant_ids"]) <= 5:
        business_reasons.append("participant_count_outside_3_5")
    if not business["participants_confirmed_real"]:
        business_reasons.append("participants_not_confirmed_real")
    if not 20 <= business["pr_count"] <= 30:
        business_reasons.append("pr_count_outside_20_30")

    model_reasons = list(business_reasons)
    if not model["real_paid_calls"]:
        model_reasons.append("real_paid_calls_not_authorized")
    if not model["read_raw_diff"]:
        model_reasons.append("raw_diff_read_not_authorized")
    for key in (
        "max_logical_calls",
        "max_http_attempts",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_microcny",
    ):
        if model[key] <= 0:
            model_reasons.append(f"{key}_not_positive")

    formal_reasons = list(model_reasons)
    humans = {
        formal["annotator_a_id"],
        formal["annotator_b_id"],
        formal["adjudicator_c_id"],
    }
    if not formal["execute"]:
        formal_reasons.append("formal_quality_not_authorized")
    if len(humans) != 3 or not formal["humans_confirmed_distinct"]:
        formal_reasons.append("three_distinct_real_humans_not_confirmed")
    if not formal["reporting_results_no_tuning"]:
        formal_reasons.append("reporting_tuning_not_prohibited")

    publish_reasons = list(business_reasons)
    if business["mode"] != "publish":
        publish_reasons.append("pilot_mode_is_not_publish")
    if not business["real_github_publish"]:
        publish_reasons.append("real_github_publish_not_authorized")
    if not external["real_github_api"]:
        publish_reasons.append("real_github_api_not_authorized")
    if not external["create_comments_or_checks"]:
        publish_reasons.append("github_comment_check_not_authorized")

    deploy_reasons = list(common)
    if not external["staging_deploy"]:
        deploy_reasons.append("staging_deploy_not_authorized")
    if external["deployment_target"] is None:
        deploy_reasons.append("deployment_target_missing")

    blocked = {
        "business": business_reasons,
        "model": model_reasons,
        "formal_quality": formal_reasons,
        "publish": publish_reasons,
        "deploy": deploy_reasons,
    }
    return {
        "authorization_complete": True,
        "scopes": {
            scope: {"ready": not reasons, "blocked_by": reasons}
            for scope, reasons in blocked.items()
        },
    }


def validate_participant_manifest(
    raw: Any,
    authorization: Mapping[str, Any],
    *,
    at: datetime | None = None,
) -> dict[str, Any]:
    manifest = _expect_dict(raw, "participants")
    _exact_keys(manifest, PARTICIPANT_MANIFEST_KEYS, "participants")
    if manifest["schema_version"] != 1 or manifest["phase_id"] != PHASE_ID:
        _fail("participant manifest schema or phase is invalid")
    _expect_identifier(manifest["pilot_id"], "participants.pilot_id")
    _expect_identifier(
        manifest["identity_custodian_id"], "participants.identity_custodian_id"
    )
    _expect_identifier(manifest["consent_version"], "participants.consent_version")
    generated_at = parse_timestamp(manifest["generated_at"], "participants.generated_at")
    synthetic = _expect_bool(manifest["synthetic"], "participants.synthetic")
    rows = _expect_list(manifest["participants"], "participants.participants")
    if not 3 <= len(rows) <= 5:
        _fail("participant manifest requires 3--5 participants")
    expected = set(validate_authorization(authorization)["_participant_ids"])
    seen: set[str] = set()
    repository_ids: set[str] = set()
    checked_at = at or datetime.now(timezone.utc)
    if generated_at > checked_at:
        _fail("participant manifest generated_at is in the future")
    for index, raw_row in enumerate(rows):
        where = f"participants.participants[{index}]"
        row = _expect_dict(raw_row, where)
        _exact_keys(row, PARTICIPANT_KEYS, where)
        participant_id = _expect_identifier(row["participant_id"], f"{where}.participant_id")
        if participant_id in seen:
            _fail("participant manifest contains duplicate participant IDs")
        seen.add(participant_id)
        confirmed_real = _expect_bool(row["confirmed_real"], f"{where}.confirmed_real")
        if not synthetic and not confirmed_real:
            _fail("a real participant manifest cannot contain an unconfirmed person")
        _expect_enum(
            row["role"], {"developer", "reviewer", "maintainer"}, f"{where}.role"
        )
        consented = parse_timestamp(row["consented_at"], f"{where}.consented_at")
        consent_expires = parse_timestamp(
            row["consent_expires_at"], f"{where}.consent_expires_at"
        )
        if not consented <= generated_at < consent_expires:
            _fail(f"{where} has an invalid consent timeline")
        if checked_at >= consent_expires:
            _fail(f"{where} consent is expired")
        scope = _expect_list(row["consent_scope"], f"{where}.consent_scope")
        if set(scope) != {"business_feedback", "review_time"}:
            _fail(f"{where}.consent_scope must explicitly cover feedback and review time")
        row_repositories = _unique_identifiers(
            row["repository_ids"], f"{where}.repository_ids", minimum=1
        )
        repository_ids.update(row_repositories)
        _expect_int(
            row["feedback_retention_days"],
            f"{where}.feedback_retention_days",
            minimum=1,
        )
        if row["feedback_retention_days"] != authorization["business_pilot"][
            "feedback_retention_days"
        ]:
            _fail(f"{where} feedback retention differs from authorization")
        if not _expect_bool(
            row["withdrawal_acknowledged"], f"{where}.withdrawal_acknowledged"
        ):
            _fail(f"{where} must acknowledge the withdrawal process")
    if seen != expected:
        _fail("participant identities do not match the authorization table")
    if not repository_ids <= set(authorization["business_pilot"]["repository_ids"]):
        _fail("participant scope references an unauthorized repository")
    if synthetic != authorization["synthetic"]:
        _fail("participant and authorization provenance differ")
    validate_artifact_hash(manifest, "manifest_sha256", "participants")
    return manifest


def validate_repository_manifest(
    raw: Any,
    authorization: Mapping[str, Any],
    *,
    at: datetime | None = None,
) -> dict[str, Any]:
    manifest = _expect_dict(raw, "repositories")
    _exact_keys(manifest, REPOSITORY_MANIFEST_KEYS, "repositories")
    if manifest["schema_version"] != 1 or manifest["phase_id"] != PHASE_ID:
        _fail("repository manifest schema or phase is invalid")
    _expect_identifier(manifest["pilot_id"], "repositories.pilot_id")
    generated_at = parse_timestamp(manifest["generated_at"], "repositories.generated_at")
    synthetic = _expect_bool(manifest["synthetic"], "repositories.synthetic")
    auth = validate_authorization(authorization)
    expected = set(auth["_repository_ids"])
    rows = _expect_list(manifest["repositories"], "repositories.repositories")
    seen: set[str] = set()
    checked_at = at or datetime.now(timezone.utc)
    if generated_at > checked_at:
        _fail("repository manifest generated_at is in the future")
    for index, raw_row in enumerate(rows):
        where = f"repositories.repositories[{index}]"
        row = _expect_dict(raw_row, where)
        _exact_keys(row, REPOSITORY_KEYS, where)
        repository_id = _expect_identifier(row["repository_id"], f"{where}.repository_id")
        if repository_id in seen:
            _fail("repository manifest contains a duplicate repository ID")
        seen.add(repository_id)
        _expect_sha(row["locator_sha256"], f"{where}.locator_sha256")
        _expect_identifier(row["authorized_by"], f"{where}.authorized_by")
        authorized_at = parse_timestamp(row["authorized_at"], f"{where}.authorized_at")
        expires_at = parse_timestamp(
            row["authorization_expires_at"], f"{where}.authorization_expires_at"
        )
        if not authorized_at <= generated_at < expires_at:
            _fail(f"{where} has an invalid authorization timeline")
        if checked_at >= expires_at:
            _fail(f"{where} repository authorization is expired")
        tracks = _expect_list(row["allowed_tracks"], f"{where}.allowed_tracks")
        if not tracks or set(tracks) - REPOSITORY_TRACKS or len(tracks) != len(set(tracks)):
            _fail(f"{where}.allowed_tracks is invalid")
        read_diff = _expect_bool(
            row["raw_diff_read_authorized"], f"{where}.raw_diff_read_authorized"
        )
        github_api = _expect_bool(
            row["real_github_api_authorized"], f"{where}.real_github_api_authorized"
        )
        mode = _expect_enum(
            row["publish_mode"], {"shadow", "publish"}, f"{where}.publish_mode"
        )
        publication = _expect_bool(
            row["publication_authorized"], f"{where}.publication_authorized"
        )
        if mode == "shadow" and publication:
            _fail(f"{where} shadow mode cannot authorize publication")
        if publication and not github_api:
            _fail(f"{where} publication requires repository GitHub API authority")
        if read_diff and not auth["model"]["read_raw_diff"]:
            _fail(f"{where} grants raw diff access beyond the run authorization")
        if github_api and not auth["external_operations"]["real_github_api"]:
            _fail(f"{where} grants GitHub API access beyond the run authorization")
        if publication and not auth["business_pilot"]["real_github_publish"]:
            _fail(f"{where} grants publication beyond the run authorization")
        _expect_int(row["data_retention_days"], f"{where}.data_retention_days", minimum=1)
        if row["data_retention_days"] != auth["business_pilot"]["data_retention_days"]:
            _fail(f"{where} retention differs from authorization")
        validate_artifact_hash(row, "repository_sha256", where)
    if seen != expected:
        _fail("repository identities do not match the authorization table")
    if synthetic != auth["synthetic"]:
        _fail("repository and authorization provenance differ")
    validate_artifact_hash(manifest, "manifest_sha256", "repositories")
    return manifest


def selection_rank(seed: str, pr_id: str) -> str:
    _expect_sha(seed, "selection seed")
    _expect_identifier(pr_id, "pr_id")
    return hashlib.sha256(f"{seed}\n{pr_id}".encode("utf-8")).hexdigest()


def derive_selection_seed(source_commit: str) -> str:
    commit = _expect_sha(source_commit, "selection seed source_commit", length=40)
    return hashlib.sha256(SELECTION_SEED_DOMAIN + commit.encode("ascii")).hexdigest()


def validate_selection_plan(
    raw: Any,
    *,
    expected_source_commit: str | None = None,
) -> dict[str, Any]:
    plan = _expect_dict(raw, "selection_plan")
    _exact_keys(plan, SELECTION_PLAN_KEYS, "selection_plan")
    if plan["schema_version"] != 1 or plan["phase_id"] != PHASE_ID:
        _fail("selection plan schema or phase is invalid")
    _expect_identifier(plan["pilot_id"], "selection_plan.pilot_id")
    seed = _expect_sha(plan["seed"], "selection_plan.seed")
    seed_derivation = _expect_dict(
        plan["seed_derivation"], "selection_plan.seed_derivation"
    )
    _exact_keys(
        seed_derivation,
        SELECTION_SEED_DERIVATION_KEYS,
        "selection_plan.seed_derivation",
    )
    if seed_derivation["method"] != "sha256_source_commit_v1":
        _fail("selection plan seed derivation method is unsupported")
    source_commit = _expect_sha(
        seed_derivation["source_commit"],
        "selection_plan.seed_derivation.source_commit",
        length=40,
    )
    if seed != derive_selection_seed(source_commit):
        _fail("selection plan seed does not match its frozen source-commit derivation")
    if expected_source_commit is not None:
        expected_commit = _expect_sha(
            expected_source_commit,
            "expected Phase 9G-Prep merge commit",
            length=40,
        )
        if source_commit != expected_commit:
            _fail("selection plan is not anchored to the expected Phase 9G-Prep merge commit")
    window = _expect_dict(plan["selection_window"], "selection_plan.selection_window")
    _exact_keys(window, SELECTION_WINDOW_KEYS, "selection_plan.selection_window")
    start = parse_timestamp(window["start"], "selection_plan.selection_window.start")
    end = parse_timestamp(window["end"], "selection_plan.selection_window.end")
    if start >= end:
        _fail("selection window start must precede end")
    parse_timestamp(plan["generated_at"], "selection_plan.generated_at")
    synthetic = _expect_bool(plan["synthetic"], "selection_plan.synthetic")
    groups = _expect_list(plan["groups"], "selection_plan.groups")
    if not groups:
        _fail("selection plan must contain at least one group")
    seen: set[tuple[str, str, str]] = set()
    business_target = 0
    for index, raw_group in enumerate(groups):
        where = f"selection_plan.groups[{index}]"
        group = _expect_dict(raw_group, where)
        _exact_keys(group, SELECTION_GROUP_KEYS, where)
        track = _expect_enum(group["track"], {"business", "formal"}, f"{where}.track")
        role = _expect_enum(
            group["role"], {"pilot", "calibration", "reporting"}, f"{where}.role"
        )
        if (track, role) not in TRACK_ROLES:
            _fail(f"{where} has an invalid track/role pair")
        repository_id = _expect_identifier(
            group["repository_id"], f"{where}.repository_id"
        )
        identity = (track, role, repository_id)
        if identity in seen:
            _fail("selection plan repeats a track/role/repository group")
        seen.add(identity)
        target = _expect_int(group["target_prs"], f"{where}.target_prs", minimum=1)
        exclusions = _expect_list(group["exclusion_reasons"], f"{where}.exclusion_reasons")
        if not exclusions or any(not isinstance(value, str) or not value for value in exclusions):
            _fail(f"{where}.exclusion_reasons must be a non-empty string list")
        if len(exclusions) != len(set(exclusions)):
            _fail(f"{where}.exclusion_reasons contains duplicates")
        if track == "business":
            business_target += target
    if not 20 <= business_target <= 30:
        _fail("selection plan must target 20--30 business PRs")
    formal_reporting_repos = {
        repository for track, role, repository in seen if (track, role) == ("formal", "reporting")
    }
    formal_calibration_repos = {
        repository for track, role, repository in seen if (track, role) == ("formal", "calibration")
    }
    if formal_reporting_repos & formal_calibration_repos:
        _fail("formal calibration and reporting repositories must be disjoint")
    if not synthetic and formal_reporting_repos:
        formal_target = sum(
            group["target_prs"]
            for group in groups
            if group["track"] == "formal" and group["role"] == "reporting"
        )
        if len(formal_reporting_repos) < 3 or formal_target < 30:
            _fail("real formal reporting requires at least 30 PRs from 3 repositories")
    validate_artifact_hash(plan, "plan_sha256", "selection_plan")
    return plan


def validate_selection_log(
    raw_rows: Sequence[Any],
    plan: Mapping[str, Any],
    repositories: Mapping[str, Any],
) -> list[dict[str, Any]]:
    plan = validate_selection_plan(plan)
    repository_rows = {
        row["repository_id"]: row
        for row in _expect_list(repositories["repositories"], "repositories.repositories")
    }
    group_by_id = {
        (group["track"], group["role"], group["repository_id"]): group
        for group in plan["groups"]
    }
    candidates: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    seen_prs: set[tuple[str, str]] = set()
    start = parse_timestamp(plan["selection_window"]["start"], "selection start")
    end = parse_timestamp(plan["selection_window"]["end"], "selection end")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows):
        where = f"selection_log[{index}]"
        row = _expect_dict(raw, where)
        _exact_keys(row, SELECTION_ROW_KEYS, where)
        if row["schema_version"] != 1 or row["pilot_id"] != plan["pilot_id"]:
            _fail(f"{where} schema or pilot ID is invalid")
        track = _expect_enum(row["track"], {"business", "formal"}, f"{where}.track")
        role = _expect_enum(
            row["role"], {"pilot", "calibration", "reporting"}, f"{where}.role"
        )
        repository_id = _expect_identifier(row["repository_id"], f"{where}.repository_id")
        group_id = (track, role, repository_id)
        if group_id not in group_by_id:
            _fail(f"{where} is outside the selection plan")
        repository = repository_rows.get(repository_id)
        if repository is None:
            _fail(f"{where} references an unknown repository")
        required_track = (
            "business" if track == "business" else f"formal_{role}"
        )
        if required_track not in repository["allowed_tracks"]:
            _fail(f"{where} uses a repository not authorized for that track")
        pr_id = _expect_identifier(row["pr_id"], f"{where}.pr_id")
        pr_key = (track, pr_id)
        if pr_key in seen_prs:
            _fail("selection log repeats a PR within one evidence track")
        seen_prs.add(pr_key)
        merged_at = parse_timestamp(row["merged_at"], f"{where}.merged_at")
        if not start <= merged_at < end:
            _fail(f"{where}.merged_at is outside the selection window")
        eligible = _expect_bool(row["eligible"], f"{where}.eligible")
        selected = _expect_bool(row["selected"], f"{where}.selected")
        reason = row["exclusion_reason"]
        if eligible:
            if reason is not None:
                _fail(f"{where}.exclusion_reason must be null for eligible PRs")
        else:
            if reason not in group_by_id[group_id]["exclusion_reasons"]:
                _fail(f"{where}.exclusion_reason is not preregistered")
            if selected:
                _fail(f"{where} cannot select an ineligible PR")
        expected_rank = selection_rank(plan["seed"], pr_id)
        if _expect_sha(row["rank_sha256"], f"{where}.rank_sha256") != expected_rank:
            _fail(f"{where}.rank_sha256 does not match the frozen formula")
        for key in ("snapshot_sha256", "diff_sha256"):
            value = row[key]
            if value is None:
                if eligible:
                    _fail(f"{where}.{key} is required for an eligible PR")
            else:
                _expect_sha(value, f"{where}.{key}")
        if _expect_bool(row["synthetic"], f"{where}.synthetic") != plan["synthetic"]:
            _fail(f"{where} provenance differs from the plan")
        validate_artifact_hash(row, "row_sha256", where)
        candidates[group_id].append(row)
        rows.append(row)

    for group_id, group in group_by_id.items():
        eligible_rows = sorted(
            (row for row in candidates.get(group_id, []) if row["eligible"]),
            key=lambda row: (row["rank_sha256"], row["pr_id"]),
        )
        if len(eligible_rows) < group["target_prs"]:
            _fail("a selection group has too few eligible PRs")
        expected = {row["pr_id"] for row in eligible_rows[: group["target_prs"]]}
        declared = {row["pr_id"] for row in candidates[group_id] if row["selected"]}
        if expected != declared:
            _fail("a selection group does not select the frozen top ranks")

    business_prs = {row["pr_id"] for row in rows if row["track"] == "business" and row["selected"]}
    reporting_prs = {
        row["pr_id"]
        for row in rows
        if row["track"] == "formal" and row["role"] == "reporting" and row["selected"]
    }
    calibration_prs = {
        row["pr_id"]
        for row in rows
        if row["track"] == "formal" and row["role"] == "calibration" and row["selected"]
    }
    if reporting_prs & (business_prs | calibration_prs):
        _fail("formal reporting PRs must be sealed from business/calibration cohorts")
    selected_snapshots: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not row["selected"]:
            continue
        identity = (row["track"], row["pr_id"])
        previous = selected_snapshots.setdefault(row["snapshot_sha256"], identity)
        if previous != identity:
            _fail("selected cohorts reuse one immutable snapshot under multiple identities")
    return sorted(rows, key=lambda row: (row["track"], row["role"], row["repository_id"], row["rank_sha256"]))


def materialize_cohort(
    plan: Mapping[str, Any],
    selection_rows: Sequence[Any],
    repositories: Mapping[str, Any],
    *,
    materialized_at: str,
) -> dict[str, Any]:
    plan = validate_selection_plan(plan)
    rows = validate_selection_log(selection_rows, plan, repositories)
    parse_timestamp(materialized_at, "materialized_at")
    if parse_timestamp(materialized_at, "materialized_at") < parse_timestamp(
        plan["generated_at"], "selection_plan.generated_at"
    ):
        _fail("cohort materialization predates the selection plan")
    entries = [
        {
            "track": row["track"],
            "role": row["role"],
            "repository_id": row["repository_id"],
            "pr_id": row["pr_id"],
            "snapshot_sha256": row["snapshot_sha256"],
            "diff_sha256": row["diff_sha256"],
            "selected_at": materialized_at,
        }
        for row in rows
        if row["selected"]
    ]
    cohort = {
        "schema_version": 1,
        "phase_id": PHASE_ID,
        "pilot_id": plan["pilot_id"],
        "materialized_at": materialized_at,
        "selection_plan_sha256": plan["plan_sha256"],
        "selection_log_sha256": sha256_value(rows),
        "entries": entries,
        "synthetic": plan["synthetic"],
        "cohort_sha256": "",
    }
    return with_artifact_hash(cohort, "cohort_sha256")


def validate_cohort(
    raw: Any,
    plan: Mapping[str, Any],
    selection_rows: Sequence[Any],
    repositories: Mapping[str, Any],
) -> dict[str, Any]:
    cohort = _expect_dict(raw, "cohort")
    _exact_keys(cohort, COHORT_KEYS, "cohort")
    expected = materialize_cohort(
        plan,
        selection_rows,
        repositories,
        materialized_at=cohort["materialized_at"],
    )
    if cohort != expected:
        _fail("cohort does not match deterministic materialization")
    for index, entry in enumerate(cohort["entries"]):
        _exact_keys(_expect_dict(entry, f"cohort.entries[{index}]"), COHORT_ENTRY_KEYS, f"cohort.entries[{index}]")
    validate_artifact_hash(cohort, "cohort_sha256", "cohort")
    return cohort


FINDING_SUBJECT_KEYS = {
    "schema_version",
    "pilot_id",
    "pr_id",
    "review_id",
    "finding_id",
    "finding_sha256",
    "evidence_sha256",
    "feedback_eligible",
    "synthetic",
    "subject_sha256",
}
FEEDBACK_PACKET_KEYS = {
    "schema_version",
    "phase_id",
    "packet_id",
    "participant_id",
    "pr_id",
    "review_id",
    "generated_at",
    "finding_set_sha256",
    "items",
    "synthetic",
    "packet_sha256",
}
FEEDBACK_PACKET_ITEM_KEYS = {"finding_id", "finding_sha256", "evidence_sha256"}
FEEDBACK_RESPONSE_KEYS = {
    "schema_version",
    "packet_id",
    "packet_sha256",
    "participant_id",
    "pr_id",
    "finding_id",
    "decision",
    "rationale",
    "created_at",
    "fixed_at",
    "completed_by_human",
    "synthetic",
    "response_sha256",
}
REVIEW_TIME_KEYS = {
    "schema_version",
    "pilot_id",
    "session_id",
    "participant_id",
    "pr_id",
    "started_at",
    "completed_at",
    "active_seconds",
    "paused_seconds",
    "completed_by_human",
    "synthetic",
    "record_sha256",
}
RUN_RECEIPT_KEYS = {
    "schema_version",
    "pilot_id",
    "run_id",
    "track",
    "role",
    "pr_id",
    "attempt_number",
    "headline",
    "provider",
    "exact_model_snapshot",
    "temperature",
    "started_at",
    "completed_at",
    "status",
    "logical_calls",
    "http_attempts",
    "input_tokens",
    "output_tokens",
    "cost_microcny",
    "latency_seconds",
    "error_category",
    "feedback_eligible_finding_ids",
    "raw_trace_sha256",
    "raw_trace_retain_until",
    "synthetic",
    "receipt_sha256",
}
RUN_MANIFEST_KEYS = {
    "schema_version",
    "phase_id",
    "pilot_id",
    "created_at",
    "cohort_sha256",
    "receipt_set_sha256",
    "attempts",
    "synthetic",
    "manifest_sha256",
}
RUN_MANIFEST_ATTEMPT_KEYS = {
    "run_id",
    "track",
    "role",
    "pr_id",
    "attempt_number",
    "headline",
    "receipt_sha256",
}
BUSINESS_REPORT_KEYS = {
    "schema_version",
    "phase_id",
    "report_version",
    "pilot_id",
    "generated_at",
    "input_hashes",
    "business_outcome",
    "model_quality",
    "budget",
    "claim_gates",
    "synthetic",
    "report_sha256",
}
FEEDBACK_DECISIONS = {"accepted", "rejected", "uncertain", "fixed", "duplicate"}
RUN_STATUSES = {"completed", "degraded", "fail_open", "failed", "cancelled", "timed_out"}
ERROR_CATEGORIES = {
    "transient_network",
    "provider_5xx",
    "rate_limit",
    "authentication",
    "authorization",
    "schema_policy",
    "budget_exhausted",
    "external_command",
    "internal",
    "cancelled",
    "timeout",
}


def validate_finding_subjects(
    raw_rows: Sequence[Any], cohort: Mapping[str, Any]
) -> list[dict[str, Any]]:
    business_prs = {
        entry["pr_id"]
        for entry in cohort["entries"]
        if entry["track"] == "business"
    }
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_rows):
        where = f"finding_subjects[{index}]"
        row = _expect_dict(raw, where)
        _exact_keys(row, FINDING_SUBJECT_KEYS, where)
        if row["schema_version"] != 1 or row["pilot_id"] != cohort["pilot_id"]:
            _fail(f"{where} schema or pilot ID is invalid")
        pr_id = _expect_identifier(row["pr_id"], f"{where}.pr_id")
        if pr_id not in business_prs:
            _fail(f"{where} is outside the business cohort")
        _expect_identifier(row["review_id"], f"{where}.review_id")
        finding_id = _expect_identifier(row["finding_id"], f"{where}.finding_id")
        if finding_id in seen:
            _fail("finding subjects contain a duplicate finding ID")
        seen.add(finding_id)
        _expect_sha(row["finding_sha256"], f"{where}.finding_sha256")
        _expect_sha(row["evidence_sha256"], f"{where}.evidence_sha256")
        _expect_bool(row["feedback_eligible"], f"{where}.feedback_eligible")
        if _expect_bool(row["synthetic"], f"{where}.synthetic") != cohort["synthetic"]:
            _fail(f"{where} provenance differs from the cohort")
        validate_artifact_hash(row, "subject_sha256", where)
        rows.append(row)
    return sorted(rows, key=lambda row: (row["pr_id"], row["finding_id"]))


def build_feedback_packets(
    finding_subjects: Sequence[Any],
    cohort: Mapping[str, Any],
    participant_assignments: Mapping[str, str],
    *,
    generated_at: str,
) -> list[dict[str, Any]]:
    """Build identity/hash-only packets; no feedback decision is generated."""

    parse_timestamp(generated_at, "feedback packet generated_at")
    if parse_timestamp(generated_at, "feedback packet generated_at") < parse_timestamp(
        cohort["materialized_at"], "cohort.materialized_at"
    ):
        _fail("feedback packet generation predates cohort materialization")
    subjects = validate_finding_subjects(finding_subjects, cohort)
    by_pr: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for subject in subjects:
        if subject["feedback_eligible"]:
            by_pr[subject["pr_id"]].append(subject)
    business_prs = sorted(
        entry["pr_id"] for entry in cohort["entries"] if entry["track"] == "business"
    )
    if set(participant_assignments) != set(business_prs):
        _fail("feedback assignments must cover each business PR exactly once")
    packets = []
    for pr_id in business_prs:
        participant_id = _expect_identifier(
            participant_assignments[pr_id], "feedback assignment participant_id"
        )
        pr_subjects = sorted(by_pr.get(pr_id, []), key=lambda row: row["finding_id"])
        review_ids = {row["review_id"] for row in pr_subjects}
        if len(review_ids) > 1:
            _fail("a business PR has findings from multiple review identities")
        review_id = next(iter(review_ids), f"no-findings:{pr_id}")
        items = [
            {
                "finding_id": row["finding_id"],
                "finding_sha256": row["finding_sha256"],
                "evidence_sha256": row["evidence_sha256"],
            }
            for row in pr_subjects
        ]
        finding_set_sha256 = sha256_value(items)
        packet = {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "packet_id": f"feedback-{sha256_value([cohort['pilot_id'], participant_id, pr_id, finding_set_sha256])[:24]}",
            "participant_id": participant_id,
            "pr_id": pr_id,
            "review_id": review_id,
            "generated_at": generated_at,
            "finding_set_sha256": finding_set_sha256,
            "items": items,
            "synthetic": cohort["synthetic"],
            "packet_sha256": "",
        }
        packets.append(with_artifact_hash(packet, "packet_sha256"))
    return packets


def validate_feedback_packet(
    raw: Any,
    finding_subjects: Sequence[Any],
    cohort: Mapping[str, Any],
) -> dict[str, Any]:
    packet = _expect_dict(raw, "feedback_packet")
    _exact_keys(packet, FEEDBACK_PACKET_KEYS, "feedback_packet")
    if packet["schema_version"] != 1 or packet["phase_id"] != PHASE_ID:
        _fail("feedback packet schema or phase is invalid")
    _expect_identifier(packet["packet_id"], "feedback_packet.packet_id")
    _expect_identifier(packet["participant_id"], "feedback_packet.participant_id")
    pr_id = _expect_identifier(packet["pr_id"], "feedback_packet.pr_id")
    _expect_identifier(packet["review_id"], "feedback_packet.review_id")
    parse_timestamp(packet["generated_at"], "feedback_packet.generated_at")
    _expect_sha(packet["finding_set_sha256"], "feedback_packet.finding_set_sha256")
    items = _expect_list(packet["items"], "feedback_packet.items")
    subject_by_id = {
        row["finding_id"]: row
        for row in validate_finding_subjects(finding_subjects, cohort)
        if row["pr_id"] == pr_id and row["feedback_eligible"]
    }
    seen: set[str] = set()
    for index, raw_item in enumerate(items):
        where = f"feedback_packet.items[{index}]"
        item = _expect_dict(raw_item, where)
        _exact_keys(item, FEEDBACK_PACKET_ITEM_KEYS, where)
        finding_id = _expect_identifier(item["finding_id"], f"{where}.finding_id")
        if finding_id in seen or finding_id not in subject_by_id:
            _fail(f"{where}.finding_id is duplicate or foreign")
        seen.add(finding_id)
        subject = subject_by_id[finding_id]
        if item["finding_sha256"] != subject["finding_sha256"]:
            _fail(f"{where}.finding_sha256 is stale")
        if item["evidence_sha256"] != subject["evidence_sha256"]:
            _fail(f"{where}.evidence_sha256 is stale")
    if seen != set(subject_by_id):
        _fail("feedback packet does not cover every feedback-eligible Finding")
    if packet["finding_set_sha256"] != sha256_value(items):
        _fail("feedback packet finding-set hash is stale")
    if _expect_bool(packet["synthetic"], "feedback_packet.synthetic") != cohort["synthetic"]:
        _fail("feedback packet provenance differs from cohort")
    validate_artifact_hash(packet, "packet_sha256", "feedback_packet")
    return packet


def validate_feedback_responses(
    raw_rows: Sequence[Any],
    packets: Sequence[Mapping[str, Any]],
    finding_subjects: Sequence[Any],
    cohort: Mapping[str, Any],
) -> list[dict[str, Any]]:
    packet_by_id = {
        packet["packet_id"]: validate_feedback_packet(packet, finding_subjects, cohort)
        for packet in packets
    }
    expected = {
        (packet["packet_id"], item["finding_id"])
        for packet in packet_by_id.values()
        for item in packet["items"]
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_rows):
        where = f"feedback_responses[{index}]"
        row = _expect_dict(raw, where)
        _exact_keys(row, FEEDBACK_RESPONSE_KEYS, where)
        if row["schema_version"] != 1:
            _fail(f"{where}.schema_version must be 1")
        packet_id = _expect_identifier(row["packet_id"], f"{where}.packet_id")
        packet = packet_by_id.get(packet_id)
        if packet is None:
            _fail(f"{where} references a foreign packet")
        if row["packet_sha256"] != packet["packet_sha256"]:
            _fail(f"{where}.packet_sha256 is stale")
        if row["participant_id"] != packet["participant_id"] or row["pr_id"] != packet["pr_id"]:
            _fail(f"{where} changes the packet participant or PR binding")
        finding_id = _expect_identifier(row["finding_id"], f"{where}.finding_id")
        identity = (packet_id, finding_id)
        if identity in seen or identity not in expected:
            _fail(f"{where}.finding_id is duplicate or foreign")
        seen.add(identity)
        decision = _expect_enum(row["decision"], FEEDBACK_DECISIONS, f"{where}.decision")
        rationale = _expect_nullable_str(row["rationale"], f"{where}.rationale", maximum=4000)
        if decision in {"rejected", "uncertain", "duplicate"} and rationale is None:
            _fail(f"{where} requires a rationale for {decision}")
        created_at = parse_timestamp(row["created_at"], f"{where}.created_at")
        if created_at < parse_timestamp(packet["generated_at"], "feedback packet generated_at"):
            _fail(f"{where}.created_at predates its packet")
        fixed_at = row["fixed_at"]
        if decision == "fixed":
            fixed = parse_timestamp(fixed_at, f"{where}.fixed_at")
            if fixed < created_at:
                _fail(f"{where}.fixed_at precedes feedback creation")
        elif fixed_at is not None:
            _fail(f"{where}.fixed_at is only valid for fixed feedback")
        human = _expect_bool(row["completed_by_human"], f"{where}.completed_by_human")
        synthetic = _expect_bool(row["synthetic"], f"{where}.synthetic")
        if synthetic != packet["synthetic"]:
            _fail(f"{where} provenance differs from packet")
        if not synthetic and not human:
            _fail(f"{where} real feedback must be completed by a human")
        validate_artifact_hash(row, "response_sha256", where)
        rows.append(row)
    # A missing human response is a business outcome, not malformed evidence.
    # Keep it absent so the report counts it in the full packet denominator and
    # closes the claim gate; never synthesize a replacement response.
    return sorted(rows, key=lambda row: (row["pr_id"], row["finding_id"]))


def validate_feedback_packet_assignments(
    packets: Sequence[Mapping[str, Any]],
    participants: Mapping[str, Any],
    cohort: Mapping[str, Any],
) -> None:
    business_repository_by_pr = {
        entry["pr_id"]: entry["repository_id"]
        for entry in cohort["entries"]
        if entry["track"] == "business"
    }
    participant_by_id = {
        row["participant_id"]: row for row in participants["participants"]
    }
    seen_prs: set[str] = set()
    for packet in packets:
        pr_id = packet["pr_id"]
        if pr_id in seen_prs or pr_id not in business_repository_by_pr:
            _fail("feedback packets repeat or reference a foreign business PR")
        seen_prs.add(pr_id)
        participant = participant_by_id.get(packet["participant_id"])
        if participant is None:
            _fail("feedback packet references an unknown participant")
        if business_repository_by_pr[pr_id] not in participant["repository_ids"]:
            _fail("feedback packet exceeds the participant repository consent scope")
    if seen_prs != set(business_repository_by_pr):
        _fail("feedback packets must cover every business PR exactly once")


def validate_review_times(
    raw_rows: Sequence[Any],
    cohort: Mapping[str, Any],
    participants: Mapping[str, Any],
) -> list[dict[str, Any]]:
    business_prs = {
        entry["pr_id"] for entry in cohort["entries"] if entry["track"] == "business"
    }
    participant_ids = {row["participant_id"] for row in participants["participants"]}
    participant_by_id = {
        row["participant_id"]: row for row in participants["participants"]
    }
    business_repository_by_pr = {
        entry["pr_id"]: entry["repository_id"]
        for entry in cohort["entries"]
        if entry["track"] == "business"
    }
    rows: list[dict[str, Any]] = []
    seen_sessions: set[str] = set()
    prs_seen: set[str] = set()
    for index, raw in enumerate(raw_rows):
        where = f"review_times[{index}]"
        row = _expect_dict(raw, where)
        _exact_keys(row, REVIEW_TIME_KEYS, where)
        if row["schema_version"] != 1 or row["pilot_id"] != cohort["pilot_id"]:
            _fail(f"{where} schema or pilot ID is invalid")
        session_id = _expect_identifier(row["session_id"], f"{where}.session_id")
        if session_id in seen_sessions:
            _fail("review-time records repeat a session ID")
        seen_sessions.add(session_id)
        participant_id = _expect_identifier(row["participant_id"], f"{where}.participant_id")
        if participant_id not in participant_ids:
            _fail(f"{where} references an unknown participant")
        pr_id = _expect_identifier(row["pr_id"], f"{where}.pr_id")
        if pr_id not in business_prs:
            _fail(f"{where} is outside the business cohort")
        if business_repository_by_pr[pr_id] not in participant_by_id[participant_id][
            "repository_ids"
        ]:
            _fail(f"{where} exceeds the participant repository consent scope")
        if pr_id in prs_seen:
            _fail("each business PR requires one consolidated review-time record")
        prs_seen.add(pr_id)
        started = parse_timestamp(row["started_at"], f"{where}.started_at")
        completed = parse_timestamp(row["completed_at"], f"{where}.completed_at")
        if completed < started:
            _fail(f"{where}.completed_at precedes started_at")
        if started < parse_timestamp(cohort["materialized_at"], "cohort.materialized_at"):
            _fail(f"{where}.started_at predates cohort materialization")
        active = _expect_finite_number(row["active_seconds"], f"{where}.active_seconds")
        paused = _expect_finite_number(row["paused_seconds"], f"{where}.paused_seconds")
        elapsed = (completed - started).total_seconds()
        if active + paused > elapsed + 1.0:
            _fail(f"{where} active plus paused time exceeds wall time")
        human = _expect_bool(row["completed_by_human"], f"{where}.completed_by_human")
        synthetic = _expect_bool(row["synthetic"], f"{where}.synthetic")
        if synthetic != cohort["synthetic"]:
            _fail(f"{where} provenance differs from cohort")
        if not synthetic and not human:
            _fail(f"{where} real review time must be recorded by a human")
        validate_artifact_hash(row, "record_sha256", where)
        rows.append(row)
    if prs_seen != business_prs:
        _fail("review-time records must cover every business PR exactly once")
    return sorted(rows, key=lambda row: row["pr_id"])


def validate_run_receipts(
    raw_rows: Sequence[Any],
    cohort: Mapping[str, Any],
    authorization: Mapping[str, Any],
) -> list[dict[str, Any]]:
    cohort_entry_by_key = {
        (entry["track"], entry["pr_id"]): entry for entry in cohort["entries"]
    }
    model = authorization["model"]
    rows: list[dict[str, Any]] = []
    run_ids: set[str] = set()
    attempts: set[tuple[str, str, int]] = set()
    totals: Counter[str] = Counter()
    for index, raw in enumerate(raw_rows):
        where = f"run_receipts[{index}]"
        row = _expect_dict(raw, where)
        _exact_keys(row, RUN_RECEIPT_KEYS, where)
        if row["schema_version"] != 1 or row["pilot_id"] != cohort["pilot_id"]:
            _fail(f"{where} schema or pilot ID is invalid")
        run_id = _expect_identifier(row["run_id"], f"{where}.run_id")
        if run_id in run_ids:
            _fail("run receipts contain a duplicate run ID")
        run_ids.add(run_id)
        track = _expect_enum(row["track"], {"business", "formal"}, f"{where}.track")
        role = _expect_enum(
            row["role"], {"pilot", "calibration", "reporting"}, f"{where}.role"
        )
        if (track, role) not in TRACK_ROLES:
            _fail(f"{where} has an invalid track/role pair")
        pr_id = _expect_identifier(row["pr_id"], f"{where}.pr_id")
        cohort_entry = cohort_entry_by_key.get((track, pr_id))
        if cohort_entry is None or cohort_entry["role"] != role:
            _fail(f"{where} is outside its declared cohort track/role")
        attempt = _expect_int(row["attempt_number"], f"{where}.attempt_number", minimum=1)
        if (track, pr_id, attempt) in attempts:
            _fail("run receipts repeat a track/PR attempt number")
        attempts.add((track, pr_id, attempt))
        _expect_bool(row["headline"], f"{where}.headline")
        if row["provider"] != model["provider"] or row["exact_model_snapshot"] != model[
            "exact_model_snapshot"
        ]:
            _fail(f"{where} provider/model differs from authorization")
        temperature = _expect_finite_number(row["temperature"], f"{where}.temperature")
        if not math.isclose(temperature, float(model["temperature"]), abs_tol=1e-12):
            _fail(f"{where}.temperature differs from authorization")
        started = parse_timestamp(row["started_at"], f"{where}.started_at")
        completed = parse_timestamp(row["completed_at"], f"{where}.completed_at")
        if completed < started:
            _fail(f"{where}.completed_at precedes started_at")
        if started < parse_timestamp(cohort["materialized_at"], "cohort.materialized_at"):
            _fail(f"{where}.started_at predates cohort materialization")
        status = _expect_enum(row["status"], RUN_STATUSES, f"{where}.status")
        logical = _expect_int(row["logical_calls"], f"{where}.logical_calls")
        http = _expect_int(row["http_attempts"], f"{where}.http_attempts")
        if http < logical:
            _fail(f"{where}.http_attempts cannot be below logical_calls")
        input_tokens = _expect_int(row["input_tokens"], f"{where}.input_tokens")
        output_tokens = _expect_int(row["output_tokens"], f"{where}.output_tokens")
        cost = _expect_int(row["cost_microcny"], f"{where}.cost_microcny")
        latency = _expect_finite_number(row["latency_seconds"], f"{where}.latency_seconds")
        if latency > (completed - started).total_seconds() + 1.0:
            _fail(f"{where}.latency_seconds exceeds its timestamp duration")
        error = row["error_category"]
        if status in {"completed", "degraded", "fail_open"}:
            if error is not None:
                _fail(f"{where}.error_category must be null for a completed outcome")
        else:
            _expect_enum(error, ERROR_CATEGORIES, f"{where}.error_category")
        _unique_identifiers(
            row["feedback_eligible_finding_ids"],
            f"{where}.feedback_eligible_finding_ids",
        )
        _expect_sha(row["raw_trace_sha256"], f"{where}.raw_trace_sha256")
        retain_until = parse_timestamp(
            row["raw_trace_retain_until"], f"{where}.raw_trace_retain_until"
        )
        if retain_until <= completed:
            _fail(f"{where}.raw_trace_retain_until must follow completion")
        minimum_retention_seconds = model["raw_trace_retention_days"] * 86400
        if (retain_until - completed).total_seconds() < minimum_retention_seconds:
            _fail(f"{where}.raw_trace_retain_until is below authorized retention")
        if _expect_bool(row["synthetic"], f"{where}.synthetic") != cohort["synthetic"]:
            _fail(f"{where} provenance differs from cohort")
        validate_artifact_hash(row, "receipt_sha256", where)
        totals.update(
            logical_calls=logical,
            http_attempts=http,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microcny=cost,
        )
        rows.append(row)
    limits = {
        "logical_calls": model["max_logical_calls"],
        "http_attempts": model["max_http_attempts"],
        "input_tokens": model["max_input_tokens"],
        "output_tokens": model["max_output_tokens"],
        "cost_microcny": model["max_cost_microcny"],
    }
    for key, limit in limits.items():
        if totals[key] > limit:
            _fail(f"run receipts exceed authorized aggregate {key}")
    return sorted(
        rows,
        key=lambda row: (row["track"], row["role"], row["pr_id"], row["attempt_number"]),
    )


def validate_receipt_finding_bindings(
    receipts: Sequence[Mapping[str, Any]],
    finding_subjects: Sequence[Mapping[str, Any]],
) -> None:
    eligible_by_pr: dict[str, set[str]] = defaultdict(set)
    for subject in finding_subjects:
        if subject["feedback_eligible"]:
            eligible_by_pr[subject["pr_id"]].add(subject["finding_id"])
    for receipt in receipts:
        if receipt["track"] != "business":
            if receipt["feedback_eligible_finding_ids"]:
                _fail("formal run receipts cannot enter the business feedback denominator")
            continue
        declared = set(receipt["feedback_eligible_finding_ids"])
        expected = eligible_by_pr.get(receipt["pr_id"], set())
        if receipt["headline"]:
            if declared != expected:
                _fail(
                    "a headline receipt does not bind the complete feedback-eligible "
                    "Finding set"
                )
        elif not declared <= expected:
            _fail(
                "a non-headline receipt references a foreign Finding"
            )


def build_run_manifest(
    receipts: Sequence[Mapping[str, Any]],
    cohort: Mapping[str, Any],
    *,
    created_at: str,
) -> dict[str, Any]:
    parse_timestamp(created_at, "run manifest created_at")
    attempts = [
        {
            "run_id": receipt["run_id"],
            "track": receipt["track"],
            "role": receipt["role"],
            "pr_id": receipt["pr_id"],
            "attempt_number": receipt["attempt_number"],
            "headline": receipt["headline"],
            "receipt_sha256": receipt["receipt_sha256"],
        }
        for receipt in sorted(
            receipts,
            key=lambda row: (
                row["track"],
                row["role"],
                row["pr_id"],
                row["attempt_number"],
            ),
        )
    ]
    manifest = {
        "schema_version": 1,
        "phase_id": PHASE_ID,
        "pilot_id": cohort["pilot_id"],
        "created_at": created_at,
        "cohort_sha256": cohort["cohort_sha256"],
        "receipt_set_sha256": sha256_value(
            sorted(row["receipt_sha256"] for row in receipts)
        ),
        "attempts": attempts,
        "synthetic": cohort["synthetic"],
        "manifest_sha256": "",
    }
    return with_artifact_hash(manifest, "manifest_sha256")


def validate_run_manifest(
    raw: Any,
    receipts: Sequence[Mapping[str, Any]],
    cohort: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = _expect_dict(raw, "run_manifest")
    _exact_keys(manifest, RUN_MANIFEST_KEYS, "run_manifest")
    if manifest["schema_version"] != 1 or manifest["phase_id"] != PHASE_ID:
        _fail("run manifest schema or phase is invalid")
    expected = build_run_manifest(receipts, cohort, created_at=manifest["created_at"])
    if manifest != expected:
        _fail("run manifest does not bind the complete receipt set")
    cohort_keys = {(entry["track"], entry["pr_id"]) for entry in cohort["entries"]}
    by_pr: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for index, raw_attempt in enumerate(manifest["attempts"]):
        attempt = _expect_dict(raw_attempt, f"run_manifest.attempts[{index}]")
        _exact_keys(attempt, RUN_MANIFEST_ATTEMPT_KEYS, f"run_manifest.attempts[{index}]")
        by_pr[(attempt["track"], attempt["pr_id"])].append(attempt)
    if set(by_pr) != cohort_keys:
        _fail("run manifest must include every selected cohort PR/track")
    for (track, pr_id), attempts in by_pr.items():
        ordered = sorted(attempts, key=lambda row: row["attempt_number"])
        if [row["attempt_number"] for row in ordered] != list(range(1, len(ordered) + 1)):
            _fail(
                "run manifest has a missing or non-contiguous attempt"
            )
        headlines = [row for row in ordered if row["headline"]]
        if len(headlines) != 1 or headlines[0]["attempt_number"] != 1:
            _fail(
                "the immutable first attempt must be the sole headline"
            )
    validate_artifact_hash(manifest, "manifest_sha256", "run_manifest")
    return manifest


def _safe_div(numerator: int | float, denominator: int | float) -> float | None:
    return None if denominator == 0 else round(float(numerator) / float(denominator), 6)


def _percentile(values: Sequence[int | float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 6)
    weight = position - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 6)


def _distribution(values: Sequence[int | float], *, total: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "count": len(values),
        "mean": round(statistics.fmean(values), 6) if values else None,
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
    }
    if total:
        result["total"] = sum(values)
    return result


def build_business_report(
    *,
    authorization: Mapping[str, Any],
    participants: Mapping[str, Any],
    repositories: Mapping[str, Any],
    cohort: Mapping[str, Any],
    finding_subjects: Sequence[Mapping[str, Any]],
    feedback_packets: Sequence[Mapping[str, Any]],
    feedback_responses: Sequence[Mapping[str, Any]],
    review_times: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
    run_manifest: Mapping[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    parse_timestamp(generated_at, "business report generated_at")
    readiness = authorization_readiness(authorization)
    business_prs = sorted(
        entry["pr_id"] for entry in cohort["entries"] if entry["track"] == "business"
    )
    receipt_by_id = {row["run_id"]: row for row in receipts}
    headline_receipts = [
        receipt_by_id[attempt["run_id"]]
        for attempt in run_manifest["attempts"]
        if attempt["headline"] and attempt["track"] == "business"
    ]
    status_counts = Counter(row["status"] for row in headline_receipts)
    completed = sum(
        status in {"completed", "degraded", "fail_open"}
        for status in (row["status"] for row in headline_receipts)
    )
    eligible_findings = {
        row["finding_id"] for row in finding_subjects if row["feedback_eligible"]
    }
    feedback_by_finding = {row["finding_id"]: row for row in feedback_responses}
    adopted = {
        finding_id
        for finding_id, row in feedback_by_finding.items()
        if row["decision"] in {"accepted", "fixed"}
    }
    fixed = {
        finding_id for finding_id, row in feedback_by_finding.items() if row["decision"] == "fixed"
    }
    active_seconds = [row["active_seconds"] for row in review_times]
    headline_latencies = [row["latency_seconds"] for row in headline_receipts]
    business_receipts = [row for row in receipts if row["track"] == "business"]
    all_costs = [row["cost_microcny"] for row in business_receipts]
    retry_attempts = max(0, len(business_receipts) - len(headline_receipts))
    missing_feedback = eligible_findings - set(feedback_by_finding)
    common_synthetic = any(
        [
            authorization["synthetic"],
            participants["synthetic"],
            repositories["synthetic"],
            cohort["synthetic"],
            run_manifest["synthetic"],
            any(row["synthetic"] for row in finding_subjects),
            any(row["synthetic"] for row in feedback_packets),
            any(row["synthetic"] for row in feedback_responses),
            any(row["synthetic"] for row in review_times),
            any(row["synthetic"] for row in receipts),
        ]
    )
    claim_reasons = list(readiness["scopes"]["business"]["blocked_by"])
    claim_reasons.extend(readiness["scopes"]["model"]["blocked_by"])
    if common_synthetic and "synthetic_evidence" not in claim_reasons:
        claim_reasons.append("synthetic_evidence")
    if len(business_prs) != authorization["business_pilot"]["pr_count"]:
        claim_reasons.append("cohort_pr_count_differs_from_authorization")
    if len(headline_receipts) != len(business_prs):
        claim_reasons.append("headline_receipt_coverage_incomplete")
    if len(review_times) != len(business_prs):
        claim_reasons.append("review_time_coverage_incomplete")
    if missing_feedback:
        claim_reasons.append("feedback_coverage_incomplete")
    claim_reasons = sorted(set(claim_reasons))

    total_cost = sum(all_costs)
    report = {
        "schema_version": 1,
        "phase_id": PHASE_ID,
        "report_version": REPORT_VERSION,
        "pilot_id": cohort["pilot_id"],
        "generated_at": generated_at,
        "input_hashes": {
            "authorization_sha256": authorization["authorization_sha256"],
            "participants_sha256": participants["manifest_sha256"],
            "repositories_sha256": repositories["manifest_sha256"],
            "cohort_sha256": cohort["cohort_sha256"],
            "finding_subjects_sha256": sha256_value(
                [row["subject_sha256"] for row in finding_subjects]
            ),
            "feedback_packets_sha256": sha256_value(
                [row["packet_sha256"] for row in feedback_packets]
            ),
            "feedback_responses_sha256": sha256_value(
                [row["response_sha256"] for row in feedback_responses]
            ),
            "review_times_sha256": sha256_value(
                [row["record_sha256"] for row in review_times]
            ),
            "run_manifest_sha256": run_manifest["manifest_sha256"],
        },
        "business_outcome": {
            "selected_prs": len(business_prs),
            "participants": len(participants["participants"]),
            "completion": {
                "numerator": completed,
                "denominator": len(business_prs),
                "rate": _safe_div(completed, len(business_prs)),
            },
            "feedback_coverage": {
                "numerator": len(set(feedback_by_finding) & eligible_findings),
                "denominator": len(eligible_findings),
                "missing": len(missing_feedback),
                "rate": _safe_div(
                    len(set(feedback_by_finding) & eligible_findings), len(eligible_findings)
                ),
            },
            "adoption": {
                "numerator": len(adopted),
                "denominator": len(eligible_findings),
                "rate": _safe_div(len(adopted), len(eligible_findings)),
            },
            "fixed": {
                "numerator": len(fixed),
                "denominator": len(eligible_findings),
                "rate": _safe_div(len(fixed), len(eligible_findings)),
            },
            "feedback_decisions": dict(
                sorted(Counter(row["decision"] for row in feedback_responses).items())
            ),
            "review_time_active_seconds": _distribution(active_seconds),
            "headline_latency_seconds": _distribution(headline_latencies),
            "headline_status_counts": {
                status: status_counts.get(status, 0) for status in sorted(RUN_STATUSES)
            },
            "retry_attempts": retry_attempts,
            "error_categories": dict(
                sorted(
                    Counter(
                        row["error_category"]
                        for row in business_receipts
                        if row["error_category"] is not None
                    ).items()
                )
            ),
        },
        "model_quality": {
            "measured": False,
            "precision": None,
            "recall": None,
            "f1": None,
            "reason": "business_feedback_is_not_independent_double_annotated_gold",
        },
        "budget": {
            "logical_calls": sum(row["logical_calls"] for row in business_receipts),
            "http_attempts": sum(row["http_attempts"] for row in business_receipts),
            "input_tokens": sum(row["input_tokens"] for row in business_receipts),
            "output_tokens": sum(row["output_tokens"] for row in business_receipts),
            "cost_microcny": {
                **_distribution(all_costs, total=True),
                "per_selected_pr": _safe_div(total_cost, len(business_prs)),
                "per_adopted_finding": _safe_div(total_cost, len(adopted)),
            },
        },
        "claim_gates": {
            "business_claim_allowed": not claim_reasons,
            "blocked_by": claim_reasons,
            "formal_quality_claim_allowed": False,
            "formal_quality_blocked_by": ["business_report_is_not_a_formal_quality_report"],
        },
        "synthetic": common_synthetic,
        "report_sha256": "",
    }
    return with_artifact_hash(report, "report_sha256")


def validate_business_report(
    raw: Any,
    **inputs: Any,
) -> dict[str, Any]:
    report = _expect_dict(raw, "business_report")
    _exact_keys(report, BUSINESS_REPORT_KEYS, "business_report")
    expected = build_business_report(generated_at=report["generated_at"], **inputs)
    if report != expected:
        _fail("business report does not match its immutable inputs")
    validate_artifact_hash(report, "report_sha256", "business_report")
    return report


ANNOTATION_SUBJECT_KEYS = {
    "schema_version",
    "pilot_id",
    "stage",
    "subject_kind",
    "subject_id",
    "pr_id",
    "subject_sha256",
    "evidence_sha256",
    "severity",
    "synthetic",
    "record_sha256",
}
ANNOTATION_PACKET_KEYS = {
    "schema_version",
    "phase_id",
    "packet_id",
    "mode",
    "stage",
    "annotator_id",
    "generated_at",
    "rubric_sha256",
    "cohort_sha256",
    "order_seed",
    "subject_set_sha256",
    "items",
    "synthetic",
    "packet_sha256",
}
ANNOTATION_PACKET_ITEM_KEYS = {
    "subject_id",
    "subject_kind",
    "pr_id",
    "subject_sha256",
    "evidence_sha256",
    "severity",
    "source_annotations",
}
SOURCE_ANNOTATION_KEYS = {
    "response_sha256",
    "annotator_id",
    "label",
    "gold_id",
}
ANNOTATION_RESPONSE_KEYS = {
    "schema_version",
    "packet_id",
    "packet_sha256",
    "annotator_id",
    "subject_id",
    "label",
    "gold_id",
    "discovered",
    "severity",
    "rationale",
    "evidence_sha256",
    "created_at",
    "completed_by_human",
    "synthetic",
    "response_sha256",
}
GOLD_FREEZE_KEYS = {
    "schema_version",
    "phase_id",
    "pilot_id",
    "cohort_sha256",
    "trusted_cohort_sha256",
    "rubric_sha256",
    "packet_a_sha256",
    "packet_b_sha256",
    "adjudication_packet_sha256",
    "annotation_set_sha256",
    "custodian_id",
    "frozen_at",
    "external_git_commit",
    "synthetic",
    "real_run_ready",
    "quality_claim_allowed",
    "incomplete_gates",
    "freeze_sha256",
}
TRUSTED_REPORT_KEYS = {
    "schema_version",
    "metric_version",
    "generated_at",
    "cohort_id",
    "config_id",
    "split",
    "source_commits",
    "gold_freeze_commit",
    "frozen_cohort_sha256",
    "provider",
    "model_id",
    "pricing_revision",
    "runtime_config_sha256",
    "input_hashes",
    "agreement",
    "review",
    "bootstrap_95_ci",
    "telemetry",
}
TRUSTED_REPORT_INPUT_HASH_KEYS = {
    "annotations_sha256",
    "cohort_sha256",
    "runs_sha256",
    "selection_log_sha256",
}
TRUSTED_AGREEMENT_KEYS = {
    "annotators",
    "adjudicators",
    "overall",
    "by_subject_kind",
    "by_repository",
}
TRUSTED_AGREEMENT_BLOCK_KEYS = {
    "subjects",
    "exact_agreements",
    "exact_agreement_rate",
    "cohen_kappa",
    "cohen_kappa_reason",
    "contingency",
    "arbitrated_subjects",
    "arbitration_rate",
    "unresolved_subjects",
    "malformed_subjects",
    "invalid_subject_policy",
    "discovery",
    "severity",
}
TRUSTED_REVIEW_KEYS = {
    "micro",
    "repository_macro",
    "pr_macro",
    "by_repository",
    "per_pr",
}
TRUSTED_COUNT_KEYS = {
    "tp_findings",
    "fp_findings",
    "tp_gold",
    "fn_gold",
    "novel_valid",
    "duplicates",
    "unscorable",
}
TRUSTED_MICRO_KEYS = TRUSTED_COUNT_KEYS | {"precision", "recall", "f1"}
TRUSTED_BOOTSTRAP_KEYS = {
    "method",
    "seed",
    "replicates",
    "alpha",
    "precision",
    "recall",
    "f1",
}
TRUSTED_BOOTSTRAP_INTERVAL_KEYS = {
    "low",
    "high",
    "defined_replicates",
    "reason",
}
GOLD_LABELS = {"valid_defect", "not_defect", "uncertain"}
SYSTEM_LABELS = {
    "matched",
    "novel_valid",
    "invalid",
    "duplicate",
    "unscorable",
    "uncertain",
}
SEVERITIES = {"low", "medium", "high"}


def validate_annotation_subjects(
    raw_rows: Sequence[Any], cohort: Mapping[str, Any]
) -> list[dict[str, Any]]:
    cohort_prs = {entry["pr_id"] for entry in cohort["entries"]}
    formal_reporting_prs = {
        entry["pr_id"]
        for entry in cohort["entries"]
        if entry["track"] == "formal" and entry["role"] == "reporting"
    }
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_rows):
        where = f"annotation_subjects[{index}]"
        row = _expect_dict(raw, where)
        _exact_keys(row, ANNOTATION_SUBJECT_KEYS, where)
        if row["schema_version"] != 1 or row["pilot_id"] != cohort["pilot_id"]:
            _fail(f"{where} schema or pilot ID is invalid")
        stage = _expect_enum(row["stage"], {"gold", "system"}, f"{where}.stage")
        expected_kind = "gold_candidate" if stage == "gold" else "system_finding"
        if row["subject_kind"] != expected_kind:
            _fail(f"{where}.subject_kind does not match its stage")
        subject_id = _expect_identifier(row["subject_id"], f"{where}.subject_id")
        if subject_id in seen:
            _fail("annotation subjects contain a duplicate subject ID")
        seen.add(subject_id)
        pr_id = _expect_identifier(row["pr_id"], f"{where}.pr_id")
        if pr_id not in cohort_prs:
            _fail(f"{where} references a PR outside the cohort")
        if not cohort["synthetic"] and pr_id not in formal_reporting_prs:
            _fail(f"{where} real formal annotation is outside the reporting cohort")
        _expect_sha(row["subject_sha256"], f"{where}.subject_sha256")
        _expect_sha(row["evidence_sha256"], f"{where}.evidence_sha256")
        if stage == "gold":
            _expect_enum(row["severity"], SEVERITIES, f"{where}.severity")
        elif row["severity"] is not None:
            _fail(f"{where}.severity must be null for system Findings")
        if _expect_bool(row["synthetic"], f"{where}.synthetic") != cohort["synthetic"]:
            _fail(f"{where} provenance differs from cohort")
        validate_artifact_hash(row, "record_sha256", where)
        rows.append(row)
    return sorted(rows, key=lambda row: row["subject_id"])


def _annotation_packet_item(
    subject: Mapping[str, Any],
    source_annotations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "subject_id": subject["subject_id"],
        "subject_kind": subject["subject_kind"],
        "pr_id": subject["pr_id"],
        "subject_sha256": subject["subject_sha256"],
        "evidence_sha256": subject["evidence_sha256"],
        "severity": subject["severity"],
        "source_annotations": [
            {
                "response_sha256": row["response_sha256"],
                "annotator_id": row["annotator_id"],
                "label": row["label"],
                "gold_id": row["gold_id"],
            }
            for row in source_annotations
        ],
    }


def build_independent_annotation_packet(
    subjects: Sequence[Mapping[str, Any]],
    cohort: Mapping[str, Any],
    *,
    stage: str,
    annotator_id: str,
    rubric_sha256: str,
    order_seed: int,
    generated_at: str,
) -> dict[str, Any]:
    _expect_enum(stage, {"gold", "system"}, "annotation packet stage")
    annotator = _expect_identifier(annotator_id, "annotation packet annotator_id")
    rubric = _expect_sha(rubric_sha256, "annotation packet rubric_sha256")
    if isinstance(order_seed, bool) or not isinstance(order_seed, int):
        _fail("annotation packet order_seed must be an integer")
    parse_timestamp(generated_at, "annotation packet generated_at")
    if parse_timestamp(generated_at, "annotation packet generated_at") < parse_timestamp(
        cohort["materialized_at"], "cohort.materialized_at"
    ):
        _fail("annotation packet generation predates cohort materialization")
    selected = [row for row in subjects if row["stage"] == stage]
    if not selected:
        _fail("cannot build an annotation packet without stage subjects")
    ordered = sorted(selected, key=lambda row: row["subject_id"])
    random.Random(order_seed).shuffle(ordered)
    subject_set_sha256 = sha256_value(
        sorted((row["subject_id"], row["record_sha256"]) for row in selected)
    )
    packet = {
        "schema_version": 1,
        "phase_id": PHASE_ID,
        "packet_id": f"annotation-{sha256_value([stage, annotator, subject_set_sha256, order_seed])[:24]}",
        "mode": "independent",
        "stage": stage,
        "annotator_id": annotator,
        "generated_at": generated_at,
        "rubric_sha256": rubric,
        "cohort_sha256": cohort["cohort_sha256"],
        "order_seed": order_seed,
        "subject_set_sha256": subject_set_sha256,
        "items": [_annotation_packet_item(row, []) for row in ordered],
        "synthetic": cohort["synthetic"],
        "packet_sha256": "",
    }
    return with_artifact_hash(packet, "packet_sha256")


def validate_annotation_packet(
    raw: Any,
    subjects: Sequence[Mapping[str, Any]],
    cohort: Mapping[str, Any],
) -> dict[str, Any]:
    packet = _expect_dict(raw, "annotation_packet")
    _exact_keys(packet, ANNOTATION_PACKET_KEYS, "annotation_packet")
    if packet["schema_version"] != 1 or packet["phase_id"] != PHASE_ID:
        _fail("annotation packet schema or phase is invalid")
    _expect_identifier(packet["packet_id"], "annotation_packet.packet_id")
    mode = _expect_enum(
        packet["mode"], {"independent", "adjudication"}, "annotation_packet.mode"
    )
    stage = _expect_enum(packet["stage"], {"gold", "system"}, "annotation_packet.stage")
    _expect_identifier(packet["annotator_id"], "annotation_packet.annotator_id")
    parse_timestamp(packet["generated_at"], "annotation_packet.generated_at")
    _expect_sha(packet["rubric_sha256"], "annotation_packet.rubric_sha256")
    if packet["cohort_sha256"] != cohort["cohort_sha256"]:
        _fail("annotation packet cohort binding is stale")
    if isinstance(packet["order_seed"], bool) or not isinstance(packet["order_seed"], int):
        _fail("annotation packet order_seed must be an integer")
    _expect_sha(packet["subject_set_sha256"], "annotation_packet.subject_set_sha256")
    subject_by_id = {row["subject_id"]: row for row in subjects if row["stage"] == stage}
    items = _expect_list(packet["items"], "annotation_packet.items")
    if not items:
        _fail("annotation packet items must be non-empty")
    seen: set[str] = set()
    for index, raw_item in enumerate(items):
        where = f"annotation_packet.items[{index}]"
        item = _expect_dict(raw_item, where)
        _exact_keys(item, ANNOTATION_PACKET_ITEM_KEYS, where)
        subject_id = _expect_identifier(item["subject_id"], f"{where}.subject_id")
        if subject_id in seen or subject_id not in subject_by_id:
            _fail(f"{where}.subject_id is duplicate or foreign")
        seen.add(subject_id)
        subject = subject_by_id[subject_id]
        for key in (
            "subject_kind",
            "pr_id",
            "subject_sha256",
            "evidence_sha256",
            "severity",
        ):
            if item[key] != subject[key]:
                _fail(f"{where}.{key} does not bind its subject")
        sources = _expect_list(item["source_annotations"], f"{where}.source_annotations")
        expected_sources = 0 if mode == "independent" else 2
        if len(sources) != expected_sources:
            _fail(f"{where} has the wrong source-annotation count")
        for source_index, raw_source in enumerate(sources):
            source_where = f"{where}.source_annotations[{source_index}]"
            source = _expect_dict(raw_source, source_where)
            _exact_keys(source, SOURCE_ANNOTATION_KEYS, source_where)
            _expect_sha(source["response_sha256"], f"{source_where}.response_sha256")
            _expect_identifier(source["annotator_id"], f"{source_where}.annotator_id")
            allowed = GOLD_LABELS if stage == "gold" else SYSTEM_LABELS
            _expect_enum(source["label"], allowed, f"{source_where}.label")
            if source["gold_id"] is not None:
                _expect_identifier(source["gold_id"], f"{source_where}.gold_id")
        if mode == "adjudication" and len({source["annotator_id"] for source in sources}) != 2:
            _fail(f"{where} does not bind two distinct independent annotators")
    expected_seen = set(subject_by_id) if mode == "independent" else seen
    if mode == "independent" and seen != expected_seen:
        _fail("independent packet does not cover its complete stage subject set")
    expected_set_hash = sha256_value(
        sorted((subject_id, subject_by_id[subject_id]["record_sha256"]) for subject_id in seen)
    )
    if packet["subject_set_sha256"] != expected_set_hash:
        _fail("annotation packet subject-set hash is stale")
    if _expect_bool(packet["synthetic"], "annotation_packet.synthetic") != cohort["synthetic"]:
        _fail("annotation packet provenance differs from cohort")
    validate_artifact_hash(packet, "packet_sha256", "annotation_packet")
    return packet


def validate_independent_annotation_pair(
    packet_a: Mapping[str, Any], packet_b: Mapping[str, Any]
) -> None:
    if packet_a["mode"] != "independent" or packet_b["mode"] != "independent":
        _fail("A/B packets must both be independent")
    if packet_a["annotator_id"] == packet_b["annotator_id"]:
        _fail("A/B packets must bind different annotators")
    for key in ("stage", "rubric_sha256", "cohort_sha256", "subject_set_sha256", "synthetic"):
        if packet_a[key] != packet_b[key]:
            _fail(f"A/B packet {key} bindings differ")
    if [item["subject_id"] for item in packet_a["items"]] == [
        item["subject_id"] for item in packet_b["items"]
    ] and len(packet_a["items"]) > 1:
        _fail("A/B packet ordering must differ")


def validate_annotation_responses(
    raw_rows: Sequence[Any],
    packet: Mapping[str, Any],
    subjects: Sequence[Mapping[str, Any]],
    cohort: Mapping[str, Any],
) -> list[dict[str, Any]]:
    packet = validate_annotation_packet(packet, subjects, cohort)
    item_by_id = {item["subject_id"]: item for item in packet["items"]}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_rows):
        where = f"annotation_responses[{index}]"
        row = _expect_dict(raw, where)
        _exact_keys(row, ANNOTATION_RESPONSE_KEYS, where)
        if row["schema_version"] != 1:
            _fail(f"{where}.schema_version must be 1")
        if row["packet_id"] != packet["packet_id"] or row["packet_sha256"] != packet[
            "packet_sha256"
        ]:
            _fail(f"{where} has a foreign or stale packet binding")
        if row["annotator_id"] != packet["annotator_id"]:
            _fail(f"{where} changes the packet annotator")
        subject_id = _expect_identifier(row["subject_id"], f"{where}.subject_id")
        if subject_id in seen or subject_id not in item_by_id:
            _fail(f"{where}.subject_id is duplicate or foreign")
        seen.add(subject_id)
        allowed = GOLD_LABELS if packet["stage"] == "gold" else SYSTEM_LABELS
        label = _expect_enum(row["label"], allowed, f"{where}.label")
        if packet["mode"] == "adjudication" and label == "uncertain":
            _fail(f"{where} adjudication cannot remain uncertain")
        gold_id = row["gold_id"]
        if packet["stage"] == "system" and label in {"matched", "duplicate"}:
            _expect_identifier(gold_id, f"{where}.gold_id")
        elif gold_id is not None:
            _fail(f"{where}.gold_id is not valid for this stage/label")
        discovered = row["discovered"]
        severity = row["severity"]
        if packet["stage"] == "gold" and packet["mode"] == "independent":
            _expect_bool(discovered, f"{where}.discovered")
            _expect_enum(severity, SEVERITIES, f"{where}.severity")
        elif packet["stage"] == "gold":
            if discovered is not None:
                _fail(f"{where}.discovered must be null for adjudication")
            _expect_enum(severity, SEVERITIES, f"{where}.severity")
        elif discovered is not None or severity is not None:
            _fail(f"{where} system annotations cannot set discovered/severity")
        _expect_str(row["rationale"], f"{where}.rationale", maximum=4000)
        if row["evidence_sha256"] != item_by_id[subject_id]["evidence_sha256"]:
            _fail(f"{where}.evidence_sha256 is stale")
        parse_timestamp(row["created_at"], f"{where}.created_at")
        if parse_timestamp(row["created_at"], f"{where}.created_at") < parse_timestamp(
            packet["generated_at"], "annotation packet generated_at"
        ):
            _fail(f"{where}.created_at predates its packet")
        human = _expect_bool(row["completed_by_human"], f"{where}.completed_by_human")
        synthetic = _expect_bool(row["synthetic"], f"{where}.synthetic")
        if synthetic != packet["synthetic"]:
            _fail(f"{where} provenance differs from packet")
        if not synthetic and not human:
            _fail(f"{where} a real label must be completed by a human")
        validate_artifact_hash(row, "response_sha256", where)
        rows.append(row)
    if seen != set(item_by_id):
        _fail("annotation responses do not cover the packet exactly once")
    return sorted(rows, key=lambda row: row["subject_id"])


def _annotation_signature(row: Mapping[str, Any]) -> tuple[Any, Any]:
    return row["label"], row["gold_id"]


def build_adjudication_packet(
    subjects: Sequence[Mapping[str, Any]],
    cohort: Mapping[str, Any],
    packet_a: Mapping[str, Any],
    packet_b: Mapping[str, Any],
    responses_a: Sequence[Mapping[str, Any]],
    responses_b: Sequence[Mapping[str, Any]],
    *,
    adjudicator_id: str,
    order_seed: int,
    generated_at: str,
) -> dict[str, Any] | None:
    validate_independent_annotation_pair(packet_a, packet_b)
    response_a_by_id = {row["subject_id"]: row for row in responses_a}
    response_b_by_id = {row["subject_id"]: row for row in responses_b}
    if set(response_a_by_id) != set(response_b_by_id):
        _fail("A/B responses do not cover the same subject set")
    adjudicator = _expect_identifier(adjudicator_id, "adjudicator_id")
    if adjudicator in {packet_a["annotator_id"], packet_b["annotator_id"]}:
        _fail("adjudicator must be distinct from A and B")
    needs = []
    subject_by_id = {row["subject_id"]: row for row in subjects}
    for subject_id in sorted(response_a_by_id):
        left = response_a_by_id[subject_id]
        right = response_b_by_id[subject_id]
        if _annotation_signature(left) != _annotation_signature(right) or "uncertain" in {
            left["label"],
            right["label"],
        }:
            needs.append((subject_by_id[subject_id], [left, right]))
    if not needs:
        return None
    random.Random(order_seed).shuffle(needs)
    selected = [subject for subject, _ in needs]
    subject_set_sha256 = sha256_value(
        sorted((row["subject_id"], row["record_sha256"]) for row in selected)
    )
    packet = {
        "schema_version": 1,
        "phase_id": PHASE_ID,
        "packet_id": f"adjudication-{sha256_value([packet_a['stage'], adjudicator, subject_set_sha256, order_seed])[:24]}",
        "mode": "adjudication",
        "stage": packet_a["stage"],
        "annotator_id": adjudicator,
        "generated_at": generated_at,
        "rubric_sha256": packet_a["rubric_sha256"],
        "cohort_sha256": cohort["cohort_sha256"],
        "order_seed": order_seed,
        "subject_set_sha256": subject_set_sha256,
        "items": [
            _annotation_packet_item(subject, source_rows) for subject, source_rows in needs
        ],
        "synthetic": packet_a["synthetic"],
        "packet_sha256": "",
    }
    return with_artifact_hash(packet, "packet_sha256")


def resolve_annotation_responses(
    responses_a: Sequence[Mapping[str, Any]],
    responses_b: Sequence[Mapping[str, Any]],
    responses_c: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    by_id_a = {row["subject_id"]: row for row in responses_a}
    by_id_b = {row["subject_id"]: row for row in responses_b}
    by_id_c = {row["subject_id"]: row for row in responses_c}
    if set(by_id_a) != set(by_id_b):
        _fail("A/B responses do not cover the same subjects")
    finals: dict[str, Mapping[str, Any]] = {}
    required_c: set[str] = set()
    for subject_id in sorted(by_id_a):
        left = by_id_a[subject_id]
        right = by_id_b[subject_id]
        conflict = _annotation_signature(left) != _annotation_signature(right) or "uncertain" in {
            left["label"],
            right["label"],
        }
        if conflict:
            required_c.add(subject_id)
            final = by_id_c.get(subject_id)
            if final is None:
                _fail("a conflicting subject requires adjudication")
            finals[subject_id] = final
        else:
            if subject_id in by_id_c:
                _fail("an agreed subject has unnecessary adjudication")
            finals[subject_id] = left
    if set(by_id_c) != required_c:
        _fail("adjudication responses contain missing or extra subjects")
    return finals


def build_gold_freeze(
    *,
    authorization: Mapping[str, Any],
    cohort: Mapping[str, Any],
    packet_a: Mapping[str, Any],
    packet_b: Mapping[str, Any],
    responses_a: Sequence[Mapping[str, Any]],
    responses_b: Sequence[Mapping[str, Any]],
    adjudication_packet: Mapping[str, Any] | None,
    responses_c: Sequence[Mapping[str, Any]],
    frozen_at: str,
    external_git_commit: str,
    trusted_cohort_sha256: str,
) -> dict[str, Any]:
    if packet_a["stage"] != "gold" or packet_b["stage"] != "gold":
        _fail("gold freeze requires gold-stage A/B packets")
    validate_independent_annotation_pair(packet_a, packet_b)
    discovered_by_subject: dict[str, bool] = defaultdict(bool)
    for row in [*responses_a, *responses_b]:
        discovered_by_subject[row["subject_id"]] = (
            discovered_by_subject[row["subject_id"]] or row["discovered"] is True
        )
    undiscovered = sorted(
        subject_id for subject_id, discovered in discovered_by_subject.items() if not discovered
    )
    if undiscovered:
        _fail("every gold subject must be independently discovered by A or B")
    finals = resolve_annotation_responses(responses_a, responses_b, responses_c)
    if any(row["label"] == "uncertain" for row in finals.values()):
        _fail("gold freeze cannot contain an unresolved label")
    frozen = parse_timestamp(frozen_at, "gold freeze frozen_at")
    if frozen < max(
        parse_timestamp(row["created_at"], "annotation created_at")
        for rows in (responses_a, responses_b, responses_c)
        for row in rows
    ):
        _fail("gold freeze predates an annotation")
    commit = _expect_sha(external_git_commit, "gold freeze external_git_commit", length=40)
    trusted_cohort = _expect_sha(
        trusted_cohort_sha256, "gold freeze trusted_cohort_sha256"
    )
    formal = authorization["formal_quality"]
    if packet_a["annotator_id"] != formal["annotator_a_id"]:
        _fail("packet A annotator differs from authorization")
    if packet_b["annotator_id"] != formal["annotator_b_id"]:
        _fail("packet B annotator differs from authorization")
    if responses_c and (
        adjudication_packet is None
        or adjudication_packet["annotator_id"] != formal["adjudicator_c_id"]
    ):
        _fail("adjudication identity differs from authorization")
    synthetic = any(
        [
            authorization["synthetic"],
            cohort["synthetic"],
            packet_a["synthetic"],
            packet_b["synthetic"],
            any(row["synthetic"] for row in responses_a),
            any(row["synthetic"] for row in responses_b),
            any(row["synthetic"] for row in responses_c),
        ]
    )
    readiness = authorization_readiness(authorization)["scopes"]["formal_quality"]
    gates = list(readiness["blocked_by"])
    if synthetic:
        gates.append("synthetic_annotations")
    if packet_a["rubric_sha256"] != packet_b["rubric_sha256"]:
        gates.append("rubric_binding_mismatch")
    gates = sorted(set(gates))
    freeze = {
        "schema_version": 1,
        "phase_id": PHASE_ID,
        "pilot_id": cohort["pilot_id"],
        "cohort_sha256": cohort["cohort_sha256"],
        "trusted_cohort_sha256": trusted_cohort,
        "rubric_sha256": packet_a["rubric_sha256"],
        "packet_a_sha256": packet_a["packet_sha256"],
        "packet_b_sha256": packet_b["packet_sha256"],
        "adjudication_packet_sha256": (
            adjudication_packet["packet_sha256"] if adjudication_packet is not None else None
        ),
        "annotation_set_sha256": sha256_value(
            sorted(
                row["response_sha256"]
                for rows in (responses_a, responses_b, responses_c)
                for row in rows
            )
        ),
        "custodian_id": formal["gold_freeze_custodian_id"],
        "frozen_at": frozen_at,
        "external_git_commit": commit,
        "synthetic": synthetic,
        "real_run_ready": not gates,
        "quality_claim_allowed": False,
        "incomplete_gates": gates,
        "freeze_sha256": "",
    }
    return with_artifact_hash(freeze, "freeze_sha256")


def validate_gold_freeze(raw: Any) -> dict[str, Any]:
    freeze = _expect_dict(raw, "gold_freeze")
    _exact_keys(freeze, GOLD_FREEZE_KEYS, "gold_freeze")
    if freeze["schema_version"] != 1 or freeze["phase_id"] != PHASE_ID:
        _fail("gold freeze schema or phase is invalid")
    _expect_identifier(freeze["pilot_id"], "gold_freeze.pilot_id")
    for key in (
        "cohort_sha256",
        "trusted_cohort_sha256",
        "rubric_sha256",
        "packet_a_sha256",
        "packet_b_sha256",
        "annotation_set_sha256",
    ):
        _expect_sha(freeze[key], f"gold_freeze.{key}")
    if freeze["adjudication_packet_sha256"] is not None:
        _expect_sha(
            freeze["adjudication_packet_sha256"],
            "gold_freeze.adjudication_packet_sha256",
        )
    _expect_identifier(freeze["custodian_id"], "gold_freeze.custodian_id")
    parse_timestamp(freeze["frozen_at"], "gold_freeze.frozen_at")
    _expect_sha(freeze["external_git_commit"], "gold_freeze.external_git_commit", length=40)
    synthetic = _expect_bool(freeze["synthetic"], "gold_freeze.synthetic")
    real_run_ready = _expect_bool(freeze["real_run_ready"], "gold_freeze.real_run_ready")
    if _expect_bool(
        freeze["quality_claim_allowed"], "gold_freeze.quality_claim_allowed"
    ):
        _fail("gold freeze alone can never authorize a quality claim")
    gates = _expect_list(freeze["incomplete_gates"], "gold_freeze.incomplete_gates")
    if any(not isinstance(gate, str) or not gate for gate in gates):
        _fail("gold freeze incomplete gates must be stable strings")
    if real_run_ready != (not gates) or (synthetic and real_run_ready):
        _fail("gold freeze readiness is inconsistent with its gates/provenance")
    validate_artifact_hash(freeze, "freeze_sha256", "gold_freeze")
    return freeze


def validate_formal_quality_report(
    raw: Any,
    authorization: Mapping[str, Any],
    gold_freeze: Mapping[str, Any],
    *,
    validated_at: str,
    system_packet_provenance_valid: bool = False,
) -> dict[str, Any]:
    """Validate a trusted-review report and return a separate claim receipt."""

    report = _expect_dict(raw, "formal_quality_report")
    freeze = validate_gold_freeze(gold_freeze)
    validated_timestamp = parse_timestamp(validated_at, "formal report validated_at")
    reasons = list(
        authorization_readiness(authorization)["scopes"]["formal_quality"]["blocked_by"]
    )
    if freeze["synthetic"] or not freeze["real_run_ready"]:
        reasons.append("gold_freeze_not_real_run_ready")
    if not system_packet_provenance_valid:
        reasons.append("system_packet_provenance_not_bound")
    if set(report) != TRUSTED_REPORT_KEYS:
        reasons.append("trusted_review_report_shape_invalid")
    if report.get("schema_version") != 1 or report.get("metric_version") != "trusted-review-v2":
        reasons.append("trusted_review_report_version_invalid")
    if report.get("split") != "reporting":
        reasons.append("formal_report_is_not_reporting_split")
    if report.get("gold_freeze_commit") != freeze["external_git_commit"]:
        reasons.append("gold_freeze_commit_mismatch")
    if report.get("frozen_cohort_sha256") != freeze["trusted_cohort_sha256"]:
        reasons.append("trusted_cohort_hash_mismatch")
    if report.get("provider") != authorization["model"]["provider"]:
        reasons.append("formal_provider_mismatch")
    if report.get("model_id") != authorization["model"]["exact_model_snapshot"]:
        reasons.append("formal_model_snapshot_mismatch")
    for key in ("cohort_id", "config_id", "pricing_revision"):
        try:
            _expect_identifier(report.get(key), f"formal_quality_report.{key}")
        except ValidationError:
            reasons.append(f"formal_{key}_invalid")
    try:
        _expect_sha(
            report.get("runtime_config_sha256"),
            "formal_quality_report.runtime_config_sha256",
        )
    except ValidationError:
        reasons.append("formal_runtime_config_hash_invalid")
    source_commits = report.get("source_commits")
    if (
        not isinstance(source_commits, list)
        or not source_commits
        or any(not isinstance(value, str) for value in source_commits)
        or len(source_commits) != len(set(source_commits))
    ):
        reasons.append("formal_source_commits_invalid")
    else:
        try:
            for index, source_commit in enumerate(source_commits):
                _expect_sha(
                    source_commit,
                    f"formal_quality_report.source_commits[{index}]",
                    length=40,
                )
        except ValidationError:
            reasons.append("formal_source_commits_invalid")
    input_hashes = report.get("input_hashes")
    if not isinstance(input_hashes, dict) or set(input_hashes) != TRUSTED_REPORT_INPUT_HASH_KEYS:
        reasons.append("formal_input_hashes_invalid")
    else:
        try:
            for key, value in input_hashes.items():
                _expect_sha(value, f"formal_quality_report.input_hashes.{key}")
        except ValidationError:
            reasons.append("formal_input_hashes_invalid")
    generated_at = report.get("generated_at")
    try:
        generated = parse_timestamp(generated_at, "formal_quality_report.generated_at")
        if generated < parse_timestamp(freeze["frozen_at"], "gold_freeze.frozen_at"):
            reasons.append("formal_report_predates_gold_freeze")
        if validated_timestamp < generated:
            reasons.append("formal_validation_predates_report")
    except ValidationError:
        reasons.append("formal_report_generated_at_invalid")
    agreement = report.get("agreement")
    if not isinstance(agreement, dict) or set(agreement) != TRUSTED_AGREEMENT_KEYS:
        reasons.append("formal_agreement_shape_invalid")
    else:
        expected_ab = sorted(
            [
                authorization["formal_quality"]["annotator_a_id"],
                authorization["formal_quality"]["annotator_b_id"],
            ]
        )
        if agreement.get("annotators") != expected_ab:
            reasons.append("formal_annotator_ids_mismatch")
        adjudicators = agreement.get("adjudicators")
        if (
            not isinstance(adjudicators, list)
            or any(not isinstance(value, str) for value in adjudicators)
            or len(adjudicators) != len(set(adjudicators))
            or any(
                value != authorization["formal_quality"]["adjudicator_c_id"]
                for value in adjudicators
            )
        ):
            reasons.append("formal_adjudicator_id_mismatch")
        overall = agreement.get("overall")
        if not isinstance(overall, dict) or set(overall) != TRUSTED_AGREEMENT_BLOCK_KEYS:
            reasons.append("formal_agreement_overall_invalid")
        elif (
            overall.get("unresolved_subjects") != 0
            or overall.get("malformed_subjects") != 0
            or overall.get("invalid_subject_policy") != "fail_closed_before_metrics"
            or isinstance(overall.get("subjects"), bool)
            or not isinstance(overall.get("subjects"), int)
            or overall["subjects"] < 1
        ):
            reasons.append("formal_annotations_unresolved")
        by_subject_kind = agreement.get("by_subject_kind")
        if not isinstance(by_subject_kind, dict) or set(by_subject_kind) != {
            "gold_candidate",
            "system_finding",
        }:
            reasons.append("formal_agreement_subject_kinds_invalid")
        if not isinstance(agreement.get("by_repository"), dict):
            reasons.append("formal_agreement_repositories_invalid")
    review = report.get("review")
    if not isinstance(review, dict) or set(review) != TRUSTED_REVIEW_KEYS:
        reasons.append("formal_review_metrics_missing")
    else:
        micro = review.get("micro")
        if not isinstance(micro, dict) or set(micro) != TRUSTED_MICRO_KEYS:
            reasons.append("formal_review_micro_shape_invalid")
        else:
            counts_valid = True
            for key in TRUSTED_COUNT_KEYS:
                value = micro[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    counts_valid = False
            if not counts_valid:
                reasons.append("formal_review_counts_invalid")
            else:
                precision = _safe_div(
                    micro["tp_findings"],
                    micro["tp_findings"] + micro["fp_findings"],
                )
                recall = _safe_div(
                    micro["tp_gold"], micro["tp_gold"] + micro["fn_gold"]
                )
                expected_f1 = (
                    None
                    if precision is None or recall is None
                    else 0.0
                    if precision == 0.0 or recall == 0.0
                    else round(2.0 * precision * recall / (precision + recall), 6)
                )
                expected_metrics = {
                    "precision": None if precision is None else round(precision, 6),
                    "recall": None if recall is None else round(recall, 6),
                    "f1": expected_f1,
                }
                for metric, expected in expected_metrics.items():
                    value = micro[metric]
                    valid = value is None if expected is None else (
                        not isinstance(value, bool)
                        and isinstance(value, (int, float))
                        and math.isfinite(float(value))
                        and math.isclose(float(value), expected, abs_tol=1e-9)
                    )
                    if not valid:
                        reasons.append(f"formal_{metric}_invalid")
            for key in ("repository_macro", "pr_macro", "by_repository"):
                if not isinstance(review.get(key), dict):
                    reasons.append(f"formal_review_{key}_invalid")
            if not isinstance(review.get("per_pr"), list):
                reasons.append("formal_review_per_pr_invalid")
    bootstrap = report.get("bootstrap_95_ci")
    if not isinstance(bootstrap, dict) or set(bootstrap) != TRUSTED_BOOTSTRAP_KEYS:
        reasons.append("formal_bootstrap_invalid")
    else:
        if bootstrap["method"] != "percentile_pr_within_repository":
            reasons.append("formal_bootstrap_invalid")
        seed = bootstrap["seed"]
        replicates = bootstrap["replicates"]
        alpha = bootstrap["alpha"]
        if isinstance(seed, bool) or not isinstance(seed, int):
            reasons.append("formal_bootstrap_seed_invalid")
        if isinstance(replicates, bool) or not isinstance(replicates, int) or replicates < 1:
            reasons.append("formal_bootstrap_replicates_invalid")
        if (
            isinstance(alpha, bool)
            or not isinstance(alpha, (int, float))
            or not math.isfinite(float(alpha))
            or not math.isclose(float(alpha), 0.05, abs_tol=1e-12)
        ):
            reasons.append("formal_bootstrap_not_95_ci")
        for metric in ("precision", "recall", "f1"):
            interval = bootstrap[metric]
            if not isinstance(interval, dict) or set(interval) != TRUSTED_BOOTSTRAP_INTERVAL_KEYS:
                reasons.append(f"formal_bootstrap_{metric}_invalid")
                continue
            defined = interval["defined_replicates"]
            reason = interval["reason"]
            low = interval["low"]
            high = interval["high"]
            if (
                isinstance(defined, bool)
                or not isinstance(defined, int)
                or defined < 0
                or not isinstance(replicates, int)
                or isinstance(replicates, bool)
                or defined > replicates
            ):
                reasons.append(f"formal_bootstrap_{metric}_invalid")
            if reason is not None and (not isinstance(reason, str) or not reason):
                reasons.append(f"formal_bootstrap_{metric}_invalid")
            if low is None or high is None:
                if low is not None or high is not None or defined != 0:
                    reasons.append(f"formal_bootstrap_{metric}_invalid")
            elif (
                isinstance(low, bool)
                or isinstance(high, bool)
                or not isinstance(low, (int, float))
                or not isinstance(high, (int, float))
                or not math.isfinite(float(low))
                or not math.isfinite(float(high))
                or not 0 <= float(low) <= float(high) <= 1
                or defined == 0
            ):
                reasons.append(f"formal_bootstrap_{metric}_invalid")
    reasons = sorted(set(reasons))
    return {
        "schema_version": 1,
        "phase_id": PHASE_ID,
        "validated_at": validated_at,
        "formal_report_sha256": sha256_value(report),
        "gold_freeze_sha256": freeze["freeze_sha256"],
        "quality_claim_allowed": not reasons,
        "blocked_by": reasons,
        "business_outcome_measured": False,
        "business_outcome_reason": "formal_quality_report_is_not_business_pilot_feedback",
    }


BUNDLE_KEYS = {
    "schema_version",
    "phase_id",
    "authorization",
    "participants",
    "repositories",
    "selection_plan",
    "selection_log",
    "cohort",
    "finding_subjects",
    "feedback_packets",
    "feedback_responses",
    "review_times",
    "run_receipts",
    "run_manifest",
    "annotation_subjects",
    "annotation_packets",
    "annotation_responses",
    "gold_freeze",
    "business_report",
    "formal_quality_report",
    "bundle_sha256",
}
BUNDLE_FIXTURE_KEYS = {
    "schema_version",
    "phase_id",
    "fixture",
    "expected_bundle_sha256",
    "business_claim_allowed",
    "quality_claim_allowed",
    "fixture_sha256",
}


def validate_bundle(
    raw: Any,
    *,
    expected_source_commit: str | None = None,
) -> dict[str, Any]:
    bundle = _expect_dict(raw, "bundle")
    _exact_keys(bundle, BUNDLE_KEYS, "bundle")
    if bundle["schema_version"] != 1 or bundle["phase_id"] != PHASE_ID:
        _fail("bundle schema or phase is invalid")
    authorization = _expect_dict(bundle["authorization"], "bundle.authorization")
    validate_authorization(authorization)
    participants = validate_participant_manifest(bundle["participants"], authorization)
    repositories = validate_repository_manifest(bundle["repositories"], authorization)
    plan = validate_selection_plan(
        bundle["selection_plan"],
        expected_source_commit=expected_source_commit,
    )
    if not plan["synthetic"] and expected_source_commit is None:
        _fail("real bundle validation requires the expected Phase 9G-Prep merge commit")
    if not (
        participants["pilot_id"]
        == repositories["pilot_id"]
        == plan["pilot_id"]
    ):
        _fail("bundle pilot IDs differ")
    selection_rows = validate_selection_log(bundle["selection_log"], plan, repositories)
    cohort = validate_cohort(bundle["cohort"], plan, selection_rows, repositories)
    finding_subjects = validate_finding_subjects(bundle["finding_subjects"], cohort)
    feedback_packets = [
        validate_feedback_packet(packet, finding_subjects, cohort)
        for packet in _expect_list(bundle["feedback_packets"], "bundle.feedback_packets")
    ]
    validate_feedback_packet_assignments(feedback_packets, participants, cohort)
    feedback_responses = validate_feedback_responses(
        bundle["feedback_responses"], feedback_packets, finding_subjects, cohort
    )
    review_times = validate_review_times(bundle["review_times"], cohort, participants)
    receipts = validate_run_receipts(bundle["run_receipts"], cohort, authorization)
    validate_receipt_finding_bindings(receipts, finding_subjects)
    run_manifest = validate_run_manifest(bundle["run_manifest"], receipts, cohort)

    annotation_subjects = validate_annotation_subjects(bundle["annotation_subjects"], cohort)
    packet_map: dict[str, dict[str, Any]] = {}
    for packet in _expect_list(bundle["annotation_packets"], "bundle.annotation_packets"):
        validated = validate_annotation_packet(packet, annotation_subjects, cohort)
        if validated["packet_id"] in packet_map:
            _fail("annotation packets repeat a packet ID")
        packet_map[validated["packet_id"]] = validated
    response_map: dict[str, list[dict[str, Any]]] = {}
    for response_group in _expect_list(
        bundle["annotation_responses"], "bundle.annotation_responses"
    ):
        group = _expect_dict(response_group, "bundle.annotation_responses[]")
        _exact_keys(group, {"packet_id", "responses"}, "bundle.annotation_responses[]")
        packet = packet_map.get(group["packet_id"])
        if packet is None:
            _fail("annotation response group references a foreign packet")
        if group["packet_id"] in response_map:
            _fail("annotation response groups repeat a packet ID")
        response_map[group["packet_id"]] = validate_annotation_responses(
            group["responses"], packet, annotation_subjects, cohort
        )
    formal = authorization["formal_quality"]

    def validate_stage(
        stage: str,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any] | None,
        list[dict[str, Any]],
    ]:
        independent = [
            packet
            for packet in packet_map.values()
            if packet["mode"] == "independent" and packet["stage"] == stage
        ]
        if len(independent) != 2:
            _fail(f"{stage} stage requires exactly two independent annotation packets")
        independent_by_annotator = {
            packet["annotator_id"]: packet for packet in independent
        }
        packet_a = independent_by_annotator.get(formal["annotator_a_id"])
        packet_b = independent_by_annotator.get(formal["annotator_b_id"])
        if packet_a is None or packet_b is None:
            _fail(f"{stage} packets do not match authorized A/B identities")
        validate_independent_annotation_pair(packet_a, packet_b)
        responses_a = response_map.get(packet_a["packet_id"], [])
        responses_b = response_map.get(packet_b["packet_id"], [])
        if not responses_a or not responses_b:
            _fail(f"{stage} independent annotation responses are incomplete")
        adjudication_packets = [
            packet
            for packet in packet_map.values()
            if packet["mode"] == "adjudication" and packet["stage"] == stage
        ]
        if len(adjudication_packets) > 1:
            _fail(f"{stage} stage contains more than one adjudication packet")
        adjudication_packet = adjudication_packets[0] if adjudication_packets else None
        expected_adjudication = build_adjudication_packet(
            annotation_subjects,
            cohort,
            packet_a,
            packet_b,
            responses_a,
            responses_b,
            adjudicator_id=formal["adjudicator_c_id"],
            order_seed=(adjudication_packet["order_seed"] if adjudication_packet else 0),
            generated_at=(
                adjudication_packet["generated_at"]
                if adjudication_packet
                else max(packet_a["generated_at"], packet_b["generated_at"])
            ),
        )
        if expected_adjudication is None:
            if adjudication_packet is not None:
                _fail(f"{stage} stage contains unnecessary adjudication")
            responses_c: list[dict[str, Any]] = []
        else:
            if adjudication_packet is None:
                _fail(f"{stage} stage is missing required adjudication")
            if adjudication_packet != expected_adjudication:
                _fail(f"{stage} adjudication packet does not bind the exact A/B responses")
            responses_c = response_map.get(adjudication_packet["packet_id"], [])
            if not responses_c:
                _fail(f"{stage} adjudication responses are incomplete")
        resolve_annotation_responses(responses_a, responses_b, responses_c)
        return (
            packet_a,
            packet_b,
            responses_a,
            responses_b,
            adjudication_packet,
            responses_c,
        )

    (
        gold_packet_a,
        gold_packet_b,
        gold_responses_a,
        gold_responses_b,
        gold_adjudication_packet,
        gold_responses_c,
    ) = validate_stage("gold")
    system_present = any(
        subject["stage"] == "system" for subject in annotation_subjects
    ) or any(packet["stage"] == "system" for packet in packet_map.values())
    system_packet_provenance_valid = False
    if system_present:
        validate_stage("system")
        system_packet_provenance_valid = not cohort["synthetic"]
    if set(response_map) != set(packet_map):
        _fail("every annotation packet requires exactly one response group")
    expected_freeze = build_gold_freeze(
        authorization=authorization,
        cohort=cohort,
        packet_a=gold_packet_a,
        packet_b=gold_packet_b,
        responses_a=gold_responses_a,
        responses_b=gold_responses_b,
        adjudication_packet=gold_adjudication_packet,
        responses_c=gold_responses_c,
        frozen_at=bundle["gold_freeze"]["frozen_at"],
        external_git_commit=bundle["gold_freeze"]["external_git_commit"],
        trusted_cohort_sha256=bundle["gold_freeze"]["trusted_cohort_sha256"],
    )
    if bundle["gold_freeze"] != expected_freeze:
        _fail("gold freeze does not match packet/annotation inputs")
    gold_freeze = validate_gold_freeze(bundle["gold_freeze"])
    business_report = validate_business_report(
        bundle["business_report"],
        authorization=authorization,
        participants=participants,
        repositories=repositories,
        cohort=cohort,
        finding_subjects=finding_subjects,
        feedback_packets=feedback_packets,
        feedback_responses=feedback_responses,
        review_times=review_times,
        receipts=receipts,
        run_manifest=run_manifest,
    )
    formal_receipt = None
    if bundle["formal_quality_report"] is not None:
        if not system_present:
            _fail("formal quality report requires post-run system annotation packets")
        formal_reporting_prs = {
            entry["pr_id"]
            for entry in cohort["entries"]
            if entry["track"] == "formal" and entry["role"] == "reporting"
        }
        if not formal_reporting_prs:
            _fail("formal quality report requires a materialized reporting cohort")
        formal_headlines = [
            receipt
            for receipt in receipts
            if receipt["track"] == "formal"
            and receipt["role"] == "reporting"
            and receipt["headline"]
        ]
        if {receipt["pr_id"] for receipt in formal_headlines} != formal_reporting_prs:
            _fail("formal headline receipts do not cover the reporting cohort")
        freeze_time = parse_timestamp(gold_freeze["frozen_at"], "gold_freeze.frozen_at")
        if any(
            parse_timestamp(receipt["started_at"], "formal receipt started_at") < freeze_time
            for receipt in formal_headlines
        ):
            _fail("a formal reporting headline run started before gold freeze")
        telemetry = bundle["formal_quality_report"].get("telemetry")
        if not isinstance(telemetry, dict):
            _fail("formal quality report telemetry is missing")
        if telemetry.get("attempted_runs") != len(formal_headlines):
            _fail("formal quality report attempted-run denominator differs from headlines")
        expected_statuses: Counter[str] = Counter()
        for receipt in formal_headlines:
            expected_statuses[
                {
                    "completed": "ok",
                    "degraded": "degraded",
                    "fail_open": "fail_open",
                }.get(receipt["status"], "failed")
            ] += 1
        declared_statuses = telemetry.get("status_counts")
        if not isinstance(declared_statuses, dict) or any(
            declared_statuses.get(status) != expected_statuses.get(status, 0)
            for status in ("ok", "degraded", "fail_open", "failed")
        ):
            _fail("formal quality report status denominators differ from headline receipts")
        formal_receipt = validate_formal_quality_report(
            bundle["formal_quality_report"],
            authorization,
            gold_freeze,
            validated_at=business_report["generated_at"],
            system_packet_provenance_valid=system_packet_provenance_valid,
        )
    validate_artifact_hash(bundle, "bundle_sha256", "bundle")
    readiness = authorization_readiness(authorization)
    return {
        "valid": True,
        "pilot_id": cohort["pilot_id"],
        "synthetic": bundle["authorization"]["synthetic"],
        "selected_business_prs": sum(
            entry["track"] == "business" for entry in cohort["entries"]
        ),
        "business_claim_allowed": business_report["claim_gates"][
            "business_claim_allowed"
        ],
        "quality_claim_allowed": (
            formal_receipt["quality_claim_allowed"] if formal_receipt is not None else False
        ),
        "authorization_scopes": readiness["scopes"],
    }


def build_synthetic_bundle() -> dict[str, Any]:
    """Return a deterministic full-protocol fixture whose real gates stay closed."""

    participant_ids = ["synthetic-developer-a", "synthetic-developer-b", "synthetic-developer-c"]
    repository_ids = ["synthetic-repository"]
    authorization = with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "authorization_id": "synthetic-authorization-v1",
            "approved_by": "synthetic-approver",
            "approved_at": "2026-01-01T00:00:00Z",
            "expires_at": "2035-01-01T00:00:00Z",
            "business_pilot": {
                "participant_ids": participant_ids,
                "participants_confirmed_real": False,
                "repository_ids": repository_ids,
                "pr_count": 20,
                "pr_selection_rule": "deterministic synthetic rank fixture",
                "mode": "shadow",
                "real_github_publish": False,
                "publish_approver_id": None,
                "data_retention_days": 30,
                "feedback_retention_days": 30,
            },
            "model": {
                "provider": "synthetic-provider",
                "exact_model_snapshot": "synthetic-no-model",
                "temperature": 0.0,
                "max_logical_calls": 0,
                "max_http_attempts": 0,
                "max_input_tokens": 0,
                "max_output_tokens": 0,
                "max_cost_microcny": 0,
                "real_paid_calls": False,
                "read_raw_diff": False,
                "raw_trace_retention_days": 30,
            },
            "formal_quality": {
                "execute": False,
                "annotator_a_id": "synthetic-annotator-a",
                "annotator_b_id": "synthetic-annotator-b",
                "adjudicator_c_id": "synthetic-adjudicator-c",
                "humans_confirmed_distinct": False,
                "gold_freeze_custodian_id": "synthetic-freeze-custodian",
                "reporting_results_no_tuning": True,
            },
            "external_operations": {
                "staging_deploy": False,
                "deployment_target": None,
                "real_github_api": False,
                "create_comments_or_checks": False,
                "local_commit": False,
                "push_task_branch": False,
                "create_pr": False,
                "merge_master": False,
            },
            "synthetic": True,
            "authorization_sha256": "",
        },
        "authorization_sha256",
    )
    participants = with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "pilot_id": "synthetic-pilot-v1",
            "identity_custodian_id": "synthetic-identity-custodian",
            "consent_version": "synthetic-consent-v1",
            "generated_at": "2026-01-03T00:00:00Z",
            "participants": [
                {
                    "participant_id": participant_id,
                    "confirmed_real": False,
                    "role": "developer",
                    "consented_at": "2026-01-02T00:00:00Z",
                    "consent_expires_at": "2035-01-01T00:00:00Z",
                    "consent_scope": ["business_feedback", "review_time"],
                    "repository_ids": repository_ids,
                    "feedback_retention_days": 30,
                    "withdrawal_acknowledged": True,
                }
                for participant_id in participant_ids
            ],
            "synthetic": True,
            "manifest_sha256": "",
        },
        "manifest_sha256",
    )
    repository_row = with_artifact_hash(
        {
            "repository_id": "synthetic-repository",
            "locator_sha256": sha256_value("synthetic repository locator"),
            "authorized_by": "synthetic-repository-authorizer",
            "authorized_at": "2026-01-02T00:00:00Z",
            "authorization_expires_at": "2035-01-01T00:00:00Z",
            "allowed_tracks": ["business"],
            "raw_diff_read_authorized": False,
            "real_github_api_authorized": False,
            "publish_mode": "shadow",
            "publication_authorized": False,
            "data_retention_days": 30,
            "repository_sha256": "",
        },
        "repository_sha256",
    )
    repositories = with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "pilot_id": "synthetic-pilot-v1",
            "generated_at": "2026-01-03T00:00:00Z",
            "repositories": [repository_row],
            "synthetic": True,
            "manifest_sha256": "",
        },
        "manifest_sha256",
    )
    selection_plan = with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "pilot_id": "synthetic-pilot-v1",
            "seed": derive_selection_seed("b" * 40),
            "seed_derivation": {
                "method": "sha256_source_commit_v1",
                "source_commit": "b" * 40,
            },
            "selection_window": {
                "start": "2024-01-01T00:00:00Z",
                "end": "2026-01-01T00:00:00Z",
            },
            "groups": [
                {
                    "track": "business",
                    "role": "pilot",
                    "repository_id": "synthetic-repository",
                    "target_prs": 20,
                    "exclusion_reasons": ["outside_scope", "not_reproducible"],
                }
            ],
            "generated_at": "2026-01-03T00:00:00Z",
            "synthetic": True,
            "plan_sha256": "",
        },
        "plan_sha256",
    )
    selection_rows = []
    for index in range(1, 21):
        pr_id = f"synthetic-pr-{index:02d}"
        selection_rows.append(
            with_artifact_hash(
                {
                    "schema_version": 1,
                    "pilot_id": "synthetic-pilot-v1",
                    "track": "business",
                    "role": "pilot",
                    "repository_id": "synthetic-repository",
                    "pr_id": pr_id,
                    "merged_at": f"2025-01-{index:02d}T00:00:00Z",
                    "eligible": True,
                    "exclusion_reason": None,
                    "selected": True,
                    "rank_sha256": selection_rank(selection_plan["seed"], pr_id),
                    "snapshot_sha256": sha256_value(["snapshot", pr_id]),
                    "diff_sha256": sha256_value(["diff", pr_id]),
                    "synthetic": True,
                    "row_sha256": "",
                },
                "row_sha256",
            )
        )
    selection_rows = validate_selection_log(selection_rows, selection_plan, repositories)
    cohort = materialize_cohort(
        selection_plan,
        selection_rows,
        repositories,
        materialized_at="2026-01-04T00:00:00Z",
    )

    finding_subjects = []
    for index in range(1, 21):
        pr_id = f"synthetic-pr-{index:02d}"
        finding_id = f"synthetic-finding-{index:02d}"
        finding_subjects.append(
            with_artifact_hash(
                {
                    "schema_version": 1,
                    "pilot_id": "synthetic-pilot-v1",
                    "pr_id": pr_id,
                    "review_id": f"synthetic-review-{index:02d}",
                    "finding_id": finding_id,
                    "finding_sha256": sha256_value(["finding", finding_id]),
                    "evidence_sha256": sha256_value(["evidence", finding_id]),
                    "feedback_eligible": True,
                    "synthetic": True,
                    "subject_sha256": "",
                },
                "subject_sha256",
            )
        )
    assignments = {
        f"synthetic-pr-{index:02d}": participant_ids[(index - 1) % len(participant_ids)]
        for index in range(1, 21)
    }
    feedback_packets = build_feedback_packets(
        finding_subjects,
        cohort,
        assignments,
        generated_at="2026-01-05T00:00:00Z",
    )
    feedback_responses = []
    for index, packet in enumerate(feedback_packets, start=1):
        decision = ("accepted", "fixed", "rejected")[index % 3]
        finding_id = packet["items"][0]["finding_id"]
        feedback_responses.append(
            with_artifact_hash(
                {
                    "schema_version": 1,
                    "packet_id": packet["packet_id"],
                    "packet_sha256": packet["packet_sha256"],
                    "participant_id": packet["participant_id"],
                    "pr_id": packet["pr_id"],
                    "finding_id": finding_id,
                    "decision": decision,
                    "rationale": (
                        "Synthetic rejection rationale" if decision == "rejected" else None
                    ),
                    "created_at": "2026-01-06T00:00:00Z",
                    "fixed_at": "2026-01-06T00:01:00Z" if decision == "fixed" else None,
                    "completed_by_human": False,
                    "synthetic": True,
                    "response_sha256": "",
                },
                "response_sha256",
            )
        )
    feedback_responses = validate_feedback_responses(
        feedback_responses, feedback_packets, finding_subjects, cohort
    )

    review_times = []
    receipts = []
    for index in range(1, 21):
        pr_id = f"synthetic-pr-{index:02d}"
        review_times.append(
            with_artifact_hash(
                {
                    "schema_version": 1,
                    "pilot_id": "synthetic-pilot-v1",
                    "session_id": f"synthetic-session-{index:02d}",
                    "participant_id": assignments[pr_id],
                    "pr_id": pr_id,
                    "started_at": "2026-01-07T00:00:00Z",
                    "completed_at": "2026-01-07T00:02:00Z",
                    "active_seconds": 90.0,
                    "paused_seconds": 30.0,
                    "completed_by_human": False,
                    "synthetic": True,
                    "record_sha256": "",
                },
                "record_sha256",
            )
        )
        finding_id = f"synthetic-finding-{index:02d}"
        receipts.append(
            with_artifact_hash(
                {
                    "schema_version": 1,
                    "pilot_id": "synthetic-pilot-v1",
                    "run_id": f"synthetic-run-{index:02d}",
                    "track": "business",
                    "role": "pilot",
                    "pr_id": pr_id,
                    "attempt_number": 1,
                    "headline": True,
                    "provider": "synthetic-provider",
                    "exact_model_snapshot": "synthetic-no-model",
                    "temperature": 0.0,
                    "started_at": "2026-01-07T00:00:00Z",
                    "completed_at": "2026-01-07T00:00:10Z",
                    "status": "completed",
                    "logical_calls": 0,
                    "http_attempts": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_microcny": 0,
                    "latency_seconds": 10.0,
                    "error_category": None,
                    "feedback_eligible_finding_ids": [finding_id],
                    "raw_trace_sha256": sha256_value(["trace", pr_id]),
                    "raw_trace_retain_until": "2026-02-07T00:00:00Z",
                    "synthetic": True,
                    "receipt_sha256": "",
                },
                "receipt_sha256",
            )
        )
    review_times = validate_review_times(review_times, cohort, participants)
    receipts = validate_run_receipts(receipts, cohort, authorization)
    run_manifest = build_run_manifest(receipts, cohort, created_at="2026-01-08T00:00:00Z")

    annotation_subjects = [
        with_artifact_hash(
            {
                "schema_version": 1,
                "pilot_id": "synthetic-pilot-v1",
                "stage": "gold",
                "subject_kind": "gold_candidate",
                "subject_id": f"synthetic-gold-{index}",
                "pr_id": f"synthetic-pr-{index:02d}",
                "subject_sha256": sha256_value(["gold-subject", index]),
                "evidence_sha256": sha256_value(["gold-evidence", index]),
                "severity": "high" if index == 1 else "medium",
                "synthetic": True,
                "record_sha256": "",
            },
            "record_sha256",
        )
        for index in (1, 2)
    ]
    packet_a = build_independent_annotation_packet(
        annotation_subjects,
        cohort,
        stage="gold",
        annotator_id="synthetic-annotator-a",
        rubric_sha256=sha256_value("synthetic rubric"),
        order_seed=1,
        generated_at="2026-01-05T00:00:00Z",
    )
    packet_b = build_independent_annotation_packet(
        annotation_subjects,
        cohort,
        stage="gold",
        annotator_id="synthetic-annotator-b",
        rubric_sha256=sha256_value("synthetic rubric"),
        order_seed=2,
        generated_at="2026-01-05T00:00:00Z",
    )
    if [item["subject_id"] for item in packet_a["items"]] == [
        item["subject_id"] for item in packet_b["items"]
    ]:
        packet_b = build_independent_annotation_packet(
            annotation_subjects,
            cohort,
            stage="gold",
            annotator_id="synthetic-annotator-b",
            rubric_sha256=sha256_value("synthetic rubric"),
            order_seed=5,
            generated_at="2026-01-05T00:00:00Z",
        )
    subject_by_id = {row["subject_id"]: row for row in annotation_subjects}

    def annotation_response(
        packet: Mapping[str, Any], subject_id: str, label: str
    ) -> dict[str, Any]:
        subject = subject_by_id[subject_id]
        return with_artifact_hash(
            {
                "schema_version": 1,
                "packet_id": packet["packet_id"],
                "packet_sha256": packet["packet_sha256"],
                "annotator_id": packet["annotator_id"],
                "subject_id": subject_id,
                "label": label,
                "gold_id": None,
                "discovered": True,
                "severity": subject["severity"],
                "rationale": "Synthetic format-validation rationale",
                "evidence_sha256": subject["evidence_sha256"],
                "created_at": "2026-01-06T00:00:00Z",
                "completed_by_human": False,
                "synthetic": True,
                "response_sha256": "",
            },
            "response_sha256",
        )

    responses_a = validate_annotation_responses(
        [
            annotation_response(packet_a, "synthetic-gold-1", "valid_defect"),
            annotation_response(packet_a, "synthetic-gold-2", "not_defect"),
        ],
        packet_a,
        annotation_subjects,
        cohort,
    )
    responses_b = validate_annotation_responses(
        [
            annotation_response(packet_b, "synthetic-gold-1", "uncertain"),
            annotation_response(packet_b, "synthetic-gold-2", "not_defect"),
        ],
        packet_b,
        annotation_subjects,
        cohort,
    )
    adjudication_packet = build_adjudication_packet(
        annotation_subjects,
        cohort,
        packet_a,
        packet_b,
        responses_a,
        responses_b,
        adjudicator_id="synthetic-adjudicator-c",
        order_seed=7,
        generated_at="2026-01-06T00:01:00Z",
    )
    if adjudication_packet is None:
        _fail("synthetic fixture must exercise adjudication")
    adjudication_subject = adjudication_packet["items"][0]
    response_c = with_artifact_hash(
        {
            "schema_version": 1,
            "packet_id": adjudication_packet["packet_id"],
            "packet_sha256": adjudication_packet["packet_sha256"],
            "annotator_id": adjudication_packet["annotator_id"],
            "subject_id": adjudication_subject["subject_id"],
            "label": "valid_defect",
            "gold_id": None,
            "discovered": None,
            "severity": "high",
            "rationale": "Synthetic adjudication rationale",
            "evidence_sha256": adjudication_subject["evidence_sha256"],
            "created_at": "2026-01-06T00:02:00Z",
            "completed_by_human": False,
            "synthetic": True,
            "response_sha256": "",
        },
        "response_sha256",
    )
    responses_c = validate_annotation_responses(
        [response_c], adjudication_packet, annotation_subjects, cohort
    )
    gold_freeze = build_gold_freeze(
        authorization=authorization,
        cohort=cohort,
        packet_a=packet_a,
        packet_b=packet_b,
        responses_a=responses_a,
        responses_b=responses_b,
        adjudication_packet=adjudication_packet,
        responses_c=responses_c,
        frozen_at="2026-01-07T00:00:00Z",
        external_git_commit="a" * 40,
        trusted_cohort_sha256=sha256_value("synthetic trusted cohort"),
    )
    system_subjects = [
        with_artifact_hash(
            {
                "schema_version": 1,
                "pilot_id": "synthetic-pilot-v1",
                "stage": "system",
                "subject_kind": "system_finding",
                "subject_id": f"synthetic-system-finding-{index}",
                "pr_id": f"synthetic-pr-{index + 2:02d}",
                "subject_sha256": sha256_value(["system-subject", index]),
                "evidence_sha256": sha256_value(["system-evidence", index]),
                "severity": None,
                "synthetic": True,
                "record_sha256": "",
            },
            "record_sha256",
        )
        for index in (1, 2)
    ]
    annotation_subjects.extend(system_subjects)
    system_packet_a = build_independent_annotation_packet(
        annotation_subjects,
        cohort,
        stage="system",
        annotator_id="synthetic-annotator-a",
        rubric_sha256=sha256_value("synthetic rubric"),
        order_seed=11,
        generated_at="2026-01-08T00:00:00Z",
    )
    system_seed_b = 12
    while True:
        system_packet_b = build_independent_annotation_packet(
            annotation_subjects,
            cohort,
            stage="system",
            annotator_id="synthetic-annotator-b",
            rubric_sha256=sha256_value("synthetic rubric"),
            order_seed=system_seed_b,
            generated_at="2026-01-08T00:00:00Z",
        )
        if [item["subject_id"] for item in system_packet_a["items"]] != [
            item["subject_id"] for item in system_packet_b["items"]
        ]:
            break
        system_seed_b += 1

    def system_response(
        packet: Mapping[str, Any], subject: Mapping[str, Any]
    ) -> dict[str, Any]:
        return with_artifact_hash(
            {
                "schema_version": 1,
                "packet_id": packet["packet_id"],
                "packet_sha256": packet["packet_sha256"],
                "annotator_id": packet["annotator_id"],
                "subject_id": subject["subject_id"],
                "label": "invalid",
                "gold_id": None,
                "discovered": None,
                "severity": None,
                "rationale": "Synthetic post-run format-validation rationale",
                "evidence_sha256": subject["evidence_sha256"],
                "created_at": "2026-01-08T00:01:00Z",
                "completed_by_human": False,
                "synthetic": True,
                "response_sha256": "",
            },
            "response_sha256",
        )

    system_responses_a = validate_annotation_responses(
        [system_response(system_packet_a, subject) for subject in system_subjects],
        system_packet_a,
        annotation_subjects,
        cohort,
    )
    system_responses_b = validate_annotation_responses(
        [system_response(system_packet_b, subject) for subject in system_subjects],
        system_packet_b,
        annotation_subjects,
        cohort,
    )
    business_report = build_business_report(
        authorization=authorization,
        participants=participants,
        repositories=repositories,
        cohort=cohort,
        finding_subjects=finding_subjects,
        feedback_packets=feedback_packets,
        feedback_responses=feedback_responses,
        review_times=review_times,
        receipts=receipts,
        run_manifest=run_manifest,
        generated_at="2026-01-09T00:00:00Z",
    )
    bundle = {
        "schema_version": 1,
        "phase_id": PHASE_ID,
        "authorization": authorization,
        "participants": participants,
        "repositories": repositories,
        "selection_plan": selection_plan,
        "selection_log": selection_rows,
        "cohort": cohort,
        "finding_subjects": finding_subjects,
        "feedback_packets": feedback_packets,
        "feedback_responses": feedback_responses,
        "review_times": review_times,
        "run_receipts": receipts,
        "run_manifest": run_manifest,
        "annotation_subjects": annotation_subjects,
        "annotation_packets": [
            packet_a,
            packet_b,
            adjudication_packet,
            system_packet_a,
            system_packet_b,
        ],
        "annotation_responses": [
            {"packet_id": packet_a["packet_id"], "responses": responses_a},
            {"packet_id": packet_b["packet_id"], "responses": responses_b},
            {"packet_id": adjudication_packet["packet_id"], "responses": responses_c},
            {
                "packet_id": system_packet_a["packet_id"],
                "responses": system_responses_a,
            },
            {
                "packet_id": system_packet_b["packet_id"],
                "responses": system_responses_b,
            },
        ],
        "gold_freeze": gold_freeze,
        "business_report": business_report,
        "formal_quality_report": None,
        "bundle_sha256": "",
    }
    return with_artifact_hash(bundle, "bundle_sha256")


def validate_bundle_fixture(raw: Any) -> dict[str, Any]:
    """Expand the compact committed descriptor into the full synthetic bundle."""

    descriptor = _expect_dict(raw, "bundle_fixture")
    _exact_keys(descriptor, BUNDLE_FIXTURE_KEYS, "bundle_fixture")
    if descriptor["schema_version"] != 1 or descriptor["phase_id"] != PHASE_ID:
        _fail("bundle fixture schema or phase is invalid")
    if descriptor["fixture"] != "built_in_synthetic_v1":
        _fail("bundle fixture kind is unsupported")
    _expect_sha(
        descriptor["expected_bundle_sha256"],
        "bundle_fixture.expected_bundle_sha256",
    )
    if _expect_bool(
        descriptor["business_claim_allowed"],
        "bundle_fixture.business_claim_allowed",
    ):
        _fail("synthetic fixture cannot allow a business claim")
    if _expect_bool(
        descriptor["quality_claim_allowed"],
        "bundle_fixture.quality_claim_allowed",
    ):
        _fail("synthetic fixture cannot allow a quality claim")
    validate_artifact_hash(descriptor, "fixture_sha256", "bundle_fixture")
    bundle = build_synthetic_bundle()
    if bundle["bundle_sha256"] != descriptor["expected_bundle_sha256"]:
        _fail("built-in synthetic bundle differs from the committed descriptor")
    return validate_bundle(bundle)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Phase 9G real-pilot artifacts without external calls"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    authorization = subparsers.add_parser("validate-authorization")
    authorization.add_argument("--authorization", required=True)

    seal = subparsers.add_parser("seal-authorization")
    seal.add_argument("--authorization", required=True)
    seal.add_argument("--output", required=True)

    hash_artifact = subparsers.add_parser("hash-artifact")
    hash_artifact.add_argument("--input", required=True)
    hash_artifact.add_argument("--hash-field", required=True)
    hash_artifact.add_argument("--output", required=True)
    hash_artifact.add_argument("--jsonl", action="store_true")

    cohort = subparsers.add_parser("materialize-cohort")
    cohort.add_argument("--plan", required=True)
    cohort.add_argument("--selection-log", required=True)
    cohort.add_argument("--repositories", required=True)
    cohort.add_argument("--expected-source-commit", required=True)
    cohort.add_argument("--materialized-at", required=True)
    cohort.add_argument("--output", required=True)

    annotations = subparsers.add_parser("export-annotation-packets")
    annotations.add_argument("--subjects", required=True)
    annotations.add_argument("--cohort", required=True)
    annotations.add_argument("--stage", choices=["gold", "system"], required=True)
    annotations.add_argument("--annotator-a", required=True)
    annotations.add_argument("--annotator-b", required=True)
    annotations.add_argument("--rubric-sha256", required=True)
    annotations.add_argument("--seed-a", required=True, type=int)
    annotations.add_argument("--seed-b", required=True, type=int)
    annotations.add_argument("--generated-at", required=True)
    annotations.add_argument("--output-a", required=True)
    annotations.add_argument("--output-b", required=True)

    bundle = subparsers.add_parser("validate-bundle")
    bundle.add_argument("--bundle", required=True)
    bundle.add_argument("--expected-source-commit")

    formal = subparsers.add_parser("validate-formal-report")
    formal.add_argument("--report", required=True)
    formal.add_argument("--authorization", required=True)
    formal.add_argument("--gold-freeze", required=True)
    formal.add_argument("--validated-at", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "validate-authorization":
            result = authorization_readiness(load_json(args.authorization))
        elif args.command == "seal-authorization":
            draft = _expect_dict(load_json(args.authorization), "authorization")
            draft = {**draft, "authorization_sha256": ""}
            validate_authorization(draft, require_hash=False)
            result = with_artifact_hash(draft, "authorization_sha256")
            _write_json(args.output, result)
        elif args.command == "hash-artifact":
            if not IDENTIFIER_RE.fullmatch(args.hash_field):
                _fail("hash field must be a stable identifier")
            if args.jsonl:
                raw_rows = load_jsonl(args.input)
                sealed_rows = []
                for index, raw in enumerate(raw_rows):
                    row = _expect_dict(raw, f"artifact[{index}]")
                    if args.hash_field not in row:
                        _fail(f"artifact[{index}] is missing the requested hash field")
                    sealed_rows.append(
                        with_artifact_hash({**row, args.hash_field: ""}, args.hash_field)
                    )
                _write_jsonl(args.output, sealed_rows)
                result = {
                    "valid": True,
                    "rows": len(sealed_rows),
                    "artifact_sha256": sha256_value(sealed_rows),
                }
            else:
                raw = _expect_dict(load_json(args.input), "artifact")
                if args.hash_field not in raw:
                    _fail("artifact is missing the requested hash field")
                result = with_artifact_hash(
                    {**raw, args.hash_field: ""}, args.hash_field
                )
                _write_json(args.output, result)
        elif args.command == "materialize-cohort":
            plan = load_json(args.plan)
            validate_selection_plan(
                plan,
                expected_source_commit=args.expected_source_commit,
            )
            result = materialize_cohort(
                plan,
                load_jsonl(args.selection_log),
                load_json(args.repositories),
                materialized_at=args.materialized_at,
            )
            _write_json(args.output, result)
        elif args.command == "export-annotation-packets":
            cohort = load_json(args.cohort)
            subjects = validate_annotation_subjects(load_jsonl(args.subjects), cohort)
            packet_a = build_independent_annotation_packet(
                subjects,
                cohort,
                stage=args.stage,
                annotator_id=args.annotator_a,
                rubric_sha256=args.rubric_sha256,
                order_seed=args.seed_a,
                generated_at=args.generated_at,
            )
            packet_b = build_independent_annotation_packet(
                subjects,
                cohort,
                stage=args.stage,
                annotator_id=args.annotator_b,
                rubric_sha256=args.rubric_sha256,
                order_seed=args.seed_b,
                generated_at=args.generated_at,
            )
            validate_independent_annotation_pair(packet_a, packet_b)
            _write_json(args.output_a, packet_a)
            _write_json(args.output_b, packet_b)
            result = {
                "valid": True,
                "packet_a_sha256": packet_a["packet_sha256"],
                "packet_b_sha256": packet_b["packet_sha256"],
            }
        elif args.command == "validate-bundle":
            requested = _reject_forbidden_path(args.bundle)
            bundle_path = requested / "bundle.json" if requested.is_dir() else requested
            loaded = load_json(bundle_path)
            if isinstance(loaded, dict) and "fixture" in loaded:
                result = validate_bundle_fixture(loaded)
            else:
                result = validate_bundle(
                    loaded,
                    expected_source_commit=args.expected_source_commit,
                )
        else:
            result = validate_formal_quality_report(
                load_json(args.report),
                validate_authorization(load_json(args.authorization)),
                validate_gold_freeze(load_json(args.gold_freeze)),
                validated_at=args.validated_at,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
