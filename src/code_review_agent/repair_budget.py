"""Hard, restart-safe resource accounting for repair tasks."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import math
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
