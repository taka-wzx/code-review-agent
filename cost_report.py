"""Token/cost report from run traces (W7).

Reads the JSONL traces a run leaves behind (agent.py --trace PATH, or the
traces/ folder inside any run_eval/repeat_eval results dir) and aggregates
LLM usage per component and per diff. Prices are optional -- pass them per
million tokens to get a cost column (they change too often to hardcode).

Judge calls are not traced (judge.py has no trace hook); the report covers
finder + verifier only, and says so.

Cache-aware pricing (W14): providers bill cache-hit input tokens separately
(DeepSeek: ~1/10 of the miss price), so on high-hit agent loops raw
tokens_in overstates the real bill by up to an order of magnitude. Pass
--price-hit to price recorded hits; traces predating the W13 cache fields
carry no hits and are billed as all-miss (a conservative upper bound).

Usage:
    python cost_report.py eval/results_repeat/v2_run1 [more dirs/files...]
                          [--per-diff] [--price-in 2.0 --price-out 8.0]
                          [--price-hit 0.2]
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

from tracelog import force_utf8, iter_events

force_utf8()


def billed_cost(tokens_in: float, tokens_out: float, cache_hit: float,
                price_in: float, price_out: float,
                price_hit: float = None):
    """Real billed cost for one aggregate of events. cache_hit is the hit
    volume the events actually recorded; events without cache fields add
    nothing to it, so their whole tokens_in is billed at the miss price.
    price_hit defaults to price_in (no discount), which keeps invocations
    that predate --price-hit meaning what they always did."""
    if price_in is None or price_out is None:
        return None
    if price_hit is None:
        price_hit = price_in
    return ((tokens_in - cache_hit) / 1e6 * price_in
            + cache_hit / 1e6 * price_hit
            + tokens_out / 1e6 * price_out)


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
        "calls": 0, "tokens_in": 0, "tokens_out": 0, "tool_calls": 0,
        "cache_hit": 0}))
    for tf in trace_files:
        name = tf.stem
        for e in iter_events(tf):
            comp = e.get("component", "?")
            if e.get("kind") == "llm_response":
                d = per_diff[name][comp]
                d["calls"] += 1
                d["tokens_in"] += e.get("tokens_in", 0)
                d["tokens_out"] += e.get("tokens_out", 0)
                d["cache_hit"] += e.get("cache_hit", 0)
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
                                 "chain_calls": 0, "cache_hit": 0,
                                 "cache_seen_in": 0})
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
                # cache rate only over events that carry the fields (W13+
                # traces); old traces stay blank rather than reading 0%.
                if "cache_hit" in e:
                    d["cache_hit"] += e["cache_hit"]
                    d["cache_seen_in"] += e.get("tokens_in", 0)
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
                      "miss", "chain_calls", "cache_hit", "cache_seen_in"):
                a[k] += d[k]
            a["step_sum"] += sum(d["steps"])
            a["step_n"] += len(d["steps"])
    n = max(1, len(runs))
    out = {}
    for comp, a in acc.items():
        out[comp] = {k: a[k] / n for k in ("tokens_in", "tokens_out", "calls",
                                           "tool_calls", "miss", "chain_calls",
                                           "cache_hit", "cache_seen_in")}
        out[comp]["step_depth"] = (a["step_sum"] / a["step_n"]) if a["step_n"] else 0.0
    return out, len(runs)


def print_summary(label: str, stats: dict, n_runs: int, baseline: dict = None,
                  prices: tuple = None):
    """prices, when given, is (price_in, price_out, price_hit) per 1M tokens
    and adds a real-billed cost column (see billed_cost)."""
    print(f"\n== {label} (mean over {n_runs} run(s); search-miss detection is "
          "heuristic on old traces)")
    hdr = (f"{'component':<10} {'tok_in':>9} {'tok_out':>8} {'calls':>6} "
           f"{'depth':>6} {'miss':>5} {'chain':>6} {'cache%':>7}")
    if prices:
        hdr += f" {'cost':>9}"
    print(hdr + ("   [delta vs baseline]" if baseline else ""))
    for comp in sorted(stats):
        d = stats[comp]
        if not (d["calls"] or d["tool_calls"]):
            continue  # context-builder events carry no component
        seen = d.get("cache_seen_in", 0)
        cache = (f"{d['cache_hit'] / seen * 100:>6.1f}%" if seen
                 else f"{'-':>7}")
        row = (f"{comp:<10} {d['tokens_in']:>9,.0f} {d['tokens_out']:>8,.0f} "
               f"{d['calls']:>6.1f} {d['step_depth']:>6.2f} {d['miss']:>5.1f} "
               f"{d['chain_calls']:>6.1f} {cache}")
        if prices:
            c = billed_cost(d["tokens_in"], d["tokens_out"],
                            d.get("cache_hit", 0), *prices)
            row += f" {c:>9.4f}"
        if baseline and comp in baseline:
            b = baseline[comp]
            row += (f"   [in {d['tokens_in']-b['tokens_in']:+,.0f}, "
                    f"calls {d['calls']-b['calls']:+.1f}, "
                    f"chain {d['chain_calls']-b['chain_calls']:+.1f}]")
        print(row)
    tin = sum(d["tokens_in"] for d in stats.values())
    tout = sum(d["tokens_out"] for d in stats.values())
    thit = sum(d.get("cache_hit", 0) for d in stats.values())
    tseen = sum(d.get("cache_seen_in", 0) for d in stats.values())
    line = f"{'TOTAL':<10} {tin:>9,.0f} {tout:>8,.0f}"
    if tseen:
        line += f"  cache {thit / tseen * 100:.1f}%"
    if prices:
        tot_cost = billed_cost(tin, tout, thit, *prices)
        line += f"  cost {tot_cost:.4f}"
    if baseline:
        btin = sum(d["tokens_in"] for d in baseline.values())
        pct = (tin - btin) / btin * 100 if btin else 0
        line += f"   [in {tin-btin:+,.0f} = {pct:+.1f}%"
        if prices:
            bcost = billed_cost(btin,
                                sum(d["tokens_out"] for d in baseline.values()),
                                sum(d.get("cache_hit", 0) for d in baseline.values()),
                                *prices)
            bpct = (tot_cost - bcost) / bcost * 100 if bcost else 0
            line += f", cost {tot_cost-bcost:+.4f} = {bpct:+.1f}%"
        line += "]"
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
    parser.add_argument("--price-hit", type=float, default=None,
                        help="Cache-hit input price per 1M tokens (default: "
                             "same as --price-in, i.e. no cache discount)")
    parser.add_argument("--baseline", default=None,
                        help="Results dir or repeat root to diff against "
                             "(per-run means, by component)")
    args = parser.parse_args()

    have_prices = args.price_in is not None and args.price_out is not None
    prices = ((args.price_in, args.price_out, args.price_hit)
              if have_prices else None)

    if args.baseline:
        base_stats, bn = mean_run_stats(args.baseline)
        print_summary(f"baseline: {args.baseline}", base_stats, bn,
                      prices=prices)
        for t in args.targets:
            cur, cn = mean_run_stats(t)
            print_summary(t, cur, cn, baseline=base_stats, prices=prices)
        return

    expanded = [str(rd) for t in args.targets for rd in expand_run_dirs(Path(t))]
    per_diff = collect(iter_trace_files(expanded))
    if not per_diff:
        sys.exit("no trace events found")

    def cost(d):
        if not have_prices:
            return None
        return billed_cost(d["tokens_in"], d["tokens_out"],
                           d.get("cache_hit", 0), *prices)

    comp_tot = defaultdict(lambda: {"calls": 0, "tokens_in": 0,
                                    "tokens_out": 0, "tool_calls": 0,
                                    "cache_hit": 0})
    for name, comps in per_diff.items():
        for comp, d in comps.items():
            for k in d:
                comp_tot[comp][k] += d[k]

    if args.per_diff:
        print(f"{'diff':<22} {'comp':<9} {'llm':>4} {'tools':>6} "
              f"{'tok_in':>9} {'tok_out':>8}" + ("  cost" if have_prices else ""))
        for name in sorted(per_diff):
            for comp in sorted(per_diff[name]):
                d = per_diff[name][comp]
                c = cost(d)
                print(f"{name:<22} {comp:<9} {d['calls']:>4} {d['tool_calls']:>6} "
                      f"{d['tokens_in']:>9,} {d['tokens_out']:>8,}"
                      + (f"  {c:.4f}" if c is not None else ""))
        print()

    print(f"{'component':<9} {'llm_calls':>9} {'tool_calls':>10} "
          f"{'tokens_in':>11} {'tokens_out':>10}"
          + ("  cost" if have_prices else ""))
    g = {"tokens_in": 0, "tokens_out": 0, "cache_hit": 0}
    for comp in sorted(comp_tot):
        d = comp_tot[comp]
        for k in g:
            g[k] += d[k]
        c = cost(d)
        print(f"{comp:<9} {d['calls']:>9} {d['tool_calls']:>10} "
              f"{d['tokens_in']:>11,} {d['tokens_out']:>10,}"
              + (f"  {c:.4f}" if c is not None else ""))
    c = cost(g)
    print(f"{'TOTAL':<9} {sum(d['calls'] for d in comp_tot.values()):>9} "
          f"{sum(d['tool_calls'] for d in comp_tot.values()):>10} "
          f"{g['tokens_in']:>11,} {g['tokens_out']:>10,}"
          + (f"  {c:.4f}" if c is not None else ""))
    print(f"\n({len(per_diff)} trace file(s); judge calls are not traced "
          "and are excluded)")


if __name__ == "__main__":
    main()
