from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_production_metrics_assets import (  # noqa: E402
    DEFAULT_CONTRACT,
    DEFAULT_DASHBOARD,
    DEFAULT_EXPORTER,
    ValidationError,
    validate_assets,
)


class Issue38ProductionMetricsAssetTests(unittest.TestCase):
    def _write_json(self, directory: Path, name: str, value: object) -> Path:
        path = directory / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _assets(self) -> tuple[dict[str, object], dict[str, object]]:
        return (
            json.loads(DEFAULT_CONTRACT.read_text(encoding="utf-8")),
            json.loads(DEFAULT_DASHBOARD.read_text(encoding="utf-8")),
        )

    def test_committed_contract_and_dashboard_validate(self) -> None:
        result = validate_assets()

        self.assertEqual(result["metric_families"], 22)
        self.assertEqual(result["service_paths"], 7)
        self.assertEqual(result["dashboard_panels"], 17)

    def test_dashboard_unknown_metric_is_rejected(self) -> None:
        contract, dashboard = self._assets()
        panels = dashboard["panels"]
        assert isinstance(panels, list)
        target = panels[1]["targets"][0]
        assert isinstance(target, dict)
        target["expr"] = "rate(unrecognized_metric_total[5m])"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValidationError, "unknown metric"):
                validate_assets(
                    contract_path=self._write_json(root, "contract.json", contract),
                    dashboard_path=self._write_json(root, "dashboard.json", dashboard),
                    exporter_path=DEFAULT_EXPORTER,
                )

    def test_prohibited_or_unbounded_label_is_rejected(self) -> None:
        contract, dashboard = self._assets()
        metrics = contract["metrics"]
        assert isinstance(metrics, list)
        first_metric = metrics[0]
        assert isinstance(first_metric, dict)
        first_metric["label_values"] = {"user_id": ["unbounded-identity"]}
        first_metric["max_series"] = 1

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValidationError, "unbounded or prohibited label"):
                validate_assets(
                    contract_path=self._write_json(root, "contract.json", contract),
                    dashboard_path=self._write_json(root, "dashboard.json", dashboard),
                    exporter_path=DEFAULT_EXPORTER,
                )

    def test_selector_label_must_belong_to_its_metric(self) -> None:
        contract, dashboard = self._assets()
        panels = dashboard["panels"]
        assert isinstance(panels, list)
        target = panels[4]["targets"][0]
        assert isinstance(target, dict)
        target["expr"] = 'queue_depth{status="queued"}'

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValidationError, "label is undefined"):
                validate_assets(
                    contract_path=self._write_json(root, "contract.json", contract),
                    dashboard_path=self._write_json(root, "dashboard.json", dashboard),
                    exporter_path=DEFAULT_EXPORTER,
                )

    def test_duplicate_panel_id_is_rejected_before_import(self) -> None:
        contract, dashboard = self._assets()
        panels = dashboard["panels"]
        assert isinstance(panels, list)
        second = deepcopy(panels[1])
        panels.append(second)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValidationError, "panel id is duplicated"):
                validate_assets(
                    contract_path=self._write_json(root, "contract.json", contract),
                    dashboard_path=self._write_json(root, "dashboard.json", dashboard),
                    exporter_path=DEFAULT_EXPORTER,
                )

    def test_contract_label_policy_drift_is_rejected(self) -> None:
        contract, dashboard = self._assets()
        policy = contract["label_policy"]
        assert isinstance(policy, dict)
        allowed = policy["allowed"]
        assert isinstance(allowed, list)
        allowed.remove("status")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValidationError, "label policy drifted"):
                validate_assets(
                    contract_path=self._write_json(root, "contract.json", contract),
                    dashboard_path=self._write_json(root, "dashboard.json", dashboard),
                    exporter_path=DEFAULT_EXPORTER,
                )


if __name__ == "__main__":
    unittest.main()
