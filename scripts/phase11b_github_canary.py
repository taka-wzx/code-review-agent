"""Run the installed Phase 11B canary executor without adding a package entry point."""
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from code_review_agent.github_canary_executor import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
