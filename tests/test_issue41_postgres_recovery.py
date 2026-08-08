from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import postgres_recovery_rehearsal as recovery  # noqa: E402


INVENTORY = {
    "alembic_heads": ["0009_issue29_publish_outbox", "0009_issue27_published_feedback"],
    "table_rows": {
        "alembic_version": 2,
        "organizations": 3,
        "repositories": 5,
        "review_jobs": 21,
        "findings": 8,
        "audit_events": 34,
    },
}


class StepClock:
    def __init__(self, *values: float) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        return next(self.values)


class FakeRecoveryRunner:
    def __init__(
        self,
        *,
        source_inventory: dict | None = None,
        target_inventory: dict | None = None,
        clean_table_count: int = 0,
        replay_lag_seconds: float | None = 4.5,
        source_in_recovery: bool = False,
    ) -> None:
        self.source_inventory = source_inventory or INVENTORY
        self.target_inventory = target_inventory or INVENTORY
        self.clean_table_count = clean_table_count
        self.replay_lag_seconds = replay_lag_seconds
        self.source_in_recovery = source_in_recovery
        self.calls: list[tuple[str, ...]] = []
        self.promoted = False
        self.fail_executable: str | None = None
        self.failure_stdout = "raw output must not escape"
        self.target_is_standby = True
        self.promotion_output = "promoted\n"
        self.post_promotion_in_recovery = False
        self.probe_output = "BEGIN\nprobe_ok\nROLLBACK\n"

    def __call__(self, command: tuple[str, ...]) -> recovery.CommandResult:
        self.calls.append(command)
        executable = command[0]
        if executable == self.fail_executable:
            return recovery.CommandResult(returncode=1, stdout=self.failure_stdout)
        if executable == "pg_dump":
            dump_path = Path(command[command.index("--file") + 1])
            dump_path.write_bytes(b"fake custom-format postgres dump")
            return recovery.CommandResult(returncode=0, stdout="")
        if executable == "pg_restore":
            return recovery.CommandResult(returncode=0, stdout="")
        if executable != "psql":
            raise AssertionError(f"unexpected executable: {executable}")

        service = command[command.index("--dbname") + 1].removeprefix("service=")
        sql = command[-1]
        if "information_schema.tables" in sql:
            return self._json({"public_table_count": self.clean_table_count})
        if "pg_last_xact_replay_timestamp" in sql:
            return self._json(
                {
                    "in_recovery": self.target_is_standby,
                    "replay_lag_seconds": self.replay_lag_seconds,
                }
            )
        if "pg_promote" in sql:
            self.promoted = self.promotion_output.strip() == "promoted"
            return recovery.CommandResult(returncode=0, stdout=self.promotion_output)
        if "CREATE TEMP TABLE crag_recovery_probe" in sql:
            return recovery.CommandResult(returncode=0, stdout=self.probe_output)
        if "json_build_object('in_recovery'" in sql:
            in_recovery = (
                self.source_in_recovery
                if service == "primary_a"
                else self.post_promotion_in_recovery
            )
            return self._json({"in_recovery": in_recovery})
        if "'alembic_heads'" in sql:
            inventory = self.source_inventory if service == "primary_a" else self.target_inventory
            return self._json(inventory)
        raise AssertionError("unexpected psql query")

    @staticmethod
    def _json(value: dict) -> recovery.CommandResult:
        return recovery.CommandResult(returncode=0, stdout=json.dumps(value) + "\n")


