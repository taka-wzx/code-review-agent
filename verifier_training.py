"""Offline data, baseline-training, and evaluation tools for Week 8.

The module intentionally uses only the Python standard library. Its lexical
models validate the experiment pipeline; they are not evidence about a trained
code model and are never loaded by the production review agent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 1
MAX_DATASET_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_BYTES = 8_000
MAX_RATIONALE_BYTES = 4_000
MAX_SUMMARY_BYTES = 2_000
MAX_EVIDENCE_ITEMS = 8
MAX_TOOL_SUMMARIES = 8
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,199}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,63}|[0-9]+")
SENSITIVE_PATTERNS = (
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*"
        r"[\"']?[^\s,\"']{8,}"
    ),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b[A-Z]:[\\/]Users[\\/][^\\/\s]+"),
    re.compile(r"/(?:home|Users)/[^/\s]+/"),
)

CANDIDATE_KEYS = {
    "schema_version",
    "candidate_id",
    "repository_id",
    "change_id",
    "source_revision",
    "candidate_text",
    "evidence",
    "tool_summaries",
    "label",
    "label_source",
    "rationale",
    "pair_id",
    "language",
    "severity",
    "content_sha256",
    "record_sha256",
}
EVIDENCE_KEYS = {"kind", "path", "line", "summary"}
TOOL_KEYS = {"tool", "status", "summary"}
SPLIT_KEYS = {
    "schema_version",
    "dataset_sha256",
    "splits",
    "operating_threshold",
    "threshold_source",
}
PREDICTION_KEYS = {
    "schema_version",
    "experiment_id",
    "candidate_id",
    "score",
    "latency_ms",
}


class ValidationError(ValueError):
    """Raised when an input violates the frozen Week 8 protocol."""


def _fail(message: str) -> None:
    raise ValidationError(message)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def candidate_content_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Return the label-independent payload used for duplicate detection."""

    return {
        "candidate_text": row["candidate_text"],
        "evidence": row["evidence"],
        "tool_summaries": row["tool_summaries"],
    }


