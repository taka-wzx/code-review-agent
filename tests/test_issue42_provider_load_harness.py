from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import issue42_provider_load_harness as harness  # noqa: E402


class Issue42ProviderLoadHarnessTests(unittest.TestCase):
    @staticmethod
    def _run() -> dict[str, object]:
        return harness.run_harness(
            jobs_per_scenario=1,
            workers=2,
            timeout=12,
            lease_seconds=1.0,
            heartbeat_seconds=0.1,
            long_observation_seconds=1.15,
        )

    def test_all_failures_retry_once_without_duplicate_side_effects(self) -> None:
        report = self._run()

        self.assertTrue(report["passed"])
        self.assertTrue(report["fake_provider"])
        self.assertEqual(report["total_jobs"], 4)
        self.assertEqual(report["provider_usage_rows"], 7)
        self.assertEqual(report["duplicate_usage_rows"], 0)
        scenarios = report["scenarios"]
        assert isinstance(scenarios, dict)
        for name in ("rate_limit", "provider_5xx", "cancelled_request"):
            self.assertEqual(scenarios[name]["attempts_per_job"], 2)
            self.assertEqual(scenarios[name]["side_effects"], 1)
        self.assertEqual(scenarios["long_running"]["attempts_per_job"], 1)

    def test_long_job_renews_lease_without_expiry_recovery(self) -> None:
        report = self._run()

        heartbeat = report["heartbeat"]
        assert isinstance(heartbeat, dict)
        self.assertEqual(heartbeat["long_jobs"], 1)
        self.assertEqual(heartbeat["renewed_long_jobs"], 1)
        self.assertEqual(heartbeat["lease_expired_events"], 0)

    def test_default_capacity_keeps_attempt_usage_unique(self) -> None:
        report = harness.run_harness(timeout=25)

        self.assertEqual(report["jobs_per_scenario"], 4)
        self.assertEqual(report["total_jobs"], 13)
        self.assertEqual(report["provider_usage_rows"], 25)
        self.assertEqual(report["duplicate_usage_rows"], 0)

    def test_cli_emits_identity_free_json_report(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            harness.main(
                [
                    "--jobs-per-scenario",
                    "1",
                    "--workers",
                    "2",
                    "--timeout",
                    "12",
                    "--long-observation-seconds",
                    "1.15",
                ]
            )
        rendered = output.getvalue()
        report = json.loads(rendered)

        self.assertTrue(report["passed"])
        self.assertNotIn("load/repo", rendered)
        self.assertNotIn("review_id", rendered)
        self.assertNotIn("trace", rendered)

    def test_invalid_configuration_fails_before_work_is_created(self) -> None:
        invalid = (
            {"jobs_per_scenario": 0},
            {"workers": 0},
            {"timeout": 0},
            {"timeout": float("nan")},
            {"lease_seconds": 0.5},
            {"heartbeat_seconds": 0.5},
            {"long_observation_seconds": 1.0},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                options = {
                    "jobs_per_scenario": 1,
                    "workers": 1,
                    "timeout": 12.0,
                    "lease_seconds": 1.0,
                    "heartbeat_seconds": 0.1,
                    "long_observation_seconds": 1.15,
                }
                options.update(values)
                harness.run_harness(**options)


if __name__ == "__main__":
    unittest.main()
