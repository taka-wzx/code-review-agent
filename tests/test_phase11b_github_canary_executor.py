from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from sqlalchemy import text

from code_review_agent.database import Database, sqlite_database_url, upgrade_database
from code_review_agent.github_canary_executor import (
    COMMIT_ACTOR_EMAIL,
    COMMIT_ACTOR_NAME,
    EGRESS_ALLOWLIST,
    GITHUB_API_URL,
    RUNTIME_ENVIRONMENT,
    RUNTIME_SCHEMA_VERSION,
    TOKEN_INJECTION_MODE,
    CanaryExecutorError,
    ExpectedCanaryRestart,
    GitHubCanaryExecutor,
    GitHubCanaryRuntimeConfig,
    GitTreeEntry,
    SecureBearerTokenFileProvider,
    SecureInstallationTokenFileProvider,
    freeze_runtime_case,
    git_commit_sha,
    git_object_sha,
    git_tree_sha,
    load_runtime_config,
    validate_runtime_authorization,
    validate_secret_metadata,
)
from code_review_agent.github_sandbox_publish import (
    AUTHORIZATION_SCHEMA_VERSION,
    AuthorizationCase,
    GitHubResponse,
    GitHubSandboxAuthorization,
    GitHubSandboxPublicationError,
    InstallationToken,
    canonical_json,
    sha256_hex,
)
from code_review_agent.identity import Role


