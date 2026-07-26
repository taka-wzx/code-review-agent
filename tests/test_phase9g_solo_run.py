from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import phase9g_solo as solo
import phase9g_solo_run as run


class Phase9GSoloRunTests(unittest.TestCase):
    @staticmethod
    def candidate(index: int) -> run.Candidate:
        return run.Candidate(
            commit_sha=f"{index + 1:040x}",
            merged_at=f"2026-07-{index + 1:02d}T00:00:00Z",
            subject=f"synthetic subject {index}",
            pr_number=str(index + 1),
            opaque_pr_id=f"synthetic-pr-{index + 1}",
            rank_sha256=f"{index + 1:064x}",
        )

    @staticmethod
    def accepted_authorization() -> dict[str, object]:
        return {
            "authorization_id": run.EXPECTED_AUTHORIZATION_ID,
            "authorization_sha256": run.EXPECTED_AUTHORIZATION_SHA256,
            "pr_count": run.TARGET_PRS,
            "mode": "shadow",
            "model": {
                "provider": run.EXPECTED_PROVIDER,
                "exact_model_snapshot": run.EXPECTED_MODEL,
                "runtime_config_sha256": run.EXPECTED_RUNTIME_SHA256,
                "temperature": 0,
            },
        }

    @staticmethod
    def runtime_config() -> dict[str, object]:
        return {
            "schema_version": 1,
            "provider": run.EXPECTED_PROVIDER,
            "exact_model_snapshot": run.EXPECTED_MODEL,
            "temperature": 0,
            "mode": "shadow",
        }

    @staticmethod
    def public_receipt(*, blocked_diffs: int = 0) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": 1,
            "phase_id": run.RUN_PHASE_ID,
            "evidence_type": solo.EVIDENCE_TYPE,
            "source_commit": run.SOURCE_COMMIT,
            "selection_seed": run.SELECTION_SEED,
            "window_start": run.WINDOW_START,
            "window_end": run.WINDOW_END,
            "candidate_prs": 8,
            "selected_prs": run.TARGET_PRS,
            "authorization_sha256": run.EXPECTED_AUTHORIZATION_SHA256,
            "participant_manifest_sha256": "1" * 64,
            "repository_manifest_sha256": "2" * 64,
            "selection_plan_sha256": "3" * 64,
            "selection_log_sha256": "4" * 64,
            "cohort_sha256": "5" * 64,
            "private_artifact_index_sha256": "6" * 64,
            "selected_diff_secret_scan_blocked": blocked_diffs,
            "paid_call_gate": False,
            "paid_call_blockers": run._paid_call_blockers(blocked_diffs),
            "business_claim_allowed": False,
            "quality_claim_allowed": False,
            "formal_quality_status": "incomplete",
            "generated_at": "2026-07-26T08:00:00Z",
            "receipt_sha256": "",
        }
        return solo.with_artifact_hash(value, "receipt_sha256")

    def test_frozen_selection_seed_matches_source_commit(self) -> None:
        self.assertEqual(
            run.SELECTION_SEED,
            solo.derive_selection_seed(run.SOURCE_COMMIT),
        )

    def test_opaque_pr_id_and_rank_are_deterministic(self) -> None:
        first = run.opaque_pr_id("synthetic-repository", "17")
        second = run.opaque_pr_id("synthetic-repository", "17")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("pr-"))
        self.assertNotEqual(first, "pr-17")
        self.assertEqual(
            solo.selection_rank(run.SELECTION_SEED, first),
            solo.selection_rank(run.SELECTION_SEED, second),
        )

    def test_candidate_collection_uses_only_first_parent_metadata(self) -> None:
        records = []
        for index in range(5):
            records.append(
                f"{index + 1:040x}\x1f2026-07-{index + 1:02d}T00:00:00+00:00"
                f"\x1fMerge pull request #{index + 1} from synthetic/branch\x1e"
            )
        records.append(
            f"{'f' * 40}\x1f2026-07-06T00:00:00+00:00\x1fordinary commit\x1e"
        )

        def fake_git_text(_root: Path, arguments: list[str]) -> str:
            if arguments[:2] == ["rev-parse", "origin/master"]:
                return run.SOURCE_COMMIT
            self.assertEqual(arguments[0:3], ["log", "origin/master", "--first-parent"])
            return "".join(records)

        with mock.patch.object(run, "_git_text", side_effect=fake_git_text):
            candidates, first_parent_commits = run.collect_candidates(
                Path("synthetic-root"), "synthetic-repository"
            )
        self.assertEqual(first_parent_commits, 6)
        self.assertEqual(len(candidates), 5)
        self.assertEqual({candidate.pr_number for candidate in candidates}, set("12345"))

    def test_selected_only_diff_access(self) -> None:
        candidates = [self.candidate(index) for index in range(7)]
        git_calls: list[list[str]] = []

        def fake_git_bytes(_root: Path, arguments: list[str]) -> bytes:
            git_calls.append(arguments)
            if arguments[0] == "cat-file":
                return b"synthetic commit object"
            return f"synthetic diff for {arguments[2]}".encode()

        with tempfile.TemporaryDirectory() as directory:
            private_root = Path(directory) / "evidence"
            with mock.patch.object(run, "_git_bytes", side_effect=fake_git_bytes):
                rows, private_map, blocked = run._selection_rows_and_private_map(
                    Path("synthetic-root"),
                    candidates,
                    repository_id="synthetic-repository",
                    private_root=private_root,
                )
            self.assertEqual(sum(row["selected"] for row in rows), run.TARGET_PRS)
            self.assertEqual(sum(row["selected"] for row in private_map), run.TARGET_PRS)
            self.assertEqual(blocked, 0)
            self.assertEqual(len(git_calls), run.TARGET_PRS * 2)
            touched = {
                call[2] if call[0] == "diff" else call[2]
                for call in git_calls
            }
            expected = {candidate.commit_sha for candidate in candidates[: run.TARGET_PRS]}
            self.assertEqual(touched, expected)
            self.assertEqual(
                len(list((private_root / "selected-diffs").glob("*.diff"))),
                run.TARGET_PRS,
            )

    def test_private_evidence_must_be_external_and_non_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            public = repo / "receipt.json"
            with self.assertRaisesRegex(run.RunValidationError, "outside"):
                run._validate_storage_roots(repo, repo / "private", public)
            private = root / "private"
            private.mkdir()
            with self.assertRaisesRegex(run.RunValidationError, "already exists"):
                run._validate_storage_roots(repo, private, public)
            private.rmdir()
            public.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(run.RunValidationError, "already exists"):
                run._validate_storage_roots(repo, private, public)

    def test_private_authorization_initialization_discloses_no_stable_ids(self) -> None:
        environment = {
            "PHASE9G_SOLO_PARTICIPANT_ID": "synthetic-participant",
            "PHASE9G_SOLO_REPOSITORY_ID": "synthetic-repository",
            "PHASE9G_SOLO_APPROVER_ID": "synthetic-approver",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            output = root / "private-authorization"
            with (
                mock.patch.object(run, "validate_authorization"),
                mock.patch.object(run, "validate_runtime_config"),
            ):
                result = run.initialize_auth_002(
                    repo_root=repo,
                    output_root=output,
                    environment=environment,
                )
            self.assertTrue((output / "authorization.json").is_file())
            self.assertTrue((output / "runtime-config.json").is_file())
            rendered = repr(result)
            for stable_id in environment.values():
                self.assertNotIn(stable_id, rendered)
            with self.assertRaisesRegex(run.RunValidationError, "already exists"):
                run.initialize_auth_002(
                    repo_root=repo,
                    output_root=output,
                    environment=environment,
                )

    def test_authorization_and_runtime_are_exactly_hash_bound(self) -> None:
        authorization = self.accepted_authorization()
        with mock.patch.object(solo, "validate_authorization", return_value=authorization):
            self.assertIs(run.validate_authorization({}), authorization)
            run.validate_runtime_config(self.runtime_config(), authorization)
            altered = {**self.runtime_config(), "temperature": 0.7}
            with self.assertRaisesRegex(run.RunValidationError, "hash differs"):
                run.validate_runtime_config(altered, authorization)

    def test_revoked_authorization_identity_is_rejected(self) -> None:
        authorization = self.accepted_authorization()
        authorization["authorization_id"] = "phase9g-solo-run-v1-auth-001"
        with mock.patch.object(solo, "validate_authorization", return_value=authorization):
            with self.assertRaisesRegex(run.RunValidationError, "not the approved"):
                run.validate_authorization({})

    def test_credential_preflight_does_not_disclose_secret_or_path(self) -> None:
        authorization = self.accepted_authorization()
        runtime_config = self.runtime_config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            secret = root / "credential.txt"
            secret_value = "synthetic-local-credential-value"
            secret.write_text(secret_value, encoding="utf-8")
            environment = {
                "LLM_PROVIDER": run.EXPECTED_PROVIDER,
                "LLM_MODEL": run.EXPECTED_MODEL,
                "GLM_API_KEY_FILE": str(secret),
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(run, "validate_authorization", return_value=authorization),
                mock.patch.object(run, "validate_runtime_config", return_value=runtime_config),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = run.credential_preflight(
                    {}, {}, environment=environment, repo_root=repo
                )
            rendered = repr(result) + stdout.getvalue() + stderr.getvalue()
            self.assertTrue(result["credential_source_ready"])
            self.assertFalse(result["paid_call_gate"])
            self.assertNotIn(secret_value, rendered)
            self.assertNotIn(str(secret), rendered)

    def test_credential_preflight_rejects_ambiguous_sources(self) -> None:
        authorization = self.accepted_authorization()
        runtime_config = self.runtime_config()
        environment = {
            "LLM_PROVIDER": run.EXPECTED_PROVIDER,
            "LLM_MODEL": run.EXPECTED_MODEL,
            "GLM_API_KEY_FILE": "first",
            "ZHIPUAI_API_KEY_FILE": "second",
        }
        with (
            mock.patch.object(run, "validate_authorization", return_value=authorization),
            mock.patch.object(run, "validate_runtime_config", return_value=runtime_config),
            self.assertRaisesRegex(run.RunValidationError, "ambiguous"),
        ):
            run.credential_preflight({}, {}, environment=environment)

    def test_budget_reservation_is_atomic_under_concurrency(self) -> None:
        ledger = run.BudgetLedger(run.BudgetLimits(10, 10, 100, 100, 100))
        reservation = run.BudgetReservation(1, 1, 10, 10, 10)

        def reserve_once(_index: int) -> bool:
            try:
                ledger.reserve(reservation)
            except run.RunValidationError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=20) as executor:
            outcomes = list(executor.map(reserve_once, range(20)))
        self.assertEqual(sum(outcomes), 10)
        self.assertEqual(ledger.snapshot(), run.BudgetReservation(10, 10, 100, 100, 100))

    def test_tariff_cost_uses_integer_microcny_and_rounds_up(self) -> None:
        tariff = solo.with_artifact_hash(
            {
                "schema_version": 1,
                "provider": run.EXPECTED_PROVIDER,
                "model": run.EXPECTED_MODEL,
                "endpoint_kind": "standard",
                "effective_at": "2026-07-26T00:00:00Z",
                "input_microcny_per_million_tokens": 10_000_000,
                "output_microcny_per_million_tokens": 20_000_000,
                "cached_input_microcny_per_million_tokens": 2_000_000,
                "source_sha256": "7" * 64,
                "tariff_sha256": "",
            },
            "tariff_sha256",
        )
        run.validate_tariff(tariff)
        self.assertEqual(
            run.reserve_cost_microcny(
                tariff,
                input_tokens=1_000_000,
                cached_tokens=200_000,
                output_tokens=500_000,
            ),
            18_400_000,
        )
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            run.reserve_cost_microcny(
                tariff, input_tokens=1, cached_tokens=2, output_tokens=0
            )

    def test_public_receipt_permanently_denies_business_and_quality_claims(self) -> None:
        receipt = self.public_receipt()
        result = run.validate_public_receipt(receipt)
        self.assertFalse(result["paid_call_gate"])
        self.assertFalse(result["business_claim_allowed"])
        self.assertFalse(result["quality_claim_allowed"])
        self.assertEqual(result["formal_quality_status"], "incomplete")

    def test_public_receipt_requires_secret_scan_blocker_when_needed(self) -> None:
        receipt = self.public_receipt(blocked_diffs=1)
        receipt["paid_call_blockers"] = run._paid_call_blockers(0)
        receipt = solo.with_artifact_hash(receipt, "receipt_sha256")
        with self.assertRaisesRegex(run.RunValidationError, "blockers"):
            run.validate_public_receipt(receipt)

    def test_synthetic_gate_cannot_open_a_real_or_quality_claim(self) -> None:
        result = run.validate_synthetic()
        self.assertTrue(result["valid"])
        self.assertTrue(result["synthetic"])
        self.assertFalse(result["paid_call_gate"])
        self.assertFalse(result["business_claim_allowed"])
        self.assertFalse(result["quality_claim_allowed"])
        self.assertEqual(result["formal_quality_status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
