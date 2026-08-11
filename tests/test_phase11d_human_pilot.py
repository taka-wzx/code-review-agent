from __future__ import annotations

import copy
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from unittest import mock

import phase11d_human_pilot as pilot
import phase11d_gate_b_executor as gate_b_executor


def _bundle() -> dict[str, object]:
    return copy.deepcopy(pilot.build_gate_a_bundle())


def _rows(files: dict[str, object], name: str) -> list[dict[str, object]]:
    rows = files[name]
    assert isinstance(rows, list)
    return rows


def _object(files: dict[str, object], name: str) -> dict[str, object]:
    value = files[name]
    assert isinstance(value, dict)
    return value


def _write_bundle(root: Path, files: dict[str, object], *, rebuild_manifest: bool) -> None:
    body = {name: value for name, value in files.items() if name != "canonical-manifest.json"}
    if rebuild_manifest:
        files["canonical-manifest.json"] = pilot._manifest(body)
    for name, value in files.items():
        path = root / name
        if name.endswith(".jsonl"):
            assert isinstance(value, list)
            pilot._write_jsonl(path, value)
        else:
            assert isinstance(value, dict)
            pilot._write_json(path, value)


def _rehash_authorization(authorization: dict[str, object]) -> None:
    authorization["canonical_authorization_sha256"] = ""
    authorization["canonical_authorization_sha256"] = pilot._self_hash(
        authorization,
        "canonical_authorization_sha256",
    )


def _valid_parts() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    files = _bundle()
    return (
        _object(files, "cohort.json"),
        _object(files, "headline-manifest.json"),
        _object(files, "authorization.json"),
        _rows(files, "review-receipts.jsonl"),
        _rows(files, "repair-receipts.jsonl"),
        _rows(files, "draft-pr-receipts.jsonl"),
        _rows(files, "feedback-receipts.jsonl"),
        _rows(files, "time-cost-latency-receipts.jsonl"),
        _rows(files, "incident-stop-receipts.jsonl"),
    )


def _gate_b_real_inputs() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    participants: dict[str, object] = {
        "consent_version": "phase11d-gate-b-consent-v1",
        "generated_at": "2026-08-05T20:02:36Z",
        "identity_custodian_id": "custodian-01",
        "manifest_sha256": "",
        "participants": [
            {
                "confirmed_real": True,
                "consent_expires_at": "2026-09-05T23:59:59Z",
                "consent_scope": ["business_feedback", "review_time"],
                "consented_at": "2026-08-05T12:00:00Z",
                "feedback_retention_days": 30,
                "participant_id": "p-01",
                "repository_ids": ["repo-1301558766"],
                "role": "maintainer",
                "withdrawal_acknowledged": True,
            },
            {
                "confirmed_real": True,
                "consent_expires_at": "2026-09-05T23:59:59Z",
                "consent_scope": ["business_feedback", "review_time"],
                "consented_at": "2026-08-05T12:00:00Z",
                "feedback_retention_days": 30,
                "participant_id": "p-02",
                "repository_ids": ["repo-1301558766"],
                "role": "maintainer",
                "withdrawal_acknowledged": True,
            },
            {
                "confirmed_real": True,
                "consent_expires_at": "2026-09-05T23:59:59Z",
                "consent_scope": ["business_feedback", "review_time"],
                "consented_at": "2026-08-05T12:00:00Z",
                "feedback_retention_days": 30,
                "participant_id": "p-03",
                "repository_ids": ["repo-1301558766"],
                "role": "org_admin",
                "withdrawal_acknowledged": True,
            },
        ],
        "phase_id": "phase11d-gate-b-human-pilot-v1",
        "pilot_id": "phase11d-gate-b-human-pilot-v1-20260805-001",
        "schema_version": 1,
        "synthetic": False,
    }
    participants["manifest_sha256"] = gate_b_executor._self_hash(participants, "manifest_sha256")

    repository: dict[str, object] = {
        "generated_at": "2026-08-05T14:32:00Z",
        "manifest_sha256": "",
        "phase_id": "phase11d-gate-b-human-pilot-v1",
        "pilot_id": "phase11d-gate-b-human-pilot-v1-20260805-001",
        "repositories": [
            {
                "allowed_tracks": ["business"],
                "authorization_expires_at": "2026-09-05T23:59:59Z",
                "authorized_at": "2026-08-05T09:54:00Z",
                "authorized_by": "p-03",
                "data_retention_days": 30,
                "locator_sha256": "c" * 64,
                "publication_authorized": True,
                "publish_mode": "publish",
                "raw_diff_read_authorized": True,
                "real_github_api_authorized": True,
                "repository_id": "repo-1301558766",
                "repository_sha256": "d" * 64,
            }
        ],
        "schema_version": 1,
        "synthetic": False,
    }
    repository["manifest_sha256"] = gate_b_executor._self_hash(repository, "manifest_sha256")

    descriptor = pilot.build_credential_descriptor(
        authorization_id="phase11d-gate-b-human-pilot-v1-20260805-001",
        credential_descriptor_id="phase11d-gate-b-credentials-20260805-001",
        github_app_id=4421400,
        github_app_installation_id=149747930,
        github_app_private_key_fingerprint_sha256=gate_b_executor.sha256_bytes(b"test-private-key"),
        provider_id="zhipu",
        provider_model_snapshot="glm-5.2",
        provider_api_key_fingerprint_sha256=gate_b_executor.sha256_text("test-provider-key"),
        credential_delivery_mode="local_secret_store_to_ephemeral_process_environment",
        credential_revoke_procedure="github_delete_private_key_and_zhipu_disable_api_key",
    )

    draft: dict[str, object] = {
        "business_claim_allowed": False,
        "exact_approval_text": "PENDING_FREEZE",
        "formal_quality_status": "incomplete",
        "gate_b_allowed": False,
        "generated_at_utc": "2026-08-05T12:00:00Z",
        "model_quality_status": "not_measured",
        "permission_switches": {
            "allow_agent_push_merge_master": False,
            "allow_auto_merge": False,
            "allow_comments_checks_labels_reviews": False,
            "allow_default_branch_mutation": False,
            "allow_pilot_pr_merge": False,
            "allow_pilot_pr_ready": False,
            "allow_real_draft_repair_pr": True,
            "allow_real_github_repair_branch_push": True,
            "allow_real_provider_calls": True,
        },
        "required_fields": {
            "allowed_base_branch_rule": {
                "base_branch": "master",
                "base_sha_rule": "read_and_pin_at_selection",
                "protected_branch_mutation": False,
            },
            "authorization_id": "phase11d-gate-b-human-pilot-v1-20260805-001",
            "business_success_thresholds": {
                "adoption_rate_permille": 500,
                "business_claim_requires_owner_signoff": True,
                "feedback_coverage_permille": 700,
                "headline_completion_permille": 800,
            },
            "canonical_authorization_sha256": None,
            "cost_stop_thresholds": {
                "max_http_attempts": 80,
                "max_logical_calls": 40,
                "max_micro_cny": 100000000,
            },
            "credential_delivery_mode": "local_secret_store_to_ephemeral_process_environment",
            "credential_fingerprint_sha256": descriptor["credential_descriptor_sha256"],
            "credential_revoke_procedure": "github_delete_private_key_and_zhipu_disable_api_key",
            "data_classification": "restricted_source_code",
            "deletion_owner_process": {
                "owner": "p-03",
                "process": "purge_raw_content_at_retention_deadline_and_record_hash_only_receipt",
            },
            "deterministic_selection_rule": "sha256_rank_lowest_eligible_pr_ids_no_replacement_after_failure",
            "deterministic_selection_seed_sha256": "e" * 64,
            "feedback_retention_days": 30,
            "frozen_deployment_sha256": None,
            "frozen_executable_source_sha256": None,
            "frozen_runtime_identity_sha256": None,
            "frozen_runtime_image_sha256": None,
            "frozen_source_tree_sha256": None,
            "github_app_installation_id": 149747930,
            "github_repository_scopes": {"contents": "write", "metadata": "read", "pull_requests": "write"},
            "human_approval_sla_seconds": 86400,
            "incident_owner": "p-03",
            "kill_switch": {
                "activation": "stop_new_jobs_revoke_or_isolate_credentials_quarantine_unresolved",
                "owner": "p-03",
            },
            "max_cached_tokens": 1000000,
            "max_http_attempts": 80,
            "max_input_tokens": 2000000,
            "max_logical_calls": 40,
            "max_micro_cny": 100000000,
            "max_output_tokens": 400000,
            "max_real_branches": 1,
            "max_real_commits": 1,
            "max_real_draft_repair_prs": 1,
            "max_real_pushes": 1,
            "max_repair_findings_per_pr": 1,
            "max_repair_jobs": 1,
            "max_wall_clock_seconds": 7200,
            "metadata_retention_days": 90,
            "organization_id": "github-account-186135139",
            "participant_consent_receipt_sha256": participants["manifest_sha256"],
            "participant_roles": {"p-01": "maintainer", "p-02": "maintainer", "p-03": "org_admin"},
            "participant_stable_ids": ["p-01", "p-02", "p-03"],
            "pr_selection_window_end_utc": "2026-09-05T23:59:59Z",
            "pr_selection_window_start_utc": "2026-08-05T12:00:00Z",
            "provider_endpoint_allowlist": ["https://open.bigmodel.cn/api/paas/v4/chat/completions"],
            "provider_model_snapshot": {"api_family": "openai_compatible", "model": "glm-5.2", "provider": "zhipu"},
            "provider_sendable_code_scope": "selected_authorized_pr_diff_and_minimal_context_after_secret_scan",
            "raw_content_retention_days": 0,
            "repository_allowlist": {
                "repository_authorization_sha256": "d" * 64,
                "repository_ids": ["repo-1301558766"],
            },
            "safety_stop_thresholds": {
                "max_duplicate_external_writes": 0,
                "max_protected_branch_writes": 0,
                "max_unauthorized_operations": 0,
                "stop_on_credential_revoke_or_expiry": True,
                "stop_on_provider_text_only_response": True,
                "stop_on_publisher_ambiguous_result": True,
            },
            "selected_pr_count": 20,
        },
        "schema_version": pilot.AUTHORIZATION_SCHEMA_VERSION,
        "template_id": "phase11d-gate-b-authorization-draft-v1",
        "template_status": "runtime_preflight_frozen_real_executor_not_implemented",
    }
    return draft, participants, repository, descriptor


