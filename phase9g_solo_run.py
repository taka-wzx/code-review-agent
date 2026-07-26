"""Offline materialization and hard gates for Phase 9G-Solo-Run v1.

The module may inspect local Git metadata and, only after deterministic selection,
read the five selected first-parent diffs. It never contacts GitHub or a model. The
paid execution command is intentionally absent until a replacement authorization,
endpoint, temperature profile, zero-retry runtime, and CNY tariff are hash-bound.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any, Mapping, NoReturn, Sequence

import phase9g_solo as solo


SCHEMA_VERSION = 1
RUN_PHASE_ID = "phase9g-solo-run-v1"
SOLO_ID = "phase9g-solo-run-v1-001"
SOURCE_COMMIT = "a79b77e9e7e3792dd46cea4d6415c18ddcc54bb4"
SELECTION_SEED = "4520d525e3673de85dda5c12144ed6cd32bf26da34e3892e518e7ed089e7f52f"
WINDOW_START = "2026-01-01T00:00:00Z"
WINDOW_END = "2026-07-26T00:00:00Z"
TARGET_PRS = 5
EXPECTED_AUTHORIZATION_ID = "phase9g-solo-run-v1-auth-002"
EXPECTED_AUTHORIZATION_SHA256 = (
    "365ba325a31645f40610c8bf9cf32b21e071fb7109163955384468084d2bcc89"
)
EXPECTED_RUNTIME_SHA256 = (
    "cee0b676c3c00f2570aab960ca506473fb9412d7ba2809e917083d57554660da"
)
EXPECTED_PROVIDER = "glm"
EXPECTED_MODEL = "glm-5.2"
APPROVED_AT = "2026-07-26T07:29:48Z"
EXPIRES_AT = "2026-08-25T07:29:48Z"
PR_ID_DOMAIN = b"phase9g-solo-opaque-pr-v1\0"
MAX_SECRET_FILE_BYTES = 4096
PR_PATTERNS = (
    re.compile(r"^Merge pull request #(\d+)\b"),
    re.compile(r"\(#(\d+)\)\s*$"),
)
POTENTIAL_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[^\s'\"]{12,}"),
    re.compile(rb"(?i)authorization:\s*bearer\s+[^\s]+"),
)

PUBLIC_RECEIPT_KEYS = {
    "schema_version",
    "phase_id",
    "evidence_type",
    "source_commit",
    "selection_seed",
    "window_start",
    "window_end",
    "candidate_prs",
    "selected_prs",
    "authorization_sha256",
    "participant_manifest_sha256",
    "repository_manifest_sha256",
    "selection_plan_sha256",
    "selection_log_sha256",
    "cohort_sha256",
    "private_artifact_index_sha256",
    "selected_diff_secret_scan_blocked",
    "paid_call_gate",
    "paid_call_blockers",
    "business_claim_allowed",
    "quality_claim_allowed",
    "formal_quality_status",
    "generated_at",
    "receipt_sha256",
}
TARIFF_KEYS = {
    "schema_version",
    "provider",
    "model",
    "endpoint_kind",
    "effective_at",
    "input_microcny_per_million_tokens",
    "output_microcny_per_million_tokens",
    "cached_input_microcny_per_million_tokens",
    "source_sha256",
    "tariff_sha256",
}


class RunValidationError(ValueError):
    """Stable, content-free Solo-Run gate failure."""


def _fail(message: str) -> NoReturn:
    raise RunValidationError(message)


def _expect_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{where} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], where: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        details = f" missing={missing}" if missing else ""
        if actual - expected:
            details += " unknown-keys-present"
        _fail(f"{where} has invalid keys.{details}")


def _canonical_timestamp(value: str, where: str) -> datetime:
    return solo.parse_timestamp(value, where)


def _utc_timestamp(value: str, where: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunValidationError(f"{where} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        _fail(f"{where} must include an offset")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_storage_roots(repo_root: Path, private_root: Path, public_receipt: Path) -> None:
    repo = repo_root.resolve(strict=True)
    private = private_root.resolve(strict=False)
    public = public_receipt.resolve(strict=False)
    if _is_within(private, repo) or private == repo:
        _fail("private evidence root must be outside the Git worktree")
    if not _is_within(public, repo):
        _fail("public receipt must be inside the Git worktree")
    if public.suffix != ".json":
        _fail("public receipt must be JSON")
    if private.exists():
        _fail("private evidence root already exists; evidence cannot be overwritten")
    if public.exists():
        _fail("public receipt already exists; evidence cannot be overwritten")


def initialize_auth_002(
    *,
    repo_root: Path,
    output_root: Path,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Write the approved private authorization without disclosing stable IDs."""

    repo = repo_root.resolve(strict=True)
    output = output_root.resolve(strict=False)
    if _is_within(output, repo) or output == repo:
        _fail("private authorization root must be outside the Git worktree")
    if output.exists():
        _fail("private authorization root already exists; authorization cannot be overwritten")
    env = os.environ if environment is None else environment
    participant_id = env.get("PHASE9G_SOLO_PARTICIPANT_ID")
    repository_id = env.get("PHASE9G_SOLO_REPOSITORY_ID")
    approved_by = env.get("PHASE9G_SOLO_APPROVER_ID")
    if not participant_id or not repository_id or not approved_by:
        _fail("private stable-ID environment is incomplete")
    runtime_config = {
        "schema_version": 1,
        "provider": EXPECTED_PROVIDER,
        "exact_model_snapshot": EXPECTED_MODEL,
        "temperature": 0,
        "mode": "shadow",
    }
    if solo.sha256_value(runtime_config) != EXPECTED_RUNTIME_SHA256:
        _fail("built runtime configuration differs from the frozen runtime hash")
    authorization = solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": solo.PHASE_ID,
            "authorization_id": EXPECTED_AUTHORIZATION_ID,
            "participant_id": participant_id,
            "participant_confirmed_real": True,
            "repository_ids": [repository_id],
            "pr_count": TARGET_PRS,
            "selection_rule": solo.SELECTION_RULE,
            "mode": "shadow",
            "model": {
                "provider": EXPECTED_PROVIDER,
                "exact_model_snapshot": EXPECTED_MODEL,
                "runtime_config_sha256": EXPECTED_RUNTIME_SHA256,
                "temperature": 0,
                "max_logical_calls": 30,
                "max_http_attempts": 45,
                "max_input_tokens": 2_000_000,
                "max_output_tokens": 200_000,
                "max_cost_microcny": 20_000_000,
                "real_paid_calls": True,
                "read_raw_diff": True,
            },
            "retention": {
                "data_days": 30,
                "feedback_days": 30,
                "raw_trace_days": 7,
            },
            "external_operations": {
                "staging_deploy": False,
                "deployment_target": None,
                "real_github_api": False,
                "create_comments_or_checks": False,
                "github_publish": False,
            },
            "approved_by": approved_by,
            "approved_at": APPROVED_AT,
            "expires_at": EXPIRES_AT,
            "synthetic": False,
            "authorization_sha256": "",
        },
        "authorization_sha256",
    )
    validate_authorization(authorization)
    validate_runtime_config(runtime_config, authorization)
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "authorization.json", authorization)
    _write_json(output / "runtime-config.json", runtime_config)
    return {
        "valid": True,
        "authorization_id": EXPECTED_AUTHORIZATION_ID,
        "authorization_sha256": authorization["authorization_sha256"],
        "runtime_config_sha256": EXPECTED_RUNTIME_SHA256,
        "stable_ids_disclosed": False,
        "paid_call_gate": False,
    }


