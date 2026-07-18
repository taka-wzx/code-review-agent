from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import swebench_repair_runner as runner


ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "afb6e1fa85701d7b6af5b16198c9dd992740a03d"
AGENT_COMMIT = "a" * 40
FREEZE_COMMIT = "b" * 40
FREEZE_HASH = "c" * 64
CREATED_AT = "2026-07-18T12:00:00Z"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def make_frozen_config() -> dict:
    config = runner.load_json(ROOT / "swebench_repair" / "config-plan.json")
    config["models_frozen"] = True
    config["model_slots"] = {
        "model_a": {
            "provider": "provider-a",
            "model": "model-a-2026-07-18",
            "pricing_revision": "pricing-a-v1",
        },
        "model_b": {
            "provider": "provider-b",
            "model": "model-b-2026-07-18",
            "pricing_revision": "pricing-b-v1",
        },
    }
    return config


def make_materialized_cohort() -> tuple[dict, bytes]:
    cohort = runner.load_json(ROOT / "swebench_repair" / "cohort-plan.json")
    seed = cohort["selection"]["seed"]
    repositories = [f"example/repo-{letter}" for letter in "abcdefg"]
    candidates_by_repo: dict[str, list[dict]] = {}
    for repo in repositories:
        token = repo.rsplit("-", 1)[1]
        rows = []
        for number in range(6):
            instance_id = f"{token}_task_{number}"
            rows.append(
                {
                    "instance_id": instance_id,
                    "repository": repo,
                    "eligible": True,
                    "exclusion_reason": None,
                    "selected": False,
                    "role": None,
                    "size_band": "small",
                    "repository_rank_sha256": runner.repository_rank(seed, repo),
                    "task_rank_sha256": runner.task_rank(seed, instance_id),
                }
            )
        rows.sort(key=lambda row: row["task_rank_sha256"])
        for index, row in enumerate(rows):
            changed_lines = 10 if index == 4 else (30 if index == 5 else index + 1)
            row["patch_changed_lines"] = changed_lines
            row["size_band"] = runner.size_band_for_changed_lines(changed_lines)
        candidates_by_repo[repo] = rows

    ranked_repositories = sorted(
        repositories, key=lambda repo: runner.repository_rank(seed, repo)
    )
    roles: dict[str, str] = {}
    offset = 0
    for role in runner.ROLE_ORDER:
        count = runner.REPOSITORIES_PER_ROLE[role]
        for repo in ranked_repositories[offset : offset + count]:
            roles[repo] = role
        offset += count

    selection_rows: list[dict] = []
    tasks: list[dict] = []
    for repo in sorted(repositories):
        role = roles.get(repo)
        for index, row in enumerate(candidates_by_repo[repo]):
            row["selected"] = (
                role is not None and index < runner.TASKS_PER_REPOSITORY
            )
            row["role"] = role if row["selected"] else None
            selection_rows.append(row)
            if row["selected"]:
                instance_id = row["instance_id"]
                tasks.append(
                    {
                        "instance_id": instance_id,
                        "repository": repo,
                        "role": role,
                        "base_sha": _sha1(f"base:{instance_id}"),
                        "base_tree_sha256": _sha256(f"tree:{instance_id}"),
                        "source_snapshot_sha256": _sha256(
                            f"snapshot:{instance_id}"
                        ),
                        "harness_task_sha256": _sha256(
                            f"harness:{instance_id}"
                        ),
                        "image_digest": f"sha256:{_sha256(f'image:{instance_id}')}",
                        "fail_to_pass_count": 1 + (index % 2),
                        "pass_to_pass_count": index % 3,
                        "patch_changed_lines": row["patch_changed_lines"],
                        "size_band": row["size_band"],
                        "repository_rank_sha256": row[
                            "repository_rank_sha256"
                        ],
                        "task_rank_sha256": row["task_rank_sha256"],
                    }
                )
    selection_rows.sort(key=lambda row: row["instance_id"])
    selection_bytes = b"".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
        for row in selection_rows
    )
    cohort["materialized"] = True
    cohort["dataset"] = {
        "name": runner.DATASET_NAME,
        "revision": "1" * 40,
        "manifest_sha256": "2" * 64,
        "manifest_task_count": len(selection_rows),
        "harness_revision": "3" * 40,
    }
    cohort["selection"]["selection_log_sha256"] = runner.sha256_bytes(
        selection_bytes
    )
    cohort["tasks"] = sorted(tasks, key=lambda task: task["instance_id"])
    return cohort, selection_bytes


