"""Bounded GLM-5.2 Finder executor for the Phase 8D real corpus.

The module is import-safe and offline-testable.  Network access occurs only in
the explicit ``run`` command after config, credential, queue, and all 29 raw
diff objects have passed their frozen checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Sequence

from openai import OpenAI

import verifier_corpus as vc
import verifier_phase8d as v8d
from code_review_agent.agent import (
    EXPLORE_TOOLS,
    MAX_STEPS,
    MAX_SUBMIT_ATTEMPTS,
    SUBMIT_TOOL,
    SYSTEM,
    build_review_input,
    validate_review,
)
from code_review_agent.agentloop import run_submit_loop
from code_review_agent.context import parse_diff
from code_review_agent.findings import dedup_union, split_by_scope


FINDER_LIMITATION = """

Phase 8D evidence boundary: only the frozen unified diff is available. The
repository checkout and unchanged full files are intentionally unavailable.
Tool responses can only confirm that limitation; never infer repository-wide
absence from them. Base every finding on concrete lines visible in the diff,
and describe uncertainty honestly.
"""
FINDER_SYSTEM = SYSTEM + FINDER_LIMITATION
PROMPT_SHA256 = hashlib.sha256(FINDER_SYSTEM.encode("utf-8")).hexdigest()
MAX_TOKENS_PER_CALL = 8000
KEY_ENVS = ("GLM_API_KEY", "ZHIPUAI_API_KEY")


class Phase8DExecutionError(RuntimeError):
    """Raised when the bounded executor cannot proceed safely."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_diff_path(raw_root: Path, object_key: str) -> Path:
    key = PurePosixPath(object_key)
    if key.is_absolute() or key.parts[:1] != ("objects",) or ".." in key.parts:
        raise Phase8DExecutionError("diff object key escapes the authorized objects directory")
    root = raw_root.resolve()
    path = (root / Path(*key.parts)).resolve()
    objects = (root / "objects").resolve()
    if not path.is_relative_to(objects):
        raise Phase8DExecutionError("resolved diff path escapes the authorized objects directory")
    return path


def attest_diff_objects(
    plan: dict[str, Any],
    pr_sources: Sequence[dict[str, Any]],
    queue_rows: Sequence[dict[str, Any]],
    raw_root: Path,
) -> dict[str, Any]:
    """Read and hash exactly the 29 authorized objects without returning content."""

    sources = vc.validate_pr_sources(pr_sources, plan)
    queue = vc.validate_finder_queue(queue_rows, sources)
    source_by_id = {row["source_id"]: row for row in sources}
    attestations: list[dict[str, Any]] = []
    for row in sorted(queue, key=lambda item: item["queue_id"]):
        source = source_by_id[row["source_id"]]
        path = _safe_diff_path(raw_root, row["diff_object_key"])
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise Phase8DExecutionError(f"cannot read authorized diff object: {path.name}") from exc
        digest = hashlib.sha256(payload).hexdigest()
        if digest != row["diff_sha256"] or digest != source["diff_sha256"]:
            raise Phase8DExecutionError(f"diff SHA-256 mismatch for {row['queue_id']}")
        if len(payload) != source["diff_bytes"]:
            raise Phase8DExecutionError(f"diff byte count mismatch for {row['queue_id']}")
        attestations.append(
            {
                "queue_id": row["queue_id"],
                "diff_sha256": digest,
                "diff_bytes": len(payload),
            }
        )
    if len(attestations) != 29:
        raise Phase8DExecutionError("authorized raw-diff set must contain exactly 29 objects")
    return {
        "objects": len(attestations),
        "total_bytes": sum(row["diff_bytes"] for row in attestations),
        "attestation_sha256": hashlib.sha256(_canonical_bytes(attestations)).hexdigest(),
    }


