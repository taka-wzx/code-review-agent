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
"""
import json
import sys

from tools import READ_FILE_TOOL, SEARCH_REPO_TOOL, ToolSession
from tracelog import tev

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


def verify_findings(client, model: str, review_input: str, findings: list,
                    repo_root, trace=None) -> tuple[list, list]:
    """Run the verifier pass. review_input is the exact user content the
    finder saw (diff + retrieved context), so both passes share one view;
    the tools let the verifier check anything beyond that view itself.

    Returns (kept, dropped). On persistent verifier failure, fails open:
    keeps everything (a broken verifier must not silently eat findings).
    """
    if not findings:
        return [], []
    numbered = [{"index": i, **f} for i, f in enumerate(findings)]
    messages = [
        {"role": "system", "content": VERIFIER_SYSTEM},
        {"role": "user", "content":
            review_input
            + "\n\nCandidate findings to verify:\n\n"
            + json.dumps(numbered, ensure_ascii=False, indent=2)},
    ]
    session = ToolSession(repo_root, trace=trace, component="verifier")
    bad_submits = 0
    last_problems = []
    for step in range(1, MAX_STEPS + 1):
        # Graceful stop condition: on the last step, withdraw the explore
        # tools and demand verdicts, instead of silently failing open.
        final = step == MAX_STEPS
        if final:
            messages.append({"role": "user", "content":
                "Step budget exhausted. Call submit_verdicts NOW, covering "
                "every finding index exactly once, based on what you have "
                "verified so far."})
        response = client.chat.completions.create(
            model=model, max_tokens=4000, temperature=0.0,
            tools=[VERDICT_TOOL] if final else [READ_FILE_TOOL, SEARCH_REPO_TOOL, VERDICT_TOOL],
            tool_choice="auto", messages=messages,
        )
        msg = response.choices[0].message
        tool_calls = msg.tool_calls or []
        tev(trace, "llm_response", component="verifier", step=step,
            tool_calls=[tc.function.name for tc in tool_calls])
        submit = next((tc for tc in tool_calls
                       if tc.function.name == "submit_verdicts"), None)

        problems = []
        if submit is not None:
            try:
                verdicts = json.loads(submit.function.arguments).get("verdicts")
            except json.JSONDecodeError as e:
                problems = [f"malformed JSON in tool arguments: {e}"]
            else:
                problems = validate_verdicts(verdicts, len(findings))
                if not problems:
                    kept, dropped = apply_verdicts(findings, verdicts)
                    tev(trace, "verdicts", steps=step,
                        kept=len(kept), dropped=len(dropped))
                    return kept, dropped
            bad_submits += 1
            last_problems = problems
            print(f"[verifier step {step}] invalid verdicts: {problems}", file=sys.stderr)
            tev(trace, "submit_rejected", component="verifier", problems=problems)
            if bad_submits >= MAX_SUBMIT_ATTEMPTS:
                break

        if not tool_calls:
            # Answered in text: counts as a failed attempt, then nudge.
            bad_submits += 1
            last_problems = ["verifier answered in text instead of calling submit_verdicts"]
            print(f"[verifier step {step}] {last_problems[0]}", file=sys.stderr)
            if bad_submits >= MAX_SUBMIT_ATTEMPTS:
                break
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user",
                             "content": "You must call submit_verdicts covering "
                                        "every finding index exactly once."})
            continue

        # Execute this round's tool calls; a rejected submit gets its
        # problem list back as the tool result.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            } for tc in tool_calls],
        })
        for tc in tool_calls:
            if tc is submit:
                content = "Verdicts rejected: " + "; ".join(problems)
            else:
                print(f"[verifier step {step}] {tc.function.name} "
                      f"{tc.function.arguments[:120]}", file=sys.stderr)
                content = session.execute(tc.function.name, tc.function.arguments)
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": content})

    print(f"[verifier] FAILED (steps={MAX_STEPS} cap or {bad_submits} bad submits), "
          f"failing open -- keeping all {len(findings)} findings; "
          f"last problems: {last_problems}", file=sys.stderr)
    tev(trace, "verifier_fail_open", n_findings=len(findings), problems=last_problems)
    return findings, []