class _FakeJsonTransport:
    def __init__(self, responses: dict[tuple[str, str], gate_b_executor.HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: object,
        payload: object,
        timeout_seconds: int,
    ) -> gate_b_executor.HttpResponse:
        del headers, payload, timeout_seconds
        self.calls.append((method, url))
        return self.responses[(method, url)]


def _json_response(value: object, *, status: int = 200) -> gate_b_executor.HttpResponse:
    return gate_b_executor.HttpResponse(
        status=status,
        headers={},
        body=json.dumps(value, sort_keys=True).encode("utf-8"),
    )


def _approved_gate_b_context() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    source_root = Path(__file__).resolve().parents[1]
    runtime = gate_b_executor.freeze_executor_runtime(
        source_root=source_root,
        authorization_id="phase11d-gate-b-human-pilot-v1-20260805-001",
        executor_id="phase11d-gate-b-executor-20260806-001",
        created_at_utc="2026-08-06T00:00:00Z",
    )
    draft, participants, repository, descriptor = _gate_b_real_inputs()
    frozen = gate_b_executor.freeze_authorization(
        draft=draft,
        participants=participants,
        repository=repository,
        credential_descriptor=descriptor,
        runtime=runtime,
    )
    approved = gate_b_executor.approve_authorization(
        frozen=frozen,
        participants=participants,
        actor_id="p-03",
        approved_at_utc="2026-08-06T00:01:00Z",
        exact_approval_text=gate_b_executor.build_exact_approval_text(frozen),
    )
    return approved, participants, repository, descriptor, runtime


class _PublisherTransport:
    def __init__(
        self,
        *,
        owner: str,
        repository: str,
        base_sha: str,
        expected_tree_sha: str,
        expected_commit_sha: str,
        current_base_sha: str | None = None,
        source_tree_sha: str = "b" * 40,
        compare_status: str = "ahead",
        fail_after_draft_pr: bool = False,
    ) -> None:
        self.owner = owner
        self.repository = repository
        self.base_sha = base_sha
        self.expected_tree_sha = expected_tree_sha
        self.expected_commit_sha = expected_commit_sha
        self.source_tree_sha = source_tree_sha
        self.compare_status = compare_status
        self.fail_after_draft_pr = fail_after_draft_pr
        self.refs = {"master": current_base_sha or base_sha}
        self.prs: list[dict[str, object]] = []
        self.calls: list[tuple[str, str]] = []
        self.commit_parents: list[list[str]] = []

    @property
    def base_path(self) -> str:
        return f"/repos/{self.owner}/{self.repository}"

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: object,
        payload: object,
        timeout_seconds: int,
    ) -> gate_b_executor.HttpResponse:
        del headers, timeout_seconds
        parsed = urllib_parse.urlsplit(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self.calls.append((method, path))
        if method == "GET" and path == self.base_path:
            return _json_response({"id": 1301558766, "archived": False, "disabled": False})
        commit_prefix = f"{self.base_path}/git/commits/"
        if method == "GET" and path.startswith(commit_prefix):
            return _json_response(
                {
                    "sha": path.removeprefix(commit_prefix),
                    "tree": {"sha": self.source_tree_sha},
                }
            )
        compare_prefix = f"{self.base_path}/compare/"
        if method == "GET" and path.startswith(compare_prefix):
            base_sha, current_sha = path.removeprefix(compare_prefix).split("...", 1)
            del current_sha
            return _json_response(
                {
                    "status": self.compare_status,
                    "ahead_by": 1 if self.compare_status == "ahead" else 0,
                    "behind_by": 0,
                    "base_commit": {"sha": base_sha},
                    "merge_base_commit": {"sha": base_sha},
                }
            )
        prefix = f"{self.base_path}/git/ref/heads/"
        if method == "GET" and path.startswith(prefix):
            branch = urllib_parse.unquote(path[len(prefix) :])
            sha = self.refs.get(branch)
            if sha is None:
                return _json_response({}, status=404)
            return _json_response({"object": {"sha": sha}})
        if method == "POST" and path == f"{self.base_path}/git/blobs":
            assert isinstance(payload, dict)
            content = payload["content"]
            assert isinstance(content, str)
            raw = base64.b64decode(content)
            sha = hashlib.sha1(
                f"blob {len(raw)}\0".encode("ascii") + raw,
                usedforsecurity=False,
            ).hexdigest()
            return _json_response({"sha": sha}, status=201)
        if method == "POST" and path == f"{self.base_path}/git/trees":
            return _json_response({"sha": self.expected_tree_sha}, status=201)
        if method == "POST" and path == f"{self.base_path}/git/commits":
            assert isinstance(payload, dict)
            parents = payload["parents"]
            assert isinstance(parents, list) and all(isinstance(item, str) for item in parents)
            self.commit_parents.append(parents)
            return _json_response({"sha": self.expected_commit_sha}, status=201)
        if method == "POST" and path == f"{self.base_path}/git/refs":
            assert isinstance(payload, dict)
            ref = payload["ref"]
            sha = payload["sha"]
            assert isinstance(ref, str) and isinstance(sha, str)
            self.refs[ref.removeprefix("refs/heads/")] = sha
            return _json_response({"ref": ref}, status=201)
        if method == "GET" and path.startswith(f"{self.base_path}/pulls?"):
            query = urllib_parse.parse_qs(parsed.query)
            head = query.get("head", [""])[0]
            base = query.get("base", [""])[0]
            rows = [
                row
                for row in self.prs
                if row["head"]["ref"] == head.split(":", 1)[-1] and row["base"]["ref"] == base
            ]
            return _json_response(rows)
        if method == "POST" and path == f"{self.base_path}/pulls":
            assert isinstance(payload, dict)
            number = len(self.prs) + 1
            row: dict[str, object] = {
                "number": number,
                "state": "open",
                "draft": True,
                "merged": False,
                "merged_at": None,
                "body": payload["body"],
                "base": {"ref": payload["base"]},
                "head": {"ref": payload["head"], "sha": self.expected_commit_sha},
            }
            self.prs.append(row)
            if self.fail_after_draft_pr:
                raise gate_b_executor.GateBExecutorError("http_transport_failure")
            return _json_response(row, status=201)
        raise AssertionError(f"unexpected publisher call: {method} {path}")


def _repair_candidates() -> tuple[gate_b_executor.PullRequestCandidate, list[gate_b_executor.PullRequestCandidate]]:
    candidates = [
        gate_b_executor.PullRequestCandidate(
            number=number,
            github_id=5000 + number,
            base_branch="master",
            base_sha="a" * 40,
            head_sha=f"{number + 200:040x}",
            updated_at_utc="2026-08-06T00:00:00Z",
            selection_rank_sha256=gate_b_executor.sha256_text(f"{'e' * 64}\npr-{number}"),
        )
        for number in range(1, 21)
    ]
    candidates.sort(key=lambda item: (item.selection_rank_sha256, item.number))
    return next(item for item in candidates if item.number == 1), candidates


def _completed_review(candidate: gate_b_executor.PullRequestCandidate) -> gate_b_executor.ReviewOutcome:
    finding_id = "f" * 64
    return gate_b_executor.ReviewOutcome(
        pr_id=candidate.pr_id,
        status="completed",
        terminal_category="completed",
        finding_ids=(finding_id,),
        feedback_eligible_finding_ids=(finding_id,),
        provider_call_count=1,
        http_attempt_count=1,
        input_tokens=10,
        output_tokens=5,
        cached_tokens=0,
        response_sha256="e" * 64,
    )


def _completed_review_receipt(
    *,
    authorization: dict[str, object],
    selection_receipt: dict[str, object],
    selected_candidate: gate_b_executor.PullRequestCandidate,
    candidates: list[gate_b_executor.PullRequestCandidate],
) -> dict[str, object]:
    outcomes: list[gate_b_executor.ReviewOutcome] = []
    required = authorization["required_fields"]
    assert isinstance(required, dict)
    budget = gate_b_executor._review_budget_from_authorization(required)
    for candidate in candidates:
        outcome = (
            _completed_review(candidate)
            if candidate == selected_candidate
            else gate_b_executor.ReviewOutcome(
                pr_id=candidate.pr_id,
                status="completed",
                terminal_category="completed",
                finding_ids=(),
                feedback_eligible_finding_ids=(),
                provider_call_count=1,
                http_attempt_count=1,
                input_tokens=10,
                output_tokens=5,
                cached_tokens=0,
                response_sha256="e" * 64,
            )
        )
        budget.reserve_call()
        budget.reserve_http()
        budget.settle(outcome)
        outcomes.append(outcome)
    return gate_b_executor.build_review_cohort_receipt(
        authorization=authorization,
        selection_receipt=selection_receipt,
        outcomes=outcomes,
        budget=budget,
        stop_category="none",
        created_at_utc="2026-08-06T00:02:00Z",
    )


def _operator_timeout_receipt(
    *,
    authorization: dict[str, object],
    review_receipt: dict[str, object],
    finding_id: str,
) -> dict[str, object]:
    required = authorization["required_fields"]
    assert isinstance(required, dict)
    receipt: dict[str, object] = {
        "schema_version": gate_b_executor.OPERATOR_SESSION_SCHEMA_VERSION,
        "authorization_id": required["authorization_id"],
        "canonical_authorization_sha256": required["canonical_authorization_sha256"],
        "review_cohort_receipt_sha256": review_receipt[
            "review_cohort_receipt_sha256"
        ],
        "selected_finding_id": finding_id,
        "state": "expired",
        "terminal_category": "timeout",
        "started_at_utc": "2026-08-06T00:03:00Z",
        "session_receipt_sha256": "",
    }
    receipt["session_receipt_sha256"] = gate_b_executor._self_hash(
        receipt, "session_receipt_sha256"
    )
    return receipt


def _approved_recovery_context(
    *,
    checkpoint_sha256: str,
    participants: dict[str, object],
    repository: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    authorization_id = "phase11d-gate-b-timeout-recovery-v1-20260806-001"
    draft, _participants, _repository, _descriptor = _gate_b_real_inputs()
    required = draft["required_fields"]
    assert isinstance(required, dict)
    required["authorization_id"] = authorization_id
    required["deterministic_selection_seed_sha256"] = checkpoint_sha256
    descriptor = pilot.build_credential_descriptor(
        authorization_id=authorization_id,
        credential_descriptor_id="phase11d-gate-b-timeout-recovery-credentials-001",
        github_app_id=4421400,
        github_app_installation_id=149747930,
        github_app_private_key_fingerprint_sha256=gate_b_executor.sha256_bytes(
            b"test-private-key"
        ),
        provider_id="zhipu",
        provider_model_snapshot="glm-5.2",
        provider_api_key_fingerprint_sha256=gate_b_executor.sha256_text(
            "test-provider-key"
        ),
        credential_delivery_mode="local_secret_store_to_ephemeral_process_environment",
        credential_revoke_procedure="github_delete_private_key_and_zhipu_disable_api_key",
    )
    required["credential_fingerprint_sha256"] = descriptor[
        "credential_descriptor_sha256"
    ]
    runtime = gate_b_executor.freeze_executor_runtime(
        source_root=Path(__file__).resolve().parents[1],
        authorization_id=authorization_id,
        executor_id="phase11d-gate-b-timeout-recovery-executor-001",
        created_at_utc="2026-08-06T00:10:00Z",
    )
    frozen = gate_b_executor.freeze_authorization(
        draft=draft,
        participants=participants,
        repository=repository,
        credential_descriptor=descriptor,
        runtime=runtime,
    )
    approved = gate_b_executor.approve_authorization(
        frozen=frozen,
        participants=participants,
        actor_id="p-03",
        approved_at_utc="2026-08-06T00:11:00Z",
        exact_approval_text=gate_b_executor.build_exact_approval_text(frozen),
    )
    return approved, descriptor, runtime


class _CohortReviewReader:
    def __init__(
        self,
        candidates: list[gate_b_executor.PullRequestCandidate],
        *,
        drift_first: bool = False,
        failure_read_call: int | None = None,
    ) -> None:
        self._candidates = {candidate.number: candidate for candidate in candidates}
        self._drift_first = drift_first
        self._failure_read_call = failure_read_call
        self.read_calls: list[int] = []
        self.diff_calls: list[int] = []

    def pull_request_candidate(
        self,
        number: int,
        *,
        selection_seed_sha256: str,
    ) -> gate_b_executor.PullRequestCandidate:
        self.read_calls.append(number)
        if self._failure_read_call == len(self.read_calls):
            raise gate_b_executor.GateBExecutorError("http_transport_failure")
        candidate = self._candidates[number]
        if self._drift_first and len(self.read_calls) == 1:
            return gate_b_executor.PullRequestCandidate(
                number=candidate.number,
                github_id=candidate.github_id,
                base_branch=candidate.base_branch,
                base_sha=candidate.base_sha,
                head_sha="f" * 40,
                updated_at_utc=candidate.updated_at_utc,
                selection_rank_sha256=gate_b_executor.sha256_text(
                    f"{selection_seed_sha256}\n{candidate.pr_id}"
                ),
            )
        return candidate

    def pull_request_diff(self, number: int) -> str:
        self.diff_calls.append(number)
        return "diff --git a/module.py b/module.py\n+covered = True\n"


class _CohortReviewClient:
    def __init__(self, *, failure_category: str | None = None) -> None:
        self.failure_category = failure_category
        self.calls: list[str] = []

    def review(
        self,
        *,
        candidate: gate_b_executor.PullRequestCandidate,
        diff_text: str,
    ) -> gate_b_executor.ReviewOutcome:
        del diff_text
        self.calls.append(candidate.pr_id)
        if self.failure_category is not None and len(self.calls) == 1:
            return gate_b_executor.ReviewOutcome(
                pr_id=candidate.pr_id,
                status="failed",
                terminal_category=self.failure_category,
                finding_ids=(),
                feedback_eligible_finding_ids=(),
                provider_call_count=1,
                http_attempt_count=1,
                input_tokens=10,
                output_tokens=5,
                cached_tokens=0,
                response_sha256="d" * 64,
            )
        return gate_b_executor.ReviewOutcome(
            pr_id=candidate.pr_id,
            status="completed",
            terminal_category="completed",
            finding_ids=(),
            feedback_eligible_finding_ids=(),
            provider_call_count=1,
            http_attempt_count=1,
            input_tokens=10,
            output_tokens=5,
            cached_tokens=0,
            response_sha256="c" * 64,
        )


def _sandbox_result(intent: gate_b_executor.RepairIntent) -> gate_b_executor.SandboxResult:
    patch = gate_b_executor.SandboxPatchFile("src/repair_target.py", b"patched = True\n")
    patch_sha = gate_b_executor.sha256_bytes(
        gate_b_executor.canonical_json(
            [
                {
                    "path": patch.path,
                    "mode": patch.mode,
                    "blob_sha": patch.blob_sha,
                    "content_sha256": gate_b_executor.sha256_bytes(patch.content),
                }
            ]
        )
    )
    return gate_b_executor.SandboxResult(
        repair_job_id=intent.repair_job_id,
        worktree_receipt_sha256="1" * 64,
        task_branch_sha256=gate_b_executor.sha256_text(intent.head_branch),
        patch_sha256=patch_sha,
        checkpoint_sha256="2" * 64,
        test_sha256="3" * 64,
        budget_sha256="4" * 64,
        tests_passed=True,
        reflection_passed=True,
        exact_commit_sha="d" * 40,
        expected_tree_sha="c" * 40,
        patch_files=(patch,),
    )


class Phase11DHumanPilotTests(unittest.TestCase):
    def test_real_executor_freezes_exact_authorization_and_requires_owner_approval(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        runtime = gate_b_executor.freeze_executor_runtime(
            source_root=source_root,
            authorization_id="phase11d-gate-b-human-pilot-v1-20260805-001",
            executor_id="phase11d-gate-b-executor-20260806-001",
            created_at_utc="2026-08-06T00:00:00Z",
        )
        draft, participants, repository, descriptor = _gate_b_real_inputs()
        frozen = gate_b_executor.freeze_authorization(
            draft=draft,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=runtime,
        )

        self.assertFalse(frozen["gate_b_allowed"])
        self.assertEqual(frozen["template_status"], "awaiting_owner_exact_approval")
        expected_text = gate_b_executor.build_exact_approval_text(frozen)
        self.assertEqual(frozen["exact_approval_text"], expected_text)
        self.assertEqual(
            frozen["required_fields"]["canonical_authorization_sha256"],
            gate_b_executor.canonical_authorization_sha256(frozen),
        )
        blocked = gate_b_executor.validate_execution_authorization(
            authorization=frozen,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=runtime,
            now_utc="2026-08-06T00:00:00Z",
        )
        self.assertFalse(blocked.gate_b_allowed)
        self.assertIn("owner_approval_missing", blocked.blockers)

        approved = gate_b_executor.approve_authorization(
            frozen=frozen,
            participants=participants,
            actor_id="p-03",
            approved_at_utc="2026-08-06T00:01:00Z",
            exact_approval_text=expected_text,
        )
        active = gate_b_executor.validate_execution_authorization(
            authorization=approved,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=runtime,
            now_utc="2026-08-06T00:02:00Z",
        )
        self.assertTrue(active.gate_b_allowed, active.blockers)
        self.assertEqual(active.execution_capability, "authorization_gated_real_executor")

    def test_review_only_authorization_cannot_enter_repair(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        runtime = gate_b_executor.freeze_executor_runtime(
            source_root=source_root,
            authorization_id="phase11d-gate-b-human-pilot-v1-20260805-001",
            executor_id="phase11d-gate-b-executor-20260806-001",
            created_at_utc="2026-08-06T00:00:00Z",
        )
        draft, participants, repository, descriptor = _gate_b_real_inputs()
        draft["permission_switches"]["allow_real_github_repair_branch_push"] = False
        draft["permission_switches"]["allow_real_draft_repair_pr"] = False
        frozen = gate_b_executor.freeze_authorization(
            draft=draft,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=runtime,
        )
        self.assertIn(
            "allow_real_github_repair_branch_push=false",
            frozen["exact_approval_text"],
        )
        self.assertIn("allow_real_draft_repair_pr=false", frozen["exact_approval_text"])
        approved = gate_b_executor.approve_authorization(
            frozen=frozen,
            participants=participants,
            actor_id="p-03",
            approved_at_utc="2026-08-06T00:01:00Z",
            exact_approval_text=frozen["exact_approval_text"],
        )
        status = gate_b_executor.validate_execution_authorization(
            authorization=approved,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=runtime,
            now_utc="2026-08-06T00:02:00Z",
        )
        self.assertTrue(status.gate_b_allowed, status.blockers)
        _candidate, candidates = _repair_candidates()
        selection_receipt = gate_b_executor.build_selection_receipt(
            authorization=approved,
            candidates=candidates,
            excluded_counts={
                "draft": 0,
                "malformed": 0,
                "outside_window": 0,
                "wrong_base": 0,
            },
        )
        with self.assertRaisesRegex(
            gate_b_executor.GateBExecutorError,
            "authorization_repair_permission_denied",
        ):
            gate_b_executor.GateBRepairCoordinator(
                authorization=approved,
                participants=participants,
                repository=repository,
                credential_descriptor=descriptor,
                runtime=runtime,
                source_root=source_root,
                selection_receipt=selection_receipt,
                now_utc="2026-08-06T00:02:00Z",
            )

    def test_real_executor_rejects_tampering_and_wrong_exact_approval(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        runtime = gate_b_executor.freeze_executor_runtime(
            source_root=source_root,
            authorization_id="phase11d-gate-b-human-pilot-v1-20260805-001",
            executor_id="phase11d-gate-b-executor-20260806-001",
            created_at_utc="2026-08-06T00:00:00Z",
        )
        draft, participants, repository, descriptor = _gate_b_real_inputs()
        frozen = gate_b_executor.freeze_authorization(
            draft=draft,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=runtime,
        )
        with self.assertRaisesRegex(gate_b_executor.GateBExecutorError, "exact_approval_text_mismatch"):
            gate_b_executor.approve_authorization(
                frozen=frozen,
                participants=participants,
                actor_id="p-03",
                approved_at_utc="2026-08-06T00:01:00Z",
                exact_approval_text="approve everything",
            )

        approved = gate_b_executor.approve_authorization(
            frozen=frozen,
            participants=participants,
            actor_id="p-03",
            approved_at_utc="2026-08-06T00:01:00Z",
            exact_approval_text=gate_b_executor.build_exact_approval_text(frozen),
        )
        tampered_runtime = copy.deepcopy(runtime)
        tampered_runtime["frozen_executable_source_sha256"] = "f" * 64
        tampered_runtime["runtime_sha256"] = gate_b_executor._self_hash(
            tampered_runtime,
            "runtime_sha256",
        )
        status = gate_b_executor.validate_execution_authorization(
            authorization=approved,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=tampered_runtime,
            now_utc="2026-08-06T00:02:00Z",
        )
        self.assertFalse(status.gate_b_allowed)
        self.assertIn("runtime_hash_mismatch", status.blockers)
        live_drift = copy.deepcopy(runtime)
        live_drift["frozen_source_tree_sha256"] = "e" * 64
        live_drift["runtime_sha256"] = gate_b_executor._self_hash(
            live_drift,
            "runtime_sha256",
        )
        with self.assertRaisesRegex(gate_b_executor.GateBExecutorError, "live_runtime_drift"):
            gate_b_executor.validate_live_executor_runtime(
                source_root=source_root,
                runtime=live_drift,
            )

    def test_real_executor_verifies_only_matching_in_memory_secret_fingerprints(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        runtime = gate_b_executor.freeze_executor_runtime(
            source_root=source_root,
            authorization_id="phase11d-gate-b-human-pilot-v1-20260805-001",
            executor_id="phase11d-gate-b-executor-20260806-001",
            created_at_utc="2026-08-06T00:00:00Z",
        )
        draft, participants, repository, descriptor = _gate_b_real_inputs()
        frozen = gate_b_executor.freeze_authorization(
            draft=draft,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=runtime,
        )
        approved = gate_b_executor.approve_authorization(
            frozen=frozen,
            participants=participants,
            actor_id="p-03",
            approved_at_utc="2026-08-06T00:01:00Z",
            exact_approval_text=gate_b_executor.build_exact_approval_text(frozen),
        )
        with tempfile.TemporaryDirectory() as temp:
            key_file = Path(temp) / "private-key.pem"
            key_file.write_bytes(b"test-private-key")
            with mock.patch.dict(os.environ, {"PHASE11D_TEST_PROVIDER_KEY": "test-provider-key"}):
                matched = gate_b_executor.verify_credential_fingerprints(
                    authorization=approved,
                    participants=participants,
                    repository=repository,
                    credential_descriptor=descriptor,
                    runtime=runtime,
                    source_root=Path(__file__).resolve().parents[1],
                    github_app_private_key_file=key_file,
                    provider_key_environment="PHASE11D_TEST_PROVIDER_KEY",
                    now_utc="2026-08-06T00:02:00Z",
                )
            self.assertEqual(
                matched,
                {
                    "github_app_private_key_fingerprint_matched": True,
                    "provider_api_key_fingerprint_matched": True,
                },
            )

            key_file.write_bytes(b"other-private-key")
            with mock.patch.dict(os.environ, {"PHASE11D_TEST_PROVIDER_KEY": "test-provider-key"}):
                with self.assertRaisesRegex(
                    gate_b_executor.GateBExecutorError,
                    "credential_fingerprint_mismatch",
                ):
                    gate_b_executor.verify_credential_fingerprints(
                        authorization=approved,
                        participants=participants,
                        repository=repository,
                        credential_descriptor=descriptor,
                        runtime=runtime,
                        source_root=Path(__file__).resolve().parents[1],
                        github_app_private_key_file=key_file,
                        provider_key_environment="PHASE11D_TEST_PROVIDER_KEY",
                        now_utc="2026-08-06T00:02:00Z",
                    )

    def test_gate_b_selection_is_deterministic_and_endpoint_bound(self) -> None:
        base_url = "https://api.github.com/repos/example-owner/example-repo"
        query = "state=open&base=master&sort=updated&direction=desc&per_page=100&page=1"
        rows: list[dict[str, object]] = []
        for number in range(1, 26):
            rows.append(
                {
                    "number": number,
                    "id": 1000 + number,
                    "draft": False,
                    "updated_at": "2026-08-06T00:00:00Z",
                    "base": {"ref": "master", "sha": f"{number:040x}"},
                    "head": {"sha": f"{number + 100:040x}"},
                }
            )
        rows.append(
            {
                "number": 99,
                "id": 1099,
                "draft": True,
                "updated_at": "2026-08-06T00:00:00Z",
                "base": {"ref": "master", "sha": "a" * 40},
                "head": {"sha": "b" * 40},
            }
        )
        transport = _FakeJsonTransport(
            {
                ("GET", base_url): _json_response({"id": 1301558766, "archived": False, "disabled": False}),
                ("GET", f"{base_url}/pulls?{query}"): _json_response(rows),
            }
        )
        reader = gate_b_executor.GitHubRepositoryReader(
            transport,
            token=gate_b_executor.InstallationToken(
                value="test-installation-token",
                expires_at_utc="2026-08-06T01:00:00Z",
                app_id=4421400,
                installation_id=149747930,
            ),
            owner="example-owner",
            repository="example-repo",
            expected_repository_id=1301558766,
        )
        reader.verify_repository()
        selected, excluded = reader.list_open_pull_requests(
            base_branch="master",
            selection_seed_sha256="a" * 64,
            window_start_utc="2026-08-05T12:00:00Z",
            window_end_utc="2026-09-05T23:59:59Z",
            selected_count=20,
        )
        self.assertEqual(len(selected), 20)
        self.assertEqual(excluded["draft"], 1)
        self.assertEqual(
            [candidate.number for candidate in selected],
            sorted(range(1, 26), key=lambda number: (pilot.sha256_text(f"{'a' * 64}\npr-{number}"), number))[:20],
        )
        self.assertEqual(selected[0].receipt_row()["pr_id"], f"pr-{selected[0].number}")
        with self.assertRaisesRegex(gate_b_executor.GateBExecutorError, "github_endpoint_denied"):
            reader._request(path="/issues")

    def test_gate_b_zhipu_review_requires_a_structured_tool_call(self) -> None:
        candidate = gate_b_executor.PullRequestCandidate(
            number=7,
            github_id=1007,
            base_branch="master",
            base_sha="a" * 40,
            head_sha="b" * 40,
            updated_at_utc="2026-08-06T00:00:00Z",
            selection_rank_sha256="c" * 64,
        )
        arguments = {
            "findings": [
                {
                    "title": "Missing input check",
                    "severity": "high",
                    "path": "src/example.py",
                    "line": 12,
                    "description": "The changed branch accepts an invalid empty value.",
                }
            ]
        }
        response = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "submit_review",
                                    "arguments": json.dumps(arguments),
                                }
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        transport = _FakeJsonTransport(
            {
                ("POST", gate_b_executor.ZhipuReviewClient.endpoint): _json_response(response),
            }
        )
        ephemeral: list[tuple[gate_b_executor.EphemeralReviewFinding, ...]] = []
        client = gate_b_executor.ZhipuReviewClient(
            transport,
            api_key="test-provider-key",
            model="glm-5.2",
            finding_sink=ephemeral.append,
        )
        outcome = client.review(candidate=candidate, diff_text="diff --git a/a.py b/a.py\n+line")
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.terminal_category, "completed")
        self.assertEqual(len(outcome.finding_ids), 1)
        self.assertEqual(outcome.input_tokens, 100)
        self.assertEqual(outcome.receipt_row()["finding_ids"], list(outcome.finding_ids))
        self.assertEqual(ephemeral[0][0].title, "Missing input check")
        self.assertEqual(ephemeral[0][0].finding_id, outcome.finding_ids[0])
        self.assertNotIn("Missing input check", json.dumps(outcome.receipt_row()))

        text_only = _FakeJsonTransport(
            {
                ("POST", gate_b_executor.ZhipuReviewClient.endpoint): _json_response(
                    {
                        "choices": [{"message": {"content": "Here is my review", "tool_calls": []}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                    }
                )
            }
        )
        failed = gate_b_executor.ZhipuReviewClient(
            text_only,
            api_key="test-provider-key",
            model="glm-5.2",
        ).review(candidate=candidate, diff_text="diff --git a/a.py b/a.py\n+line")
        self.assertEqual(failed.terminal_category, "provider_text_only_response")

    def test_gate_b_review_budget_reserves_before_transport_and_stops_at_limit(self) -> None:
        candidate, _candidates = _repair_candidates()
        response = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [{"function": {"name": "submit_review", "arguments": '{"findings":[]}'}}],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "prompt_cache_hit_tokens": 0},
        }
        client = gate_b_executor.ZhipuReviewClient(
            _FakeJsonTransport(
                {("POST", gate_b_executor.ZhipuReviewClient.endpoint): _json_response(response)}
            ),
            api_key="test-provider-key",
            model="glm-5.2",
        )
        budget = gate_b_executor.ReviewBudget(
            max_logical_calls=1,
            max_http_attempts=1,
            max_input_tokens=10,
            max_output_tokens=5,
            max_cached_tokens=0,
            max_micro_cny=1000,
            max_wall_clock_seconds=60,
        )
        outcome = gate_b_executor.review_with_budget(
            client=client,
            budget=budget,
            candidate=candidate,
            diff_text="diff --git a/a.py b/a.py\n+line",
        )
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(budget.to_dict()["logical_calls"], 1)
        self.assertEqual(budget.to_dict()["http_attempts"], 1)
        with self.assertRaisesRegex(gate_b_executor.GateBExecutorError, "budget_logical_calls_exhausted"):
            gate_b_executor.review_with_budget(
                client=client,
                budget=budget,
                candidate=candidate,
                diff_text="diff --git a/a.py b/a.py\n+line",
            )

    def test_gate_b_review_reader_materializes_current_snapshot(self) -> None:
        number = 7
        seed = "e" * 64
        pull_url = f"https://api.github.com/repos/acme/widget/pulls/{number}"
        transport = _FakeJsonTransport(
            {
                ("GET", pull_url): _json_response(
                    {
                        "id": 5007,
                        "number": number,
                        "state": "open",
                        "draft": False,
                        "updated_at": "2026-08-06T00:00:00Z",
                        "base": {"ref": "master", "sha": "a" * 40},
                        "head": {"sha": "b" * 40},
                    }
                )
            }
        )
        reader = gate_b_executor.GitHubRepositoryReader(
            transport,
            token=gate_b_executor.InstallationToken(
                value="installation-token",
                expires_at_utc="2026-08-06T01:00:00Z",
                app_id=4421400,
                installation_id=149747930,
            ),
            owner="acme",
            repository="widget",
            expected_repository_id=1301558766,
        )
        candidate = reader.pull_request_candidate(number, selection_seed_sha256=seed)
        self.assertEqual(candidate.base_branch, "master")
        self.assertEqual(candidate.head_sha, "b" * 40)
        self.assertEqual(candidate.selection_rank_sha256, gate_b_executor.sha256_text(f"{seed}\npr-7"))

    def test_gate_b_review_cohort_success_is_fixed_and_hash_bound(self) -> None:
        approved, _participants, _repository, _descriptor, _runtime = _approved_gate_b_context()
        _candidate, candidates = _repair_candidates()
        selection_receipt = gate_b_executor.build_selection_receipt(
            authorization=approved,
            candidates=candidates,
            excluded_counts={"draft": 0, "malformed": 0, "outside_window": 0, "wrong_base": 0},
        )
        reader = _CohortReviewReader(candidates)
        client = _CohortReviewClient()
        receipt = gate_b_executor.run_review_cohort(
            authorization=approved,
            selection_receipt=selection_receipt,
            reader=reader,
            client=client,
            created_at_utc="2026-08-06T00:03:00Z",
        )
        self.assertEqual(receipt["selected_pr_count"], 20)
        self.assertEqual(receipt["stop_category"], "none")
        self.assertEqual(len(receipt["review_rows"]), 20)
        self.assertEqual(len(client.calls), 20)
        self.assertEqual(receipt["budget_usage"]["logical_calls"], 20)
        gate_b_executor.validate_review_cohort_receipt(
            receipt,
            authorization=approved,
            selection_receipt=selection_receipt,
        )
        tampered = copy.deepcopy(receipt)
        tampered["budget_usage"]["logical_calls"] = 19
        with self.assertRaisesRegex(
            gate_b_executor.GateBExecutorError, "review_budget_usage_mismatch"
        ):
            gate_b_executor.validate_review_cohort_receipt(
                tampered,
                authorization=approved,
                selection_receipt=selection_receipt,
            )

    def test_gate_b_review_cohort_stops_without_replacing_denominator(self) -> None:
        approved, _participants, _repository, _descriptor, _runtime = _approved_gate_b_context()
        _candidate, candidates = _repair_candidates()
        selection_receipt = gate_b_executor.build_selection_receipt(
            authorization=approved,
            candidates=candidates,
            excluded_counts={"draft": 0, "malformed": 0, "outside_window": 0, "wrong_base": 0},
        )
        client = _CohortReviewClient(failure_category="provider_text_only_response")
        receipt = gate_b_executor.run_review_cohort(
            authorization=approved,
            selection_receipt=selection_receipt,
            reader=_CohortReviewReader(candidates),
            client=client,
            created_at_utc="2026-08-06T00:03:00Z",
        )
        rows = receipt["review_rows"]
        self.assertEqual(receipt["stop_category"], "provider_text_only_response")
        self.assertEqual(len(rows), 20)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(rows[0]["terminal_category"], "provider_text_only_response")
        self.assertTrue(all(row["terminal_category"] == "cohort_stopped" for row in rows[1:]))
        self.assertTrue(all(row["provider_call_count"] == 0 for row in rows[1:]))

    def test_gate_b_review_cohort_stops_before_provider_on_snapshot_drift(self) -> None:
        approved, _participants, _repository, _descriptor, _runtime = _approved_gate_b_context()
        _candidate, candidates = _repair_candidates()
        selection_receipt = gate_b_executor.build_selection_receipt(
            authorization=approved,
            candidates=candidates,
            excluded_counts={"draft": 0, "malformed": 0, "outside_window": 0, "wrong_base": 0},
        )
        client = _CohortReviewClient()
        receipt = gate_b_executor.run_review_cohort(
            authorization=approved,
            selection_receipt=selection_receipt,
            reader=_CohortReviewReader(candidates, drift_first=True),
            client=client,
            created_at_utc="2026-08-06T00:03:00Z",
        )
        self.assertEqual(receipt["stop_category"], "selection_candidate_drift")
        self.assertEqual(client.calls, [])
        self.assertEqual(receipt["budget_usage"]["logical_calls"], 0)

    def test_gate_b_review_resume_carries_completed_rows_without_replay(self) -> None:
        approved, _participants, _repository, _descriptor, _runtime = _approved_gate_b_context()
        _candidate, candidates = _repair_candidates()
        selection_receipt = gate_b_executor.build_selection_receipt(
            authorization=approved,
            candidates=candidates,
            excluded_counts={"draft": 0, "malformed": 0, "outside_window": 0, "wrong_base": 0},
        )
        previous_client = _CohortReviewClient()
        previous_receipt = gate_b_executor.run_review_cohort(
            authorization=approved,
            selection_receipt=selection_receipt,
            reader=_CohortReviewReader(candidates, failure_read_call=2),
            client=previous_client,
            created_at_utc="2026-08-06T00:03:00Z",
        )
        self.assertEqual(previous_receipt["stop_category"], "http_transport_failure")
        self.assertEqual(len(previous_client.calls), 1)

        resume_reader = _CohortReviewReader(candidates)
        resume_client = _CohortReviewClient()
        receipt = gate_b_executor.resume_review_cohort(
            authorization=approved,
            selection_receipt=selection_receipt,
            previous_authorization=approved,
            previous_selection_receipt=selection_receipt,
            previous_review_receipt=previous_receipt,
            reader=resume_reader,
            client=resume_client,
            created_at_utc="2026-08-06T00:04:00Z",
        )
        rows = receipt["review_rows"]
        self.assertEqual(receipt["schema_version"], gate_b_executor.RESUMED_REVIEW_COHORT_RECEIPT_SCHEMA_VERSION)
        self.assertEqual(
            receipt["previous_review_cohort_receipt_sha256"],
            previous_receipt["review_cohort_receipt_sha256"],
        )
        self.assertEqual(receipt["stop_category"], "none")
        self.assertEqual(receipt["budget_usage"]["logical_calls"], 20)
        self.assertEqual(len(resume_client.calls), 19)
        self.assertNotIn(previous_client.calls[0], resume_client.calls)
        self.assertEqual(resume_reader.read_calls[0], candidates[1].number)
        self.assertEqual(rows[0], previous_receipt["review_rows"][0])
        self.assertTrue(all(row["status"] == "completed" for row in rows))
        gate_b_executor.validate_review_cohort_receipt(
            receipt,
            authorization=approved,
            selection_receipt=selection_receipt,
        )

    def test_gate_b_review_resume_rejects_provider_failure(self) -> None:
        approved, _participants, _repository, _descriptor, _runtime = _approved_gate_b_context()
        _candidate, candidates = _repair_candidates()
        selection_receipt = gate_b_executor.build_selection_receipt(
            authorization=approved,
            candidates=candidates,
            excluded_counts={"draft": 0, "malformed": 0, "outside_window": 0, "wrong_base": 0},
        )
        previous_receipt = gate_b_executor.run_review_cohort(
            authorization=approved,
            selection_receipt=selection_receipt,
            reader=_CohortReviewReader(candidates),
            client=_CohortReviewClient(failure_category="provider_text_only_response"),
            created_at_utc="2026-08-06T00:03:00Z",
        )
        client = _CohortReviewClient()
        with self.assertRaisesRegex(
            gate_b_executor.GateBExecutorError,
            "review_resume_category_denied",
        ):
            gate_b_executor.resume_review_cohort(
                authorization=approved,
                selection_receipt=selection_receipt,
                previous_authorization=approved,
                previous_selection_receipt=selection_receipt,
                previous_review_receipt=previous_receipt,
                reader=_CohortReviewReader(candidates),
                client=client,
                created_at_utc="2026-08-06T00:04:00Z",
            )
        self.assertEqual(client.calls, [])

    def test_gate_b_review_command_parser_requires_all_frozen_inputs(self) -> None:
        args = gate_b_executor.build_parser().parse_args(
            [
                "review-selected-pull-requests",
                "--authorization",
                "authorization.json",
                "--participants",
                "participants.json",
                "--repository-authorization",
                "repository.json",
                "--credential-descriptor",
                "credentials.json",
                "--runtime",
                "runtime.json",
                "--selection-receipt",
                "selection.json",
                "--source-root",
                ".",
                "--github-app-private-key-file",
                "private-key.pem",
                "--provider-key-environment",
                "PROVIDER_KEY",
                "--owner",
                "acme",
                "--repository",
                "widget",
                "--output",
                "reviews.json",
            ]
        )
        self.assertEqual(args.command, "review-selected-pull-requests")
        self.assertEqual(args.selection_receipt, Path("selection.json"))

        resume_args = gate_b_executor.build_parser().parse_args(
            [
                "resume-selected-pull-requests",
                "--authorization",
                "authorization.json",
                "--participants",
                "participants.json",
                "--repository-authorization",
                "repository.json",
                "--credential-descriptor",
                "credentials.json",
                "--runtime",
                "runtime.json",
                "--selection-receipt",
                "selection.json",
                "--previous-authorization",
                "previous-authorization.json",
                "--previous-selection-receipt",
                "previous-selection.json",
                "--previous-review-receipt",
                "previous-review.json",
                "--source-root",
                ".",
                "--github-app-private-key-file",
                "private-key.pem",
                "--provider-key-environment",
                "PROVIDER_KEY",
                "--owner",
                "acme",
                "--repository",
                "widget",
                "--output",
                "reviews.json",
            ]
        )
        self.assertEqual(resume_args.command, "resume-selected-pull-requests")
        self.assertEqual(resume_args.previous_review_receipt, Path("previous-review.json"))

        checkpoint_args = gate_b_executor.build_parser().parse_args(
            [
                "build-timeout-recovery-checkpoint",
                "--source-authorization",
                "source-authorization.json",
                "--participants",
                "participants.json",
                "--repository-authorization",
                "repository.json",
                "--source-credential-descriptor",
                "source-credentials.json",
                "--source-runtime",
                "source-runtime.json",
                "--selection-receipt",
                "selection.json",
                "--review-receipt",
                "review.json",
                "--timeout-receipt",
                "timeout.json",
                "--selected-finding-id",
                "f" * 64,
                "--prior-selection-sha256",
                "1" * 64,
                "--prior-plan-sha256",
                "2" * 64,
                "--prior-write-binding-sha256",
                "3" * 64,
                "--prior-write-approval-id",
                "write-source-001",
                "--prior-write-approved-at-utc",
                "2026-08-06T00:04:00Z",
                "--recovery-actor-id",
                "p-03",
                "--recovery-selection-id",
                "selection-recovery-001",
                "--recovery-repair-job-id",
                "repair-recovery-001",
                "--recovery-write-approval-id",
                "write-recovery-001",
                "--recovery-requested-at-utc",
                "2026-08-06T00:12:00Z",
                "--output",
                "checkpoint.json",
            ]
        )
        self.assertEqual(
            checkpoint_args.command, "build-timeout-recovery-checkpoint"
        )
        self.assertEqual(checkpoint_args.recovery_actor_id, "p-03")

        recovery_args = gate_b_executor.build_parser().parse_args(
            [
                "resume-write-approved-repair-session",
                "--authorization",
                "authorization.json",
                "--source-authorization",
                "source-authorization.json",
                "--participants",
                "participants.json",
                "--repository-authorization",
                "repository.json",
                "--credential-descriptor",
                "credentials.json",
                "--source-credential-descriptor",
                "source-credentials.json",
                "--runtime",
                "runtime.json",
                "--source-runtime",
                "source-runtime.json",
                "--selection-receipt",
                "selection.json",
                "--review-receipt",
                "review.json",
                "--timeout-receipt",
                "timeout.json",
                "--recovery-checkpoint",
                "checkpoint.json",
                "--plan-file",
                "plan.txt",
                "--source-root",
                ".",
                "--github-app-private-key-file",
                "private-key.pem",
                "--provider-key-environment",
                "PROVIDER_KEY",
                "--owner",
                "acme",
                "--repository",
                "widget",
                "--receipt-directory",
                "receipts",
            ]
        )
        self.assertEqual(
            recovery_args.command, "resume-write-approved-repair-session"
        )
        self.assertEqual(recovery_args.recovery_checkpoint, Path("checkpoint.json"))

    def test_timeout_recovery_reuses_receipts_without_provider_or_github_access(
        self,
    ) -> None:
        (
            source_authorization,
            participants,
            repository,
            source_descriptor,
            source_runtime,
        ) = _approved_gate_b_context()
        selected_candidate, candidates = _repair_candidates()
        source_selection = gate_b_executor.build_selection_receipt(
            authorization=source_authorization,
            candidates=candidates,
            excluded_counts={
                "draft": 0,
                "malformed": 0,
                "outside_window": 0,
                "wrong_base": 0,
            },
        )
        source_review = _completed_review_receipt(
            authorization=source_authorization,
            selection_receipt=source_selection,
            selected_candidate=selected_candidate,
            candidates=candidates,
        )
        timeout_receipt = _operator_timeout_receipt(
            authorization=source_authorization,
            review_receipt=source_review,
            finding_id="f" * 64,
        )
        checkpoint = gate_b_executor.build_timeout_recovery_checkpoint(
            source_authorization=source_authorization,
            source_runtime=source_runtime,
            selection_receipt=source_selection,
            review_receipt=source_review,
            timeout_receipt=timeout_receipt,
            selected_finding_id="f" * 64,
            prior_selection_sha256="1" * 64,
            prior_plan_sha256="2" * 64,
            prior_write_binding_sha256="3" * 64,
            prior_write_approval_id="write-source-001",
            prior_write_approved_at_utc="2026-08-06T00:04:00Z",
            recovery_actor_id="p-03",
            recovery_selection_id="selection-recovery-001",
            recovery_repair_job_id="repair-recovery-001",
            recovery_write_approval_id="write-recovery-001",
            recovery_requested_at_utc="2026-08-06T00:12:00Z",
        )
        authorization, descriptor, runtime = _approved_recovery_context(
            checkpoint_sha256=checkpoint["checkpoint_sha256"],
            participants=participants,
            repository=repository,
        )
        publisher_factory_calls: list[str] = []

        def publisher_factory() -> object:
            publisher_factory_calls.append("github")
            raise AssertionError("publisher must remain closed before DRAFT_PR approval")

        with tempfile.TemporaryDirectory() as temp:
            receipt_directory = Path(temp) / "receipts"
            session = gate_b_executor.prepare_timeout_recovery_operator_session(
                authorization=authorization,
                source_authorization=source_authorization,
                participants=participants,
                repository_authorization=repository,
                credential_descriptor=descriptor,
                source_credential_descriptor=source_descriptor,
                runtime=runtime,
                source_runtime=source_runtime,
                source_selection_receipt=source_selection,
                source_review_receipt=source_review,
                timeout_receipt=timeout_receipt,
                recovery_checkpoint=checkpoint,
                plan_text="Repair only the selected token-prefix validation and tests.",
                source_root=Path(__file__).resolve().parents[1],
                receipt_directory=receipt_directory,
                publisher_factory=publisher_factory,
                now_utc="2026-08-06T00:12:00Z",
            )
            status = session.status()
            self.assertEqual(status["state"], "awaiting_write_approval")
            self.assertNotEqual(status["write_binding_sha256"], "3" * 64)
            self.assertEqual(publisher_factory_calls, [])
            with self.assertRaisesRegex(
                gate_b_executor.GateBExecutorError, "operator_state_invalid"
            ):
                session.publish({"published_at_utc": "2026-08-06T00:13:00Z"})
            self.assertEqual(publisher_factory_calls, [])
            with self.assertRaisesRegex(
                gate_b_executor.GateBExecutorError, "human_actor_role_denied"
            ):
                session.decide_write(
                    {
                        "approval_id": "write-recovery-001",
                        "actor_id": "not-a-participant",
                        "decision": "approved",
                        "approved_at_utc": "2026-08-06T00:13:00Z",
                    }
                )
            write_result = session.decide_write(
                {
                    "approval_id": "write-recovery-001",
                    "actor_id": "p-02",
                    "decision": "approved",
                    "approved_at_utc": "2026-08-06T00:13:01Z",
                }
            )
            self.assertEqual(write_result["state"], "awaiting_sandbox")
            with self.assertRaisesRegex(
                gate_b_executor.GateBExecutorError, "operator_state_invalid"
            ):
                session.decide_write(
                    {
                        "approval_id": "write-recovery-001",
                        "actor_id": "p-02",
                        "decision": "approved",
                        "approved_at_utc": "2026-08-06T00:13:02Z",
                    }
                )
            self.assertEqual(publisher_factory_calls, [])
            recovery_receipt = gate_b_executor.load_json(
                receipt_directory / "operator-timeout-recovery-receipt.json"
            )
            self.assertEqual(
                recovery_receipt["source_review_cohort_receipt_sha256"],
                source_review["review_cohort_receipt_sha256"],
            )
            rebound_review = gate_b_executor.load_json(
                receipt_directory / "recovery-review-cohort-receipt.json"
            )
            self.assertEqual(len(rebound_review["review_rows"]), 20)
            self.assertEqual(
                rebound_review["previous_review_cohort_receipt_sha256"],
                source_review["review_cohort_receipt_sha256"],
            )

            tampered_checkpoint = copy.deepcopy(checkpoint)
            tampered_checkpoint["prior_plan_sha256"] = "4" * 64
            with self.assertRaisesRegex(
                gate_b_executor.GateBExecutorError,
                "recovery_checkpoint_hash_mismatch",
            ):
                gate_b_executor.prepare_timeout_recovery_operator_session(
                    authorization=authorization,
                    source_authorization=source_authorization,
                    participants=participants,
                    repository_authorization=repository,
                    credential_descriptor=descriptor,
                    source_credential_descriptor=source_descriptor,
                    runtime=runtime,
                    source_runtime=source_runtime,
                    source_selection_receipt=source_selection,
                    source_review_receipt=source_review,
                    timeout_receipt=timeout_receipt,
                    recovery_checkpoint=tampered_checkpoint,
                    plan_text="Repair only the selected token-prefix validation and tests.",
                    source_root=Path(__file__).resolve().parents[1],
                    receipt_directory=Path(temp) / "tampered",
                    publisher_factory=publisher_factory,
                    now_utc="2026-08-06T00:12:00Z",
                )

            drifted_timeout = copy.deepcopy(timeout_receipt)
            drifted_timeout["started_at_utc"] = "2026-08-06T00:03:01Z"
            drifted_timeout["session_receipt_sha256"] = gate_b_executor._self_hash(
                drifted_timeout, "session_receipt_sha256"
            )
            with self.assertRaisesRegex(
                gate_b_executor.GateBExecutorError,
                "recovery_checkpoint_binding_mismatch",
            ):
                gate_b_executor.validate_timeout_recovery_checkpoint(
                    checkpoint,
                    source_authorization=source_authorization,
                    source_runtime=source_runtime,
                    selection_receipt=source_selection,
                    review_receipt=source_review,
                    timeout_receipt=drifted_timeout,
                )

            with self.assertRaisesRegex(
                gate_b_executor.GateBExecutorError,
                "recovery_authorization_checkpoint_mismatch",
            ):
                gate_b_executor.prepare_timeout_recovery_operator_session(
                    authorization=source_authorization,
                    source_authorization=source_authorization,
                    participants=participants,
                    repository_authorization=repository,
                    credential_descriptor=source_descriptor,
                    source_credential_descriptor=source_descriptor,
                    runtime=source_runtime,
                    source_runtime=source_runtime,
                    source_selection_receipt=source_selection,
                    source_review_receipt=source_review,
                    timeout_receipt=timeout_receipt,
                    recovery_checkpoint=checkpoint,
                    plan_text="Repair only the selected token-prefix validation and tests.",
                    source_root=Path(__file__).resolve().parents[1],
                    receipt_directory=Path(temp) / "generic-authorization",
                    publisher_factory=publisher_factory,
                    now_utc="2026-08-06T00:12:00Z",
                )
            self.assertEqual(publisher_factory_calls, [])

    @mock.patch.dict(os.environ, {"PHASE11D_PUBLISHER_TEST_KEY": "test-provider-key"})
    def test_gate_b_repair_requires_human_selection_two_approvals_and_exact_sandbox_commit(self) -> None:
        approved, participants, repository, descriptor, runtime = _approved_gate_b_context()
        candidate, candidates = _repair_candidates()
        selection_receipt = gate_b_executor.build_selection_receipt(
            authorization=approved,
            candidates=candidates,
            excluded_counts={"draft": 0, "malformed": 0, "outside_window": 0, "wrong_base": 0},
        )
        coordinator = gate_b_executor.GateBRepairCoordinator(
            authorization=approved,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=runtime,
            source_root=Path(__file__).resolve().parents[1],
            selection_receipt=selection_receipt,
            now_utc="2026-08-06T00:02:00Z",
        )
        with self.assertRaisesRegex(gate_b_executor.GateBExecutorError, "repair_finding_not_selected"):
            coordinator.prepare_repair(
                candidate=candidate,
                plan_text="Change only the selected validation branch.",
                repair_job_id="repair-001",
                requested_by="p-01",
                requested_at_utc="2026-08-06T00:03:00Z",
            )
        selection = coordinator.select_finding(
            candidate=candidate,
            review=_completed_review(candidate),
            finding_id="f" * 64,
            selection_id="selection-001",
            selector_id="p-01",
            selected_at_utc="2026-08-06T00:03:00Z",
        )
        self.assertEqual(selection.selector_id, "p-01")
        intent = coordinator.prepare_repair(
            candidate=candidate,
            plan_text="Change only the selected validation branch.",
            repair_job_id="repair-001",
            requested_by="p-01",
            requested_at_utc="2026-08-06T00:04:00Z",
        )
        write_binding = coordinator.request_write_approval(
            approval_id="write-001", requested_at_utc="2026-08-06T00:04:01Z"
        )
        self.assertEqual(write_binding, intent.write_binding_sha256)
        write = coordinator.decide_write_approval(
            approval_id="write-001",
            actor_id="p-02",
            decision="approved",
            approved_at_utc="2026-08-06T00:04:02Z",
        )
        self.assertEqual(write.actor_role, "maintainer")
        with self.assertRaisesRegex(gate_b_executor.GateBExecutorError, "repair_approval_replay"):
            coordinator.decide_write_approval(
                approval_id="write-001",
                actor_id="p-02",
                decision="approved",
                approved_at_utc="2026-08-06T00:04:03Z",
            )
        sandbox = _sandbox_result(intent)
        coordinator.submit_sandbox_result(result=sandbox, observed_at_utc="2026-08-06T00:05:00Z")
        material = gate_b_executor.build_draft_publication_material(
            intent=intent,
            sandbox=sandbox,
            base_tree_sha="b" * 40,
            commit_message="Phase 11D repair repair-001",
            commit_timestamp_utc="2026-08-06T00:05:00Z",
        )
        draft_binding = coordinator.request_draft_pr_approval(
            approval_id="draft-001",
            material=material,
            requested_at_utc="2026-08-06T00:05:01Z",
        )
        self.assertEqual(len(draft_binding), 64)
        draft = coordinator.decide_draft_pr_approval(
            approval_id="draft-001",
            actor_id="p-03",
            decision="approved",
            approved_at_utc="2026-08-06T00:05:02Z",
        )
        self.assertEqual(draft.actor_role, "org_admin")
        with tempfile.TemporaryDirectory() as temp:
            key_file = Path(temp) / "private-key.pem"
            key_file.write_bytes(b"test-private-key")
            transport = _PublisherTransport(
                owner="example-owner",
                repository="example-repo",
                base_sha=intent.base_sha,
                expected_tree_sha=material.expected_tree_sha,
                expected_commit_sha=material.exact_commit_sha,
            )
            publisher = gate_b_executor.GitHubDraftPublisher(
                transport,
                authorization=approved,
                participants=participants,
                repository_authorization=repository,
                credential_descriptor=descriptor,
                runtime=runtime,
                source_root=Path(__file__).resolve().parents[1],
                github_app_private_key_file=key_file,
                provider_key_environment="PHASE11D_PUBLISHER_TEST_KEY",
                token=gate_b_executor.InstallationToken(
                    value="test-installation-token",
                    expires_at_utc="2026-09-01T00:00:00Z",
                    app_id=4421400,
                    installation_id=149747930,
                ),
                owner="example-owner",
                repository="example-repo",
                expected_repository_id=1301558766,
                expected_app_id=4421400,
                expected_installation_id=149747930,
                journal=gate_b_executor.PublicationJournal(Path(temp) / "publication.json"),
            )
            receipt = coordinator.publish_draft_pr(
                publisher=publisher, published_at_utc="2026-08-06T00:06:00Z"
            )
            self.assertEqual(receipt.draft_pr_id, "draft-pr-1")
            self.assertFalse(receipt.ready)
            self.assertFalse(receipt.merged)
            self.assertEqual(
                transport.calls.count(("POST", "/repos/example-owner/example-repo/git/refs")), 1
            )
            self.assertEqual(transport.commit_parents, [[intent.head_sha]])
            self.assertEqual(
                transport.calls.count(("POST", "/repos/example-owner/example-repo/pulls")), 1
            )
            self.assertEqual(
                coordinator.publish_draft_pr(publisher=publisher, published_at_utc="2026-08-06T00:06:01Z"),
                receipt,
            )
            rendered = json.dumps(coordinator.repair_receipt(), sort_keys=True)
            self.assertNotIn("patched = True", rendered)

    @mock.patch.dict(os.environ, {"PHASE11D_PUBLISHER_TEST_KEY": "test-provider-key"})
    def test_loopback_operator_session_keeps_raw_material_ephemeral(self) -> None:
        approved, participants, repository, descriptor, runtime = _approved_gate_b_context()
        candidate, candidates = _repair_candidates()
        selection_receipt = gate_b_executor.build_selection_receipt(
            authorization=approved,
            candidates=candidates,
            excluded_counts={"draft": 0, "malformed": 0, "outside_window": 0, "wrong_base": 0},
        )
        outcomes: list[gate_b_executor.ReviewOutcome] = []
        budget = gate_b_executor._review_budget_from_authorization(approved["required_fields"])
        for item in candidates:
            outcome = _completed_review(item) if item == candidate else gate_b_executor.ReviewOutcome(
                pr_id=item.pr_id,
                status="completed",
                terminal_category="completed",
                finding_ids=(),
                feedback_eligible_finding_ids=(),
                provider_call_count=1,
                http_attempt_count=1,
                input_tokens=10,
                output_tokens=5,
                cached_tokens=0,
                response_sha256="e" * 64,
            )
            budget.reserve_call()
            budget.reserve_http()
            budget.settle(outcome)
            outcomes.append(outcome)
        review_receipt = gate_b_executor.build_review_cohort_receipt(
            authorization=approved,
            selection_receipt=selection_receipt,
            outcomes=outcomes,
            budget=budget,
            stop_category="none",
            created_at_utc="2026-08-06T00:02:00Z",
        )
        coordinator = gate_b_executor.GateBRepairCoordinator(
            authorization=approved,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=runtime,
            source_root=Path(__file__).resolve().parents[1],
            selection_receipt=selection_receipt,
            now_utc="2026-08-06T00:02:00Z",
        )
        finding = gate_b_executor.EphemeralReviewFinding(
            pr_id=candidate.pr_id,
            finding_id="f" * 64,
            index=1,
            title="Ephemeral validation finding",
            severity="high",
            path="src/repair_target.py",
            line=1,
            description="The validation branch accepts an invalid value.",
            response_sha256="e" * 64,
        )

        class Publisher:
            def publish(
                self,
                publication: gate_b_executor.DraftPublication,
                *,
                now_utc: str | None = None,
            ) -> gate_b_executor.DraftPublicationReceipt:
                del now_utc
                return gate_b_executor.DraftPublicationReceipt(
                    authorization_id=publication.authorization_id,
                    authorization_sha256=publication.authorization_sha256,
                    repository_id=publication.repository_id,
                    repair_job_id=publication.repair_job_id,
                    pr_id=publication.pr_id,
                    draft_pr_id="draft-pr-101",
                    head_branch=publication.head_branch,
                    base_branch=publication.base_branch,
                    commit_sha=publication.exact_commit_sha,
                    payload_sha256=publication.payload_sha256,
                    publisher_status="draft_published",
                    state="receipt_reconciled",
                )

        with tempfile.TemporaryDirectory() as temp:
            session = gate_b_executor.ReviewRepairOperatorSession(
                coordinator=coordinator,
                authorization=approved,
                selection_receipt=selection_receipt,
                review_receipt=review_receipt,
                findings=(finding,),
                receipt_directory=Path(temp),
                source_root=Path(__file__).resolve().parents[1],
                publisher_factory=Publisher,
                started_at_utc="2026-08-06T00:02:00Z",
                timeout_seconds=3600,
            )
            status = session.status()
            self.assertEqual(status["findings"][0]["title"], finding.title)
            selected = session.select_and_plan(
                {
                    "finding_id": finding.finding_id,
                    "selection_id": "selection-live-001",
                    "selector_id": "p-01",
                    "selected_at_utc": "2026-08-06T00:03:00Z",
                    "plan_text": "Change only the selected validation branch.",
                    "repair_job_id": "repair-live-001",
                    "requested_by": "p-01",
                    "requested_at_utc": "2026-08-06T00:04:00Z",
                    "write_approval_id": "write-live-001",
                    "write_requested_at_utc": "2026-08-06T00:04:01Z",
                }
            )
            self.assertEqual(selected["state"], "awaiting_write_approval")
            session.decide_write(
                {
                    "approval_id": "write-live-001",
                    "actor_id": "p-02",
                    "decision": "approved",
                    "approved_at_utc": "2026-08-06T00:04:02Z",
                }
            )
            assert session._intent is not None
            sandbox = _sandbox_result(session._intent)
            patch = sandbox.patch_files[0]
            sandbox_result = session.submit_sandbox(
                {
                    "repair_job_id": sandbox.repair_job_id,
                    "worktree_receipt_sha256": sandbox.worktree_receipt_sha256,
                    "task_branch_sha256": sandbox.task_branch_sha256,
                    "patch_sha256": sandbox.patch_sha256,
                    "checkpoint_sha256": sandbox.checkpoint_sha256,
                    "test_sha256": sandbox.test_sha256,
                    "budget_sha256": sandbox.budget_sha256,
                    "tests_passed": True,
                    "reflection_passed": True,
                    "exact_commit_sha": sandbox.exact_commit_sha,
                    "expected_tree_sha": sandbox.expected_tree_sha,
                    "patch_files": [
                        {
                            "path": patch.path,
                            "mode": patch.mode,
                            "content_base64": base64.b64encode(patch.content).decode("ascii"),
                        }
                    ],
                    "base_tree_sha": "b" * 40,
                    "commit_message": "Phase 11D repair repair-live-001",
                    "commit_timestamp_utc": "2026-08-06T00:05:00Z",
                    "observed_at_utc": "2026-08-06T00:05:00Z",
                    "draft_approval_id": "draft-live-001",
                    "draft_requested_at_utc": "2026-08-06T00:05:01Z",
                }
            )
            self.assertEqual(sandbox_result["state"], "awaiting_draft_pr_approval")
            session.decide_draft_pr(
                {
                    "approval_id": "draft-live-001",
                    "actor_id": "p-03",
                    "decision": "approved",
                    "approved_at_utc": "2026-08-06T00:05:02Z",
                }
            )
            published = session.publish({"published_at_utc": "2026-08-06T00:06:00Z"})
            self.assertEqual(published["state"], "published")
            self.assertTrue(session.terminal)
            disk_text = "\n".join(
                path.read_text(encoding="utf-8") for path in Path(temp).glob("*.json")
            )
            self.assertNotIn(finding.title, disk_text)
            self.assertNotIn(finding.description, disk_text)
            self.assertNotIn("Change only the selected validation branch.", disk_text)
            self.assertNotIn("patched = True", disk_text)
            self.assertTrue((Path(temp) / "repair-receipt.json").is_file())
            self.assertTrue((Path(temp) / "draft-pr-receipt.json").is_file())
            self.assertTrue((Path(temp) / "operator-session-receipt.json").is_file())
            gate_b_executor._write_json(
                Path(temp) / "review-cohort-receipt.json", review_receipt
            )

            closeout = gate_b_executor.prepare_pilot_closeout(
                authorization=approved,
                participants=participants,
                selection_receipt=selection_receipt,
                review_receipt=review_receipt,
                repair_receipt=gate_b_executor.load_json(Path(temp) / "repair-receipt.json"),
                draft_pr_receipt=gate_b_executor.load_json(
                    Path(temp) / "draft-pr-receipt.json"
                ),
                feedback={
                    "actor_id": "p-01",
                    "finding_id": finding.finding_id,
                    "decision": "accepted",
                    "repair_requested": True,
                    "draft_pr_adopted": True,
                    "active_review_seconds": 180,
                    "paused_review_seconds": 30,
                    "rationale_text": "The exact draft fixes the selected validation defect.",
                    "submitted_at_utc": "2026-08-06T00:07:00Z",
                },
                output_directory=Path(temp),
            )
            final = gate_b_executor.approve_pilot_closeout(
                participants=participants,
                output_directory=Path(temp),
                actor_id="p-03",
                approved_at_utc="2026-08-06T00:08:00Z",
                exact_approval_text=closeout["exact_approval_text"],
            )
            self.assertTrue(final["phase11d_pilot_complete"])
            acceptance = gate_b_executor.load_json(
                Path(temp) / "final-acceptance-report.json"
            )
            self.assertTrue(acceptance["phase11d_pilot_complete"])
            self.assertFalse(acceptance["production_ready"])
            self.assertFalse(acceptance["final_project_complete"])
            self.assertEqual(acceptance["model_quality_status"], "not_measured")
            self.assertEqual(acceptance["formal_quality_status"], "incomplete")
            manifest = gate_b_executor.load_json(Path(temp) / "canonical-manifest.json")
            self.assertEqual(
                manifest["manifest_sha256"],
                gate_b_executor._self_hash(manifest, "manifest_sha256"),
            )
            disk_text = "\n".join(
                path.read_text(encoding="utf-8") for path in Path(temp).glob("*.json")
            )
            self.assertNotIn(
                "The exact draft fixes the selected validation defect.", disk_text
            )

            server = gate_b_executor.LoopbackReviewRepairServer(
                session,
                bearer_token="t" * 32,
            )
            self.assertEqual(server.address[0], "127.0.0.1")
            serving = threading.Thread(target=server.serve_until_terminal, daemon=True)
            serving.start()
            url = f"http://127.0.0.1:{server.address[1]}/v1/status"
            with self.assertRaises(urllib_error.HTTPError) as unauthorized:
                urllib_request.urlopen(url, timeout=2)
            self.assertEqual(unauthorized.exception.code, 401)
            request = urllib_request.Request(
                url,
                headers={"Authorization": f"Bearer {'t' * 32}"},
            )
            with urllib_request.urlopen(request, timeout=2) as response:
                operator_status = json.loads(response.read())
            self.assertEqual(operator_status["state"], "published")
            serving.join(timeout=2)
            self.assertFalse(serving.is_alive())
            server.close()

    @mock.patch.dict(os.environ, {"PHASE11D_PUBLISHER_TEST_KEY": "test-provider-key"})
    def test_gate_b_publisher_reconciles_after_remote_success_and_quarantines_drift(self) -> None:
        approved, participants, repository, descriptor, runtime = _approved_gate_b_context()
        candidate, candidates = _repair_candidates()
        selection_receipt = gate_b_executor.build_selection_receipt(
            authorization=approved,
            candidates=candidates,
            excluded_counts={"draft": 0, "malformed": 0, "outside_window": 0, "wrong_base": 0},
        )
        coordinator = gate_b_executor.GateBRepairCoordinator(
            authorization=approved,
            participants=participants,
            repository=repository,
            credential_descriptor=descriptor,
            runtime=runtime,
            source_root=Path(__file__).resolve().parents[1],
            selection_receipt=selection_receipt,
            now_utc="2026-08-06T00:02:00Z",
        )
        coordinator.select_finding(
            candidate=candidate,
            review=_completed_review(candidate),
            finding_id="f" * 64,
            selection_id="selection-002",
            selector_id="p-01",
            selected_at_utc="2026-08-06T00:03:00Z",
        )
        intent = coordinator.prepare_repair(
            candidate=candidate,
            plan_text="Change only the selected validation branch.",
            repair_job_id="repair-002",
            requested_by="p-01",
            requested_at_utc="2026-08-06T00:04:00Z",
        )
        coordinator.request_write_approval(approval_id="write-002", requested_at_utc="2026-08-06T00:04:01Z")
        coordinator.decide_write_approval(
            approval_id="write-002",
            actor_id="p-02",
            decision="approved",
            approved_at_utc="2026-08-06T00:04:02Z",
        )
        sandbox = _sandbox_result(intent)
        coordinator.submit_sandbox_result(result=sandbox, observed_at_utc="2026-08-06T00:05:00Z")
        material = gate_b_executor.build_draft_publication_material(
            intent=intent,
            sandbox=sandbox,
            base_tree_sha="b" * 40,
            commit_message="Phase 11D repair repair-002",
            commit_timestamp_utc="2026-08-06T00:05:00Z",
        )
        coordinator.request_draft_pr_approval(
            approval_id="draft-002", material=material, requested_at_utc="2026-08-06T00:05:01Z"
        )
        coordinator.decide_draft_pr_approval(
            approval_id="draft-002",
            actor_id="p-03",
            decision="approved",
            approved_at_utc="2026-08-06T00:05:02Z",
        )
        with tempfile.TemporaryDirectory() as temp:
            key_file = Path(temp) / "private-key.pem"
            key_file.write_bytes(b"test-private-key")
            transport = _PublisherTransport(
                owner="example-owner",
                repository="example-repo",
                base_sha=intent.base_sha,
                expected_tree_sha=material.expected_tree_sha,
                expected_commit_sha=material.exact_commit_sha,
                fail_after_draft_pr=True,
            )
            journal = gate_b_executor.PublicationJournal(Path(temp) / "publication.json")
            publisher = gate_b_executor.GitHubDraftPublisher(
                transport,
                authorization=approved,
                participants=participants,
                repository_authorization=repository,
                credential_descriptor=descriptor,
                runtime=runtime,
                source_root=Path(__file__).resolve().parents[1],
                github_app_private_key_file=key_file,
                provider_key_environment="PHASE11D_PUBLISHER_TEST_KEY",
                token=gate_b_executor.InstallationToken(
                    value="test-installation-token",
                    expires_at_utc="2026-09-01T00:00:00Z",
                    app_id=4421400,
                    installation_id=149747930,
                ),
                owner="example-owner",
                repository="example-repo",
                expected_repository_id=1301558766,
                expected_app_id=4421400,
                expected_installation_id=149747930,
                journal=journal,
            )
            receipt = coordinator.publish_draft_pr(
                publisher=publisher, published_at_utc="2026-08-06T00:06:00Z"
            )
            self.assertEqual(receipt.draft_pr_id, "draft-pr-1")
            self.assertEqual(len(transport.prs), 1)
            self.assertEqual(
                transport.calls.count(("POST", "/repos/example-owner/example-repo/pulls")), 1
            )
            self.assertEqual(journal.load()["state"], "receipt_reconciled")
            self.assertEqual(transport.commit_parents, [[intent.head_sha]])

            publication = coordinator._publication
            assert publication is not None
            legacy_row = copy.deepcopy(journal.load())
            legacy_row["schema_version"] = gate_b_executor.LEGACY_JOURNAL_SCHEMA_VERSION
            legacy_row.pop("source_head_sha")
            legacy_row["journal_sha256"] = gate_b_executor._self_hash(
                legacy_row, "journal_sha256"
            )
            legacy_journal = gate_b_executor.PublicationJournal(
                Path(temp) / "legacy-publication.json"
            )
            legacy_journal.path.write_bytes(gate_b_executor.canonical_json(legacy_row) + b"\n")
            legacy_transport = _PublisherTransport(
                owner="example-owner",
                repository="example-repo",
                base_sha=intent.base_sha,
                expected_tree_sha=material.expected_tree_sha,
                expected_commit_sha=material.exact_commit_sha,
            )
            legacy_publisher = gate_b_executor.GitHubDraftPublisher(
                legacy_transport,
                authorization=approved,
                participants=participants,
                repository_authorization=repository,
                credential_descriptor=descriptor,
                runtime=runtime,
                source_root=Path(__file__).resolve().parents[1],
                github_app_private_key_file=key_file,
                provider_key_environment="PHASE11D_PUBLISHER_TEST_KEY",
                token=gate_b_executor.InstallationToken(
                    value="test-installation-token",
                    expires_at_utc="2026-09-01T00:00:00Z",
                    app_id=4421400,
                    installation_id=149747930,
                ),
                owner="example-owner",
                repository="example-repo",
                expected_repository_id=1301558766,
                expected_app_id=4421400,
                expected_installation_id=149747930,
                journal=legacy_journal,
            )
            with self.assertRaisesRegex(
                gate_b_executor.GateBExecutorError,
                "publisher_journal_upgrade_required",
            ):
                legacy_publisher.publish(publication, now_utc="2026-08-06T00:06:00Z")
            self.assertEqual(legacy_transport.calls, [])

            blocked_authorization = copy.deepcopy(approved)
            blocked_authorization["gate_b_allowed"] = False
            blocked_transport = _PublisherTransport(
                owner="example-owner",
                repository="example-repo",
                base_sha=intent.base_sha,
                expected_tree_sha=material.expected_tree_sha,
                expected_commit_sha=material.exact_commit_sha,
            )
            blocked_publisher = gate_b_executor.GitHubDraftPublisher(
                blocked_transport,
                authorization=blocked_authorization,
                participants=participants,
                repository_authorization=repository,
                credential_descriptor=descriptor,
                runtime=runtime,
                source_root=Path(__file__).resolve().parents[1],
                github_app_private_key_file=key_file,
                provider_key_environment="PHASE11D_PUBLISHER_TEST_KEY",
                token=gate_b_executor.InstallationToken(
                    value="test-installation-token",
                    expires_at_utc="2026-09-01T00:00:00Z",
                    app_id=4421400,
                    installation_id=149747930,
                ),
                owner="example-owner",
                repository="example-repo",
                expected_repository_id=1301558766,
                expected_app_id=4421400,
                expected_installation_id=149747930,
                journal=gate_b_executor.PublicationJournal(Path(temp) / "blocked.json"),
            )
            with self.assertRaisesRegex(gate_b_executor.GateBExecutorError, "gate_b_closed"):
                blocked_publisher.publish(publication, now_utc="2026-08-06T00:06:00Z")
            self.assertEqual(blocked_transport.calls, [])

            forward_transport = _PublisherTransport(
                owner="example-owner",
                repository="example-repo",
                base_sha=intent.base_sha,
                current_base_sha="8" * 40,
                expected_tree_sha=material.expected_tree_sha,
                expected_commit_sha=material.exact_commit_sha,
            )
            forward_publisher = gate_b_executor.GitHubDraftPublisher(
                forward_transport,
                authorization=approved,
                participants=participants,
                repository_authorization=repository,
                credential_descriptor=descriptor,
                runtime=runtime,
                source_root=Path(__file__).resolve().parents[1],
                github_app_private_key_file=key_file,
                provider_key_environment="PHASE11D_PUBLISHER_TEST_KEY",
                token=gate_b_executor.InstallationToken(
                    value="test-installation-token",
                    expires_at_utc="2026-09-01T00:00:00Z",
                    app_id=4421400,
                    installation_id=149747930,
                ),
                owner="example-owner",
                repository="example-repo",
                expected_repository_id=1301558766,
                expected_app_id=4421400,
                expected_installation_id=149747930,
                journal=gate_b_executor.PublicationJournal(Path(temp) / "forward.json"),
            )
            forward_receipt = forward_publisher.publish(
                publication, now_utc="2026-08-06T00:06:00Z"
            )
            self.assertEqual(forward_receipt.draft_pr_id, "draft-pr-1")
            self.assertEqual(forward_transport.commit_parents, [[intent.head_sha]])
            self.assertIn(
                (
                    "GET",
                    f"/repos/example-owner/example-repo/compare/{intent.base_sha}...{'8' * 40}",
                ),
                forward_transport.calls,
            )

            drift_transport = _PublisherTransport(
                owner="example-owner",
                repository="example-repo",
                base_sha=intent.base_sha,
                current_base_sha="9" * 40,
                expected_tree_sha=material.expected_tree_sha,
                expected_commit_sha=material.exact_commit_sha,
                compare_status="diverged",
            )
            drift_journal = gate_b_executor.PublicationJournal(Path(temp) / "drift.json")
            drift_publisher = gate_b_executor.GitHubDraftPublisher(
                drift_transport,
                authorization=approved,
                participants=participants,
                repository_authorization=repository,
                credential_descriptor=descriptor,
                runtime=runtime,
                source_root=Path(__file__).resolve().parents[1],
                github_app_private_key_file=key_file,
                provider_key_environment="PHASE11D_PUBLISHER_TEST_KEY",
                token=gate_b_executor.InstallationToken(
                    value="test-installation-token",
                    expires_at_utc="2026-09-01T00:00:00Z",
                    app_id=4421400,
                    installation_id=149747930,
                ),
                owner="example-owner",
                repository="example-repo",
                expected_repository_id=1301558766,
                expected_app_id=4421400,
                expected_installation_id=149747930,
                journal=drift_journal,
            )
            with self.assertRaisesRegex(gate_b_executor.GateBExecutorError, "publisher_base_drift"):
                drift_publisher.publish(publication, now_utc="2026-08-06T00:06:00Z")
            self.assertEqual(drift_journal.load()["state"], "quarantined")
            self.assertNotIn(("POST", "/repos/example-owner/example-repo/git/refs"), drift_transport.calls)

            source_drift_transport = _PublisherTransport(
                owner="example-owner",
                repository="example-repo",
                base_sha=intent.base_sha,
                source_tree_sha="7" * 40,
                expected_tree_sha=material.expected_tree_sha,
                expected_commit_sha=material.exact_commit_sha,
            )
            source_drift_publisher = gate_b_executor.GitHubDraftPublisher(
                source_drift_transport,
                authorization=approved,
                participants=participants,
                repository_authorization=repository,
                credential_descriptor=descriptor,
                runtime=runtime,
                source_root=Path(__file__).resolve().parents[1],
                github_app_private_key_file=key_file,
                provider_key_environment="PHASE11D_PUBLISHER_TEST_KEY",
                token=gate_b_executor.InstallationToken(
                    value="test-installation-token",
                    expires_at_utc="2026-09-01T00:00:00Z",
                    app_id=4421400,
                    installation_id=149747930,
                ),
                owner="example-owner",
                repository="example-repo",
                expected_repository_id=1301558766,
                expected_app_id=4421400,
                expected_installation_id=149747930,
                journal=gate_b_executor.PublicationJournal(
                    Path(temp) / "source-drift.json"
                ),
            )
            with self.assertRaisesRegex(
                gate_b_executor.GateBExecutorError, "publisher_source_head_drift"
            ):
                source_drift_publisher.publish(
                    publication, now_utc="2026-08-06T00:06:00Z"
                )
            self.assertEqual(source_drift_transport.commit_parents, [])

    def test_gate_b_github_app_jwt_has_a_valid_rs256_signature(self) -> None:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError:
            self.skipTest("cryptography is unavailable in the local validation environment")
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        token = gate_b_executor.build_github_app_jwt(
            app_id=4421400,
            private_key=pem,
            issued_at_epoch=1_754_000_000,
        )
        header_text, payload_text, signature_text = token.split(".")
        header = json.loads(base64.urlsafe_b64decode(header_text + "=="))
        payload = json.loads(base64.urlsafe_b64decode(payload_text + "=="))
        signature = base64.urlsafe_b64decode(signature_text + "==")
        self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(payload["iss"], "4421400")
        key.public_key().verify(
            signature,
            f"{header_text}.{payload_text}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def test_hash_only_credential_descriptor_is_self_bound(self) -> None:
        descriptor = pilot.build_credential_descriptor(
            authorization_id="phase11d-gate-b-human-pilot-v1-20260805-001",
            credential_descriptor_id="phase11d-gate-b-credentials-20260805-001",
            github_app_id=4421400,
            github_app_installation_id=149747930,
            github_app_private_key_fingerprint_sha256="a" * 64,
            provider_id="zhipu",
            provider_model_snapshot="glm-5.2",
            provider_api_key_fingerprint_sha256="b" * 64,
            credential_delivery_mode="local_secret_store_to_ephemeral_process_environment",
            credential_revoke_procedure="github_delete_private_key_and_zhipu_disable_api_key",
        )
        self.assertEqual(
            descriptor["credential_descriptor_sha256"],
            pilot._self_hash(descriptor, "credential_descriptor_sha256"),
        )
        pilot.validate_credential_descriptor(descriptor)

        tampered = copy.deepcopy(descriptor)
        tampered["provider_model_snapshot"] = "glm-5.3"
        with self.assertRaisesRegex(pilot.Phase11DError, "canonical hash mismatch"):
            pilot.validate_credential_descriptor(tampered)

    def test_credential_descriptor_rejects_raw_private_key_content(self) -> None:
        descriptor = pilot.build_credential_descriptor(
            authorization_id="phase11d-gate-b-human-pilot-v1-20260805-001",
            credential_descriptor_id="phase11d-gate-b-credentials-20260805-001",
            github_app_id=4421400,
            github_app_installation_id=149747930,
            github_app_private_key_fingerprint_sha256="a" * 64,
            provider_id="zhipu",
            provider_model_snapshot="glm-5.2",
            provider_api_key_fingerprint_sha256="b" * 64,
            credential_delivery_mode="local_secret_store_to_ephemeral_process_environment",
            credential_revoke_procedure="github_delete_private_key_and_zhipu_disable_api_key",
        )
        descriptor["provider_model_snapshot"] = "-----BEGIN PRIVATE KEY-----"
        descriptor["credential_descriptor_sha256"] = pilot._self_hash(
            descriptor,
            "credential_descriptor_sha256",
        )
        with self.assertRaisesRegex(pilot.Phase11DError, "stable identifier"):
            pilot.validate_credential_descriptor(descriptor)

    def test_gate_b_preflight_freezes_runtime_and_remains_closed(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        preflight = pilot.freeze_gate_b_preflight(
            source_root=source_root,
            authorization_id="phase11d-gate-b-human-pilot-v1-20260805-001",
            preflight_id="phase11d-gate-b-preflight-20260806-001",
            created_at_utc="2026-08-06T00:00:00Z",
        )
        self.assertEqual(preflight["execution_capability"], "preflight_only")
        self.assertFalse(preflight["real_operations_enabled"])
        self.assertFalse(preflight["credentials_read"])
        self.assertFalse(preflight["network_opened"])
        pilot.validate_gate_b_preflight(preflight)

        tampered = copy.deepcopy(preflight)
        tampered["real_operations_enabled"] = True
        tampered["preflight_sha256"] = pilot._self_hash(tampered, "preflight_sha256")
        with self.assertRaisesRegex(pilot.Phase11DError, "real operation is prohibited"):
            pilot.validate_gate_b_preflight(tampered)

    def test_generate_and_validate_full_gate_a_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pilot.write_gate_a_bundle(root)

            summary = pilot.validate_bundle(root)

            self.assertEqual(summary.selected_prs, 20)
            self.assertEqual(summary.completed_headlines, 16)
            self.assertEqual(summary.feedback_eligible_findings, 3)
            self.assertEqual(summary.repair_jobs, 2)
            self.assertEqual(summary.draft_pr_receipts, 1)
            self.assertFalse(summary.business_claim_allowed)
            self.assertFalse(summary.gate_b_allowed)
            self.assertIn(
                "permission_not_granted:allow_real_provider_calls",
                summary.gate_b_blockers,
            )

    def test_manifest_detects_canonical_artifact_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pilot.write_gate_a_bundle(root)
            cohort_path = root / "cohort.json"
            cohort = json.loads(cohort_path.read_text(encoding="utf-8"))
            cohort["selected_prs"][0]["selection_rank_sha256"] = "a" * 64
            pilot._write_json(cohort_path, cohort)

            with self.assertRaisesRegex(pilot.Phase11DError, "canonical SHA-256 mismatch"):
                pilot.validate_bundle(root)

    def test_gate_b_template_requires_complete_fields_and_permission_state(self) -> None:
        template = pilot.build_gate_b_template()
        allowed, blockers = pilot.evaluate_gate_b_template(template)
        self.assertFalse(allowed)
        self.assertNotIn("missing:authorization_id", blockers)
        self.assertNotIn("missing:github_app_installation_id", blockers)
        self.assertEqual(
            template["required_fields"]["authorization_id"],
            "phase11d-gate-b-human-pilot-v1-20260805-001",
        )
        self.assertEqual(template["required_fields"]["github_app_installation_id"], 149747930)
        self.assertIn("exact_approval_text_missing", blockers)

        filled = copy.deepcopy(template)
        for field in pilot.GATE_B_REQUIRED_FIELDS:
            filled["required_fields"][field] = "filled"
        filled["exact_approval_text"] = "OWNER EXACT APPROVAL TEXT"
        for name in (
            "allow_real_provider_calls",
            "allow_real_github_repair_branch_push",
            "allow_real_draft_repair_pr",
        ):
            filled["permission_switches"][name] = True
        allowed, blockers = pilot.evaluate_gate_b_template(filled)
        self.assertTrue(allowed, blockers)

        for name in (
            "allow_real_provider_calls",
            "allow_real_github_repair_branch_push",
            "allow_real_draft_repair_pr",
        ):
            bad = copy.deepcopy(filled)
            bad["permission_switches"][name] = False
            allowed, blockers = pilot.evaluate_gate_b_template(bad)
            self.assertFalse(allowed)
            self.assertIn(f"permission_not_granted:{name}", blockers)

        for name in (
            "allow_comments_checks_labels_reviews",
            "allow_pilot_pr_ready",
            "allow_pilot_pr_merge",
            "allow_default_branch_mutation",
            "allow_auto_merge",
            "allow_agent_push_merge_master",
        ):
            bad = copy.deepcopy(filled)
            bad["permission_switches"][name] = True
            allowed, blockers = pilot.evaluate_gate_b_template(bad)
            self.assertFalse(allowed)
            self.assertIn(f"prohibited_permission_enabled:{name}", blockers)

    def test_unknown_fields_duplicate_keys_bool_ints_and_counter_floats_fail(self) -> None:
        cohort, headline, authorization, reviews, _repairs, _drafts, *_rest = _valid_parts()

        with tempfile.TemporaryDirectory() as temp:
            duplicate = Path(temp) / "duplicate.json"
            duplicate.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
            with self.assertRaisesRegex(pilot.Phase11DError, "duplicate JSON key"):
                pilot.load_json(duplicate)

        bad_authorization = copy.deepcopy(authorization)
        bad_authorization["extra"] = "not allowed"
        with self.assertRaisesRegex(pilot.Phase11DError, "unexpected extra"):
            pilot.validate_authorization(bad_authorization)

        bad_cohort = copy.deepcopy(cohort)
        bad_cohort["selected_pr_count"] = True
        with self.assertRaisesRegex(pilot.Phase11DError, "expected integer"):
            pilot.validate_cohort(bad_cohort)

        bad_reviews = copy.deepcopy(reviews)
        bad_reviews[0]["cost_micro_cny"] = 1.5
        with self.assertRaisesRegex(pilot.Phase11DError, "expected integer"):
            pilot.validate_reviews(bad_reviews, cohort, headline)

    def test_unauthorized_actors_cannot_start_or_approve(self) -> None:
        _cohort, _headline, _authorization, reviews, repairs, *_rest = _valid_parts()
        denied_roles = ("viewer", "reviewer", "webhook", "model", "Finding", "agent", "system")
        denied_methods = ("anonymous", "agent", "finding", "github_webhook", "model", "system", "webhook")

        for role in denied_roles:
            with self.subTest(start_role=role):
                candidate = copy.deepcopy(repairs)
                candidate[0]["request_actor_role"] = role
                with self.assertRaises(pilot.Phase11DError):
                    pilot.validate_repairs(candidate, reviews)

            with self.subTest(write_role=role):
                candidate = copy.deepcopy(repairs)
                candidate[0]["write_approval"]["actor_role"] = role
                with self.assertRaises(pilot.Phase11DError):
                    pilot.validate_repairs(candidate, reviews)

        for method in denied_methods:
            with self.subTest(start_method=method):
                candidate = copy.deepcopy(repairs)
                candidate[0]["request_actor_method"] = method
                with self.assertRaises(pilot.Phase11DError):
                    pilot.validate_repairs(candidate, reviews)

            with self.subTest(write_method=method):
                candidate = copy.deepcopy(repairs)
                candidate[0]["write_approval"]["actor_method"] = method
                with self.assertRaises(pilot.Phase11DError):
                    pilot.validate_repairs(candidate, reviews)

    def test_approval_race_replay_and_stale_bindings_fail_closed(self) -> None:
        _cohort, _headline, _authorization, reviews, repairs, *_rest = _valid_parts()

        replay = copy.deepcopy(repairs)
        replay[0]["draft_pr_approval"]["approval_id"] = replay[0]["write_approval"][
            "approval_id"
        ]
        with self.assertRaisesRegex(pilot.Phase11DError, "approval replay"):
            pilot.validate_repairs(replay, reviews)

        drift_values = {
            "base_sha": "4" * 40,
            "head_sha": "5" * 40,
            "plan_sha256": "a" * 64,
            "patch_sha256": "b" * 64,
            "test_sha256": "c" * 64,
            "checkpoint_sha256": "d" * 64,
            "budget_sha256": "e" * 64,
        }
        for field, value in drift_values.items():
            with self.subTest(stale_field=field):
                candidate = copy.deepcopy(repairs)
                candidate[0][field] = value
                with self.assertRaisesRegex(pilot.Phase11DError, "approval binding is stale"):
                    pilot.validate_repairs(candidate, reviews)

        policy_drift = copy.deepcopy(repairs)
        policy_drift[0]["sandbox"]["network_mode"] = "bridge"
        with self.assertRaisesRegex(pilot.Phase11DError, "approval binding is stale"):
            pilot.validate_repairs(policy_drift, reviews)

    def test_declines_test_failures_budget_kill_switch_and_credential_revocation(self) -> None:
        _cohort, _headline, authorization, reviews, repairs, _drafts, _feedback, _time, incidents = (
            _valid_parts()
        )

        declined = copy.deepcopy(repairs)
        declined[0]["write_approval"]["decision"] = "declined"
        with self.assertRaisesRegex(pilot.Phase11DError, "declined WRITE"):
            pilot.validate_repairs(declined, reviews)

        test_failed = copy.deepcopy(repairs)
        test_failed[0]["sandbox"]["tests_passed"] = False
        test_failed[0]["draft_pr_approval"]["binding_sha256"] = pilot._approval_binding(
            "draft_pr",
            test_failed[0],
        )
        with self.assertRaisesRegex(pilot.Phase11DError, "failed tests"):
            pilot.validate_repairs(test_failed, reviews)

        budget_exhausted = copy.deepcopy(repairs)
        budget_exhausted[0]["final_status"] = "budget_exhausted"
        budget_exhausted[0]["failure_category"] = "budget_exhausted"
        with self.assertRaisesRegex(pilot.Phase11DError, "budget exhaustion"):
            pilot.validate_repairs(budget_exhausted, reviews)

        killed = copy.deepcopy(authorization)
        killed["incident_policy"]["kill_switch_active"] = True
        _rehash_authorization(killed)
        with self.assertRaisesRegex(pilot.Phase11DError, "kill switch"):
            pilot.validate_authorization(killed)

        credential_not_isolated = copy.deepcopy(incidents)
        credential_not_isolated[0]["credential_revoked_or_isolated"] = False
        with self.assertRaisesRegex(pilot.Phase11DError, "credential"):
            pilot.validate_incidents(credential_not_isolated)

    def test_provider_and_publisher_failures_are_fail_closed(self) -> None:
        cohort, headline, _authorization, reviews, repairs, drafts, *_rest = _valid_parts()

        text_only_completed = copy.deepcopy(reviews)
        text_only_completed[1]["status"] = "completed"
        text_only_completed[1]["terminal_category"] = "provider_text_only_response"
        with self.assertRaisesRegex(pilot.Phase11DError, "text-only"):
            pilot.validate_reviews(text_only_completed, cohort, headline)

        malformed_completed = copy.deepcopy(reviews)
        malformed_completed[6]["status"] = "completed"
        malformed_completed[6]["terminal_category"] = "provider_malformed_tool_response"
        with self.assertRaisesRegex(pilot.Phase11DError, "completed terminal"):
            pilot.validate_reviews(malformed_completed, cohort, headline)

        usage_ambiguity = copy.deepcopy(reviews)
        usage_ambiguity[10]["status"] = "failed"
        usage_ambiguity[10]["terminal_category"] = "provider_usage_ambiguity"
        pilot.validate_reviews(usage_ambiguity, cohort, headline)

        publisher_failure = copy.deepcopy(repairs)
        publisher_failure[0]["publisher_status"] = "publisher_failed"
        publisher_failure[0]["final_status"] = "publisher_failed"
        publisher_failure[0]["failure_category"] = "publisher_failed"
        with self.assertRaisesRegex(pilot.Phase11DError, "publisher uncertainty"):
            pilot.validate_repairs(publisher_failure, reviews)

        with self.assertRaisesRegex(pilot.Phase11DError, "missing receipt"):
            pilot.validate_drafts([], repairs)

        draft_mismatch = copy.deepcopy(drafts)
        draft_mismatch[0]["commit_sha"] = "4" * 40
        with self.assertRaisesRegex(pilot.Phase11DError, "approved commit mismatch"):
            pilot.validate_drafts(draft_mismatch, repairs)

    def test_tenant_redaction_and_draft_pr_boundaries(self) -> None:
        files = _bundle()
        authorization = _object(files, "authorization.json")
        repositories = _object(files, "repository-allowlist.json")
        cohort = _object(files, "cohort.json")
        selection = _object(files, "selection-receipt.json")
        headline = _object(files, "headline-manifest.json")
        reviews = _rows(files, "review-receipts.jsonl")
        repairs = _rows(files, "repair-receipts.jsonl")
        drafts = _rows(files, "draft-pr-receipts.jsonl")

        foreign_cohort = copy.deepcopy(cohort)
        foreign_cohort["selected_prs"][0]["repository_id"] = "foreign-repo"
        linked_authorization = copy.deepcopy(authorization)
        linked_authorization["cohort_sha256"] = pilot._artifact_sha256(
            "cohort.json",
            foreign_cohort,
        )
        _rehash_authorization(linked_authorization)
        with self.assertRaisesRegex(pilot.Phase11DError, "outside repository allowlist"):
            pilot.validate_bundle_links(
                files,
                linked_authorization,
                repositories,
                foreign_cohort,
                selection,
                headline,
            )

        with self.assertRaisesRegex(pilot.Phase11DError, "prohibited raw-content"):
            pilot._scan_no_raw_content({"receipt": {"raw_diff": "diff --git a/file b/file"}})

        for field in ("ready", "merged"):
            candidate = copy.deepcopy(drafts)
            candidate[0][field] = True
            with self.subTest(field=field):
                with self.assertRaisesRegex(pilot.Phase11DError, "Draft PR must stay Draft"):
                    pilot.validate_drafts(candidate, repairs)

        leaked = _bundle()
        _rows(leaked, "review-receipts.jsonl")[0]["raw_diff"] = "diff --git a b"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_bundle(root, leaked, rebuild_manifest=True)
            with self.assertRaisesRegex(pilot.Phase11DError, "prohibited raw-content"):
                pilot.validate_bundle(root)

        missing_feedback = copy.deepcopy(_rows(files, "feedback-receipts.jsonl"))
        missing_feedback.append(copy.deepcopy(missing_feedback[0]))
        with self.assertRaisesRegex(pilot.Phase11DError, "duplicate finding feedback"):
            pilot.validate_feedback(missing_feedback, reviews)

    def test_phase11c_auth004_and_claim_boundaries_cannot_drift(self) -> None:
        cohort, _headline, authorization, reviews, repairs, drafts, feedback, time_cost, incidents = (
            _valid_parts()
        )

        phase11c_drift = copy.deepcopy(authorization)
        phase11c_drift["phase11c_facts"]["headline_cohort_status"] = "completed"
        _rehash_authorization(phase11c_drift)
        with self.assertRaisesRegex(pilot.Phase11DError, "Phase 11C facts drifted"):
            pilot.validate_authorization(phase11c_drift)

        auth004_drift = copy.deepcopy(authorization)
        auth004_drift["auth004_boundary"]["completed"] = 1
        _rehash_authorization(auth004_drift)
        with self.assertRaisesRegex(pilot.Phase11DError, "auth-004 boundary drifted"):
            pilot.validate_authorization(auth004_drift)

        business = pilot._business_report(cohort, reviews, repairs, drafts, feedback, time_cost, incidents)
        claim = pilot._claim_decision()
        acceptance = pilot._acceptance_report(("gate_b_closed",))
        business["business_claim_allowed"] = True
        with self.assertRaisesRegex(pilot.Phase11DError, "report does not recompute"):
            pilot.validate_reports(
                business,
                claim,
                acceptance,
                cohort,
                reviews,
                repairs,
                drafts,
                feedback,
                time_cost,
                incidents,
            )


if __name__ == "__main__":
    unittest.main()
