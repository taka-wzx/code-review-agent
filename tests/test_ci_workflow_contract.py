from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
WORKFLOW = WORKFLOW_PATH.read_text(encoding="utf-8")

CHECKOUT_REF = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_REF = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
EXPECTED_JOB_IDS = {
    "quality",
    "compatibility",
    "lock-check",
    "container-compose",
    "postgres-integration",
}
PR_COMPATIBILITY = [
    {"os": "ubuntu-latest", "python": "3.10"},
    {"os": "windows-latest", "python": "3.11"},
]
PUSH_COMPATIBILITY = [
    {"os": "ubuntu-latest", "python": "3.10"},
    {"os": "ubuntu-latest", "python": "3.11"},
    {"os": "ubuntu-latest", "python": "3.12"},
    {"os": "windows-latest", "python": "3.11"},
]
PR_REQUIRED_CONTEXTS = {
    "quality",
    "compatibility (ubuntu-latest, 3.10)",
    "compatibility (windows-latest, 3.11)",
    "lock-check",
    "container-compose",
    "postgres-integration",
}


def job_block(job_id: str) -> str:
    jobs = WORKFLOW.split("\njobs:\n", 1)[1]
    match = re.search(
        rf"(?ms)^  {re.escape(job_id)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", jobs
    )
    if match is None:
        raise AssertionError(f"missing workflow job: {job_id}")
    return match.group(1)


class CIWorkflowContractTests(unittest.TestCase):
    def test_triggers_permissions_and_concurrency_are_frozen(self) -> None:
        header = WORKFLOW.split("\njobs:\n", 1)[0]
        self.assertIn("on:\n  push:\n    branches: [master, main]\n  pull_request:\n", header)
        self.assertNotIn("workflow_dispatch", header)
        self.assertNotIn("schedule:", header)
        self.assertIn("permissions:\n  contents: read\n", header)
        self.assertIn(
            "group: ${{ github.workflow }}-${{ "
            "github.event.pull_request.number || github.run_id }}",
            header,
        )
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name == 'pull_request' }}", header
        )

    def test_job_set_and_pr_required_contexts_are_exact(self) -> None:
        jobs = WORKFLOW.split("\njobs:\n", 1)[1]
        job_ids = set(re.findall(r"(?m)^  ([a-z][a-z0-9-]*):$", jobs))
        self.assertEqual(job_ids, EXPECTED_JOB_IDS)
        derived_contexts = EXPECTED_JOB_IDS - {"compatibility"}
        derived_contexts.update(
            f"compatibility ({entry['os']}, {entry['python']})"
            for entry in PR_COMPATIBILITY
        )
        self.assertEqual(derived_contexts, PR_REQUIRED_CONTEXTS)

    def test_actions_are_official_full_sha_pins(self) -> None:
        action_lines = re.findall(
            r"(?m)^\s*- uses: ([^\s#]+)\s+#\s+(v[^\s]+)\s*$", WORKFLOW
        )
        self.assertEqual(len(action_lines), 10)
        expected = {
            (CHECKOUT_REF, "v7.0.1"),
            (SETUP_PYTHON_REF, "v7.0.0"),
        }
        self.assertEqual(set(action_lines), expected)
        for reference, _tag in action_lines:
            self.assertRegex(reference, r"^actions/[a-z-]+@[0-9a-f]{40}$")

    def test_full_quality_runs_once_with_full_history(self) -> None:
        quality = job_block("quality")
        self.assertEqual(WORKFLOW.count("python scripts/verify.py"), 1)
        self.assertIn("runs-on: ubuntu-latest", quality)
        self.assertIn('python-version: "3.13"', quality)
        self.assertIn("fetch-depth: 0", quality)
        self.assertIn("timeout-minutes: 30", quality)

    def test_event_specific_compatibility_matrix_is_exact(self) -> None:
        compatibility = job_block("compatibility")
        encoded_matrices = re.findall(r"'(\{\"include\":\[.*?\]\})'", compatibility)
        self.assertEqual(len(encoded_matrices), 2)
        self.assertEqual(json.loads(encoded_matrices[0]), {"include": PR_COMPATIBILITY})
        self.assertEqual(json.loads(encoded_matrices[1]), {"include": PUSH_COMPATIBILITY})
        self.assertIn(
            "fail-fast: ${{ github.event_name == 'pull_request' }}", compatibility
        )
        self.assertIn("fetch-depth: 0", compatibility)
        self.assertIn("python -m unittest discover -s tests -v", compatibility)
        for duplicate_gate in (
            "scripts/verify.py",
            "ruff",
            "mypy",
            "coverage",
            "code_review_agent --help",
            "crag --help",
        ):
            self.assertNotIn(duplicate_gate, compatibility)

    def test_cache_inputs_and_timeouts_are_present(self) -> None:
        expected_timeouts = {
            "quality": 30,
            "compatibility": 30,
            "lock-check": 20,
            "container-compose": 45,
            "postgres-integration": 30,
        }
        for job_id, minutes in expected_timeouts.items():
            block = job_block(job_id)
            self.assertIn(f"timeout-minutes: {minutes}", block)
            self.assertIn("cache: pip", block)
            self.assertIn("requirements.lock", block)
            self.assertIn("pyproject.toml", block)

    def test_lockfile_gate_is_preserved(self) -> None:
        lock_check = job_block("lock-check")
        self.assertIn("python -m pip install -r requirements.lock", lock_check)
        self.assertIn("python -m pip install -e . --no-deps", lock_check)
        self.assertIn('python -c "import code_review_agent"', lock_check)

    def test_cli_smoke_and_compose_share_one_job(self) -> None:
        container = job_block("container-compose")
        ordered_commands = [
            "--prepare-context",
            "docker build --tag code-review-agent:ci",
            "docker run --rm code-review-agent:ci --help",
            "docker compose version",
        ]
        positions = [container.index(command) for command in ordered_commands]
        harness = re.search(
            r"(?m)^\s*run: python scripts/phase9c_container_test\.py\s*$", container
        )
        self.assertIsNotNone(harness)
        assert harness is not None
        positions.append(harness.start())
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("Dockerfile.service", container)
        self.assertNotIn("code-review-agent-service:ci", container)

    def test_postgres_migration_load_and_integration_are_preserved(self) -> None:
        postgres = job_block("postgres-integration")
        for required in (
            "image: postgres:16-alpine",
            "python -m code_review_agent.database upgrade",
            "scripts/phase9c_load_test.py --submissions 50 --concurrency 50",
            "--workers 2",
            "python -m unittest -v tests.test_phase9c_postgres",
        ):
            self.assertIn(required, postgres)

    def test_real_external_and_untrusted_paths_are_absent(self) -> None:
        lowered = WORKFLOW.lower()
        for forbidden in (
            "self-hosted",
            "secrets.",
            "--eval-assets",
            "workflow_dispatch",
            "phase11b",
            "github-canary",
        ):
            self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
