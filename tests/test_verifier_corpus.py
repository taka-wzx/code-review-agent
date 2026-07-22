from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import verifier_corpus as vc


ROOT = Path(__file__).resolve().parents[1]
TRAINING = ROOT / "verifier_training"
EXAMPLES = TRAINING / "examples" / "corpus"


class VerifierCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = vc.load_plan(TRAINING / "corpus-plan.json")
        cls.pr_sources = vc.load_pr_sources(EXAMPLES / "pr-sources.jsonl", cls.plan)
        cls.candidates = vc.load_candidate_sources(
            EXAMPLES / "candidate-sources.jsonl", cls.plan, cls.pr_sources
        )
        cls.annotations = vc.load_annotations(EXAMPLES / "annotations.jsonl", cls.candidates)

    def test_frozen_plan_has_expected_seed_splits_and_limits(self) -> None:
        seed_input = f"{self.plan['corpus_id']}\n{self.plan['base_commit']}".encode()
        self.assertEqual(hashlib.sha256(seed_input).hexdigest(), self.plan["corpus_seed"])
        self.assertEqual(sum(row["target_prs"] for row in self.plan["repositories"]), 29)
        self.assertEqual(
            [row["repository_id"] for row in self.plan["repositories"] if row["split"] == "test"],
            ["pallets/flask", "psf/requests", "encode/httpx"],
        )
        self.assertEqual(self.plan["limits"]["max_paid_model_cny"], 0)
        self.assertEqual(self.plan["limits"]["max_accelerator_hours"], 0)
        self.assertEqual(self.plan["eligibility"]["selection_pool_cap_per_repository"], 64)

    def test_plan_rejects_permission_and_resource_expansion(self) -> None:
        mutated = copy.deepcopy(self.plan)
        mutated["authorization"]["paid_provider"] = True
        with self.assertRaisesRegex(vc.CorpusValidationError, "paid_provider"):
            vc.validate_plan(mutated)

        mutated = copy.deepcopy(self.plan)
        mutated["limits"]["max_raw_bytes"] += 1
        with self.assertRaisesRegex(vc.CorpusValidationError, "512 MiB"):
            vc.validate_plan(mutated)

        mutated = copy.deepcopy(self.plan)
        mutated["repositories"][0]["license_spdx"] = "GPL-3.0-only"
        with self.assertRaisesRegex(vc.CorpusValidationError, "permissive license"):
            vc.validate_plan(mutated)

    def test_pr_sources_are_complete_and_reject_tampering(self) -> None:
        self.assertEqual(len(self.pr_sources), 29)
        self.assertEqual({row["secret_scan_findings"] for row in self.pr_sources}, {0})

        tampered = copy.deepcopy(self.pr_sources)
        tampered[0]["selection_digest"] = "0" * 64
        tampered[0] = vc.with_record_hash(tampered[0], "record_sha256")
        with self.assertRaisesRegex(vc.CorpusValidationError, "selection_digest"):
            vc.validate_pr_sources(tampered, self.plan)

    def test_real_public_source_snapshot_is_complete_but_not_trainable(self) -> None:
        snapshot = TRAINING / "corpus-snapshot"
        pr_sources = vc.load_pr_sources(snapshot / "pr-sources.jsonl", self.plan)
        manifest = vc.validate_acquisition_manifest(
            json.loads((snapshot / "acquisition-manifest.json").read_text(encoding="utf-8")),
            self.plan,
            pr_sources,
        )
        self.assertEqual(len(pr_sources), 29)
        self.assertEqual(manifest["raw_bytes"], 2_021_424)
        self.assertFalse(manifest["trainable"])
        self.assertEqual({row["secret_scan_findings"] for row in pr_sources}, {0})

        tampered = copy.deepcopy(self.pr_sources)
        tampered[0]["secret_scan_findings"] = 1
        tampered[0] = vc.with_record_hash(tampered[0], "record_sha256")
        with self.assertRaisesRegex(vc.CorpusValidationError, "must be zero"):
            vc.validate_pr_sources(tampered, self.plan)

    def test_finder_queue_binds_every_real_source_and_rejects_tampering(self) -> None:
        snapshot = TRAINING / "corpus-snapshot"
        pr_sources = vc.load_pr_sources(snapshot / "pr-sources.jsonl", self.plan)
        queue = vc.validate_finder_queue(
            vc._load_jsonl(snapshot / "finder-queue.jsonl"), pr_sources
        )
        self.assertEqual(len(queue), 29)
        self.assertEqual({row["status"] for row in queue}, {"pending"})
        self.assertEqual(queue, vc.build_finder_queue(pr_sources))

        tampered = copy.deepcopy(queue)
        tampered[0]["diff_sha256"] = "0" * 64
        tampered[0] = vc.with_record_hash(tampered[0], "queue_sha256")
        with self.assertRaisesRegex(vc.CorpusValidationError, "frozen source"):
            vc.validate_finder_queue(tampered, pr_sources)

    def test_candidate_sources_bind_pr_and_reject_duplicate_content(self) -> None:
        tampered = copy.deepcopy(self.candidates)
        tampered[0]["pr_source_sha256"] = "0" * 64
        tampered[0] = vc.with_candidate_source_hashes(tampered[0])
        with self.assertRaisesRegex(vc.CorpusValidationError, "does not bind"):
            vc.validate_candidate_sources(tampered, self.plan, self.pr_sources)

        duplicate = copy.deepcopy(self.candidates)
        copied = copy.deepcopy(duplicate[0])
        copied["candidate_id"] = "synthetic-duplicate-content"
        copied = vc.with_candidate_source_hashes(copied)
        duplicate.append(copied)
        with self.assertRaisesRegex(vc.CorpusValidationError, "duplicate canonical"):
            vc.validate_candidate_sources(duplicate, self.plan, self.pr_sources)

    def test_annotation_resolution_requires_independence_and_fresh_adjudication(self) -> None:
        repeated = copy.deepcopy(self.annotations)
        repeated[1]["annotator_id"] = repeated[0]["annotator_id"]
        repeated[1] = vc.with_annotation_hash(repeated[1])
        repeated = vc.validate_annotations(repeated, self.candidates)
        with self.assertRaisesRegex(vc.CorpusValidationError, "repeats"):
            vc.resolve_annotations(self.candidates, repeated)

        stale = copy.deepcopy(self.annotations)
        adjudication = next(row for row in stale if row["role"] == "adjudicator")
        adjudication["source_annotation_sha256s"][0] = "0" * 64
        updated = vc.with_annotation_hash(adjudication)
        stale[stale.index(adjudication)] = updated
        stale = vc.validate_annotations(stale, self.candidates)
        with self.assertRaisesRegex(vc.CorpusValidationError, "stale"):
            vc.resolve_annotations(self.candidates, stale)

    def test_synthetic_example_freeze_is_deterministic_and_not_trainable(self) -> None:
        first = vc.build_freeze(
            self.plan,
            self.pr_sources,
            self.candidates,
            self.annotations,
            "2026-07-19T02:00:00Z",
        )
        second = vc.build_freeze(
            self.plan,
            list(reversed(self.pr_sources)),
            list(reversed(self.candidates)),
            list(reversed(self.annotations)),
            "2026-07-19T02:00:00Z",
        )
        self.assertEqual(first, second)
        frozen, splits, manifest = first
        self.assertEqual(len(frozen), 3)
        self.assertEqual(splits["splits"]["test"], ["pallets/flask"])
        self.assertFalse(manifest["trainable"])
        self.assertEqual(
            {gate["gate"] for gate in manifest["incomplete_gates"]},
            {
                "repository_without_resolved_candidates",
                "selected_pr_without_candidates",
                "synthetic_records_present",
            },
        )

    def test_completed_zero_candidate_sources_close_only_the_execution_gate(self) -> None:
        completed = {row["source_id"] for row in self.pr_sources}
        _, _, manifest = vc.build_freeze(
            self.plan,
            self.pr_sources,
            self.candidates,
            self.annotations,
            "2026-07-22T01:00:00Z",
            completed_source_ids=completed,
        )
        self.assertNotIn(
            "selected_pr_without_candidates",
            {gate["gate"] for gate in manifest["incomplete_gates"]},
        )
        self.assertFalse(manifest["trainable"])

        with self.assertRaisesRegex(vc.CorpusValidationError, "outside the admitted"):
            vc.build_freeze(
                self.plan,
                self.pr_sources,
                self.candidates,
                self.annotations,
                "2026-07-22T01:00:00Z",
                completed_source_ids=completed | {"foreign/repo#1"},
            )

    def test_complete_real_annotations_open_the_trainable_gate(self) -> None:
        candidates: list[dict[str, object]] = []
        annotations: list[dict[str, object]] = []
        for index, pr_source in enumerate(self.pr_sources, 1):
            candidate = vc.with_candidate_source_hashes(
                {
                    "schema_version": 1,
                    "candidate_id": f"real-candidate-{index}",
                    "source_id": pr_source["source_id"],
                    "repository_id": pr_source["repository_id"],
                    "source_revision": pr_source["merge_sha"],
                    "pr_source_sha256": pr_source["record_sha256"],
                    "candidate_text": f"Distinct candidate finding for admitted source {index}.",
                    "evidence": [
                        {
                            "kind": "positive",
                            "path": f"src/real_{index}.py",
                            "line": index,
                            "summary": f"Distinct bounded evidence for source {index}.",
                        }
                    ],
                    "tool_summaries": [],
                    "pair_id": None,
                    "language": "python",
                    "severity": "medium",
                    "content_sha256": "",
                    "candidate_source_sha256": "",
                }
            )
            candidates.append(candidate)
            for annotator in ("human-reviewer-a", "human-reviewer-b"):
                annotations.append(
                    vc.with_annotation_hash(
                        {
                            "schema_version": 1,
                            "annotation_id": f"{candidate['candidate_id']}-{annotator}",
                            "candidate_id": candidate["candidate_id"],
                            "candidate_source_sha256": candidate["candidate_source_sha256"],
                            "annotator_id": annotator,
                            "role": "annotator",
                            "label": "keep",
                            "rationale": "The bounded evidence demonstrates the reported behavior.",
                            "evidence_sha256": candidate["content_sha256"],
                            "source_annotation_ids": [],
                            "source_annotation_sha256s": [],
                            "created_at": "2026-07-19T03:00:00Z",
                            "synthetic": False,
                            "annotation_sha256": "",
                        }
                    )
                )
        candidates = vc.validate_candidate_sources(candidates, self.plan, self.pr_sources)
        annotations = vc.validate_annotations(annotations, candidates)
        _, splits, manifest = vc.build_freeze(
            self.plan,
            self.pr_sources,
            candidates,
            annotations,
            "2026-07-19T04:00:00Z",
        )
        self.assertTrue(manifest["trainable"])
        self.assertEqual(manifest["incomplete_gates"], [])
        self.assertEqual(sum(len(rows) for rows in splits["splits"].values()), 9)

    def test_cli_validate_and_freeze_examples(self) -> None:
        common = [
            "--plan",
            str(TRAINING / "corpus-plan.json"),
            "--pr-sources",
            str(EXAMPLES / "pr-sources.jsonl"),
            "--candidate-sources",
            str(EXAMPLES / "candidate-sources.jsonl"),
            "--annotations",
            str(EXAMPLES / "annotations.jsonl"),
        ]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(vc.main(["validate", *common]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["pr_sources"], 29)

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = vc.main(
                    [
                        "freeze",
                        *common,
                        "--frozen-at",
                        "2026-07-19T02:00:00Z",
                        "--candidates-out",
                        str(temporary / "candidates.jsonl"),
                        "--splits-out",
                        str(temporary / "splits.json"),
                        "--manifest-out",
                        str(temporary / "manifest.json"),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertFalse(json.loads(stdout.getvalue())["trainable"])
            self.assertTrue((temporary / "candidates.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
