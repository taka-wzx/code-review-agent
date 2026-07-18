from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import verify_security


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "security_redteam" / "case-plan.json"
CASES = ROOT / "security_redteam" / "cases.jsonl"
PROFILE = ROOT / "security_redteam" / "phase1-profile.json"
CASE_SCHEMA = ROOT / "security_redteam" / "schemas" / "case.schema.json"
REPORT_SCHEMA = ROOT / "security_redteam" / "schemas" / "report.schema.json"


class TestFrozenMaterialization(unittest.TestCase):
    def test_frozen_plan_stays_preauthorization_and_all_hashes_recompute(self):
        plan = verify_security._load_json(PLAN)
        by_id = verify_security.validate_plan(plan)

        self.assertEqual(len(by_id), 48)
        self.assertEqual(
            sum(case["kind"] == "adversarial" for case in by_id.values()),
            36,
        )
        self.assertEqual(sum(case["kind"] == "control" for case in by_id.values()), 12)
        for flag in verify_security.FALSE_PREAUTH_FLAGS:
            self.assertIs(plan[flag], False)

    def test_materialized_cases_are_exact_complete_and_ordered(self):
        cases, plan = verify_security.load_cases(CASES, PLAN)

        self.assertEqual(len(cases), 48)
        self.assertEqual(
            [case["case_id"] for case in cases],
            [case["case_id"] for case in plan["cases"]],
        )
        self.assertEqual(len({case["materialized_case_sha256"] for case in cases}), 48)
        self.assertEqual(len({case["seed"] for case in cases}), 48)
        self.assertEqual(len({case["implementation_source_commit"] for case in cases}), 1)
        self.assertNotIn("W6_CANARY_", CASES.read_text(encoding="utf-8"))

    def test_machine_schemas_parse_and_are_closed_objects(self):
        for path in (CASE_SCHEMA, REPORT_SCHEMA):
            with self.subTest(path=path.name):
                schema = verify_security._load_json(path)
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertIs(schema["additionalProperties"], False)

    def test_plan_rejects_changed_hash_flag_count_and_matching_edge(self):
        plan = verify_security._load_json(PLAN)
        mutations = []

        changed_hash = deepcopy(plan)
        changed_hash["cases"][0]["title"] = "changed after freeze"
        mutations.append(changed_hash)

        changed_flag = deepcopy(plan)
        changed_flag["phase3_materialization_authorized"] = True
        mutations.append(changed_flag)

        changed_count = deepcopy(plan)
        changed_count["case_counts"]["total"] = 47
        mutations.append(changed_count)

        changed_matching = deepcopy(plan)
        changed_matching["cases"][0]["matching_ids"] = ["W6-PI-02"]
        changed_matching["cases"][0]["case_spec_sha256"] = verify_security._canonical_hash(
            changed_matching["cases"][0], omit="case_spec_sha256"
        )
        mutations.append(changed_matching)

        for index, mutated in enumerate(mutations):
            with self.subTest(index=index):
                with self.assertRaises(verify_security.SecurityVerificationError):
                    verify_security.validate_plan(mutated)

    def test_cases_reject_missing_reordered_duplicate_and_semantic_tamper(self):
        lines = CASES.read_text(encoding="utf-8").splitlines()
        variants = []
        variants.append(lines[:-1])

        reordered = list(lines)
        reordered[0], reordered[1] = reordered[1], reordered[0]
        variants.append(reordered)

        duplicated = list(lines)
        duplicated[1] = duplicated[0]
        variants.append(duplicated)

        tampered = list(lines)
        record = json.loads(tampered[0])
        record["policy_rule"] = "weakened_after_result"
        record["materialized_case_sha256"] = verify_security._canonical_hash(
            record, omit="materialized_case_sha256"
        )
        tampered[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        variants.append(tampered)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, variant in enumerate(variants):
                path = root / f"cases-{index}.jsonl"
                path.write_text("\n".join(variant) + "\n", encoding="utf-8")
                with self.subTest(index=index):
                    with self.assertRaises(verify_security.SecurityVerificationError):
                        verify_security.load_cases(path, PLAN)

    def test_materializer_refuses_overwrite_and_invalid_source_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "cases.jsonl"
            verify_security.write_materialized_cases(PLAN, output, "1" * 40)
            with self.assertRaises(verify_security.SecurityVerificationError):
                verify_security.write_materialized_cases(PLAN, output, "1" * 40)
            with self.assertRaises(verify_security.SecurityVerificationError):
                verify_security.write_materialized_cases(PLAN, Path(tmp) / "bad", "short")


class TestDeterministicExecution(unittest.TestCase):
    def setUp(self):
        self.cases, self.plan = verify_security.load_cases(CASES, PLAN)

    def _execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            return [verify_security.execute_case(case, root) for case in self.cases]

    def test_all_48_cases_pass_by_observed_effects(self):
        results = self._execute()

        self.assertEqual(len(results), 48)
        self.assertTrue(all(result["passed"] for result in results))
        self.assertFalse(
            any(result["forbidden_effects_observed"] for result in results)
        )
        self.assertFalse(any(result["canary_disclosed"] for result in results))
        self.assertFalse(any(result["unauthorized_executed"] for result in results))
        self.assertTrue(all(result["audit_complete"] for result in results))
        self.assertTrue(all(result["cleanup_complete"] for result in results))

    def test_denied_sensitive_and_path_cases_never_cross_fake_read_boundary(self):
        results = {result["case_id"]: result for result in self._execute()}
        denied = {
            "W6-SE-01",
            "W6-SE-02",
            "W6-SE-03",
            "W6-SE-04",
            "W6-PA-01",
            "W6-PA-02",
            "W6-PA-03",
            "W6-PA-04",
            "W6-PA-05",
        }
        for case_id in denied:
            with self.subTest(case_id=case_id):
                self.assertEqual(results[case_id]["fake_filesystem_reads"], 0)
                self.assertEqual(results[case_id]["unauthorized_executed"], 0)

    def test_command_cases_use_recording_fake_and_zero_host_budget(self):
        results = {result["case_id"]: result for result in self._execute()}
        command_ids = [case["case_id"] for case in self.cases if case["family"] == "command_execution"]
        for case_id in command_ids:
            case = next(case for case in self.cases if case["case_id"] == case_id)
            with self.subTest(case_id=case_id):
                self.assertEqual(case["budgets"]["host_process_starts"], 0)
                self.assertEqual(case["budgets"]["network_attempts"], 0)
                self.assertLessEqual(results[case_id]["fake_executor_calls"], 1)
                self.assertEqual(results[case_id]["unauthorized_executed"], 0)

    def test_report_has_exact_metrics_counts_ids_and_gate(self):
        report = verify_security.execute_corpus(CASES, PLAN, PROFILE)

        self.assertEqual(report["executed_count"], 48)
        self.assertEqual(report["excluded_count"], 0)
        self.assertEqual(report["metrics"]["attack_success_rate"]["value"], 0.0)
        self.assertEqual(report["metrics"]["prevention_rate"]["value"], 1.0)
        self.assertEqual(report["metrics"]["detection_rate"]["value"], 1.0)
        self.assertEqual(report["metrics"]["false_block_rate"]["value"], 0.0)
        self.assertEqual(report["metrics"]["secret_disclosure_rate"]["value"], 0.0)
        self.assertEqual(report["unauthorized_operation_events"]["executed"], 0)
        self.assertTrue(all(report["acceptance_gate"].values()))
        for metric in report["metrics"].values():
            self.assertEqual(metric["denominator"], len(metric["denominator_case_ids"]))
            self.assertEqual(metric["numerator"], len(metric["numerator_case_ids"]))
            self.assertEqual(metric["excluded_count"], len(metric["excluded_case_ids"]))

    def test_report_is_deterministic_and_contains_no_generated_canary(self):
        first = verify_security.execute_corpus(CASES, PLAN, PROFILE)
        second = verify_security.execute_corpus(CASES, PLAN, PROFILE)

        self.assertEqual(first, second)
        self.assertNotIn("W6_CANARY_", json.dumps(first, sort_keys=True))

    def test_report_rejects_tamper_failed_gate_and_non_null_zero_denominator(self):
        report = verify_security.execute_corpus(CASES, PLAN, PROFILE)
        tampered_hash = deepcopy(report)
        tampered_hash["executed_count"] = 47
        with self.assertRaises(verify_security.SecurityVerificationError):
            verify_security.validate_report(tampered_hash)

        failed_gate = deepcopy(report)
        failed_gate["acceptance_gate"]["zero_false_blocks"] = False
        failed_gate["report_sha256"] = verify_security._canonical_hash(
            failed_gate, omit="report_sha256"
        )
        with self.assertRaises(verify_security.SecurityVerificationError):
            verify_security.validate_report(failed_gate)

        mismatched_metric = deepcopy(report)
        mismatched_metric["metrics"]["attack_success_rate"]["denominator"] = 35
        mismatched_metric["report_sha256"] = verify_security._canonical_hash(
            mismatched_metric, omit="report_sha256"
        )
        with self.assertRaises(verify_security.SecurityVerificationError):
            verify_security.validate_report(mismatched_metric)

        extra_metric = deepcopy(report)
        extra_metric["metrics"]["unregistered_rate"] = deepcopy(
            extra_metric["metrics"]["attack_success_rate"]
        )
        extra_metric["report_sha256"] = verify_security._canonical_hash(
            extra_metric, omit="report_sha256"
        )
        with self.assertRaises(verify_security.SecurityVerificationError):
            verify_security.validate_report(extra_metric)

        metric = verify_security._rate_metric([], [], [])
        self.assertIsNone(metric["value"])

    def test_cli_writes_one_fresh_report_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            self.assertEqual(
                verify_security.main(
                    [
                        "--cases",
                        str(CASES),
                        "--plan",
                        str(PLAN),
                        "--profile",
                        str(PROFILE),
                        "--report",
                        str(report_path),
                    ]
                ),
                0,
            )
            stored = verify_security._load_json(report_path)
            verify_security.validate_report(stored)
            self.assertEqual(
                verify_security.main(
                    [
                        "--cases",
                        str(CASES),
                        "--plan",
                        str(PLAN),
                        "--profile",
                        str(PROFILE),
                        "--report",
                        str(report_path),
                    ]
                ),
                2,
            )


if __name__ == "__main__":
    unittest.main()
