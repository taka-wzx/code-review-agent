"""Offline planner for the preregistered Week 5 SWE-bench Repair evaluation.

This module deliberately has no network, Docker, Git, subprocess, model, or
benchmark-dataset integration.  It validates already-local metadata and emits a
deterministic run plan whose rows bind every task/configuration attempt to a
unique worktree and container identity.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
DATASET_NAME = "princeton-nlp/SWE-bench_Verified"
COHORT_METHOD = "sha256_repo_then_task_v1"
SEED_DERIVATION = (
    "sha256(swebench-repair-cohort-v1\\0 + ascii(source_commit))"
)
SEED_DOMAIN = b"swebench-repair-cohort-v1\x00"
RUN_PLAN_DOMAIN = b"swebench-repair-run-plan-v1\x00"
IDENTITY_DOMAIN = b"swebench-repair-run-identity-v1\x00"
PATH_DOMAIN = b"swebench-repair-worktree-path-v1\x00"
ROLE_TARGETS = {"development": 5, "tuning": 5, "reporting": 20}
ROLE_ORDER = ("reporting", "tuning", "development")
REPOSITORIES_PER_ROLE = {"reporting": 4, "tuning": 1, "development": 1}
TASKS_PER_REPOSITORY = 5
CONFIGURATION_SPECS: dict[str, dict[str, Any]] = {
    "primary": {
        "finder": "dual",
        "context_retrieval": True,
        "verifier": True,
        "repair_reflection": True,
        "model_slot": "model_a",
    },
    "single_finder": {
        "finder": "single",
        "context_retrieval": True,
        "verifier": True,
        "repair_reflection": True,
        "model_slot": "model_a",
    },
    "no_context": {
        "finder": "dual",
        "context_retrieval": False,
        "verifier": True,
        "repair_reflection": True,
        "model_slot": "model_a",
    },
    "no_verifier": {
        "finder": "dual",
        "context_retrieval": True,
        "verifier": False,
        "repair_reflection": True,
        "model_slot": "model_a",
    },
    "no_reflection": {
        "finder": "dual",
        "context_retrieval": True,
        "verifier": True,
        "repair_reflection": False,
        "model_slot": "model_a",
    },
    "model_b": {
        "finder": "dual",
        "context_retrieval": True,
        "verifier": True,
        "repair_reflection": True,
        "model_slot": "model_b",
    },
}
CONFIGURATION_ORDER = tuple(CONFIGURATION_SPECS)
EXCLUSION_REASONS = {
    "duplicate",
    "flaky",
    "forbidden_repository",
    "nonredistributable",
    "not_reproducible",
    "not_verified",
    "previously_used",
    "requires_network",
    "resource_limit",
    "security_or_secret",
    "unsupported_git",
}
RUN_PURPOSES = {"final_report", "ablation_report"}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REPOSITORY = re.compile(
    r"^[a-z0-9](?:[a-z0-9_.-]{0,98}[a-z0-9])?/"
    r"[a-z0-9](?:[a-z0-9_.-]{0,98}[a-z0-9])?$"
)
CANONICAL_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
FORBIDDEN_ARTIFACT_DIRECTORIES = {"eval", "holdout"}


class PlanValidationError(ValueError):
    """A cohort, configuration, selection, or run plan is unsafe or invalid."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlanValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PlanValidationError(f"non-finite JSON number: {value}")


def safe_artifact_path(path: Path) -> Path:
    """Resolve a path and reject any existing-evaluation directory component."""

    resolved = path.resolve()
    if any(
        part.casefold() in FORBIDDEN_ARTIFACT_DIRECTORIES
        for part in resolved.parts
    ):
        raise PlanValidationError(
            f"existing eval/holdout assets are forbidden inputs or outputs: {resolved}"
        )
    return resolved


def read_artifact_bytes(path: Path) -> bytes:
    """Read bytes only after enforcing the Week 5 artifact boundary."""

    resolved = safe_artifact_path(path)
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise PlanValidationError(f"cannot read artifact {resolved}: {exc}") from exc


