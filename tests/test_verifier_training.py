from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import verifier_training as vt


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "verifier_training" / "examples"
SCHEMAS = ROOT / "verifier_training" / "schemas"


class VerifierTrainingDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = vt.load_candidates(EXAMPLES / "candidates.jsonl")
        cls.manifest = vt.load_split_manifest(EXAMPLES / "split-manifest.json", cls.rows)

    def test_example_dataset_is_repository_split_and_hash_stable(self) -> None:
        self.assertEqual(len(self.rows), 9)
        self.assertEqual(vt.dataset_sha256(self.rows), self.manifest["dataset_sha256"])
        self.assertEqual(vt.dataset_sha256(list(reversed(self.rows))), self.manifest["dataset_sha256"])
        self.assertEqual(
            self.manifest["splits"],
            {
                "train": ["repo-train"],
                "validation": ["repo-validation"],
                "test": ["repo-test"],
            },
        )

    def test_candidate_rejects_unknown_fields_and_hash_tampering(self) -> None:
        unknown = copy.deepcopy(self.rows[0])
        unknown["unexpected"] = True
        with self.assertRaisesRegex(vt.ValidationError, "unknown"):
            vt.validate_candidate_row(unknown)

        tampered = copy.deepcopy(self.rows[0])
        tampered["candidate_text"] += " Changed after freeze."
        with self.assertRaisesRegex(vt.ValidationError, "content_sha256"):
            vt.validate_candidate_row(tampered)

    def test_candidate_rejects_sensitive_text_and_unsafe_path(self) -> None:
        sensitive = copy.deepcopy(self.rows[0])
        sensitive["candidate_text"] = "The password=abcdefgh1234 was copied into a finding."
        sensitive = vt.with_candidate_hashes(sensitive)
        with self.assertRaisesRegex(vt.ValidationError, "credential-like"):
            vt.validate_candidate_row(sensitive)

        unsafe = copy.deepcopy(self.rows[0])
        unsafe["evidence"][0]["path"] = "../outside.py"
        unsafe = vt.with_candidate_hashes(unsafe)
        with self.assertRaisesRegex(vt.ValidationError, "repository-relative"):
            vt.validate_candidate_row(unsafe)

    def test_split_rejects_repository_overlap(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["splits"]["test"].append("repo-train")
        with self.assertRaisesRegex(vt.ValidationError, "occurs in both"):
            vt.validate_split_manifest(manifest, self.rows)

    def test_split_rejects_duplicate_content_across_repositories(self) -> None:
        rows = copy.deepcopy(self.rows)
        duplicate = copy.deepcopy(rows[0])
        duplicate.update(
            {
                "candidate_id": "validation-duplicate",
                "repository_id": "repo-validation",
                "change_id": "change-validation-duplicate",
                "source_revision": "a" * 40,
                "pair_id": "pair-validation-duplicate",
            }
        )
        rows.append(vt.with_candidate_hashes(duplicate))
        manifest = copy.deepcopy(self.manifest)
        manifest["dataset_sha256"] = vt.dataset_sha256(rows)
        with self.assertRaisesRegex(vt.ValidationError, "content_sha256"):
            vt.validate_split_manifest(manifest, rows)

    def test_all_json_schemas_are_well_formed(self) -> None:
        names = {
            "annotation-packet.schema.json",
            "annotation-response.schema.json",
            "candidate.schema.json",
            "candidate-source.schema.json",
            "corpus-annotation.schema.json",
            "experiment.schema.json",
            "finder-run.schema.json",
            "freeze-manifest.schema.json",
            "prediction.schema.json",
            "pr-source.schema.json",
            "real-freeze-manifest.schema.json",
            "split-manifest.schema.json",
        }
        self.assertEqual({path.name for path in SCHEMAS.glob("*.json")}, names)
        for path in SCHEMAS.glob("*.json"):
            schema = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertFalse(schema["additionalProperties"])


class VerifierTrainingMetricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = vt.load_candidates(EXAMPLES / "candidates.jsonl")
        cls.manifest = vt.load_split_manifest(EXAMPLES / "split-manifest.json", cls.rows)
        cls.predictions = vt.load_predictions(EXAMPLES / "predictions.jsonl")

    def test_confusion_metrics_distinguish_undefined_from_zero(self) -> None:
        metrics = vt.confusion_metrics([(0, 0.1)], 0.5)
        self.assertIsNone(metrics["precision"])
        self.assertIsNone(metrics["recall"])
        self.assertIsNone(metrics["f1"])
        self.assertEqual(metrics["tn"], 1)

    def test_pr_curve_groups_ties_and_uses_average_precision(self) -> None:
        report = vt.precision_recall_curve([(1, 0.8), (0, 0.8), (1, 0.2)])
        self.assertEqual(len(report["points"]), 2)
        self.assertEqual(report["points"][0], {"threshold": 0.8, "precision": 0.5, "recall": 0.5})
        self.assertEqual(report["average_precision"], 0.58333334)

    def test_calibration_reports_empty_bins_and_ece(self) -> None:
        report = vt.calibration_report([(0, 0.1), (1, 0.9)], bins=2)
        self.assertEqual(report["ece"], 0.1)
        self.assertEqual([bucket["count"] for bucket in report["bins"]], [1, 1])
        empty = vt.calibration_report([], bins=2)
        self.assertIsNone(empty["ece"])

    def test_threshold_selection_is_deterministic(self) -> None:
        selected = vt.select_threshold([(1, 0.8), (0, 0.6), (1, 0.4), (0, 0.2)])
        self.assertEqual(selected["threshold"], 0.4)
        self.assertEqual(selected["validation_metrics"]["f1"], 0.8)
        with self.assertRaisesRegex(vt.ValidationError, "both keep and drop"):
            vt.select_threshold([(1, 0.8), (1, 0.4)])

    def test_validation_threshold_selection_requires_complete_predictions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prediction_path = Path(directory) / "validation.jsonl"
            prediction_path.write_text(
                '{"schema_version":1,"experiment_id":"partial",'
                '"candidate_id":"validation-keep","score":0.8,"latency_ms":1}\n',
                encoding="utf-8",
            )
            args = type(
                "Args",
                (),
                {
                    "data": EXAMPLES / "candidates.jsonl",
                    "splits": EXAMPLES / "split-manifest.json",
                    "predictions": prediction_path,
                    "threshold": None,
                    "split": "validation",
                    "ece_bins": 10,
                },
            )()
            with self.assertRaisesRegex(vt.ValidationError, "missing predictions"):
                vt._command_evaluate(args)

    def test_example_evaluation_excludes_uncertain_and_reports_per_repository(self) -> None:
        report = vt.evaluate_predictions(
            self.rows,
            self.predictions,
            self.manifest,
            "test",
            0.5,
            threshold_source="manifest_validation",
        )
        self.assertEqual(report["support"], {"total": 3, "binary": 2, "uncertain_excluded": 1})
        self.assertEqual(report["micro"]["f1"], 1.0)
        self.assertEqual(report["calibration"]["ece"], 0.1)
        self.assertEqual(report["per_repository"]["repo-test"]["support"], 2)
        self.assertEqual(report["errors"], [])

    def test_prediction_rejects_nonfinite_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "predictions.jsonl"
            path.write_text(
                '{"schema_version":1,"experiment_id":"bad","candidate_id":"test-keep",'
                '"score":NaN,"latency_ms":1}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(vt.ValidationError, "finite"):
                vt.load_predictions(path)


class VerifierTrainingBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = vt.load_candidates(EXAMPLES / "candidates.jsonl")
        cls.manifest = vt.load_split_manifest(EXAMPLES / "split-manifest.json", cls.rows)
        cls.train_rows = [row for row in cls.rows if row["repository_id"] == "repo-train"]

    def test_logreg_baseline_is_seed_deterministic(self) -> None:
        first = vt.train_lexical_baseline(
            self.train_rows, algorithm="logreg", dimensions=32, epochs=10, seed=5
        )
        second = vt.train_lexical_baseline(
            self.train_rows, algorithm="logreg", dimensions=32, epochs=10, seed=5
        )
        self.assertEqual(first, second)
        predictions = vt.predict_lexical_baseline(first, self.train_rows, "logreg-test")
        score_by_id = {row["candidate_id"]: row["score"] for row in predictions}
        self.assertGreater(score_by_id["train-keep"], score_by_id["train-drop"])

    def test_pairwise_baseline_orders_complete_pair(self) -> None:
        model = vt.train_lexical_baseline(
            self.train_rows, algorithm="pairwise", dimensions=32, epochs=10, seed=5
        )
        predictions = vt.predict_lexical_baseline(model, self.train_rows, "pairwise-test")
        score_by_id = {row["candidate_id"]: row["score"] for row in predictions}
        self.assertGreater(score_by_id["train-keep"], score_by_id["train-drop"])

    def test_train_cli_writes_auditable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "model.json"
            prediction_path = Path(directory) / "predictions.jsonl"
            output = io.StringIO()
            with redirect_stdout(output):
                result = vt.main(
                    [
                        "train",
                        "--data",
                        str(EXAMPLES / "candidates.jsonl"),
                        "--splits",
                        str(EXAMPLES / "split-manifest.json"),
                        "--algorithm",
                        "logreg",
                        "--experiment-id",
                        "cli-test",
                        "--dimensions",
                        "32",
                        "--epochs",
                        "5",
                        "--model-out",
                        str(model_path),
                        "--predictions-out",
                        str(prediction_path),
                    ]
                )
            self.assertEqual(result, 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["model_kind"], "pipeline_lexical_baseline")
            model = json.loads(model_path.read_text(encoding="utf-8"))
            self.assertEqual(model["training_dataset_sha256"], self.manifest["dataset_sha256"])
            self.assertEqual(model["train_repositories"], ["repo-train"])
            predictions = vt.load_predictions(prediction_path)
            self.assertEqual({row["candidate_id"] for row in predictions}, {"test-keep", "test-drop", "test-uncertain"})


if __name__ == "__main__":
    unittest.main()
