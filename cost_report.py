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
import sys
from collections import defaultdict
from pathlib import Path

from tracelog import force_utf8, iter_events

force_utf8()


def expand_run_dirs(target: Path) -> list[Path]:
    """A repeat_eval output root (contains *_run*/traces) expands to its run
    dirs so stats can be averaged per run; anything else is itself one unit."""
    if target.is_dir() and not (target / "traces").is_dir():
        runs = sorted(d for d in target.glob("*_run*")
                      if (d / "traces").is_dir())
        if runs:
            return runs
    return [target]


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
        for e in iter_events(tf):
            comp = e.get("component", "?")
            if e.get("kind") == "llm_response":
                d = per_diff[name][comp]
                d["calls"] += 1
                d["tokens_in"] += e.get("tokens_in", 0)
                d["tokens_out"] += e.get("tokens_out", 0)
            elif e.get("kind") == "tool":
                per_diff[name][comp]["tool_calls"] += 1
    return per_diff


# search_repo miss detection on traces. New traces carry an explicit "miss"
# field; older ones only have result_chars -- a clean no-match reply is short
# (<150 chars), and a nudged one (3+ streak, W11) sits in a ~[380,560] band
# because the nudge suffix is fixed-length. Heuristic, stated in the output.
MISS_CHARS = 150
NUDGED_MISS_BAND = (380, 560)


def _is_miss(e: dict) -> bool:
    if "miss" in e:
        return bool(e["miss"])
    chars = e.get("result_chars", 10 ** 9)
    return chars < MISS_CHARS or NUDGED_MISS_BAND[0] <= chars <= NUDGED_MISS_BAND[1]


def run_stats(run_dir) -> dict:
    """Per-component stats for one run: tokens, calls, mean max-step per diff,
    search misses and calls inside 3+ consecutive-miss chains."""
    stats = defaultdict(lambda: {"tokens_in": 0, "tokens_out": 0, "calls": 0,
                                 "tool_calls": 0, "steps": [], "miss": 0,
                                 "chain_calls": 0})
    for tf in iter_trace_files([str(run_dir)]):
        maxstep = defaultdict(int)
        streak = defaultdict(int)
        for e in iter_events(tf):
            comp = e.get("component", "?")
            d = stats[comp]
            if e.get("kind") == "llm_response":
                d["calls"] += 1
                d["tokens_in"] += e.get("tokens_in", 0)
                d["tokens_out"] += e.get("tokens_out", 0)
                maxstep[comp] = max(maxstep[comp], e.get("step", 0))
            elif e.get("kind") == "tool":
                d["tool_calls"] += 1
                if e.get("tool") == "search_repo" and not e.get("repeat"):
                    if _is_miss(e):
                        d["miss"] += 1
                        streak[comp] += 1
                        if streak[comp] >= 3:
                            d["chain_calls"] += 1
                    else:
                        streak[comp] = 0
        for comp, s in maxstep.items():
            stats[comp]["steps"].append(s)
    return stats


def mean_run_stats(target: str) -> tuple[dict, int]:
    """Average run_stats over the run dirs a target expands to."""
    runs = expand_run_dirs(Path(target))
    acc = defaultdict(lambda: defaultdict(float))
    for rd in runs:
        for comp, d in run_stats(rd).items():
            a = acc[comp]
            for k in ("tokens_in", "tokens_out", "calls", "tool_calls",
                      "miss", "chain_calls"):
                a[k] += d[k]
            a["step_sum"] += sum(d["steps"])
            a["step_n"] += len(d["steps"])
    n = max(1, len(runs))
    out = {}
    for comp, a in acc.items():
        out[comp] = {k: a[k] / n for k in ("tokens_in", "tokens_out", "calls",
                                           "tool_calls", "miss", "chain_calls")}
        out[comp]["step_depth"] = (a["step_sum"] / a["step_n"]) if a["step_n"] else 0.0
    return out, len(runs)


def print_summary(label: str, stats: dict, n_runs: int, baseline: dict = None):
    print(f"\n== {label} (mean over {n_runs} run(s); search-miss detection is "
          "heuristic on old traces)")
    hdr = (f"{'component':<10} {'tok_in':>9} {'tok_out':>8} {'calls':>6} "
           f"{'depth':>6} {'miss':>5} {'chain':>6}")
    print(hdr + ("   [delta vs baseline]" if baseline else ""))
    for comp in sorted(stats):
        d = stats[comp]
        if not (d["calls"] or d["tool_calls"]):
            continue  # context-builder events carry no component
        row = (f"{comp:<10} {d['tokens_in']:>9,.0f} {d['tokens_out']:>8,.0f} "
               f"{d['calls']:>6.1f} {d['step_depth']:>6.2f} {d['miss']:>5.1f} "
               f"{d['chain_calls']:>6.1f}")
        if baseline and comp in baseline:
            b = baseline[comp]
            row += (f"   [in {d['tokens_in']-b['tokens_in']:+,.0f}, "
                    f"calls {d['calls']-b['calls']:+.1f}, "
                    f"chain {d['chain_calls']-b['chain_calls']:+.1f}]")
        print(row)
    tin = sum(d["tokens_in"] for d in stats.values())
    tout = sum(d["tokens_out"] for d in stats.values())
    line = f"{'TOTAL':<10} {tin:>9,.0f} {tout:>8,.0f}"
    if baseline:
        btin = sum(d["tokens_in"] for d in baseline.values())
        pct = (tin - btin) / btin * 100 if btin else 0
        line += f"   [in {tin-btin:+,.0f} = {pct:+.1f}%]"
    print(line)


def main():
    parser = argparse.ArgumentParser(description="Token/cost report from traces")
    parser.add_argument("targets", nargs="+",
                        help="Results dirs, repeat roots, traces dirs, or *.jsonl files")
    parser.add_argument("--per-diff", action="store_true",
                        help="Print one row per diff instead of totals only")
    parser.add_argument("--price-in", type=float, default=None,
                        help="Input price per 1M tokens (any currency)")
    parser.add_argument("--price-out", type=float, default=None,
                        help="Output price per 1M tokens (same currency)")
    parser.add_argument("--baseline", default=None,
                        help="Results dir or repeat root to diff against "
                             "(per-run means, by component)")
    args = parser.parse_args()

    if args.baseline:
        base_stats, bn = mean_run_stats(args.baseline)
        print_summary(f"baseline: {args.baseline}", base_stats, bn)
        for t in args.targets:
            cur, cn = mean_run_stats(t)
            print_summary(t, cur, cn, baseline=base_stats)
        return

    expanded = [str(rd) for t in args.targets for rd in expand_run_dirs(Path(t))]
    per_diff = collect(iter_trace_files(expanded))
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
