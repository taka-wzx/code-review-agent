"""Materialization and hard gates for Phase 9G-Solo-Run v1.

The original auth-003 path is immutable and reads only its locally selected diffs.
The auth-004 path is a separate, fail-closed alternative that anonymously acquires
an exact public Git snapshot and sends only deterministically selected public diffs.
Neither path may publish, deploy, call GitHub's API, or allow business/quality claims.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping, NoReturn, Sequence

from openai import OpenAI

import phase9g_solo as solo
import code_review_agent.agent as review_agent
from code_review_agent.redaction import contains_forbidden_content, sanitize_value


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
AUTH3_ID = "phase9g-solo-run-v1-auth-003"
EXPECTED_AUTH3_AUTHORIZATION_SHA256 = (
    "bce0e6cf8bdaa5fc0153c71a167a20eeb26a6d24d370226f77b054c519306082"
)
AUTH4_ID = "phase9g-solo-run-v1-auth-004"
STANDARD_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
EXECUTOR_VERSION = "phase9g-solo-executor-v1"
AUTH4_EXECUTOR_VERSION = "phase9g-solo-public-executor-v1"
PER_CALL_MAX_OUTPUT_TOKENS = 2048
REQUEST_TIMEOUT_SECONDS = 120
AUTH3_LIMITS = {
    "max_logical_calls": 96,
    "max_http_attempts": 96,
    "max_input_tokens": 1_750_000,
    "max_output_tokens": 200_000,
    "max_cost_microcny": 20_000_000,
}
AUTH3_TEMPERATURE_PROFILE = {
    "finder_anchor": 0.01,
    "finder_sampler": 0.70,
    "verifier_a": 0.01,
    "verifier_b": 0.01,
}
AUTH3_TARIFF_RATES = {
    "input_microcny_per_million_tokens": 8_000_000,
    "output_microcny_per_million_tokens": 28_000_000,
    "cached_input_microcny_per_million_tokens": 2_000_000,
}
PR_ID_DOMAIN = b"phase9g-solo-opaque-pr-v1\0"
PUBLIC_PR_ID_DOMAIN = b"phase9g-solo-public-pr-v1\0"
AUTH4_PUBLIC_SOURCE_URL = "https://github.com/psf/black.git"
AUTH4_PUBLIC_SOURCE_BRANCH = "main"
AUTH4_PUBLIC_SOURCE_COMMIT = "db2e3e7b317b40685ba4618235a8388c7c6ea5e2"
AUTH4_PUBLIC_SELECTION_SEED = (
    "5e190bc14d84c2439e43e0560db7d250c4cd702cd42cf32c9746425078c8ad38"
)
AUTH4_PUBLIC_LICENSE = "MIT"
AUTH4_EXPECTED_CANDIDATES = 180
AUTH4_EXPECTED_BLOCKED = 0
AUTH4_EXPECTED_RUNNABLE = 5
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
AUTH3_RUNTIME_KEYS = {
    "schema_version",
    "executor_version",
    "executor_commit",
    "executor_source_sha256",
    "product_source_commit",
    "provider",
    "exact_model_snapshot",
    "endpoint_kind",
    "base_url",
    "temperature_profile",
    "sdk_max_retries",
    "per_call_max_output_tokens",
    "request_timeout_seconds",
    "review_timeout_seconds",
    "use_context",
    "use_verify",
    "tiebreak",
    "pr_execution",
    "selected_diff_policy",
    "max_runnable_prs",
    "selection_receipt_sha256",
    "cohort_sha256",
}
AUTH3_KEYS = {
    "schema_version",
    "phase_id",
    "authorization_id",
    "supersedes_authorization_sha256",
    "participant_id",
    "repository_ids",
    "approved_by",
    "approved_at",
    "expires_at",
    "provider",
    "exact_model_snapshot",
    "runtime_config_sha256",
    "tariff_sha256",
    "selection_receipt_sha256",
    "cohort_sha256",
    "temperature_profile",
    "sdk_max_retries",
    "max_logical_calls",
    "max_http_attempts",
    "max_input_tokens",
    "max_output_tokens",
    "max_cost_microcny",
    "real_paid_calls",
    "read_selected_raw_diff",
    "real_github_api",
    "github_publish",
    "staging_deploy",
    "selected_diff_policy",
    "blocked_selected_prs",
    "max_runnable_prs",
    "approval_statement_sha256",
    "authorization_sha256",
}
AUTH3_ATTESTATION_KEYS = {
    "schema_version",
    "phase_id",
    "authorization_id",
    "authorization_sha256",
    "runtime_config_sha256",
    "tariff_sha256",
    "selection_receipt_sha256",
    "cohort_sha256",
    "endpoint_kind",
    "provider",
    "exact_model_snapshot",
    "temperature_profile",
    "sdk_max_retries",
    "max_logical_calls",
    "max_http_attempts",
    "max_input_tokens",
    "max_output_tokens",
    "max_cost_microcny",
    "blocked_selected_prs",
    "max_runnable_prs",
    "authorization_complete",
    "paid_call_gate",
    "paid_call_blockers",
    "business_claim_allowed",
    "quality_claim_allowed",
    "formal_quality_status",
    "approved_at",
    "expires_at",
    "attestation_sha256",
}
AUTH4_PUBLIC_SOURCE_RECEIPT_KEYS = {
    "schema_version",
    "phase_id",
    "evidence_type",
    "authorization_id",
    "source_kind",
    "source_locator_sha256",
    "source_commit",
    "source_branch",
    "license_spdx",
    "license_sha256",
    "anonymous_clone",
    "credentials_disabled",
    "github_api_used",
    "private_workspace_diff_read",
    "selection_seed",
    "window_start",
    "window_end",
    "candidate_prs",
    "selected_prs",
    "selection_log_sha256",
    "cohort_sha256",
    "private_artifact_index_sha256",
    "selected_diff_secret_scan_blocked",
    "selected_diff_total_bytes",
    "paid_call_gate",
    "business_claim_allowed",
    "quality_claim_allowed",
    "formal_quality_status",
    "generated_at",
    "receipt_sha256",
}
AUTH4_RUNTIME_KEYS = AUTH3_RUNTIME_KEYS | {
    "public_candidate_input_only",
    "anonymous_public_git_read",
    "github_api_used",
    "private_workspace_diff_read",
    "public_source_locator_sha256",
    "public_source_commit",
}
AUTH4_KEYS = AUTH3_KEYS | {
    "public_candidate_input_only",
    "anonymous_public_git_read",
    "public_source_locator_sha256",
    "public_source_commit",
}
AUTH4_ATTESTATION_KEYS = AUTH3_ATTESTATION_KEYS | {
    "public_candidate_input_only",
    "anonymous_public_git_read",
    "github_api_used",
    "private_workspace_diff_read",
    "public_source_locator_sha256",
    "public_source_commit",
}
HEADLINE_RECEIPT_KEYS = {
    "schema_version",
    "phase_id",
    "solo_id",
    "run_id",
    "pr_id",
    "attempt_number",
    "headline",
    "authorization_sha256",
    "runtime_config_sha256",
    "temperature_profile_sha256",
    "status",
    "started_at",
    "completed_at",
    "logical_calls",
    "http_attempts",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cost_microcny",
    "actual_usage_known",
    "reserved_input_tokens",
    "reserved_output_tokens",
    "reserved_cost_microcny",
    "latency_seconds",
    "error_category",
    "feedback_eligible_finding_ids",
    "review_sha256",
    "raw_trace_sha256",
    "raw_trace_retain_until",
    "receipt_sha256",
}
PUBLIC_RUN_RECEIPT_KEYS = {
    "schema_version",
    "phase_id",
    "evidence_type",
    "authorization_sha256",
    "runtime_config_sha256",
    "tariff_sha256",
    "selection_receipt_sha256",
    "cohort_sha256",
    "selected_prs",
    "blocked_zero_call_headlines",
    "runnable_headlines",
    "headline_attempts",
    "headline_status_counts",
    "actual_usage",
    "reserved_budget",
    "actual_usage_known",
    "feedback_eligible_findings",
    "feedback_responses",
    "feedback_status",
    "business_claim_allowed",
    "quality_claim_allowed",
    "formal_quality_status",
    "model_quality_status",
    "generated_at",
    "private_run_index_sha256",
    "receipt_sha256",
}
OFFLINE_VALIDATION_KEYS = {
    "schema_version",
    "phase_id",
    "executor_commit",
    "executor_source_sha256",
    "runtime_config_sha256",
    "dedicated_tests_passed",
    "synthetic_gate_passed",
    "solo_bundle_passed",
    "ruff_passed",
    "mypy_passed",
    "scripts_verify_passed",
    "pip_check_passed",
    "diff_check_passed",
    "external_calls_made",
    "validated_at",
    "validation_sha256",
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


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    with path.open("ab") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


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


def _public_git_environment(home: Path) -> dict[str, str]:
    """Return an environment that cannot use ambient GitHub credentials."""

    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if upper.startswith(("GIT_", "GCM_", "GH_", "GITHUB_", "SSH_")):
            env.pop(key, None)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home),
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
    )
    return env


def _run_public_git(
    arguments: Sequence[str],
    *,
    home: Path,
    cwd: Path | None = None,
) -> bytes:
    command = [
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=",
        "-c",
        "http.extraHeader=",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=_public_git_environment(home),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=300,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RunValidationError("anonymous public Git operation failed") from exc
    return result.stdout


def _public_git_bytes(git_dir: Path, arguments: Sequence[str]) -> bytes:
    return _run_public_git(
        [f"--git-dir={git_dir}", *arguments],
        home=git_dir.parent / "empty-home",
    )


def _public_git_text(git_dir: Path, arguments: Sequence[str]) -> str:
    try:
        return _public_git_bytes(git_dir, arguments).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RunValidationError("anonymous public Git metadata is not UTF-8") from exc


def _clone_auth4_public_source(staging_root: Path) -> Path:
    home = staging_root / "empty-home"
    git_dir = staging_root / "public.git"
    home.mkdir(parents=True, exist_ok=False)
    _run_public_git(
        [
            "clone",
            "--bare",
            "--filter=blob:none",
            "--single-branch",
            "--branch",
            AUTH4_PUBLIC_SOURCE_BRANCH,
            AUTH4_PUBLIC_SOURCE_URL,
            str(git_dir),
        ],
        home=home,
        cwd=staging_root,
    )
    if _public_git_text(git_dir, ["symbolic-ref", "--short", "HEAD"]) != (
        AUTH4_PUBLIC_SOURCE_BRANCH
    ):
        _fail("anonymous public Git default branch differs from auth-004")
    _public_git_bytes(
        git_dir,
        ["cat-file", "-e", f"{AUTH4_PUBLIC_SOURCE_COMMIT}^{{commit}}"],
    )
    _public_git_bytes(
        git_dir,
        ["merge-base", "--is-ancestor", AUTH4_PUBLIC_SOURCE_COMMIT, "HEAD"],
    )
    return git_dir


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


def auth4_public_repository_id() -> str:
    locator_hash = hashlib.sha256(AUTH4_PUBLIC_SOURCE_URL.encode("ascii")).hexdigest()
    return "repo-public-" + locator_hash[:24]


def auth4_public_pr_id(pr_number: str) -> str:
    if not pr_number.isascii() or not pr_number.isdigit():
        _fail("public opaque PR identity input is invalid")
    material = (
        PUBLIC_PR_ID_DOMAIN
        + AUTH4_PUBLIC_SOURCE_COMMIT.encode("ascii")
        + b"\n"
        + auth4_public_repository_id().encode("ascii")
        + b"\n"
        + pr_number.encode("ascii")
    )
    return "pr-public-" + hashlib.sha256(material).hexdigest()[:32]


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


def collect_auth4_public_candidates(git_dir: Path) -> tuple[list[Candidate], int]:
    raw = _public_git_text(
        git_dir,
        [
            "log",
            AUTH4_PUBLIC_SOURCE_COMMIT,
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
            _fail("public candidate metadata record is malformed")
        commit_sha, merged_at_raw, subject = parts
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            _fail("public candidate commit identity is malformed")
        pr_number = _extract_pr_number(subject)
        if pr_number is None:
            continue
        if pr_number in seen_numbers:
            _fail("public candidate metadata repeats a PR identity")
        seen_numbers.add(pr_number)
        merged_at = _utc_timestamp(merged_at_raw, "public candidate merged_at")
        merged = _canonical_timestamp(merged_at, "public candidate merged_at")
        if not (
            _canonical_timestamp(WINDOW_START, "window start")
            <= merged
            < _canonical_timestamp(WINDOW_END, "window end")
        ):
            _fail("public candidate falls outside the frozen window")
        pr_id = auth4_public_pr_id(pr_number)
        candidates.append(
            Candidate(
                commit_sha=commit_sha,
                merged_at=merged_at,
                subject=subject,
                pr_number=pr_number,
                opaque_pr_id=pr_id,
                rank_sha256=solo.selection_rank(AUTH4_PUBLIC_SELECTION_SEED, pr_id),
            )
        )
    if len(candidates) != AUTH4_EXPECTED_CANDIDATES:
        _fail("public candidate denominator differs from the frozen auth-004 source")
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


def _auth4_public_rows_and_map(
    git_dir: Path,
    candidates: Sequence[Candidate],
    *,
    staging_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], int, int]:
    selected_ids = {
        candidate.opaque_pr_id
        for candidate in sorted(
            candidates,
            key=lambda candidate: (candidate.rank_sha256, candidate.opaque_pr_id),
        )[:TARGET_PRS]
    }
    rows: list[dict[str, Any]] = []
    private_map: list[dict[str, Any]] = []
    cohort_entries: list[dict[str, Any]] = []
    blocked = 0
    total_bytes = 0
    diff_root = staging_root / "selected-diffs"
    diff_root.mkdir(parents=True, exist_ok=False)
    for candidate in candidates:
        selected = candidate.opaque_pr_id in selected_ids
        snapshot_sha256: str | None = None
        diff_sha256: str | None = None
        secret_findings = 0
        diff_bytes_count = 0
        if selected:
            commit_bytes = _public_git_bytes(
                git_dir, ["cat-file", "commit", candidate.commit_sha]
            )
            diff_bytes = _public_git_bytes(
                git_dir,
                [
                    "diff",
                    f"{candidate.commit_sha}^1",
                    candidate.commit_sha,
                    "--binary",
                    "--no-ext-diff",
                    "--no-color",
                ],
            )
            try:
                diff_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RunValidationError("selected public diff is not UTF-8") from exc
            snapshot_sha256 = hashlib.sha256(commit_bytes).hexdigest()
            diff_sha256 = hashlib.sha256(diff_bytes).hexdigest()
            secret_findings = _potential_secret_count(diff_bytes)
            diff_bytes_count = len(diff_bytes)
            blocked += int(secret_findings > 0)
            total_bytes += diff_bytes_count
            (diff_root / f"{candidate.opaque_pr_id}.diff").write_bytes(diff_bytes)
            cohort_entries.append(
                {
                    "pr_id": candidate.opaque_pr_id,
                    "snapshot_sha256": snapshot_sha256,
                    "diff_sha256": diff_sha256,
                    "synthetic": False,
                }
            )
        rows.append(
            solo.with_artifact_hash(
                {
                    "schema_version": 1,
                    "phase_id": RUN_PHASE_ID,
                    "authorization_id": AUTH4_ID,
                    "pr_id": candidate.opaque_pr_id,
                    "merged_at": candidate.merged_at,
                    "eligible": True,
                    "selected": selected,
                    "rank_sha256": candidate.rank_sha256,
                    "snapshot_sha256": snapshot_sha256,
                    "diff_sha256": diff_sha256,
                    "synthetic": False,
                    "row_sha256": "",
                },
                "row_sha256",
            )
        )
        private_map.append(
            {
                "opaque_pr_id": candidate.opaque_pr_id,
                "commit_sha": candidate.commit_sha,
                "pr_number": candidate.pr_number,
                "merged_at": candidate.merged_at,
                "subject": candidate.subject,
                "selected": selected,
                "rank_sha256": candidate.rank_sha256,
                "snapshot_sha256": snapshot_sha256,
                "diff_sha256": diff_sha256,
                "potential_secret_findings": secret_findings,
                "diff_bytes": diff_bytes_count,
            }
        )
    cohort = solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "authorization_id": AUTH4_ID,
            "solo_id": SOLO_ID,
            "repository_id": auth4_public_repository_id(),
            "source_commit": AUTH4_PUBLIC_SOURCE_COMMIT,
            "entries": sorted(cohort_entries, key=lambda entry: entry["pr_id"]),
            "synthetic": False,
            "cohort_sha256": "",
        },
        "cohort_sha256",
    )
    if len(cohort_entries) != TARGET_PRS:
        _fail("auth-004 public selection did not produce five PRs")
    return rows, private_map, cohort, blocked, total_bytes


def materialize_auth4_public_source(
    *,
    repo_root: Path,
    evidence_root: Path,
    public_receipt_path: Path,
    generated_at: str,
) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    evidence = evidence_root.resolve(strict=True)
    public_receipt = public_receipt_path.resolve(strict=False)
    if _is_within(evidence, repo) or evidence == repo:
        _fail("auth-004 public evidence root must be outside the Git worktree")
    if not _is_within(public_receipt, repo):
        _fail("auth-004 public source receipt must be inside the Git worktree")
    source_root = evidence / "auth-004-public-source"
    staging_root = evidence / "auth-004-public-source.initializing"
    if source_root.exists() or staging_root.exists() or public_receipt.exists():
        _fail("auth-004 public source evidence already exists and cannot be overwritten")
    generated = _canonical_timestamp(generated_at, "auth-004 source generated_at")
    if not (
        _canonical_timestamp(APPROVED_AT, "auth-002 approved_at")
        <= generated
        < _canonical_timestamp(EXPIRES_AT, "auth-002 expires_at")
    ):
        _fail("auth-004 public source materialization is outside the inherited window")

    git_dir = _clone_auth4_public_source(staging_root)
    license_bytes = _public_git_bytes(
        git_dir, ["show", f"{AUTH4_PUBLIC_SOURCE_COMMIT}:LICENSE"]
    )
    try:
        license_text = license_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunValidationError("public source license is not UTF-8") from exc
    if "MIT License" not in license_text or "Permission is hereby granted" not in license_text:
        _fail("public source license differs from the frozen permissive license")
    candidates, first_parent_commits = collect_auth4_public_candidates(git_dir)
    rows, private_map, cohort, blocked, total_bytes = _auth4_public_rows_and_map(
        git_dir,
        candidates,
        staging_root=staging_root,
    )
    if blocked != AUTH4_EXPECTED_BLOCKED:
        _fail("auth-004 selected public diff scan differs from the frozen probe")
    source_proof = {
        "schema_version": 1,
        "authorization_id": AUTH4_ID,
        "source_url": AUTH4_PUBLIC_SOURCE_URL,
        "source_branch": AUTH4_PUBLIC_SOURCE_BRANCH,
        "source_commit": AUTH4_PUBLIC_SOURCE_COMMIT,
        "source_locator_sha256": hashlib.sha256(
            AUTH4_PUBLIC_SOURCE_URL.encode("ascii")
        ).hexdigest(),
        "anonymous_clone": True,
        "credentials_disabled": True,
        "github_api_used": False,
        "private_workspace_diff_read": False,
        "license_spdx": AUTH4_PUBLIC_LICENSE,
        "license_sha256": hashlib.sha256(license_bytes).hexdigest(),
        "first_parent_commits": first_parent_commits,
        "candidate_prs": len(candidates),
        "candidate_map_sha256": solo.sha256_value(private_map),
        "generated_at": generated_at,
    }
    (staging_root / "license.public.txt").write_bytes(license_bytes)
    artifacts: dict[str, Any] = {
        "source_proof": source_proof,
        "selection_log": rows,
        "candidate_map": private_map,
        "cohort": cohort,
    }
    private_index = _private_index(artifacts)
    _write_json(staging_root / "source-proof.private.json", source_proof)
    _write_jsonl(staging_root / "selection-log.jsonl", rows)
    _write_json(staging_root / "candidate-map.private.json", private_map)
    _write_json(staging_root / "cohort.json", cohort)
    _write_json(staging_root / "artifact-index.private.json", private_index)
    os.replace(staging_root, source_root)

    receipt = solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "evidence_type": solo.EVIDENCE_TYPE,
            "authorization_id": AUTH4_ID,
            "source_kind": "anonymous_public_git_exact_commit",
            "source_locator_sha256": source_proof["source_locator_sha256"],
            "source_commit": AUTH4_PUBLIC_SOURCE_COMMIT,
            "source_branch": AUTH4_PUBLIC_SOURCE_BRANCH,
            "license_spdx": AUTH4_PUBLIC_LICENSE,
            "license_sha256": source_proof["license_sha256"],
            "anonymous_clone": True,
            "credentials_disabled": True,
            "github_api_used": False,
            "private_workspace_diff_read": False,
            "selection_seed": AUTH4_PUBLIC_SELECTION_SEED,
            "window_start": WINDOW_START,
            "window_end": WINDOW_END,
            "candidate_prs": len(candidates),
            "selected_prs": TARGET_PRS,
            "selection_log_sha256": solo.sha256_value(rows),
            "cohort_sha256": cohort["cohort_sha256"],
            "private_artifact_index_sha256": private_index["index_sha256"],
            "selected_diff_secret_scan_blocked": blocked,
            "selected_diff_total_bytes": total_bytes,
            "paid_call_gate": False,
            "business_claim_allowed": False,
            "quality_claim_allowed": False,
            "formal_quality_status": "incomplete",
            "generated_at": generated_at,
            "receipt_sha256": "",
        },
        "receipt_sha256",
    )
    validate_auth4_public_source_receipt(receipt)
    _write_json(public_receipt, receipt)
    return {
        "valid": True,
        "authorization_id": AUTH4_ID,
        "candidate_prs": len(candidates),
        "selected_prs": TARGET_PRS,
        "selected_diff_secret_scan_blocked": blocked,
        "selected_diff_total_bytes": total_bytes,
        "anonymous_public_source": True,
        "private_workspace_diff_read": False,
        "paid_call_gate": False,
        "public_receipt_sha256": receipt["receipt_sha256"],
    }


def validate_auth4_public_source_receipt(raw: Any) -> dict[str, Any]:
    receipt = _expect_dict(raw, "auth4_public_source_receipt")
    _exact_keys(receipt, AUTH4_PUBLIC_SOURCE_RECEIPT_KEYS, "auth4_public_source_receipt")
    if receipt["schema_version"] != 1 or receipt["phase_id"] != RUN_PHASE_ID:
        _fail("auth-004 public source receipt schema or phase is invalid")
    if receipt["evidence_type"] != solo.EVIDENCE_TYPE or receipt["authorization_id"] != AUTH4_ID:
        _fail("auth-004 public source receipt identity is invalid")
    if receipt["source_kind"] != "anonymous_public_git_exact_commit":
        _fail("auth-004 public source kind is invalid")
    expected_locator = hashlib.sha256(AUTH4_PUBLIC_SOURCE_URL.encode("ascii")).hexdigest()
    if receipt["source_locator_sha256"] != expected_locator:
        _fail("auth-004 public source locator hash is invalid")
    if (
        receipt["source_commit"] != AUTH4_PUBLIC_SOURCE_COMMIT
        or receipt["source_branch"] != AUTH4_PUBLIC_SOURCE_BRANCH
        or receipt["selection_seed"] != AUTH4_PUBLIC_SELECTION_SEED
    ):
        _fail("auth-004 public source selection anchor is invalid")
    if receipt["license_spdx"] != AUTH4_PUBLIC_LICENSE:
        _fail("auth-004 public source license is invalid")
    solo._expect_sha(receipt["license_sha256"], "auth-004 license hash")
    for key in ("anonymous_clone", "credentials_disabled"):
        if receipt[key] is not True:
            _fail("auth-004 public source anonymity proof is incomplete")
    for key in ("github_api_used", "private_workspace_diff_read", "paid_call_gate"):
        if receipt[key] is not False:
            _fail("auth-004 public source external boundary is invalid")
    if receipt["window_start"] != WINDOW_START or receipt["window_end"] != WINDOW_END:
        _fail("auth-004 public source window is invalid")
    if (
        receipt["candidate_prs"] != AUTH4_EXPECTED_CANDIDATES
        or receipt["selected_prs"] != TARGET_PRS
        or receipt["selected_diff_secret_scan_blocked"] != AUTH4_EXPECTED_BLOCKED
    ):
        _fail("auth-004 public source denominator is invalid")
    if (
        isinstance(receipt["selected_diff_total_bytes"], bool)
        or not isinstance(receipt["selected_diff_total_bytes"], int)
        or receipt["selected_diff_total_bytes"] <= 0
    ):
        _fail("auth-004 selected public diff size is invalid")
    for key in (
        "selection_log_sha256",
        "cohort_sha256",
        "private_artifact_index_sha256",
    ):
        solo._expect_sha(receipt[key], f"auth-004 public source {key}")
    if receipt["business_claim_allowed"] or receipt["quality_claim_allowed"]:
        _fail("auth-004 public source receipt cannot allow claims")
    if receipt["formal_quality_status"] != "incomplete":
        _fail("auth-004 formal quality status must remain incomplete")
    generated = _canonical_timestamp(receipt["generated_at"], "auth-004 source generated_at")
    if not (
        _canonical_timestamp(APPROVED_AT, "auth-002 approved_at")
        <= generated
        < _canonical_timestamp(EXPIRES_AT, "auth-002 expires_at")
    ):
        _fail("auth-004 public source receipt is outside the inherited window")
    solo.validate_artifact_hash(
        receipt, "receipt_sha256", "auth4_public_source_receipt"
    )
    return receipt


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _auth3_approval_statement() -> dict[str, Any]:
    return {
        "endpoint_kind": "standard",
        "provider": EXPECTED_PROVIDER,
        "exact_model_snapshot": EXPECTED_MODEL,
        "temperature_profile": dict(AUTH3_TEMPERATURE_PROFILE),
        "sdk_max_retries": 0,
        **AUTH3_LIMITS,
        **AUTH3_TARIFF_RATES,
        "selected_diff_policy": "block_headline_zero_call",
        "blocked_selected_prs": 2,
        "max_runnable_prs": 3,
    }


def validate_auth3_runtime(raw: Any) -> dict[str, Any]:
    config = _expect_dict(raw, "auth3_runtime_config")
    _exact_keys(config, AUTH3_RUNTIME_KEYS, "auth3_runtime_config")
    if config["schema_version"] != 1 or config["executor_version"] != EXECUTOR_VERSION:
        _fail("auth-003 runtime schema or executor version is invalid")
    if not isinstance(config["executor_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", config["executor_commit"]
    ):
        _fail("auth-003 executor commit is invalid")
    solo._expect_sha(config["executor_source_sha256"], "auth-003 executor source hash")
    if config["product_source_commit"] != SOURCE_COMMIT:
        _fail("auth-003 product source commit differs from the frozen source")
    if config["provider"] != EXPECTED_PROVIDER or config["exact_model_snapshot"] != EXPECTED_MODEL:
        _fail("auth-003 runtime provider or model is invalid")
    if config["endpoint_kind"] != "standard" or config["base_url"] != STANDARD_BASE_URL:
        _fail("auth-003 runtime endpoint is invalid")
    if config["temperature_profile"] != AUTH3_TEMPERATURE_PROFILE:
        _fail("auth-003 runtime temperature profile is invalid")
    if config["sdk_max_retries"] != 0:
        _fail("auth-003 runtime must disable SDK retries")
    if config["per_call_max_output_tokens"] != PER_CALL_MAX_OUTPUT_TOKENS:
        _fail("auth-003 per-call output ceiling is invalid")
    if config["request_timeout_seconds"] != REQUEST_TIMEOUT_SECONDS:
        _fail("auth-003 request timeout is invalid")
    if config["review_timeout_seconds"] != review_agent.REVIEW_TIMEOUT_SECONDS:
        _fail("auth-003 review timeout is invalid")
    if config["use_context"] is not False or config["use_verify"] is not True:
        _fail("auth-003 context or verifier mode is invalid")
    if config["tiebreak"] is not False:
        _fail("auth-003 tiebreak must remain disabled")
    if config["pr_execution"] != "sequential_with_product_stage_pairs":
        _fail("auth-003 PR execution policy is invalid")
    if config["selected_diff_policy"] != "block_headline_zero_call":
        _fail("auth-003 selected-diff policy is invalid")
    if config["max_runnable_prs"] != 3:
        _fail("auth-003 runnable PR count is invalid")
    if config["selection_receipt_sha256"] == "" or config["cohort_sha256"] == "":
        _fail("auth-003 runtime evidence binding is missing")
    solo._expect_sha(config["selection_receipt_sha256"], "auth-003 selection receipt hash")
    solo._expect_sha(config["cohort_sha256"], "auth-003 cohort hash")
    return config


def validate_auth3(
    raw: Any,
    *,
    runtime_config: Mapping[str, Any],
    tariff: Mapping[str, Any],
) -> dict[str, Any]:
    authorization = _expect_dict(raw, "auth3_authorization")
    _exact_keys(authorization, AUTH3_KEYS, "auth3_authorization")
    if authorization["schema_version"] != 1 or authorization["phase_id"] != RUN_PHASE_ID:
        _fail("auth-003 schema or phase is invalid")
    if authorization["authorization_id"] != AUTH3_ID:
        _fail("auth-003 authorization ID is invalid")
    if authorization["supersedes_authorization_sha256"] != EXPECTED_AUTHORIZATION_SHA256:
        _fail("auth-003 does not bind the approved auth-002")
    solo._expect_identifier(authorization["participant_id"], "auth-003 participant ID")
    repository_ids = authorization["repository_ids"]
    if not isinstance(repository_ids, list) or len(repository_ids) != 1:
        _fail("auth-003 must bind exactly one repository")
    solo._expect_identifier(repository_ids[0], "auth-003 repository ID")
    solo._expect_identifier(authorization["approved_by"], "auth-003 approver ID")
    approved_at = _canonical_timestamp(authorization["approved_at"], "auth-003 approved_at")
    expires_at = _canonical_timestamp(authorization["expires_at"], "auth-003 expires_at")
    if expires_at <= approved_at:
        _fail("auth-003 expiry must follow approval")
    if authorization["provider"] != EXPECTED_PROVIDER or authorization["exact_model_snapshot"] != EXPECTED_MODEL:
        _fail("auth-003 provider or model is invalid")
    runtime = validate_auth3_runtime(runtime_config)
    if authorization["runtime_config_sha256"] != solo.sha256_value(runtime):
        _fail("auth-003 runtime hash is invalid")
    validated_tariff = validate_tariff(tariff)
    if authorization["tariff_sha256"] != validated_tariff["tariff_sha256"]:
        _fail("auth-003 tariff hash is invalid")
    if authorization["selection_receipt_sha256"] != runtime["selection_receipt_sha256"]:
        _fail("auth-003 selection receipt binding is invalid")
    if authorization["cohort_sha256"] != runtime["cohort_sha256"]:
        _fail("auth-003 cohort binding is invalid")
    if authorization["temperature_profile"] != AUTH3_TEMPERATURE_PROFILE:
        _fail("auth-003 temperature profile is invalid")
    if authorization["sdk_max_retries"] != 0:
        _fail("auth-003 must disable SDK retries")
    for key, expected in AUTH3_LIMITS.items():
        if authorization[key] != expected:
            _fail(f"auth-003 {key} ceiling is invalid")
    for key in ("real_paid_calls", "read_selected_raw_diff"):
        if authorization[key] is not True:
            _fail("auth-003 paid-call or raw-diff authority is missing")
    for key in ("real_github_api", "github_publish", "staging_deploy"):
        if authorization[key] is not False:
            _fail("auth-003 external operation authority is invalid")
    if authorization["selected_diff_policy"] != "block_headline_zero_call":
        _fail("auth-003 selected-diff policy is invalid")
    if authorization["blocked_selected_prs"] != 2 or authorization["max_runnable_prs"] != 3:
        _fail("auth-003 blocked/runnable PR counts are invalid")
    if authorization["approval_statement_sha256"] != solo.sha256_value(
        _auth3_approval_statement()
    ):
        _fail("auth-003 approval statement hash is invalid")
    solo.validate_artifact_hash(
        authorization,
        "authorization_sha256",
        "auth3_authorization",
    )
    return authorization


def validate_auth3_attestation(raw: Any) -> dict[str, Any]:
    attestation = _expect_dict(raw, "auth3_attestation")
    _exact_keys(attestation, AUTH3_ATTESTATION_KEYS, "auth3_attestation")
    if attestation["schema_version"] != 1 or attestation["phase_id"] != RUN_PHASE_ID:
        _fail("auth-003 attestation schema or phase is invalid")
    if attestation["authorization_id"] != AUTH3_ID:
        _fail("auth-003 attestation ID is invalid")
    for key in (
        "authorization_sha256",
        "runtime_config_sha256",
        "tariff_sha256",
        "selection_receipt_sha256",
        "cohort_sha256",
    ):
        solo._expect_sha(attestation[key], f"auth-003 attestation {key}")
    if attestation["endpoint_kind"] != "standard":
        _fail("auth-003 attestation endpoint is invalid")
    if attestation["provider"] != EXPECTED_PROVIDER or attestation["exact_model_snapshot"] != EXPECTED_MODEL:
        _fail("auth-003 attestation provider or model is invalid")
    if attestation["temperature_profile"] != AUTH3_TEMPERATURE_PROFILE:
        _fail("auth-003 attestation temperature profile is invalid")
    if attestation["sdk_max_retries"] != 0:
        _fail("auth-003 attestation retry policy is invalid")
    for key, expected in AUTH3_LIMITS.items():
        if attestation[key] != expected:
            _fail(f"auth-003 attestation {key} is invalid")
    if attestation["blocked_selected_prs"] != 2 or attestation["max_runnable_prs"] != 3:
        _fail("auth-003 attestation selected-PR policy is invalid")
    if attestation["authorization_complete"] is not True:
        _fail("auth-003 attestation must mark authorization complete")
    if attestation["paid_call_gate"] is not False:
        _fail("auth-003 attestation cannot open the dynamic paid-call gate")
    if attestation["paid_call_blockers"] != [
        "credential_preflight_pending",
        "offline_validation_pending",
    ]:
        _fail("auth-003 attestation blockers are invalid")
    if attestation["business_claim_allowed"] or attestation["quality_claim_allowed"]:
        _fail("auth-003 attestation cannot allow business or quality claims")
    if attestation["formal_quality_status"] != "incomplete":
        _fail("auth-003 attestation formal quality status is invalid")
    _canonical_timestamp(attestation["approved_at"], "auth-003 attestation approved_at")
    _canonical_timestamp(attestation["expires_at"], "auth-003 attestation expires_at")
    solo.validate_artifact_hash(attestation, "attestation_sha256", "auth3_attestation")
    return attestation


def initialize_auth_003(
    *,
    repo_root: Path,
    evidence_root: Path,
    selection_receipt_path: Path,
    public_attestation_path: Path,
    approved_at: str,
) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    evidence = evidence_root.resolve(strict=True)
    public_attestation = public_attestation_path.resolve(strict=False)
    if _is_within(evidence, repo) or evidence == repo:
        _fail("private evidence root must be outside the Git worktree")
    if not _is_within(public_attestation, repo):
        _fail("auth-003 public attestation must be inside the Git worktree")
    auth3_root = evidence / "auth-003"
    if auth3_root.exists() or public_attestation.exists():
        _fail("auth-003 evidence already exists and cannot be overwritten")
    if _git_text(repo, ["status", "--porcelain", "--", "phase9g_solo_run.py"]):
        _fail("auth-003 executor source must be committed before authorization")
    executor_commit = _git_text(repo, ["rev-parse", "HEAD"])
    selection_receipt = validate_public_receipt(solo.load_json(selection_receipt_path))
    auth2 = validate_authorization(solo.load_json(evidence / "authorization.json"))
    cohort = solo.load_json(evidence / "cohort.json")
    if cohort.get("cohort_sha256") != selection_receipt["cohort_sha256"]:
        _fail("private cohort differs from the public selection receipt")
    candidate_map = solo.load_json(evidence / "candidate-map.private.json")
    if not isinstance(candidate_map, list):
        _fail("private candidate map is invalid")
    blocked_selected = sum(
        bool(row.get("selected")) and int(row.get("potential_secret_findings", 0)) > 0
        for row in candidate_map
        if isinstance(row, dict)
    )
    if blocked_selected != 2:
        _fail("private selected-diff blocks differ from the approved policy")
    approved = _canonical_timestamp(approved_at, "auth-003 approved_at")
    if approved < _canonical_timestamp(auth2["approved_at"], "auth-002 approved_at"):
        _fail("auth-003 approval predates auth-002")
    if approved >= _canonical_timestamp(auth2["expires_at"], "auth-002 expires_at"):
        _fail("auth-003 approval is outside the inherited authorization window")

    pricing_source = {
        "schema_version": 1,
        "source_kind": "user_confirmed_official_standard_pricing",
        "source_url": "https://bigmodel.cn/pricing",
        "confirmed_at": approved_at,
        **AUTH3_TARIFF_RATES,
    }
    tariff = solo.with_artifact_hash(
        {
            "schema_version": 1,
            "provider": EXPECTED_PROVIDER,
            "model": EXPECTED_MODEL,
            "endpoint_kind": "standard",
            "effective_at": approved_at,
            **AUTH3_TARIFF_RATES,
            "source_sha256": solo.sha256_value(pricing_source),
            "tariff_sha256": "",
        },
        "tariff_sha256",
    )
    validate_tariff(tariff)
    runtime_config = {
        "schema_version": 1,
        "executor_version": EXECUTOR_VERSION,
        "executor_commit": executor_commit,
        "executor_source_sha256": _sha256_file(repo / "phase9g_solo_run.py"),
        "product_source_commit": SOURCE_COMMIT,
        "provider": EXPECTED_PROVIDER,
        "exact_model_snapshot": EXPECTED_MODEL,
        "endpoint_kind": "standard",
        "base_url": STANDARD_BASE_URL,
        "temperature_profile": dict(AUTH3_TEMPERATURE_PROFILE),
        "sdk_max_retries": 0,
        "per_call_max_output_tokens": PER_CALL_MAX_OUTPUT_TOKENS,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "review_timeout_seconds": review_agent.REVIEW_TIMEOUT_SECONDS,
        "use_context": False,
        "use_verify": True,
        "tiebreak": False,
        "pr_execution": "sequential_with_product_stage_pairs",
        "selected_diff_policy": "block_headline_zero_call",
        "max_runnable_prs": 3,
        "selection_receipt_sha256": selection_receipt["receipt_sha256"],
        "cohort_sha256": cohort["cohort_sha256"],
    }
    validate_auth3_runtime(runtime_config)
    authorization = solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "authorization_id": AUTH3_ID,
            "supersedes_authorization_sha256": auth2["authorization_sha256"],
            "participant_id": auth2["participant_id"],
            "repository_ids": list(auth2["repository_ids"]),
            "approved_by": auth2["approved_by"],
            "approved_at": approved_at,
            "expires_at": auth2["expires_at"],
            "provider": EXPECTED_PROVIDER,
            "exact_model_snapshot": EXPECTED_MODEL,
            "runtime_config_sha256": solo.sha256_value(runtime_config),
            "tariff_sha256": tariff["tariff_sha256"],
            "selection_receipt_sha256": selection_receipt["receipt_sha256"],
            "cohort_sha256": cohort["cohort_sha256"],
            "temperature_profile": dict(AUTH3_TEMPERATURE_PROFILE),
            "sdk_max_retries": 0,
            **AUTH3_LIMITS,
            "real_paid_calls": True,
            "read_selected_raw_diff": True,
            "real_github_api": False,
            "github_publish": False,
            "staging_deploy": False,
            "selected_diff_policy": "block_headline_zero_call",
            "blocked_selected_prs": 2,
            "max_runnable_prs": 3,
            "approval_statement_sha256": solo.sha256_value(_auth3_approval_statement()),
            "authorization_sha256": "",
        },
        "authorization_sha256",
    )
    validate_auth3(authorization, runtime_config=runtime_config, tariff=tariff)
    attestation = solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "authorization_id": AUTH3_ID,
            "authorization_sha256": authorization["authorization_sha256"],
            "runtime_config_sha256": authorization["runtime_config_sha256"],
            "tariff_sha256": tariff["tariff_sha256"],
            "selection_receipt_sha256": selection_receipt["receipt_sha256"],
            "cohort_sha256": cohort["cohort_sha256"],
            "endpoint_kind": "standard",
            "provider": EXPECTED_PROVIDER,
            "exact_model_snapshot": EXPECTED_MODEL,
            "temperature_profile": dict(AUTH3_TEMPERATURE_PROFILE),
            "sdk_max_retries": 0,
            **AUTH3_LIMITS,
            "blocked_selected_prs": 2,
            "max_runnable_prs": 3,
            "authorization_complete": True,
            "paid_call_gate": False,
            "paid_call_blockers": [
                "credential_preflight_pending",
                "offline_validation_pending",
            ],
            "business_claim_allowed": False,
            "quality_claim_allowed": False,
            "formal_quality_status": "incomplete",
            "approved_at": approved_at,
            "expires_at": auth2["expires_at"],
            "attestation_sha256": "",
        },
        "attestation_sha256",
    )
    validate_auth3_attestation(attestation)
    auth3_root.mkdir(parents=True, exist_ok=False)
    _write_json(auth3_root / "pricing-source.json", pricing_source)
    _write_json(auth3_root / "tariff.json", tariff)
    _write_json(auth3_root / "runtime-config.json", runtime_config)
    _write_json(auth3_root / "authorization.json", authorization)
    _write_json(public_attestation, attestation)
    return {
        "valid": True,
        "authorization_id": AUTH3_ID,
        "authorization_sha256": authorization["authorization_sha256"],
        "runtime_config_sha256": authorization["runtime_config_sha256"],
        "tariff_sha256": tariff["tariff_sha256"],
        "blocked_selected_prs": 2,
        "max_runnable_prs": 3,
        "paid_call_gate": False,
        "stable_ids_disclosed": False,
    }


def _auth4_approval_statement(
    source_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        **_auth3_approval_statement(),
        "authorization_id": AUTH4_ID,
        "supersedes_authorization_sha256": EXPECTED_AUTH3_AUTHORIZATION_SHA256,
        "public_candidate_input_only": True,
        "anonymous_public_git_read": True,
        "github_api_used": False,
        "private_workspace_diff_read": False,
        "public_source_locator_sha256": source_receipt["source_locator_sha256"],
        "public_source_commit": AUTH4_PUBLIC_SOURCE_COMMIT,
        "selection_receipt_sha256": source_receipt["receipt_sha256"],
        "cohort_sha256": source_receipt["cohort_sha256"],
        "blocked_selected_prs": AUTH4_EXPECTED_BLOCKED,
        "max_runnable_prs": AUTH4_EXPECTED_RUNNABLE,
    }


def validate_auth4_runtime(raw: Any) -> dict[str, Any]:
    config = _expect_dict(raw, "auth4_runtime_config")
    _exact_keys(config, AUTH4_RUNTIME_KEYS, "auth4_runtime_config")
    if config["schema_version"] != 1 or config["executor_version"] != AUTH4_EXECUTOR_VERSION:
        _fail("auth-004 runtime schema or executor version is invalid")
    if not isinstance(config["executor_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", config["executor_commit"]
    ):
        _fail("auth-004 executor commit is invalid")
    solo._expect_sha(config["executor_source_sha256"], "auth-004 executor source hash")
    if config["product_source_commit"] != SOURCE_COMMIT:
        _fail("auth-004 product source commit differs from the frozen source")
    if config["provider"] != EXPECTED_PROVIDER or config["exact_model_snapshot"] != EXPECTED_MODEL:
        _fail("auth-004 runtime provider or model is invalid")
    if config["endpoint_kind"] != "standard" or config["base_url"] != STANDARD_BASE_URL:
        _fail("auth-004 runtime endpoint is invalid")
    if config["temperature_profile"] != AUTH3_TEMPERATURE_PROFILE:
        _fail("auth-004 runtime temperature profile is invalid")
    if config["sdk_max_retries"] != 0:
        _fail("auth-004 runtime must disable SDK retries")
    if config["per_call_max_output_tokens"] != PER_CALL_MAX_OUTPUT_TOKENS:
        _fail("auth-004 per-call output ceiling is invalid")
    if config["request_timeout_seconds"] != REQUEST_TIMEOUT_SECONDS:
        _fail("auth-004 request timeout is invalid")
    if config["review_timeout_seconds"] != review_agent.REVIEW_TIMEOUT_SECONDS:
        _fail("auth-004 review timeout is invalid")
    if config["use_context"] is not False or config["use_verify"] is not True:
        _fail("auth-004 context or verifier mode is invalid")
    if config["tiebreak"] is not False:
        _fail("auth-004 tiebreak must remain disabled")
    if config["pr_execution"] != "sequential_with_product_stage_pairs":
        _fail("auth-004 PR execution policy is invalid")
    if config["selected_diff_policy"] != "block_headline_zero_call":
        _fail("auth-004 selected-diff policy is invalid")
    if config["max_runnable_prs"] != AUTH4_EXPECTED_RUNNABLE:
        _fail("auth-004 runnable PR count is invalid")
    if (
        config["public_candidate_input_only"] is not True
        or config["anonymous_public_git_read"] is not True
        or config["github_api_used"] is not False
        or config["private_workspace_diff_read"] is not False
    ):
        _fail("auth-004 public-source runtime boundary is invalid")
    expected_locator = hashlib.sha256(AUTH4_PUBLIC_SOURCE_URL.encode("ascii")).hexdigest()
    if (
        config["public_source_locator_sha256"] != expected_locator
        or config["public_source_commit"] != AUTH4_PUBLIC_SOURCE_COMMIT
    ):
        _fail("auth-004 public source runtime binding is invalid")
    for key in ("selection_receipt_sha256", "cohort_sha256"):
        solo._expect_sha(config[key], f"auth-004 runtime {key}")
    return config


def validate_auth4(
    raw: Any,
    *,
    runtime_config: Mapping[str, Any],
    tariff: Mapping[str, Any],
    source_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    authorization = _expect_dict(raw, "auth4_authorization")
    _exact_keys(authorization, AUTH4_KEYS, "auth4_authorization")
    if authorization["schema_version"] != 1 or authorization["phase_id"] != RUN_PHASE_ID:
        _fail("auth-004 schema or phase is invalid")
    if authorization["authorization_id"] != AUTH4_ID:
        _fail("auth-004 authorization ID is invalid")
    if authorization["supersedes_authorization_sha256"] != EXPECTED_AUTH3_AUTHORIZATION_SHA256:
        _fail("auth-004 does not bind the immutable auth-003")
    solo._expect_identifier(authorization["participant_id"], "auth-004 participant ID")
    if authorization["repository_ids"] != [auth4_public_repository_id()]:
        _fail("auth-004 repository identity is invalid")
    solo._expect_identifier(authorization["approved_by"], "auth-004 approver ID")
    approved_at = _canonical_timestamp(authorization["approved_at"], "auth-004 approved_at")
    expires_at = _canonical_timestamp(authorization["expires_at"], "auth-004 expires_at")
    source_generated = _canonical_timestamp(
        str(source_receipt.get("generated_at")), "auth-004 source generated_at"
    )
    if (
        expires_at <= approved_at
        or authorization["expires_at"] != EXPIRES_AT
        or approved_at < source_generated
    ):
        _fail("auth-004 authorization approval window is invalid")
    if authorization["provider"] != EXPECTED_PROVIDER or authorization["exact_model_snapshot"] != EXPECTED_MODEL:
        _fail("auth-004 provider or model is invalid")
    runtime = validate_auth4_runtime(runtime_config)
    if authorization["runtime_config_sha256"] != solo.sha256_value(runtime):
        _fail("auth-004 runtime hash is invalid")
    validated_tariff = validate_tariff(tariff)
    if authorization["tariff_sha256"] != validated_tariff["tariff_sha256"]:
        _fail("auth-004 tariff hash is invalid")
    public_source = validate_auth4_public_source_receipt(source_receipt)
    if authorization["selection_receipt_sha256"] != public_source["receipt_sha256"]:
        _fail("auth-004 public selection receipt binding is invalid")
    if authorization["cohort_sha256"] != public_source["cohort_sha256"]:
        _fail("auth-004 public cohort binding is invalid")
    if authorization["temperature_profile"] != AUTH3_TEMPERATURE_PROFILE:
        _fail("auth-004 temperature profile is invalid")
    if authorization["sdk_max_retries"] != 0:
        _fail("auth-004 must disable SDK retries")
    for key, expected in AUTH3_LIMITS.items():
        if authorization[key] != expected:
            _fail(f"auth-004 {key} ceiling is invalid")
    for key in ("real_paid_calls", "read_selected_raw_diff"):
        if authorization[key] is not True:
            _fail("auth-004 paid-call or public-diff authority is missing")
    for key in ("real_github_api", "github_publish", "staging_deploy"):
        if authorization[key] is not False:
            _fail("auth-004 external operation authority is invalid")
    if (
        authorization["public_candidate_input_only"] is not True
        or authorization["anonymous_public_git_read"] is not True
    ):
        _fail("auth-004 public-source authority is invalid")
    if (
        authorization["public_source_locator_sha256"]
        != public_source["source_locator_sha256"]
        or authorization["public_source_commit"] != AUTH4_PUBLIC_SOURCE_COMMIT
    ):
        _fail("auth-004 public source authority binding is invalid")
    if authorization["selected_diff_policy"] != "block_headline_zero_call":
        _fail("auth-004 selected-diff policy is invalid")
    if (
        authorization["blocked_selected_prs"] != AUTH4_EXPECTED_BLOCKED
        or authorization["max_runnable_prs"] != AUTH4_EXPECTED_RUNNABLE
    ):
        _fail("auth-004 blocked/runnable PR counts are invalid")
    if authorization["approval_statement_sha256"] != solo.sha256_value(
        _auth4_approval_statement(public_source)
    ):
        _fail("auth-004 approval statement hash is invalid")
    solo.validate_artifact_hash(
        authorization, "authorization_sha256", "auth4_authorization"
    )
    return authorization


def validate_auth4_attestation(raw: Any) -> dict[str, Any]:
    attestation = _expect_dict(raw, "auth4_attestation")
    _exact_keys(attestation, AUTH4_ATTESTATION_KEYS, "auth4_attestation")
    if attestation["schema_version"] != 1 or attestation["phase_id"] != RUN_PHASE_ID:
        _fail("auth-004 attestation schema or phase is invalid")
    if attestation["authorization_id"] != AUTH4_ID:
        _fail("auth-004 attestation ID is invalid")
    for key in (
        "authorization_sha256",
        "runtime_config_sha256",
        "tariff_sha256",
        "selection_receipt_sha256",
        "cohort_sha256",
        "public_source_locator_sha256",
    ):
        solo._expect_sha(attestation[key], f"auth-004 attestation {key}")
    if attestation["endpoint_kind"] != "standard":
        _fail("auth-004 attestation endpoint is invalid")
    if attestation["provider"] != EXPECTED_PROVIDER or attestation["exact_model_snapshot"] != EXPECTED_MODEL:
        _fail("auth-004 attestation provider or model is invalid")
    if attestation["temperature_profile"] != AUTH3_TEMPERATURE_PROFILE:
        _fail("auth-004 attestation temperature profile is invalid")
    if attestation["sdk_max_retries"] != 0:
        _fail("auth-004 attestation retry policy is invalid")
    for key, expected in AUTH3_LIMITS.items():
        if attestation[key] != expected:
            _fail(f"auth-004 attestation {key} is invalid")
    if (
        attestation["blocked_selected_prs"] != AUTH4_EXPECTED_BLOCKED
        or attestation["max_runnable_prs"] != AUTH4_EXPECTED_RUNNABLE
    ):
        _fail("auth-004 attestation selected-PR policy is invalid")
    if (
        attestation["public_candidate_input_only"] is not True
        or attestation["anonymous_public_git_read"] is not True
        or attestation["github_api_used"] is not False
        or attestation["private_workspace_diff_read"] is not False
        or attestation["public_source_commit"] != AUTH4_PUBLIC_SOURCE_COMMIT
    ):
        _fail("auth-004 attestation public-source boundary is invalid")
    if attestation["authorization_complete"] is not True:
        _fail("auth-004 attestation must mark authorization complete")
    if attestation["paid_call_gate"] is not False:
        _fail("auth-004 attestation cannot open the dynamic paid-call gate")
    if attestation["paid_call_blockers"] != [
        "credential_preflight_pending",
        "offline_validation_pending",
    ]:
        _fail("auth-004 attestation blockers are invalid")
    if attestation["business_claim_allowed"] or attestation["quality_claim_allowed"]:
        _fail("auth-004 attestation cannot allow business or quality claims")
    if attestation["formal_quality_status"] != "incomplete":
        _fail("auth-004 attestation formal quality status is invalid")
    approved = _canonical_timestamp(
        attestation["approved_at"], "auth-004 attestation approved_at"
    )
    expires = _canonical_timestamp(
        attestation["expires_at"], "auth-004 attestation expires_at"
    )
    if attestation["expires_at"] != EXPIRES_AT or expires <= approved:
        _fail("auth-004 attestation approval window is invalid")
    solo.validate_artifact_hash(attestation, "attestation_sha256", "auth4_attestation")
    return attestation


def initialize_auth_004(
    *,
    repo_root: Path,
    evidence_root: Path,
    public_source_receipt_path: Path,
    public_attestation_path: Path,
    approved_at: str,
) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    evidence = evidence_root.resolve(strict=True)
    public_attestation = public_attestation_path.resolve(strict=False)
    if _is_within(evidence, repo) or evidence == repo:
        _fail("private evidence root must be outside the Git worktree")
    if not _is_within(public_attestation, repo):
        _fail("auth-004 public attestation must be inside the Git worktree")
    auth4_root = evidence / "auth-004"
    if auth4_root.exists() or public_attestation.exists():
        _fail("auth-004 evidence already exists and cannot be overwritten")
    if _git_text(repo, ["status", "--porcelain", "--", "phase9g_solo_run.py"]):
        _fail("auth-004 executor source must be committed before authorization")
    executor_commit = _git_text(repo, ["rev-parse", "HEAD"])
    public_source = validate_auth4_public_source_receipt(
        solo.load_json(public_source_receipt_path)
    )
    selection = _load_auth4_public_selection(evidence)
    cohort = selection["cohort"]
    if cohort["cohort_sha256"] != public_source["cohort_sha256"]:
        _fail("auth-004 private/public cohort hashes differ")
    auth3_bundle = _load_auth3_bundle(evidence)
    auth3 = auth3_bundle["authorization"]
    if auth3["authorization_sha256"] != EXPECTED_AUTH3_AUTHORIZATION_SHA256:
        _fail("auth-004 predecessor auth-003 hash is invalid")
    approved = _canonical_timestamp(approved_at, "auth-004 approved_at")
    source_generated = _canonical_timestamp(
        public_source["generated_at"], "auth-004 source generated_at"
    )
    if approved < source_generated:
        _fail("auth-004 approval predates exact public source materialization")
    if approved >= _canonical_timestamp(EXPIRES_AT, "auth-004 expires_at"):
        _fail("auth-004 approval is outside the inherited authorization window")

    tariff = dict(auth3_bundle["tariff"])
    validate_tariff(tariff)
    runtime_config = {
        "schema_version": 1,
        "executor_version": AUTH4_EXECUTOR_VERSION,
        "executor_commit": executor_commit,
        "executor_source_sha256": _sha256_file(repo / "phase9g_solo_run.py"),
        "product_source_commit": SOURCE_COMMIT,
        "provider": EXPECTED_PROVIDER,
        "exact_model_snapshot": EXPECTED_MODEL,
        "endpoint_kind": "standard",
        "base_url": STANDARD_BASE_URL,
        "temperature_profile": dict(AUTH3_TEMPERATURE_PROFILE),
        "sdk_max_retries": 0,
        "per_call_max_output_tokens": PER_CALL_MAX_OUTPUT_TOKENS,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "review_timeout_seconds": review_agent.REVIEW_TIMEOUT_SECONDS,
        "use_context": False,
        "use_verify": True,
        "tiebreak": False,
        "pr_execution": "sequential_with_product_stage_pairs",
        "selected_diff_policy": "block_headline_zero_call",
        "max_runnable_prs": AUTH4_EXPECTED_RUNNABLE,
        "selection_receipt_sha256": public_source["receipt_sha256"],
        "cohort_sha256": cohort["cohort_sha256"],
        "public_candidate_input_only": True,
        "anonymous_public_git_read": True,
        "github_api_used": False,
        "private_workspace_diff_read": False,
        "public_source_locator_sha256": public_source["source_locator_sha256"],
        "public_source_commit": AUTH4_PUBLIC_SOURCE_COMMIT,
    }
    validate_auth4_runtime(runtime_config)
    authorization = solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "authorization_id": AUTH4_ID,
            "supersedes_authorization_sha256": auth3["authorization_sha256"],
            "participant_id": auth3["participant_id"],
            "repository_ids": [auth4_public_repository_id()],
            "approved_by": auth3["approved_by"],
            "approved_at": approved_at,
            "expires_at": EXPIRES_AT,
            "provider": EXPECTED_PROVIDER,
            "exact_model_snapshot": EXPECTED_MODEL,
            "runtime_config_sha256": solo.sha256_value(runtime_config),
            "tariff_sha256": tariff["tariff_sha256"],
            "selection_receipt_sha256": public_source["receipt_sha256"],
            "cohort_sha256": cohort["cohort_sha256"],
            "temperature_profile": dict(AUTH3_TEMPERATURE_PROFILE),
            "sdk_max_retries": 0,
            **AUTH3_LIMITS,
            "real_paid_calls": True,
            "read_selected_raw_diff": True,
            "real_github_api": False,
            "github_publish": False,
            "staging_deploy": False,
            "selected_diff_policy": "block_headline_zero_call",
            "blocked_selected_prs": AUTH4_EXPECTED_BLOCKED,
            "max_runnable_prs": AUTH4_EXPECTED_RUNNABLE,
            "public_candidate_input_only": True,
            "anonymous_public_git_read": True,
            "public_source_locator_sha256": public_source["source_locator_sha256"],
            "public_source_commit": AUTH4_PUBLIC_SOURCE_COMMIT,
            "approval_statement_sha256": solo.sha256_value(
                _auth4_approval_statement(public_source)
            ),
            "authorization_sha256": "",
        },
        "authorization_sha256",
    )
    validate_auth4(
        authorization,
        runtime_config=runtime_config,
        tariff=tariff,
        source_receipt=public_source,
    )
    attestation = solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "authorization_id": AUTH4_ID,
            "authorization_sha256": authorization["authorization_sha256"],
            "runtime_config_sha256": authorization["runtime_config_sha256"],
            "tariff_sha256": tariff["tariff_sha256"],
            "selection_receipt_sha256": public_source["receipt_sha256"],
            "cohort_sha256": cohort["cohort_sha256"],
            "endpoint_kind": "standard",
            "provider": EXPECTED_PROVIDER,
            "exact_model_snapshot": EXPECTED_MODEL,
            "temperature_profile": dict(AUTH3_TEMPERATURE_PROFILE),
            "sdk_max_retries": 0,
            **AUTH3_LIMITS,
            "blocked_selected_prs": AUTH4_EXPECTED_BLOCKED,
            "max_runnable_prs": AUTH4_EXPECTED_RUNNABLE,
            "public_candidate_input_only": True,
            "anonymous_public_git_read": True,
            "github_api_used": False,
            "private_workspace_diff_read": False,
            "public_source_locator_sha256": public_source["source_locator_sha256"],
            "public_source_commit": AUTH4_PUBLIC_SOURCE_COMMIT,
            "authorization_complete": True,
            "paid_call_gate": False,
            "paid_call_blockers": [
                "credential_preflight_pending",
                "offline_validation_pending",
            ],
            "business_claim_allowed": False,
            "quality_claim_allowed": False,
            "formal_quality_status": "incomplete",
            "approved_at": approved_at,
            "expires_at": EXPIRES_AT,
            "attestation_sha256": "",
        },
        "attestation_sha256",
    )
    validate_auth4_attestation(attestation)
    auth4_root.mkdir(parents=True, exist_ok=False)
    _write_json(auth4_root / "tariff.json", tariff)
    _write_json(auth4_root / "runtime-config.json", runtime_config)
    _write_json(auth4_root / "authorization.json", authorization)
    _write_json(public_attestation, attestation)
    return {
        "valid": True,
        "authorization_id": AUTH4_ID,
        "authorization_sha256": authorization["authorization_sha256"],
        "runtime_config_sha256": authorization["runtime_config_sha256"],
        "tariff_sha256": tariff["tariff_sha256"],
        "selected_prs": TARGET_PRS,
        "blocked_selected_prs": AUTH4_EXPECTED_BLOCKED,
        "max_runnable_prs": AUTH4_EXPECTED_RUNNABLE,
        "public_candidate_input_only": True,
        "private_workspace_diff_read": False,
        "paid_call_gate": False,
        "stable_ids_disclosed": False,
    }


@dataclass(frozen=True)
class ActualUsage:
    logical_calls: int = 0
    http_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_microcny: int = 0
    unknown_usage_calls: int = 0


class BudgetedCompletionGate:
    """Zero-retry paid-call boundary with pre-request worst-case reservation."""

    def __init__(
        self,
        underlying: Any,
        *,
        ledger: BudgetLedger,
        tariff: Mapping[str, Any],
        temperature_profile: Mapping[str, float],
        journal_path: Path | None = None,
    ) -> None:
        self._underlying = underlying
        self._ledger = ledger
        self._tariff = tariff
        self._temperature_profile = temperature_profile
        self._journal_path = journal_path
        self._records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._next_call = 1

    def _effective_temperature(self, requested: Any) -> float:
        if requested in (0, 0.0):
            return float(self._temperature_profile["finder_anchor"])
        if requested == 0.7:
            return float(self._temperature_profile["finder_sampler"])
        _fail("product requested a temperature outside the authorized profile")

    def _reserve(
        self, kwargs: Mapping[str, Any]
    ) -> tuple[int, str, int, int, float]:
        requested_temperature = kwargs.get("temperature")
        effective_temperature = self._effective_temperature(requested_temperature)
        request_material = {
            "model": kwargs.get("model"),
            "messages": kwargs.get("messages"),
            "tools": kwargs.get("tools"),
            "tool_choice": kwargs.get("tool_choice"),
            "temperature": effective_temperature,
            "max_tokens": PER_CALL_MAX_OUTPUT_TOKENS,
        }
        encoded = json.dumps(
            request_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        input_token_upper_bound = len(encoded)
        cost_upper_bound = reserve_cost_microcny(
            self._tariff,
            input_tokens=input_token_upper_bound,
            output_tokens=PER_CALL_MAX_OUTPUT_TOKENS,
        )
        self._ledger.reserve(
            BudgetReservation(
                logical_calls=1,
                http_attempts=1,
                input_tokens=input_token_upper_bound,
                output_tokens=PER_CALL_MAX_OUTPUT_TOKENS,
                cost_microcny=cost_upper_bound,
            )
        )
        with self._lock:
            call_number = self._next_call
            self._next_call += 1
        return (
            call_number,
            hashlib.sha256(encoded).hexdigest(),
            input_token_upper_bound,
            cost_upper_bound,
            effective_temperature,
        )

    @staticmethod
    def _usage(response: Any) -> tuple[int, int, int] | None:
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if (
            isinstance(prompt_tokens, bool)
            or not isinstance(prompt_tokens, int)
            or prompt_tokens < 0
            or isinstance(completion_tokens, bool)
            or not isinstance(completion_tokens, int)
            or completion_tokens < 0
        ):
            return None
        details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = getattr(details, "cached_tokens", 0) if details is not None else 0
        if isinstance(cached_tokens, bool) or not isinstance(cached_tokens, int):
            cached_tokens = 0
        cached_tokens = max(0, min(cached_tokens, prompt_tokens))
        return prompt_tokens, completion_tokens, cached_tokens

    def create(self, **kwargs: Any) -> Any:
        (
            call_number,
            request_sha256,
            input_upper,
            cost_upper,
            effective_temperature,
        ) = self._reserve(kwargs)
        outgoing = dict(kwargs)
        outgoing["temperature"] = effective_temperature
        outgoing["max_tokens"] = PER_CALL_MAX_OUTPUT_TOKENS
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        record: dict[str, Any] = {
            "schema_version": 1,
            "call_number": call_number,
            "request_sha256": request_sha256,
            "effective_temperature": effective_temperature,
            "reserved_input_tokens": input_upper,
            "reserved_output_tokens": PER_CALL_MAX_OUTPUT_TOKENS,
            "reserved_cost_microcny": cost_upper,
            "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "completed_at": None,
            "latency_seconds": None,
            "status": "started",
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "cost_microcny": None,
            "usage_known": False,
            "response_sha256": None,
            "error_category": None,
        }
        try:
            response = self._underlying.create(**outgoing)
            usage = self._usage(response)
            if usage is not None:
                input_tokens, output_tokens, cached_tokens = usage
                record.update(
                    {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cached_input_tokens": cached_tokens,
                        "cost_microcny": reserve_cost_microcny(
                            self._tariff,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cached_tokens=cached_tokens,
                        ),
                        "usage_known": True,
                    }
                )
            response_material = {
                "id": getattr(response, "id", None),
                "model": getattr(response, "model", None),
                "created": getattr(response, "created", None),
                "usage": usage,
            }
            record["response_sha256"] = solo.sha256_value(response_material)
            record["status"] = "completed"
            return response
        except BaseException as exc:
            record["status"] = "failed"
            record["error_category"] = type(exc).__name__[:100]
            raise
        finally:
            completed = datetime.now(timezone.utc)
            record["completed_at"] = completed.strftime("%Y-%m-%dT%H:%M:%SZ")
            record["latency_seconds"] = round(time.monotonic() - started_monotonic, 6)
            sealed = solo.with_artifact_hash(
                {**record, "call_sha256": ""},
                "call_sha256",
            )
            with self._lock:
                if self._journal_path is not None:
                    _append_jsonl(self._journal_path, sealed)
                self._records.append(sealed)

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                (dict(record) for record in self._records),
                key=lambda record: int(record["call_number"]),
            )

    def actual_usage(self) -> ActualUsage:
        records = self.records()
        known = [record for record in records if record["usage_known"]]
        return ActualUsage(
            logical_calls=len(records),
            http_attempts=len(records),
            input_tokens=sum(int(record["input_tokens"]) for record in known),
            output_tokens=sum(int(record["output_tokens"]) for record in known),
            cached_input_tokens=sum(
                int(record["cached_input_tokens"]) for record in known
            ),
            cost_microcny=sum(int(record["cost_microcny"]) for record in known),
            unknown_usage_calls=len(records) - len(known),
        )


class _BudgetedChat:
    def __init__(self, completions: BudgetedCompletionGate) -> None:
        self.completions = completions


class BudgetedOpenAIClient:
    def __init__(self, completions: BudgetedCompletionGate) -> None:
        self.chat = _BudgetedChat(completions)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    _fail(f"JSONL row {line_number} must be an object")
                rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunValidationError("private JSONL evidence is unavailable or invalid") from exc
    return rows


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    if temporary.exists():
        _fail("stale private evidence transaction exists")
    _write_json(temporary, value)
    os.replace(temporary, path)


def _load_private_selection(evidence_root: Path) -> dict[str, Any]:
    auth2 = validate_authorization(solo.load_json(evidence_root / "authorization.json"))
    participants = solo.load_json(evidence_root / "participants.json")
    repositories = solo.load_json(evidence_root / "repositories.json")
    plan = solo.load_json(evidence_root / "selection-plan.json")
    rows = _load_jsonl(evidence_root / "selection-log.jsonl")
    cohort = solo.load_json(evidence_root / "cohort.json")
    candidate_map = solo.load_json(evidence_root / "candidate-map.private.json")
    source_receipt = solo.load_json(evidence_root / "source-receipt.private.json")
    solo.validate_participant_manifest(participants, auth2)
    solo.validate_repository_manifest(repositories, auth2)
    solo.validate_selection_plan(plan, expected_source_commit=SOURCE_COMMIT)
    solo.validate_selection_log(rows, plan, repositories)
    solo.validate_cohort(cohort, plan, rows, repositories)
    if not isinstance(candidate_map, list) or not all(
        isinstance(row, dict) for row in candidate_map
    ):
        _fail("private candidate map is invalid")
    if source_receipt.get("candidate_map_sha256") != solo.sha256_value(candidate_map):
        _fail("private candidate map hash is invalid")
    if len(candidate_map) != source_receipt.get("pr_candidates"):
        _fail("private candidate map denominator is invalid")
    return {
        "authorization": auth2,
        "participants": participants,
        "repositories": repositories,
        "selection_plan": plan,
        "selection_log": rows,
        "cohort": cohort,
        "candidate_map": candidate_map,
        "source_receipt": source_receipt,
    }


def _load_auth4_public_selection(evidence_root: Path) -> dict[str, Any]:
    source_root = evidence_root / "auth-004-public-source"
    rows = _load_jsonl(source_root / "selection-log.jsonl")
    cohort = solo.load_json(source_root / "cohort.json")
    candidate_map = solo.load_json(source_root / "candidate-map.private.json")
    source_proof = solo.load_json(source_root / "source-proof.private.json")
    private_index = solo.load_json(source_root / "artifact-index.private.json")
    if not isinstance(source_proof, dict):
        _fail("auth-004 private source proof is invalid")
    if (
        source_proof.get("authorization_id") != AUTH4_ID
        or source_proof.get("source_url") != AUTH4_PUBLIC_SOURCE_URL
        or source_proof.get("source_branch") != AUTH4_PUBLIC_SOURCE_BRANCH
        or source_proof.get("source_commit") != AUTH4_PUBLIC_SOURCE_COMMIT
        or source_proof.get("license_spdx") != AUTH4_PUBLIC_LICENSE
        or source_proof.get("candidate_prs") != AUTH4_EXPECTED_CANDIDATES
        or source_proof.get("anonymous_clone") is not True
        or source_proof.get("credentials_disabled") is not True
        or source_proof.get("github_api_used") is not False
        or source_proof.get("private_workspace_diff_read") is not False
    ):
        _fail("auth-004 private source proof boundary is invalid")
    expected_locator = hashlib.sha256(AUTH4_PUBLIC_SOURCE_URL.encode("ascii")).hexdigest()
    if source_proof.get("source_locator_sha256") != expected_locator:
        _fail("auth-004 private source locator hash is invalid")
    solo._expect_sha(source_proof.get("license_sha256"), "auth-004 private license hash")
    if _sha256_file(source_root / "license.public.txt") != source_proof["license_sha256"]:
        _fail("auth-004 private license file hash is invalid")
    _canonical_timestamp(str(source_proof.get("generated_at")), "auth-004 source generated_at")
    if not isinstance(candidate_map, list) or not all(
        isinstance(row, dict) for row in candidate_map
    ):
        _fail("auth-004 private candidate map is invalid")
    if len(candidate_map) != AUTH4_EXPECTED_CANDIDATES or len(rows) != len(candidate_map):
        _fail("auth-004 private candidate denominator is invalid")
    if source_proof.get("candidate_map_sha256") != solo.sha256_value(candidate_map):
        _fail("auth-004 private candidate map hash is invalid")
    row_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        solo.validate_artifact_hash(row, "row_sha256", "auth-004 selection row")
        pr_id = row.get("pr_id")
        if not isinstance(pr_id, str) or pr_id in row_by_id:
            _fail("auth-004 selection row identity is invalid")
        if (
            row.get("schema_version") != 1
            or row.get("phase_id") != RUN_PHASE_ID
            or row.get("authorization_id") != AUTH4_ID
            or row.get("eligible") is not True
            or row.get("synthetic") is not False
            or not isinstance(row.get("selected"), bool)
        ):
            _fail("auth-004 selection row boundary is invalid")
        row_by_id[pr_id] = row
    candidate_by_id: dict[str, dict[str, Any]] = {}
    for private_row in candidate_map:
        pr_number = private_row.get("pr_number")
        pr_id = private_row.get("opaque_pr_id")
        commit_sha = private_row.get("commit_sha")
        if (
            not isinstance(pr_number, str)
            or not isinstance(pr_id, str)
            or not isinstance(commit_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", commit_sha)
            or pr_id != auth4_public_pr_id(pr_number)
            or pr_id in candidate_by_id
        ):
            _fail("auth-004 private candidate identity is invalid")
        expected_rank = solo.selection_rank(AUTH4_PUBLIC_SELECTION_SEED, pr_id)
        public_row = row_by_id.get(pr_id)
        if (
            private_row.get("rank_sha256") != expected_rank
            or public_row is None
            or public_row.get("rank_sha256") != expected_rank
            or public_row.get("merged_at") != private_row.get("merged_at")
            or public_row.get("selected") is not private_row.get("selected")
            or public_row.get("snapshot_sha256") != private_row.get("snapshot_sha256")
            or public_row.get("diff_sha256") != private_row.get("diff_sha256")
        ):
            _fail("auth-004 private/public candidate binding is invalid")
        selected = private_row.get("selected") is True
        if selected:
            for key in ("snapshot_sha256", "diff_sha256"):
                solo._expect_sha(private_row.get(key), f"auth-004 selected {key}")
        elif (
            private_row.get("snapshot_sha256") is not None
            or private_row.get("diff_sha256") is not None
            or private_row.get("potential_secret_findings") != 0
            or private_row.get("diff_bytes") != 0
        ):
            _fail("auth-004 unselected candidate contains diff-derived evidence")
        candidate_by_id[pr_id] = private_row
    if set(row_by_id) != set(candidate_by_id):
        _fail("auth-004 candidate identity sets differ")
    expected_selected_ids = {
        pr_id
        for pr_id, _rank in sorted(
            (
                (pr_id, str(row["rank_sha256"]))
                for pr_id, row in candidate_by_id.items()
            ),
            key=lambda item: (item[1], item[0]),
        )[:TARGET_PRS]
    }
    selected_rows = [row for row in rows if row.get("selected") is True]
    if len(selected_rows) != TARGET_PRS:
        _fail("auth-004 private selected denominator is invalid")
    selected_ids = {row["pr_id"] for row in selected_rows}
    if selected_ids != expected_selected_ids:
        _fail("auth-004 selected set differs from the frozen ranking")
    entries = cohort.get("entries")
    if not isinstance(entries, list) or len(entries) != TARGET_PRS:
        _fail("auth-004 private cohort entries are invalid")
    if {entry.get("pr_id") for entry in entries if isinstance(entry, dict)} != selected_ids:
        _fail("auth-004 private cohort selected set is invalid")
    if cohort.get("authorization_id") != AUTH4_ID or cohort.get("source_commit") != (
        AUTH4_PUBLIC_SOURCE_COMMIT
    ):
        _fail("auth-004 private cohort source binding is invalid")
    solo.validate_artifact_hash(cohort, "cohort_sha256", "auth-004 cohort")
    artifacts: dict[str, Any] = {
        "source_proof": source_proof,
        "selection_log": rows,
        "candidate_map": candidate_map,
        "cohort": cohort,
    }
    expected_index = _private_index(artifacts)
    if private_index != expected_index:
        _fail("auth-004 private artifact index is invalid")
    diff_root = source_root / "selected-diffs"
    blocked = 0
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            _fail("auth-004 private cohort entry is invalid")
        pr_id = entry["pr_id"]
        private_row = candidate_by_id.get(pr_id)
        if private_row is None or private_row.get("selected") is not True:
            _fail("auth-004 private selected mapping is invalid")
        if (
            entry.get("snapshot_sha256") != private_row.get("snapshot_sha256")
            or entry.get("diff_sha256") != private_row.get("diff_sha256")
            or entry.get("synthetic") is not False
        ):
            _fail("auth-004 private cohort content binding is invalid")
        diff_path = diff_root / f"{pr_id}.diff"
        if _sha256_file(diff_path) != entry["diff_sha256"]:
            _fail("auth-004 selected public diff hash is invalid")
        diff_bytes = diff_path.read_bytes()
        findings = _potential_secret_count(diff_bytes)
        if findings != private_row.get("potential_secret_findings"):
            _fail("auth-004 selected public diff scan is invalid")
        blocked += int(findings > 0)
        total_bytes += len(diff_bytes)
    if blocked != AUTH4_EXPECTED_BLOCKED:
        _fail("auth-004 selected public diff block count is invalid")
    return {
        "selection_log": rows,
        "cohort": cohort,
        "candidate_map": candidate_map,
        "source_proof": source_proof,
        "diff_root": diff_root,
        "blocked_selected_prs": blocked,
        "selected_diff_total_bytes": total_bytes,
    }


def _load_auth3_bundle(evidence_root: Path) -> dict[str, Any]:
    auth3_root = evidence_root / "auth-003"
    runtime_config = solo.load_json(auth3_root / "runtime-config.json")
    tariff = solo.load_json(auth3_root / "tariff.json")
    authorization = solo.load_json(auth3_root / "authorization.json")
    validate_auth3(authorization, runtime_config=runtime_config, tariff=tariff)
    return {
        "authorization": authorization,
        "runtime_config": runtime_config,
        "tariff": tariff,
    }


def _load_auth4_bundle(
    evidence_root: Path,
    source_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    auth4_root = evidence_root / "auth-004"
    runtime_config = solo.load_json(auth4_root / "runtime-config.json")
    tariff = solo.load_json(auth4_root / "tariff.json")
    authorization = solo.load_json(auth4_root / "authorization.json")
    validate_auth4(
        authorization,
        runtime_config=runtime_config,
        tariff=tariff,
        source_receipt=source_receipt,
    )
    return {
        "authorization": authorization,
        "runtime_config": runtime_config,
        "tariff": tariff,
    }


def _auth3_credential_value(
    *,
    environment: Mapping[str, str] | None,
    repo_root: Path,
) -> str:
    env = os.environ if environment is None else environment
    glm_path = env.get("GLM_API_KEY_FILE")
    zhipu_path = env.get("ZHIPUAI_API_KEY_FILE")
    if glm_path and zhipu_path and Path(glm_path) != Path(zhipu_path):
        _fail("credential file source is ambiguous")
    path_value = glm_path or zhipu_path
    if not path_value:
        _fail("credential file source is missing")
    credential_path = Path(path_value).resolve(strict=False)
    repo = repo_root.resolve(strict=True)
    if _is_within(credential_path, repo):
        _fail("credential file must be outside the Git worktree")
    try:
        encoded = credential_path.read_bytes()
    except OSError as exc:
        raise RunValidationError("credential file is unavailable") from exc
    if not encoded or len(encoded) > MAX_SECRET_FILE_BYTES:
        _fail("credential file size is invalid")
    try:
        credential = encoded.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RunValidationError("credential file encoding is invalid") from exc
    if not credential:
        _fail("credential file is empty")
    return credential


def preflight_auth_003(
    *,
    repo_root: Path,
    evidence_root: Path,
    public_attestation_path: Path,
    environment: Mapping[str, str] | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    evidence = evidence_root.resolve(strict=True)
    if _is_within(evidence, repo):
        _fail("private evidence root must be outside the Git worktree")
    bundle = _load_auth3_bundle(evidence)
    authorization = bundle["authorization"]
    runtime = bundle["runtime_config"]
    attestation = validate_auth3_attestation(solo.load_json(public_attestation_path))
    if attestation["authorization_sha256"] != authorization["authorization_sha256"]:
        _fail("auth-003 attestation differs from private authorization")
    instant_text = at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    instant = _canonical_timestamp(instant_text, "auth-003 preflight time")
    if not (
        _canonical_timestamp(authorization["approved_at"], "auth-003 approved_at")
        <= instant
        < _canonical_timestamp(authorization["expires_at"], "auth-003 expires_at")
    ):
        _fail("auth-003 is not active")
    if _sha256_file(repo / "phase9g_solo_run.py") != runtime["executor_source_sha256"]:
        _fail("working executor source differs from the authorized hash")
    committed_source = _git_bytes(
        repo,
        ["show", f"{runtime['executor_commit']}:phase9g_solo_run.py"],
    )
    if hashlib.sha256(committed_source).hexdigest() != runtime["executor_source_sha256"]:
        _fail("authorized executor commit does not contain the authorized source")
    if _git_text(repo, ["status", "--porcelain", "--", "phase9g_solo_run.py"]):
        _fail("working executor source has uncommitted changes")
    _auth3_credential_value(environment=environment, repo_root=repo)
    return {
        "valid": True,
        "authorization_id": AUTH3_ID,
        "authorization_sha256": authorization["authorization_sha256"],
        "runtime_config_sha256": authorization["runtime_config_sha256"],
        "credential_source_ready": True,
        "endpoint_kind": "standard",
        "provider": EXPECTED_PROVIDER,
        "model": EXPECTED_MODEL,
        "sdk_max_retries": 0,
        "paid_call_gate": True,
        "secret_disclosed": False,
        "checked_at": instant_text,
    }


def preflight_auth_004(
    *,
    repo_root: Path,
    evidence_root: Path,
    public_source_receipt_path: Path,
    public_attestation_path: Path,
    environment: Mapping[str, str] | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    evidence = evidence_root.resolve(strict=True)
    if _is_within(evidence, repo):
        _fail("private evidence root must be outside the Git worktree")
    source_receipt = validate_auth4_public_source_receipt(
        solo.load_json(public_source_receipt_path)
    )
    selection = _load_auth4_public_selection(evidence)
    if selection["cohort"]["cohort_sha256"] != source_receipt["cohort_sha256"]:
        _fail("auth-004 public/private source selection differs")
    if selection["selected_diff_total_bytes"] != source_receipt["selected_diff_total_bytes"]:
        _fail("auth-004 public/private selected diff size differs")
    bundle = _load_auth4_bundle(evidence, source_receipt)
    authorization = bundle["authorization"]
    runtime = bundle["runtime_config"]
    attestation = validate_auth4_attestation(solo.load_json(public_attestation_path))
    if attestation["authorization_sha256"] != authorization["authorization_sha256"]:
        _fail("auth-004 attestation differs from private authorization")
    for key in (
        "runtime_config_sha256",
        "tariff_sha256",
        "selection_receipt_sha256",
        "cohort_sha256",
        "public_source_locator_sha256",
        "public_source_commit",
        "approved_at",
        "expires_at",
    ):
        expected = (
            authorization[key]
            if key in authorization
            else runtime[key]
        )
        if attestation[key] != expected:
            _fail(f"auth-004 attestation {key} differs from private evidence")
    instant_text = at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    instant = _canonical_timestamp(instant_text, "auth-004 preflight time")
    if not (
        _canonical_timestamp(authorization["approved_at"], "auth-004 approved_at")
        <= instant
        < _canonical_timestamp(authorization["expires_at"], "auth-004 expires_at")
    ):
        _fail("auth-004 is not active")
    if _sha256_file(repo / "phase9g_solo_run.py") != runtime["executor_source_sha256"]:
        _fail("working executor source differs from the auth-004 hash")
    committed_source = _git_bytes(
        repo,
        ["show", f"{runtime['executor_commit']}:phase9g_solo_run.py"],
    )
    if hashlib.sha256(committed_source).hexdigest() != runtime["executor_source_sha256"]:
        _fail("auth-004 executor commit does not contain the authorized source")
    if _git_text(repo, ["status", "--porcelain", "--", "phase9g_solo_run.py"]):
        _fail("working auth-004 executor source has uncommitted changes")
    _auth3_credential_value(environment=environment, repo_root=repo)
    return {
        "valid": True,
        "authorization_id": AUTH4_ID,
        "authorization_sha256": authorization["authorization_sha256"],
        "runtime_config_sha256": authorization["runtime_config_sha256"],
        "credential_source_ready": True,
        "endpoint_kind": "standard",
        "provider": EXPECTED_PROVIDER,
        "model": EXPECTED_MODEL,
        "sdk_max_retries": 0,
        "public_candidate_input_only": True,
        "anonymous_public_git_read": True,
        "github_api_used": False,
        "private_workspace_diff_read": False,
        "paid_call_gate": True,
        "secret_disclosed": False,
        "checked_at": instant_text,
    }


def _reservation_delta(
    after: BudgetReservation,
    before: BudgetReservation,
) -> BudgetReservation:
    return BudgetReservation(
        logical_calls=after.logical_calls - before.logical_calls,
        http_attempts=after.http_attempts - before.http_attempts,
        input_tokens=after.input_tokens - before.input_tokens,
        output_tokens=after.output_tokens - before.output_tokens,
        cost_microcny=after.cost_microcny - before.cost_microcny,
    )


def _retention_timestamp(completed_at: str, days: int) -> str:
    completed = _canonical_timestamp(completed_at, "receipt completed_at")
    return (completed + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_private_trace(path: Path, trace: Mapping[str, Any]) -> str:
    if contains_forbidden_content(trace):
        _fail("private trace contains forbidden content")
    if path.exists():
        _fail("private trace already exists and cannot be overwritten")
    _write_json(path, trace)
    return solo.sha256_value(trace)


def _sealed_headline_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    receipt = solo.with_artifact_hash(
        {**value, "receipt_sha256": ""},
        "receipt_sha256",
    )
    validate_headline_receipt(receipt)
    return receipt


def validate_headline_receipt(raw: Any) -> dict[str, Any]:
    receipt = _expect_dict(raw, "auth3_headline_receipt")
    _exact_keys(receipt, HEADLINE_RECEIPT_KEYS, "auth3_headline_receipt")
    if receipt["schema_version"] != 1 or receipt["phase_id"] != RUN_PHASE_ID:
        _fail("headline receipt schema or phase is invalid")
    for key in ("solo_id", "run_id", "pr_id"):
        solo._expect_identifier(receipt[key], f"headline receipt {key}")
    if receipt["attempt_number"] != 1 or receipt["headline"] is not True:
        _fail("attempt 1 must be the sole headline")
    for key in (
        "authorization_sha256",
        "runtime_config_sha256",
        "temperature_profile_sha256",
        "raw_trace_sha256",
    ):
        solo._expect_sha(receipt[key], f"headline receipt {key}")
    if receipt["status"] not in {"completed", "failed", "timed_out", "cancelled"}:
        _fail("headline receipt status is invalid")
    started = _canonical_timestamp(receipt["started_at"], "headline started_at")
    completed = _canonical_timestamp(receipt["completed_at"], "headline completed_at")
    if completed < started:
        _fail("headline completion precedes start")
    for key in (
        "logical_calls",
        "http_attempts",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cost_microcny",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_cost_microcny",
    ):
        value = receipt[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail("headline usage must use non-negative integers")
    if receipt["http_attempts"] != receipt["logical_calls"]:
        _fail("zero-retry headline calls and HTTP attempts must match")
    if receipt["cached_input_tokens"] > receipt["input_tokens"]:
        _fail("headline cached input exceeds total input")
    if not isinstance(receipt["actual_usage_known"], bool):
        _fail("headline actual-usage flag is invalid")
    latency = receipt["latency_seconds"]
    if isinstance(latency, bool) or not isinstance(latency, (int, float)) or latency < 0:
        _fail("headline latency is invalid")
    if latency > (completed - started).total_seconds() + 1:
        _fail("headline latency exceeds wall interval")
    error = receipt["error_category"]
    if error is not None and (not isinstance(error, str) or not error):
        _fail("headline error category is invalid")
    if receipt["status"] == "completed":
        if error is not None or receipt["actual_usage_known"] is not True:
            _fail("completed headline requires known usage and no error")
        if receipt["logical_calls"] == 0:
            _fail("completed headline requires at least one paid logical call")
    elif error is None:
        _fail("non-completed headline requires an error category")
    finding_ids = receipt["feedback_eligible_finding_ids"]
    if not isinstance(finding_ids, list) or len(finding_ids) != len(set(finding_ids)):
        _fail("headline Finding IDs are invalid")
    for finding_id in finding_ids:
        solo._expect_identifier(finding_id, "headline Finding ID")
    if receipt["status"] != "completed" and finding_ids:
        _fail("failed headline cannot introduce feedback-eligible Findings")
    review_hash = receipt["review_sha256"]
    if review_hash is not None:
        solo._expect_sha(review_hash, "headline review hash")
    if receipt["status"] == "completed" and review_hash is None:
        _fail("completed headline requires a review hash")
    retain_until = _canonical_timestamp(
        receipt["raw_trace_retain_until"],
        "headline raw trace retention",
    )
    if retain_until < completed or retain_until > completed + timedelta(days=7):
        _fail("headline raw trace retention is invalid")
    solo.validate_artifact_hash(receipt, "receipt_sha256", "auth3_headline_receipt")
    return receipt


def _error_status(exc: BaseException) -> tuple[str, str]:
    name = type(exc).__name__
    if isinstance(exc, KeyboardInterrupt):
        return "cancelled", "process_interrupted"
    if name in {"APITimeoutError", "TimeoutError"}:
        return "timed_out", "provider_timeout"
    if name == "AuthenticationError":
        return "failed", "provider_authentication"
    if name == "RateLimitError":
        return "failed", "provider_rate_limit"
    if isinstance(exc, RunValidationError):
        return "failed", "local_budget_or_gate_refusal"
    return "failed", f"provider_or_pipeline_{name[:80]}"


def _finding_packet(pr_id: str, review: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    findings = review.get("findings", [])
    if not isinstance(findings, list):
        _fail("review findings are malformed")
    packet: list[dict[str, Any]] = []
    finding_ids: list[str] = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            _fail("review Finding is malformed")
        finding_sha256 = solo.sha256_value(finding)
        finding_id = "finding-" + hashlib.sha256(
            f"{pr_id}\n{index}\n{finding_sha256}".encode()
        ).hexdigest()[:32]
        finding_ids.append(finding_id)
        packet.append(
            {
                "schema_version": 1,
                "pr_id": pr_id,
                "finding_id": finding_id,
                "finding_sha256": finding_sha256,
                "finding": finding,
                "decision": None,
                "rationale": None,
                "fixed_at": None,
                "completed_by_human": False,
            }
        )
    return packet, finding_ids


def _blocked_headline(
    *,
    run_root: Path,
    solo_id: str,
    pr_id: str,
    authorization: Mapping[str, Any],
    runtime: Mapping[str, Any],
    at: str,
) -> dict[str, Any]:
    trace = {
        "schema_version": 1,
        "phase_id": RUN_PHASE_ID,
        "pr_id": pr_id,
        "attempt_number": 1,
        "status": "failed",
        "error_category": "selected_diff_secret_scan_hit",
        "logical_calls": 0,
        "http_attempts": 0,
        "content_retained": False,
    }
    trace_path = run_root / "traces" / f"{pr_id}.json"
    if trace_path.exists():
        existing = solo.load_json(trace_path)
        if existing != trace:
            _fail("existing blocked-headline trace differs from recovery evidence")
        trace_hash = solo.sha256_value(existing)
    else:
        trace_hash = _write_private_trace(trace_path, trace)
    return _sealed_headline_receipt(
        {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "solo_id": solo_id,
            "run_id": f"{RUN_PHASE_ID}-{pr_id}-attempt-1",
            "pr_id": pr_id,
            "attempt_number": 1,
            "headline": True,
            "authorization_sha256": authorization["authorization_sha256"],
            "runtime_config_sha256": authorization["runtime_config_sha256"],
            "temperature_profile_sha256": solo.sha256_value(
                runtime["temperature_profile"]
            ),
            "status": "failed",
            "started_at": at,
            "completed_at": at,
            "logical_calls": 0,
            "http_attempts": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "cost_microcny": 0,
            "actual_usage_known": True,
            "reserved_input_tokens": 0,
            "reserved_output_tokens": 0,
            "reserved_cost_microcny": 0,
            "latency_seconds": 0,
            "error_category": "selected_diff_secret_scan_hit",
            "feedback_eligible_finding_ids": [],
            "review_sha256": None,
            "raw_trace_sha256": trace_hash,
            "raw_trace_retain_until": _retention_timestamp(at, 7),
            "receipt_sha256": "",
        }
    )


def validate_offline_validation(raw: Any) -> dict[str, Any]:
    receipt = _expect_dict(raw, "auth3_offline_validation")
    _exact_keys(receipt, OFFLINE_VALIDATION_KEYS, "auth3_offline_validation")
    if receipt["schema_version"] != 1 or receipt["phase_id"] != RUN_PHASE_ID:
        _fail("offline validation schema or phase is invalid")
    if not isinstance(receipt["executor_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", receipt["executor_commit"]
    ):
        _fail("offline validation executor commit is invalid")
    for key in ("executor_source_sha256", "runtime_config_sha256"):
        solo._expect_sha(receipt[key], f"offline validation {key}")
    for key in (
        "dedicated_tests_passed",
        "synthetic_gate_passed",
        "solo_bundle_passed",
        "ruff_passed",
        "mypy_passed",
        "scripts_verify_passed",
        "pip_check_passed",
        "diff_check_passed",
    ):
        if receipt[key] is not True:
            _fail(f"offline validation {key} must be true")
    if receipt["external_calls_made"] is not False:
        _fail("offline validation must not make external calls")
    _canonical_timestamp(receipt["validated_at"], "offline validation timestamp")
    solo.validate_artifact_hash(receipt, "validation_sha256", "auth3_offline_validation")
    return receipt


def record_offline_validation(
    *,
    repo_root: Path,
    evidence_root: Path,
    output_path: Path,
    validated_at: str,
) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    output = output_path.resolve(strict=False)
    if not _is_within(output, repo):
        _fail("offline validation receipt must be inside the Git worktree")
    if output.exists():
        _fail("offline validation receipt already exists and cannot be overwritten")
    bundle = _load_auth3_bundle(evidence_root.resolve(strict=True))
    runtime = bundle["runtime_config"]
    if _sha256_file(repo / "phase9g_solo_run.py") != runtime["executor_source_sha256"]:
        _fail("offline validation executor hash differs from auth-003")
    receipt = solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "executor_commit": runtime["executor_commit"],
            "executor_source_sha256": runtime["executor_source_sha256"],
            "runtime_config_sha256": bundle["authorization"]["runtime_config_sha256"],
            "dedicated_tests_passed": True,
            "synthetic_gate_passed": True,
            "solo_bundle_passed": True,
            "ruff_passed": True,
            "mypy_passed": True,
            "scripts_verify_passed": True,
            "pip_check_passed": True,
            "diff_check_passed": True,
            "external_calls_made": False,
            "validated_at": validated_at,
            "validation_sha256": "",
        },
        "validation_sha256",
    )
    validate_offline_validation(receipt)
    _write_json(output, receipt)
    return {
        "valid": True,
        "executor_commit": runtime["executor_commit"],
        "runtime_config_sha256": bundle["authorization"]["runtime_config_sha256"],
        "external_calls_made": False,
        "paid_call_gate": False,
    }


def record_offline_validation_auth4(
    *,
    repo_root: Path,
    evidence_root: Path,
    public_source_receipt_path: Path,
    output_path: Path,
    validated_at: str,
) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    output = output_path.resolve(strict=False)
    if not _is_within(output, repo):
        _fail("auth-004 offline validation receipt must be inside the Git worktree")
    if output.exists():
        _fail("auth-004 offline validation receipt already exists and cannot be overwritten")
    source_receipt = validate_auth4_public_source_receipt(
        solo.load_json(public_source_receipt_path)
    )
    bundle = _load_auth4_bundle(evidence_root.resolve(strict=True), source_receipt)
    runtime = bundle["runtime_config"]
    if _sha256_file(repo / "phase9g_solo_run.py") != runtime["executor_source_sha256"]:
        _fail("offline validation executor hash differs from auth-004")
    receipt = solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "executor_commit": runtime["executor_commit"],
            "executor_source_sha256": runtime["executor_source_sha256"],
            "runtime_config_sha256": bundle["authorization"]["runtime_config_sha256"],
            "dedicated_tests_passed": True,
            "synthetic_gate_passed": True,
            "solo_bundle_passed": True,
            "ruff_passed": True,
            "mypy_passed": True,
            "scripts_verify_passed": True,
            "pip_check_passed": True,
            "diff_check_passed": True,
            "external_calls_made": False,
            "validated_at": validated_at,
            "validation_sha256": "",
        },
        "validation_sha256",
    )
    validate_offline_validation(receipt)
    _write_json(output, receipt)
    return {
        "valid": True,
        "authorization_id": AUTH4_ID,
        "executor_commit": runtime["executor_commit"],
        "runtime_config_sha256": bundle["authorization"]["runtime_config_sha256"],
        "external_calls_made": False,
        "paid_call_gate": False,
    }


def validate_headline_receipt_set(
    receipts: Sequence[Mapping[str, Any]],
    *,
    cohort: Mapping[str, Any],
    authorization: Mapping[str, Any],
) -> list[dict[str, Any]]:
    validated = [validate_headline_receipt(receipt) for receipt in receipts]
    selected = {entry["pr_id"] for entry in cohort["entries"]}
    actual = {receipt["pr_id"] for receipt in validated}
    if len(validated) != len(selected) or actual != selected:
        _fail("headline receipts do not cover the complete selected denominator")
    if any(receipt["authorization_sha256"] != authorization["authorization_sha256"] for receipt in validated):
        _fail("headline receipt authorization binding is invalid")
    if any(receipt["runtime_config_sha256"] != authorization["runtime_config_sha256"] for receipt in validated):
        _fail("headline receipt runtime binding is invalid")
    expected_blocked = authorization.get("blocked_selected_prs")
    if (
        isinstance(expected_blocked, bool)
        or not isinstance(expected_blocked, int)
        or expected_blocked < 0
        or expected_blocked > TARGET_PRS
    ):
        _fail("headline receipt authorization block count is invalid")
    if sum(
        receipt["error_category"] == "selected_diff_secret_scan_hit"
        for receipt in validated
    ) != expected_blocked:
        _fail("headline receipt secret-scan failure count is invalid")
    actual_totals = {
        "logical_calls": sum(receipt["logical_calls"] for receipt in validated),
        "http_attempts": sum(receipt["http_attempts"] for receipt in validated),
        "input_tokens": sum(receipt["input_tokens"] for receipt in validated),
        "output_tokens": sum(receipt["output_tokens"] for receipt in validated),
        "cost_microcny": sum(receipt["cost_microcny"] for receipt in validated),
    }
    reserved_totals = {
        "input_tokens": sum(receipt["reserved_input_tokens"] for receipt in validated),
        "output_tokens": sum(receipt["reserved_output_tokens"] for receipt in validated),
        "cost_microcny": sum(receipt["reserved_cost_microcny"] for receipt in validated),
    }
    for usage_key, ceiling_key in (
        ("logical_calls", "max_logical_calls"),
        ("http_attempts", "max_http_attempts"),
        ("input_tokens", "max_input_tokens"),
        ("output_tokens", "max_output_tokens"),
        ("cost_microcny", "max_cost_microcny"),
    ):
        if actual_totals[usage_key] > authorization[ceiling_key]:
            _fail(f"headline actual {usage_key} exceeds auth-003")
    for usage_key, ceiling_key in (
        ("input_tokens", "max_input_tokens"),
        ("output_tokens", "max_output_tokens"),
        ("cost_microcny", "max_cost_microcny"),
    ):
        if reserved_totals[usage_key] > authorization[ceiling_key]:
            _fail(f"headline reserved {usage_key} exceeds auth-003")
    return validated


def validate_public_run_receipt(raw: Any) -> dict[str, Any]:
    receipt = _expect_dict(raw, "public_run_receipt")
    _exact_keys(receipt, PUBLIC_RUN_RECEIPT_KEYS, "public_run_receipt")
    if receipt["schema_version"] != 1 or receipt["phase_id"] != RUN_PHASE_ID:
        _fail("public run receipt schema or phase is invalid")
    if receipt["evidence_type"] != solo.EVIDENCE_TYPE:
        _fail("public run receipt evidence type is invalid")
    for key in (
        "authorization_sha256",
        "runtime_config_sha256",
        "tariff_sha256",
        "selection_receipt_sha256",
        "cohort_sha256",
        "private_run_index_sha256",
    ):
        solo._expect_sha(receipt[key], f"public run receipt {key}")
    if receipt["selected_prs"] != TARGET_PRS or receipt["headline_attempts"] != TARGET_PRS:
        _fail("public run receipt selected/headline denominator is invalid")
    blocked = receipt["blocked_zero_call_headlines"]
    runnable = receipt["runnable_headlines"]
    if (
        isinstance(blocked, bool)
        or not isinstance(blocked, int)
        or blocked < 0
        or blocked > TARGET_PRS
        or isinstance(runnable, bool)
        or not isinstance(runnable, int)
        or runnable != TARGET_PRS - blocked
    ):
        _fail("public run receipt runnable/headline denominator is invalid")
    statuses = receipt["headline_status_counts"]
    if not isinstance(statuses, dict) or sum(statuses.values()) != TARGET_PRS:
        _fail("public run receipt status denominator is invalid")
    actual = receipt["actual_usage"]
    reserved = receipt["reserved_budget"]
    if not isinstance(actual, dict) or not isinstance(reserved, dict):
        _fail("public run receipt usage objects are invalid")
    if set(actual) != {
        "logical_calls",
        "http_attempts",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cost_microcny",
    }:
        _fail("public run receipt actual usage keys are invalid")
    if set(reserved) != {
        "logical_calls",
        "http_attempts",
        "input_tokens",
        "output_tokens",
        "cost_microcny",
    }:
        _fail("public run receipt reserved budget keys are invalid")
    for value in (*actual.values(), *reserved.values()):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail("public run receipt usage values are invalid")
    if actual["logical_calls"] != actual["http_attempts"]:
        _fail("public run receipt violates zero-retry accounting")
    if not isinstance(receipt["actual_usage_known"], bool):
        _fail("public run receipt usage-known flag is invalid")
    if receipt["feedback_responses"] != 0:
        _fail("public run receipt cannot invent human feedback")
    if receipt["feedback_status"] != "pending_human":
        _fail("public run receipt feedback status is invalid")
    if receipt["business_claim_allowed"] or receipt["quality_claim_allowed"]:
        _fail("public run receipt cannot allow business or quality claims")
    if receipt["formal_quality_status"] != "incomplete":
        _fail("public run receipt formal quality status is invalid")
    if receipt["model_quality_status"] != "not_measured":
        _fail("public run receipt model quality status is invalid")
    _canonical_timestamp(receipt["generated_at"], "public run receipt generated_at")
    solo.validate_artifact_hash(receipt, "receipt_sha256", "public_run_receipt")
    return receipt


def _finalize_run(
    *,
    run_root: Path,
    public_run: Path,
    registrations: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
    cohort: Mapping[str, Any],
    authorization: Mapping[str, Any],
    tariff: Mapping[str, Any],
    selection_receipt: Mapping[str, Any],
    blocked_headlines: int,
    runnable_headlines: int,
) -> dict[str, Any]:
    if public_run.exists() or (run_root / "run-index.private.json").exists():
        _fail("final run evidence already exists and cannot be overwritten")
    if (
        blocked_headlines + runnable_headlines != TARGET_PRS
        or authorization.get("blocked_selected_prs") != blocked_headlines
        or authorization.get("max_runnable_prs") != runnable_headlines
    ):
        _fail("final run denominator differs from the authorization")
    validated = validate_headline_receipt_set(
        receipts,
        cohort=cohort,
        authorization=authorization,
    )
    status_counts = Counter(receipt["status"] for receipt in validated)
    actual_usage = {
        "logical_calls": sum(receipt["logical_calls"] for receipt in validated),
        "http_attempts": sum(receipt["http_attempts"] for receipt in validated),
        "input_tokens": sum(receipt["input_tokens"] for receipt in validated),
        "output_tokens": sum(receipt["output_tokens"] for receipt in validated),
        "cached_input_tokens": sum(
            receipt["cached_input_tokens"] for receipt in validated
        ),
        "cost_microcny": sum(receipt["cost_microcny"] for receipt in validated),
    }
    reserved_budget = {
        "logical_calls": actual_usage["logical_calls"],
        "http_attempts": actual_usage["http_attempts"],
        "input_tokens": sum(
            receipt["reserved_input_tokens"] for receipt in validated
        ),
        "output_tokens": sum(
            receipt["reserved_output_tokens"] for receipt in validated
        ),
        "cost_microcny": sum(
            receipt["reserved_cost_microcny"] for receipt in validated
        ),
    }
    findings_path = run_root / "feedback-packet.private.jsonl"
    finding_subjects = _load_jsonl(findings_path) if findings_path.exists() else []
    private_index = {
        "schema_version": 1,
        "phase_id": RUN_PHASE_ID,
        "registrations_sha256": solo.sha256_value(list(registrations)),
        "headline_receipts_sha256": solo.sha256_value(list(validated)),
        "finding_subjects_sha256": solo.sha256_value(finding_subjects),
        "preflight_sha256": solo.sha256_value(
            solo.load_json(run_root / "preflight.json")
        ),
    }
    private_index["index_sha256"] = solo.sha256_value(private_index)
    _write_json(run_root / "run-index.private.json", private_index)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    public_receipt = solo.with_artifact_hash(
        {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "evidence_type": solo.EVIDENCE_TYPE,
            "authorization_sha256": authorization["authorization_sha256"],
            "runtime_config_sha256": authorization["runtime_config_sha256"],
            "tariff_sha256": tariff["tariff_sha256"],
            "selection_receipt_sha256": selection_receipt["receipt_sha256"],
            "cohort_sha256": cohort["cohort_sha256"],
            "selected_prs": TARGET_PRS,
            "blocked_zero_call_headlines": blocked_headlines,
            "runnable_headlines": runnable_headlines,
            "headline_attempts": TARGET_PRS,
            "headline_status_counts": dict(sorted(status_counts.items())),
            "actual_usage": actual_usage,
            "reserved_budget": reserved_budget,
            "actual_usage_known": all(
                receipt["actual_usage_known"] for receipt in validated
            ),
            "feedback_eligible_findings": len(finding_subjects),
            "feedback_responses": 0,
            "feedback_status": "pending_human",
            "business_claim_allowed": False,
            "quality_claim_allowed": False,
            "formal_quality_status": "incomplete",
            "model_quality_status": "not_measured",
            "generated_at": generated_at,
            "private_run_index_sha256": private_index["index_sha256"],
            "receipt_sha256": "",
        },
        "receipt_sha256",
    )
    validate_public_run_receipt(public_receipt)
    _write_json(public_run, public_receipt)
    return {
        "valid": True,
        "selected_prs": TARGET_PRS,
        "blocked_zero_call_headlines": blocked_headlines,
        "runnable_headlines": runnable_headlines,
        "headline_status_counts": dict(sorted(status_counts.items())),
        "actual_usage": actual_usage,
        "actual_usage_known": public_receipt["actual_usage_known"],
        "feedback_eligible_findings": len(finding_subjects),
        "feedback_status": "pending_human",
        "business_claim_allowed": False,
        "quality_claim_allowed": False,
        "formal_quality_status": "incomplete",
        "public_receipt_sha256": public_receipt["receipt_sha256"],
    }


def _execute_headlines(
    *,
    authorization_revision: str,
    repo_root: Path,
    evidence_root: Path,
    public_selection_receipt_path: Path,
    public_attestation_path: Path,
    offline_validation_path: Path,
    public_run_receipt_path: Path,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    evidence = evidence_root.resolve(strict=True)
    if _is_within(evidence, repo):
        _fail("private evidence root must be outside the Git worktree")
    public_run = public_run_receipt_path.resolve(strict=False)
    if not _is_within(public_run, repo):
        _fail("public run receipt must be inside the Git worktree")
    if authorization_revision not in {"auth-003", "auth-004"}:
        _fail("run authorization revision is invalid")
    run_suffix = authorization_revision.replace("-", "")
    run_root = evidence / f"run-{run_suffix}-001"
    staging_root = evidence / f"run-{run_suffix}-001.initializing"
    if run_root.exists() or staging_root.exists() or public_run.exists():
        _fail(f"{authorization_revision} run evidence already exists and cannot be overwritten")
    if authorization_revision == "auth-003":
        selection_receipt = validate_public_receipt(
            solo.load_json(public_selection_receipt_path)
        )
        selection = _load_private_selection(evidence)
        bundle = _load_auth3_bundle(evidence)
        attestation = validate_auth3_attestation(solo.load_json(public_attestation_path))
        diff_root = evidence / "selected-diffs"
        expected_blocked = 2
        expected_runnable = 3
    else:
        selection_receipt = validate_auth4_public_source_receipt(
            solo.load_json(public_selection_receipt_path)
        )
        selection = _load_auth4_public_selection(evidence)
        bundle = _load_auth4_bundle(evidence, selection_receipt)
        attestation = validate_auth4_attestation(solo.load_json(public_attestation_path))
        diff_root = selection["diff_root"]
        expected_blocked = AUTH4_EXPECTED_BLOCKED
        expected_runnable = AUTH4_EXPECTED_RUNNABLE
    cohort = selection["cohort"]
    authorization = bundle["authorization"]
    runtime = bundle["runtime_config"]
    tariff = bundle["tariff"]
    offline = validate_offline_validation(solo.load_json(offline_validation_path))
    if selection_receipt["cohort_sha256"] != cohort["cohort_sha256"]:
        _fail("selection receipt differs from the private cohort")
    if attestation["authorization_sha256"] != authorization["authorization_sha256"]:
        _fail(f"{authorization_revision} public/private authorization hashes differ")
    if offline["runtime_config_sha256"] != authorization["runtime_config_sha256"]:
        _fail("offline validation differs from the authorized runtime")
    if offline["executor_source_sha256"] != runtime["executor_source_sha256"]:
        _fail("offline validation differs from the authorized executor")
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if authorization_revision == "auth-003":
        preflight = preflight_auth_003(
            repo_root=repo,
            evidence_root=evidence,
            public_attestation_path=public_attestation_path,
            environment=environment,
            at=checked_at,
        )
    else:
        preflight = preflight_auth_004(
            repo_root=repo,
            evidence_root=evidence,
            public_source_receipt_path=public_selection_receipt_path,
            public_attestation_path=public_attestation_path,
            environment=environment,
            at=checked_at,
        )

    selected_ids = [entry["pr_id"] for entry in cohort["entries"]]
    candidate_by_id = {
        row["opaque_pr_id"]: row for row in selection["candidate_map"]
    }
    if set(selected_ids) != {
        pr_id for pr_id, row in candidate_by_id.items() if row.get("selected")
    }:
        _fail("private candidate map selected set differs from the cohort")
    blocked_ids: list[str] = []
    runnable_ids: list[str] = []
    for pr_id in selected_ids:
        row = candidate_by_id[pr_id]
        diff_path = diff_root / f"{pr_id}.diff"
        if _sha256_file(diff_path) != row["diff_sha256"]:
            _fail("selected diff hash differs from the frozen cohort")
        diff_bytes = diff_path.read_bytes()
        findings = _potential_secret_count(diff_bytes)
        if findings != row["potential_secret_findings"]:
            _fail("selected diff secret scan differs from materialization")
        if findings:
            blocked_ids.append(pr_id)
        else:
            runnable_ids.append(pr_id)
    if len(blocked_ids) != expected_blocked or len(runnable_ids) != expected_runnable:
        _fail(f"selected diff block/runnable counts differ from {authorization_revision}")

    credential = _auth3_credential_value(environment=environment, repo_root=repo)
    raw_client = OpenAI(
        api_key=credential,
        base_url=runtime["base_url"],
        timeout=runtime["request_timeout_seconds"],
        max_retries=0,
    )
    credential = ""
    if getattr(raw_client, "max_retries", None) != 0:
        _fail("OpenAI-compatible client did not disable retries")

    registrations = [
        solo.with_artifact_hash(
            {
                "schema_version": 1,
                "phase_id": RUN_PHASE_ID,
                "pr_id": pr_id,
                "attempt_number": 1,
                "headline": True,
                "registered_at": checked_at,
                "initial_disposition": (
                    "blocked_zero_call" if pr_id in blocked_ids else "pending_paid_call"
                ),
                "registration_sha256": "",
            },
            "registration_sha256",
        )
        for pr_id in selected_ids
    ]
    staging_root.mkdir(parents=True, exist_ok=False)
    _write_json(staging_root / "registrations.json", registrations)
    _write_json(
        staging_root / "preflight.json",
        {
            **preflight,
            "offline_validation_sha256": offline["validation_sha256"],
            "secret_file_fingerprint_retained": False,
        },
    )
    os.replace(staging_root, run_root)
    receipts_path = run_root / "headline-receipts.jsonl"
    findings_path = run_root / "feedback-packet.private.jsonl"
    receipts: list[dict[str, Any]] = []
    for pr_id in blocked_ids:
        receipt = _blocked_headline(
            run_root=run_root,
            solo_id=cohort["solo_id"],
            pr_id=pr_id,
            authorization=authorization,
            runtime=runtime,
            at=checked_at,
        )
        _append_jsonl(receipts_path, receipt)
        receipts.append(receipt)

    ledger = BudgetLedger(
        BudgetLimits(
            logical_calls=authorization["max_logical_calls"],
            http_attempts=authorization["max_http_attempts"],
            input_tokens=authorization["max_input_tokens"],
            output_tokens=authorization["max_output_tokens"],
            cost_microcny=authorization["max_cost_microcny"],
        )
    )
    interrupted: BaseException | None = None
    for pr_id in runnable_ids:
        started = datetime.now(timezone.utc)
        started_text = started.strftime("%Y-%m-%dT%H:%M:%SZ")
        before = ledger.snapshot()
        call_journal = run_root / "calls" / f"{pr_id}.jsonl"
        gate = BudgetedCompletionGate(
            raw_client.chat.completions,
            ledger=ledger,
            tariff=tariff,
            temperature_profile=runtime["temperature_profile"],
            journal_path=call_journal,
        )
        client = BudgetedOpenAIClient(gate)
        status = "failed"
        error_category: str | None = None
        review_sha256: str | None = None
        finding_ids: list[str] = []
        review_packet: list[dict[str, Any]] = []
        caught: BaseException | None = None
        try:
            diff_bytes = (diff_root / f"{pr_id}.diff").read_bytes()
            try:
                diff_text = diff_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RunValidationError("selected diff is not UTF-8") from exc
            with tempfile.TemporaryDirectory(prefix="phase9g-solo-empty-") as empty:
                review_raw = review_agent.run_review(
                    client,  # type: ignore[arg-type]
                    diff_text,
                    Path(empty),
                    EXPECTED_MODEL,
                    use_context=False,
                    use_verify=True,
                    trace=None,
                    context_mode="off",
                )
            sanitized = sanitize_value(review_raw)
            if not isinstance(sanitized.value, dict):
                _fail("sanitized review is not an object")
            if sanitized.truncated or sanitized.omitted_count:
                _fail("review redaction would lose evidence")
            if contains_forbidden_content(sanitized.value):
                _fail("sanitized review contains forbidden content")
            usage = gate.actual_usage()
            if usage.unknown_usage_calls:
                raise RunValidationError("provider usage is missing")
            review_packet, finding_ids = _finding_packet(pr_id, sanitized.value)
            review_sha256 = solo.sha256_value(sanitized.value)
            _write_json(run_root / "reviews" / f"{pr_id}.json", sanitized.value)
            for finding in review_packet:
                _append_jsonl(findings_path, finding)
            status = "completed"
        except BaseException as exc:
            status, error_category = _error_status(exc)
            caught = exc
            finding_ids = []
            review_sha256 = None
        completed = datetime.now(timezone.utc)
        completed_text = completed.strftime("%Y-%m-%dT%H:%M:%SZ")
        usage = gate.actual_usage()
        after = ledger.snapshot()
        reserved = _reservation_delta(after, before)
        trace = {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "pr_id": pr_id,
            "attempt_number": 1,
            "status": status,
            "error_category": error_category,
            "calls": gate.records(),
            "prompt_retained": False,
            "response_content_retained": False,
            "credential_retained": False,
        }
        trace_path = run_root / "traces" / f"{pr_id}.json"
        if trace_path.exists():
            existing_trace = solo.load_json(trace_path)
            if existing_trace != trace:
                _fail("existing interrupted trace differs from recovery evidence")
            trace_hash = solo.sha256_value(existing_trace)
        else:
            trace_hash = _write_private_trace(trace_path, trace)
        receipt = _sealed_headline_receipt(
            {
                "schema_version": 1,
                "phase_id": RUN_PHASE_ID,
                "solo_id": cohort["solo_id"],
                "run_id": f"{RUN_PHASE_ID}-{pr_id}-attempt-1",
                "pr_id": pr_id,
                "attempt_number": 1,
                "headline": True,
                "authorization_sha256": authorization["authorization_sha256"],
                "runtime_config_sha256": authorization["runtime_config_sha256"],
                "temperature_profile_sha256": solo.sha256_value(
                    runtime["temperature_profile"]
                ),
                "status": status,
                "started_at": started_text,
                "completed_at": completed_text,
                "logical_calls": usage.logical_calls,
                "http_attempts": usage.http_attempts,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "cost_microcny": usage.cost_microcny,
                "actual_usage_known": usage.unknown_usage_calls == 0,
                "reserved_input_tokens": reserved.input_tokens,
                "reserved_output_tokens": reserved.output_tokens,
                "reserved_cost_microcny": reserved.cost_microcny,
                "latency_seconds": round((completed - started).total_seconds(), 6),
                "error_category": error_category,
                "feedback_eligible_finding_ids": finding_ids,
                "review_sha256": review_sha256,
                "raw_trace_sha256": trace_hash,
                "raw_trace_retain_until": _retention_timestamp(completed_text, 7),
                "receipt_sha256": "",
            }
        )
        _append_jsonl(receipts_path, receipt)
        receipts.append(receipt)
        if caught is not None and not isinstance(caught, Exception):
            interrupted = caught
            break

    if interrupted is not None:
        raise interrupted
    return _finalize_run(
        run_root=run_root,
        public_run=public_run,
        registrations=registrations,
        receipts=receipts,
        cohort=cohort,
        authorization=authorization,
        tariff=tariff,
        selection_receipt=selection_receipt,
        blocked_headlines=expected_blocked,
        runnable_headlines=expected_runnable,
    )


def execute_auth3_headlines(
    *,
    repo_root: Path,
    evidence_root: Path,
    public_selection_receipt_path: Path,
    public_auth3_attestation_path: Path,
    offline_validation_path: Path,
    public_run_receipt_path: Path,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return _execute_headlines(
        authorization_revision="auth-003",
        repo_root=repo_root,
        evidence_root=evidence_root,
        public_selection_receipt_path=public_selection_receipt_path,
        public_attestation_path=public_auth3_attestation_path,
        offline_validation_path=offline_validation_path,
        public_run_receipt_path=public_run_receipt_path,
        environment=environment,
    )


def execute_auth4_headlines(
    *,
    repo_root: Path,
    evidence_root: Path,
    public_source_receipt_path: Path,
    public_auth4_attestation_path: Path,
    offline_validation_path: Path,
    public_run_receipt_path: Path,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return _execute_headlines(
        authorization_revision="auth-004",
        repo_root=repo_root,
        evidence_root=evidence_root,
        public_selection_receipt_path=public_source_receipt_path,
        public_attestation_path=public_auth4_attestation_path,
        offline_validation_path=offline_validation_path,
        public_run_receipt_path=public_run_receipt_path,
        environment=environment,
    )


def _recover_interrupted_run(
    *,
    authorization_revision: str,
    repo_root: Path,
    evidence_root: Path,
    public_selection_receipt_path: Path,
    public_run_receipt_path: Path,
) -> dict[str, Any]:
    repo = repo_root.resolve(strict=True)
    evidence = evidence_root.resolve(strict=True)
    if _is_within(evidence, repo):
        _fail("private evidence root must be outside the Git worktree")
    if authorization_revision not in {"auth-003", "auth-004"}:
        _fail("recovery authorization revision is invalid")
    run_suffix = authorization_revision.replace("-", "")
    run_root = evidence / f"run-{run_suffix}-001"
    staging_root = evidence / f"run-{run_suffix}-001.initializing"
    public_run = public_run_receipt_path.resolve(strict=False)
    if not run_root.exists() and staging_root.is_dir():
        if not (staging_root / "registrations.json").is_file() or not (
            staging_root / "preflight.json"
        ).is_file():
            _fail(
                f"{authorization_revision} initialization was interrupted before headline registration"
            )
        os.replace(staging_root, run_root)
    if not run_root.is_dir():
        _fail(f"no interrupted {authorization_revision} run exists")
    if public_run.exists() or (run_root / "run-index.private.json").exists():
        _fail(f"{authorization_revision} run is already finalized")
    if authorization_revision == "auth-003":
        selection_receipt = validate_public_receipt(
            solo.load_json(public_selection_receipt_path)
        )
        selection = _load_private_selection(evidence)
        bundle = _load_auth3_bundle(evidence)
        expected_blocked = 2
        expected_runnable = 3
    else:
        selection_receipt = validate_auth4_public_source_receipt(
            solo.load_json(public_selection_receipt_path)
        )
        selection = _load_auth4_public_selection(evidence)
        bundle = _load_auth4_bundle(evidence, selection_receipt)
        expected_blocked = AUTH4_EXPECTED_BLOCKED
        expected_runnable = AUTH4_EXPECTED_RUNNABLE
    cohort = selection["cohort"]
    authorization = bundle["authorization"]
    runtime = bundle["runtime_config"]
    tariff = bundle["tariff"]
    registrations_raw = solo.load_json(run_root / "registrations.json")
    if not isinstance(registrations_raw, list) or len(registrations_raw) != 5:
        _fail("interrupted run registrations are invalid")
    registrations: list[dict[str, Any]] = []
    for raw in registrations_raw:
        registration = _expect_dict(raw, "headline registration")
        solo.validate_artifact_hash(
            registration,
            "registration_sha256",
            "headline registration",
        )
        registrations.append(registration)
    selected = {entry["pr_id"] for entry in cohort["entries"]}
    if {row["pr_id"] for row in registrations} != selected:
        _fail("interrupted run registrations differ from the cohort")
    blocked_registrations = sum(
        row.get("initial_disposition") == "blocked_zero_call" for row in registrations
    )
    pending_registrations = sum(
        row.get("initial_disposition") == "pending_paid_call" for row in registrations
    )
    if (
        blocked_registrations != expected_blocked
        or pending_registrations != expected_runnable
    ):
        _fail("interrupted run registration dispositions are invalid")
    receipts_path = run_root / "headline-receipts.jsonl"
    receipts = _load_jsonl(receipts_path) if receipts_path.exists() else []
    for receipt in receipts:
        validate_headline_receipt(receipt)
    completed_ids = {receipt["pr_id"] for receipt in receipts}
    recovered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for registration in registrations:
        pr_id = registration["pr_id"]
        if pr_id in completed_ids:
            continue
        if registration["initial_disposition"] == "blocked_zero_call":
            receipt = _blocked_headline(
                run_root=run_root,
                solo_id=cohort["solo_id"],
                pr_id=pr_id,
                authorization=authorization,
                runtime=runtime,
                at=recovered_at,
            )
            _append_jsonl(receipts_path, receipt)
            receipts.append(receipt)
            continue
        call_path = run_root / "calls" / f"{pr_id}.jsonl"
        calls = _load_jsonl(call_path) if call_path.exists() else []
        for call in calls:
            solo.validate_artifact_hash(call, "call_sha256", "recovered call record")
        known = [call for call in calls if call.get("usage_known") is True]
        actual_usage_known = len(known) == len(calls)
        started_at = (
            str(calls[0]["started_at"])
            if calls
            else str(registration["registered_at"])
        )
        started = _canonical_timestamp(started_at, "recovered headline start")
        completed = _canonical_timestamp(recovered_at, "recovered headline completion")
        trace = {
            "schema_version": 1,
            "phase_id": RUN_PHASE_ID,
            "pr_id": pr_id,
            "attempt_number": 1,
            "status": "cancelled",
            "error_category": "process_interrupted",
            "calls": calls,
            "prompt_retained": False,
            "response_content_retained": False,
            "credential_retained": False,
        }
        trace_path = run_root / "traces" / f"{pr_id}.json"
        if trace_path.exists():
            existing_trace = solo.load_json(trace_path)
            if existing_trace != trace:
                _fail("existing interrupted trace differs from recovery evidence")
            trace_hash = solo.sha256_value(existing_trace)
        else:
            trace_hash = _write_private_trace(trace_path, trace)
        receipt = _sealed_headline_receipt(
            {
                "schema_version": 1,
                "phase_id": RUN_PHASE_ID,
                "solo_id": cohort["solo_id"],
                "run_id": f"{RUN_PHASE_ID}-{pr_id}-attempt-1",
                "pr_id": pr_id,
                "attempt_number": 1,
                "headline": True,
                "authorization_sha256": authorization["authorization_sha256"],
                "runtime_config_sha256": authorization["runtime_config_sha256"],
                "temperature_profile_sha256": solo.sha256_value(
                    runtime["temperature_profile"]
                ),
                "status": "cancelled",
                "started_at": started_at,
                "completed_at": recovered_at,
                "logical_calls": len(calls),
                "http_attempts": len(calls),
                "input_tokens": sum(int(call["input_tokens"]) for call in known),
                "output_tokens": sum(int(call["output_tokens"]) for call in known),
                "cached_input_tokens": sum(
                    int(call["cached_input_tokens"]) for call in known
                ),
                "cost_microcny": sum(int(call["cost_microcny"]) for call in known),
                "actual_usage_known": actual_usage_known,
                "reserved_input_tokens": sum(
                    int(call["reserved_input_tokens"]) for call in calls
                ),
                "reserved_output_tokens": sum(
                    int(call["reserved_output_tokens"]) for call in calls
                ),
                "reserved_cost_microcny": sum(
                    int(call["reserved_cost_microcny"]) for call in calls
                ),
                "latency_seconds": max(0, (completed - started).total_seconds()),
                "error_category": "process_interrupted",
                "feedback_eligible_finding_ids": [],
                "review_sha256": None,
                "raw_trace_sha256": trace_hash,
                "raw_trace_retain_until": _retention_timestamp(recovered_at, 7),
                "receipt_sha256": "",
            }
        )
        _append_jsonl(receipts_path, receipt)
        receipts.append(receipt)
    return _finalize_run(
        run_root=run_root,
        public_run=public_run,
        registrations=registrations,
        receipts=receipts,
        cohort=cohort,
        authorization=authorization,
        tariff=tariff,
        selection_receipt=selection_receipt,
        blocked_headlines=expected_blocked,
        runnable_headlines=expected_runnable,
    )


def recover_interrupted_auth3_run(
    *,
    repo_root: Path,
    evidence_root: Path,
    public_selection_receipt_path: Path,
    public_run_receipt_path: Path,
) -> dict[str, Any]:
    return _recover_interrupted_run(
        authorization_revision="auth-003",
        repo_root=repo_root,
        evidence_root=evidence_root,
        public_selection_receipt_path=public_selection_receipt_path,
        public_run_receipt_path=public_run_receipt_path,
    )


def recover_interrupted_auth4_run(
    *,
    repo_root: Path,
    evidence_root: Path,
    public_source_receipt_path: Path,
    public_run_receipt_path: Path,
) -> dict[str, Any]:
    return _recover_interrupted_run(
        authorization_revision="auth-004",
        repo_root=repo_root,
        evidence_root=evidence_root,
        public_selection_receipt_path=public_source_receipt_path,
        public_run_receipt_path=public_run_receipt_path,
    )


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
        description="Materialize, validate, and gate Phase 9G-Solo-Run evidence"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("initialize-auth-002")
    initialize.add_argument("--repo-root", required=True)
    initialize.add_argument("--output-root", required=True)
    initialize_auth3 = commands.add_parser("initialize-auth-003")
    initialize_auth3.add_argument("--repo-root", required=True)
    initialize_auth3.add_argument("--evidence-root", required=True)
    initialize_auth3.add_argument("--selection-receipt", required=True)
    initialize_auth3.add_argument("--public-attestation", required=True)
    initialize_auth3.add_argument("--approved-at", required=True)
    public_source_auth4 = commands.add_parser("materialize-auth-004-public-source")
    public_source_auth4.add_argument("--repo-root", required=True)
    public_source_auth4.add_argument("--evidence-root", required=True)
    public_source_auth4.add_argument("--public-receipt", required=True)
    public_source_auth4.add_argument("--generated-at", required=True)
    initialize_auth4 = commands.add_parser("initialize-auth-004")
    initialize_auth4.add_argument("--repo-root", required=True)
    initialize_auth4.add_argument("--evidence-root", required=True)
    initialize_auth4.add_argument("--public-source-receipt", required=True)
    initialize_auth4.add_argument("--public-attestation", required=True)
    initialize_auth4.add_argument("--approved-at", required=True)
    materialize = commands.add_parser("materialize-selection")
    materialize.add_argument("--repo-root", required=True)
    materialize.add_argument("--private-root", required=True)
    materialize.add_argument("--public-receipt", required=True)
    materialize.add_argument("--authorization", required=True)
    materialize.add_argument("--runtime-config", required=True)
    materialize.add_argument("--generated-at", required=True)
    validate_receipt = commands.add_parser("validate-public-receipt")
    validate_receipt.add_argument("--receipt", required=True)
    validate_public_source_auth4 = commands.add_parser(
        "validate-auth-004-public-source"
    )
    validate_public_source_auth4.add_argument("--receipt", required=True)
    validate_attestation = commands.add_parser("validate-auth-003-attestation")
    validate_attestation.add_argument("--attestation", required=True)
    validate_attestation_auth4 = commands.add_parser("validate-auth-004-attestation")
    validate_attestation_auth4.add_argument("--attestation", required=True)
    credential = commands.add_parser("preflight-credential")
    credential.add_argument("--authorization", required=True)
    credential.add_argument("--runtime-config", required=True)
    credential.add_argument("--repo-root", required=True)
    credential_auth3 = commands.add_parser("preflight-auth-003")
    credential_auth3.add_argument("--repo-root", required=True)
    credential_auth3.add_argument("--evidence-root", required=True)
    credential_auth3.add_argument("--public-attestation", required=True)
    credential_auth4 = commands.add_parser("preflight-auth-004")
    credential_auth4.add_argument("--repo-root", required=True)
    credential_auth4.add_argument("--evidence-root", required=True)
    credential_auth4.add_argument("--public-source-receipt", required=True)
    credential_auth4.add_argument("--public-attestation", required=True)
    record_validation = commands.add_parser("record-offline-validation")
    record_validation.add_argument("--repo-root", required=True)
    record_validation.add_argument("--evidence-root", required=True)
    record_validation.add_argument("--output", required=True)
    record_validation.add_argument("--validated-at", required=True)
    record_validation_auth4 = commands.add_parser("record-offline-validation-auth-004")
    record_validation_auth4.add_argument("--repo-root", required=True)
    record_validation_auth4.add_argument("--evidence-root", required=True)
    record_validation_auth4.add_argument("--public-source-receipt", required=True)
    record_validation_auth4.add_argument("--output", required=True)
    record_validation_auth4.add_argument("--validated-at", required=True)
    execute_auth3 = commands.add_parser("execute-auth-003-headlines")
    execute_auth3.add_argument("--repo-root", required=True)
    execute_auth3.add_argument("--evidence-root", required=True)
    execute_auth3.add_argument("--selection-receipt", required=True)
    execute_auth3.add_argument("--public-attestation", required=True)
    execute_auth3.add_argument("--offline-validation", required=True)
    execute_auth3.add_argument("--public-run-receipt", required=True)
    execute_auth4 = commands.add_parser("execute-auth-004-headlines")
    execute_auth4.add_argument("--repo-root", required=True)
    execute_auth4.add_argument("--evidence-root", required=True)
    execute_auth4.add_argument("--public-source-receipt", required=True)
    execute_auth4.add_argument("--public-attestation", required=True)
    execute_auth4.add_argument("--offline-validation", required=True)
    execute_auth4.add_argument("--public-run-receipt", required=True)
    validate_run = commands.add_parser("validate-public-run-receipt")
    validate_run.add_argument("--receipt", required=True)
    recover_auth3 = commands.add_parser("recover-interrupted-auth-003")
    recover_auth3.add_argument("--repo-root", required=True)
    recover_auth3.add_argument("--evidence-root", required=True)
    recover_auth3.add_argument("--selection-receipt", required=True)
    recover_auth3.add_argument("--public-run-receipt", required=True)
    recover_auth4 = commands.add_parser("recover-interrupted-auth-004")
    recover_auth4.add_argument("--repo-root", required=True)
    recover_auth4.add_argument("--evidence-root", required=True)
    recover_auth4.add_argument("--public-source-receipt", required=True)
    recover_auth4.add_argument("--public-run-receipt", required=True)
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
        elif args.command == "initialize-auth-003":
            result = initialize_auth_003(
                repo_root=Path(args.repo_root),
                evidence_root=Path(args.evidence_root),
                selection_receipt_path=Path(args.selection_receipt),
                public_attestation_path=Path(args.public_attestation),
                approved_at=args.approved_at,
            )
        elif args.command == "materialize-auth-004-public-source":
            result = materialize_auth4_public_source(
                repo_root=Path(args.repo_root),
                evidence_root=Path(args.evidence_root),
                public_receipt_path=Path(args.public_receipt),
                generated_at=args.generated_at,
            )
        elif args.command == "initialize-auth-004":
            result = initialize_auth_004(
                repo_root=Path(args.repo_root),
                evidence_root=Path(args.evidence_root),
                public_source_receipt_path=Path(args.public_source_receipt),
                public_attestation_path=Path(args.public_attestation),
                approved_at=args.approved_at,
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
        elif args.command == "validate-auth-004-public-source":
            receipt = validate_auth4_public_source_receipt(solo.load_json(args.receipt))
            result = {
                "valid": True,
                "authorization_id": AUTH4_ID,
                "candidate_prs": receipt["candidate_prs"],
                "selected_prs": receipt["selected_prs"],
                "anonymous_public_source": True,
                "private_workspace_diff_read": False,
                "paid_call_gate": False,
            }
        elif args.command == "validate-auth-003-attestation":
            attestation = validate_auth3_attestation(solo.load_json(args.attestation))
            result = {
                "valid": True,
                "authorization_id": attestation["authorization_id"],
                "authorization_complete": True,
                "paid_call_gate": False,
            }
        elif args.command == "validate-auth-004-attestation":
            attestation = validate_auth4_attestation(solo.load_json(args.attestation))
            result = {
                "valid": True,
                "authorization_id": attestation["authorization_id"],
                "authorization_complete": True,
                "public_candidate_input_only": True,
                "paid_call_gate": False,
            }
        elif args.command == "preflight-credential":
            result = credential_preflight(
                solo.load_json(args.authorization),
                solo.load_json(args.runtime_config),
                repo_root=Path(args.repo_root),
            )
        elif args.command == "preflight-auth-003":
            result = preflight_auth_003(
                repo_root=Path(args.repo_root),
                evidence_root=Path(args.evidence_root),
                public_attestation_path=Path(args.public_attestation),
            )
        elif args.command == "preflight-auth-004":
            result = preflight_auth_004(
                repo_root=Path(args.repo_root),
                evidence_root=Path(args.evidence_root),
                public_source_receipt_path=Path(args.public_source_receipt),
                public_attestation_path=Path(args.public_attestation),
            )
        elif args.command == "record-offline-validation":
            result = record_offline_validation(
                repo_root=Path(args.repo_root),
                evidence_root=Path(args.evidence_root),
                output_path=Path(args.output),
                validated_at=args.validated_at,
            )
        elif args.command == "record-offline-validation-auth-004":
            result = record_offline_validation_auth4(
                repo_root=Path(args.repo_root),
                evidence_root=Path(args.evidence_root),
                public_source_receipt_path=Path(args.public_source_receipt),
                output_path=Path(args.output),
                validated_at=args.validated_at,
            )
        elif args.command == "execute-auth-003-headlines":
            result = execute_auth3_headlines(
                repo_root=Path(args.repo_root),
                evidence_root=Path(args.evidence_root),
                public_selection_receipt_path=Path(args.selection_receipt),
                public_auth3_attestation_path=Path(args.public_attestation),
                offline_validation_path=Path(args.offline_validation),
                public_run_receipt_path=Path(args.public_run_receipt),
            )
        elif args.command == "execute-auth-004-headlines":
            result = execute_auth4_headlines(
                repo_root=Path(args.repo_root),
                evidence_root=Path(args.evidence_root),
                public_source_receipt_path=Path(args.public_source_receipt),
                public_auth4_attestation_path=Path(args.public_attestation),
                offline_validation_path=Path(args.offline_validation),
                public_run_receipt_path=Path(args.public_run_receipt),
            )
        elif args.command == "validate-public-run-receipt":
            receipt = validate_public_run_receipt(solo.load_json(args.receipt))
            result = {
                "valid": True,
                "selected_prs": receipt["selected_prs"],
                "headline_attempts": receipt["headline_attempts"],
                "actual_usage_known": receipt["actual_usage_known"],
                "feedback_status": receipt["feedback_status"],
                "business_claim_allowed": False,
                "quality_claim_allowed": False,
            }
        elif args.command == "recover-interrupted-auth-003":
            result = recover_interrupted_auth3_run(
                repo_root=Path(args.repo_root),
                evidence_root=Path(args.evidence_root),
                public_selection_receipt_path=Path(args.selection_receipt),
                public_run_receipt_path=Path(args.public_run_receipt),
            )
        elif args.command == "recover-interrupted-auth-004":
            result = recover_interrupted_auth4_run(
                repo_root=Path(args.repo_root),
                evidence_root=Path(args.evidence_root),
                public_source_receipt_path=Path(args.public_source_receipt),
                public_run_receipt_path=Path(args.public_run_receipt),
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
