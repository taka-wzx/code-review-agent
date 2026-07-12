"""Verifier kill-case bench (W13): a frozen set of recorded finder outputs
with known desired verdicts, replayed live against the CURRENT verifier and
checked deterministically -- no LLM judge. This is the fast iteration loop
for verifier changes (~0.3M tokens/round vs ~1.7M for a full V2 run).

Cases are frozen from eval/results_repeat_w12 into eval/bench_verifier.json
(self-contained: candidates embedded, survives even if the results dirs
move). Rebuild with `python bench_verifier.py build` after a finder change
produces new recordings worth pinning; the checks table below is
hand-authored against the recorded candidate texts.

Check kinds (all keyword matching is case-insensitive substring on issue):
    survives       >=1 matching finding is KEPT (any verification tag) --
                   the rescue targets, plus a few validated keeps pinned
                   as non-regression positions
    stays_dropped  the matching candidates exist and NONE is kept -- the
                   legitimate-drop negative controls (LLM-dependent)
    never_rescued  no KEPT finding carrying the sentinel "rescue" tag
                   matches (no keywords = no rescued finding at all) --
                   pins the sentinel itself, tolerant of ordinary
                   keep/split variance

Usage:
    python bench_verifier.py build
    python bench_verifier.py run [--only id1,id2] [--out DIR]
"""
import argparse
import json
import sys
from pathlib import Path

from tracelog import Trace, force_utf8, iter_events

force_utf8()

HERE = Path(__file__).parent
BENCH_PATH = HERE / "eval" / "bench_verifier.json"
SOURCE_ROOT = HERE / "eval" / "results_repeat_w12"

# id, diff stem, source result (relative to SOURCE_ROOT), checks.
# Keywords quote the recorded candidate texts.
CASES = [
    ("d7-r1-deadflag", "d7_display", "v2_run1/d7_display.json", [
        {"kind": "survives", "issue_contains_any": ["PREDICT_DISPLAY_ONLY_FROZEN"]},
        {"kind": "stays_dropped", "issue_contains_any": ["no callers anywhere"]},
        {"kind": "never_rescued", "issue_contains_any": ["supports slicing"]},
    ]),
    ("d7-r2-deadflag-nonregress", "d7_display", "v2_run2/d7_display.json", [
        # kept (uncertain) in W12 -- the sentinel must not regress a keep
        {"kind": "survives", "issue_contains_any": ["PREDICT_DISPLAY_ONLY"]},
        {"kind": "stays_dropped",
         "issue_contains_any": ["draw_observed` has no callers"]},
        {"kind": "never_rescued", "issue_contains_any": ["TypeError"]},
    ]),
    ("d7-r3-deadflag", "d7_display", "v2_run3/d7_display.json", [
        {"kind": "survives", "issue_contains_any": ["PREDICT_DISPLAY_ONLY"]},
        {"kind": "stays_dropped", "issue_contains_any": ["zero callers"]},
        {"kind": "never_rescued",
         "issue_contains_any": ["draw_observed` has no callers"]},
    ]),
    ("d10-r1-cov", "d10_filter", "v2_run1/d10_filter.json", [
        {"kind": "survives",
         "issue_contains_any": ["positive-definiteness", "symmetry"]},
        {"kind": "never_rescued",
         "issue_contains_any": ["np.linalg.inv", "shapes are mismatched",
                                "len(ts) <= 1"]},
    ]),
    ("d10-r2-cov", "d10_filter", "v2_run2/d10_filter.json", [
        {"kind": "survives", "issue_contains_any": ["Joseph", "symmetry"]},
        {"kind": "never_rescued",
         "issue_contains_any": ["np.linalg.inv", "ndarray"]},
    ]),
    ("d10-r3-negctl", "d10_filter", "v2_run3/d10_filter.json", [
        # no cov candidate exists in this recording (finder-side miss):
        # nothing may be rescued here at all
        {"kind": "never_rescued"},
    ]),
    ("d12-r3-trap", "d12_trap_clean", "v2_run3/d12_trap_clean.json", [
        {"kind": "never_rescued"},
        {"kind": "stays_dropped", "issue_contains_any": ["No tests exist"]},
    ]),
    ("d13-r1-trap", "d13_trap_refactor", "v2_run1/d13_trap_refactor.json", [
        {"kind": "never_rescued"},
        {"kind": "stays_dropped", "issue_contains_any": ["no docstring"]},
    ]),
    ("d16-r2-fsum", "d16_missing_dep", "v2_run2/d16_missing_dep.json", [
        # the pattern-(ii) discriminator: accumulation phrasing without a
        # named-invariant claim, legitimately refuted -- never rescue
        {"kind": "never_rescued", "issue_contains_any": ["accumulates"]},
        {"kind": "stays_dropped", "issue_contains_any": ["accumulates"]},
        # validated keeps pinned as non-regression positions
        {"kind": "survives", "issue_contains_any": ["timeutil"]},
        {"kind": "survives", "issue_contains_any": ["len(rallies)"]},
    ]),
    ("d11-r1-negctl", "d11_cor", "v2_run1/d11_cor.json", [
        # div-zero drops here carry legitimate mechanism refutations
        {"kind": "never_rescued", "issue_contains_any": ["division by zero"]},
        {"kind": "survives", "issue_contains_any": ["vz_in_raw"]},
    ]),
]


