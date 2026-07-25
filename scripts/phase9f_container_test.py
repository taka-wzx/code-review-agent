"""Phase 9F filtered-context container smoke, always using the fake runner."""
from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess
import sys

from phase9c_container_test import HarnessError, main as phase9c_main


ROOT = Path(__file__).resolve().parents[1]


def _check_phase9f_inputs() -> None:
    required = (
        ROOT / "migrations/versions/0006_phase9f_production_metrics.py",
        ROOT / "src/code_review_agent/production_metrics.py",
        ROOT / "observability/grafana/phase9f-overview.json",
        ROOT / "observability/prometheus/alerts.yml",
    )
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise HarnessError("Phase 9F container inputs are missing: " + ", ".join(missing))
    service_source = (ROOT / "src/code_review_agent/service.py").read_text(encoding="utf-8")
    if '@app.get("/metrics")' not in service_source:
        raise HarnessError("service image source does not expose /metrics")


def _docker_daemon_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        result = subprocess.run(
            [docker, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def main() -> None:
    _check_phase9f_inputs()
    if not _docker_daemon_available():
        print(json.dumps({
            "schema_version": "crag.phase9f.container/v1",
            "passed": False,
            "skipped": "docker_daemon_unavailable",
        }, sort_keys=True))
        return
    phase9c_main([])


if __name__ == "__main__":
    try:
        main()
    except HarnessError as exc:
        print(f"phase9f container smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
