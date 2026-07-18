"""Offline tests for the Week 4 trusted Review evaluation framework."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import trusted_review_eval as tre


REPOSITORIES = (
    ("calibration/project", "calibration"),
    ("reporting/alpha", "reporting"),
    ("reporting/beta", "reporting"),
    ("reporting/gamma", "reporting"),
)


def digest(text: str, length: int = 64) -> str:
    algorithm = hashlib.sha1 if length == 40 else hashlib.sha256
    return algorithm(text.encode("utf-8")).hexdigest()


def make_cohort(*, materialized: bool = True, gold: bool = True) -> dict:
    seed_source_commit = tre.PREREGISTERED_SEED_SOURCE_COMMIT
    repositories = [
        {"slug": slug, "role": role, "target_prs": 10}
        for slug, role in REPOSITORIES
    ]
    prs = []
    if materialized:
        for repo_index, (slug, role) in enumerate(REPOSITORIES):
            start = 1 + repo_index * 100
            for offset in range(10):
                number = start + offset
                pr_id = f"{slug}#{number}"
                prs.append(
                    {
                        "pr_id": pr_id,
                        "repository": slug,
                        "number": number,
                        "role": role,
                        "base_sha": digest(f"{pr_id}:base", 40),
                        "head_sha": digest(f"{pr_id}:head", 40),
                        "merge_sha": digest(f"{pr_id}:merge", 40),
                        "diff_sha256": digest(f"{pr_id}:diff"),
                        "snapshot_sha256": digest(f"{pr_id}:snapshot"),
                        "merged_at": f"2025-{(offset % 9) + 1:02d}-01T00:00:00Z",
                        "selected_at": "2026-01-02T00:00:00Z",
                        "changed_lines": 50 if offset % 2 == 0 else 150,
                        "change_type": "bug_fix" if offset % 2 == 0 else "non_bug_fix",
                        "human_review_comments_present": offset % 2 == 0,
                        "author_is_benchmark_implementer": False,
                        "previously_used": False,
                        "gold_review_complete": gold,
                        "gold_annotation_set_sha256": digest(f"{pr_id}:annotations"),
                    }
                )
    return {
        "schema_version": 1,
        "cohort_id": "test-cohort",
        "cohort_seed": tre.derive_cohort_seed(seed_source_commit),
        "cohort_seed_derivation": {
            "method": "sha256_source_commit_v1",
            "source_commit": seed_source_commit,
        },
        "selection_window": {
            "start": "2024-01-01T00:00:00Z",
            "end": "2026-01-01T00:00:00Z",
        },
        "repositories": repositories,
        "prs": prs,
        "gold_frozen_at": "2026-01-10T00:00:00Z" if materialized else None,
        "selection_log_sha256": digest("selection-log") if materialized else None,
    }


def reporting_pr_ids(cohort: dict) -> list[str]:
    return sorted(pr["pr_id"] for pr in cohort["prs"] if pr["role"] == "reporting")


def bind_gold_hashes(cohort: dict, annotations: list[dict]) -> None:
    for pr in cohort["prs"]:
        if pr["role"] != "reporting":
            continue
        rows = sorted(
            (
                row
                for row in annotations
                if row["pr_id"] == pr["pr_id"]
                and row["subject_kind"] == "gold_candidate"
            ),
            key=lambda row: row["annotation_id"],
        )
        pr["gold_annotation_set_sha256"] = tre.canonical_sha256(rows)


def annotation(
    *,
    annotation_id: str,
    subject_kind: str,
    subject_id: str,
    pr_id: str,
    annotator_id: str,
    label: str,
    role: str = "annotator",
    gold_id: str | None = None,
    discovered: bool | None = None,
    created_at: str | None = None,
    severity: str | None = None,
    source_rows: list[dict] | None = None,
) -> dict:
    if created_at is None:
        created_at = (
            "2026-01-09T00:00:00Z"
            if subject_kind == "gold_candidate"
            else "2026-01-12T00:00:00Z"
        )
    if severity is None and subject_kind == "gold_candidate":
        severity = "high"
    source_rows = source_rows or []
    return {
        "schema_version": 1,
        "annotation_id": annotation_id,
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "pr_id": pr_id,
        "annotator_id": annotator_id,
        "role": role,
        "label": label,
        "gold_id": gold_id,
        "discovered": discovered,
        "severity": severity,
        "rationale": f"rationale for {annotation_id}",
        "evidence_sha256": digest(f"{annotation_id}:evidence"),
        "source_annotation_ids": [row["annotation_id"] for row in source_rows],
        "source_annotation_sha256s": [
            tre.canonical_sha256(row) for row in source_rows
        ],
        "created_at": created_at,
    }


def finding(finding_id: str) -> dict:
    return {
        "finding_id": finding_id,
        "fingerprint_sha256": digest(f"{finding_id}:fingerprint"),
        "path": "src/example.py",
        "line": 10,
    }


def run(
    pr_id: str,
    *,
    findings: list[dict] | None = None,
    status: str = "ok",
    purpose: str = "final_report",
    started_at: str = "2026-01-11T00:00:00Z",
    cost: int = 1000,
    latency: float = 9.0,
    tools: dict[str, int] | None = None,
    test_status: str = "not_applicable",
    unauthorized: int = 0,
) -> dict:
    if tools is None:
        tools = {"finder": 2, "verifier": 1}
    return {
        "schema_version": 1,
        "run_id": f"run:{pr_id}",
        "pr_id": pr_id,
        "config_id": "frozen-v1",
        "purpose": purpose,
        "source_commit": digest("source", 40),
        "gold_freeze_commit": digest("gold-freeze-commit", 40),
        "frozen_cohort_sha256": digest("unbound-cohort"),
        "provider": "provider-a",
        "model_id": "model-a-v1",
        "pricing_revision": "pricing-2026-07",
        "runtime_config_sha256": digest("runtime-config"),
        "snapshot_sha256": digest(f"{pr_id}:snapshot"),
        "started_at": started_at,
        "completed_at": "2026-01-11T00:00:10Z",
        "status": status,
        "scorable": status != "failed",
        "cost_microusd": cost,
        "latency_seconds": latency,
        "tool_calls": sum(tools.values()),
        "tool_calls_by_component": tools,
        "test_status": test_status,
        "unauthorized_operation_count": unauthorized,
        "findings": [] if findings is None else findings,
    }


def make_perfect_dataset() -> tuple[dict, list[dict], list[dict]]:
    cohort = make_cohort()
    annotations = []
    runs = []
    for index, pr_id in enumerate(reporting_pr_ids(cohort)):
        gold_id = f"gold:{index}"
        finding_id = f"finding:{index}"
        annotations.extend(
            [
                annotation(
                    annotation_id=f"gold-a:{index}",
                    subject_kind="gold_candidate",
                    subject_id=gold_id,
                    pr_id=pr_id,
                    annotator_id="annotator-a",
                    label="valid_defect",
                    discovered=True,
                ),
                annotation(
                    annotation_id=f"gold-b:{index}",
                    subject_kind="gold_candidate",
                    subject_id=gold_id,
                    pr_id=pr_id,
                    annotator_id="annotator-b",
                    label="valid_defect",
                    discovered=index % 2 == 0,
                ),
                annotation(
                    annotation_id=f"finding-a:{index}",
                    subject_kind="system_finding",
                    subject_id=finding_id,
                    pr_id=pr_id,
                    annotator_id="annotator-a",
                    label="matched",
                    gold_id=gold_id,
                ),
                annotation(
                    annotation_id=f"finding-b:{index}",
                    subject_kind="system_finding",
                    subject_id=finding_id,
                    pr_id=pr_id,
                    annotator_id="annotator-b",
                    label="matched",
                    gold_id=gold_id,
                ),
            ]
        )
        runs.append(run(pr_id, findings=[finding(finding_id)]))
    bind_gold_hashes(cohort, annotations)
    frozen_cohort_sha256 = tre.canonical_sha256(cohort)
    for run_row in runs:
        run_row["frozen_cohort_sha256"] = frozen_cohort_sha256
    return cohort, annotations, runs


def make_selection_log(cohort: dict) -> list[dict]:
    return [
        {
            "schema_version": 1,
            "pr_id": pr["pr_id"],
            "repository": pr["repository"],
            "number": pr["number"],
            "merged_at": pr["merged_at"],
            "eligible": True,
            "exclusion_reason": None,
            "selected": True,
            "rank_sha256": tre.selection_rank_sha256(
                cohort["cohort_seed"],
                pr["pr_id"],
            ),
        }
        for pr in cohort["prs"]
    ]


def jsonl_bytes(rows: list[dict]) -> bytes:
    return (
        "\n".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for row in rows
        )
        + "\n"
    ).encode("utf-8")


class TestCohortValidation(unittest.TestCase):
    def test_preregistered_plan_has_disjoint_30_pr_reporting_target(self):
        validated = tre.validate_cohort(make_cohort(materialized=False), require_materialized=False)
        self.assertEqual(len(validated["_reporting_repositories"]), 3)
        self.assertEqual(validated["_calibration_repositories"], ["calibration/project"])

    def test_cohort_seed_is_recomputed_from_fixed_source_commit(self):
        self.assertEqual(
            tre.derive_cohort_seed(
                "9564cc817d5d0639b6c31cf4bde540594b38382d"
            ),
            "eb832b864d2094ce30983c92edf7a7ec77a612ae218244a1a37c7692c340ee95",
        )
        cohort = make_cohort(materialized=False)
        cohort["cohort_seed"] = digest("seed-shopping")
        with self.assertRaisesRegex(tre.ValidationError, "deterministic source-commit"):
            tre.validate_cohort(cohort, require_materialized=False)

        cohort = make_cohort(materialized=False)
        cohort["cohort_seed_derivation"]["source_commit"] = digest(
            "alternate-source",
            40,
        )
        cohort["cohort_seed"] = tre.derive_cohort_seed(
            cohort["cohort_seed_derivation"]["source_commit"]
        )
        with self.assertRaisesRegex(tre.ValidationError, "preregistered Week 4 base"):
            tre.validate_cohort(cohort, require_materialized=False)

    def test_materialized_requires_exact_repository_counts(self):
        cohort = make_cohort()
        cohort["prs"].pop()
        with self.assertRaisesRegex(tre.ValidationError, "expected exactly 10"):
            tre.validate_cohort(cohort, require_materialized=True)

    def test_materialized_requires_completed_gold_review(self):
        cohort = make_cohort()
        cohort["prs"][10]["gold_review_complete"] = False
        with self.assertRaisesRegex(tre.ValidationError, "completed independent gold review"):
            tre.validate_cohort(cohort, require_materialized=True)

    def test_three_reporting_repositories_and_30_prs_are_mandatory(self):
        cohort = make_cohort(materialized=False)
        cohort["repositories"] = cohort["repositories"][:-1]
        with self.assertRaisesRegex(tre.ValidationError, "at least 30 reporting PRs"):
            tre.validate_cohort(cohort, require_materialized=False)

    def test_duplicate_repository_and_pr_are_rejected(self):
        cohort = make_cohort(materialized=False)
        cohort["repositories"].append(copy.deepcopy(cohort["repositories"][0]))
        with self.assertRaisesRegex(tre.ValidationError, "duplicate repository"):
            tre.validate_cohort(cohort, require_materialized=False)

        cohort = make_cohort()
        cohort["prs"].append(copy.deepcopy(cohort["prs"][0]))
        with self.assertRaisesRegex(tre.ValidationError, "duplicate PR"):
            tre.validate_cohort(cohort, require_materialized=True)

    def test_unknown_fields_are_rejected(self):
        cohort = make_cohort(materialized=False)
        cohort["tuning_notes"] = "should not be silently accepted"
        with self.assertRaisesRegex(tre.ValidationError, "unknown keys"):
            tre.validate_cohort(cohort, require_materialized=False)

    def test_materialized_cohort_enforces_window_diversity_and_no_prior_use(self):
        cohort = make_cohort()
        cohort["prs"][10]["merged_at"] = "2026-01-01T00:00:00Z"
        with self.assertRaisesRegex(tre.ValidationError, "outside the preregistered"):
            tre.validate_cohort(cohort, require_materialized=True)

        cohort = make_cohort()
        for pr in cohort["prs"]:
            if pr["role"] == "reporting":
                pr["changed_lines"] = 50
        with self.assertRaisesRegex(tre.ValidationError, "two changed-line size bands"):
            tre.validate_cohort(cohort, require_materialized=True)

        cohort = make_cohort()
        cohort["prs"][10]["previously_used"] = True
        with self.assertRaisesRegex(tre.ValidationError, "contaminate"):
            tre.validate_cohort(cohort, require_materialized=True)

        cohort = make_cohort()
        cohort["prs"][10]["author_is_benchmark_implementer"] = True
        with self.assertRaisesRegex(tre.ValidationError, "benchmark implementer"):
            tre.validate_cohort(cohort, require_materialized=True)

        cohort = make_cohort()
        cohort["prs"][10]["selected_at"] = "2026-01-11T00:00:00Z"
        with self.assertRaisesRegex(tre.ValidationError, "selected after gold_frozen_at"):
            tre.validate_cohort(cohort, require_materialized=True)

        cohort = make_cohort()
        cohort["prs"][10]["selected_at"] = "2024-02-01T00:00:00Z"
        with self.assertRaisesRegex(tre.ValidationError, "precedes merged_at"):
            tre.validate_cohort(cohort, require_materialized=True)

        cohort = make_cohort(materialized=False)
        cohort["selection_window"]["start"] = "20240101T000000Z"
        with self.assertRaisesRegex(tre.ValidationError, "canonical UTC form"):
            tre.validate_cohort(cohort, require_materialized=False)


class TestSelectionLog(unittest.TestCase):
    def _validated(self) -> tuple[dict, list[dict], str]:
        raw_cohort = make_cohort()
        rows = make_selection_log(raw_cohort)
        artifact_sha256 = hashlib.sha256(jsonl_bytes(rows)).hexdigest()
        raw_cohort["selection_log_sha256"] = artifact_sha256
        cohort = tre.validate_cohort(raw_cohort, require_materialized=True)
        return cohort, rows, artifact_sha256

    def test_hashed_selection_log_recomputes_rank_and_manifest_set(self):
        cohort, rows, artifact_sha256 = self._validated()
        result = tre.validate_selection_log(
            rows,
            cohort,
            artifact_sha256=artifact_sha256,
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["candidate_rows"], 40)
        self.assertEqual(result["selected_prs"], 40)

    def test_selection_log_rejects_hash_rank_and_selected_set_tampering(self):
        cohort, rows, artifact_sha256 = self._validated()
        with self.assertRaisesRegex(tre.ValidationError, "artifact hash"):
            tre.validate_selection_log(
                rows,
                cohort,
                artifact_sha256=digest("wrong-selection-log"),
            )

        tampered = copy.deepcopy(rows)
        tampered[0]["rank_sha256"] = digest("wrong-rank")
        with self.assertRaisesRegex(tre.ValidationError, "rank_sha256"):
            tre.validate_selection_log(
                tampered,
                cohort,
                artifact_sha256=artifact_sha256,
            )

        tampered = copy.deepcopy(rows)
        tampered[0]["selected"] = False
        with self.assertRaisesRegex(tre.ValidationError, "selected set mismatch"):
            tre.validate_selection_log(
                tampered,
                cohort,
                artifact_sha256=artifact_sha256,
            )

        tampered = copy.deepcopy(rows)
        tampered[0]["merged_at"] = "2025-12-31T00:00:00Z"
        with self.assertRaisesRegex(tre.ValidationError, "merge time"):
            tre.validate_selection_log(
                tampered,
                cohort,
                artifact_sha256=artifact_sha256,
            )


class TestAnnotationProtocol(unittest.TestCase):
    def setUp(self):
        raw_cohort, self.annotations, self.raw_runs = make_perfect_dataset()
        self.cohort = tre.validate_cohort(raw_cohort, require_materialized=True)
        self.runs = tre.validate_runs(self.raw_runs, self.cohort, config_id="frozen-v1")

    def test_two_independent_labels_resolve_and_agreement_is_reported(self):
        resolved = tre.resolve_annotations(self.annotations, self.cohort, self.runs)
        agreement = tre.annotation_agreement(resolved)
        self.assertEqual(agreement["annotators"], ["annotator-a", "annotator-b"])
        self.assertEqual(agreement["adjudicators"], [])
        self.assertEqual(agreement["overall"]["exact_agreement_rate"], 1.0)
        self.assertEqual(agreement["overall"]["cohen_kappa"], 1.0)
        gold_agreement = agreement["by_subject_kind"]["gold_candidate"]
        self.assertIsNone(gold_agreement["cohen_kappa"])
        self.assertEqual(
            gold_agreement["cohen_kappa_reason"],
            "expected agreement is one",
        )
        self.assertEqual(agreement["overall"]["discovery"]["annotator_a"], 30)
        self.assertEqual(agreement["overall"]["discovery"]["annotator_b"], 15)
        self.assertEqual(agreement["overall"]["unresolved_subjects"], 0)
        self.assertEqual(agreement["overall"]["malformed_subjects"], 0)
        self.assertEqual(
            agreement["overall"]["invalid_subject_policy"],
            "fail_closed_before_metrics",
        )

    def test_missing_second_label_fails_closed(self):
        rows = [
            row
            for row in self.annotations
            if row["annotation_id"] != "gold-b:0"
        ]
        with self.assertRaisesRegex(tre.ValidationError, "exactly two independent"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

    def test_gold_candidate_must_be_discovered_by_at_least_one_annotator(self):
        rows = copy.deepcopy(self.annotations)
        for row in rows:
            if row["subject_id"] == "gold:0":
                row["discovered"] = False
        with self.assertRaisesRegex(tre.ValidationError, "not discovered by either"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

    def test_annotation_cannot_reference_calibration_pr(self):
        rows = copy.deepcopy(self.annotations)
        calibration_pr = next(
            pr_id
            for pr_id, pr in self.cohort["_pr_by_id"].items()
            if pr["role"] == "calibration"
        )
        rows[0]["pr_id"] = calibration_pr
        with self.assertRaisesRegex(tre.ValidationError, "non-reporting or unknown PR"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

    def test_disagreement_requires_third_party_and_preserves_raw_agreement(self):
        rows = copy.deepcopy(self.annotations)
        gold_b = next(row for row in rows if row["annotation_id"] == "gold-b:0")
        gold_b["label"] = "uncertain"
        with self.assertRaisesRegex(tre.ValidationError, "requires third-party"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

        source_rows = [
            row
            for row in rows
            if row["subject_id"] == "gold:0" and row["role"] == "annotator"
        ]
        rows.append(
            annotation(
                annotation_id="gold-c:0",
                subject_kind="gold_candidate",
                subject_id="gold:0",
                pr_id=gold_b["pr_id"],
                annotator_id="adjudicator-c",
                role="adjudicator",
                label="valid_defect",
                discovered=None,
                source_rows=source_rows,
            )
        )
        bind_gold_hashes(self.cohort, rows)
        resolved = tre.resolve_annotations(rows, self.cohort, self.runs)
        agreement = tre.annotation_agreement(resolved)
        self.assertLess(agreement["overall"]["exact_agreement_rate"], 1.0)
        self.assertEqual(agreement["overall"]["arbitrated_subjects"], 1)
        self.assertEqual(resolved["finals"]["gold:0"]["label"], "valid_defect")

    def test_same_person_cannot_adjudicate_and_extra_adjudication_is_rejected(self):
        rows = copy.deepcopy(self.annotations)
        source_rows = [
            row
            for row in rows
            if row["subject_id"] == "gold:0" and row["role"] == "annotator"
        ]
        rows.append(
            annotation(
                annotation_id="extra-c",
                subject_kind="gold_candidate",
                subject_id="gold:0",
                pr_id=rows[0]["pr_id"],
                annotator_id="adjudicator-c",
                role="adjudicator",
                label="valid_defect",
                discovered=None,
                source_rows=source_rows,
            )
        )
        with self.assertRaisesRegex(tre.ValidationError, "unnecessary adjudication"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

        rows[1]["label"] = "uncertain"
        rows[-1]["annotator_id"] = "annotator-a"
        with self.assertRaisesRegex(tre.ValidationError, "third person"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

    def test_gold_after_freeze_and_finding_before_run_are_rejected(self):
        rows = copy.deepcopy(self.annotations)
        rows[0]["created_at"] = "2026-01-10T00:00:01Z"
        with self.assertRaisesRegex(tre.ValidationError, "after gold freeze"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

        rows = copy.deepcopy(self.annotations)
        finding_row = next(row for row in rows if row["annotation_id"] == "finding-a:0")
        finding_row["created_at"] = "2026-01-10T12:00:00Z"
        with self.assertRaisesRegex(tre.ValidationError, "predates run completion"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

        rows = copy.deepcopy(self.annotations)
        rows[0]["created_at"] = "2026-01-01T00:00:00Z"
        with self.assertRaisesRegex(tre.ValidationError, "predates PR selection"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

    def test_match_requires_gold_id_and_non_match_forbids_it(self):
        rows = copy.deepcopy(self.annotations)
        finding_row = next(row for row in rows if row["annotation_id"] == "finding-a:0")
        finding_row["gold_id"] = None
        with self.assertRaisesRegex(tre.ValidationError, "must be a non-empty string"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

        rows = copy.deepcopy(self.annotations)
        rows[0]["rationale"] = "   "
        with self.assertRaisesRegex(tre.ValidationError, "non-whitespace"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

    def test_gold_annotation_rows_are_bound_to_the_frozen_manifest_hash(self):
        first_pr = reporting_pr_ids(self.cohort)[0]
        self.cohort["_pr_by_id"][first_pr]["gold_annotation_set_sha256"] = digest(
            "wrong-gold-set"
        )
        with self.assertRaisesRegex(tre.ValidationError, "gold annotation set hash mismatch"):
            tre.resolve_annotations(self.annotations, self.cohort, self.runs)

    def test_adjudication_is_bound_to_the_exact_two_source_rows(self):
        rows = copy.deepcopy(self.annotations)
        gold_b = next(row for row in rows if row["annotation_id"] == "gold-b:0")
        gold_b["label"] = "uncertain"
        source_rows = [
            row
            for row in rows
            if row["subject_id"] == "gold:0" and row["role"] == "annotator"
        ]
        adjudication = annotation(
            annotation_id="gold-c:bound",
            subject_kind="gold_candidate",
            subject_id="gold:0",
            pr_id=gold_b["pr_id"],
            annotator_id="adjudicator-c",
            role="adjudicator",
            label="valid_defect",
            discovered=None,
            source_rows=source_rows,
        )
        adjudication["source_annotation_sha256s"][0] = digest("wrong-source")
        rows.append(adjudication)
        with self.assertRaisesRegex(tre.ValidationError, "source hashes"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

    def test_adjudication_must_follow_both_independent_labels(self):
        rows = copy.deepcopy(self.annotations)
        gold_b = next(row for row in rows if row["annotation_id"] == "gold-b:0")
        gold_b["label"] = "uncertain"
        source_rows = [
            row
            for row in rows
            if row["subject_id"] == "gold:0" and row["role"] == "annotator"
        ]
        rows.append(
            annotation(
                annotation_id="gold-c:too-early",
                subject_kind="gold_candidate",
                subject_id="gold:0",
                pr_id=gold_b["pr_id"],
                annotator_id="adjudicator-c",
                role="adjudicator",
                label="valid_defect",
                discovered=None,
                source_rows=source_rows,
                created_at="2026-01-08T00:00:00Z",
            )
        )
        with self.assertRaisesRegex(tre.ValidationError, "predates an independent label"):
            tre.resolve_annotations(rows, self.cohort, self.runs)

    def test_one_third_party_adjudicator_is_used_for_all_conflicts(self):
        rows = copy.deepcopy(self.annotations)
        for index, adjudicator_id in ((0, "adjudicator-c"), (1, "adjudicator-d")):
            gold_b = next(row for row in rows if row["annotation_id"] == f"gold-b:{index}")
            gold_b["label"] = "uncertain"
            source_rows = [
                row
                for row in rows
                if row["subject_id"] == f"gold:{index}" and row["role"] == "annotator"
            ]
            rows.append(
                annotation(
                    annotation_id=f"gold-c:{index}",
                    subject_kind="gold_candidate",
                    subject_id=f"gold:{index}",
                    pr_id=gold_b["pr_id"],
                    annotator_id=adjudicator_id,
                    role="adjudicator",
                    label="valid_defect",
                    discovered=None,
                    source_rows=source_rows,
                )
            )
        with self.assertRaisesRegex(tre.ValidationError, "same third-party adjudicator"):
            tre.resolve_annotations(rows, self.cohort, self.runs)


class TestRunValidation(unittest.TestCase):
    def setUp(self):
        raw_cohort, _, self.raw_runs = make_perfect_dataset()
        self.cohort = tre.validate_cohort(raw_cohort, require_materialized=True)

    def test_exactly_one_run_per_reporting_pr(self):
        with self.assertRaisesRegex(tre.ValidationError, "run coverage mismatch"):
            tre.validate_runs(self.raw_runs[:-1], self.cohort, config_id="frozen-v1")
        duplicate = self.raw_runs + [copy.deepcopy(self.raw_runs[0])]
        duplicate[-1]["run_id"] = "another-run"
        with self.assertRaisesRegex(tre.ValidationError, "more than one run"):
            tre.validate_runs(duplicate, self.cohort, config_id="frozen-v1")

    def test_tuning_purpose_and_prefreeze_run_are_rejected(self):
        rows = copy.deepcopy(self.raw_runs)
        rows[0]["purpose"] = "tuning"
        with self.assertRaisesRegex(tre.ValidationError, "forbidden"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

        rows = copy.deepcopy(self.raw_runs)
        rows[0]["started_at"] = "2026-01-09T00:00:00Z"
        rows[0]["completed_at"] = "2026-01-09T00:00:10Z"
        with self.assertRaisesRegex(tre.ValidationError, "before gold_frozen_at"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

    def test_only_final_report_purpose_can_feed_headline_metrics(self):
        rows = copy.deepcopy(self.raw_runs)
        rows[0]["purpose"] = "audit"
        with self.assertRaisesRegex(tre.ValidationError, "must be 'final_report'"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

    def test_nonfinite_negative_and_inconsistent_telemetry_are_rejected(self):
        rows = copy.deepcopy(self.raw_runs)
        rows[0]["latency_seconds"] = float("nan")
        with self.assertRaisesRegex(tre.ValidationError, "finite number"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

        rows = copy.deepcopy(self.raw_runs)
        rows[0]["cost_microusd"] = -1
        with self.assertRaisesRegex(tre.ValidationError, "integer >= 0"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

        rows = copy.deepcopy(self.raw_runs)
        rows[0]["tool_calls"] += 1
        with self.assertRaisesRegex(tre.ValidationError, "component total"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

    def test_non_scorable_is_reserved_for_hard_failure(self):
        rows = copy.deepcopy(self.raw_runs)
        rows[0]["scorable"] = False
        with self.assertRaisesRegex(tre.ValidationError, "false only for failed"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

        rows = copy.deepcopy(self.raw_runs)
        rows[0]["status"] = "failed"
        rows[0]["scorable"] = False
        rows[0]["findings"] = []
        tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

    def test_finding_paths_and_global_ids_are_strict(self):
        rows = copy.deepcopy(self.raw_runs)
        rows[0]["findings"][0]["path"] = "../secret"
        with self.assertRaisesRegex(tre.ValidationError, "repository-relative"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

        rows = copy.deepcopy(self.raw_runs)
        rows[1]["findings"][0]["finding_id"] = rows[0]["findings"][0]["finding_id"]
        with self.assertRaisesRegex(tre.ValidationError, "globally unique"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

    def test_run_binds_snapshot_and_one_frozen_model_configuration(self):
        rows = copy.deepcopy(self.raw_runs)
        rows[0]["snapshot_sha256"] = digest("wrong-snapshot")
        with self.assertRaisesRegex(tre.ValidationError, "frozen PR"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

        rows = copy.deepcopy(self.raw_runs)
        rows[0]["frozen_cohort_sha256"] = digest("wrong-cohort")
        with self.assertRaisesRegex(tre.ValidationError, "materialized cohort"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

        rows = copy.deepcopy(self.raw_runs)
        rows[0]["gold_freeze_commit"] = digest("different-freeze", 40)
        with self.assertRaisesRegex(tre.ValidationError, "mixes freeze/source/model"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")

        rows = copy.deepcopy(self.raw_runs)
        rows[0]["model_id"] = "different-model"
        with self.assertRaisesRegex(tre.ValidationError, "mixes freeze/source/model"):
            tre.validate_runs(rows, self.cohort, config_id="frozen-v1")


class TestReviewMetrics(unittest.TestCase):
    def _validated(self):
        raw_cohort, annotations, raw_runs = make_perfect_dataset()
        cohort = tre.validate_cohort(raw_cohort, require_materialized=True)
        runs = tre.validate_runs(raw_runs, cohort, config_id="frozen-v1")
        resolved = tre.resolve_annotations(annotations, cohort, runs)
        return cohort, annotations, raw_runs, runs, resolved

    def test_perfect_micro_repo_macro_and_pr_macro(self):
        cohort, _, _, runs, resolved = self._validated()
        scored = tre.score_review_runs(runs, cohort, resolved)
        report = tre.review_metrics(scored)
        self.assertEqual(report["micro"]["tp_findings"], 30)
        self.assertEqual(report["micro"]["tp_gold"], 30)
        self.assertEqual(report["micro"]["precision"], 1.0)
        self.assertEqual(report["micro"]["recall"], 1.0)
        self.assertEqual(report["repository_macro"]["f1"], 1.0)
        self.assertEqual(report["pr_macro"]["f1"], 1.0)
        self.assertEqual(set(report["by_repository"]), {
            "reporting/alpha",
            "reporting/beta",
            "reporting/gamma",
        })

    def test_duplicate_novel_invalid_and_unscorable_semantics(self):
        raw_cohort, annotations, raw_runs = make_perfect_dataset()
        first_pr = reporting_pr_ids(raw_cohort)[0]
        additions = [
            ("finding:novel", "novel_valid", None),
            ("finding:duplicate", "duplicate", "gold:0"),
            ("finding:invalid", "invalid", None),
            ("finding:unscorable", "unscorable", None),
        ]
        for finding_id, label, gold_id in additions:
            raw_runs[0]["findings"].append(finding(finding_id))
            for side in ("a", "b"):
                annotations.append(
                    annotation(
                        annotation_id=f"{finding_id}:{side}",
                        subject_kind="system_finding",
                        subject_id=finding_id,
                        pr_id=first_pr,
                        annotator_id=f"annotator-{side}",
                        label=label,
                        gold_id=gold_id,
                    )
                )
        cohort = tre.validate_cohort(raw_cohort, require_materialized=True)
        runs = tre.validate_runs(raw_runs, cohort, config_id="frozen-v1")
        resolved = tre.resolve_annotations(annotations, cohort, runs)
        report = tre.review_metrics(tre.score_review_runs(runs, cohort, resolved))
        micro = report["micro"]
        self.assertEqual(micro["tp_findings"], 31)
        self.assertEqual(micro["fp_findings"], 3)
        self.assertEqual(micro["tp_gold"], 30)
        self.assertEqual(micro["fn_gold"], 0)
        self.assertEqual(micro["novel_valid"], 1)
        self.assertEqual(micro["duplicates"], 1)
        self.assertEqual(micro["unscorable"], 1)
        self.assertAlmostEqual(micro["precision"], 31 / 34, places=6)
        self.assertEqual(micro["recall"], 1.0)

    def test_repeated_novel_fingerprint_is_credited_once(self):
        raw_cohort, annotations, raw_runs = make_perfect_dataset()
        first_pr = reporting_pr_ids(raw_cohort)[0]
        first = finding("finding:novel-primary")
        repeated = finding("finding:novel-repeat")
        repeated["fingerprint_sha256"] = first["fingerprint_sha256"]
        raw_runs[0]["findings"].extend([first, repeated])
        for finding_id in ("finding:novel-primary", "finding:novel-repeat"):
            for side in ("a", "b"):
                annotations.append(
                    annotation(
                        annotation_id=f"{finding_id}:{side}",
                        subject_kind="system_finding",
                        subject_id=finding_id,
                        pr_id=first_pr,
                        annotator_id=f"annotator-{side}",
                        label="novel_valid",
                    )
                )
        cohort = tre.validate_cohort(raw_cohort, require_materialized=True)
        runs = tre.validate_runs(raw_runs, cohort, config_id="frozen-v1")
        resolved = tre.resolve_annotations(annotations, cohort, runs)
        micro = tre.review_metrics(
            tre.score_review_runs(runs, cohort, resolved)
        )["micro"]
        self.assertEqual(micro["tp_findings"], 31)
        self.assertEqual(micro["fp_findings"], 1)
        self.assertEqual(micro["novel_valid"], 1)
        self.assertEqual(micro["duplicates"], 1)

    def test_duplicate_can_reference_a_same_run_primary_novel_finding(self):
        raw_cohort, annotations, raw_runs = make_perfect_dataset()
        first_pr = reporting_pr_ids(raw_cohort)[0]
        primary_id = "finding:novel-primary"
        duplicate_id = "finding:novel-duplicate"
        raw_runs[0]["findings"].extend(
            [finding(primary_id), finding(duplicate_id)]
        )
        for side in ("a", "b"):
            annotations.extend(
                [
                    annotation(
                        annotation_id=f"{primary_id}:{side}",
                        subject_kind="system_finding",
                        subject_id=primary_id,
                        pr_id=first_pr,
                        annotator_id=f"annotator-{side}",
                        label="novel_valid",
                    ),
                    annotation(
                        annotation_id=f"{duplicate_id}:{side}",
                        subject_kind="system_finding",
                        subject_id=duplicate_id,
                        pr_id=first_pr,
                        annotator_id=f"annotator-{side}",
                        label="duplicate",
                        gold_id=primary_id,
                    ),
                ]
            )
        cohort = tre.validate_cohort(raw_cohort, require_materialized=True)
        runs = tre.validate_runs(raw_runs, cohort, config_id="frozen-v1")
        resolved = tre.resolve_annotations(annotations, cohort, runs)
        micro = tre.review_metrics(
            tre.score_review_runs(runs, cohort, resolved)
        )["micro"]
        self.assertEqual(micro["tp_findings"], 31)
        self.assertEqual(micro["fp_findings"], 1)
        self.assertEqual(micro["novel_valid"], 1)
        self.assertEqual(micro["duplicates"], 1)

    def test_second_match_to_same_gold_must_be_labeled_duplicate(self):
        raw_cohort, annotations, raw_runs = make_perfect_dataset()
        first_pr = reporting_pr_ids(raw_cohort)[0]
        finding_id = "finding:bad-second-match"
        raw_runs[0]["findings"].append(finding(finding_id))
        for side in ("a", "b"):
            annotations.append(
                annotation(
                    annotation_id=f"second:{side}",
                    subject_kind="system_finding",
                    subject_id=finding_id,
                    pr_id=first_pr,
                    annotator_id=f"annotator-{side}",
                    label="matched",
                    gold_id="gold:0",
                )
            )
        cohort = tre.validate_cohort(raw_cohort, require_materialized=True)
        runs = tre.validate_runs(raw_runs, cohort, config_id="frozen-v1")
        resolved = tre.resolve_annotations(annotations, cohort, runs)
        with self.assertRaisesRegex(tre.ValidationError, "later findings must be duplicate"):
            tre.score_review_runs(runs, cohort, resolved)

    def test_duplicate_requires_a_primary_match_in_the_same_run(self):
        raw_cohort, annotations, raw_runs = make_perfect_dataset()
        first_pr = reporting_pr_ids(raw_cohort)[0]
        original_finding = raw_runs[0]["findings"][0]["finding_id"]
        raw_runs[0]["findings"] = [finding("finding:orphan-duplicate")]
        annotations = [
            row for row in annotations if row["subject_id"] != original_finding
        ]
        for side in ("a", "b"):
            annotations.append(
                annotation(
                    annotation_id=f"orphan:{side}",
                    subject_kind="system_finding",
                    subject_id="finding:orphan-duplicate",
                    pr_id=first_pr,
                    annotator_id=f"annotator-{side}",
                    label="duplicate",
                    gold_id="gold:0",
                )
            )
        cohort = tre.validate_cohort(raw_cohort, require_materialized=True)
        runs = tre.validate_runs(raw_runs, cohort, config_id="frozen-v1")
        resolved = tre.resolve_annotations(annotations, cohort, runs)
        with self.assertRaisesRegex(tre.ValidationError, "without a matched primary"):
            tre.score_review_runs(runs, cohort, resolved)

    def test_hard_failure_penalizes_recall_without_inventing_precision_denominator(self):
        raw_cohort, annotations, raw_runs = make_perfect_dataset()
        failed_finding = raw_runs[0]["findings"][0]["finding_id"]
        raw_runs[0]["status"] = "failed"
        raw_runs[0]["scorable"] = False
        raw_runs[0]["findings"] = []
        annotations = [
            row for row in annotations if row["subject_id"] != failed_finding
        ]
        cohort = tre.validate_cohort(raw_cohort, require_materialized=True)
        runs = tre.validate_runs(raw_runs, cohort, config_id="frozen-v1")
        resolved = tre.resolve_annotations(annotations, cohort, runs)
        micro = tre.review_metrics(
            tre.score_review_runs(runs, cohort, resolved)
        )["micro"]
        self.assertEqual(micro["tp_findings"], 29)
        self.assertEqual(micro["tp_gold"], 29)
        self.assertEqual(micro["fn_gold"], 1)
        self.assertEqual(micro["precision"], 1.0)
        self.assertAlmostEqual(micro["recall"], 29 / 30, places=6)

    def test_unannotated_finding_fails_closed(self):
        cohort, _, raw_runs, _, resolved = self._validated()
        raw_runs[0]["findings"].append(finding("finding:unannotated"))
        runs = tre.validate_runs(raw_runs, cohort, config_id="frozen-v1")
        with self.assertRaisesRegex(tre.ValidationError, "lacks final"):
            tre.score_review_runs(runs, cohort, resolved)

    def test_zero_denominators_are_null(self):
        raw_cohort = make_cohort()
        bind_gold_hashes(raw_cohort, [])
        cohort = tre.validate_cohort(raw_cohort, require_materialized=True)
        raw_runs = [run(pr_id, findings=[]) for pr_id in reporting_pr_ids(raw_cohort)]
        for row in raw_runs:
            row["frozen_cohort_sha256"] = cohort["_canonical_sha256"]
        runs = tre.validate_runs(raw_runs, cohort, config_id="frozen-v1")
        resolved = tre.resolve_annotations([], cohort, runs)
        micro = tre.review_metrics(
            tre.score_review_runs(runs, cohort, resolved)
        )["micro"]
        self.assertIsNone(micro["precision"])
        self.assertIsNone(micro["recall"])
        self.assertIsNone(micro["f1"])

    def test_macro_metrics_report_defined_component_counts(self):
        cohort, _, _, runs, resolved = self._validated()
        scored = tre.score_review_runs(runs, cohort, resolved)
        empty_counts = {key: 0 for key in tre.COUNT_KEYS}
        for row in scored:
            if row["repository"] == "reporting/alpha":
                row.update(tre._metric_from_counts(empty_counts))
        report = tre.review_metrics(scored)
        self.assertEqual(
            report["repository_macro"]["defined_repositories"],
            {"precision": 2, "recall": 2, "f1": 2},
        )
        self.assertEqual(report["repository_macro"]["total_repositories"], 3)
        self.assertEqual(
            report["pr_macro"]["defined_prs"],
            {"precision": 20, "recall": 20, "f1": 20},
        )
        self.assertEqual(report["pr_macro"]["total_prs"], 30)


class TestBootstrapAndTelemetry(unittest.TestCase):
    def setUp(self):
        raw_cohort, annotations, raw_runs = make_perfect_dataset()
        self.cohort = tre.validate_cohort(raw_cohort, require_materialized=True)
        self.runs = tre.validate_runs(raw_runs, self.cohort, config_id="frozen-v1")
        self.resolved = tre.resolve_annotations(annotations, self.cohort, self.runs)
        self.scored = tre.score_review_runs(self.runs, self.cohort, self.resolved)

    def test_bootstrap_is_seeded_order_independent_and_repository_stratified(self):
        forward = tre.stratified_pr_bootstrap(self.scored, replicates=200, seed=7)
        reverse = tre.stratified_pr_bootstrap(list(reversed(self.scored)), replicates=200, seed=7)
        self.assertEqual(forward, reverse)
        self.assertEqual(forward["method"], "percentile_pr_within_repository")
        self.assertEqual(forward["recall"]["low"], 1.0)
        self.assertEqual(forward["recall"]["high"], 1.0)
        self.assertEqual(forward["recall"]["defined_replicates"], 200)

    def test_bootstrap_reflects_pr_level_variation(self):
        varied = copy.deepcopy(self.scored)
        varied[0]["tp_findings"] = 0
        varied[0]["tp_gold"] = 0
        varied[0]["fn_gold"] = 1
        result = tre.stratified_pr_bootstrap(varied, replicates=500, seed=3)
        self.assertLess(result["recall"]["low"], result["recall"]["high"])
        self.assertLessEqual(result["recall"]["low"], 29 / 30)
        self.assertGreaterEqual(result["recall"]["high"], 29 / 30)

    def test_bootstrap_rejects_non_integer_seed(self):
        with self.assertRaisesRegex(tre.ValidationError, "seed must be an integer"):
            tre.stratified_pr_bootstrap(self.scored, replicates=10, seed=True)

    def test_bootstrap_interval_uses_interpolated_percentiles(self):
        interval = tre._bootstrap_interval(
            [0.0, 10.0],
            alpha=0.05,
            replicates=2,
        )
        self.assertEqual(interval["low"], 0.25)
        self.assertEqual(interval["high"], 9.75)

    def test_telemetry_denominators_include_fail_open_degraded_and_failed(self):
        runs = copy.deepcopy(self.runs)
        runs[0]["status"] = "degraded"
        runs[1]["status"] = "fail_open"
        runs[2]["status"] = "failed"
        runs[2]["scorable"] = False
        runs[2]["findings"] = []
        runs[0]["test_status"] = "failed"
        runs[1]["test_status"] = "passed"
        runs[1]["unauthorized_operation_count"] = 2
        report = tre.telemetry_report(runs)
        self.assertEqual(report["attempted_runs"], 30)
        self.assertEqual(report["scorable_runs"], 29)
        self.assertAlmostEqual(report["degraded_rate"], 1 / 30, places=6)
        self.assertAlmostEqual(report["fail_open_rate"], 1 / 30, places=6)
        self.assertAlmostEqual(report["hard_failure_rate"], 1 / 30, places=6)
        self.assertEqual(report["test_failures"]["eligible_runs"], 2)
        self.assertEqual(report["test_failures"]["rate"], 0.5)
        self.assertEqual(report["unauthorized_operations"]["events"], 2)
        self.assertAlmostEqual(
            report["unauthorized_operations"]["run_rate"],
            1 / 30,
            places=6,
        )
        self.assertEqual(report["tool_calls"]["by_component"]["finder"]["total"], 60)

    def test_full_report_contains_lineage_and_all_sections(self):
        report = tre.build_report(
            self.cohort,
            self.resolved,
            self.runs,
            config_id="frozen-v1",
            replicates=50,
            seed=9,
            input_hashes={"cohort_sha256": digest("manifest")},
        )
        self.assertEqual(report["metric_version"], "trusted-review-v2")
        self.assertRegex(report["generated_at"], r"Z$")
        self.assertEqual(report["input_hashes"], {"cohort_sha256": digest("manifest")})
        self.assertEqual(
            report["gold_freeze_commit"],
            digest("gold-freeze-commit", 40),
        )
        self.assertEqual(
            report["frozen_cohort_sha256"],
            self.cohort["_canonical_sha256"],
        )
        self.assertIn("agreement", report)
        self.assertIn("review", report)
        self.assertIn("bootstrap_95_ci", report)
        self.assertIn("telemetry", report)


class TestInputBoundaryAndCLI(unittest.TestCase):
    def test_existing_eval_or_holdout_path_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            forbidden = Path(tmp) / "eval"
            forbidden.mkdir()
            path = forbidden / "cohort.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(tre.ValidationError, "forbidden inputs"):
                tre.load_json(path)

    def test_canonical_hash_ignores_object_key_order(self):
        self.assertEqual(
            tre.canonical_sha256({"a": 1, "b": 2}),
            tre.canonical_sha256({"b": 2, "a": 1}),
        )

    def test_malformed_jsonl_reports_the_exact_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "annotations.jsonl"
            path.write_text('{"valid": true}\n{"broken":\n', encoding="utf-8")
            with self.assertRaisesRegex(tre.ValidationError, r":2 is not valid JSON"):
                tre.load_jsonl(path)

    def test_validate_cohort_cli_is_offline_and_returns_two_on_invalid_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cohort.json"
            path.write_text(json.dumps(make_cohort(materialized=False)), encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = tre.main(["validate-cohort", "--cohort", str(path)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(stdout.getvalue())["valid"])

            invalid = make_cohort(materialized=False)
            invalid["repositories"] = invalid["repositories"][:2]
            path.write_text(json.dumps(invalid), encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = tre.main(["validate-cohort", "--cohort", str(path)])
            self.assertEqual(code, 2)
            self.assertIn("at least 30 reporting PRs", stderr.getvalue())

    def test_report_cli_loads_hash_binds_and_writes_deterministic_sections(self):
        cohort, annotations, runs = make_perfect_dataset()
        selection_rows = make_selection_log(cohort)
        selection_bytes = jsonl_bytes(selection_rows)
        cohort["selection_log_sha256"] = hashlib.sha256(selection_bytes).hexdigest()
        frozen_cohort_sha256 = tre.canonical_sha256(cohort)
        for row in runs:
            row["frozen_cohort_sha256"] = frozen_cohort_sha256
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cohort_path = root / "cohort.json"
            selection_path = root / "selection.jsonl"
            annotations_path = root / "annotations.jsonl"
            runs_path = root / "runs.jsonl"
            output_path = root / "report.json"
            cohort_path.write_text(json.dumps(cohort), encoding="utf-8")
            selection_path.write_bytes(selection_bytes)
            annotations_path.write_text(
                "\n".join(json.dumps(row) for row in annotations) + "\n",
                encoding="utf-8",
            )
            runs_path.write_text(
                "\n".join(json.dumps(row) for row in runs) + "\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = tre.main(
                    [
                        "report",
                        "--cohort",
                        str(cohort_path),
                        "--selection-log",
                        str(selection_path),
                        "--annotations",
                        str(annotations_path),
                        "--runs",
                        str(runs_path),
                        "--config-id",
                        "frozen-v1",
                        "--bootstrap",
                        "20",
                        "--seed",
                        "4",
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(code, 0, stderr.getvalue())
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["review"]["micro"]["f1"], 1.0)
            self.assertEqual(report["bootstrap_95_ci"]["replicates"], 20)
            self.assertEqual(
                set(report["input_hashes"]),
                {
                    "annotations_sha256",
                    "cohort_sha256",
                    "runs_sha256",
                    "selection_log_sha256",
                },
            )

    def test_verify_selection_cli_checks_the_hashed_candidate_log(self):
        cohort = make_cohort()
        selection_rows = make_selection_log(cohort)
        selection_bytes = jsonl_bytes(selection_rows)
        cohort["selection_log_sha256"] = hashlib.sha256(selection_bytes).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cohort_path = root / "cohort.json"
            selection_path = root / "selection.jsonl"
            cohort_path.write_text(json.dumps(cohort), encoding="utf-8")
            selection_path.write_bytes(selection_bytes)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = tre.main(
                    [
                        "verify-selection",
                        "--cohort",
                        str(cohort_path),
                        "--selection-log",
                        str(selection_path),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(stdout.getvalue())["valid"])

    def test_committed_schemas_and_examples_are_valid_json(self):
        root = Path(__file__).resolve().parents[1]
        schema_root = root / "trusted_review" / "schemas"
        schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(schema_root.glob("*.json"))
        }
        self.assertEqual(
            set(schemas["cohort.schema.json"]["properties"]),
            tre.COHORT_KEYS,
        )
        self.assertEqual(
            set(schemas["cohort.schema.json"]["required"]),
            tre.COHORT_KEYS,
        )
        self.assertEqual(
            set(schemas["annotations.schema.json"]["properties"]),
            tre.ANNOTATION_KEYS,
        )
        self.assertEqual(
            set(schemas["annotations.schema.json"]["required"]),
            tre.ANNOTATION_KEYS,
        )
        self.assertEqual(
            set(schemas["runs.schema.json"]["properties"]),
            tre.RUN_KEYS,
        )
        self.assertEqual(
            set(schemas["runs.schema.json"]["required"]),
            tre.RUN_KEYS,
        )
        plan = json.loads(
            (root / "trusted_review" / "cohort-plan.json").read_text(
                encoding="utf-8"
            )
        )
        validated_plan = tre.validate_cohort(plan, require_materialized=False)
        self.assertEqual(
            validated_plan["cohort_seed"],
            tre.derive_cohort_seed(
                validated_plan["cohort_seed_derivation"]["source_commit"]
            ),
        )
        example_root = root / "trusted_review" / "examples"
        for path in sorted(example_root.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                self.assertIsInstance(json.loads(line), dict)
        annotation_rows = [
            json.loads(line)
            for line in (example_root / "annotations.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        for index, row in enumerate(annotation_rows):
            tre._validate_annotation_row(row, index)
        self.assertEqual(
            annotation_rows[2]["source_annotation_sha256s"],
            [
                tre.canonical_sha256(annotation_rows[0]),
                tre.canonical_sha256(annotation_rows[1]),
            ],
        )
        run_rows = [
            json.loads(line)
            for line in (example_root / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        for index, row in enumerate(run_rows):
            tre._validate_run_row(row, index)


if __name__ == "__main__":
    unittest.main()
