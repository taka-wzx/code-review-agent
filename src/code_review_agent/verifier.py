"""Second-pass verifier: re-examines every finder finding and drops the
ones that don't hold up (W5).

The eval showed the finder's bottleneck is noise, not recall: 30+ style
nits / speculative suggestions pin precision at ~0.45 while FP=0. So the
verifier's job is narrow and explicit: keep only findings that name a
concrete defect with a plausible failure scenario in THIS code.

Same cross-provider structured-output pattern as agent.py/judge.py:
verdicts are delivered via a submit_verdicts tool call, structurally
validated, with one retry that feeds the validation errors back.

The verifier has the same read-only tools as the finder (read_file,
search_repo): a Reflection pass without the means to check claims beyond
the given context can only guess on findings whose truth lives in another
file -- that is how d5 got wrongly dropped.

W9: verdicts on boundary bugs proved to be a coin flip run-to-run (W8
record), and rewording the criteria demonstrably cannot fix that. So the
verifier now runs TWO independent passes with the unchanged prompt (pass
B sees the findings in reverse order for deterministic decorrelation) and
merges: agreement applies, disagreement keeps the finding marked
"uncertain" with the dissenting reason attached. The split between passes
is the boundary-case detector -- no prompt self-reports doubt.
"""
import json
import sys

from code_review_agent.agentloop import run_submit_loop
from code_review_agent.sentinels import rescue_forbidden_drops
from code_review_agent.tools import READ_FILE_TOOL, RUN_LINTER_TOOL, SEARCH_REPO_TOOL, ToolSession
from code_review_agent.tracelog import tev

VERIFIER_SYSTEM = """You are a strict code-review verifier. You receive a
diff, repository context, and a numbered list of candidate findings from a
first-pass reviewer. Decide KEEP or DROP for every finding.

You have read_file and search_repo tools. When a finding's correctness
depends on code you have not seen (another file, a caller, an import, a
project convention), CHECK it with the tools before deciding. Never drop
a finding as "unverifiable" without having tried to verify it.

KEEP a finding only if ALL hold:
- it identifies a concrete defect or high-risk behavior in the changed
  code: wrong result, crash, leak, dead code path, or a violated project
  convention that has a stated real consequence;
- you can verify the claim yourself against the code shown or retrieved
  -- do not trust the finder; re-check the logic and the line it points at;
- it is not a duplicate/restatement of a finding you already kept.

DROP a finding if ANY hold:
- factually incorrect for this code;
- a style/naming/docstring/type-annotation nit;
- speculative robustness advice with no evidence the scenario occurs
  (e.g. "add validation in case callers pass None");
- generic best-practice advice not tied to a concrete failure here;
- duplicate of an already-kept finding.

Evidence rules (when one of these applies, it takes precedence over the
DROP list -- check them before dropping):
- A docstring or comment stating that an input condition occurs
  (duplicates, gaps, missing frames, empty input) is evidence FOR a
  finding that the code fails to handle that condition: the docs prove
  the scenario is real. Documenting a condition is not handling it, and
  does not make the unhandled behavior an intentional design choice.
  (The absence of such documentation does not by itself make a finding
  speculative either.)
- A numeric-correctness finding is not "generic best-practice advice"
  when it identifies a specific quantity in this code that becomes wrong
  and the mechanism (an invariant lost across repeated updates, a
  missing term in an estimate, a degenerate value reaching a division).
  For slowly accumulating defects no one can demonstrate the drift
  inside one diff; that is not grounds to drop. Keep unless you can
  refute the mechanism itself (show the invariant is preserved, the
  update is not repeated, or the math is wrong). "Technique X is more
  robust than Y" with no such identified error is still a DROP.
- Judge dead-path reachability against the code that exists in this
  repository: the actual flag and config definitions and their comments.
  If those show the enabling condition never holds (e.g. the only
  documented wiring that could supply it is disabled or absent), the
  finding stands; hypothesizing an unseen future caller that might
  supply a different value is not a refutation. The reverse also holds:
  a newly added function that merely has no callers yet is not thereby
  dead code or a defect -- new code gets wired up later; only
  unreachability that follows from existing definitions counts.

Severity is not the criterion: a low-severity real defect is a KEEP; a
high-severity-sounding speculation is a DROP.

Call submit_verdicts exactly once, covering every finding index exactly
once. Do not answer in plain text."""

