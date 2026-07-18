"""Offline metrics and integrity checks for the trusted Review evaluation.

This module deliberately uses only the Python standard library. It does not
call Git, GitHub, an LLM provider, or any existing ``eval/`` asset. The CLI
accepts a frozen cohort manifest, independent/adjudicated annotation JSONL, and
run JSONL, then emits a hash-bound deterministic metrics report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 1
METRIC_VERSION = "trusted-review-v1"
DEFAULT_BOOTSTRAP_REPLICATES = 10_000
DEFAULT_BOOTSTRAP_SEED = 20260718

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PR_ID_RE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,199}$")

COHORT_KEYS = {
    "schema_version",
    "cohort_id",
    "cohort_seed",
    "selection_window",
    "repositories",
    "prs",
    "gold_frozen_at",
    "selection_log_sha256",
}
SELECTION_WINDOW_KEYS = {"start", "end"}
REPOSITORY_KEYS = {"slug", "role", "target_prs"}
PR_KEYS = {
    "pr_id",
    "repository",
    "number",
    "role",
    "base_sha",
    "head_sha",
    "merge_sha",
    "diff_sha256",
    "snapshot_sha256",
    "merged_at",
    "selected_at",
    "changed_lines",
    "change_type",
    "human_review_comments_present",
    "author_is_benchmark_implementer",
    "previously_used",
    "gold_review_complete",
    "gold_annotation_set_sha256",
}
ANNOTATION_KEYS = {
    "schema_version",
    "annotation_id",
    "subject_kind",
    "subject_id",
    "pr_id",
    "annotator_id",
    "role",
    "label",
    "gold_id",
    "discovered",
    "severity",
    "rationale",
    "evidence_sha256",
    "source_annotation_ids",
    "source_annotation_sha256s",
    "created_at",
}
RUN_KEYS = {
    "schema_version",
    "run_id",
    "pr_id",
    "config_id",
    "purpose",
    "source_commit",
    "provider",
    "model_id",
    "pricing_revision",
    "runtime_config_sha256",
    "snapshot_sha256",
    "started_at",
    "completed_at",
    "status",
    "scorable",
    "cost_microusd",
    "latency_seconds",
    "tool_calls",
    "tool_calls_by_component",
    "test_status",
    "unauthorized_operation_count",
    "findings",
}
FINDING_KEYS = {"finding_id", "fingerprint_sha256", "path", "line"}

GOLD_LABELS = {"valid_defect", "not_defect", "uncertain"}
GOLD_FINAL_LABELS = GOLD_LABELS - {"uncertain"}
FINDING_LABELS = {
    "matched",
    "novel_valid",
    "invalid",
    "duplicate",
    "unscorable",
    "uncertain",
}
FINDING_FINAL_LABELS = FINDING_LABELS - {"uncertain"}
RUN_STATUSES = {"ok", "degraded", "fail_open", "failed"}
RUN_PURPOSES = {"final_report", "audit", "annotation"}
FORBIDDEN_PURPOSES = {"tuning", "prompt_selection", "sentinel_design", "threshold_search"}
TEST_STATUSES = {"not_applicable", "not_run", "passed", "failed"}
SEVERITIES = {"low", "medium", "high"}


class ValidationError(ValueError):
    """Raised when an evaluation artifact violates the frozen protocol."""


def _fail(message: str) -> None:
    raise ValidationError(message)


def _expect_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{where} must be an object")
    return value


def _expect_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{where} must be an array")
    return value


def _expect_str(value: Any, where: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        _fail(f"{where} must be a{' non-empty' if not allow_empty else ''} string")
    return value


def _expect_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{where} must be a boolean")
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
        _fail(f"{where} must be one of {sorted(allowed)}, got {result!r}")
    return result


def _expect_identifier(value: Any, where: str) -> str:
    result = _expect_str(value, where)
    if not IDENTIFIER_RE.fullmatch(result):
        _fail(f"{where} is not a valid stable identifier: {result!r}")
    return result


def _expect_sha(value: Any, where: str, *, length: int) -> str:
    result = _expect_str(value, where)
    pattern = HEX40_RE if length == 40 else HEX64_RE
    if not pattern.fullmatch(result):
        _fail(f"{where} must be a lowercase {length}-character hexadecimal digest")
    return result


def _expect_nullable_sha(value: Any, where: str) -> str | None:
    if value is None:
        return None
    return _expect_sha(value, where, length=64)


def _expect_exact_keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        pieces = []
        if missing:
            pieces.append(f"missing keys {missing}")
        if unknown:
            pieces.append(f"unknown keys {unknown}")
        _fail(f"{where}: {'; '.join(pieces)}")


def parse_timestamp(value: Any, where: str) -> datetime:
    text = _expect_str(value, where)
    if not text.endswith("Z"):
        _fail(f"{where} must use UTC with a trailing Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ValidationError(f"{where} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail(f"{where} must be UTC")
    return parsed


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return SHA-256 of canonical JSON, useful for fixtures and audit bindings."""
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _reject_forbidden_path(path: Path) -> Path:
    resolved = path.resolve()
    forbidden = {"eval", "holdout"}
    if any(part.casefold() in forbidden for part in resolved.parts):
        _fail(f"existing evaluation assets are forbidden inputs: {resolved}")
    return resolved


def load_json(path: str | Path) -> tuple[Any, str]:
    resolved = _reject_forbidden_path(Path(path))
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read {resolved}: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{resolved} is not valid UTF-8 JSON: {exc}") from exc
    return value, hashlib.sha256(raw).hexdigest()


def load_jsonl(path: str | Path) -> tuple[list[Any], str]:
    resolved = _reject_forbidden_path(Path(path))
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read {resolved}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{resolved} is not valid UTF-8") from exc
    rows = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"{resolved}:{line_number} is not valid JSON: {exc.msg}"
            ) from exc
    return rows, hashlib.sha256(raw).hexdigest()


def _repository_from_pr_id(pr_id: str, where: str) -> tuple[str, int]:
    match = PR_ID_RE.fullmatch(pr_id)
    if match is None:
        _fail(f"{where} must use canonical owner/repo#number form")
    return match.group("repo"), int(match.group("number"))


