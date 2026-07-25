from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import re
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from code_review_agent.production_metrics import _labels_text
from code_review_agent.service import HttpSettings, create_app
from code_review_agent.approval_publish import PublicationError
from code_review_agent.identity import Role
from code_review_agent.service_core import (
    ApprovalConflict,
    AuthorizationDenied,
    JobStore,
    RepositoryRegistry,
    ReviewRequest,
    ReviewService,
)
from code_review_agent.tracelog import Trace
from code_review_agent.worker import ReviewWorker


ROOT = Path(__file__).resolve().parents[1]


class CanonicalFakeRunner:
    """Offline runner that emits canonical trace evidence without a model call."""

    def __call__(self, request: ReviewRequest, trace_path: Path) -> dict:
        trace = Trace(trace_path, run_id=request.job_id, source_commit="a" * 40)
        trace.event("meta", provider="deepseek", model="fake-model")
        trace.event("llm_response", tokens_in=12, tokens_out=4)
        trace.event("tool", tool="read_file")
        trace.event("verifier_fail_open")
        trace.event("review")
        trace.close()
        return {"summary": "offline fake review", "findings": []}


class FakeClock:
    def __init__(self, *values: float) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        return next(self.values)


def parse_prometheus(text_value: str) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    sample = re.compile(
        r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^{}]*\})? "
        r"(?P<value>-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$"
    )
    for line in text_value.splitlines():
        if not line or line.startswith("#"):
            continue
        match = sample.fullmatch(line)
        if match is None:
            raise AssertionError(f"invalid Prometheus sample: {line}")
        result.setdefault(match.group("name"), []).append(float(match.group("value")))
    return result


