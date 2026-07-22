"""Offline Phase 8B corpus validation and deterministic freeze compiler.

This module never contacts GitHub or an LLM. Network acquisition is a separate,
bounded operator step whose selected source identities and object hashes are
validated here before any training artifact can be marked trainable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

import verifier_training as vt


SCHEMA_VERSION = 1
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ALLOWED_LICENSES = {"MIT", "BSD-3-Clause", "Apache-2.0"}
PLAN_KEYS = {
    "schema_version",
    "corpus_id",
    "base_commit",
    "corpus_seed",
    "seed_derivation",
    "selection_window",
    "limits",
    "repositories",
    "eligibility",
    "annotation",
    "retention",
    "authorization",
}
WINDOW_KEYS = {"start", "end"}
LIMIT_KEYS = {
    "max_prs",
    "max_candidates_per_pr",
    "max_candidates",
    "max_sanitized_bytes",
    "max_raw_bytes",
    "raw_retention_days",
    "max_paid_model_cny",
    "max_accelerator_hours",
}
REPOSITORY_KEYS = {
    "repository_id",
    "slug",
    "split",
    "target_prs",
    "language",
    "default_branch",
    "license_spdx",
    "license_url",
    "public",
    "verified_at",
}
ELIGIBILITY_KEYS = {
    "required_states",
    "selection_pool_cap_per_repository",
    "excluded_categories",
    "source_suffixes",
    "test_path_markers",
    "max_diff_bytes",
}
ANNOTATION_PLAN_KEYS = {
    "independent_annotators",
    "adjudicator_required_on_disagreement",
    "labels",
    "blind_test_split_until_freeze",
}
RETENTION_KEYS = {
    "raw_root",
    "raw_committed",
    "sanitized_committed",
    "delete_raw_after_days",
    "attribution_required",
}
AUTHORIZATION_KEYS = {
    "authority",
    "authorized_at",
    "public_github_read",
    "github_mutation",
    "paid_provider",
    "model_download",
    "dataset_hub_download",
    "accelerator",
    "model_upload",
}
PR_SOURCE_KEYS = {
    "schema_version",
    "source_id",
    "repository_id",
    "split",
    "pr_number",
    "merged_at",
    "base_sha",
    "head_sha",
    "merge_sha",
    "changed_files",
    "additions",
    "deletions",
    "selection_rank",
    "selection_digest",
    "selection_log_sha256",
    "eligible_pool_size",
    "diff_sha256",
    "diff_bytes",
    "diff_object_key",
    "secret_scan_sha256",
    "secret_scan_findings",
    "license_spdx",
    "source_url",
    "record_sha256",
}
CANDIDATE_SOURCE_KEYS = {
    "schema_version",
    "candidate_id",
    "source_id",
    "repository_id",
    "source_revision",
    "pr_source_sha256",
    "candidate_text",
    "evidence",
    "tool_summaries",
    "pair_id",
    "language",
    "severity",
    "content_sha256",
    "candidate_source_sha256",
}
CORPUS_ANNOTATION_KEYS = {
    "schema_version",
    "annotation_id",
    "candidate_id",
    "candidate_source_sha256",
    "annotator_id",
    "role",
    "label",
    "rationale",
    "evidence_sha256",
    "source_annotation_ids",
    "source_annotation_sha256s",
    "created_at",
    "synthetic",
    "annotation_sha256",
}
FREEZE_MANIFEST_KEYS = {
    "schema_version",
    "corpus_id",
    "frozen_at",
    "plan_sha256",
    "pr_sources_sha256",
    "candidate_sources_sha256",
    "annotations_sha256",
    "frozen_dataset_sha256",
    "split_manifest_sha256",
    "counts",
    "agreement",
    "repositories",
    "incomplete_gates",
    "trainable",
    "manifest_sha256",
}
ACQUISITION_MANIFEST_KEYS = {
    "schema_version",
    "corpus_id",
    "snapshot_at",
    "plan_sha256",
    "pr_sources_sha256",
    "pr_sources",
    "raw_bytes",
    "raw_limit_bytes",
    "repositories",
    "trainable",
    "incomplete_gates",
    "manifest_sha256",
}
ACQUISITION_REPOSITORY_KEYS = {
    "repository_id",
    "pool_size",
    "github_total_count",
    "inspected",
    "selected",
    "selection_log_sha256",
    "exclusions",
}
FINDER_QUEUE_KEYS = {
    "schema_version",
    "queue_id",
    "source_id",
    "repository_id",
    "source_revision",
    "pr_source_sha256",
    "diff_sha256",
    "diff_object_key",
    "max_candidates",
    "status",
    "queue_sha256",
}


class CorpusValidationError(ValueError):
    """Raised when Phase 8B corpus provenance or annotations fail closed."""


def _fail(message: str) -> None:
    raise CorpusValidationError(message)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _record_payload(row: dict[str, Any], hash_key: str) -> dict[str, Any]:
    return {key: row[key] for key in sorted(set(row) - {hash_key})}


def with_record_hash(row: dict[str, Any], hash_key: str) -> dict[str, Any]:
    result = dict(row)
    result[hash_key] = _sha256(_record_payload(result, hash_key))
    return result


def records_sha256(rows: Sequence[dict[str, Any]], hash_key: str) -> str:
    return _sha256(sorted(row[hash_key] for row in rows))


def selection_digest(seed: str, source_id: str) -> str:
    return hashlib.sha256(f"{seed}\n{source_id}".encode("utf-8")).hexdigest()


def _expect_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{where} must be an object")
    return value


def _expect_exact_keys(row: dict[str, Any], expected: set[str], where: str) -> None:
    missing = sorted(expected - set(row))
    unknown = sorted(set(row) - expected)
    if missing or unknown:
        _fail(f"{where} keys differ: missing={missing}, unknown={unknown}")


def _expect_int(value: Any, where: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{where} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        _fail(f"{where} must be <= {maximum}")
    return value


def _expect_identifier(value: Any, where: str) -> str:
    if not isinstance(value, str) or not vt.IDENTIFIER_RE.fullmatch(value):
        _fail(f"{where} must be a stable identifier")
    return value


def _expect_sha1(value: Any, where: str) -> str:
    if not isinstance(value, str) or not vt.SHA1_RE.fullmatch(value):
        _fail(f"{where} must be a lowercase SHA-1")
    return value


def _expect_sha256(value: Any, where: str) -> str:
    if not isinstance(value, str) or not vt.SHA256_RE.fullmatch(value):
        _fail(f"{where} must be a lowercase SHA-256")
    return value


def _expect_timestamp(value: Any, where: str) -> datetime:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        _fail(f"{where} must be canonical UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        _fail(f"{where} is not a real timestamp")


def _expect_text(value: Any, where: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{where} must be non-empty text")
    if "\x00" in value or len(value.encode("utf-8")) > maximum_bytes:
        _fail(f"{where} exceeds its safe text boundary")
    for pattern in vt.SENSITIVE_PATTERNS:
        if pattern.search(value):
            _fail(f"{where} contains credential-like or host-path content")
    return value


def _load_json(path: Path) -> Any:
    if not path.is_file() or path.stat().st_size > vt.MAX_DATASET_BYTES:
        _fail(f"missing or oversized JSON file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"cannot parse JSON file {path}: {exc.__class__.__name__}")


def _load_jsonl(path: Path) -> list[Any]:
    if not path.is_file() or path.stat().st_size > vt.MAX_DATASET_BYTES:
        _fail(f"missing or oversized JSONL file: {path}")
    output: list[Any] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    output.append(json.loads(line))
                except json.JSONDecodeError:
                    _fail(f"invalid JSONL at {path}:{line_number}")
    except (OSError, UnicodeError) as exc:
        _fail(f"cannot read JSONL file {path}: {exc.__class__.__name__}")
    return output


def validate_plan(raw: Any) -> dict[str, Any]:
    plan = _expect_dict(raw, "corpus_plan")
    _expect_exact_keys(plan, PLAN_KEYS, "corpus_plan")
    if plan["schema_version"] != SCHEMA_VERSION:
        _fail(f"corpus_plan.schema_version must be {SCHEMA_VERSION}")
    _expect_identifier(plan["corpus_id"], "corpus_plan.corpus_id")
    base_commit = _expect_sha1(plan["base_commit"], "corpus_plan.base_commit")
    seed = _expect_sha256(plan["corpus_seed"], "corpus_plan.corpus_seed")
    expected_seed = hashlib.sha256(
        f"{plan['corpus_id']}\n{base_commit}".encode("utf-8")
    ).hexdigest()
    if seed != expected_seed:
        _fail("corpus_plan.corpus_seed does not match corpus_id/base_commit derivation")
    expected_derivation = "sha256('week8b-verifier-corpus-v1\\n' + base_commit)"
    if plan["seed_derivation"] != expected_derivation:
        _fail("corpus_plan.seed_derivation is not the frozen expression")

    window = _expect_dict(plan["selection_window"], "corpus_plan.selection_window")
    _expect_exact_keys(window, WINDOW_KEYS, "corpus_plan.selection_window")
    start = _expect_timestamp(window["start"], "corpus_plan.selection_window.start")
    end = _expect_timestamp(window["end"], "corpus_plan.selection_window.end")
    if start >= end:
        _fail("corpus_plan selection window must be increasing")

    limits = _expect_dict(plan["limits"], "corpus_plan.limits")
    _expect_exact_keys(limits, LIMIT_KEYS, "corpus_plan.limits")
    for key in (
        "max_prs",
        "max_candidates_per_pr",
        "max_candidates",
        "max_sanitized_bytes",
        "max_raw_bytes",
        "raw_retention_days",
    ):
        _expect_int(limits[key], f"corpus_plan.limits.{key}", minimum=1)
    for key in ("max_paid_model_cny", "max_accelerator_hours"):
        if limits[key] != 0:
            _fail(f"corpus_plan.limits.{key} must remain zero in Phase 8B")
    if limits["max_prs"] != 29 or limits["max_candidates_per_pr"] != 16:
        _fail("Phase 8B pilot PR and per-PR candidate ceilings are frozen at 29 and 16")
    if limits["max_candidates"] != 480:
        _fail("Phase 8B max_candidates must remain 480")
    if limits["max_sanitized_bytes"] > 64 * 1024 * 1024:
        _fail("sanitized corpus ceiling exceeds 64 MiB")
    if limits["max_raw_bytes"] > 512 * 1024 * 1024:
        _fail("raw corpus ceiling exceeds 512 MiB")
    if limits["raw_retention_days"] > 30:
        _fail("raw retention exceeds 30 days")

    repositories = plan["repositories"]
    if not isinstance(repositories, list) or len(repositories) != 9:
        _fail("corpus_plan.repositories must contain the frozen nine repositories")
    repository_ids: set[str] = set()
    slugs: set[str] = set()
    target_total = 0
    split_repositories: Counter[str] = Counter()
    for index, raw_repository in enumerate(repositories):
        where = f"corpus_plan.repositories[{index}]"
        repository = _expect_dict(raw_repository, where)
        _expect_exact_keys(repository, REPOSITORY_KEYS, where)
        repository_id = repository["repository_id"]
        slug = repository["slug"]
        if not isinstance(repository_id, str) or not REPOSITORY_RE.fullmatch(repository_id):
            _fail(f"{where}.repository_id is invalid")
        if slug != repository_id:
            _fail(f"{where}.slug must equal repository_id")
        if repository_id in repository_ids or slug in slugs:
            _fail(f"duplicate repository {repository_id!r}")
        repository_ids.add(repository_id)
        slugs.add(slug)
        if repository["split"] not in {"train", "validation", "test"}:
            _fail(f"{where}.split is invalid")
        split_repositories[repository["split"]] += 1
        target_total += _expect_int(repository["target_prs"], f"{where}.target_prs", minimum=1)
        if repository["language"] != "python":
            _fail(f"{where}.language must be python for the pilot")
        _expect_identifier(repository["default_branch"], f"{where}.default_branch")
        if repository["license_spdx"] not in ALLOWED_LICENSES:
            _fail(f"{where}.license_spdx is not an allowed permissive license")
        if not isinstance(repository["license_url"], str) or not repository[
            "license_url"
        ].startswith(f"https://github.com/{slug}/blob/"):
            _fail(f"{where}.license_url must bind the declared GitHub repository")
        if repository["public"] is not True:
            _fail(f"{where}.public must be true")
        _expect_timestamp(repository["verified_at"], f"{where}.verified_at")
    if target_total != limits["max_prs"]:
        _fail("repository target PR total must equal max_prs")
    if split_repositories != Counter({"train": 4, "validation": 2, "test": 3}):
        _fail("repository split counts must remain train=4, validation=2, test=3")

    eligibility = _expect_dict(plan["eligibility"], "corpus_plan.eligibility")
    _expect_exact_keys(eligibility, ELIGIBILITY_KEYS, "corpus_plan.eligibility")
    if eligibility["required_states"] != [
        "merged",
        "non_draft",
        "code_change",
        "secret_scan_passed",
    ]:
        _fail("corpus_plan.eligibility.required_states differs from the frozen list")
    if eligibility["selection_pool_cap_per_repository"] != 64:
        _fail("selection_pool_cap_per_repository must remain 64")
    if not isinstance(eligibility["excluded_categories"], list) or not eligibility[
        "excluded_categories"
    ]:
        _fail("corpus_plan.eligibility.excluded_categories must be non-empty")
    if eligibility["source_suffixes"] != [".py", ".pyi"]:
        _fail("corpus_plan.eligibility.source_suffixes is not frozen")
    if eligibility["test_path_markers"] != ["test", "tests"]:
        _fail("corpus_plan.eligibility.test_path_markers is not frozen")
    if eligibility["max_diff_bytes"] > 512 * 1024:
        _fail("per-PR diff ceiling exceeds 512 KiB")

    annotation = _expect_dict(plan["annotation"], "corpus_plan.annotation")
    _expect_exact_keys(annotation, ANNOTATION_PLAN_KEYS, "corpus_plan.annotation")
    if annotation != {
        "independent_annotators": 2,
        "adjudicator_required_on_disagreement": True,
        "labels": ["keep", "drop", "uncertain"],
        "blind_test_split_until_freeze": True,
    }:
        _fail("corpus_plan.annotation differs from the frozen two-labeler protocol")

    retention = _expect_dict(plan["retention"], "corpus_plan.retention")
    _expect_exact_keys(retention, RETENTION_KEYS, "corpus_plan.retention")
    raw_root = retention["raw_root"]
    if not isinstance(raw_root, str) or "\\" in raw_root:
        _fail("corpus_plan.retention.raw_root must be a POSIX task-local path")
    raw_path = PurePosixPath(raw_root)
    if raw_path.is_absolute() or raw_path.parts[:1] != ("traces",) or ".." in raw_path.parts:
        _fail("corpus_plan.retention.raw_root must stay under ignored traces/")
    if retention != {
        "raw_root": "traces/week8b-corpus",
        "raw_committed": False,
        "sanitized_committed": True,
        "delete_raw_after_days": 30,
        "attribution_required": True,
    }:
        _fail("corpus_plan.retention differs from the frozen policy")

    authorization = _expect_dict(plan["authorization"], "corpus_plan.authorization")
    _expect_exact_keys(authorization, AUTHORIZATION_KEYS, "corpus_plan.authorization")
    _expect_text(authorization["authority"], "corpus_plan.authorization.authority", 500)
    _expect_timestamp(authorization["authorized_at"], "corpus_plan.authorization.authorized_at")
    if authorization["public_github_read"] is not True:
        _fail("public GitHub read authorization is required for Phase 8B")
    for key in (
        "github_mutation",
        "paid_provider",
        "model_download",
        "dataset_hub_download",
        "accelerator",
        "model_upload",
    ):
        if authorization[key] is not False:
            _fail(f"corpus_plan.authorization.{key} must remain false")
    return plan


def load_plan(path: Path) -> dict[str, Any]:
    return validate_plan(_load_json(path))


def _plan_repository_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {repository["repository_id"]: repository for repository in plan["repositories"]}


def validate_pr_sources(raw_rows: Sequence[Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
    repositories = _plan_repository_map(plan)
    start = _expect_timestamp(plan["selection_window"]["start"], "selection_window.start")
    end = _expect_timestamp(plan["selection_window"]["end"], "selection_window.end")
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    total_diff_bytes = 0
    by_repository: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, raw in enumerate(raw_rows):
        where = f"pr_sources[{index}]"
        row = _expect_dict(raw, where)
        _expect_exact_keys(row, PR_SOURCE_KEYS, where)
        if row["schema_version"] != SCHEMA_VERSION:
            _fail(f"{where}.schema_version must be {SCHEMA_VERSION}")
        repository_id = row["repository_id"]
        if repository_id not in repositories:
            _fail(f"{where}.repository_id is not in the frozen corpus plan")
        repository = repositories[repository_id]
        pr_number = _expect_int(row["pr_number"], f"{where}.pr_number", minimum=1)
        source_id = f"{repository_id}#{pr_number}"
        if row["source_id"] != source_id:
            _fail(f"{where}.source_id does not match repository/pr_number")
        if source_id in seen_ids:
            _fail(f"duplicate PR source {source_id!r}")
        seen_ids.add(source_id)
        if row["split"] != repository["split"]:
            _fail(f"{where}.split does not match the corpus plan")
        merged_at = _expect_timestamp(row["merged_at"], f"{where}.merged_at")
        if not start <= merged_at < end:
            _fail(f"{where}.merged_at lies outside the selection window")
        for key in ("base_sha", "head_sha", "merge_sha"):
            _expect_sha1(row[key], f"{where}.{key}")
        if len({row["base_sha"], row["head_sha"], row["merge_sha"]}) < 3:
            _fail(f"{where} source revisions must be distinct")
        for key in ("changed_files", "additions", "deletions"):
            _expect_int(row[key], f"{where}.{key}", minimum=1 if key == "changed_files" else 0)
        _expect_int(
            row["selection_rank"],
            f"{where}.selection_rank",
            minimum=1,
            maximum=repository["target_prs"],
        )
        expected_digest = selection_digest(plan["corpus_seed"], source_id)
        if row["selection_digest"] != expected_digest:
            _fail(f"{where}.selection_digest does not match the frozen seed")
        _expect_sha256(row["selection_log_sha256"], f"{where}.selection_log_sha256")
        if _expect_int(row["eligible_pool_size"], f"{where}.eligible_pool_size", minimum=1) < repository[
            "target_prs"
        ]:
            _fail(f"{where}.eligible_pool_size is smaller than the target")
        diff_sha = _expect_sha256(row["diff_sha256"], f"{where}.diff_sha256")
        diff_bytes = _expect_int(
            row["diff_bytes"],
            f"{where}.diff_bytes",
            minimum=1,
            maximum=plan["eligibility"]["max_diff_bytes"],
        )
        total_diff_bytes += diff_bytes
        expected_key = f"objects/{diff_sha}.diff"
        if row["diff_object_key"] != expected_key:
            _fail(f"{where}.diff_object_key must be content-addressed")
        _expect_sha256(row["secret_scan_sha256"], f"{where}.secret_scan_sha256")
        if row["secret_scan_findings"] != 0:
            _fail(f"{where}.secret_scan_findings must be zero")
        if row["license_spdx"] != repository["license_spdx"]:
            _fail(f"{where}.license_spdx does not match the corpus plan")
        expected_url = f"https://github.com/{repository_id}/pull/{pr_number}"
        if row["source_url"] != expected_url:
            _fail(f"{where}.source_url must be the canonical GitHub PR URL")
        _expect_sha256(row["record_sha256"], f"{where}.record_sha256")
        if row["record_sha256"] != _sha256(_record_payload(row, "record_sha256")):
            _fail(f"{where}.record_sha256 does not match the canonical record")
        rows.append(row)
        by_repository[repository_id].append(row)
    if len(rows) != plan["limits"]["max_prs"]:
        _fail("PR source records must exactly fill the frozen 29-PR pilot")
    if total_diff_bytes > plan["limits"]["max_raw_bytes"]:
        _fail("selected PR diffs exceed the raw-byte ceiling")
    for repository_id, repository in repositories.items():
        repository_rows = by_repository.get(repository_id, [])
        if len(repository_rows) != repository["target_prs"]:
            _fail(f"repository {repository_id!r} does not meet target_prs")
        ranks = sorted(row["selection_rank"] for row in repository_rows)
        if ranks != list(range(1, repository["target_prs"] + 1)):
            _fail(f"repository {repository_id!r} selection ranks are incomplete")
        selection_logs = {row["selection_log_sha256"] for row in repository_rows}
        pool_sizes = {row["eligible_pool_size"] for row in repository_rows}
        if len(selection_logs) != 1 or len(pool_sizes) != 1:
            _fail(f"repository {repository_id!r} has inconsistent selection-log provenance")
    return rows


def load_pr_sources(path: Path, plan: dict[str, Any]) -> list[dict[str, Any]]:
    return validate_pr_sources(_load_jsonl(path), plan)


def _candidate_source_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in sorted(CANDIDATE_SOURCE_KEYS - {"candidate_source_sha256"})}


def with_candidate_source_hashes(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["content_sha256"] = _sha256(
        {
            "candidate_text": result["candidate_text"],
            "evidence": result["evidence"],
            "tool_summaries": result["tool_summaries"],
        }
    )
    result["candidate_source_sha256"] = _sha256(_candidate_source_payload(result))
    return result


def validate_candidate_sources(
    raw_rows: Sequence[Any], plan: dict[str, Any], pr_sources: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    pr_by_id = {row["source_id"]: row for row in pr_sources}
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    per_source: Counter[str] = Counter()
    for index, raw in enumerate(raw_rows):
        where = f"candidate_sources[{index}]"
        row = _expect_dict(raw, where)
        _expect_exact_keys(row, CANDIDATE_SOURCE_KEYS, where)
        if row["schema_version"] != SCHEMA_VERSION:
            _fail(f"{where}.schema_version must be {SCHEMA_VERSION}")
        candidate_id = _expect_identifier(row["candidate_id"], f"{where}.candidate_id")
        if candidate_id in seen_ids:
            _fail(f"duplicate candidate_id {candidate_id!r}")
        seen_ids.add(candidate_id)
        source_id = row["source_id"]
        if source_id not in pr_by_id:
            _fail(f"{where}.source_id does not reference an admitted PR")
        pr_source = pr_by_id[source_id]
        if row["repository_id"] != pr_source["repository_id"]:
            _fail(f"{where}.repository_id does not match its PR source")
        if row["source_revision"] != pr_source["merge_sha"]:
            _fail(f"{where}.source_revision must equal the selected merge SHA")
        if row["pr_source_sha256"] != pr_source["record_sha256"]:
            _fail(f"{where}.pr_source_sha256 does not bind its PR record")
        _expect_text(row["candidate_text"], f"{where}.candidate_text", vt.MAX_CANDIDATE_BYTES)
        try:
            vt._validate_evidence(row["evidence"], f"{where}.evidence")
            vt._validate_tool_summaries(row["tool_summaries"], f"{where}.tool_summaries")
        except vt.ValidationError as exc:
            _fail(str(exc))
        if row["pair_id"] is not None:
            _expect_identifier(row["pair_id"], f"{where}.pair_id")
        if row["language"] is not None:
            _expect_identifier(row["language"], f"{where}.language")
        if row["severity"] not in {None, "low", "medium", "high"}:
            _fail(f"{where}.severity is invalid")
        _expect_sha256(row["content_sha256"], f"{where}.content_sha256")
        expected_content = _sha256(
            {
                "candidate_text": row["candidate_text"],
                "evidence": row["evidence"],
                "tool_summaries": row["tool_summaries"],
            }
        )
        if row["content_sha256"] != expected_content:
            _fail(f"{where}.content_sha256 does not match canonical content")
        if expected_content in seen_hashes:
            _fail(f"duplicate canonical candidate content {expected_content}")
        seen_hashes.add(expected_content)
        _expect_sha256(row["candidate_source_sha256"], f"{where}.candidate_source_sha256")
        if row["candidate_source_sha256"] != _sha256(_candidate_source_payload(row)):
            _fail(f"{where}.candidate_source_sha256 does not match the canonical record")
        per_source[source_id] += 1
        if per_source[source_id] > plan["limits"]["max_candidates_per_pr"]:
            _fail(f"source {source_id!r} exceeds the per-PR candidate ceiling")
        rows.append(row)
    if not rows:
        _fail("candidate source dataset is empty")
    if len(rows) > plan["limits"]["max_candidates"]:
        _fail("candidate source dataset exceeds max_candidates")
    return rows


def load_candidate_sources(
    path: Path, plan: dict[str, Any], pr_sources: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    return validate_candidate_sources(_load_jsonl(path), plan, pr_sources)


def _annotation_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in sorted(CORPUS_ANNOTATION_KEYS - {"annotation_sha256"})}


def with_annotation_hash(row: dict[str, Any]) -> dict[str, Any]:
    return with_record_hash(row, "annotation_sha256")


def validate_annotations(
    raw_rows: Sequence[Any], candidate_sources: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    candidates = {row["candidate_id"]: row for row in candidate_sources}
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_rows):
        where = f"annotations[{index}]"
        row = _expect_dict(raw, where)
        _expect_exact_keys(row, CORPUS_ANNOTATION_KEYS, where)
        if row["schema_version"] != SCHEMA_VERSION:
            _fail(f"{where}.schema_version must be {SCHEMA_VERSION}")
        annotation_id = _expect_identifier(row["annotation_id"], f"{where}.annotation_id")
        if annotation_id in seen_ids:
            _fail(f"duplicate annotation_id {annotation_id!r}")
        seen_ids.add(annotation_id)
        candidate_id = row["candidate_id"]
        if candidate_id not in candidates:
            _fail(f"{where}.candidate_id is unknown")
        if row["candidate_source_sha256"] != candidates[candidate_id]["candidate_source_sha256"]:
            _fail(f"{where}.candidate_source_sha256 does not bind its candidate")
        _expect_identifier(row["annotator_id"], f"{where}.annotator_id")
        if row["role"] not in {"annotator", "adjudicator"}:
            _fail(f"{where}.role is invalid")
        if row["label"] not in {"keep", "drop", "uncertain"}:
            _fail(f"{where}.label is invalid")
        _expect_text(row["rationale"], f"{where}.rationale", vt.MAX_RATIONALE_BYTES)
        _expect_sha256(row["evidence_sha256"], f"{where}.evidence_sha256")
        if not isinstance(row["source_annotation_ids"], list) or not isinstance(
            row["source_annotation_sha256s"], list
        ):
            _fail(f"{where} source annotation references must be lists")
        for source_index, source_id in enumerate(row["source_annotation_ids"]):
            _expect_identifier(source_id, f"{where}.source_annotation_ids[{source_index}]")
        for source_index, source_hash in enumerate(row["source_annotation_sha256s"]):
            _expect_sha256(source_hash, f"{where}.source_annotation_sha256s[{source_index}]")
        if row["role"] == "annotator" and (
            row["source_annotation_ids"] or row["source_annotation_sha256s"]
        ):
            _fail(f"{where} independent annotations cannot cite source annotations")
        if row["role"] == "adjudicator" and (
            len(row["source_annotation_ids"]) != 2
            or len(row["source_annotation_sha256s"]) != 2
        ):
            _fail(f"{where} adjudication must cite exactly two source annotations")
        _expect_timestamp(row["created_at"], f"{where}.created_at")
        if not isinstance(row["synthetic"], bool):
            _fail(f"{where}.synthetic must be boolean")
        _expect_sha256(row["annotation_sha256"], f"{where}.annotation_sha256")
        if row["annotation_sha256"] != _sha256(_annotation_payload(row)):
            _fail(f"{where}.annotation_sha256 does not match the canonical record")
        rows.append(row)
    if not rows:
        _fail("annotation dataset is empty")
    return rows


def load_annotations(
    path: Path, candidate_sources: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    return validate_annotations(_load_jsonl(path), candidate_sources)


def resolve_annotations(
    candidate_sources: Sequence[dict[str, Any]], annotations: Sequence[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, int], list[str]]:
    by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in annotations:
        by_candidate[row["candidate_id"]].append(row)
    finals: dict[str, dict[str, Any]] = {}
    agreement = Counter(
        {
            "candidates": len(candidate_sources),
            "independent_agreements": 0,
            "adjudications": 0,
            "final_uncertain": 0,
            "synthetic_final": 0,
            "unresolved": 0,
        }
    )
    unresolved: list[str] = []
    for candidate in candidate_sources:
        candidate_id = candidate["candidate_id"]
        candidate_annotations = by_candidate.get(candidate_id, [])
        independent = sorted(
            [row for row in candidate_annotations if row["role"] == "annotator"],
            key=lambda row: row["annotation_id"],
        )
        adjudications = [row for row in candidate_annotations if row["role"] == "adjudicator"]
        if len(independent) > 2:
            _fail(f"candidate {candidate_id!r} has more than two independent annotations")
        if len({row["annotator_id"] for row in independent}) != len(independent):
            _fail(f"candidate {candidate_id!r} repeats an independent annotator")
        if len(adjudications) > 1:
            _fail(f"candidate {candidate_id!r} has multiple adjudications")
        if len(independent) < 2:
            unresolved.append(candidate_id)
            agreement["unresolved"] += 1
            continue
        synthetic_values = {row["synthetic"] for row in independent}
        if len(synthetic_values) != 1:
            _fail(f"candidate {candidate_id!r} mixes synthetic and real annotations")
        agreed = independent[0]["label"] == independent[1]["label"] != "uncertain"
        if agreed:
            if adjudications:
                _fail(f"candidate {candidate_id!r} has unnecessary adjudication")
            final = {
                "label": independent[0]["label"],
                "rationale": "Independent agreement: "
                + " | ".join(row["rationale"] for row in independent),
                "synthetic": independent[0]["synthetic"],
            }
            agreement["independent_agreements"] += 1
        else:
            if not adjudications:
                unresolved.append(candidate_id)
                agreement["unresolved"] += 1
                continue
            adjudication = adjudications[0]
            if adjudication["annotator_id"] in {row["annotator_id"] for row in independent}:
                _fail(f"candidate {candidate_id!r} adjudicator is not independent")
            expected_ids = [row["annotation_id"] for row in independent]
            expected_hashes = [row["annotation_sha256"] for row in independent]
            if adjudication["source_annotation_ids"] != expected_ids:
                _fail(f"candidate {candidate_id!r} adjudication cites the wrong annotations")
            if adjudication["source_annotation_sha256s"] != expected_hashes:
                _fail(f"candidate {candidate_id!r} adjudication cites stale annotation hashes")
            if adjudication["synthetic"] != independent[0]["synthetic"]:
                _fail(f"candidate {candidate_id!r} adjudication changes synthetic provenance")
            final = {
                "label": adjudication["label"],
                "rationale": adjudication["rationale"],
                "synthetic": adjudication["synthetic"],
            }
            agreement["adjudications"] += 1
        if final["label"] == "uncertain":
            agreement["final_uncertain"] += 1
        if final["synthetic"]:
            agreement["synthetic_final"] += 1
        finals[candidate_id] = final
    extra_candidates = sorted(set(by_candidate) - {row["candidate_id"] for row in candidate_sources})
    if extra_candidates:
        _fail(f"annotations reference unknown candidates: {extra_candidates}")
    return finals, dict(agreement), sorted(unresolved)


def compile_frozen_candidates(
    candidate_sources: Sequence[dict[str, Any]], finals: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for source in sorted(candidate_sources, key=lambda row: row["candidate_id"]):
        final = finals.get(source["candidate_id"])
        if final is None:
            continue
        row = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": source["candidate_id"],
            "repository_id": source["repository_id"],
            "change_id": source["source_id"],
            "source_revision": source["source_revision"],
            "candidate_text": source["candidate_text"],
            "evidence": source["evidence"],
            "tool_summaries": source["tool_summaries"],
            "label": final["label"],
            "label_source": "synthetic" if final["synthetic"] else "human_adjudicated",
            "rationale": final["rationale"],
            "pair_id": source["pair_id"],
            "language": source["language"],
            "severity": source["severity"],
            "content_sha256": source["content_sha256"],
            "record_sha256": "",
        }
        row = vt.with_candidate_hashes(row)
        vt.validate_candidate_row(row, len(output))
        output.append(row)
    return output


def build_freeze(
    plan: dict[str, Any],
    pr_sources: Sequence[dict[str, Any]],
    candidate_sources: Sequence[dict[str, Any]],
    annotations: Sequence[dict[str, Any]],
    frozen_at: str,
    completed_source_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    _expect_timestamp(frozen_at, "frozen_at")
    finals, agreement, unresolved = resolve_annotations(candidate_sources, annotations)
    candidates = compile_frozen_candidates(candidate_sources, finals)
    if not candidates:
        _fail("freeze has no resolved candidates")
    represented_repositories = {row["repository_id"] for row in candidates}
    split_manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_sha256": vt.dataset_sha256(candidates),
        "splits": {
            split_name: sorted(
                repository["repository_id"]
                for repository in plan["repositories"]
                if repository["split"] == split_name
                and repository["repository_id"] in represented_repositories
            )
            for split_name in ("train", "validation", "test")
        },
        "operating_threshold": None,
        "threshold_source": "unfrozen",
    }
    try:
        vt.validate_split_manifest(split_manifest, candidates)
    except vt.ValidationError as exc:
        _fail(str(exc))
    source_ids_with_candidates = {row["source_id"] for row in candidate_sources}
    admitted_source_ids = {row["source_id"] for row in pr_sources}
    completed = source_ids_with_candidates if completed_source_ids is None else completed_source_ids
    if not completed.issubset(admitted_source_ids):
        _fail("completed_source_ids contains a source outside the admitted corpus")
    if not source_ids_with_candidates.issubset(completed):
        _fail("a candidate source is not covered by a completed Finder run")
    missing_source_candidates = sorted(
        row["source_id"] for row in pr_sources if row["source_id"] not in completed
    )
    planned_repositories = {row["repository_id"] for row in plan["repositories"]}
    missing_repositories = sorted(planned_repositories - represented_repositories)
    gates: list[dict[str, Any]] = []
    if missing_source_candidates:
        gates.append(
            {
                "gate": "selected_pr_without_candidates",
                "count": len(missing_source_candidates),
                "sample_ids": missing_source_candidates[:10],
            }
        )
    if unresolved:
        gates.append(
            {
                "gate": "unresolved_annotations",
                "count": len(unresolved),
                "sample_ids": unresolved[:10],
            }
        )
    if agreement["synthetic_final"]:
        gates.append(
            {
                "gate": "synthetic_records_present",
                "count": agreement["synthetic_final"],
                "sample_ids": [],
            }
        )
    if missing_repositories:
        gates.append(
            {
                "gate": "repository_without_resolved_candidates",
                "count": len(missing_repositories),
                "sample_ids": missing_repositories[:10],
            }
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "corpus_id": plan["corpus_id"],
        "frozen_at": frozen_at,
        "plan_sha256": _sha256(plan),
        "pr_sources_sha256": records_sha256(pr_sources, "record_sha256"),
        "candidate_sources_sha256": records_sha256(
            candidate_sources, "candidate_source_sha256"
        ),
        "annotations_sha256": records_sha256(annotations, "annotation_sha256"),
        "frozen_dataset_sha256": vt.dataset_sha256(candidates),
        "split_manifest_sha256": _sha256(split_manifest),
        "counts": {
            "pr_sources": len(pr_sources),
            "candidate_sources": len(candidate_sources),
            "annotations": len(annotations),
            "frozen_candidates": len(candidates),
        },
        "agreement": agreement,
        "repositories": {
            split_name: split_manifest["splits"][split_name]
            for split_name in ("train", "validation", "test")
        },
        "incomplete_gates": gates,
        "trainable": not gates,
        "manifest_sha256": "",
    }
    manifest["manifest_sha256"] = _sha256(_record_payload(manifest, "manifest_sha256"))
    validate_freeze_manifest(manifest)
    output_size = (
        sum(len(_canonical_bytes(row)) + 1 for row in candidates)
        + len(_canonical_bytes(split_manifest))
        + len(_canonical_bytes(manifest))
    )
    if output_size > plan["limits"]["max_sanitized_bytes"]:
        _fail("compiled sanitized corpus exceeds its byte ceiling")
    return candidates, split_manifest, manifest


def validate_freeze_manifest(raw: Any) -> dict[str, Any]:
    manifest = _expect_dict(raw, "freeze_manifest")
    _expect_exact_keys(manifest, FREEZE_MANIFEST_KEYS, "freeze_manifest")
    if manifest["schema_version"] != SCHEMA_VERSION:
        _fail(f"freeze_manifest.schema_version must be {SCHEMA_VERSION}")
    _expect_identifier(manifest["corpus_id"], "freeze_manifest.corpus_id")
    _expect_timestamp(manifest["frozen_at"], "freeze_manifest.frozen_at")
    for key in (
        "plan_sha256",
        "pr_sources_sha256",
        "candidate_sources_sha256",
        "annotations_sha256",
        "frozen_dataset_sha256",
        "split_manifest_sha256",
        "manifest_sha256",
    ):
        _expect_sha256(manifest[key], f"freeze_manifest.{key}")
    if manifest["manifest_sha256"] != _sha256(_record_payload(manifest, "manifest_sha256")):
        _fail("freeze_manifest.manifest_sha256 does not match canonical content")
    if not isinstance(manifest["counts"], dict) or not isinstance(manifest["agreement"], dict):
        _fail("freeze_manifest counts/agreement must be objects")
    if not isinstance(manifest["repositories"], dict):
        _fail("freeze_manifest.repositories must be an object")
    if not isinstance(manifest["incomplete_gates"], list):
        _fail("freeze_manifest.incomplete_gates must be a list")
    if not isinstance(manifest["trainable"], bool):
        _fail("freeze_manifest.trainable must be boolean")
    if manifest["trainable"] != (not manifest["incomplete_gates"]):
        _fail("freeze_manifest.trainable contradicts incomplete_gates")
    return manifest


def validate_acquisition_manifest(
    raw: Any, plan: dict[str, Any], pr_sources: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    manifest = _expect_dict(raw, "acquisition_manifest")
    _expect_exact_keys(manifest, ACQUISITION_MANIFEST_KEYS, "acquisition_manifest")
    if manifest["schema_version"] != SCHEMA_VERSION:
        _fail(f"acquisition_manifest.schema_version must be {SCHEMA_VERSION}")
    if manifest["corpus_id"] != plan["corpus_id"]:
        _fail("acquisition_manifest.corpus_id does not match the plan")
    _expect_timestamp(manifest["snapshot_at"], "acquisition_manifest.snapshot_at")
    if manifest["plan_sha256"] != _sha256(plan):
        _fail("acquisition_manifest.plan_sha256 does not match the plan")
    if manifest["pr_sources_sha256"] != records_sha256(pr_sources, "record_sha256"):
        _fail("acquisition_manifest.pr_sources_sha256 does not match the source records")
    if manifest["pr_sources"] != len(pr_sources) or len(pr_sources) != 29:
        _fail("acquisition_manifest must bind exactly 29 PR sources")
    raw_bytes = _expect_int(manifest["raw_bytes"], "acquisition_manifest.raw_bytes", minimum=1)
    if manifest["raw_limit_bytes"] != plan["limits"]["max_raw_bytes"]:
        _fail("acquisition_manifest.raw_limit_bytes does not match the plan")
    if raw_bytes > manifest["raw_limit_bytes"]:
        _fail("acquisition_manifest raw bytes exceed the frozen ceiling")
    repositories = manifest["repositories"]
    if not isinstance(repositories, list) or len(repositories) != 9:
        _fail("acquisition_manifest.repositories must contain nine summaries")
    planned = _plan_repository_map(plan)
    seen: set[str] = set()
    for index, raw_repository in enumerate(repositories):
        where = f"acquisition_manifest.repositories[{index}]"
        repository = _expect_dict(raw_repository, where)
        _expect_exact_keys(repository, ACQUISITION_REPOSITORY_KEYS, where)
        repository_id = repository["repository_id"]
        if repository_id not in planned or repository_id in seen:
            _fail(f"{where}.repository_id is unknown or duplicated")
        seen.add(repository_id)
        pool_size = _expect_int(repository["pool_size"], f"{where}.pool_size", minimum=1)
        if pool_size > plan["eligibility"]["selection_pool_cap_per_repository"]:
            _fail(f"{where}.pool_size exceeds the frozen cap")
        _expect_int(repository["github_total_count"], f"{where}.github_total_count", minimum=pool_size)
        selected = _expect_int(repository["selected"], f"{where}.selected", minimum=1)
        _expect_int(repository["inspected"], f"{where}.inspected", minimum=selected)
        if selected != planned[repository_id]["target_prs"]:
            _fail(f"{where}.selected does not meet the repository target")
        _expect_sha256(repository["selection_log_sha256"], f"{where}.selection_log_sha256")
        if not isinstance(repository["exclusions"], dict) or any(
            not isinstance(key, str) or not isinstance(value, int) or value < 0
            for key, value in repository["exclusions"].items()
        ):
            _fail(f"{where}.exclusions must be non-negative integer counts")
    if manifest["trainable"] is not False:
        _fail("an acquisition-only manifest cannot be trainable")
    if manifest["incomplete_gates"] != [
        "finder_candidates_missing",
        "independent_human_annotations_missing",
    ]:
        _fail("acquisition_manifest incomplete gates are not frozen")
    _expect_sha256(manifest["manifest_sha256"], "acquisition_manifest.manifest_sha256")
    if manifest["manifest_sha256"] != _sha256(_record_payload(manifest, "manifest_sha256")):
        _fail("acquisition_manifest.manifest_sha256 does not match canonical content")
    return manifest


def build_finder_queue(pr_sources: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for source in sorted(pr_sources, key=lambda row: row["source_id"]):
        row = {
            "schema_version": SCHEMA_VERSION,
            "queue_id": f"finder-{source['record_sha256'][:24]}",
            "source_id": source["source_id"],
            "repository_id": source["repository_id"],
            "source_revision": source["merge_sha"],
            "pr_source_sha256": source["record_sha256"],
            "diff_sha256": source["diff_sha256"],
            "diff_object_key": source["diff_object_key"],
            "max_candidates": 16,
            "status": "pending",
            "queue_sha256": "",
        }
        queue.append(with_record_hash(row, "queue_sha256"))
    return queue


def validate_finder_queue(
    raw_rows: Sequence[Any], pr_sources: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    sources = {row["source_id"]: row for row in pr_sources}
    if len(raw_rows) != len(sources) or len(sources) != 29:
        _fail("finder queue must bind all 29 selected PR sources exactly once")
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows):
        where = f"finder_queue[{index}]"
        row = _expect_dict(raw, where)
        _expect_exact_keys(row, FINDER_QUEUE_KEYS, where)
        source_id = row["source_id"]
        if source_id not in sources or source_id in seen:
            _fail(f"{where}.source_id is unknown or duplicated")
        seen.add(source_id)
        source = sources[source_id]
        if row["schema_version"] != SCHEMA_VERSION:
            _fail(f"{where}.schema_version must be {SCHEMA_VERSION}")
        expected_id = f"finder-{source['record_sha256'][:24]}"
        if row["queue_id"] != expected_id:
            _fail(f"{where}.queue_id does not bind the PR source hash")
        expected = {
            "repository_id": source["repository_id"],
            "source_revision": source["merge_sha"],
            "pr_source_sha256": source["record_sha256"],
            "diff_sha256": source["diff_sha256"],
            "diff_object_key": source["diff_object_key"],
            "max_candidates": 16,
            "status": "pending",
        }
        for key, value in expected.items():
            if row[key] != value:
                _fail(f"{where}.{key} does not match the frozen source")
        if row["queue_sha256"] != _sha256(_record_payload(row, "queue_sha256")):
            _fail(f"{where}.queue_sha256 does not match canonical content")
        rows.append(row)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def _load_all(args: argparse.Namespace) -> tuple[
    dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
]:
    plan = load_plan(args.plan)
    pr_sources = load_pr_sources(args.pr_sources, plan)
    candidate_sources = load_candidate_sources(args.candidate_sources, plan, pr_sources)
    annotations = load_annotations(args.annotations, candidate_sources)
    return plan, pr_sources, candidate_sources, annotations


def _command_validate(args: argparse.Namespace) -> dict[str, Any]:
    plan, pr_sources, candidate_sources, annotations = _load_all(args)
    finals, agreement, unresolved = resolve_annotations(candidate_sources, annotations)
    return {
        "status": "ok",
        "corpus_id": plan["corpus_id"],
        "plan_sha256": _sha256(plan),
        "pr_sources": len(pr_sources),
        "candidate_sources": len(candidate_sources),
        "annotations": len(annotations),
        "resolved_candidates": len(finals),
        "unresolved_candidates": unresolved,
        "agreement": agreement,
        "trainable": False,
        "note": "validate does not mark a corpus trainable; run freeze and inspect gates",
    }


def _command_freeze(args: argparse.Namespace) -> dict[str, Any]:
    plan, pr_sources, candidate_sources, annotations = _load_all(args)
    candidates, splits, manifest = build_freeze(
        plan, pr_sources, candidate_sources, annotations, args.frozen_at
    )
    _write_jsonl(args.candidates_out, candidates)
    _write_json(args.splits_out, splits)
    _write_json(args.manifest_out, manifest)
    return {
        "status": "ok",
        "corpus_id": plan["corpus_id"],
        "frozen_candidates": len(candidates),
        "trainable": manifest["trainable"],
        "incomplete_gates": manifest["incomplete_gates"],
        "manifest_sha256": manifest["manifest_sha256"],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and freeze the offline Week 8B verifier corpus."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "freeze"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--plan", type=Path, required=True)
        command_parser.add_argument("--pr-sources", type=Path, required=True)
        command_parser.add_argument("--candidate-sources", type=Path, required=True)
        command_parser.add_argument("--annotations", type=Path, required=True)
        if command == "validate":
            command_parser.set_defaults(handler=_command_validate)
        else:
            command_parser.add_argument("--frozen-at", required=True)
            command_parser.add_argument("--candidates-out", type=Path, required=True)
            command_parser.add_argument("--splits-out", type=Path, required=True)
            command_parser.add_argument("--manifest-out", type=Path, required=True)
            command_parser.set_defaults(handler=_command_freeze)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (CorpusValidationError, vt.ValidationError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
