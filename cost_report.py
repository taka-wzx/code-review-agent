"""Token/cost report from run traces (W7).

Reads the JSONL traces a run leaves behind (agent.py --trace PATH, or the
traces/ folder inside any run_eval/repeat_eval results dir) and aggregates
LLM usage per component and per diff. Prices are optional -- pass them per
million tokens to get a cost column (they change too often to hardcode).

Judge calls are not traced (judge.py has no trace hook); the report covers
finder + verifier only, and says so.

Usage:
    python cost_report.py eval/results_repeat/v2_run1 [more dirs/files...]
                          [--per-diff] [--price-in 2.0 --price-out 8.0]
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def iter_trace_files(targets: list[str]):
    for t in targets:
        p = Path(t)
        if p.is_file() and p.suffix == ".jsonl":
            yield p
        elif p.is_dir():
            sub = p / "traces" if (p / "traces").is_dir() else p
            yield from sorted(sub.glob("*.jsonl"))
        else:
            print(f"[warn] no traces at {t}", file=sys.stderr)


def collect(trace_files) -> dict:
    per_diff = defaultdict(lambda: defaultdict(lambda: {
        "calls": 0, "tokens_in": 0, "tokens_out": 0, "tool_calls": 0}))
    for tf in trace_files:
        name = tf.stem
        for line in tf.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            comp = e.get("component", "?")
            if e.get("kind") == "llm_response":
                d = per_diff[name][comp]
                d["calls"] += 1
                d["tokens_in"] += e.get("tokens_in", 0)
                d["tokens_out"] += e.get("tokens_out", 0)
            elif e.get("kind") == "tool":
                per_diff[name][comp]["tool_calls"] += 1
    return per_diff


def main():
    parser = argparse.ArgumentParser(description="Token/cost report from traces")
    parser.add_argument("targets", nargs="+",
                        help="Results dirs, traces dirs, or *.jsonl trace files")
    parser.add_argument("--per-diff", action="store_true",
                        help="Print one row per diff instead of totals only")
    parser.add_argument("--price-in", type=float, default=None,
                        help="Input price per 1M tokens (any currency)")
    parser.add_argument("--price-out", type=float, default=None,
                        help="Output price per 1M tokens (same currency)")
    args = parser.parse_args()

    per_diff = collect(iter_trace_files(args.targets))
    if not per_diff:
        sys.exit("no trace events found")

    def cost(tin, tout):
        if args.price_in is None or args.price_out is None:
            return None
        return tin / 1e6 * args.price_in + tout / 1e6 * args.price_out

    comp_tot = defaultdict(lambda: {"calls": 0, "tokens_in": 0,
                                    "tokens_out": 0, "tool_calls": 0})
    for name, comps in per_diff.items():
        for comp, d in comps.items():
            for k in d:
                comp_tot[comp][k] += d[k]

    if args.per_diff:
        print(f"{'diff':<22} {'comp':<9} {'llm':>4} {'tools':>6} "
              f"{'tok_in':>9} {'tok_out':>8}" + ("  cost" if cost(0, 0) is not None else ""))
        for name in sorted(per_diff):
            for comp in sorted(per_diff[name]):
                d = per_diff[name][comp]
                c = cost(d["tokens_in"], d["tokens_out"])
                print(f"{name:<22} {comp:<9} {d['calls']:>4} {d['tool_calls']:>6} "
                      f"{d['tokens_in']:>9,} {d['tokens_out']:>8,}"
                      + (f"  {c:.4f}" if c is not None else ""))
        print()

    print(f"{'component':<9} {'llm_calls':>9} {'tool_calls':>10} "
          f"{'tokens_in':>11} {'tokens_out':>10}"
          + ("  cost" if cost(0, 0) is not None else ""))
    g_in = g_out = 0
    for comp in sorted(comp_tot):
        d = comp_tot[comp]
        g_in += d["tokens_in"]; g_out += d["tokens_out"]
        c = cost(d["tokens_in"], d["tokens_out"])
        print(f"{comp:<9} {d['calls']:>9} {d['tool_calls']:>10} "
              f"{d['tokens_in']:>11,} {d['tokens_out']:>10,}"
              + (f"  {c:.4f}" if c is not None else ""))
    c = cost(g_in, g_out)
    print(f"{'TOTAL':<9} {sum(d['calls'] for d in comp_tot.values()):>9} "
          f"{sum(d['tool_calls'] for d in comp_tot.values()):>10} "
          f"{g_in:>11,} {g_out:>10,}"
          + (f"  {c:.4f}" if c is not None else ""))
    print(f"\n({len(per_diff)} trace file(s); judge calls are not traced "
          "and are excluded)")


if __name__ == "__main__":
    main()
