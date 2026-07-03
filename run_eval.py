"""Batch-run the review agent over the eval set (W1).

Runs agent on every eval/diffs/*.diff, saves each review to eval/results/,
and prints the findings so you can score them by hand against eval/cases.md.
Deliberately does NOT auto-score — semantic match of free-text findings is
unreliable; human scoring against cases.md is the baseline for W1.

Usage:
    python run_eval.py [--repo path/to/repo]
"""
import argparse
import json
import os
import sys
from pathlib import Path

import anthropic

from agent import run_review

HERE = Path(__file__).parent
DIFFS = HERE / "eval" / "diffs"
RESULTS = HERE / "eval" / "results"


def main():
    parser = argparse.ArgumentParser(description="Batch eval runner")
    parser.add_argument("--repo", default=".", help="Repo root for read_file context")
    args = parser.parse_args()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("No credentials: set ANTHROPIC_API_KEY first")

    diffs = sorted(DIFFS.glob("*.diff"))
    if not diffs:
        sys.exit(f"No diffs found in {DIFFS}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    client = anthropic.Anthropic(timeout=120.0)

    for diff_path in diffs:
        name = diff_path.stem
        print(f"\n{'='*60}\n{name}\n{'='*60}")
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
        try:
            review = run_review(client, diff_text, Path(args.repo))
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            continue

        (RESULTS / f"{name}.json").write_text(
            json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        for f in review["findings"]:
            print(f"  [{f['severity']:<6}] {f['file']}:{f['line']}  {f['issue']}")

    print(f"\nDone. Results in {RESULTS}/. "
          f"Now score them by hand against eval/cases.md.")


if __name__ == "__main__":
    main()
