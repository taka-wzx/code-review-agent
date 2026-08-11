import json
import unittest

import phase11d_gate_b_executor as gate_b


class Phase11DPilotV3C11Tests(unittest.TestCase):
    def test_duplicate_json_keys_fail_closed(self) -> None:
        with self.assertRaisesRegex(gate_b.GateBExecutorError, "duplicate_json_key"):
            json.loads('{"state":"open","state":"closed"}', object_pairs_hook=gate_b._reject_duplicate_keys)


if __name__ == "__main__":
    unittest.main()

