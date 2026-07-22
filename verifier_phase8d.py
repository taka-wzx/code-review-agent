"""Phase 8D real Verifier evidence preparation and evidence validation.

Provider and raw-diff I/O live in the separately bounded GLM executor.  This
module validates its authority and receipts, creates blinded human packets,
imports decisions, and binds a real corpus freeze.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

import verifier_corpus as vc


SCHEMA_VERSION = 1
PHASE_ID = "week8d-real-verifier-evidence-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,199}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
CONFIG_KEYS = {
    "schema_version",
    "phase_id",
    "offline_preparation_only",
    "authorization",
    "finder",
    "retention",
    "humans",
    "limits",
    "real_model_plan",
}
FINDER_RUN_KEYS = {
    "schema_version",
    "run_id",
    "queue_id",
    "queue_sha256",
    "source_id",
    "pr_source_sha256",
    "diff_sha256",
    "status",
    "candidate_ids",
    "candidate_count",
    "provider",
    "model",
    "prompt_sha256",
    "started_at",
    "finished_at",
    "input_tokens",
    "output_tokens",
    "cost_cny",
    "trace_sha256",
    "error_category",
    "synthetic",
    "run_sha256",
}
PACKET_KEYS = {
    "schema_version",
    "packet_id",
    "mode",
    "reviewer_id",
    "created_at",
    "rubric_sha256",
    "candidate_set_sha256",
    "order_seed",
    "synthetic",
    "items",
    "packet_sha256",
}
PACKET_ITEM_KEYS = {
    "candidate_id",
    "candidate_source_sha256",
    "evidence_sha256",
    "repository_id",
    "source_id",
    "source_revision",
    "candidate_text",
    "evidence",
    "tool_summaries",
    "language",
    "severity",
    "source_annotations",
}
SOURCE_ANNOTATION_KEYS = {
    "annotation_id",
    "annotation_sha256",
    "annotator_id",
    "label",
    "rationale",
}
RESPONSE_KEYS = {"candidate_id", "label", "rationale", "created_at"}


class Phase8DValidationError(ValueError):
    """Raised when a Phase 8D artifact expands authority or loses provenance."""


def _fail(message: str) -> None:
    raise Phase8DValidationError(message)


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


def _without_hash(row: dict[str, Any], hash_key: str) -> dict[str, Any]:
    return {key: row[key] for key in sorted(set(row) - {hash_key})}


def _with_hash(row: dict[str, Any], hash_key: str) -> dict[str, Any]:
    result = dict(row)
    result[hash_key] = _sha256(_without_hash(result, hash_key))
    return result


def _expect_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{where} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        _fail(f"{where} keys differ: missing={missing}, unknown={unknown}")


def _identifier(value: Any, where: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        _fail(f"{where} is not a valid identifier")
    return value


def _sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        _fail(f"{where} is not a SHA-256 digest")
    return value


def _timestamp(value: Any, where: str) -> str:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        _fail(f"{where} must be a second-precision UTC timestamp")
    return value


def _bounded_text(value: Any, where: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{where} must be non-empty text")
    if len(value.encode("utf-8")) > maximum:
        _fail(f"{where} exceeds {maximum} UTF-8 bytes")
    return value


def _non_negative_integer(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{where} must be a non-negative integer")
    return value


def _non_negative_number(value: Any, where: str) -> float | int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        _fail(f"{where} must be a non-negative number")
    return value


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"cannot read JSON file {path}: {exc.__class__.__name__}")


def _load_jsonl(path: Path) -> list[Any]:
    rows: list[Any] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    _fail(f"{path}:{line_number} is blank")
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    _fail(f"{path}:{line_number} is not valid JSON")
    except (OSError, UnicodeError) as exc:
        _fail(f"cannot read JSONL file {path}: {exc.__class__.__name__}")
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def validate_config(raw: Any) -> dict[str, Any]:
    config = _expect_dict(raw, "phase8d_config")
    _exact_keys(config, CONFIG_KEYS, "phase8d_config")
    if config["schema_version"] != SCHEMA_VERSION or config["phase_id"] != PHASE_ID:
        _fail("phase8d_config schema or phase ID is not frozen")
    if config["offline_preparation_only"] is not False:
        _fail("Phase 8D GLM amendment must enable the separately bounded executor")

    authorization = _expect_dict(config["authorization"], "authorization")
    _exact_keys(
        authorization,
        {"provider_calls", "raw_diff_read", "real_model_training", "local_commit"},
        "authorization",
    )
    if authorization != {
        "provider_calls": True,
        "raw_diff_read": True,
        "real_model_training": False,
        "local_commit": True,
    }:
        _fail("Phase 8D authorization differs from the GLM amendment")

    finder = _expect_dict(config["finder"], "finder")
    _exact_keys(
        finder,
        {
            "provider",
            "base_url",
            "model",
            "model_alias_mutable",
            "prompt_sha256",
            "anchor_temperature",
            "sampling_temperature",
            "thinking_type",
            "reasoning_effort",
            "stream",
            "tool_choice",
            "max_calls",
            "max_http_attempts",
            "max_input_tokens",
            "max_output_tokens",
            "max_cost_cny",
            "uncached_input_cny_per_million",
            "cached_input_cny_per_million",
            "output_cny_per_million",
        },
        "finder",
    )
    expected_finder = {
        "provider": "glm",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.2",
        "model_alias_mutable": True,
        "anchor_temperature": 0.2,
        "sampling_temperature": 0.7,
        "thinking_type": "disabled",
        "reasoning_effort": "none",
        "stream": False,
        "tool_choice": "auto",
        "max_calls": 580,
        "max_http_attempts": 1740,
        "max_input_tokens": 20_000_000,
        "max_output_tokens": 2_000_000,
        "max_cost_cny": 250,
        "uncached_input_cny_per_million": 8,
        "cached_input_cny_per_million": 2,
        "output_cny_per_million": 28,
    }
    for key, expected in expected_finder.items():
        if finder[key] != expected:
            _fail(f"finder.{key} differs from the authorized GLM value")
    _sha(finder["prompt_sha256"], "finder.prompt_sha256")

    retention = _expect_dict(config["retention"], "retention")
    _exact_keys(retention, {"raw_diff_days", "raw_trace_days"}, "retention")
    if retention != {"raw_diff_days": 30, "raw_trace_days": 30}:
        _fail("raw diff/trace retention must remain exactly 30 days")

    humans = _expect_dict(config["humans"], "humans")
    _exact_keys(humans, {"annotator_a", "annotator_b", "adjudicator"}, "humans")
    if humans != {
        "annotator_a": "human-reviewer-a-v1",
        "annotator_b": "human-reviewer-b-v1",
        "adjudicator": "human-adjudicator-c-v1",
    }:
        _fail("human IDs differ from the authorized three-person assignment")
    if len(set(humans.values())) != 3:
        _fail("annotators and adjudicator must be three distinct stable IDs")

    limits = _expect_dict(config["limits"], "limits")
    _exact_keys(
        limits,
        {"selected_prs", "max_candidates_per_pr", "max_candidates", "max_sanitized_bytes"},
        "limits",
    )
    if limits != {
        "selected_prs": 29,
        "max_candidates_per_pr": 16,
        "max_candidates": 480,
        "max_sanitized_bytes": 64 * 1024 * 1024,
    }:
        _fail("Phase 8D corpus limits differ from the frozen Phase 8B ceilings")

    model_plan = _expect_dict(config["real_model_plan"], "real_model_plan")
    _exact_keys(
        model_plan,
        {
            "authorized",
            "seeds",
            "test_labels_sealed",
            "primary_metric",
            "secondary_metric",
            "threshold_source",
        },
        "real_model_plan",
    )
    if (
        model_plan["authorized"] is not False
        or model_plan["seeds"] != []
        or model_plan["test_labels_sealed"] is not True
        or model_plan["primary_metric"] != "f1"
        or model_plan["secondary_metric"] != "average_precision"
        or model_plan["threshold_source"] != "validation"
    ):
        _fail("real model plan is not the frozen unauthorized template")
    return config


def load_config(path: Path) -> dict[str, Any]:
    return validate_config(_load_json(path))


def build_finder_envelopes(
    config: dict[str, Any],
    queue_rows: Sequence[dict[str, Any]],
    pr_sources: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    validate_config(config)
    queue = vc.validate_finder_queue(queue_rows, pr_sources)
    envelopes: list[dict[str, Any]] = []
    for row in sorted(queue, key=lambda item: item["queue_id"]):
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "phase_id": PHASE_ID,
            "queue_id": row["queue_id"],
            "queue_sha256": row["queue_sha256"],
            "source_id": row["source_id"],
            "pr_source_sha256": row["pr_source_sha256"],
            "diff_sha256": row["diff_sha256"],
            "diff_object_key": row["diff_object_key"],
            "max_candidates": row["max_candidates"],
            "executable": True,
            "blocked_by": [],
            "envelope_sha256": "",
        }
        envelopes.append(_with_hash(envelope, "envelope_sha256"))
    return envelopes


def with_finder_run_hash(row: dict[str, Any]) -> dict[str, Any]:
    return _with_hash(row, "run_sha256")


def validate_finder_runs(
    raw_rows: Sequence[Any],
    config: dict[str, Any],
    plan: dict[str, Any],
    queue_rows: Sequence[dict[str, Any]],
    pr_sources: Sequence[dict[str, Any]],
    candidate_sources: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    validate_config(config)
    queue = vc.validate_finder_queue(queue_rows, pr_sources)
    queue_by_id = {row["queue_id"]: row for row in queue}
    if len(raw_rows) != len(queue_by_id):
        _fail("Finder receipts must cover all 29 queue records exactly once")
    candidates = (
        vc.validate_candidate_sources(candidate_sources, plan, pr_sources)
        if candidate_sources
        else []
    )
    candidates_by_source: dict[str, list[str]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_source[candidate["source_id"]].append(candidate["candidate_id"])

    rows: list[dict[str, Any]] = []
    seen_queue_ids: set[str] = set()
    seen_run_ids: set[str] = set()
    seen_candidate_ids: set[str] = set()
    for index, raw in enumerate(raw_rows):
        where = f"finder_runs[{index}]"
        row = _expect_dict(raw, where)
        _exact_keys(row, FINDER_RUN_KEYS, where)
        if row["schema_version"] != SCHEMA_VERSION:
            _fail(f"{where}.schema_version must be 1")
        run_id = _identifier(row["run_id"], f"{where}.run_id")
        queue_id = _identifier(row["queue_id"], f"{where}.queue_id")
        if run_id in seen_run_ids or queue_id in seen_queue_ids or queue_id not in queue_by_id:
            _fail(f"{where} has a duplicate or unknown run/queue identity")
        seen_run_ids.add(run_id)
        seen_queue_ids.add(queue_id)
        queue_row = queue_by_id[queue_id]
        bindings = {
            "queue_sha256": queue_row["queue_sha256"],
            "source_id": queue_row["source_id"],
            "pr_source_sha256": queue_row["pr_source_sha256"],
            "diff_sha256": queue_row["diff_sha256"],
        }
        for key, expected in bindings.items():
            if row[key] != expected:
                _fail(f"{where}.{key} does not bind the frozen queue")
        _sha(row["queue_sha256"], f"{where}.queue_sha256")
        _sha(row["pr_source_sha256"], f"{where}.pr_source_sha256")
        _sha(row["diff_sha256"], f"{where}.diff_sha256")
        status = row["status"]
        if status not in {"completed", "completed_zero_candidates", "failed"}:
            _fail(f"{where}.status is invalid")
        candidate_ids = row["candidate_ids"]
        if (
            not isinstance(candidate_ids, list)
            or len(candidate_ids) > queue_row["max_candidates"]
            or candidate_ids != sorted(candidate_ids)
        ):
            _fail(f"{where}.candidate_ids must be a sorted bounded list")
        for item_index, candidate_id in enumerate(candidate_ids):
            _identifier(candidate_id, f"{where}.candidate_ids[{item_index}]")
            if candidate_id in seen_candidate_ids:
                _fail(f"candidate {candidate_id!r} appears in multiple Finder receipts")
            seen_candidate_ids.add(candidate_id)
        count = _non_negative_integer(row["candidate_count"], f"{where}.candidate_count")
        if count != len(candidate_ids):
            _fail(f"{where}.candidate_count does not match candidate_ids")
        expected_ids = sorted(candidates_by_source.get(row["source_id"], []))
        if candidate_ids != expected_ids:
            _fail(f"{where}.candidate_ids do not reconcile with candidate sources")
        _identifier(row["provider"], f"{where}.provider")
        _bounded_text(row["model"], f"{where}.model", 200)
        _sha(row["prompt_sha256"], f"{where}.prompt_sha256")
        started = _timestamp(row["started_at"], f"{where}.started_at")
        finished = _timestamp(row["finished_at"], f"{where}.finished_at")
        if finished < started:
            _fail(f"{where}.finished_at precedes started_at")
        _non_negative_integer(row["input_tokens"], f"{where}.input_tokens")
        _non_negative_integer(row["output_tokens"], f"{where}.output_tokens")
        _non_negative_number(row["cost_cny"], f"{where}.cost_cny")
        _sha(row["trace_sha256"], f"{where}.trace_sha256")
        if not isinstance(row["synthetic"], bool):
            _fail(f"{where}.synthetic must be boolean")
        if row["provider"] != config["finder"]["provider"]:
            _fail(f"{where}.provider differs from the authorized provider")
        if row["model"] != config["finder"]["model"]:
            _fail(f"{where}.model differs from the requested API model ID")
        if row["prompt_sha256"] != config["finder"]["prompt_sha256"]:
            _fail(f"{where}.prompt_sha256 differs from the frozen Finder prompt")
        if row["synthetic"] is True and (
            row["input_tokens"] != 0 or row["output_tokens"] != 0 or row["cost_cny"] != 0
        ):
            _fail(f"{where} synthetic receipt must have zero tokens and cost")
        if status == "completed" and (count == 0 or row["error_category"] is not None):
            _fail(f"{where} completed status requires candidates and no error")
        if status == "completed_zero_candidates" and (
            count != 0 or row["error_category"] is not None
        ):
            _fail(f"{where} zero-candidate completion is inconsistent")
        if status == "failed":
            if count != 0:
                _fail(f"{where} failed run cannot emit candidates")
            _identifier(row["error_category"], f"{where}.error_category")
        elif row["error_category"] is not None:
            _fail(f"{where} non-failed run cannot carry an error category")
        _sha(row["run_sha256"], f"{where}.run_sha256")
        if row["run_sha256"] != _sha256(_without_hash(row, "run_sha256")):
            _fail(f"{where}.run_sha256 does not match canonical content")
        rows.append(row)
    if seen_candidate_ids != {row["candidate_id"] for row in candidates}:
        _fail("Finder receipts do not cover the candidate source set exactly")
    if sum(row["input_tokens"] for row in rows) > config["finder"]["max_input_tokens"]:
        _fail("Finder receipts exceed the authorized input-token ceiling")
    if sum(row["output_tokens"] for row in rows) > config["finder"]["max_output_tokens"]:
        _fail("Finder receipts exceed the authorized output-token ceiling")
    if sum(row["cost_cny"] for row in rows) > config["finder"]["max_cost_cny"]:
        _fail("Finder receipts exceed the authorized CNY ceiling")
    return sorted(rows, key=lambda item: item["queue_id"])


def _candidate_set_sha256(candidates: Sequence[dict[str, Any]]) -> str:
    return _sha256(
        sorted(
            (row["candidate_id"], row["candidate_source_sha256"])
            for row in candidates
        )
    )


def _packet_item(
    candidate: dict[str, Any], source_annotations: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "candidate_source_sha256": candidate["candidate_source_sha256"],
        "evidence_sha256": candidate["content_sha256"],
        "repository_id": candidate["repository_id"],
        "source_id": candidate["source_id"],
        "source_revision": candidate["source_revision"],
        "candidate_text": candidate["candidate_text"],
        "evidence": candidate["evidence"],
        "tool_summaries": candidate["tool_summaries"],
        "language": candidate["language"],
        "severity": candidate["severity"],
        "source_annotations": [
            {
                "annotation_id": row["annotation_id"],
                "annotation_sha256": row["annotation_sha256"],
                "annotator_id": row["annotator_id"],
                "label": row["label"],
                "rationale": row["rationale"],
            }
            for row in source_annotations
        ],
    }


def build_independent_packet(
    candidate_sources: Sequence[dict[str, Any]],
    reviewer_id: str,
    rubric_sha256: str,
    order_seed: int,
    created_at: str,
    *,
    synthetic: bool,
) -> dict[str, Any]:
    if not candidate_sources:
        _fail("cannot build an annotation packet without candidates")
    reviewer = _identifier(reviewer_id, "reviewer_id")
    rubric = _sha(rubric_sha256, "rubric_sha256")
    _timestamp(created_at, "created_at")
    if isinstance(order_seed, bool) or not isinstance(order_seed, int):
        _fail("order_seed must be an integer")
    if not isinstance(synthetic, bool):
        _fail("synthetic must be boolean")
    if synthetic is False:
        _fail("real annotation packets are forbidden until human identities are assigned")
    candidates = sorted(candidate_sources, key=lambda row: row["candidate_id"])
    order = list(candidates)
    random.Random(order_seed).shuffle(order)
    candidate_set_hash = _candidate_set_sha256(candidates)
    packet = {
        "schema_version": SCHEMA_VERSION,
        "packet_id": f"independent-{_sha256([reviewer, candidate_set_hash, order_seed])[:24]}",
        "mode": "independent",
        "reviewer_id": reviewer,
        "created_at": created_at,
        "rubric_sha256": rubric,
        "candidate_set_sha256": candidate_set_hash,
        "order_seed": order_seed,
        "synthetic": synthetic,
        "items": [_packet_item(candidate, []) for candidate in order],
        "packet_sha256": "",
    }
    return _with_hash(packet, "packet_sha256")


def validate_packet(
    raw: Any, candidate_sources: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    packet = _expect_dict(raw, "annotation_packet")
    _exact_keys(packet, PACKET_KEYS, "annotation_packet")
    if packet["schema_version"] != SCHEMA_VERSION:
        _fail("annotation_packet.schema_version must be 1")
    _identifier(packet["packet_id"], "annotation_packet.packet_id")
    mode = packet["mode"]
    if mode not in {"independent", "adjudication"}:
        _fail("annotation_packet.mode is invalid")
    _identifier(packet["reviewer_id"], "annotation_packet.reviewer_id")
    _timestamp(packet["created_at"], "annotation_packet.created_at")
    _sha(packet["rubric_sha256"], "annotation_packet.rubric_sha256")
    if isinstance(packet["order_seed"], bool) or not isinstance(packet["order_seed"], int):
        _fail("annotation_packet.order_seed must be an integer")
    if not isinstance(packet["synthetic"], bool):
        _fail("annotation_packet.synthetic must be boolean")
    if packet["synthetic"] is False:
        _fail("real annotation packets are forbidden until human identities are assigned")
    candidates = {row["candidate_id"]: row for row in candidate_sources}
    items = packet["items"]
    if not isinstance(items, list) or not items:
        _fail("annotation_packet.items must be non-empty")
    seen: set[str] = set()
    for index, raw_item in enumerate(items):
        where = f"annotation_packet.items[{index}]"
        item = _expect_dict(raw_item, where)
        _exact_keys(item, PACKET_ITEM_KEYS, where)
        candidate_id = _identifier(item["candidate_id"], f"{where}.candidate_id")
        if candidate_id in seen or candidate_id not in candidates:
            _fail(f"{where}.candidate_id is duplicate or unknown")
        seen.add(candidate_id)
        candidate = candidates[candidate_id]
        bindings = {
            "candidate_source_sha256": candidate["candidate_source_sha256"],
            "evidence_sha256": candidate["content_sha256"],
            "repository_id": candidate["repository_id"],
            "source_id": candidate["source_id"],
            "source_revision": candidate["source_revision"],
            "candidate_text": candidate["candidate_text"],
            "evidence": candidate["evidence"],
            "tool_summaries": candidate["tool_summaries"],
            "language": candidate["language"],
            "severity": candidate["severity"],
        }
        for key, expected in bindings.items():
            if item[key] != expected:
                _fail(f"{where}.{key} does not bind the candidate source")
        source_annotations = item["source_annotations"]
        expected_count = 0 if mode == "independent" else 2
        if not isinstance(source_annotations, list) or len(source_annotations) != expected_count:
            _fail(f"{where}.source_annotations has the wrong count for {mode}")
        for source_index, source in enumerate(source_annotations):
            source_where = f"{where}.source_annotations[{source_index}]"
            source = _expect_dict(source, source_where)
            _exact_keys(source, SOURCE_ANNOTATION_KEYS, source_where)
            _identifier(source["annotation_id"], f"{source_where}.annotation_id")
            _sha(source["annotation_sha256"], f"{source_where}.annotation_sha256")
            _identifier(source["annotator_id"], f"{source_where}.annotator_id")
            if source["label"] not in {"keep", "drop", "uncertain"}:
                _fail(f"{source_where}.label is invalid")
            _bounded_text(source["rationale"], f"{source_where}.rationale", 4000)
        if mode == "adjudication" and (
            len({row["annotation_id"] for row in source_annotations}) != 2
            or len({row["annotator_id"] for row in source_annotations}) != 2
        ):
            _fail(f"{where}.source_annotations are not two distinct independent labels")
    expected_set = (
        set(candidates) if mode == "independent" else {item["candidate_id"] for item in items}
    )
    if mode == "independent" and seen != expected_set:
        _fail("independent packet does not cover the complete candidate set")
    if packet["candidate_set_sha256"] != _candidate_set_sha256(
        [candidates[candidate_id] for candidate_id in sorted(seen)]
    ):
        _fail("annotation_packet.candidate_set_sha256 does not match its items")
    _sha(packet["packet_sha256"], "annotation_packet.packet_sha256")
    if packet["packet_sha256"] != _sha256(_without_hash(packet, "packet_sha256")):
        _fail("annotation_packet.packet_sha256 does not match canonical content")
    return packet


def validate_independent_packet_pair(
    packet_a: dict[str, Any], packet_b: dict[str, Any]
) -> None:
    if packet_a["mode"] != "independent" or packet_b["mode"] != "independent":
        _fail("both packets must be independent annotation packets")
    if packet_a["reviewer_id"] == packet_b["reviewer_id"]:
        _fail("independent annotation packets repeat the reviewer identity")
    if packet_a["candidate_set_sha256"] != packet_b["candidate_set_sha256"]:
        _fail("independent packets do not cover the same candidate set")
    if packet_a["rubric_sha256"] != packet_b["rubric_sha256"]:
        _fail("independent packets do not bind the same rubric")
    if packet_a["synthetic"] != packet_b["synthetic"]:
        _fail("independent packets mix synthetic and real provenance")


def import_packet_responses(
    packet: dict[str, Any],
    candidate_sources: Sequence[dict[str, Any]],
    raw_responses: Sequence[Any],
) -> list[dict[str, Any]]:
    packet = validate_packet(packet, candidate_sources)
    items = {item["candidate_id"]: item for item in packet["items"]}
    if len(raw_responses) != len(items):
        _fail("response rows must cover every packet item exactly once")
    responses: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_responses):
        where = f"responses[{index}]"
        response = _expect_dict(raw, where)
        _exact_keys(response, RESPONSE_KEYS, where)
        candidate_id = _identifier(response["candidate_id"], f"{where}.candidate_id")
        if candidate_id in responses or candidate_id not in items:
            _fail(f"{where}.candidate_id is duplicate or outside the packet")
        if response["label"] not in {"keep", "drop", "uncertain"}:
            _fail(f"{where}.label is invalid")
        _bounded_text(response["rationale"], f"{where}.rationale", 4000)
        _timestamp(response["created_at"], f"{where}.created_at")
        responses[candidate_id] = response
    if set(responses) != set(items):
        _fail("response rows do not cover the packet candidate set")

    annotations: list[dict[str, Any]] = []
    for candidate_id in sorted(responses):
        response = responses[candidate_id]
        item = items[candidate_id]
        sources = item["source_annotations"]
        if packet["mode"] == "adjudication":
            source_reviewers = {source["annotator_id"] for source in sources}
            if packet["reviewer_id"] in source_reviewers:
                _fail("adjudicator repeats an independent reviewer identity")
        annotation = {
            "schema_version": SCHEMA_VERSION,
            "annotation_id": f"ann-{_sha256([packet['packet_id'], candidate_id])[:24]}",
            "candidate_id": candidate_id,
            "candidate_source_sha256": item["candidate_source_sha256"],
            "annotator_id": packet["reviewer_id"],
            "role": "annotator" if packet["mode"] == "independent" else "adjudicator",
            "label": response["label"],
            "rationale": response["rationale"],
            "evidence_sha256": item["evidence_sha256"],
            "source_annotation_ids": [source["annotation_id"] for source in sources],
            "source_annotation_sha256s": [source["annotation_sha256"] for source in sources],
            "created_at": response["created_at"],
            "synthetic": packet["synthetic"],
            "annotation_sha256": "",
        }
        annotations.append(vc.with_annotation_hash(annotation))
    return vc.validate_annotations(annotations, candidate_sources)


def build_adjudication_packet(
    candidate_sources: Sequence[dict[str, Any]],
    independent_annotations: Sequence[dict[str, Any]],
    reviewer_id: str,
    rubric_sha256: str,
    order_seed: int,
    created_at: str,
) -> dict[str, Any]:
    annotations = vc.validate_annotations(independent_annotations, candidate_sources)
    by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for annotation in annotations:
        if annotation["role"] != "annotator":
            _fail("adjudication packet input may contain only independent annotations")
        by_candidate[annotation["candidate_id"]].append(annotation)
    reviewer = _identifier(reviewer_id, "reviewer_id")
    candidate_by_id = {row["candidate_id"]: row for row in candidate_sources}
    needs_adjudication: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    provenance: set[bool] = set()
    for candidate_id, candidate in sorted(candidate_by_id.items()):
        independent = sorted(by_candidate.get(candidate_id, []), key=lambda row: row["annotation_id"])
        if len(independent) != 2 or len({row["annotator_id"] for row in independent}) != 2:
            _fail(f"candidate {candidate_id!r} does not have two distinct independent labels")
        if reviewer in {row["annotator_id"] for row in independent}:
            _fail("adjudicator repeats an independent reviewer identity")
        provenance.update(row["synthetic"] for row in independent)
        agreed = independent[0]["label"] == independent[1]["label"] != "uncertain"
        if not agreed:
            needs_adjudication.append((candidate, independent))
    if len(provenance) != 1:
        _fail("independent annotations mix synthetic and real provenance")
    synthetic = next(iter(provenance))
    if synthetic is False:
        _fail("real adjudication packets are forbidden until a human amendment is recorded")
    if not needs_adjudication:
        _fail("no candidate requires adjudication")
    random.Random(order_seed).shuffle(needs_adjudication)
    selected_candidates = [candidate for candidate, _ in needs_adjudication]
    candidate_set_hash = _candidate_set_sha256(selected_candidates)
    packet = {
        "schema_version": SCHEMA_VERSION,
        "packet_id": f"adjudication-{_sha256([reviewer, candidate_set_hash, order_seed])[:24]}",
        "mode": "adjudication",
        "reviewer_id": reviewer,
        "created_at": _timestamp(created_at, "created_at"),
        "rubric_sha256": _sha(rubric_sha256, "rubric_sha256"),
        "candidate_set_sha256": candidate_set_hash,
        "order_seed": order_seed,
        "synthetic": synthetic,
        "items": [
            _packet_item(candidate, independent)
            for candidate, independent in needs_adjudication
        ],
        "packet_sha256": "",
    }
    return _with_hash(packet, "packet_sha256")


def merge_annotations(
    candidate_sources: Sequence[dict[str, Any]],
    annotation_groups: Sequence[Sequence[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    merged = [row for group in annotation_groups for row in group]
    validated = vc.validate_annotations(merged, candidate_sources)
    _, agreement, unresolved = vc.resolve_annotations(candidate_sources, validated)
    return sorted(validated, key=lambda row: row["annotation_id"]), {
        "agreement": agreement,
        "unresolved_candidates": unresolved,
        "ready_to_freeze": not unresolved,
    }


def build_real_freeze(
    config: dict[str, Any],
    plan: dict[str, Any],
    pr_sources: Sequence[dict[str, Any]],
    queue_rows: Sequence[dict[str, Any]],
    finder_runs: Sequence[dict[str, Any]],
    candidate_sources: Sequence[dict[str, Any]],
    annotations: Sequence[dict[str, Any]],
    frozen_at: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    runs = validate_finder_runs(
        finder_runs, config, plan, queue_rows, pr_sources, candidate_sources
    )
    completed_source_ids = {
        row["source_id"]
        for row in runs
        if row["status"] in {"completed", "completed_zero_candidates"}
    }
    candidates, splits, corpus_manifest = vc.build_freeze(
        plan,
        pr_sources,
        candidate_sources,
        annotations,
        frozen_at,
        completed_source_ids=completed_source_ids,
    )
    gates = list(corpus_manifest["incomplete_gates"])
    failed = [row for row in runs if row["status"] == "failed"]
    synthetic_runs = [row for row in runs if row["synthetic"]]
    if failed:
        gates.append(
            {
                "gate": "finder_runs_failed",
                "count": len(failed),
                "sample_ids": [row["source_id"] for row in failed[:10]],
            }
        )
    if synthetic_runs:
        gates.append(
            {
                "gate": "synthetic_finder_runs_present",
                "count": len(synthetic_runs),
                "sample_ids": [],
            }
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "phase_id": PHASE_ID,
        "frozen_at": frozen_at,
        "finder_runs_sha256": _sha256([row["run_sha256"] for row in runs]),
        "finder_completed": sum(row["status"] == "completed" for row in runs),
        "finder_completed_zero_candidates": sum(
            row["status"] == "completed_zero_candidates" for row in runs
        ),
        "finder_failed": len(failed),
        "corpus_manifest": corpus_manifest,
        "trainable": corpus_manifest["trainable"] and not gates,
        "incomplete_gates": gates,
        "manifest_sha256": "",
    }
    return candidates, splits, _with_hash(manifest, "manifest_sha256")


def validate_real_freeze_manifest(raw: Any) -> dict[str, Any]:
    manifest = _expect_dict(raw, "real_freeze_manifest")
    expected = {
        "schema_version",
        "phase_id",
        "frozen_at",
        "finder_runs_sha256",
        "finder_completed",
        "finder_completed_zero_candidates",
        "finder_failed",
        "corpus_manifest",
        "trainable",
        "incomplete_gates",
        "manifest_sha256",
    }
    _exact_keys(manifest, expected, "real_freeze_manifest")
    if manifest["schema_version"] != SCHEMA_VERSION or manifest["phase_id"] != PHASE_ID:
        _fail("real_freeze_manifest schema or phase ID is invalid")
    _timestamp(manifest["frozen_at"], "real_freeze_manifest.frozen_at")
    _sha(manifest["finder_runs_sha256"], "real_freeze_manifest.finder_runs_sha256")
    counts = [
        _non_negative_integer(manifest[key], f"real_freeze_manifest.{key}")
        for key in ("finder_completed", "finder_completed_zero_candidates", "finder_failed")
    ]
    if sum(counts) != 29:
        _fail("real_freeze_manifest Finder counts must total 29")
    corpus_manifest = _expect_dict(
        manifest["corpus_manifest"], "real_freeze_manifest.corpus_manifest"
    )
    if corpus_manifest.get("manifest_sha256") != vc._sha256(
        vc._record_payload(corpus_manifest, "manifest_sha256")
    ):
        _fail("real_freeze_manifest corpus manifest hash is stale")
    gates = manifest["incomplete_gates"]
    if not isinstance(gates, list) or not all(isinstance(gate, dict) for gate in gates):
        _fail("real_freeze_manifest.incomplete_gates must be an object list")
    corpus_gates = corpus_manifest.get("incomplete_gates")
    if not isinstance(corpus_gates, list) or gates[: len(corpus_gates)] != corpus_gates:
        _fail("real_freeze_manifest omits or changes corpus incomplete gates")
    gate_names = {gate.get("gate") for gate in gates}
    if manifest["finder_failed"] and "finder_runs_failed" not in gate_names:
        _fail("real_freeze_manifest omits the failed Finder run gate")
    if not isinstance(manifest["trainable"], bool):
        _fail("real_freeze_manifest.trainable must be boolean")
    expected_trainable = corpus_manifest.get("trainable") is True and not gates
    if manifest["trainable"] is not expected_trainable:
        _fail("real_freeze_manifest.trainable is inconsistent with its gates")
    _sha(manifest["manifest_sha256"], "real_freeze_manifest.manifest_sha256")
    if manifest["manifest_sha256"] != _sha256(
        _without_hash(manifest, "manifest_sha256")
    ):
        _fail("real_freeze_manifest.manifest_sha256 does not match canonical content")
    return manifest


def real_model_readiness(config: dict[str, Any], real_manifest: dict[str, Any]) -> dict[str, Any]:
    validate_config(config)
    real_manifest = validate_real_freeze_manifest(real_manifest)
    reasons: list[str] = []
    if not real_manifest.get("trainable"):
        reasons.append("real_freeze_not_trainable")
    if real_manifest.get("incomplete_gates"):
        reasons.append("real_freeze_has_incomplete_gates")
    if not config["authorization"]["real_model_training"]:
        reasons.append("real_model_training_unauthorized")
    if not config["real_model_plan"]["authorized"]:
        reasons.append("real_model_plan_unfrozen")
    if not config["real_model_plan"]["seeds"]:
        reasons.append("real_model_seeds_missing")
    if not config["real_model_plan"]["test_labels_sealed"]:
        reasons.append("test_labels_not_sealed")
    return {"ready": not reasons, "blocked_by": reasons}


def assert_real_model_ready(config: dict[str, Any], real_manifest: dict[str, Any]) -> None:
    readiness = real_model_readiness(config, real_manifest)
    if not readiness["ready"]:
        _fail("real model run is blocked: " + ", ".join(readiness["blocked_by"]))


def _common_corpus_inputs(args: argparse.Namespace) -> tuple[
    dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]
]:
    plan = vc.load_plan(args.plan)
    sources = vc.load_pr_sources(args.pr_sources, plan)
    queue = vc.validate_finder_queue(_load_jsonl(args.queue), sources)
    return plan, sources, queue


def _command_validate_config(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    return {
        "status": "ok",
        "phase_id": config["phase_id"],
        "offline_preparation_only": config["offline_preparation_only"],
        "provider_calls_authorized": config["authorization"]["provider_calls"],
    }


def _command_prepare_finder(args: argparse.Namespace) -> dict[str, Any]:
    _, sources, queue = _common_corpus_inputs(args)
    envelopes = build_finder_envelopes(load_config(args.config), queue, sources)
    _write_jsonl(args.out, envelopes)
    executable = sum(row["executable"] is True for row in envelopes)
    return {"status": "ready", "envelopes": len(envelopes), "executable": executable}


def _command_export_independent(args: argparse.Namespace) -> dict[str, Any]:
    plan = vc.load_plan(args.plan)
    sources = vc.load_pr_sources(args.pr_sources, plan)
    candidates = vc.load_candidate_sources(args.candidate_sources, plan, sources)
    packet = build_independent_packet(
        candidates,
        args.reviewer_id,
        args.rubric_sha256,
        args.order_seed,
        args.created_at,
        synthetic=args.synthetic,
    )
    _write_json(args.out, packet)
    return {"status": "ok", "packet_id": packet["packet_id"], "items": len(packet["items"])}


def _command_validate_finder_runs(args: argparse.Namespace) -> dict[str, Any]:
    plan, sources, queue = _common_corpus_inputs(args)
    raw_candidates = _load_jsonl(args.candidate_sources)
    candidates = (
        vc.validate_candidate_sources(raw_candidates, plan, sources)
        if raw_candidates
        else []
    )
    runs = validate_finder_runs(
        _load_jsonl(args.finder_runs),
        load_config(args.config),
        plan,
        queue,
        sources,
        candidates,
    )
    return {
        "status": "ok",
        "runs": len(runs),
        "completed": sum(row["status"] == "completed" for row in runs),
        "completed_zero_candidates": sum(
            row["status"] == "completed_zero_candidates" for row in runs
        ),
        "failed": sum(row["status"] == "failed" for row in runs),
        "synthetic": all(row["synthetic"] for row in runs),
    }


def _command_import_responses(args: argparse.Namespace) -> dict[str, Any]:
    plan = vc.load_plan(args.plan)
    sources = vc.load_pr_sources(args.pr_sources, plan)
    candidates = vc.load_candidate_sources(args.candidate_sources, plan, sources)
    annotations = import_packet_responses(
        _load_json(args.packet), candidates, _load_jsonl(args.responses)
    )
    _write_jsonl(args.out, annotations)
    return {"status": "ok", "annotations": len(annotations)}


def _command_export_adjudication(args: argparse.Namespace) -> dict[str, Any]:
    plan = vc.load_plan(args.plan)
    sources = vc.load_pr_sources(args.pr_sources, plan)
    candidates = vc.load_candidate_sources(args.candidate_sources, plan, sources)
    annotations = vc.load_annotations(args.annotations, candidates)
    packet = build_adjudication_packet(
        candidates,
        annotations,
        args.reviewer_id,
        args.rubric_sha256,
        args.order_seed,
        args.created_at,
    )
    _write_json(args.out, packet)
    return {"status": "ok", "packet_id": packet["packet_id"], "items": len(packet["items"])}


def _command_merge_annotations(args: argparse.Namespace) -> dict[str, Any]:
    plan = vc.load_plan(args.plan)
    sources = vc.load_pr_sources(args.pr_sources, plan)
    candidates = vc.load_candidate_sources(args.candidate_sources, plan, sources)
    groups = [vc.load_annotations(path, candidates) for path in args.inputs]
    merged, summary = merge_annotations(candidates, groups)
    _write_jsonl(args.out, merged)
    return {"status": "ok", "annotations": len(merged), **summary}


def _command_freeze_real(args: argparse.Namespace) -> dict[str, Any]:
    plan, sources, queue = _common_corpus_inputs(args)
    candidates = vc.load_candidate_sources(args.candidate_sources, plan, sources)
    annotations = vc.load_annotations(args.annotations, candidates)
    frozen, splits, manifest = build_real_freeze(
        load_config(args.config),
        plan,
        sources,
        queue,
        _load_jsonl(args.finder_runs),
        candidates,
        annotations,
        args.frozen_at,
    )
    _write_jsonl(args.candidates_out, frozen)
    _write_json(args.splits_out, splits)
    _write_json(args.manifest_out, manifest)
    return {
        "status": "ok",
        "records": len(frozen),
        "trainable": manifest["trainable"],
        "incomplete_gates": manifest["incomplete_gates"],
    }


def _command_check_real_model(args: argparse.Namespace) -> dict[str, Any]:
    return real_model_readiness(load_config(args.config), _load_json(args.manifest))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare Phase 8D evidence without network access.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config")
    validate.add_argument("--config", type=Path, required=True)
    validate.set_defaults(handler=_command_validate_config)

    prepare = subparsers.add_parser("prepare-finder")
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--plan", type=Path, required=True)
    prepare.add_argument("--pr-sources", type=Path, required=True)
    prepare.add_argument("--queue", type=Path, required=True)
    prepare.add_argument("--out", type=Path, required=True)
    prepare.set_defaults(handler=_command_prepare_finder)

    independent = subparsers.add_parser("export-independent")
    independent.add_argument("--plan", type=Path, required=True)
    independent.add_argument("--pr-sources", type=Path, required=True)
    independent.add_argument("--candidate-sources", type=Path, required=True)
    independent.add_argument("--reviewer-id", required=True)
    independent.add_argument("--rubric-sha256", required=True)
    independent.add_argument("--order-seed", type=int, required=True)
    independent.add_argument("--created-at", required=True)
    independent.add_argument("--synthetic", action="store_true")
    independent.add_argument("--out", type=Path, required=True)
    independent.set_defaults(handler=_command_export_independent)

    run_validation = subparsers.add_parser("validate-finder-runs")
    run_validation.add_argument("--config", type=Path, required=True)
    run_validation.add_argument("--plan", type=Path, required=True)
    run_validation.add_argument("--pr-sources", type=Path, required=True)
    run_validation.add_argument("--queue", type=Path, required=True)
    run_validation.add_argument("--finder-runs", type=Path, required=True)
    run_validation.add_argument("--candidate-sources", type=Path, required=True)
    run_validation.set_defaults(handler=_command_validate_finder_runs)

    response = subparsers.add_parser("import-responses")
    response.add_argument("--plan", type=Path, required=True)
    response.add_argument("--pr-sources", type=Path, required=True)
    response.add_argument("--candidate-sources", type=Path, required=True)
    response.add_argument("--packet", type=Path, required=True)
    response.add_argument("--responses", type=Path, required=True)
    response.add_argument("--out", type=Path, required=True)
    response.set_defaults(handler=_command_import_responses)

    adjudication = subparsers.add_parser("export-adjudication")
    adjudication.add_argument("--plan", type=Path, required=True)
    adjudication.add_argument("--pr-sources", type=Path, required=True)
    adjudication.add_argument("--candidate-sources", type=Path, required=True)
    adjudication.add_argument("--annotations", type=Path, required=True)
    adjudication.add_argument("--reviewer-id", required=True)
    adjudication.add_argument("--rubric-sha256", required=True)
    adjudication.add_argument("--order-seed", type=int, required=True)
    adjudication.add_argument("--created-at", required=True)
    adjudication.add_argument("--out", type=Path, required=True)
    adjudication.set_defaults(handler=_command_export_adjudication)

    merge = subparsers.add_parser("merge-annotations")
    merge.add_argument("--plan", type=Path, required=True)
    merge.add_argument("--pr-sources", type=Path, required=True)
    merge.add_argument("--candidate-sources", type=Path, required=True)
    merge.add_argument("--inputs", type=Path, nargs="+", required=True)
    merge.add_argument("--out", type=Path, required=True)
    merge.set_defaults(handler=_command_merge_annotations)

    freeze = subparsers.add_parser("freeze-real")
    freeze.add_argument("--config", type=Path, required=True)
    freeze.add_argument("--plan", type=Path, required=True)
    freeze.add_argument("--pr-sources", type=Path, required=True)
    freeze.add_argument("--queue", type=Path, required=True)
    freeze.add_argument("--finder-runs", type=Path, required=True)
    freeze.add_argument("--candidate-sources", type=Path, required=True)
    freeze.add_argument("--annotations", type=Path, required=True)
    freeze.add_argument("--frozen-at", required=True)
    freeze.add_argument("--candidates-out", type=Path, required=True)
    freeze.add_argument("--splits-out", type=Path, required=True)
    freeze.add_argument("--manifest-out", type=Path, required=True)
    freeze.set_defaults(handler=_command_freeze_real)

    readiness = subparsers.add_parser("check-real-model")
    readiness.add_argument("--config", type=Path, required=True)
    readiness.add_argument("--manifest", type=Path, required=True)
    readiness.set_defaults(handler=_command_check_real_model)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (Phase8DValidationError, vc.CorpusValidationError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