def _git_bytes(repo_root: Path, arguments: Sequence[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RunValidationError("local Git metadata operation failed") from exc
    return result.stdout


def _git_text(repo_root: Path, arguments: Sequence[str]) -> str:
    try:
        return _git_bytes(repo_root, arguments).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RunValidationError("local Git metadata is not UTF-8") from exc


@dataclass(frozen=True)
class Candidate:
    commit_sha: str
    merged_at: str
    subject: str
    pr_number: str
    opaque_pr_id: str
    rank_sha256: str


def opaque_pr_id(repository_id: str, pr_number: str) -> str:
    if not repository_id or not pr_number.isascii() or not pr_number.isdigit():
        _fail("opaque PR identity input is invalid")
    material = (
        PR_ID_DOMAIN
        + SOURCE_COMMIT.encode("ascii")
        + b"\n"
        + repository_id.encode("utf-8")
        + b"\n"
        + pr_number.encode("ascii")
    )
    return "pr-" + hashlib.sha256(material).hexdigest()[:32]


def _extract_pr_number(subject: str) -> str | None:
    matches = {
        match.group(1)
        for pattern in PR_PATTERNS
        if (match := pattern.search(subject)) is not None
    }
    if len(matches) > 1:
        _fail("candidate metadata contains conflicting PR identities")
    return next(iter(matches), None)


def collect_candidates(repo_root: Path, repository_id: str) -> tuple[list[Candidate], int]:
    if _git_text(repo_root, ["rev-parse", "origin/master"]) != SOURCE_COMMIT:
        _fail("origin/master differs from the frozen selection source commit")
    raw = _git_text(
        repo_root,
        [
            "log",
            "origin/master",
            "--first-parent",
            f"--since={WINDOW_START}",
            "--until=2026-07-25T23:59:59Z",
            "--format=%H%x1f%cI%x1f%s%x1e",
        ],
    )
    records = [record for record in raw.split("\x1e") if record.strip()]
    candidates: list[Candidate] = []
    seen_numbers: set[str] = set()
    for record in records:
        parts = record.strip().split("\x1f", 2)
        if len(parts) != 3:
            _fail("candidate metadata record is malformed")
        commit_sha, merged_at_raw, subject = parts
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            _fail("candidate commit identity is malformed")
        pr_number = _extract_pr_number(subject)
        if pr_number is None:
            continue
        if pr_number in seen_numbers:
            _fail("candidate metadata repeats a PR identity")
        seen_numbers.add(pr_number)
        merged_at = _utc_timestamp(merged_at_raw, "candidate merged_at")
        merged = _canonical_timestamp(merged_at, "candidate merged_at")
        if not (
            _canonical_timestamp(WINDOW_START, "window start")
            <= merged
            < _canonical_timestamp(WINDOW_END, "window end")
        ):
            _fail("candidate falls outside the frozen window")
        pr_id = opaque_pr_id(repository_id, pr_number)
        candidates.append(
            Candidate(
                commit_sha=commit_sha,
                merged_at=merged_at,
                subject=subject,
                pr_number=pr_number,
                opaque_pr_id=pr_id,
                rank_sha256=solo.selection_rank(SELECTION_SEED, pr_id),
            )
        )
    if len(candidates) < TARGET_PRS:
        _fail("candidate ledger has fewer PRs than the frozen target")
    candidates.sort(key=lambda candidate: (candidate.merged_at, candidate.commit_sha))
    return candidates, len(records)


def _selected_candidates(candidates: Sequence[Candidate]) -> set[str]:
    ranked = sorted(
        candidates,
        key=lambda candidate: (candidate.rank_sha256, candidate.opaque_pr_id),
    )
    return {candidate.opaque_pr_id for candidate in ranked[:TARGET_PRS]}


def _potential_secret_count(diff_bytes: bytes) -> int:
    return sum(bool(pattern.search(diff_bytes)) for pattern in POTENTIAL_SECRET_PATTERNS)


def _paid_call_blockers(blocked_diffs: int = 0) -> list[str]:
    blockers = [
        "auth_003_missing",
        "endpoint_kind_missing",
        "tariff_missing",
        "temperature_profile_mismatch",
        "sdk_retry_policy_mismatch",
    ]
    if blocked_diffs:
        blockers.append("selected_diff_secret_scan_hit")
    return blockers


def validate_authorization(raw: Any) -> dict[str, Any]:
    authorization = solo.validate_authorization(raw)
    if authorization["authorization_id"] != EXPECTED_AUTHORIZATION_ID:
        _fail("authorization ID is not the approved Solo-Run authorization")
    if authorization["authorization_sha256"] != EXPECTED_AUTHORIZATION_SHA256:
        _fail("authorization hash is not the approved Solo-Run authorization")
    if authorization["model"]["provider"] != EXPECTED_PROVIDER:
        _fail("authorization provider differs from the frozen provider")
    if authorization["model"]["exact_model_snapshot"] != EXPECTED_MODEL:
        _fail("authorization model differs from the frozen model")
    if authorization["model"]["runtime_config_sha256"] != EXPECTED_RUNTIME_SHA256:
        _fail("authorization runtime hash differs from the frozen runtime")
    if authorization["pr_count"] != TARGET_PRS:
        _fail("authorization PR target differs from the frozen target")
    return authorization


def validate_runtime_config(raw: Any, authorization: Mapping[str, Any]) -> dict[str, Any]:
    config = _expect_dict(raw, "runtime_config")
    expected_keys = {
        "schema_version",
        "provider",
        "exact_model_snapshot",
        "temperature",
        "mode",
    }
    _exact_keys(config, expected_keys, "runtime_config")
    if config["schema_version"] != 1:
        _fail("runtime config schema is invalid")
    if solo.sha256_value(config) != authorization["model"]["runtime_config_sha256"]:
        _fail("runtime config hash differs from authorization")
    for key in ("provider", "exact_model_snapshot", "temperature", "mode"):
        if config[key] != (
            authorization["model"][key]
            if key in authorization["model"]
            else authorization[key]
        ):
            _fail("runtime config differs from authorization")
    return config


def build_participant_manifest(
    authorization: Mapping[str, Any], *, generated_at: str
) -> dict[str, Any]:
    _canonical_timestamp(generated_at, "participant generated_at")
    return solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": solo.PHASE_ID,
            "solo_id": SOLO_ID,
            "identity_custodian_id": authorization["participant_id"],
            "consent_version": "phase9g-solo-consent-v1",
            "generated_at": generated_at,
            "participants": [
                {
                    "participant_id": authorization["participant_id"],
                    "confirmed_real": True,
                    "role": "developer",
                    "consented_at": authorization["approved_at"],
                    "consent_expires_at": authorization["expires_at"],
                    "consent_scope": ["exploratory_feedback", "review_time"],
                    "repository_ids": list(authorization["repository_ids"]),
                    "feedback_retention_days": authorization["retention"]["feedback_days"],
                    "withdrawal_acknowledged": True,
                }
            ],
            "synthetic": False,
            "manifest_sha256": "",
        },
        "manifest_sha256",
    )


