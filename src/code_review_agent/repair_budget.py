"""Hard, restart-safe resource accounting for repair tasks."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from decimal import Decimal
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
from threading import Lock
from typing import Any
from uuid import uuid4


class BudgetError(RuntimeError):
    pass


class BudgetExceeded(BudgetError):
    def __init__(self, resource: str, limit: float, requested_total: float):
        self.resource = resource
        self.limit = limit
        self.requested_total = requested_total
        super().__init__(
            f"{resource} budget exceeded: limit={limit}, requested_total={requested_total}"
        )


class BudgetAccountingError(BudgetError):
    pass


class CohortLedgerError(BudgetAccountingError):
    """The persistent aggregate cost ledger cannot safely account for a call."""


class CohortLedgerCorrupt(CohortLedgerError):
    pass


class CohortLimitMismatch(CohortLedgerError):
    pass


def _real_number(name: str, value: Any) -> float:
    """Reject non-numeric types outright: a parseable string must not linger in
    the ledger only to break arithmetic (or comparisons) much later."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number, not {type(value).__name__}")
    return float(value)


def _positive_finite(name: str, value: float) -> None:
    number = _real_number(name, value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")


def _nonnegative_finite(name: str, value: float) -> None:
    number = _real_number(name, value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number")


def _nonnegative_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _complete_section(name: str, data: Any, section_type: Any) -> dict[str, Any]:
    """A restored snapshot section must carry every field: a truncated section
    silently reverting to defaults would reset usage or relax limits."""
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be an object")
    missing = sorted(item.name for item in fields(section_type) if item.name not in data)
    if missing:
        raise ValueError(f"{name} is missing required fields: {', '.join(missing)}")
    return data


@dataclass(frozen=True)
class BudgetLimits:
    total_seconds: float = 1_800.0
    total_tokens: int = 80_000
    total_cost_usd: float = 1.0
    tool_calls: int = 100
    repair_attempts: int = 2
    command_seconds: float = 300.0
    command_output_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        _positive_finite("total_seconds", self.total_seconds)
        _positive_finite("total_cost_usd", self.total_cost_usd)
        _positive_finite("command_seconds", self.command_seconds)
        for name in ("total_tokens", "tool_calls", "command_output_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        _nonnegative_int("repair_attempts", self.repair_attempts)


@dataclass
class BudgetUsage:
    elapsed_seconds: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    commands: int = 0
    repair_attempts: int = 0

    def validate(self) -> None:
        _nonnegative_finite("elapsed_seconds", self.elapsed_seconds)
        _nonnegative_finite("cost_usd", self.cost_usd)
        for name in ("tokens", "tool_calls", "commands", "repair_attempts"):
            _nonnegative_int(name, getattr(self, name))


@dataclass(frozen=True)
class LLMReservation:
    reservation_id: str
    tokens: int
    cost_usd: float

    def __post_init__(self) -> None:
        if not isinstance(self.reservation_id, str) or not self.reservation_id:
            raise ValueError("reservation_id must be a non-empty string")
        if isinstance(self.tokens, bool) or not isinstance(self.tokens, int) or self.tokens <= 0:
            raise ValueError("reservation tokens must be a positive integer")
        _nonnegative_finite("reservation cost_usd", self.cost_usd)


class BudgetManager:
    """Thread-safe accounting with pre-call reservations for parallel lanes."""

    def __init__(
        self,
        limits: BudgetLimits | None = None,
        usage: BudgetUsage | None = None,
        reservations: list[LLMReservation] | None = None,
        accounting_failure: str | None = None,
    ):
        self.limits = limits or BudgetLimits()
        self.usage = usage or BudgetUsage()
        self.usage.validate()
        self._reservations = {
            reservation.reservation_id: reservation for reservation in (reservations or [])
        }
        if len(self._reservations) != len(reservations or []):
            raise ValueError("duplicate LLM reservation id")
        self._accounting_failure = accounting_failure
        self._lock = Lock()
        self._assert_current_usage()

    def _reserved(self) -> tuple[int, float]:
        return (
            sum(item.tokens for item in self._reservations.values()),
            sum(item.cost_usd for item in self._reservations.values()),
        )

    @staticmethod
    def _raise_if_over(resource: str, limit: float, total: float) -> None:
        if total > limit:
            raise BudgetExceeded(resource, limit, total)

    def _assert_current_usage(self) -> None:
        reserved_tokens, reserved_cost = self._reserved()
        self._raise_if_over(
            "elapsed_seconds", self.limits.total_seconds, self.usage.elapsed_seconds
        )
        self._raise_if_over(
            "tokens", self.limits.total_tokens, self.usage.tokens + reserved_tokens
        )
        self._raise_if_over(
            "cost_usd", self.limits.total_cost_usd, self.usage.cost_usd + reserved_cost
        )
        self._raise_if_over("tool_calls", self.limits.tool_calls, self.usage.tool_calls)
        self._raise_if_over(
            "repair_attempts", self.limits.repair_attempts, self.usage.repair_attempts
        )

    def _ensure_healthy(self) -> None:
        if self._accounting_failure is not None:
            raise BudgetAccountingError(
                "budget accounting is fail-closed after a prior violation: "
                + self._accounting_failure
            )

    def reserve_llm(self, tokens: int, cost_usd: float) -> LLMReservation:
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            raise ValueError("reserved tokens must be a positive integer")
        _nonnegative_finite("reserved cost_usd", cost_usd)
        with self._lock:
            self._ensure_healthy()
            reserved_tokens, reserved_cost = self._reserved()
            self._raise_if_over(
                "tokens",
                self.limits.total_tokens,
                self.usage.tokens + reserved_tokens + tokens,
            )
            self._raise_if_over(
                "cost_usd",
                self.limits.total_cost_usd,
                self.usage.cost_usd + reserved_cost + cost_usd,
            )
            reservation = LLMReservation(uuid4().hex, tokens, float(cost_usd))
            self._reservations[reservation.reservation_id] = reservation
            return reservation

    def reconcile_llm(
        self,
        reservation_id: str,
        actual_tokens: int,
        actual_cost_usd: float,
    ) -> None:
        _nonnegative_int("actual_tokens", actual_tokens)
        _nonnegative_finite("actual_cost_usd", actual_cost_usd)
        with self._lock:
            self._ensure_healthy()
            try:
                reservation = self._reservations.pop(reservation_id)
            except KeyError as exc:
                raise BudgetAccountingError("unknown or already reconciled reservation") from exc
            self.usage.tokens += actual_tokens
            self.usage.cost_usd += float(actual_cost_usd)
            if actual_tokens > reservation.tokens or actual_cost_usd > reservation.cost_usd:
                self._accounting_failure = (
                    "actual LLM usage exceeded its pre-call reservation"
                )
                raise BudgetAccountingError(
                    "actual LLM usage exceeded its pre-call reservation; usage was retained"
                )
            self._assert_current_usage()

    def cancel_llm(self, reservation_id: str) -> None:
        with self._lock:
            self._ensure_healthy()
            if self._reservations.pop(reservation_id, None) is None:
                raise BudgetAccountingError("unknown or already reconciled reservation")

    def consume_elapsed(self, seconds: float) -> None:
        _nonnegative_finite("seconds", seconds)
        with self._lock:
            self._ensure_healthy()
            total = self.usage.elapsed_seconds + float(seconds)
            self._raise_if_over("elapsed_seconds", self.limits.total_seconds, total)
            self.usage.elapsed_seconds = total

    def consume_tool_call(self, count: int = 1, *, command: bool = False) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("tool-call count must be a positive integer")
        with self._lock:
            self._ensure_healthy()
            total = self.usage.tool_calls + count
            self._raise_if_over("tool_calls", self.limits.tool_calls, total)
            self.usage.tool_calls = total
            if command:
                self.usage.commands += count

    def consume_command(self, count: int = 1) -> None:
        """Record subprocesses nested inside one public tool invocation."""
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("command count must be a positive integer")
        with self._lock:
            self._ensure_healthy()
            self.usage.commands += count

    def consume_repair_attempt(self) -> None:
        with self._lock:
            self._ensure_healthy()
            total = self.usage.repair_attempts + 1
            self._raise_if_over("repair_attempts", self.limits.repair_attempts, total)
            self.usage.repair_attempts = total

    def remaining(self) -> dict[str, float]:
        with self._lock:
            reserved_tokens, reserved_cost = self._reserved()
            return {
                "elapsed_seconds": self.limits.total_seconds - self.usage.elapsed_seconds,
                "tokens": float(
                    self.limits.total_tokens - self.usage.tokens - reserved_tokens
                ),
                "cost_usd": self.limits.total_cost_usd - self.usage.cost_usd - reserved_cost,
                "tool_calls": float(self.limits.tool_calls - self.usage.tool_calls),
                "repair_attempts": float(
                    self.limits.repair_attempts - self.usage.repair_attempts
                ),
            }

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "limits": asdict(self.limits),
                "usage": asdict(self.usage),
                "reservations": [
                    asdict(item)
                    for item in sorted(
                        self._reservations.values(), key=lambda item: item.reservation_id
                    )
                ],
                "accounting_failure": self._accounting_failure,
            }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BudgetManager":
        try:
            limits = BudgetLimits(**_complete_section("limits", data["limits"], BudgetLimits))
            usage = BudgetUsage(**_complete_section("usage", data["usage"], BudgetUsage))
            reservations = [LLMReservation(**item) for item in data.get("reservations", [])]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid budget snapshot: {exc}") from exc
        accounting_failure = data.get("accounting_failure")
        if accounting_failure is not None and not isinstance(accounting_failure, str):
            raise ValueError("invalid budget snapshot: accounting_failure must be a string")
        return cls(limits, usage, reservations, accounting_failure)


_COHORT_LEDGER_SCHEMA = 1
_MICRO_USD_PER_USD = 1_000_000
_MAX_MICRO_USD = (1 << 63) - 1
_OVER_RESERVATION_FAILURE = "actual LLM cost exceeded its cohort reservation"
_LEDGER_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,127})\Z")
_WINDOWS_DEVICE_NAMES = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def _ledger_identifier(name: str, value: Any) -> str:
    if not isinstance(value, str) or _LEDGER_ID.fullmatch(value) is None:
        raise ValueError(
            f"{name} must be a lowercase ASCII identifier containing only ._-"
        )
    if value.endswith((".", " ")) or value.split(".", 1)[0] in _WINDOWS_DEVICE_NAMES:
        raise ValueError(f"{name} is unsafe as a portable identifier")
    return value