def validate_cohort(raw: Any, *, require_materialized: bool) -> dict[str, Any]:
    """Validate a cohort plan or a fully materialized cohort manifest."""
    cohort = _expect_dict(raw, "cohort")
    _expect_exact_keys(cohort, COHORT_KEYS, "cohort")
    if _expect_int(cohort["schema_version"], "cohort.schema_version") != SCHEMA_VERSION:
        _fail(f"unsupported cohort schema_version {cohort['schema_version']!r}")
    _expect_identifier(cohort["cohort_id"], "cohort.cohort_id")
    _expect_sha(cohort["cohort_seed"], "cohort.cohort_seed", length=64)

    window = _expect_dict(cohort["selection_window"], "cohort.selection_window")
    _expect_exact_keys(window, SELECTION_WINDOW_KEYS, "cohort.selection_window")
    start = parse_timestamp(window["start"], "cohort.selection_window.start")
    end = parse_timestamp(window["end"], "cohort.selection_window.end")
    if start >= end:
        _fail("cohort.selection_window.start must precede end")

    repositories = _expect_list(cohort["repositories"], "cohort.repositories")
    if not repositories:
        _fail("cohort.repositories must not be empty")
    repo_by_slug: dict[str, dict[str, Any]] = {}
    targets = Counter()
    for index, item in enumerate(repositories):
        where = f"cohort.repositories[{index}]"
        repo = _expect_dict(item, where)
        _expect_exact_keys(repo, REPOSITORY_KEYS, where)
        slug = _expect_str(repo["slug"], f"{where}.slug")
        if not REPOSITORY_RE.fullmatch(slug):
            _fail(f"{where}.slug must use owner/repo form")
        if slug in repo_by_slug:
            _fail(f"duplicate repository {slug!r}")
        role = _expect_enum(repo["role"], {"calibration", "reporting"}, f"{where}.role")
        target = _expect_int(repo["target_prs"], f"{where}.target_prs", minimum=1)
        repo_by_slug[slug] = repo
        targets[role] += target

    reporting_repos = sorted(
        slug for slug, repo in repo_by_slug.items() if repo["role"] == "reporting"
    )
    calibration_repos = sorted(
        slug for slug, repo in repo_by_slug.items() if repo["role"] == "calibration"
    )
    if len(reporting_repos) < 3 or targets["reporting"] < 30:
        _fail("cohort must plan at least 30 reporting PRs from at least 3 repositories")
    if not calibration_repos:
        _fail("cohort must include a repository-disjoint calibration repository")
    if set(reporting_repos) & set(calibration_repos):
        _fail("calibration and reporting repositories must be disjoint")

    prs = _expect_list(cohort["prs"], "cohort.prs")
    seen_pr_ids: set[str] = set()
    seen_snapshots: set[str] = set()
    selected_counts = Counter()
    validated_prs = []
    reporting_size_bands: dict[str, set[str]] = defaultdict(set)
    reporting_change_types: set[str] = set()
    reporting_review_comment_values: set[bool] = set()
    for index, item in enumerate(prs):
        where = f"cohort.prs[{index}]"
        pr = _expect_dict(item, where)
        _expect_exact_keys(pr, PR_KEYS, where)
        pr_id = _expect_identifier(pr["pr_id"], f"{where}.pr_id")
        repository = _expect_str(pr["repository"], f"{where}.repository")
        parsed_repo, parsed_number = _repository_from_pr_id(pr_id, f"{where}.pr_id")
        number = _expect_int(pr["number"], f"{where}.number", minimum=1)
        if repository != parsed_repo or number != parsed_number:
            _fail(f"{where} repository/number do not match pr_id")
        if repository not in repo_by_slug:
            _fail(f"{where}.repository is not preregistered: {repository!r}")
        role = _expect_enum(pr["role"], {"calibration", "reporting"}, f"{where}.role")
        if role != repo_by_slug[repository]["role"]:
            _fail(f"{where}.role does not match repository role")
        if pr_id in seen_pr_ids:
            _fail(f"duplicate PR {pr_id!r}")
        seen_pr_ids.add(pr_id)
        snapshot = _expect_sha(pr["snapshot_sha256"], f"{where}.snapshot_sha256", length=64)
        if snapshot in seen_snapshots:
            _fail(f"duplicate snapshot_sha256 {snapshot!r}")
        seen_snapshots.add(snapshot)
        _expect_sha(pr["base_sha"], f"{where}.base_sha", length=40)
        _expect_sha(pr["head_sha"], f"{where}.head_sha", length=40)
        _expect_sha(pr["merge_sha"], f"{where}.merge_sha", length=40)
        _expect_sha(pr["diff_sha256"], f"{where}.diff_sha256", length=64)
        merged_at = parse_timestamp(pr["merged_at"], f"{where}.merged_at")
        if not start <= merged_at < end:
            _fail(f"{where}.merged_at is outside the preregistered selection window")
        selected_at = parse_timestamp(pr["selected_at"], f"{where}.selected_at")
        if selected_at < start:
            _fail(f"{where}.selected_at precedes the selection window")
        changed_lines = _expect_int(pr["changed_lines"], f"{where}.changed_lines", minimum=1)
        change_type = _expect_enum(
            pr["change_type"],
            {"bug_fix", "non_bug_fix"},
            f"{where}.change_type",
        )
        has_review_comments = _expect_bool(
            pr["human_review_comments_present"],
            f"{where}.human_review_comments_present",
        )
        if _expect_bool(
            pr["author_is_benchmark_implementer"],
            f"{where}.author_is_benchmark_implementer",
        ):
            _fail(f"{where} was authored by the benchmark implementer")
        if _expect_bool(pr["previously_used"], f"{where}.previously_used"):
            _fail(f"{where} was previously used and would contaminate the cohort")
        _expect_bool(pr["gold_review_complete"], f"{where}.gold_review_complete")
        _expect_sha(
            pr["gold_annotation_set_sha256"],
            f"{where}.gold_annotation_set_sha256",
            length=64,
        )
        selected_counts[repository] += 1
        if role == "reporting":
            size_band = "small" if changed_lines < 100 else "medium" if changed_lines < 500 else "large"
            reporting_size_bands[repository].add(size_band)
            reporting_change_types.add(change_type)
            reporting_review_comment_values.add(has_review_comments)
        validated_prs.append(pr)

    gold_frozen_at = cohort["gold_frozen_at"]
    selection_hash = _expect_nullable_sha(
        cohort["selection_log_sha256"], "cohort.selection_log_sha256"
    )
    if gold_frozen_at is not None:
        parse_timestamp(gold_frozen_at, "cohort.gold_frozen_at")

    if require_materialized:
        if not prs:
            _fail("materialized cohort must contain PR records")
        if gold_frozen_at is None:
            _fail("materialized cohort requires gold_frozen_at")
        if selection_hash is None:
            _fail("materialized cohort requires selection_log_sha256")
        frozen_timestamp = parse_timestamp(gold_frozen_at, "cohort.gold_frozen_at")
        for slug, repo in repo_by_slug.items():
            if selected_counts[slug] != repo["target_prs"]:
                _fail(
                    f"repository {slug!r} has {selected_counts[slug]} PRs; "
                    f"expected exactly {repo['target_prs']}"
                )
        for index, pr in enumerate(validated_prs):
            if not pr["gold_review_complete"]:
                _fail(f"cohort.prs[{index}] is missing completed independent gold review")
            if parse_timestamp(pr["selected_at"], f"cohort.prs[{index}].selected_at") > frozen_timestamp:
                _fail(f"cohort.prs[{index}] was selected after gold_frozen_at")
        for repository in reporting_repos:
            if len(reporting_size_bands[repository]) < 2:
                _fail(
                    f"reporting repository {repository!r} must contain at least "
                    "two changed-line size bands"
                )
        if reporting_change_types != {"bug_fix", "non_bug_fix"}:
            _fail("reporting cohort must contain bug-fix and non-bug-fix PRs")
        if reporting_review_comment_values != {False, True}:
            _fail("reporting cohort must contain PRs with and without human review comments")

    return {
        **cohort,
        "_repo_by_slug": repo_by_slug,
        "_reporting_repositories": reporting_repos,
        "_calibration_repositories": calibration_repos,
        "_pr_by_id": {pr["pr_id"]: pr for pr in validated_prs},
    }


