"""Repeat-run orchestrator (W7): n runs per version, variance vs effect.

The 30-bug rerun raised questions a single run cannot answer (is the V1
recall drop real? how often does the verifier flip?). This script runs
run_eval.py + judge.py N times per version, resumably, then aggregates:

  * per version: recall / precision / F1 / FP / noise as mean [min..max]
  * per bug: hit stability across runs -- the flip list IS the variance
    attribution (a bug hit 1/3 runs is a variance problem, 0/3 a real miss)
  * per run: token totals from the traces

Resumable: a run directory with scores.json is done and skipped; one with
all result JSONs but no scores.json only re-runs the judge. Safe to rerun
after an interruption or API failure.

Usage:
    python repeat_eval.py [--runs 3] [--versions v0,v1,v2]
                          [--out eval/results_repeat] [--only d1_sign,...]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from tracelog import force_utf8, iter_events

force_utf8()

HERE = Path(__file__).parent
DIFFS = HERE / "eval" / "diffs"

VERSIONS = {
    "v0": ["--no-context", "--no-verify"],   # passive tools only
    "v1": ["--no-verify"],                   # + proactive retrieval
    "v2": [],                                # + verifier (full pipeline)
}


def f1(recall, precision):
    if recall is None or precision is None:
        return None   # slice had no bugs / no findings -- F1 undefined
    if not recall or not precision:
        return 0.0
    return round(2 * recall * precision / (recall + precision), 3)


def run_one(ver: str, run_dir: Path, only: str) -> bool:
    """Ensure run_dir contains eval results + scores.json. True on success."""
    expected = sorted(p.stem for p in DIFFS.glob("*.diff"))
    if only:
        expected = [s for s in expected if s in {x.strip() for x in only.split(",")}]

    if (run_dir / "scores.json").is_file():
        print(f"[skip] {run_dir.name}: already judged")
        return True

    have = {p.stem for p in run_dir.glob("*.json")} - {"scores"}
    if not set(expected) <= have:
        cmd = [sys.executable, str(HERE / "run_eval.py"),
               "--results-dir", str(run_dir)] + VERSIONS[ver]
        if only:
            cmd += ["--only", only]
        print(f"[eval] {run_dir.name}: {' '.join(cmd[1:])}", flush=True)
        if subprocess.run(cmd, cwd=HERE).returncode != 0:
            print(f"[eval] {run_dir.name}: run_eval FAILED", file=sys.stderr)
            return False
        have = {p.stem for p in run_dir.glob("*.json")} - {"scores"}
        if not set(expected) <= have:
            print(f"[eval] {run_dir.name}: missing results for "
                  f"{sorted(set(expected) - have)}", file=sys.stderr)
            return False

    print(f"[judge] {run_dir.name}", flush=True)
    if subprocess.run([sys.executable, str(HERE / "judge.py"),
                       "--results-dir", str(run_dir)], cwd=HERE).returncode != 0:
        print(f"[judge] {run_dir.name}: judge FAILED", file=sys.stderr)
        return False
    return True


def trace_tokens(run_dir: Path) -> dict:
    """Sum finder/verifier tokens over all traces in a run directory."""
    tot = {"tokens_in": 0, "tokens_out": 0, "llm_calls": 0}
    for tf in (run_dir / "traces").glob("*.jsonl"):
        for e in iter_events(tf):
            if e.get("kind") == "llm_response":
                tot["tokens_in"] += e.get("tokens_in", 0)
                tot["tokens_out"] += e.get("tokens_out", 0)
                tot["llm_calls"] += 1
    return tot


def aggregate(out: Path, versions: list[str], runs: int) -> dict:
    summary = {}
    for ver in versions:
        rows, bug_hits = [], {}   # bug_hits: id -> [hit_bool per completed run]
        for i in range(1, runs + 1):
            run_dir = out / f"{ver}_run{i}"
            sp = run_dir / "scores.json"
            if not sp.is_file():
                continue
            data = json.loads(sp.read_text(encoding="utf-8"))
            t = data["metrics"]["total"]
            rows.append({
                "run": i,
                "recall": t["recall"], "precision": t["precision"],
                "f1": f1(t["recall"], t["precision"]),
                "fp": t["false_positives"], "noise": t["noise"],
                "out_of_scope": t.get("out_of_scope", 0),
                "hits": t["hits"], "findings": t["findings"],
                **trace_tokens(run_dir),
            })
            for name, entry in data["verdicts"].items():
                for b in entry["verdict"]["bugs"]:
                    bug_hits.setdefault(b["id"], []).append(bool(b["hit"]))

        if not rows:
            summary[ver] = {"completed_runs": 0}
            continue

        def stat(key):
            vals = [r[key] for r in rows if r[key] is not None]
            if not vals:   # e.g. recall on a bug-free trap slice
                return {"mean": None, "min": None, "max": None}
            return {"mean": round(sum(vals) / len(vals), 3),
                    "min": min(vals), "max": max(vals)}

        n = len(rows)
        flappers = {b: f"{sum(h)}/{len(h)}" for b, h in sorted(bug_hits.items())
                    if 0 < sum(h) < len(h)}
        never_hit = sorted(b for b, h in bug_hits.items() if not any(h))
        summary[ver] = {
            "completed_runs": n,
            "recall": stat("recall"), "precision": stat("precision"),
            "f1": stat("f1"), "fp": stat("fp"), "noise": stat("noise"),
            "out_of_scope": stat("out_of_scope"),
            "tokens_in": stat("tokens_in"), "tokens_out": stat("tokens_out"),
            "runs": rows,
            "unstable_bugs": flappers,   # hit in some runs, missed in others
            "never_hit_bugs": never_hit, # real misses, not variance
        }
    return summary


def print_summary(summary: dict) -> None:
    print(f"\n{'ver':<4} {'n':>2} {'recall':>18} {'precision':>18} "
          f"{'F1':>18} {'FP':>7} {'noise':>9} {'oos':>9}")
    for ver, s in summary.items():
        if not s.get("completed_runs"):
            print(f"{ver:<4}  0  (no completed runs)")
            continue
        fmt = lambda st: ("n/a" if st["mean"] is None
                          else f"{st['mean']:.3f} [{st['min']:.3f}-{st['max']:.3f}]")
        fmt_i = lambda st: ("n/a" if st["mean"] is None
                            else f"{st['mean']:.1f} [{st['min']}-{st['max']}]")
        print(f"{ver:<4} {s['completed_runs']:>2} {fmt(s['recall']):>18} "
              f"{fmt(s['precision']):>18} {fmt(s['f1']):>18} "
              f"{fmt_i(s['fp']):>7} {fmt_i(s['noise']):>9} "
              f"{fmt_i(s['out_of_scope']):>9}")
    for ver, s in summary.items():
        if s.get("unstable_bugs"):
            print(f"\n{ver} unstable bugs (variance, hit x/n): {s['unstable_bugs']}")
        if s.get("never_hit_bugs"):
            print(f"{ver} never hit (real misses): {s['never_hit_bugs']}")


def main():
    parser = argparse.ArgumentParser(description="Repeat-run eval orchestrator")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--versions", default="v0,v1,v2")
    parser.add_argument("--out", default=str(HERE / "eval" / "results_repeat"))
    parser.add_argument("--only", default="", help="Comma-separated diff stems")
    parser.add_argument("--aggregate-only", action="store_true",
                        help="Skip running; just re-aggregate what exists")
    args = parser.parse_args()
    versions = [v.strip() for v in args.versions.split(",") if v.strip()]
    for v in versions:
        if v not in VERSIONS:
            sys.exit(f"unknown version {v!r}; choose from {list(VERSIONS)}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if not args.aggregate_only:
        failures = 0
        for ver in versions:
            for i in range(1, args.runs + 1):
                if not run_one(ver, out / f"{ver}_run{i}", args.only):
                    failures += 1
        if failures:
            print(f"\n{failures} run(s) failed -- rerun this command to resume them.",
                  file=sys.stderr)

    summary = aggregate(out, versions, args.runs)
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print_summary(summary)
    print(f"\nsummary -> {out / 'summary.json'}")


if __name__ == "__main__":
    main()
