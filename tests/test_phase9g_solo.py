from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

import phase9g_solo as solo


class Phase9GSoloTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = solo.build_synthetic_bundle()

    @staticmethod
    def seal(value: dict[str, object], hash_field: str) -> dict[str, object]:
        return solo.with_artifact_hash({**value, hash_field: ""}, hash_field)

    def test_synthetic_bundle_validates_with_all_claims_closed(self) -> None:
        result = solo.validate_bundle(self.bundle)
        self.assertTrue(result["valid"])
        self.assertTrue(result["synthetic"])
        self.assertEqual(result["selected_prs"], 5)
        self.assertFalse(result["exploratory_summary_allowed"])
        self.assertFalse(result["business_claim_allowed"])
        self.assertFalse(result["quality_claim_allowed"])
        self.assertEqual(result["formal_quality_status"], "incomplete")
        self.assertFalse(any(result["authorization_scopes"].values()))

    def test_committed_synthetic_descriptor_validates(self) -> None:
        descriptor = solo.load_json("phase9g_solo/examples/synthetic/bundle.json")
        result = solo.validate_bundle_fixture(descriptor)
        self.assertTrue(result["valid"])
        self.assertFalse(result["exploratory_summary_allowed"])

    def test_synthetic_fixture_contains_retained_headline_failure(self) -> None:
        report = self.bundle["solo_report"]
        self.assertEqual(report["metrics"]["headline_status_counts"]["failed"], 1)
        self.assertEqual(report["metrics"]["headline_completed"], 4)
        self.assertEqual(
            report["metrics"]["diagnostic_attempts_after_headline_failure"], 1
        )

    def test_authorization_rejects_missing_key(self) -> None:
        authorization = copy.deepcopy(self.bundle["authorization"])
        del authorization["approved_by"]
        with self.assertRaisesRegex(solo.ValidationError, "missing keys"):
            solo.validate_authorization(authorization)

    def test_authorization_rejects_unknown_key(self) -> None:
        authorization = copy.deepcopy(self.bundle["authorization"])
        authorization["quality_override"] = True
        with self.assertRaisesRegex(solo.ValidationError, "unknown keys"):
            solo.validate_authorization(authorization)

    def test_expired_authorization_fails_real_scope_closed(self) -> None:
        authorization = copy.deepcopy(self.bundle["authorization"])
        authorization["expires_at"] = "2027-01-01T00:00:00Z"
        authorization = self.seal(authorization, "authorization_sha256")
        readiness = solo.authorization_readiness(authorization, at="2028-01-01T00:00:00Z")
        self.assertFalse(readiness["unexpired"])
        self.assertFalse(readiness["scopes"]["real_exploratory_run"])
        self.assertFalse(readiness["scopes"]["model_execution"])

    def test_future_authorization_fails_real_scope_closed(self) -> None:
        authorization = copy.deepcopy(self.bundle["authorization"])
        authorization["approved_at"] = "2028-01-01T00:00:00Z"
        authorization["expires_at"] = "2029-01-01T00:00:00Z"
        authorization = self.seal(authorization, "authorization_sha256")
        readiness = solo.authorization_readiness(authorization, at="2027-01-01T00:00:00Z")
        self.assertFalse(readiness["unexpired"])
        self.assertFalse(readiness["scopes"]["real_exploratory_run"])

    def test_one_real_participant_can_open_only_solo_and_model_scopes(self) -> None:
        authorization = copy.deepcopy(self.bundle["authorization"])
        authorization["participant_confirmed_real"] = True
        authorization["synthetic"] = False
        authorization["model"]["real_paid_calls"] = True
        authorization["model"]["read_raw_diff"] = True
        authorization = self.seal(authorization, "authorization_sha256")
        readiness = solo.authorization_readiness(authorization, at="2026-01-02T00:00:00Z")
        self.assertTrue(readiness["scopes"]["real_exploratory_run"])
        self.assertTrue(readiness["scopes"]["model_execution"])
        self.assertFalse(readiness["scopes"]["github_publish"])
        self.assertFalse(readiness["scopes"]["business_claim"])
        self.assertFalse(readiness["scopes"]["quality_claim"])

    def test_paid_call_denial_keeps_model_scope_closed(self) -> None:
        authorization = copy.deepcopy(self.bundle["authorization"])
        authorization["participant_confirmed_real"] = True
        authorization["synthetic"] = False
        authorization["model"]["read_raw_diff"] = True
        authorization["model"]["real_paid_calls"] = False
        authorization = self.seal(authorization, "authorization_sha256")
        readiness = solo.authorization_readiness(authorization, at="2026-01-02T00:00:00Z")
        self.assertTrue(readiness["scopes"]["real_exploratory_run"])
        self.assertFalse(readiness["scopes"]["model_execution"])

    def test_raw_diff_denial_keeps_model_scope_closed(self) -> None:
        authorization = copy.deepcopy(self.bundle["authorization"])
        authorization["participant_confirmed_real"] = True
        authorization["synthetic"] = False
        authorization["model"]["read_raw_diff"] = False
        authorization["model"]["real_paid_calls"] = True
        authorization = self.seal(authorization, "authorization_sha256")
        readiness = solo.authorization_readiness(authorization, at="2026-01-02T00:00:00Z")
        self.assertFalse(readiness["scopes"]["model_execution"])

    def test_non_synthetic_usage_without_model_authority_is_rejected(self) -> None:
        authorization = copy.deepcopy(self.bundle["authorization"])
        authorization["participant_confirmed_real"] = True
        authorization["synthetic"] = False
        authorization = self.seal(authorization, "authorization_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "lacks paid-call or raw-diff"):
            solo.validate_run_receipts(
                self.bundle["run_receipts"], self.bundle["cohort"], authorization
            )

    def test_external_operations_are_structurally_forbidden(self) -> None:
        for key in (
            "staging_deploy",
            "real_github_api",
            "create_comments_or_checks",
            "github_publish",
        ):
            with self.subTest(key=key):
                authorization = copy.deepcopy(self.bundle["authorization"])
                authorization["external_operations"][key] = True
                authorization = self.seal(authorization, "authorization_sha256")
                with self.assertRaisesRegex(solo.ValidationError, "structurally forbids"):
                    solo.validate_authorization(authorization)

    def test_deployment_target_must_be_null(self) -> None:
        authorization = copy.deepcopy(self.bundle["authorization"])
        authorization["external_operations"]["deployment_target"] = "staging-target"
        authorization = self.seal(authorization, "authorization_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "must be null"):
            solo.validate_authorization(authorization)

    def test_pr_count_must_be_five_to_ten(self) -> None:
        for count in (4, 11):
            with self.subTest(count=count):
                authorization = copy.deepcopy(self.bundle["authorization"])
                authorization["pr_count"] = count
                authorization = self.seal(authorization, "authorization_sha256")
                with self.assertRaises(solo.ValidationError):
                    solo.validate_authorization(authorization)

    def test_participant_manifest_requires_exactly_one_identity(self) -> None:
        participants = copy.deepcopy(self.bundle["participants"])
        participants["participants"].append(copy.deepcopy(participants["participants"][0]))
        participants = self.seal(participants, "manifest_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "exactly one"):
            solo.validate_participant_manifest(participants, self.bundle["authorization"])

    def test_expired_consent_is_rejected_at_materialization(self) -> None:
        participants = copy.deepcopy(self.bundle["participants"])
        participants["participants"][0]["consent_expires_at"] = "2026-01-01T12:00:00Z"
        participants = self.seal(participants, "manifest_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "consent is not active"):
            solo.validate_participant_manifest(participants, self.bundle["authorization"])

    def test_expired_repository_authority_is_rejected_at_materialization(self) -> None:
        repositories = copy.deepcopy(self.bundle["repositories"])
        repository = repositories["repositories"][0]
        repository["authorization_expires_at"] = "2026-01-01T12:00:00Z"
        repository = self.seal(repository, "repository_sha256")
        repositories["repositories"][0] = repository
        repositories = self.seal(repositories, "manifest_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "authority is not active"):
            solo.validate_repository_manifest(repositories, self.bundle["authorization"])

    def test_selection_seed_and_rank_are_deterministic(self) -> None:
        commit = "a" * 40
        self.assertEqual(solo.derive_selection_seed(commit), solo.derive_selection_seed(commit))
        seed = solo.derive_selection_seed(commit)
        self.assertEqual(solo.selection_rank(seed, "opaque-pr-1"), solo.selection_rank(seed, "opaque-pr-1"))
        self.assertNotEqual(solo.selection_rank(seed, "opaque-pr-1"), solo.selection_rank(seed, "opaque-pr-2"))

    def test_real_selection_requires_external_merge_commit_anchor(self) -> None:
        plan = copy.deepcopy(self.bundle["selection_plan"])
        plan["synthetic"] = False
        plan = self.seal(plan, "plan_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "externally expected"):
            solo.validate_selection_plan(plan)
        with self.assertRaisesRegex(solo.ValidationError, "differs"):
            solo.validate_selection_plan(plan, expected_source_commit="2" * 40)

    def test_selection_rejects_rank_shopping(self) -> None:
        rows = copy.deepcopy(self.bundle["selection_log"])
        rows[0]["rank_sha256"] = "0" * 64
        rows[0] = self.seal(rows[0], "row_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "rank mismatch"):
            solo.validate_selection_log(
                rows, self.bundle["selection_plan"], self.bundle["repositories"]
            )

    def test_forbidden_path_is_rejected_before_open(self) -> None:
        with self.assertRaisesRegex(solo.ValidationError, "forbidden"):
            solo.load_json(Path("eval") / "does-not-exist.json")
        with self.assertRaisesRegex(solo.ValidationError, "forbidden"):
            solo.load_json(Path("holdout") / "does-not-exist.json")

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(solo.ValidationError, "duplicate JSON"):
                solo.load_json(path)

    def test_cumulative_cost_budget_counts_every_attempt(self) -> None:
        authorization = copy.deepcopy(self.bundle["authorization"])
        authorization["model"]["max_cost_microcny"] = 1
        authorization = self.seal(authorization, "authorization_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "cumulative cost_microcny"):
            solo.validate_run_receipts(
                self.bundle["run_receipts"], self.bundle["cohort"], authorization
            )

    def test_http_attempts_cannot_be_lower_than_logical_calls(self) -> None:
        receipts = copy.deepcopy(self.bundle["run_receipts"])
        completed = next(row for row in receipts if row["logical_calls"] > 0)
        completed["http_attempts"] = 0
        index = receipts.index(completed)
        receipts[index] = self.seal(completed, "receipt_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "cannot be lower"):
            solo.validate_run_receipts(
                receipts, self.bundle["cohort"], self.bundle["authorization"]
            )

    def test_attempt_one_is_the_sole_headline(self) -> None:
        receipts = copy.deepcopy(self.bundle["run_receipts"])
        receipts[0]["headline"] = False
        receipts[0] = self.seal(receipts[0], "receipt_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "sole immutable headline"):
            solo.validate_run_receipts(
                receipts, self.bundle["cohort"], self.bundle["authorization"]
            )

    def test_diagnostic_attempt_cannot_add_feedback_findings(self) -> None:
        receipts = copy.deepcopy(self.bundle["run_receipts"])
        diagnostic = next(row for row in receipts if not row["headline"])
        diagnostic["feedback_eligible_finding_ids"] = ["diagnostic-finding"]
        index = receipts.index(diagnostic)
        receipts[index] = self.seal(diagnostic, "receipt_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "diagnostic attempts"):
            solo.validate_run_receipts(
                receipts, self.bundle["cohort"], self.bundle["authorization"]
            )

    def test_receipt_cannot_predate_materialized_cohort(self) -> None:
        receipts = copy.deepcopy(self.bundle["run_receipts"])
        receipts[0]["started_at"] = "2026-01-01T00:00:00Z"
        receipts[0]["completed_at"] = "2026-01-01T00:01:00Z"
        receipts[0] = self.seal(receipts[0], "receipt_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "before cohort"):
            solo.validate_run_receipts(
                receipts, self.bundle["cohort"], self.bundle["authorization"]
            )

    def test_missing_headline_receipt_is_not_removed_from_denominator(self) -> None:
        receipts = copy.deepcopy(self.bundle["run_receipts"])
        target_pr = self.bundle["cohort"]["entries"][1]["pr_id"]
        receipts = [row for row in receipts if row["pr_id"] != target_pr]
        with self.assertRaisesRegex(solo.ValidationError, "exactly one headline"):
            solo.validate_run_receipts(
                receipts, self.bundle["cohort"], self.bundle["authorization"]
            )

    def test_partial_feedback_remains_in_full_denominator(self) -> None:
        metrics = self.bundle["solo_report"]["metrics"]
        self.assertEqual(metrics["feedback_eligible_findings"], 5)
        self.assertEqual(metrics["feedback_responses"], 2)
        self.assertEqual(metrics["feedback_missing"], 3)
        self.assertEqual(metrics["feedback_coverage_rate"], 0.4)
        self.assertEqual(metrics["accepted_or_fixed_observations"], 2)
        self.assertEqual(metrics["http_retries"], 1)

    def test_ai_or_non_human_feedback_flag_is_rejected(self) -> None:
        responses = copy.deepcopy(self.bundle["feedback_responses"])
        responses[0]["completed_by_human"] = False
        responses[0] = self.seal(responses[0], "response_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "real participant"):
            solo.validate_feedback_responses(
                responses,
                self.bundle["participants"],
                self.bundle["cohort"],
                self.bundle["finding_subjects"],
            )

    def test_missing_review_time_is_rejected(self) -> None:
        with self.assertRaisesRegex(solo.ValidationError, "complete selected-PR denominator"):
            solo.validate_review_times(
                self.bundle["review_times"][:-1],
                self.bundle["participants"],
                self.bundle["cohort"],
            )

    def test_synthetic_provenance_must_propagate(self) -> None:
        subjects = copy.deepcopy(self.bundle["finding_subjects"])
        subjects[0]["synthetic"] = False
        subjects[0] = self.seal(subjects[0], "subject_sha256")
        with self.assertRaisesRegex(solo.ValidationError, "provenance mismatch"):
            solo.validate_finding_subjects(subjects, self.bundle["cohort"])

    def test_report_is_exactly_recomputed(self) -> None:
        report = copy.deepcopy(self.bundle["solo_report"])
        report["metrics"]["feedback_missing"] = 0
        report = self.seal(report, "report_sha256")
        bundle = copy.deepcopy(self.bundle)
        bundle["solo_report"] = report
        with self.assertRaisesRegex(solo.ValidationError, "exactly match"):
            solo.validate_bundle(bundle)

    def test_report_rejects_prohibited_quality_metric(self) -> None:
        report = copy.deepcopy(self.bundle["solo_report"])
        report["metrics"]["precision"] = 1.0
        report = self.seal(report, "report_sha256")
        bundle = copy.deepcopy(self.bundle)
        bundle["solo_report"] = report
        with self.assertRaisesRegex(solo.ValidationError, "exactly match"):
            solo.validate_bundle(bundle)

    def test_report_claims_cannot_be_enabled(self) -> None:
        report = copy.deepcopy(self.bundle["solo_report"])
        report["claim_gates"]["business_claim_allowed"] = True
        report = self.seal(report, "report_sha256")
        bundle = copy.deepcopy(self.bundle)
        bundle["solo_report"] = report
        with self.assertRaisesRegex(solo.ValidationError, "exactly match"):
            solo.validate_bundle(bundle)

    def test_cli_validates_synthetic_directory_without_external_commit(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = solo.main(
                [
                    "validate-bundle",
                    "--bundle",
                    "phase9g_solo/examples/synthetic",
                ]
            )
        self.assertEqual(result, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["valid"])
        self.assertFalse(payload["exploratory_summary_allowed"])

    def test_cli_rejects_forbidden_bundle_path(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = solo.main(["validate-bundle", "--bundle", "eval/anything"])
        self.assertEqual(result, 2)
        self.assertIn("forbidden", stderr.getvalue())

    def test_cli_materializes_hash_bound_cohort_with_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization_path = root / "authorization.json"
            plan_path = root / "plan.json"
            repositories_path = root / "repositories.json"
            rows_path = root / "selection.jsonl"
            output_path = root / "cohort.json"
            authorization_path.write_text(
                json.dumps(self.bundle["authorization"]), encoding="utf-8"
            )
            plan_path.write_text(json.dumps(self.bundle["selection_plan"]), encoding="utf-8")
            repositories_path.write_text(
                json.dumps(self.bundle["repositories"]), encoding="utf-8"
            )
            rows_path.write_text(
                "".join(json.dumps(row) + "\n" for row in self.bundle["selection_log"]),
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = solo.main(
                    [
                        "materialize-cohort",
                        "--authorization",
                        str(authorization_path),
                        "--plan",
                        str(plan_path),
                        "--selection-log",
                        str(rows_path),
                        "--repositories",
                        str(repositories_path),
                        "--expected-source-commit",
                        "1" * 40,
                        "--materialized-at",
                        "2026-01-02T01:00:00Z",
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(solo.load_json(output_path), self.bundle["cohort"])

    def test_hash_artifact_helper_only_seals_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "artifact.json"
            output_path = root / "sealed.json"
            input_path.write_text('{"value":1,"artifact_sha256":""}', encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = solo.main(
                    [
                        "hash-artifact",
                        "--input",
                        str(input_path),
                        "--hash-field",
                        "artifact_sha256",
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(result, 0)
            sealed = solo.load_json(output_path)
            solo.validate_artifact_hash(sealed, "artifact_sha256", "artifact")

    def test_seal_authorization_does_not_grant_missing_authority(self) -> None:
        draft = copy.deepcopy(self.bundle["authorization"])
        draft["model"]["real_paid_calls"] = False
        draft["model"]["read_raw_diff"] = False
        draft["authorization_sha256"] = ""
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "authorization.json"
            output_path = Path(directory) / "sealed.json"
            input_path.write_text(json.dumps(draft), encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = solo.main(
                    [
                        "seal-authorization",
                        "--authorization",
                        str(input_path),
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(result, 0)
            sealed = solo.load_json(output_path)
            readiness = solo.authorization_readiness(sealed, at="2026-01-02T00:00:00Z")
            self.assertFalse(readiness["scopes"]["model_execution"])


if __name__ == "__main__":
    unittest.main()