def load_json(path: Path) -> dict[str, Any]:
    """Load one strict JSON object, rejecting duplicate keys and NaN/Infinity."""

    resolved = safe_artifact_path(path)
    try:
        value = json.loads(
            resolved.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanValidationError(
            f"cannot read strict JSON {resolved}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PlanValidationError(f"{resolved} must contain one JSON object")
    return value


def load_jsonl_bytes(data: bytes, *, label: str) -> list[dict[str, Any]]:
    """Parse strict UTF-8 JSONL without normalizing the bytes used for hashing."""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanValidationError(f"{label} is not valid UTF-8") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            raise PlanValidationError(f"{label} line {line_number} is blank")
        try:
            row = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, PlanValidationError) as exc:
            raise PlanValidationError(
                f"{label} line {line_number} is invalid JSON: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise PlanValidationError(f"{label} line {line_number} must be an object")
        rows.append(row)
    if not rows:
        raise PlanValidationError(f"{label} must contain at least one row")
    return rows


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def derive_cohort_seed(source_commit: str) -> str:
    _hex(source_commit, 40, "source_commit")
    return sha256_bytes(SEED_DOMAIN + source_commit.encode("ascii"))


def repository_rank(seed: str, repository: str) -> str:
    _hex(seed, 64, "selection seed")
    _repository(repository, "repository")
    return sha256_bytes(f"{seed}\nrepo\n{repository}".encode("ascii"))


def task_rank(seed: str, instance_id: str) -> str:
    _hex(seed, 64, "selection seed")
    _identifier(instance_id, "instance_id")
    return sha256_bytes(f"{seed}\ntask\n{instance_id}".encode("ascii"))


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanValidationError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PlanValidationError(f"{label} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: Iterable[str], label: str) -> None:
    actual = set(value)
    required = set(expected)
    missing = sorted(required - actual)
    unknown = sorted(actual - required)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if unknown:
            details.append(f"unknown={','.join(unknown)}")
        raise PlanValidationError(f"{label} has invalid keys ({'; '.join(details)})")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PlanValidationError(f"{label} must be a non-empty trimmed string")
    if any(ord(char) < 32 for char in value):
        raise PlanValidationError(f"{label} contains control characters")
    return value


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise PlanValidationError(f"{label} must be a boolean")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PlanValidationError(f"{label} must be an integer >= {minimum}")
    return value


def _finite_number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanValidationError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise PlanValidationError(f"{label} must be a finite number >= {minimum}")
    return number


def _hex(value: Any, length: int, label: str) -> str:
    text = _text(value, label)
    pattern = HEX40 if length == 40 else HEX64
    if pattern.fullmatch(text) is None:
        raise PlanValidationError(f"{label} must be {length} lowercase hex characters")
    return text


def _identifier(value: Any, label: str) -> str:
    text = _text(value, label)
    if IDENTIFIER.fullmatch(text) is None:
        raise PlanValidationError(f"{label} is not a canonical identifier")
    return text


def _repository(value: Any, label: str) -> str:
    text = _text(value, label)
    if REPOSITORY.fullmatch(text) is None or text != text.casefold():
        raise PlanValidationError(f"{label} is not canonical lower-case owner/repo")
    return text


def _timestamp(value: Any, label: str) -> str:
    text = _text(value, label)
    if CANONICAL_TIMESTAMP.fullmatch(text) is None:
        raise PlanValidationError(f"{label} must use YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise PlanValidationError(f"{label} is not a valid UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != text:
        raise PlanValidationError(f"{label} is not canonical")
    return text


def _image_digest(value: Any, label: str) -> str:
    text = _text(value, label)
    if not text.startswith("sha256:") or HEX64.fullmatch(text[7:]) is None:
        raise PlanValidationError(f"{label} must be sha256:<64 lowercase hex>")
    return text


def validate_cohort(cohort: dict[str, Any]) -> dict[str, Any]:
    """Validate cohort shape, frozen source identity, roles, and task metadata."""

    _exact_keys(
        cohort,
        {
            "schema_version",
            "cohort_id",
            "materialized",
            "source_commit",
            "dataset",
            "selection",
            "targets",
            "forbidden_repositories",
            "eligibility",
            "tasks",
        },
        "cohort",
    )
    if _integer(cohort["schema_version"], "cohort.schema_version", minimum=1) != 1:
        raise PlanValidationError("unsupported cohort schema_version")
    _identifier(cohort["cohort_id"], "cohort.cohort_id")
    materialized = _boolean(cohort["materialized"], "cohort.materialized")
    source_commit = _hex(cohort["source_commit"], 40, "cohort.source_commit")

    dataset = _object(cohort["dataset"], "cohort.dataset")
    _exact_keys(
        dataset,
        {"name", "revision", "manifest_sha256", "harness_revision"},
        "cohort.dataset",
    )
    if _text(dataset["name"], "cohort.dataset.name") != DATASET_NAME:
        raise PlanValidationError("cohort dataset must be SWE-bench Verified")
    if materialized:
        _hex(dataset["revision"], 40, "cohort.dataset.revision")
        _hex(dataset["manifest_sha256"], 64, "cohort.dataset.manifest_sha256")
        _hex(dataset["harness_revision"], 40, "cohort.dataset.harness_revision")
    else:
        for name in ("revision", "manifest_sha256", "harness_revision"):
            if dataset[name] is not None:
                raise PlanValidationError(
                    f"unmaterialized cohort dataset.{name} must be null"
                )

    selection = _object(cohort["selection"], "cohort.selection")
    _exact_keys(
        selection,
        {
            "method",
            "seed_derivation",
            "seed",
            "selection_log_sha256",
            "outcome_blind",
        },
        "cohort.selection",
    )
    if _text(selection["method"], "cohort.selection.method") != COHORT_METHOD:
        raise PlanValidationError("unsupported cohort selection method")
    if (
        _text(selection["seed_derivation"], "cohort.selection.seed_derivation")
        != SEED_DERIVATION
    ):
        raise PlanValidationError("unsupported cohort seed derivation")
    expected_seed = derive_cohort_seed(source_commit)
    if _hex(selection["seed"], 64, "cohort.selection.seed") != expected_seed:
        raise PlanValidationError("cohort selection seed does not match source_commit")
    _boolean(selection["outcome_blind"], "cohort.selection.outcome_blind")
    if selection["outcome_blind"] is not True:
        raise PlanValidationError("cohort selection must be outcome blind")
    if materialized:
        _hex(
            selection["selection_log_sha256"],
            64,
            "cohort.selection.selection_log_sha256",
        )
    elif selection["selection_log_sha256"] is not None:
        raise PlanValidationError(
            "unmaterialized cohort selection_log_sha256 must be null"
        )

    targets = _object(cohort["targets"], "cohort.targets")
    _exact_keys(
        targets,
        {
            "minimum_total",
            "maximum_total",
            "selected_total",
            "roles",
            "minimum_reporting_repositories",
            "repository_disjoint_roles",
        },
        "cohort.targets",
    )
    expected_targets = {
        "minimum_total": 20,
        "maximum_total": 50,
        "selected_total": 30,
        "minimum_reporting_repositories": 4,
    }
    for name, expected in expected_targets.items():
        if _integer(targets[name], f"cohort.targets.{name}", minimum=1) != expected:
            raise PlanValidationError(f"cohort target {name} must equal {expected}")
    if (
        _boolean(
            targets["repository_disjoint_roles"],
            "cohort.targets.repository_disjoint_roles",
        )
        is not True
    ):
        raise PlanValidationError("cohort roles must be repository-disjoint")
    roles = _object(targets["roles"], "cohort.targets.roles")
    _exact_keys(roles, ROLE_TARGETS, "cohort.targets.roles")
    for role, expected in ROLE_TARGETS.items():
        if _integer(roles[role], f"cohort.targets.roles.{role}", minimum=1) != expected:
            raise PlanValidationError(f"cohort role {role} must target {expected}")

    forbidden_raw = _list(
        cohort["forbidden_repositories"], "cohort.forbidden_repositories"
    )
    forbidden = [
        _repository(value, f"cohort.forbidden_repositories[{index}]")
        for index, value in enumerate(forbidden_raw)
    ]
    if forbidden != sorted(set(forbidden)):
        raise PlanValidationError(
            "cohort forbidden_repositories must be sorted and unique"
        )

    eligibility = _object(cohort["eligibility"], "cohort.eligibility")
    _exact_keys(
        eligibility,
        {
            "verified_only",
            "require_fail_to_pass",
            "require_pass_to_pass",
            "require_red_base_green_gold",
            "require_offline_reproducibility",
            "network_mode",
            "allow_gpu",
            "allow_privileged",
            "allow_host_docker_socket",
            "maximum_seconds",
            "maximum_cpus",
            "maximum_memory_mib",
            "maximum_pids",
            "maximum_writable_mib",
        },
        "cohort.eligibility",
    )
    for name in (
        "verified_only",
        "require_fail_to_pass",
        "require_pass_to_pass",
        "require_red_base_green_gold",
        "require_offline_reproducibility",
    ):
        if _boolean(eligibility[name], f"cohort.eligibility.{name}") is not True:
            raise PlanValidationError(f"cohort eligibility {name} must be true")
    for name in ("allow_gpu", "allow_privileged", "allow_host_docker_socket"):
        if _boolean(eligibility[name], f"cohort.eligibility.{name}") is not False:
            raise PlanValidationError(f"cohort eligibility {name} must be false")
    if _text(eligibility["network_mode"], "cohort.eligibility.network_mode") != "none":
        raise PlanValidationError("cohort task network mode must be none")
    expected_limits = {
        "maximum_seconds": 3600,
        "maximum_cpus": 2,
        "maximum_memory_mib": 4096,
        "maximum_pids": 256,
        "maximum_writable_mib": 20480,
    }
    for name, expected in expected_limits.items():
        if _integer(eligibility[name], f"cohort.eligibility.{name}", minimum=1) != expected:
            raise PlanValidationError(
                f"cohort eligibility {name} must equal {expected}"
            )

    task_rows = _list(cohort["tasks"], "cohort.tasks")
    if not materialized:
        if task_rows:
            raise PlanValidationError("unmaterialized cohort tasks must be empty")
        return cohort

    tasks = [_validate_task(row, index) for index, row in enumerate(task_rows)]
    if len(tasks) != 30:
        raise PlanValidationError("materialized cohort must contain exactly 30 tasks")
    instance_ids = [task["instance_id"] for task in tasks]
    if len(set(instance_ids)) != len(instance_ids):
        raise PlanValidationError("materialized cohort contains duplicate instance_id")
    if any(task["repository"] in forbidden for task in tasks):
        raise PlanValidationError("materialized cohort contains a forbidden repository")
    counts = Counter(task["role"] for task in tasks)
    if counts != Counter(ROLE_TARGETS):
        raise PlanValidationError(
            f"materialized role counts are invalid: {dict(sorted(counts.items()))}"
        )
    repo_roles: dict[str, set[str]] = defaultdict(set)
    repo_counts: Counter[tuple[str, str]] = Counter()
    for task in tasks:
        repo_roles[task["repository"]].add(task["role"])
        repo_counts[(task["repository"], task["role"])] += 1
    overlap = sorted(repo for repo, assigned in repo_roles.items() if len(assigned) != 1)
    if overlap:
        raise PlanValidationError(
            f"repositories overlap roles: {', '.join(overlap)}"
        )
    reporting_repos = sorted(
        repo for repo, assigned in repo_roles.items() if assigned == {"reporting"}
    )
    if len(reporting_repos) < 4:
        raise PlanValidationError("reporting cohort needs at least four repositories")
    for repo in reporting_repos:
        if repo_counts[(repo, "reporting")] < 3:
            raise PlanValidationError(
                f"reporting repository {repo} has fewer than three tasks"
            )
    for role in ROLE_TARGETS:
        bands = {task["size_band"] for task in tasks if task["role"] == role}
        if len(bands) < 2:
            raise PlanValidationError(f"{role} tasks need at least two size bands")
    return cohort


def _validate_task(value: Any, index: int) -> dict[str, Any]:
    task = _object(value, f"cohort.tasks[{index}]")
    _exact_keys(
        task,
        {
            "instance_id",
            "repository",
            "role",
            "base_sha",
            "base_tree_sha256",
            "source_snapshot_sha256",
            "harness_task_sha256",
            "image_digest",
            "fail_to_pass_count",
            "pass_to_pass_count",
            "size_band",
            "repository_rank_sha256",
            "task_rank_sha256",
        },
        f"cohort.tasks[{index}]",
    )
    _identifier(task["instance_id"], f"cohort.tasks[{index}].instance_id")
    _repository(task["repository"], f"cohort.tasks[{index}].repository")
    role = _text(task["role"], f"cohort.tasks[{index}].role")
    if role not in ROLE_TARGETS:
        raise PlanValidationError(f"cohort.tasks[{index}].role is invalid")
    _hex(task["base_sha"], 40, f"cohort.tasks[{index}].base_sha")
    for name in (
        "base_tree_sha256",
        "source_snapshot_sha256",
        "harness_task_sha256",
        "repository_rank_sha256",
        "task_rank_sha256",
    ):
        _hex(task[name], 64, f"cohort.tasks[{index}].{name}")
    _image_digest(task["image_digest"], f"cohort.tasks[{index}].image_digest")
    _integer(
        task["fail_to_pass_count"],
        f"cohort.tasks[{index}].fail_to_pass_count",
        minimum=1,
    )
    _integer(
        task["pass_to_pass_count"],
        f"cohort.tasks[{index}].pass_to_pass_count",
        minimum=0,
    )
    if _text(task["size_band"], f"cohort.tasks[{index}].size_band") not in {
        "small",
        "medium",
        "large",
    }:
        raise PlanValidationError(f"cohort.tasks[{index}].size_band is invalid")
    return task


def validate_selection(
    cohort: dict[str, Any], selection_log_bytes: bytes
) -> list[dict[str, Any]]:
    """Verify exact selection-log bytes, ranks, deterministic roles, and tasks."""

    validate_cohort(cohort)
    if cohort["materialized"] is not True:
        raise PlanValidationError("selection verification needs a materialized cohort")
    expected_log_hash = cohort["selection"]["selection_log_sha256"]
    if sha256_bytes(selection_log_bytes) != expected_log_hash:
        raise PlanValidationError("selection log byte hash does not match cohort")
    rows = load_jsonl_bytes(selection_log_bytes, label="selection log")
    normalized = [
        _validate_selection_row(row, index, cohort["selection"]["seed"])
        for index, row in enumerate(rows)
    ]
    ids = [row["instance_id"] for row in normalized]
    if len(set(ids)) != len(ids):
        raise PlanValidationError("selection log contains duplicate instance_id")

    forbidden = set(cohort["forbidden_repositories"])
    for row in normalized:
        if row["repository"] in forbidden:
            if (
                row["eligible"]
                or row["selected"]
                or row["exclusion_reason"] != "forbidden_repository"
            ):
                raise PlanValidationError(
                    "forbidden repositories must be explicitly excluded"
                )

    eligible_by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in normalized:
        if row["eligible"]:
            eligible_by_repo[row["repository"]].append(row)
    allocatable: list[tuple[str, str, list[dict[str, Any]]]] = []
    for repo, candidates in eligible_by_repo.items():
        ordered = sorted(candidates, key=lambda item: item["task_rank_sha256"])
        selected = ordered[:TASKS_PER_REPOSITORY]
        if len(selected) < TASKS_PER_REPOSITORY:
            continue
        if len({item["size_band"] for item in selected}) < 2:
            continue
        allocatable.append((repository_rank(cohort["selection"]["seed"], repo), repo, selected))
    allocatable.sort()
    required_repositories = sum(REPOSITORIES_PER_ROLE.values())
    if len(allocatable) < required_repositories:
        raise PlanValidationError(
            "selection log has fewer than six allocatable repository groups"
        )

    expected_role_by_repo: dict[str, str] = {}
    expected_selected: set[str] = set()
    offset = 0
    for role in ROLE_ORDER:
        count = REPOSITORIES_PER_ROLE[role]
        assigned = allocatable[offset : offset + count]
        offset += count
        for _rank, repo, selected in assigned:
            expected_role_by_repo[repo] = role
            expected_selected.update(item["instance_id"] for item in selected)

    for row in normalized:
        expected_role = expected_role_by_repo.get(row["repository"])
        if row["role"] != expected_role:
            raise PlanValidationError(
                f"selection role mismatch for {row['instance_id']}"
            )
        if row["selected"] != (row["instance_id"] in expected_selected):
            raise PlanValidationError(
                f"selection flag mismatch for {row['instance_id']}"
            )

    tasks_by_id = {task["instance_id"]: task for task in cohort["tasks"]}
    if set(tasks_by_id) != expected_selected:
        raise PlanValidationError(
            "materialized cohort tasks differ from deterministic selected set"
        )
    rows_by_id = {row["instance_id"]: row for row in normalized}
    for instance_id, task in tasks_by_id.items():
        row = rows_by_id[instance_id]
        fields = (
            "repository",
            "role",
            "size_band",
            "repository_rank_sha256",
            "task_rank_sha256",
        )
        if any(task[name] != row[name] for name in fields):
            raise PlanValidationError(
                f"materialized task metadata differs from selection log: {instance_id}"
            )
    return normalized


def _validate_selection_row(
    value: Any, index: int, seed: str
) -> dict[str, Any]:
    row = _object(value, f"selection[{index}]")
    _exact_keys(
        row,
        {
            "instance_id",
            "repository",
            "eligible",
            "exclusion_reason",
            "selected",
            "role",
            "size_band",
            "repository_rank_sha256",
            "task_rank_sha256",
        },
        f"selection[{index}]",
    )
    instance_id = _identifier(
        row["instance_id"], f"selection[{index}].instance_id"
    )
    repo = _repository(row["repository"], f"selection[{index}].repository")
    eligible = _boolean(row["eligible"], f"selection[{index}].eligible")
    selected = _boolean(row["selected"], f"selection[{index}].selected")
    exclusion = row["exclusion_reason"]
    role = row["role"]
    if eligible:
        if exclusion is not None:
            raise PlanValidationError(
                f"selection[{index}] eligible row needs null exclusion_reason"
            )
    else:
        if (
            _optional_text(exclusion, f"selection[{index}].exclusion_reason")
            not in EXCLUSION_REASONS
        ):
            raise PlanValidationError(
                f"selection[{index}] has invalid exclusion_reason"
            )
        if selected or role is not None:
            raise PlanValidationError(
                f"selection[{index}] excluded row cannot be selected or assigned"
            )
    if role is not None:
        role = _text(role, f"selection[{index}].role")
        if role not in ROLE_TARGETS:
            raise PlanValidationError(f"selection[{index}] role is invalid")
    size_band = _text(row["size_band"], f"selection[{index}].size_band")
    if size_band not in {"small", "medium", "large"}:
        raise PlanValidationError(f"selection[{index}] size_band is invalid")
    expected_repo_rank = repository_rank(seed, repo)
    expected_task_rank = task_rank(seed, instance_id)
    if (
        _hex(
            row["repository_rank_sha256"],
            64,
            f"selection[{index}].repository_rank_sha256",
        )
        != expected_repo_rank
    ):
        raise PlanValidationError(f"selection[{index}] repository rank mismatch")
    if (
        _hex(
            row["task_rank_sha256"],
            64,
            f"selection[{index}].task_rank_sha256",
        )
        != expected_task_rank
    ):
        raise PlanValidationError(f"selection[{index}] task rank mismatch")
    return row


def validate_config_plan(
    config: dict[str, Any],
    *,
    expected_cohort_id: str | None = None,
    expected_source_commit: str | None = None,
) -> dict[str, Any]:
    _exact_keys(
        config,
        {
            "schema_version",
            "plan_id",
            "cohort_id",
            "source_commit",
            "primary_configuration_id",
            "models_frozen",
            "configurations",
            "model_slots",
            "per_run_budget",
            "docker",
            "reporting_budget",
            "week5_paid_ceiling_microusd",
            "bootstrap",
        },
        "config plan",
    )
    if _integer(config["schema_version"], "config.schema_version", minimum=1) != 1:
        raise PlanValidationError("unsupported config schema_version")
    _identifier(config["plan_id"], "config.plan_id")
    cohort_id = _identifier(config["cohort_id"], "config.cohort_id")
    source_commit = _hex(config["source_commit"], 40, "config.source_commit")
    if expected_cohort_id is not None and cohort_id != expected_cohort_id:
        raise PlanValidationError("config cohort_id does not match cohort")
    if expected_source_commit is not None and source_commit != expected_source_commit:
        raise PlanValidationError("config source_commit does not match cohort")
    if (
        _identifier(
            config["primary_configuration_id"],
            "config.primary_configuration_id",
        )
        != "primary"
    ):
        raise PlanValidationError("primary configuration must be primary")
    models_frozen = _boolean(config["models_frozen"], "config.models_frozen")

    rows = _list(config["configurations"], "config.configurations")
    if len(rows) != len(CONFIGURATION_ORDER):
        raise PlanValidationError("config plan must contain six configurations")
    observed: list[str] = []
    for index, value in enumerate(rows):
        row = _object(value, f"config.configurations[{index}]")
        _exact_keys(
            row,
            {
                "configuration_id",
                "finder",
                "context_retrieval",
                "verifier",
                "repair_reflection",
                "model_slot",
            },
            f"config.configurations[{index}]",
        )
        config_id = _identifier(
            row["configuration_id"],
            f"config.configurations[{index}].configuration_id",
        )
        observed.append(config_id)
        if config_id not in CONFIGURATION_SPECS:
            raise PlanValidationError(f"unknown configuration: {config_id}")
        expected_spec = CONFIGURATION_SPECS[config_id]
        for name, expected_value in expected_spec.items():
            actual = row[name]
            if isinstance(expected_value, bool):
                _boolean(actual, f"config {config_id}.{name}")
            else:
                _text(actual, f"config {config_id}.{name}")
            if actual != expected_value:
                raise PlanValidationError(
                    f"configuration {config_id}.{name} must equal {expected_value!r}"
                )
    if tuple(observed) != CONFIGURATION_ORDER:
        raise PlanValidationError("configurations are not in preregistered order")

    model_slots = _object(config["model_slots"], "config.model_slots")
    _exact_keys(model_slots, {"model_a", "model_b"}, "config.model_slots")
    bound_models: list[tuple[str, str, str]] = []
    for slot in ("model_a", "model_b"):
        model = _object(model_slots[slot], f"config.model_slots.{slot}")
        _exact_keys(
            model,
            {"provider", "model", "pricing_revision"},
            f"config.model_slots.{slot}",
        )
        if models_frozen:
            bound_models.append(
                (
                    _text(model["provider"], f"config.model_slots.{slot}.provider"),
                    _text(model["model"], f"config.model_slots.{slot}.model"),
                    _text(
                        model["pricing_revision"],
                        f"config.model_slots.{slot}.pricing_revision",
                    ),
                )
            )
        else:
            if any(model[name] is not None for name in model):
                raise PlanValidationError(
                    "unfrozen model slots must contain only null identities"
                )
    if models_frozen and bound_models[0][:2] == bound_models[1][:2]:
        raise PlanValidationError("model A and model B must bind different model identities")

    budget = _object(config["per_run_budget"], "config.per_run_budget")
    expected_budget = {
        "total_seconds": 3600,
        "total_tokens": 120000,
        "total_cost_microusd": 500000,
        "tool_calls": 150,
        "repair_attempts": 2,
        "test_command_invocations": 10,
        "command_seconds": 600,
        "command_output_bytes": 1048576,
    }
    _exact_keys(budget, expected_budget, "config.per_run_budget")
    for name, budget_value in expected_budget.items():
        if (
            _integer(budget[name], f"config.per_run_budget.{name}", minimum=1)
            != budget_value
        ):
            raise PlanValidationError(
                f"config per-run budget {name} must equal {budget_value}"
            )

    docker = _object(config["docker"], "config.docker")
    expected_docker = {
        "cpus": 2,
        "memory_mib": 4096,
        "pids": 256,
        "writable_mib": 20480,
        "network_mode": "none",
        "read_only_root": True,
        "run_as_non_root": True,
        "cap_drop_all": True,
        "no_new_privileges": True,
        "maximum_parallel_runs": 2,
    }
    _exact_keys(docker, expected_docker, "config.docker")
    for name, docker_value in expected_docker.items():
        actual = docker[name]
        if isinstance(docker_value, bool):
            _boolean(actual, f"config.docker.{name}")
        elif isinstance(docker_value, int):
            _integer(actual, f"config.docker.{name}", minimum=1)
        else:
            _text(actual, f"config.docker.{name}")
        if actual != docker_value:
            raise PlanValidationError(
                f"config docker {name} must equal {docker_value!r}"
            )

    reporting_budget = _object(
        config["reporting_budget"], "config.reporting_budget"
    )
    expected_reporting = {
        "tasks": 20,
        "configurations": 6,
        "attempts": 120,
        "cost_ceiling_microusd": 60000000,
        "container_hour_ceiling": 120,
    }
    _exact_keys(reporting_budget, expected_reporting, "config.reporting_budget")
    for name, reporting_value in expected_reporting.items():
        if (
            _integer(
                reporting_budget[name],
                f"config.reporting_budget.{name}",
                minimum=1,
            )
            != reporting_value
        ):
            raise PlanValidationError(
                f"config reporting budget {name} must equal {reporting_value}"
            )
    if (
        _integer(
            config["week5_paid_ceiling_microusd"],
            "config.week5_paid_ceiling_microusd",
            minimum=1,
        )
        != 80000000
    ):
        raise PlanValidationError("Week 5 paid ceiling must equal 80000000 micro-USD")

    bootstrap = _object(config["bootstrap"], "config.bootstrap")
    _exact_keys(
        bootstrap,
        {"method", "seed", "replicates", "confidence"},
        "config.bootstrap",
    )
    if (
        _text(bootstrap["method"], "config.bootstrap.method")
        != "repository_stratified_task_percentile_v1"
    ):
        raise PlanValidationError("unsupported bootstrap method")
    _integer(bootstrap["seed"], "config.bootstrap.seed", minimum=0)
    if _integer(bootstrap["replicates"], "config.bootstrap.replicates", minimum=1) < 10000:
        raise PlanValidationError("final bootstrap requires at least 10000 replicates")
    confidence = _finite_number(
        bootstrap["confidence"], "config.bootstrap.confidence"
    )
    if confidence != 0.95:
        raise PlanValidationError("bootstrap confidence must equal 0.95")
    return config


def validate_plans(
    cohort: dict[str, Any],
    config: dict[str, Any],
    *,
    selection_log_bytes: bytes | None = None,
) -> dict[str, Any]:
    validate_cohort(cohort)
    validate_config_plan(
        config,
        expected_cohort_id=cohort["cohort_id"],
        expected_source_commit=cohort["source_commit"],
    )
    if cohort["materialized"]:
        if selection_log_bytes is None:
            raise PlanValidationError(
                "materialized cohort validation requires the selection log"
            )
        validate_selection(cohort, selection_log_bytes)
    elif selection_log_bytes is not None:
        raise PlanValidationError(
            "unmaterialized cohort must not receive a selection log"
        )
    return {
        "valid": True,
        "materialized": cohort["materialized"],
        "cohort_id": cohort["cohort_id"],
        "selected_tasks": len(cohort["tasks"]),
        "configurations": len(config["configurations"]),
        "planned_reporting_attempts": config["reporting_budget"]["attempts"],
        "models_frozen": config["models_frozen"],
        "cohort_sha256": canonical_sha256(cohort),
        "config_sha256": canonical_sha256(config),
    }


def generate_run_plan(
    cohort: dict[str, Any],
    config: dict[str, Any],
    *,
    selection_log_bytes: bytes,
    agent_source_commit: str,
    gold_freeze_commit: str,
    freeze_attestation_sha256: str,
    created_at: str,
) -> dict[str, Any]:
    """Create the complete deterministic sealed reporting matrix."""

    plan = _build_run_plan(
        cohort,
        config,
        selection_log_bytes=selection_log_bytes,
        agent_source_commit=agent_source_commit,
        gold_freeze_commit=gold_freeze_commit,
        freeze_attestation_sha256=freeze_attestation_sha256,
        created_at=created_at,
    )
    validate_run_plan(
        plan,
        cohort,
        config,
        selection_log_bytes=selection_log_bytes,
    )
    return plan


def _run_identity(run_plan_id: str, instance_id: str, configuration_id: str) -> str:
    material = (
        run_plan_id + "\x00" + instance_id + "\x00" + configuration_id
    ).encode("utf-8")
    return sha256_bytes(IDENTITY_DOMAIN + material)[:24]


def _slug(value: str, *, maximum: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise PlanValidationError("cannot derive a safe identifier slug")
    return slug[:maximum].rstrip("-")


def validate_run_plan(
    plan: dict[str, Any],
    cohort: dict[str, Any],
    config: dict[str, Any],
    *,
    selection_log_bytes: bytes,
) -> dict[str, Any]:
    """Validate a run plan by regenerating it from its immutable inputs."""

    _exact_keys(
        plan,
        {
            "schema_version",
            "run_plan_id",
            "cohort_id",
            "config_plan_id",
            "source_commit",
            "agent_source_commit",
            "gold_freeze_commit",
            "freeze_attestation_sha256",
            "created_at",
            "cohort_sha256",
            "config_sha256",
            "selection_log_sha256",
            "dataset_manifest_sha256",
            "harness_revision",
            "rows",
        },
        "run plan",
    )
    if _integer(plan["schema_version"], "run_plan.schema_version", minimum=1) != 1:
        raise PlanValidationError("unsupported run-plan schema_version")
    _hex(plan["run_plan_id"], 64, "run_plan.run_plan_id")
    _identifier(plan["cohort_id"], "run_plan.cohort_id")
    _identifier(plan["config_plan_id"], "run_plan.config_plan_id")
    _hex(plan["source_commit"], 40, "run_plan.source_commit")
    _hex(plan["agent_source_commit"], 40, "run_plan.agent_source_commit")
    _hex(plan["gold_freeze_commit"], 40, "run_plan.gold_freeze_commit")
    _hex(
        plan["freeze_attestation_sha256"],
        64,
        "run_plan.freeze_attestation_sha256",
    )
    _timestamp(plan["created_at"], "run_plan.created_at")
    for name in (
        "cohort_sha256",
        "config_sha256",
        "selection_log_sha256",
        "dataset_manifest_sha256",
    ):
        _hex(plan[name], 64, f"run_plan.{name}")
    _hex(plan["harness_revision"], 40, "run_plan.harness_revision")
    rows = _list(plan["rows"], "run_plan.rows")
    if len(rows) != 120:
        raise PlanValidationError("reporting run plan must contain exactly 120 rows")
    for index, row in enumerate(rows):
        _validate_run_plan_row(row, index)
    unique_fields = (
        "run_id",
        "task_branch",
        "worktree_id",
        "worktree_path_token_sha256",
        "container_name",
        "judge_container_name",
        "state_id",
    )
    for field in unique_fields:
        values = [row[field] for row in rows]
        if len(set(values)) != len(values):
            raise PlanValidationError(f"run plan reuses {field}")
    pairs = [(row["instance_id"], row["configuration_id"]) for row in rows]
    if len(set(pairs)) != len(pairs):
        raise PlanValidationError("run plan contains duplicate task/config pairs")

    expected = _build_run_plan(
        cohort,
        config,
        selection_log_bytes=selection_log_bytes,
        agent_source_commit=plan["agent_source_commit"],
        gold_freeze_commit=plan["gold_freeze_commit"],
        freeze_attestation_sha256=plan["freeze_attestation_sha256"],
        created_at=plan["created_at"],
    )
    if canonical_json_bytes(plan) != canonical_json_bytes(expected):
        raise PlanValidationError("run plan differs from deterministic regeneration")
    return plan


def _build_run_plan(
    cohort: dict[str, Any],
    config: dict[str, Any],
    *,
    selection_log_bytes: bytes,
    agent_source_commit: str,
    gold_freeze_commit: str,
    freeze_attestation_sha256: str,
    created_at: str,
) -> dict[str, Any]:
    """Build the canonical matrix without recursively validating its output."""

    validate_plans(cohort, config, selection_log_bytes=selection_log_bytes)
    if not cohort["materialized"]:
        raise PlanValidationError("run-plan generation needs a materialized cohort")
    if not config["models_frozen"]:
        raise PlanValidationError("run-plan generation needs frozen exact models")
    agent_source_commit = _hex(agent_source_commit, 40, "agent_source_commit")
    gold_freeze_commit = _hex(gold_freeze_commit, 40, "gold_freeze_commit")
    freeze_attestation_sha256 = _hex(
        freeze_attestation_sha256, 64, "freeze_attestation_sha256"
    )
    created_at = _timestamp(created_at, "created_at")
    cohort_hash = canonical_sha256(cohort)
    config_hash = canonical_sha256(config)
    material = (
        cohort_hash
        + "\n"
        + config_hash
        + "\n"
        + agent_source_commit
        + "\n"
        + gold_freeze_commit
        + "\n"
        + freeze_attestation_sha256
        + "\n"
        + created_at
    ).encode("ascii")
    run_plan_id = sha256_bytes(RUN_PLAN_DOMAIN + material)
    configs = {row["configuration_id"]: row for row in config["configurations"]}
    rows: list[dict[str, Any]] = []
    for task in sorted(
        (item for item in cohort["tasks"] if item["role"] == "reporting"),
        key=lambda item: item["instance_id"],
    ):
        for configuration_id in CONFIGURATION_ORDER:
            configuration = configs[configuration_id]
            identity = _run_identity(
                run_plan_id, task["instance_id"], configuration_id
            )
            budget = dict(config["per_run_budget"])
            budget["repair_attempts"] = (
                config["per_run_budget"]["repair_attempts"]
                if configuration["repair_reflection"]
                else 0
            )
            rows.append(
                {
                    "schema_version": 1,
                    "run_plan_id": run_plan_id,
                    "instance_id": task["instance_id"],
                    "repository": task["repository"],
                    "configuration_id": configuration_id,
                    "configuration_sha256": canonical_sha256(configuration),
                    "purpose": (
                        "final_report"
                        if configuration_id == "primary"
                        else "ablation_report"
                    ),
                    "run_id": (
                        f"w5-{_slug(configuration_id, maximum=20)}-{identity}"
                    ),
                    "task_branch": (
                        f"repair/{_slug(task['instance_id'], maximum=32)}-"
                        f"{identity[:12]}"
                    ),
                    "worktree_id": f"wt-{identity}",
                    "worktree_path_token_sha256": sha256_bytes(
                        PATH_DOMAIN + identity.encode("ascii")
                    ),
                    "container_name": f"crag-w5-{identity}",
                    "judge_container_name": f"swebench-judge-{identity}",
                    "state_id": f"state-{identity}",
                    "base_sha": task["base_sha"],
                    "base_tree_sha256": task["base_tree_sha256"],
                    "source_snapshot_sha256": task["source_snapshot_sha256"],
                    "harness_task_sha256": task["harness_task_sha256"],
                    "image_digest": task["image_digest"],
                    "fail_to_pass_count": task["fail_to_pass_count"],
                    "pass_to_pass_count": task["pass_to_pass_count"],
                    "budget": budget,
                    "isolation": dict(config["docker"]),
                }
            )
    return {
        "schema_version": 1,
        "run_plan_id": run_plan_id,
        "cohort_id": cohort["cohort_id"],
        "config_plan_id": config["plan_id"],
        "source_commit": cohort["source_commit"],
        "agent_source_commit": agent_source_commit,
        "gold_freeze_commit": gold_freeze_commit,
        "freeze_attestation_sha256": freeze_attestation_sha256,
        "created_at": created_at,
        "cohort_sha256": cohort_hash,
        "config_sha256": config_hash,
        "selection_log_sha256": sha256_bytes(selection_log_bytes),
        "dataset_manifest_sha256": cohort["dataset"]["manifest_sha256"],
        "harness_revision": cohort["dataset"]["harness_revision"],
        "rows": rows,
    }


def _validate_run_plan_row(value: Any, index: int) -> dict[str, Any]:
    row = _object(value, f"run_plan.rows[{index}]")
    _exact_keys(
        row,
        {
            "schema_version",
            "run_plan_id",
            "instance_id",
            "repository",
            "configuration_id",
            "configuration_sha256",
            "purpose",
            "run_id",
            "task_branch",
            "worktree_id",
            "worktree_path_token_sha256",
            "container_name",
            "judge_container_name",
            "state_id",
            "base_sha",
            "base_tree_sha256",
            "source_snapshot_sha256",
            "harness_task_sha256",
            "image_digest",
            "fail_to_pass_count",
            "pass_to_pass_count",
            "budget",
            "isolation",
        },
        f"run_plan.rows[{index}]",
    )
    if _integer(row["schema_version"], f"run_plan.rows[{index}].schema_version", minimum=1) != 1:
        raise PlanValidationError("unsupported run-plan row schema_version")
    _hex(row["run_plan_id"], 64, f"run_plan.rows[{index}].run_plan_id")
    _identifier(row["instance_id"], f"run_plan.rows[{index}].instance_id")
    _repository(row["repository"], f"run_plan.rows[{index}].repository")
    config_id = _identifier(
        row["configuration_id"], f"run_plan.rows[{index}].configuration_id"
    )
    if config_id not in CONFIGURATION_SPECS:
        raise PlanValidationError(f"run-plan row {index} has unknown configuration")
    _hex(
        row["configuration_sha256"],
        64,
        f"run_plan.rows[{index}].configuration_sha256",
    )
    if _text(row["purpose"], f"run_plan.rows[{index}].purpose") not in RUN_PURPOSES:
        raise PlanValidationError(f"run-plan row {index} has invalid purpose")
    for name in (
        "run_id",
        "worktree_id",
        "container_name",
        "judge_container_name",
        "state_id",
    ):
        _identifier(row[name], f"run_plan.rows[{index}].{name}")
    task_branch = _text(row["task_branch"], f"run_plan.rows[{index}].task_branch")
    if not task_branch.startswith("repair/") or ".." in task_branch:
        raise PlanValidationError(f"run-plan row {index} has unsafe task_branch")
    for name in (
        "worktree_path_token_sha256",
        "base_tree_sha256",
        "source_snapshot_sha256",
        "harness_task_sha256",
    ):
        _hex(row[name], 64, f"run_plan.rows[{index}].{name}")
    _hex(row["base_sha"], 40, f"run_plan.rows[{index}].base_sha")
    _image_digest(row["image_digest"], f"run_plan.rows[{index}].image_digest")
    _integer(
        row["fail_to_pass_count"],
        f"run_plan.rows[{index}].fail_to_pass_count",
        minimum=1,
    )
    _integer(
        row["pass_to_pass_count"],
        f"run_plan.rows[{index}].pass_to_pass_count",
        minimum=0,
    )
    budget = _object(row["budget"], f"run_plan.rows[{index}].budget")
    expected_budget_keys = {
        "total_seconds",
        "total_tokens",
        "total_cost_microusd",
        "tool_calls",
        "repair_attempts",
        "test_command_invocations",
        "command_seconds",
        "command_output_bytes",
    }
    _exact_keys(budget, expected_budget_keys, f"run_plan.rows[{index}].budget")
    for name in expected_budget_keys:
        _integer(
            budget[name],
            f"run_plan.rows[{index}].budget.{name}",
            minimum=0 if name == "repair_attempts" else 1,
        )
    isolation = _object(row["isolation"], f"run_plan.rows[{index}].isolation")
    validate_isolation_policy(isolation, label=f"run_plan.rows[{index}].isolation")
    return row


def validate_isolation_policy(value: dict[str, Any], *, label: str) -> dict[str, Any]:
    expected = {
        "cpus": 2,
        "memory_mib": 4096,
        "pids": 256,
        "writable_mib": 20480,
        "network_mode": "none",
        "read_only_root": True,
        "run_as_non_root": True,
        "cap_drop_all": True,
        "no_new_privileges": True,
        "maximum_parallel_runs": 2,
    }
    _exact_keys(value, expected, label)
    for name, expected_value in expected.items():
        actual = value[name]
        if isinstance(expected_value, bool):
            _boolean(actual, f"{label}.{name}")
        elif isinstance(expected_value, int):
            _integer(actual, f"{label}.{name}", minimum=1)
        else:
            _text(actual, f"{label}.{name}")
        if actual != expected_value:
            raise PlanValidationError(
                f"{label}.{name} must equal {expected_value!r}"
            )
    return value


def _write_json(path: Path, value: Any) -> None:
    safe_artifact_path(path).write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline SWE-bench Repair cohort and run-plan validator"
    )
    actions = parser.add_subparsers(dest="action", required=True)

    validate = actions.add_parser("validate-plans")
    validate.add_argument("--cohort", type=Path, required=True)
    validate.add_argument("--config", type=Path, required=True)
    validate.add_argument("--selection-log", type=Path)

    verify = actions.add_parser("verify-selection")
    verify.add_argument("--cohort", type=Path, required=True)
    verify.add_argument("--selection-log", type=Path, required=True)

    generate = actions.add_parser("generate-run-plan")
    generate.add_argument("--cohort", type=Path, required=True)
    generate.add_argument("--config", type=Path, required=True)
    generate.add_argument("--selection-log", type=Path, required=True)
    generate.add_argument("--agent-source-commit", required=True)
    generate.add_argument("--gold-freeze-commit", required=True)
    generate.add_argument("--freeze-attestation-sha256", required=True)
    generate.add_argument("--created-at", required=True)
    generate.add_argument("--out", type=Path, required=True)

    check = actions.add_parser("validate-run-plan")
    check.add_argument("--cohort", type=Path, required=True)
    check.add_argument("--config", type=Path, required=True)
    check.add_argument("--selection-log", type=Path, required=True)
    check.add_argument("--run-plan", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        cohort = load_json(args.cohort)
        if args.action == "validate-plans":
            config = load_json(args.config)
            selection = (
                None
                if args.selection_log is None
                else read_artifact_bytes(args.selection_log)
            )
            result = validate_plans(
                cohort, config, selection_log_bytes=selection
            )
        elif args.action == "verify-selection":
            selection_bytes = read_artifact_bytes(args.selection_log)
            rows = validate_selection(cohort, selection_bytes)
            result = {
                "valid": True,
                "rows": len(rows),
                "selected": sum(1 for row in rows if row["selected"]),
                "selection_log_sha256": sha256_bytes(selection_bytes),
            }
        elif args.action == "generate-run-plan":
            config = load_json(args.config)
            plan = generate_run_plan(
                cohort,
                config,
                selection_log_bytes=read_artifact_bytes(args.selection_log),
                agent_source_commit=args.agent_source_commit,
                gold_freeze_commit=args.gold_freeze_commit,
                freeze_attestation_sha256=args.freeze_attestation_sha256,
                created_at=args.created_at,
            )
            _write_json(args.out, plan)
            result = {
                "valid": True,
                "run_plan_id": plan["run_plan_id"],
                "rows": len(plan["rows"]),
                "out": str(args.out),
            }
        else:
            config = load_json(args.config)
            plan = load_json(args.run_plan)
            validate_run_plan(
                plan,
                cohort,
                config,
                selection_log_bytes=read_artifact_bytes(args.selection_log),
            )
            result = {
                "valid": True,
                "run_plan_id": plan["run_plan_id"],
                "rows": len(plan["rows"]),
            }
    except (OSError, PlanValidationError) as exc:
        parser.exit(2, f"repair evaluation plan refused: {exc}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