def _annotation_signature(record: dict[str, Any]) -> str:
    if record["label"] in {"matched", "duplicate"}:
        return f"{record['label']}:{record['gold_id']}"
    return record["label"]


def _validate_annotation_row(raw: Any, index: int) -> dict[str, Any]:
    where = f"annotations[{index}]"
    row = _expect_dict(raw, where)
    _expect_exact_keys(row, ANNOTATION_KEYS, where)
    if _expect_int(row["schema_version"], f"{where}.schema_version") != SCHEMA_VERSION:
        _fail(f"{where} has unsupported schema_version")
    _expect_identifier(row["annotation_id"], f"{where}.annotation_id")
    kind = _expect_enum(
        row["subject_kind"], {"gold_candidate", "system_finding"}, f"{where}.subject_kind"
    )
    _expect_identifier(row["subject_id"], f"{where}.subject_id")
    _repository_from_pr_id(_expect_str(row["pr_id"], f"{where}.pr_id"), f"{where}.pr_id")
    _expect_identifier(row["annotator_id"], f"{where}.annotator_id")
    role = _expect_enum(row["role"], {"annotator", "adjudicator"}, f"{where}.role")
    allowed = GOLD_LABELS if kind == "gold_candidate" else FINDING_LABELS
    label = _expect_enum(row["label"], allowed, f"{where}.label")
    gold_id = row["gold_id"]
    if label in {"matched", "duplicate"}:
        _expect_identifier(gold_id, f"{where}.gold_id")
    elif gold_id is not None:
        _fail(f"{where}.gold_id must be null for label {label!r}")
    discovered = row["discovered"]
    if kind == "gold_candidate" and role == "annotator":
        _expect_bool(discovered, f"{where}.discovered")
    elif discovered is not None:
        _fail(f"{where}.discovered must be null outside independent gold annotation")
    severity = row["severity"]
    if kind == "gold_candidate":
        _expect_enum(severity, SEVERITIES, f"{where}.severity")
    elif severity is not None:
        _fail(f"{where}.severity must be null for system findings")
    rationale = _expect_str(row["rationale"], f"{where}.rationale")
    if not rationale.strip():
        _fail(f"{where}.rationale must contain non-whitespace text")
    _expect_sha(row["evidence_sha256"], f"{where}.evidence_sha256", length=64)
    source_ids = _expect_list(row["source_annotation_ids"], f"{where}.source_annotation_ids")
    source_hashes = _expect_list(
        row["source_annotation_sha256s"],
        f"{where}.source_annotation_sha256s",
    )
    for source_index, source_id in enumerate(source_ids):
        _expect_identifier(source_id, f"{where}.source_annotation_ids[{source_index}]")
    for source_index, source_hash in enumerate(source_hashes):
        _expect_sha(
            source_hash,
            f"{where}.source_annotation_sha256s[{source_index}]",
            length=64,
        )
    if role == "annotator" and (source_ids or source_hashes):
        _fail(f"{where} independent annotation must not cite source annotations")
    if role == "adjudicator" and (len(source_ids) != 2 or len(source_hashes) != 2):
        _fail(f"{where} adjudication must cite exactly two source annotations and hashes")
    parse_timestamp(row["created_at"], f"{where}.created_at")
    return row


