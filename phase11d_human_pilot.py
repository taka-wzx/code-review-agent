"""Offline Phase 11D Human Review-to-Repair Pilot Gate A tooling.

This module intentionally uses only the Python standard library.  It creates and
validates hash-bound synthetic artifacts for the Phase 11D Gate A protocol; it
does not open sockets, read credentials, invoke GitHub, or run a real Pilot.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence, cast


AUTHORIZATION_SCHEMA_VERSION = "crag.phase11d.authorization/v1alpha1"
COHORT_SCHEMA_VERSION = "crag.phase11d.cohort/v1alpha1"
REVIEW_RECEIPT_SCHEMA_VERSION = "crag.phase11d.review-receipt/v1alpha1"
REPAIR_RECEIPT_SCHEMA_VERSION = "crag.phase11d.repair-receipt/v1alpha1"
DRAFT_PR_RECEIPT_SCHEMA_VERSION = "crag.phase11d.draft-pr-receipt/v1alpha1"
FEEDBACK_SCHEMA_VERSION = "crag.phase11d.feedback/v1alpha1"
INCIDENT_SCHEMA_VERSION = "crag.phase11d.incident/v1alpha1"
BUSINESS_REPORT_SCHEMA_VERSION = "crag.phase11d.business-report/v1alpha1"
CLAIM_DECISION_SCHEMA_VERSION = "crag.phase11d.claim-decision/v1alpha1"
ACCEPTANCE_SCHEMA_VERSION = "crag.phase11d.acceptance/v1alpha1"
MANIFEST_SCHEMA_VERSION = "crag.phase11d.canonical-manifest/v1alpha1"

GENERATED_AT_UTC = "2026-08-03T00:00:00Z"
BASELINE_SHA = "4af4b2756e8d2de6764d08e17a6e12040e24975e"
LOCAL_MASTER_AT_START = "21344a2b72be8cb83361875b5cc8f2952e99ffbf"

EXPECTED_PHASE11C = {
    "diagnostic_status": "completed",
    "diagnostic_receipt_sha256": (
        "97a887015e95e02e94460979dd170b36d01558ce71b882df272b1d2e8aa0a41c"
    ),
    "headline_cohort_status": "inconclusive",
    "headline_terminal_category": "text_only_response",
    "headline_cohort_receipt_sha256": (
        "107f664a6fb1f11caeb85682b648472e351f61b34b4db987ff7b32f3d0e1f146"
    ),
    "headline_ledger_sha256": (
        "680f3cc1938856cfcc00b1f9a9c1aa3dc233c97d6bd794f8409d039817760419"
    ),
    "final_evidence_archive_sha256": (
        "e269f4394a25a812b4a2ac08e3c7b1dbc396e9356b5f522286372bae65abb9f2"
    ),
}

AUTH004_BOUNDARY = {
    "authorization_id": "auth-004",
    "selected_pr_count": 5,
    "headline_attempts": 5,
    "completed": 0,
    "failed": 5,
    "stable_category": "provider_or_pipeline_RuntimeError",
    "no_rerun": True,
    "no_replacement": True,
    "no_backfill": True,
}

CLAIM_BOUNDARY = {
    "real_model_calls": False,
    "real_github_writes": False,
    "real_pilot_executed": False,
    "business_claim_allowed": False,
    "model_quality_status": "not_measured",
    "formal_quality_status": "incomplete",
    "production_ready": False,
}

AUTHORIZATION_FIELDS = frozenset(
    {
        "schema_version",
        "gate",
        "authorization_id",
        "canonical_authorization_sha256",
        "created_at_utc",
        "baseline",
        "phase11c_facts",
        "auth004_boundary",
        "permissions",
        "participants_declared_count",
        "confirmed_real_participant_count",
        "selected_pr_count",
        "synthetic_rows_present",
        "real_rows_present",
        "repository_allowlist_sha256",
        "cohort_sha256",
        "selection_sha256",
        "headline_manifest_sha256",
        "limits",
        "provider_policy",
        "github_policy",
        "retention_policy",
        "incident_policy",
        "claim_boundary",
        "gate_b_required_fields_complete",
    }
)

PERMISSION_FIELDS = frozenset(
    {
        "allow_real_provider_calls",
        "allow_real_github_repair_branch_push",
        "allow_real_draft_repair_pr",
        "allow_comments_checks_labels_reviews",
        "allow_pilot_pr_ready",
        "allow_pilot_pr_merge",
        "allow_default_branch_mutation",
        "allow_auto_merge",
        "allow_agent_push_merge_master",
    }
)

COHORT_FIELDS = frozenset(
    {
        "schema_version",
        "cohort_id",
        "authorization_id",
        "synthetic_rows_present",
        "real_rows_present",
        "selection_seed_sha256",
        "selection_window_start_utc",
        "selection_window_end_utc",
        "eligible_count",
        "excluded_count",
        "selected_pr_count",
        "selected_prs",
        "excluded",
    }
)

SELECTED_PR_FIELDS = frozenset(
    {
        "pr_id",
        "row_kind",
        "repository_id",
        "snapshot_sha256",
        "diff_sha256",
        "headline_id",
        "selection_rank_sha256",
    }
)

EXCLUDED_PR_FIELDS = frozenset({"candidate_id", "row_kind", "exclusion_reason"})

HEADLINE_FIELDS = frozenset(
    {
        "schema_version",
        "manifest_id",
        "cohort_id",
        "authorization_id",
        "immutable_headline_count",
        "headlines",
        "diagnostic_may_replace_headline",
    }
)

HEADLINE_ROW_FIELDS = frozenset(
    {"pr_id", "headline_id", "attempt_number", "review_receipt_id"}
)

REVIEW_FIELDS = frozenset(
    {
        "schema_version",
        "receipt_id",
        "pr_id",
        "headline_id",
        "row_kind",
        "attempt_number",
        "status",
        "terminal_category",
        "finding_ids",
        "feedback_eligible_finding_ids",
        "provider_call_count",
        "http_attempt_count",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cost_micro_cny",
        "latency_ms",
        "trace_sha256",
        "redaction_applied",
    }
)

REPAIR_FIELDS = frozenset(
    {
        "schema_version",
        "repair_job_id",
        "pr_id",
        "finding_id",
        "row_kind",
        "request_actor_role",
        "request_actor_method",
        "base_sha",
        "head_sha",
        "worktree_receipt_sha256",
        "task_branch_sha256",
        "plan_sha256",
        "write_approval",
        "patch_sha256",
        "checkpoint_sha256",
        "test_sha256",
        "budget_sha256",
        "sandbox",
        "draft_pr_approval",
        "commit_sha",
        "publisher_status",
        "final_status",
        "failure_category",
        "cost_micro_cny",
    }
)

APPROVAL_FIELDS = frozenset(
    {
        "approval_id",
        "decision",
        "actor_role",
        "actor_method",
        "binding_sha256",
        "consumed",
    }
)

SANDBOX_FIELDS = frozenset(
    {
        "docker",
        "network_mode",
        "non_root",
        "timeout_seconds",
        "output_limit_bytes",
        "tests_passed",
    }
)

DRAFT_PR_FIELDS = frozenset(
    {
        "schema_version",
        "draft_pr_id",
        "repair_job_id",
        "pr_id",
        "row_kind",
        "head_branch_sha256",
        "base_branch",
        "commit_sha",
        "draft",
        "ready",
        "merged",
        "comments_checks_labels_reviews",
        "publisher_status",
        "receipt_sha256",
        "redaction_applied",
    }
)

FEEDBACK_FIELDS = frozenset(
    {
        "schema_version",
        "feedback_id",
        "pr_id",
        "finding_id",
        "row_kind",
        "participant_id",
        "decision",
        "repair_requested",
        "draft_pr_adopted",
        "rationale_sha256",
        "submitted_at_utc",
        "human_attested",
    }
)

TIME_COST_FIELDS = frozenset(
    {
        "schema_version",
        "receipt_id",
        "pr_id",
        "row_kind",
        "active_review_seconds",
        "paused_review_seconds",
        "end_to_end_latency_ms",
        "cost_micro_cny",
        "recorded_at_utc",
        "human_attested",
    }
)

INCIDENT_FIELDS = frozenset(
    {
        "schema_version",
        "incident_id",
        "row_kind",
        "severity",
        "stop_reason",
        "kill_switch_activated",
        "new_tasks_stopped",
        "unresolved",
        "credential_revoked_or_isolated",
        "quarantine_count",
        "unauthorized_operation_count",
        "duplicate_external_write_count",
        "redaction_applied",
        "recorded_at_utc",
    }
)

REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "report_id",
        "cohort_id",
        "selected_pr_count",
        "synthetic_rows_present",
        "headline_completion",
        "feedback_coverage",
        "decision_counts",
        "repair_requested",
        "write_approval",
        "draft_pr_approval",
        "draft_pr_created",
        "draft_pr_adopted",
        "active_human_review_time_seconds",
        "end_to_end_latency_ms",
        "cost_micro_cny",
        "failure_counts",
        "unauthorized_operation_count",
        "duplicate_external_write_count",
        "business_claim_allowed",
        "model_quality_status",
        "formal_quality_status",
        "claim_scope",
    }
)

CLAIM_DECISION_FIELDS = frozenset(
    {
        "schema_version",
        "report_id",
        "business_claim_allowed",
        "business_claim_reason",
        "model_quality_status",
        "formal_quality_status",
        "pilot_completed_does_not_equal_success",
        "phase11c_provider_reliability_not_proven",
        "auth004_unchanged",
        "new_denominator",
        "generalization_denied",
    }
)

ACCEPTANCE_FIELDS = frozenset(
    {
        "schema_version",
        "report_id",
        "gate_a_offline_validation_ready",
        "gate_b_execution_allowed",
        "implementation_draft_pr_status",
        "ci_status",
        "frozen_hashes_ready",
        "remaining_gate_b_blockers",
        "final_project_complete",
        "production_ready",
        "model_quality_status",
        "formal_quality_status",
    }
)

MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "manifest_id",
        "manifest_sha256",
        "generated_at_utc",
        "baseline_sha",
        "local_master_at_start",
        "phase11c_facts",
        "auth004_boundary",
        "artifacts",
    }
)

ARTIFACT_FIELDS = frozenset({"kind", "path", "sha256"})

CONSENT_RECEIPTS_FIELDS = frozenset(
    {
        "schema_version",
        "participants",
        "identity_map_committed",
        "synthetic_rows_present",
        "real_rows_present",
    }
)

PARTICIPANT_FIELDS = frozenset(
    {
        "participant_id",
        "row_kind",
        "confirmed_real",
        "role",
        "consent_receipt_sha256",
        "consent_scope_sha256",
        "retention_days",
    }
)

REPOSITORY_ALLOWLIST_FIELDS = frozenset(
    {
        "schema_version",
        "allowlist_id",
        "repositories",
        "raw_repository_locator_committed",
    }
)

REPOSITORY_FIELDS = frozenset(
    {
        "repository_id",
        "row_kind",
        "locator_sha256",
        "allowed_base_branch_rule_sha256",
        "github_writes_allowed",
    }
)

SELECTION_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "selection_receipt_id",
        "cohort_id",
        "eligible_count",
        "selected_count",
        "excluded_count",
        "selection_seed_sha256",
        "selected_pr_ids_sha256",
        "selection_before_agent_output",
        "replacement_after_failure_allowed",
    }
)

REQUIRED_MANIFEST_ARTIFACTS = frozenset(
    {
        "authorization.json",
        "consent-receipts.json",
        "repository-allowlist.json",
        "cohort.json",
        "selection-receipt.json",
        "headline-manifest.json",
        "review-receipts.jsonl",
        "repair-receipts.jsonl",
        "draft-pr-receipts.jsonl",
        "feedback-receipts.jsonl",
        "time-cost-latency-receipts.jsonl",
        "incident-stop-receipts.jsonl",
        "business-report.json",
        "claim-decision-report.json",
        "final-acceptance-report.json",
    }
)

ROLES_ALLOWED_TO_APPROVE = frozenset({"maintainer", "org_admin"})
ACTOR_METHODS_DENIED = frozenset(
    {"anonymous", "agent", "finding", "github_webhook", "model", "system", "webhook"}
)
REVIEW_STATUSES = frozenset({"completed", "failed", "inconclusive"})
FEEDBACK_DECISIONS = frozenset({"accepted", "rejected", "uncertain", "fixed", "duplicate"})
TERMINAL_FAILURES = frozenset(
    {
        "completed",
        "provider_text_only_response",
        "provider_malformed_tool_response",
        "provider_failure",
        "provider_tool_call_mismatch",
        "provider_schema_mismatch",
        "provider_usage_ambiguity",
        "missing_receipt_declared",
        "receipt_mismatch",
        "approval_declined",
        "budget_exhausted",
        "publisher_ambiguous_result",
        "publisher_failed",
        "kill_switch_activated",
        "not_run_gate_blocked",
    }
)

REPAIR_FINAL_STATUSES = frozenset(
    {
        "draft_pr_created",
        "declined",
        "test_failed",
        "budget_exhausted",
        "publisher_failed",
        "quarantined",
        "not_run_gate_blocked",
    }
)

PUBLISHER_STATUSES = frozenset(
    {
        "draft_published",
        "not_published",
        "publisher_failed",
        "publisher_ambiguous_result",
        "quarantined",
    }
)

REPAIR_FAILURE_CATEGORIES = frozenset(
    {
        "none",
        "approval_declined",
        "test_failure",
        "budget_exhausted",
        "publisher_failed",
        "publisher_ambiguous_result",
        "credential_revoked",
        "kill_switch_activated",
        "receipt_mismatch",
        "checkpoint_mismatch",
    }
)

UTC_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")

PROHIBITED_KEYS = frozenset(
    {
        "api_key",
        "authorization_header",
        "comment_body",
        "credential_value",
        "diff",
        "exception_message",
        "host_path",
        "private_key",
        "prompt",
        "provider_response",
        "raw_diff",
        "repository_locator",
        "response_text",
        "secret",
        "stderr",
        "stdout",
        "token",
    }
)

PROHIBITED_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"diff --git "),
)

GATE_B_REQUIRED_FIELDS = (
    "authorization_id",
    "canonical_authorization_sha256",
    "frozen_source_tree_sha256",
    "frozen_executable_source_sha256",
    "frozen_runtime_image_sha256",
    "frozen_deployment_sha256",
    "frozen_runtime_identity_sha256",
    "organization_id",
    "participant_stable_ids",
    "participant_roles",
    "participant_consent_receipt_sha256",
    "repository_allowlist",
    "allowed_base_branch_rule",
    "pr_selection_window_start_utc",
    "pr_selection_window_end_utc",
    "deterministic_selection_rule",
    "deterministic_selection_seed_sha256",
    "selected_pr_count",
    "max_repair_findings_per_pr",
    "max_repair_jobs",
    "max_real_branches",
    "max_real_commits",
    "max_real_pushes",
    "max_real_draft_repair_prs",
    "github_app_installation_id",
    "github_repository_scopes",
    "provider_model_snapshot",
    "provider_endpoint_allowlist",
    "max_logical_calls",
    "max_http_attempts",
    "max_input_tokens",
    "max_output_tokens",
    "max_cached_tokens",
    "max_micro_cny",
    "max_wall_clock_seconds",
    "data_classification",
    "provider_sendable_code_scope",
    "raw_content_retention_days",
    "metadata_retention_days",
    "feedback_retention_days",
    "deletion_owner_process",
    "incident_owner",
    "kill_switch",
    "credential_delivery_mode",
    "credential_fingerprint_sha256",
    "credential_revoke_procedure",
    "human_approval_sla_seconds",
    "business_success_thresholds",
    "safety_stop_thresholds",
    "cost_stop_thresholds",
)


class Phase11DError(RuntimeError):
    """Stable offline validation failure."""


@dataclass(frozen=True)
class ValidationSummary:
    selected_prs: int
    completed_headlines: int
    feedback_eligible_findings: int
    repair_jobs: int
    draft_pr_receipts: int
    business_claim_allowed: bool
    gate_b_allowed: bool
    gate_b_blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_prs": self.selected_prs,
            "completed_headlines": self.completed_headlines,
            "feedback_eligible_findings": self.feedback_eligible_findings,
            "repair_jobs": self.repair_jobs,
            "draft_pr_receipts": self.draft_pr_receipts,
            "business_claim_allowed": self.business_claim_allowed,
            "gate_b_allowed": self.gate_b_allowed,
            "gate_b_blockers": list(self.gate_b_blockers),
        }


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Phase11DError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise Phase11DError(f"non-finite JSON value is prohibited: {value}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise Phase11DError(f"{path.name}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise Phase11DError(f"{path.name}: expected JSON object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except json.JSONDecodeError as exc:
            raise Phase11DError(f"{path.name}:{line_number}: invalid JSONL: {exc}") from exc
        if not isinstance(value, dict):
            raise Phase11DError(f"{path.name}:{line_number}: expected JSON object")
        rows.append(value)
    return rows


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def _self_hash(value: Mapping[str, Any], field: str) -> str:
    data = dict(value)
    data[field] = ""
    return sha256_bytes(canonical_json(data))


def _with_self_hash(value: dict[str, Any], field: str) -> dict[str, Any]:
    value[field] = ""
    value[field] = _self_hash(value, field)
    return value


def _exact_fields(name: str, value: Mapping[str, Any], expected: frozenset[str]) -> None:
    keys = set(value)
    if keys == expected:
        return
    missing = sorted(expected - keys)
    unexpected = sorted(keys - expected)
    detail = []
    if missing:
        detail.append("missing " + ", ".join(missing))
    if unexpected:
        detail.append("unexpected " + ", ".join(unexpected))
    raise Phase11DError(f"{name}: invalid fields ({'; '.join(detail)})")


def _require_str(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise Phase11DError(f"{name}: expected non-empty string")
    return value


def _require_bool(name: str, value: Any) -> bool:
    if type(value) is not bool:
        raise Phase11DError(f"{name}: expected boolean")
    return value


def _require_int(name: str, value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise Phase11DError(f"{name}: expected integer >= {minimum}")
    return value


def _require_sha256(name: str, value: Any) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise Phase11DError(f"{name}: expected lowercase SHA-256")
    return value


def _require_git_sha(name: str, value: Any) -> str:
    if not isinstance(value, str) or GIT_SHA_RE.fullmatch(value) is None:
        raise Phase11DError(f"{name}: expected lowercase Git SHA-1")
    return value


def _require_utc(name: str, value: Any) -> str:
    if not isinstance(value, str) or UTC_RE.fullmatch(value) is None:
        raise Phase11DError(f"{name}: expected UTC timestamp YYYY-MM-DDTHH:MM:SSZ")
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return value


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    if denominator == 0:
        value: float | None = None
    else:
        value = numerator / denominator
    return {"numerator": numerator, "denominator": denominator, "value": value}


def _percentile(values: Sequence[int], percentile: float) -> int | None:
    if not values:
        return None
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _scan_no_raw_content(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise Phase11DError(f"{path}: non-string JSON key")
            if key.casefold() in PROHIBITED_KEYS:
                raise Phase11DError(f"{path}.{key}: prohibited raw-content key")
            _scan_no_raw_content(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_no_raw_content(item, f"{path}[{index}]")
    elif isinstance(value, str):
        for pattern in PROHIBITED_VALUE_PATTERNS:
            if pattern.search(value):
                raise Phase11DError(f"{path}: prohibited raw-content value")


def _hash_marker(prefix: str, index: int) -> str:
    return sha256_text(f"phase11d/{prefix}/{index:03d}")


def _expected_artifact_kind(name: str) -> str:
    return name.rsplit(".", 1)[0].replace("-", "_")


def _approval_binding(kind: str, repair: Mapping[str, Any]) -> str:
    if kind == "write":
        payload = {
            "kind": kind,
            "repair_job_id": repair["repair_job_id"],
            "pr_id": repair["pr_id"],
            "finding_id": repair["finding_id"],
            "base_sha": repair["base_sha"],
            "head_sha": repair["head_sha"],
            "worktree_receipt_sha256": repair["worktree_receipt_sha256"],
            "task_branch_sha256": repair["task_branch_sha256"],
            "plan_sha256": repair["plan_sha256"],
            "budget_sha256": repair["budget_sha256"],
        }
    elif kind == "draft_pr":
        payload = {
            "kind": kind,
            "repair_job_id": repair["repair_job_id"],
            "pr_id": repair["pr_id"],
            "finding_id": repair["finding_id"],
            "base_sha": repair["base_sha"],
            "head_sha": repair["head_sha"],
            "plan_sha256": repair["plan_sha256"],
            "patch_sha256": repair["patch_sha256"],
            "checkpoint_sha256": repair["checkpoint_sha256"],
            "test_sha256": repair["test_sha256"],
            "budget_sha256": repair["budget_sha256"],
            "sandbox": repair["sandbox"],
            "commit_sha": repair["commit_sha"],
        }
    else:
        raise Phase11DError(f"unknown approval binding kind: {kind}")
    return sha256_bytes(canonical_json(payload))


def build_gate_b_template() -> dict[str, Any]:
    permissions = {
        "allow_real_provider_calls": False,
        "allow_real_github_repair_branch_push": False,
        "allow_real_draft_repair_pr": False,
        "allow_comments_checks_labels_reviews": False,
        "allow_pilot_pr_ready": False,
        "allow_pilot_pr_merge": False,
        "allow_default_branch_mutation": False,
        "allow_auto_merge": False,
        "allow_agent_push_merge_master": False,
    }
    required: dict[str, Any] = {field: None for field in GATE_B_REQUIRED_FIELDS}
    required["authorization_id"] = "phase11d-gate-b-human-pilot-v1-20260805-001"
    required["github_app_installation_id"] = 149747930
    return {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "template_id": "phase11d-gate-b-authorization-template-v1",
        "template_status": "incomplete_gate_b_template",
        "generated_at_utc": GENERATED_AT_UTC,
        "required_fields": required,
        "permission_switches": permissions,
        "exact_approval_text": "PENDING_FREEZE",
        "gate_b_allowed": False,
        "business_claim_allowed": False,
        "model_quality_status": "not_measured",
        "formal_quality_status": "incomplete",
    }


def evaluate_gate_b_template(template: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    blockers: list[str] = []
    required = template.get("required_fields")
    if not isinstance(required, Mapping):
        blockers.append("required_fields_missing")
    else:
        for field in GATE_B_REQUIRED_FIELDS:
            value = required.get(field)
            if value in (None, "", "PENDING", "PENDING_FREEZE"):
                blockers.append(f"missing:{field}")
    permissions = template.get("permission_switches")
    if not isinstance(permissions, Mapping):
        blockers.append("permission_switches_missing")
    else:
        for name in (
            "allow_real_provider_calls",
            "allow_real_github_repair_branch_push",
            "allow_real_draft_repair_pr",
        ):
            if permissions.get(name) is not True:
                blockers.append(f"permission_not_granted:{name}")
        for name in (
            "allow_comments_checks_labels_reviews",
            "allow_pilot_pr_ready",
            "allow_pilot_pr_merge",
            "allow_default_branch_mutation",
            "allow_auto_merge",
            "allow_agent_push_merge_master",
        ):
            if permissions.get(name) is not False:
                blockers.append(f"prohibited_permission_enabled:{name}")
    if template.get("exact_approval_text") in (None, "", "PENDING_FREEZE"):
        blockers.append("exact_approval_text_missing")
    return (not blockers, tuple(blockers))


def _permission_switches() -> dict[str, bool]:
    return {
        "allow_real_provider_calls": False,
        "allow_real_github_repair_branch_push": False,
        "allow_real_draft_repair_pr": False,
        "allow_comments_checks_labels_reviews": False,
        "allow_pilot_pr_ready": False,
        "allow_pilot_pr_merge": False,
        "allow_default_branch_mutation": False,
        "allow_auto_merge": False,
        "allow_agent_push_merge_master": False,
    }


def _participants() -> dict[str, Any]:
    rows = []
    roles = ("maintainer", "maintainer", "org_admin")
    for index, role in enumerate(roles, 1):
        rows.append(
            {
                "participant_id": f"synthetic-participant-{index:03d}",
                "row_kind": "synthetic",
                "confirmed_real": False,
                "role": role,
                "consent_receipt_sha256": _hash_marker("consent", index),
                "consent_scope_sha256": _hash_marker("consent-scope", index),
                "retention_days": 0,
            }
        )
    return {
        "schema_version": "crag.phase11d.consent-receipts/v1alpha1",
        "participants": rows,
        "identity_map_committed": False,
        "synthetic_rows_present": True,
        "real_rows_present": False,
    }


def _repository_allowlist() -> dict[str, Any]:
    return {
        "schema_version": "crag.phase11d.repository-allowlist/v1alpha1",
        "allowlist_id": "phase11d-gate-a-synthetic-repositories",
        "repositories": [
            {
                "repository_id": "synthetic-repo-001",
                "row_kind": "synthetic",
                "locator_sha256": _hash_marker("repo-locator", 1),
                "allowed_base_branch_rule_sha256": _hash_marker("base-rule", 1),
                "github_writes_allowed": False,
            }
        ],
        "raw_repository_locator_committed": False,
    }


def _cohort() -> dict[str, Any]:
    selected = []
    for index in range(1, 21):
        selected.append(
            {
                "pr_id": f"synthetic-pr-{index:03d}",
                "row_kind": "synthetic",
                "repository_id": "synthetic-repo-001",
                "snapshot_sha256": _hash_marker("snapshot", index),
                "diff_sha256": _hash_marker("diff", index),
                "headline_id": f"headline-{index:03d}",
                "selection_rank_sha256": _hash_marker("rank", index),
            }
        )
    return {
        "schema_version": COHORT_SCHEMA_VERSION,
        "cohort_id": "phase11d-gate-a-synthetic-cohort",
        "authorization_id": "phase11d-gate-a-synthetic-auth",
        "synthetic_rows_present": True,
        "real_rows_present": False,
        "selection_seed_sha256": _hash_marker("selection-seed", 0),
        "selection_window_start_utc": "2026-08-01T00:00:00Z",
        "selection_window_end_utc": "2026-08-02T00:00:00Z",
        "eligible_count": 22,
        "excluded_count": 2,
        "selected_pr_count": 20,
        "selected_prs": selected,
        "excluded": [
            {
                "candidate_id": "synthetic-excluded-001",
                "row_kind": "synthetic",
                "exclusion_reason": "outside_authorized_repository",
            },
            {
                "candidate_id": "synthetic-excluded-002",
                "row_kind": "synthetic",
                "exclusion_reason": "missing_required_base_sha",
            },
        ],
    }


def _selection_receipt(cohort: Mapping[str, Any]) -> dict[str, Any]:
    selected = cohort["selected_prs"]
    return {
        "schema_version": "crag.phase11d.selection-receipt/v1alpha1",
        "selection_receipt_id": "phase11d-gate-a-synthetic-selection",
        "cohort_id": cohort["cohort_id"],
        "eligible_count": cohort["eligible_count"],
        "selected_count": cohort["selected_pr_count"],
        "excluded_count": cohort["excluded_count"],
        "selection_seed_sha256": cohort["selection_seed_sha256"],
        "selected_pr_ids_sha256": sha256_bytes(
            canonical_json([row["pr_id"] for row in selected])
        ),
        "selection_before_agent_output": True,
        "replacement_after_failure_allowed": False,
    }


def _headline_manifest(cohort: Mapping[str, Any]) -> dict[str, Any]:
    headlines = [
        {
            "pr_id": row["pr_id"],
            "headline_id": row["headline_id"],
            "attempt_number": 1,
            "review_receipt_id": f"review-receipt-{index:03d}",
        }
        for index, row in enumerate(cohort["selected_prs"], 1)
    ]
    return {
        "schema_version": "crag.phase11d.headline-manifest/v1alpha1",
        "manifest_id": "phase11d-gate-a-synthetic-headlines",
        "cohort_id": cohort["cohort_id"],
        "authorization_id": cohort["authorization_id"],
        "immutable_headline_count": len(headlines),
        "headlines": headlines,
        "diagnostic_may_replace_headline": False,
    }


def _review_receipts(cohort: Mapping[str, Any]) -> list[dict[str, Any]]:
    statuses = {
        2: ("failed", "provider_text_only_response", ()),
        3: ("failed", "missing_receipt_declared", ()),
        7: ("failed", "provider_malformed_tool_response", ()),
        11: ("failed", "provider_failure", ()),
    }
    findings = {
        1: ("finding-001",),
        4: ("finding-004",),
        8: ("finding-008",),
    }
    rows = []
    for index, pr in enumerate(cohort["selected_prs"], 1):
        status, terminal, default_findings = statuses.get(index, ("completed", "completed", ()))
        finding_ids = list(findings.get(index, default_findings))
        rows.append(
            {
                "schema_version": REVIEW_RECEIPT_SCHEMA_VERSION,
                "receipt_id": f"review-receipt-{index:03d}",
                "pr_id": pr["pr_id"],
                "headline_id": pr["headline_id"],
                "row_kind": "synthetic",
                "attempt_number": 1,
                "status": status,
                "terminal_category": terminal,
                "finding_ids": finding_ids,
                "feedback_eligible_finding_ids": list(finding_ids),
                "provider_call_count": 0 if terminal == "missing_receipt_declared" else 1,
                "http_attempt_count": 0 if terminal == "missing_receipt_declared" else 1,
                "input_tokens": 0 if terminal == "missing_receipt_declared" else 100 + index,
                "output_tokens": 0 if terminal == "missing_receipt_declared" else 10 + index,
                "cached_tokens": 0,
                "cost_micro_cny": 0 if terminal == "missing_receipt_declared" else 1000 + index,
                "latency_ms": 1000 + index * 10,
                "trace_sha256": _hash_marker("review-trace", index),
                "redaction_applied": True,
            }
        )
    return rows


def _approval(approval_id: str, decision: str, role: str, binding: str) -> dict[str, Any]:
    return {
        "approval_id": approval_id,
        "decision": decision,
        "actor_role": role,
        "actor_method": "human",
        "binding_sha256": binding,
        "consumed": decision == "approved",
    }


def _repair_receipts() -> list[dict[str, Any]]:
    base = "1" * 40
    head = "2" * 40
    rows = [
        {
            "schema_version": REPAIR_RECEIPT_SCHEMA_VERSION,
            "repair_job_id": "repair-job-001",
            "pr_id": "synthetic-pr-001",
            "finding_id": "finding-001",
            "row_kind": "synthetic",
            "request_actor_role": "maintainer",
            "request_actor_method": "human",
            "base_sha": base,
            "head_sha": head,
            "worktree_receipt_sha256": _hash_marker("worktree", 1),
            "task_branch_sha256": _hash_marker("task-branch", 1),
            "plan_sha256": _hash_marker("plan", 1),
            "write_approval": _approval(
                "write-approval-001", "approved", "maintainer", "0" * 64
            ),
            "patch_sha256": _hash_marker("patch", 1),
            "checkpoint_sha256": _hash_marker("checkpoint", 1),
            "test_sha256": _hash_marker("test", 1),
            "budget_sha256": _hash_marker("budget", 1),
            "sandbox": {
                "docker": True,
                "network_mode": "none",
                "non_root": True,
                "timeout_seconds": 60,
                "output_limit_bytes": 65536,
                "tests_passed": True,
            },
            "draft_pr_approval": _approval(
                "draft-approval-001", "approved", "org_admin", "0" * 64
            ),
            "commit_sha": "3" * 40,
            "publisher_status": "draft_published",
            "final_status": "draft_pr_created",
            "failure_category": "none",
            "cost_micro_cny": 2000,
        },
        {
            "schema_version": REPAIR_RECEIPT_SCHEMA_VERSION,
            "repair_job_id": "repair-job-008",
            "pr_id": "synthetic-pr-008",
            "finding_id": "finding-008",
            "row_kind": "synthetic",
            "request_actor_role": "maintainer",
            "request_actor_method": "human",
            "base_sha": base,
            "head_sha": head,
            "worktree_receipt_sha256": _hash_marker("worktree", 8),
            "task_branch_sha256": _hash_marker("task-branch", 8),
            "plan_sha256": _hash_marker("plan", 8),
            "write_approval": _approval(
                "write-approval-008", "declined", "maintainer", "0" * 64
            ),
            "patch_sha256": _hash_marker("patch", 8),
            "checkpoint_sha256": _hash_marker("checkpoint", 8),
            "test_sha256": _hash_marker("test", 8),
            "budget_sha256": _hash_marker("budget", 8),
            "sandbox": {
                "docker": True,
                "network_mode": "none",
                "non_root": True,
                "timeout_seconds": 60,
                "output_limit_bytes": 65536,
                "tests_passed": False,
            },
            "draft_pr_approval": _approval(
                "draft-approval-008", "not_requested", "maintainer", "0" * 64
            ),
            "commit_sha": "",
            "publisher_status": "not_published",
            "final_status": "declined",
            "failure_category": "approval_declined",
            "cost_micro_cny": 0,
        },
    ]
    for row in rows:
        write_approval = cast(dict[str, Any], row["write_approval"])
        draft_approval = cast(dict[str, Any], row["draft_pr_approval"])
        write_approval["binding_sha256"] = _approval_binding("write", row)
        draft_approval["binding_sha256"] = _approval_binding("draft_pr", row)
    return rows


def _draft_pr_receipts() -> list[dict[str, Any]]:
    return [
        {
            "schema_version": DRAFT_PR_RECEIPT_SCHEMA_VERSION,
            "draft_pr_id": "draft-pr-001",
            "repair_job_id": "repair-job-001",
            "pr_id": "synthetic-pr-001",
            "row_kind": "synthetic",
            "head_branch_sha256": _hash_marker("draft-head", 1),
            "base_branch": "main",
            "commit_sha": "3" * 40,
            "draft": True,
            "ready": False,
            "merged": False,
            "comments_checks_labels_reviews": 0,
            "publisher_status": "draft_published",
            "receipt_sha256": _hash_marker("draft-receipt", 1),
            "redaction_applied": True,
        }
    ]


def _feedback_receipts() -> list[dict[str, Any]]:
    decisions = (
        ("feedback-001", "synthetic-pr-001", "finding-001", "accepted", True, True),
        ("feedback-004", "synthetic-pr-004", "finding-004", "rejected", False, False),
        ("feedback-008", "synthetic-pr-008", "finding-008", "uncertain", True, False),
    )
    return [
        {
            "schema_version": FEEDBACK_SCHEMA_VERSION,
            "feedback_id": feedback_id,
            "pr_id": pr_id,
            "finding_id": finding_id,
            "row_kind": "synthetic",
            "participant_id": "synthetic-participant-001",
            "decision": decision,
            "repair_requested": repair_requested,
            "draft_pr_adopted": adopted,
            "rationale_sha256": _hash_marker("feedback-rationale", index),
            "submitted_at_utc": GENERATED_AT_UTC,
            "human_attested": True,
        }
        for index, (feedback_id, pr_id, finding_id, decision, repair_requested, adopted)
        in enumerate(decisions, 1)
    ]


def _time_cost_receipts(cohort: Mapping[str, Any], reviews: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_pr = {row["pr_id"]: row for row in reviews}
    rows = []
    for index, pr in enumerate(cohort["selected_prs"], 1):
        review = by_pr[pr["pr_id"]]
        rows.append(
            {
                "schema_version": "crag.phase11d.time-cost-latency/v1alpha1",
                "receipt_id": f"time-cost-{index:03d}",
                "pr_id": pr["pr_id"],
                "row_kind": "synthetic",
                "active_review_seconds": index * 3,
                "paused_review_seconds": index,
                "end_to_end_latency_ms": review["latency_ms"],
                "cost_micro_cny": review["cost_micro_cny"],
                "recorded_at_utc": GENERATED_AT_UTC,
                "human_attested": True,
            }
        )
    return rows


def _incident_receipts() -> list[dict[str, Any]]:
    return [
        {
            "schema_version": INCIDENT_SCHEMA_VERSION,
            "incident_id": "incident-none-001",
            "row_kind": "synthetic",
            "severity": "none",
            "stop_reason": "none",
            "kill_switch_activated": False,
            "new_tasks_stopped": False,
            "unresolved": False,
            "credential_revoked_or_isolated": True,
            "quarantine_count": 0,
            "unauthorized_operation_count": 0,
            "duplicate_external_write_count": 0,
            "redaction_applied": True,
            "recorded_at_utc": GENERATED_AT_UTC,
        }
    ]


def _authorization(
    repository_allowlist_sha: str,
    cohort_sha: str,
    selection_sha: str,
    headline_sha: str,
) -> dict[str, Any]:
    document = {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "gate": "gate_a",
        "authorization_id": "phase11d-gate-a-synthetic-auth",
        "canonical_authorization_sha256": "",
        "created_at_utc": GENERATED_AT_UTC,
        "baseline": {
            "origin_master_sha": BASELINE_SHA,
            "local_master_at_start": LOCAL_MASTER_AT_START,
            "local_master_equal_origin_master": False,
            "master_mutated_by_codex": False,
        },
        "phase11c_facts": EXPECTED_PHASE11C,
        "auth004_boundary": AUTH004_BOUNDARY,
        "permissions": _permission_switches(),
        "participants_declared_count": 3,
        "confirmed_real_participant_count": 0,
        "selected_pr_count": 20,
        "synthetic_rows_present": True,
        "real_rows_present": False,
        "repository_allowlist_sha256": repository_allowlist_sha,
        "cohort_sha256": cohort_sha,
        "selection_sha256": selection_sha,
        "headline_manifest_sha256": headline_sha,
        "limits": {
            "max_logical_calls": 0,
            "max_http_attempts": 0,
            "max_input_tokens": 0,
            "max_output_tokens": 0,
            "max_cached_tokens": 0,
            "max_micro_cny": 0,
            "max_wall_clock_seconds": 0,
            "max_repair_jobs": 2,
            "max_real_branches": 0,
            "max_real_commits": 0,
            "max_real_pushes": 0,
            "max_real_draft_repair_prs": 0,
        },
        "provider_policy": {
            "provider_endpoint_allowlist": [],
            "provider_model_snapshot": "not_authorized_gate_a",
            "provider_text_only_response_terminal": True,
            "usage_ambiguity_terminal": True,
        },
        "github_policy": {
            "publisher_mode": "fake_only",
            "merge_api_available": False,
            "protected_branch_mutation_available": False,
            "comments_checks_labels_reviews_available": False,
        },
        "retention_policy": {
            "raw_content_retention_days": 0,
            "metadata_retention_days": 0,
            "feedback_retention_days": 0,
            "deletion_owner_process_sha256": _hash_marker("deletion-process", 1),
        },
        "incident_policy": {
            "incident_owner_sha256": _hash_marker("incident-owner", 1),
            "kill_switch_active": False,
            "kill_switch_sha256": _hash_marker("kill-switch", 1),
        },
        "claim_boundary": CLAIM_BOUNDARY,
        "gate_b_required_fields_complete": False,
    }
    return _with_self_hash(document, "canonical_authorization_sha256")


def _business_report(
    cohort: Mapping[str, Any],
    reviews: Sequence[Mapping[str, Any]],
    repairs: Sequence[Mapping[str, Any]],
    drafts: Sequence[Mapping[str, Any]],
    feedback: Sequence[Mapping[str, Any]],
    time_cost: Sequence[Mapping[str, Any]],
    incidents: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    selected_count = _require_int("selected_pr_count", cohort["selected_pr_count"])
    completed = sum(1 for row in reviews if row["status"] == "completed")
    feedback_eligible = {
        finding for row in reviews for finding in row["feedback_eligible_finding_ids"]
    }
    responded = {row["finding_id"] for row in feedback}
    decisions = {decision: 0 for decision in FEEDBACK_DECISIONS}
    for row in feedback:
        decisions[str(row["decision"])] += 1
    repair_requested = sum(1 for row in feedback if row["repair_requested"] is True)
    write_approved = sum(1 for row in repairs if row["write_approval"]["decision"] == "approved")
    write_declined = sum(1 for row in repairs if row["write_approval"]["decision"] == "declined")
    draft_approved = sum(
        1 for row in repairs if row["draft_pr_approval"]["decision"] == "approved"
    )
    draft_declined = sum(
        1 for row in repairs if row["draft_pr_approval"]["decision"] == "declined"
    )
    draft_created = sum(1 for row in drafts if row["publisher_status"] == "draft_published")
    draft_adopted = sum(1 for row in feedback if row["draft_pr_adopted"] is True)
    active_seconds = [int(row["active_review_seconds"]) for row in time_cost]
    latencies = [int(row["end_to_end_latency_ms"]) for row in time_cost]
    total_cost = sum(int(row["cost_micro_cny"]) for row in reviews) + sum(
        int(row["cost_micro_cny"]) for row in repairs
    )
    failures: dict[str, int] = {}
    for row in reviews:
        if row["terminal_category"] != "completed":
            category = str(row["terminal_category"])
            failures[category] = failures.get(category, 0) + 1
    unauthorized = sum(int(row["unauthorized_operation_count"]) for row in incidents)
    duplicates = sum(int(row["duplicate_external_write_count"]) for row in incidents)
    return {
        "schema_version": BUSINESS_REPORT_SCHEMA_VERSION,
        "report_id": "phase11d-gate-a-synthetic-business-report",
        "cohort_id": cohort["cohort_id"],
        "selected_pr_count": selected_count,
        "synthetic_rows_present": True,
        "headline_completion": _rate(completed, selected_count),
        "feedback_coverage": _rate(len(responded & feedback_eligible), len(feedback_eligible)),
        "decision_counts": decisions,
        "repair_requested": _rate(repair_requested, len(feedback_eligible)),
        "write_approval": {
            "approved": write_approved,
            "declined": write_declined,
            "denominator": len(repairs),
        },
        "draft_pr_approval": {
            "approved": draft_approved,
            "declined": draft_declined,
            "denominator": len(repairs),
        },
        "draft_pr_created": _rate(draft_created, len(repairs)),
        "draft_pr_adopted": _rate(draft_adopted, len(feedback_eligible)),
        "active_human_review_time_seconds": {
            "p50": _percentile(active_seconds, 50),
            "p95": _percentile(active_seconds, 95),
            "sample_count": len(active_seconds),
        },
        "end_to_end_latency_ms": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "sample_count": len(latencies),
        },
        "cost_micro_cny": {
            "total": total_cost,
            "per_selected_pr": total_cost // selected_count,
            "per_adopted_finding": total_cost // draft_adopted if draft_adopted else None,
        },
        "failure_counts": failures,
        "unauthorized_operation_count": unauthorized,
        "duplicate_external_write_count": duplicates,
        "business_claim_allowed": False,
        "model_quality_status": "not_measured",
        "formal_quality_status": "incomplete",
        "claim_scope": "synthetic_gate_a_only",
    }


def _claim_decision() -> dict[str, Any]:
    return {
        "schema_version": CLAIM_DECISION_SCHEMA_VERSION,
        "report_id": "phase11d-gate-a-synthetic-claim-decision",
        "business_claim_allowed": False,
        "business_claim_reason": "synthetic_rows_present_and_gate_b_not_executed",
        "model_quality_status": "not_measured",
        "formal_quality_status": "incomplete",
        "pilot_completed_does_not_equal_success": True,
        "phase11c_provider_reliability_not_proven": True,
        "auth004_unchanged": True,
        "new_denominator": True,
        "generalization_denied": True,
    }


def _acceptance_report(blockers: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "report_id": "phase11d-gate-a-synthetic-acceptance",
        "gate_a_offline_validation_ready": True,
        "gate_b_execution_allowed": False,
        "implementation_draft_pr_status": "pending_external_delivery",
        "ci_status": "pending_external_ci",
        "frozen_hashes_ready": False,
        "remaining_gate_b_blockers": list(blockers),
        "final_project_complete": False,
        "production_ready": False,
        "model_quality_status": "not_measured",
        "formal_quality_status": "incomplete",
    }


def build_gate_a_bundle() -> dict[str, Any]:
    participants = _participants()
    repositories = _repository_allowlist()
    cohort = _cohort()
    selection = _selection_receipt(cohort)
    headline = _headline_manifest(cohort)
    authorization = _authorization(
        sha256_bytes(canonical_json(repositories)),
        sha256_bytes(canonical_json(cohort)),
        sha256_bytes(canonical_json(selection)),
        sha256_bytes(canonical_json(headline)),
    )
    reviews = _review_receipts(cohort)
    repairs = _repair_receipts()
    drafts = _draft_pr_receipts()
    feedback = _feedback_receipts()
    time_cost = _time_cost_receipts(cohort, reviews)
    incidents = _incident_receipts()
    business = _business_report(cohort, reviews, repairs, drafts, feedback, time_cost, incidents)
    claim = _claim_decision()
    template = build_gate_b_template()
    _allowed, blockers = evaluate_gate_b_template(template)
    acceptance = _acceptance_report(blockers)
    files: dict[str, Any] = {
        "authorization.json": authorization,
        "consent-receipts.json": participants,
        "repository-allowlist.json": repositories,
        "cohort.json": cohort,
        "selection-receipt.json": selection,
        "headline-manifest.json": headline,
        "review-receipts.jsonl": reviews,
        "repair-receipts.jsonl": repairs,
        "draft-pr-receipts.jsonl": drafts,
        "feedback-receipts.jsonl": feedback,
        "time-cost-latency-receipts.jsonl": time_cost,
        "incident-stop-receipts.jsonl": incidents,
        "business-report.json": business,
        "claim-decision-report.json": claim,
        "final-acceptance-report.json": acceptance,
    }
    manifest = _manifest(files)
    files["canonical-manifest.json"] = manifest
    return files


def _artifact_bytes(name: str, value: Any) -> bytes:
    if name.endswith(".jsonl"):
        if not isinstance(value, list):
            raise Phase11DError(f"{name}: JSONL artifact must be a list")
        return "".join(
            json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
            for row in value
        ).encode("utf-8")
    if not isinstance(value, Mapping):
        raise Phase11DError(f"{name}: JSON artifact must be an object")
    return canonical_json(value)


def _artifact_sha256(name: str, value: Any) -> str:
    return sha256_bytes(_artifact_bytes(name, value))


def _manifest(files: Mapping[str, Any]) -> dict[str, Any]:
    artifacts = [
        {
            "kind": _expected_artifact_kind(name),
            "path": name,
            "sha256": sha256_bytes(_artifact_bytes(name, value)),
        }
        for name, value in sorted(files.items())
    ]
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_id": "phase11d-gate-a-synthetic-manifest",
        "manifest_sha256": "",
        "generated_at_utc": GENERATED_AT_UTC,
        "baseline_sha": BASELINE_SHA,
        "local_master_at_start": LOCAL_MASTER_AT_START,
        "phase11c_facts": EXPECTED_PHASE11C,
        "auth004_boundary": AUTH004_BOUNDARY,
        "artifacts": artifacts,
    }
    return _with_self_hash(manifest, "manifest_sha256")


def write_gate_a_bundle(output: Path) -> None:
    files = build_gate_a_bundle()
    for name, value in files.items():
        path = output / name
        if name.endswith(".jsonl"):
            if not isinstance(value, list):
                raise Phase11DError(f"{name}: internal JSONL artifact is not a list")
            _write_jsonl(path, value)
        else:
            if not isinstance(value, Mapping):
                raise Phase11DError(f"{name}: internal JSON artifact is not an object")
            _write_json(path, value)


def write_gate_b_template(output: Path) -> None:
    _write_json(output, build_gate_b_template())


def _load_bundle(root: Path) -> dict[str, Any]:
    manifest_path = root / "canonical-manifest.json"
    manifest = load_json(manifest_path)
    _validate_manifest_shape(manifest)
    loaded: dict[str, Any] = {"canonical-manifest.json": manifest}
    manifest_entries = manifest["artifacts"]
    if not isinstance(manifest_entries, list):
        raise Phase11DError("canonical-manifest.json: artifacts must be an array")
    seen: set[str] = set()
    for raw in manifest_entries:
        if not isinstance(raw, Mapping):
            raise Phase11DError("canonical-manifest.json: artifact must be an object")
        _exact_fields("manifest artifact", raw, ARTIFACT_FIELDS)
        name = _require_str("artifact path", raw["path"])
        if "/" in name or "\\" in name or name.startswith("."):
            raise Phase11DError(f"artifact path is not a simple relative file name: {name}")
        if name not in REQUIRED_MANIFEST_ARTIFACTS:
            raise Phase11DError(f"unexpected manifest artifact: {name}")
        if name in seen:
            raise Phase11DError(f"duplicate manifest artifact: {name}")
        seen.add(name)
        expected_kind = _expected_artifact_kind(name)
        if raw["kind"] != expected_kind:
            raise Phase11DError(f"{name}: manifest artifact kind mismatch")
        path = root / name
        expected = _require_sha256(f"{name}.sha256", raw["sha256"])
        if not path.is_file():
            raise Phase11DError(f"manifest artifact is missing: {name}")
        value = load_jsonl(path) if name.endswith(".jsonl") else load_json(path)
        actual = sha256_bytes(_artifact_bytes(name, value))
        if actual != expected:
            raise Phase11DError(f"{name}: canonical SHA-256 mismatch")
        loaded[name] = value
    missing = sorted(REQUIRED_MANIFEST_ARTIFACTS - seen)
    if missing:
        raise Phase11DError("canonical-manifest.json: missing artifacts " + ", ".join(missing))
    return loaded


def validate_bundle(root: Path) -> ValidationSummary:
    loaded = _load_bundle(root)
    for name, value in loaded.items():
        _scan_no_raw_content(value, name)
    manifest = loaded["canonical-manifest.json"]
    authorization = loaded["authorization.json"]
    consents = loaded["consent-receipts.json"]
    repositories = loaded["repository-allowlist.json"]
    cohort = loaded["cohort.json"]
    selection = loaded["selection-receipt.json"]
    headline = loaded["headline-manifest.json"]
    reviews = _expect_rows(loaded["review-receipts.jsonl"], "review-receipts.jsonl")
    repairs = _expect_rows(loaded["repair-receipts.jsonl"], "repair-receipts.jsonl")
    drafts = _expect_rows(loaded["draft-pr-receipts.jsonl"], "draft-pr-receipts.jsonl")
    feedback = _expect_rows(loaded["feedback-receipts.jsonl"], "feedback-receipts.jsonl")
    time_cost = _expect_rows(
        loaded["time-cost-latency-receipts.jsonl"], "time-cost-latency-receipts.jsonl"
    )
    incidents = _expect_rows(loaded["incident-stop-receipts.jsonl"], "incident-stop-receipts.jsonl")
    business = loaded["business-report.json"]
    claim = loaded["claim-decision-report.json"]
    acceptance = loaded["final-acceptance-report.json"]

    validate_authorization(authorization)
    validate_consent_receipts(consents, authorization)
    validate_repository_allowlist(repositories)
    validate_cohort(cohort)
    validate_selection_receipt(selection, cohort)
    validate_headline_manifest(headline, cohort)
    validate_bundle_links(loaded, authorization, repositories, cohort, selection, headline)
    validate_reviews(reviews, cohort, headline)
    validate_repairs(repairs, reviews)
    validate_drafts(drafts, repairs)
    validate_feedback(feedback, reviews)
    validate_time_cost(time_cost, cohort, reviews)
    validate_incidents(incidents)
    validate_reports(business, claim, acceptance, cohort, reviews, repairs, drafts, feedback, time_cost, incidents)

    if manifest["phase11c_facts"] != EXPECTED_PHASE11C:
        raise Phase11DError("canonical manifest does not preserve Phase 11C facts")
    if manifest["auth004_boundary"] != AUTH004_BOUNDARY:
        raise Phase11DError("canonical manifest does not preserve auth-004 boundary")

    gate_b_allowed, blockers = evaluate_gate_b_template(build_gate_b_template())
    return ValidationSummary(
        selected_prs=int(cohort["selected_pr_count"]),
        completed_headlines=sum(1 for row in reviews if row["status"] == "completed"),
        feedback_eligible_findings=len(
            {finding for row in reviews for finding in row["feedback_eligible_finding_ids"]}
        ),
        repair_jobs=len(repairs),
        draft_pr_receipts=len(drafts),
        business_claim_allowed=bool(business["business_claim_allowed"]),
        gate_b_allowed=gate_b_allowed,
        gate_b_blockers=blockers,
    )


def _expect_rows(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise Phase11DError(f"{name}: expected JSONL row list")
    return value


def _validate_manifest_shape(manifest: Mapping[str, Any]) -> None:
    _exact_fields("canonical-manifest.json", manifest, MANIFEST_FIELDS)
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise Phase11DError("canonical-manifest.json: unsupported schema")
    if _self_hash(manifest, "manifest_sha256") != manifest["manifest_sha256"]:
        raise Phase11DError("canonical-manifest.json: manifest self-hash mismatch")
    _require_utc("manifest.generated_at_utc", manifest["generated_at_utc"])
    _require_git_sha("manifest.baseline_sha", manifest["baseline_sha"])
    _require_git_sha("manifest.local_master_at_start", manifest["local_master_at_start"])


def validate_authorization(authorization: Mapping[str, Any]) -> None:
    _exact_fields("authorization.json", authorization, AUTHORIZATION_FIELDS)
    if authorization["schema_version"] != AUTHORIZATION_SCHEMA_VERSION:
        raise Phase11DError("authorization.json: unsupported schema")
    if authorization["gate"] != "gate_a":
        raise Phase11DError("authorization.json: only Gate A artifacts are valid here")
    if _self_hash(authorization, "canonical_authorization_sha256") != authorization[
        "canonical_authorization_sha256"
    ]:
        raise Phase11DError("authorization.json: canonical authorization hash mismatch")
    _require_utc("authorization.created_at_utc", authorization["created_at_utc"])
    if authorization["phase11c_facts"] != EXPECTED_PHASE11C:
        raise Phase11DError("authorization.json: Phase 11C facts drifted")
    if authorization["auth004_boundary"] != AUTH004_BOUNDARY:
        raise Phase11DError("authorization.json: auth-004 boundary drifted")
    permissions = authorization["permissions"]
    if not isinstance(permissions, Mapping):
        raise Phase11DError("authorization.json: permissions must be an object")
    _exact_fields("authorization.permissions", permissions, PERMISSION_FIELDS)
    if any(_require_bool(f"permission.{name}", value) for name, value in permissions.items()):
        raise Phase11DError("authorization.json: Gate A cannot enable a real operation")
    if authorization["claim_boundary"] != CLAIM_BOUNDARY:
        raise Phase11DError("authorization.json: claim boundary drifted")
    if _require_bool("authorization.real_rows_present", authorization["real_rows_present"]):
        raise Phase11DError("authorization.json: Gate A example cannot contain real rows")
    if not _require_bool("authorization.synthetic_rows_present", authorization["synthetic_rows_present"]):
        raise Phase11DError("authorization.json: Gate A example must be synthetic")
    if _require_bool(
        "authorization.gate_b_required_fields_complete",
        authorization["gate_b_required_fields_complete"],
    ):
        raise Phase11DError("authorization.json: Gate B required fields are not complete")
    incident_policy = authorization["incident_policy"]
    if not isinstance(incident_policy, Mapping):
        raise Phase11DError("authorization.json: incident_policy must be an object")
    if incident_policy.get("kill_switch_active") is not False:
        raise Phase11DError("authorization.json: kill switch blocks all new work")


def validate_consent_receipts(
    consents: Mapping[str, Any], authorization: Mapping[str, Any]
) -> None:
    _exact_fields("consent-receipts.json", consents, CONSENT_RECEIPTS_FIELDS)
    if consents["schema_version"] != "crag.phase11d.consent-receipts/v1alpha1":
        raise Phase11DError("consent-receipts.json: unsupported schema")
    if consents["identity_map_committed"] is not False:
        raise Phase11DError("consent-receipts.json: identity map cannot be committed")
    if consents["synthetic_rows_present"] is not True or consents["real_rows_present"] is not False:
        raise Phase11DError("consent-receipts.json: Gate A consents must be synthetic-only")
    participants = consents["participants"]
    declared = _require_int(
        "authorization.participants_declared_count",
        authorization["participants_declared_count"],
        minimum=3,
    )
    if not isinstance(participants, list) or len(participants) != declared:
        raise Phase11DError("consent-receipts.json: participant count mismatch")
    if not 3 <= len(participants) <= 5:
        raise Phase11DError("consent-receipts.json: expected 3-5 participants")
    confirmed_real_count = 0
    seen: set[str] = set()
    for row in participants:
        if not isinstance(row, Mapping):
            raise Phase11DError("consent-receipts.json: participant row must be an object")
        _exact_fields("consent participant", row, PARTICIPANT_FIELDS)
        participant_id = _require_str("participant.participant_id", row["participant_id"])
        if participant_id in seen:
            raise Phase11DError("consent-receipts.json: duplicate participant")
        seen.add(participant_id)
        if row["row_kind"] != "synthetic":
            raise Phase11DError("consent-receipts.json: Gate A participant must be synthetic")
        if _require_bool("participant.confirmed_real", row["confirmed_real"]):
            confirmed_real_count += 1
        if row["role"] not in ROLES_ALLOWED_TO_APPROVE:
            raise Phase11DError("consent-receipts.json: participant role cannot approve")
        _require_sha256("participant.consent_receipt_sha256", row["consent_receipt_sha256"])
        _require_sha256("participant.consent_scope_sha256", row["consent_scope_sha256"])
        _require_int("participant.retention_days", row["retention_days"])
    expected_real = _require_int(
        "authorization.confirmed_real_participant_count",
        authorization["confirmed_real_participant_count"],
    )
    if confirmed_real_count != expected_real:
        raise Phase11DError("consent-receipts.json: confirmed-real participant count mismatch")
    if expected_real != 0:
        raise Phase11DError("consent-receipts.json: Gate A cannot enroll real participants")


def validate_repository_allowlist(repositories: Mapping[str, Any]) -> None:
    _exact_fields("repository-allowlist.json", repositories, REPOSITORY_ALLOWLIST_FIELDS)
    if repositories["schema_version"] != "crag.phase11d.repository-allowlist/v1alpha1":
        raise Phase11DError("repository-allowlist.json: unsupported schema")
    if repositories["raw_repository_locator_committed"] is not False:
        raise Phase11DError("repository-allowlist.json: raw repository locators are prohibited")
    rows = repositories["repositories"]
    if not isinstance(rows, list) or not rows:
        raise Phase11DError("repository-allowlist.json: expected repository rows")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise Phase11DError("repository-allowlist.json: repository row must be an object")
        _exact_fields("repository row", row, REPOSITORY_FIELDS)
        repository_id = _require_str("repository.repository_id", row["repository_id"])
        if repository_id in seen:
            raise Phase11DError("repository-allowlist.json: duplicate repository")
        seen.add(repository_id)
        if row["row_kind"] != "synthetic":
            raise Phase11DError("repository-allowlist.json: Gate A repositories must be synthetic")
        _require_sha256("repository.locator_sha256", row["locator_sha256"])
        _require_sha256(
            "repository.allowed_base_branch_rule_sha256",
            row["allowed_base_branch_rule_sha256"],
        )
        if _require_bool("repository.github_writes_allowed", row["github_writes_allowed"]):
            raise Phase11DError("repository-allowlist.json: GitHub writes are not authorized")


def validate_selection_receipt(
    selection: Mapping[str, Any], cohort: Mapping[str, Any]
) -> None:
    _exact_fields("selection-receipt.json", selection, SELECTION_RECEIPT_FIELDS)
    if selection["schema_version"] != "crag.phase11d.selection-receipt/v1alpha1":
        raise Phase11DError("selection-receipt.json: unsupported schema")
    if selection["cohort_id"] != cohort["cohort_id"]:
        raise Phase11DError("selection-receipt.json: cohort mismatch")
    for selection_field, cohort_field in (
        ("eligible_count", "eligible_count"),
        ("selected_count", "selected_pr_count"),
        ("excluded_count", "excluded_count"),
    ):
        if _require_int(f"selection.{selection_field}", selection[selection_field]) != _require_int(
            f"cohort.{cohort_field}", cohort[cohort_field]
        ):
            raise Phase11DError("selection-receipt.json: denominator count mismatch")
    _require_sha256("selection.selection_seed_sha256", selection["selection_seed_sha256"])
    if selection["selection_seed_sha256"] != cohort["selection_seed_sha256"]:
        raise Phase11DError("selection-receipt.json: seed mismatch")
    selected_ids = [row["pr_id"] for row in cohort["selected_prs"]]
    if selection["selected_pr_ids_sha256"] != sha256_bytes(canonical_json(selected_ids)):
        raise Phase11DError("selection-receipt.json: selected PR digest mismatch")
    if selection["selection_before_agent_output"] is not True:
        raise Phase11DError("selection-receipt.json: selection must precede agent output")
    if selection["replacement_after_failure_allowed"] is not False:
        raise Phase11DError("selection-receipt.json: replacement after failure is prohibited")


def validate_bundle_links(
    loaded: Mapping[str, Any],
    authorization: Mapping[str, Any],
    repositories: Mapping[str, Any],
    cohort: Mapping[str, Any],
    selection: Mapping[str, Any],
    headline: Mapping[str, Any],
) -> None:
    expected_authorization_hashes = {
        "repository_allowlist_sha256": _artifact_sha256("repository-allowlist.json", repositories),
        "cohort_sha256": _artifact_sha256("cohort.json", cohort),
        "selection_sha256": _artifact_sha256("selection-receipt.json", selection),
        "headline_manifest_sha256": _artifact_sha256("headline-manifest.json", headline),
    }
    for field, expected in expected_authorization_hashes.items():
        if authorization[field] != expected:
            raise Phase11DError(f"authorization.json: {field} does not match artifact")
    if cohort["authorization_id"] != authorization["authorization_id"]:
        raise Phase11DError("cohort.json: authorization mismatch")
    if headline["authorization_id"] != authorization["authorization_id"]:
        raise Phase11DError("headline-manifest.json: authorization mismatch")
    if authorization["selected_pr_count"] != cohort["selected_pr_count"]:
        raise Phase11DError("authorization.json: selected PR count mismatch")
    repository_ids = {row["repository_id"] for row in repositories["repositories"]}
    selected_repository_ids = {row["repository_id"] for row in cohort["selected_prs"]}
    if not selected_repository_ids <= repository_ids:
        raise Phase11DError("cohort.json: selected PR outside repository allowlist")
    manifest = loaded["canonical-manifest.json"]
    if manifest["baseline_sha"] != BASELINE_SHA:
        raise Phase11DError("canonical-manifest.json: baseline SHA drifted")
    if manifest["local_master_at_start"] != LOCAL_MASTER_AT_START:
        raise Phase11DError("canonical-manifest.json: local master start SHA drifted")


def validate_cohort(cohort: Mapping[str, Any]) -> None:
    _exact_fields("cohort.json", cohort, COHORT_FIELDS)
    if cohort["schema_version"] != COHORT_SCHEMA_VERSION:
        raise Phase11DError("cohort.json: unsupported schema")
    if cohort["synthetic_rows_present"] is not True or cohort["real_rows_present"] is not False:
        raise Phase11DError("cohort.json: Gate A cohort must be synthetic-only")
    _require_sha256("cohort.selection_seed_sha256", cohort["selection_seed_sha256"])
    start = _require_utc("cohort.selection_window_start_utc", cohort["selection_window_start_utc"])
    end = _require_utc("cohort.selection_window_end_utc", cohort["selection_window_end_utc"])
    if start >= end:
        raise Phase11DError("cohort.json: selection window must be positive")
    eligible_count = _require_int("cohort.eligible_count", cohort["eligible_count"], minimum=1)
    excluded_count = _require_int("cohort.excluded_count", cohort["excluded_count"])
    selected_count = _require_int("cohort.selected_pr_count", cohort["selected_pr_count"], minimum=1)
    if not 20 <= selected_count <= 30:
        raise Phase11DError("cohort.json: selected PR count must be 20-30")
    if eligible_count != selected_count + excluded_count:
        raise Phase11DError("cohort.json: eligible denominator count mismatch")
    selected = cohort["selected_prs"]
    if not isinstance(selected, list) or len(selected) != selected_count:
        raise Phase11DError("cohort.json: selected_pr_count mismatch")
    ids: set[str] = set()
    for row in selected:
        if not isinstance(row, Mapping):
            raise Phase11DError("cohort.json: selected row must be an object")
        _exact_fields("cohort.selected_pr", row, SELECTED_PR_FIELDS)
        pr_id = _require_str("selected.pr_id", row["pr_id"])
        if pr_id in ids:
            raise Phase11DError(f"cohort.json: duplicate selected PR {pr_id}")
        ids.add(pr_id)
        if row["row_kind"] not in {"synthetic", "real"}:
            raise Phase11DError("cohort.json: row_kind must be synthetic or real")
        for field in ("snapshot_sha256", "diff_sha256", "selection_rank_sha256"):
            _require_sha256(f"selected.{field}", row[field])
    if cohort["synthetic_rows_present"] is True and cohort["real_rows_present"] is True:
        raise Phase11DError("cohort.json: synthetic and real rows must be separated")
    excluded = cohort["excluded"]
    if not isinstance(excluded, list) or len(excluded) != excluded_count:
        raise Phase11DError("cohort.json: excluded count mismatch")
    seen_excluded: set[str] = set()
    for row in excluded:
        if not isinstance(row, Mapping):
            raise Phase11DError("cohort.json: excluded row must be an object")
        _exact_fields("cohort.excluded", row, EXCLUDED_PR_FIELDS)
        candidate_id = _require_str("excluded.candidate_id", row.get("candidate_id"))
        if candidate_id in seen_excluded:
            raise Phase11DError("cohort.json: duplicate excluded candidate")
        seen_excluded.add(candidate_id)
        if row.get("row_kind") != "synthetic":
            raise Phase11DError("cohort.json: Gate A excluded row must be synthetic")
        _require_str("excluded.exclusion_reason", row.get("exclusion_reason"))


def validate_headline_manifest(headline: Mapping[str, Any], cohort: Mapping[str, Any]) -> None:
    _exact_fields("headline-manifest.json", headline, HEADLINE_FIELDS)
    if headline["schema_version"] != "crag.phase11d.headline-manifest/v1alpha1":
        raise Phase11DError("headline-manifest.json: unsupported schema")
    if headline["cohort_id"] != cohort["cohort_id"]:
        raise Phase11DError("headline-manifest.json: cohort mismatch")
    if headline["diagnostic_may_replace_headline"] is not False:
        raise Phase11DError("headline-manifest.json: diagnostic cannot replace headline")
    selected = {row["pr_id"]: row for row in cohort["selected_prs"]}
    rows = headline["headlines"]
    if not isinstance(rows, list) or len(rows) != cohort["selected_pr_count"]:
        raise Phase11DError("headline-manifest.json: headline count mismatch")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise Phase11DError("headline-manifest.json: headline row must be an object")
        _exact_fields("headline row", row, HEADLINE_ROW_FIELDS)
        pr_id = _require_str("headline.pr_id", row["pr_id"])
        if pr_id not in selected:
            raise Phase11DError("headline-manifest.json: foreign PR")
        if pr_id in seen:
            raise Phase11DError("headline-manifest.json: duplicate headline PR")
        seen.add(pr_id)
        if _require_int("headline.attempt_number", row["attempt_number"]) != 1:
            raise Phase11DError("headline-manifest.json: headline attempt must be 1")
        if row["headline_id"] != selected[pr_id]["headline_id"]:
            raise Phase11DError("headline-manifest.json: headline id mismatch")


def validate_reviews(
    reviews: Sequence[Mapping[str, Any]],
    cohort: Mapping[str, Any],
    headline: Mapping[str, Any],
) -> None:
    selected = {row["pr_id"]: row for row in cohort["selected_prs"]}
    headline_by_pr = {row["pr_id"]: row for row in headline["headlines"]}
    if len(reviews) != len(selected):
        raise Phase11DError("review-receipts.jsonl: every selected PR needs one receipt")
    seen: set[str] = set()
    for row in reviews:
        _exact_fields("review receipt", row, REVIEW_FIELDS)
        if row["schema_version"] != REVIEW_RECEIPT_SCHEMA_VERSION:
            raise Phase11DError("review receipt: unsupported schema")
        pr_id = _require_str("review.pr_id", row["pr_id"])
        if pr_id not in selected:
            raise Phase11DError("review receipt: foreign PR")
        if row["row_kind"] != "synthetic":
            raise Phase11DError("review receipt: Gate A review row must be synthetic")
        if pr_id in seen:
            raise Phase11DError("review receipt: duplicate PR receipt")
        seen.add(pr_id)
        expected_headline = headline_by_pr[pr_id]
        if row["headline_id"] != expected_headline["headline_id"]:
            raise Phase11DError("review receipt: headline mismatch")
        if row["receipt_id"] != expected_headline["review_receipt_id"]:
            raise Phase11DError("review receipt: receipt id mismatch")
        if row["attempt_number"] != 1:
            raise Phase11DError("review receipt: headline attempt must be 1")
        if row["status"] not in REVIEW_STATUSES:
            raise Phase11DError("review receipt: invalid status")
        if row["terminal_category"] not in TERMINAL_FAILURES:
            raise Phase11DError("review receipt: invalid terminal category")
        if row["terminal_category"] == "provider_text_only_response" and row["status"] == "completed":
            raise Phase11DError("review receipt: text-only provider response must fail closed")
        if row["status"] == "completed" and row["terminal_category"] != "completed":
            raise Phase11DError("review receipt: completed status needs completed terminal category")
        for field in ("provider_call_count", "http_attempt_count", "input_tokens", "output_tokens", "cached_tokens", "cost_micro_cny", "latency_ms"):
            _require_int(f"review.{field}", row[field])
        if row["http_attempt_count"] < row["provider_call_count"]:
            raise Phase11DError("review receipt: HTTP attempts cannot be below logical calls")
        _require_sha256("review.trace_sha256", row["trace_sha256"])
        if _require_bool("review.redaction_applied", row["redaction_applied"]) is not True:
            raise Phase11DError("review receipt: redaction must be applied")


def _validate_actor(role: Any, method: Any, context: str) -> None:
    role_text = _require_str(f"{context}.actor_role", role)
    method_text = _require_str(f"{context}.actor_method", method)
    if method_text in ACTOR_METHODS_DENIED:
        raise Phase11DError(f"{context}: actor method cannot approve")
    if role_text not in ROLES_ALLOWED_TO_APPROVE:
        raise Phase11DError(f"{context}: role cannot approve")


def _validate_approval(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Phase11DError(f"{context}: approval must be an object")
    _exact_fields(context, value, APPROVAL_FIELDS)
    decision = _require_str(f"{context}.decision", value["decision"])
    if decision not in {"approved", "declined", "not_requested"}:
        raise Phase11DError(f"{context}: invalid decision")
    if decision == "approved":
        _validate_actor(value["actor_role"], value["actor_method"], context)
        if value["consumed"] is not True:
            raise Phase11DError(f"{context}: approved approval must be consumed")
    else:
        _require_bool(f"{context}.consumed", value["consumed"])
    _require_sha256(f"{context}.binding_sha256", value["binding_sha256"])
    return value


def validate_repairs(repairs: Sequence[Mapping[str, Any]], reviews: Sequence[Mapping[str, Any]]) -> None:
    known_findings = {finding for row in reviews for finding in row["feedback_eligible_finding_ids"]}
    seen_approvals: set[str] = set()
    seen_jobs: set[str] = set()
    for row in repairs:
        _exact_fields("repair receipt", row, REPAIR_FIELDS)
        if row["schema_version"] != REPAIR_RECEIPT_SCHEMA_VERSION:
            raise Phase11DError("repair receipt: unsupported schema")
        job_id = _require_str("repair.repair_job_id", row["repair_job_id"])
        if job_id in seen_jobs:
            raise Phase11DError("repair receipt: duplicate repair job")
        seen_jobs.add(job_id)
        if row["row_kind"] != "synthetic":
            raise Phase11DError("repair receipt: Gate A repair row must be synthetic")
        if row["finding_id"] not in known_findings:
            raise Phase11DError("repair receipt: repair finding is not feedback-eligible")
        _validate_actor(row["request_actor_role"], row["request_actor_method"], "repair.start")
        _require_git_sha("repair.base_sha", row["base_sha"])
        _require_git_sha("repair.head_sha", row["head_sha"])
        for field in (
            "worktree_receipt_sha256",
            "task_branch_sha256",
            "plan_sha256",
            "patch_sha256",
            "checkpoint_sha256",
            "test_sha256",
            "budget_sha256",
        ):
            _require_sha256(f"repair.{field}", row[field])
        write = _validate_approval(row["write_approval"], "repair.write_approval")
        draft = _validate_approval(row["draft_pr_approval"], "repair.draft_pr_approval")
        if write["binding_sha256"] != _approval_binding("write", row):
            raise Phase11DError("repair receipt: WRITE approval binding is stale")
        if draft["binding_sha256"] != _approval_binding("draft_pr", row):
            raise Phase11DError("repair receipt: DRAFT_PR approval binding is stale")
        for approval in (write, draft):
            approval_id = str(approval["approval_id"])
            if approval_id in seen_approvals:
                raise Phase11DError("repair receipt: approval replay or race detected")
            seen_approvals.add(approval_id)
        sandbox = row["sandbox"]
        if not isinstance(sandbox, Mapping):
            raise Phase11DError("repair receipt: sandbox must be an object")
        _exact_fields("repair.sandbox", sandbox, SANDBOX_FIELDS)
        if sandbox["network_mode"] != "none" or sandbox["docker"] is not True or sandbox["non_root"] is not True:
            raise Phase11DError("repair receipt: sandbox policy is not offline/non-root")
        _require_int("repair.sandbox.timeout_seconds", sandbox["timeout_seconds"], minimum=1)
        _require_int("repair.sandbox.output_limit_bytes", sandbox["output_limit_bytes"], minimum=1)
        _require_bool("repair.sandbox.tests_passed", sandbox["tests_passed"])
        final_status = _require_str("repair.final_status", row["final_status"])
        publisher_status = _require_str("repair.publisher_status", row["publisher_status"])
        failure_category = _require_str("repair.failure_category", row["failure_category"])
        if final_status not in REPAIR_FINAL_STATUSES:
            raise Phase11DError("repair receipt: invalid final status")
        if publisher_status not in PUBLISHER_STATUSES:
            raise Phase11DError("repair receipt: invalid publisher status")
        if failure_category not in REPAIR_FAILURE_CATEGORIES:
            raise Phase11DError("repair receipt: invalid failure category")
        if write["decision"] != "approved" and row["commit_sha"]:
            raise Phase11DError("repair receipt: declined WRITE cannot create commit")
        if sandbox["tests_passed"] is not True and row["commit_sha"]:
            raise Phase11DError("repair receipt: failed tests cannot create commit")
        if draft["decision"] != "approved" and row["publisher_status"] == "draft_published":
            raise Phase11DError("repair receipt: unapproved Draft PR cannot publish")
        if final_status == "budget_exhausted" or failure_category == "budget_exhausted":
            if row["commit_sha"] or publisher_status != "not_published":
                raise Phase11DError("repair receipt: budget exhaustion cannot create external writes")
        if final_status == "draft_pr_created":
            _require_git_sha("repair.commit_sha", row["commit_sha"])
            if row["publisher_status"] != "draft_published":
                raise Phase11DError("repair receipt: final draft status needs publisher receipt")
            if failure_category != "none":
                raise Phase11DError("repair receipt: created Draft PR cannot carry failure")
        if publisher_status in {"publisher_failed", "publisher_ambiguous_result"} and final_status != "quarantined":
            raise Phase11DError("repair receipt: publisher uncertainty must quarantine")
        _require_int("repair.cost_micro_cny", row["cost_micro_cny"])


def validate_drafts(drafts: Sequence[Mapping[str, Any]], repairs: Sequence[Mapping[str, Any]]) -> None:
    repair_jobs = {row["repair_job_id"]: row for row in repairs}
    seen_jobs: set[str] = set()
    for row in drafts:
        _exact_fields("draft-pr receipt", row, DRAFT_PR_FIELDS)
        if row["schema_version"] != DRAFT_PR_RECEIPT_SCHEMA_VERSION:
            raise Phase11DError("draft-pr receipt: unsupported schema")
        job_id = _require_str("draft.repair_job_id", row["repair_job_id"])
        if job_id not in repair_jobs:
            raise Phase11DError("draft-pr receipt: foreign repair job")
        if job_id in seen_jobs:
            raise Phase11DError("draft-pr receipt: duplicate repair job receipt")
        seen_jobs.add(job_id)
        repair = repair_jobs[job_id]
        if repair["publisher_status"] != "draft_published":
            raise Phase11DError("draft-pr receipt: publisher did not report success")
        if row["row_kind"] != "synthetic":
            raise Phase11DError("draft-pr receipt: Gate A draft row must be synthetic")
        if row["draft"] is not True or row["ready"] is not False or row["merged"] is not False:
            raise Phase11DError("draft-pr receipt: Pilot Draft PR must stay Draft and unmerged")
        if _require_int("draft.comments_checks_labels_reviews", row["comments_checks_labels_reviews"]) != 0:
            raise Phase11DError("draft-pr receipt: comments/checks/labels/reviews are not authorized")
        _require_sha256("draft.head_branch_sha256", row["head_branch_sha256"])
        _require_sha256("draft.receipt_sha256", row["receipt_sha256"])
        _require_git_sha("draft.commit_sha", row["commit_sha"])
        if row["commit_sha"] != repair["commit_sha"]:
            raise Phase11DError("draft-pr receipt: approved commit mismatch")
        if _require_bool("draft.redaction_applied", row["redaction_applied"]) is not True:
            raise Phase11DError("draft-pr receipt: redaction must be applied")
    published_jobs = {
        row["repair_job_id"] for row in repairs if row["publisher_status"] == "draft_published"
    }
    if seen_jobs != published_jobs:
        raise Phase11DError("draft-pr receipt: missing receipt after publisher success")


def validate_feedback(feedback: Sequence[Mapping[str, Any]], reviews: Sequence[Mapping[str, Any]]) -> None:
    known_findings = {finding for row in reviews for finding in row["feedback_eligible_finding_ids"]}
    seen: set[str] = set()
    for row in feedback:
        _exact_fields("feedback receipt", row, FEEDBACK_FIELDS)
        if row["schema_version"] != FEEDBACK_SCHEMA_VERSION:
            raise Phase11DError("feedback receipt: unsupported schema")
        finding_id = _require_str("feedback.finding_id", row["finding_id"])
        if finding_id not in known_findings:
            raise Phase11DError("feedback receipt: foreign finding")
        if row["row_kind"] != "synthetic":
            raise Phase11DError("feedback receipt: Gate A feedback row must be synthetic")
        if finding_id in seen:
            raise Phase11DError("feedback receipt: duplicate finding feedback")
        seen.add(finding_id)
        if row["decision"] not in FEEDBACK_DECISIONS:
            raise Phase11DError("feedback receipt: invalid decision")
        _require_bool("feedback.repair_requested", row["repair_requested"])
        _require_bool("feedback.draft_pr_adopted", row["draft_pr_adopted"])
        _require_sha256("feedback.rationale_sha256", row["rationale_sha256"])
        _require_utc("feedback.submitted_at_utc", row["submitted_at_utc"])
        if row["human_attested"] is not True:
            raise Phase11DError("feedback receipt: human attestation required")


def validate_time_cost(
    rows: Sequence[Mapping[str, Any]],
    cohort: Mapping[str, Any],
    reviews: Sequence[Mapping[str, Any]],
) -> None:
    del reviews
    selected = {row["pr_id"] for row in cohort["selected_prs"]}
    if len(rows) != len(selected):
        raise Phase11DError("time-cost receipts: every selected PR needs one row")
    seen: set[str] = set()
    for row in rows:
        _exact_fields("time-cost receipt", row, TIME_COST_FIELDS)
        pr_id = _require_str("time-cost.pr_id", row["pr_id"])
        if pr_id not in selected or pr_id in seen:
            raise Phase11DError("time-cost receipt: foreign or duplicate PR")
        seen.add(pr_id)
        if row["row_kind"] != "synthetic":
            raise Phase11DError("time-cost receipt: Gate A time/cost row must be synthetic")
        for field in ("active_review_seconds", "paused_review_seconds", "end_to_end_latency_ms", "cost_micro_cny"):
            _require_int(f"time-cost.{field}", row[field])
        _require_utc("time-cost.recorded_at_utc", row["recorded_at_utc"])
        if row["human_attested"] is not True:
            raise Phase11DError("time-cost receipt: human attestation required")


def validate_incidents(rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        raise Phase11DError("incident-stop receipts: at least one receipt is required")
    for row in rows:
        _exact_fields("incident-stop receipt", row, INCIDENT_FIELDS)
        if row["schema_version"] != INCIDENT_SCHEMA_VERSION:
            raise Phase11DError("incident-stop receipt: unsupported schema")
        if row["row_kind"] != "synthetic":
            raise Phase11DError("incident-stop receipt: Gate A incident row must be synthetic")
        for field in (
            "kill_switch_activated",
            "new_tasks_stopped",
            "unresolved",
            "credential_revoked_or_isolated",
            "redaction_applied",
        ):
            _require_bool(f"incident.{field}", row[field])
        for field in (
            "quarantine_count",
            "unauthorized_operation_count",
            "duplicate_external_write_count",
        ):
            _require_int(f"incident.{field}", row[field])
        _require_utc("incident.recorded_at_utc", row["recorded_at_utc"])
        if row["redaction_applied"] is not True:
            raise Phase11DError("incident-stop receipt: redaction must be applied")
        if row["kill_switch_activated"] is True or row["new_tasks_stopped"] is True:
            raise Phase11DError("incident-stop receipt: kill switch blocks new work")
        if row["credential_revoked_or_isolated"] is not True:
            raise Phase11DError("incident-stop receipt: credential must be revoked or isolated")
        if row["unresolved"] is True:
            raise Phase11DError("incident-stop receipt: unresolved incident blocks acceptance")
        if row["unauthorized_operation_count"] or row["duplicate_external_write_count"]:
            raise Phase11DError("incident-stop receipt: unauthorized or duplicate write observed")


def validate_reports(
    business: Mapping[str, Any],
    claim: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    cohort: Mapping[str, Any],
    reviews: Sequence[Mapping[str, Any]],
    repairs: Sequence[Mapping[str, Any]],
    drafts: Sequence[Mapping[str, Any]],
    feedback: Sequence[Mapping[str, Any]],
    time_cost: Sequence[Mapping[str, Any]],
    incidents: Sequence[Mapping[str, Any]],
) -> None:
    expected = _business_report(cohort, reviews, repairs, drafts, feedback, time_cost, incidents)
    _exact_fields("business-report.json", business, REPORT_FIELDS)
    if business != expected:
        raise Phase11DError("business-report.json: report does not recompute from receipts")
    _exact_fields("claim-decision-report.json", claim, CLAIM_DECISION_FIELDS)
    if claim != _claim_decision():
        raise Phase11DError("claim-decision-report.json: claim decision drifted")
    _exact_fields("final-acceptance-report.json", acceptance, ACCEPTANCE_FIELDS)
    if acceptance["gate_b_execution_allowed"] is not False:
        raise Phase11DError("final-acceptance-report.json: Gate B must remain closed")
    if acceptance["production_ready"] is not False:
        raise Phase11DError("final-acceptance-report.json: production claim is prohibited")
    if acceptance["model_quality_status"] != "not_measured":
        raise Phase11DError("final-acceptance-report.json: model quality must be not_measured")
    if acceptance["formal_quality_status"] != "incomplete":
        raise Phase11DError("final-acceptance-report.json: formal quality must be incomplete")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 11D Gate A offline tooling")
    subcommands = parser.add_subparsers(dest="command", required=True)
    generate = subcommands.add_parser("generate-gate-a")
    generate.add_argument("--output", required=True, type=Path)
    validate = subcommands.add_parser("validate-bundle")
    validate.add_argument("--bundle", required=True, type=Path)
    template = subcommands.add_parser("generate-gate-b-template")
    template.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate-gate-a":
            write_gate_a_bundle(args.output)
            print(json.dumps({"generated": True, "output": str(args.output)}, sort_keys=True))
            return 0
        if args.command == "validate-bundle":
            summary = validate_bundle(args.bundle)
            print(json.dumps(summary.to_dict(), sort_keys=True))
            return 0
        if args.command == "generate-gate-b-template":
            write_gate_b_template(args.output)
            allowed, blockers = evaluate_gate_b_template(build_gate_b_template())
            print(
                json.dumps(
                    {
                        "generated": True,
                        "gate_b_allowed": allowed,
                        "blocker_count": len(blockers),
                    },
                    sort_keys=True,
                )
            )
            return 0
    except Phase11DError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
