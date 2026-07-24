from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from code_review_agent.context import build_context_for_mode
from code_review_agent.context_memory import (
    ContextMode,
    MemoryKind,
    MemoryQuery,
    MemorySource,
    MemoryTrustError,
    MemoryWrite,
    OrganizationPolicy,
    OrganizationPolicyStore,
    RepositoryMemoryStore,
    RunContext,
    repository_source_sha,
    render_policy,
)
from code_review_agent.service_core import JobStore


class Phase9EContextMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = JobStore(self.root / "state")
        self.db = self.store.database
        self.org = self.db.create_organization("phase9e", "Phase 9E")
        self.repo = self.db.register_repository(self.org["id"], "owner/repo")
        self.memory = RepositoryMemoryStore(self.db)
        self.policies = OrganizationPolicyStore(self.db)
        self.sha = "a" * 40

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _write(self, **overrides):
        values = {
            "organization_id": self.org["id"],
            "repository_id": self.repo["id"],
            "kind": MemoryKind.CONVENTION,
            "content": "Use explicit transactions for review state.",
            "source_sha": self.sha,
            "source_kind": MemorySource.HUMAN_CONFIRMED,
            "created_by": "maintainer",
            "confirmed_by": "maintainer",
            "reason": "confirmed during repository onboarding",
            "path": "CONVENTIONS.md",
            "language": "python",
            "symbol": "transaction",
        }
        values.update(overrides)
        return MemoryWrite(**values)

    def _query(self, sha=None, **kwargs):
        return self.memory.retrieve(
            MemoryQuery(
                organization_id=self.org["id"],
                repository_id=self.repo["id"],
                base_sha=sha or self.sha,
                token_budget=kwargs.pop("token_budget", 2000),
                **kwargs,
            )
        )

    def test_trust_gate_revision_and_deterministic_rerank(self) -> None:
        with self.assertRaises(MemoryTrustError):
            self.memory.add(self._write(source_kind=MemorySource.MODEL_OUTPUT))
        first = self.memory.add(self._write())
        second = self.memory.add(
            self._write(
                kind=MemoryKind.RISK_PATH,
                content="Review migrations before changing tenant predicates.",
                path="migrations/versions/0005_phase9e_context_memory.py",
                reason="maintainer confirmed a high-risk path",
            )
        )
        selection = self._query(paths=("CONVENTIONS.md",), symbols=("transaction",))
        self.assertEqual([record.id for record in selection.records], [first, second])
        self.assertEqual(selection.records[0].source_sha, self.sha)
        self.assertEqual(selection.provenance[0]["organization_id"], self.org["id"])
        other_org = self.db.create_organization("phase9e-other", "Phase 9E other")
        self.assertEqual(
            self.memory.retrieve(
                MemoryQuery(other_org["id"], self.repo["id"], self.sha)
            ).records,
            (),
        )
        self.assertEqual(self._query(sha="b" * 40).records, ())
        self.assertEqual(self._query(token_budget=1).records, ())

    def test_feedback_hit_expiry_and_repository_deletion(self) -> None:
        accepted = self.memory.add_feedback(
            organization_id=self.org["id"],
            repository_id=self.repo["id"],
            decision="accepted",
            fingerprint="accepted-fingerprint",
            finding_hash="c" * 64,
            source_sha=self.sha,
            principal_id="maintainer",
            reason="human confirmed the finding",
            retention_days=30,
        )
        rejected = self.memory.add_feedback(
            organization_id=self.org["id"],
            repository_id=self.repo["id"],
            decision="rejected",
            fingerprint="suppression-fingerprint",
            finding_hash="d" * 64,
            source_sha=self.sha,
            principal_id="maintainer",
            reason="human rejected this repository-specific report",
            retention_days=30,
        )
        records = self._query(lexical="fingerprint")
        self.assertEqual({record.id for record in records.records}, {accepted, rejected})
        expired = self.memory.add(
            self._write(
                kind=MemoryKind.CODE_OWNER,
                content="owners@example.invalid",
                reason="temporary owner mapping",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        self.assertNotIn(expired, {record.id for record in self._query().records})
        self.assertGreaterEqual(self.memory.purge_expired(self.org["id"]), 1)
        self.assertGreaterEqual(self.memory.remove_repository(self.org["id"], self.repo["id"]), 2)
        self.assertEqual(self._query().records, ())

    def test_policy_lifecycle_and_org_isolation(self) -> None:
        policy = OrganizationPolicy(
            organization_id=self.org["id"],
            version="policy/9e",
            severity_levels=("high", "medium", "low"),
            forbidden_operations=("publish_without_approval",),
            allowed_tools=("read_file", "search_repo"),
            approval_threshold=80,
            retention_days=30,
            cost_budget_microusd=1000,
            created_by="admin",
            reason="administrator configuration",
            source_sha=self.sha,
        )
        policy_id = self.policies.put(policy, source_kind=MemorySource.ADMIN_CONFIG)
        self.assertEqual(self.policies.active(self.org["id"]).version, "policy/9e")
        self.assertTrue(self.policies.invalidate(self.org["id"], "policy/9e"))
        self.assertIsNone(self.policies.active(self.org["id"]))
        self.assertEqual(
            self.policies.purge_invalidated(
                self.org["id"], now=datetime.now(timezone.utc) + timedelta(days=31)
            ),
            1,
        )
        self.assertIsNotNone(policy_id)

    def test_run_context_is_ephemeral_and_modes_are_deterministic(self) -> None:
        repo_root = self.root / "checkout"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        (repo_root / "CONVENTIONS.md").write_text("Prefer transactions.\n", encoding="utf-8")
        (repo_root / "a.py").write_text("def changed():\n    return 1\n", encoding="utf-8")
        diff = """diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def changed():\n-    return 0\n+    return 1\n"""
        run = RunContext.create(diff, self.sha, 200)
        self.memory.add(self._write(content="Use transactions in changed code."))
        off = build_context_for_mode(diff, repo_root, mode=ContextMode.OFF, run_context=run)
        static = build_context_for_mode(diff, repo_root, mode=ContextMode.CURRENT_STATIC, run_context=run)
        hierarchical = build_context_for_mode(
            diff,
            repo_root,
            mode=ContextMode.HIERARCHICAL,
            run_context=run,
            memory_store=self.memory,
            policy_store=self.policies,
            organization_id=self.org["id"],
            repository_id=self.repo["id"],
            base_sha=self.sha,
            token_cap=200,
        )
        self.assertEqual(off, "")
        self.assertIn("Changed file", static)
        self.assertIn("Trusted repository memory", hierarchical)
        self.assertLessEqual(run.token_used, run.token_budget)
        self.assertEqual(hierarchical, build_context_for_mode(
            diff,
            repo_root,
            mode="hierarchical",
            run_context=RunContext.create(diff, self.sha, 200),
            memory_store=self.memory,
            policy_store=self.policies,
            organization_id=self.org["id"],
            repository_id=self.repo["id"],
            base_sha=self.sha,
            token_cap=200,
        ))
        run.close()
        self.assertTrue(run.closed)
        with self.assertRaises(RuntimeError):
            run.record_tool("finder", "read_file", "ok")

    def test_validation_graph_invalidation_and_policy_rendering(self) -> None:
        with self.assertRaises(ValueError):
            ContextMode.parse("unknown")
        with self.assertRaises(ValueError):
            RunContext.create("diff", self.sha, 0)
        run = RunContext.create("diff", self.sha, 2)
        run.set_plan([" first ", "", 3])
        run.record_stage("verifier", accepted=True, ignored=object())
        for _ in range(130):
            run.record_tool("finder", "read_file", "Error: missing")
        self.assertEqual(len(run.tool_summaries), 128)
        with self.assertRaises(ValueError):
            run.consume_tokens(-1)

        invalid_writes = (
            self._write(organization_id=""),
            self._write(content=""),
            self._write(source_sha="not-a-sha"),
            self._write(valid_from_sha="bad"),
            self._write(confirmed_by=None),
            self._write(kind=MemoryKind.ACCEPTED_FINDING, fingerprint=None),
        )
        for write in invalid_writes:
            with self.assertRaises(MemoryTrustError):
                write.validate()
        with self.assertRaises(MemoryTrustError):
            self.memory.add_feedback(
                organization_id=self.org["id"], repository_id=self.repo["id"],
                decision="fixed", fingerprint="x", finding_hash="e" * 64,
                source_sha=self.sha, principal_id="human", reason="not memory",
                retention_days=1,
            )
        with self.assertRaises(ValueError):
            MemoryQuery(self.org["id"], self.repo["id"], "bad").validate()
        with self.assertRaises(ValueError):
            MemoryQuery(self.org["id"], self.repo["id"], self.sha, token_budget=-1).validate()

        memory_id = self.memory.add(self._write())
        with self.assertRaises(ValueError):
            self.memory.add_edge(
                organization_id=self.org["id"], repository_id=self.repo["id"],
                source_sha="bad", memory_id=memory_id, relation="imports",
            )
        edge_id = self.memory.add_edge(
            organization_id=self.org["id"], repository_id=self.repo["id"],
            source_sha=self.sha, memory_id=memory_id, relation="imports",
            path="CONVENTIONS.md", symbol="transaction",
        )
        self.assertEqual(len(edge_id), 64)
        self.assertTrue(self.memory.invalidate(self.org["id"], self.repo["id"], memory_id))
        self.assertFalse(self.memory.invalidate(self.org["id"], self.repo["id"], memory_id))

        policy = OrganizationPolicy(
            organization_id=self.org["id"], version="policy/validation",
            severity_levels=("high",), forbidden_operations=(), allowed_tools=(),
            approval_threshold=1, retention_days=1, cost_budget_microusd=0,
            created_by="admin", reason="test", source_sha=self.sha,
        )
        with self.assertRaises(MemoryTrustError):
            self.policies.put(policy, source_kind=MemorySource.HUMAN_CONFIRMED)
        with self.assertRaises(MemoryTrustError):
            OrganizationPolicy(**{**policy.__dict__, "approval_threshold": 0}).validate()
        self.policies.put(policy, source_kind=MemorySource.ADMIN_CONFIG)
        self.assertIsNotNone(self.policies.active(self.org["id"], version=policy.version))
        self.assertIn("allowed_tools=none", render_policy(policy))
        self.assertEqual(render_policy(None), "")

        checkout = self.root / "git-checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir()
        (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
        (checkout / ".git" / "packed-refs").write_text(
            f"{self.sha} refs/heads/main\n", encoding="ascii"
        )
        self.assertEqual(repository_source_sha(checkout), self.sha)
        self.assertIsNone(repository_source_sha(self.root / "missing"))


if __name__ == "__main__":
    unittest.main()