def build() -> None:
    from replay_verifier import reconstruct_candidates
    cases = []
    for case_id, diff, source, checks in CASES:
        result = json.loads((SOURCE_ROOT / source).read_text(encoding="utf-8"))
        cases.append({"id": case_id, "diff": diff, "source": source,
                      "candidates": reconstruct_candidates(result),
                      "checks": checks})
    BENCH_PATH.write_text(json.dumps(
        {"built_from": f"{SOURCE_ROOT.name} @ see git log", "cases": cases},
        indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(cases)} case(s) -> {BENCH_PATH}")


def _matches(f: dict, keywords) -> bool:
    if not keywords:
        return True
    issue = f.get("issue", "").lower()
    return any(k.lower() in issue for k in keywords)


def check_one(check: dict, candidates: list, kept: list) -> tuple[bool, str]:
    kind = check["kind"]
    kws = check.get("issue_contains_any")
    if kind == "survives":
        ok = any(_matches(f, kws) for f in kept)
        return ok, "kept" if ok else "NOT kept"
    if kind == "stays_dropped":
        in_cands = [f for f in candidates if _matches(f, kws)]
        kept_hits = [f for f in kept if _matches(f, kws)]
        if not in_cands:
            return False, "no matching candidate (stale bench?)"
        ok = not kept_hits
        return ok, "dropped" if ok else f"{len(kept_hits)} kept"
    if kind == "never_rescued":
        rescued = [f for f in kept if "rescue" in f and _matches(f, kws)]
        ok = not rescued
        return ok, "no rescue" if ok else (
            "RESCUED: " + "; ".join(f["issue"][:60] for f in rescued))
    return False, f"unknown check kind {kind!r}"


def run(only: set, out: Path) -> int:
    from agent import build_review_input
    from llm import make_client
    from verifier import verify_findings
    bench = json.loads(BENCH_PATH.read_text(encoding="utf-8"))
    client, model = make_client()
    out.mkdir(parents=True, exist_ok=True)
    n_checks = n_fail = 0
    tin = tout = 0
    for case in bench["cases"]:
        if only and case["id"] not in only:
            continue
        diff_text = (HERE / "eval" / "diffs" / f"{case['diff']}.diff").read_text(
            encoding="utf-8", errors="replace")
        user = build_review_input(diff_text, HERE / "eval" / "repo")
        trace_path = out / f"{case['id']}.jsonl"
        trace = Trace(trace_path)
        try:
            kept, dropped = verify_findings(client, model, user,
                                            case["candidates"],
                                            HERE / "eval" / "repo",
                                            trace=trace)
        finally:
            trace.close()
        ct_in = ct_out = 0
        for e in iter_events(trace_path):
            if e.get("kind") == "llm_response":
                ct_in += e.get("tokens_in", 0)
                ct_out += e.get("tokens_out", 0)
        tin += ct_in; tout += ct_out
        print(f"\n== {case['id']}  (kept {len(kept)}/{len(case['candidates'])}, "
              f"tokens {ct_in:,}/{ct_out:,})")
        for check in case["checks"]:
            ok, detail = check_one(check, case["candidates"], kept)
            n_checks += 1
            n_fail += 0 if ok else 1
            kws = check.get("issue_contains_any") or ["<any>"]
            print(f"  [{'PASS' if ok else 'FAIL'}] {check['kind']:<14} "
                  f"{'|'.join(k[:40] for k in kws):<45} {detail}")
    print(f"\nbench: {n_checks - n_fail}/{n_checks} checks passed, "
          f"tokens {tin:,} in / {tout:,} out")
    return 1 if n_fail else 0


def main():
    parser = argparse.ArgumentParser(description="Verifier kill-case bench")
    parser.add_argument("mode", choices=["build", "run"])
    parser.add_argument("--only", default="", help="Comma-separated case ids")
    parser.add_argument("--out", default=str(HERE / "eval" / "bench_out"),
                        help="Where run traces go (default eval/bench_out)")
    args = parser.parse_args()
    if args.mode == "build":
        build()
        return
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    sys.exit(run(only, Path(args.out)))


if __name__ == "__main__":
    main()
