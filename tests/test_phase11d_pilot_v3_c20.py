import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C20Tests(unittest.TestCase):
    def test_sandbox_result_rejects_duplicate_patch_paths(self) -> None:
        first = gate_b.SandboxPatchFile(path="src/example.py", content=b"x = 1\n")
        second = gate_b.SandboxPatchFile(path="src/example.py", content=b"x = 2\n")
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "sandbox_patch_duplicate_path"):
            gate_b.SandboxResult(
                repair_job_id="repair-104", worktree_receipt_sha256="1" * 64,
                task_branch_sha256="2" * 64, patch_sha256="3" * 64,
                checkpoint_sha256="4" * 64, test_sha256="5" * 64,
                budget_sha256="6" * 64, tests_passed=False, reflection_passed=False,
                exact_commit_sha="", expected_tree_sha="", patch_files=(first, second),
            )

    def test_duplicate_path_rejected_across_file_modes(self) -> None:
        regular = gate_b.SandboxPatchFile(path="scripts/check.py", content=b"pass\n")
        executable = gate_b.SandboxPatchFile(
            path="scripts/check.py", content=b"pass\n", mode="100755"
        )
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "sandbox_patch_duplicate_path"):
            gate_b.SandboxResult(
                repair_job_id="repair-123", worktree_receipt_sha256="1" * 64,
                task_branch_sha256="2" * 64, patch_sha256="3" * 64,
                checkpoint_sha256="4" * 64, test_sha256="5" * 64,
                budget_sha256="6" * 64, tests_passed=False, reflection_passed=False,
                exact_commit_sha="", expected_tree_sha="", patch_files=(regular, executable),
            )


if __name__ == "__main__":
    unittest.main()