def resolve_annotations(
    raw_rows: Sequence[Any],
    cohort: dict[str, Any],
    runs: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate independent labels and return immutable final subject labels."""
    rows = [_validate_annotation_row(raw, index) for index, raw in enumerate(raw_rows)]
    annotation_ids = [row["annotation_id"] for row in rows]
    if len(annotation_ids) != len(set(annotation_ids)):
        _fail("annotation_id values must be unique")

    reporting_prs = {
        pr_id
        for pr_id, pr in cohort["_pr_by_id"].items()
        if pr["role"] == "reporting"
    }
    frozen_at = parse_timestamp(cohort["gold_frozen_at"], "cohort.gold_frozen_at")

    run_by_finding: dict[str, dict[str, Any]] = {}
    if runs is not None:
        for run in runs:
            for finding in run["findings"]:
                finding_id = finding["finding_id"]
                if finding_id in run_by_finding:
                    _fail(f"finding_id {finding_id!r} occurs in more than one run")
                run_by_finding[finding_id] = run

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    subject_identity: dict[str, tuple[str, str]] = {}
    for row in rows:
        if row["pr_id"] not in reporting_prs:
            _fail(f"annotation references non-reporting or unknown PR {row['pr_id']!r}")
        identity = (row["subject_kind"], row["pr_id"])
        previous = subject_identity.setdefault(row["subject_id"], identity)
        if previous != identity:
            _fail(f"subject_id {row['subject_id']!r} has inconsistent kind or PR")
        created_at = parse_timestamp(row["created_at"], "annotation.created_at")
        if row["subject_kind"] == "gold_candidate":
            selected_at = parse_timestamp(
                cohort["_pr_by_id"][row["pr_id"]]["selected_at"],
                "cohort PR selected_at",
            )
            if created_at < selected_at:
                _fail(f"gold annotation {row['annotation_id']!r} predates PR selection")
            if created_at > frozen_at:
                _fail(f"gold annotation {row['annotation_id']!r} was created after gold freeze")
        elif runs is not None:
            run = run_by_finding.get(row["subject_id"])
            if run is None:
                _fail(f"system annotation references unknown finding {row['subject_id']!r}")
            if run["pr_id"] != row["pr_id"]:
                _fail(f"finding annotation {row['annotation_id']!r} has wrong PR")
            if created_at < parse_timestamp(run["completed_at"], "run.completed_at"):
                _fail(
                    f"system annotation {row['annotation_id']!r} predates run completion"
                )
        groups[row["subject_id"]].append(row)

    global_annotators: set[str] | None = None
    global_adjudicators: set[str] = set()
    finals: dict[str, dict[str, Any]] = {}
    independent_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for subject_id in sorted(groups):
        subject_rows = groups[subject_id]
        independent = sorted(
            (row for row in subject_rows if row["role"] == "annotator"),
            key=lambda row: row["annotator_id"],
        )
        adjudicators = [row for row in subject_rows if row["role"] == "adjudicator"]
        if len(independent) != 2:
            _fail(f"subject {subject_id!r} requires exactly two independent labels")
        annotator_ids = {row["annotator_id"] for row in independent}
        if len(annotator_ids) != 2:
            _fail(f"subject {subject_id!r} requires two distinct annotators")
        if global_annotators is None:
            global_annotators = annotator_ids
        elif global_annotators != annotator_ids:
            _fail("the same two independent annotators must cover every subject")
        if len(adjudicators) > 1:
            _fail(f"subject {subject_id!r} has more than one adjudicator")
        if adjudicators and adjudicators[0]["annotator_id"] in annotator_ids:
            _fail(f"subject {subject_id!r} adjudicator must be a third person")
        if adjudicators:
            global_adjudicators.add(adjudicators[0]["annotator_id"])
            if len(global_adjudicators) > 1:
                _fail("the same third-party adjudicator must cover every conflict")

        signatures = [_annotation_signature(row) for row in independent]
        requires_adjudication = signatures[0] != signatures[1] or any(
            row["label"] == "uncertain" for row in independent
        )
        if requires_adjudication and not adjudicators:
            _fail(f"subject {subject_id!r} requires third-party adjudication")
        if not requires_adjudication and adjudicators:
            _fail(f"subject {subject_id!r} has unnecessary adjudication")

        if adjudicators:
            final = adjudicators[0]
            if parse_timestamp(final["created_at"], "adjudication.created_at") < max(
                parse_timestamp(row["created_at"], "annotation.created_at")
                for row in independent
            ):
                _fail(f"subject {subject_id!r} adjudication predates an independent label")
            expected_source_ids = [row["annotation_id"] for row in independent]
            expected_source_hashes = [canonical_sha256(row) for row in independent]
            if final["source_annotation_ids"] != expected_source_ids:
                _fail(
                    f"subject {subject_id!r} adjudication source_annotation_ids "
                    "do not match the independent labels"
                )
            if final["source_annotation_sha256s"] != expected_source_hashes:
                _fail(
                    f"subject {subject_id!r} adjudication source hashes "
                    "do not match the independent labels"
                )
        else:
            final = independent[0]
        final_allowed = (
            GOLD_FINAL_LABELS
            if final["subject_kind"] == "gold_candidate"
            else FINDING_FINAL_LABELS
        )
        if final["label"] not in final_allowed:
            _fail(f"subject {subject_id!r} has unresolved final label {final['label']!r}")
        finals[subject_id] = {
            "subject_id": subject_id,
            "subject_kind": final["subject_kind"],
            "pr_id": final["pr_id"],
            "label": final["label"],
            "gold_id": final["gold_id"],
            "adjudicated": bool(adjudicators),
        }
        independent_pairs.append((independent[0], independent[1]))

    if rows and global_annotators is None:
        _fail("annotations have no independent annotators")
    for pr_id in sorted(reporting_prs):
        gold_rows = sorted(
            (
                row
                for row in rows
                if row["pr_id"] == pr_id and row["subject_kind"] == "gold_candidate"
            ),
            key=lambda row: row["annotation_id"],
        )
        actual_hash = canonical_sha256(gold_rows)
        expected_hash = cohort["_pr_by_id"][pr_id]["gold_annotation_set_sha256"]
        if actual_hash != expected_hash:
            _fail(
                f"gold annotation set hash mismatch for {pr_id!r}: "
                f"expected {expected_hash}, got {actual_hash}"
            )
    return {
        "rows": rows,
        "finals": finals,
        "pairs": independent_pairs,
        "annotators": sorted(global_annotators or []),
        "adjudicators": sorted(global_adjudicators),
    }


def _cohen_kappa(signatures: Sequence[tuple[str, str]]) -> tuple[float | None, str | None]:
    if not signatures:
        return None, "no paired labels"
    total = len(signatures)
    observed = sum(left == right for left, right in signatures) / total
    left_counts = Counter(left for left, _ in signatures)
    right_counts = Counter(right for _, right in signatures)
    categories = set(left_counts) | set(right_counts)
    expected = sum(
        (left_counts[category] / total) * (right_counts[category] / total)
        for category in categories
    )
    if math.isclose(expected, 1.0):
        return None, "expected agreement is one"
    return round((observed - expected) / (1.0 - expected), 6), None


def _agreement_block(
    pairs: Sequence[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    signatures = [(_annotation_signature(left), _annotation_signature(right)) for left, right in pairs]
    total = len(signatures)
    agreements = sum(left == right for left, right in signatures)
    kappa, reason = _cohen_kappa(signatures)
    arbitrated = sum(
        left != right or pair[0]["label"] == "uncertain" or pair[1]["label"] == "uncertain"
        for pair, (left, right) in zip(pairs, signatures)
    )

    gold_pairs = [pair for pair in pairs if pair[0]["subject_kind"] == "gold_candidate"]
    discovered_left = {pair[0]["subject_id"] for pair in gold_pairs if pair[0]["discovered"]}
    discovered_right = {pair[1]["subject_id"] for pair in gold_pairs if pair[1]["discovered"]}
    intersection = len(discovered_left & discovered_right)
    union = len(discovered_left | discovered_right)
    discovery_jaccard = round(intersection / union, 6) if union else None
    discovery_f1 = (
        round(2 * intersection / (len(discovered_left) + len(discovered_right)), 6)
        if discovered_left or discovered_right
        else None
    )
    severity_agreements = sum(pair[0]["severity"] == pair[1]["severity"] for pair in gold_pairs)
    contingency: dict[str, Counter] = defaultdict(Counter)
    for left, right in signatures:
        contingency[left][right] += 1
    return {
        "subjects": total,
        "exact_agreements": agreements,
        "exact_agreement_rate": round(agreements / total, 6) if total else None,
        "cohen_kappa": kappa,
        "cohen_kappa_reason": reason,
        "contingency": {
            left: dict(sorted(right_counts.items()))
            for left, right_counts in sorted(contingency.items())
        },
        "arbitrated_subjects": arbitrated,
        "arbitration_rate": round(arbitrated / total, 6) if total else None,
        "unresolved_subjects": 0,
        "malformed_subjects": 0,
        "discovery": {
            "annotator_a": len(discovered_left),
            "annotator_b": len(discovered_right),
            "intersection": intersection,
            "union": union,
            "jaccard": discovery_jaccard,
            "f1": discovery_f1,
        },
        "severity": {
            "subjects": len(gold_pairs),
            "exact_agreements": severity_agreements,
            "exact_agreement_rate": (
                round(severity_agreements / len(gold_pairs), 6) if gold_pairs else None
            ),
        },
    }


def annotation_agreement(resolved: dict[str, Any]) -> dict[str, Any]:
    pairs = resolved["pairs"]
    by_kind = {
        kind: [pair for pair in pairs if pair[0]["subject_kind"] == kind]
        for kind in ("gold_candidate", "system_finding")
    }
    repositories = sorted({_repository_from_pr_id(pair[0]["pr_id"], "pr_id")[0] for pair in pairs})
    return {
        "annotators": resolved["annotators"],
        "adjudicators": resolved["adjudicators"],
        "overall": _agreement_block(pairs),
        "by_subject_kind": {kind: _agreement_block(kind_pairs) for kind, kind_pairs in by_kind.items()},
        "by_repository": {
            repository: _agreement_block(
                [
                    pair
                    for pair in pairs
                    if _repository_from_pr_id(pair[0]["pr_id"], "pr_id")[0] == repository
                ]
            )
            for repository in repositories
        },
    }


def _validate_run_row(raw: Any, index: int) -> dict[str, Any]:
    where = f"runs[{index}]"
    row = _expect_dict(raw, where)
    _expect_exact_keys(row, RUN_KEYS, where)
    if _expect_int(row["schema_version"], f"{where}.schema_version") != SCHEMA_VERSION:
        _fail(f"{where} has unsupported schema_version")
    _expect_identifier(row["run_id"], f"{where}.run_id")
    _repository_from_pr_id(_expect_str(row["pr_id"], f"{where}.pr_id"), f"{where}.pr_id")
    _expect_identifier(row["config_id"], f"{where}.config_id")
    purpose = _expect_str(row["purpose"], f"{where}.purpose")
    if purpose in FORBIDDEN_PURPOSES:
        _fail(f"{where}.purpose {purpose!r} is forbidden for reporting data")
    if purpose not in RUN_PURPOSES:
        _fail(f"{where}.purpose must be one of {sorted(RUN_PURPOSES)}")
    _expect_sha(row["source_commit"], f"{where}.source_commit", length=40)
    _expect_identifier(row["provider"], f"{where}.provider")
    _expect_identifier(row["model_id"], f"{where}.model_id")
    _expect_identifier(row["pricing_revision"], f"{where}.pricing_revision")
    _expect_sha(
        row["runtime_config_sha256"],
        f"{where}.runtime_config_sha256",
        length=64,
    )
    _expect_sha(row["snapshot_sha256"], f"{where}.snapshot_sha256", length=64)
    started = parse_timestamp(row["started_at"], f"{where}.started_at")
    completed = parse_timestamp(row["completed_at"], f"{where}.completed_at")
    if completed < started:
        _fail(f"{where}.completed_at precedes started_at")
    status = _expect_enum(row["status"], RUN_STATUSES, f"{where}.status")
    scorable = _expect_bool(row["scorable"], f"{where}.scorable")
    if (status == "failed") == scorable:
        _fail(f"{where}.scorable must be false only for failed runs")
    _expect_int(row["cost_microusd"], f"{where}.cost_microusd")
    latency = _expect_finite_number(row["latency_seconds"], f"{where}.latency_seconds")
    elapsed = (completed - started).total_seconds()
    if latency > elapsed + 1.0:
        _fail(f"{where}.latency_seconds exceeds timestamp duration by more than one second")
    tool_calls = _expect_int(row["tool_calls"], f"{where}.tool_calls")
    components = _expect_dict(row["tool_calls_by_component"], f"{where}.tool_calls_by_component")
    component_total = 0
    for component, count in components.items():
        _expect_identifier(component, f"{where}.tool_calls_by_component key")
        component_total += _expect_int(count, f"{where}.tool_calls_by_component.{component}")
    if components and component_total != tool_calls:
        _fail(f"{where}.tool_calls must equal the component total")
    _expect_enum(row["test_status"], TEST_STATUSES, f"{where}.test_status")
    _expect_int(
        row["unauthorized_operation_count"],
        f"{where}.unauthorized_operation_count",
    )
    findings = _expect_list(row["findings"], f"{where}.findings")
    if not scorable and findings:
        _fail(f"{where}.findings must be empty for a non-scorable run")
    seen_findings: set[str] = set()
    for finding_index, item in enumerate(findings):
        finding_where = f"{where}.findings[{finding_index}]"
        finding = _expect_dict(item, finding_where)
        _expect_exact_keys(finding, FINDING_KEYS, finding_where)
        finding_id = _expect_identifier(finding["finding_id"], f"{finding_where}.finding_id")
        if finding_id in seen_findings:
            _fail(f"{where} has duplicate finding_id {finding_id!r}")
        seen_findings.add(finding_id)
        _expect_sha(
            finding["fingerprint_sha256"],
            f"{finding_where}.fingerprint_sha256",
            length=64,
        )
        path = _expect_str(finding["path"], f"{finding_where}.path")
        if Path(path).is_absolute() or ".." in Path(path).parts:
            _fail(f"{finding_where}.path must be a repository-relative path without '..'")
        _expect_int(finding["line"], f"{finding_where}.line", minimum=1)
    return row


def validate_runs(
    raw_rows: Sequence[Any],
    cohort: dict[str, Any],
    *,
    config_id: str,
) -> list[dict[str, Any]]:
    rows = [_validate_run_row(raw, index) for index, raw in enumerate(raw_rows)]
    run_ids = [row["run_id"] for row in rows]
    if len(run_ids) != len(set(run_ids)):
        _fail("run_id values must be unique")

    reporting_prs = {
        pr_id
        for pr_id, pr in cohort["_pr_by_id"].items()
        if pr["role"] == "reporting"
    }
    frozen_at = parse_timestamp(cohort["gold_frozen_at"], "cohort.gold_frozen_at")
    selected = [row for row in rows if row["config_id"] == config_id]
    by_pr: dict[str, dict[str, Any]] = {}
    all_finding_ids: set[str] = set()
    frozen_signatures: set[tuple[str, ...]] = set()
    for row in selected:
        pr_id = row["pr_id"]
        if pr_id not in reporting_prs:
            _fail(f"run {row['run_id']!r} references non-reporting or unknown PR {pr_id!r}")
        if pr_id in by_pr:
            _fail(f"config {config_id!r} has more than one run for {pr_id!r}")
        expected_snapshot = cohort["_pr_by_id"][pr_id]["snapshot_sha256"]
        if row["snapshot_sha256"] != expected_snapshot:
            _fail(f"run {row['run_id']!r} snapshot_sha256 does not match its frozen PR")
        if parse_timestamp(row["started_at"], "run.started_at") < frozen_at:
            _fail(f"run {row['run_id']!r} started before gold_frozen_at")
        if row["purpose"] != "final_report":
            _fail(
                f"run {row['run_id']!r} purpose must be 'final_report' "
                "for a headline reporting command"
            )
        frozen_signatures.add(
            (
                row["source_commit"],
                row["provider"],
                row["model_id"],
                row["pricing_revision"],
                row["runtime_config_sha256"],
            )
        )
        by_pr[pr_id] = row
        for finding in row["findings"]:
            finding_id = finding["finding_id"]
            if finding_id in all_finding_ids:
                _fail(f"finding_id {finding_id!r} is not globally unique")
            all_finding_ids.add(finding_id)
    missing = sorted(reporting_prs - set(by_pr))
    extra = sorted(set(by_pr) - reporting_prs)
    if missing or extra:
        _fail(f"config {config_id!r} run coverage mismatch: missing={missing}, extra={extra}")
    if len(frozen_signatures) != 1:
        _fail(
            f"config {config_id!r} mixes source/model/pricing/runtime identities"
        )
    return [by_pr[pr_id] for pr_id in sorted(by_pr)]


def _safe_div(numerator: int | float, denominator: int | float) -> float | None:
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision == 0.0 or recall == 0.0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def _rounded(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def _metric_from_counts(counts: dict[str, int]) -> dict[str, Any]:
    precision = _safe_div(counts["tp_findings"], counts["tp_findings"] + counts["fp_findings"])
    recall = _safe_div(counts["tp_gold"], counts["tp_gold"] + counts["fn_gold"])
    return {
        **counts,
        "precision": _rounded(precision),
        "recall": _rounded(recall),
        "f1": _rounded(_f1(precision, recall)),
    }


COUNT_KEYS = (
    "tp_findings",
    "fp_findings",
    "tp_gold",
    "fn_gold",
    "novel_valid",
    "duplicates",
    "unscorable",
)


def _sum_counts(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    total = {key: 0 for key in COUNT_KEYS}
    for row in rows:
        for key in COUNT_KEYS:
            total[key] += row[key]
    return total


def _mean_defined(values: Iterable[float | None]) -> float | None:
    defined = [value for value in values if value is not None]
    return statistics.fmean(defined) if defined else None


def score_review_runs(
    runs: Sequence[dict[str, Any]],
    cohort: dict[str, Any],
    resolved: dict[str, Any],
) -> list[dict[str, Any]]:
    finals = resolved["finals"]
    gold_by_pr: dict[str, set[str]] = defaultdict(set)
    for subject_id, final in finals.items():
        if final["subject_kind"] == "gold_candidate" and final["label"] == "valid_defect":
            gold_by_pr[final["pr_id"]].add(subject_id)

    finding_finals = {
        subject_id: final
        for subject_id, final in finals.items()
        if final["subject_kind"] == "system_finding"
    }
    used_finding_annotations: set[str] = set()
    scored = []
    for run in sorted(runs, key=lambda row: row["pr_id"]):
        pr_id = run["pr_id"]
        repository = cohort["_pr_by_id"][pr_id]["repository"]
        gold = gold_by_pr.get(pr_id, set())
        matched_gold: set[str] = set()
        counts = {key: 0 for key in COUNT_KEYS}
        finding_labels = Counter()
        duplicate_targets: set[str] = set()
        for finding in run["findings"]:
            finding_id = finding["finding_id"]
            final = finding_finals.get(finding_id)
            if final is None:
                _fail(f"finding {finding_id!r} lacks final independent/adjudicated labels")
            used_finding_annotations.add(finding_id)
            if final["pr_id"] != pr_id:
                _fail(f"finding {finding_id!r} annotation points to a different PR")
            label = final["label"]
            finding_labels[label] += 1
            if label == "matched":
                gold_id = final["gold_id"]
                if gold_id not in gold:
                    _fail(f"finding {finding_id!r} matches unknown or invalid gold {gold_id!r}")
                if gold_id in matched_gold:
                    _fail(
                        f"gold {gold_id!r} is matched more than once; later findings must be duplicate"
                    )
                matched_gold.add(gold_id)
                counts["tp_findings"] += 1
            elif label == "novel_valid":
                counts["tp_findings"] += 1
                counts["novel_valid"] += 1
            elif label == "duplicate":
                gold_id = final["gold_id"]
                if gold_id not in gold:
                    _fail(f"duplicate finding {finding_id!r} references invalid gold {gold_id!r}")
                duplicate_targets.add(gold_id)
                counts["fp_findings"] += 1
                counts["duplicates"] += 1
            elif label == "unscorable":
                counts["fp_findings"] += 1
                counts["unscorable"] += 1
            elif label == "invalid":
                counts["fp_findings"] += 1
            else:
                _fail(f"finding {finding_id!r} has unsupported final label {label!r}")
        unmatched_duplicate_targets = sorted(duplicate_targets - matched_gold)
        if unmatched_duplicate_targets:
            _fail(
                f"run {run['run_id']!r} labels findings as duplicate without a matched "
                f"primary finding for gold IDs {unmatched_duplicate_targets}"
            )
        counts["tp_gold"] = len(matched_gold)
        counts["fn_gold"] = len(gold - matched_gold)
        scored.append(
            {
                "pr_id": pr_id,
                "repository": repository,
                "run_id": run["run_id"],
                "status": run["status"],
                "scorable": run["scorable"],
                "gold_total": len(gold),
                "finding_total": len(run["findings"]),
                "finding_labels": dict(sorted(finding_labels.items())),
                **_metric_from_counts(counts),
            }
        )

    unused = sorted(set(finding_finals) - used_finding_annotations)
    if unused:
        _fail(f"system-finding annotations do not belong to the selected runs: {unused}")
    return scored


def _percentile(values: Sequence[float | int], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _distribution(values: Sequence[float | int], *, include_total: bool) -> dict[str, Any]:
    if not values:
        return {
            **({"total": 0} if include_total else {}),
            "count": 0,
            "mean": None,
            "p50": None,
            "p95": None,
            "max": None,
        }
    result = {
        "count": len(values),
        "mean": _rounded(statistics.fmean(values)),
        "p50": _rounded(_percentile(values, 0.50)),
        "p95": _rounded(_percentile(values, 0.95)),
        "max": max(values),
    }
    if include_total:
        result = {"total": sum(values), **result}
    return result


def telemetry_report(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    attempted = len(runs)
    statuses = Counter(run["status"] for run in runs)
    scorable = sum(run["scorable"] for run in runs)
    costs = [run["cost_microusd"] for run in runs]
    latencies = [run["latency_seconds"] for run in runs]
    tool_calls = [run["tool_calls"] for run in runs]
    components: dict[str, list[int]] = defaultdict(list)
    all_component_names = sorted(
        {name for run in runs for name in run["tool_calls_by_component"]}
    )
    for name in all_component_names:
        for run in runs:
            components[name].append(run["tool_calls_by_component"].get(name, 0))

    test_eligible = [run for run in runs if run["test_status"] in {"passed", "failed"}]
    test_failures = sum(run["test_status"] == "failed" for run in test_eligible)
    unauthorized_events = sum(run["unauthorized_operation_count"] for run in runs)
    unauthorized_runs = sum(run["unauthorized_operation_count"] > 0 for run in runs)
    return {
        "attempted_runs": attempted,
        "scorable_runs": scorable,
        "scorable_run_rate": _rounded(_safe_div(scorable, attempted)),
        "status_counts": {status: statuses.get(status, 0) for status in sorted(RUN_STATUSES)},
        "fail_open_rate": _rounded(_safe_div(statuses["fail_open"], attempted)),
        "degraded_rate": _rounded(_safe_div(statuses["degraded"], attempted)),
        "hard_failure_rate": _rounded(_safe_div(statuses["failed"], attempted)),
        "cost_microusd": {
            **_distribution(costs, include_total=True),
            "per_scorable_pr": _rounded(_safe_div(sum(costs), scorable)),
        },
        "latency_seconds": _distribution(latencies, include_total=False),
        "tool_calls": {
            **_distribution(tool_calls, include_total=True),
            "by_component": {
                name: _distribution(values, include_total=True)
                for name, values in sorted(components.items())
            },
        },
        "test_failures": {
            "eligible_runs": len(test_eligible),
            "failed_runs": test_failures,
            "rate": _rounded(_safe_div(test_failures, len(test_eligible))),
        },
        "unauthorized_operations": {
            "events": unauthorized_events,
            "affected_runs": unauthorized_runs,
            "run_rate": _rounded(_safe_div(unauthorized_runs, attempted)),
        },
    }


def review_metrics(scored_prs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    overall = _metric_from_counts(_sum_counts(scored_prs))
    by_repository_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scored_prs:
        by_repository_rows[row["repository"]].append(row)
    by_repository = {
        repository: _metric_from_counts(_sum_counts(rows))
        for repository, rows in sorted(by_repository_rows.items())
    }
    repository_macro = {
        metric: _rounded(_mean_defined(value[metric] for value in by_repository.values()))
        for metric in ("precision", "recall", "f1")
    }
    pr_macro = {
        metric: _rounded(_mean_defined(row[metric] for row in scored_prs))
        for metric in ("precision", "recall", "f1")
    }
    return {
        "micro": overall,
        "repository_macro": repository_macro,
        "pr_macro": pr_macro,
        "by_repository": by_repository,
        "per_pr": sorted(scored_prs, key=lambda row: row["pr_id"]),
    }


def _bootstrap_interval(
    values: Sequence[float],
    *,
    alpha: float,
    replicates: int,
) -> dict[str, Any]:
    if not values:
        return {
            "low": None,
            "high": None,
            "defined_replicates": 0,
            "reason": "metric undefined in every bootstrap replicate",
        }
    ordered = sorted(values)
    low_index = min(len(ordered) - 1, math.floor((len(ordered) - 1) * alpha / 2.0))
    high_index = min(
        len(ordered) - 1,
        math.ceil((len(ordered) - 1) * (1.0 - alpha / 2.0)),
    )
    return {
        "low": _rounded(ordered[low_index]),
        "high": _rounded(ordered[high_index]),
        "defined_replicates": len(ordered),
        "reason": None if len(ordered) == replicates else "undefined replicates omitted",
    }


def stratified_pr_bootstrap(
    scored_prs: Sequence[dict[str, Any]],
    *,
    replicates: int = DEFAULT_BOOTSTRAP_REPLICATES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Percentile 95% CI by resampling PRs within each repository."""
    if isinstance(replicates, bool) or not isinstance(replicates, int) or replicates < 1:
        _fail("bootstrap replicates must be an integer >= 1")
    if isinstance(seed, bool) or not isinstance(seed, int):
        _fail("bootstrap seed must be an integer")
    if not 0.0 < alpha < 1.0:
        _fail("bootstrap alpha must be between zero and one")
    by_repository: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(scored_prs, key=lambda item: item["pr_id"]):
        by_repository[row["repository"]].append(row)
    if not by_repository or sum(map(len, by_repository.values())) < 2:
        return {
            "method": "percentile_pr_within_repository",
            "seed": seed,
            "replicates": replicates,
            "alpha": alpha,
            "precision": {"low": None, "high": None, "defined_replicates": 0, "reason": "fewer than two PRs"},
            "recall": {"low": None, "high": None, "defined_replicates": 0, "reason": "fewer than two PRs"},
            "f1": {"low": None, "high": None, "defined_replicates": 0, "reason": "fewer than two PRs"},
        }
    rng = random.Random(seed)
    samples: dict[str, list[float]] = {metric: [] for metric in ("precision", "recall", "f1")}
    repositories = sorted(by_repository)
    for _ in range(replicates):
        sampled_rows = []
        for repository in repositories:
            rows = by_repository[repository]
            sampled_rows.extend(rng.choices(rows, k=len(rows)))
        metrics = _metric_from_counts(_sum_counts(sampled_rows))
        for metric in samples:
            value = metrics[metric]
            if value is not None:
                samples[metric].append(value)
    return {
        "method": "percentile_pr_within_repository",
        "seed": seed,
        "replicates": replicates,
        "alpha": alpha,
        **{
            metric: _bootstrap_interval(values, alpha=alpha, replicates=replicates)
            for metric, values in samples.items()
        },
    }


def build_report(
    cohort: dict[str, Any],
    resolved: dict[str, Any],
    runs: Sequence[dict[str, Any]],
    *,
    config_id: str,
    replicates: int = DEFAULT_BOOTSTRAP_REPLICATES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    input_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    scored = score_review_runs(runs, cohort, resolved)
    metrics = review_metrics(scored)
    return {
        "schema_version": SCHEMA_VERSION,
        "metric_version": METRIC_VERSION,
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "cohort_id": cohort["cohort_id"],
        "config_id": config_id,
        "split": "reporting",
        "source_commits": sorted({run["source_commit"] for run in runs}),
        "provider": runs[0]["provider"] if runs else None,
        "model_id": runs[0]["model_id"] if runs else None,
        "pricing_revision": runs[0]["pricing_revision"] if runs else None,
        "runtime_config_sha256": runs[0]["runtime_config_sha256"] if runs else None,
        "input_hashes": dict(sorted((input_hashes or {}).items())),
        "agreement": annotation_agreement(resolved),
        "review": metrics,
        "bootstrap_95_ci": stratified_pr_bootstrap(
            scored,
            replicates=replicates,
            seed=seed,
        ),
        "telemetry": telemetry_report(runs),
    }


def _public_cohort_summary(cohort: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": cohort["schema_version"],
        "cohort_id": cohort["cohort_id"],
        "reporting_repositories": cohort["_reporting_repositories"],
        "calibration_repositories": cohort["_calibration_repositories"],
        "planned_reporting_prs": sum(
            repo["target_prs"]
            for repo in cohort["repositories"]
            if repo["role"] == "reporting"
        ),
        "planned_calibration_prs": sum(
            repo["target_prs"]
            for repo in cohort["repositories"]
            if repo["role"] == "calibration"
        ),
        "materialized_prs": len(cohort["prs"]),
        "gold_frozen": cohort["gold_frozen_at"] is not None,
    }


def _write_json(value: Any, output: str | None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    if output is None:
        print(text)
        return
    path = _reject_forbidden_path(Path(output))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"cannot write {path}: {exc}") from exc
    print(f"report -> {path}", file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and report the sealed trusted Review evaluation"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate-cohort",
        help="validate a preregistered plan or materialized cohort",
    )
    validate.add_argument("--cohort", required=True)
    validate.add_argument("--materialized", action="store_true")

    report = subparsers.add_parser(
        "report",
        help="validate frozen inputs and calculate the reporting split",
    )
    report.add_argument("--cohort", required=True)
    report.add_argument("--annotations", required=True)
    report.add_argument("--runs", required=True)
    report.add_argument("--config-id", required=True)
    report.add_argument("--bootstrap", type=int, default=DEFAULT_BOOTSTRAP_REPLICATES)
    report.add_argument("--seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    report.add_argument("--output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        cohort_raw, cohort_hash = load_json(args.cohort)
        cohort = validate_cohort(
            cohort_raw,
            require_materialized=args.command == "report" or args.materialized,
        )
        if args.command == "validate-cohort":
            _write_json(
                {
                    **_public_cohort_summary(cohort),
                    "cohort_sha256": cohort_hash,
                    "valid": True,
                },
                None,
            )
            return 0

        annotation_rows, annotation_hash = load_jsonl(args.annotations)
        run_rows, runs_hash = load_jsonl(args.runs)
        runs = validate_runs(run_rows, cohort, config_id=args.config_id)
        resolved = resolve_annotations(annotation_rows, cohort, runs)
        report = build_report(
            cohort,
            resolved,
            runs,
            config_id=args.config_id,
            replicates=args.bootstrap,
            seed=args.seed,
            input_hashes={
                "annotations_sha256": annotation_hash,
                "cohort_sha256": cohort_hash,
                "runs_sha256": runs_hash,
            },
        )
        _write_json(report, args.output)
        return 0
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
