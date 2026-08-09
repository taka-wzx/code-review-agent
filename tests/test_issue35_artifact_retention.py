from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_review_agent.artifact_retention import (  # noqa: E402
    ArtifactRetentionError,
    ArtifactRetentionLedger,
    RetentionReceipt,
)
from scripts import run_artifact_retention as retention_cli  # noqa: E402


class Issue35ArtifactRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.artifact_root = self.base / "artifacts"
        self.artifact_root.mkdir()
        self.ledger_path = self.base / "state" / "retention.sqlite3"
        self.ledger = ArtifactRetentionLedger(self.artifact_root, self.ledger_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write(self, relative_path: str, content: bytes = b"retained artifact") -> Path:
        path = self.artifact_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _register(
        self,
        artifact_id: str,
        relative_path: str,
        *,
        deadline: float = 10.0,
        content: bytes = b"retained artifact",
    ) -> Path:
        path = self._write(relative_path, content)
        self.ledger.register_artifact(
            artifact_id,
            relative_path,
            retention_deadline=deadline,
        )
        return path

    def _cli(self, *argv: str) -> dict:
        output = io.StringIO()
        with redirect_stdout(output):
            retention_cli.main(list(argv))
        return json.loads(output.getvalue())

    def test_registration_stays_inside_root_and_respects_schedule_deadline(self) -> None:
        outside = self.base / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        with self.assertRaisesRegex(ArtifactRetentionError, "escapes"):
            self.ledger.register_artifact(
                "artifact-1",
                "../outside.txt",
                retention_deadline=10,
            )
        with self.assertRaisesRegex(ArtifactRetentionError, "identifier"):
            self.ledger.register_artifact(
                "invalid/id",
                "missing.txt",
                retention_deadline=10,
            )
        ledger_inside_root = ArtifactRetentionLedger(
            self.artifact_root,
            self.artifact_root / "retention.sqlite3",
        )
        with self.assertRaisesRegex(ArtifactRetentionError, "reserved"):
            ledger_inside_root.register_artifact(
                "artifact-ledger",
                "retention.sqlite3",
                retention_deadline=10,
            )

        path = self._register("artifact-1", "nested/retained.txt", deadline=20)
        first = self.ledger.run_scheduled(interval_seconds=60, now=10)
        self.assertTrue(first.scheduled)
        self.assertEqual(first.eligible, 0)
        self.assertTrue(path.exists())

        skipped = self.ledger.run_scheduled(interval_seconds=60, now=20)
        self.assertFalse(skipped.scheduled)
        self.assertTrue(path.exists())

        deleted = self.ledger.run_scheduled(interval_seconds=60, now=70)
        self.assertEqual(deleted.deleted, 1)
        self.assertFalse(path.exists())
        self.assertEqual(len(deleted.receipts), 1)

        path.write_bytes(b"replacement artifact")
        with self.assertRaisesRegex(ArtifactRetentionError, "conflicts"):
            self.ledger.register_artifact(
                "artifact-1",
                "nested/retained.txt",
                retention_deadline=80,
            )

    def test_legal_hold_blocks_dry_run_and_real_deletion(self) -> None:
        path = self._register("holdable-1", "holdable.txt", deadline=1)
        hold = self.ledger.place_legal_hold("holdable-1", "pending legal review", now=0)
        self.assertEqual(hold["artifact_id_sha256"], hashlib.sha256(b"holdable-1").hexdigest())
        self.assertNotIn("pending legal review", json.dumps(hold))

        dry_run = self.ledger.run_scheduled(interval_seconds=1, now=2, dry_run=True)
        self.assertTrue(dry_run.scheduled)
        self.assertEqual(dry_run.eligible, 0)
        self.assertEqual(dry_run.held, 1)
        self.assertTrue(path.exists())
        self.assertEqual(self.ledger.list_receipts(), ())

        real_run = self.ledger.run_scheduled(interval_seconds=1, now=2)
        self.assertEqual(real_run.held, 1)
        self.assertTrue(path.exists())
        self.assertTrue(self.ledger.release_legal_hold("holdable-1"))
        self.assertFalse(self.ledger.release_legal_hold("holdable-1"))

        deleted = self.ledger.run_scheduled(interval_seconds=1, now=3)
        self.assertEqual(deleted.deleted, 1)
        self.assertFalse(path.exists())

    def test_dry_run_does_not_mutate_file_schedule_or_receipts(self) -> None:
        path = self._register("dry-run-1", "dry-run.txt", deadline=1)

        preview = self.ledger.run_scheduled(interval_seconds=3600, now=2, dry_run=True)
        self.assertTrue(preview.scheduled)
        self.assertTrue(preview.dry_run)
        self.assertEqual(preview.eligible, 1)
        self.assertTrue(path.exists())
        self.assertEqual(self.ledger.list_receipts(), ())

        actual = self.ledger.run_scheduled(interval_seconds=3600, now=2)
        self.assertEqual(actual.deleted, 1)
        self.assertFalse(path.exists())
        self.assertEqual(len(self.ledger.list_receipts()), 1)

    def test_failed_deletion_retries_without_duplicate_receipts(self) -> None:
        calls: list[Path] = []

        def fail_once(path: Path) -> None:
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError("simulated failure")
            path.unlink()

        ledger = ArtifactRetentionLedger(self.artifact_root, self.ledger_path, unlinker=fail_once)
        path = self._write("retry.txt")
        ledger.register_artifact("retry-1", "retry.txt", retention_deadline=1)

        failed = ledger.run_scheduled(interval_seconds=60, now=10)
        self.assertEqual(failed.retry_scheduled, 1)
        self.assertTrue(path.exists())
        self.assertEqual(ledger.list_receipts(), ())

        too_soon = ledger.run_scheduled(interval_seconds=60, now=11)
        self.assertFalse(too_soon.scheduled)

        recovered = ledger.run_scheduled(interval_seconds=60, now=12)
        self.assertEqual(recovered.deleted, 1)
        self.assertFalse(path.exists())
        self.assertEqual(recovered.receipts[0].attempt, 2)

        later = ledger.run_scheduled(interval_seconds=1, now=13)
        self.assertEqual(later.deleted, 0)
        self.assertEqual(len(ledger.list_receipts()), 1)
        self.assertEqual(len(calls), 2)

    def test_missing_file_creates_one_hash_only_absence_receipt(self) -> None:
        path = self._register("missing-1", "private/customer.txt", deadline=1)
        path.unlink()

        result = self.ledger.run_scheduled(interval_seconds=1, now=2)
        self.assertEqual(result.already_absent, 1)
        receipt = result.receipts[0]
        self.assertEqual(receipt.deletion_outcome, "already_absent")
        self.assertIsNone(receipt.content_sha256)
        self.assertEqual(len(self.ledger.list_receipts()), 1)

        repeated = self.ledger.run_scheduled(interval_seconds=1, now=3)
        self.assertEqual(repeated.already_absent, 0)
        self.assertEqual(len(self.ledger.list_receipts()), 1)

    def test_receipts_are_stable_hash_only_records(self) -> None:
        artifact_id = "confidential-job-47"
        relative_path = "sensitive/customer-contract.txt"
        content = b"do not disclose this content"
        path = self._register(
            artifact_id,
            relative_path,
            deadline=1,
            content=content,
        )

        result = self.ledger.run_scheduled(interval_seconds=1, now=2)
        self.assertFalse(path.exists())
        receipt = result.receipts[0]
        serialized = json.dumps(receipt.as_dict(), sort_keys=True)
        for forbidden in (artifact_id, relative_path, str(self.artifact_root), content.decode("ascii")):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(receipt.artifact_id_sha256, hashlib.sha256(artifact_id.encode()).hexdigest())
        self.assertEqual(receipt.artifact_path_sha256, hashlib.sha256(relative_path.encode()).hexdigest())
        self.assertEqual(receipt.content_sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(RetentionReceipt.from_dict(receipt.as_dict()), receipt)

        malformed = receipt.as_dict()
        malformed["receipt_sha256"] = "b" * 64
        with self.assertRaisesRegex(ArtifactRetentionError, "malformed"):
            RetentionReceipt.from_dict(malformed)

    def test_cli_outputs_only_hashes_and_aggregate_state(self) -> None:
        artifact_id = "cli-private-1"
        relative_path = "restricted/entry.txt"
        self._write(relative_path, b"private cli content")
        common = (
            "--artifact-root",
            str(self.artifact_root),
            "--ledger",
            str(self.ledger_path),
        )

        registered = self._cli(
            "register",
            *common,
            "--artifact-id",
            artifact_id,
            "--relative-path",
            relative_path,
            "--retention-deadline",
            "1",
        )
        self.assertIn("artifact_id_sha256", registered)
        self.assertNotIn(artifact_id, json.dumps(registered))
        self.assertNotIn(relative_path, json.dumps(registered))

        held = self._cli(
            "hold",
            *common,
            "--artifact-id",
            artifact_id,
            "--reason",
            "legal-team-review",
            "--now",
            "0",
        )
        self.assertNotIn("legal-team-review", json.dumps(held))
        released = self._cli("release-hold", *common, "--artifact-id", artifact_id)
        self.assertEqual(released, {"released": True})

        preview = self._cli(
            "run",
            *common,
            "--interval-seconds",
            "1",
            "--now",
            "2",
            "--dry-run",
        )
        self.assertEqual(preview["eligible"], 1)
        self.assertNotIn(str(self.artifact_root), json.dumps(preview))

        actual = self._cli(
            "run",
            *common,
            "--interval-seconds",
            "1",
            "--now",
            "2",
        )
        self.assertEqual(actual["deleted"], 1)
        receipts = self._cli("receipts", *common)
        self.assertEqual(len(receipts["receipts"]), 1)
        self.assertNotIn(artifact_id, json.dumps(receipts))
        self.assertNotIn(relative_path, json.dumps(receipts))


if __name__ == "__main__":
    unittest.main()
