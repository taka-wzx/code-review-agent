"""Bounded, offline Phase 8C transformer experiment runner.

The heavy dependencies and model live in an ignored task-local environment.
Imports stay lazy so the normal project environment remains dependency-free.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import verifier_training as vt


SCHEMA_VERSION = 1
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
INPUT_TEMPLATE = "finding: {candidate_text} evidence: {evidence} tools: {tools}"
SYNTHETIC_DATASET_SHA256 = "4a7f1f4a9c0e6ff52b781ca5a31d30c6af7a484a984c4e695d5962e20344e8e8"
CONFIG_KEYS = {
    "schema_version",
    "experiment_family",
    "seed",
    "model_snapshot",
    "runtime_lock",
    "model_root",
    "data",
    "splits",
    "dataset_sha256",
    "input_template",
    "synthetic_only",
    "max_length",
    "cpu_threads",
    "max_cpu_seconds",
    "max_runtime_bytes",
    "max_model_bytes",
    "paid_cost_cny",
    "accelerator_hours",
    "experiments",
}
EXPERIMENTS = ("base", "full-sft", "lora-sft", "lora-pairwise")


class TransformerValidationError(ValueError):
    """Raised when the Phase 8C contract or artifact is invalid."""


def _fail(message: str) -> None:
    raise TransformerValidationError(message)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical_bytes(value)
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"cannot read JSON file {path}: {exc.__class__.__name__}")


def _exact_keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        _fail(f"{where} keys differ: missing={missing}, unknown={unknown}")


def _relative_path(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        _fail(f"{where} must be a non-empty POSIX repository-relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        _fail(f"{where} must remain repository-relative")
    return value


def validate_model_snapshot(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _fail("model_snapshot must be an object")
    expected = {
        "schema_version",
        "repository",
        "revision",
        "license_spdx",
        "format",
        "total_bytes",
        "files",
    }
    _exact_keys(raw, expected, "model_snapshot")
    if raw["schema_version"] != SCHEMA_VERSION:
        _fail("model_snapshot.schema_version must be 1")
    if raw["repository"] != "google/bert_uncased_L-2_H-128_A-2":
        _fail("model_snapshot.repository is not the frozen model")
    if not isinstance(raw["revision"], str) or not SHA1_RE.fullmatch(raw["revision"]):
        _fail("model_snapshot.revision must be an exact commit SHA")
    if raw["license_spdx"] != "Apache-2.0" or raw["format"] != "safetensors":
        _fail("model snapshot license/format is not frozen")
    if not isinstance(raw["total_bytes"], int) or not 0 < raw["total_bytes"] <= 64 * 1024 * 1024:
        _fail("model snapshot exceeds the 64 MiB ceiling")
    files = raw["files"]
    if not isinstance(files, list) or not files:
        _fail("model_snapshot.files must be non-empty")
    paths: set[str] = set()
    total = 0
    for index, item in enumerate(files):
        where = f"model_snapshot.files[{index}]"
        if not isinstance(item, dict):
            _fail(f"{where} must be an object")
        _exact_keys(item, {"path", "bytes", "sha256"}, where)
        path = _relative_path(item["path"], f"{where}.path")
        if path in paths or path.endswith((".bin", ".pt", ".pth", ".pickle")):
            _fail(f"{where}.path is duplicated or unsafe")
        paths.add(path)
        if not isinstance(item["bytes"], int) or item["bytes"] < 1:
            _fail(f"{where}.bytes must be positive")
        if not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"]):
            _fail(f"{where}.sha256 is invalid")
        total += item["bytes"]
    if total != raw["total_bytes"] or "model.safetensors" not in paths:
        _fail("model snapshot file total or safetensors payload is inconsistent")
    return raw


def validate_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _fail("phase8c_config must be an object")
    _exact_keys(raw, CONFIG_KEYS, "phase8c_config")
    if raw["schema_version"] != SCHEMA_VERSION:
        _fail("phase8c_config.schema_version must be 1")
    if raw["experiment_family"] != "week8c-synthetic-transformer-smoke-v1":
        _fail("phase8c_config.experiment_family is not frozen")
    if not isinstance(raw["seed"], int) or isinstance(raw["seed"], bool):
        _fail("phase8c_config.seed must be an integer")
    for key in ("model_snapshot", "runtime_lock", "model_root", "data", "splits"):
        _relative_path(raw[key], f"phase8c_config.{key}")
    if raw["dataset_sha256"] != SYNTHETIC_DATASET_SHA256:
        _fail("phase8c_config.dataset_sha256 is not the frozen synthetic dataset")
    if raw["input_template"] != INPUT_TEMPLATE:
        _fail("phase8c_config.input_template is not frozen")
    if raw["synthetic_only"] is not True:
        _fail("Phase 8C remains synthetic-only until human annotation closes")
    if raw["max_length"] != 256 or raw["cpu_threads"] != 4:
        _fail("max_length/cpu_threads differ from the frozen values")
    if raw["max_cpu_seconds"] > 7200 or raw["max_runtime_bytes"] > 2 * 1024**3:
        _fail("Phase 8C time or storage ceiling expanded")
    if raw["max_model_bytes"] > 64 * 1024**2:
        _fail("Phase 8C model ceiling expanded")
    if raw["paid_cost_cny"] != 0 or raw["accelerator_hours"] != 0:
        _fail("Phase 8C paid cost and accelerator use must remain zero")
    experiments = raw["experiments"]
    if not isinstance(experiments, dict) or tuple(experiments) != EXPERIMENTS:
        _fail("phase8c_config.experiments must preserve frozen order")
    for name, experiment in experiments.items():
        if not isinstance(experiment, dict):
            _fail(f"experiment {name!r} must be an object")
        expected = {"epochs", "learning_rate"}
        if name.startswith("lora-"):
            expected |= {"rank", "alpha", "dropout"}
        _exact_keys(experiment, expected, f"experiments.{name}")
        if not isinstance(experiment["epochs"], int) or experiment["epochs"] < 0:
            _fail(f"experiments.{name}.epochs is invalid")
        rate = experiment["learning_rate"]
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate < 0:
            _fail(f"experiments.{name}.learning_rate is invalid")
        if name.startswith("lora-") and (
            experiment["rank"] != 4
            or experiment["alpha"] != 8
            or experiment["dropout"] != 0.0
        ):
            _fail(f"experiments.{name} LoRA shape differs from the frozen values")
    return raw


def load_config(path: Path) -> dict[str, Any]:
    return validate_config(_load_json(path))


def candidate_text(row: dict[str, Any]) -> str:
    evidence = " ".join(
        f"{item['kind']} {item['path']}:{item['line'] or '?'} {item['summary']}"
        for item in row["evidence"]
    )
    tools = " ".join(
        f"{item['tool']} {item['status']} {item['summary']}"
        for item in row["tool_summaries"]
    )
    return INPUT_TEMPLATE.format(
        candidate_text=row["candidate_text"], evidence=evidence, tools=tools
    ).strip()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_local_model(root: Path, snapshot: dict[str, Any]) -> None:
    for item in snapshot["files"]:
        path = root / item["path"]
        if not path.is_file() or path.stat().st_size != item["bytes"]:
            _fail(f"local model file is missing or has wrong size: {item['path']}")
        if _file_sha256(path) != item["sha256"]:
            _fail(f"local model file hash mismatch: {item['path']}")
    forbidden = [
        path for path in root.rglob("*") if path.suffix in {".bin", ".pt", ".pth", ".pickle"}
    ]
    if forbidden:
        _fail("local model root contains a forbidden pickle-capable weight file")


def _label(row: dict[str, Any]) -> int | None:
    return 1 if row["label"] == "keep" else 0 if row["label"] == "drop" else None


def _state_hash(model: Any) -> str:
    digest = hashlib.sha256()
    for name, tensor in sorted(model.state_dict().items()):
        digest.update(name.encode("utf-8"))
        digest.update(tensor.detach().cpu().contiguous().numpy().tobytes())
    return digest.hexdigest()


def _run_experiment(
    name: str,
    parameters: dict[str, Any],
    config: dict[str, Any],
    candidates: list[dict[str, Any]],
    splits: dict[str, Any],
    model_root: Path,
    run_at: str,
) -> dict[str, Any]:
    import psutil
    import peft
    import torch
    import transformers
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    runtime = {
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "peft": peft.__version__,
    }
    expected_runtime = {
        "python": "3.13.0",
        "torch": "2.13.0+cpu",
        "transformers": "5.13.0",
        "peft": "0.19.1",
    }
    if runtime != expected_runtime:
        _fail(f"runtime versions differ from the frozen lock: {runtime}")

    seed = config["seed"]
    random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(config["cpu_threads"])
    torch.use_deterministic_algorithms(True)
    started = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(model_root, local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        model_root,
        num_labels=2,
        local_files_only=True,
        use_safetensors=True,
        id2label={0: "drop", 1: "keep"},
        label2id={"drop": 0, "keep": 1},
    )
    if name.startswith("lora-"):
        model = get_peft_model(
            model,
            LoraConfig(
                task_type=TaskType.SEQ_CLS,
                r=parameters["rank"],
                lora_alpha=parameters["alpha"],
                lora_dropout=parameters["dropout"],
                target_modules=["query", "value"],
                modules_to_save=["classifier"],
                bias="none",
            ),
        )
    repositories = {
        repository: split_name
        for split_name, values in splits["splits"].items()
        for repository in values
    }
    binary = [row for row in candidates if _label(row) is not None]
    train_rows = [row for row in binary if repositories[row["repository_id"]] == "train"]

    def encode(row: dict[str, Any]) -> dict[str, Any]:
        return tokenizer(
            candidate_text(row),
            truncation=True,
            padding="max_length",
            max_length=config["max_length"],
            return_tensors="pt",
        )

    if parameters["epochs"]:
        optimizer = torch.optim.AdamW(
            [parameter for parameter in model.parameters() if parameter.requires_grad],
            lr=parameters["learning_rate"],
        )
        model.train()
        if name == "lora-pairwise":
            by_pair: dict[str, dict[int, dict[str, Any]]] = {}
            for row in train_rows:
                if row["pair_id"] is not None:
                    by_pair.setdefault(row["pair_id"], {})[int(_label(row))] = row
            pairs = [pair for pair in by_pair.values() if set(pair) == {0, 1}]
            if not pairs:
                _fail("pairwise training has no complete keep/drop pair")
            for _ in range(parameters["epochs"]):
                for pair in pairs:
                    optimizer.zero_grad(set_to_none=True)
                    keep_logits = model(**encode(pair[1])).logits
                    drop_logits = model(**encode(pair[0])).logits
                    keep_score = keep_logits[:, 1] - keep_logits[:, 0]
                    drop_score = drop_logits[:, 1] - drop_logits[:, 0]
                    loss = torch.nn.functional.softplus(-(keep_score - drop_score)).mean()
                    loss.backward()
                    optimizer.step()
        else:
            for _ in range(parameters["epochs"]):
                for row in sorted(train_rows, key=lambda item: item["candidate_id"]):
                    optimizer.zero_grad(set_to_none=True)
                    encoded = encode(row)
                    encoded["labels"] = torch.tensor([int(_label(row))])
                    loss = model(**encoded).loss
                    loss.backward()
                    optimizer.step()
    model.eval()
    experiment_id = f"week8c-{name}"
    predictions: list[dict[str, Any]] = []
    with torch.no_grad():
        for row in sorted(binary, key=lambda item: item["candidate_id"]):
            prediction_started = time.perf_counter()
            logits = model(**encode(row)).logits
            score = float(torch.softmax(logits, dim=-1)[0, 1].item())
            predictions.append(
                {
                    "schema_version": 1,
                    "experiment_id": experiment_id,
                    "candidate_id": row["candidate_id"],
                    "score": score,
                    "latency_ms": (time.perf_counter() - prediction_started) * 1000,
                }
            )
    validation_rows = [row for row in binary if repositories[row["repository_id"]] == "validation"]
    score_by_id = {row["candidate_id"]: row["score"] for row in predictions}
    threshold = vt.select_threshold(
        [(int(_label(row)), score_by_id[row["candidate_id"]]) for row in validation_rows]
    )
    test_metrics = vt.evaluate_predictions(
        candidates,
        predictions,
        splits,
        "test",
        threshold["threshold"],
        threshold_source="validation",
    )
    validation_metrics = vt.evaluate_predictions(
        candidates,
        predictions,
        splits,
        "validation",
        threshold["threshold"],
        threshold_source="validation",
    )
    elapsed = time.perf_counter() - started
    cpu_seconds_upper_bound = elapsed * config["cpu_threads"]
    if cpu_seconds_upper_bound > config["max_cpu_seconds"]:
        _fail("experiment exceeded the frozen CPU-time ceiling")
    trainable = sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)
    total = sum(parameter.numel() for parameter in model.parameters())
    report = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "run_at": run_at,
        "algorithm": name,
        "synthetic_only": True,
        "quality_claim_allowed": False,
        "seed": seed,
        "dataset_sha256": splits["dataset_sha256"],
        "config_sha256": _sha256(config),
        "input_template_sha256": _sha256(INPUT_TEMPLATE.encode("utf-8")),
        "model_state_sha256": _state_hash(model),
        "hyperparameters": parameters,
        "threshold": threshold,
        "validation": validation_metrics,
        "test": test_metrics,
        "predictions": predictions,
        "resources": {
            "device": "cpu",
            "cpu_threads": config["cpu_threads"],
            "wall_seconds": elapsed,
            "cpu_seconds_upper_bound": cpu_seconds_upper_bound,
            "rss_bytes_after": psutil.Process().memory_info().rss,
            "trainable_parameters": trainable,
            "total_parameters": total,
            "accelerator_hours": 0,
            "paid_cost_cny": 0,
        },
        "runtime": runtime,
        "manifest_sha256": "",
    }
    report["manifest_sha256"] = _sha256(
        {key: value for key, value in report.items() if key != "manifest_sha256"}
    )
    return report


def run_all(config_path: Path, output_dir: Path, run_at: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", run_at):
        _fail("run_at must be a second-precision UTC timestamp")
    root = config_path.resolve().parents[1]
    config = load_config(config_path)
    snapshot_path = root / config["model_snapshot"]
    lock_path = root / config["runtime_lock"]
    model_root = root / config["model_root"]
    snapshot = validate_model_snapshot(_load_json(snapshot_path))
    verify_local_model(model_root, snapshot)
    runtime_root = model_root.parent
    runtime_bytes = sum(path.stat().st_size for path in runtime_root.rglob("*") if path.is_file())
    if runtime_bytes > config["max_runtime_bytes"]:
        _fail("Phase 8C runtime exceeds the 2 GiB ceiling")
    candidates = vt.load_candidates(root / config["data"])
    splits = vt.load_split_manifest(root / config["splits"], candidates)
    if splits["dataset_sha256"] != config["dataset_sha256"]:
        _fail("split manifest differs from the frozen dataset hash")
    if any(row["label_source"] != "synthetic" for row in candidates):
        _fail("synthetic smoke input contains non-synthetic provenance")
    output_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    for name in EXPERIMENTS:
        report = _run_experiment(
            name,
            config["experiments"][name],
            config,
            candidates,
            splits,
            model_root,
            run_at,
        )
        (output_dir / f"{name}.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        reports.append(report)
    comparison = {
        "schema_version": 1,
        "experiment_family": config["experiment_family"],
        "run_at": run_at,
        "synthetic_only": True,
        "quality_claim_allowed": False,
        "comparable": True,
        "dataset_sha256": splits["dataset_sha256"],
        "config_sha256": _sha256(config),
        "input_template_sha256": _sha256(INPUT_TEMPLATE.encode("utf-8")),
        "model_snapshot_sha256": _sha256(snapshot),
        "runtime_lock_sha256": _file_sha256(lock_path),
        "runtime_bytes": runtime_bytes,
        "experiments": [
            {
                "experiment_id": report["experiment_id"],
                "algorithm": report["algorithm"],
                "manifest_sha256": report["manifest_sha256"],
                "threshold": report["threshold"]["threshold"],
                "test_metrics": report["test"]["micro"],
                "wall_seconds": report["resources"]["wall_seconds"],
                "trainable_parameters": report["resources"]["trainable_parameters"],
            }
            for report in reports
        ],
        "remaining_real_gates": [
            "finder_candidates_missing",
            "independent_human_annotations_missing",
            "adjudication_missing_where_required",
        ],
        "manifest_sha256": "",
    }
    comparison["manifest_sha256"] = _sha256(
        {key: value for key, value in comparison.items() if key != "manifest_sha256"}
    )
    (output_dir / "comparison.json").write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return comparison


def validate_experiment_report(raw: Any, config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _fail("experiment_report must be an object")
    expected = {
        "schema_version",
        "experiment_id",
        "run_at",
        "algorithm",
        "synthetic_only",
        "quality_claim_allowed",
        "seed",
        "dataset_sha256",
        "config_sha256",
        "input_template_sha256",
        "model_state_sha256",
        "hyperparameters",
        "threshold",
        "validation",
        "test",
        "predictions",
        "resources",
        "runtime",
        "manifest_sha256",
    }
    _exact_keys(raw, expected, "experiment_report")
    algorithm = raw["algorithm"]
    if raw["schema_version"] != 1 or algorithm not in EXPERIMENTS:
        _fail("experiment_report schema or algorithm is invalid")
    if raw["experiment_id"] != f"week8c-{algorithm}":
        _fail("experiment_report experiment_id does not match its algorithm")
    if raw["synthetic_only"] is not True or raw["quality_claim_allowed"] is not False:
        _fail("experiment_report overstates synthetic evidence")
    if raw["seed"] != config["seed"] or raw["hyperparameters"] != config["experiments"][algorithm]:
        _fail("experiment_report seed or hyperparameters differ from the config")
    if raw["dataset_sha256"] != config["dataset_sha256"]:
        _fail("experiment_report dataset differs from the config")
    if raw["config_sha256"] != _sha256(config):
        _fail("experiment_report does not bind the complete config")
    if raw["input_template_sha256"] != _sha256(INPUT_TEMPLATE.encode("utf-8")):
        _fail("experiment_report does not bind the input template")
    for key in (
        "dataset_sha256",
        "config_sha256",
        "input_template_sha256",
        "model_state_sha256",
        "manifest_sha256",
    ):
        if not isinstance(raw[key], str) or not SHA256_RE.fullmatch(raw[key]):
            _fail(f"experiment_report.{key} is invalid")
    predictions = raw["predictions"]
    if not isinstance(predictions, list) or len(predictions) != 6:
        _fail("experiment_report must contain six binary predictions")
    for index, prediction in enumerate(predictions):
        if not isinstance(prediction, dict):
            _fail(f"experiment_report.predictions[{index}] must be an object")
        score = prediction.get("score")
        latency = prediction.get("latency_ms")
        if (
            prediction.get("experiment_id") != raw["experiment_id"]
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= 1
            or isinstance(latency, bool)
            or not isinstance(latency, (int, float))
            or not math.isfinite(latency)
            or latency < 0
        ):
            _fail(f"experiment_report.predictions[{index}] is invalid")
    resources = raw["resources"]
    if (
        not isinstance(resources, dict)
        or resources.get("device") != "cpu"
        or resources.get("cpu_threads") != config["cpu_threads"]
        or resources.get("accelerator_hours") != 0
        or resources.get("paid_cost_cny") != 0
        or not 0 <= resources.get("wall_seconds", -1) <= config["max_cpu_seconds"]
        or not 0
        <= resources.get("cpu_seconds_upper_bound", -1)
        <= config["max_cpu_seconds"]
    ):
        _fail("experiment_report resources violate the frozen limits")
    if raw["runtime"] != {
        "python": "3.13.0",
        "torch": "2.13.0+cpu",
        "transformers": "5.13.0",
        "peft": "0.19.1",
    }:
        _fail("experiment_report runtime differs from the frozen lock")
    expected_hash = _sha256({key: value for key, value in raw.items() if key != "manifest_sha256"})
    if raw["manifest_sha256"] != expected_hash:
        _fail("experiment_report.manifest_sha256 does not match canonical content")
    return raw


def validate_comparison(
    raw: Any, config: dict[str, Any], reports: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _fail("comparison must be an object")
    expected = {
        "schema_version",
        "experiment_family",
        "run_at",
        "synthetic_only",
        "quality_claim_allowed",
        "comparable",
        "dataset_sha256",
        "config_sha256",
        "input_template_sha256",
        "model_snapshot_sha256",
        "runtime_lock_sha256",
        "runtime_bytes",
        "experiments",
        "remaining_real_gates",
        "manifest_sha256",
    }
    _exact_keys(raw, expected, "comparison")
    if (
        raw["schema_version"] != 1
        or raw["experiment_family"] != config["experiment_family"]
        or raw["synthetic_only"] is not True
        or raw["quality_claim_allowed"] is not False
        or raw["comparable"] is not True
    ):
        _fail("comparison evidence flags are invalid")
    if raw["runtime_bytes"] > config["max_runtime_bytes"]:
        _fail("comparison runtime bytes exceed the frozen ceiling")
    if (
        raw["dataset_sha256"] != config["dataset_sha256"]
        or raw["config_sha256"] != _sha256(config)
        or raw["input_template_sha256"] != _sha256(INPUT_TEMPLATE.encode("utf-8"))
    ):
        _fail("comparison does not bind the frozen data, config, and template")
    experiments = raw["experiments"]
    if (
        not isinstance(experiments, list)
        or len(experiments) != len(EXPERIMENTS)
        or not all(isinstance(item, dict) for item in experiments)
    ):
        _fail("comparison experiments must be a four-object list")
    if [item.get("algorithm") for item in experiments] != list(EXPERIMENTS):
        _fail("comparison experiment order is invalid")
    if [item.get("manifest_sha256") for item in experiments] != [
        report["manifest_sha256"] for report in reports
    ]:
        _fail("comparison does not bind the experiment reports")
    expected_gates = [
        "finder_candidates_missing",
        "independent_human_annotations_missing",
        "adjudication_missing_where_required",
    ]
    if raw["remaining_real_gates"] != expected_gates:
        _fail("comparison remaining real gates are not frozen")
    expected_hash = _sha256({key: value for key, value in raw.items() if key != "manifest_sha256"})
    if raw["manifest_sha256"] != expected_hash:
        _fail("comparison.manifest_sha256 does not match canonical content")
    return raw


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run bounded offline Phase 8C experiments.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--config", type=Path, required=True)
    validate.add_argument("--model-snapshot", type=Path, required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--run-at", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            config = load_config(args.config)
            snapshot = validate_model_snapshot(_load_json(args.model_snapshot))
            result = {
                "status": "ok",
                "experiment_family": config["experiment_family"],
                "model_revision": snapshot["revision"],
            }
        else:
            result = run_all(args.config, args.output_dir, args.run_at)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (TransformerValidationError, vt.ValidationError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