def build_repository_manifest(
    authorization: Mapping[str, Any], *, locator_sha256: str, generated_at: str
) -> dict[str, Any]:
    solo._expect_sha(locator_sha256, "repository locator hash")
    repository = solo.with_artifact_hash(
        {
            "repository_id": authorization["repository_ids"][0],
            "locator_sha256": locator_sha256,
            "authorized_by": authorization["approved_by"],
            "authorized_at": authorization["approved_at"],
            "authorization_expires_at": authorization["expires_at"],
            "raw_diff_read_authorized": True,
            "real_github_api_authorized": False,
            "publish_mode": "shadow",
            "publication_authorized": False,
            "data_retention_days": authorization["retention"]["data_days"],
            "repository_sha256": "",
        },
        "repository_sha256",
    )
    return solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": solo.PHASE_ID,
            "solo_id": SOLO_ID,
            "generated_at": generated_at,
            "repositories": [repository],
            "synthetic": False,
            "manifest_sha256": "",
        },
        "manifest_sha256",
    )


def build_selection_plan(authorization: Mapping[str, Any], *, generated_at: str) -> dict[str, Any]:
    return solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": solo.PHASE_ID,
            "solo_id": SOLO_ID,
            "seed": SELECTION_SEED,
            "seed_derivation": {
                "method": "sha256_source_commit_v1",
                "source_commit": SOURCE_COMMIT,
            },
            "selection_window": {"start": WINDOW_START, "end": WINDOW_END},
            "repository_ids": list(authorization["repository_ids"]),
            "target_prs": TARGET_PRS,
            "exclusion_reasons": [
                "outside_scope",
                "not_reproducible",
                "authorization_missing",
            ],
            "generated_at": generated_at,
            "synthetic": False,
            "plan_sha256": "",
        },
        "plan_sha256",
    )


