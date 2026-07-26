from __future__ import annotations

import ast
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import phase9g_pilot as pilot


class Phase9GPilotPrepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = pilot.build_synthetic_bundle()

    @staticmethod
    def rehash(value: dict[str, object], field: str) -> dict[str, object]:
        value[field] = ""
        return pilot.with_artifact_hash(value, field)

    def test_synthetic_bundle_exercises_protocol_but_opens_no_real_gate(self) -> None:
        result = pilot.validate_bundle(self.bundle)
        self.assertTrue(result["valid"])
        self.assertTrue(result["synthetic"])
        self.assertEqual(result["selected_business_prs"], 20)
        self.assertFalse(result["business_claim_allowed"])
        self.assertFalse(result["quality_claim_allowed"])
        self.assertTrue(
            all(not scope["ready"] for scope in result["authorization_scopes"].values())
        )

    def test_compact_synthetic_descriptor_is_hash_bound(self) -> None:
        descriptor = {
            "schema_version": 1,
            "phase_id": pilot.PHASE_ID,
            "fixture": "built_in_synthetic_v1",
            "expected_bundle_sha256": self.bundle["bundle_sha256"],
            "business_claim_allowed": False,
            "quality_claim_allowed": False,
            "fixture_sha256": "",
        }
        descriptor = pilot.with_artifact_hash(descriptor, "fixture_sha256")
        self.assertTrue(pilot.validate_bundle_fixture(descriptor)["valid"])
        descriptor["quality_claim_allowed"] = True
        descriptor = self.rehash(descriptor, "fixture_sha256")
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_bundle_fixture(descriptor)

    def test_authorization_unknown_or_null_field_fails_closed(self) -> None:
        authorization = deepcopy(self.bundle["authorization"])
        authorization["unexpected"] = True
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_authorization(authorization)

        authorization = deepcopy(self.bundle["authorization"])
        authorization["model"]["provider"] = None
        authorization = self.rehash(authorization, "authorization_sha256")
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_authorization(authorization)

    def test_authorization_tamper_and_expiry_fail(self) -> None:
        authorization = deepcopy(self.bundle["authorization"])
        authorization["approved_by"] = "different-approver"
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_authorization(authorization)

        authorization = deepcopy(self.bundle["authorization"])
        authorization["expires_at"] = "2026-01-02T00:00:00Z"
        authorization = self.rehash(authorization, "authorization_sha256")
        readiness = pilot.authorization_readiness(authorization)
        self.assertIn(
            "authorization_expired", readiness["scopes"]["business"]["blocked_by"]
        )

    def test_participant_count_and_real_confirmation_gate(self) -> None:
        authorization = deepcopy(self.bundle["authorization"])
        authorization["business_pilot"]["participant_ids"] = ["only-a", "only-b"]
        authorization = self.rehash(authorization, "authorization_sha256")
        readiness = pilot.authorization_readiness(authorization)
        self.assertIn(
            "participant_count_outside_3_5",
            readiness["scopes"]["business"]["blocked_by"],
        )
        self.assertIn(
            "participants_not_confirmed_real",
            readiness["scopes"]["business"]["blocked_by"],
        )

    def test_publish_cannot_exceed_github_authority(self) -> None:
        authorization = deepcopy(self.bundle["authorization"])
        authorization["business_pilot"]["mode"] = "publish"
        authorization["business_pilot"]["real_github_publish"] = True
        authorization["business_pilot"]["publish_approver_id"] = "human-approver"
        authorization = self.rehash(authorization, "authorization_sha256")
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_authorization(authorization)

    def test_repository_grant_cannot_exceed_authorization(self) -> None:
        repositories = deepcopy(self.bundle["repositories"])
        row = repositories["repositories"][0]
        row["real_github_api_authorized"] = True
        repositories["repositories"][0] = self.rehash(row, "repository_sha256")
        repositories = self.rehash(repositories, "manifest_sha256")
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_repository_manifest(repositories, self.bundle["authorization"])

    def test_selection_rank_tamper_fails_even_when_row_is_rehashed(self) -> None:
        rows = deepcopy(self.bundle["selection_log"])
        rows[0]["rank_sha256"] = "f" * 64
        rows[0] = self.rehash(rows[0], "row_sha256")
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_selection_log(
                rows, self.bundle["selection_plan"], self.bundle["repositories"]
            )

    def test_selected_snapshot_cannot_be_reused_under_another_identity(self) -> None:
        rows = deepcopy(self.bundle["selection_log"])
        rows[1]["snapshot_sha256"] = rows[0]["snapshot_sha256"]
        rows[1] = self.rehash(rows[1], "row_sha256")
        with self.assertRaisesRegex(pilot.ValidationError, "reuse one immutable snapshot"):
            pilot.validate_selection_log(
                rows, self.bundle["selection_plan"], self.bundle["repositories"]
            )

    def test_excluded_candidate_does_not_require_unauthorized_diff_or_snapshot(self) -> None:
        rows = deepcopy(self.bundle["selection_log"])
        pr_id = "synthetic-excluded-pr"
        rows.append(
            pilot.with_artifact_hash(
                {
                    "schema_version": 1,
                    "pilot_id": "synthetic-pilot-v1",
                    "track": "business",
                    "role": "pilot",
                    "repository_id": "synthetic-repository",
                    "pr_id": pr_id,
                    "merged_at": "2025-02-01T00:00:00Z",
                    "eligible": False,
                    "exclusion_reason": "not_reproducible",
                    "selected": False,
                    "rank_sha256": pilot.selection_rank(
                        self.bundle["selection_plan"]["seed"], pr_id
                    ),
                    "snapshot_sha256": None,
                    "diff_sha256": None,
                    "synthetic": True,
                    "row_sha256": "",
                },
                "row_sha256",
            )
        )
        validated = pilot.validate_selection_log(
            rows, self.bundle["selection_plan"], self.bundle["repositories"]
        )
        self.assertEqual(len(validated), 21)

    def test_business_selection_target_must_be_between_20_and_30(self) -> None:
        plan = deepcopy(self.bundle["selection_plan"])
        plan["groups"][0]["target_prs"] = 19
        plan = self.rehash(plan, "plan_sha256")
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_selection_plan(plan)

    def test_selection_seed_cannot_be_shopped_after_source_commit_freeze(self) -> None:
        plan = deepcopy(self.bundle["selection_plan"])
        plan["seed"] = "f" * 64
        plan = self.rehash(plan, "plan_sha256")
        with self.assertRaisesRegex(pilot.ValidationError, "source-commit derivation"):
            pilot.validate_selection_plan(plan)

        plan = deepcopy(self.bundle["selection_plan"])
        plan["seed_derivation"]["source_commit"] = "c" * 40
        plan["seed"] = pilot.derive_selection_seed("c" * 40)
        plan = self.rehash(plan, "plan_sha256")
        with self.assertRaisesRegex(pilot.ValidationError, "expected Phase 9G-Prep"):
            pilot.validate_selection_plan(
                plan,
                expected_source_commit="b" * 40,
            )

    def test_feedback_and_formal_annotation_schemas_are_not_interchangeable(self) -> None:
        packet = self.bundle["annotation_packets"][0]
        feedback = self.bundle["feedback_responses"][0]
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_annotation_responses(
                [feedback],
                packet,
                self.bundle["annotation_subjects"],
                self.bundle["cohort"],
            )

    def test_feedback_reject_requires_human_rationale(self) -> None:
        responses = deepcopy(self.bundle["feedback_responses"])
        responses[0]["decision"] = "rejected"
        responses[0]["rationale"] = None
        responses[0] = self.rehash(responses[0], "response_sha256")
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_feedback_responses(
                responses,
                self.bundle["feedback_packets"],
                self.bundle["finding_subjects"],
                self.bundle["cohort"],
            )

    def test_feedback_packet_hash_and_partial_response_denominator_are_preserved(self) -> None:
        packets = deepcopy(self.bundle["feedback_packets"])
        packets[0]["packet_sha256"] = "0" * 64
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_feedback_packet(
                packets[0], self.bundle["finding_subjects"], self.bundle["cohort"]
            )
        partial = pilot.validate_feedback_responses(
            self.bundle["feedback_responses"][:-1],
            self.bundle["feedback_packets"],
            self.bundle["finding_subjects"],
            self.bundle["cohort"],
        )
        report = pilot.build_business_report(
            authorization=self.bundle["authorization"],
            participants=self.bundle["participants"],
            repositories=self.bundle["repositories"],
            cohort=self.bundle["cohort"],
            finding_subjects=self.bundle["finding_subjects"],
            feedback_packets=self.bundle["feedback_packets"],
            feedback_responses=partial,
            review_times=self.bundle["review_times"],
            receipts=self.bundle["run_receipts"],
            run_manifest=self.bundle["run_manifest"],
            generated_at="2026-01-09T00:00:00Z",
        )
        self.assertEqual(report["business_outcome"]["feedback_coverage"]["numerator"], 19)
        self.assertEqual(report["business_outcome"]["feedback_coverage"]["denominator"], 20)
        self.assertEqual(report["business_outcome"]["feedback_coverage"]["missing"], 1)
        self.assertFalse(report["claim_gates"]["business_claim_allowed"])
        self.assertIn("feedback_coverage_incomplete", report["claim_gates"]["blocked_by"])

    def test_feedback_packet_requires_consented_participant_and_every_pr(self) -> None:
        packets = deepcopy(self.bundle["feedback_packets"])
        packets[0]["participant_id"] = "foreign-participant"
        packets[0] = self.rehash(packets[0], "packet_sha256")
        with self.assertRaisesRegex(pilot.ValidationError, "unknown participant"):
            pilot.validate_feedback_packet_assignments(
                packets, self.bundle["participants"], self.bundle["cohort"]
            )
        with self.assertRaisesRegex(pilot.ValidationError, "every business PR"):
            pilot.validate_feedback_packet_assignments(
                self.bundle["feedback_packets"][:-1],
                self.bundle["participants"],
                self.bundle["cohort"],
            )

    def test_review_time_cannot_exceed_wall_time(self) -> None:
        rows = deepcopy(self.bundle["review_times"])
        rows[0]["active_seconds"] = 121.0
        rows[0] = self.rehash(rows[0], "record_sha256")
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_review_times(
                rows, self.bundle["cohort"], self.bundle["participants"]
            )

    def test_cumulative_model_budget_counts_failed_and_nonheadline_attempts(self) -> None:
        authorization = deepcopy(self.bundle["authorization"])
        authorization["model"]["max_logical_calls"] = 1
        authorization["model"]["max_http_attempts"] = 1
        authorization = self.rehash(authorization, "authorization_sha256")
        receipts = deepcopy(self.bundle["run_receipts"])
        for index in (0, 1):
            receipts[index]["logical_calls"] = 1
            receipts[index]["http_attempts"] = 1
            receipts[index] = self.rehash(receipts[index], "receipt_sha256")
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_run_receipts(receipts, self.bundle["cohort"], authorization)

    def test_headline_receipt_must_bind_exact_feedback_finding_set(self) -> None:
        receipts = deepcopy(self.bundle["run_receipts"])
        receipts[0]["feedback_eligible_finding_ids"] = ["foreign-finding"]
        receipts[0] = self.rehash(receipts[0], "receipt_sha256")
        receipts = pilot.validate_run_receipts(
            receipts, self.bundle["cohort"], self.bundle["authorization"]
        )
        with self.assertRaisesRegex(pilot.ValidationError, "complete feedback-eligible"):
            pilot.validate_receipt_finding_bindings(
                receipts, self.bundle["finding_subjects"]
            )

    def test_raw_trace_retention_cannot_be_shorter_than_authorized(self) -> None:
        receipts = deepcopy(self.bundle["run_receipts"])
        receipts[0]["raw_trace_retain_until"] = "2026-01-08T00:00:00Z"
        receipts[0] = self.rehash(receipts[0], "receipt_sha256")
        with self.assertRaisesRegex(pilot.ValidationError, "below authorized retention"):
            pilot.validate_run_receipts(
                receipts, self.bundle["cohort"], self.bundle["authorization"]
            )

    def test_successful_rerun_does_not_replace_headline_failure(self) -> None:
        authorization = deepcopy(self.bundle["authorization"])
        authorization["model"]["max_logical_calls"] = 2
        authorization["model"]["max_http_attempts"] = 2
        authorization = self.rehash(authorization, "authorization_sha256")
        receipts = deepcopy(self.bundle["run_receipts"])
        receipts[0]["status"] = "failed"
        receipts[0]["error_category"] = "internal"
        receipts[0] = self.rehash(receipts[0], "receipt_sha256")
        rerun = deepcopy(receipts[0])
        rerun.update(
            {
                "run_id": "synthetic-rerun-01",
                "attempt_number": 2,
                "headline": False,
                "status": "completed",
                "error_category": None,
                "started_at": "2026-01-07T00:01:00Z",
                "completed_at": "2026-01-07T00:01:10Z",
            }
        )
        rerun = self.rehash(rerun, "receipt_sha256")
        receipts.append(rerun)
        receipts = pilot.validate_run_receipts(receipts, self.bundle["cohort"], authorization)
        manifest = pilot.build_run_manifest(
            receipts, self.bundle["cohort"], created_at="2026-01-08T00:00:00Z"
        )
        manifest = pilot.validate_run_manifest(manifest, receipts, self.bundle["cohort"])
        report = pilot.build_business_report(
            authorization=authorization,
            participants=self.bundle["participants"],
            repositories=self.bundle["repositories"],
            cohort=self.bundle["cohort"],
            finding_subjects=self.bundle["finding_subjects"],
            feedback_packets=self.bundle["feedback_packets"],
            feedback_responses=self.bundle["feedback_responses"],
            review_times=self.bundle["review_times"],
            receipts=receipts,
            run_manifest=manifest,
            generated_at="2026-01-09T00:00:00Z",
        )
        self.assertEqual(report["business_outcome"]["completion"]["numerator"], 19)
        self.assertEqual(report["business_outcome"]["retry_attempts"], 1)

    def test_later_attempt_cannot_become_headline(self) -> None:
        receipts = deepcopy(self.bundle["run_receipts"])
        rerun = deepcopy(receipts[0])
        rerun.update({"run_id": "synthetic-rerun", "attempt_number": 2, "headline": True})
        rerun = self.rehash(rerun, "receipt_sha256")
        receipts.append(rerun)
        manifest = pilot.build_run_manifest(
            receipts, self.bundle["cohort"], created_at="2026-01-08T00:00:00Z"
        )
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_run_manifest(manifest, receipts, self.bundle["cohort"])

    def test_run_manifest_requires_formal_as_well_as_business_headlines(self) -> None:
        cohort = deepcopy(self.bundle["cohort"])
        formal_entry = deepcopy(cohort["entries"][0])
        formal_entry.update(
            {
                "track": "formal",
                "role": "reporting",
                "pr_id": "synthetic-formal-pr",
                "snapshot_sha256": "d" * 64,
                "diff_sha256": "e" * 64,
            }
        )
        cohort["entries"].append(formal_entry)
        cohort = self.rehash(cohort, "cohort_sha256")
        receipts = deepcopy(self.bundle["run_receipts"])
        formal_receipt = deepcopy(receipts[0])
        formal_receipt.update(
            {
                "run_id": "synthetic-formal-run",
                "track": "formal",
                "role": "reporting",
                "pr_id": "synthetic-formal-pr",
                "feedback_eligible_finding_ids": [],
            }
        )
        formal_receipt = self.rehash(formal_receipt, "receipt_sha256")
        receipts.append(formal_receipt)
        receipts = pilot.validate_run_receipts(
            receipts, cohort, self.bundle["authorization"]
        )
        manifest = pilot.build_run_manifest(
            receipts, cohort, created_at="2026-01-08T00:00:00Z"
        )
        self.assertEqual(len(pilot.validate_run_manifest(manifest, receipts, cohort)["attempts"]), 21)
        business_only = [receipt for receipt in receipts if receipt["track"] == "business"]
        incomplete = pilot.build_run_manifest(
            business_only, cohort, created_at="2026-01-08T00:00:00Z"
        )
        with self.assertRaisesRegex(pilot.ValidationError, "every selected cohort"):
            pilot.validate_run_manifest(incomplete, business_only, cohort)

    def test_annotation_packets_require_distinct_people_and_bindings(self) -> None:
        packet_a = deepcopy(self.bundle["annotation_packets"][0])
        packet_b = deepcopy(self.bundle["annotation_packets"][1])
        packet_b["annotator_id"] = packet_a["annotator_id"]
        packet_b = self.rehash(packet_b, "packet_sha256")
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_independent_annotation_pair(packet_a, packet_b)

        packet_b = deepcopy(self.bundle["annotation_packets"][1])
        packet_b["rubric_sha256"] = "f" * 64
        packet_b = self.rehash(packet_b, "packet_sha256")
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_independent_annotation_pair(packet_a, packet_b)

    def test_adjudicator_cannot_repeat_a_or_b(self) -> None:
        packet_a = self.bundle["annotation_packets"][0]
        packet_b = self.bundle["annotation_packets"][1]
        response_groups = {
            group["packet_id"]: group["responses"]
            for group in self.bundle["annotation_responses"]
        }
        with self.assertRaises(pilot.ValidationError):
            pilot.build_adjudication_packet(
                self.bundle["annotation_subjects"],
                self.bundle["cohort"],
                packet_a,
                packet_b,
                response_groups[packet_a["packet_id"]],
                response_groups[packet_b["packet_id"]],
                adjudicator_id=packet_a["annotator_id"],
                order_seed=7,
                generated_at="2026-01-06T00:01:00Z",
            )

    def test_adjudication_cannot_remain_uncertain(self) -> None:
        adjudication = self.bundle["annotation_packets"][2]
        group = next(
            group
            for group in self.bundle["annotation_responses"]
            if group["packet_id"] == adjudication["packet_id"]
        )
        responses = deepcopy(group["responses"])
        responses[0]["label"] = "uncertain"
        responses[0] = self.rehash(responses[0], "response_sha256")
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_annotation_responses(
                responses,
                adjudication,
                self.bundle["annotation_subjects"],
                self.bundle["cohort"],
            )

    def test_gold_candidate_must_be_discovered_by_a_or_b(self) -> None:
        packet_a, packet_b, adjudication = self.bundle["annotation_packets"][:3]
        groups = {
            group["packet_id"]: deepcopy(group["responses"])
            for group in self.bundle["annotation_responses"]
        }
        responses_a = groups[packet_a["packet_id"]]
        responses_b = groups[packet_b["packet_id"]]
        for rows in (responses_a, responses_b):
            row = next(row for row in rows if row["subject_id"] == "synthetic-gold-2")
            row["discovered"] = False
            updated = self.rehash(row, "response_sha256")
            rows[rows.index(row)] = updated
        with self.assertRaisesRegex(pilot.ValidationError, "independently discovered"):
            pilot.build_gold_freeze(
                authorization=self.bundle["authorization"],
                cohort=self.bundle["cohort"],
                packet_a=packet_a,
                packet_b=packet_b,
                responses_a=responses_a,
                responses_b=responses_b,
                adjudication_packet=adjudication,
                responses_c=groups[adjudication["packet_id"]],
                frozen_at="2026-01-07T00:00:00Z",
                external_git_commit="a" * 40,
                trusted_cohort_sha256=self.bundle["gold_freeze"][
                    "trusted_cohort_sha256"
                ],
            )

    def test_adjudication_packet_must_bind_exact_a_b_response_hashes(self) -> None:
        bundle = deepcopy(self.bundle)
        adjudication = bundle["annotation_packets"][2]
        adjudication["items"][0]["source_annotations"][0]["response_sha256"] = "f" * 64
        adjudication = self.rehash(adjudication, "packet_sha256")
        bundle["annotation_packets"][2] = adjudication
        c_group = next(
            group
            for group in bundle["annotation_responses"]
            if group["packet_id"] == adjudication["packet_id"]
        )
        c_group["responses"][0]["packet_sha256"] = adjudication["packet_sha256"]
        c_group["responses"][0] = self.rehash(
            c_group["responses"][0], "response_sha256"
        )
        bundle = self.rehash(bundle, "bundle_sha256")
        with self.assertRaisesRegex(pilot.ValidationError, "exact A/B responses"):
            pilot.validate_bundle(bundle)

    def test_gold_freeze_alone_never_allows_quality_claim(self) -> None:
        freeze = pilot.validate_gold_freeze(self.bundle["gold_freeze"])
        self.assertTrue(freeze["synthetic"])
        self.assertFalse(freeze["real_run_ready"])
        self.assertFalse(freeze["quality_claim_allowed"])
        self.assertTrue(freeze["incomplete_gates"])

    def test_formal_report_validator_keeps_synthetic_quality_gate_closed(self) -> None:
        report = {
            "schema_version": 1,
            "metric_version": "trusted-review-v2",
            "generated_at": "2026-01-09T00:00:00Z",
            "split": "reporting",
            "gold_freeze_commit": "a" * 40,
            "agreement": {
                "annotators": ["synthetic-annotator-a", "synthetic-annotator-b"],
                "adjudicators": ["synthetic-adjudicator-c"],
                "overall": {"unresolved_subjects": 0},
            },
            "review": {"micro": {"precision": 1.0, "recall": 1.0, "f1": 1.0}},
            "bootstrap_95_ci": {"method": "percentile_pr_within_repository"},
        }
        receipt = pilot.validate_formal_quality_report(
            report,
            self.bundle["authorization"],
            self.bundle["gold_freeze"],
            validated_at="2026-01-10T00:00:00Z",
        )
        self.assertFalse(receipt["quality_claim_allowed"])
        self.assertIn("gold_freeze_not_real_run_ready", receipt["blocked_by"])
        self.assertIn("system_packet_provenance_not_bound", receipt["blocked_by"])
        self.assertFalse(receipt["business_outcome_measured"])

    def test_formal_report_requires_normative_metrics_and_bootstrap_shape(self) -> None:
        report = {
            "schema_version": 1,
            "metric_version": "trusted-review-v2",
            "generated_at": "2026-01-09T00:00:00Z",
            "cohort_id": "trusted-reporting-v1",
            "config_id": "frozen-v1",
            "split": "reporting",
            "source_commits": ["b" * 40],
            "gold_freeze_commit": "a" * 40,
            "frozen_cohort_sha256": self.bundle["gold_freeze"][
                "trusted_cohort_sha256"
            ],
            "provider": "synthetic-provider",
            "model_id": "synthetic-no-model",
            "pricing_revision": "synthetic-pricing-v1",
            "runtime_config_sha256": "c" * 64,
            "input_hashes": {
                "annotations_sha256": "1" * 64,
                "cohort_sha256": "2" * 64,
                "runs_sha256": "3" * 64,
                "selection_log_sha256": "4" * 64,
            },
            "agreement": {
                "annotators": ["synthetic-annotator-a", "synthetic-annotator-b"],
                "adjudicators": ["synthetic-adjudicator-c"],
                "overall": {"unresolved_subjects": 0},
            },
            "review": {
                "micro": {
                    "tp_findings": 3,
                    "fp_findings": 1,
                    "tp_gold": 3,
                    "fn_gold": 1,
                    "novel_valid": 0,
                    "duplicates": 0,
                    "unscorable": 0,
                    "precision": 0.75,
                    "recall": 0.75,
                    "f1": 0.75,
                },
                "repository_macro": {},
                "pr_macro": {},
                "by_repository": {},
                "per_pr": [],
            },
            "bootstrap_95_ci": {
                "method": "percentile_pr_within_repository",
                "seed": 20260718,
                "replicates": 10000,
                "alpha": 0.05,
                "precision": {
                    "low": 0.5,
                    "high": 0.9,
                    "defined_replicates": 10000,
                    "reason": None,
                },
                "recall": {
                    "low": 0.5,
                    "high": 0.9,
                    "defined_replicates": 10000,
                    "reason": None,
                },
                "f1": {
                    "low": 0.5,
                    "high": 0.9,
                    "defined_replicates": 10000,
                    "reason": None,
                },
            },
            "telemetry": {},
        }
        valid_shape = pilot.validate_formal_quality_report(
            report,
            self.bundle["authorization"],
            self.bundle["gold_freeze"],
            validated_at="2026-01-10T00:00:00Z",
        )
        self.assertNotIn("formal_review_micro_shape_invalid", valid_shape["blocked_by"])
        self.assertNotIn("formal_bootstrap_invalid", valid_shape["blocked_by"])

        report["review"]["micro"].pop("fn_gold")
        report["bootstrap_95_ci"]["alpha"] = 0.1
        invalid_shape = pilot.validate_formal_quality_report(
            report,
            self.bundle["authorization"],
            self.bundle["gold_freeze"],
            validated_at="2026-01-10T00:00:00Z",
        )
        self.assertIn("formal_review_micro_shape_invalid", invalid_shape["blocked_by"])
        self.assertIn("formal_bootstrap_not_95_ci", invalid_shape["blocked_by"])

    def test_business_report_never_contains_formal_quality_numbers(self) -> None:
        report = self.bundle["business_report"]
        self.assertFalse(report["model_quality"]["measured"])
        self.assertIsNone(report["model_quality"]["precision"])
        self.assertIsNone(report["model_quality"]["recall"])
        self.assertIsNone(report["model_quality"]["f1"])
        self.assertFalse(report["claim_gates"]["formal_quality_claim_allowed"])

    def test_forbidden_path_is_rejected_before_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            forbidden = Path(directory) / "holdout" / "missing.json"
            with self.assertRaisesRegex(pilot.ValidationError, "protected evaluation paths"):
                pilot.load_json(forbidden)

    def test_offline_tool_has_no_network_sdk_or_subprocess_import(self) -> None:
        source = Path(pilot.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(
            imported.isdisjoint(
                {
                    "subprocess",
                    "socket",
                    "urllib",
                    "requests",
                    "httpx",
                    "openai",
                    "github",
                }
            )
        )

    def test_authorization_template_is_deliberately_incomplete(self) -> None:
        template = json.loads(
            (Path(__file__).parents[1] / "phase9g" / "authorization.template.json").read_text(
                encoding="utf-8"
            )
        )
        with self.assertRaises(pilot.ValidationError):
            pilot.validate_authorization(template)

    def test_cli_validates_committed_synthetic_descriptor(self) -> None:
        path = Path(__file__).parents[1] / "phase9g" / "examples" / "synthetic"
        output = io.StringIO()
        with patch("sys.stdout", output):
            status = pilot.main(["validate-bundle", "--bundle", str(path)])
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result["business_claim_allowed"])
        self.assertFalse(result["quality_claim_allowed"])


if __name__ == "__main__":
    unittest.main()