VERDICT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_verdicts",
        "description": "Submit keep/drop verdicts for all findings. Call exactly once.",
        "parameters": {
            "type": "object",
            "properties": {
                "verdicts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "finding_index": {"type": "integer"},
                            "verdict": {"type": "string", "enum": ["keep", "drop"]},
                            "reason": {"type": "string"},
                        },
                        "required": ["finding_index", "verdict", "reason"],
                    },
                },
            },
            "required": ["verdicts"],
        },
    },
}

MAX_STEPS = 6            # tool-use rounds before failing open
MAX_SUBMIT_ATTEMPTS = 2  # invalid submit_verdicts payloads before failing open


def validate_verdicts(verdicts: list, n_findings: int) -> list[str]:
    """Structural checks: every finding index covered exactly once.
    Pure function, unit-testable offline."""
    if not isinstance(verdicts, list):
        return ["'verdicts' missing or not a list"]
    problems = []
    seen = set()
    for v in verdicts:
        i = v.get("finding_index")
        if not isinstance(i, int) or not (0 <= i < n_findings):
            problems.append(f"finding index {i!r} out of range 0..{n_findings - 1}")
            continue
        if i in seen:
            problems.append(f"finding {i} judged twice")
        seen.add(i)
        if v.get("verdict") not in ("keep", "drop"):
            problems.append(f"finding {i}: bad verdict {v.get('verdict')!r}")
    missing = set(range(n_findings)) - seen
    if missing:
        problems.append(f"findings never judged: {sorted(missing)}")
    return problems


def apply_verdicts(findings: list, verdicts: list) -> tuple[list, list]:
    """Split findings into (kept, dropped); dropped carry the reason."""
    drop_reasons = {v["finding_index"]: v["reason"]
                    for v in verdicts if v["verdict"] == "drop"}
    kept, dropped = [], []
    for i, f in enumerate(findings):
        if i in drop_reasons:
            dropped.append({**f, "drop_reason": drop_reasons[i]})
        else:
            kept.append(f)
    return kept, dropped


def merge_verdicts(findings: list, verdicts_a: list, verdicts_b: list) -> tuple[list, list]:
    """Merge two independent passes (W9). Agreement applies; disagreement
    keeps the finding marked uncertain -- the split between two passes IS
    the boundary-case detector, so no prompt has to self-report doubt.
    Pure function, unit-testable offline.

    Returns (kept, dropped): kept items carry verification "confirmed" or
    "uncertain" (+ dissent_reason); dropped need 2/2 drop votes.
    """
    ma = {v["finding_index"]: v for v in verdicts_a}
    mb = {v["finding_index"]: v for v in verdicts_b}
    kept, dropped = [], []
    for i, f in enumerate(findings):
        va, vb = ma[i], mb[i]
        if va["verdict"] == "keep" and vb["verdict"] == "keep":
            kept.append({**f, "verification": "confirmed"})
        elif va["verdict"] == "drop" and vb["verdict"] == "drop":
            dropped.append({**f, "drop_reason": "2/2: " + va["reason"]})
        else:
            dissent = va if va["verdict"] == "drop" else vb
            kept.append({**f, "verification": "uncertain",
                         "dissent_reason": dissent["reason"]})
    return kept, dropped


