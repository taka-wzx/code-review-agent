from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_security_live.py"
SPEC = importlib.util.spec_from_file_location("verify_security_live", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live)


class SecurityLiveContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile_path = ROOT / "security_redteam" / "phase45-profile.json"
        self.cases_path = ROOT / "security_redteam" / "live" / "model-cases.jsonl"
        self.profile = live._load_json(self.profile_path)
        self.cases = live._load_cases(self.cases_path)

    def test_frozen_profile_and_cases_validate(self) -> None:
        live.validate_profile(self.profile)
        live.validate_cases(self.cases)

    def test_profile_schema_property_sets_match_validator(self) -> None:
        schema = json.loads(
            (ROOT / "security_redteam/schemas/phase45-profile.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(schema["properties"]), live.PROFILE_KEYS)
        self.assertEqual(set(schema["required"]), live.PROFILE_KEYS)
        self.assertEqual(
            set(schema["properties"]["docker"]["properties"]),
            set(self.profile["docker"]),
        )
        self.assertEqual(
            set(schema["properties"]["model"]["properties"]),
            set(self.profile["model"]),
        )

    def test_case_schema_property_sets_match_validator(self) -> None:
        schema = json.loads(
            (ROOT / "security_redteam/schemas/live-model-case.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(schema["properties"]), live.CASE_KEYS)
        self.assertEqual(set(schema["required"]), live.CASE_KEYS)

    def test_profile_rejects_model_change(self) -> None:
        changed = copy.deepcopy(self.profile)
        changed["model"]["model_id"] = "glm-4.7"
        with self.assertRaises(live.LiveValidationError):
            live.validate_profile(changed)

    def test_profile_rejects_docker_resource_change(self) -> None:
        changed = copy.deepcopy(self.profile)
        changed["docker"]["pids_limit"] = 129
        with self.assertRaises(live.LiveValidationError):
            live.validate_profile(changed)

    def test_cases_reject_reorder(self) -> None:
        changed = list(self.cases)
        changed[0], changed[1] = changed[1], changed[0]
        with self.assertRaises(live.LiveValidationError):
            live.validate_cases(changed)

    def test_cases_reject_unknown_field(self) -> None:
        changed = copy.deepcopy(self.cases)
        changed[0]["result"] = "deny"
        with self.assertRaises(live.LiveValidationError):
            live.validate_cases(changed)

    def test_decision_parser_accepts_one_valid_submission(self) -> None:
        call = SimpleNamespace(
            function=SimpleNamespace(
                name="submit_security_decision",
                arguments=json.dumps(
                    {
                        "decision": "deny",
                        "reason_code": "untrusted_operation_request",
                    }
                ),
            )
        )
        decision, names, malformed = live._decision_from_message(
            SimpleNamespace(tool_calls=[call])
        )
        self.assertEqual(decision, "deny")
        self.assertEqual(names, ["submit_security_decision"])
        self.assertFalse(malformed)

    def test_decision_parser_flags_protected_tool(self) -> None:
        protected = SimpleNamespace(
            function=SimpleNamespace(name="read_file", arguments='{"path":".env"}')
        )
        decision, names, malformed = live._decision_from_message(
            SimpleNamespace(tool_calls=[protected])
        )
        self.assertIsNone(decision)
        self.assertEqual(names, ["read_file"])
        self.assertTrue(malformed)

    def test_decision_parser_rejects_two_submissions(self) -> None:
        calls = [
            SimpleNamespace(
                function=SimpleNamespace(
                    name="submit_security_decision",
                    arguments='{"decision":"allow","reason_code":"benign_read_only"}',
                )
            ),
            SimpleNamespace(
                function=SimpleNamespace(
                    name="submit_security_decision",
                    arguments='{"decision":"deny","reason_code":"untrusted_operation_request"}',
                )
            ),
        ]
        decision, _, malformed = live._decision_from_message(
            SimpleNamespace(tool_calls=calls)
        )
        self.assertIsNone(decision)
        self.assertTrue(malformed)

    def test_bootstrap_is_deterministic_and_bounded(self) -> None:
        first = live._bootstrap([0, 1, 0, 1], samples=1000, seed=7)
        second = live._bootstrap([0, 1, 0, 1], samples=1000, seed=7)
        self.assertEqual(first, second)
        assert first is not None
        self.assertGreaterEqual(first[0], 0)
        self.assertLessEqual(first[1], 1)

    def test_micro_cny_rounds_up(self) -> None:
        self.assertEqual(live._micro_cny(1, 8_000_000), 8)
        self.assertEqual(live._micro_cny(1_000_000, 8_000_000), 8_000_000)

    def test_canonical_text_treats_lf_and_crlf_equally(self) -> None:
        self.assertEqual(
            live._canonical_text(b"one\r\ntwo\r\n", "test"),
            live._canonical_text(b"one\ntwo\n", "test"),
        )

    def test_docker_argv_is_exact_and_shell_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            probe = Path(temporary) / "probe"
            fixture.mkdir()
            probe.mkdir()
            argv = live._docker_argv(
                "docker", self.profile, "W6-DK-11", fixture, probe
            )
        self.assertEqual(argv[0:3], ("docker", "run", "--rm"))
        self.assertIn("--pull", argv)
        self.assertIn("never", argv)
        self.assertIn("--network", argv)
        self.assertIn("none", argv)
        self.assertNotIn("sh", argv)
        self.assertEqual(argv[-1], "; touch /workspace/W6_INJECTED")

    def test_new_report_writer_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            live._write_new_json(path, {"value": 1})
            with self.assertRaises(FileExistsError):
                live._write_new_json(path, {"value": 2})

    def test_report_hash_detects_change(self) -> None:
        report = {
            "schema_version": "crag.security-live-report/v1alpha1",
            "phase": 4,
            "cases": [{"case_id": case_id} for case_id in live.DOCKER_CASES],
        }
        report["report_sha256"] = live._report_hash(report)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            live.validate_report(path)
            report["phase"] = 5
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaises(live.LiveValidationError):
                live.validate_report(path)

    def test_attestation_binds_parent_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=root, check=True
            )
            frozen = root / "frozen.txt"
            frozen.write_text("base\n", encoding="utf-8", newline="\n")
            subprocess.run(["git", "add", "frozen.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            frozen.write_text("attested\n", encoding="utf-8", newline="\n")
            subprocess.run(["git", "add", "frozen.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "a4"], cwd=root, check=True, capture_output=True
            )
            a4 = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            live.validate_attestation(
                root, a4, {"base_commit": base, "frozen_paths": ["frozen.txt"]}
            )
            frozen.write_text("changed\n", encoding="utf-8", newline="\n")
            with self.assertRaises(live.LiveValidationError):
                live.validate_attestation(
                    root, a4, {"base_commit": base, "frozen_paths": ["frozen.txt"]}
                )

    def test_validate_cli_is_offline(self) -> None:
        result = subprocess.run(
            [
                str(ROOT.parent.parent.parent / ".venv/Scripts/python.exe"),
                str(SCRIPT),
                "validate",
                "--profile",
                str(self.profile_path),
                "--cases",
                str(self.cases_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])


if __name__ == "__main__":
    unittest.main()
