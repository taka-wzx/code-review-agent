"""Phase 1 tests for atomic checkpoint persistence and resume validation."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from code_review_agent.repair_approval import issue_write_approval
from code_review_agent.repair_budget import BudgetManager
from code_review_agent.repair_checkpoint import (
    CheckpointCorrupt,
    CheckpointMismatch,
    CheckpointStore,
    CheckpointVersionError,
    RepairCheckpoint,
)
from code_review_agent.repair_state import RepairState


class TestCheckpointStore(unittest.TestCase):
    def make_checkpoint(self, worktree: str, **overrides):
        approval = issue_write_approval(
            run_id="run-1",
            checkpoint_id="cp-1",
            base_sha="a" * 40,
            diff_hash="d" * 64,
            plan_hash="p" * 64,
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

    def test_atomic_replace_failure_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            first = self.make_checkpoint(str(Path(tmp) / "worktree"))
            store.save(first)
            second = replace(first, sequence=4, state=RepairState.PATCH)
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


if __name__ == "__main__":
    unittest.main()
