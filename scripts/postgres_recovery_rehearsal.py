"""Plan and execute bounded Postgres backup/restore or failover rehearsals."""
from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT / "reliability" / "postgres-recovery-policy.json"
PLAN_SCHEMA_VERSION = "crag.postgres-recovery-plan/v1"
RESULT_SCHEMA_VERSION = "crag.postgres-recovery-result/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SERVICE_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_REHEARSAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_TABLE_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_UTC_SECOND = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_EXPECTED_TABLES = (
    "alembic_version",
    "organizations",
    "repositories",
    "review_jobs",
    "findings",
    "audit_events",
)
_PLAN_FIELDS = {
    "schema_version",
    "operation",
    "rehearsal_id",
    "source_service",
    "target_service",
    "source_fence_receipt_sha256",
    "failover_scope",
    "created_at_utc",
    "policy_sha256",
    "rpo_target_seconds",
    "rto_target_seconds",
    "plan_sha256",
}
_RESULT_FIELDS = {
    "schema_version",
    "evidence_kind",
    "operation",
    "status",
    "plan_sha256",
    "rehearsal_id_sha256",
    "source_service_sha256",
    "target_service_sha256",
    "source_fence_receipt_sha256",
    "started_at_utc",
    "completed_at_utc",
    "duration_seconds",
    "rpo_target_seconds",
    "rpo_observed_seconds",
    "rpo_passed",
    "rto_target_seconds",
    "rto_passed",
    "source_inventory_sha256",
    "target_inventory_sha256",
    "backup_sha256",
    "command_count",
    "raw_output_retained",
    "credentials_retained",
}


