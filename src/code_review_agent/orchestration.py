"""Concurrency and latency-budget primitives for review orchestration.

The OpenAI-compatible client is synchronous, so an in-flight HTTP request
cannot be safely killed from another Python thread.  Deadline is therefore a
soft, cooperative budget: loops refuse to start another request after expiry
and cap each request by the remaining budget.  The provider request may still
finish slightly after the deadline (for example while SDK retries unwind).
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import math
import time
from typing import Any, Callable

from code_review_agent.tracelog import tev


DEFAULT_REVIEW_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class Deadline:
    """One monotonic deadline shared by every lane in a review."""

    timeout_seconds: float
    expires_at: float
    _clock: Callable[[], float] = field(repr=False, compare=False)

    @classmethod
    def after(cls, seconds: float,
              clock: Callable[[], float] = time.monotonic) -> "Deadline":
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("review timeout must be a finite positive number")
        return cls(seconds, clock() + seconds, clock)

    def remaining(self) -> float:
        return max(0.0, self.expires_at - self._clock())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def request_timeout(self, request_cap: float) -> float:
        """Cap one request without returning an SDK-invalid zero timeout."""
        remaining = self.remaining()
        if remaining <= 0.0:
            return 0.0
        return max(0.001, min(float(request_cap), remaining))


@dataclass
class CallOutcome:
    """A lane result that preserves exceptions until both lanes are joined."""

    value: Any = None
    error: Exception | None = None


def _capture(call: Callable[[], Any]) -> CallOutcome:
    try:
        return CallOutcome(value=call())
    except Exception as exc:  # callers preserve their existing error policy
        return CallOutcome(error=exc)


def run_parallel_pair(first: Callable[[], Any], second: Callable[[], Any], *,
                      stage: str, trace=None) -> tuple[CallOutcome, CallOutcome]:
    """Run two independent review lanes concurrently and join both outcomes."""
    started = time.monotonic()
    tev(trace, "parallel_stage_started", stage=stage, lanes=2)
    with ThreadPoolExecutor(max_workers=2,
                            thread_name_prefix=f"crag-{stage}") as pool:
        futures = (pool.submit(_capture, first), pool.submit(_capture, second))
        outcomes = (futures[0].result(), futures[1].result())
    elapsed_ms = round((time.monotonic() - started) * 1000, 3)
    tev(trace, "parallel_stage_finished", stage=stage, lanes=2,
        elapsed_ms=elapsed_ms,
        errors=[type(outcome.error).__name__ if outcome.error else None
                for outcome in outcomes])
    return outcomes
