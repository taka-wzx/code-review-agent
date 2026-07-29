from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib import error

from alembic import command
from sqlalchemy import inspect, text

from code_review_agent.database import (
    _alembic_config,
    create_database_engine,
    current_revision,
    sqlite_database_url,
    upgrade_database,
)
from code_review_agent.github_sandbox_publish import (
    AUTHORIZATION_SCHEMA_VERSION,
    AuthorizationCase,
    GitBlob,
    GitHubCanaryPublication,
    GitHubCanaryPublishRequest,
    GitHubCanaryStore,
    GitHubFailure,
    GitHubResponse,
    GitHubSandboxAuthorization,
    GitHubSandboxPublicationError,
    InstallationToken,
    StrictGitHubHttpsTransport,
    classify_github_failure,
    sha256_hex,
)
from code_review_agent.identity import Principal, Role
from code_review_agent.repair_publish import (
    DraftPrPublicationError,
    GitHubDraftPrPublisher,
)


NOW = datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc).timestamp()
BASE_SHA = "1" * 40
BASE_TREE_SHA = "0" * 40
TREE_SHA = "2" * 40
COMMIT_SHA = "3" * 40
REPAIR_BASE_SHA = "4" * 40
DIFF_SHA = "4" * 64
REPAIR_DIFF_SHA = "5" * 64
TEST_SHA = "5" * 64
BUDGET_SHA = "6" * 64
CHECKPOINT_SHA = "7" * 64
CONFIG_SHA = "8" * 64
CODE_SHA = "9" * 40


def make_authorization(**changes: object) -> GitHubSandboxAuthorization:
    values = {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "authorization_id": "auth-phase11b-test",
        "organization_id": "org-test",
        "repository_owner": "sandbox-owner",
        "repository_name": "sandbox-repo",
        "repository_id": 101,
        "github_app_id": 202,
        "installation_id": 303,
        "installation_account_id": 404,
        "allowed_base_branch": "main",
        "frozen_base_sha": BASE_SHA,
        "cases": (
            AuthorizationCase("normal", "crag-canary/normal"),
            AuthorizationCase("crash_after_branch", "crag-canary/crash-after-branch"),
            AuthorizationCase("crash_after_draft_pr", "crag-canary/crash-after-draft-pr"),
        ),
        "max_denominator": 3,
        "executable_code_sha": CODE_SHA,
        "runtime_config_sha256": CONFIG_SHA,
        "issued_at": "2026-07-28T01:00:00Z",
        "not_before": "2026-07-28T01:01:00Z",
        "expires_at": "2026-07-28T02:00:00Z",
        "max_requests": 40,
        "max_mutations": 15,
        "max_reads": 25,
        "max_branches": 3,
        "max_commits": 3,
        "max_draft_prs": 3,
        "cost_ceiling_micro_cny": 0,
        "authorization_owner": "repository-owner",
        "revocation_owner": "repository-owner",
        "kill_switch_owner": "repository-owner",
    }
    values.update(changes)
    return GitHubSandboxAuthorization(**values)


class RecordingGitHubTransport:
    real_github_writes = False

    def __init__(self, authorization: GitHubSandboxAuthorization) -> None:
        self.authorization = authorization
        self.calls: list[str] = []
        self.blobs: set[str] = set()
        self.trees: set[str] = set()
        self.commits: dict[str, str] = {authorization.frozen_base_sha: BASE_TREE_SHA}
        self.refs = {authorization.allowed_base_branch: authorization.frozen_base_sha}
        self.prs: list[dict[str, object]] = []
        self.failures: dict[str, list[object]] = {}
        self.ambiguous_after_effect: set[str] = set()
        self.repository_id = authorization.repository_id
        self.default_branch = authorization.allowed_base_branch

    @property
    def mutation_calls(self) -> list[str]:
        return [
            endpoint
            for endpoint in self.calls
            if endpoint in {"blob_create", "tree_create", "commit_create", "ref_create", "draft_pr_create"}
        ]

    def queue_failure(self, endpoint: str, value: object) -> None:
        self.failures.setdefault(endpoint, []).append(value)

    def _failure(self, endpoint: str) -> GitHubResponse | None:
        values = self.failures.get(endpoint)
        if not values:
            return None
        value = values.pop(0)
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, GitHubResponse)
        return value

    def _pr(self, body: dict[str, object], number: int) -> dict[str, object]:
        return {
            "number": number,
            "draft": body["draft"],
            "title": body["title"],
            "body": body["body"],
            "head": {"ref": body["head"], "sha": self.refs[str(body["head"])]},
            "base": {"ref": body["base"]},
        }

    def send(
        self,
        endpoint: str,
        parameters: dict[str, object],
        *,
        body: dict[str, object] | None,
        token: str,
        timeout_seconds: float,
    ) -> GitHubResponse:
        del token, timeout_seconds
        self.calls.append(endpoint)
        failure = self._failure(endpoint)
        if failure is not None:
            return failure
        if endpoint == "repository_read":
            return GitHubResponse(
                200,
                {},
                {
                    "id": self.repository_id,
                    "name": self.authorization.repository_name,
                    "owner": {"login": self.authorization.repository_owner},
                    "default_branch": self.default_branch,
                },
            )
        if endpoint == "ref_read":
            branch = str(parameters["branch"])
            if branch not in self.refs:
                return GitHubResponse(404, {}, None)
            return GitHubResponse(
                200,
                {},
                {"ref": f"refs/heads/{branch}", "object": {"sha": self.refs[branch]}},
            )
        if endpoint == "blob_create":
            assert body is not None
            content = base64.b64decode(str(body["content"]))
            blob_sha = hashlib.sha1(
                f"blob {len(content)}\0".encode("ascii") + content,
                usedforsecurity=False,
            ).hexdigest()
            self.blobs.add(blob_sha)
            response = GitHubResponse(201, {}, {"sha": blob_sha})
        elif endpoint == "blob_read":
            sha = str(parameters["sha"])
            return GitHubResponse(200, {}, {"sha": sha}) if sha in self.blobs else GitHubResponse(404, {}, None)
        elif endpoint == "tree_create":
            self.trees.add(TREE_SHA)
            response = GitHubResponse(201, {}, {"sha": TREE_SHA})
        elif endpoint == "tree_read":
            sha = str(parameters["sha"])
            return GitHubResponse(200, {}, {"sha": sha}) if sha in self.trees else GitHubResponse(404, {}, None)
        elif endpoint == "commit_create":
            self.commits[COMMIT_SHA] = TREE_SHA
            response = GitHubResponse(201, {}, {"sha": COMMIT_SHA, "tree": {"sha": TREE_SHA}})
        elif endpoint == "commit_read":
            sha = str(parameters["sha"])
            return GitHubResponse(200, {}, {"sha": sha, "tree": {"sha": self.commits[sha]}}) if sha in self.commits else GitHubResponse(404, {}, None)
        elif endpoint == "ref_create":
            assert body is not None
            branch = str(body["ref"])[len("refs/heads/") :]
            if branch in self.refs:
                return GitHubResponse(422, {}, None)
            self.refs[branch] = str(body["sha"])
            response = GitHubResponse(201, {}, {"ref": body["ref"], "object": {"sha": body["sha"]}})
        elif endpoint == "draft_pr_create":
            assert body is not None
            created = self._pr(body, len(self.prs) + 1)
            self.prs.append(created)
            response = GitHubResponse(201, {}, created)
        elif endpoint == "draft_pr_list":
            return GitHubResponse(200, {}, list(self.prs))
        elif endpoint == "draft_pr_read":
            number = int(parameters["number"])
            return GitHubResponse(200, {}, self.prs[number - 1])
        else:
            raise AssertionError(endpoint)
        if endpoint in self.ambiguous_after_effect:
            self.ambiguous_after_effect.remove(endpoint)
            raise GitHubSandboxPublicationError(GitHubFailure.TIMEOUT)
        return response