def make_run_plan() -> tuple[dict, dict, bytes, dict]:
    cohort, selection_bytes = make_materialized_cohort()
    config = make_frozen_config()
    plan = runner.generate_run_plan(
        cohort,
        config,
        selection_log_bytes=selection_bytes,
        agent_source_commit=AGENT_COMMIT,
        gold_freeze_commit=FREEZE_COMMIT,
        freeze_attestation_sha256=FREEZE_HASH,
        created_at=CREATED_AT,
    )
    return cohort, config, selection_bytes, plan


class RunnerPlanTests(unittest.TestCase):
    def test_committed_unmaterialized_plans_validate(self):
        cohort = runner.load_json(ROOT / "swebench_repair" / "cohort-plan.json")
        config = runner.load_json(ROOT / "swebench_repair" / "config-plan.json")
        result = runner.validate_plans(cohort, config)
        self.assertTrue(result["valid"])
        self.assertFalse(result["materialized"])
        self.assertEqual(result["planned_reporting_attempts"], 120)

    def test_seed_is_bound_to_week5_base(self):
        self.assertEqual(
            runner.derive_cohort_seed(BASE_COMMIT),
            "39a89ee8c3368d08f2444ce84c5c86294bef36b2164397f514b50b01be963ce0",
        )

    def test_materialized_selection_validates(self):
        cohort, selection = make_materialized_cohort()
        rows = runner.validate_selection(cohort, selection)
        self.assertEqual(sum(row["selected"] for row in rows), 30)
        self.assertEqual(len(cohort["tasks"]), 30)
        self.assertTrue(
            any(
                row["eligible"] and not row["selected"] and row["role"] is None
                for row in rows
            )
        )

    def test_run_plan_has_complete_matrix(self):
        _cohort, _config, _selection, plan = make_run_plan()
        self.assertEqual(len(plan["rows"]), 120)
        self.assertEqual(
            {row["configuration_id"] for row in plan["rows"]},
            set(runner.CONFIGURATION_ORDER),
        )

    def test_run_plan_isolation_identities_are_unique(self):
        _cohort, _config, _selection, plan = make_run_plan()
        for field in (
            "run_id",
            "task_branch",
            "worktree_id",
            "container_name",
            "judge_container_name",
            "state_id",
        ):
            values = [row[field] for row in plan["rows"]]
            self.assertEqual(len(values), len(set(values)), field)

    def test_run_plan_contains_no_host_absolute_path(self):
        _cohort, _config, _selection, plan = make_run_plan()
        text = json.dumps(plan)
        self.assertNotIn("E:\\", text)
        self.assertNotIn("C:\\", text)
        self.assertNotIn("/home/", text)

    def test_no_reflection_has_zero_retry_budget(self):
        _cohort, _config, _selection, plan = make_run_plan()
        rows = [
            row
            for row in plan["rows"]
            if row["configuration_id"] == "no_reflection"
        ]
        self.assertTrue(rows)
        self.assertTrue(all(row["budget"]["repair_attempts"] == 0 for row in rows))

    def test_primary_and_ablation_purposes_are_distinct(self):
        _cohort, _config, _selection, plan = make_run_plan()
        for row in plan["rows"]:
            expected = (
                "final_report"
                if row["configuration_id"] == "primary"
                else "ablation_report"
            )
            self.assertEqual(row["purpose"], expected)

    def test_selection_order_does_not_change_selected_set(self):
        cohort, selection = make_materialized_cohort()
        rows = runner.load_jsonl_bytes(selection, label="selection")
        rows.reverse()
        reversed_bytes = b"".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
            for row in rows
        )
        cohort["selection"]["selection_log_sha256"] = runner.sha256_bytes(
            reversed_bytes
        )
        observed = runner.validate_selection(cohort, reversed_bytes)
        self.assertEqual(
            {row["instance_id"] for row in observed if row["selected"]},
            {task["instance_id"] for task in cohort["tasks"]},
        )

    def test_bad_seed_is_rejected(self):
        cohort = runner.load_json(ROOT / "swebench_repair" / "cohort-plan.json")
        cohort["selection"]["seed"] = "0" * 64
        with self.assertRaisesRegex(runner.PlanValidationError, "seed"):
            runner.validate_cohort(cohort)

    def test_repository_role_overlap_is_rejected(self):
        cohort, _selection = make_materialized_cohort()
        repo = cohort["tasks"][0]["repository"]
        other = next(task for task in cohort["tasks"] if task["repository"] == repo)
        other["role"] = "development" if other["role"] != "development" else "tuning"
        with self.assertRaisesRegex(runner.PlanValidationError, "role counts|overlap"):
            runner.validate_cohort(cohort)

    def test_forbidden_repository_is_rejected(self):
        cohort, _selection = make_materialized_cohort()
        cohort["tasks"][0]["repository"] = "pallets/click"
        with self.assertRaisesRegex(runner.PlanValidationError, "forbidden"):
            runner.validate_cohort(cohort)

    def test_wrong_role_count_is_rejected(self):
        cohort, _selection = make_materialized_cohort()
        cohort["tasks"].pop()
        with self.assertRaisesRegex(runner.PlanValidationError, "30 tasks"):
            runner.validate_cohort(cohort)

    def test_duplicate_task_is_rejected(self):
        cohort, _selection = make_materialized_cohort()
        cohort["tasks"][1]["instance_id"] = cohort["tasks"][0]["instance_id"]
        with self.assertRaisesRegex(runner.PlanValidationError, "duplicate"):
            runner.validate_cohort(cohort)

    def test_selection_log_needs_six_allocatable_repositories(self):
        cohort, selection = make_materialized_cohort()
        rows = runner.load_jsonl_bytes(selection, label="selection")
        removed_repositories = set(
            sorted({row["repository"] for row in rows})[:2]
        )
        rows = [
            row for row in rows if row["repository"] not in removed_repositories
        ]
        data = b"".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
            for row in rows
        )
        cohort["selection"]["selection_log_sha256"] = runner.sha256_bytes(data)
        cohort["dataset"]["manifest_task_count"] = len(rows)
        with self.assertRaisesRegex(runner.PlanValidationError, "six allocatable"):
            runner.validate_selection(cohort, data)

    def test_selection_log_exact_byte_hash_is_enforced(self):
        cohort, selection = make_materialized_cohort()
        with self.assertRaisesRegex(runner.PlanValidationError, "byte hash"):
            runner.validate_selection(cohort, selection + b"\n")

    def test_selection_rank_tampering_is_rejected(self):
        cohort, selection = make_materialized_cohort()
        rows = runner.load_jsonl_bytes(selection, label="selection")
        rows[0]["task_rank_sha256"] = "0" * 64
        data = b"".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
            for row in rows
        )
        cohort["selection"]["selection_log_sha256"] = runner.sha256_bytes(data)
        with self.assertRaisesRegex(runner.PlanValidationError, "task rank"):
            runner.validate_selection(cohort, data)

    def test_selection_flag_tampering_is_rejected(self):
        cohort, selection = make_materialized_cohort()
        rows = runner.load_jsonl_bytes(selection, label="selection")
        rows[0]["selected"] = not rows[0]["selected"]
        data = b"".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
            for row in rows
        )
        cohort["selection"]["selection_log_sha256"] = runner.sha256_bytes(data)
        with self.assertRaisesRegex(
            runner.PlanValidationError, "selection flag|selected row"
        ):
            runner.validate_selection(cohort, data)

    def test_single_size_band_repository_is_not_allocatable(self):
        cohort, selection = make_materialized_cohort()
        rows = runner.load_jsonl_bytes(selection, label="selection")
        target_repo = rows[0]["repository"]
        for row in rows:
            if row["repository"] == target_repo:
                row["patch_changed_lines"] = 1
                row["size_band"] = "small"
        data = b"".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
            for row in rows
        )
        cohort["selection"]["selection_log_sha256"] = runner.sha256_bytes(data)
        with self.assertRaisesRegex(
            runner.PlanValidationError, "six allocatable|selection role mismatch"
        ):
            runner.validate_selection(cohort, data)

    def test_assigned_repository_may_include_ineligible_audit_row(self):
        cohort, selection = make_materialized_cohort()
        rows = runner.load_jsonl_bytes(selection, label="selection")
        assigned_repo = cohort["tasks"][0]["repository"]
        instance_id = "excluded_flaky_task"
        rows.append(
            {
                "instance_id": instance_id,
                "repository": assigned_repo,
                "eligible": False,
                "exclusion_reason": "flaky",
                "selected": False,
                "role": None,
                "patch_changed_lines": 2,
                "size_band": "small",
                "repository_rank_sha256": runner.repository_rank(
                    cohort["selection"]["seed"], assigned_repo
                ),
                "task_rank_sha256": runner.task_rank(
                    cohort["selection"]["seed"], instance_id
                ),
            }
        )
        data = b"".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
            for row in rows
        )
        cohort["selection"]["selection_log_sha256"] = runner.sha256_bytes(data)
        cohort["dataset"]["manifest_task_count"] = len(rows)
        observed = runner.validate_selection(cohort, data)
        self.assertFalse(next(row for row in observed if row["instance_id"] == instance_id)["selected"])

        rows[-1]["role"] = cohort["tasks"][0]["role"]
        bad_data = b"".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
            for row in rows
        )
        cohort["selection"]["selection_log_sha256"] = runner.sha256_bytes(bad_data)
        with self.assertRaisesRegex(runner.PlanValidationError, "excluded row"):
            runner.validate_selection(cohort, bad_data)

    def test_manifest_task_count_prevents_silent_selection_row_deletion(self):
        cohort, selection = make_materialized_cohort()
        rows = runner.load_jsonl_bytes(selection, label="selection")
        data = b"".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
            for row in rows[:-1]
        )
        cohort["selection"]["selection_log_sha256"] = runner.sha256_bytes(data)
        with self.assertRaisesRegex(runner.PlanValidationError, "manifest task count"):
            runner.validate_selection(cohort, data)

    def test_size_band_is_recomputed_from_changed_lines(self):
        cohort, selection = make_materialized_cohort()
        rows = runner.load_jsonl_bytes(selection, label="selection")
        rows[0]["size_band"] = (
            "large" if rows[0]["size_band"] != "large" else "small"
        )
        data = b"".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
            for row in rows
        )
        cohort["selection"]["selection_log_sha256"] = runner.sha256_bytes(data)
        with self.assertRaisesRegex(runner.PlanValidationError, "size_band"):
            runner.validate_selection(cohort, data)

    def test_configuration_tampering_is_rejected(self):
        config = make_frozen_config()
        config["configurations"][0]["finder"] = "single"
        with self.assertRaisesRegex(runner.PlanValidationError, "must equal"):
            runner.validate_config_plan(config)

    def test_unfrozen_models_refuse_run_plan(self):
        cohort, selection = make_materialized_cohort()
        config = runner.load_json(ROOT / "swebench_repair" / "config-plan.json")
        with self.assertRaisesRegex(runner.PlanValidationError, "frozen exact models"):
            runner.generate_run_plan(
                cohort,
                config,
                selection_log_bytes=selection,
                agent_source_commit=AGENT_COMMIT,
                gold_freeze_commit=FREEZE_COMMIT,
                freeze_attestation_sha256=FREEZE_HASH,
                created_at=CREATED_AT,
            )

    def test_model_ablation_needs_distinct_model(self):
        config = make_frozen_config()
        config["model_slots"]["model_b"] = copy.deepcopy(
            config["model_slots"]["model_a"]
        )
        with self.assertRaisesRegex(runner.PlanValidationError, "different model"):
            runner.validate_config_plan(config)

    def test_run_plan_mutation_is_rejected(self):
        cohort, config, selection, plan = make_run_plan()
        plan["rows"][0]["container_name"] = "crag-w5-tampered"
        with self.assertRaisesRegex(runner.PlanValidationError, "regeneration"):
            runner.validate_run_plan(
                plan,
                cohort,
                config,
                selection_log_bytes=selection,
            )

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text('{"a":1,"a":2}', encoding="utf-8")
            with self.assertRaisesRegex(runner.PlanValidationError, "duplicate"):
                runner.load_json(path)

    def test_non_finite_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text('{"a":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(runner.PlanValidationError, "non-finite"):
                runner.load_json(path)

    def test_existing_eval_or_holdout_path_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            for directory in (
                "eval",
                "holdout",
                "EVAL.",
                "holdout ",
                "EVAL~1",
                "HOLDOUT~1",
            ):
                path = Path(tmp) / directory / "missing.json"
                with self.assertRaisesRegex(
                    runner.PlanValidationError, "forbidden inputs or outputs"
                ):
                    runner.load_json(path)

    def test_cli_validates_unmaterialized_plans(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = runner.main(
                [
                    "validate-plans",
                    "--cohort",
                    str(ROOT / "swebench_repair" / "cohort-plan.json"),
                    "--config",
                    str(ROOT / "swebench_repair" / "config-plan.json"),
                ]
            )
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(output.getvalue())["valid"])

    def test_cohort_schema_top_level_matches_normative_plan(self):
        schema = runner.load_json(
            ROOT / "swebench_repair" / "schemas" / "cohort.schema.json"
        )
        cohort = runner.load_json(ROOT / "swebench_repair" / "cohort-plan.json")
        self.assertEqual(set(schema["properties"]), set(cohort))
        self.assertEqual(set(schema["required"]), set(cohort))

    def test_synthetic_cohort_is_explicitly_unmaterialized_and_valid(self):
        cohort = runner.load_json(
            ROOT / "swebench_repair" / "examples" / "synthetic-cohort.json"
        )
        runner.validate_cohort(cohort)
        self.assertFalse(cohort["materialized"])
        self.assertEqual(cohort["tasks"], [])

    def test_run_plan_schema_and_shape_example_have_matching_properties(self):
        schema = runner.load_json(
            ROOT / "swebench_repair" / "schemas" / "run-plan.schema.json"
        )
        example = runner.load_json(
            ROOT / "swebench_repair" / "examples" / "synthetic-run-plan.json"
        )
        self.assertEqual(set(schema["properties"]), set(example))
        self.assertEqual(
            set(schema["$defs"]["row"]["properties"]),
            set(example["rows"][0]),
        )


if __name__ == "__main__":
    unittest.main()
