"""Verifier replay harness (W13): re-run ONLY the verifier over finder
outputs recorded in an existing results dir, so verifier-only changes never
pay for finder runs (~60% of a full run) and A/B comparisons are paired --
both variants see identical candidates, eliminating finder sampling
variance from the comparison.

Candidate reconstruction: result JSONs written from W13 on carry the exact
pre-verifier list as "candidate_findings"; older results (e.g. the W12
acceptance dirs) are reconstructed as kept-then-dropped with the verifier's
output keys stripped. That ordering is a stable partition of the live
interleaved order (merge_verdicts preserves relative order within each
partition), so replayed variants are order-identical to EACH OTHER --
paired verdicts are clean -- but replay absolutes carry an order caveat
against the original recording.

The finder user message is rebuilt with agent.build_review_input (shared
production code, deterministic given the diff + fixture repo).

Modes:
    --sweep     zero-LLM inner loop: apply the sentinel patterns to every
                recorded dropped finding and print would-rescues
    (live)      replay verify_findings per diff per source run; the run
                writes the B view (sentinel active) and derives the A view
                (HEAD behavior) by demoting rescued findings back -- one
                live execution, two variants, pairing by construction

Usage:
    python replay_verifier.py --sweep --source eval/results_repeat_w12
    python replay_verifier.py --source eval/results_repeat_w12 \
        --out eval/results_replay_w13 [--only d7_display,...] [--judge]
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from code_review_agent.tracelog import Trace, force_utf8

force_utf8()

HERE = Path(__file__).parent

# Keys the verifier (and the W13 sentinel) add to findings; stripping them
# from kept/dropped recovers the candidate the verifier was given.
VERIFIER_KEYS = ("verification", "dissent_reason", "drop_reason", "rescue")


def reconstruct_candidates(result: dict) -> list:
    """The verifier's candidate list for one recorded review.

    Prefers the exact "candidate_findings" recording (W13+); otherwise
    rebuilds kept-then-dropped with verifier output keys stripped. Returned
    dicts are copies -- callers may annotate freely.
    """
    recorded = result.get("candidate_findings")
    if isinstance(recorded, list):
        return [dict(f) for f in recorded]
    def strip(f):
        return {k: v for k, v in f.items() if k not in VERIFIER_KEYS}
    return ([strip(f) for f in result.get("findings", [])]
            + [strip(f) for f in result.get("dropped_findings", [])])


def source_run_dirs(source: Path) -> list[Path]:
    """A repeat root (v2_run*) expands to its run dirs; a single run dir
    (contains result JSONs) is itself one unit."""
    runs = sorted(d for d in source.glob("*_run*") if d.is_dir())
    return runs if runs else [source]


def iter_results(run_dir: Path, only: set):
    for p in sorted(run_dir.glob("*.json")):
        if p.stem == "scores" or (only and p.stem not in only):
            continue
        yield p.stem, json.loads(p.read_text(encoding="utf-8"))


def sweep(source: Path, only: set) -> int:
    """Zero-LLM: apply the live sentinel semantics (rescue_forbidden_drops
    against each result's kept list, so the W17 guard refinement is
    reflected) to every recorded dropped finding. Prints would-rescues and
    guard blocks; returns the number of would-rescues (for scripting)."""
    from code_review_agent.sentinels import classify_drop, rescue_forbidden_drops
    n_drops = n_rescue = n_guard = 0
    for run_dir in source_run_dirs(source):
        for name, result in iter_results(run_dir, only):
            drops = result.get("dropped_findings", [])
            n_drops += len(drops)
            rescued, still = rescue_forbidden_drops(
                drops, kept=result.get("findings", []))
            for f in rescued:
                n_rescue += 1
                where = f"{run_dir.name}/{name} {f['file']}:{f['line']}"
                print(f"[rescue] {where}  tag={f['rescue']}")
                print(f"         issue:  {f['issue'][:110]}")
                print(f"         reason: {f.get('dissent_reason', '')[:110]}")
            for f in still:
                if classify_drop(f) == "duplicate-guard":
                    n_guard += 1
                    print(f"[guard ] {run_dir.name}/{name} "
                          f"{f['file']}:{f['line']}  (pattern hit, "
                          "surviving twin, rescue blocked)")
    print(f"\nsweep: {n_rescue} would-rescue, {n_guard} guard-blocked, "
          f"out of {n_drops} recorded drops")
    return n_rescue


_SENTINEL_PREFIX_RE = re.compile(r"^\[sentinel:[^\]]+\]\s*")


def derive_head_view(result: dict) -> dict:
    """The A (HEAD, sentinel-off) view of a replayed review: rescued
    findings are demoted back to dropped_findings with their original
    drop_reason recovered from the machine-strippable dissent prefix.
    The sentinel itself makes no LLM calls, so one live execution yields
    both variants and the pairing is exact by construction."""
    findings, demoted = [], []
    for f in result["findings"]:
        if "rescue" in f:
            d = {k: v for k, v in f.items()
                 if k not in ("verification", "dissent_reason", "rescue")}
            d["drop_reason"] = _SENTINEL_PREFIX_RE.sub(
                "", f.get("dissent_reason", ""))
            demoted.append(d)
        else:
            findings.append(f)
    return {**result, "findings": findings,
            "dropped_findings": result["dropped_findings"] + demoted}


def replay_run(client, model, run_dir: Path, out: Path, tag: str,
               diffs_dir: Path, repo: Path, only: set,
               tiebreak: bool = True) -> None:
    """Replay one recorded run dir: B view (sentinel active) written from
    the live execution, A view derived. Resumable per diff."""
    from code_review_agent.agent import build_review_input
    from code_review_agent.verifier import verify_findings
    a_dir, b_dir = out / f"A_{tag}", out / f"B_{tag}"
    a_dir.mkdir(parents=True, exist_ok=True)
    b_dir.mkdir(parents=True, exist_ok=True)
    for name, source in iter_results(run_dir, only):
        if (a_dir / f"{name}.json").is_file() and (b_dir / f"{name}.json").is_file():
            print(f"[skip] {tag}/{name}: already replayed")
            continue
        diff_path = diffs_dir / f"{name}.diff"
        if not diff_path.is_file():
            print(f"[warn] {tag}/{name}: no diff at {diff_path}, skipped",
                  file=sys.stderr)
            continue
        candidates = reconstruct_candidates(source)
        print(f"\n[replay] {tag}/{name}: {len(candidates)} candidate(s)")
        diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
        user = build_review_input(diff_text, repo)
        trace = Trace(b_dir / "traces" / f"{name}.jsonl")
        try:
            kept, dropped, _status = verify_findings(client, model, user,
                                                     candidates, repo,
                                                     trace=trace,
                                                     tiebreak=tiebreak)
        finally:
            trace.close()
        b_view = {"summary": source.get("summary", ""),
                  "findings": kept, "dropped_findings": dropped,
                  "out_of_scope_findings":
                      source.get("out_of_scope_findings", []),
                  "candidate_findings": candidates}
        (b_dir / f"{name}.json").write_text(
            json.dumps(b_view, indent=2, ensure_ascii=False), encoding="utf-8")
        (a_dir / f"{name}.json").write_text(
            json.dumps(derive_head_view(b_view), indent=2, ensure_ascii=False),
            encoding="utf-8")


def judge_dir(results_dir: Path) -> bool:
    if (results_dir / "scores.json").is_file():
        print(f"[skip] {results_dir.name}: already judged")
        return True
    print(f"[judge] {results_dir.name}", flush=True)
    return subprocess.run([sys.executable, str(HERE / "judge.py"),
                           "--results-dir", str(results_dir)],
                          cwd=HERE).returncode == 0


def main():
    parser = argparse.ArgumentParser(description="Verifier replay harness")
    parser.add_argument("--source", required=True,
                        help="Results dir or repeat root with recorded reviews")
    parser.add_argument("--out", default=None,
                        help="Output root for replayed runs (live mode)")
    parser.add_argument("--diffs-dir", default=str(HERE / "eval" / "diffs"))
    parser.add_argument("--repo", default=str(HERE / "eval" / "repo"))
    parser.add_argument("--only", default="",
                        help="Comma-separated diff stems")
    parser.add_argument("--judge", action="store_true",
                        help="Run judge.py on each replayed output dir")
    parser.add_argument("--sweep", action="store_true",
                        help="Zero-LLM sentinel sweep over recorded drops")
    parser.add_argument("--no-tiebreak", action="store_true",
                        help="Disable the W17 disagreement tiebreak pass "
                             "(A/B comparison baseline)")
    args = parser.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    source = Path(args.source)
    if not source.is_dir():
        sys.exit(f"no such source dir: {source}")

    if args.sweep:
        sweep(source, only)
        return

    if not args.out:
        sys.exit("--out is required for live replay")
    from code_review_agent.llm import make_client
    from repeat_eval import aggregate, print_summary
    out = Path(args.out)
    client, model = make_client()
    run_dirs = source_run_dirs(source)
    failures = 0
    for i, run_dir in enumerate(run_dirs, 1):
        replay_run(client, model, run_dir, out, f"run{i}",
                   Path(args.diffs_dir), Path(args.repo), only,
                   tiebreak=not args.no_tiebreak)
        if args.judge:
            for tag in ("A", "B"):
                if not judge_dir(out / f"{tag}_run{i}"):
                    failures += 1
    if failures:
        print(f"\n{failures} judge run(s) failed -- rerun to resume.",
              file=sys.stderr)
    if args.judge:
        summary = aggregate(out, ["A", "B"], len(run_dirs))
        (out / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8")
        print_summary(summary)
        print(f"\nsummary -> {out / 'summary.json'}")


if __name__ == "__main__":
    main()