class Phase11BGitHubSandboxCanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database_url = sqlite_database_url(Path(self.temp.name) / "canary.db")
        upgrade_database(self.database_url)
        self.engine = create_database_engine(self.database_url)
        self.addCleanup(self.engine.dispose)
        self.authorization = make_authorization()
        self.transport = RecordingGitHubTransport(self.authorization)
        self.store = GitHubCanaryStore(self.engine, clock=lambda: NOW)
        self.maintainer = Principal(
            principal_id="principal-maintainer",
            user_id="user-maintainer",
            organization_id=self.authorization.organization_id,
            role=Role.MAINTAINER,
            auth_method="oidc",
        )

    def _publication(self, case_id: str = "normal") -> GitHubCanaryPublication:
        case = self.authorization.case(case_id)
        job_id = f"repair-{case_id}"
        idempotency_key = f"canary-idempotency-{case_id}"
        content = f"synthetic {case_id}\n".encode()
        blob_sha = hashlib.sha1(
            f"blob {len(content)}\0".encode("ascii") + content,
            usedforsecurity=False,
        ).hexdigest()
        marker = f"<!-- crag-canary:{sha256_hex(idempotency_key.encode())} -->"
        title = f"Synthetic canary {case_id}"
        body = f"Synthetic input only.\n\n{marker}"
        publication = GitHubCanaryPublication(
            repair_job_id=job_id,
            organization_id=self.authorization.organization_id,
            repair_repository_id="repo-internal-sandbox",
            repair_base_sha=REPAIR_BASE_SHA,
            repair_diff_sha256=REPAIR_DIFF_SHA,
            repository_owner=self.authorization.repository_owner,
            repository_name=self.authorization.repository_name,
            repository_id=self.authorization.repository_id,
            github_app_id=self.authorization.github_app_id,
            installation_id=self.authorization.installation_id,
            installation_account_id=self.authorization.installation_account_id,
            base_branch=self.authorization.allowed_base_branch,
            base_sha=self.authorization.frozen_base_sha,
            base_tree_sha=BASE_TREE_SHA,
            head_branch=case.head_branch,
            diff_sha256=DIFF_SHA,
            test_evidence_sha256=TEST_SHA,
            durable_budget_sha256=BUDGET_SHA,
            checkpoint_sha256=CHECKPOINT_SHA,
            exact_commit_sha=COMMIT_SHA,
            commit_message="Synthetic sandbox canary",
            commit_timestamp="2026-07-28T01:10:00Z",
            expected_tree_sha=TREE_SHA,
            blobs=(GitBlob(f"synthetic/{case_id}.txt", content, blob_sha),),
            title=title,
            body=body,
            title_marker_sha256=sha256_hex(title.encode()),
            body_marker_sha256=sha256_hex(marker.encode()),
            publisher_payload_sha256="auto",
            authorization_id=self.authorization.authorization_id,
            authorization_sha256=self.authorization.canonical_sha256,
            app_idempotency_key=idempotency_key,
            canary_case_id=case_id,
            executable_code_sha=self.authorization.executable_code_sha,
            runtime_config_sha256=self.authorization.runtime_config_sha256,
        )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repair_jobs "
                    "(id, organization_id, repository_id, finding_sha256, base_sha, head_sha, "
                    "state, version, attempt, checkpoint_json, checkpoint_sha256, "
                    "current_diff_sha256, budget_sha256, lease_owner, lease_token, "
                    "lease_expires_at, failure_code, created_at, updated_at) VALUES "
                    "(:id, :organization, :repository, :finding, :base, :head, 'queued_publish', "
                    "1, 1, :checkpoint_json, :checkpoint, :diff, :budget, NULL, NULL, 0, NULL, 1, 1)"
                ),
                {
                    "id": job_id,
                    "organization": publication.organization_id,
                    "repository": publication.repair_repository_id,
                    "finding": "a" * 64,
                    "base": publication.repair_base_sha,
                    "head": "b" * 40,
                    "checkpoint_json": json.dumps({"tests_sha256": publication.test_evidence_sha256}),
                    "checkpoint": publication.checkpoint_sha256,
                    "diff": publication.repair_diff_sha256,
                    "budget": publication.durable_budget_sha256,
                },
            )
        return publication

    def _approved_request(
        self,
        publication: GitHubCanaryPublication,
        *,
        actor: Principal | None = None,
    ) -> GitHubCanaryPublishRequest:
        write_id, write_sha = self.store.issue_approval(publication, kind="write", ttl_seconds=300)
        draft_id, draft_sha = self.store.issue_approval(publication, kind="draft_pr", ttl_seconds=300)
        chosen_actor = actor or self.maintainer
        self.store.decide_approval(write_id, publication, kind="write", actor=chosen_actor, approved=True)
        self.store.decide_approval(draft_id, publication, kind="draft_pr", actor=chosen_actor, approved=True)
        return GitHubCanaryPublishRequest(
            publication=publication,
            write_approval_id=write_id,
            write_approval_binding_sha256=write_sha,
            draft_pr_approval_id=draft_id,
            draft_pr_approval_binding_sha256=draft_sha,
        )

    def _token(self, **changes: object) -> InstallationToken:
        values = {
            "value": "test-installation-token-not-real",
            "app_id": self.authorization.github_app_id,
            "installation_id": self.authorization.installation_id,
            "installation_account_id": self.authorization.installation_account_id,
            "expires_at": datetime(2026, 7, 28, 1, 55, tzinfo=timezone.utc),
            "revoked": False,
        }
        values.update(changes)
        return InstallationToken(**values)

    def _publisher(self, **changes: object) -> GitHubDraftPrPublisher:
        values = {
            "feature_enabled": True,
            "authorization": self.authorization,
            "authorization_sha256": self.authorization.canonical_sha256,
            "executable_code_sha": self.authorization.executable_code_sha,
            "runtime_config_sha256": self.authorization.runtime_config_sha256,
            "repository_allowlist": frozenset(
                {
                    (
                        self.authorization.repository_owner,
                        self.authorization.repository_name,
                        self.authorization.repository_id,
                    )
                }
            ),
            "store": self.store,
            "transport": self.transport,
            "token_provider": self._token,
            "clock": lambda: NOW,
        }
        values.update(changes)
        return GitHubDraftPrPublisher(**values)

    def test_default_disabled_and_missing_configuration_fail_closed(self) -> None:
        with self.assertRaisesRegex(DraftPrPublicationError, "github_draft_pr_publisher_disabled"):
            GitHubDraftPrPublisher().publish(object())
        with self.assertRaises(GitHubSandboxPublicationError) as caught:
            GitHubDraftPrPublisher(feature_enabled=True)
        self.assertEqual(caught.exception.code, "authorization_mismatch")
        with self.assertRaises(GitHubSandboxPublicationError):
            self._publisher(real_github_writes_enabled=True)
        real_transport = StrictGitHubHttpsTransport()
        configured = self._publisher(
            transport=real_transport,
            real_github_writes_enabled=True,
        )
        self.assertTrue(configured._real_github_writes)
        self.assertEqual(self.transport.calls, [])

    def test_enabled_publisher_rejects_legacy_request_type(self) -> None:
        with self.assertRaises(GitHubSandboxPublicationError) as caught:
            self._publisher().publish(object())
        self.assertEqual(caught.exception.code, "authorization_mismatch")
        self.assertIsNone(GitHubDraftPrPublisher().lookup("missing"))
        self.assertIsNone(self._publisher().lookup("missing"))

    def test_canonical_authorization_rejects_unknown_missing_and_bad_time_fields(self) -> None:
        raw = self.authorization.to_dict()
        self.assertEqual(
            GitHubSandboxAuthorization.from_dict(raw).canonical_sha256,
            self.authorization.canonical_sha256,
        )
        for mutation in (
            lambda value: value.update({"unknown": True}),
            lambda value: value.pop("repository_id"),
            lambda value: value.update({"expires_at": "2026-07-28T01:01:00Z"}),
        ):
            changed = dict(raw)
            mutation(changed)
            with self.assertRaises(ValueError):
                GitHubSandboxAuthorization.from_dict(changed)
        schema = json.loads(
            (Path(__file__).parents[1] / "schemas" / "phase11b-github-sandbox-authorization.schema.json").read_text(encoding="utf-8")
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["max_denominator"]["const"], 3)

    def test_authorization_and_payload_validation_matrix(self) -> None:
        invalid_cases = (
            ("bad", "crag-canary/x"),
            ("normal", "main"),
            ("normal", "bad..branch"),
        )
        for case_id, branch in invalid_cases:
            with self.subTest(case_id=case_id, branch=branch), self.assertRaises(ValueError):
                AuthorizationCase(case_id, branch)

        auth_changes = (
            {"schema_version": "bad"},
            {"repository_owner": "bad owner"},
            {"repository_name": "bad/repo"},
            {"repository_id": 0},
            {"allowed_base_branch": "bad..branch"},
            {"frozen_base_sha": "bad"},
            {"cases": self.authorization.cases[:2]},
            {
                "cases": (
                    self.authorization.cases[0],
                    AuthorizationCase("crash_after_branch", "crag-canary/normal"),
                    self.authorization.cases[2],
                )
            },
            {"allowed_base_branch": "crag-canary/normal"},
            {"max_denominator": 2},
            {"executable_code_sha": "bad"},
            {"runtime_config_sha256": "bad"},
            {"issued_at": "not-a-time"},
            {"not_before": "2026-07-28T02:00:00Z"},
            {"max_requests": 101},
            {"max_requests": 10},
            {"cost_ceiling_micro_cny": 1},
            {"authorization_owner": ""},
        )
        for changes in auth_changes:
            with self.subTest(auth_changes=changes), self.assertRaises(ValueError):
                replace(self.authorization, **changes)
        raw = self.authorization.to_dict()
        for cases in ("not-an-array", [{"case_id": "normal"}]):
            invalid = dict(raw)
            invalid["cases"] = cases
            with self.assertRaises(ValueError):
                GitHubSandboxAuthorization.from_dict(invalid)
        with self.assertRaises(GitHubSandboxPublicationError):
            self.authorization.case("missing")

        content = b"synthetic\n"
        blob_sha = hashlib.sha1(
            f"blob {len(content)}\0".encode("ascii") + content,
            usedforsecurity=False,
        ).hexdigest()
        for args in (
            ("../escape", content, blob_sha, "100644"),
            ("ok.txt", b"", blob_sha, "100644"),
            ("ok.txt", content, "0" * 40, "100644"),
            ("ok.txt", content, blob_sha, "160000"),
        ):
            with self.assertRaises(ValueError):
                GitBlob(*args)

        publication = self._publication()
        payload_changes = (
            {"repository_owner": "bad owner"},
            {"repository_id": 0},
            {"head_branch": "main"},
            {"repair_base_sha": "bad"},
            {"repair_diff_sha256": "bad"},
            {"base_sha": "bad"},
            {"base_tree_sha": "bad"},
            {"diff_sha256": "bad"},
            {"commit_message": ""},
            {"commit_timestamp": "bad"},
            {"blobs": ()},
            {"canary_case_id": "bad"},
            {"title_marker_sha256": "0" * 64},
            {"body": "marker missing"},
            {"body_marker_sha256": "0" * 64},
            {"publisher_payload_sha256": "0" * 64},
        )
        for changes in payload_changes:
            if "publisher_payload_sha256" not in changes:
                changes = {**changes, "publisher_payload_sha256": "auto"}
            with self.subTest(payload_changes=changes), self.assertRaises(ValueError):
                replace(publication, **changes)
        with self.assertRaises(ValueError):
            publication.approval_binding("admin")

    def test_receipt_token_request_and_store_validation_matrix(self) -> None:
        publication = self._publication()
        request = self._approved_request(publication)
        receipt = self._publisher().publish(request)
        receipt_changes = (
            {"receipt_sha256": "bad"},
            {"environment": "production"},
            {"real_github_sandbox_writes": "yes"},
            {"real_model_calls": True},
            {"real_business_repository_writes": True},
            {"business_claim_allowed": True},
            {"quality_claim_allowed": True},
            {"production_ready": True},
        )
        for changes in receipt_changes:
            with self.subTest(receipt_changes=changes), self.assertRaises(ValueError):
                replace(receipt, **changes)
        token = self._token()
        for changes in (
            {"value": ""},
            {"app_id": 0},
            {"expires_at": datetime(2026, 7, 28, 1, 0)},
        ):
            with self.assertRaises(ValueError):
                replace(token, **changes)
        with self.assertRaises(ValueError):
            GitHubCanaryPublishRequest(
                publication,
                "",
                request.write_approval_binding_sha256,
                request.draft_pr_approval_id,
                request.draft_pr_approval_binding_sha256,
            )
        with self.assertRaises(ValueError):
            self.store.issue_approval(publication, kind="other", ttl_seconds=1)
        with self.assertRaises(ValueError):
            self.store.issue_approval(publication, kind="write", ttl_seconds=0)
        with self.assertRaises(ValueError):
            self.store.transition(publication.app_idempotency_key, "bad", "quarantined")
        with self.assertRaises(ValueError):
            self.store.finish_request(
                publication.app_idempotency_key, "missing", status="bad"
            )
        with self.assertRaises(ValueError):
            self.store.finish_request(
                publication.app_idempotency_key,
                "missing",
                status="failed",
                failure_code="not-an-enum",
            )
        self.store.quarantine(publication.app_idempotency_key, "not-an-enum")

    def test_strict_transport_projection_and_failure_matrix(self) -> None:
        class FakeResponse:
            def __init__(self, status: int, body: object, headers: dict[str, str] | None = None):
                self.status = status
                self.body = json.dumps(body).encode() if body is not None else b""
                self.headers = headers or {}

            def getcode(self) -> int:
                return self.status

            def read(self, maximum: int) -> bytes:
                return self.body[:maximum]

        class Opener:
            def __init__(self, response: object):
                self.response = response
                self.urls: list[str] = []

            def open(self, req: object, timeout: float) -> object:
                del timeout
                self.urls.append(req.full_url)  # type: ignore[attr-defined]
                if isinstance(self.response, BaseException):
                    raise self.response
                return self.response

        endpoint_cases = (
            (
                "repository_read",
                {"owner": "x", "repo": "y"},
                {"id": 1, "name": "y", "owner": {"login": "x", "secret": "drop"}, "default_branch": "main", "secret": "drop"},
                {"id": 1, "name": "y", "owner": {"login": "x"}, "default_branch": "main"},
            ),
            (
                "ref_read",
                {"owner": "x", "repo": "y", "branch": "a/b"},
                {"ref": "refs/heads/a/b", "object": {"sha": BASE_SHA, "url": "drop"}},
                {"ref": "refs/heads/a/b", "object": {"sha": BASE_SHA}},
            ),
            ("blob_read", {"owner": "x", "repo": "y", "sha": BASE_SHA}, {"sha": BASE_SHA, "content": "drop"}, {"sha": BASE_SHA}),
            ("tree_read", {"owner": "x", "repo": "y", "sha": BASE_SHA}, {"sha": BASE_SHA, "tree": ["drop"]}, {"sha": BASE_SHA}),
            ("commit_read", {"owner": "x", "repo": "y", "sha": BASE_SHA}, {"sha": BASE_SHA, "tree": {"sha": TREE_SHA}, "message": "drop"}, {"sha": BASE_SHA, "tree": {"sha": TREE_SHA}}),
            (
                "draft_pr_list",
                {"owner": "x", "repo": "y", "head": "a/b", "base": "main"},
                [{"number": 1, "draft": True, "title": "t", "body": "b", "head": {"ref": "a/b", "sha": COMMIT_SHA}, "base": {"ref": "main"}, "secret": "drop"}],
                [{"number": 1, "draft": True, "title": "t", "body": "b", "head": {"ref": "a/b", "sha": COMMIT_SHA}, "base": {"ref": "main"}}],
            ),
        )
        for endpoint, parameters, raw, expected in endpoint_cases:
            opener = Opener(FakeResponse(200, raw, {"X-RateLimit-Remaining": "9", "Secret": "drop"}))
            response = StrictGitHubHttpsTransport(opener=opener).send(
                endpoint,
                parameters,
                body=None,
                token="fake-not-real",
                timeout_seconds=1,
            )
            self.assertEqual(response.body, expected)
            self.assertEqual(response.headers, {"x-ratelimit-remaining": "9"})
            self.assertTrue(opener.urls[0].startswith("https://api.github.com/"))
        with self.assertRaises(ValueError):
            StrictGitHubHttpsTransport(max_response_bytes=1)
        transport = StrictGitHubHttpsTransport(opener=Opener(FakeResponse(200, {})))
        for token, timeout in (("", 1), ("ok", 0), ("ok", 31)):
            with self.assertRaises((ValueError, GitHubSandboxPublicationError)):
                transport.send(
                    "repository_read",
                    {"owner": "x", "repo": "y"},
                    body=None,
                    token=token,
                    timeout_seconds=timeout,
                )
        for failure, expected in (
            (TimeoutError(), "timeout"),
            (error.URLError("redacted"), "other"),
            (OSError(), "other"),
        ):
            with self.assertRaises(GitHubSandboxPublicationError) as caught:
                StrictGitHubHttpsTransport(opener=Opener(failure)).send(
                    "repository_read",
                    {"owner": "x", "repo": "y"},
                    body=None,
                    token="fake-not-real",
                    timeout_seconds=1,
                )
            self.assertEqual(caught.exception.code, expected)

    def test_no_consumed_approvals_means_zero_external_writes(self) -> None:
        publication = self._publication()
        write_id, write_sha = self.store.issue_approval(publication, kind="write", ttl_seconds=300)
        draft_id, draft_sha = self.store.issue_approval(publication, kind="draft_pr", ttl_seconds=300)
        request = GitHubCanaryPublishRequest(publication, write_id, write_sha, draft_id, draft_sha)
        with self.assertRaises(GitHubSandboxPublicationError):
            self._publisher().publish(request)
        self.assertEqual(self.transport.calls, [])

    def test_only_same_org_human_maintainer_or_admin_can_approve(self) -> None:
        publication = self._publication()
        denied = (
            Principal("p-view", "u-view", "org-test", Role.VIEWER, "oidc"),
            Principal("p-review", "u-review", "org-test", Role.REVIEWER, "oidc"),
            Principal("p-webhook", "u-webhook", "org-test", Role.MAINTAINER, "webhook"),
            Principal("p-cross", "u-cross", "org-other", Role.ORG_ADMIN, "oidc"),
        )
        for index, actor in enumerate(denied):
            approval_id, _binding = self.store.issue_approval(
                publication, kind="write", ttl_seconds=300
            )
            with self.subTest(index=index), self.assertRaises(GitHubSandboxPublicationError):
                self.store.decide_approval(
                    approval_id, publication, kind="write", actor=actor, approved=True
                )

    def test_concurrent_double_approval_has_one_winner(self) -> None:
        publication = self._publication()
        approval_id, _binding = self.store.issue_approval(
            publication, kind="write", ttl_seconds=300
        )

        def decide() -> str:
            try:
                self.store.decide_approval(
                    approval_id,
                    publication,
                    kind="write",
                    actor=self.maintainer,
                    approved=True,
                )
                return "winner"
            except GitHubSandboxPublicationError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _item: decide(), range(2)))
        self.assertEqual(results.count("winner"), 1)
        self.assertEqual(results.count("conflict_409"), 1)
        self.assertEqual(self.transport.mutation_calls, [])

    def test_normal_flow_is_exactly_once_and_receipt_is_redacted(self) -> None:
        publication = self._publication()
        request = self._approved_request(publication)
        publisher = self._publisher()
        receipt = publisher.publish(request)
        calls_after_first = list(self.transport.calls)
        second = publisher.publish(request)
        self.assertEqual(second, receipt)
        self.assertEqual(publisher.lookup(publication.app_idempotency_key), receipt)
        self.assertEqual(self.transport.calls, calls_after_first)
        self.assertEqual(
            self.transport.mutation_calls,
            ["blob_create", "tree_create", "commit_create", "ref_create", "draft_pr_create"],
        )
        self.assertEqual(receipt.environment, "github_sandbox_canary")
        self.assertTrue(receipt.synthetic_input_only)
        self.assertFalse(receipt.real_github_sandbox_writes)
        self.assertFalse(receipt.real_model_calls)
        self.assertFalse(receipt.real_business_repository_writes)
        self.assertFalse(receipt.business_claim_allowed)
        self.assertFalse(receipt.quality_claim_allowed)
        self.assertFalse(receipt.production_ready)
        serialized = json.dumps(receipt.__dict__, sort_keys=True)
        for prohibited in (publication.body, publication.title, publication.head_branch, self._token().value):
            self.assertNotIn(prohibited, serialized)

    def test_crash_after_branch_recovers_by_readback_without_second_mutation(self) -> None:
        publication = self._publication("crash_after_branch")
        request = self._approved_request(publication)
        triggered = False

        def fault(name: str) -> None:
            nonlocal triggered
            if name == "after_ref_create_before_receipt" and not triggered:
                triggered = True
                raise RuntimeError("synthetic_crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic_crash"):
            self._publisher(fault=fault).publish(request)
        self.assertEqual(self.transport.mutation_calls.count("ref_create"), 1)
        receipt = self._publisher().publish(request)
        self.assertEqual(receipt.commit_sha, COMMIT_SHA)
        self.assertEqual(self.transport.mutation_calls.count("ref_create"), 1)
        self.assertEqual(self.transport.mutation_calls.count("commit_create"), 1)
        self.assertIn("ref_read", self.transport.calls)

    def test_crash_after_draft_pr_recovers_by_list_without_second_pr(self) -> None:
        publication = self._publication("crash_after_draft_pr")
        request = self._approved_request(publication)
        triggered = False

        def fault(name: str) -> None:
            nonlocal triggered
            if name == "after_draft_pr_create_before_receipt" and not triggered:
                triggered = True
                raise RuntimeError("synthetic_crash")

        with self.assertRaisesRegex(RuntimeError, "synthetic_crash"):
            self._publisher(fault=fault).publish(request)
        receipt = self._publisher().publish(request)
        self.assertEqual(receipt.commit_sha, COMMIT_SHA)
        self.assertEqual(self.transport.mutation_calls.count("draft_pr_create"), 1)
        self.assertEqual(len(self.transport.prs), 1)
        self.assertIn("draft_pr_list", self.transport.calls)

    def test_timeout_after_pr_effect_is_read_back_not_resent(self) -> None:
        publication = self._publication("crash_after_draft_pr")
        request = self._approved_request(publication)
        self.transport.ambiguous_after_effect.add("draft_pr_create")
        receipt = self._publisher().publish(request)
        self.assertEqual(receipt.commit_sha, COMMIT_SHA)
        self.assertEqual(self.transport.mutation_calls.count("draft_pr_create"), 1)
        self.assertEqual(len(self.transport.prs), 1)

    def test_ref_collision_and_multiple_pr_candidates_quarantine(self) -> None:
        publication = self._publication("crash_after_branch")
        request = self._approved_request(publication)
        self.transport.refs[publication.head_branch] = "f" * 40
        self.transport.ambiguous_after_effect.add("ref_create")
        with self.assertRaises(GitHubSandboxPublicationError) as collision:
            self._publisher().publish(request)
        self.assertEqual(collision.exception.code, "ref_collision")
        self.assertNotEqual(
            self.store.publication(publication.app_idempotency_key)["state"],
            "receipt_reconciled",
        )

    def test_preflight_drift_installation_and_token_gates_write_zero(self) -> None:
        publication = self._publication()
        request = self._approved_request(publication)
        scenarios = (
            ("repository_mismatch", lambda: setattr(self.transport, "repository_id", 999), {}),
            ("token_revoked", lambda: None, {"token_provider": lambda: self._token(revoked=True)}),
            (
                "installation_mismatch",
                lambda: None,
                {"token_provider": lambda: self._token(installation_id=999)},
            ),
        )
        expected, mutate, publisher_changes = scenarios[0]
        mutate()
        with self.assertRaises(GitHubSandboxPublicationError) as caught:
            self._publisher(**publisher_changes).publish(request)
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(self.transport.mutation_calls, [])

    def test_every_exact_binding_drift_invalidates_old_approvals_before_io(self) -> None:
        publication = self._publication()
        request = self._approved_request(publication)
        changes = {
            "repair_base_sha": "c" * 40,
            "repair_diff_sha256": "9" * 64,
            "diff_sha256": "a" * 64,
            "test_evidence_sha256": "b" * 64,
            "durable_budget_sha256": "c" * 64,
            "checkpoint_sha256": "d" * 64,
            "base_sha": "e" * 40,
            "base_tree_sha": "d" * 40,
            "head_branch": "crag-canary/drifted",
            "exact_commit_sha": "f" * 40,
            "commit_message": "Drifted commit message",
            "authorization_id": "auth-drifted",
            "authorization_sha256": "0" * 64,
            "executable_code_sha": "a" * 40,
            "runtime_config_sha256": "b" * 64,
            "repository_id": 999,
            "github_app_id": 998,
            "installation_id": 997,
            "installation_account_id": 996,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                drifted = replace(
                    publication,
                    **{field: value, "publisher_payload_sha256": "auto"},
                )
                with self.assertRaises(ValueError):
                    GitHubCanaryPublishRequest(
                        drifted,
                        request.write_approval_id,
                        request.write_approval_binding_sha256,
                        request.draft_pr_approval_id,
                        request.draft_pr_approval_binding_sha256,
                    )
        self.assertEqual(self.transport.calls, [])

    def test_base_drift_and_protected_head_are_denied_before_mutation(self) -> None:
        publication = self._publication()
        request = self._approved_request(publication)
        self.transport.refs[publication.base_branch] = "e" * 40
        with self.assertRaises(GitHubSandboxPublicationError) as drift:
            self._publisher().publish(request)
        self.assertEqual(drift.exception.code, "base_drift")
        self.assertEqual(self.transport.mutation_calls, [])

    def test_protected_exact_head_is_denied_before_mutation(self) -> None:
        publication = self._publication()
        request = self._approved_request(publication)
        with self.assertRaises(GitHubSandboxPublicationError) as caught:
            self._publisher(protected_branches=frozenset({publication.head_branch})).publish(
                request
            )
        self.assertEqual(caught.exception.code, "branch_protected")
        self.assertEqual(self.transport.mutation_calls, [])

    def test_expired_authorization_and_token_are_denied_before_mutation(self) -> None:
        self.authorization = make_authorization(
            issued_at="2026-07-28T00:00:00Z",
            not_before="2026-07-28T00:01:00Z",
            expires_at="2026-07-28T01:00:00Z",
        )
        self.transport = RecordingGitHubTransport(self.authorization)
        publication = self._publication()
        request = self._approved_request(publication)
        with self.assertRaises(GitHubSandboxPublicationError) as caught:
            self._publisher().publish(request)
        self.assertEqual(caught.exception.code, "authorization_expired")
        self.assertEqual(self.transport.calls, [])

    def test_http_read_failure_is_stable_and_quarantined_without_mutation(self) -> None:
        publication = self._publication()
        request = self._approved_request(publication)
        self.transport.queue_failure("repository_read", GitHubResponse(401, {}, {"secret": "drop"}))
        with self.assertRaises(GitHubSandboxPublicationError) as caught:
            self._publisher().publish(request)
        self.assertEqual(caught.exception.code, "auth_401")
        self.assertEqual(self.transport.mutation_calls, [])
        self.assertEqual(
            self.store.publication(publication.app_idempotency_key)["state"], "quarantined"
        )

    def test_ambiguous_blob_without_observed_object_quarantines_without_resend(self) -> None:
        publication = self._publication()
        request = self._approved_request(publication)
        self.transport.queue_failure(
            "blob_create", GitHubSandboxPublicationError(GitHubFailure.TIMEOUT)
        )
        with self.assertRaises(GitHubSandboxPublicationError) as caught:
            self._publisher().publish(request)
        self.assertEqual(caught.exception.code, "ambiguous_result")
        self.assertEqual(self.transport.mutation_calls.count("blob_create"), 1)
        self.assertEqual(
            self.store.publication(publication.app_idempotency_key)["state"], "quarantined"
        )

    def test_object_receipt_mismatch_quarantines(self) -> None:
        publication = self._publication()
        request = self._approved_request(publication)
        self.transport.queue_failure("tree_create", GitHubResponse(201, {}, {"sha": "f" * 40}))
        with self.assertRaises(GitHubSandboxPublicationError) as caught:
            self._publisher().publish(request)
        self.assertEqual(caught.exception.code, "receipt_mismatch")
        self.assertEqual(
            self.store.publication(publication.app_idempotency_key)["state"], "quarantined"
        )

    def test_revoked_token_after_one_write_stops_remaining_mutations(self) -> None:
        publication = self._publication()
        request = self._approved_request(publication)
        calls = 0

        def provider() -> InstallationToken:
            nonlocal calls
            calls += 1
            return self._token(revoked=calls >= 5)

        with self.assertRaises(GitHubSandboxPublicationError) as caught:
            self._publisher(token_provider=provider).publish(request)
        self.assertEqual(caught.exception.code, "token_revoked")
        self.assertEqual(self.transport.mutation_calls, ["blob_create"])
        self.assertEqual(
            self.store.publication(publication.app_idempotency_key)["state"], "quarantined"
        )

    def test_request_and_mutation_budget_is_durable_and_bounded(self) -> None:
        self.authorization = make_authorization(
            max_requests=6,
            max_mutations=3,
            max_reads=3,
        )
        self.transport = RecordingGitHubTransport(self.authorization)
        publication = self._publication()
        request = self._approved_request(publication)
        with self.assertRaises(GitHubSandboxPublicationError) as caught:
            self._publisher().publish(request)
        self.assertEqual(caught.exception.code, "budget_exhausted")
        self.assertEqual(
            self.transport.mutation_calls,
            ["blob_create", "tree_create", "commit_create"],
        )
        record = self.store.publication(publication.app_idempotency_key)
        self.assertEqual(record["request_count"], 6)
        self.assertEqual(record["mutation_count"], 3)
        self.assertEqual(record["read_count"], 3)
        self.assertEqual(record["state"], "quarantined")

    def test_authorization_budget_is_shared_across_canary_cases(self) -> None:
        self.authorization = make_authorization(
            max_requests=8,
            max_mutations=5,
            max_reads=3,
        )
        self.transport = RecordingGitHubTransport(self.authorization)
        first = self._publication("normal")
        self._publisher().publish(self._approved_request(first))
        calls_after_first = list(self.transport.calls)
        second = self._publication("crash_after_branch")
        with self.assertRaises(GitHubSandboxPublicationError) as caught:
            self._publisher().publish(self._approved_request(second))
        self.assertEqual(caught.exception.code, "budget_exhausted")
        self.assertEqual(self.transport.calls, calls_after_first)
        with self.engine.connect() as connection:
            budget = dict(
                connection.execute(
                    text(
                        "SELECT request_count, mutation_count, read_count, branch_count, "
                        "commit_count, draft_pr_count FROM github_canary_authorization_budgets"
                    )
                ).one()._mapping
            )
        self.assertEqual(
            budget,
            {
                "request_count": 8,
                "mutation_count": 5,
                "read_count": 3,
                "branch_count": 1,
                "commit_count": 1,
                "draft_pr_count": 1,
            },
        )

    def test_multiple_matching_pr_candidates_quarantine_without_resend(self) -> None:
        publication = self._publication("crash_after_draft_pr")
        request = self._approved_request(publication)
        triggered = False

        def fault(name: str) -> None:
            nonlocal triggered
            if name == "after_draft_pr_create_before_receipt" and not triggered:
                triggered = True
                duplicate = dict(self.transport.prs[0])
                duplicate["number"] = 2
                self.transport.prs.append(duplicate)
                raise RuntimeError("synthetic_crash")

        with self.assertRaises(RuntimeError):
            self._publisher(fault=fault).publish(request)
        with self.assertRaises(GitHubSandboxPublicationError) as caught:
            self._publisher().publish(request)
        self.assertEqual(caught.exception.code, "receipt_mismatch")
        self.assertEqual(self.transport.mutation_calls.count("draft_pr_create"), 1)
        self.assertEqual(
            self.store.publication(publication.app_idempotency_key)["state"], "quarantined"
        )

    def test_durable_ledger_contains_hashes_not_payload_identity_or_token(self) -> None:
        publication = self._publication()
        request = self._approved_request(publication)
        self._publisher().publish(request)
        with self.engine.connect() as connection:
            rows: list[dict[str, object]] = []
            for table in (
                "github_canary_approvals",
                "github_canary_authorization_budgets",
                "github_canary_publications",
                "github_canary_requests",
            ):
                rows.extend(dict(row._mapping) for row in connection.execute(text(f"SELECT * FROM {table}")))
        serialized = json.dumps(rows, sort_keys=True, default=str)
        for prohibited in (
            publication.title,
            publication.body,
            publication.commit_message,
            publication.head_branch,
            publication.blobs[0].path,
            self.maintainer.principal_id,
            self._token().value,
        ):
            self.assertNotIn(prohibited, serialized)

    def test_http_failure_taxonomy_distinguishes_rate_limit_403(self) -> None:
        cases = {
            (401, ()): "auth_401",
            (403, ()): "permission_403",
            (403, (("X-RateLimit-Remaining", "0"),)): "rate_limited",
            (404, ()): "missing_404",
            (409, ()): "conflict_409",
            (422, ()): "validation_422",
            (429, ()): "rate_limited",
            (503, ()): "server_5xx",
            (302, ()): "redirect_denied",
        }
        for (status, header_items), expected in cases.items():
            with self.subTest(status=status, headers=header_items):
                result = classify_github_failure(status, dict(header_items))
                self.assertIsNotNone(result)
                self.assertEqual(result.value, expected)

    def test_endpoint_redirect_and_budget_denial_make_no_extra_mutation(self) -> None:
        class NeverOpen:
            def open(self, req: object, timeout: float) -> object:
                del req, timeout
                raise AssertionError("opener must not run")

        strict = StrictGitHubHttpsTransport(opener=NeverOpen())
        with self.assertRaises(GitHubSandboxPublicationError) as denied:
            strict.send(
                "merge",
                {},
                body=None,
                token="fake-not-real",
                timeout_seconds=1,
            )
        self.assertEqual(denied.exception.code, "endpoint_denied")
        for prohibited in ("merge", "ready", "comment", "review", "label", "check", "cleanup"):
            self.assertFalse(hasattr(self._publisher(), prohibited))

    def test_strict_transport_redacts_error_body_and_rejects_redirect(self) -> None:
        class RedirectOpener:
            def open(self, req: object, timeout: float) -> object:
                del req, timeout
                raise error.HTTPError(
                    "https://api.github.com/repos/x/y",
                    302,
                    "secret-provider-message",
                    {"Location": "https://evil.invalid/token?secret=1"},
                    None,
                )

        response = StrictGitHubHttpsTransport(opener=RedirectOpener()).send(
            "repository_read",
            {"owner": "x", "repo": "y"},
            body=None,
            token="fake-not-real",
            timeout_seconds=1,
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers, {})
        self.assertIsNone(response.body)
        self.assertEqual(classify_github_failure(response.status, response.headers), GitHubFailure.REDIRECT_DENIED)

    def test_migration_is_additive_and_downgrades_to_phase11a_schema(self) -> None:
        self.assertEqual(current_revision(self.database_url), "0008_phase11b_github_canary")
        tables = set(inspect(self.engine).get_table_names())
        self.assertIn("repair_jobs", tables)
        self.assertIn("github_canary_approvals", tables)
        self.assertIn("github_canary_authorization_budgets", tables)
        self.assertIn("github_canary_publications", tables)
        self.engine.dispose()
        command.downgrade(_alembic_config(self.database_url), "0007_phase11a_repair")
        downgraded = create_database_engine(self.database_url)
        try:
            tables = set(inspect(downgraded).get_table_names())
            self.assertIn("repair_jobs", tables)
            self.assertNotIn("github_canary_publications", tables)
        finally:
            downgraded.dispose()
        upgrade_database(self.database_url)
        self.assertEqual(current_revision(self.database_url), "0008_phase11b_github_canary")


if __name__ == "__main__":
    unittest.main()