class Issue41PostgresRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.policy = recovery.load_policy()
        self.utc_now = lambda: datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _backup_plan(self) -> dict:
        return recovery.build_plan(
            operation="backup_restore",
            rehearsal_id="backup-20260808-a",
            source_service="primary_a",
            target_service="clean_restore_b",
            policy=self.policy,
            created_at_utc="2026-08-08T12:00:00Z",
        )

    def _failover_plan(self) -> dict:
        return recovery.build_plan(
            operation="failover",
            rehearsal_id="failover-20260808-a",
            source_service="primary_a",
            target_service="standby_b",
            source_fence_receipt_sha256="f" * 64,
            policy=self.policy,
            created_at_utc="2026-08-08T12:00:00Z",
        )

    def _rehearsal(
        self,
        runner: FakeRecoveryRunner,
        *,
        elapsed_seconds: float = 5.0,
    ) -> recovery.PostgresRecoveryRehearsal:
        return recovery.PostgresRecoveryRehearsal(
            policy=self.policy,
            runner=runner,
            monotonic=StepClock(100.0, 100.0 + elapsed_seconds),
            utc_now=self.utc_now,
        )

    def test_confirmation_and_plan_tampering_run_no_commands(self) -> None:
        runner = FakeRecoveryRunner()
        rehearsal = self._rehearsal(runner)
        plan = self._backup_plan()

        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "confirmation"):
            rehearsal.execute(
                plan,
                confirmation_sha256="0" * 64,
                artifact_directory=self.root / "artifacts",
                evidence_kind="offline_fake",
            )
        tampered = dict(plan)
        tampered["target_service"] = "different_restore"
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "content"):
            rehearsal.execute(
                tampered,
                confirmation_sha256=plan["plan_sha256"],
                artifact_directory=self.root / "artifacts",
                evidence_kind="offline_fake",
            )
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.root / "artifacts").exists())

        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "fence"):
            recovery.build_plan(
                operation="failover",
                rehearsal_id="missing-fence",
                source_service="primary_a",
                target_service="standby_b",
                policy=self.policy,
                created_at_utc="2026-08-08T12:00:00Z",
            )

    def test_backup_to_clean_restore_produces_hash_only_result(self) -> None:
        runner = FakeRecoveryRunner()
        plan = self._backup_plan()
        result = self._rehearsal(runner).execute(
            plan,
            confirmation_sha256=plan["plan_sha256"],
            artifact_directory=self.root / "artifacts",
            evidence_kind="offline_fake",
        )

        self.assertEqual(result["operation"], "backup_restore")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["command_count"], 5)
        self.assertIsNone(result["rpo_observed_seconds"])
        self.assertIsNone(result["rpo_passed"])
        self.assertEqual(len(result["backup_sha256"]), 64)
        self.assertEqual([call[0] for call in runner.calls], ["psql", "psql", "pg_dump", "pg_restore", "psql"])

        serialized = json.dumps(result, sort_keys=True)
        for forbidden in (
            "primary_a",
            "clean_restore_b",
            str(self.root),
            "fake custom-format postgres dump",
        ):
            self.assertNotIn(forbidden, serialized)
        result_path = self.root / "results" / "backup-result.json"
        result_sha256 = recovery.write_result(result_path, result)
        self.assertEqual(len(result_sha256), 64)
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), result)
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "written"):
            recovery.write_result(result_path, result)

    def test_backup_restore_rejects_dirty_target_and_inventory_mismatch(self) -> None:
        plan = self._backup_plan()
        dirty_runner = FakeRecoveryRunner(clean_table_count=1)
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "clean"):
            self._rehearsal(dirty_runner).execute(
                plan,
                confirmation_sha256=plan["plan_sha256"],
                artifact_directory=self.root / "dirty-artifacts",
                evidence_kind="offline_fake",
            )
        self.assertEqual([call[0] for call in dirty_runner.calls], ["psql", "psql"])
        self.assertFalse((self.root / "dirty-artifacts").exists())

        mismatched = json.loads(json.dumps(INVENTORY))
        mismatched["table_rows"]["review_jobs"] += 1
        mismatch_runner = FakeRecoveryRunner(target_inventory=mismatched)
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "inventory"):
            self._rehearsal(mismatch_runner).execute(
                plan,
                confirmation_sha256=plan["plan_sha256"],
                artifact_directory=self.root / "mismatch-artifacts",
                evidence_kind="offline_fake",
            )
        self.assertEqual(mismatch_runner.calls[-1][0], "psql")

    def test_failover_checks_rpo_promotes_and_runs_rollback_probe(self) -> None:
        runner = FakeRecoveryRunner(replay_lag_seconds=4.5)
        plan = self._failover_plan()
        result = self._rehearsal(runner, elapsed_seconds=12.25).execute(
            plan,
            confirmation_sha256=plan["plan_sha256"],
            artifact_directory=self.root / "unused",
            evidence_kind="offline_fake",
        )

        self.assertEqual(result["operation"], "failover")
        self.assertEqual(result["rpo_observed_seconds"], 4.5)
        self.assertTrue(result["rpo_passed"])
        self.assertEqual(result["source_fence_receipt_sha256"], "f" * 64)
        self.assertIsNone(result["backup_sha256"])
        self.assertEqual(result["command_count"], 8)
        self.assertTrue(runner.promoted)
        commands = "\n".join(" ".join(call) for call in runner.calls)
        self.assertIn("pg_promote", commands)
        self.assertIn("CREATE TEMP TABLE crag_recovery_probe", commands)
        self.assertIn("ROLLBACK", commands)
        serialized = json.dumps(result)
        self.assertNotIn("primary_a", serialized)
        self.assertNotIn("standby_b", serialized)

    def test_failover_rejects_excessive_or_unknown_replay_lag_before_promotion(self) -> None:
        plan = self._failover_plan()
        for replay_lag in (901.0, None):
            runner = FakeRecoveryRunner(replay_lag_seconds=replay_lag)
            with self.subTest(replay_lag=replay_lag), self.assertRaisesRegex(
                recovery.RecoveryRehearsalError,
                "RPO",
            ):
                self._rehearsal(runner).execute(
                    plan,
                    confirmation_sha256=plan["plan_sha256"],
                    artifact_directory=self.root / "unused",
                    evidence_kind="offline_fake",
                )
            self.assertFalse(runner.promoted)
            self.assertEqual(len(runner.calls), 3)

    def test_command_failure_does_not_expose_raw_output_or_aliases(self) -> None:
        runner = FakeRecoveryRunner()
        runner.fail_executable = "pg_dump"
        runner.failure_stdout = "sensitive-command-output-marker"
        plan = self._backup_plan()
        with self.assertRaises(recovery.RecoveryRehearsalError) as caught:
            self._rehearsal(runner).execute(
                plan,
                confirmation_sha256=plan["plan_sha256"],
                artifact_directory=self.root / "failure-artifacts",
                evidence_kind="offline_fake",
            )
        message = str(caught.exception)
        for forbidden in (runner.failure_stdout, "sensitive", "primary_a", str(self.root)):
            self.assertNotIn(forbidden, message)
        self.assertIn("command failed", message)

    def test_failover_role_promotion_and_probe_failures_stop_safely(self) -> None:
        plan = self._failover_plan()

        source_standby = FakeRecoveryRunner(source_in_recovery=True)
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "source"):
            self._rehearsal(source_standby).execute(
                plan,
                confirmation_sha256=plan["plan_sha256"],
                artifact_directory=self.root / "unused-a",
                evidence_kind="offline_fake",
            )
        self.assertFalse(source_standby.promoted)

        target_primary = FakeRecoveryRunner()
        target_primary.target_is_standby = False
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "standby"):
            self._rehearsal(target_primary).execute(
                plan,
                confirmation_sha256=plan["plan_sha256"],
                artifact_directory=self.root / "unused-b",
                evidence_kind="offline_fake",
            )
        self.assertFalse(target_primary.promoted)

        promotion_failed = FakeRecoveryRunner()
        promotion_failed.promotion_output = "not_promoted\n"
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "promotion"):
            self._rehearsal(promotion_failed).execute(
                plan,
                confirmation_sha256=plan["plan_sha256"],
                artifact_directory=self.root / "unused-c",
                evidence_kind="offline_fake",
            )

        recovery_mode = FakeRecoveryRunner()
        recovery_mode.post_promotion_in_recovery = True
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "recovery mode"):
            self._rehearsal(recovery_mode).execute(
                plan,
                confirmation_sha256=plan["plan_sha256"],
                artifact_directory=self.root / "unused-d",
                evidence_kind="offline_fake",
            )

        probe_failed = FakeRecoveryRunner()
        probe_failed.probe_output = "BEGIN\nprobe_failed\nROLLBACK\n"
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "probe"):
            self._rehearsal(probe_failed).execute(
                plan,
                confirmation_sha256=plan["plan_sha256"],
                artifact_directory=self.root / "unused-e",
                evidence_kind="offline_fake",
            )

    def test_invalid_inputs_rto_and_result_validation_fail_closed(self) -> None:
        invalid_plans = (
            {"operation": "invalid", "rehearsal_id": "valid", "source": "a", "target": "b"},
            {"operation": "backup_restore", "rehearsal_id": "bad/id", "source": "a", "target": "b"},
            {"operation": "backup_restore", "rehearsal_id": "valid", "source": "bad/service", "target": "b"},
            {"operation": "backup_restore", "rehearsal_id": "valid", "source": "same", "target": "same"},
        )
        for case in invalid_plans:
            with self.subTest(case=case), self.assertRaises(recovery.RecoveryRehearsalError):
                recovery.build_plan(
                    operation=case["operation"],
                    rehearsal_id=case["rehearsal_id"],
                    source_service=case["source"],
                    target_service=case["target"],
                    policy=self.policy,
                    created_at_utc="2026-08-08T12:00:00Z",
                )
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "timestamp"):
            recovery.build_plan(
                operation="backup_restore",
                rehearsal_id="bad-time",
                source_service="primary_a",
                target_service="clean_restore_b",
                policy=self.policy,
                created_at_utc="2026-99-99T12:00:00Z",
            )
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "cannot bind"):
            recovery.build_plan(
                operation="backup_restore",
                rehearsal_id="unexpected-fence",
                source_service="primary_a",
                target_service="clean_restore_b",
                source_fence_receipt_sha256="f" * 64,
                policy=self.policy,
                created_at_utc="2026-08-08T12:00:00Z",
            )

        policy_value = json.loads(
            (ROOT / "reliability" / "postgres-recovery-policy.json").read_text(encoding="utf-8")
        )
        policy_value["rpo_seconds"] = 901
        invalid_policy = self.root / "invalid-policy.json"
        invalid_policy.write_text(json.dumps(policy_value), encoding="utf-8")
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "values"):
            recovery.load_policy(invalid_policy)
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "could not be loaded"):
            recovery.load_policy(self.root / "missing-policy.json")
        array_policy = self.root / "array-policy.json"
        array_policy.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "JSON object"):
            recovery.load_policy(array_policy)

        plan = self._backup_plan()
        missing_plan_field = dict(plan)
        missing_plan_field.pop("operation")
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "fields"):
            recovery.validate_plan(
                missing_plan_field,
                policy=self.policy,
                confirmation_sha256=plan["plan_sha256"],
            )
        bad_plan_hash = dict(plan)
        bad_plan_hash["plan_sha256"] = "bad"
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "SHA-256"):
            recovery.validate_plan(
                bad_plan_hash,
                policy=self.policy,
                confirmation_sha256="bad",
            )
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "evidence"):
            self._rehearsal(FakeRecoveryRunner()).execute(
                plan,
                confirmation_sha256=plan["plan_sha256"],
                artifact_directory=self.root / "invalid-evidence",
                evidence_kind="made_up",
            )
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "RTO"):
            self._rehearsal(FakeRecoveryRunner(), elapsed_seconds=1801).execute(
                plan,
                confirmation_sha256=plan["plan_sha256"],
                artifact_directory=self.root / "slow-artifacts",
                evidence_kind="offline_fake",
            )

        result = self._rehearsal(FakeRecoveryRunner()).execute(
            plan,
            confirmation_sha256=plan["plan_sha256"],
            artifact_directory=self.root / "valid-artifacts",
            evidence_kind="offline_fake",
        )
        missing = dict(result)
        missing.pop("command_count")
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "fields"):
            recovery.validate_result(missing)
        invalid_hash = dict(result)
        invalid_hash["plan_sha256"] = "bad"
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "hashes"):
            recovery.validate_result(invalid_hash)
        invalid_duration = dict(result)
        invalid_duration["duration_seconds"] = True
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "values"):
            recovery.validate_result(invalid_duration)

    def test_cli_execute_writes_a_redacted_result_artifact(self) -> None:
        plan = self._backup_plan()
        plan_path = self.root / "execute-plan.json"
        recovery.write_plan(plan_path, plan)
        fake_result = self._rehearsal(FakeRecoveryRunner()).execute(
            plan,
            confirmation_sha256=plan["plan_sha256"],
            artifact_directory=self.root / "seed-artifacts",
            evidence_kind="offline_fake",
        )
        result_path = self.root / "execute-result.json"
        stdout = io.StringIO()
        with patch.object(recovery, "PostgresRecoveryRehearsal") as rehearsal_class:
            rehearsal_class.return_value.execute.return_value = fake_result
            with redirect_stdout(stdout):
                recovery.main(
                    [
                        "execute",
                        "--plan",
                        str(plan_path),
                        "--confirmation-sha256",
                        plan["plan_sha256"],
                        "--artifact-directory",
                        str(self.root / "unused-cli-artifacts"),
                        "--result-output",
                        str(result_path),
                    ]
                )
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), fake_result)
        rendered = json.dumps(summary)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn("primary_a", rendered)

    def test_policy_schema_and_cli_plan_are_frozen_and_redacted(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "postgres-recovery-result.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["rpo_target_seconds"]["const"], 900)
        self.assertEqual(schema["properties"]["rto_target_seconds"]["const"], 1800)

        plan_path = self.root / "plans" / "backup.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            recovery.main(
                [
                    "plan",
                    "--operation",
                    "backup_restore",
                    "--rehearsal-id",
                    "cli-backup-a",
                    "--source-service",
                    "primary_a",
                    "--target-service",
                    "clean_restore_b",
                    "--created-at-utc",
                    "2026-08-08T12:00:00Z",
                    "--output",
                    str(plan_path),
                ]
            )
        summary = json.loads(stdout.getvalue())
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["plan_sha256"], plan["plan_sha256"])
        rendered = json.dumps(summary)
        self.assertNotIn("primary_a", rendered)
        self.assertNotIn("clean_restore_b", rendered)
        self.assertNotIn(str(self.root), rendered)
        with self.assertRaisesRegex(recovery.RecoveryRehearsalError, "written"):
            recovery.write_plan(plan_path, plan)


if __name__ == "__main__":
    unittest.main()
