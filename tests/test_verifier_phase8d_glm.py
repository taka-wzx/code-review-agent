from __future__ import annotations

import hashlib
import json
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import verifier_corpus as vc
import verifier_phase8d as v8d
import verifier_phase8d_glm as glm


ROOT = Path(__file__).resolve().parents[1]
TRAINING = ROOT / "verifier_training"
SNAPSHOT = TRAINING / "corpus-snapshot"
RAW_ROOT = ROOT / "traces" / "week8b-corpus"
REAL = TRAINING / "real"


class FakeCompletions:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        submit = SimpleNamespace(
            id=f"tool-{len(self.requests)}",
            function=SimpleNamespace(
                name="submit_review",
                arguments=json.dumps({"summary": "No bounded finding.", "findings": []}),
            ),
        )
        return SimpleNamespace(
            id=f"response-{len(self.requests)}",
            model="glm-5.2",
            system_fingerprint="fake-fingerprint",
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(content=None, tool_calls=[submit]),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )


class FakeClient:
    def __init__(self) -> None:
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


class Phase8DGlmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = v8d.load_config(TRAINING / "phase8d-config.json")
        cls.plan = vc.load_plan(TRAINING / "corpus-plan.json")
        cls.sources = vc.load_pr_sources(SNAPSHOT / "pr-sources.jsonl", cls.plan)
        cls.queue = vc.validate_finder_queue(
            vc._load_jsonl(SNAPSHOT / "finder-queue.jsonl"), cls.sources
        )
        cls.recovery = glm.load_recovery_config(TRAINING / "phase8d-r1-config.json")

    def test_prompt_and_diff_attestation_bind_all_29_objects(self) -> None:
        self.assertEqual(self.config["finder"]["prompt_sha256"], glm.PROMPT_SHA256)
        result = glm.attest_diff_objects(self.plan, self.sources, self.queue, RAW_ROOT)
        self.assertEqual(result["objects"], 29)
        self.assertEqual(result["total_bytes"], sum(row["diff_bytes"] for row in self.sources))
        self.assertRegex(result["attestation_sha256"], r"^[0-9a-f]{64}$")

    def test_recovery_config_rejects_any_third_or_reordered_queue(self) -> None:
        mutated = copy.deepcopy(self.recovery)
        mutated["queue_ids"] = list(reversed(mutated["queue_ids"]))
        with self.assertRaisesRegex(glm.Phase8DExecutionError, "two frozen"):
            glm.validate_recovery_config(mutated)

    def test_budget_proxy_forces_glm_options_and_tracks_response_identity(self) -> None:
        fake = FakeClient()
        ledger = glm.BudgetLedger(self.config)
        client = glm.BudgetedClient(fake, ledger)
        response = client.chat.completions.create(
            model="glm-5.2",
            max_tokens=8000,
            temperature=0.2,
            messages=[{"role": "user", "content": "bounded"}],
            tools=[],
        )
        self.assertEqual(response.id, "response-1")
        request = fake.completions.requests[0]
        self.assertFalse(request["stream"])
        self.assertEqual(request["tool_choice"], "auto")
        self.assertEqual(request["extra_body"]["thinking"], {"type": "disabled"})
        self.assertEqual(request["extra_body"]["reasoning_effort"], "none")
        self.assertEqual(ledger.logical_calls, 1)
        self.assertEqual(ledger.input_tokens, 100)
        self.assertEqual(ledger.output_tokens, 20)
        self.assertEqual(ledger.response_metadata[0]["response_model"], "glm-5.2")

    def test_fake_full_queue_writes_zero_candidate_receipts_without_network(self) -> None:
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = glm.execute_queue(
                self.config,
                self.plan,
                self.sources,
                self.queue,
                RAW_ROOT,
                root / "traces",
                root / "finder-runs.jsonl",
                root / "candidate-sources.jsonl",
                fake,
            )
            self.assertEqual(result["receipts"], 29)
            self.assertEqual(result["completed_zero_candidates"], 29)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["logical_calls"], 58)
            self.assertEqual(len(fake.completions.requests), 58)
            runs = v8d._load_jsonl(root / "finder-runs.jsonl")
            self.assertEqual({row["synthetic"] for row in runs}, {False})
            self.assertEqual({row["status"] for row in runs}, {"completed_zero_candidates"})
            self.assertEqual((root / "candidate-sources.jsonl").read_text(), "")
            trace = json.loads(next((root / "traces").glob("*.json")).read_text())
            self.assertEqual(trace["requested_model"], "glm-5.2")
            self.assertEqual(trace["responses"][0]["system_fingerprint"], "fake-fingerprint")

    def test_committed_real_run_is_hash_bound_and_truthfully_incomplete(self) -> None:
        runs_path = REAL / "phase8d-glm52-finder-runs.jsonl"
        candidates_path = REAL / "phase8d-glm52-candidate-sources.jsonl"
        summary = json.loads((REAL / "phase8d-glm52-summary.json").read_text())
        runs = v8d._load_jsonl(runs_path)
        candidates = vc.validate_candidate_sources(
            v8d._load_jsonl(candidates_path), self.plan, self.sources
        )
        validated = v8d.validate_finder_runs(
            runs, self.config, self.plan, self.queue, self.sources, candidates
        )
        self.assertEqual(len(validated), 29)
        self.assertEqual(len(candidates), 116)
        self.assertEqual(sum(row["status"] == "failed" for row in validated), 2)
        self.assertFalse(summary["finder_complete"])
        self.assertFalse(summary["trainable"])
        self.assertFalse(summary["quality_claim_allowed"])
        self.assertEqual(
            hashlib.sha256(runs_path.read_bytes()).hexdigest(),
            summary["artifacts"]["finder_runs_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(candidates_path.read_bytes()).hexdigest(),
            summary["artifacts"]["candidate_sources_sha256"],
        )

    def test_fake_recovery_supersedes_only_two_failures_without_network(self) -> None:
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = glm.execute_recovery(
                self.config,
                self.recovery,
                self.plan,
                self.sources,
                self.queue,
                RAW_ROOT,
                REAL / "phase8d-glm52-finder-runs.jsonl",
                REAL / "phase8d-glm52-candidate-sources.jsonl",
                root / "traces",
                root / "recovery-runs.jsonl",
                root / "recovered-candidates.jsonl",
                root / "effective-runs.jsonl",
                root / "effective-candidates.jsonl",
                root / "audit.json",
                fake,
            )
            self.assertEqual(result["recovery_runs"], 2)
            self.assertEqual(result["recovered_candidates"], 0)
            self.assertEqual(result["effective_failed"], 0)
            self.assertEqual(result["effective_candidates"], 116)
            self.assertEqual(result["logical_calls"], 4)
            recovery_runs = v8d._load_jsonl(root / "recovery-runs.jsonl")
            self.assertEqual(
                [row["queue_id"] for row in recovery_runs], glm.R1_QUEUE_IDS
            )
            self.assertEqual(
                {row["status"] for row in recovery_runs},
                {"completed_zero_candidates"},
            )
            effective_runs = v8d._load_jsonl(root / "effective-runs.jsonl")
            effective_candidates = v8d._load_jsonl(root / "effective-candidates.jsonl")
            self.assertEqual(
                len(
                    v8d.validate_finder_runs(
                        effective_runs,
                        self.config,
                        self.plan,
                        self.queue,
                        self.sources,
                        effective_candidates,
                    )
                ),
                29,
            )


if __name__ == "__main__":
    unittest.main()
