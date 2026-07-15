"""Phase 1 tests for repair states, budgets, and approval records."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import unittest

from code_review_agent.repair_approval import (
    ApprovalConsumed,
    ApprovalExpired,
    ApprovalKind,
    ApprovalMismatch,
    ApprovalRecord,
    issue_commit_approval,
    issue_write_approval,
)
from code_review_agent.repair_budget import (
    BudgetAccountingError,
    BudgetExceeded,
    BudgetLimits,
    BudgetManager,
)
from code_review_agent.repair_state import (
    IllegalTransitionError,
    RepairState,
    RepairStateMachine,
    allowed_targets,
)


class TestRepairStateMachine(unittest.TestCase):
    def test_happy_path_and_repair_loop(self):
        machine = RepairStateMachine()
        for state in (
            RepairState.PLAN,
            RepairState.PATCH,
            RepairState.TEST,
            RepairState.REFLECT,
            RepairState.PATCH,
            RepairState.TEST,
            RepairState.REFLECT,
            RepairState.WAIT_APPROVAL,
            RepairState.SUBMIT,
        ):
            machine.transition(state)
        self.assertEqual(machine.state, RepairState.SUBMIT)
        self.assertEqual(machine.history.count(RepairState.PATCH), 2)

    def test_illegal_transition_does_not_mutate_history(self):
        machine = RepairStateMachine()
        with self.assertRaisesRegex(IllegalTransitionError, "DISCOVER -> PATCH"):
            machine.transition(RepairState.PATCH)
        self.assertEqual(machine.state, RepairState.DISCOVER)
        self.assertEqual(machine.history, [RepairState.DISCOVER])

    def test_failed_and_cancelled_are_terminal(self):
        for terminal in (RepairState.FAILED, RepairState.CANCELLED):
            machine = RepairStateMachine()
            machine.transition(terminal)
            self.assertEqual(allowed_targets(terminal), frozenset())
            with self.assertRaises(IllegalTransitionError):
                machine.transition(RepairState.DISCOVER)

    def test_submit_can_return_for_new_commit_approval(self):
        machine = RepairStateMachine(
            state=RepairState.SUBMIT,
            history=[RepairState.DISCOVER, RepairState.SUBMIT],
        )
        machine.transition(RepairState.WAIT_APPROVAL)
        self.assertEqual(machine.state, RepairState.WAIT_APPROVAL)

    def test_history_must_end_at_current_state(self):
        with self.assertRaisesRegex(ValueError, "history must end"):
            RepairStateMachine(RepairState.PLAN, [RepairState.DISCOVER])


class TestBudgetManager(unittest.TestCase):
    def test_default_limits_match_contract(self):
        limits = BudgetLimits()
        self.assertEqual(limits.total_seconds, 1_800.0)
        self.assertEqual(limits.total_tokens, 80_000)
        self.assertEqual(limits.total_cost_usd, 1.0)
        self.assertEqual(limits.tool_calls, 100)
        self.assertEqual(limits.repair_attempts, 2)
        self.assertEqual(limits.command_seconds, 300.0)
        self.assertEqual(limits.command_output_bytes, 1024 * 1024)

    def test_invalid_limits_rejected(self):
        cases = (
            {"total_seconds": 0},
            {"total_tokens": True},
            {"total_cost_usd": float("nan")},
            {"tool_calls": -1},
            {"repair_attempts": -1},
            {"command_output_bytes": 0},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                BudgetLimits(**values)

    def test_reserve_reconcile_and_restore_active_reservation(self):
        manager = BudgetManager(BudgetLimits(total_tokens=100, total_cost_usd=1.0))
        first = manager.reserve_llm(40, 0.4)
        manager.reconcile_llm(first.reservation_id, 25, 0.2)
        active = manager.reserve_llm(30, 0.3)
        snapshot = manager.to_dict()

        restored = BudgetManager.from_dict(snapshot)
        self.assertEqual(restored.remaining()["tokens"], 45.0)
        restored.cancel_llm(active.reservation_id)
        self.assertEqual(restored.remaining()["tokens"], 75.0)
        with self.assertRaises(BudgetAccountingError):
            restored.cancel_llm(active.reservation_id)

    def test_parallel_reservation_cannot_overbook(self):
        manager = BudgetManager(BudgetLimits(total_tokens=100, total_cost_usd=1.0))

        def reserve():
            try:
                return manager.reserve_llm(60, 0.6)
            except BudgetExceeded as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: reserve(), range(2)))
        self.assertEqual(sum(not isinstance(item, Exception) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, BudgetExceeded) for item in outcomes), 1)

    def test_tool_elapsed_and_repair_limits_fail_before_mutation(self):
        manager = BudgetManager(
            BudgetLimits(total_seconds=5, tool_calls=2, repair_attempts=1)
        )
        manager.consume_elapsed(4)
        manager.consume_tool_call(2, command=True)
        manager.consume_repair_attempt()
        with self.assertRaises(BudgetExceeded):
            manager.consume_elapsed(2)
        with self.assertRaises(BudgetExceeded):
            manager.consume_tool_call()
        with self.assertRaises(BudgetExceeded):
            manager.consume_repair_attempt()
        self.assertEqual(manager.usage.elapsed_seconds, 4)
        self.assertEqual(manager.usage.tool_calls, 2)
        self.assertEqual(manager.usage.commands, 2)
        self.assertEqual(manager.usage.repair_attempts, 1)

    def test_actual_usage_over_reservation_is_retained_and_fails_closed(self):
        manager = BudgetManager(BudgetLimits(total_tokens=100, total_cost_usd=1.0))
        reservation = manager.reserve_llm(10, 0.1)
        with self.assertRaises(BudgetAccountingError):
            manager.reconcile_llm(reservation.reservation_id, 11, 0.11)
        self.assertEqual(manager.usage.tokens, 11)
        self.assertEqual(manager.usage.cost_usd, 0.11)
        self.assertEqual(manager.to_dict()["reservations"], [])
        restored = BudgetManager.from_dict(manager.to_dict())
        with self.assertRaisesRegex(BudgetAccountingError, "prior violation"):
            restored.reserve_llm(1, 0.0)

    def test_zero_cost_reservation_still_consumes_token_budget(self):
        manager = BudgetManager(BudgetLimits(total_tokens=10))
        reservation = manager.reserve_llm(10, 0.0)
        self.assertEqual(manager.remaining()["tokens"], 0.0)
        manager.reconcile_llm(reservation.reservation_id, 8, 0.0)
        self.assertEqual(manager.remaining()["tokens"], 2.0)

    def test_corrupt_reservation_snapshot_cannot_create_budget_credit(self):
        manager = BudgetManager()
        snapshot = manager.to_dict()
        snapshot["reservations"] = [
            {"reservation_id": "bad", "tokens": -1, "cost_usd": -1.0}
        ]
        with self.assertRaisesRegex(ValueError, "invalid budget snapshot"):
            BudgetManager.from_dict(snapshot)


class TestApprovals(unittest.TestCase):
    @staticmethod
    def write_approval(**overrides):
        values = {
            "run_id": "run-1",
            "checkpoint_id": "cp-1",
            "base_sha": "a" * 40,
            "diff_hash": "d" * 64,
            "plan_hash": "p" * 64,
            "writable_paths": ("src/b.py", "src/a.py"),
            "patch_attempt": 1,
            "ttl_seconds": 60,
            "now": 100.0,
            "nonce": "human-nonce",
        }
        values.update(overrides)
        return issue_write_approval(**values)

    def test_write_binding_is_normalized_consumed_once_and_serializable(self):
        record = self.write_approval()
        self.assertEqual(record.binding.kind, ApprovalKind.WRITE)
        self.assertEqual(record.binding.writable_paths, ("src/a.py", "src/b.py"))
        consumed = record.consume(record.binding, now=120.0)
        self.assertEqual(consumed.consumed_at, 120.0)
        self.assertEqual(ApprovalRecord.from_dict(consumed.to_dict()), consumed)
        with self.assertRaises(ApprovalConsumed):
            consumed.consume(consumed.binding, now=121.0)

    def test_stale_or_mismatched_approval_is_rejected(self):
        record = self.write_approval()
        with self.assertRaises(ApprovalMismatch):
            record.consume(replace(record.binding, diff_hash="changed"), now=120.0)
        with self.assertRaises(ApprovalExpired):
            record.consume(record.binding, now=160.0)

    def test_each_patch_attempt_needs_a_distinct_binding(self):
        first = self.write_approval(patch_attempt=1)
        second = self.write_approval(patch_attempt=2)
        self.assertNotEqual(first.binding, second.binding)
        with self.assertRaises(ApprovalMismatch):
            first.consume(second.binding, now=120.0)

    def test_commit_approval_has_no_write_scope(self):
        record = issue_commit_approval(
            run_id="run-1",
            checkpoint_id="cp-9",
            base_sha="a" * 40,
            diff_hash="d" * 64,
            test_result_hash="t" * 64,
            commit_message="fix: small issue",
            ttl_seconds=60,
            now=100.0,
            nonce="human-commit-nonce",
        )
        self.assertEqual(record.binding.kind, ApprovalKind.COMMIT)
        self.assertEqual(record.binding.writable_paths, ())
        self.assertEqual(ApprovalRecord.from_dict(record.to_dict()), record)

    def test_unsafe_write_paths_are_never_bindable(self):
        for path in ("../outside.py", "/absolute.py", ".git/config", "C:/secret.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.write_approval(writable_paths=(path,))

    def test_serialized_paths_must_be_a_list_not_one_string(self):
        data = self.write_approval().binding.to_dict()
        data["writable_paths"] = "src/mod.py"
        with self.assertRaisesRegex(ValueError, "list of strings"):
            type(self.write_approval().binding).from_dict(data)


if __name__ == "__main__":
    unittest.main()
