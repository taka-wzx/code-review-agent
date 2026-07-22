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
import verifier_phase8d as v8d


ROOT = Path(__file__).resolve().parents[1]
TRAINING = ROOT / "verifier_training"
SNAPSHOT = TRAINING / "corpus-snapshot"


class Phase8DOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = v8d.load_config(TRAINING / "phase8d-config.json")
        cls.plan = vc.load_plan(TRAINING / "corpus-plan.json")
        cls.sources = vc.load_pr_sources(SNAPSHOT / "pr-sources.jsonl", cls.plan)
        cls.queue = vc.validate_finder_queue(
            vc._load_jsonl(SNAPSHOT / "finder-queue.jsonl"), cls.sources
        )
        first_by_repository: dict[str, dict[str, object]] = {}
        for source in cls.sources:
            first_by_repository.setdefault(source["repository_id"], source)
        cls.candidates = []
        for index, source in enumerate(first_by_repository.values(), 1):
            cls.candidates.append(
                vc.with_candidate_source_hashes(
                    {
                        "schema_version": 1,
                        "candidate_id": f"phase8d-synthetic-{index}",
                        "source_id": source["source_id"],
                        "repository_id": source["repository_id"],
                        "source_revision": source["merge_sha"],
                        "pr_source_sha256": source["record_sha256"],
                        "candidate_text": f"Synthetic Phase 8D candidate {index} for protocol tests.",
                        "evidence": [
                            {
                                "kind": "positive",
                                "path": f"src/protocol_{index}.py",
                                "line": index,
                                "summary": f"Bounded synthetic evidence {index}.",
                            }
                        ],
                        "tool_summaries": [],
                        "pair_id": source["source_id"],
                        "language": "python",
                        "severity": "medium",
                        "content_sha256": "",
                        "candidate_source_sha256": "",
                    }
                )
            )
        cls.candidates = vc.validate_candidate_sources(cls.candidates, cls.plan, cls.sources)
        cls.runs = cls._build_runs()

    @classmethod
    def _build_runs(cls) -> list[dict[str, object]]:
        candidate_by_source = {
            candidate["source_id"]: candidate["candidate_id"] for candidate in cls.candidates
        }
        runs = []
        for index, queue in enumerate(cls.queue, 1):
            candidate_ids = (
                [candidate_by_source[queue["source_id"]]]
                if queue["source_id"] in candidate_by_source
                else []
            )
            runs.append(
                v8d.with_finder_run_hash(
                    {
                        "schema_version": 1,
                        "run_id": f"synthetic-run-{index}",
                        "queue_id": queue["queue_id"],
                        "queue_sha256": queue["queue_sha256"],
                        "source_id": queue["source_id"],
                        "pr_source_sha256": queue["pr_source_sha256"],
                        "diff_sha256": queue["diff_sha256"],
                        "status": "completed" if candidate_ids else "completed_zero_candidates",
                        "candidate_ids": candidate_ids,
                        "candidate_count": len(candidate_ids),
                        "provider": cls.config["finder"]["provider"],
                        "model": cls.config["finder"]["model"],
                        "prompt_sha256": cls.config["finder"]["prompt_sha256"],
                        "started_at": "2026-07-22T01:00:00Z",
                        "finished_at": "2026-07-22T01:00:01Z",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_cny": 0,
                        "trace_sha256": hashlib.sha256(
                            f"synthetic-trace-{index}".encode()
                        ).hexdigest(),
                        "error_category": None,
                        "synthetic": True,
                        "run_sha256": "",
                    }
                )
            )
        return runs

    @staticmethod
    def _responses(
        packet: dict[str, object], labels: dict[str, str]
    ) -> list[dict[str, str]]:
        return [
            {
                "candidate_id": item["candidate_id"],
                "label": labels[item["candidate_id"]],
                "rationale": f"Independent protocol rationale for {item['candidate_id']}.",
                "created_at": "2026-07-22T02:00:00Z",
            }
            for item in packet["items"]
        ]

    def test_config_freezes_the_exact_glm_amendment(self) -> None:
        self.assertFalse(self.config["offline_preparation_only"])
        self.assertTrue(self.config["authorization"]["provider_calls"])
        self.assertTrue(self.config["authorization"]["raw_diff_read"])
        self.assertEqual(self.config["finder"]["model"], "glm-5.2")
        self.assertEqual(self.config["finder"]["max_calls"], 580)
        self.assertEqual(self.config["humans"], {
            "annotator_a": "human-reviewer-a-v1",
            "annotator_b": "human-reviewer-b-v1",
            "adjudicator": "human-adjudicator-c-v1",
        })

        mutated = copy.deepcopy(self.config)
        mutated["authorization"]["raw_diff_read"] = False
        with self.assertRaisesRegex(v8d.Phase8DValidationError, "GLM amendment"):
            v8d.validate_config(mutated)

        mutated = copy.deepcopy(self.config)
        mutated["finder"]["max_cost_cny"] = 251
        with self.assertRaisesRegex(v8d.Phase8DValidationError, "authorized GLM value"):
            v8d.validate_config(mutated)

    def test_finder_envelopes_are_complete_deterministic_and_executable(self) -> None:
        first = v8d.build_finder_envelopes(self.config, self.queue, self.sources)
        second = v8d.build_finder_envelopes(
            self.config, list(reversed(self.queue)), self.sources
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 29)
        self.assertEqual({row["executable"] for row in first}, {True})
        self.assertEqual(
            {tuple(row["blocked_by"]) for row in first},
            {()},
        )

    def test_finder_receipts_accept_honest_zero_and_reject_tampering_or_real_calls(self) -> None:
        rows = v8d.validate_finder_runs(
            self.runs,
            self.config,
            self.plan,
            self.queue,
            self.sources,
            self.candidates,
        )
        self.assertEqual(len(rows), 29)
        self.assertEqual(sum(row["status"] == "completed" for row in rows), 9)
        self.assertEqual(sum(row["status"] == "completed_zero_candidates" for row in rows), 20)

        tampered = copy.deepcopy(self.runs)
        zero = next(row for row in tampered if row["status"] == "completed_zero_candidates")
        zero["candidate_count"] = 1
        zero["run_sha256"] = v8d._sha256(v8d._without_hash(zero, "run_sha256"))
        with self.assertRaisesRegex(v8d.Phase8DValidationError, "candidate_count"):
            v8d.validate_finder_runs(
                tampered,
                self.config,
                self.plan,
                self.queue,
                self.sources,
                self.candidates,
            )

        real = copy.deepcopy(self.runs)
        for row in real:
            row["synthetic"] = False
            row["run_sha256"] = v8d._sha256(v8d._without_hash(row, "run_sha256"))
        self.assertEqual(
            len(
                v8d.validate_finder_runs(
                    real,
                    self.config,
                    self.plan,
                    self.queue,
                    self.sources,
                    self.candidates,
                )
            ),
            29,
        )

    def test_blind_packets_import_two_labels_and_fresh_adjudication(self) -> None:
        rubric = hashlib.sha256(b"phase8d-rubric-v1").hexdigest()
        packet_a = v8d.build_independent_packet(
            self.candidates,
            "synthetic-human-a",
            rubric,
            101,
            "2026-07-22T01:30:00Z",
            synthetic=True,
        )
        packet_b = v8d.build_independent_packet(
            self.candidates,
            "synthetic-human-b",
            rubric,
            202,
            "2026-07-22T01:30:00Z",
            synthetic=True,
        )
        v8d.validate_packet(packet_a, self.candidates)
        v8d.validate_packet(packet_b, self.candidates)
        v8d.validate_independent_packet_pair(packet_a, packet_b)
        forbidden_keys = {"split", "label", "score", "prediction", "peer_label"}
        self.assertFalse(forbidden_keys & set(packet_a["items"][0]))
        self.assertNotEqual(
            [row["candidate_id"] for row in packet_a["items"]],
            [row["candidate_id"] for row in packet_b["items"]],
        )

        labels_a = {candidate["candidate_id"]: "keep" for candidate in self.candidates}
        labels_b = dict(labels_a)
        disputed_id = self.candidates[0]["candidate_id"]
        labels_b[disputed_id] = "drop"
        annotations_a = v8d.import_packet_responses(
            packet_a, self.candidates, self._responses(packet_a, labels_a)
        )
        annotations_b = v8d.import_packet_responses(
            packet_b, self.candidates, self._responses(packet_b, labels_b)
        )
        adjudication_packet = v8d.build_adjudication_packet(
            self.candidates,
            annotations_a + annotations_b,
            "synthetic-human-c",
            rubric,
            303,
            "2026-07-22T02:30:00Z",
        )
        self.assertEqual(
            [item["candidate_id"] for item in adjudication_packet["items"]],
            [disputed_id],
        )
        adjudications = v8d.import_packet_responses(
            adjudication_packet,
            self.candidates,
            [
                {
                    "candidate_id": disputed_id,
                    "label": "keep",
                    "rationale": "Synthetic adjudicator retains the supported issue.",
                    "created_at": "2026-07-22T03:00:00Z",
                }
            ],
        )
        merged, summary = v8d.merge_annotations(
            self.candidates, [annotations_a, annotations_b, adjudications]
        )
        self.assertTrue(summary["ready_to_freeze"])
        self.assertEqual(summary["agreement"]["adjudications"], 1)
        self.assertEqual(len(merged), len(self.candidates) * 2 + 1)

        repeated = copy.deepcopy(adjudication_packet)
        repeated["reviewer_id"] = "synthetic-human-a"
        repeated = v8d._with_hash(repeated, "packet_sha256")
        with self.assertRaisesRegex(v8d.Phase8DValidationError, "repeats"):
            v8d.import_packet_responses(
                repeated,
                self.candidates,
                [
                    {
                        "candidate_id": disputed_id,
                        "label": "keep",
                        "rationale": "Invalid repeated reviewer.",
                        "created_at": "2026-07-22T03:00:00Z",
                    }
                ],
            )

    def test_real_freeze_wrapper_binds_receipts_but_synthetic_fixture_stays_closed(self) -> None:
        annotations = []
        for candidate in self.candidates:
            for reviewer in ("synthetic-human-a", "synthetic-human-b"):
                annotations.append(
                    vc.with_annotation_hash(
                        {
                            "schema_version": 1,
                            "annotation_id": f"{candidate['candidate_id']}-{reviewer}",
                            "candidate_id": candidate["candidate_id"],
                            "candidate_source_sha256": candidate["candidate_source_sha256"],
                            "annotator_id": reviewer,
                            "role": "annotator",
                            "label": "keep",
                            "rationale": "Synthetic agreement for Phase 8D protocol testing.",
                            "evidence_sha256": candidate["content_sha256"],
                            "source_annotation_ids": [],
                            "source_annotation_sha256s": [],
                            "created_at": "2026-07-22T02:00:00Z",
                            "synthetic": True,
                            "annotation_sha256": "",
                        }
                    )
                )
        frozen, splits, manifest = v8d.build_real_freeze(
            self.config,
            self.plan,
            self.sources,
            self.queue,
            self.runs,
            self.candidates,
            annotations,
            "2026-07-22T04:00:00Z",
        )
        self.assertEqual(len(frozen), 9)
        self.assertEqual(len(splits["splits"]["test"]), 3)
        self.assertFalse(manifest["trainable"])
        gates = {gate["gate"] for gate in manifest["incomplete_gates"]}
        self.assertNotIn("selected_pr_without_candidates", gates)
        self.assertIn("synthetic_records_present", gates)
        self.assertIn("synthetic_finder_runs_present", gates)
        readiness = v8d.real_model_readiness(self.config, manifest)
        self.assertFalse(readiness["ready"])
        self.assertIn("real_model_training_unauthorized", readiness["blocked_by"])
        with self.assertRaisesRegex(v8d.Phase8DValidationError, "blocked"):
            v8d.assert_real_model_ready(self.config, manifest)

    def test_cli_validates_config_and_prepares_only_blocked_envelopes(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(
                v8d.main(
                    [
                        "validate-config",
                        "--config",
                        str(TRAINING / "phase8d-config.json"),
                    ]
                ),
                0,
            )
        self.assertTrue(json.loads(stdout.getvalue())["provider_calls_authorized"])

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "envelopes.jsonl"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    v8d.main(
                        [
                            "prepare-finder",
                            "--config",
                            str(TRAINING / "phase8d-config.json"),
                            "--plan",
                            str(TRAINING / "corpus-plan.json"),
                            "--pr-sources",
                            str(SNAPSHOT / "pr-sources.jsonl"),
                            "--queue",
                            str(SNAPSHOT / "finder-queue.jsonl"),
                            "--out",
                            str(output),
                        ]
                    ),
                    0,
                )
            self.assertEqual(json.loads(stdout.getvalue())["executable"], 29)
            self.assertEqual(len(v8d._load_jsonl(output)), 29)


if __name__ == "__main__":
    unittest.main()