@dataclass
class BudgetLedger:
    config: dict[str, Any]
    logical_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cny: float = 0.0
    response_metadata: list[dict[str, Any]] = field(default_factory=list)

    @property
    def finder(self) -> dict[str, Any]:
        return self.config["finder"]

    def reserve(self, request: dict[str, Any]) -> None:
        if self.logical_calls >= self.finder["max_calls"]:
            raise Phase8DExecutionError("logical call ceiling exhausted")
        request_bytes = len(_canonical_bytes(request))
        if self.input_tokens + request_bytes > self.finder["max_input_tokens"]:
            raise Phase8DExecutionError("conservative input-token ceiling exhausted")
        requested_output = request.get("max_tokens")
        if not isinstance(requested_output, int) or requested_output < 1:
            raise Phase8DExecutionError("every request must have a positive max_tokens cap")
        if self.output_tokens + requested_output > self.finder["max_output_tokens"]:
            raise Phase8DExecutionError("reserved output-token ceiling exhausted")
        worst_increment = (
            request_bytes * self.finder["uncached_input_cny_per_million"]
            + requested_output * self.finder["output_cny_per_million"]
        ) / 1_000_000
        if self.cost_cny + worst_increment > self.finder["max_cost_cny"]:
            raise Phase8DExecutionError("reserved CNY ceiling exhausted")
        self.logical_calls += 1

    def record(self, response: Any) -> None:
        usage = response.usage
        prompt_tokens = int(usage.prompt_tokens)
        completion_tokens = int(usage.completion_tokens)
        if prompt_tokens < 0 or completion_tokens < 0:
            raise Phase8DExecutionError("provider returned invalid token usage")
        self.input_tokens += prompt_tokens
        self.output_tokens += completion_tokens
        self.cost_cny += (
            prompt_tokens * self.finder["uncached_input_cny_per_million"]
            + completion_tokens * self.finder["output_cny_per_million"]
        ) / 1_000_000
        if (
            self.input_tokens > self.finder["max_input_tokens"]
            or self.output_tokens > self.finder["max_output_tokens"]
            or self.cost_cny > self.finder["max_cost_cny"]
        ):
            raise Phase8DExecutionError("provider usage crossed an authorized ceiling")
        self.response_metadata.append(
            {
                "request_id": getattr(response, "id", None),
                "response_model": getattr(response, "model", None),
                "system_fingerprint": getattr(response, "system_fingerprint", None),
                "received_at": _utc_now(),
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
            }
        )


class _BudgetedCompletions:
    def __init__(self, inner: Any, ledger: BudgetLedger):
        self._inner = inner
        self._ledger = ledger

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("model") != self._ledger.finder["model"]:
            raise Phase8DExecutionError("request model differs from glm-5.2")
        kwargs["stream"] = False
        kwargs["tool_choice"] = "auto"
        extra_body = dict(kwargs.pop("extra_body", {}) or {})
        extra_body.update(
            {
                "thinking": {"type": "disabled"},
                "reasoning_effort": "none",
            }
        )
        kwargs["extra_body"] = extra_body
        self._ledger.reserve(kwargs)
        response = self._inner.create(**kwargs)
        self._ledger.record(response)
        return response


class BudgetedClient:
    """Minimal OpenAI client proxy that enforces the frozen request options."""

    def __init__(self, client: Any, ledger: BudgetLedger):
        self.chat = SimpleNamespace(
            completions=_BudgetedCompletions(client.chat.completions, ledger)
        )


