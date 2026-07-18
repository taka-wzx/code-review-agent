from __future__ import annotations

import contextlib
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import swebench_repair_eval as evaluation
import swebench_repair_runner as runner
from tests.test_swebench_repair_runner import make_run_plan


REPORT_CREATED_AT = "2026-07-20T00:00:00Z"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_test_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def records_bytes(records: list[dict]) -> bytes:
    return b"".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for record in records
    )


def make_run_records(
    plan: dict,
    config: dict,
) -> list[dict]:
    task_ids = sorted(
        {
            row["instance_id"]
            for row in plan["rows"]
            if row["configuration_id"] == "primary"
        }
    )
    task_index = {instance_id: index for index, instance_id in enumerate(task_ids)}
    config_rows = {
        row["configuration_id"]: row for row in config["configurations"]
    }
    base_time = datetime(2026, 7, 18, 13, 0, 0, tzinfo=timezone.utc)
    run_plan_hash = runner.canonical_sha256(plan)
    records = []
    for index, row in enumerate(plan["rows"]):
        position = task_index[row["instance_id"]]
        config_id = row["configuration_id"]
        resolved = {
            "primary": position % 2 == 0,
            "single_finder": position % 3 == 0,
            "no_context": position % 4 != 0,
            "no_verifier": position < 5,
            "no_reflection": position >= 5,
            "model_b": False,
        }[config_id]
        started = base_time + timedelta(minutes=index * 2)
        duration_seconds = 60 + index
        completed = started + timedelta(seconds=duration_seconds)
        container_started = started + timedelta(seconds=1)
        container_completed = completed - timedelta(seconds=20)
        judge_started = completed - timedelta(seconds=15)
        judge_completed = completed - timedelta(seconds=2)
        model_slot = config_rows[config_id]["model_slot"]
        f2p_total = row["fail_to_pass_count"]
        p2p_total = row["pass_to_pass_count"]
        f2p_passed = f2p_total if resolved else f2p_total - 1
        test_total = index % 4
        test_failed = 1 if index % 11 == 0 and test_total else 0
        tool_calls = 5 + index % 10
        repair_attempts = 0 if config_id == "no_reflection" else index % 3
        operations_total = tool_calls
        records.append(
            {
                "schema_version": 1,
                "run_id": row["run_id"],
                "run_plan_id": plan["run_plan_id"],
                "instance_id": row["instance_id"],
                "repository": row["repository"],
                "configuration_id": config_id,
                "purpose": row["purpose"],
                "started_at": _timestamp(started),
                "completed_at": _timestamp(completed),
                "recorded_at": _timestamp(completed + timedelta(seconds=1)),
                "status": "completed",
                "terminal_reason": (
                    "official_resolved" if resolved else "official_tests_failed"
                ),
                "resolved": resolved,
                "patch_sha256": _sha(f"patch:{row['run_id']}"),
                "cost_microusd": 1000 + index,
                "tokens_total": 2000 + index,
                "latency_milliseconds": duration_seconds * 1000,
                "tool_calls": tool_calls,
                "test_commands_total": test_total,
                "test_commands_failed": test_failed,
                "repair_attempts_used": repair_attempts,
                "max_command_milliseconds": 1000 + index,
                "max_command_output_bytes": 128 + index,
                "unauthorized_operations": 0,
                "operations_total": operations_total,
                "model": copy.deepcopy(config["model_slots"][model_slot]),
                "isolation": {
                    "worktree_id": row["worktree_id"],
                    "worktree_created": True,
                    "task_branch": row["task_branch"],
                    "container_name": row["container_name"],
                    "container_started": True,
                    "container_started_at": _timestamp(container_started),
                    "container_completed_at": _timestamp(container_completed),
                    "judge_container_name": row["judge_container_name"],
                    "judge_container_started": True,
                    "judge_container_started_at": _timestamp(judge_started),
                    "judge_container_completed_at": _timestamp(judge_completed),
                    "state_id": row["state_id"],
                    "image_digest": row["image_digest"],
                    "network_mode": "none",
                    "read_only_root": True,
                    "run_as_non_root": True,
                    "cap_drop_all": True,
                    "no_new_privileges": True,
                    "pids": row["isolation"]["pids"],
                    "cpus": row["isolation"]["cpus"],
                    "memory_mib": row["isolation"]["memory_mib"],
                    "writable_mib": row["isolation"]["writable_mib"],
                    "writable_mounts": 1,
                    "original_checkout_unchanged": True,
                    "cleanup_status": "removed",
                },
                "evaluator": {
                    "attempted": True,
                    "fail_to_pass_total": f2p_total,
                    "fail_to_pass_passed": f2p_passed,
                    "pass_to_pass_total": p2p_total,
                    "pass_to_pass_passed": p2p_total,
                    "exit_code": 0 if resolved else 1,
                    "log_sha256": _sha(f"log:{row['run_id']}"),
                    "official_resolved": resolved,
                },
                "hashes": {
                    "run_plan_sha256": run_plan_hash,
                    "trace_sha256": _sha(f"trace:{row['run_id']}"),
                    "checkpoint_sha256": _sha(f"checkpoint:{row['run_id']}"),
                    "evaluator_input_sha256": _sha(
                        f"evaluator-input:{row['run_id']}"
                    ),
                    "evaluator_output_sha256": _sha(
                        f"evaluator-output:{row['run_id']}"
                    ),
                },
            }
        )
    return records