def _selection_rows_and_private_map(
    repo_root: Path,
    candidates: Sequence[Candidate],
    *,
    repository_id: str,
    private_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    selected = _selected_candidates(candidates)
    rows: list[dict[str, Any]] = []
    private_map: list[dict[str, Any]] = []
    blocked_diffs = 0
    diff_root = private_root / "selected-diffs"
    diff_root.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        is_selected = candidate.opaque_pr_id in selected
        snapshot_sha256: str | None = None
        diff_sha256: str | None = None
        secret_findings = 0
        if is_selected:
            commit_object = _git_bytes(
                repo_root, ["cat-file", "commit", candidate.commit_sha]
            )
            diff_bytes = _git_bytes(
                repo_root,
                [
                    "diff",
                    f"{candidate.commit_sha}^1",
                    candidate.commit_sha,
                    "--binary",
                    "--no-ext-diff",
                    "--no-color",
                ],
            )
            snapshot_sha256 = hashlib.sha256(commit_object).hexdigest()
            diff_sha256 = hashlib.sha256(diff_bytes).hexdigest()
            secret_findings = _potential_secret_count(diff_bytes)
            blocked_diffs += int(secret_findings > 0)
            (diff_root / f"{candidate.opaque_pr_id}.diff").write_bytes(diff_bytes)
        row = solo.with_artifact_hash(
            {
                "schema_version": 1,
                "solo_id": SOLO_ID,
                "repository_id": repository_id,
                "pr_id": candidate.opaque_pr_id,
                "merged_at": candidate.merged_at,
                "eligible": True,
                "exclusion_reason": None,
                "selected": is_selected,
                "rank_sha256": candidate.rank_sha256,
                "snapshot_sha256": snapshot_sha256,
                "diff_sha256": diff_sha256,
                "synthetic": False,
                "row_sha256": "",
            },
            "row_sha256",
        )
        rows.append(row)
        private_map.append(
            {
                "opaque_pr_id": candidate.opaque_pr_id,
                "commit_sha": candidate.commit_sha,
                "pr_number": candidate.pr_number,
                "merged_at": candidate.merged_at,
                "subject": candidate.subject,
                "selected": is_selected,
                "rank_sha256": candidate.rank_sha256,
                "snapshot_sha256": snapshot_sha256,
                "diff_sha256": diff_sha256,
                "potential_secret_findings": secret_findings,
            }
        )
    return rows, private_map, blocked_diffs


def _private_index(artifacts: Mapping[str, Any]) -> dict[str, Any]:
    entries = [
        {"artifact": name, "sha256": solo.sha256_value(value)}
        for name, value in sorted(artifacts.items())
    ]
    return {
        "schema_version": 1,
        "phase_id": RUN_PHASE_ID,
        "entries": entries,
        "index_sha256": solo.sha256_value(entries),
    }


def materialize_selection(
    *,
    repo_root: Path,
    private_root: Path,
    public_receipt_path: Path,
    authorization_raw: Any,
    runtime_config_raw: Any,
    generated_at: str,
) -> dict[str, Any]:
    _validate_storage_roots(repo_root, private_root, public_receipt_path)
    authorization = validate_authorization(authorization_raw)
    validate_runtime_config(runtime_config_raw, authorization)
    readiness = solo.authorization_readiness(authorization, at=generated_at)
    if not readiness["scopes"]["real_exploratory_run"]:
        _fail("real exploratory authorization is not active")
    if not authorization["model"]["read_raw_diff"]:
        _fail("raw diff access is not authorized")
    if len(authorization["repository_ids"]) != 1:
        _fail("Solo-Run v1 requires exactly one repository")

    remote_locator = _git_text(repo_root, ["remote", "get-url", "origin"])
    locator_sha256 = hashlib.sha256(remote_locator.encode("utf-8")).hexdigest()
    participants = build_participant_manifest(authorization, generated_at=generated_at)
    repositories = build_repository_manifest(
        authorization,
        locator_sha256=locator_sha256,
        generated_at=generated_at,
    )
    solo.validate_participant_manifest(participants, authorization)
    solo.validate_repository_manifest(repositories, authorization)
    plan = build_selection_plan(authorization, generated_at=generated_at)
    solo.validate_selection_plan(plan, expected_source_commit=SOURCE_COMMIT)
    candidates, first_parent_commits = collect_candidates(
        repo_root, authorization["repository_ids"][0]
    )
    rows, private_map, blocked_diffs = _selection_rows_and_private_map(
        repo_root,
        candidates,
        repository_id=authorization["repository_ids"][0],
        private_root=private_root,
    )
    solo.validate_selection_log(rows, plan, repositories)
    cohort = solo.materialize_cohort(
        plan,
        rows,
        repositories,
        materialized_at=generated_at,
    )
    solo.validate_cohort(cohort, plan, rows, repositories)
    source_receipt = {
        "schema_version": 1,
        "source_commit": SOURCE_COMMIT,
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "first_parent_commits": first_parent_commits,
        "pr_candidates": len(candidates),
        "candidate_map_sha256": solo.sha256_value(private_map),
    }
    artifacts: dict[str, Any] = {
        "authorization": authorization,
        "runtime_config": runtime_config_raw,
        "participants": participants,
        "repositories": repositories,
        "selection_plan": plan,
        "selection_log": rows,
        "cohort": cohort,
        "candidate_map": private_map,
        "source_receipt": source_receipt,
    }
    private_index = _private_index(artifacts)
    private_root.mkdir(parents=True, exist_ok=True)
    _write_json(private_root / "authorization.json", authorization)
    _write_json(private_root / "runtime-config.json", runtime_config_raw)
    _write_json(private_root / "participants.json", participants)
    _write_json(private_root / "repositories.json", repositories)
    _write_json(private_root / "selection-plan.json", plan)
    _write_jsonl(private_root / "selection-log.jsonl", rows)
    _write_json(private_root / "cohort.json", cohort)
    _write_json(private_root / "candidate-map.private.json", private_map)
    _write_json(private_root / "source-receipt.private.json", source_receipt)
    _write_json(private_root / "artifact-index.private.json", private_index)

    receipt = solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "evidence_type": solo.EVIDENCE_TYPE,
            "source_commit": SOURCE_COMMIT,
            "selection_seed": SELECTION_SEED,
            "window_start": WINDOW_START,
            "window_end": WINDOW_END,
            "candidate_prs": len(candidates),
            "selected_prs": len(cohort["entries"]),
            "authorization_sha256": authorization["authorization_sha256"],
            "participant_manifest_sha256": participants["manifest_sha256"],
            "repository_manifest_sha256": repositories["manifest_sha256"],
            "selection_plan_sha256": plan["plan_sha256"],
            "selection_log_sha256": solo.sha256_value(rows),
            "cohort_sha256": cohort["cohort_sha256"],
            "private_artifact_index_sha256": private_index["index_sha256"],
            "selected_diff_secret_scan_blocked": blocked_diffs,
            "paid_call_gate": False,
            "paid_call_blockers": _paid_call_blockers(blocked_diffs),
            "business_claim_allowed": False,
            "quality_claim_allowed": False,
            "formal_quality_status": "incomplete",
            "generated_at": generated_at,
            "receipt_sha256": "",
        },
        "receipt_sha256",
    )
    _write_json(public_receipt_path, receipt)
    return {
        "valid": True,
        "candidate_prs": len(candidates),
        "selected_prs": len(cohort["entries"]),
        "selected_diff_secret_scan_blocked": blocked_diffs,
        "paid_call_gate": False,
        "public_receipt_sha256": receipt["receipt_sha256"],
    }


