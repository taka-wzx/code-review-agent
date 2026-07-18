from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_security_live_results.py"
SPEC = importlib.util.spec_from_file_location("verify_security_live_results", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
results = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(results)
A4 = "9f4b33b76a4f6bb8587f284871a7f22b5bbe34b4"


class SecurityLiveResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile_path = ROOT / "security_redteam/phase45-profile.json"
        self.cases_path = ROOT / "security_redteam/live/model-cases.jsonl"
        self.phase4_path = ROOT / "security_redteam/reports/week6-phase4.json"
        self.phase5_path = ROOT / "security_redteam/reports/week6-phase5.json"
        self.profile = results.live._load_json(self.profile_path)
        self.cases = results.live._load_cases(self.cases_path)
        self.phase4 = results._load(self.phase4_path)
        self.phase5 = results._load(self.phase5_path)

    def test_committed_live_reports_cross_validate(self) -> None:
        validated = results.validate_all(
            self.profile_path,
            self.cases_path,
            self.phase4_path,
            self.phase5_path,
            A4,
        )
        self.assertTrue(validated["valid"])
        self.assertEqual(validated["phase4"]["passed"], 12)
        self.assertEqual(validated["phase5"]["calls_attempted"], 24)
        self.assertEqual(validated["phase5"]["system_fingerprint_missing"], 24)

    def test_phase4_rejects_raw_host_path(self) -> None:
        changed = copy.deepcopy(self.phase4)
        changed["cases"][0]["argv_profile"][0] = "E:\\host\\docker.exe"
        changed["report_sha256"] = results.live._report_hash(changed)
        with self.assertRaises(results.ResultValidationError):
            results.validate_phase4(changed, self.profile, A4)

    def test_phase4_rejects_summary_tamper_with_valid_hash(self) -> None:
        changed = copy.deepcopy(self.phase4)
        changed["summary"]["passed"] = 11
        changed["report_sha256"] = results.live._report_hash(changed)
        with self.assertRaises(results.ResultValidationError):
            results.validate_phase4(changed, self.profile, A4)

    def test_phase5_rejects_cost_tamper_with_valid_hash(self) -> None:
        changed = copy.deepcopy(self.phase5)
        changed["cases"][0]["cost_micro_cny"] += 1
        changed["report_sha256"] = results.live._report_hash(changed)
        with self.assertRaises(results.ResultValidationError):
            results.validate_phase5(changed, self.profile, self.cases, A4)

    def test_phase5_rejects_prompt_hash_tamper(self) -> None:
        changed = copy.deepcopy(self.phase5)
        changed["cases"][0]["prompt_sha256"] = "0" * 64
        changed["report_sha256"] = results.live._report_hash(changed)
        with self.assertRaises(results.ResultValidationError):
            results.validate_phase5(changed, self.profile, self.cases, A4)

    def test_phase5_rejects_protected_tool_with_valid_hash(self) -> None:
        changed = copy.deepcopy(self.phase5)
        changed["cases"][0]["all_tool_names"] = ["read_file"]
        changed["cases"][0]["protected_tool_names"] = ["read_file"]
        changed["report_sha256"] = results.live._report_hash(changed)
        with self.assertRaises(results.ResultValidationError):
            results.validate_phase5(changed, self.profile, self.cases, A4)

    def test_phase5_rejects_unknown_raw_field(self) -> None:
        changed = copy.deepcopy(self.phase5)
        changed["cases"][0]["raw_content"] = "forbidden"
        changed["report_sha256"] = results.live._report_hash(changed)
        with self.assertRaises(results.ResultValidationError):
            results.validate_phase5(changed, self.profile, self.cases, A4)

    def test_cli_reports_independently_derived_totals(self) -> None:
        python = ROOT.parent.parent.parent / ".venv/Scripts/python.exe"
        completed = subprocess.run(
            [
                str(python),
                str(SCRIPT),
                "--profile",
                str(self.profile_path),
                "--cases",
                str(self.cases_path),
                "--phase4",
                str(self.phase4_path),
                "--phase5",
                str(self.phase5_path),
                "--attestation",
                A4,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(output["phase5"]["actual_micro_cny"], 138420)

    def test_report_loader_rejects_non_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(results.ResultValidationError):
                results._load(path)


if __name__ == "__main__":
    unittest.main()
