"""Strict offline metrics for the Week 5 SWE-bench Repair evaluation."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
from typing import Any, Sequence

from swebench_repair_runner import (
    CONFIGURATION_ORDER,
    PlanValidationError,
    _boolean,
    _exact_keys,
    _hex,
    _identifier,
    _image_digest,
    _integer,
    _object,
    _repository,
    _text,
    _timestamp,
    canonical_sha256,
    load_json,
    load_jsonl_bytes,
    read_artifact_bytes,
    safe_artifact_path,
    sha256_bytes,
    validate_run_plan,
)


METRIC_VERSION = 1
RUN_STATUSES = {
    "approval_rejected",
    "budget_exhausted",
    "cleanup_quarantined",
    "completed",
    "hard_failure",
    "model_failure",
    "policy_violation",
    "sandbox_failure",
    "test_failure",
    "timeout",
}
CLEANUP_STATUSES = {"not_created", "removed", "quarantined"}
BOOTSTRAP_METRICS = (
    "pass_at_1",
    "mean_cost_microusd",
    "p50_latency_milliseconds",
    "p95_latency_milliseconds",
    "mean_tool_calls",
    "task_test_failure_rate",
    "unauthorized_operation_task_rate",
)


class EvaluationValidationError(PlanValidationError):
    """Run evidence is incomplete, contradictory, or outside the frozen plan."""


def _parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def _nullable_hex(value: Any, length: int, label: str) -> str | None:
    if value is None:
        return None
    return _hex(value, length, label)


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": None if denominator == 0 else numerator / denominator,
    }


def _mean(values: Sequence[int | float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def percentile(values: Sequence[int | float], probability: float) -> float | None:
    """Linear-interpolation percentile used by resources and bootstrap."""

    if not values:
        return None
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("percentile probability must be within [0, 1]")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _validate_actual_isolation(
    value: Any,
    *,
    index: int,
    plan_row: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    label = f"runs[{index}].isolation"
    isolation = _object(value, label)
    _exact_keys(
        isolation,
        {
            "worktree_id",
            "worktree_created",
            "task_branch",
            "container_name",
            "container_started",
            "judge_container_name",
            "judge_container_started",
            "state_id",
            "image_digest",
            "network_mode",
            "read_only_root",
            "run_as_non_root",
            "cap_drop_all",
            "no_new_privileges",
            "pids",
            "cpus",
            "memory_mib",
            "writable_mib",
            "writable_mounts",
            "original_checkout_unchanged",
            "cleanup_status",
        },
        label,
    )
    for name in (
        "worktree_id",
        "container_name",
        "judge_container_name",
        "state_id",
    ):
        if _identifier(isolation[name], f"{label}.{name}") != plan_row[name]:
            raise EvaluationValidationError(f"{label}.{name} differs from run plan")
    branch = _text(isolation["task_branch"], f"{label}.task_branch")
    if branch != plan_row["task_branch"]:
        raise EvaluationValidationError(f"{label}.task_branch differs from run plan")
    if (
        _image_digest(isolation["image_digest"], f"{label}.image_digest")
        != plan_row["image_digest"]
    ):
        raise EvaluationValidationError(f"{label}.image_digest differs from run plan")
    worktree_created = _boolean(
        isolation["worktree_created"], f"{label}.worktree_created"
    )
    container_started = _boolean(
        isolation["container_started"], f"{label}.container_started"
    )
    judge_started = _boolean(
        isolation["judge_container_started"], f"{label}.judge_container_started"
    )
    for name in (
        "read_only_root",
        "run_as_non_root",
        "cap_drop_all",
        "no_new_privileges",
        "original_checkout_unchanged",
    ):
        actual = _boolean(isolation[name], f"{label}.{name}")
        if name != "original_checkout_unchanged" and actual is not True:
            raise EvaluationValidationError(f"{label}.{name} must be true")
    if isolation["original_checkout_unchanged"] is not True:
        raise EvaluationValidationError(f"{label} cannot prove original checkout unchanged")
    if _text(isolation["network_mode"], f"{label}.network_mode") != "none":
        raise EvaluationValidationError(f"{label}.network_mode must be none")
    expected_numbers = {
        "pids": plan_row["isolation"]["pids"],
        "cpus": plan_row["isolation"]["cpus"],
        "memory_mib": plan_row["isolation"]["memory_mib"],
        "writable_mib": plan_row["isolation"]["writable_mib"],
        "writable_mounts": 1 if container_started else 0,
    }
    for name, expected_number in expected_numbers.items():
        numeric_actual = _integer(isolation[name], f"{label}.{name}", minimum=0)
        if numeric_actual != expected_number:
            raise EvaluationValidationError(
                f"{label}.{name} must equal frozen value {expected_number}"
            )
    cleanup_status = _text(isolation["cleanup_status"], f"{label}.cleanup_status")
    if cleanup_status not in CLEANUP_STATUSES:
        raise EvaluationValidationError(f"{label}.cleanup_status is invalid")
    if not worktree_created:
        if container_started or judge_started or cleanup_status != "not_created":
            raise EvaluationValidationError(
                f"{label} has activity without a created worktree"
            )
        if status not in {"hard_failure", "sandbox_failure"}:
            raise EvaluationValidationError(
                f"{label} missing worktree is only valid for setup failures"
            )
    elif cleanup_status == "not_created":
        raise EvaluationValidationError(
            f"{label} created worktree needs removed/quarantined cleanup"
        )
    if cleanup_status == "quarantined" and status != "cleanup_quarantined":
        raise EvaluationValidationError(
            f"{label} quarantined cleanup needs cleanup_quarantined status"
        )
    if status == "cleanup_quarantined" and cleanup_status != "quarantined":
        raise EvaluationValidationError(
            f"{label} cleanup_quarantined status needs quarantine evidence"
        )
    if judge_started and not container_started:
        raise EvaluationValidationError(
            f"{label} judge container cannot start before the Agent container"
        )
    return isolation


def _validate_evaluator(
    value: Any,
    *,
    index: int,
    plan_row: dict[str, Any],
    judge_started: bool,
) -> dict[str, Any]:
    label = f"runs[{index}].evaluator"
    evaluator = _object(value, label)
    _exact_keys(
        evaluator,
        {
            "attempted",
            "fail_to_pass_total",
            "fail_to_pass_passed",
            "pass_to_pass_total",
            "pass_to_pass_passed",
            "exit_code",
            "log_sha256",
            "official_resolved",
        },
        label,
    )
    attempted = _boolean(evaluator["attempted"], f"{label}.attempted")
    official_resolved = _boolean(
        evaluator["official_resolved"], f"{label}.official_resolved"
    )
    f2p_total = _integer(
        evaluator["fail_to_pass_total"], f"{label}.fail_to_pass_total", minimum=0
    )
    f2p_passed = _integer(
        evaluator["fail_to_pass_passed"], f"{label}.fail_to_pass_passed", minimum=0
    )
    p2p_total = _integer(
        evaluator["pass_to_pass_total"], f"{label}.pass_to_pass_total", minimum=0
    )
    p2p_passed = _integer(
        evaluator["pass_to_pass_passed"], f"{label}.pass_to_pass_passed", minimum=0
    )
    if f2p_total != plan_row["fail_to_pass_count"]:
        raise EvaluationValidationError(f"{label} FAIL_TO_PASS denominator mismatch")
    if p2p_total != plan_row["pass_to_pass_count"]:
        raise EvaluationValidationError(f"{label} PASS_TO_PASS denominator mismatch")
    if f2p_passed > f2p_total or p2p_passed > p2p_total:
        raise EvaluationValidationError(f"{label} passed count exceeds total")
    if attempted:
        if not judge_started:
            raise EvaluationValidationError(
                f"{label} attempted evaluator without judge container"
            )
        exit_code = _integer(evaluator["exit_code"], f"{label}.exit_code", minimum=0)
        _hex(evaluator["log_sha256"], 64, f"{label}.log_sha256")
        expected_resolved = (
            exit_code == 0
            and f2p_passed == f2p_total
            and p2p_passed == p2p_total
        )
        if official_resolved != expected_resolved:
            raise EvaluationValidationError(
                f"{label} official_resolved contradicts test evidence"
            )
    else:
        if judge_started:
            raise EvaluationValidationError(
                f"{label} judge container started but evaluator is unattempted"
            )
        if (
            evaluator["exit_code"] is not None
            or evaluator["log_sha256"] is not None
            or f2p_passed != 0
            or p2p_passed != 0
            or official_resolved
        ):
            raise EvaluationValidationError(
                f"{label} unattempted evaluator carries result evidence"
            )
    return evaluator


def _validate_model(
    value: Any,
    *,
    index: int,
    plan_row: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    label = f"runs[{index}].model"
    model = _object(value, label)
    _exact_keys(model, {"provider", "model", "pricing_revision"}, label)
    config_row = next(
        row
        for row in config["configurations"]
        if row["configuration_id"] == plan_row["configuration_id"]
    )
    expected = config["model_slots"][config_row["model_slot"]]
    for name in ("provider", "model", "pricing_revision"):
        if _text(model[name], f"{label}.{name}") != expected[name]:
            raise EvaluationValidationError(
                f"{label}.{name} differs from frozen model slot"
            )
    return model


def _validate_hashes(
    value: Any,
    *,
    index: int,
    evaluator_attempted: bool,
    patch_sha256: str | None,
    run_plan_sha256: str,
) -> dict[str, Any]:
    label = f"runs[{index}].hashes"
    hashes = _object(value, label)
    _exact_keys(
        hashes,
        {
            "run_plan_sha256",
            "trace_sha256",
            "checkpoint_sha256",
            "evaluator_input_sha256",
            "evaluator_output_sha256",
        },
        label,
    )
    if (
        _hex(hashes["run_plan_sha256"], 64, f"{label}.run_plan_sha256")
        != run_plan_sha256
    ):
        raise EvaluationValidationError(f"{label}.run_plan_sha256 mismatch")
    _hex(hashes["trace_sha256"], 64, f"{label}.trace_sha256")
    _hex(hashes["checkpoint_sha256"], 64, f"{label}.checkpoint_sha256")
    for name in ("evaluator_input_sha256", "evaluator_output_sha256"):
        observed = _nullable_hex(hashes[name], 64, f"{label}.{name}")
        if evaluator_attempted and observed is None:
            raise EvaluationValidationError(
                f"{label}.{name} is required when evaluator ran"
            )
        if not evaluator_attempted and observed is not None:
            raise EvaluationValidationError(
                f"{label}.{name} must be null when evaluator did not run"
            )
    if evaluator_attempted and patch_sha256 is None:
        raise EvaluationValidationError(
            f"runs[{index}] evaluator needs a frozen patch hash"
        )
    return hashes


def validate_run_records(
    records: list[dict[str, Any]],
    *,
    run_plan: dict[str, Any],
    cohort: dict[str, Any],
    config: dict[str, Any],
    selection_log_bytes: bytes,
) -> list[dict[str, Any]]:
    """Validate exact coverage, cross-file bindings, and every evidence record."""

    validate_run_plan(
        run_plan,
        cohort,
        config,
        selection_log_bytes=selection_log_bytes,
    )
    if config["models_frozen"] is not True:
        raise EvaluationValidationError("run records require frozen exact models")
    if len(records) != len(run_plan["rows"]):
        raise EvaluationValidationError(
            "run records must exactly cover every run-plan row"
        )
    plan_by_id = {row["run_id"]: row for row in run_plan["rows"]}
    run_plan_hash = canonical_sha256(run_plan)
    plan_created = _parse_timestamp(run_plan["created_at"])
    normalized: list[dict[str, Any]] = []
    for index, value in enumerate(records):
        run = _object(value, f"runs[{index}]")
        _exact_keys(
            run,
            {
                "schema_version",
                "run_id",
                "run_plan_id",
                "instance_id",
                "repository",
                "configuration_id",
                "purpose",
                "started_at",
                "completed_at",
                "recorded_at",
                "status",
                "terminal_reason",
                "resolved",
                "patch_sha256",
                "cost_microusd",
                "tokens_total",
                "latency_milliseconds",
                "tool_calls",
                "test_commands_total",
                "test_commands_failed",
                "unauthorized_operations",
                "operations_total",
                "model",
                "isolation",
                "evaluator",
                "hashes",
            },
            f"runs[{index}]",
        )
        if _integer(run["schema_version"], f"runs[{index}].schema_version", minimum=1) != 1:
            raise EvaluationValidationError("unsupported run schema_version")
        run_id = _identifier(run["run_id"], f"runs[{index}].run_id")
        if run_id not in plan_by_id:
            raise EvaluationValidationError(f"runs[{index}] references unknown run_id")
        plan_row = plan_by_id[run_id]
        bindings = (
            ("run_plan_id", run_plan["run_plan_id"]),
            ("instance_id", plan_row["instance_id"]),
            ("repository", plan_row["repository"]),
            ("configuration_id", plan_row["configuration_id"]),
            ("purpose", plan_row["purpose"]),
        )
        for name, expected in bindings:
            actual = _text(run[name], f"runs[{index}].{name}")
            if actual != expected:
                raise EvaluationValidationError(
                    f"runs[{index}].{name} differs from run plan"
                )
        _repository(run["repository"], f"runs[{index}].repository")
        status = _text(run["status"], f"runs[{index}].status")
        if status not in RUN_STATUSES:
            raise EvaluationValidationError(f"runs[{index}].status is invalid")
        _text(run["terminal_reason"], f"runs[{index}].terminal_reason")
        resolved = _boolean(run["resolved"], f"runs[{index}].resolved")
        patch_sha256 = _nullable_hex(
            run["patch_sha256"], 64, f"runs[{index}].patch_sha256"
        )

        started_text = _timestamp(run["started_at"], f"runs[{index}].started_at")
        completed_text = _timestamp(
            run["completed_at"], f"runs[{index}].completed_at"
        )
        recorded_text = _timestamp(
            run["recorded_at"], f"runs[{index}].recorded_at"
        )
        started = _parse_timestamp(started_text)
        completed = _parse_timestamp(completed_text)
        recorded = _parse_timestamp(recorded_text)
        if started < plan_created:
            raise EvaluationValidationError(f"runs[{index}] started before run-plan freeze")
        if completed < started or recorded < completed:
            raise EvaluationValidationError(f"runs[{index}] timestamps are out of order")
        latency_ms = _integer(
            run["latency_milliseconds"],
            f"runs[{index}].latency_milliseconds",
            minimum=1,
        )
        observed_wall_ms = int((completed - started).total_seconds() * 1000)
        if abs(observed_wall_ms - latency_ms) > 999:
            raise EvaluationValidationError(
                f"runs[{index}] latency contradicts start/completion timestamps"
            )

        cost = _integer(
            run["cost_microusd"], f"runs[{index}].cost_microusd", minimum=0
        )
        tokens = _integer(
            run["tokens_total"], f"runs[{index}].tokens_total", minimum=0
        )
        tool_calls = _integer(
            run["tool_calls"], f"runs[{index}].tool_calls", minimum=0
        )
        test_total = _integer(
            run["test_commands_total"],
            f"runs[{index}].test_commands_total",
            minimum=0,
        )
        test_failed = _integer(
            run["test_commands_failed"],
            f"runs[{index}].test_commands_failed",
            minimum=0,
        )
        unauthorized = _integer(
            run["unauthorized_operations"],
            f"runs[{index}].unauthorized_operations",
            minimum=0,
        )
        operations_total = _integer(
            run["operations_total"],
            f"runs[{index}].operations_total",
            minimum=0,
        )
        if cost > plan_row["budget"]["total_cost_microusd"]:
            raise EvaluationValidationError(f"runs[{index}] exceeds cost budget")
        if tokens > plan_row["budget"]["total_tokens"]:
            raise EvaluationValidationError(f"runs[{index}] exceeds token budget")
        if latency_ms > plan_row["budget"]["total_seconds"] * 1000:
            raise EvaluationValidationError(f"runs[{index}] exceeds time budget")
        if tool_calls > plan_row["budget"]["tool_calls"]:
            raise EvaluationValidationError(f"runs[{index}] exceeds tool-call budget")
        if test_total > plan_row["budget"]["test_command_invocations"]:
            raise EvaluationValidationError(f"runs[{index}] exceeds test-command budget")
        if test_failed > test_total:
            raise EvaluationValidationError(
                f"runs[{index}] failed tests exceed test total"
            )
        if unauthorized > operations_total:
            raise EvaluationValidationError(
                f"runs[{index}] unauthorized operations exceed total operations"
            )
        if unauthorized:
            if status != "policy_violation" or resolved:
                raise EvaluationValidationError(
                    f"runs[{index}] policy violation must be terminal and unresolved"
                )
        elif status == "policy_violation":
            raise EvaluationValidationError(
                f"runs[{index}] policy_violation needs an unauthorized event"
            )

        _validate_model(run["model"], index=index, plan_row=plan_row, config=config)
        isolation = _validate_actual_isolation(
            run["isolation"], index=index, plan_row=plan_row, status=status
        )
        evaluator = _validate_evaluator(
            run["evaluator"],
            index=index,
            plan_row=plan_row,
            judge_started=isolation["judge_container_started"],
        )
        _validate_hashes(
            run["hashes"],
            index=index,
            evaluator_attempted=evaluator["attempted"],
            patch_sha256=patch_sha256,
            run_plan_sha256=run_plan_hash,
        )
        safely_resolved = (
            status == "completed"
            and evaluator["attempted"]
            and evaluator["official_resolved"]
            and isolation["worktree_created"]
            and isolation["container_started"]
            and isolation["judge_container_started"]
            and isolation["cleanup_status"] == "removed"
            and isolation["original_checkout_unchanged"]
            and unauthorized == 0
        )
        if resolved != safely_resolved:
            raise EvaluationValidationError(
                f"runs[{index}].resolved contradicts evaluator/isolation/status evidence"
            )
        if status == "completed" and not evaluator["attempted"]:
            raise EvaluationValidationError(
                f"runs[{index}] completed status requires official evaluator"
            )
        normalized.append(run)

    run_ids = [run["run_id"] for run in normalized]
    if len(set(run_ids)) != len(run_ids) or set(run_ids) != set(plan_by_id):
        raise EvaluationValidationError("run records duplicate or omit run-plan rows")
    pairs = [(run["instance_id"], run["configuration_id"]) for run in normalized]
    if len(set(pairs)) != len(pairs):
        raise EvaluationValidationError("run records duplicate a task/config pair")
    isolation_fields = (
        "worktree_id",
        "task_branch",
        "container_name",
        "judge_container_name",
        "state_id",
    )
    for field in isolation_fields:
        values = [run["isolation"][field] for run in normalized]
        if len(set(values)) != len(values):
            raise EvaluationValidationError(f"run records reuse isolation {field}")
    for field in ("trace_sha256", "checkpoint_sha256"):
        values = [run["hashes"][field] for run in normalized]
        if len(set(values)) != len(values):
            raise EvaluationValidationError(f"run records reuse {field}")
    evaluator_outputs = [
        run["hashes"]["evaluator_output_sha256"]
        for run in normalized
        if run["hashes"]["evaluator_output_sha256"] is not None
    ]
    if len(set(evaluator_outputs)) != len(evaluator_outputs):
        raise EvaluationValidationError("run records reuse evaluator output evidence")
    return sorted(
        normalized,
        key=lambda run: (run["instance_id"], run["configuration_id"]),
    )


def _configuration_metrics(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    attempted = len(records)
    resolved = sum(1 for run in records if run["resolved"])
    costs = [run["cost_microusd"] for run in records]
    tokens = [run["tokens_total"] for run in records]
    latencies = [run["latency_milliseconds"] for run in records]
    tools = [run["tool_calls"] for run in records]
    test_total = sum(run["test_commands_total"] for run in records)
    test_failed = sum(run["test_commands_failed"] for run in records)
    task_test_failed = sum(1 for run in records if run["test_commands_failed"] > 0)
    operations = sum(run["operations_total"] for run in records)
    unauthorized = sum(run["unauthorized_operations"] for run in records)
    unauthorized_tasks = sum(
        1 for run in records if run["unauthorized_operations"] > 0
    )
    cost_per_resolved = None
    if resolved:
        cost_per_resolved = sum(costs) / resolved
    return {
        "attempted_tasks": attempted,
        "resolved_tasks": resolved,
        "pass_at_1": _rate(resolved, attempted),
        "cost_microusd": {
            "total": sum(costs),
            "mean": _mean(costs),
            "median": percentile(costs, 0.5),
            "p95": percentile(costs, 0.95),
            "per_resolved_task": cost_per_resolved,
        },
        "tokens_total": {
            "total": sum(tokens),
            "mean": _mean(tokens),
            "p50": percentile(tokens, 0.5),
            "p95": percentile(tokens, 0.95),
        },
        "latency_milliseconds": {
            "mean": _mean(latencies),
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "maximum": None if not latencies else max(latencies),
        },
        "tool_calls": {
            "total": sum(tools),
            "mean": _mean(tools),
            "p50": percentile(tools, 0.5),
            "p95": percentile(tools, 0.95),
        },
        "test_failures": {
            "invocation_rate": _rate(test_failed, test_total),
            "task_rate": _rate(task_test_failed, attempted),
        },
        "unauthorized_operations": {
            "event_rate": _rate(unauthorized, operations),
            "task_rate": _rate(unauthorized_tasks, attempted),
        },
        "status_counts": dict(sorted(Counter(run["status"] for run in records).items())),
    }


def _bootstrap_scalar_metrics(records: Sequence[dict[str, Any]]) -> dict[str, float]:
    costs = [run["cost_microusd"] for run in records]
    latencies = [run["latency_milliseconds"] for run in records]
    tools = [run["tool_calls"] for run in records]
    attempted = len(records)
    p50_latency = percentile(latencies, 0.5)
    p95_latency = percentile(latencies, 0.95)
    if p50_latency is None or p95_latency is None:
        raise ValueError("bootstrap sample cannot be empty")
    return {
        "pass_at_1": sum(1 for run in records if run["resolved"]) / attempted,
        "mean_cost_microusd": sum(costs) / attempted,
        "p50_latency_milliseconds": p50_latency,
        "p95_latency_milliseconds": p95_latency,
        "mean_tool_calls": sum(tools) / attempted,
        "task_test_failure_rate": (
            sum(1 for run in records if run["test_commands_failed"] > 0) / attempted
        ),
        "unauthorized_operation_task_rate": (
            sum(1 for run in records if run["unauthorized_operations"] > 0)
            / attempted
        ),
    }


def _interval(values: Sequence[float], replicates: int) -> dict[str, Any]:
    if not values:
        return {
            "lower": None,
            "upper": None,
            "defined_replicates": 0,
            "replicates": replicates,
            "reason": "undefined_in_all_resamples",
        }
    return {
        "lower": percentile(values, 0.025),
        "upper": percentile(values, 0.975),
        "defined_replicates": len(values),
        "replicates": replicates,
        "reason": None,
    }


def bootstrap_intervals(
    records: Sequence[dict[str, Any]],
    *,
    seed: int,
    replicates: int,
) -> dict[str, Any]:
    """Repository-stratified paired task bootstrap over the complete matrix."""

    if isinstance(replicates, bool) or not isinstance(replicates, int) or replicates < 100:
        raise ValueError("bootstrap needs at least 100 replicates")
    by_repo: dict[str, list[str]] = defaultdict(list)
    record_map: dict[tuple[str, str], dict[str, Any]] = {}
    for run in records:
        if run["configuration_id"] == "primary":
            by_repo[run["repository"]].append(run["instance_id"])
        record_map[(run["instance_id"], run["configuration_id"])] = run
    for repo in by_repo:
        by_repo[repo] = sorted(set(by_repo[repo]))
    if sum(len(tasks) for tasks in by_repo.values()) < 2:
        return {
            "method": "repository_stratified_task_percentile_v1",
            "seed": seed,
            "replicates": replicates,
            "configurations": {},
            "paired_pass_at_1_delta": {},
            "reason": "fewer_than_two_tasks",
        }
    rng = random.Random(seed)
    samples: dict[str, dict[str, list[float]]] = {
        config_id: {metric: [] for metric in BOOTSTRAP_METRICS}
        for config_id in CONFIGURATION_ORDER
    }
    deltas: dict[str, list[float]] = {
        config_id: []
        for config_id in CONFIGURATION_ORDER
        if config_id != "primary"
    }
    for _ in range(replicates):
        sampled_ids: list[str] = []
        for repo in sorted(by_repo):
            task_ids = by_repo[repo]
            sampled_ids.extend(rng.choice(task_ids) for _item in task_ids)
        replicate_values: dict[str, dict[str, float]] = {}
        for config_id in CONFIGURATION_ORDER:
            selected = [
                record_map[(instance_id, config_id)]
                for instance_id in sampled_ids
            ]
            metrics = _bootstrap_scalar_metrics(selected)
            replicate_values[config_id] = metrics
            for name, value in metrics.items():
                samples[config_id][name].append(value)
        primary = replicate_values["primary"]["pass_at_1"]
        for config_id in deltas:
            deltas[config_id].append(
                replicate_values[config_id]["pass_at_1"] - primary
            )
    return {
        "method": "repository_stratified_task_percentile_v1",
        "seed": seed,
        "replicates": replicates,
        "configurations": {
            config_id: {
                name: _interval(values, replicates)
                for name, values in metric_values.items()
            }
            for config_id, metric_values in samples.items()
        },
        "paired_pass_at_1_delta": {
            config_id: _interval(values, replicates)
            for config_id, values in deltas.items()
        },
        "reason": None,
    }


def build_report(
    records: list[dict[str, Any]],
    *,
    run_plan: dict[str, Any],
    cohort: dict[str, Any],
    config: dict[str, Any],
    selection_log_bytes: bytes,
    runs_bytes_sha256: str,
    created_at: str,
    bootstrap_replicates: int | None = None,
) -> dict[str, Any]:
    normalized = validate_run_records(
        records,
        run_plan=run_plan,
        cohort=cohort,
        config=config,
        selection_log_bytes=selection_log_bytes,
    )
    created_at = _timestamp(created_at, "report.created_at")
    latest_record = max(_parse_timestamp(run["recorded_at"]) for run in normalized)
    if _parse_timestamp(created_at) < latest_record:
        raise EvaluationValidationError("report created_at precedes a run record")
    _hex(runs_bytes_sha256, 64, "runs_bytes_sha256")
    by_config: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in normalized:
        by_config[run["configuration_id"]].append(run)
    if set(by_config) != set(CONFIGURATION_ORDER):
        raise EvaluationValidationError("report matrix lacks a configuration")
    aggregates = {
        config_id: _configuration_metrics(by_config[config_id])
        for config_id in CONFIGURATION_ORDER
    }
    primary_value = aggregates["primary"]["pass_at_1"]["value"]
    ablations = {}
    for config_id in CONFIGURATION_ORDER:
        if config_id == "primary":
            continue
        value = aggregates[config_id]["pass_at_1"]["value"]
        ablations[config_id] = {
            "resolved_delta": (
                aggregates[config_id]["resolved_tasks"]
                - aggregates["primary"]["resolved_tasks"]
            ),
            "pass_at_1_delta": value - primary_value,
        }
    replicates = (
        config["bootstrap"]["replicates"]
        if bootstrap_replicates is None
        else bootstrap_replicates
    )
    bootstrap = bootstrap_intervals(
        normalized,
        seed=config["bootstrap"]["seed"],
        replicates=replicates,
    )
    for config_id, values in ablations.items():
        values["paired_bootstrap_95_ci"] = bootstrap[
            "paired_pass_at_1_delta"
        ][config_id]
    per_task = [
        {
            "instance_id": run["instance_id"],
            "repository": run["repository"],
            "configuration_id": run["configuration_id"],
            "resolved": run["resolved"],
            "status": run["status"],
            "cost_microusd": run["cost_microusd"],
            "tokens_total": run["tokens_total"],
            "latency_milliseconds": run["latency_milliseconds"],
            "tool_calls": run["tool_calls"],
            "test_commands_failed": run["test_commands_failed"],
            "test_commands_total": run["test_commands_total"],
            "unauthorized_operations": run["unauthorized_operations"],
            "operations_total": run["operations_total"],
        }
        for run in normalized
    ]
    return {
        "schema_version": 1,
        "metric_version": METRIC_VERSION,
        "created_at": created_at,
        "cohort_id": cohort["cohort_id"],
        "run_plan_id": run_plan["run_plan_id"],
        "primary_configuration_id": config["primary_configuration_id"],
        "input_sha256": {
            "cohort_canonical": canonical_sha256(cohort),
            "config_canonical": canonical_sha256(config),
            "selection_log_bytes": sha256_bytes(selection_log_bytes),
            "run_plan_canonical": canonical_sha256(run_plan),
            "runs_bytes": runs_bytes_sha256,
        },
        "headline": aggregates["primary"],
        "configurations": aggregates,
        "ablations": ablations,
        "bootstrap_95_ci": bootstrap,
        "per_task": per_task,
        "integrity": {
            "attempted_task_configurations": len(normalized),
            "expected_task_configurations": len(run_plan["rows"]),
            "all_run_plan_rows_covered_once": True,
            "all_isolation_identities_unique": True,
            "fail_closed_before_metrics": True,
        },
    }


def _write_json(path: Path, value: Any) -> None:
    safe_artifact_path(path).write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline metrics for sealed SWE-bench Repair runs"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("validate-runs", "report"):
        command = actions.add_parser(action)
        command.add_argument("--cohort", type=Path, required=True)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--selection-log", type=Path, required=True)
        command.add_argument("--run-plan", type=Path, required=True)
        command.add_argument("--runs", type=Path, required=True)
        if action == "report":
            command.add_argument("--created-at", required=True)
            command.add_argument("--out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        cohort = load_json(args.cohort)
        config = load_json(args.config)
        run_plan = load_json(args.run_plan)
        selection_log_bytes = read_artifact_bytes(args.selection_log)
        runs_bytes = read_artifact_bytes(args.runs)
        records = load_jsonl_bytes(runs_bytes, label="runs")
        normalized = validate_run_records(
            records,
            run_plan=run_plan,
            cohort=cohort,
            config=config,
            selection_log_bytes=selection_log_bytes,
        )
        if args.action == "validate-runs":
            result: dict[str, Any] = {
                "valid": True,
                "runs": len(normalized),
                "resolved_primary": sum(
                    1
                    for run in normalized
                    if run["configuration_id"] == "primary" and run["resolved"]
                ),
                "runs_sha256": sha256_bytes(runs_bytes),
            }
        else:
            result = build_report(
                records,
                run_plan=run_plan,
                cohort=cohort,
                config=config,
                selection_log_bytes=selection_log_bytes,
                runs_bytes_sha256=sha256_bytes(runs_bytes),
                created_at=args.created_at,
            )
            if args.out is not None:
                _write_json(args.out, result)
                result = {
                    "valid": True,
                    "run_plan_id": run_plan["run_plan_id"],
                    "attempts": len(normalized),
                    "out": str(args.out),
                }
    except (OSError, PlanValidationError, EvaluationValidationError) as exc:
        parser.exit(2, f"repair evaluation refused: {exc}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
