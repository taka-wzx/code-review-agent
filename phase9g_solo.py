"""Fail-closed offline artifacts for Phase 9G-Solo Exploratory v1.

This module is deliberately standard-library only.  It validates and derives
hash-bound artifacts, but it never opens a network connection, invokes a model,
deploys software, or writes to GitHub.  Paths containing ``eval`` or ``holdout``
are rejected before they are opened.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Mapping, NoReturn, Sequence


SCHEMA_VERSION = 1
PHASE_ID = "phase9g-solo-exploratory-v1"
EVIDENCE_TYPE = "single_participant_exploratory"
SELECTION_SEED_DOMAIN = b"phase9g-solo-selection-v1\0"
SELECTION_RULE = "lowest deterministic eligible ranks before output inspection"
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
    "participant_id",
    "participant_confirmed_real",
    "repository_ids",
    "pr_count",
    "selection_rule",
    "mode",
    "model",
    "retention",
    "external_operations",
    "approved_by",
    "approved_at",
    "expires_at",
    "synthetic",
    "authorization_sha256",
}
MODEL_KEYS = {
    "provider",
    "exact_model_snapshot",
    "runtime_config_sha256",
    "temperature",
    "max_logical_calls",
    "max_http_attempts",
    "max_input_tokens",
    "max_output_tokens",
    "max_cost_microcny",
    "real_paid_calls",
    "read_raw_diff",
}
RETENTION_KEYS = {"data_days", "feedback_days", "raw_trace_days"}
EXTERNAL_KEYS = {
    "staging_deploy",
    "deployment_target",
    "real_github_api",
    "create_comments_or_checks",
    "github_publish",
}
PARTICIPANT_MANIFEST_KEYS = {
    "schema_version",
    "phase_id",
    "solo_id",
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
    "solo_id",
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
    "solo_id",
    "seed",
    "seed_derivation",
    "selection_window",
    "repository_ids",
    "target_prs",
    "exclusion_reasons",
    "generated_at",
    "synthetic",
    "plan_sha256",
}
SEED_DERIVATION_KEYS = {"method", "source_commit"}
SELECTION_WINDOW_KEYS = {"start", "end"}
SELECTION_ROW_KEYS = {
    "schema_version",
    "solo_id",
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
    "solo_id",
    "materialized_at",
    "selection_plan_sha256",
    "selection_log_sha256",
    "entries",
    "synthetic",
    "cohort_sha256",
}
COHORT_ENTRY_KEYS = {
    "repository_id",
    "pr_id",
    "snapshot_sha256",
    "diff_sha256",
    "selected_at",
}
FINDING_KEYS = {
    "schema_version",
    "solo_id",
    "pr_id",
    "review_id",
    "finding_id",
    "finding_sha256",
    "evidence_sha256",
    "feedback_eligible",
    "synthetic",
    "subject_sha256",
}
FEEDBACK_KEYS = {
    "schema_version",
    "solo_id",
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
    "solo_id",
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
RECEIPT_KEYS = {
    "schema_version",
    "solo_id",
    "run_id",
    "pr_id",
    "attempt_number",
    "headline",
    "provider",
    "exact_model_snapshot",
    "runtime_config_sha256",
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
    "solo_id",
    "generated_at",
    "cohort_sha256",
    "receipt_set_sha256",
    "selected_prs",
    "attempts",
    "headline_attempts",
    "cumulative_usage",
    "synthetic",
    "manifest_sha256",
}
USAGE_KEYS = {
    "logical_calls",
    "http_attempts",
    "input_tokens",
    "output_tokens",
    "cost_microcny",
}
REPORT_KEYS = {
    "schema_version",
    "phase_id",
    "report_version",
    "solo_id",
    "evidence_type",
    "generated_at",
    "authorization_sha256",
    "cohort_sha256",
    "run_manifest_sha256",
    "wording",
    "metrics",
    "claim_gates",
    "synthetic",
    "report_sha256",
}
WORDING_KEYS = {"observation", "quality_statement"}
CLAIM_GATE_KEYS = {
    "exploratory_summary_allowed",
    "business_claim_allowed",
    "quality_claim_allowed",
    "formal_quality_status",
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
    "feedback_responses",
    "review_times",
    "run_receipts",
    "run_manifest",
    "solo_report",
    "bundle_sha256",
}
BUNDLE_FIXTURE_KEYS = {
    "schema_version",
    "phase_id",
    "fixture",
    "expected_bundle_sha256",
    "evidence_type",
    "exploratory_summary_allowed",
    "business_claim_allowed",
    "quality_claim_allowed",
    "fixture_sha256",
}


class ValidationError(ValueError):
    """A stable, content-free Solo artifact validation failure."""


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
        parts = []
        if missing:
            parts.append(f"missing keys {missing}")
        if unknown:
            parts.append("unknown keys present")
        _fail(f"{where}: {'; '.join(parts)}")


def _expect_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{where} must be a boolean")
    return value


def _expect_int(value: Any, where: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{where} must be an integer >= {minimum}")
    return value


def _expect_number(value: Any, where: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{where} must be a finite number >= {minimum}")
    try:
        result = float(value)
    except OverflowError:
        _fail(f"{where} must be a finite number >= {minimum}")
    if not math.isfinite(result) or result < minimum:
        _fail(f"{where} must be a finite number >= {minimum}")
    return result


def _expect_str(value: Any, where: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail(f"{where} must be a non-empty string of at most {maximum} characters")
    return value


def _expect_nullable_str(value: Any, where: str, *, maximum: int = 4096) -> str | None:
    if value is None:
        return None
    return _expect_str(value, where, maximum=maximum)


def _expect_identifier(value: Any, where: str) -> str:
    result = _expect_str(value, where, maximum=200)
    if not IDENTIFIER_RE.fullmatch(result):
        _fail(f"{where} must be a stable opaque identifier")
    return result


def _expect_sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        _fail(f"{where} must be a lowercase SHA-256")
    return value


def _expect_unique_identifiers(value: Any, where: str, *, minimum: int = 1) -> list[str]:
    items = _expect_list(value, where)
    result = [_expect_identifier(item, f"{where}[]") for item in items]
    if len(result) < minimum or len(result) != len(set(result)):
        _fail(f"{where} must contain at least {minimum} unique identifiers")
    return result


def parse_timestamp(value: Any, where: str) -> datetime:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        _fail(f"{where} must use canonical UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        _fail(f"{where} is not a valid UTC timestamp")


def canonical_bytes(value: Any) -> bytes:
    """Return canonical UTF-8 JSON, rejecting non-finite numbers."""

    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError("artifact is not canonical-JSON compatible") from exc
    return rendered.encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def with_artifact_hash(value: Mapping[str, Any], hash_field: str) -> dict[str, Any]:
    if hash_field not in value:
        _fail(f"artifact is missing hash field {hash_field}")
    draft = dict(value)
    draft[hash_field] = ""
    draft[hash_field] = sha256_value(draft)
    return draft


def validate_artifact_hash(value: Mapping[str, Any], hash_field: str, where: str) -> None:
    declared = _expect_sha(value.get(hash_field), f"{where}.{hash_field}")
    draft = dict(value)
    draft[hash_field] = ""
    if declared != sha256_value(draft):
        _fail(f"{where} hash mismatch")


def _reject_forbidden_path(path: str | Path) -> Path:
    requested = Path(path)
    resolved = requested.resolve(strict=False)
    if any(part.casefold() in FORBIDDEN_PATH_PARTS for part in resolved.parts):
        _fail("path is forbidden by the Solo contract")
    return requested


def load_json(path: str | Path) -> Any:
    requested = _reject_forbidden_path(path)
    try:
        with requested.open("r", encoding="utf-8") as handle:
            return json.load(
                handle,
                parse_constant=lambda _: _fail("non-finite JSON is forbidden"),
                object_pairs_hook=_object_without_duplicates,
            )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("unable to load canonical JSON artifact") from exc


def load_jsonl(path: str | Path) -> list[Any]:
    requested = _reject_forbidden_path(path)
    rows: list[Any] = []
    try:
        with requested.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(
                        json.loads(
                            line,
                            parse_constant=lambda _: _fail("non-finite JSON is forbidden"),
                            object_pairs_hook=_object_without_duplicates,
                        )
                    )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("unable to load canonical JSONL artifact") from exc
    return rows


def _write_json(path: str | Path, value: Any) -> None:
    requested = _reject_forbidden_path(path)
    requested.parent.mkdir(parents=True, exist_ok=True)
    requested.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: str | Path, rows: Sequence[Mapping[str, Any]]) -> None:
    requested = _reject_forbidden_path(path)
    requested.parent.mkdir(parents=True, exist_ok=True)
    rendered = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        for row in rows
    )
    requested.write_text(rendered, encoding="utf-8")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON object keys are forbidden")
        result[key] = value
    return result


def validate_authorization(
    raw: Any,
    *,
    require_hash: bool = True,
) -> dict[str, Any]:
    authorization = _expect_dict(raw, "authorization")
    _exact_keys(authorization, AUTHORIZATION_KEYS, "authorization")
    if authorization["schema_version"] != SCHEMA_VERSION or authorization["phase_id"] != PHASE_ID:
        _fail("authorization schema or phase is invalid")
    _expect_identifier(authorization["authorization_id"], "authorization.authorization_id")
    participant_id = _expect_identifier(
        authorization["participant_id"], "authorization.participant_id"
    )
    confirmed = _expect_bool(
        authorization["participant_confirmed_real"],
        "authorization.participant_confirmed_real",
    )
    repository_ids = _expect_unique_identifiers(
        authorization["repository_ids"], "authorization.repository_ids"
    )
    pr_count = _expect_int(authorization["pr_count"], "authorization.pr_count", minimum=5)
    if pr_count > 10:
        _fail("authorization.pr_count must be between 5 and 10")
    if authorization["selection_rule"] != SELECTION_RULE:
        _fail("authorization selection rule differs from the frozen Solo rule")
    if authorization["mode"] != "shadow":
        _fail("Solo mode must be shadow")

    model = _expect_dict(authorization["model"], "authorization.model")
    _exact_keys(model, MODEL_KEYS, "authorization.model")
    _expect_str(model["provider"], "authorization.model.provider", maximum=200)
    _expect_str(
        model["exact_model_snapshot"],
        "authorization.model.exact_model_snapshot",
        maximum=300,
    )
    _expect_sha(model["runtime_config_sha256"], "authorization.model.runtime_config_sha256")
    _expect_number(model["temperature"], "authorization.model.temperature")
    max_calls = _expect_int(
        model["max_logical_calls"], "authorization.model.max_logical_calls"
    )
    max_http = _expect_int(model["max_http_attempts"], "authorization.model.max_http_attempts")
    if max_http < max_calls:
        _fail("maximum HTTP attempts cannot be lower than maximum logical calls")
    _expect_int(model["max_input_tokens"], "authorization.model.max_input_tokens")
    _expect_int(model["max_output_tokens"], "authorization.model.max_output_tokens")
    _expect_int(model["max_cost_microcny"], "authorization.model.max_cost_microcny")
    _expect_bool(model["real_paid_calls"], "authorization.model.real_paid_calls")
    _expect_bool(model["read_raw_diff"], "authorization.model.read_raw_diff")

    retention = _expect_dict(authorization["retention"], "authorization.retention")
    _exact_keys(retention, RETENTION_KEYS, "authorization.retention")
    for key in sorted(RETENTION_KEYS):
        _expect_int(retention[key], f"authorization.retention.{key}", minimum=1)

    external = _expect_dict(
        authorization["external_operations"], "authorization.external_operations"
    )
    _exact_keys(external, EXTERNAL_KEYS, "authorization.external_operations")
    for key in EXTERNAL_KEYS - {"deployment_target"}:
        if _expect_bool(external[key], f"authorization.external_operations.{key}"):
            _fail(f"Solo structurally forbids external operation {key}")
    if external["deployment_target"] is not None:
        _fail("Solo deployment target must be null")

    _expect_identifier(authorization["approved_by"], "authorization.approved_by")
    approved_at = parse_timestamp(authorization["approved_at"], "authorization.approved_at")
    expires_at = parse_timestamp(authorization["expires_at"], "authorization.expires_at")
    if expires_at <= approved_at:
        _fail("authorization expiry must follow approval")
    synthetic = _expect_bool(authorization["synthetic"], "authorization.synthetic")
    if synthetic and confirmed:
        _fail("a synthetic participant cannot be confirmed real")
    if not synthetic and not confirmed:
        _fail("a real Solo authorization requires one confirmed real participant")
    if participant_id in repository_ids:
        _fail("participant and repository identifiers must be distinct")
    if require_hash:
        validate_artifact_hash(authorization, "authorization_sha256", "authorization")
    elif authorization["authorization_sha256"] not in ("", None):
        _expect_sha(authorization["authorization_sha256"], "authorization.authorization_sha256")
    return authorization


def authorization_readiness(
    raw: Any,
    *,
    at: str | datetime | None = None,
) -> dict[str, Any]:
    authorization = validate_authorization(raw)
    if at is None:
        instant = datetime.now(timezone.utc)
    elif isinstance(at, datetime):
        instant = at.astimezone(timezone.utc)
    else:
        instant = parse_timestamp(at, "readiness.at")
    approved_at = parse_timestamp(authorization["approved_at"], "authorization.approved_at")
    expires_at = parse_timestamp(authorization["expires_at"], "authorization.expires_at")
    active = approved_at <= instant < expires_at
    model = authorization["model"]
    positive_budget = all(
        model[key] > 0
        for key in (
            "max_logical_calls",
            "max_http_attempts",
            "max_input_tokens",
            "max_output_tokens",
            "max_cost_microcny",
        )
    )
    real_scope = bool(
        active
        and not authorization["synthetic"]
        and authorization["participant_confirmed_real"]
    )
    model_scope = bool(
        real_scope
        and positive_budget
        and model["real_paid_calls"]
        and model["read_raw_diff"]
    )
    return {
        "valid": True,
        "authorization_id": authorization["authorization_id"],
        "synthetic": authorization["synthetic"],
        "unexpired": active,
        "scopes": {
            "real_exploratory_run": real_scope,
            "model_execution": model_scope,
            "real_github_api": False,
            "github_publish": False,
            "staging_deploy": False,
            "business_claim": False,
            "quality_claim": False,
        },
    }


def validate_participant_manifest(
    raw: Any,
    authorization: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = _expect_dict(raw, "participants")
    _exact_keys(manifest, PARTICIPANT_MANIFEST_KEYS, "participants")
    if manifest["schema_version"] != 1 or manifest["phase_id"] != PHASE_ID:
        _fail("participant manifest schema or phase is invalid")
    _expect_identifier(manifest["solo_id"], "participants.solo_id")
    _expect_identifier(
        manifest["identity_custodian_id"], "participants.identity_custodian_id"
    )
    _expect_identifier(manifest["consent_version"], "participants.consent_version")
    generated_at = parse_timestamp(manifest["generated_at"], "participants.generated_at")
    participants = _expect_list(manifest["participants"], "participants.participants")
    if len(participants) != 1:
        _fail("Solo requires exactly one participant record")
    participant = _expect_dict(participants[0], "participants.participants[0]")
    _exact_keys(participant, PARTICIPANT_KEYS, "participants.participants[0]")
    if participant["participant_id"] != authorization["participant_id"]:
        _fail("participant ID differs from the authorization")
    confirmed = _expect_bool(participant["confirmed_real"], "participant.confirmed_real")
    if confirmed != authorization["participant_confirmed_real"]:
        _fail("participant real-person confirmation differs from authorization")
    if participant["role"] != "developer":
        _fail("Solo participant role must be developer")
    consented_at = parse_timestamp(participant["consented_at"], "participant.consented_at")
    consent_expires_at = parse_timestamp(
        participant["consent_expires_at"], "participant.consent_expires_at"
    )
    if not (consented_at <= generated_at < consent_expires_at):
        _fail("participant consent is not active at manifest generation")
    scopes = _expect_list(participant["consent_scope"], "participant.consent_scope")
    if set(scopes) != {"exploratory_feedback", "review_time"} or len(scopes) != 2:
        _fail("participant consent scope must exactly cover feedback and review time")
    repository_ids = _expect_unique_identifiers(
        participant["repository_ids"], "participant.repository_ids"
    )
    if set(repository_ids) != set(authorization["repository_ids"]):
        _fail("participant repository bindings differ from authorization")
    if participant["feedback_retention_days"] != authorization["retention"]["feedback_days"]:
        _fail("participant feedback retention differs from authorization")
    if not _expect_bool(
        participant["withdrawal_acknowledged"], "participant.withdrawal_acknowledged"
    ):
        _fail("participant withdrawal process must be acknowledged")
    synthetic = _expect_bool(manifest["synthetic"], "participants.synthetic")
    if synthetic != authorization["synthetic"]:
        _fail("participant synthetic provenance differs from authorization")
    validate_artifact_hash(manifest, "manifest_sha256", "participants")
    return manifest


def validate_repository_manifest(
    raw: Any,
    authorization: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = _expect_dict(raw, "repositories")
    _exact_keys(manifest, REPOSITORY_MANIFEST_KEYS, "repositories")
    if manifest["schema_version"] != 1 or manifest["phase_id"] != PHASE_ID:
        _fail("repository manifest schema or phase is invalid")
    _expect_identifier(manifest["solo_id"], "repositories.solo_id")
    generated_at = parse_timestamp(manifest["generated_at"], "repositories.generated_at")
    rows = _expect_list(manifest["repositories"], "repositories.repositories")
    if len(rows) != len(authorization["repository_ids"]):
        _fail("repository manifest does not cover the authorized repository set")
    seen: set[str] = set()
    for index, raw_row in enumerate(rows):
        row = _expect_dict(raw_row, f"repositories.repositories[{index}]")
        _exact_keys(row, REPOSITORY_KEYS, f"repositories.repositories[{index}]")
        repository_id = _expect_identifier(row["repository_id"], "repository.repository_id")
        if repository_id in seen:
            _fail("repository manifest repeats a repository ID")
        seen.add(repository_id)
        _expect_sha(row["locator_sha256"], "repository.locator_sha256")
        _expect_identifier(row["authorized_by"], "repository.authorized_by")
        authorized_at = parse_timestamp(row["authorized_at"], "repository.authorized_at")
        expires_at = parse_timestamp(
            row["authorization_expires_at"], "repository.authorization_expires_at"
        )
        if not (authorized_at <= generated_at < expires_at):
            _fail("repository authority is not active at manifest generation")
        if row["raw_diff_read_authorized"] != authorization["model"]["read_raw_diff"]:
            _fail("repository raw-diff authority differs from top-level authorization")
        if _expect_bool(
            row["real_github_api_authorized"], "repository.real_github_api_authorized"
        ):
            _fail("Solo repositories cannot authorize the real GitHub API")
        if row["publish_mode"] != "shadow":
            _fail("Solo repository mode must be shadow")
        if _expect_bool(row["publication_authorized"], "repository.publication_authorized"):
            _fail("Solo repositories cannot authorize publication")
        if row["data_retention_days"] != authorization["retention"]["data_days"]:
            _fail("repository data retention differs from authorization")
        validate_artifact_hash(row, "repository_sha256", "repository")
    if seen != set(authorization["repository_ids"]):
        _fail("repository IDs differ from authorization")
    synthetic = _expect_bool(manifest["synthetic"], "repositories.synthetic")
    if synthetic != authorization["synthetic"]:
        _fail("repository synthetic provenance differs from authorization")
    validate_artifact_hash(manifest, "manifest_sha256", "repositories")
    return manifest


def derive_selection_seed(source_commit: str) -> str:
    if not isinstance(source_commit, str) or not HEX40_RE.fullmatch(source_commit):
        _fail("selection source commit must be a lowercase 40-character Git commit")
    return hashlib.sha256(SELECTION_SEED_DOMAIN + source_commit.encode("ascii")).hexdigest()


def selection_rank(seed: str, pr_id: str) -> str:
    _expect_sha(seed, "selection seed")
    _expect_identifier(pr_id, "opaque PR ID")
    return hashlib.sha256((seed + "\n" + pr_id).encode("utf-8")).hexdigest()


def validate_selection_plan(
    raw: Any,
    *,
    expected_source_commit: str | None = None,
) -> dict[str, Any]:
    plan = _expect_dict(raw, "selection_plan")
    _exact_keys(plan, SELECTION_PLAN_KEYS, "selection_plan")
    if plan["schema_version"] != 1 or plan["phase_id"] != PHASE_ID:
        _fail("selection plan schema or phase is invalid")
    _expect_identifier(plan["solo_id"], "selection_plan.solo_id")
    derivation = _expect_dict(plan["seed_derivation"], "selection_plan.seed_derivation")
    _exact_keys(derivation, SEED_DERIVATION_KEYS, "selection_plan.seed_derivation")
    if derivation["method"] != "sha256_source_commit_v1":
        _fail("selection seed derivation method is invalid")
    source_commit = derivation["source_commit"]
    expected_seed = derive_selection_seed(source_commit)
    if plan["seed"] != expected_seed:
        _fail("selection seed is not derived from the declared source commit")
    synthetic = _expect_bool(plan["synthetic"], "selection_plan.synthetic")
    if not synthetic:
        if expected_source_commit is None:
            _fail("real selection validation requires the externally expected merge commit")
        if source_commit != expected_source_commit:
            _fail("selection source commit differs from the externally expected merge commit")
    elif expected_source_commit is not None and source_commit != expected_source_commit:
        _fail("selection source commit differs from the externally expected merge commit")
    window = _expect_dict(plan["selection_window"], "selection_plan.selection_window")
    _exact_keys(window, SELECTION_WINDOW_KEYS, "selection_plan.selection_window")
    start = parse_timestamp(window["start"], "selection_plan.selection_window.start")
    end = parse_timestamp(window["end"], "selection_plan.selection_window.end")
    generated = parse_timestamp(plan["generated_at"], "selection_plan.generated_at")
    if not (start < end <= generated):
        _fail("selection window must close before plan generation")
    _expect_unique_identifiers(plan["repository_ids"], "selection_plan.repository_ids")
    target = _expect_int(plan["target_prs"], "selection_plan.target_prs", minimum=5)
    if target > 10:
        _fail("selection target must be between 5 and 10")
    reasons = _expect_unique_identifiers(
        plan["exclusion_reasons"], "selection_plan.exclusion_reasons"
    )
    if "authorization_missing" not in reasons:
        _fail("selection exclusions must preregister authorization_missing")
    validate_artifact_hash(plan, "plan_sha256", "selection_plan")
    return plan


def validate_selection_log(
    raw: Any,
    plan: Mapping[str, Any],
    repositories: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows_raw = _expect_list(raw, "selection_log")
    if not rows_raw:
        _fail("selection log cannot be empty")
    rows: list[dict[str, Any]] = []
    pr_ids: set[str] = set()
    start = parse_timestamp(plan["selection_window"]["start"], "selection window start")
    end = parse_timestamp(plan["selection_window"]["end"], "selection window end")
    allowed_repositories = set(plan["repository_ids"])
    allowed_exclusions = set(plan["exclusion_reasons"])
    for index, raw_row in enumerate(rows_raw):
        row = _expect_dict(raw_row, f"selection_log[{index}]")
        _exact_keys(row, SELECTION_ROW_KEYS, f"selection_log[{index}]")
        if row["schema_version"] != 1 or row["solo_id"] != plan["solo_id"]:
            _fail("selection row schema or Solo ID is invalid")
        if row["repository_id"] not in allowed_repositories:
            _fail("selection row references a foreign repository")
        pr_id = _expect_identifier(row["pr_id"], "selection row PR ID")
        if pr_id in pr_ids:
            _fail("selection log repeats a PR ID")
        pr_ids.add(pr_id)
        merged_at = parse_timestamp(row["merged_at"], "selection row merged_at")
        if not (start <= merged_at < end):
            _fail("selection row falls outside the frozen window")
        eligible = _expect_bool(row["eligible"], "selection row eligible")
        selected = _expect_bool(row["selected"], "selection row selected")
        exclusion = _expect_nullable_str(row["exclusion_reason"], "selection row exclusion")
        if eligible and exclusion is not None:
            _fail("eligible selection row cannot have an exclusion")
        if not eligible and exclusion not in allowed_exclusions:
            _fail("ineligible selection row lacks a preregistered exclusion")
        if selected and not eligible:
            _fail("ineligible selection row cannot be selected")
        if row["rank_sha256"] != selection_rank(plan["seed"], pr_id):
            _fail("selection rank mismatch")
        if selected:
            _expect_sha(row["snapshot_sha256"], "selected row snapshot_sha256")
            _expect_sha(row["diff_sha256"], "selected row diff_sha256")
        else:
            if row["snapshot_sha256"] is not None:
                _expect_sha(row["snapshot_sha256"], "unselected row snapshot_sha256")
            if row["diff_sha256"] is not None:
                _expect_sha(row["diff_sha256"], "unselected row diff_sha256")
        if _expect_bool(row["synthetic"], "selection row synthetic") != plan["synthetic"]:
            _fail("selection row synthetic provenance mismatch")
        validate_artifact_hash(row, "row_sha256", "selection row")
        rows.append(row)
    eligible_ranked = sorted(
        (row for row in rows if row["eligible"]), key=lambda row: row["rank_sha256"]
    )
    target = plan["target_prs"]
    if len(eligible_ranked) < target:
        _fail("selection log has fewer eligible PRs than the frozen target")
    expected_selected = {row["pr_id"] for row in eligible_ranked[:target]}
    actual_selected = {row["pr_id"] for row in rows if row["selected"]}
    if actual_selected != expected_selected:
        _fail("selected PRs are not the lowest frozen eligible ranks")
    repository_ids = {row["repository_id"] for row in repositories["repositories"]}
    if allowed_repositories != repository_ids:
        _fail("selection plan repository set differs from manifest")
    return rows


def materialize_cohort(
    plan: Mapping[str, Any],
    selection_rows: Sequence[Mapping[str, Any]],
    repositories: Mapping[str, Any],
    *,
    materialized_at: str,
) -> dict[str, Any]:
    validate_selection_log(list(selection_rows), plan, repositories)
    materialized = parse_timestamp(materialized_at, "cohort.materialized_at")
    if materialized < parse_timestamp(plan["generated_at"], "selection plan generated_at"):
        _fail("cohort cannot be materialized before the selection plan")
    entries = [
        {
            "repository_id": row["repository_id"],
            "pr_id": row["pr_id"],
            "snapshot_sha256": row["snapshot_sha256"],
            "diff_sha256": row["diff_sha256"],
            "selected_at": materialized_at,
        }
        for row in sorted(selection_rows, key=lambda item: str(item["rank_sha256"]))
        if row["selected"]
    ]
    return with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "solo_id": plan["solo_id"],
            "materialized_at": materialized_at,
            "selection_plan_sha256": plan["plan_sha256"],
            "selection_log_sha256": sha256_value(list(selection_rows)),
            "entries": entries,
            "synthetic": plan["synthetic"],
            "cohort_sha256": "",
        },
        "cohort_sha256",
    )


def validate_cohort(
    raw: Any,
    plan: Mapping[str, Any],
    selection_rows: Sequence[Mapping[str, Any]],
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
    entries = _expect_list(cohort["entries"], "cohort.entries")
    for index, entry_raw in enumerate(entries):
        entry = _expect_dict(entry_raw, f"cohort.entries[{index}]")
        _exact_keys(entry, COHORT_ENTRY_KEYS, f"cohort.entries[{index}]")
    return cohort


def validate_finding_subjects(
    raw: Any,
    cohort: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows_raw = _expect_list(raw, "finding_subjects")
    selected_prs = {entry["pr_id"] for entry in cohort["entries"]}
    rows: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for index, raw_row in enumerate(rows_raw):
        row = _expect_dict(raw_row, f"finding_subjects[{index}]")
        _exact_keys(row, FINDING_KEYS, f"finding_subjects[{index}]")
        if row["schema_version"] != 1 or row["solo_id"] != cohort["solo_id"]:
            _fail("Finding subject schema or Solo ID is invalid")
        if row["pr_id"] not in selected_prs:
            _fail("Finding subject references a PR outside the cohort")
        _expect_identifier(row["review_id"], "Finding review ID")
        finding_id = _expect_identifier(row["finding_id"], "Finding ID")
        identity = (row["pr_id"], finding_id)
        if identity in identities:
            _fail("Finding subjects repeat a PR/Finding identity")
        identities.add(identity)
        _expect_sha(row["finding_sha256"], "Finding content hash")
        _expect_sha(row["evidence_sha256"], "Finding evidence hash")
        _expect_bool(row["feedback_eligible"], "Finding feedback eligibility")
        if _expect_bool(row["synthetic"], "Finding synthetic") != cohort["synthetic"]:
            _fail("Finding synthetic provenance mismatch")
        validate_artifact_hash(row, "subject_sha256", "Finding subject")
        rows.append(row)
    return rows


def validate_feedback_responses(
    raw: Any,
    participants: Mapping[str, Any],
    cohort: Mapping[str, Any],
    finding_subjects: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows_raw = _expect_list(raw, "feedback_responses")
    participant = participants["participants"][0]
    consent_start = parse_timestamp(participant["consented_at"], "participant consent start")
    consent_end = parse_timestamp(participant["consent_expires_at"], "participant consent expiry")
    eligible = {
        (subject["pr_id"], subject["finding_id"])
        for subject in finding_subjects
        if subject["feedback_eligible"]
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw_row in enumerate(rows_raw):
        row = _expect_dict(raw_row, f"feedback_responses[{index}]")
        _exact_keys(row, FEEDBACK_KEYS, f"feedback_responses[{index}]")
        if row["schema_version"] != 1 or row["solo_id"] != cohort["solo_id"]:
            _fail("feedback response schema or Solo ID is invalid")
        if row["participant_id"] != participant["participant_id"]:
            _fail("feedback response was not imported from the Solo participant")
        identity = (row["pr_id"], row["finding_id"])
        if identity not in eligible:
            _fail("feedback response references a non-eligible Finding")
        if identity in seen:
            _fail("feedback responses repeat a Finding")
        seen.add(identity)
        decision = row["decision"]
        if decision not in {"accepted", "rejected", "uncertain", "fixed", "duplicate"}:
            _fail("feedback decision is invalid")
        rationale = _expect_nullable_str(row["rationale"], "feedback rationale", maximum=2000)
        if decision in {"rejected", "uncertain", "duplicate"} and rationale is None:
            _fail("rejected, uncertain, and duplicate feedback require a human rationale")
        created_at = parse_timestamp(row["created_at"], "feedback created_at")
        if not (consent_start <= created_at < consent_end):
            _fail("feedback response falls outside the participant consent window")
        if decision == "fixed":
            fixed_at = parse_timestamp(row["fixed_at"], "feedback fixed_at")
            if fixed_at < created_at:
                _fail("feedback fixed_at cannot precede created_at")
            if fixed_at >= consent_end:
                _fail("fixed feedback falls outside the participant consent window")
        elif row["fixed_at"] is not None:
            _fail("only fixed feedback may declare fixed_at")
        if not _expect_bool(row["completed_by_human"], "feedback completed_by_human"):
            _fail("Solo feedback must be completed by the real participant")
        if row["synthetic"] != cohort["synthetic"]:
            _fail("feedback synthetic provenance mismatch")
        validate_artifact_hash(row, "response_sha256", "feedback response")
        rows.append(row)
    return rows


def validate_review_times(
    raw: Any,
    participants: Mapping[str, Any],
    cohort: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows_raw = _expect_list(raw, "review_times")
    selected_prs = {entry["pr_id"] for entry in cohort["entries"]}
    participant_id = participants["participants"][0]["participant_id"]
    participant = participants["participants"][0]
    consent_start = parse_timestamp(participant["consented_at"], "participant consent start")
    consent_end = parse_timestamp(participant["consent_expires_at"], "participant consent expiry")
    rows: list[dict[str, Any]] = []
    seen_prs: set[str] = set()
    seen_sessions: set[str] = set()
    for index, raw_row in enumerate(rows_raw):
        row = _expect_dict(raw_row, f"review_times[{index}]")
        _exact_keys(row, REVIEW_TIME_KEYS, f"review_times[{index}]")
        if row["schema_version"] != 1 or row["solo_id"] != cohort["solo_id"]:
            _fail("review time schema or Solo ID is invalid")
        session_id = _expect_identifier(row["session_id"], "review-time session ID")
        if session_id in seen_sessions:
            _fail("review times repeat a session ID")
        seen_sessions.add(session_id)
        if row["participant_id"] != participant_id:
            _fail("review time belongs to a foreign participant")
        pr_id = row["pr_id"]
        if pr_id not in selected_prs or pr_id in seen_prs:
            _fail("review times must contain one record per selected PR")
        seen_prs.add(pr_id)
        started = parse_timestamp(row["started_at"], "review time started_at")
        completed = parse_timestamp(row["completed_at"], "review time completed_at")
        if completed < started:
            _fail("review time completion cannot precede start")
        if started < parse_timestamp(cohort["materialized_at"], "cohort materialized_at"):
            _fail("review time cannot start before cohort materialization")
        if not (consent_start <= started <= completed < consent_end):
            _fail("review time falls outside the participant consent window")
        active = _expect_int(row["active_seconds"], "review time active_seconds")
        paused = _expect_int(row["paused_seconds"], "review time paused_seconds")
        if active + paused > int((completed - started).total_seconds()):
            _fail("review active plus paused seconds exceed the recorded wall time")
        if not _expect_bool(row["completed_by_human"], "review time completed_by_human"):
            _fail("review time must be recorded by the real participant")
        if row["synthetic"] != cohort["synthetic"]:
            _fail("review time synthetic provenance mismatch")
        validate_artifact_hash(row, "record_sha256", "review time")
        rows.append(row)
    if seen_prs != selected_prs:
        _fail("review times do not cover the complete selected-PR denominator")
    return rows


def validate_run_receipts(
    raw: Any,
    cohort: Mapping[str, Any],
    authorization: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows_raw = _expect_list(raw, "run_receipts")
    selected_prs = {entry["pr_id"] for entry in cohort["entries"]}
    rows: list[dict[str, Any]] = []
    seen_runs: set[str] = set()
    attempts_by_pr: dict[str, list[dict[str, Any]]] = {pr_id: [] for pr_id in selected_prs}
    model = authorization["model"]
    statuses = {"completed", "degraded", "fail_open", "failed", "cancelled", "timed_out"}
    for index, raw_row in enumerate(rows_raw):
        row = _expect_dict(raw_row, f"run_receipts[{index}]")
        _exact_keys(row, RECEIPT_KEYS, f"run_receipts[{index}]")
        if row["schema_version"] != 1 or row["solo_id"] != cohort["solo_id"]:
            _fail("run receipt schema or Solo ID is invalid")
        run_id = _expect_identifier(row["run_id"], "run receipt run ID")
        if run_id in seen_runs:
            _fail("run receipts repeat a run ID")
        seen_runs.add(run_id)
        pr_id = row["pr_id"]
        if pr_id not in selected_prs:
            _fail("run receipt references a PR outside the cohort")
        attempt = _expect_int(row["attempt_number"], "attempt number", minimum=1)
        headline = _expect_bool(row["headline"], "receipt headline")
        if headline is not (attempt == 1):
            _fail("attempt 1 is the sole immutable headline")
        for key in ("provider", "exact_model_snapshot", "runtime_config_sha256", "temperature"):
            if row[key] != model[key]:
                _fail(f"run receipt {key} differs from authorization")
        started = parse_timestamp(row["started_at"], "run receipt started_at")
        completed = parse_timestamp(row["completed_at"], "run receipt completed_at")
        if completed < started:
            _fail("run completion cannot precede start")
        if started < parse_timestamp(cohort["materialized_at"], "cohort materialized_at"):
            _fail("run receipt cannot start before cohort materialization")
        approved_at = parse_timestamp(authorization["approved_at"], "authorization approved_at")
        expires_at = parse_timestamp(authorization["expires_at"], "authorization expires_at")
        if not (approved_at <= started < expires_at):
            _fail("run receipt started outside the authorization window")
        if row["status"] not in statuses:
            _fail("run receipt status is invalid")
        calls = _expect_int(row["logical_calls"], "receipt logical_calls")
        http = _expect_int(row["http_attempts"], "receipt http_attempts")
        if http < calls:
            _fail("receipt HTTP attempts cannot be lower than logical calls")
        _expect_int(row["input_tokens"], "receipt input_tokens")
        _expect_int(row["output_tokens"], "receipt output_tokens")
        _expect_int(row["cost_microcny"], "receipt cost_microcny")
        latency = _expect_number(row["latency_seconds"], "receipt latency_seconds")
        if latency > (completed - started).total_seconds() + 1:
            _fail("receipt latency exceeds its recorded wall interval")
        error = _expect_nullable_str(row["error_category"], "receipt error_category", maximum=200)
        if row["status"] == "completed" and error is not None:
            _fail("completed receipt cannot declare an error category")
        if row["status"] in {"failed", "cancelled", "timed_out"} and error is None:
            _fail("failed, cancelled, and timed-out receipts require an error category")
        finding_ids = _expect_list(
            row["feedback_eligible_finding_ids"],
            "receipt feedback_eligible_finding_ids",
        )
        validated_findings = [
            _expect_identifier(value, "receipt Finding ID") for value in finding_ids
        ]
        if len(validated_findings) != len(set(validated_findings)):
            _fail("receipt repeats a feedback-eligible Finding ID")
        if not headline and validated_findings:
            _fail("diagnostic attempts cannot introduce feedback-eligible Findings")
        _expect_sha(row["raw_trace_sha256"], "receipt raw_trace_sha256")
        retain_until = parse_timestamp(
            row["raw_trace_retain_until"], "receipt raw_trace_retain_until"
        )
        retention_seconds = authorization["retention"]["raw_trace_days"] * 86400
        actual_retention = int((retain_until - completed).total_seconds())
        if actual_retention < 0 or actual_retention > retention_seconds:
            _fail("raw-trace retention exceeds the authorized bound")
        if row["synthetic"] != cohort["synthetic"]:
            _fail("run receipt synthetic provenance mismatch")
        validate_artifact_hash(row, "receipt_sha256", "run receipt")
        attempts_by_pr[pr_id].append(row)
        rows.append(row)

    for pr_id, attempts in attempts_by_pr.items():
        ordered = sorted(attempts, key=lambda item: item["attempt_number"])
        if [row["attempt_number"] for row in ordered] != list(range(1, len(ordered) + 1)):
            _fail("run attempts are not contiguous from attempt 1")
        if sum(row["headline"] for row in ordered) != 1:
            _fail("every selected PR requires exactly one headline receipt")

    totals = {
        key: sum(row[key] for row in rows)
        for key in (
            "logical_calls",
            "http_attempts",
            "input_tokens",
            "output_tokens",
            "cost_microcny",
        )
    }
    if not authorization["synthetic"] and any(totals.values()):
        if not model["real_paid_calls"] or not model["read_raw_diff"]:
            _fail("real model usage lacks paid-call or raw-diff authority")
    ceiling_map = {
        "logical_calls": "max_logical_calls",
        "http_attempts": "max_http_attempts",
        "input_tokens": "max_input_tokens",
        "output_tokens": "max_output_tokens",
        "cost_microcny": "max_cost_microcny",
    }
    for usage_key, ceiling_key in ceiling_map.items():
        if totals[usage_key] > model[ceiling_key]:
            _fail(f"cumulative {usage_key} exceeds the authorization")
    return rows


def validate_receipt_finding_bindings(
    receipts: Sequence[Mapping[str, Any]],
    finding_subjects: Sequence[Mapping[str, Any]],
) -> None:
    declared = {
        (row["pr_id"], finding_id)
        for row in receipts
        if row["headline"]
        for finding_id in row["feedback_eligible_finding_ids"]
    }
    subjects = {
        (row["pr_id"], row["finding_id"])
        for row in finding_subjects
        if row["feedback_eligible"]
    }
    if declared != subjects:
        _fail("headline receipt Finding bindings differ from feedback-eligible subjects")


def build_run_manifest(
    receipts: Sequence[Mapping[str, Any]],
    cohort: Mapping[str, Any],
    *,
    generated_at: str,
) -> dict[str, Any]:
    parse_timestamp(generated_at, "run manifest generated_at")
    totals = {
        key: sum(row[key] for row in receipts)
        for key in (
            "logical_calls",
            "http_attempts",
            "input_tokens",
            "output_tokens",
            "cost_microcny",
        )
    }
    return with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "solo_id": cohort["solo_id"],
            "generated_at": generated_at,
            "cohort_sha256": cohort["cohort_sha256"],
            "receipt_set_sha256": sha256_value(list(receipts)),
            "selected_prs": len(cohort["entries"]),
            "attempts": len(receipts),
            "headline_attempts": sum(row["headline"] for row in receipts),
            "cumulative_usage": totals,
            "synthetic": cohort["synthetic"],
            "manifest_sha256": "",
        },
        "manifest_sha256",
    )


def validate_run_manifest(
    raw: Any,
    receipts: Sequence[Mapping[str, Any]],
    cohort: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = _expect_dict(raw, "run_manifest")
    _exact_keys(manifest, RUN_MANIFEST_KEYS, "run_manifest")
    usage = _expect_dict(manifest["cumulative_usage"], "run_manifest.cumulative_usage")
    _exact_keys(usage, USAGE_KEYS, "run_manifest.cumulative_usage")
    expected = build_run_manifest(receipts, cohort, generated_at=manifest["generated_at"])
    if manifest != expected:
        _fail("run manifest does not exactly match the receipts")
    generated_at = parse_timestamp(manifest["generated_at"], "run manifest generated_at")
    if any(
        parse_timestamp(row["completed_at"], "receipt completed_at") > generated_at
        for row in receipts
    ):
        _fail("run manifest cannot precede a receipt it summarizes")
    return manifest


def _safe_div(numerator: int | float, denominator: int | float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _distribution(values: Sequence[int | float]) -> dict[str, int | float | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None, "total": 0}
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
        "total": sum(values),
    }


def build_solo_report(
    *,
    authorization: Mapping[str, Any],
    participants: Mapping[str, Any],
    repositories: Mapping[str, Any],
    cohort: Mapping[str, Any],
    finding_subjects: Sequence[Mapping[str, Any]],
    feedback_responses: Sequence[Mapping[str, Any]],
    review_times: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
    run_manifest: Mapping[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    report_time = parse_timestamp(generated_at, "solo report generated_at")
    if report_time < parse_timestamp(run_manifest["generated_at"], "run manifest generated_at"):
        _fail("Solo report cannot precede its run manifest")
    if report_time < parse_timestamp(cohort["materialized_at"], "cohort materialized_at"):
        _fail("Solo report cannot precede cohort materialization")
    if any(
        parse_timestamp(row["completed_at"], "review time completed_at") > report_time
        for row in review_times
    ):
        _fail("Solo report cannot precede a review-time record")
    if any(
        parse_timestamp(row["created_at"], "feedback created_at") > report_time
        for row in feedback_responses
    ):
        _fail("Solo report cannot precede a feedback response")
    if any(
        row["fixed_at"] is not None
        and parse_timestamp(row["fixed_at"], "feedback fixed_at") > report_time
        for row in feedback_responses
    ):
        _fail("Solo report cannot precede a fixed feedback timestamp")
    headline = [row for row in receipts if row["headline"]]
    status_counts = Counter(row["status"] for row in headline)
    decision_counts = Counter(row["decision"] for row in feedback_responses)
    error_counts = Counter(
        row["error_category"] for row in receipts if row["error_category"] is not None
    )
    all_status_counts = Counter(row["status"] for row in receipts)
    eligible_findings = sum(row["feedback_eligible"] for row in finding_subjects)
    feedback_count = len(feedback_responses)
    headline_failures = {
        row["pr_id"] for row in headline if row["status"] != "completed"
    }
    rerun_after_failure = sum(
        row["attempt_number"] > 1 and row["pr_id"] in headline_failures for row in receipts
    )
    readiness = authorization_readiness(authorization, at=report_time)
    participant = participants["participants"][0]
    repository_unexpired = all(
        parse_timestamp(row["authorization_expires_at"], "repository expiry") > report_time
        for row in repositories["repositories"]
    )
    consent_unexpired = (
        parse_timestamp(participant["consent_expires_at"], "consent expiry") > report_time
    )
    complete_coverage = bool(
        len(headline) == len(cohort["entries"])
        and len(review_times) == len(cohort["entries"])
    )
    exploratory_allowed = bool(
        readiness["scopes"]["real_exploratory_run"]
        and readiness["scopes"]["model_execution"]
        and participant["confirmed_real"]
        and consent_unexpired
        and repository_unexpired
        and complete_coverage
        and 5 <= len(cohort["entries"]) <= 10
        and not cohort["synthetic"]
    )
    usage = run_manifest["cumulative_usage"]
    metrics = {
        "selected_prs": len(cohort["entries"]),
        "headline_attempts": len(headline),
        "headline_status_counts": dict(sorted(status_counts.items())),
        "headline_completed": status_counts["completed"],
        "headline_completion_rate": _safe_div(status_counts["completed"], len(headline)),
        "feedback_eligible_findings": eligible_findings,
        "feedback_responses": feedback_count,
        "feedback_missing": eligible_findings - feedback_count,
        "feedback_coverage_rate": _safe_div(feedback_count, eligible_findings),
        "feedback_decision_counts": dict(sorted(decision_counts.items())),
        "accepted_or_fixed_observations": (
            decision_counts["accepted"] + decision_counts["fixed"]
        ),
        "accepted_or_fixed_rate_among_all_eligible": _safe_div(
            decision_counts["accepted"] + decision_counts["fixed"], eligible_findings
        ),
        "active_review_seconds": _distribution(
            [row["active_seconds"] for row in review_times]
        ),
        "paused_review_seconds": _distribution(
            [row["paused_seconds"] for row in review_times]
        ),
        "headline_latency_seconds": _distribution(
            [row["latency_seconds"] for row in headline]
        ),
        "all_attempt_usage": dict(usage),
        "all_attempt_status_counts": dict(sorted(all_status_counts.items())),
        "http_retries": usage["http_attempts"] - usage["logical_calls"],
        "diagnostic_attempts": len(receipts) - len(headline),
        "diagnostic_attempts_after_headline_failure": rerun_after_failure,
        "error_category_counts": dict(sorted(error_counts.items())),
    }
    return with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "report_version": "phase9g-solo-report-v1",
            "solo_id": cohort["solo_id"],
            "evidence_type": EVIDENCE_TYPE,
            "generated_at": generated_at,
            "authorization_sha256": authorization["authorization_sha256"],
            "cohort_sha256": cohort["cohort_sha256"],
            "run_manifest_sha256": run_manifest["manifest_sha256"],
            "wording": {
                "observation": "single-participant exploratory observation",
                "quality_statement": "model quality not measured",
            },
            "metrics": metrics,
            "claim_gates": {
                "exploratory_summary_allowed": exploratory_allowed,
                "business_claim_allowed": False,
                "quality_claim_allowed": False,
                "formal_quality_status": "incomplete",
            },
            "synthetic": cohort["synthetic"],
            "report_sha256": "",
        },
        "report_sha256",
    )


def validate_solo_report(
    raw: Any,
    *,
    authorization: Mapping[str, Any],
    participants: Mapping[str, Any],
    repositories: Mapping[str, Any],
    cohort: Mapping[str, Any],
    finding_subjects: Sequence[Mapping[str, Any]],
    feedback_responses: Sequence[Mapping[str, Any]],
    review_times: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
    run_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    report = _expect_dict(raw, "solo_report")
    _exact_keys(report, REPORT_KEYS, "solo_report")
    wording = _expect_dict(report["wording"], "solo_report.wording")
    _exact_keys(wording, WORDING_KEYS, "solo_report.wording")
    gates = _expect_dict(report["claim_gates"], "solo_report.claim_gates")
    _exact_keys(gates, CLAIM_GATE_KEYS, "solo_report.claim_gates")
    expected = build_solo_report(
        authorization=authorization,
        participants=participants,
        repositories=repositories,
        cohort=cohort,
        finding_subjects=finding_subjects,
        feedback_responses=feedback_responses,
        review_times=review_times,
        receipts=receipts,
        run_manifest=run_manifest,
        generated_at=report["generated_at"],
    )
    if report != expected:
        _fail("Solo report does not exactly match recomputed evidence")
    if gates["business_claim_allowed"] or gates["quality_claim_allowed"]:
        _fail("Solo can never allow business or quality claims")
    prohibited = {
        "precision",
        "recall",
        "f1",
        "bootstrap_95_ci",
        "business_pilot_success",
        "time_saved",
    }
    if prohibited & set(report["metrics"]):
        _fail("Solo report contains a prohibited metric")
    return report


def validate_bundle(
    raw: Any,
    *,
    expected_source_commit: str | None = None,
) -> dict[str, Any]:
    bundle = _expect_dict(raw, "bundle")
    _exact_keys(bundle, BUNDLE_KEYS, "bundle")
    if bundle["schema_version"] != 1 or bundle["phase_id"] != PHASE_ID:
        _fail("bundle schema or phase is invalid")
    authorization = validate_authorization(bundle["authorization"])
    participants = validate_participant_manifest(bundle["participants"], authorization)
    repositories = validate_repository_manifest(bundle["repositories"], authorization)
    plan = validate_selection_plan(
        bundle["selection_plan"], expected_source_commit=expected_source_commit
    )
    if plan["solo_id"] != participants["solo_id"] or plan["solo_id"] != repositories["solo_id"]:
        _fail("bundle Solo IDs differ")
    if set(plan["repository_ids"]) != set(authorization["repository_ids"]):
        _fail("selection plan repositories differ from authorization")
    if plan["target_prs"] != authorization["pr_count"]:
        _fail("selection target differs from authorization")
    if plan["synthetic"] != authorization["synthetic"]:
        _fail("selection-plan synthetic provenance differs from authorization")
    plan_generated = parse_timestamp(plan["generated_at"], "selection plan generated_at")
    if plan_generated < parse_timestamp(
        participants["generated_at"], "participant manifest generated_at"
    ) or plan_generated < parse_timestamp(
        repositories["generated_at"], "repository manifest generated_at"
    ):
        _fail("selection plan cannot predate participant or repository authority")
    selection_rows = validate_selection_log(
        bundle["selection_log"], plan, repositories
    )
    cohort = validate_cohort(bundle["cohort"], plan, selection_rows, repositories)
    finding_subjects = validate_finding_subjects(bundle["finding_subjects"], cohort)
    feedback_responses = validate_feedback_responses(
        bundle["feedback_responses"], participants, cohort, finding_subjects
    )
    review_times = validate_review_times(bundle["review_times"], participants, cohort)
    receipts = validate_run_receipts(bundle["run_receipts"], cohort, authorization)
    participant = participants["participants"][0]
    consent_start = parse_timestamp(participant["consented_at"], "participant consent start")
    consent_end = parse_timestamp(participant["consent_expires_at"], "participant consent expiry")
    repository_by_id = {
        row["repository_id"]: row for row in repositories["repositories"]
    }
    repository_by_pr = {
        entry["pr_id"]: repository_by_id[entry["repository_id"]]
        for entry in cohort["entries"]
    }
    for receipt in receipts:
        started = parse_timestamp(receipt["started_at"], "receipt started_at")
        repository = repository_by_pr[receipt["pr_id"]]
        repository_start = parse_timestamp(repository["authorized_at"], "repository authorized_at")
        repository_end = parse_timestamp(
            repository["authorization_expires_at"], "repository authorization expiry"
        )
        if not (consent_start <= started < consent_end):
            _fail("run receipt falls outside the participant consent window")
        if not (repository_start <= started < repository_end):
            _fail("run receipt falls outside the repository authorization window")
    validate_receipt_finding_bindings(receipts, finding_subjects)
    run_manifest = validate_run_manifest(bundle["run_manifest"], receipts, cohort)
    report = validate_solo_report(
        bundle["solo_report"],
        authorization=authorization,
        participants=participants,
        repositories=repositories,
        cohort=cohort,
        finding_subjects=finding_subjects,
        feedback_responses=feedback_responses,
        review_times=review_times,
        receipts=receipts,
        run_manifest=run_manifest,
    )
    validate_artifact_hash(bundle, "bundle_sha256", "bundle")
    return {
        "valid": True,
        "phase_id": PHASE_ID,
        "solo_id": cohort["solo_id"],
        "evidence_type": EVIDENCE_TYPE,
        "synthetic": authorization["synthetic"],
        "selected_prs": len(cohort["entries"]),
        "exploratory_summary_allowed": report["claim_gates"][
            "exploratory_summary_allowed"
        ],
        "business_claim_allowed": False,
        "quality_claim_allowed": False,
        "formal_quality_status": "incomplete",
        "authorization_scopes": authorization_readiness(
            authorization, at=report["generated_at"]
        )["scopes"],
    }


def _sealed(value: Mapping[str, Any], hash_field: str) -> dict[str, Any]:
    return with_artifact_hash({**value, hash_field: ""}, hash_field)


def build_synthetic_bundle() -> dict[str, Any]:
    """Build a deterministic full Solo protocol with every real gate closed."""

    source_commit = "1" * 40
    solo_id = "synthetic-solo-v1"
    participant_id = "synthetic-participant"
    repository_id = "synthetic-repository"
    generated_at = "2026-01-02T00:00:00Z"
    authorization = _sealed(
        {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "authorization_id": "synthetic-solo-authorization-v1",
            "participant_id": participant_id,
            "participant_confirmed_real": False,
            "repository_ids": [repository_id],
            "pr_count": 5,
            "selection_rule": SELECTION_RULE,
            "mode": "shadow",
            "model": {
                "provider": "synthetic-provider",
                "exact_model_snapshot": "synthetic-model-snapshot",
                "runtime_config_sha256": hashlib.sha256(b"synthetic-runtime").hexdigest(),
                "temperature": 0,
                "max_logical_calls": 20,
                "max_http_attempts": 25,
                "max_input_tokens": 10000,
                "max_output_tokens": 5000,
                "max_cost_microcny": 100000,
                "real_paid_calls": False,
                "read_raw_diff": False,
            },
            "retention": {"data_days": 30, "feedback_days": 30, "raw_trace_days": 7},
            "external_operations": {
                "staging_deploy": False,
                "deployment_target": None,
                "real_github_api": False,
                "create_comments_or_checks": False,
                "github_publish": False,
            },
            "approved_by": "synthetic-approver",
            "approved_at": "2026-01-01T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "synthetic": True,
        },
        "authorization_sha256",
    )
    participants = _sealed(
        {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "solo_id": solo_id,
            "identity_custodian_id": "synthetic-custodian",
            "consent_version": "synthetic-consent-v1",
            "generated_at": generated_at,
            "participants": [
                {
                    "participant_id": participant_id,
                    "confirmed_real": False,
                    "role": "developer",
                    "consented_at": "2026-01-01T00:00:00Z",
                    "consent_expires_at": "2099-01-01T00:00:00Z",
                    "consent_scope": ["exploratory_feedback", "review_time"],
                    "repository_ids": [repository_id],
                    "feedback_retention_days": 30,
                    "withdrawal_acknowledged": True,
                }
            ],
            "synthetic": True,
        },
        "manifest_sha256",
    )
    repository = _sealed(
        {
            "repository_id": repository_id,
            "locator_sha256": hashlib.sha256(b"synthetic-repository-locator").hexdigest(),
            "authorized_by": "synthetic-repository-owner",
            "authorized_at": "2026-01-01T00:00:00Z",
            "authorization_expires_at": "2099-01-01T00:00:00Z",
            "raw_diff_read_authorized": False,
            "real_github_api_authorized": False,
            "publish_mode": "shadow",
            "publication_authorized": False,
            "data_retention_days": 30,
        },
        "repository_sha256",
    )
    repositories = _sealed(
        {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "solo_id": solo_id,
            "generated_at": generated_at,
            "repositories": [repository],
            "synthetic": True,
        },
        "manifest_sha256",
    )
    seed = derive_selection_seed(source_commit)
    plan = _sealed(
        {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "solo_id": solo_id,
            "seed": seed,
            "seed_derivation": {
                "method": "sha256_source_commit_v1",
                "source_commit": source_commit,
            },
            "selection_window": {
                "start": "2025-12-01T00:00:00Z",
                "end": "2026-01-01T00:00:00Z",
            },
            "repository_ids": [repository_id],
            "target_prs": 5,
            "exclusion_reasons": [
                "outside_scope",
                "not_reproducible",
                "authorization_missing",
            ],
            "generated_at": generated_at,
            "synthetic": True,
        },
        "plan_sha256",
    )
    selection_rows: list[dict[str, Any]] = []
    for index in range(1, 7):
        pr_id = f"synthetic-pr-{index}"
        row = {
            "schema_version": 1,
            "solo_id": solo_id,
            "repository_id": repository_id,
            "pr_id": pr_id,
            "merged_at": f"2025-12-{index:02d}T00:00:00Z",
            "eligible": index <= 5,
            "exclusion_reason": None if index <= 5 else "outside_scope",
            "selected": index <= 5,
            "rank_sha256": selection_rank(seed, pr_id),
            "snapshot_sha256": (
                hashlib.sha256(f"snapshot-{index}".encode()).hexdigest()
                if index <= 5
                else None
            ),
            "diff_sha256": (
                hashlib.sha256(f"diff-{index}".encode()).hexdigest() if index <= 5 else None
            ),
            "synthetic": True,
        }
        selection_rows.append(_sealed(row, "row_sha256"))
    # The fixture explicitly chooses the five eligible rows; rank order only
    # determines their stable order because the sixth row is preregistered out.
    cohort = materialize_cohort(
        plan,
        selection_rows,
        repositories,
        materialized_at="2026-01-02T01:00:00Z",
    )
    finding_subjects: list[dict[str, Any]] = []
    review_times: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for index, entry in enumerate(cohort["entries"], start=1):
        finding_id = f"synthetic-finding-{index}"
        finding_subjects.append(
            _sealed(
                {
                    "schema_version": 1,
                    "solo_id": solo_id,
                    "pr_id": entry["pr_id"],
                    "review_id": f"synthetic-review-{index}",
                    "finding_id": finding_id,
                    "finding_sha256": hashlib.sha256(f"finding-{index}".encode()).hexdigest(),
                    "evidence_sha256": hashlib.sha256(f"evidence-{index}".encode()).hexdigest(),
                    "feedback_eligible": True,
                    "synthetic": True,
                },
                "subject_sha256",
            )
        )
        review_times.append(
            _sealed(
                {
                    "schema_version": 1,
                    "solo_id": solo_id,
                    "session_id": f"synthetic-session-{index}",
                    "participant_id": participant_id,
                    "pr_id": entry["pr_id"],
                    "started_at": f"2026-01-03T0{index}:00:00Z",
                    "completed_at": f"2026-01-03T0{index}:10:00Z",
                    "active_seconds": 480,
                    "paused_seconds": 60,
                    "completed_by_human": True,
                    "synthetic": True,
                },
                "record_sha256",
            )
        )
        status = "failed" if index == 1 else "completed"
        receipts.append(
            _sealed(
                {
                    "schema_version": 1,
                    "solo_id": solo_id,
                    "run_id": f"synthetic-run-{index}-attempt-1",
                    "pr_id": entry["pr_id"],
                    "attempt_number": 1,
                    "headline": True,
                    "provider": authorization["model"]["provider"],
                    "exact_model_snapshot": authorization["model"]["exact_model_snapshot"],
                    "runtime_config_sha256": authorization["model"]["runtime_config_sha256"],
                    "temperature": 0,
                    "started_at": f"2026-01-03T0{index}:01:00Z",
                    "completed_at": f"2026-01-03T0{index}:02:00Z",
                    "status": status,
                    "logical_calls": 1,
                    "http_attempts": 1,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cost_microcny": 1000,
                    "latency_seconds": 60,
                    "error_category": (
                        "post_model_pipeline_failure" if status == "failed" else None
                    ),
                    "feedback_eligible_finding_ids": [finding_id],
                    "raw_trace_sha256": hashlib.sha256(f"trace-{index}-1".encode()).hexdigest(),
                    "raw_trace_retain_until": "2026-01-10T00:00:00Z",
                    "synthetic": True,
                },
                "receipt_sha256",
            )
        )
    # A successful diagnostic attempt is retained but cannot replace PR 1's failure.
    first_pr = cohort["entries"][0]["pr_id"]
    receipts.append(
        _sealed(
            {
                "schema_version": 1,
                "solo_id": solo_id,
                "run_id": "synthetic-run-1-attempt-2",
                "pr_id": first_pr,
                "attempt_number": 2,
                "headline": False,
                "provider": authorization["model"]["provider"],
                "exact_model_snapshot": authorization["model"]["exact_model_snapshot"],
                "runtime_config_sha256": authorization["model"]["runtime_config_sha256"],
                "temperature": 0,
                "started_at": "2026-01-03T01:03:00Z",
                "completed_at": "2026-01-03T01:04:00Z",
                "status": "completed",
                "logical_calls": 1,
                "http_attempts": 2,
                "input_tokens": 120,
                "output_tokens": 25,
                "cost_microcny": 1200,
                "latency_seconds": 60,
                "error_category": None,
                "feedback_eligible_finding_ids": [],
                "raw_trace_sha256": hashlib.sha256(b"trace-1-2").hexdigest(),
                "raw_trace_retain_until": "2026-01-10T00:00:00Z",
                "synthetic": True,
            },
            "receipt_sha256",
        )
    )
    feedback_responses = [
        _sealed(
            {
                "schema_version": 1,
                "solo_id": solo_id,
                "participant_id": participant_id,
                "pr_id": subject["pr_id"],
                "finding_id": subject["finding_id"],
                "decision": "accepted" if index == 1 else "fixed",
                "rationale": None,
                "created_at": f"2026-01-04T0{index}:00:00Z",
                "fixed_at": f"2026-01-04T0{index}:01:00Z" if index == 2 else None,
                "completed_by_human": True,
                "synthetic": True,
            },
            "response_sha256",
        )
        for index, subject in enumerate(finding_subjects[:2], start=1)
    ]
    run_manifest = build_run_manifest(
        receipts, cohort, generated_at="2026-01-05T00:00:00Z"
    )
    report = build_solo_report(
        authorization=authorization,
        participants=participants,
        repositories=repositories,
        cohort=cohort,
        finding_subjects=finding_subjects,
        feedback_responses=feedback_responses,
        review_times=review_times,
        receipts=receipts,
        run_manifest=run_manifest,
        generated_at="2026-01-05T01:00:00Z",
    )
    return _sealed(
        {
            "schema_version": 1,
            "phase_id": PHASE_ID,
            "authorization": authorization,
            "participants": participants,
            "repositories": repositories,
            "selection_plan": plan,
            "selection_log": selection_rows,
            "cohort": cohort,
            "finding_subjects": finding_subjects,
            "feedback_responses": feedback_responses,
            "review_times": review_times,
            "run_receipts": receipts,
            "run_manifest": run_manifest,
            "solo_report": report,
        },
        "bundle_sha256",
    )


def validate_bundle_fixture(raw: Any) -> dict[str, Any]:
    descriptor = _expect_dict(raw, "bundle_fixture")
    _exact_keys(descriptor, BUNDLE_FIXTURE_KEYS, "bundle_fixture")
    if descriptor["schema_version"] != 1 or descriptor["phase_id"] != PHASE_ID:
        _fail("bundle fixture schema or phase is invalid")
    if descriptor["fixture"] != "built_in_synthetic_v1":
        _fail("bundle fixture kind is unsupported")
    if descriptor["evidence_type"] != EVIDENCE_TYPE:
        _fail("bundle fixture evidence type is invalid")
    _expect_sha(descriptor["expected_bundle_sha256"], "fixture expected bundle hash")
    for key in (
        "exploratory_summary_allowed",
        "business_claim_allowed",
        "quality_claim_allowed",
    ):
        if _expect_bool(descriptor[key], f"bundle_fixture.{key}"):
            _fail(f"synthetic fixture cannot allow {key}")
    validate_artifact_hash(descriptor, "fixture_sha256", "bundle_fixture")
    bundle = build_synthetic_bundle()
    if descriptor["expected_bundle_sha256"] != bundle["bundle_sha256"]:
        _fail("built-in synthetic bundle differs from committed descriptor")
    return validate_bundle(bundle)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Phase 9G-Solo artifacts without external calls"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    authorization = subparsers.add_parser("validate-authorization")
    authorization.add_argument("--authorization", required=True)
    authorization.add_argument("--at")
    seal = subparsers.add_parser("seal-authorization")
    seal.add_argument("--authorization", required=True)
    seal.add_argument("--output", required=True)
    hash_artifact = subparsers.add_parser("hash-artifact")
    hash_artifact.add_argument("--input", required=True)
    hash_artifact.add_argument("--hash-field", required=True)
    hash_artifact.add_argument("--output", required=True)
    hash_artifact.add_argument("--jsonl", action="store_true")
    cohort = subparsers.add_parser("materialize-cohort")
    cohort.add_argument("--authorization", required=True)
    cohort.add_argument("--plan", required=True)
    cohort.add_argument("--selection-log", required=True)
    cohort.add_argument("--repositories", required=True)
    cohort.add_argument("--expected-source-commit", required=True)
    cohort.add_argument("--materialized-at", required=True)
    cohort.add_argument("--output", required=True)
    bundle = subparsers.add_parser("validate-bundle")
    bundle.add_argument("--bundle", required=True)
    bundle.add_argument("--expected-source-commit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "validate-authorization":
            result = authorization_readiness(load_json(args.authorization), at=args.at)
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
                rows = load_jsonl(args.input)
                sealed_rows: list[dict[str, Any]] = []
                for index, raw_row in enumerate(rows):
                    row = _expect_dict(raw_row, f"artifact[{index}]")
                    if args.hash_field not in row:
                        _fail("artifact is missing the requested hash field")
                    sealed_rows.append(
                        with_artifact_hash(
                            {**row, args.hash_field: ""}, args.hash_field
                        )
                    )
                _write_jsonl(args.output, sealed_rows)
                result = {
                    "valid": True,
                    "rows": len(sealed_rows),
                    "artifact_sha256": sha256_value(sealed_rows),
                }
            else:
                row = _expect_dict(load_json(args.input), "artifact")
                if args.hash_field not in row:
                    _fail("artifact is missing the requested hash field")
                result = with_artifact_hash(
                    {**row, args.hash_field: ""}, args.hash_field
                )
                _write_json(args.output, result)
        elif args.command == "materialize-cohort":
            authorization = validate_authorization(load_json(args.authorization))
            plan = validate_selection_plan(
                load_json(args.plan),
                expected_source_commit=args.expected_source_commit,
            )
            repositories = validate_repository_manifest(
                load_json(args.repositories), authorization
            )
            if set(plan["repository_ids"]) != set(authorization["repository_ids"]):
                _fail("selection plan repositories differ from authorization")
            if plan["target_prs"] != authorization["pr_count"]:
                _fail("selection target differs from authorization")
            if plan["synthetic"] != authorization["synthetic"]:
                _fail("selection-plan synthetic provenance differs from authorization")
            selection_rows = load_jsonl(args.selection_log)
            result = materialize_cohort(
                plan,
                selection_rows,
                repositories,
                materialized_at=args.materialized_at,
            )
            _write_json(args.output, result)
        else:
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
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
