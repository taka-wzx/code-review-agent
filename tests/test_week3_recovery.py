"""Phase 1 tests for atomic checkpoint persistence and resume validation."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from code_review_agent.repair_approval import issue_write_approval
from code_review_agent.repair_budget import (
    BudgetExceeded,
    BudgetManager,
    CohortCostLedger,
    CohortLedgerCorrupt,
    CohortLedgerError,
    CohortLimitMismatch,
)
from code_review_agent.repair_checkpoint import (
    CheckpointCorrupt,
    CheckpointMismatch,
    CheckpointStore,
    CheckpointVersionError,
    RepairCheckpoint,
    RunLockUnavailable,
)
from code_review_agent.repair_state import RepairState


def _reserve_from_process(state_root, start, results, index):
    """Spawn-safe worker proving that separate processes share one OS lock."""
    start.wait()
    ledger = CohortCostLedger(state_root, "week3-real-issues", 10.0)
    try:
        ledger.reserve(f"run-{index}", f"reservation-{index}", 3.0)
    except BudgetExceeded:
        results.put("exceeded")
    except Exception as exc:  # pragma: no cover - reported to the parent assertion
        results.put(f"error:{type(exc).__name__}:{exc}")
    else:
        results.put("reserved")


def _rechecksum_cost_ledger(envelope):
    encoded_ledger = json.dumps(
        envelope["ledger"],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope["checksum"] = hashlib.sha256(encoded_ledger).hexdigest()


class TestCheckpointStore(unittest.TestCase):
    def make_checkpoint(self, worktree: str, **overrides):
        approval = issue_write_approval(
            run_id="run-1",
            checkpoint_id="cp-1",
            base_sha="a" * 40,
            diff_hash="d" * 64,
            plan_hash="p" * 64,
            patch_hash="e" * 64,
            writable_paths=("src/mod.py",),
            patch_attempt=1,
            ttl_seconds=60,
            now=100,
            nonce="human-nonce",
        )
        values = {
            "run_id": "run-1",
            "repository_id": "repo-identity",
            "base_sha": "a" * 40,
            "task_branch": "repair/issue-1-run-1",
            "worktree": worktree,
            "state": RepairState.PLAN,
            "state_history": (RepairState.DISCOVER, RepairState.PLAN),
            "sequence": 3,
            "issue_ref": "https://github.com/example/repo/issues/1",
            "original_snapshot": {"head": "a" * 40, "status": []},
            "plan": {"summary": "修复边界", "tests": ["python -m unittest"]},
            "writable_paths": ("src/mod.py",),
            "plan_hash": "p" * 64,
            "status_summary": {"tracked": [], "untracked": []},
            "diff_hash": "d" * 64,
            "tool_ledger": [{"operation_id": "op-1", "tool": "git_status"}],
            "test_results": [],
            "budget": BudgetManager().to_dict(),
            "approvals": [approval.to_dict()],
            "last_transition": {"from": "DISCOVER", "to": "PLAN"},
            "updated_at": 123.5,
        }
        values.update(overrides)
        return RepairCheckpoint(**values)

    def test_round_trip_and_append_only_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            worktree = str(Path(tmp) / "worktree")
            checkpoint = self.make_checkpoint(worktree)
            store = CheckpointStore(root, clock=lambda: 200.0)
            checksum = store.save(checkpoint)
            store.append_event("run-1", "approval_received", {"kind": "write"})
            loaded = store.load("run-1")
            events = store.events("run-1")

        self.assertEqual(loaded, checkpoint)
        self.assertEqual(len(checksum), 64)
        self.assertEqual([event["kind"] for event in events], [
            "checkpoint_saved",
            "approval_received",
        ])
        self.assertEqual(events[0]["data"]["checksum"], checksum)
        self.assertEqual(events[1]["t"], 200.0)

    def test_complete_state_history_is_required_and_validated(self):
        with self.assertRaises(ValueError):
            self.make_checkpoint(
                "worktree",
                state=RepairState.SUBMIT,
                state_history=(RepairState.DISCOVER, RepairState.SUBMIT),
            )
        checkpoint = self.make_checkpoint("worktree")
        payload = checkpoint.to_dict()
        del payload["state_history"]
        with self.assertRaisesRegex(CheckpointCorrupt, "state_history"):
            RepairCheckpoint.from_dict(payload)

    def test_cross_process_style_run_lock_is_exclusive_and_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            with store.acquire_run_lock("run-1"):
                with self.assertRaises(RunLockUnavailable):
                    with store.acquire_run_lock("run-1"):
                        self.fail("second owner unexpectedly acquired the run lock")
            with store.acquire_run_lock("run-1"):
                self.assertTrue(store.lock_path("run-1").is_file())

    def test_checksum_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            checkpoint = self.make_checkpoint(str(Path(tmp) / "worktree"))
            store.save(checkpoint)
            path = store.snapshot_path("run-1")
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["checkpoint"]["diff_hash"] = "tampered"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaisesRegex(CheckpointCorrupt, "checksum mismatch"):
                store.load("run-1")

    def test_non_finite_checkpoint_data_is_reported_as_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            checkpoint = self.make_checkpoint(str(Path(tmp) / "worktree"))
            store.save(checkpoint)
            path = store.snapshot_path("run-1")
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["checkpoint"]["budget"] = {"cost": float("nan")}
            envelope["checksum"] = "0" * 64
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaisesRegex(CheckpointCorrupt, "canonical JSON"):
                store.load("run-1")

    def test_unknown_schema_is_rejected_before_payload_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            checkpoint = self.make_checkpoint(str(Path(tmp) / "worktree"))
            store.save(checkpoint)
            path = store.snapshot_path("run-1")
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["schema_version"] = 99
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaises(CheckpointVersionError):
                store.load("run-1")

    def test_valid_checksum_does_not_make_invalid_payload_types_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = self.make_checkpoint(str(Path(tmp) / "worktree"))
            payload = checkpoint.to_dict()
            payload["writable_paths"] = "src/mod.py"
            with self.assertRaisesRegex(CheckpointCorrupt, "list of strings"):
                RepairCheckpoint.from_dict(payload)
            payload = checkpoint.to_dict()
            payload["state"] = ["PLAN"]
            with self.assertRaisesRegex(CheckpointCorrupt, "invalid repair state"):
                RepairCheckpoint.from_dict(payload)
            for value in (True, "1.0"):
                with self.subTest(updated_at=value):
                    payload = checkpoint.to_dict()
                    payload["updated_at"] = value
                    with self.assertRaisesRegex(CheckpointCorrupt, "updated_at"):
                        RepairCheckpoint.from_dict(payload)

    def test_atomic_replace_failure_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            first = self.make_checkpoint(str(Path(tmp) / "worktree"))
            store.save(first)
            second = replace(
                first,
                sequence=4,
                state=RepairState.PATCH,
                state_history=(RepairState.DISCOVER, RepairState.PLAN, RepairState.PATCH),
            )
            with mock.patch(
                "code_review_agent.repair_checkpoint.os.replace",
                side_effect=OSError("simulated crash"),
            ), self.assertRaisesRegex(OSError, "simulated crash"):
                store.save(second)
            loaded = store.load("run-1")
            temporary_files = list((Path(tmp) / "run-1").glob("*.tmp"))

        self.assertEqual(loaded.sequence, 3)
        self.assertEqual(loaded.state, RepairState.PLAN)
        self.assertEqual(temporary_files, [])

    def test_resume_snapshot_mismatch_lists_every_changed_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = str(Path(tmp) / "worktree")
            checkpoint = self.make_checkpoint(worktree)
            with self.assertRaises(CheckpointMismatch) as caught:
                checkpoint.assert_matches(
                    repository_id="other-repo",
                    base_sha=checkpoint.base_sha,
                    task_branch=checkpoint.task_branch,
                    worktree=worktree,
                    status_summary={"tracked": ["changed"]},
                    diff_hash="other-diff",
                )
        self.assertEqual(
            caught.exception.fields,
            ["repository_id", "status_summary", "diff_hash"],
        )

    def test_run_id_cannot_escape_state_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            for run_id in ("../outside", "a/b", "", ".hidden"):
                with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                    store.snapshot_path(run_id)

    def test_windows_aliasing_run_ids_are_rejected(self):
        # Windows strips trailing dots and treats path components case
        # insensitively, so these aliases could let one run clobber another
        # run's snapshot directory.
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            for run_id in ("run.", "Run-1", "nul", "CON", "com1.log"):
                with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                    store.snapshot_path(run_id)
        for run_id in ("run-1.", "Run-1"):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                self.make_checkpoint("some-worktree", run_id=run_id)

    def test_checkpoint_writable_paths_cannot_escape_the_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = self.make_checkpoint(str(Path(tmp) / "worktree"))
            for paths in (["../escape.py"], [".git/config"], ["src/mod.py:stream"]):
                payload = checkpoint.to_dict()
                payload["writable_paths"] = paths
                with self.subTest(paths=paths), self.assertRaises(CheckpointCorrupt):
                    RepairCheckpoint.from_dict(payload)
            with self.assertRaises(ValueError):
                self.make_checkpoint(
                    str(Path(tmp) / "worktree"), writable_paths=("../escape.py",)
                )

    def test_corrupt_journal_line_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            checkpoint = self.make_checkpoint(str(Path(tmp) / "worktree"))
            store.save(checkpoint)
            with store.journal_path("run-1").open("a", encoding="utf-8") as stream:
                stream.write("{interrupted\n")
            with self.assertRaisesRegex(CheckpointCorrupt, "line 2"):
                store.events("run-1")

    def test_concurrent_journal_records_remain_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), clock=lambda: 200.0)

            def write(worker):
                for index in range(50):
                    store.append_event(
                        "run-1", "worker", {"worker": worker, "index": index}
                    )

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(write, range(4)))
            events = store.events("run-1")

        self.assertEqual(len(events), 200)
        self.assertEqual(
            {(event["data"]["worker"], event["data"]["index"]) for event in events},
            {(worker, index) for worker in range(4) for index in range(50)},
        )


class TestCohortCostLedger(unittest.TestCase):
    def test_micro_usd_accounting_is_exact_and_rejects_excess_precision(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CohortCostLedger(tmp, "week3-real-issues", 0.3)
            for index in range(3):
                ledger.reserve(f"run-{index}", f"reservation-{index}", 0.1)
            snapshot = ledger.snapshot()
            envelope = json.loads(ledger.path.read_text(encoding="utf-8"))

            self.assertEqual(snapshot.reserved_usd, 0.3)
            self.assertEqual(snapshot.remaining_usd, 0.0)
            self.assertEqual(envelope["ledger"]["total_cost_microusd"], 300_000)
            self.assertEqual(
                [item["cost_microusd"] for item in envelope["ledger"]["reservations"]],
                [100_000, 100_000, 100_000],
            )
            with self.assertRaisesRegex(ValueError, "six decimal places"):
                ledger.reserve("run-extra", "reservation-extra", 0.0000001)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = CohortCostLedger(tmp, "week3-real-issues", 1.0)
            ledger.reserve("run-1", "reservation-1", 0.3)
            with self.assertRaisesRegex(ValueError, "six decimal places"):
                ledger.reconcile("run-1", "reservation-1", 0.1 + 0.2)
            self.assertEqual(ledger.snapshot().reserved_usd, 0.3)

    def test_reservation_survives_restart_and_reconcile_retains_actual_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = CohortCostLedger(tmp, "week3-real-issues", 10.0)
            reserved = first.reserve("run-1", "reservation-1", 3.0)

            resumed = CohortCostLedger(tmp, "week3-real-issues", 10.0)
            crashed_snapshot = resumed.snapshot()
            reconciled = resumed.reconcile("run-1", "reservation-1", 2.25)

        self.assertEqual(reserved.reserved_usd, 3.0)
        self.assertEqual(crashed_snapshot.reserved_usd, 3.0)
        self.assertEqual(crashed_snapshot.remaining_usd, 7.0)
        self.assertEqual(reconciled.spent_usd, 2.25)
        self.assertEqual(reconciled.reserved_usd, 0.0)

    def test_identity_is_idempotent_and_cannot_be_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CohortCostLedger(tmp, "week3-real-issues", 10.0)
            ledger.reserve("run-1", "reservation-1", 3.0)
            duplicate = ledger.reserve("run-1", "reservation-1", 3.0)
            self.assertEqual(len(duplicate.reservations), 1)
            with self.assertRaisesRegex(CohortLedgerError, "changed"):
                ledger.reserve("run-1", "reservation-1", 2.0)

            ledger.reconcile("run-1", "reservation-1", 1.5)
            replay = ledger.reconcile("run-1", "reservation-1", 1.5)
            self.assertEqual(replay.spent_usd, 1.5)
            with self.assertRaisesRegex(CohortLedgerError, "replay mismatch"):
                ledger.reconcile("run-1", "reservation-1", 1.0)
            with self.assertRaisesRegex(CohortLedgerError, "cannot be replayed"):
                ledger.reserve("run-1", "reservation-1", 3.0)

            ledger.reserve("run-2", "reservation-1", 2.0)
            ledger.cancel("run-2", "reservation-1")
            cancelled_replay = ledger.cancel("run-2", "reservation-1")
            self.assertEqual(cancelled_replay.spent_usd, 1.5)
            with self.assertRaisesRegex(CohortLedgerError, "cannot be replayed"):
                ledger.reserve("run-2", "reservation-1", 2.0)

    def test_actual_over_reservation_is_persisted_then_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CohortCostLedger(tmp, "week3-real-issues", 10.0)
            ledger.reserve("run-1", "reservation-1", 2.0)
            with self.assertRaisesRegex(CohortLedgerError, "retained"):
                ledger.reconcile("run-1", "reservation-1", 3.0)

            resumed = CohortCostLedger(tmp, "week3-real-issues", 10.0)
            snapshot = resumed.snapshot()
            with self.assertRaisesRegex(CohortLedgerError, "fail-closed"):
                resumed.reserve("run-2", "reservation-2", 1.0)

        self.assertEqual(snapshot.spent_usd, 3.0)
        self.assertEqual(snapshot.reserved_usd, 0.0)
        self.assertIsNotNone(snapshot.accounting_failure)

    def test_processes_cannot_oversell_aggregate_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=_reserve_from_process,
                    args=(tmp, start, results, index),
                )
                for index in range(4)
            ]
            try:
                for process in processes:
                    process.start()
                start.set()
                for process in processes:
                    process.join(20)
                self.assertTrue(all(not process.is_alive() for process in processes))
                outcomes = [results.get(timeout=5) for _ in processes]
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(5)

            snapshot = CohortCostLedger(tmp, "week3-real-issues", 10.0).snapshot()

        self.assertEqual(outcomes.count("reserved"), 3, outcomes)
        self.assertEqual(outcomes.count("exceeded"), 1, outcomes)
        self.assertEqual(snapshot.reserved_usd, 9.0)
        self.assertGreaterEqual(snapshot.remaining_usd, 0.0)

    def test_missing_fields_corruption_and_limit_mismatch_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CohortCostLedger(tmp, "week3-real-issues", 10.0)
            ledger.reserve("run-1", "reservation-1", 1.0)
            with self.assertRaises(CohortLimitMismatch):
                CohortCostLedger(tmp, "week3-real-issues", 11.0).snapshot()

            envelope = json.loads(ledger.path.read_text(encoding="utf-8"))
            del envelope["ledger"]["spent_microusd"]
            _rechecksum_cost_ledger(envelope)
            ledger.path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaisesRegex(CohortLedgerCorrupt, "missing spent_microusd"):
                ledger.snapshot()

            ledger.path.write_text("{interrupted", encoding="utf-8")
            with self.assertRaisesRegex(CohortLedgerCorrupt, "invalid.*JSON"):
                ledger.snapshot()

    def test_schema_type_duplicate_keys_and_amount_encoding_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CohortCostLedger(tmp, "week3-real-issues", 10.0)
            ledger.reserve("run-1", "reservation-1", 1.0)
            original = ledger.path.read_text(encoding="utf-8")

            ledger.path.write_text(
                original.replace('"schema_version":1', '"schema_version":1.0', 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CohortLedgerCorrupt, "unsupported.*schema"):
                ledger.snapshot()

            duplicate_schema = original.replace(
                '"schema_version":1',
                '"schema_version":1,"schema_version":1',
                1,
            )
            ledger.path.write_text(duplicate_schema, encoding="utf-8")
            with self.assertRaisesRegex(CohortLedgerCorrupt, "duplicate JSON key"):
                ledger.snapshot()

            envelope = json.loads(original)
            envelope["ledger"]["spent_microusd"] = 0.0
            _rechecksum_cost_ledger(envelope)
            ledger.path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaisesRegex(CohortLedgerCorrupt, "integer.*micro-USD"):
                ledger.snapshot()

    def test_recovery_rejects_semantically_inconsistent_cost_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CohortCostLedger(tmp, "week3-real-issues", 10.0)
            ledger.reserve("run-1", "reservation-1", 2.0)
            ledger.reconcile("run-1", "reservation-1", 1.0)
            healthy = json.loads(ledger.path.read_text(encoding="utf-8"))

            spent_mismatch = json.loads(json.dumps(healthy))
            spent_mismatch["ledger"]["spent_microusd"] = 2_000_000
            _rechecksum_cost_ledger(spent_mismatch)
            ledger.path.write_text(json.dumps(spent_mismatch), encoding="utf-8")
            with self.assertRaisesRegex(CohortLedgerCorrupt, "does not match"):
                ledger.snapshot()

            hidden_overrun = json.loads(json.dumps(healthy))
            finalization = hidden_overrun["ledger"]["finalized"][0]
            finalization["actual_cost_microusd"] = 3_000_000
            hidden_overrun["ledger"]["spent_microusd"] = 3_000_000
            _rechecksum_cost_ledger(hidden_overrun)
            ledger.path.write_text(json.dumps(hidden_overrun), encoding="utf-8")
            with self.assertRaisesRegex(CohortLedgerCorrupt, "matching accounting failure"):
                ledger.snapshot()

            unexplained_failure = json.loads(json.dumps(healthy))
            unexplained_failure["ledger"]["accounting_failure"] = "unexplained"
            _rechecksum_cost_ledger(unexplained_failure)
            ledger.path.write_text(json.dumps(unexplained_failure), encoding="utf-8")
            with self.assertRaisesRegex(CohortLedgerCorrupt, "no matching"):
                ledger.snapshot()

    def test_atomic_replace_failure_preserves_previous_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CohortCostLedger(tmp, "week3-real-issues", 10.0)
            ledger.reserve("run-1", "reservation-1", 1.0)
            with mock.patch(
                "code_review_agent.repair_budget.os.replace",
                side_effect=OSError("simulated crash"),
            ), self.assertRaisesRegex(OSError, "simulated crash"):
                ledger.reserve("run-2", "reservation-2", 1.0)

            resumed = CohortCostLedger(tmp, "week3-real-issues", 10.0).snapshot()
            temporary_files = list(Path(tmp).glob("*.tmp"))

        self.assertEqual(len(resumed.reservations), 1)
        self.assertEqual(resumed.reservations[0].run_id, "run-1")
        self.assertEqual(temporary_files, [])


if __name__ == "__main__":
    unittest.main()
