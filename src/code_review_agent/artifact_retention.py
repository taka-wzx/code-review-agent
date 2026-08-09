"""Scheduled local artifact retention with legal holds and hash-only receipts."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any


SCHEMA_VERSION = "crag.artifact-retention/v1"
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ArtifactRetentionError(ValueError):
    """Raised when an artifact retention request is unsafe or malformed."""


@dataclass(frozen=True)
class RetentionReceipt:
    """A deletion receipt that deliberately contains no raw artifact locator."""

    receipt_sha256: str
    artifact_id_sha256: str
    artifact_path_sha256: str
    content_sha256: str | None
    deletion_outcome: str
    deleted_at: float
    attempt: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "receipt_sha256": self.receipt_sha256,
            "artifact_id_sha256": self.artifact_id_sha256,
            "artifact_path_sha256": self.artifact_path_sha256,
            "content_sha256": self.content_sha256,
            "deletion_outcome": self.deletion_outcome,
            "deleted_at": self.deleted_at,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RetentionReceipt":
        fields = {
            "schema_version",
            "receipt_sha256",
            "artifact_id_sha256",
            "artifact_path_sha256",
            "content_sha256",
            "deletion_outcome",
            "deleted_at",
            "attempt",
        }
        if set(value) != fields or value.get("schema_version") != SCHEMA_VERSION:
            raise ArtifactRetentionError("retention receipt is malformed")
        hashes = (
            value.get("receipt_sha256"),
            value.get("artifact_id_sha256"),
            value.get("artifact_path_sha256"),
        )
        if not all(isinstance(item, str) and _is_sha256(item) for item in hashes):
            raise ArtifactRetentionError("retention receipt is malformed")
        content_sha256 = value.get("content_sha256")
        if content_sha256 is not None and (
            not isinstance(content_sha256, str) or not _is_sha256(content_sha256)
        ):
            raise ArtifactRetentionError("retention receipt is malformed")
        outcome = value.get("deletion_outcome")
        deleted_at = value.get("deleted_at")
        attempt = value.get("attempt")
        if (
            outcome not in {"deleted", "already_absent"}
            or isinstance(deleted_at, bool)
            or not isinstance(deleted_at, (int, float))
            or not math.isfinite(float(deleted_at))
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or attempt < 1
        ):
            raise ArtifactRetentionError("retention receipt is malformed")
        signed_payload = {
            "schema_version": SCHEMA_VERSION,
            "artifact_id_sha256": value["artifact_id_sha256"],
            "artifact_path_sha256": value["artifact_path_sha256"],
            "content_sha256": content_sha256,
            "deletion_outcome": outcome,
            "deleted_at": float(deleted_at),
            "attempt": attempt,
        }
        if value["receipt_sha256"] != _sha256(_stable_json(signed_payload)):
            raise ArtifactRetentionError("retention receipt is malformed")
        return cls(
            receipt_sha256=value["receipt_sha256"],
            artifact_id_sha256=value["artifact_id_sha256"],
            artifact_path_sha256=value["artifact_path_sha256"],
            content_sha256=content_sha256,
            deletion_outcome=outcome,
            deleted_at=float(deleted_at),
            attempt=attempt,
        )


@dataclass(frozen=True)
class RetentionRun:
    """The privacy-safe aggregate outcome of one scheduler invocation."""

    scheduled: bool
    dry_run: bool
    eligible: int
    held: int
    deleted: int
    already_absent: int
    retry_scheduled: int
    receipts: tuple[RetentionReceipt, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scheduled": self.scheduled,
            "dry_run": self.dry_run,
            "eligible": self.eligible,
            "held": self.held,
            "deleted": self.deleted,
            "already_absent": self.already_absent,
            "retry_scheduled": self.retry_scheduled,
            "receipts": [receipt.as_dict() for receipt in self.receipts],
        }


Unlinker = Callable[[Path], None]


def _is_sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _sha256(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _stable_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _timestamp(value: float | int | None) -> float:
    current = time.time() if value is None else value
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        raise ArtifactRetentionError("retention timestamp is invalid")
    current_float = float(current)
    if not math.isfinite(current_float):
        raise ArtifactRetentionError("retention timestamp is invalid")
    return current_float


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ArtifactRetentionLedger:
    """A local SQLite ledger for explicitly registered artifact files."""

    def __init__(
        self,
        artifact_root: Path,
        ledger_path: Path,
        *,
        unlinker: Unlinker | None = None,
    ) -> None:
        self.artifact_root = Path(artifact_root).resolve()
        self.ledger_path = Path(ledger_path).resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._unlinker = unlinker or Path.unlink
        self._initialize()

    def register_artifact(
        self,
        artifact_id: str,
        relative_path: str | Path,
        *,
        retention_deadline: float | int,
    ) -> dict[str, Any]:
        """Register an existing artifact for future retention processing."""
        artifact_id = self._artifact_id(artifact_id)
        normalized_path = self._normalize_relative_path(relative_path)
        artifact_path = self.artifact_root / normalized_path
        if not artifact_path.is_file():
            raise ArtifactRetentionError("registered artifact must be an existing file")
        deadline = _timestamp(retention_deadline)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT relative_path, state FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO artifacts "
                    "(artifact_id, relative_path, retention_deadline, state, delete_attempts, "
                    "retry_not_before, created_at, deleted_at) "
                    "VALUES (?, ?, ?, 'active', 0, 0, ?, NULL)",
                    (artifact_id, normalized_path, deadline, time.time()),
                )
            elif row["state"] != "active" or row["relative_path"] != normalized_path:
                raise ArtifactRetentionError("artifact registration conflicts with existing ledger state")
            else:
                connection.execute(
                    "UPDATE artifacts SET retention_deadline=? WHERE artifact_id=?",
                    (deadline, artifact_id),
                )
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_id_sha256": _sha256(artifact_id),
            "artifact_path_sha256": _sha256(normalized_path),
            "retention_deadline": deadline,
        }

    def place_legal_hold(
        self,
        artifact_id: str,
        reason: str,
        *,
        now: float | int | None = None,
    ) -> dict[str, str]:
        """Persist a legal hold using only a hash of its free-text reason."""
        artifact_id = self._artifact_id(artifact_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ArtifactRetentionError("legal hold reason is invalid")
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            if row is None or row["state"] != "active":
                raise ArtifactRetentionError("legal hold requires an active registered artifact")
            connection.execute(
                "INSERT INTO legal_holds (artifact_id, reason_sha256, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(artifact_id) DO UPDATE SET "
                "reason_sha256=excluded.reason_sha256, created_at=excluded.created_at",
                (artifact_id, _sha256(reason), timestamp),
            )
        return {
            "artifact_id_sha256": _sha256(artifact_id),
            "reason_sha256": _sha256(reason),
        }

    def release_legal_hold(self, artifact_id: str) -> bool:
        """Release an active legal hold without exposing its previous reason."""
        artifact_id = self._artifact_id(artifact_id)
        with self._transaction() as connection:
            cursor = connection.execute("DELETE FROM legal_holds WHERE artifact_id=?", (artifact_id,))
        return cursor.rowcount == 1

    def run_scheduled(
        self,
        *,
        interval_seconds: float | int,
        now: float | int | None = None,
        dry_run: bool = False,
        limit: int = 64,
    ) -> RetentionRun:
        """Run one bounded retention pass when the schedule or retry is due."""
        timestamp = _timestamp(now)
        interval = self._positive_seconds(interval_seconds, "retention schedule interval")
        if not isinstance(dry_run, bool):
            raise ArtifactRetentionError("retention dry-run flag is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1024:
            raise ArtifactRetentionError("retention batch limit is invalid")
        if not self._schedule_due(timestamp, interval):
            return RetentionRun(False, dry_run, 0, 0, 0, 0, 0, ())

        candidate_ids, held = self._due_candidates(timestamp, limit)
        if dry_run:
            return RetentionRun(True, True, len(candidate_ids), held, 0, 0, 0, ())

        deleted = 0
        already_absent = 0
        retry_scheduled = 0
        receipts: list[RetentionReceipt] = []
        for artifact_id in candidate_ids:
            outcome, receipt = self._delete_one(artifact_id, timestamp)
            if outcome == "deleted":
                deleted += 1
            elif outcome == "already_absent":
                already_absent += 1
            elif outcome == "retry":
                retry_scheduled += 1
            elif outcome == "held":
                held += 1
            if receipt is not None:
                receipts.append(receipt)
        self._record_schedule(timestamp)
        return RetentionRun(
            True,
            False,
            len(candidate_ids),
            held,
            deleted,
            already_absent,
            retry_scheduled,
            tuple(receipts),
        )

    def list_receipts(self) -> tuple[RetentionReceipt, ...]:
        """Return the durable, hash-only deletion receipts in chronological order."""
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT payload_json FROM deletion_receipts ORDER BY created_at, receipt_sha256"
            ).fetchall()
        finally:
            connection.close()
        receipts: list[RetentionReceipt] = []
        for row in rows:
            try:
                value = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ArtifactRetentionError("retention receipt is malformed") from exc
            if not isinstance(value, dict):
                raise ArtifactRetentionError("retention receipt is malformed")
            receipts.append(RetentionReceipt.from_dict(value))
        return tuple(receipts)

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    relative_path TEXT NOT NULL UNIQUE,
                    retention_deadline REAL NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('active', 'deleted')),
                    delete_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delete_attempts >= 0),
                    retry_not_before REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    deleted_at REAL
                );
                CREATE TABLE IF NOT EXISTS legal_holds (
                    artifact_id TEXT PRIMARY KEY REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                    reason_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deletion_receipts (
                    artifact_id TEXT PRIMARY KEY REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
                    receipt_sha256 TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS retention_schedule (
                    schedule_name TEXT PRIMARY KEY,
                    last_run_at REAL NOT NULL
                );
                """
            )
        finally:
            connection.close()

    def _artifact_id(self, artifact_id: str) -> str:
        if not isinstance(artifact_id, str) or _ARTIFACT_ID.fullmatch(artifact_id) is None:
            raise ArtifactRetentionError("artifact identifier is invalid")
        return artifact_id

    def _normalize_relative_path(self, relative_path: str | Path) -> str:
        if not isinstance(relative_path, (str, Path)):
            raise ArtifactRetentionError("artifact path is invalid")
        path = Path(relative_path)
        if path.is_absolute():
            raise ArtifactRetentionError("artifact path must be relative to the artifact root")
        candidate = (self.artifact_root / path).resolve()
        try:
            normalized = candidate.relative_to(self.artifact_root)
        except ValueError as exc:
            raise ArtifactRetentionError("artifact path escapes the artifact root") from exc
        if not normalized.parts:
            raise ArtifactRetentionError("artifact path is invalid")
        protected_paths = {
            self.ledger_path,
            Path(f"{self.ledger_path}-journal"),
            Path(f"{self.ledger_path}-shm"),
            Path(f"{self.ledger_path}-wal"),
        }
        if candidate in protected_paths:
            raise ArtifactRetentionError("artifact path is reserved for retention state")
        return normalized.as_posix()

    def _positive_seconds(self, value: float | int, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ArtifactRetentionError(f"{label} is invalid")
        normalized = float(value)
        if not math.isfinite(normalized) or not 1 <= normalized <= 31_536_000:
            raise ArtifactRetentionError(f"{label} is invalid")
        return normalized

    def _schedule_due(self, timestamp: float, interval: float) -> bool:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT last_run_at FROM retention_schedule WHERE schedule_name='default'"
            ).fetchone()
            if row is None or timestamp >= float(row["last_run_at"]) + interval:
                return True
            retry = connection.execute(
                "SELECT 1 FROM artifacts WHERE state='active' AND delete_attempts > 0 "
                "AND retry_not_before <= ? LIMIT 1",
                (timestamp,),
            ).fetchone()
            return retry is not None
        finally:
            connection.close()

    def _due_candidates(self, timestamp: float, limit: int) -> tuple[list[str], int]:
        connection = self._connect()
        try:
            held = connection.execute(
                "SELECT COUNT(*) AS count FROM artifacts AS artifact "
                "WHERE artifact.state='active' AND artifact.retention_deadline <= ? "
                "AND EXISTS (SELECT 1 FROM legal_holds AS hold "
                "WHERE hold.artifact_id=artifact.artifact_id)",
                (timestamp,),
            ).fetchone()["count"]
            rows = connection.execute(
                "SELECT artifact_id FROM artifacts AS artifact "
                "WHERE artifact.state='active' AND artifact.retention_deadline <= ? "
                "AND artifact.retry_not_before <= ? "
                "AND NOT EXISTS (SELECT 1 FROM legal_holds AS hold "
                "WHERE hold.artifact_id=artifact.artifact_id) "
                "ORDER BY artifact.retention_deadline, artifact_id LIMIT ?",
                (timestamp, timestamp, limit),
            ).fetchall()
        finally:
            connection.close()
        return [str(row["artifact_id"]) for row in rows], int(held)

    def _delete_one(self, artifact_id: str, timestamp: float) -> tuple[str, RetentionReceipt | None]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT relative_path, retention_deadline, state, delete_attempts, retry_not_before "
                "FROM artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if (
                row is None
                or row["state"] != "active"
                or float(row["retention_deadline"]) > timestamp
                or float(row["retry_not_before"]) > timestamp
            ):
                return "skipped", None
            hold = connection.execute(
                "SELECT 1 FROM legal_holds WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            if hold is not None:
                return "held", None
            attempt = int(row["delete_attempts"]) + 1
            connection.execute(
                "UPDATE artifacts SET delete_attempts=? WHERE artifact_id=?", (attempt, artifact_id)
            )
            relative_path = str(row["relative_path"])
            artifact_path = self.artifact_root / self._normalize_relative_path(relative_path)
            try:
                content_sha256 = _file_sha256(artifact_path)
                self._unlinker(artifact_path)
                outcome = "deleted"
            except FileNotFoundError:
                content_sha256 = None
                outcome = "already_absent"
            except OSError:
                retry_delay = min(3600.0, 2.0 ** min(attempt, 12))
                connection.execute(
                    "UPDATE artifacts SET retry_not_before=? WHERE artifact_id=?",
                    (timestamp + retry_delay, artifact_id),
                )
                return "retry", None
            receipt = self._receipt(
                artifact_id=artifact_id,
                relative_path=relative_path,
                content_sha256=content_sha256,
                outcome=outcome,
                timestamp=timestamp,
                attempt=attempt,
            )
            connection.execute(
                "INSERT INTO deletion_receipts (artifact_id, receipt_sha256, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    artifact_id,
                    receipt.receipt_sha256,
                    _stable_json(receipt.as_dict()),
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE artifacts SET state='deleted', deleted_at=?, retry_not_before=0 "
                "WHERE artifact_id=?",
                (timestamp, artifact_id),
            )
        return outcome, receipt

    def _receipt(
        self,
        *,
        artifact_id: str,
        relative_path: str,
        content_sha256: str | None,
        outcome: str,
        timestamp: float,
        attempt: int,
    ) -> RetentionReceipt:
        artifact_id_sha256 = _sha256(artifact_id)
        artifact_path_sha256 = _sha256(relative_path)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "artifact_id_sha256": artifact_id_sha256,
            "artifact_path_sha256": artifact_path_sha256,
            "content_sha256": content_sha256,
            "deletion_outcome": outcome,
            "deleted_at": timestamp,
            "attempt": attempt,
        }
        return RetentionReceipt(
            receipt_sha256=_sha256(_stable_json(payload)),
            artifact_id_sha256=artifact_id_sha256,
            artifact_path_sha256=artifact_path_sha256,
            content_sha256=content_sha256,
            deletion_outcome=outcome,
            deleted_at=timestamp,
            attempt=attempt,
        )

    def _record_schedule(self, timestamp: float) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO retention_schedule (schedule_name, last_run_at) VALUES ('default', ?) "
                "ON CONFLICT(schedule_name) DO UPDATE SET last_run_at=excluded.last_run_at",
                (timestamp,),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.ledger_path), isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()
