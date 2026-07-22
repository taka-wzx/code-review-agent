from __future__ import annotations

import json
import unittest
from pathlib import Path

import verifier_phase8d as phase8d
import verifier_phase8d_simulate as simulate


class Phase8DSimulationTests(unittest.TestCase):
    def test_responses_are_deterministic_and_non_semantic(self) -> None:
        packet = {
            "synthetic": True,
            "reviewer_id": "synthetic-reviewer-a-v1",
            "mode": "independent",
            "items": [{"candidate_id": "candidate-1"}, {"candidate_id": "candidate-2"}],
        }
        first = simulate.build_synthetic_responses(packet, "2026-07-22T12:00:00Z")
        second = simulate.build_synthetic_responses(packet, "2026-07-22T12:00:00Z")
        self.assertEqual(first, second)
        self.assertEqual({row["candidate_id"] for row in first}, {"candidate-1", "candidate-2"})
        self.assertTrue(all(row["label"] in {"keep", "drop", "uncertain"} for row in first))
        self.assertTrue(all("no candidate-quality judgment" in row["rationale"] for row in first))

    def test_adjudication_never_returns_uncertain(self) -> None:
        labels = {
            simulate.synthetic_label(f"candidate-{index}", "synthetic-adjudicator-v1", "adjudication")[0]
            for index in range(100)
        }
        self.assertLessEqual(labels, {"keep", "drop"})
        self.assertTrue(labels)

    def test_real_packet_is_refused(self) -> None:
        packet = {
            "synthetic": False,
            "reviewer_id": "human-reviewer-a-v1",
            "mode": "independent",
            "items": [{"candidate_id": "candidate-1"}],
        }
        with self.assertRaisesRegex(
            phase8d.Phase8DValidationError, "refuses a real annotation packet"
        ):
            simulate.build_synthetic_responses(packet, "2026-07-22T12:00:00Z")

    def test_committed_simulation_is_hash_bound_and_real_gates_stay_closed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        path = root / "verifier_training" / "synthetic" / "phase8d-simulation-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        payload = {key: manifest[key] for key in sorted(manifest) if key != "manifest_sha256"}
        self.assertEqual(manifest["manifest_sha256"], simulate._sha256(payload))
        self.assertEqual(manifest["candidate_count"], 137)
        self.assertEqual(manifest["independent_annotation_count"], 274)
        self.assertEqual(manifest["adjudication_candidate_count"], 70)
        self.assertFalse(manifest["real_human_gate_complete"])
        self.assertFalse(manifest["quality_claim_allowed"])
        self.assertFalse(manifest["freeze_trainable"])
        self.assertFalse(manifest["real_model_readiness"]["ready"])
        for artifact in manifest["artifacts"].values():
            artifact_path = root / artifact["path"]
            self.assertEqual(simulate._artifact_hash(artifact_path), artifact["sha256"])


if __name__ == "__main__":
    unittest.main()
