import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C17Tests(unittest.TestCase):
    def test_blob_sha_matches_git_canonical_object_hash(self) -> None:
        patch = gate_b.SandboxPatchFile(path="hello.txt", content=b"hello\n")
        self.assertEqual(patch.blob_sha, "ce013625030ba8dba906f756967f9e9ca394464a")


if __name__ == "__main__":
    unittest.main()

