"""Deterministic, non-semantic Phase 8D annotation workflow simulation.

This utility exercises response import and adjudication without pretending that
an agent or hash bucket is a human quality reviewer. It refuses real packets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

import verifier_corpus as vc
import verifier_phase8d as phase8d


SIMULATION_PHASE_ID = "week8d-synthetic-annotation-dry-run-v1"


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
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[Any]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def synthetic_label(candidate_id: str, reviewer_id: str, mode: str) -> tuple[str, int]:
    """Return a reproducible workflow-only label and its non-semantic bucket."""

    digest = hashlib.sha256(f"{reviewer_id}:{candidate_id}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big")
    if mode == "adjudication":
        return ("keep" if bucket % 2 else "drop"), bucket
    if mode != "independent":
        raise ValueError(f"unsupported packet mode: {mode}")
    if bucket % 11 == 0:
        return "uncertain", bucket
    return ("keep" if bucket % 2 else "drop"), bucket


def build_synthetic_responses(packet: dict[str, Any], created_at: str) -> list[dict[str, str]]:
    if packet.get("synthetic") is not True:
        raise phase8d.Phase8DValidationError("simulation refuses a real annotation packet")
    reviewer_id = packet.get("reviewer_id")
    if not isinstance(reviewer_id, str) or not reviewer_id.startswith("synthetic-"):
        raise phase8d.Phase8DValidationError("simulation reviewer ID must start with synthetic-")
    timestamp = phase8d._timestamp(created_at, "created_at")
    rows: list[dict[str, str]] = []
    for item in packet["items"]:
        label, bucket = synthetic_label(item["candidate_id"], reviewer_id, packet["mode"])
        rows.append(
            {
                "candidate_id": item["candidate_id"],
                "label": label,
                "rationale": (
                    f"Synthetic workflow-only SHA-256 bucket {bucket}; "
                    "no candidate-quality judgment was performed."
                ),
                "created_at": timestamp,
            }
        )
    return rows


def _command_generate(args: argparse.Namespace) -> dict[str, Any]:
    plan = vc.load_plan(args.plan)
    sources = vc.load_pr_sources(args.pr_sources, plan)
    candidates = vc.load_candidate_sources(args.candidate_sources, plan, sources)
    packet = phase8d.validate_packet(_load_json(args.packet), candidates)
    responses = build_synthetic_responses(packet, args.created_at)
    _write_jsonl(args.out, responses)
    counts = {label: 0 for label in ("keep", "drop", "uncertain")}
    for row in responses:
        counts[row["label"]] += 1
    return {"status": "synthetic_only", "rows": len(responses), "labels": counts}


def _artifact_hash(path: Path) -> str:
    canonical_text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def _command_manifest(args: argparse.Namespace) -> dict[str, Any]:
    final_annotations = _load_jsonl(args.final_annotations)
    freeze_manifest = phase8d.validate_real_freeze_manifest(_load_json(args.freeze_manifest))
    readiness = phase8d.real_model_readiness(
        phase8d.load_config(args.config), freeze_manifest
    )
    if not final_annotations or any(row.get("synthetic") is not True for row in final_annotations):
        raise phase8d.Phase8DValidationError("simulation annotations must all be synthetic")
    if freeze_manifest["trainable"] or readiness["ready"]:
        raise phase8d.Phase8DValidationError("synthetic evidence unexpectedly opened a real gate")
    candidate_ids = {row["candidate_id"] for row in final_annotations}
    independent = [row for row in final_annotations if row.get("role") == "annotator"]
    adjudications = [row for row in final_annotations if row.get("role") == "adjudicator"]
    if len(independent) != 2 * len(candidate_ids):
        raise phase8d.Phase8DValidationError("synthetic independent coverage is incomplete")
    paths = {
        "packet_a": args.packet_a,
        "packet_b": args.packet_b,
        "responses_a": args.responses_a,
        "responses_b": args.responses_b,
        "adjudication_packet": args.adjudication_packet,
        "adjudication_responses": args.adjudication_responses,
        "final_annotations": args.final_annotations,
        "freeze_manifest": args.freeze_manifest,
    }
    manifest = {
        "schema_version": 1,
        "phase_id": SIMULATION_PHASE_ID,
        "generated_at": phase8d._timestamp(args.generated_at, "generated_at"),
        "decision_method": "salted_candidate_id_sha256_non_semantic",
        "external_model_calls": 0,
        "human_annotators": 0,
        "candidate_count": len(candidate_ids),
        "independent_annotation_count": len(independent),
        "adjudication_candidate_count": len(adjudications),
        "final_annotation_count": len(final_annotations),
        "real_human_gate_complete": False,
        "quality_claim_allowed": False,
        "freeze_trainable": False,
        "freeze_incomplete_gates": freeze_manifest["incomplete_gates"],
        "real_model_readiness": readiness,
        "artifacts": {
            name: {"path": str(path).replace("\\", "/"), "sha256": _artifact_hash(path)}
            for name, path in paths.items()
        },
        "manifest_sha256": "",
    }
    manifest["manifest_sha256"] = _sha256(
        {key: manifest[key] for key in sorted(manifest) if key != "manifest_sha256"}
    )
    _write_json(args.out, manifest)
    return {
        "status": "synthetic_only",
        "candidates": len(candidate_ids),
        "adjudicated": len(adjudications),
        "freeze_trainable": False,
        "real_model_ready": False,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-responses")
    generate.add_argument("--plan", type=Path, required=True)
    generate.add_argument("--pr-sources", type=Path, required=True)
    generate.add_argument("--candidate-sources", type=Path, required=True)
    generate.add_argument("--packet", type=Path, required=True)
    generate.add_argument("--created-at", required=True)
    generate.add_argument("--out", type=Path, required=True)
    generate.set_defaults(handler=_command_generate)

    manifest = subparsers.add_parser("write-manifest")
    manifest.add_argument("--config", type=Path, required=True)
    manifest.add_argument("--packet-a", type=Path, required=True)
    manifest.add_argument("--packet-b", type=Path, required=True)
    manifest.add_argument("--responses-a", type=Path, required=True)
    manifest.add_argument("--responses-b", type=Path, required=True)
    manifest.add_argument("--adjudication-packet", type=Path, required=True)
    manifest.add_argument("--adjudication-responses", type=Path, required=True)
    manifest.add_argument("--final-annotations", type=Path, required=True)
    manifest.add_argument("--freeze-manifest", type=Path, required=True)
    manifest.add_argument("--generated-at", required=True)
    manifest.add_argument("--out", type=Path, required=True)
    manifest.set_defaults(handler=_command_manifest)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ValueError, phase8d.Phase8DValidationError, vc.CorpusValidationError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
