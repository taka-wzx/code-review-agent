"""Pure finding-set operations for the dual-run finder (W12).

The finder now runs twice (a temperature-0 anchor run and a temperature>0
sampling run) and the union of both runs feeds the verifier. Findings have
no stable ID, so duplicates are detected structurally: same file plus
token-set Jaccard similarity of the issue text, with a line-distance
band. Thresholds were measured against cross-run pairs in
eval/results_repeat_w10: same-bug pairs score jac .26-.67 at line
distance 0-7, while distinct-bug pairs on the same line stay <= .22.
sim_near=0.25 is deliberately placed between those bands (above the .22
distinct ceiling, below the .26 same-bug floor), so the two-tier rule
accepts all measured same-bug pairs except one .20 outlier (left to the
verifier's duplicate->DROP rule) and rejects the .22 distinct pair.
Single-repo measurement -- revalidate the bands before trusting them on
a different codebase or model.

Merging is one-directional: anchor findings pass through verbatim and are
never removed or reworded; only the extra run's duplicate copies are
dropped. A false merge can therefore only suppress a run-2 copy -- worst
case equals single-run behavior -- never eat an anchor finding.

Scope is decided in code at file level (not by the verifier prompt, and
not by line: the same bug's line wobbles +-1..7 across runs, and
legitimate findings cite unchanged context lines inside hunks). Findings
outside the diff's changed files are demoted to out_of_scope_findings and
skip verification entirely -- their keep/drop verdicts were coin flips
across three generations, which is the wobble being removed.

Pure functions, no LLM imports; unit-tested in tests/test_pure.py.
"""
import re

_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*|\d+")


def _issue_tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def similarity(a: str, b: str) -> float:
    """Token-set Jaccard similarity of two issue texts."""
    ta, tb = _issue_tokens(a), _issue_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_duplicate(f1: dict, f2: dict, *, line_tol: int = 4,
                 sim_near: float = 0.25, sim_far: float = 0.40) -> bool:
    """Same file, and either strong text similarity at any line distance
    or moderate similarity within line_tol lines. Compares issue text
    only; suggestions add noise without adding identity."""
    if f1.get("file") != f2.get("file"):
        return False
    sim = similarity(f1.get("issue", ""), f2.get("issue", ""))
    if sim >= sim_far:
        return True
    return (sim >= sim_near
            and abs(f1.get("line", 0) - f2.get("line", 0)) <= line_tol)


def dedup_union(anchor: list, extra: list, **thresholds) -> tuple[list, int]:
    """Union of two finder runs: anchor verbatim (wording and order
    untouched), extra findings appended unless they duplicate anything
    already in the union (so extra's own near-duplicates collapse too).
    Appended findings carry an "origin": "finder2" provenance key.

    Returns (union, n_merged) where n_merged counts suppressed copies.
    """
    union = list(anchor)
    n_merged = 0
    for f in extra:
        if any(is_duplicate(f, u, **thresholds) for u in union):
            n_merged += 1
        else:
            union.append({**f, "origin": "finder2"})
    return union, n_merged


def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def split_by_scope(findings: list, changed_files: list) -> tuple[list, list]:
    """Partition findings into (in_scope, out_of_scope) by whether their
    file is one of the diff's changed files (lightly normalized exact
    match). An empty changed_files list fails open -- everything stays
    in scope rather than silently demoting the whole review."""
    if not changed_files:
        return list(findings), []
    changed = {_norm(f) for f in changed_files}
    in_scope: list = []
    out_of_scope: list = []
    for f in findings:
        dest = in_scope if _norm(f.get("file", "")) in changed else out_of_scope
        dest.append(f)
    return in_scope, out_of_scope