class MaterializingGitHubTransport:
    """Effect-recording GitHub fake that computes actual Git object IDs."""

    real_github_writes = False

    def __init__(
        self,
        runtime: GitHubCanaryRuntimeConfig,
        authorization: GitHubSandboxAuthorization,
    ) -> None:
        self.runtime = runtime
        self.authorization = authorization
        self.calls: list[str] = []
        self.blobs: set[str] = {entry.sha for entry in runtime.base_tree_entries if entry.object_type == "blob"}
        self.trees: dict[str, tuple[GitTreeEntry, ...]] = {
            runtime.base_tree_sha: runtime.base_tree_entries
        }
        self.commits: dict[str, str] = {runtime.base_sha: runtime.base_tree_sha}
        self.refs: dict[str, str] = {runtime.base_branch: runtime.base_sha}
        self.prs: list[dict[str, object]] = []

    @property
    def mutation_calls(self) -> list[str]:
        return [
            endpoint
            for endpoint in self.calls
            if endpoint
            in {"blob_create", "tree_create", "commit_create", "ref_create", "draft_pr_create"}
        ]

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
        if endpoint == "repository_read":
            return GitHubResponse(
                200,
                {},
                {
                    "id": self.runtime.repository_id,
                    "name": self.runtime.repository_name,
                    "owner": {"login": self.runtime.repository_owner},
                    "default_branch": self.runtime.base_branch,
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
            content = base64.b64decode(str(body["content"]), validate=True)
            digest = git_object_sha("blob", content)
            self.blobs.add(digest)
            return GitHubResponse(201, {}, {"sha": digest})
        if endpoint == "blob_read":
            digest = str(parameters["sha"])
            return GitHubResponse(200, {}, {"sha": digest}) if digest in self.blobs else GitHubResponse(404, {}, None)
        if endpoint == "tree_create":
            assert body is not None
            base = str(body["base_tree"])
            entries = list(self.trees[base])
            for raw in body["tree"]:  # type: ignore[union-attr]
                assert isinstance(raw, dict)
                entry = GitTreeEntry(
                    path=str(raw["path"]),
                    mode=str(raw["mode"]),
                    object_type=str(raw["type"]),
                    sha=str(raw["sha"]),
                )
                entries = [item for item in entries if item.path != entry.path]
                entries.append(entry)
            digest = git_tree_sha(entries)
            self.trees[digest] = tuple(entries)
            return GitHubResponse(201, {}, {"sha": digest})
        if endpoint == "tree_read":
            digest = str(parameters["sha"])
            return GitHubResponse(200, {}, {"sha": digest}) if digest in self.trees else GitHubResponse(404, {}, None)
        if endpoint == "commit_create":
            assert body is not None
            digest = git_commit_sha(
                tree_sha=str(body["tree"]),
                parent_sha=str(body["parents"][0]),  # type: ignore[index]
                message=str(body["message"]),
                timestamp=str(body["author"]["date"]),  # type: ignore[index]
            )
            self.commits[digest] = str(body["tree"])
            return GitHubResponse(201, {}, {"sha": digest, "tree": {"sha": body["tree"]}})
        if endpoint == "commit_read":
            digest = str(parameters["sha"])
            if digest not in self.commits:
                return GitHubResponse(404, {}, None)
            return GitHubResponse(200, {}, {"sha": digest, "tree": {"sha": self.commits[digest]}})
        if endpoint == "ref_create":
            assert body is not None
            branch = str(body["ref"])[len("refs/heads/") :]
            if branch in self.refs:
                return GitHubResponse(422, {}, None)
            self.refs[branch] = str(body["sha"])
            return GitHubResponse(201, {}, {"ref": body["ref"], "object": {"sha": body["sha"]}})
        if endpoint == "draft_pr_create":
            assert body is not None
            candidate: dict[str, object] = {
                "number": len(self.prs) + 1,
                "draft": body["draft"],
                "title": body["title"],
                "body": body["body"],
                "head": {
                    "ref": body["head"],
                    "sha": self.refs[str(body["head"])],
                },
                "base": {"ref": body["base"]},
            }
            self.prs.append(candidate)
            return GitHubResponse(201, {}, candidate)
        if endpoint == "draft_pr_list":
            head = str(parameters["head"])
            base = str(parameters["base"])
            matches = [
                item
                for item in self.prs
                if item["head"]["ref"] == head and item["base"]["ref"] == base  # type: ignore[index]
            ]
            return GitHubResponse(200, {}, matches)
        if endpoint == "draft_pr_read":
            return GitHubResponse(200, {}, self.prs[int(parameters["number"]) - 1])
        raise AssertionError(endpoint)


class Phase11BGitHubCanaryExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database_url = sqlite_database_url(Path(self.temp.name) / "executor.db")
        upgrade_database(self.database_url)
        self.database = Database(self.database_url)
        self.addCleanup(self.database.close)
        organization = self.database.create_organization("phase11b-test", "Phase 11B test")
        self.organization_id = str(organization["id"])
        membership = self.database.create_membership(
            self.organization_id,
            subject="human-maintainer",
            display_name="Human maintainer",
            role=Role.MAINTAINER,
        )
        principal = self.database.principal_for_user(
            self.organization_id,
            str(membership["user_id"]),
        )
        assert principal is not None
        self.maintainer_credential = self.database.create_credential(
            principal,
            expires_in_seconds=3600,
        )["token"]
        self.now = int(time.time())
        self.runtime = self._runtime()
        self.authorization = self._authorization()
        self._seed_jobs()
        self.executor = GitHubCanaryExecutor(
            self.database,
            self.runtime,
            self.authorization,
            clock=lambda: float(self.now),
        )
        self.transport = MaterializingGitHubTransport(self.runtime, self.authorization)

    def _runtime(self) -> GitHubCanaryRuntimeConfig:
        readme = b"# Disposable sandbox\n"
        readme_sha = git_object_sha("blob", readme)
        entries = (GitTreeEntry("README.md", "100644", "blob", readme_sha),)
        base_tree_sha = git_tree_sha(entries)
        base_sha = "1" * 40
        case_ids = ("normal", "crash_after_branch", "crash_after_draft_pr")
        cases = tuple(
            freeze_runtime_case(
                case_id=case_id,
                repair_job_id=f"repair-{case_id}",
                repair_repository_id="repair-repository-sandbox",
                repair_base_sha="3" * 40,
                repair_diff_sha256=sha256_hex(f"repair-diff:{case_id}".encode()),
                head_branch=f"crag-canary/auth-test/{case_id.replace('_', '-')}",
                app_idempotency_key=f"phase11b-auth-test-{case_id}",
                synthetic_file_path=f"crag-canary-{case_id.replace('_', '-')}.txt",
                synthetic_content=f"Synthetic Phase 11B case: {case_id}\n".encode("utf-8"),
                file_mode="100644",
                commit_message=f"Phase 11B synthetic {case_id}",
                commit_timestamp="2026-07-29T01:00:00Z",
                test_evidence_sha256=sha256_hex(f"tests:{case_id}".encode()),
                durable_budget_sha256=sha256_hex(f"budget:{case_id}".encode()),
                checkpoint_sha256=sha256_hex(f"checkpoint:{case_id}".encode()),
                title=f"Phase 11B synthetic {case_id}",
                body_prefix="Synthetic sandbox content only.",
                repository_id=1315679182,
                base_branch="main",
                base_sha=base_sha,
                base_tree_sha=base_tree_sha,
                base_tree_entries=entries,
            )
            for case_id in case_ids
        )
        return GitHubCanaryRuntimeConfig(
            schema_version=RUNTIME_SCHEMA_VERSION,
            environment="github_sandbox_canary",
            synthetic_input_only=True,
            real_github_writes_enabled=True,
            real_model_calls=False,
            real_business_repository_writes=False,
            business_claim_allowed=False,
            quality_claim_allowed=False,
            production_ready=False,
            executable_code_sha="9" * 40,
            image_id="sha256:" + "8" * 64,
            source_archive_sha256="7" * 64,
            deployment_config_sha256="6" * 64,
            runtime_host_sha256="5" * 64,
            runtime_environment=RUNTIME_ENVIRONMENT,
            github_api_url=GITHUB_API_URL,
            egress_allowlist=EGRESS_ALLOWLIST,
            tls_verify=True,
            follow_redirects=False,
            credential_injection_mode=TOKEN_INJECTION_MODE,
            request_timeout_seconds=10,
            canary_window_seconds=1200,
            approval_ttl_seconds=600,
            max_retries=0,
            backoff_seconds=0,
            max_requests=40,
            max_mutations=15,
            max_reads=25,
            max_branches=3,
            max_commits=3,
            max_draft_prs=3,
            cost_ceiling_micro_cny=0,
            repository_owner="taka-wzx",
            repository_name="crag-phase11b-sandbox",
            repository_id=1315679182,
            github_app_id=4421400,
            installation_id=149747930,
            installation_account_id=186135139,
            base_branch="main",
            base_sha=base_sha,
            base_tree_sha=base_tree_sha,
            base_tree_manifest_complete=True,
            base_tree_entries=entries,
            cases=cases,
        )

    def _authorization(self, **changes: object) -> GitHubSandboxAuthorization:
        values: dict[str, object] = {
            "schema_version": AUTHORIZATION_SCHEMA_VERSION,
            "authorization_id": "phase11b-auth-test",
            "organization_id": self.organization_id,
            "repository_owner": self.runtime.repository_owner,
            "repository_name": self.runtime.repository_name,
            "repository_id": self.runtime.repository_id,
            "github_app_id": self.runtime.github_app_id,
            "installation_id": self.runtime.installation_id,
            "installation_account_id": self.runtime.installation_account_id,
            "allowed_base_branch": self.runtime.base_branch,
            "frozen_base_sha": self.runtime.base_sha,
            "cases": tuple(
                AuthorizationCase(case.case_id, case.head_branch) for case in self.runtime.cases
            ),
            "max_denominator": 3,
            "executable_code_sha": self.runtime.executable_code_sha,
            "runtime_config_sha256": self.runtime.canonical_sha256,
            "issued_at": datetime.fromtimestamp(self.now - 60, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "not_before": datetime.fromtimestamp(self.now - 30, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "expires_at": datetime.fromtimestamp(self.now + 1170, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "max_requests": self.runtime.max_requests,
            "max_mutations": self.runtime.max_mutations,
            "max_reads": self.runtime.max_reads,
            "max_branches": self.runtime.max_branches,
            "max_commits": self.runtime.max_commits,
            "max_draft_prs": self.runtime.max_draft_prs,
            "cost_ceiling_micro_cny": 0,
            "authorization_owner": "taka-wzx",
            "revocation_owner": "taka-wzx",
            "kill_switch_owner": "taka-wzx",
        }
        values.update(changes)
        return GitHubSandboxAuthorization(**values)

    def _seed_jobs(self) -> None:
        with self.database.engine.begin() as connection:
            for case in self.runtime.cases:
                connection.execute(
                    text(
                        "INSERT INTO repair_jobs "
                        "(id, organization_id, repository_id, finding_sha256, base_sha, head_sha, "
                        "state, version, attempt, checkpoint_json, checkpoint_sha256, "
                        "current_diff_sha256, budget_sha256, lease_owner, lease_token, "
                        "lease_expires_at, failure_code, created_at, updated_at) VALUES "
                        "(:id, :organization, :repository, :finding, :base, :head, "
                        "'queued_publish', 1, 1, :checkpoint_json, :checkpoint, :diff, :budget, "
                        "NULL, NULL, 0, NULL, 1, 1)"
                    ),
                    {
                        "id": case.repair_job_id,
                        "organization": self.organization_id,
                        "repository": case.repair_repository_id,
                        "finding": sha256_hex(f"finding:{case.case_id}".encode()),
                        "base": case.repair_base_sha,
                        "head": "2" * 40,
                        "checkpoint_json": json.dumps(
                            {"tests_sha256": case.test_evidence_sha256}
                        ),
                        "checkpoint": case.checkpoint_sha256,
                        "diff": case.repair_diff_sha256,
                        "budget": case.durable_budget_sha256,
                    },
                )

    def _approve_all(self) -> dict[str, object]:
        worksheet = dict(self.executor.prepare())
        for case in worksheet["cases"]:  # type: ignore[union-attr]
            assert isinstance(case, dict)
            for kind in ("write", "draft_pr"):
                approval = case[kind]
                assert isinstance(approval, dict)
                result = self.executor.decide_approval(
                    case_id=str(case["case_id"]),
                    kind=kind,
                    approval_id=str(approval["approval_id"]),
                    approved=True,
                    bearer_provider=lambda: str(self.maintainer_credential),
                )
                self.assertEqual(result["status"], "consumed")
        return worksheet

    def _token(self) -> InstallationToken:
        return InstallationToken(
            value="fake-installation-token-offline-only",
            app_id=self.runtime.github_app_id,
            installation_id=self.runtime.installation_id,
            installation_account_id=self.runtime.installation_account_id,
            expires_at=datetime.fromtimestamp(self.now + 600, timezone.utc),
        )

    def test_runtime_round_trip_schema_and_drift_fail_closed(self) -> None:
        value = self.runtime.to_dict()
        self.assertEqual(
            GitHubCanaryRuntimeConfig.from_dict(value).canonical_sha256,
            self.runtime.canonical_sha256,
        )
        with self.assertRaises(ValueError):
            GitHubCanaryRuntimeConfig.from_dict({**value, "unknown": True})
        changed = replace(self.runtime, image_id="sha256:" + "a" * 64)
        with self.assertRaisesRegex(CanaryExecutorError, "authorization_mismatch"):
            validate_runtime_authorization(changed, self.authorization)
        with self.assertRaisesRegex(ValueError, "budgets must match"):
            replace(self.runtime, max_requests=39)
        with self.assertRaisesRegex(ValueError, "window exceeds one hour"):
            self._authorization(
                expires_at=datetime.fromtimestamp(self.now + 4000, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            )
        case = replace(self.runtime.cases[0], exact_commit_sha="f" * 40)
        with self.assertRaises(ValueError):
            replace(self.runtime, cases=(case, *self.runtime.cases[1:]))

        path = Path(self.temp.name) / "runtime.json"
        path.write_bytes(canonical_json(value))
        self.assertEqual(load_runtime_config(path).canonical_sha256, self.runtime.canonical_sha256)
        duplicate = canonical_json(value)[:-1] + b',"schema_version":"duplicate"}'
        path.write_bytes(duplicate)
        with self.assertRaisesRegex(CanaryExecutorError, "runtime_config_invalid"):
            load_runtime_config(path)

        schema = json.loads(
            (Path(__file__).parents[1] / "schemas" / "phase11b-github-sandbox-runtime.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(value))
        self.assertEqual(
            set(schema["$defs"]["case"]["required"]),
            set(value["cases"][0]),
        )

    def test_prepare_keeps_repair_lineage_separate_from_github_material(self) -> None:
        case = self.runtime.cases[0]
        self.assertNotEqual(case.repair_base_sha, self.runtime.base_sha)
        self.assertNotEqual(case.repair_diff_sha256, case.diff_sha256)

        with self.database.engine.begin() as connection:
            connection.execute(
                text("UPDATE repair_jobs SET base_sha=:base WHERE id=:id"),
                {"base": self.runtime.base_sha, "id": case.repair_job_id},
            )
        with self.assertRaisesRegex(GitHubSandboxPublicationError, "authorization_mismatch"):
            self.executor.prepare()

        with self.database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE repair_jobs SET base_sha=:base, current_diff_sha256=:diff "
                    "WHERE id=:id"
                ),
                {
                    "base": case.repair_base_sha,
                    "diff": case.diff_sha256,
                    "id": case.repair_job_id,
                },
            )
        with self.assertRaisesRegex(GitHubSandboxPublicationError, "authorization_mismatch"):
            self.executor.prepare()

    def test_module_entrypoint_and_container_secret_path_handoff(self) -> None:
        root = Path(__file__).parents[1]
        environment = {**os.environ, "PYTHONPATH": str(root / "src")}
        completed = subprocess.run(
            [sys.executable, "-m", "code_review_agent.github_canary_executor", "--help"],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
        dockerfile = (root / "Dockerfile.service").read_text(encoding="utf-8")
        self.assertIn("CRAG_CANARY_APPROVER_TOKEN_FILE", dockerfile)
        self.assertIn("CRAG_CANARY_GITHUB_TOKEN_FILE", dockerfile)
        self.assertIn("chmod 0600", dockerfile)
        self.assertIn("chown appuser:appuser", dockerfile)

    def test_git_object_hashes_match_local_git(self) -> None:
        git = shutil.which("git")
        if git is None:
            self.skipTest("git executable unavailable")
        root = Path(self.temp.name) / "git-object-check"
        root.mkdir()
        subprocess.run([git, "init", "--quiet"], cwd=root, check=True)

        def run(arguments: list[str], payload: bytes) -> str:
            completed = subprocess.run(
                [git, *arguments],
                cwd=root,
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            return completed.stdout.decode("ascii").strip()

        readme = b"# Base\n"
        synthetic = b"synthetic\n"
        readme_sha = run(["hash-object", "-w", "--stdin"], readme)
        synthetic_sha = run(["hash-object", "-w", "--stdin"], synthetic)
        self.assertEqual(readme_sha, git_object_sha("blob", readme))
        base_entries = (GitTreeEntry("README.md", "100644", "blob", readme_sha),)
        base_tree = run(["mktree"], f"100644 blob {readme_sha}\tREADME.md\n".encode())
        self.assertEqual(base_tree, git_tree_sha(base_entries))
        subtree = run(["mktree"], f"100644 blob {readme_sha}\tinside.txt\n".encode())
        mixed_tree = run(
            ["mktree"],
            (
                f"040000 tree {subtree}\tdocs\n"
                f"100644 blob {synthetic_sha}\tdocs.txt\n"
            ).encode(),
        )
        self.assertEqual(
            mixed_tree,
            git_tree_sha(
                (
                    GitTreeEntry("docs", "040000", "tree", subtree),
                    GitTreeEntry("docs.txt", "100644", "blob", synthetic_sha),
                )
            ),
        )
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": COMMIT_ACTOR_NAME,
            "GIT_AUTHOR_EMAIL": COMMIT_ACTOR_EMAIL,
            "GIT_COMMITTER_NAME": COMMIT_ACTOR_NAME,
            "GIT_COMMITTER_EMAIL": COMMIT_ACTOR_EMAIL,
            "GIT_AUTHOR_DATE": "2026-07-29T01:00:00Z",
            "GIT_COMMITTER_DATE": "2026-07-29T01:00:00Z",
        }
        base_commit = subprocess.run(
            [git, "commit-tree", base_tree],
            cwd=root,
            input=b"Base\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        ).stdout.decode("ascii").strip()
        all_entries = (
            *base_entries,
            GitTreeEntry("crag-canary-test.txt", "100644", "blob", synthetic_sha),
        )
        tree_input = (
            f"100644 blob {readme_sha}\tREADME.md\n"
            f"100644 blob {synthetic_sha}\tcrag-canary-test.txt\n"
        ).encode()
        new_tree = run(["mktree"], tree_input)
        self.assertEqual(new_tree, git_tree_sha(all_entries))
        epoch = int(datetime(2026, 7, 29, 1, tzinfo=timezone.utc).timestamp())
        commit_body = (
            f"tree {new_tree}\n"
            f"parent {base_commit}\n"
            f"author {COMMIT_ACTOR_NAME} <{COMMIT_ACTOR_EMAIL}> {epoch} +0000\n"
            f"committer {COMMIT_ACTOR_NAME} <{COMMIT_ACTOR_EMAIL}> {epoch} +0000\n"
            "\nSynthetic sandbox canary"
        ).encode("utf-8")
        git_commit = run(["hash-object", "-t", "commit", "--stdin"], commit_body)
        self.assertEqual(
            git_commit,
            git_commit_sha(
                tree_sha=new_tree,
                parent_sha=base_commit,
                message="Synthetic sandbox canary",
                timestamp="2026-07-29T01:00:00Z",
            ),
        )
        self.assertNotEqual(
            git_commit,
            run(["hash-object", "-t", "commit", "--stdin"], commit_body + b"\n"),
        )
        for invalid_message in (
            "Synthetic sandbox\ncanary",
            "Synthetic sandbox canary\n",
            "Synthetic sandbox\rcanary",
            "Synthetic sandbox canary\r",
        ):
            with self.subTest(invalid_message=repr(invalid_message)):
                with self.assertRaises(ValueError):
                    git_commit_sha(
                        tree_sha=new_tree,
                        parent_sha=base_commit,
                        message=invalid_message,
                        timestamp="2026-07-29T01:00:00Z",
                    )

    def test_secure_file_metadata_rejects_mode_owner_and_symlink(self) -> None:
        validate_secret_metadata(
            mode=stat_mode(0o600),
            owner_uid=1000,
            current_uid=1000,
            is_symlink=False,
            is_regular=True,
        )
        for values in (
            {"mode": stat_mode(0o644)},
            {"owner_uid": 2000},
            {"is_symlink": True},
            {"is_regular": False},
        ):
            arguments = {
                "mode": stat_mode(0o600),
                "owner_uid": 1000,
                "current_uid": 1000,
                "is_symlink": False,
                "is_regular": True,
                **values,
            }
            with self.assertRaisesRegex(CanaryExecutorError, "secret_file_denied"):
                validate_secret_metadata(**arguments)

    def test_secure_bearer_provider_accepts_only_one_bounded_token(self) -> None:
        provider = SecureBearerTokenFileProvider(Path("/not-observed"))
        with patch(
            "code_review_agent.github_canary_executor.read_secure_secret_file",
            return_value=b"a" * 40 + b"\n",
        ):
            self.assertEqual(provider(), "a" * 40)
        for raw in (b"short", b"a" * 32 + b" b"):
            with patch(
                "code_review_agent.github_canary_executor.read_secure_secret_file",
                return_value=raw,
            ):
                with self.assertRaisesRegex(CanaryExecutorError, "secret_file_invalid"):
                    provider()

    def test_installation_token_file_exact_fields_lifetime_and_identity(self) -> None:
        expires = datetime.fromtimestamp(self.now + 600, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        value: dict[str, object] = {
            "token": "github-installation-token-offline-fixture",
            "github_app_id": self.runtime.github_app_id,
            "installation_id": self.runtime.installation_id,
            "installation_account_id": self.runtime.installation_account_id,
            "expires_at": expires,
            "revoked": False,
        }
        provider = SecureInstallationTokenFileProvider(
            Path("/not-observed"),
            runtime=self.runtime,
            authorization=self.authorization,
            clock=lambda: float(self.now),
        )

        def load(candidate: dict[str, object]) -> InstallationToken:
            with patch(
                "code_review_agent.github_canary_executor.read_secure_secret_file",
                return_value=canonical_json(candidate),
            ):
                return provider()

        self.assertEqual(load(value).installation_id, self.runtime.installation_id)
        token_outlives_authorization = {
            **value,
            "expires_at": datetime.fromtimestamp(self.now + 3000, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        self.assertEqual(
            load(token_outlives_authorization).installation_id,
            self.runtime.installation_id,
        )
        for candidate, code in (
            ({**value, "unknown": True}, "secret_file_invalid"),
            ({**value, "revoked": True}, "token_revoked"),
            ({**value, "installation_id": 1}, "token_identity_mismatch"),
            (
                {
                    **value,
                    "expires_at": (datetime.fromtimestamp(self.now, timezone.utc) + timedelta(hours=2)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                },
                "token_expired",
            ),
        ):
            with self.assertRaisesRegex(CanaryExecutorError, code):
                load(candidate)

    def test_prepare_uses_hash_only_worksheet_and_six_one_use_human_decisions(self) -> None:
        worksheet = self.executor.prepare()
        self.assertEqual(len(worksheet["cases"]), 3)  # type: ignore[arg-type]
        rendered = canonical_json(worksheet).decode("utf-8")
        for prohibited in (
            self.runtime.repository_owner,
            self.runtime.repository_name,
            self.runtime.cases[0].head_branch,
            self.runtime.cases[0].synthetic_file_path,
            self.runtime.cases[0].body_prefix,
            str(self.maintainer_credential),
        ):
            self.assertNotIn(prohibited, rendered)
        self._approve_all()
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT kind, status, COUNT(*) AS count FROM github_canary_approvals GROUP BY kind, status")
            ).all()
        self.assertEqual(
            {(row._mapping["kind"], row._mapping["status"], row._mapping["count"]) for row in rows},
            {("write", "consumed", 3), ("draft_pr", "consumed", 3)},
        )
        first = worksheet["cases"][0]  # type: ignore[index]
        with self.assertRaisesRegex(CanaryExecutorError, "approval_conflict"):
            self.executor.decide_approval(
                case_id="normal",
                kind="write",
                approval_id=str(first["write"]["approval_id"]),  # type: ignore[index]
                approved=True,
                bearer_provider=lambda: str(self.maintainer_credential),
            )

    def test_database_role_not_caller_input_and_invalid_token_cannot_approve(self) -> None:
        worksheet = self.executor.prepare()
        first = worksheet["cases"][0]  # type: ignore[index]
        approval_id = str(first["write"]["approval_id"])  # type: ignore[index]
        viewer_membership = self.database.create_membership(
            self.organization_id,
            subject="human-viewer",
            display_name="Human viewer",
            role=Role.VIEWER,
        )
        viewer = self.database.principal_for_user(
            self.organization_id,
            str(viewer_membership["user_id"]),
        )
        assert viewer is not None
        viewer_token = self.database.create_credential(viewer, expires_in_seconds=3600)["token"]
        with self.assertRaisesRegex(CanaryExecutorError, "approval_conflict"):
            self.executor.decide_approval(
                case_id="normal",
                kind="write",
                approval_id=approval_id,
                approved=True,
                bearer_provider=lambda: str(viewer_token),
            )
        with self.assertRaisesRegex(CanaryExecutorError, "human_authentication_failed"):
            self.executor.decide_approval(
                case_id="normal",
                kind="write",
                approval_id=approval_id,
                approved=True,
                bearer_provider=lambda: "not-a-current-database-token",
            )

    def test_normal_and_restart_cases_use_exact_objects_without_duplicate_mutations(self) -> None:
        self._approve_all()
        normal = self.executor.run_case(
            "normal", token_provider=self._token, transport=self.transport
        )
        self.assertFalse(normal.real_github_sandbox_writes)
        first_counts = list(self.transport.mutation_calls)
        self.assertEqual(
            first_counts,
            ["blob_create", "tree_create", "commit_create", "ref_create", "draft_pr_create"],
        )
        repeated = self.executor.run_case(
            "normal", token_provider=self._token, transport=self.transport
        )
        self.assertEqual(repeated.receipt_sha256, normal.receipt_sha256)
        self.assertEqual(self.transport.mutation_calls, first_counts)

        with self.assertRaises(ExpectedCanaryRestart):
            self.executor.run_case(
                "crash_after_branch", token_provider=self._token, transport=self.transport
            )
        branch_counts = list(self.transport.mutation_calls)
        branch_receipt = self.executor.run_case(
            "crash_after_branch", token_provider=self._token, transport=self.transport
        )
        self.assertEqual(branch_receipt.commit_sha, self.runtime.cases[1].exact_commit_sha)
        self.assertEqual(
            self.transport.mutation_calls.count("ref_create"),
            2,
        )
        self.assertEqual(
            self.transport.mutation_calls[len(branch_counts) :],
            ["draft_pr_create"],
        )

        with self.assertRaises(ExpectedCanaryRestart):
            self.executor.run_case(
                "crash_after_draft_pr", token_provider=self._token, transport=self.transport
            )
        draft_counts = list(self.transport.mutation_calls)
        final = self.executor.run_case(
            "crash_after_draft_pr", token_provider=self._token, transport=self.transport
        )
        self.assertEqual(final.commit_sha, self.runtime.cases[2].exact_commit_sha)
        self.assertEqual(self.transport.mutation_calls, draft_counts)
        self.assertEqual(self.transport.mutation_calls.count("draft_pr_create"), 3)
        self.assertEqual(len(self.transport.prs), 3)

    def test_later_case_is_blocked_until_prior_case_succeeds(self) -> None:
        self._approve_all()
        with self.assertRaisesRegex(CanaryExecutorError, "later_case_blocked"):
            self.executor.run_case(
                "crash_after_branch", token_provider=self._token, transport=self.transport
            )

    def test_token_gate_failure_before_and_after_intent_never_mutates(self) -> None:
        self._approve_all()

        def unavailable() -> InstallationToken:
            raise CanaryExecutorError("secret_file_denied")

        with self.assertRaisesRegex(CanaryExecutorError, "secret_file_denied"):
            self.executor.run_case(
                "normal", token_provider=unavailable, transport=self.transport
            )
        with self.database.engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SELECT COUNT(*) FROM github_canary_publications")).scalar_one(),
                0,
            )

        calls = 0

        def revoked_after_preflight() -> InstallationToken:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise CanaryExecutorError("token_revoked")
            return self._token()

        with self.assertRaisesRegex(GitHubSandboxPublicationError, "token_revoked"):
            self.executor.run_case(
                "normal",
                token_provider=revoked_after_preflight,
                transport=self.transport,
            )
        self.assertEqual(self.transport.mutation_calls, [])
        with self.database.engine.connect() as connection:
            state = connection.execute(
                text("SELECT state FROM github_canary_publications")
            ).scalar_one()
            request_status = connection.execute(
                text("SELECT status FROM github_canary_requests")
            ).scalar_one()
        self.assertEqual(state, "quarantined")
        self.assertEqual(request_status, "failed")


def stat_mode(permissions: int) -> int:
    return 0o100000 | permissions


if __name__ == "__main__":
    unittest.main()