def make_inputs() -> tuple[dict, dict, bytes, dict, list[dict]]:
    cohort, config, selection, plan = make_run_plan()
    records = make_run_records(plan, config)
    return cohort, config, selection, plan, records


def validate(
    cohort: dict,
    config: dict,
    selection: bytes,
    plan: dict,
    records: list[dict],
) -> list[dict]:
    return evaluation.validate_run_records(
        records,
        run_plan=plan,
        cohort=cohort,
        config=config,
        selection_log_bytes=selection,
    )


def make_report(
    cohort: dict,
    config: dict,
    selection: bytes,
    plan: dict,
    records: list[dict],
) -> dict:
    data = records_bytes(records)
    return evaluation.build_report(
        records,
        run_plan=plan,
        cohort=cohort,
        config=config,
        selection_log_bytes=selection,
        runs_bytes_sha256=runner.sha256_bytes(data),
        created_at=REPORT_CREATED_AT,
        bootstrap_replicates=200,
    )


def mark_unattempted_failure(record: dict, *, status: str) -> None:
    record["status"] = status
    record["terminal_reason"] = status
    record["resolved"] = False
    record["patch_sha256"] = None
    record["isolation"]["judge_container_started"] = False
    record["isolation"]["judge_container_started_at"] = None
    record["isolation"]["judge_container_completed_at"] = None
    record["evaluator"] = {
        "attempted": False,
        "fail_to_pass_total": record["evaluator"]["fail_to_pass_total"],
        "fail_to_pass_passed": 0,
        "pass_to_pass_total": record["evaluator"]["pass_to_pass_total"],
        "pass_to_pass_passed": 0,
        "exit_code": None,
        "log_sha256": None,
        "official_resolved": False,
    }
    record["hashes"]["evaluator_input_sha256"] = None
    record["hashes"]["evaluator_output_sha256"] = None