class FrozenDiffSession:
    """Honest tool boundary for a corpus that contains diffs, not checkouts."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, name: str, arguments_json: str) -> str:
        del arguments_json
        self.calls.append(name)
        return (
            "Unavailable in Phase 8D: only the frozen unified diff in the user message "
            "is authorized. No full repository checkout is present. Do not infer that a "
            "file, symbol, caller, or lint issue is absent from this bounded response."
        )


def _parse_submit(raw: str) -> tuple[Any, list[str]]:
    try:
        review = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"malformed JSON in submit_review arguments: {exc}"]
    return review, validate_review(review)


def run_finder_pass(
    client: Any,
    model: str,
    diff_text: str,
    temperature: float,
) -> tuple[dict[str, Any], int, list[str]]:
    session = FrozenDiffSession()
    messages = [
        {"role": "system", "content": FINDER_SYSTEM},
        {"role": "user", "content": build_review_input(diff_text, Path("."), use_context=False)},
    ]
    result = run_submit_loop(
        client,
        model,
        messages,
        explore_tools=EXPLORE_TOOLS,
        submit_tool=SUBMIT_TOOL,
        parse=_parse_submit,
        session=session,
        max_steps=MAX_STEPS,
        max_submit_attempts=MAX_SUBMIT_ATTEMPTS,
        max_tokens=MAX_TOKENS_PER_CALL,
        temperature=temperature,
        budget_msg="Step budget exhausted. Call submit_review NOW with the findings established from the frozen diff.",
        reject_msg=lambda problems: "Review rejected -- fix these problems and call submit_review again: "
        + "; ".join(problems),
        component="phase8d_finder",
        on_text_answer="raise",
    )
    if result.reason != "ok" or not isinstance(result.payload, dict):
        raise Phase8DExecutionError(f"Finder pass did not complete: {result.reason}")
    return result.payload, result.steps, session.calls


def _candidate_rows(
    source: dict[str, Any], diff_text: str, findings: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    changed_files, _ = parse_diff(diff_text)
    in_scope, _ = split_by_scope(list(findings), changed_files)
    rows: list[dict[str, Any]] = []
    for finding in in_scope:
        identity = hashlib.sha256(
            _canonical_bytes(
                {
                    "source_id": source["source_id"],
                    "file": finding["file"],
                    "line": finding["line"],
                    "issue": finding["issue"],
                }
            )
        ).hexdigest()[:24]
        candidate_text = f"{finding['issue']} Suggested correction: {finding['suggestion']}"
        row = vc.with_candidate_source_hashes(
            {
                "schema_version": 1,
                "candidate_id": f"real-finder-{identity}",
                "source_id": source["source_id"],
                "repository_id": source["repository_id"],
                "source_revision": source["merge_sha"],
                "pr_source_sha256": source["record_sha256"],
                "candidate_text": candidate_text,
                "evidence": [
                    {
                        "kind": "positive",
                        "path": finding["file"],
                        "line": finding["line"],
                        "summary": finding["issue"],
                    }
                ],
                "tool_summaries": [
                    {
                        "tool": "frozen_diff",
                        "status": "ok",
                        "summary": f"Inspected hash-bound unified diff for {source['source_id']}; full checkout unavailable.",
                    }
                ],
                "pair_id": source["source_id"],
                "language": "python",
                "severity": finding["severity"],
                "content_sha256": "",
                "candidate_source_sha256": "",
            }
        )
        rows.append(row)
    rows.sort(key=lambda row: row["candidate_id"])
    return rows


def _load_inputs(args: argparse.Namespace) -> tuple[Any, Any, Any, Any]:
    config = v8d.load_config(args.config)
    if config["finder"]["prompt_sha256"] != PROMPT_SHA256:
        raise Phase8DExecutionError("config prompt hash does not match this executor")
    plan = vc.load_plan(args.plan)
    sources = vc.load_pr_sources(args.pr_sources, plan)
    queue = vc.validate_finder_queue(vc._load_jsonl(args.queue), sources)
    return config, plan, sources, queue


def _make_client(config: dict[str, Any]) -> OpenAI:
    api_key = next((os.environ.get(name) for name in KEY_ENVS if os.environ.get(name)), None)
    if not api_key:
        raise Phase8DExecutionError("missing GLM_API_KEY or ZHIPUAI_API_KEY")
    return OpenAI(
        api_key=api_key,
        base_url=config["finder"]["base_url"],
        timeout=120.0,
        max_retries=2,
    )


def _trace_bytes(trace: dict[str, Any]) -> bytes:
    return json.dumps(trace, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"


def _error_category(exc: BaseException) -> str:
    if isinstance(exc, Phase8DExecutionError) and "ceiling" in str(exc):
        return "budget_exhausted"
    if isinstance(exc, Phase8DExecutionError):
        return "finder_protocol_error"
    return "provider_error"


def execute_queue(
    config: dict[str, Any],
    plan: dict[str, Any],
    sources: Sequence[dict[str, Any]],
    queue: Sequence[dict[str, Any]],
    raw_root: Path,
    trace_dir: Path,
    receipts_out: Path,
    candidates_out: Path,
    client: Any,
) -> dict[str, Any]:
    """Execute every queue item exactly once and write auditable artifacts."""

    v8d.validate_config(config)
    if config["finder"]["prompt_sha256"] != PROMPT_SHA256:
        raise Phase8DExecutionError("config prompt hash does not match this executor")
    if receipts_out.exists() or candidates_out.exists():
        raise Phase8DExecutionError("refusing to replace an existing Finder run artifact")
    if trace_dir.exists() and any(trace_dir.iterdir()):
        raise Phase8DExecutionError("refusing to mix a new run with existing raw traces")
    attest_diff_objects(plan, sources, queue, raw_root)
    source_by_id = {row["source_id"]: row for row in sources}
    ledger = BudgetLedger(config)
    bounded_client = BudgetedClient(client, ledger)
    receipts: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    trace_dir.mkdir(parents=True, exist_ok=True)
    docs = [
        "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2",
        "https://docs.bigmodel.cn/cn/guide/develop/openai/introduction",
        "https://docs.bigmodel.cn/cn/guide/start/migrate-to-glm-new",
    ]
    documentation_sha256 = hashlib.sha256(_canonical_bytes(docs)).hexdigest()
    config_sha256 = hashlib.sha256(_canonical_bytes(config)).hexdigest()

    for queue_row in sorted(queue, key=lambda row: row["queue_id"]):
        source = source_by_id[queue_row["source_id"]]
        started_at = _utc_now()
        before = (
            ledger.logical_calls,
            ledger.input_tokens,
            ledger.output_tokens,
            ledger.cost_cny,
            len(ledger.response_metadata),
        )
        item_candidates: list[dict[str, Any]] = []
        pass_trace: dict[str, Any] = {}
        error_category: str | None = None
        try:
            path = _safe_diff_path(raw_root, queue_row["diff_object_key"])
            diff_text = path.read_text(encoding="utf-8")
            anchor, anchor_steps, anchor_tools = run_finder_pass(
                bounded_client,
                config["finder"]["model"],
                diff_text,
                config["finder"]["anchor_temperature"],
            )
            pass_trace["anchor"] = {
                "status": "completed",
                "temperature": config["finder"]["anchor_temperature"],
                "steps": anchor_steps,
                "tool_calls": anchor_tools,
                "finding_count": len(anchor["findings"]),
            }
            sampler_findings: list[dict[str, Any]] = []
            try:
                sampler, sampler_steps, sampler_tools = run_finder_pass(
                    bounded_client,
                    config["finder"]["model"],
                    diff_text,
                    config["finder"]["sampling_temperature"],
                )
                sampler_findings = sampler["findings"]
                pass_trace["sampling"] = {
                    "status": "completed",
                    "temperature": config["finder"]["sampling_temperature"],
                    "steps": sampler_steps,
                    "tool_calls": sampler_tools,
                    "finding_count": len(sampler_findings),
                }
            except Exception as exc:
                pass_trace["sampling"] = {
                    "status": "degraded",
                    "temperature": config["finder"]["sampling_temperature"],
                    "error_category": _error_category(exc),
                }
            findings, duplicates = dedup_union(anchor["findings"], sampler_findings)
            pass_trace["deduplicated_findings"] = duplicates
            item_candidates = _candidate_rows(source, diff_text, findings)
            if len(item_candidates) > queue_row["max_candidates"]:
                raise Phase8DExecutionError("candidate ceiling exceeded")
            status = "completed" if item_candidates else "completed_zero_candidates"
        except Exception as exc:
            status = "failed"
            item_candidates = []
            error_category = _error_category(exc)
            pass_trace.setdefault(
                "anchor",
                {"status": "failed", "error_category": error_category},
            )

        finished_at = _utc_now()
        after_metadata = ledger.response_metadata[before[4] :]
        delete_after = (
            datetime.now(timezone.utc) + timedelta(days=config["retention"]["raw_trace_days"])
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        trace = {
            "schema_version": 1,
            "phase_id": config["phase_id"],
            "queue_id": queue_row["queue_id"],
            "source_id": queue_row["source_id"],
            "queue_sha256": queue_row["queue_sha256"],
            "pr_source_sha256": queue_row["pr_source_sha256"],
            "diff_sha256": queue_row["diff_sha256"],
            "config_sha256": config_sha256,
            "documentation_sha256": documentation_sha256,
            "prompt_sha256": PROMPT_SHA256,
            "requested_model": config["finder"]["model"],
            "request_options": {
                "anchor_temperature": config["finder"]["anchor_temperature"],
                "sampling_temperature": config["finder"]["sampling_temperature"],
                "thinking": {"type": "disabled"},
                "reasoning_effort": "none",
                "stream": False,
                "tool_choice": "auto",
            },
            "started_at": started_at,
            "finished_at": finished_at,
            "delete_after": delete_after,
            "passes": pass_trace,
            "responses": after_metadata,
            "status": status,
            "error_category": error_category,
        }
        raw_trace = _trace_bytes(trace)
        trace_path = trace_dir / f"{queue_row['queue_id']}.json"
        trace_path.write_bytes(raw_trace)
        trace_sha256 = hashlib.sha256(raw_trace).hexdigest()
        candidates.extend(item_candidates)
        candidate_ids = sorted(row["candidate_id"] for row in item_candidates)
        receipt = v8d.with_finder_run_hash(
            {
                "schema_version": 1,
                "run_id": f"glm52-{queue_row['queue_id']}",
                "queue_id": queue_row["queue_id"],
                "queue_sha256": queue_row["queue_sha256"],
                "source_id": queue_row["source_id"],
                "pr_source_sha256": queue_row["pr_source_sha256"],
                "diff_sha256": queue_row["diff_sha256"],
                "status": status,
                "candidate_ids": candidate_ids,
                "candidate_count": len(candidate_ids),
                "provider": config["finder"]["provider"],
                "model": config["finder"]["model"],
                "prompt_sha256": PROMPT_SHA256,
                "started_at": started_at,
                "finished_at": finished_at,
                "input_tokens": ledger.input_tokens - before[1],
                "output_tokens": ledger.output_tokens - before[2],
                "cost_cny": round(ledger.cost_cny - before[3], 6),
                "trace_sha256": trace_sha256,
                "error_category": error_category,
                "synthetic": False,
                "run_sha256": "",
            }
        )
        receipts.append(receipt)

    if len(_canonical_bytes(candidates)) > config["limits"]["max_sanitized_bytes"]:
        raise Phase8DExecutionError("sanitized candidate artifact exceeds its byte ceiling")
    vc.validate_candidate_sources(candidates, plan, sources) if candidates else None
    v8d.validate_finder_runs(receipts, config, plan, queue, sources, candidates)
    receipts_out.parent.mkdir(parents=True, exist_ok=True)
    candidates_out.parent.mkdir(parents=True, exist_ok=True)
    v8d._write_jsonl(receipts_out, receipts)
    v8d._write_jsonl(candidates_out, candidates)
    return {
        "status": "completed",
        "receipts": len(receipts),
        "completed": sum(row["status"] == "completed" for row in receipts),
        "completed_zero_candidates": sum(
            row["status"] == "completed_zero_candidates" for row in receipts
        ),
        "failed": sum(row["status"] == "failed" for row in receipts),
        "candidates": len(candidates),
        "logical_calls": ledger.logical_calls,
        "input_tokens": ledger.input_tokens,
        "output_tokens": ledger.output_tokens,
        "cost_cny": round(ledger.cost_cny, 6),
    }


def _command_attest(args: argparse.Namespace) -> dict[str, Any]:
    config, plan, sources, queue = _load_inputs(args)
    if not config["authorization"]["raw_diff_read"]:
        raise Phase8DExecutionError("raw diff read is not authorized")
    return attest_diff_objects(plan, sources, queue, args.raw_root)


def _command_run(args: argparse.Namespace) -> dict[str, Any]:
    config, plan, sources, queue = _load_inputs(args)
    if not config["authorization"]["provider_calls"]:
        raise Phase8DExecutionError("provider calls are not authorized")
    return execute_queue(
        config,
        plan,
        sources,
        queue,
        args.raw_root,
        args.trace_dir,
        args.receipts_out,
        args.candidates_out,
        _make_client(config),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the bounded Phase 8D GLM-5.2 Finder.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    attest = subparsers.add_parser("attest-inputs")
    attest.add_argument("--config", type=Path, required=True)
    attest.add_argument("--plan", type=Path, required=True)
    attest.add_argument("--pr-sources", type=Path, required=True)
    attest.add_argument("--queue", type=Path, required=True)
    attest.add_argument("--raw-root", type=Path, required=True)
    attest.set_defaults(handler=_command_attest)

    run = subparsers.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--pr-sources", type=Path, required=True)
    run.add_argument("--queue", type=Path, required=True)
    run.add_argument("--raw-root", type=Path, required=True)
    run.add_argument("--trace-dir", type=Path, required=True)
    run.add_argument("--receipts-out", type=Path, required=True)
    run.add_argument("--candidates-out", type=Path, required=True)
    run.set_defaults(handler=_command_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (Phase8DExecutionError, v8d.Phase8DValidationError, vc.CorpusValidationError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
