from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import unittest

import swebench_repair_eval as evaluation
import swebench_repair_runner as runner
from tests.test_swebench_repair_runner import make_run_plan


REPORT_CREATED_AT = "2026-07-20T00:00:00Z"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        model_slot = config_rows[config_id]["model_slot"]
        f2p_total = row["fail_to_pass_count"]
        p2p_total = row["pass_to_pass_count"]
        f2p_passed = f2p_total if resolved else f2p_total - 1
        test_total = index % 4
        test_failed = 1 if index % 11 == 0 and test_total else 0
        tool_calls = 5 + index % 10
        operations_total = tool_calls + test_total + 2
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
                "unauthorized_operations": 0,
                "operations_total": operations_total,
                "model": copy.deepcopy(config["model_slots"][model_slot]),
                "isolation": {
                    "worktree_id": row["worktree_id"],
                    "worktree_created": True,
                    "task_branch": row["task_branch"],
                    "container_name": row["container_name"],
                    "container_started": True,
                    "judge_container_name": row["judge_container_name"],
                    "judge_container_started": True,
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
        target["operations_total"] = max(1, target["operations_total"])
        normalized = validate(cohort, config, selection, plan, records)
        self.assertFalse(next(r for r in normalized if r["run_id"] == target["run_id"])["resolved"])
        report = make_report(cohort, config, selection, plan, records)
        self.assertEqual(
            report["headline"]["unauthorized_operations"]["task_rate"]["numerator"],
            1,
        )

    def test_unauthorized_event_cannot_complete(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["unauthorized_operations"] = 1
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "policy violation"
        ):
            validate(cohort, config, selection, plan, records)

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
        with self.assertRaisesRegex(evaluation.EvaluationValidationError, "cost budget"):
            validate(cohort, config, selection, plan, records)

    def test_token_budget_is_enforced(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["tokens_total"] = 120001
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "token budget"
        ):
            validate(cohort, config, selection, plan, records)

    def test_latency_budget_is_enforced(self):
        cohort, config, selection, plan, records = make_inputs()
        records[0]["latency_milliseconds"] = 3600001
        with self.assertRaisesRegex(
            evaluation.EvaluationValidationError, "latency contradicts|time budget"
        ):
            validate(cohort, config, selection, plan, records)

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
        target["isolation"]["writable_mounts"] = 0
        target["isolation"]["cleanup_status"] = "not_created"
        normalized = validate(cohort, config, selection, plan, records)
        observed = next(run for run in normalized if run["run_id"] == target["run_id"])
        self.assertFalse(observed["resolved"])

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


if __name__ == "__main__":
    unittest.main()
