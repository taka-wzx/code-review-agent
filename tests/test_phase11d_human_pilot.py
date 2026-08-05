from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

import phase11d_human_pilot as pilot


def _bundle() -> dict[str, object]:
    return copy.deepcopy(pilot.build_gate_a_bundle())


def _rows(files: dict[str, object], name: str) -> list[dict[str, object]]:
    rows = files[name]
    assert isinstance(rows, list)
    return rows


def _object(files: dict[str, object], name: str) -> dict[str, object]:
    value = files[name]
    assert isinstance(value, dict)
    return value


def _write_bundle(root: Path, files: dict[str, object], *, rebuild_manifest: bool) -> None:
    body = {name: value for name, value in files.items() if name != "canonical-manifest.json"}
    if rebuild_manifest:
        files["canonical-manifest.json"] = pilot._manifest(body)
    for name, value in files.items():
        path = root / name
        if name.endswith(".jsonl"):
            assert isinstance(value, list)
            pilot._write_jsonl(path, value)
        else:
            assert isinstance(value, dict)
            pilot._write_json(path, value)


def _rehash_authorization(authorization: dict[str, object]) -> None:
    authorization["canonical_authorization_sha256"] = ""
    authorization["canonical_authorization_sha256"] = pilot._self_hash(
        authorization,
        "canonical_authorization_sha256",
    )


def _valid_parts() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    files = _bundle()
    return (
        _object(files, "cohort.json"),
        _object(files, "headline-manifest.json"),
        _object(files, "authorization.json"),
        _rows(files, "review-receipts.jsonl"),
        _rows(files, "repair-receipts.jsonl"),
        _rows(files, "draft-pr-receipts.jsonl"),
        _rows(files, "feedback-receipts.jsonl"),
        _rows(files, "time-cost-latency-receipts.jsonl"),
        _rows(files, "incident-stop-receipts.jsonl"),
    )