def validate_public_receipt(raw: Any) -> dict[str, Any]:
    receipt = _expect_dict(raw, "public_receipt")
    _exact_keys(receipt, PUBLIC_RECEIPT_KEYS, "public_receipt")
    if receipt["schema_version"] != 1 or receipt["phase_id"] != RUN_PHASE_ID:
        _fail("public receipt schema or phase is invalid")
    if receipt["evidence_type"] != solo.EVIDENCE_TYPE:
        _fail("public receipt evidence type is invalid")
    if receipt["source_commit"] != SOURCE_COMMIT or receipt["selection_seed"] != SELECTION_SEED:
        _fail("public receipt selection anchor is invalid")
    if receipt["window_start"] != WINDOW_START or receipt["window_end"] != WINDOW_END:
        _fail("public receipt selection window is invalid")
    if receipt["selected_prs"] != TARGET_PRS or receipt["candidate_prs"] < TARGET_PRS:
        _fail("public receipt selection denominator is invalid")
    if receipt["authorization_sha256"] != EXPECTED_AUTHORIZATION_SHA256:
        _fail("public receipt authorization hash is invalid")
    for key in (
        "participant_manifest_sha256",
        "repository_manifest_sha256",
        "selection_plan_sha256",
        "selection_log_sha256",
        "cohort_sha256",
        "private_artifact_index_sha256",
    ):
        solo._expect_sha(receipt[key], f"public_receipt.{key}")
    if receipt["paid_call_gate"] is not False:
        _fail("auth-002 public receipt cannot open the paid-call gate")
    blockers = receipt["paid_call_blockers"]
    expected_blockers = set(_paid_call_blockers(receipt["selected_diff_secret_scan_blocked"]))
    if not isinstance(blockers, list) or len(blockers) != len(set(blockers)):
        _fail("public receipt paid-call blockers are invalid")
    if set(blockers) != expected_blockers:
        _fail("public receipt paid-call blockers are invalid")
    if receipt["business_claim_allowed"] or receipt["quality_claim_allowed"]:
        _fail("public receipt cannot allow business or quality claims")
    if receipt["formal_quality_status"] != "incomplete":
        _fail("public receipt formal quality status must remain incomplete")
    _canonical_timestamp(receipt["generated_at"], "public receipt generated_at")
    solo.validate_artifact_hash(receipt, "receipt_sha256", "public_receipt")
    return receipt


