"""Run the Phase 11A synthetic-only staging validation suite.

This intentionally imports only the targeted offline test module.  It never enables
an eval-assets mode, reads ``eval/``, contacts a provider, or writes a repository.
"""
from __future__ import annotations

import unittest
from pathlib import Path
import sys


def main() -> int:
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_phase11a_synthetic_staging"
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
