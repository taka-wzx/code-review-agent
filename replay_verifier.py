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
import sys
from pathlib import Path

from tracelog import force_utf8

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
    strip = lambda f: {k: v for k, v in f.items() if k not in VERIFIER_KEYS}
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
    """Zero-LLM: classify every recorded dropped finding under the sentinel
    patterns. Prints would-rescues and duplicate-guard blocks; returns the
    number of would-rescues (for scripting)."""
    from verifier import classify_drop
    n_drops = n_rescue = n_guard = 0
    for run_dir in source_run_dirs(source):
        for name, result in iter_results(run_dir, only):
            for f in result.get("dropped_findings", []):
                n_drops += 1
                verdict = classify_drop(f)
                if verdict is None:
                    continue
                where = f"{run_dir.name}/{name} {f['file']}:{f['line']}"
                if verdict == "duplicate-guard":
                    n_guard += 1
                    print(f"[guard ] {where}  (pattern hit, duplicate blocked)")
                else:
                    n_rescue += 1
                    print(f"[rescue] {where}  tag={verdict}")
                    print(f"         issue:  {f['issue'][:110]}")
                    print(f"         reason: {f.get('drop_reason', '')[:110]}")
    print(f"\nsweep: {n_rescue} would-rescue, {n_guard} guard-blocked, "
          f"out of {n_drops} recorded drops")
    return n_rescue


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
    args = parser.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    source = Path(args.source)
    if not source.is_dir():
        sys.exit(f"no such source dir: {source}")

    if args.sweep:
        sweep(source, only)
        return

    sys.exit("live replay mode lands in W13 T4 -- only --sweep is available")


if __name__ == "__main__":
    main()
