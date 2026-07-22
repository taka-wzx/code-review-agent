from __future__ import annotations

import json
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

    def test_prompt_and_diff_attestation_bind_all_29_objects(self) -> None:
        self.assertEqual(self.config["finder"]["prompt_sha256"], glm.PROMPT_SHA256)
        result = glm.attest_diff_objects(self.plan, self.sources, self.queue, RAW_ROOT)
        self.assertEqual(result["objects"], 29)
        self.assertEqual(result["total_bytes"], sum(row["diff_bytes"] for row in self.sources))
        self.assertRegex(result["attestation_sha256"], r"^[0-9a-f]{64}$")

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


if __name__ == "__main__":
    unittest.main()
