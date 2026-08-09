"""Run deterministic offline provider-failure and heartbeat load scenarios."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Mapping

from sqlalchemy import text

from code_review_agent.service_core import JobState, RepositoryRegistry, ReviewRequest, ReviewService
from code_review_agent.service_queue import JobStore, SCHEMA_VERSION
from code_review_agent.worker import ReviewWorker


RETRY_SCENARIOS = ("rate_limit", "provider_5xx", "cancelled_request")
LONG_RUNNING_SCENARIO = "long_running"


class HarnessError(RuntimeError):
    """Raised when a deterministic load assertion fails."""


class SimulatedRateLimitError(RuntimeError):
    status_code = 429
    headers = {"Retry-After": "0"}


class SimulatedProvider5xxError(RuntimeError):
    status_code = 503


class SimulatedCancellationTimeout(TimeoutError):
    """A request cancelled before the simulated provider has a side effect."""


def _diff(scenario: str, index: int) -> str:
    return (
        "diff --git a/load.py b/load.py\n"
        "--- a/load.py\n"
        "+++ b/load.py\n"
        "@@ -1 +1 @@\n"
        f"-scenario = '{scenario}-{index}'\n"
        f"+scenario = '{scenario}-{index}-complete'\n"
    )


def _write_trace(request: ReviewRequest, trace_path: Path) -> None:
    descriptor = os.open(trace_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "event": "simulated_provider_completed",
                    "review_id": request.job_id,
                },
                sort_keys=True,
            )
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())


class SimulatedProvider:
    """Local provider fake that fails once per retry scenario before succeeding."""

    def __init__(
        self,
        scenario_by_job: Mapping[str, str],
        *,
        long_wait_seconds: float,
    ) -> None:
        self._scenario_by_job = dict(scenario_by_job)
        self._long_wait_seconds = long_wait_seconds
        self._release_long = threading.Event()
        self.long_started = threading.Event()
        self._attempts: dict[str, list[int]] = defaultdict(list)
        self._side_effects: Counter[str] = Counter()
        self._lock = threading.Lock()

    def release_long_running_jobs(self) -> None:
        self._release_long.set()

    def attempts(self) -> dict[str, list[int]]:
        with self._lock:
            return {job_id: list(values) for job_id, values in self._attempts.items()}

    def side_effects(self) -> dict[str, int]:
        with self._lock:
            return dict(self._side_effects)

    def __call__(self, request: ReviewRequest, trace_path: Path) -> dict[str, Any]:
        scenario = self._scenario_by_job[request.job_id]
        with self._lock:
            self._attempts[request.job_id].append(request.attempt_count)
        if request.attempt_count == 1:
            if scenario == "rate_limit":
                raise SimulatedRateLimitError()
            if scenario == "provider_5xx":
                raise SimulatedProvider5xxError()
            if scenario == "cancelled_request":
                raise SimulatedCancellationTimeout()
        if scenario == LONG_RUNNING_SCENARIO:
            self.long_started.set()
            if not self._release_long.wait(self._long_wait_seconds):
                raise SimulatedCancellationTimeout()
        with self._lock:
            self._side_effects[request.job_id] += 1
            if self._side_effects[request.job_id] != 1:
                raise HarnessError("simulated provider side effect was repeated")
        _write_trace(request, trace_path)
        return {"summary": "simulated provider review", "findings": []}


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        raise HarnessError("lease timestamp is unavailable")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise HarnessError("lease timestamp is malformed") from exc


def _wait_for_state(
    store: JobStore,
    job_ids: list[str],
    *,
    timeout: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    records: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        records = [store.get(job_id) for job_id in job_ids]
        if all(record["state"] == JobState.AWAITING_APPROVAL.value for record in records):
            return records
        time.sleep(0.02)
    states = Counter(str(record["state"]) for record in records)
    raise HarnessError(f"load jobs did not finish: {dict(sorted(states.items()))}")


def _long_lease(store: JobStore, job_id: str) -> dict[str, Any]:
    with store.database.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state, attempt_count, heartbeat_at, lease_expires_at "
                "FROM review_jobs WHERE id=:job"
            ),
            {"job": job_id},
        ).one_or_none()
    if row is None:
        raise HarnessError("long-running job disappeared")
    return dict(row._mapping)


def _wait_for_long_start(store: JobStore, job_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = _long_lease(store, job_id)
        if record["state"] in {"leased", "running"} and record["lease_expires_at"] is not None:
            return record
        time.sleep(0.02)
    raise HarnessError("long-running job did not obtain a lease")


def _validate_arguments(
    *,
    jobs_per_scenario: int,
    workers: int,
    timeout: float,
    lease_seconds: float,
    heartbeat_seconds: float,
    long_observation_seconds: float,
) -> None:
    def finite_number(value: object, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite number")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError(f"{name} must be a finite number")
        return normalized

    if (
        isinstance(jobs_per_scenario, bool)
        or not isinstance(jobs_per_scenario, int)
        or not 1 <= jobs_per_scenario <= 32
    ):
        raise ValueError("jobs_per_scenario must be between 1 and 32")
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    timeout = finite_number(timeout, "timeout")
    lease_seconds = finite_number(lease_seconds, "lease_seconds")
    heartbeat_seconds = finite_number(heartbeat_seconds, "heartbeat_seconds")
    long_observation_seconds = finite_number(
        long_observation_seconds, "long_observation_seconds"
    )
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if not 1 <= lease_seconds <= 30:
        raise ValueError("lease_seconds must be between 1 and 30")
    if not 0.1 <= heartbeat_seconds < lease_seconds / 2:
        raise ValueError("heartbeat_seconds must be at least 0.1 and less than half the lease")
    if long_observation_seconds <= lease_seconds:
        raise ValueError("long_observation_seconds must exceed lease_seconds")
    if timeout <= long_observation_seconds:
        raise ValueError("timeout must exceed long_observation_seconds")


def run_harness(
    *,
    jobs_per_scenario: int = 4,
    workers: int = 2,
    timeout: float = 20.0,
    lease_seconds: float = 1.0,
    heartbeat_seconds: float = 0.1,
    long_observation_seconds: float = 1.2,
) -> dict[str, Any]:
    """Run all simulated scenarios and return an aggregate-only JSON-ready report."""
    _validate_arguments(
        jobs_per_scenario=jobs_per_scenario,
        workers=workers,
        timeout=timeout,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        long_observation_seconds=long_observation_seconds,
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repository = root / "repo"
        repository.mkdir()
        (repository / ".git").mkdir()
        registry = RepositoryRegistry.from_json(json.dumps({"load/repo": str(repository.resolve())}))
        store = JobStore(root / "state")
        service = ReviewService(registry, store, runner=None, local_mode=True)
        active_workers: list[ReviewWorker] = []
        provider: SimulatedProvider | None = None
        try:
            work = [
                (scenario, index)
                for scenario in RETRY_SCENARIOS
                for index in range(jobs_per_scenario)
            ]
            work.append((LONG_RUNNING_SCENARIO, 0))

            def submit(item: tuple[str, int]) -> tuple[str, str]:
                scenario, index = item
                record = service.submit_diff(
                    "load/repo",
                    _diff(scenario, index),
                    idempotency_key=f"issue42-{scenario}-{index}",
                )
                return scenario, str(record["review_id"])

            with ThreadPoolExecutor(max_workers=min(len(work), 16)) as executor:
                submitted = list(executor.map(submit, work))
            all_job_ids = [job_id for _, job_id in submitted]
            if len(set(all_job_ids)) != len(work):
                raise HarnessError("concurrent submission lost or duplicated a logical job")
            jobs_by_scenario: dict[str, list[str]] = defaultdict(list)
            scenario_by_job: dict[str, str] = {}
            for scenario, job_id in submitted:
                jobs_by_scenario[scenario].append(job_id)
                scenario_by_job[job_id] = scenario

            provider = SimulatedProvider(scenario_by_job, long_wait_seconds=timeout)
            for index in range(workers):
                worker = ReviewWorker(
                    registry,
                    store,
                    runner=provider,
                    worker_id=f"issue42-worker-{index}",
                    concurrency=2,
                    lease_seconds=lease_seconds,
                    heartbeat_seconds=heartbeat_seconds,
                    poll_seconds=0.05,
                    shutdown_grace_seconds=3.0,
                )
                active_workers.append(worker)
                worker.start()

            long_job_id = jobs_by_scenario[LONG_RUNNING_SCENARIO][0]
            if not provider.long_started.wait(timeout):
                raise HarnessError("long-running scenario did not start")
            initial_long_lease = _wait_for_long_start(store, long_job_id, timeout)
            initial_expiry = _utc(initial_long_lease["lease_expires_at"])
            time.sleep(long_observation_seconds)
            renewed_long_lease = _long_lease(store, long_job_id)
            renewed_expiry = _utc(renewed_long_lease["lease_expires_at"])
            if renewed_long_lease["state"] not in {"leased", "running"}:
                raise HarnessError("long-running job was not active during heartbeat observation")
            if int(renewed_long_lease["attempt_count"]) != 1:
                raise HarnessError("long-running job was retried during heartbeat observation")
            if renewed_long_lease["heartbeat_at"] is None or renewed_expiry <= initial_expiry:
                raise HarnessError("long-running job lease was not renewed")
            if renewed_expiry <= datetime.now(timezone.utc):
                raise HarnessError("long-running job lease expired despite heartbeat")
            provider.release_long_running_jobs()

            records = _wait_for_state(store, all_job_ids, timeout=timeout)
            expected_attempts = {
                "rate_limit": 2,
                "provider_5xx": 2,
                "cancelled_request": 2,
                LONG_RUNNING_SCENARIO: 1,
            }
            for scenario, job_ids in jobs_by_scenario.items():
                records_by_id = {str(record["review_id"]): record for record in records}
                if any(
                    int(records_by_id[job_id]["attempt_count"]) != expected_attempts[scenario]
                    for job_id in job_ids
                ):
                    raise HarnessError(f"{scenario} did not finish with the expected retry count")

            side_effects = provider.side_effects()
            if set(side_effects) != set(all_job_ids) or any(value != 1 for value in side_effects.values()):
                raise HarnessError("retry processing caused missing or duplicate simulated side effects")
            attempts = provider.attempts()
            if any(
                values != list(range(1, expected_attempts[scenario_by_job[job_id]] + 1))
                for job_id, values in attempts.items()
            ):
                raise HarnessError("simulated provider attempt sequence was not deterministic")

            with store.database.engine.connect() as connection:
                usage_rows = [
                    dict(row._mapping)
                    for row in connection.execute(
                        text(
                            "SELECT review_job_id, attempt_count, COUNT(*) AS count "
                            "FROM provider_usage GROUP BY review_job_id, attempt_count"
                        )
                    )
                ]
                retry_rows = [
                    dict(row._mapping)
                    for row in connection.execute(
                        text(
                            "SELECT reason_code, COUNT(*) AS count FROM audit_events "
                            "WHERE action='review.retry_scheduled' GROUP BY reason_code"
                        )
                    )
                ]
                lease_expired_events = int(
                    connection.execute(
                        text("SELECT COUNT(*) FROM audit_events WHERE action='review.lease_expired'")
                    ).scalar_one()
                )
            expected_usage_rows = sum(
                expected_attempts[scenario] * len(job_ids)
                for scenario, job_ids in jobs_by_scenario.items()
            )
            duplicate_usage_rows = sum(max(0, int(row["count"]) - 1) for row in usage_rows)
            if len(usage_rows) != expected_usage_rows or duplicate_usage_rows:
                raise HarnessError("provider usage is not exactly once per durable attempt")
            retry_counts = {str(row["reason_code"]): int(row["count"]) for row in retry_rows}
            expected_retry_counts = {
                "rate_limit": jobs_per_scenario,
                "provider_5xx": jobs_per_scenario,
                "transient_network": jobs_per_scenario,
            }
            if retry_counts != expected_retry_counts:
                raise HarnessError("retry audit categories do not match simulated provider failures")
            if lease_expired_events:
                raise HarnessError("heartbeat-protected long job produced a lease-expired event")

            return {
                "schema_version": "crag.issue42.provider-load/v1",
                "jobs_per_scenario": jobs_per_scenario,
                "total_jobs": len(all_job_ids),
                "workers": workers,
                "scenarios": {
                    scenario: {
                        "jobs": len(jobs_by_scenario[scenario]),
                        "attempts_per_job": expected_attempts[scenario],
                        "side_effects": len(jobs_by_scenario[scenario]),
                    }
                    for scenario in (*RETRY_SCENARIOS, LONG_RUNNING_SCENARIO)
                },
                "provider_usage_rows": len(usage_rows),
                "duplicate_usage_rows": duplicate_usage_rows,
                "heartbeat": {
                    "long_jobs": 1,
                    "renewed_long_jobs": 1,
                    "lease_expired_events": lease_expired_events,
                },
                "fake_provider": True,
                "passed": True,
            }
        finally:
            if provider is not None:
                provider.release_long_running_jobs()
            for worker in reversed(active_workers):
                worker.shutdown()
            service.shutdown()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-per-scenario", type=int, default=4)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--lease-seconds", type=float, default=1.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=0.1)
    parser.add_argument("--long-observation-seconds", type=float, default=1.2)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    result = run_harness(
        jobs_per_scenario=args.jobs_per_scenario,
        workers=args.workers,
        timeout=args.timeout,
        lease_seconds=args.lease_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        long_observation_seconds=args.long_observation_seconds,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
