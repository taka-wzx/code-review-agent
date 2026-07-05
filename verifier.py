"""Second-pass verifier: re-examines every finder finding and drops the
ones that don't hold up (W5).

The eval showed the finder's bottleneck is noise, not recall: 30+ style
nits / speculative suggestions pin precision at ~0.45 while FP=0. So the
verifier's job is narrow and explicit: keep only findings that name a
concrete defect with a plausible failure scenario in THIS code.

Same cross-provider structured-output pattern as agent.py/judge.py:
verdicts are delivered via a submit_verdicts tool call, structurally
validated, with one retry that feeds the validation errors back.
"""
import json
import sys

VERIFIER_SYSTEM = """You are a strict code-review verifier. You receive a
diff, repository context, and a numbered list of candidate findings from a
first-pass reviewer. Decide KEEP or DROP for every finding.

KEEP a finding only if ALL hold:
- it identifies a concrete defect or high-risk behavior in the changed
  code: wrong result, crash, leak, dead code path, or a violated project
  convention that has a stated real consequence;
- you can verify the claim yourself against the code shown -- do not
  trust the finder; re-check the logic and the line it points at;
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

MAX_ATTEMPTS = 2


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


def verify_findings(client, model: str, review_input: str, findings: list) -> tuple[list, list]:
    """Run the verifier pass. review_input is the exact user content the
    finder saw (diff + retrieved context), so both passes share one view.

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
    last_problems = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = client.chat.completions.create(
            model=model, max_tokens=4000, temperature=0.0,
            tools=[VERDICT_TOOL], tool_choice="auto", messages=messages,
        )
        msg = response.choices[0].message
        call = next((tc for tc in (msg.tool_calls or [])
                     if tc.function.name == "submit_verdicts"), None)
        if call is None:
            last_problems = ["verifier answered in text instead of calling submit_verdicts"]
        else:
            try:
                verdicts = json.loads(call.function.arguments).get("verdicts")
            except json.JSONDecodeError as e:
                last_problems = [f"malformed JSON in tool arguments: {e}"]
            else:
                last_problems = validate_verdicts(verdicts, len(findings))
                if not last_problems:
                    return apply_verdicts(findings, verdicts)
        print(f"[verifier attempt {attempt}] invalid: {last_problems}", file=sys.stderr)
        messages.append({"role": "assistant", "content": msg.content or "",
                         **({"tool_calls": [{"id": call.id, "type": "function",
                                             "function": {"name": "submit_verdicts",
                                                          "arguments": call.function.arguments}}]}
                            if call else {})})
        if call:
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": "Verdicts rejected: " + "; ".join(last_problems)})
        else:
            messages.append({"role": "user",
                             "content": "You must call submit_verdicts. Problems: "
                                        + "; ".join(last_problems)})
    print(f"[verifier] FAILED after {MAX_ATTEMPTS} attempts, failing open "
          f"(keeping all {len(findings)} findings)", file=sys.stderr)
    return findings, []