def _ledger_microusd(name: str, value: Any, *, positive: bool = False) -> int:
    """Convert a public USD value to an exact, bounded micro-USD integer.

    ``str(float)`` preserves the decimal value callers normally intended while
    exposing arithmetic artifacts (for example ``0.1 + 0.2``) as excess
    precision instead of silently rounding a hard budget.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{name} must be an int, float, or Decimal")
    number = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    if not number.is_finite() or number < 0:
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a finite {qualifier} number")
    scaled = number.scaleb(6)
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValueError(f"{name} must have at most six decimal places")
    microusd = int(integral)
    if positive and microusd <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    if microusd > _MAX_MICRO_USD:
        raise ValueError(f"{name} exceeds the supported micro-USD range")
    return microusd


def _stored_microusd(name: str, value: Any, *, positive: bool = False) -> int:
    """Validate the exact integer representation used by schema version 1."""
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer number of micro-USD")
    if value < 0 or (positive and value <= 0) or value > _MAX_MICRO_USD:
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a supported {qualifier} micro-USD amount")
    return value


def _usd_float(microusd: int) -> float:
    return microusd / _MICRO_USD_PER_USD


def _canonical_ledger_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _ledger_checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_ledger_json(value).encode("utf-8")).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _exact_keys(name: str, value: Any, required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CohortLedgerCorrupt(f"{name} must be an object")
    keys = set(value)
    if keys != required:
        missing = sorted(required - keys)
        unexpected = sorted(keys - required)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise CohortLedgerCorrupt(f"{name} has invalid fields ({'; '.join(details)})")
    return value


@dataclass(frozen=True)
class CohortCostReservation:
    run_id: str
    reservation_id: str
    cost_microusd: int

    def __post_init__(self) -> None:
        _ledger_identifier("run_id", self.run_id)
        _ledger_identifier("reservation_id", self.reservation_id)
        _stored_microusd("reservation cost_microusd", self.cost_microusd, positive=True)

    @property
    def cost_usd(self) -> float:
        return _usd_float(self.cost_microusd)


@dataclass(frozen=True)
class CohortCostFinalization:
    run_id: str
    reservation_id: str
    reserved_cost_microusd: int
    actual_cost_microusd: int
    outcome: str

    def __post_init__(self) -> None:
        _ledger_identifier("run_id", self.run_id)
        _ledger_identifier("reservation_id", self.reservation_id)
        _stored_microusd(
            "reserved_cost_microusd", self.reserved_cost_microusd, positive=True
        )
        _stored_microusd("actual_cost_microusd", self.actual_cost_microusd)
        if self.outcome not in {"reconciled", "cancelled"}:
            raise ValueError("finalization outcome must be reconciled or cancelled")
        if self.outcome == "cancelled" and self.actual_cost_microusd != 0:
            raise ValueError("a cancelled reservation cannot retain actual cost")

    @property
    def reserved_cost_usd(self) -> float:
        return _usd_float(self.reserved_cost_microusd)

    @property
    def actual_cost_usd(self) -> float:
        return _usd_float(self.actual_cost_microusd)


@dataclass(frozen=True)
class CohortCostSnapshot:
    cohort_id: str
    total_cost_microusd: int
    spent_microusd: int
    reservations: tuple[CohortCostReservation, ...] = ()
    finalized: tuple[CohortCostFinalization, ...] = ()
    accounting_failure: str | None = None

    @property
    def total_cost_usd(self) -> float:
        return _usd_float(self.total_cost_microusd)

    @property
    def spent_usd(self) -> float:
        return _usd_float(self.spent_microusd)

    @property
    def reserved_microusd(self) -> int:
        return sum(item.cost_microusd for item in self.reservations)

    @property
    def reserved_usd(self) -> float:
        return _usd_float(self.reserved_microusd)

    @property
    def remaining_usd(self) -> float:
        return _usd_float(
            self.total_cost_microusd - self.spent_microusd - self.reserved_microusd
        )


class _CohortFileLock:
    """Blocking advisory lock shared by every process using a cohort ledger."""

    def __init__(self, path: Path):
        self.path = path
        self._stream: Any = None

    def __enter__(self) -> "_CohortFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
        except OSError as exc:
            stream.close()
            raise CohortLedgerError(f"cannot lock aggregate cost ledger: {self.path}") from exc
        self._stream = stream
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        if self._stream is None:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    self._stream.fileno(), fcntl.LOCK_UN  # type: ignore[attr-defined]
                )
        finally:
            self._stream.close()
            self._stream = None


class CohortCostLedger:
    """Persistent LLM cost cap shared by all runs in an issue cohort.

    Every read-modify-write transaction is protected by an OS file lock and
    committed through ``os.replace``. Active reservations intentionally remain
    charged after a process crash until a caller explicitly reconciles or
    cancels them.
    """

    def __init__(
        self,
        state_root: str | Path,
        cohort_id: str,
        total_cost_usd: int | float | Decimal,
    ):
        self.state_root = Path(state_root)
        self.cohort_id = _ledger_identifier("cohort_id", cohort_id)
        self.total_cost_microusd = _ledger_microusd(
            "total_cost_usd", total_cost_usd, positive=True
        )
        self.path = self.state_root / f"{self.cohort_id}.cost.json"
        self.lock_path = self.state_root / f"{self.cohort_id}.cost.lock"
        self._thread_lock = Lock()

    @property
    def total_cost_usd(self) -> float:
        return _usd_float(self.total_cost_microusd)

    def _initial_snapshot(self) -> CohortCostSnapshot:
        return CohortCostSnapshot(self.cohort_id, self.total_cost_microusd, 0)

    @staticmethod
    def _reservation_from_dict(value: Any) -> CohortCostReservation:
        item = _exact_keys(
            "reservation", value, {"run_id", "reservation_id", "cost_microusd"}
        )
        try:
            return CohortCostReservation(**item)
        except (TypeError, ValueError) as exc:
            raise CohortLedgerCorrupt(f"invalid reservation: {exc}") from exc

    @staticmethod
    def _finalization_from_dict(value: Any) -> CohortCostFinalization:
        item = _exact_keys(
            "finalization",
            value,
            {
                "run_id",
                "reservation_id",
                "reserved_cost_microusd",
                "actual_cost_microusd",
                "outcome",
            },
        )
        try:
            return CohortCostFinalization(**item)
        except (TypeError, ValueError) as exc:
            raise CohortLedgerCorrupt(f"invalid finalization: {exc}") from exc

    def _decode(self, raw: str) -> CohortCostSnapshot:
        try:
            envelope_value = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number: {value}")
                ),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise CohortLedgerCorrupt(f"invalid aggregate cost ledger JSON: {exc}") from exc
        envelope = _exact_keys(
            "aggregate cost ledger envelope",
            envelope_value,
            {"schema_version", "checksum", "ledger"},
        )
        version = envelope["schema_version"]
        if type(version) is not int or version != _COHORT_LEDGER_SCHEMA:
            raise CohortLedgerCorrupt(f"unsupported aggregate ledger schema: {version!r}")
        checksum = envelope["checksum"]
        if not isinstance(checksum, str) or not hmac.compare_digest(
            checksum, _ledger_checksum(envelope["ledger"])
        ):
            raise CohortLedgerCorrupt("aggregate cost ledger checksum mismatch")
        ledger = _exact_keys(
            "aggregate cost ledger",
            envelope["ledger"],
            {
                "cohort_id",
                "total_cost_microusd",
                "spent_microusd",
                "reservations",
                "finalized",
                "accounting_failure",
            },
        )
        if not isinstance(ledger["reservations"], list) or not isinstance(
            ledger["finalized"], list
        ):
            raise CohortLedgerCorrupt("reservations and finalized must be arrays")
        try:
            cohort_id = _ledger_identifier("cohort_id", ledger["cohort_id"])
            total_cost_microusd = _stored_microusd(
                "total_cost_microusd", ledger["total_cost_microusd"], positive=True
            )
            spent_microusd = _stored_microusd(
                "spent_microusd", ledger["spent_microusd"]
            )
        except ValueError as exc:
            raise CohortLedgerCorrupt(f"invalid aggregate cost ledger: {exc}") from exc
        if cohort_id != self.cohort_id:
            raise CohortLedgerCorrupt("aggregate cost ledger cohort_id mismatch")
        if total_cost_microusd != self.total_cost_microusd:
            raise CohortLimitMismatch(
                "aggregate cost limit mismatch: "
                f"stored={_usd_float(total_cost_microusd)}, "
                f"configured={self.total_cost_usd}"
            )
        failure = ledger["accounting_failure"]
        if failure is not None and (not isinstance(failure, str) or not failure):
            raise CohortLedgerCorrupt("accounting_failure must be null or a non-empty string")
        reservations = tuple(
            self._reservation_from_dict(item) for item in ledger["reservations"]
        )
        finalized = tuple(
            self._finalization_from_dict(item) for item in ledger["finalized"]
        )
        identities = [
            (item.run_id, item.reservation_id) for item in reservations
        ] + [(item.run_id, item.reservation_id) for item in finalized]
        if len(identities) != len(set(identities)):
            raise CohortLedgerCorrupt("duplicate run_id/reservation_id identity")
        recorded_spent = sum(
            item.actual_cost_microusd
            for item in finalized
            if item.outcome == "reconciled"
        )
        if recorded_spent != spent_microusd:
            raise CohortLedgerCorrupt(
                "spent_microusd does not match finalized actual costs"
            )
        overruns = [
            item
            for item in finalized
            if item.outcome == "reconciled"
            and item.actual_cost_microusd > item.reserved_cost_microusd
        ]
        if overruns:
            if len(overruns) != 1 or finalized[-1] is not overruns[0]:
                raise CohortLedgerCorrupt(
                    "an over-reservation finalization must be the ledger's final event"
                )
            if failure != _OVER_RESERVATION_FAILURE:
                raise CohortLedgerCorrupt(
                    "over-reservation finalization requires the matching accounting failure"
                )
        elif failure is not None:
            raise CohortLedgerCorrupt(
                "accounting_failure has no matching over-reservation finalization"
            )
        snapshot = CohortCostSnapshot(
            cohort_id,
            total_cost_microusd,
            spent_microusd,
            reservations,
            finalized,
            failure,
        )
        accounted_microusd = snapshot.spent_microusd + snapshot.reserved_microusd
        if accounted_microusd > snapshot.total_cost_microusd:
            if snapshot.accounting_failure is None:
                raise CohortLedgerCorrupt("aggregate cost ledger exceeds its limit")
        return snapshot

    def _load_locked(self) -> CohortCostSnapshot:
        if not self.path.exists():
            return self._initial_snapshot()
        try:
            return self._decode(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CohortLedgerError(f"cannot read aggregate cost ledger: {self.path}") from exc

    @staticmethod
    def _payload(snapshot: CohortCostSnapshot) -> dict[str, Any]:
        return {
            "cohort_id": snapshot.cohort_id,
            "total_cost_microusd": snapshot.total_cost_microusd,
            "spent_microusd": snapshot.spent_microusd,
            "reservations": [asdict(item) for item in snapshot.reservations],
            "finalized": [asdict(item) for item in snapshot.finalized],
            "accounting_failure": snapshot.accounting_failure,
        }

    def _save_locked(self, snapshot: CohortCostSnapshot) -> None:
        payload = self._payload(snapshot)
        envelope = {
            "schema_version": _COHORT_LEDGER_SCHEMA,
            "checksum": _ledger_checksum(payload),
            "ledger": payload,
        }
        data = _canonical_ledger_json(envelope) + "\n"
        self.state_root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                descriptor = os.open(self.state_root, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _ensure_healthy(snapshot: CohortCostSnapshot) -> None:
        if snapshot.accounting_failure is not None:
            raise CohortLedgerError(
                "aggregate cost ledger is fail-closed after a prior violation: "
                + snapshot.accounting_failure
            )

    def snapshot(self) -> CohortCostSnapshot:
        with self._thread_lock, _CohortFileLock(self.lock_path):
            return self._load_locked()

    @staticmethod
    def _find(
        snapshot: CohortCostSnapshot, run_id: str, reservation_id: str
    ) -> tuple[CohortCostReservation | None, CohortCostFinalization | None]:
        identity = (run_id, reservation_id)
        reservation = next(
            (
                item
                for item in snapshot.reservations
                if (item.run_id, item.reservation_id) == identity
            ),
            None,
        )
        finalization = next(
            (
                item
                for item in snapshot.finalized
                if (item.run_id, item.reservation_id) == identity
            ),
            None,
        )
        return reservation, finalization

    def reserve(
        self,
        run_id: str,
        reservation_id: str,
        cost_usd: int | float | Decimal,
    ) -> CohortCostSnapshot:
        run_id = _ledger_identifier("run_id", run_id)
        reservation_id = _ledger_identifier("reservation_id", reservation_id)
        cost_microusd = _ledger_microusd("cost_usd", cost_usd, positive=True)
        with self._thread_lock, _CohortFileLock(self.lock_path):
            snapshot = self._load_locked()
            self._ensure_healthy(snapshot)
            existing, finalization = self._find(snapshot, run_id, reservation_id)
            if existing is not None:
                if existing.cost_microusd != cost_microusd:
                    raise CohortLedgerError(
                        "reservation replay changed the requested cost"
                    )
                return snapshot
            if finalization is not None:
                raise CohortLedgerError("finalized reservation cannot be replayed")
            requested_total = (
                snapshot.spent_microusd
                + snapshot.reserved_microusd
                + cost_microusd
            )
            if requested_total > snapshot.total_cost_microusd:
                raise BudgetExceeded(
                    "cohort_cost_usd",
                    snapshot.total_cost_usd,
                    _usd_float(requested_total),
                )
            updated = replace(
                snapshot,
                reservations=snapshot.reservations
                + (CohortCostReservation(run_id, reservation_id, cost_microusd),),
            )
            self._save_locked(updated)
            return updated

    def reconcile(
        self,
        run_id: str,
        reservation_id: str,
        actual_cost_usd: int | float | Decimal,
    ) -> CohortCostSnapshot:
        run_id = _ledger_identifier("run_id", run_id)
        reservation_id = _ledger_identifier("reservation_id", reservation_id)
        actual_cost_microusd = _ledger_microusd("actual_cost_usd", actual_cost_usd)
        with self._thread_lock, _CohortFileLock(self.lock_path):
            snapshot = self._load_locked()
            self._ensure_healthy(snapshot)
            reservation, finalization = self._find(snapshot, run_id, reservation_id)
            if finalization is not None:
                if (
                    finalization.outcome == "reconciled"
                    and finalization.actual_cost_microusd == actual_cost_microusd
                ):
                    return snapshot
                raise CohortLedgerError("reservation finalization replay mismatch")
            if reservation is None:
                raise CohortLedgerError("unknown reservation identity")
            failure = None
            if actual_cost_microusd > reservation.cost_microusd:
                failure = _OVER_RESERVATION_FAILURE
            updated = replace(
                snapshot,
                spent_microusd=snapshot.spent_microusd + actual_cost_microusd,
                reservations=tuple(
                    item
                    for item in snapshot.reservations
                    if item is not reservation
                ),
                finalized=snapshot.finalized
                + (
                    CohortCostFinalization(
                        run_id,
                        reservation_id,
                        reservation.cost_microusd,
                        actual_cost_microusd,
                        "reconciled",
                    ),
                ),
                accounting_failure=failure,
            )
            self._save_locked(updated)
            if failure is not None:
                raise CohortLedgerError(failure + "; actual cost was retained")
            return updated

    def cancel(self, run_id: str, reservation_id: str) -> CohortCostSnapshot:
        run_id = _ledger_identifier("run_id", run_id)
        reservation_id = _ledger_identifier("reservation_id", reservation_id)
        with self._thread_lock, _CohortFileLock(self.lock_path):
            snapshot = self._load_locked()
            self._ensure_healthy(snapshot)
            reservation, finalization = self._find(snapshot, run_id, reservation_id)
            if finalization is not None:
                if finalization.outcome == "cancelled":
                    return snapshot
                raise CohortLedgerError("reconciled reservation cannot be cancelled")
            if reservation is None:
                raise CohortLedgerError("unknown reservation identity")
            updated = replace(
                snapshot,
                reservations=tuple(
                    item
                    for item in snapshot.reservations
                    if item is not reservation
                ),
                finalized=snapshot.finalized
                + (
                    CohortCostFinalization(
                        run_id,
                        reservation_id,
                        reservation.cost_microusd,
                        0,
                        "cancelled",
                    ),
                ),
            )
            self._save_locked(updated)
            return updated