def credential_preflight(
    authorization_raw: Any,
    runtime_config_raw: Any,
    *,
    environment: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    authorization = validate_authorization(authorization_raw)
    validate_runtime_config(runtime_config_raw, authorization)
    env = os.environ if environment is None else environment
    if env.get("LLM_PROVIDER") != EXPECTED_PROVIDER:
        _fail("runtime provider environment is not frozen")
    if env.get("LLM_MODEL") != EXPECTED_MODEL:
        _fail("runtime model environment is not frozen")
    glm_path = env.get("GLM_API_KEY_FILE")
    zhipu_path = env.get("ZHIPUAI_API_KEY_FILE")
    if glm_path and zhipu_path and Path(glm_path) != Path(zhipu_path):
        _fail("credential file source is ambiguous")
    path_value = glm_path or zhipu_path
    if not path_value:
        _fail("credential file source is missing")
    credential_path = Path(path_value).resolve(strict=False)
    if repo_root is not None and _is_within(
        credential_path, repo_root.resolve(strict=True)
    ):
        _fail("credential file must be outside the Git worktree")
    try:
        encoded = credential_path.read_bytes()
    except OSError as exc:
        raise RunValidationError("credential file is unavailable") from exc
    if not encoded or len(encoded) > MAX_SECRET_FILE_BYTES:
        _fail("credential file size is invalid")
    try:
        value = encoded.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RunValidationError("credential file encoding is invalid") from exc
    if not value:
        _fail("credential file is empty")
    return {
        "valid": True,
        "credential_source_ready": True,
        "provider": EXPECTED_PROVIDER,
        "model": EXPECTED_MODEL,
        "paid_call_gate": False,
        "paid_call_blockers": _paid_call_blockers(),
    }


@dataclass(frozen=True)
class BudgetLimits:
    logical_calls: int
    http_attempts: int
    input_tokens: int
    output_tokens: int
    cost_microcny: int


@dataclass(frozen=True)
class BudgetReservation:
    logical_calls: int
    http_attempts: int
    input_tokens: int
    output_tokens: int
    cost_microcny: int


class BudgetLedger:
    """Thread-safe, fail-before-side-effect cumulative budget ledger."""

    def __init__(self, limits: BudgetLimits) -> None:
        values = (
            limits.logical_calls,
            limits.http_attempts,
            limits.input_tokens,
            limits.output_tokens,
            limits.cost_microcny,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("budget limits must be non-negative integers")
        if limits.http_attempts < limits.logical_calls:
            raise ValueError("HTTP-attempt limit cannot be below logical-call limit")
        self._limits = limits
        self._used = BudgetReservation(0, 0, 0, 0, 0)
        self._lock = threading.Lock()

    def reserve(self, reservation: BudgetReservation) -> BudgetReservation:
        values = (
            reservation.logical_calls,
            reservation.http_attempts,
            reservation.input_tokens,
            reservation.output_tokens,
            reservation.cost_microcny,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("budget reservation must use non-negative integers")
        if reservation.http_attempts < reservation.logical_calls:
            raise ValueError("reserved HTTP attempts cannot be below logical calls")
        with self._lock:
            proposed = BudgetReservation(
                self._used.logical_calls + reservation.logical_calls,
                self._used.http_attempts + reservation.http_attempts,
                self._used.input_tokens + reservation.input_tokens,
                self._used.output_tokens + reservation.output_tokens,
                self._used.cost_microcny + reservation.cost_microcny,
            )
            if (
                proposed.logical_calls > self._limits.logical_calls
                or proposed.http_attempts > self._limits.http_attempts
                or proposed.input_tokens > self._limits.input_tokens
                or proposed.output_tokens > self._limits.output_tokens
                or proposed.cost_microcny > self._limits.cost_microcny
            ):
                raise RunValidationError("budget reservation would exceed a frozen ceiling")
            self._used = proposed
            return proposed

    def snapshot(self) -> BudgetReservation:
        with self._lock:
            return self._used


def validate_tariff(raw: Any) -> dict[str, Any]:
    tariff = _expect_dict(raw, "tariff")
    _exact_keys(tariff, TARIFF_KEYS, "tariff")
    if tariff["schema_version"] != 1:
        _fail("tariff schema is invalid")
    if tariff["provider"] != EXPECTED_PROVIDER or tariff["model"] != EXPECTED_MODEL:
        _fail("tariff provider/model differs from the frozen runtime")
    if tariff["endpoint_kind"] not in {"standard", "coding_plan"}:
        _fail("tariff endpoint kind is invalid")
    _canonical_timestamp(tariff["effective_at"], "tariff effective_at")
    for key in (
        "input_microcny_per_million_tokens",
        "output_microcny_per_million_tokens",
        "cached_input_microcny_per_million_tokens",
    ):
        value = tariff[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail("tariff rates must be non-negative integer micro-CNY")
    solo._expect_sha(tariff["source_sha256"], "tariff source_sha256")
    solo.validate_artifact_hash(tariff, "tariff_sha256", "tariff")
    return tariff


def reserve_cost_microcny(
    tariff: Mapping[str, Any], *, input_tokens: int, output_tokens: int, cached_tokens: int = 0
) -> int:
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (input_tokens, output_tokens, cached_tokens)
    ):
        raise ValueError("token reservations must be non-negative integers")
    if cached_tokens > input_tokens:
        raise ValueError("cached tokens cannot exceed input tokens")
    uncached = input_tokens - cached_tokens
    numerator = (
        uncached * tariff["input_microcny_per_million_tokens"]
        + cached_tokens * tariff["cached_input_microcny_per_million_tokens"]
        + output_tokens * tariff["output_microcny_per_million_tokens"]
    )
    return math.ceil(numerator / 1_000_000)


def validate_synthetic() -> dict[str, Any]:
    limits = BudgetLimits(2, 2, 100, 50, 1000)
    ledger = BudgetLedger(limits)
    ledger.reserve(BudgetReservation(1, 1, 40, 20, 300))
    ledger.reserve(BudgetReservation(1, 1, 60, 30, 700))
    blocked = False
    try:
        ledger.reserve(BudgetReservation(1, 1, 0, 0, 0))
    except RunValidationError:
        blocked = True
    return {
        "valid": True,
        "synthetic": True,
        "budget_boundary_blocked": blocked,
        "paid_call_gate": False,
        "business_claim_allowed": False,
        "quality_claim_allowed": False,
        "formal_quality_status": "incomplete",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize and validate Phase 9G-Solo-Run evidence without network calls"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("initialize-auth-002")
    initialize.add_argument("--repo-root", required=True)
    initialize.add_argument("--output-root", required=True)
    materialize = commands.add_parser("materialize-selection")
    materialize.add_argument("--repo-root", required=True)
    materialize.add_argument("--private-root", required=True)
    materialize.add_argument("--public-receipt", required=True)
    materialize.add_argument("--authorization", required=True)
    materialize.add_argument("--runtime-config", required=True)
    materialize.add_argument("--generated-at", required=True)
    validate_receipt = commands.add_parser("validate-public-receipt")
    validate_receipt.add_argument("--receipt", required=True)
    credential = commands.add_parser("preflight-credential")
    credential.add_argument("--authorization", required=True)
    credential.add_argument("--runtime-config", required=True)
    credential.add_argument("--repo-root", required=True)
    commands.add_parser("validate-synthetic")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "initialize-auth-002":
            result = initialize_auth_002(
                repo_root=Path(args.repo_root),
                output_root=Path(args.output_root),
            )
        elif args.command == "materialize-selection":
            result = materialize_selection(
                repo_root=Path(args.repo_root),
                private_root=Path(args.private_root),
                public_receipt_path=Path(args.public_receipt),
                authorization_raw=solo.load_json(args.authorization),
                runtime_config_raw=solo.load_json(args.runtime_config),
                generated_at=args.generated_at,
            )
        elif args.command == "validate-public-receipt":
            receipt = validate_public_receipt(solo.load_json(args.receipt))
            result = {
                "valid": True,
                "candidate_prs": receipt["candidate_prs"],
                "selected_prs": receipt["selected_prs"],
                "paid_call_gate": False,
            }
        elif args.command == "preflight-credential":
            result = credential_preflight(
                solo.load_json(args.authorization),
                solo.load_json(args.runtime_config),
                repo_root=Path(args.repo_root),
            )
        else:
            result = validate_synthetic()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except (RunValidationError, solo.ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