def candidate_record_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Return every source field except the self-referential record hash."""

    return {key: row[key] for key in sorted(CANDIDATE_KEYS - {"record_sha256"})}


def with_candidate_hashes(row: dict[str, Any]) -> dict[str, Any]:
    """Populate canonical hashes for a newly constructed candidate row."""

    result = dict(row)
    result["content_sha256"] = _sha256(candidate_content_payload(result))
    result["record_sha256"] = _sha256(candidate_record_payload(result))
    return result


def dataset_sha256(rows: Sequence[dict[str, Any]]) -> str:
    """Hash the exact record set independent of JSONL ordering."""

    return _sha256(sorted(row["record_sha256"] for row in rows))


def _expect_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{where} must be an object")
    return value


def _expect_exact_keys(row: dict[str, Any], keys: set[str], where: str) -> None:
    unknown = sorted(set(row) - keys)
    missing = sorted(keys - set(row))
    if unknown or missing:
        _fail(f"{where} keys differ: missing={missing}, unknown={unknown}")


def _expect_identifier(value: Any, where: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        _fail(f"{where} must be a stable identifier")
    return value


def _expect_hash(value: Any, where: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail(f"{where} has an invalid hash")
    return value


def _expect_text(
    value: Any,
    where: str,
    *,
    max_bytes: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _fail(f"{where} must be non-empty text")
    if "\x00" in value or len(value.encode("utf-8")) > max_bytes:
        _fail(f"{where} exceeds its safe text boundary")
    for pattern in SENSITIVE_PATTERNS:
        if pattern.search(value):
            _fail(f"{where} contains credential-like or host-path content")
    return value


def _expect_relative_path(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        _fail(f"{where} must be a POSIX repository-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _fail(f"{where} must be a safe repository-relative path")
    return value


def _validate_evidence(value: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE_ITEMS:
        _fail(f"{where} must contain at most {MAX_EVIDENCE_ITEMS} items")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item_where = f"{where}[{index}]"
        item = _expect_dict(raw, item_where)
        _expect_exact_keys(item, EVIDENCE_KEYS, item_where)
        if item["kind"] not in {"positive", "negative", "missing"}:
            _fail(f"{item_where}.kind is invalid")
        _expect_relative_path(item["path"], f"{item_where}.path")
        line = item["line"]
        if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
            _fail(f"{item_where}.line must be null or a positive integer")
        _expect_text(item["summary"], f"{item_where}.summary", max_bytes=MAX_SUMMARY_BYTES)
        result.append(item)
    return result


def _validate_tool_summaries(value: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_TOOL_SUMMARIES:
        _fail(f"{where} must contain at most {MAX_TOOL_SUMMARIES} items")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item_where = f"{where}[{index}]"
        item = _expect_dict(raw, item_where)
        _expect_exact_keys(item, TOOL_KEYS, item_where)
        _expect_identifier(item["tool"], f"{item_where}.tool")
        if item["status"] not in {"ok", "error", "not_run"}:
            _fail(f"{item_where}.status is invalid")
        _expect_text(item["summary"], f"{item_where}.summary", max_bytes=MAX_SUMMARY_BYTES)
        result.append(item)
    return result


def validate_candidate_row(raw: Any, index: int = 0) -> dict[str, Any]:
    where = f"candidates[{index}]"
    row = _expect_dict(raw, where)
    _expect_exact_keys(row, CANDIDATE_KEYS, where)
    if row["schema_version"] != SCHEMA_VERSION:
        _fail(f"{where}.schema_version must be {SCHEMA_VERSION}")
    for key in ("candidate_id", "repository_id", "change_id"):
        _expect_identifier(row[key], f"{where}.{key}")
    _expect_hash(row["source_revision"], f"{where}.source_revision", SHA1_RE)
    _expect_text(
        row["candidate_text"], f"{where}.candidate_text", max_bytes=MAX_CANDIDATE_BYTES
    )
    _validate_evidence(row["evidence"], f"{where}.evidence")
    _validate_tool_summaries(row["tool_summaries"], f"{where}.tool_summaries")
    if row["label"] not in {"keep", "drop", "uncertain"}:
        _fail(f"{where}.label is invalid")
    if row["label_source"] not in {"human_adjudicated", "human_single", "synthetic"}:
        _fail(f"{where}.label_source is invalid")
    _expect_text(row["rationale"], f"{where}.rationale", max_bytes=MAX_RATIONALE_BYTES)
    _expect_identifier(row["pair_id"], f"{where}.pair_id", nullable=True)
    _expect_identifier(row["language"], f"{where}.language", nullable=True)
    if row["severity"] not in {None, "low", "medium", "high"}:
        _fail(f"{where}.severity is invalid")
    _expect_hash(row["content_sha256"], f"{where}.content_sha256", SHA256_RE)
    _expect_hash(row["record_sha256"], f"{where}.record_sha256", SHA256_RE)
    expected_content = _sha256(candidate_content_payload(row))
    if row["content_sha256"] != expected_content:
        _fail(f"{where}.content_sha256 does not match canonical content")
    expected_record = _sha256(candidate_record_payload(row))
    if row["record_sha256"] != expected_record:
        _fail(f"{where}.record_sha256 does not match the source record")
    return row


def _load_json(path: Path) -> Any:
    if not path.is_file():
        _fail(f"missing input file: {path}")
    if path.stat().st_size > MAX_DATASET_BYTES:
        _fail(f"input file exceeds {MAX_DATASET_BYTES} bytes: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"cannot parse JSON file {path}: {exc.__class__.__name__}")


def _load_jsonl(path: Path) -> list[Any]:
    if not path.is_file():
        _fail(f"missing input file: {path}")
    if path.stat().st_size > MAX_DATASET_BYTES:
        _fail(f"input file exceeds {MAX_DATASET_BYTES} bytes: {path}")
    rows: list[Any] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    _fail(f"invalid JSONL at {path}:{line_number}")
    except (OSError, UnicodeError) as exc:
        _fail(f"cannot read JSONL file {path}: {exc.__class__.__name__}")
    return rows


def load_candidates(path: Path) -> list[dict[str, Any]]:
    rows = [validate_candidate_row(raw, index) for index, raw in enumerate(_load_jsonl(path))]
    if not rows:
        _fail("candidate dataset is empty")
    for key in ("candidate_id", "record_sha256"):
        values = [row[key] for row in rows]
        if len(values) != len(set(values)):
            _fail(f"candidate dataset has duplicate {key}")
    return rows


def validate_split_manifest(raw: Any, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    manifest = _expect_dict(raw, "split_manifest")
    _expect_exact_keys(manifest, SPLIT_KEYS, "split_manifest")
    if manifest["schema_version"] != SCHEMA_VERSION:
        _fail(f"split_manifest.schema_version must be {SCHEMA_VERSION}")
    _expect_hash(manifest["dataset_sha256"], "split_manifest.dataset_sha256", SHA256_RE)
    expected_dataset_hash = dataset_sha256(rows)
    if manifest["dataset_sha256"] != expected_dataset_hash:
        _fail("split_manifest.dataset_sha256 does not match candidate records")
    splits = _expect_dict(manifest["splits"], "split_manifest.splits")
    _expect_exact_keys(splits, {"train", "validation", "test"}, "split_manifest.splits")
    seen_repositories: dict[str, str] = {}
    for split_name in ("train", "validation", "test"):
        repositories = splits[split_name]
        if not isinstance(repositories, list) or not repositories:
            _fail(f"split_manifest.splits.{split_name} must be a non-empty list")
        if len(repositories) != len(set(repositories)):
            _fail(f"split_manifest.splits.{split_name} contains duplicates")
        for index, repository in enumerate(repositories):
            _expect_identifier(repository, f"split_manifest.splits.{split_name}[{index}]")
            previous = seen_repositories.setdefault(repository, split_name)
            if previous != split_name:
                _fail(f"repository {repository!r} occurs in both {previous} and {split_name}")
    dataset_repositories = {row["repository_id"] for row in rows}
    if set(seen_repositories) != dataset_repositories:
        missing = sorted(dataset_repositories - set(seen_repositories))
        unknown = sorted(set(seen_repositories) - dataset_repositories)
        _fail(f"split repositories differ from data: missing={missing}, unknown={unknown}")
    threshold = manifest["operating_threshold"]
    if threshold is not None:
        _expect_score(threshold, "split_manifest.operating_threshold")
    if manifest["threshold_source"] not in {"validation", "diagnostic", "unfrozen"}:
        _fail("split_manifest.threshold_source is invalid")
    if threshold is None and manifest["threshold_source"] != "unfrozen":
        _fail("a null operating threshold must use threshold_source='unfrozen'")
    if threshold is not None and manifest["threshold_source"] == "unfrozen":
        _fail("a populated operating threshold cannot use threshold_source='unfrozen'")
    _audit_cross_split_leakage(rows, seen_repositories)
    return manifest


def load_split_manifest(path: Path, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return validate_split_manifest(_load_json(path), rows)


def _audit_cross_split_leakage(
    rows: Sequence[dict[str, Any]], repository_split: dict[str, str]
) -> None:
    keys = ("candidate_id", "change_id", "pair_id", "content_sha256", "record_sha256")
    seen: dict[str, dict[str, str]] = {key: {} for key in keys}
    for row in rows:
        split_name = repository_split[row["repository_id"]]
        for key in keys:
            value = row[key]
            if value is None:
                continue
            previous = seen[key].setdefault(value, split_name)
            if previous != split_name:
                _fail(f"cross-split leakage: {key}={value!r} occurs in {previous} and {split_name}")


def _expect_score(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{where} must be a number in [0, 1]")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        _fail(f"{where} must be finite and in [0, 1]")
    return score


def load_predictions(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(_load_jsonl(path)):
        where = f"predictions[{index}]"
        row = _expect_dict(raw, where)
        _expect_exact_keys(row, PREDICTION_KEYS, where)
        if row["schema_version"] != SCHEMA_VERSION:
            _fail(f"{where}.schema_version must be {SCHEMA_VERSION}")
        _expect_identifier(row["experiment_id"], f"{where}.experiment_id")
        candidate_id = _expect_identifier(row["candidate_id"], f"{where}.candidate_id")
        if candidate_id in seen_ids:
            _fail(f"duplicate prediction for candidate {candidate_id!r}")
        seen_ids.add(candidate_id)
        row = dict(row)
        row["score"] = _expect_score(row["score"], f"{where}.score")
        latency = row["latency_ms"]
        if isinstance(latency, bool) or not isinstance(latency, (int, float)):
            _fail(f"{where}.latency_ms must be a non-negative finite number")
        latency = float(latency)
        if not math.isfinite(latency) or latency < 0:
            _fail(f"{where}.latency_ms must be a non-negative finite number")
        row["latency_ms"] = latency
        rows.append(row)
    if not rows:
        _fail("prediction dataset is empty")
    experiment_ids = {row["experiment_id"] for row in rows}
    if len(experiment_ids) != 1:
        _fail("prediction file must contain exactly one experiment_id")
    return rows


def _safe_div(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 8)


def confusion_metrics(
    labeled_scores: Sequence[tuple[int, float]], threshold: float
) -> dict[str, Any]:
    tp = fp = tn = fn = 0
    for label, score in labeled_scores:
        predicted = score >= threshold
        if label == 1 and predicted:
            tp += 1
        elif label == 0 and predicted:
            fp += 1
        elif label == 0:
            tn += 1
        else:
            fn += 1
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    f1 = None
    if precision is not None and recall is not None and precision + recall:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": _round(precision),
        "recall": _round(recall),
        "f1": _round(f1),
    }


def precision_recall_curve(labeled_scores: Sequence[tuple[int, float]]) -> dict[str, Any]:
    positives = sum(label for label, _ in labeled_scores)
    if not labeled_scores or positives == 0:
        return {"points": [], "average_precision": None}
    points: list[dict[str, Any]] = []
    previous_recall = 0.0
    average_precision = 0.0
    for threshold in sorted({score for _, score in labeled_scores}, reverse=True):
        metrics = confusion_metrics(labeled_scores, threshold)
        recall = metrics["recall"]
        precision = metrics["precision"]
        points.append(
            {
                "threshold": round(threshold, 8),
                "precision": precision,
                "recall": recall,
            }
        )
        if recall is not None and precision is not None and recall > previous_recall:
            average_precision += (recall - previous_recall) * precision
            previous_recall = recall
    return {"points": points, "average_precision": round(average_precision, 8)}


def calibration_report(
    labeled_scores: Sequence[tuple[int, float]], bins: int = 10
) -> dict[str, Any]:
    if isinstance(bins, bool) or not isinstance(bins, int) or not 2 <= bins <= 100:
        _fail("ECE bins must be an integer in [2, 100]")
    buckets: list[list[tuple[int, float]]] = [[] for _ in range(bins)]
    for label, score in labeled_scores:
        bucket = min(int(score * bins), bins - 1)
        buckets[bucket].append((label, score))
    total = len(labeled_scores)
    ece = 0.0
    output_bins: list[dict[str, Any]] = []
    for index, bucket in enumerate(buckets):
        count = len(bucket)
        mean_confidence = _safe_div(sum(score for _, score in bucket), count)
        keep_rate = _safe_div(sum(label for label, _ in bucket), count)
        if count and mean_confidence is not None and keep_rate is not None and total:
            ece += (count / total) * abs(mean_confidence - keep_rate)
        output_bins.append(
            {
                "lower": round(index / bins, 8),
                "upper": round((index + 1) / bins, 8),
                "count": count,
                "mean_confidence": _round(mean_confidence),
                "keep_rate": _round(keep_rate),
            }
        )
    return {"ece": round(ece, 8) if total else None, "bins": output_bins}


def select_threshold(labeled_scores: Sequence[tuple[int, float]]) -> dict[str, Any]:
    if not labeled_scores:
        _fail("cannot select a threshold without binary validation labels")
    if {label for label, _ in labeled_scores} != {0, 1}:
        _fail("threshold selection requires both keep and drop validation labels")
    candidates = sorted({score for _, score in labeled_scores}, reverse=True)
    ranked: list[tuple[tuple[float, float, float, float], float, dict[str, Any]]] = []
    for threshold in candidates:
        metrics = confusion_metrics(labeled_scores, threshold)
        rank = (
            -1.0 if metrics["f1"] is None else metrics["f1"],
            -1.0 if metrics["recall"] is None else metrics["recall"],
            -1.0 if metrics["precision"] is None else metrics["precision"],
            threshold,
        )
        ranked.append((rank, threshold, metrics))
    _, threshold, metrics = max(ranked, key=lambda item: item[0])
    return {"threshold": round(threshold, 8), "validation_metrics": metrics}


def _label_value(row: dict[str, Any]) -> int | None:
    if row["label"] == "keep":
        return 1
    if row["label"] == "drop":
        return 0
    return None


def _repository_split_map(manifest: dict[str, Any]) -> dict[str, str]:
    return {
        repository: split_name
        for split_name, repositories in manifest["splits"].items()
        for repository in repositories
    }


def evaluate_predictions(
    candidates: Sequence[dict[str, Any]],
    predictions: Sequence[dict[str, Any]],
    manifest: dict[str, Any],
    split_name: str,
    threshold: float,
    *,
    bins: int = 10,
    threshold_source: str = "manifest",
) -> dict[str, Any]:
    if split_name not in {"train", "validation", "test"}:
        _fail(f"invalid split {split_name!r}")
    prediction_by_id = {row["candidate_id"]: row for row in predictions}
    known_ids = {row["candidate_id"] for row in candidates}
    unknown_predictions = sorted(set(prediction_by_id) - known_ids)
    if unknown_predictions:
        _fail(f"predictions reference unknown candidates: {unknown_predictions}")
    repositories = set(manifest["splits"][split_name])
    selected = [row for row in candidates if row["repository_id"] in repositories]
    binary = [row for row in selected if _label_value(row) is not None]
    missing = sorted(row["candidate_id"] for row in binary if row["candidate_id"] not in prediction_by_id)
    if missing:
        _fail(f"missing predictions for binary candidates: {missing}")
    labeled_scores = [
        (_label_value(row), prediction_by_id[row["candidate_id"]]["score"]) for row in binary
    ]
    typed_scores = [(int(label), score) for label, score in labeled_scores if label is not None]
    per_repository: dict[str, Any] = {}
    repository_metric_rows: list[dict[str, Any]] = []
    for repository in sorted(repositories):
        repository_rows = [row for row in binary if row["repository_id"] == repository]
        repository_scores = [
            (int(_label_value(row)), prediction_by_id[row["candidate_id"]]["score"])
            for row in repository_rows
        ]
        metrics = confusion_metrics(repository_scores, threshold)
        metrics["support"] = len(repository_scores)
        per_repository[repository] = metrics
        repository_metric_rows.append(metrics)
    macro: dict[str, Any] = {}
    for key in ("precision", "recall", "f1"):
        values = [row[key] for row in repository_metric_rows if row[key] is not None]
        macro[key] = _round(_safe_div(sum(values), len(values)))
    latencies = [
        prediction_by_id[row["candidate_id"]]["latency_ms"]
        for row in selected
        if row["candidate_id"] in prediction_by_id
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": predictions[0]["experiment_id"],
        "dataset_sha256": manifest["dataset_sha256"],
        "split": split_name,
        "repositories": sorted(repositories),
        "threshold": round(threshold, 8),
        "threshold_source": threshold_source,
        "support": {
            "total": len(selected),
            "binary": len(binary),
            "uncertain_excluded": sum(row["label"] == "uncertain" for row in selected),
        },
        "micro": confusion_metrics(typed_scores, threshold),
        "macro": macro,
        "pr_curve": precision_recall_curve(typed_scores),
        "calibration": calibration_report(typed_scores, bins),
        "per_repository": per_repository,
        "latency_ms": {
            "count": len(latencies),
            "mean": _round(_safe_div(sum(latencies), len(latencies))),
            "max": _round(max(latencies) if latencies else None),
        },
        "errors": _error_rows(binary, prediction_by_id, threshold),
    }


def _error_rows(
    rows: Sequence[dict[str, Any]],
    prediction_by_id: dict[str, dict[str, Any]],
    threshold: float,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for row in rows:
        score = prediction_by_id[row["candidate_id"]]["score"]
        predicted_keep = score >= threshold
        actual_keep = row["label"] == "keep"
        if predicted_keep == actual_keep:
            continue
        errors.append(
            {
                "candidate_id": row["candidate_id"],
                "repository_id": row["repository_id"],
                "kind": "false_positive" if predicted_keep else "false_negative",
                "score": round(score, 8),
                "severity": row["severity"],
                "language": row["language"],
                "evidence_missing": any(item["kind"] == "missing" for item in row["evidence"]),
                "tool_error": any(item["status"] == "error" for item in row["tool_summaries"]),
            }
        )
    return errors


def _tokens(row: dict[str, Any]) -> list[str]:
    texts = [row["candidate_text"]]
    texts.extend(item["summary"] for item in row["evidence"])
    texts.extend(item["summary"] for item in row["tool_summaries"])
    return [token.lower() for token in TOKEN_RE.findall(" ".join(texts))]


def _features(row: dict[str, Any], dimensions: int) -> dict[int, float]:
    counts = Counter(_tokens(row))
    total = sum(counts.values()) or 1
    features: dict[int, float] = defaultdict(float)
    for token, count in counts.items():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % dimensions
        sign = 1.0 if digest[8] & 1 else -1.0
        features[index] += sign * count / total
    return dict(features)


def _dot(weights: Sequence[float], features: dict[int, float]) -> float:
    return sum(weights[index] * value for index, value in features.items())


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + inverse)
    exponential = math.exp(max(value, -60.0))
    return exponential / (1.0 + exponential)


def train_lexical_baseline(
    rows: Sequence[dict[str, Any]],
    *,
    algorithm: str,
    dimensions: int = 256,
    epochs: int = 25,
    learning_rate: float = 0.4,
    seed: int = 17,
) -> dict[str, Any]:
    if algorithm not in {"logreg", "pairwise"}:
        _fail(f"unsupported baseline algorithm {algorithm!r}")
    if not 16 <= dimensions <= 16_384:
        _fail("feature dimensions must be in [16, 16384]")
    if not 1 <= epochs <= 10_000:
        _fail("epochs must be in [1, 10000]")
    if not math.isfinite(learning_rate) or not 0 < learning_rate <= 10:
        _fail("learning rate must be finite and in (0, 10]")
    weights = [0.0] * dimensions
    bias = 0.0
    rng = random.Random(seed)
    if algorithm == "logreg":
        examples = [(row, _label_value(row)) for row in rows if _label_value(row) is not None]
        if not examples or len({label for _, label in examples}) < 2:
            _fail("logreg baseline requires both keep and drop training labels")
        for _ in range(epochs):
            epoch_rows = list(examples)
            rng.shuffle(epoch_rows)
            for row, label in epoch_rows:
                features = _features(row, dimensions)
                probability = _sigmoid(_dot(weights, features) + bias)
                gradient = int(label) - probability
                for index, value in features.items():
                    weights[index] += learning_rate * gradient * value
                bias += learning_rate * gradient
    else:
        grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for row in rows:
            if row["pair_id"] is not None and row["label"] in {"keep", "drop"}:
                if row["label"] in grouped[row["pair_id"]]:
                    _fail(f"pair {row['pair_id']!r} has duplicate {row['label']} examples")
                grouped[row["pair_id"]][row["label"]] = row
        pairs = [
            (group["keep"], group["drop"])
            for group in grouped.values()
            if set(group) == {"keep", "drop"}
        ]
        if not pairs:
            _fail("pairwise baseline requires at least one complete keep/drop pair")
        for _ in range(epochs):
            epoch_pairs = list(pairs)
            rng.shuffle(epoch_pairs)
            for keep_row, drop_row in epoch_pairs:
                keep_features = _features(keep_row, dimensions)
                drop_features = _features(drop_row, dimensions)
                difference: dict[int, float] = defaultdict(float)
                for index, value in keep_features.items():
                    difference[index] += value
                for index, value in drop_features.items():
                    difference[index] -= value
                probability = _sigmoid(_dot(weights, difference))
                gradient = 1.0 - probability
                for index, value in difference.items():
                    weights[index] += learning_rate * gradient * value
    return {
        "schema_version": SCHEMA_VERSION,
        "model_kind": "pipeline_lexical_baseline",
        "algorithm": algorithm,
        "dimensions": dimensions,
        "epochs": epochs,
        "learning_rate": learning_rate,
        "seed": seed,
        "weights": [round(weight, 12) for weight in weights],
        "bias": round(bias, 12),
    }


def predict_lexical_baseline(
    model: dict[str, Any], rows: Sequence[dict[str, Any]], experiment_id: str
) -> list[dict[str, Any]]:
    _expect_identifier(experiment_id, "experiment_id")
    weights = model["weights"]
    dimensions = model["dimensions"]
    predictions: list[dict[str, Any]] = []
    for row in rows:
        started = time.perf_counter()
        score = _sigmoid(_dot(weights, _features(row, dimensions)) + model["bias"])
        latency_ms = (time.perf_counter() - started) * 1000
        predictions.append(
            {
                "schema_version": SCHEMA_VERSION,
                "experiment_id": experiment_id,
                "candidate_id": row["candidate_id"],
                "score": round(score, 12),
                "latency_ms": round(latency_ms, 6),
            }
        )
    return predictions


def _rows_for_split(
    rows: Sequence[dict[str, Any]], manifest: dict[str, Any], split_name: str
) -> list[dict[str, Any]]:
    repositories = set(manifest["splits"][split_name])
    return [row for row in rows if row["repository_id"] in repositories]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def _command_validate(args: argparse.Namespace) -> dict[str, Any]:
    candidates = load_candidates(args.data)
    manifest = load_split_manifest(args.splits, candidates)
    repository_split = _repository_split_map(manifest)
    labels = Counter(row["label"] for row in candidates)
    return {
        "status": "ok",
        "dataset_sha256": manifest["dataset_sha256"],
        "records": len(candidates),
        "repositories": len(repository_split),
        "labels": dict(sorted(labels.items())),
        "splits": {
            split_name: sum(repository_split[row["repository_id"]] == split_name for row in candidates)
            for split_name in ("train", "validation", "test")
        },
    }


def _command_evaluate(args: argparse.Namespace) -> dict[str, Any]:
    candidates = load_candidates(args.data)
    manifest = load_split_manifest(args.splits, candidates)
    predictions = load_predictions(args.predictions)
    if args.threshold is not None:
        threshold = _expect_score(args.threshold, "--threshold")
        threshold_source = "cli_explicit"
    elif args.split == "validation":
        prediction_by_id = {row["candidate_id"]: row for row in predictions}
        validation_rows = _rows_for_split(candidates, manifest, "validation")
        missing = sorted(
            row["candidate_id"]
            for row in validation_rows
            if _label_value(row) is not None and row["candidate_id"] not in prediction_by_id
        )
        if missing:
            _fail(f"missing predictions for binary validation candidates: {missing}")
        validation_scores = [
            (_label_value(row), prediction_by_id[row["candidate_id"]]["score"])
            for row in validation_rows
            if _label_value(row) is not None
        ]
        selection = select_threshold(
            [(int(label), score) for label, score in validation_scores if label is not None]
        )
        threshold = selection["threshold"]
        threshold_source = "validation_selected_diagnostic"
    else:
        threshold = manifest["operating_threshold"]
        if threshold is None:
            _fail("test evaluation requires a frozen manifest threshold or explicit --threshold")
        threshold_source = f"manifest_{manifest['threshold_source']}"
    return evaluate_predictions(
        candidates,
        predictions,
        manifest,
        args.split,
        threshold,
        bins=args.ece_bins,
        threshold_source=threshold_source,
    )


def _command_train(args: argparse.Namespace) -> dict[str, Any]:
    candidates = load_candidates(args.data)
    manifest = load_split_manifest(args.splits, candidates)
    train_rows = _rows_for_split(candidates, manifest, "train")
    validation_rows = _rows_for_split(candidates, manifest, "validation")
    model = train_lexical_baseline(
        train_rows,
        algorithm=args.algorithm,
        dimensions=args.dimensions,
        epochs=args.epochs,
        learning_rate=args.learning_rate,
        seed=args.seed,
    )
    model["training_dataset_sha256"] = manifest["dataset_sha256"]
    model["train_repositories"] = sorted(manifest["splits"]["train"])
    validation_predictions = predict_lexical_baseline(model, validation_rows, args.experiment_id)
    validation_by_id = {row["candidate_id"]: row for row in validation_predictions}
    validation_scores = [
        (int(_label_value(row)), validation_by_id[row["candidate_id"]]["score"])
        for row in validation_rows
        if _label_value(row) is not None
    ]
    model["threshold_selection"] = select_threshold(validation_scores)
    prediction_rows = _rows_for_split(candidates, manifest, args.predict_split)
    predictions = predict_lexical_baseline(model, prediction_rows, args.experiment_id)
    _write_json(args.model_out, model)
    _write_jsonl(args.predictions_out, predictions)
    return {
        "status": "ok",
        "model_kind": model["model_kind"],
        "algorithm": model["algorithm"],
        "records_trained": sum(_label_value(row) is not None for row in train_rows),
        "validation_threshold": model["threshold_selection"]["threshold"],
        "predictions_written": len(predictions),
        "predict_split": args.predict_split,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate, train pipeline baselines, and evaluate Week 8 verifier data."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="validate candidates and splits")
    validate_parser.add_argument("--data", type=Path, required=True)
    validate_parser.add_argument("--splits", type=Path, required=True)
    validate_parser.set_defaults(handler=_command_validate)

    evaluate_parser = subparsers.add_parser("evaluate", help="evaluate frozen predictions")
    evaluate_parser.add_argument("--data", type=Path, required=True)
    evaluate_parser.add_argument("--splits", type=Path, required=True)
    evaluate_parser.add_argument("--predictions", type=Path, required=True)
    evaluate_parser.add_argument("--split", choices=("train", "validation", "test"), required=True)
    evaluate_parser.add_argument("--threshold", type=float)
    evaluate_parser.add_argument("--ece-bins", type=int, default=10)
    evaluate_parser.add_argument("--out", type=Path)
    evaluate_parser.set_defaults(handler=_command_evaluate)

    train_parser = subparsers.add_parser(
        "train", help="train a dependency-free pipeline baseline (not model evidence)"
    )
    train_parser.add_argument("--data", type=Path, required=True)
    train_parser.add_argument("--splits", type=Path, required=True)
    train_parser.add_argument("--algorithm", choices=("logreg", "pairwise"), required=True)
    train_parser.add_argument("--experiment-id", required=True)
    train_parser.add_argument("--dimensions", type=int, default=256)
    train_parser.add_argument("--epochs", type=int, default=25)
    train_parser.add_argument("--learning-rate", type=float, default=0.4)
    train_parser.add_argument("--seed", type=int, default=17)
    train_parser.add_argument(
        "--predict-split", choices=("train", "validation", "test"), default="test"
    )
    train_parser.add_argument("--model-out", type=Path, required=True)
    train_parser.add_argument("--predictions-out", type=Path, required=True)
    train_parser.set_defaults(handler=_command_train)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        output = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if getattr(args, "out", None):
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0
    except ValidationError as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