class Phase9FProductionObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        repository = self.root / "repo"
        repository.mkdir()
        (repository / ".git").mkdir()
        self.registry = RepositoryRegistry.from_json(
            json.dumps({"owner/repo": str(repository.resolve())})
        )
        self.store = JobStore(self.root / "state")
        self.service = ReviewService(
            self.registry,
            self.store,
            runner=None,
            local_mode=True,
        )
        self.workers = [
            ReviewWorker(
                self.registry,
                self.store,
                runner=CanonicalFakeRunner(),
                worker_id=f"phase9f-worker-{index}",
                concurrency=1,
                poll_seconds=0.05,
                heartbeat_seconds=0.1,
                lease_seconds=2.0,
                shutdown_grace_seconds=1.0,
            )
            for index in range(2)
        ]

    def tearDown(self) -> None:
        for worker in self.workers:
            worker.shutdown(wait=True)
        self.service.shutdown()
        self.temp.cleanup()

    def _wait_for(self, job_ids: list[str]) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            states = [self.store.get(job_id)["state"] for job_id in job_ids]
            if states == ["awaiting_approval"] * len(job_ids):
                return
            time.sleep(0.02)
        self.fail(f"jobs did not complete: {states}")

    def test_two_workers_aggregate_real_transitions_and_canonical_traces(self) -> None:
        for worker in self.workers:
            worker.start()
        job_ids = []
        for index in range(4):
            record = self.service.submit_diff(
                "owner/repo",
                f"diff --git a/a.py b/a.py\n+phase9f-{index}\n",
            )
            job_ids.append(record["review_id"])
        duplicate = self.service.submit_diff(
            "owner/repo",
            "diff --git a/a.py b/a.py\n+phase9f-0\n",
        )
        self.assertTrue(duplicate["duplicate"])
        self._wait_for(job_ids)

        scrape = self.service.metrics.render()
        parsed = parse_prometheus(scrape)
        self.assertIn('review_jobs_total{status="awaiting_approval"} 4', scrape)
        self.assertIn('llm_requests_total{provider="deepseek",status="ok"} 4', scrape)
        self.assertIn('tool_calls_total{status="ok",tool="read_file"} 4', scrape)
        self.assertIn('llm_tokens_total{type="input"} 48', scrape)
        self.assertIn("idempotency_hits_total 1", scrape)
        self.assertIn('trace_completeness_total{status="complete"} 4', scrape)
        self.assertEqual(parsed["queue_depth"], [0.0])
        self.assertEqual(len(set(job_ids)), 4)
        self.assertFalse(any(job_id in scrape for job_id in job_ids))
        self.assertNotIn("owner/repo", scrape)
        label_keys = set(re.findall(r"(?:\{|,)([a-zA-Z_][a-zA-Z0-9_]*)=", scrape))
        self.assertLessEqual(
            label_keys,
            {"status", "provider", "type", "tool", "decision", "operation", "reason", "le"},
        )

    def test_security_rejections_increment_bounded_aggregate_series(self) -> None:
        local = self.store.local_principal
        assert local is not None
        repository = self.store.database.authorized_repository(local, "owner/repo")
        assert repository is not None
        member = self.store.database.create_membership(
            local.organization_id,
            subject="phase9f-viewer",
            display_name="Phase 9F Viewer",
            role=Role.VIEWER,
            repository_ids=(str(repository["id"]),),
        )
        viewer = self.store.database.principal_for_user(
            local.organization_id, str(member["user_id"])
        )
        assert viewer is not None
        with self.assertRaises(AuthorizationDenied):
            self.service.submit_diff("owner/repo", "diff --git a/a b/a\n+x\n", principal=viewer)
        with self.assertRaises(ApprovalConflict):
            self.service._raise_approval_error(PublicationError("approval_replayed"))
        scrape = self.service.metrics.render()
        self.assertIn('unauthorized_operations_total{operation="other"} 1', scrape)
        self.assertIn('approval_validation_failures_total{reason="replay"} 1', scrape)

    def test_fake_clock_webhook_scrape_and_identity_free_histogram(self) -> None:
        settings = HttpSettings(
            service_token="local-phase9f-token-value-32-bytes-minimum",
            webhook_secret="phase9f-webhook-secret",
            allowed_origins=frozenset({"http://localhost"}),
            allowed_hosts=frozenset({"testserver"}),
            local_token_enabled=True,
        )
        app = create_app(
            settings=settings,
            review_service=self.service,
            metrics_clock=FakeClock(10.0, 10.2),
        )
        body = b"{}"
        signature = "sha256=" + hmac.new(
            settings.webhook_secret, body, hashlib.sha256
        ).hexdigest()
        with TestClient(app) as client:
            response = client.post(
                "/webhooks/github",
                content=body,
                headers={
                    "x-hub-signature-256": signature,
                    "x-github-event": "ping",
                    "x-github-delivery": "phase9f-ping",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            scrape = client.get("/metrics")
        self.assertEqual(scrape.status_code, 200)
        self.assertIn("text/plain", scrape.headers["content-type"])
        self.assertIn('webhook_ack_seconds_bucket{le="0.25"} 1', scrape.text)
        self.assertIn("webhook_ack_seconds_count 1", scrape.text)
        parse_prometheus(scrape.text)

    def test_label_guard_rejects_identity_and_unbounded_values(self) -> None:
        for label in ("user_id", "review_id", "repository", "trace_id", "message"):
            with self.subTest(label=label), self.assertRaises(ValueError):
                _labels_text({label: "sensitive"})
        with self.assertRaises(ValueError):
            _labels_text({"status": "x" * 65})

    def test_dashboard_loads_with_stable_unique_ids_and_six_sections(self) -> None:
        dashboard = json.loads(
            (ROOT / "observability/grafana/phase9f-overview.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(dashboard["uid"], "crag-phase9f-overview")
        panel_ids = [panel["id"] for panel in dashboard["panels"]]
        self.assertEqual(len(panel_ids), len(set(panel_ids)))
        rows = {panel["title"] for panel in dashboard["panels"] if panel["type"] == "row"}
        self.assertEqual(
            rows,
            {
                "Business Effect",
                "Stability",
                "Quality Feedback",
                "Cost",
                "Queue Capacity",
                "Approval Safety",
            },
        )
        serialized = json.dumps(dashboard)
        self.assertNotRegex(serialized, r"[A-Za-z]:\\|/Users/")
        self.assertNotRegex(serialized.casefold(), r'"(?:token|credential|secret)"\s*:')

    def test_each_alert_has_positive_and_negative_threshold_fixture(self) -> None:
        rules = (ROOT / "observability/prometheus/alerts.yml").read_text(encoding="utf-8")
        required = {
            "CragQueueGrowing",
            "CragCompletionRateLow",
            "CragProviderErrorsHigh",
            "CragReviewP95High",
            "CragDailyCostOverBudget",
            "CragTelemetryDegraded",
            "CragUnauthorizedOperation",
            "CragApprovalReplayOrMismatch",
        }
        expressions = dict(
            re.findall(r"- alert: (\w+)\s+expr: (.+)", rules)
        )
        self.assertEqual(set(expressions), required)
        for name, expression in expressions.items():
            match = re.search(r"([<>])\s*([0-9]+(?:\.[0-9]+)?)\s*$", expression)
            self.assertIsNotNone(match, name)
            assert match is not None
            operator, raw_threshold = match.groups()
            threshold = float(raw_threshold)
            positive = threshold + 1 if operator == ">" else max(0.0, threshold - 0.01)
            negative = max(0.0, threshold - 0.01) if operator == ">" else threshold + 0.01
            evaluates = (lambda value: value > threshold) if operator == ">" else (
                lambda value: value < threshold
            )
            self.assertTrue(evaluates(positive), f"positive fixture failed for {name}")
            self.assertFalse(evaluates(negative), f"negative fixture failed for {name}")


if __name__ == "__main__":
    unittest.main()
