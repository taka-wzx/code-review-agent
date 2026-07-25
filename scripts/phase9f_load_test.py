"""Offline Phase 9F multi-worker metrics load gate using only fake review work."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time

from code_review_agent.service_core import JobState, RepositoryRegistry, ReviewService
from code_review_agent.service_queue import JobStore
from code_review_agent.worker import FakeReviewRunner, ReviewWorker


def _diff(index: int) -> str:
    return (
        "diff --git a/load.py b/load.py\n--- a/load.py\n+++ b/load.py\n@@ -1 +1 @@\n"
        f"-value = {index}\n+value = {index + 1}\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the offline Phase 9F metrics load gate")
    parser.add_argument("--submissions", type=int, default=50)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not 1 <= args.submissions <= 1000:
        raise SystemExit("--submissions must be between 1 and 1000")
    if not 1 <= args.workers <= 8:
        raise SystemExit("--workers must be between 1 and 8")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repository = root / "repo"
        repository.mkdir()
        (repository / ".git").mkdir()
        registry = RepositoryRegistry.from_json(
            json.dumps({"load/repo": str(repository.resolve())})
        )
        api_store = JobStore(root / "state")
        service = ReviewService(registry, api_store, runner=None, local_mode=True)
        worker_stores: list[JobStore] = []
        workers: list[ReviewWorker] = []
        try:
            started = time.monotonic()

            def submit(index: int) -> dict:
                return service.submit_diff(
                    "load/repo",
                    _diff(index),
                    idempotency_key=f"phase9f-load-{index}",
                )

            with ThreadPoolExecutor(max_workers=min(50, args.submissions)) as executor:
                submitted = list(executor.map(submit, range(args.submissions)))
            job_ids = {str(item["review_id"]) for item in submitted}
            if len(job_ids) != args.submissions:
                raise RuntimeError("concurrent submission lost or duplicated logical jobs")

            with ThreadPoolExecutor(max_workers=min(50, args.submissions)) as executor:
                replay = list(executor.map(submit, range(args.submissions)))
            if not all(item["duplicate"] for item in replay):
                raise RuntimeError("idempotency replay created new logical work")

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
                    worker_id=f"phase9f-load-worker-{index}",
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
                if all(
                    api_store.get(job_id)["state"] == JobState.AWAITING_APPROVAL.value
                    for job_id in job_ids
                ):
                    break
                time.sleep(0.02)
            else:
                raise RuntimeError("load jobs did not finish before the deadline")

            scrape = service.metrics.render()
            expected_outcome = (
                f'review_jobs_total{{status="awaiting_approval"}} {args.submissions}'
            )
            if expected_outcome not in scrape:
                raise RuntimeError("scrape did not aggregate all worker outcomes")
            if f"idempotency_hits_total {args.submissions}" not in scrape:
                raise RuntimeError("scrape did not aggregate idempotency replays")
            if any(job_id in scrape for job_id in job_ids):
                raise RuntimeError("scrape leaked a review identity")

            print(json.dumps({
                "schema_version": "crag.phase9f.load/v1",
                "submissions": args.submissions,
                "workers": args.workers,
                "logical_jobs": len(job_ids),
                "idempotency_hits": args.submissions,
                "seconds": round(time.monotonic() - started, 6),
                "fake_runner": True,
                "passed": True,
            }, sort_keys=True))
        finally:
            for worker in reversed(workers):
                worker.shutdown()
            for store in reversed(worker_stores):
                store.close()
            service.shutdown()


if __name__ == "__main__":
    main()