def _tiebreak_pass(client, model, review_input: str, kept: list,
                   dropped: list, repo_root, trace=None) -> tuple[list, list]:
    """Third vote on A/B disagreements only (W17). The uncertain channel
    measured 43% matched / 49% noise / 8% FP across W10-W14 -- half real
    value, half precision leak -- so instead of capping the channel, a
    pass C re-judges JUST the disputed findings and the 2/3 majority
    lands: keep -> confirmed (tiebreak-tagged for audit), drop -> dropped
    with a "2/3:" reason (the sentinel still applies to those drops
    afterwards). A failed pass C fails open to the W9 uncertain
    semantics. Cost is proportional to the disagreement count (~1-3
    findings/diff), not the full candidate list."""
    disputed = [f for f in kept if f.get("verification") == "uncertain"]
    if not disputed:
        return kept, dropped
    sub = [{k: v for k, v in f.items()
            if k not in ("verification", "dissent_reason")} for f in disputed]
    vc = _verify_pass(client, model, review_input, sub, repo_root,
                      trace=trace, pass_id="C", reverse_order=False)
    if vc is None:
        print("[verifier] tiebreak pass failed -- disagreements stay "
              "uncertain", file=sys.stderr)
        tev(trace, "tiebreak_failed", n_disputed=len(disputed))
        return kept, dropped
    votes = {v["finding_index"]: v for v in vc}
    new_kept, resolved = [], []
    di = 0
    for f in kept:
        if f.get("verification") != "uncertain":
            new_kept.append(f)
            continue
        v = votes[di]
        di += 1
        if v["verdict"] == "keep":
            r = {k: x for k, x in f.items() if k != "dissent_reason"}
            r["verification"] = "confirmed"
            r["tiebreak"] = "2/3"
            new_kept.append(r)
        else:
            d = {k: x for k, x in f.items()
                 if k not in ("verification", "dissent_reason")}
            d["drop_reason"] = "2/3: " + v["reason"]
            resolved.append(d)
    print(f"[verifier] tiebreak: {len(disputed)} disputed -> "
          f"{len(disputed) - len(resolved)} confirmed, "
          f"{len(resolved)} dropped", file=sys.stderr)
    tev(trace, "tiebreak", n_disputed=len(disputed),
        confirmed=len(disputed) - len(resolved), dropped=len(resolved))
    return new_kept, dropped + resolved


# The rule-firing sentinel (W13-W15) lives in sentinels.py: pattern-gated
# rescue of drops whose drop_reason uses reasoning the Evidence rules above
# forbid. See that module's docstring for the design rationale, validation
# method (sweeps/bench/replay), and known generalization limits.
def _apply_sentinel(kept: list, dropped: list, trace) -> tuple[list, list]:
    rescued, dropped = rescue_forbidden_drops(dropped, kept)
    if rescued:
        print(f"[verifier] sentinel rescued {len(rescued)} dropped "
              "finding(s) -> uncertain", file=sys.stderr)
        tev(trace, "sentinel_rescue", n=len(rescued),
            items=[{"file": f["file"], "line": f["line"], "tag": f["rescue"]}
                   for f in rescued])
    return kept + rescued, dropped


