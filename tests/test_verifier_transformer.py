from __future__ import annotations

import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import verifier_training as vt
import verifier_transformer as vtf


ROOT = Path(__file__).resolve().parents[1]
TRAINING = ROOT / "verifier_training"


class VerifierTransformerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = vtf.load_config(TRAINING / "phase8c-config.json")
        cls.snapshot = vtf.validate_model_snapshot(
            json.loads((TRAINING / "model-snapshot.json").read_text(encoding="utf-8"))
        )

    def test_frozen_config_is_cpu_only_zero_cost_and_synthetic(self) -> None:
        self.assertTrue(self.config["synthetic_only"])
        self.assertEqual(self.config["cpu_threads"], 4)
        self.assertEqual(self.config["paid_cost_cny"], 0)
        self.assertEqual(self.config["accelerator_hours"], 0)
        self.assertEqual(tuple(self.config["experiments"]), vtf.EXPERIMENTS)
        self.assertEqual(self.config["input_template"], vtf.INPUT_TEMPLATE)
        self.assertEqual(self.config["dataset_sha256"], vtf.SYNTHETIC_DATASET_SHA256)

    def test_config_rejects_scope_and_resource_expansion(self) -> None:
        mutated = copy.deepcopy(self.config)
        mutated["synthetic_only"] = False
        with self.assertRaisesRegex(vtf.TransformerValidationError, "synthetic-only"):
            vtf.validate_config(mutated)

        mutated = copy.deepcopy(self.config)
        mutated["max_runtime_bytes"] += 1
        with self.assertRaisesRegex(vtf.TransformerValidationError, "ceiling"):
            vtf.validate_config(mutated)

        mutated = copy.deepcopy(self.config)
        mutated["input_template"] += " changed"
        with self.assertRaisesRegex(vtf.TransformerValidationError, "input_template"):
            vtf.validate_config(mutated)

    def test_model_snapshot_requires_exact_safe_hashes(self) -> None:
        self.assertEqual(self.snapshot["format"], "safetensors")
        self.assertEqual(self.snapshot["license_spdx"], "Apache-2.0")
        mutated = copy.deepcopy(self.snapshot)
        mutated["files"][2]["path"] = "pytorch_model.bin"
        with self.assertRaisesRegex(vtf.TransformerValidationError, "unsafe"):
            vtf.validate_model_snapshot(mutated)

        mutated = copy.deepcopy(self.snapshot)
        mutated["revision"] = "main"
        with self.assertRaisesRegex(vtf.TransformerValidationError, "commit SHA"):
            vtf.validate_model_snapshot(mutated)

    def test_candidate_text_is_bounded_structured_input(self) -> None:
        rows = vt.load_candidates(TRAINING / "examples" / "candidates.jsonl")
        text = vtf.candidate_text(rows[0])
        self.assertIn("finding:", text)
        self.assertIn("evidence:", text)
        self.assertIn("tools:", text)
        self.assertNotIn(str(ROOT), text)

    def test_validate_cli_does_not_import_training_runtime(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = vtf.main(
                [
                    "validate",
                    "--config",
                    str(TRAINING / "phase8c-config.json"),
                    "--model-snapshot",
                    str(TRAINING / "model-snapshot.json"),
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "ok")

    def test_frozen_phase8c_reports_are_hash_bound_and_non_claiming(self) -> None:
        result_root = TRAINING / "examples" / "phase8c"
        reports = [
            vtf.validate_experiment_report(
                json.loads((result_root / f"{name}.json").read_text(encoding="utf-8")),
                self.config,
            )
            for name in vtf.EXPERIMENTS
        ]
        comparison = vtf.validate_comparison(
            json.loads((result_root / "comparison.json").read_text(encoding="utf-8")),
            self.config,
            reports,
        )
        self.assertFalse(comparison["quality_claim_allowed"])
        self.assertEqual(comparison["config_sha256"], vtf._sha256(self.config))
        self.assertEqual(len({report["dataset_sha256"] for report in reports}), 1)
        self.assertEqual(
            [report["resources"]["trainable_parameters"] for report in reports],
            [4_386_178, 4_386_178, 4_354, 4_354],
        )

        malformed = copy.deepcopy(reports[0])
        malformed["predictions"][0] = "not-an-object"
        with self.assertRaisesRegex(vtf.TransformerValidationError, "must be an object"):
            vtf.validate_experiment_report(malformed, self.config)

        malformed_comparison = copy.deepcopy(comparison)
        malformed_comparison["experiments"] = ["not-an-object"] * 4
        with self.assertRaisesRegex(vtf.TransformerValidationError, "four-object list"):
            vtf.validate_comparison(malformed_comparison, self.config, reports)


if __name__ == "__main__":
    unittest.main()