class RecoveryRehearsalError(RuntimeError):
    """A bounded recovery-rehearsal failure safe to expose to an operator."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str


@dataclass(frozen=True)
class RecoveryPolicy:
    rpo_seconds: int
    rto_seconds: int
    promotion_wait_seconds: int
    verification_tables: tuple[str, ...]
    canonical_sha256: str


CommandRunner = Callable[[tuple[str, ...]], CommandResult]
MonotonicClock = Callable[[], float]
UtcClock = Callable[[], datetime]


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha256(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _utc_second(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_utc_second(value: Any) -> str:
    if not isinstance(value, str) or _UTC_SECOND.fullmatch(value) is None:
        raise RecoveryRehearsalError("recovery timestamp is invalid")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise RecoveryRehearsalError("recovery timestamp is invalid") from exc
    return value


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryRehearsalError(f"{label} could not be loaded") from exc
    if not isinstance(value, dict):
        raise RecoveryRehearsalError(f"{label} must be a JSON object")
    return value


def _write_new_json(path: Path, value: dict[str, Any], label: str) -> str:
    payload = (_stable_json(value) + "\n").encode("utf-8")
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(payload)
        if os.name != "nt":
            target.chmod(0o600)
    except OSError as exc:
        raise RecoveryRehearsalError(f"{label} could not be written") from exc
    return _sha256(payload)


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> RecoveryPolicy:
    value = _read_json(path, "recovery policy")
    expected_fields = {
        "schema_version",
        "rpo_seconds",
        "rto_seconds",
        "promotion_wait_seconds",
        "backup_format",
        "require_clean_restore",
        "require_source_fence_receipt",
        "verification_tables",
    }
    if set(value) != expected_fields:
        raise RecoveryRehearsalError("recovery policy fields are invalid")
    tables = value.get("verification_tables")
    if (
        value.get("schema_version") != "crag.postgres-recovery-policy/v1"
        or value.get("rpo_seconds") != 900
        or value.get("rto_seconds") != 1800
        or value.get("promotion_wait_seconds") != 60
        or value.get("backup_format") != "custom"
        or value.get("require_clean_restore") is not True
        or value.get("require_source_fence_receipt") is not True
        or not isinstance(tables, list)
        or tuple(tables) != _EXPECTED_TABLES
        or not all(isinstance(table, str) and _TABLE_NAME.fullmatch(table) for table in tables)
    ):
        raise RecoveryRehearsalError("recovery policy values are invalid")
    return RecoveryPolicy(
        rpo_seconds=900,
        rto_seconds=1800,
        promotion_wait_seconds=60,
        verification_tables=tuple(tables),
        canonical_sha256=_sha256(_stable_json(value)),
    )


def build_plan(
    *,
    operation: str,
    rehearsal_id: str,
    source_service: str,
    target_service: str,
    policy: RecoveryPolicy,
    source_fence_receipt_sha256: str | None = None,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    if operation not in {"backup_restore", "failover"}:
        raise RecoveryRehearsalError("recovery operation is invalid")
    if not isinstance(rehearsal_id, str) or _REHEARSAL_ID.fullmatch(rehearsal_id) is None:
        raise RecoveryRehearsalError("rehearsal identifier is invalid")
    for service in (source_service, target_service):
        if not isinstance(service, str) or _SERVICE_ALIAS.fullmatch(service) is None:
            raise RecoveryRehearsalError("libpq service alias is invalid")
    if source_service == target_service:
        raise RecoveryRehearsalError("source and target service aliases must differ")
    if operation == "failover":
        if (
            not isinstance(source_fence_receipt_sha256, str)
            or _SHA256.fullmatch(source_fence_receipt_sha256) is None
        ):
            raise RecoveryRehearsalError("failover requires a source fence receipt SHA-256")
        failover_scope: str | None = "isolated_rehearsal"
    else:
        if source_fence_receipt_sha256 is not None:
            raise RecoveryRehearsalError("backup restore cannot bind a source fence receipt")
        failover_scope = None
    timestamp = _validate_utc_second(created_at_utc or _utc_second(datetime.now(timezone.utc)))
    core = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "operation": operation,
        "rehearsal_id": rehearsal_id,
        "source_service": source_service,
        "target_service": target_service,
        "source_fence_receipt_sha256": source_fence_receipt_sha256,
        "failover_scope": failover_scope,
        "created_at_utc": timestamp,
        "policy_sha256": policy.canonical_sha256,
        "rpo_target_seconds": policy.rpo_seconds,
        "rto_target_seconds": policy.rto_seconds,
    }
    return {**core, "plan_sha256": _sha256(_stable_json(core))}


def validate_plan(
    value: dict[str, Any],
    *,
    policy: RecoveryPolicy,
    confirmation_sha256: str,
) -> dict[str, Any]:
    if set(value) != _PLAN_FIELDS:
        raise RecoveryRehearsalError("recovery plan fields are invalid")
    plan_sha256 = value.get("plan_sha256")
    if not isinstance(plan_sha256, str) or _SHA256.fullmatch(plan_sha256) is None:
        raise RecoveryRehearsalError("recovery plan SHA-256 is invalid")
    if confirmation_sha256 != plan_sha256:
        raise RecoveryRehearsalError("recovery plan confirmation does not match")
    core = {key: value[key] for key in sorted(_PLAN_FIELDS - {"plan_sha256"})}
    if _sha256(_stable_json(core)) != plan_sha256:
        raise RecoveryRehearsalError("recovery plan content does not match its SHA-256")
    operation = value.get("operation")
    rehearsal_id = value.get("rehearsal_id")
    source_service = value.get("source_service")
    target_service = value.get("target_service")
    if (
        not isinstance(operation, str)
        or not isinstance(rehearsal_id, str)
        or not isinstance(source_service, str)
        or not isinstance(target_service, str)
    ):
        raise RecoveryRehearsalError("recovery plan values are invalid")
    rebuilt = build_plan(
        operation=operation,
        rehearsal_id=rehearsal_id,
        source_service=source_service,
        target_service=target_service,
        policy=policy,
        source_fence_receipt_sha256=value.get("source_fence_receipt_sha256"),
        created_at_utc=value.get("created_at_utc"),
    )
    if rebuilt != value or value.get("policy_sha256") != policy.canonical_sha256:
        raise RecoveryRehearsalError("recovery plan does not match the active policy")
    return value


def _default_runner(command: tuple[str, ...]) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryRehearsalError("postgres recovery command could not complete") from exc
    return CommandResult(returncode=completed.returncode, stdout=completed.stdout)


class PostgresRecoveryRehearsal:
    """Execute a validated recovery plan through a bounded command runner."""

    def __init__(
        self,
        *,
        policy: RecoveryPolicy,
        runner: CommandRunner = _default_runner,
        monotonic: MonotonicClock = time.monotonic,
        utc_now: UtcClock | None = None,
    ) -> None:
        self.policy = policy
        self.runner = runner
        self.monotonic = monotonic
        self.utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self.command_count = 0

    def execute(
        self,
        plan: dict[str, Any],
        *,
        confirmation_sha256: str,
        artifact_directory: Path,
        evidence_kind: str,
    ) -> dict[str, Any]:
        validated = validate_plan(
            plan,
            policy=self.policy,
            confirmation_sha256=confirmation_sha256,
        )
        if evidence_kind not in {"offline_fake", "operator_executed"}:
            raise RecoveryRehearsalError("recovery evidence kind is invalid")
        started_at = _utc_second(self.utc_now())
        started = self.monotonic()
        operation = validated["operation"]
        if operation == "backup_restore":
            result_parts = self._backup_restore(validated, Path(artifact_directory))
        else:
            result_parts = self._failover(validated)
        duration = self.monotonic() - started
        if not math.isfinite(duration) or duration < 0 or duration > self.policy.rto_seconds:
            raise RecoveryRehearsalError("recovery rehearsal exceeded the RTO target")
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "evidence_kind": evidence_kind,
            "operation": operation,
            "status": "passed",
            "plan_sha256": validated["plan_sha256"],
            "rehearsal_id_sha256": _sha256(validated["rehearsal_id"]),
            "source_service_sha256": _sha256(validated["source_service"]),
            "target_service_sha256": _sha256(validated["target_service"]),
            "source_fence_receipt_sha256": validated["source_fence_receipt_sha256"],
            "started_at_utc": started_at,
            "completed_at_utc": _utc_second(self.utc_now()),
            "duration_seconds": round(duration, 6),
            "rpo_target_seconds": self.policy.rpo_seconds,
            "rpo_observed_seconds": result_parts["rpo_observed_seconds"],
            "rpo_passed": result_parts["rpo_passed"],
            "rto_target_seconds": self.policy.rto_seconds,
            "rto_passed": True,
            "source_inventory_sha256": result_parts["source_inventory_sha256"],
            "target_inventory_sha256": result_parts["target_inventory_sha256"],
            "backup_sha256": result_parts["backup_sha256"],
            "command_count": self.command_count,
            "raw_output_retained": False,
            "credentials_retained": False,
        }
        validate_result(result)
        return result

    def _backup_restore(
        self,
        plan: dict[str, Any],
        artifact_directory: Path,
    ) -> dict[str, Any]:
        source_inventory = self._inventory(plan["source_service"])
        clean = self._query_json(plan["target_service"], self._clean_target_sql())
        if set(clean) != {"public_table_count"} or clean.get("public_table_count") != 0:
            raise RecoveryRehearsalError("restore target is not a clean database")
        try:
            artifact_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RecoveryRehearsalError("backup artifact directory could not be prepared") from exc
        dump_path = artifact_directory / f"recovery-{_sha256(plan['rehearsal_id'])[:16]}.dump"
        if dump_path.exists():
            raise RecoveryRehearsalError("backup artifact already exists")
        self._run(
            (
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--file",
                str(dump_path),
                "--dbname",
                f"service={plan['source_service']}",
            )
        )
        try:
            if not dump_path.is_file() or dump_path.stat().st_size < 1:
                raise RecoveryRehearsalError("backup artifact was not created")
            if os.name != "nt":
                dump_path.chmod(0o600)
        except OSError as exc:
            raise RecoveryRehearsalError("backup artifact could not be verified") from exc
        self._run(
            (
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                f"service={plan['target_service']}",
                str(dump_path),
            )
        )
        target_inventory = self._inventory(plan["target_service"])
        self._require_matching_inventory(source_inventory, target_inventory)
        return {
            "rpo_observed_seconds": None,
            "rpo_passed": None,
            "source_inventory_sha256": _sha256(_stable_json(source_inventory)),
            "target_inventory_sha256": _sha256(_stable_json(target_inventory)),
            "backup_sha256": self._file_sha256(dump_path),
        }

    def _failover(self, plan: dict[str, Any]) -> dict[str, Any]:
        source_inventory = self._inventory(plan["source_service"])
        source_status = self._query_json(plan["source_service"], self._post_promotion_sql())
        if set(source_status) != {"in_recovery"} or source_status.get("in_recovery") is not False:
            raise RecoveryRehearsalError("failover source is not a primary")
        standby = self._query_json(plan["target_service"], self._standby_sql())
        if set(standby) != {"in_recovery", "replay_lag_seconds"}:
            raise RecoveryRehearsalError("standby preflight output is invalid")
        if standby.get("in_recovery") is not True:
            raise RecoveryRehearsalError("failover target is not a standby")
        replay_lag = standby.get("replay_lag_seconds")
        if (
            isinstance(replay_lag, bool)
            or not isinstance(replay_lag, (int, float))
            or not math.isfinite(float(replay_lag))
            or float(replay_lag) < 0
            or float(replay_lag) > self.policy.rpo_seconds
        ):
            raise RecoveryRehearsalError("standby replay lag exceeds the RPO target")
        target_before = self._inventory(plan["target_service"])
        self._require_matching_inventory(source_inventory, target_before)
        promotion = self._run(
            self._psql_command(
                plan["target_service"],
                "SELECT CASE WHEN pg_promote(wait => true, wait_seconds => "
                f"{self.policy.promotion_wait_seconds}) THEN 'promoted' "
                "ELSE 'not_promoted' END;",
            )
        )
        if [line.strip() for line in promotion.splitlines() if line.strip()] != ["promoted"]:
            raise RecoveryRehearsalError("standby promotion was not confirmed")
        postflight = self._query_json(plan["target_service"], self._post_promotion_sql())
        if set(postflight) != {"in_recovery"} or postflight.get("in_recovery") is not False:
            raise RecoveryRehearsalError("promoted target still reports recovery mode")
        probe = self._run(self._psql_command(plan["target_service"], self._write_probe_sql()))
        lines = [line.strip() for line in probe.splitlines() if line.strip()]
        if "probe_ok" not in lines:
            raise RecoveryRehearsalError("promoted target write/read probe failed")
        target_after = self._inventory(plan["target_service"])
        self._require_matching_inventory(source_inventory, target_after)
        return {
            "rpo_observed_seconds": round(float(replay_lag), 6),
            "rpo_passed": True,
            "source_inventory_sha256": _sha256(_stable_json(source_inventory)),
            "target_inventory_sha256": _sha256(_stable_json(target_after)),
            "backup_sha256": None,
        }

    def _run(self, command: tuple[str, ...]) -> str:
        self.command_count += 1
        try:
            result = self.runner(command)
        except RecoveryRehearsalError:
            raise
        except Exception as exc:
            raise RecoveryRehearsalError("postgres recovery command could not complete") from exc
        if not isinstance(result, CommandResult) or result.returncode != 0:
            raise RecoveryRehearsalError("postgres recovery command failed")
        if not isinstance(result.stdout, str):
            raise RecoveryRehearsalError("postgres recovery command output is invalid")
        return result.stdout

    def _query_json(self, service: str, sql: str) -> dict[str, Any]:
        output = self._run(self._psql_command(service, sql))
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if len(lines) != 1:
            raise RecoveryRehearsalError("postgres recovery query output is invalid")
        try:
            value = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise RecoveryRehearsalError("postgres recovery query output is invalid") from exc
        if not isinstance(value, dict):
            raise RecoveryRehearsalError("postgres recovery query output is invalid")
        return value

    def _inventory(self, service: str) -> dict[str, Any]:
        value = self._query_json(service, self._inventory_sql())
        if set(value) != {"alembic_heads", "table_rows"}:
            raise RecoveryRehearsalError("database inventory output is invalid")
        heads = value.get("alembic_heads")
        rows = value.get("table_rows")
        if (
            not isinstance(heads, list)
            or not heads
            or not all(isinstance(head, str) and head for head in heads)
            or not isinstance(rows, dict)
            or set(rows) != set(self.policy.verification_tables)
            or not all(
                isinstance(count, int) and not isinstance(count, bool) and count >= 0
                for count in rows.values()
            )
        ):
            raise RecoveryRehearsalError("database inventory output is invalid")
        return {"alembic_heads": sorted(heads), "table_rows": dict(sorted(rows.items()))}

    @staticmethod
    def _require_matching_inventory(source: dict[str, Any], target: dict[str, Any]) -> None:
        if source != target:
            raise RecoveryRehearsalError("restored or promoted inventory does not match source")

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise RecoveryRehearsalError("backup artifact could not be hashed") from exc
        return digest.hexdigest()

    @staticmethod
    def _psql_command(service: str, sql: str) -> tuple[str, ...]:
        return (
            "psql",
            "--no-psqlrc",
            "--quiet",
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--dbname",
            f"service={service}",
            "--command",
            sql,
        )

    def _inventory_sql(self) -> str:
        row_pairs = ", ".join(
            f"'{table}', (SELECT COUNT(*) FROM {table})"
            for table in self.policy.verification_tables
        )
        return (
            "SELECT json_build_object("
            "'alembic_heads', COALESCE((SELECT json_agg(version_num ORDER BY version_num) "
            "FROM alembic_version), '[]'::json), "
            f"'table_rows', json_build_object({row_pairs}))::text;"
        )

    @staticmethod
    def _clean_target_sql() -> str:
        return (
            "SELECT json_build_object('public_table_count', COUNT(*))::text "
            "FROM information_schema.tables WHERE table_schema='public';"
        )

    @staticmethod
    def _standby_sql() -> str:
        return (
            "SELECT json_build_object("
            "'in_recovery', pg_is_in_recovery(), "
            "'replay_lag_seconds', CASE WHEN pg_last_xact_replay_timestamp() IS NULL "
            "THEN NULL ELSE GREATEST(0, EXTRACT(EPOCH FROM "
            "clock_timestamp() - pg_last_xact_replay_timestamp())) END)::text;"
        )

    @staticmethod
    def _post_promotion_sql() -> str:
        return "SELECT json_build_object('in_recovery', pg_is_in_recovery())::text;"

    @staticmethod
    def _write_probe_sql() -> str:
        return (
            "BEGIN; "
            "CREATE TEMP TABLE crag_recovery_probe(value text NOT NULL); "
            "INSERT INTO crag_recovery_probe(value) VALUES ('bounded-probe'); "
            "SELECT CASE WHEN COUNT(*)=1 THEN 'probe_ok' ELSE 'probe_failed' END "
            "FROM crag_recovery_probe; ROLLBACK;"
        )


def validate_result(value: dict[str, Any]) -> None:
    if set(value) != _RESULT_FIELDS:
        raise RecoveryRehearsalError("recovery result fields are invalid")
    required_hashes = (
        value.get("plan_sha256"),
        value.get("rehearsal_id_sha256"),
        value.get("source_service_sha256"),
        value.get("target_service_sha256"),
        value.get("source_inventory_sha256"),
        value.get("target_inventory_sha256"),
    )
    if not all(isinstance(item, str) and _SHA256.fullmatch(item) for item in required_hashes):
        raise RecoveryRehearsalError("recovery result hashes are invalid")
    optional_hashes = (
        value.get("source_fence_receipt_sha256"),
        value.get("backup_sha256"),
    )
    if not all(item is None or (isinstance(item, str) and _SHA256.fullmatch(item)) for item in optional_hashes):
        raise RecoveryRehearsalError("recovery result hashes are invalid")
    duration = value.get("duration_seconds")
    command_count = value.get("command_count")
    if (
        value.get("schema_version") != RESULT_SCHEMA_VERSION
        or value.get("evidence_kind") not in {"offline_fake", "operator_executed"}
        or value.get("operation") not in {"backup_restore", "failover"}
        or value.get("status") != "passed"
        or value.get("rpo_target_seconds") != 900
        or value.get("rto_target_seconds") != 1800
        or value.get("rto_passed") is not True
        or value.get("raw_output_retained") is not False
        or value.get("credentials_retained") is not False
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or not 0 <= float(duration) <= 86400
        or isinstance(command_count, bool)
        or not isinstance(command_count, int)
        or not 1 <= command_count <= 64
    ):
        raise RecoveryRehearsalError("recovery result values are invalid")
    _validate_utc_second(value.get("started_at_utc"))
    _validate_utc_second(value.get("completed_at_utc"))
    if value["operation"] == "backup_restore":
        if (
            value.get("source_fence_receipt_sha256") is not None
            or value.get("backup_sha256") is None
            or value.get("rpo_observed_seconds") is not None
            or value.get("rpo_passed") is not None
        ):
            raise RecoveryRehearsalError("backup restore result values are invalid")
    else:
        observed = value.get("rpo_observed_seconds")
        if (
            value.get("source_fence_receipt_sha256") is None
            or value.get("backup_sha256") is not None
            or value.get("rpo_passed") is not True
            or isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not 0 <= float(observed) <= 900
        ):
            raise RecoveryRehearsalError("failover result values are invalid")


def write_plan(path: Path, plan: dict[str, Any]) -> str:
    """Write a frozen plan to a new file and return its artifact SHA-256."""
    if set(plan) != _PLAN_FIELDS:
        raise RecoveryRehearsalError("recovery plan fields are invalid")
    return _write_new_json(path, plan, "recovery plan")


def write_result(path: Path, result: dict[str, Any]) -> str:
    """Validate and write a successful result to a new file."""
    validate_result(result)
    return _write_new_json(path, result, "recovery result")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="freeze a recovery rehearsal plan")
    plan.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    plan.add_argument("--operation", choices=("backup_restore", "failover"), required=True)
    plan.add_argument("--rehearsal-id", required=True)
    plan.add_argument("--source-service", required=True)
    plan.add_argument("--target-service", required=True)
    plan.add_argument("--source-fence-receipt-sha256")
    plan.add_argument("--created-at-utc")
    plan.add_argument("--output", type=Path, required=True)

    execute = commands.add_parser("execute", help="execute an exact frozen plan")
    execute.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--confirmation-sha256", required=True)
    execute.add_argument("--artifact-directory", type=Path, required=True)
    execute.add_argument("--result-output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        if args.command == "plan":
            plan = build_plan(
                operation=args.operation,
                rehearsal_id=args.rehearsal_id,
                source_service=args.source_service,
                target_service=args.target_service,
                policy=policy,
                source_fence_receipt_sha256=args.source_fence_receipt_sha256,
                created_at_utc=args.created_at_utc,
            )
            artifact_sha256 = write_plan(args.output, plan)
            summary = {
                "schema_version": "crag.postgres-recovery-plan-receipt/v1",
                "operation": plan["operation"],
                "plan_sha256": plan["plan_sha256"],
                "plan_artifact_sha256": artifact_sha256,
            }
        else:
            plan = _read_json(args.plan, "recovery plan")
            rehearsal = PostgresRecoveryRehearsal(policy=policy)
            result = rehearsal.execute(
                plan,
                confirmation_sha256=args.confirmation_sha256,
                artifact_directory=args.artifact_directory,
                evidence_kind="operator_executed",
            )
            result_sha256 = write_result(args.result_output, result)
            summary = {
                "schema_version": "crag.postgres-recovery-result-receipt/v1",
                "operation": result["operation"],
                "status": result["status"],
                "plan_sha256": result["plan_sha256"],
                "result_artifact_sha256": result_sha256,
            }
    except RecoveryRehearsalError as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
