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
import sys
from pathlib import Path

from agent import run_review, make_client

# Windows redirects default to GBK; model output may contain any unicode
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
DIFFS = HERE / "eval" / "diffs"
RESULTS = HERE / "eval" / "results"


def main():
    parser = argparse.ArgumentParser(description="Batch eval runner")
    parser.add_argument("--repo", default=str(HERE / "eval" / "repo"),
                        help="Repo root for context retrieval (default: eval fixture repo)")
    parser.add_argument("--no-context", action="store_true",
                        help="Skip proactive context retrieval (ablation baseline)")
    parser.add_argument("--results-dir", default=str(RESULTS),
                        help="Where to write per-diff review JSONs")
    args = parser.parse_args()
    results_dir = Path(args.results_dir)
    print(f"repo={args.repo}  context={'OFF (ablation)' if args.no_context else 'ON'}  "
          f"results={results_dir}")

    diffs = sorted(DIFFS.glob("*.diff"))
    if not diffs:
        sys.exit(f"No diffs found in {DIFFS}")

    results_dir.mkdir(parents=True, exist_ok=True)
    client, model = make_client()

    for diff_path in diffs:
        name = diff_path.stem
        print(f"\n{'='*60}\n{name}\n{'='*60}")
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
        try:
            review = run_review(client, diff_text, Path(args.repo), model,
                                use_context=not args.no_context)
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            continue

        (results_dir / f"{name}.json").write_text(
            json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        for f in review["findings"]:
            print(f"  [{f['severity']:<6}] {f['file']}:{f['line']}  {f['issue']}")

    print(f"\nDone. Results in {results_dir}/. "
          f"Score them with: python judge.py --results-dir {results_dir}")


if __name__ == "__main__":
    main()