class Phase11DHumanPilotTests(unittest.TestCase):
    def test_generate_and_validate_full_gate_a_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pilot.write_gate_a_bundle(root)

            summary = pilot.validate_bundle(root)

            self.assertEqual(summary.selected_prs, 20)
            self.assertEqual(summary.completed_headlines, 16)
            self.assertEqual(summary.feedback_eligible_findings, 3)
            self.assertEqual(summary.repair_jobs, 2)
            self.assertEqual(summary.draft_pr_receipts, 1)
            self.assertFalse(summary.business_claim_allowed)
            self.assertFalse(summary.gate_b_allowed)
            self.assertIn(
                "permission_not_granted:allow_real_provider_calls",
                summary.gate_b_blockers,
            )

    def test_manifest_detects_canonical_artifact_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pilot.write_gate_a_bundle(root)
            cohort_path = root / "cohort.json"
            cohort = json.loads(cohort_path.read_text(encoding="utf-8"))
            cohort["selected_prs"][0]["selection_rank_sha256"] = "a" * 64
            pilot._write_json(cohort_path, cohort)

            with self.assertRaisesRegex(pilot.Phase11DError, "canonical SHA-256 mismatch"):
                pilot.validate_bundle(root)

    def test_gate_b_template_requires_complete_fields_and_permission_state(self) -> None:
        template = pilot.build_gate_b_template()
        allowed, blockers = pilot.evaluate_gate_b_template(template)
        self.assertFalse(allowed)
        self.assertNotIn("missing:authorization_id", blockers)
        self.assertNotIn("missing:github_app_installation_id", blockers)
        self.assertEqual(
            template["required_fields"]["authorization_id"],
            "phase11d-gate-b-human-pilot-v1-20260805-001",
        )
        self.assertEqual(template["required_fields"]["github_app_installation_id"], 149747930)
        self.assertIn("exact_approval_text_missing", blockers)

        filled = copy.deepcopy(template)
        for field in pilot.GATE_B_REQUIRED_FIELDS:
            filled["required_fields"][field] = "filled"
        filled["exact_approval_text"] = "OWNER EXACT APPROVAL TEXT"
        for name in (
            "allow_real_provider_calls",
            "allow_real_github_repair_branch_push",
            "allow_real_draft_repair_pr",
        ):
            filled["permission_switches"][name] = True
        allowed, blockers = pilot.evaluate_gate_b_template(filled)
        self.assertTrue(allowed, blockers)

        for name in (
            "allow_real_provider_calls",
            "allow_real_github_repair_branch_push",
            "allow_real_draft_repair_pr",
        ):
            bad = copy.deepcopy(filled)
            bad["permission_switches"][name] = False
            allowed, blockers = pilot.evaluate_gate_b_template(bad)
            self.assertFalse(allowed)
            self.assertIn(f"permission_not_granted:{name}", blockers)

        for name in (
            "allow_comments_checks_labels_reviews",
            "allow_pilot_pr_ready",
            "allow_pilot_pr_merge",
            "allow_default_branch_mutation",
            "allow_auto_merge",
            "allow_agent_push_merge_master",
        ):
            bad = copy.deepcopy(filled)
            bad["permission_switches"][name] = True
            allowed, blockers = pilot.evaluate_gate_b_template(bad)
            self.assertFalse(allowed)
            self.assertIn(f"prohibited_permission_enabled:{name}", blockers)

    def test_unknown_fields_duplicate_keys_bool_ints_and_counter_floats_fail(self) -> None:
        cohort, headline, authorization, reviews, _repairs, _drafts, *_rest = _valid_parts()

        with tempfile.TemporaryDirectory() as temp:
            duplicate = Path(temp) / "duplicate.json"
            duplicate.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
            with self.assertRaisesRegex(pilot.Phase11DError, "duplicate JSON key"):
                pilot.load_json(duplicate)

        bad_authorization = copy.deepcopy(authorization)
        bad_authorization["extra"] = "not allowed"
        with self.assertRaisesRegex(pilot.Phase11DError, "unexpected extra"):
            pilot.validate_authorization(bad_authorization)

        bad_cohort = copy.deepcopy(cohort)
        bad_cohort["selected_pr_count"] = True
        with self.assertRaisesRegex(pilot.Phase11DError, "expected integer"):
            pilot.validate_cohort(bad_cohort)

        bad_reviews = copy.deepcopy(reviews)
        bad_reviews[0]["cost_micro_cny"] = 1.5
        with self.assertRaisesRegex(pilot.Phase11DError, "expected integer"):
            pilot.validate_reviews(bad_reviews, cohort, headline)

    def test_unauthorized_actors_cannot_start_or_approve(self) -> None:
        _cohort, _headline, _authorization, reviews, repairs, *_rest = _valid_parts()
        denied_roles = ("viewer", "reviewer", "webhook", "model", "Finding", "agent", "system")
        denied_methods = ("anonymous", "agent", "finding", "github_webhook", "model", "system", "webhook")

        for role in denied_roles:
            with self.subTest(start_role=role):
                candidate = copy.deepcopy(repairs)
                candidate[0]["request_actor_role"] = role
                with self.assertRaises(pilot.Phase11DError):
                    pilot.validate_repairs(candidate, reviews)

            with self.subTest(write_role=role):
                candidate = copy.deepcopy(repairs)
                candidate[0]["write_approval"]["actor_role"] = role
                with self.assertRaises(pilot.Phase11DError):
                    pilot.validate_repairs(candidate, reviews)

        for method in denied_methods:
            with self.subTest(start_method=method):
                candidate = copy.deepcopy(repairs)
                candidate[0]["request_actor_method"] = method
                with self.assertRaises(pilot.Phase11DError):
                    pilot.validate_repairs(candidate, reviews)

            with self.subTest(write_method=method):
                candidate = copy.deepcopy(repairs)
                candidate[0]["write_approval"]["actor_method"] = method
                with self.assertRaises(pilot.Phase11DError):
                    pilot.validate_repairs(candidate, reviews)

    def test_approval_race_replay_and_stale_bindings_fail_closed(self) -> None:
        _cohort, _headline, _authorization, reviews, repairs, *_rest = _valid_parts()

        replay = copy.deepcopy(repairs)
        replay[0]["draft_pr_approval"]["approval_id"] = replay[0]["write_approval"][
            "approval_id"
        ]
        with self.assertRaisesRegex(pilot.Phase11DError, "approval replay"):
            pilot.validate_repairs(replay, reviews)

        drift_values = {
            "base_sha": "4" * 40,
            "head_sha": "5" * 40,
            "plan_sha256": "a" * 64,
            "patch_sha256": "b" * 64,
            "test_sha256": "c" * 64,
            "checkpoint_sha256": "d" * 64,
            "budget_sha256": "e" * 64,
        }
        for field, value in drift_values.items():
            with self.subTest(stale_field=field):
                candidate = copy.deepcopy(repairs)
                candidate[0][field] = value
                with self.assertRaisesRegex(pilot.Phase11DError, "approval binding is stale"):
                    pilot.validate_repairs(candidate, reviews)

        policy_drift = copy.deepcopy(repairs)
        policy_drift[0]["sandbox"]["network_mode"] = "bridge"
        with self.assertRaisesRegex(pilot.Phase11DError, "approval binding is stale"):
            pilot.validate_repairs(policy_drift, reviews)

    def test_declines_test_failures_budget_kill_switch_and_credential_revocation(self) -> None:
        _cohort, _headline, authorization, reviews, repairs, _drafts, _feedback, _time, incidents = (
            _valid_parts()
        )

        declined = copy.deepcopy(repairs)
        declined[0]["write_approval"]["decision"] = "declined"
        with self.assertRaisesRegex(pilot.Phase11DError, "declined WRITE"):
            pilot.validate_repairs(declined, reviews)

        test_failed = copy.deepcopy(repairs)
        test_failed[0]["sandbox"]["tests_passed"] = False
        test_failed[0]["draft_pr_approval"]["binding_sha256"] = pilot._approval_binding(
            "draft_pr",
            test_failed[0],
        )
        with self.assertRaisesRegex(pilot.Phase11DError, "failed tests"):
            pilot.validate_repairs(test_failed, reviews)

        budget_exhausted = copy.deepcopy(repairs)
        budget_exhausted[0]["final_status"] = "budget_exhausted"
        budget_exhausted[0]["failure_category"] = "budget_exhausted"
        with self.assertRaisesRegex(pilot.Phase11DError, "budget exhaustion"):
            pilot.validate_repairs(budget_exhausted, reviews)

        killed = copy.deepcopy(authorization)
        killed["incident_policy"]["kill_switch_active"] = True
        _rehash_authorization(killed)
        with self.assertRaisesRegex(pilot.Phase11DError, "kill switch"):
            pilot.validate_authorization(killed)

        credential_not_isolated = copy.deepcopy(incidents)
        credential_not_isolated[0]["credential_revoked_or_isolated"] = False
        with self.assertRaisesRegex(pilot.Phase11DError, "credential"):
            pilot.validate_incidents(credential_not_isolated)

    def test_provider_and_publisher_failures_are_fail_closed(self) -> None:
        cohort, headline, _authorization, reviews, repairs, drafts, *_rest = _valid_parts()

        text_only_completed = copy.deepcopy(reviews)
        text_only_completed[1]["status"] = "completed"
        text_only_completed[1]["terminal_category"] = "provider_text_only_response"
        with self.assertRaisesRegex(pilot.Phase11DError, "text-only"):
            pilot.validate_reviews(text_only_completed, cohort, headline)

        malformed_completed = copy.deepcopy(reviews)
        malformed_completed[6]["status"] = "completed"
        malformed_completed[6]["terminal_category"] = "provider_malformed_tool_response"
        with self.assertRaisesRegex(pilot.Phase11DError, "completed terminal"):
            pilot.validate_reviews(malformed_completed, cohort, headline)

        usage_ambiguity = copy.deepcopy(reviews)
        usage_ambiguity[10]["status"] = "failed"
        usage_ambiguity[10]["terminal_category"] = "provider_usage_ambiguity"
        pilot.validate_reviews(usage_ambiguity, cohort, headline)

        publisher_failure = copy.deepcopy(repairs)
        publisher_failure[0]["publisher_status"] = "publisher_failed"
        publisher_failure[0]["final_status"] = "publisher_failed"
        publisher_failure[0]["failure_category"] = "publisher_failed"
        with self.assertRaisesRegex(pilot.Phase11DError, "publisher uncertainty"):
            pilot.validate_repairs(publisher_failure, reviews)

        with self.assertRaisesRegex(pilot.Phase11DError, "missing receipt"):
            pilot.validate_drafts([], repairs)

        draft_mismatch = copy.deepcopy(drafts)
        draft_mismatch[0]["commit_sha"] = "4" * 40
        with self.assertRaisesRegex(pilot.Phase11DError, "approved commit mismatch"):
            pilot.validate_drafts(draft_mismatch, repairs)

    def test_tenant_redaction_and_draft_pr_boundaries(self) -> None:
        files = _bundle()
        authorization = _object(files, "authorization.json")
        repositories = _object(files, "repository-allowlist.json")
        cohort = _object(files, "cohort.json")
        selection = _object(files, "selection-receipt.json")
        headline = _object(files, "headline-manifest.json")
        reviews = _rows(files, "review-receipts.jsonl")
        repairs = _rows(files, "repair-receipts.jsonl")
        drafts = _rows(files, "draft-pr-receipts.jsonl")

        foreign_cohort = copy.deepcopy(cohort)
        foreign_cohort["selected_prs"][0]["repository_id"] = "foreign-repo"
        linked_authorization = copy.deepcopy(authorization)
        linked_authorization["cohort_sha256"] = pilot._artifact_sha256(
            "cohort.json",
            foreign_cohort,
        )
        _rehash_authorization(linked_authorization)
        with self.assertRaisesRegex(pilot.Phase11DError, "outside repository allowlist"):
            pilot.validate_bundle_links(
                files,
                linked_authorization,
                repositories,
                foreign_cohort,
                selection,
                headline,
            )

        with self.assertRaisesRegex(pilot.Phase11DError, "prohibited raw-content"):
            pilot._scan_no_raw_content({"receipt": {"raw_diff": "diff --git a/file b/file"}})

        for field in ("ready", "merged"):
            candidate = copy.deepcopy(drafts)
            candidate[0][field] = True
            with self.subTest(field=field):
                with self.assertRaisesRegex(pilot.Phase11DError, "Draft PR must stay Draft"):
                    pilot.validate_drafts(candidate, repairs)

        leaked = _bundle()
        _rows(leaked, "review-receipts.jsonl")[0]["raw_diff"] = "diff --git a b"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_bundle(root, leaked, rebuild_manifest=True)
            with self.assertRaisesRegex(pilot.Phase11DError, "prohibited raw-content"):
                pilot.validate_bundle(root)

        missing_feedback = copy.deepcopy(_rows(files, "feedback-receipts.jsonl"))
        missing_feedback.append(copy.deepcopy(missing_feedback[0]))
        with self.assertRaisesRegex(pilot.Phase11DError, "duplicate finding feedback"):
            pilot.validate_feedback(missing_feedback, reviews)

    def test_phase11c_auth004_and_claim_boundaries_cannot_drift(self) -> None:
        cohort, _headline, authorization, reviews, repairs, drafts, feedback, time_cost, incidents = (
            _valid_parts()
        )

        phase11c_drift = copy.deepcopy(authorization)
        phase11c_drift["phase11c_facts"]["headline_cohort_status"] = "completed"
        _rehash_authorization(phase11c_drift)
        with self.assertRaisesRegex(pilot.Phase11DError, "Phase 11C facts drifted"):
            pilot.validate_authorization(phase11c_drift)

        auth004_drift = copy.deepcopy(authorization)
        auth004_drift["auth004_boundary"]["completed"] = 1
        _rehash_authorization(auth004_drift)
        with self.assertRaisesRegex(pilot.Phase11DError, "auth-004 boundary drifted"):
            pilot.validate_authorization(auth004_drift)

        business = pilot._business_report(cohort, reviews, repairs, drafts, feedback, time_cost, incidents)
        claim = pilot._claim_decision()
        acceptance = pilot._acceptance_report(("gate_b_closed",))
        business["business_claim_allowed"] = True
        with self.assertRaisesRegex(pilot.Phase11DError, "report does not recompute"):
            pilot.validate_reports(
                business,
                claim,
                acceptance,
                cohort,
                reviews,
                repairs,
                drafts,
                feedback,
                time_cost,
                incidents,
            )


if __name__ == "__main__":
    unittest.main()
