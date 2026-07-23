"""Offline Phase 9C concurrency/load acceptance with deterministic fake work."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from sqlalchemy import text

from code_review_agent.service_core import JobState, RepositoryRegistry, ReviewService
from code_review_agent.service_queue import JobStore
from code_review_agent.worker import FakeReviewRunner, ReviewWorker


def _diff(index: int) -> str:
    return (
        "diff --git a/load.py b/load.py\n"
        "--- a/load.py\n"
        "+++ b/load.py\n"
        "@@ -1 +1 @@\n"
        f"-value = {index}\n"
        f"+value = {index + 1}\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the offline Phase 9C load gate")
    parser.add_argument("--submissions", type=int, default=50)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not 1 <= args.submissions <= 1000:
        raise SystemExit("--submissions must be between 1 and 1000")
    if not 1 <= args.workers <= 8:
        raise SystemExit("--workers must be between 1 and 8")
    concurrency = args.concurrency if args.concurrency is not None else min(args.submissions, 50)
    if not 1 <= concurrency <= min(args.submissions, 256):
        raise SystemExit("--concurrency must be between 1 and min(submissions, 256)")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repo = root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        registry = RepositoryRegistry.from_json(
            json.dumps({"load/repo": str(repo.resolve())})
        )
        database_url = os.environ.get("CRAG_TEST_POSTGRES_URL")
        api_store = JobStore(
            root / "state",
            database_url=database_url,
            auto_migrate=database_url is None,
        )
        if database_url is not None:
            api_store.bootstrap_local(("load/repo",))
        service = ReviewService(registry, api_store, local_mode=True)
        worker_stores: list[JobStore] = []
        workers: list[ReviewWorker] = []
        try:
            started = time.monotonic()

            start_submissions = threading.Event()

            def submit(index: int):
                start_submissions.wait()
                return service.submit_diff(
                    "load/repo",
                    _diff(index),
                    idempotency_key=f"load-{index}",
                )

            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [executor.submit(submit, index) for index in range(args.submissions)]
                start_submissions.set()
                submitted = [future.result() for future in futures]
            submission_seconds = time.monotonic() - started
            job_ids = {item["review_id"] for item in submitted}
            if len(job_ids) != args.submissions:
                raise RuntimeError("concurrent submission lost or duplicated logical jobs")

            start_submissions.clear()
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [executor.submit(submit, index) for index in range(args.submissions)]
                start_submissions.set()
                replay = [future.result() for future in futures]
            if not all(item["duplicate"] for item in replay):
                raise RuntimeError("idempotency replay created new logical work")
            if {item["review_id"] for item in replay} != job_ids:
                raise RuntimeError("idempotency replay changed review identity")

            for index in range(args.workers):
                store = JobStore(
                    root / "state",
                    database_url=api_store.database_url,
                    auto_migrate=False,
                )
                worker = ReviewWorker(
                    registry,
                    store,
                    runner=FakeReviewRunner(),
                    worker_id=f"load-worker-{index}",
                    concurrency=2,
                    lease_seconds=5,
                    heartbeat_seconds=0.25,
                    poll_seconds=0.05,
                    shutdown_grace_seconds=3,
                )
                worker_stores.append(store)
                workers.append(worker)
                worker.start()

            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                states = [api_store.get(job_id)["state"] for job_id in job_ids]
                if all(state == JobState.AWAITING_APPROVAL.value for state in states):
                    break
                time.sleep(0.02)
            else:
                counts: dict[str, int] = {}
                for job_id in job_ids:
                    state = api_store.get(job_id)["state"]
                    counts[state] = counts.get(state, 0) + 1
                raise RuntimeError(f"load jobs did not finish: {counts}")

            with api_store.database.engine.connect() as connection:
                job_count = int(
                    connection.execute(text("SELECT COUNT(*) FROM review_jobs")).scalar_one()
                )
                attempt_rows = int(
                    connection.execute(text("SELECT COUNT(*) FROM provider_usage")).scalar_one()
                )
                duplicate_attempts = int(
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM (SELECT review_job_id, attempt_count, "
                            "COUNT(*) AS n FROM provider_usage GROUP BY review_job_id, "
                            "attempt_count HAVING COUNT(*)>1) AS duplicate_attempts"
                        )
                    ).scalar_one()
                )
            if job_count != args.submissions or attempt_rows != args.submissions:
                raise RuntimeError("job/attempt accounting is not one-to-one")
            if duplicate_attempts != 0:
                raise RuntimeError("a job attempt was committed more than once")
            if (api_store.state_dir / ".service.lock").exists():
                raise RuntimeError("Phase 9C unexpectedly created a state-directory lock")

            print(
                json.dumps(
                    {
                        "schema_version": "crag.phase9c.load/v1",
                        "submissions": args.submissions,
                        "concurrency": concurrency,
                        "workers": args.workers,
                        "logical_jobs": job_count,
                        "attempt_rows": attempt_rows,
                        "duplicate_attempts": duplicate_attempts,
                        "submission_seconds": round(submission_seconds, 6),
                        "total_seconds": round(time.monotonic() - started, 6),
                        "passed": True,
                    },
                    sort_keys=True,
                )
            )
        finally:
            for worker in reversed(workers):
                worker.shutdown()
            for store in reversed(worker_stores):
                store.close()
            service.shutdown()


if __name__ == "__main__":
    main()
