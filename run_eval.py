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

from agent import run_review
from llm import make_client
from tracelog import Trace, force_utf8

force_utf8()

HERE = Path(__file__).parent
DIFFS = HERE / "eval" / "diffs"
RESULTS = HERE / "eval" / "results"


def main():
    parser = argparse.ArgumentParser(description="Batch eval runner")
    parser.add_argument("--repo", default=str(HERE / "eval" / "repo"),
                        help="Repo root for context retrieval (default: eval fixture repo)")
    parser.add_argument("--no-context", action="store_true",
                        help="Skip proactive context retrieval (ablation baseline)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip the verifier second pass (ablation)")
    parser.add_argument("--results-dir", default=str(RESULTS),
                        help="Where to write per-diff review JSONs")
    parser.add_argument("--diffs-dir", default=str(DIFFS),
                        help="Directory of *.diff files (e.g. eval/holdout/diffs)")
    parser.add_argument("--only", default="",
                        help="Comma-separated diff stems to run (default: all)")
    args = parser.parse_args()
    results_dir = Path(args.results_dir)
    print(f"repo={args.repo}  context={'OFF' if args.no_context else 'ON'}  "
          f"verify={'OFF' if args.no_verify else 'ON'}  results={results_dir}")

    diffs = sorted(Path(args.diffs_dir).glob("*.diff"))
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        diffs = [d for d in diffs if d.stem in wanted]
        missing = wanted - {d.stem for d in diffs}
        if missing:
            sys.exit(f"--only names not found in {args.diffs_dir}: {sorted(missing)}")
    if not diffs:
        sys.exit(f"No diffs found in {args.diffs_dir}")

    results_dir.mkdir(parents=True, exist_ok=True)
    client, model = make_client()

    for diff_path in diffs:
        name = diff_path.stem
        print(f"\n{'='*60}\n{name}\n{'='*60}")
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
        trace = Trace(results_dir / "traces" / f"{name}.jsonl")
        try:
            review = run_review(client, diff_text, Path(args.repo), model,
                                use_context=not args.no_context,
                                use_verify=not args.no_verify,
                                trace=trace)
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            continue
        finally:
            trace.close()

        (results_dir / f"{name}.json").write_text(
            json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        for f in review["findings"]:
            print(f"  [{f['severity']:<6}] {f['file']}:{f['line']}  {f['issue']}")

    print(f"\nDone. Results in {results_dir}/. "
          f"Score them with: python judge.py --results-dir {results_dir}")


if __name__ == "__main__":
    main()