def _verify_pass(client, model: str, review_input: str, findings: list,
                 repo_root, trace=None, pass_id: str = "A",
                 reverse_order: bool = False):
    """One independent verifier conversation (unchanged baseline prompt).

    reverse_order presents the numbered findings back-to-front -- explicit
    index fields keep the mapping intact -- giving the two passes
    deterministic decorrelation without touching temperature.

    Returns a validated verdicts list, or None when this pass failed.
    """
    numbered = [{"index": i, **f} for i, f in enumerate(findings)]
    if reverse_order:
        numbered = list(reversed(numbered))
    messages = [
        {"role": "system", "content": VERIFIER_SYSTEM},
        {"role": "user", "content":
            review_input
            + "\n\nCandidate findings to verify:\n\n"
            + json.dumps(numbered, ensure_ascii=False, indent=2)},
    ]

    def parse_verdicts(raw: str):
        try:
            verdicts = json.loads(raw).get("verdicts")
        except json.JSONDecodeError as e:
            return None, [f"malformed JSON in tool arguments: {e}"]
        return verdicts, validate_verdicts(verdicts, len(findings))

    component = f"verifier{pass_id}"
    result = run_submit_loop(
        client, model, messages,
        explore_tools=[READ_FILE_TOOL, SEARCH_REPO_TOOL, RUN_LINTER_TOOL],
        submit_tool=VERDICT_TOOL, parse=parse_verdicts,
        session=ToolSession(repo_root, trace=trace, component=component),
        max_steps=MAX_STEPS, max_submit_attempts=MAX_SUBMIT_ATTEMPTS,
        # 8000, not 4000: a W16 real-PR run with 11 candidates x long
        # reasons truncated the submit_verdicts JSON mid-string and the
        # pass failed. Sized to the finder's budget.
        max_tokens=8000,
        budget_msg="Step budget exhausted. Call submit_verdicts NOW, covering "
                   "every finding index exactly once, based on what you have "
                   "verified so far.",
        reject_msg=lambda problems: "Verdicts rejected: " + "; ".join(problems),
        trace=trace, component=component, label=component,
        on_text_answer="count",
        text_answer_problem="verifier answered in text instead of calling "
                            "submit_verdicts",
        text_answer_nudge="You must call submit_verdicts covering "
                          "every finding index exactly once.",
    )
    if result.reason == "ok":
        verdicts = result.payload
        tev(trace, "verifier_pass", pass_id=pass_id, steps=result.steps,
            drops=sum(1 for v in verdicts if v["verdict"] == "drop"))
        return verdicts

    print(f"[{component}] pass FAILED ({result.reason} at step "
          f"{result.steps}); last problems: {result.problems}",
          file=sys.stderr)
    tev(trace, "verifier_pass_failed", pass_id=pass_id,
        problems=result.problems)
    return None


def verify_findings(client, model: str, review_input: str, findings: list,
                    repo_root, trace=None,
                    tiebreak: bool = True) -> tuple[list, list, str]:
    """Double-verification orchestrator (W9). review_input is the exact
    user content the finder saw, so all passes share one view; the tools
    let each pass check anything beyond that view itself.

    Two independent passes (B sees the findings in reverse order); their
    agreement applies, their disagreement keeps the finding as
    "uncertain". One failed pass degrades to single-pass verdicts; both
    failed fails open (a broken verifier must not silently eat findings).

    Returns (kept, dropped, status) with status "ok" | "degraded" |
    "failed_open" -- fail-open output is unfiltered, and the caller must
    be able to say so instead of presenting it as a verified review.
    """
    if not findings:
        return [], [], "ok"
    va = _verify_pass(client, model, review_input, findings, repo_root,
                      trace=trace, pass_id="A", reverse_order=False)
    vb = _verify_pass(client, model, review_input, findings, repo_root,
                      trace=trace, pass_id="B", reverse_order=True)

    if va is None and vb is None:
        print(f"[verifier] both passes FAILED, failing open -- keeping all "
              f"{len(findings)} findings", file=sys.stderr)
        tev(trace, "verifier_fail_open", n_findings=len(findings))
        return findings, [], "failed_open"
    if va is None or vb is None:
        failed = "A" if va is None else "B"
        print(f"[verifier] pass {failed} failed -- degrading to single-pass "
              "verdicts (no uncertainty detection this run)", file=sys.stderr)
        kept, dropped = apply_verdicts(findings, va or vb)
        kept, dropped = _apply_sentinel(kept, dropped, trace)
        tev(trace, "verdicts", kept=len(kept), dropped=len(dropped),
            degraded=True, failed_pass=failed)
        return kept, dropped, "degraded"

    kept, dropped = merge_verdicts(findings, va, vb)
    if tiebreak:
        kept, dropped = _tiebreak_pass(client, model, review_input, kept,
                                       dropped, repo_root, trace=trace)
    kept, dropped = _apply_sentinel(kept, dropped, trace)
    n_unc = sum(1 for f in kept if f.get("verification") == "uncertain")
    print(f"[verifier] merged: {len(kept)} kept "
          f"({len(kept) - n_unc} confirmed, {n_unc} uncertain), "
          f"{len(dropped)} dropped", file=sys.stderr)
    tev(trace, "verdicts", kept=len(kept), dropped=len(dropped),
        confirmed=len(kept) - n_unc, uncertain=n_unc)
    return kept, dropped, "ok"