class EvaluationTests(unittest.TestCase):
    def test_valid_complete_matrix(self):
        cohort, config, selection, plan, records = make_inputs()
        observed = validate(cohort, config, selection, plan, records)
        self.assertEqual(len(observed), 120)

    def test_primary_pass_at_one_keeps_all_twenty_in_denominator(self):
        cohort, config, selection, plan, records = make_inputs()
        report = make_report(cohort, config, selection, plan, records)
        self.assertEqual(report["headline"]["pass_at_1"]["numerator"], 10)
        self.assertEqual(report["headline"]["pass_at_1"]["denominator"], 20)
        self.assertEqual(report["headline"]["pass_at_1"]["value"], 0.5)

    def test_per_configuration_resource_metrics(self):
        cohort, config, selection, plan, records = make_inputs()
        report = make_report(cohort, config, selection, plan, records)
        primary_records = [
            record for record in records if record["configuration_id"] == "primary"
        ]
        expected_cost = sum(record["cost_microusd"] for record in primary_records)
        expected_tokens = sum(record["tokens_total"] for record in primary_records)
        expected_tools = sum(record["tool_calls"] for record in primary_records)
        self.assertEqual(report["headline"]["cost_microusd"]["total"], expected_cost)
        self.assertEqual(report["headline"]["tokens_total"]["total"], expected_tokens)
        self.assertEqual(report["headline"]["tool_calls"]["total"], expected_tools)
        self.assertIsNotNone(report["headline"]["latency_milliseconds"]["p95"])
        self.assertEqual(
            report["integrity"]["reporting_cost_microusd_observed"],
            sum(record["cost_microusd"] for record in records),
        )
        self.assertFalse(report["integrity"]["reporting_cost_ceiling_exceeded"])
        self.assertEqual(report["integrity"]["runs_with_budget_overrun"], 0)

    def test_test_failure_rates_have_explicit_denominators(self):
        cohort, config, selection, plan, records = make_inputs()
        report = make_report(cohort, config, selection, plan, records)
        primary = [r for r in records if r["configuration_id"] == "primary"]
        self.assertEqual(
            report["headline"]["test_failures"]["invocation_rate"]["denominator"],
            sum(record["test_commands_total"] for record in primary),
        )
        self.assertEqual(
            report["headline"]["test_failures"]["task_rate"]["denominator"], 20
        )

    def test_unauthorized_rates_are_zero_not_undefined_when_operations_exist(self):
        cohort, config, selection, plan, records = make_inputs()
        report = make_report(cohort, config, selection, plan, records)
        event_rate = report["headline"]["unauthorized_operations"]["event_rate"]
        self.assertEqual(event_rate["numerator"], 0)
        self.assertGreater(event_rate["denominator"], 0)
        self.assertEqual(event_rate["value"], 0)

    def test_bootstrap_is_deterministic(self):
        cohort, config, selection, plan, records = make_inputs()
        first = make_report(cohort, config, selection, plan, records)
        second = make_report(cohort, config, selection, plan, records)
        self.assertEqual(first["bootstrap_95_ci"], second["bootstrap_95_ci"])

    def test_bootstrap_is_order_independent(self):
        cohort, config, selection, plan, records = make_inputs()
        first = make_report(cohort, config, selection, plan, records)
        records.reverse()
        second = make_report(cohort, config, selection, plan, records)
        self.assertEqual(
            first["bootstrap_95_ci"], second["bootstrap_95_ci"]
        )
        self.assertEqual(first["configurations"], second["configurations"])

    def test_ablation_delta_is_paired_and_reported(self):
        cohort, config, selection, plan, records = make_inputs()
        report = make_report(cohort, config, selection, plan, records)
        self.assertEqual(report["ablations"]["no_context"]["resolved_delta"], 5)
        self.assertEqual(report["ablations"]["no_context"]["pass_at_1_delta"], 0.25)
        self.assertIsNotNone(
            report["ablations"]["no_context"]["paired_bootstrap_95_ci"]["lower"]
        )

    def test_missing_run_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records.pop()
        with self.assertRaisesRegex(evaluation.EvaluationValidationError, "cover"):
            validate(cohort, config, selection, plan, records)

    def test_duplicate_run_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[-1] = copy.deepcopy(records[0])
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "duplicate|omit"
        ):
            validate(cohort, config, selection, plan, records)

    def test_reused_worktree_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[1]["isolation"]["worktree_id"] = records[0]["isolation"]["worktree_id"]
        with self.assertRaisesRegex(evaluation.EvaluationValidationError, "worktree_id"):
            validate(cohort, config, selection, plan, records)

    def test_network_enabled_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["isolation"]["network_mode"] = "bridge"
        with self.assertRaisesRegex(evaluation.EvaluationValidationError, "network"):
            validate(cohort, config, selection, plan, records)

    def test_root_container_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["isolation"]["run_as_non_root"] = False
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "run_as_non_root"
        ):
            validate(cohort, config, selection, plan, records)

    def test_extra_writable_mount_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["isolation"]["writable_mounts"] = 2
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "writable_mounts"
        ):
            validate(cohort, config, selection, plan, records)

    def test_judge_cannot_start_before_agent_container(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["isolation"]["container_started"] = False
        records[0]["isolation"]["container_started_at"] = None
        records[0]["isolation"]["container_completed_at"] = None
        records[0]["isolation"]["writable_mounts"] = 0
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError,
            "judge container cannot start before",
        ):
            validate(cohort, config, selection, plan, records)

    def test_original_checkout_change_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["isolation"]["original_checkout_unchanged"] = False
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "original checkout"
        ):
            validate(cohort, config, selection, plan, records)

    def test_evaluator_contradiction_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["evaluator"]["official_resolved"] = not records[0]["evaluator"][
            "official_resolved"
        ]
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "official_resolved"
        ):
            validate(cohort, config, selection, plan, records)

    def test_resolved_contradiction_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["resolved"] = not records[0]["resolved"]
        with self.assertRaisesRegex(evaluation.EvaluationValidationError, "resolved"):
            validate(cohort, config, selection, plan, records)

    def test_policy_violation_counts_as_unresolved(self):
        cohort, config, selection, plan, records = make_inputs()
        target = next(record for record in records if record["configuration_id"] == "primary")
        mark_unattempted_failure(target, status="policy_violation")
        target["unauthorized_operations"] = 1
        target["operations_total"] = target["tool_calls"] + 1
        normalized = validate(cohort, config, selection, plan, records)
        self.assertFalse(next(r for r in normalized if r["run_id"] == target["run_id"])["resolved"])
        report = make_report(cohort, config, selection, plan, records)
        self.assertEqual(
            report["headline"]["unauthorized_operations"]["task_rate"]["numerator"],
            1,
        )

    def test_unauthorized_event_can_keep_truthful_completed_status(self):
        cohort, config, selection, plan, records = make_inputs()
        target = next(record for record in records if record["resolved"])
        target["unauthorized_operations"] = 1
        target["operations_total"] = target["tool_calls"] + 1
        target["resolved"] = False
        observed = validate(cohort, config, selection, plan, records)
        normalized = next(run for run in observed if run["run_id"] == target["run_id"])
        self.assertEqual(normalized["status"], "completed")
        self.assertFalse(normalized["resolved"])

    def test_failed_tests_cannot_exceed_total(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["test_commands_total"] = 0
        records[0]["test_commands_failed"] = 1
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "failed tests"
        ):
            validate(cohort, config, selection, plan, records)

    def test_cost_budget_is_enforced(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["cost_microusd"] = 500001
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "settlement grace"
        ):
            validate(cohort, config, selection, plan, records)

    def test_token_budget_is_enforced(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["tokens_total"] = 120001
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "settlement grace"
        ):
            validate(cohort, config, selection, plan, records)

    def test_latency_budget_is_enforced(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["latency_milliseconds"] = 3600001
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError,
            "latency contradicts|termination grace",
        ):
            validate(cohort, config, selection, plan, records)

    def test_bounded_budget_and_timeout_overruns_are_recorded_truthfully(self):
        cohort, config, selection, plan, records = make_inputs()
        budget_target = records[0]
        mark_unattempted_failure(budget_target, status="budget_exhausted")
        budget_target["cost_microusd"] = 510000

        timeout_target = records[-1]
        mark_unattempted_failure(timeout_target, status="timeout")
        started = datetime(2026, 7, 18, 13, 0, 0, tzinfo=timezone.utc) + timedelta(
            minutes=119 * 2
        )
        completed = started + timedelta(seconds=3604)
        timeout_target["started_at"] = _timestamp(started)
        timeout_target["completed_at"] = _timestamp(completed)
        timeout_target["recorded_at"] = _timestamp(completed + timedelta(seconds=1))
        timeout_target["latency_milliseconds"] = 3604000
        timeout_target["isolation"]["container_started_at"] = _timestamp(
            started + timedelta(seconds=1)
        )
        timeout_target["isolation"]["container_completed_at"] = _timestamp(
            started + timedelta(seconds=20)
        )

        observed = validate(cohort, config, selection, plan, records)
        self.assertEqual(len(observed), 120)
        report = make_report(cohort, config, selection, plan, records)
        by_run = {row["instance_id"] + row["configuration_id"]: row for row in report["per_task"]}
        self.assertTrue(any(row["budget_overrun_dimensions"] for row in by_run.values()))

    def test_model_binding_is_enforced(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["model"]["model"] = "changed"
        with self.assertRaisesRegex(evaluation.EvaluationValidationError, "model"):
            validate(cohort, config, selection, plan, records)

    def test_purpose_binding_refuses_tuning(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["purpose"] = "tuning"
        with self.assertRaisesRegex(evaluation.EvaluationValidationError, "purpose"):
            validate(cohort, config, selection, plan, records)

    def test_run_before_freeze_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["started_at"] = "2026-07-18T11:59:59Z"
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "before run-plan freeze"
        ):
            validate(cohort, config, selection, plan, records)

    def test_reused_trace_hash_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[1]["hashes"]["trace_sha256"] = records[0]["hashes"]["trace_sha256"]
        with self.assertRaisesRegex(evaluation.EvaluationValidationError, "trace"):
            validate(cohort, config, selection, plan, records)

    def test_reused_evaluator_output_hash_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[1]["hashes"]["evaluator_output_sha256"] = records[0]["hashes"][
            "evaluator_output_sha256"
        ]
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "evaluator output"
        ):
            validate(cohort, config, selection, plan, records)

    def test_completed_run_needs_evaluator(self):
        cohort, config, selection, plan, records = make_inputs()
        mark_unattempted_failure(records[0], status="completed")
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "completed status"
        ):
            validate(cohort, config, selection, plan, records)

    def test_setup_sandbox_failure_is_counted_unresolved(self):
        cohort, config, selection, plan, records = make_inputs()
        target = records[0]
        mark_unattempted_failure(target, status="sandbox_failure")
        target["isolation"]["worktree_created"] = False
        target["isolation"]["container_started"] = False
        target["isolation"]["container_started_at"] = None
        target["isolation"]["container_completed_at"] = None
        target["isolation"]["writable_mounts"] = 0
        target["isolation"]["cleanup_status"] = "not_created"
        normalized = validate(cohort, config, selection, plan, records)
        observed = next(run for run in normalized if run["run_id"] == target["run_id"])
        self.assertFalse(observed["resolved"])

    def test_created_worktree_with_unstarted_container_is_valid_failure(self):
        cohort, config, selection, plan, records = make_inputs()
        target = records[0]
        mark_unattempted_failure(target, status="sandbox_failure")
        target["isolation"]["container_started"] = False
        target["isolation"]["container_started_at"] = None
        target["isolation"]["container_completed_at"] = None
        target["isolation"]["writable_mounts"] = 0
        target["isolation"]["cleanup_status"] = "removed"
        observed = validate(cohort, config, selection, plan, records)
        self.assertFalse(
            next(run for run in observed if run["run_id"] == target["run_id"])[
                "resolved"
            ]
        )

    def test_cleanup_quarantine_overrides_official_success(self):
        cohort, config, selection, plan, records = make_inputs()
        target = next(record for record in records if record["resolved"])
        target["status"] = "cleanup_quarantined"
        target["terminal_reason"] = "cleanup_not_proven"
        target["resolved"] = False
        target["isolation"]["cleanup_status"] = "quarantined"
        normalized = validate(cohort, config, selection, plan, records)
        observed = next(run for run in normalized if run["run_id"] == target["run_id"])
        self.assertFalse(observed["resolved"])
        self.assertTrue(observed["evaluator"]["official_resolved"])

    def test_operations_total_must_match_executed_plus_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["operations_total"] -= 1
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "operations_total"
        ):
            validate(cohort, config, selection, plan, records)

    def test_run_and_container_intervals_need_positive_duration(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["completed_at"] = records[0]["started_at"]
        records[0]["recorded_at"] = records[0]["started_at"]
        records[0]["latency_milliseconds"] = 1
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "timestamps are out of order"
        ):
            validate(cohort, config, selection, plan, records)

        cohort, config, selection, plan, records = make_inputs()
        records[0]["isolation"]["container_completed_at"] = records[0][
            "isolation"
        ]["container_started_at"]
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "timestamps are out of order"
        ):
            validate(cohort, config, selection, plan, records)

    def test_no_reflection_cannot_report_a_repair_attempt(self):
        cohort, config, selection, plan, records = make_inputs()
        target = next(
            record
            for record in records
            if record["configuration_id"] == "no_reflection"
        )
        target["repair_attempts_used"] = 1
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "repair-attempt"
        ):
            validate(cohort, config, selection, plan, records)

    def test_test_failure_status_needs_a_failed_test(self):
        cohort, config, selection, plan, records = make_inputs()
        target = records[0]
        mark_unattempted_failure(target, status="test_failure")
        target["test_commands_failed"] = 0
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "failed test command"
        ):
            validate(cohort, config, selection, plan, records)

    def test_more_than_two_parallel_runs_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        started = datetime(2026, 7, 18, 13, 0, 0, tzinfo=timezone.utc)
        completed = started + timedelta(seconds=120)
        for record in records[:3]:
            record["started_at"] = _timestamp(started)
            record["completed_at"] = _timestamp(completed)
            record["recorded_at"] = _timestamp(completed + timedelta(seconds=1))
            record["latency_milliseconds"] = 120000
            record["isolation"]["container_started_at"] = _timestamp(
                started + timedelta(seconds=1)
            )
            record["isolation"]["container_completed_at"] = _timestamp(
                started + timedelta(seconds=90)
            )
            record["isolation"]["judge_container_started_at"] = _timestamp(
                started + timedelta(seconds=90)
            )
            record["isolation"]["judge_container_completed_at"] = _timestamp(
                started + timedelta(seconds=110)
            )
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "maximum_parallel_runs"
        ):
            validate(cohort, config, selection, plan, records)

    def test_reused_checkpoint_hash_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        records[1]["hashes"]["checkpoint_sha256"] = records[0]["hashes"][
            "checkpoint_sha256"
        ]
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "checkpoint"
        ):
            validate(cohort, config, selection, plan, records)

    def test_exact_resource_budget_boundaries_are_accepted(self):
        cohort, config, selection, plan, records = make_inputs()
        target = records[-1]
        budget = plan["rows"][-1]["budget"]
        target["cost_microusd"] = budget["total_cost_microusd"]
        target["tokens_total"] = budget["total_tokens"]
        target["tool_calls"] = budget["tool_calls"]
        target["test_commands_total"] = budget["test_command_invocations"]
        target["test_commands_failed"] = 0
        target["repair_attempts_used"] = budget["repair_attempts"]
        target["max_command_milliseconds"] = budget["command_seconds"] * 1000
        target["max_command_output_bytes"] = budget["command_output_bytes"]
        target["operations_total"] = target["tool_calls"]
        started = _parse_test_timestamp(target["started_at"])
        completed = started + timedelta(seconds=budget["total_seconds"])
        target["completed_at"] = _timestamp(completed)
        target["recorded_at"] = _timestamp(completed + timedelta(seconds=1))
        target["latency_milliseconds"] = budget["total_seconds"] * 1000
        self.assertEqual(len(validate(cohort, config, selection, plan, records)), 120)

    def test_zero_denominator_is_null(self):
        self.assertEqual(
            evaluation._rate(0, 0),
            {"numerator": 0, "denominator": 0, "value": None},
        )

    def test_percentile_uses_linear_interpolation(self):
        self.assertEqual(evaluation.percentile([0, 10], 0.25), 2.5)
        self.assertEqual(evaluation.percentile([0, 10], 0.95), 9.5)

    def test_bootstrap_rejects_too_few_replicates(self):
        cohort, config, selection, plan, records = make_inputs()
        normalized = validate(cohort, config, selection, plan, records)
        with self.assertRaisesRegex(ValueError, "at least 100"):
            evaluation.bootstrap_intervals(normalized, seed=1, replicates=99)

    def test_report_created_before_runs_is_rejected(self):
        cohort, config, selection, plan, records = make_inputs()
        data = records_bytes(records)
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "precedes"
        ):
            evaluation.build_report(
                records,
                run_plan=plan,
                cohort=cohort,
                config=config,
                selection_log_bytes=selection,
                runs_bytes_sha256=runner.sha256_bytes(data),
                created_at="2026-07-18T12:00:01Z",
                bootstrap_replicates=200,
            )

    def test_run_schema_and_synthetic_row_have_matching_properties(self):
        root = Path(__file__).resolve().parents[1]
        schema = runner.load_json(
            root / "swebench_repair" / "schemas" / "runs.schema.json"
        )
        data = (
            root
            / "swebench_repair"
            / "examples"
            / "synthetic-runs.jsonl"
        ).read_bytes()
        rows = runner.load_jsonl_bytes(data, label="synthetic runs")
        self.assertEqual(set(schema["properties"]), set(rows[0]))
        self.assertFalse(rows[0]["resolved"])
        self.assertEqual(rows[0]["status"], "sandbox_failure")

    def test_cli_validates_runs_and_refuses_unsafe_report_output(self):
        cohort, config, selection, plan, records = make_inputs()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cohort_path = root / "cohort.json"
            config_path = root / "config.json"
            selection_path = root / "selection.jsonl"
            plan_path = root / "run-plan.json"
            runs_path = root / "runs.jsonl"
            cohort_path.write_text(json.dumps(cohort), encoding="utf-8")
            config_path.write_text(json.dumps(config), encoding="utf-8")
            selection_path.write_bytes(selection)
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            runs_path.write_bytes(records_bytes(records))
            common = [
                "--cohort",
                str(cohort_path),
                "--config",
                str(config_path),
                "--selection-log",
                str(selection_path),
                "--run-plan",
                str(plan_path),
                "--runs",
                str(runs_path),
            ]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = evaluation.main(["validate-runs", *common])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["runs"], 120)

            errors = io.StringIO()
            with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
                evaluation.main(
                    [
                        "report",
                        *common,
                        "--created-at",
                        REPORT_CREATED_AT,
                        "--out",
                        str(root / "eval." / "report.json"),
                    ]
                )
            self.assertIn("forbidden inputs or outputs", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
