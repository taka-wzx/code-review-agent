"""LLM-judge auto-scorer for the eval set (W2).

Reads ground truth (eval/truth.json) + agent outputs (eval/results/*.json),
asks a judge model to match findings against planted bugs, validates the
verdict structurally, and prints/saves recall & precision.

The judge's verdict is delivered via a forced-single tool call
(submit_scores) -- same cross-provider structured-output trick as agent.py.

Usage (run after run_eval.py):
    python judge.py [--results-dir eval/results]
"""
import argparse
import json
import sys
from pathlib import Path

from llm import make_client

# Windows redirects default to GBK; judge reasons may contain any unicode
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
TRUTH_PATH = HERE / "eval" / "truth.json"
DEFAULT_RESULTS = HERE / "eval" / "results"

MAX_ATTEMPTS = 2   # per diff: 1 try + 1 retry with the validation error fed back

JUDGE_SYSTEM = """You are scoring a code-review agent's output against a list
of planted (ground-truth) bugs.

You receive: the planted bugs (each with an id, severity, and a description
that includes the hit criteria) and the agent's findings (numbered from 0).

Rules:
- A planted bug is HIT when some finding identifies the substance of that
  bug per its stated hit criteria. Wording need not match; line numbers may
  differ slightly. Judge strictly on substance: a finding that only brushes
  near the bug without naming its actual mechanism is NOT a hit.
- One bug may be hit by several findings (list them all), and one finding
  may hit several bugs (agents sometimes merge two real defects into one
  finding -- give credit for each bug whose substance it states).
- Classify every finding that hits no bug as exactly one of:
  - "false_positive": claims something factually incorrect / that does not
    hold for this code.
  - "noise": technically true but trivial -- style/naming/docstring/type
    -annotation nits, speculative robustness suggestions, or restatements
    of an already-matched finding.
- Cover every bug id exactly once and every unmatched finding index exactly
  once. Call submit_scores exactly once. Do not answer in plain text."""

SCORE_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_scores",
        "description": "Submit the verdict for one diff. Call exactly once.",
        "parameters": {
            "type": "object",
            "properties": {
                "bugs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "hit": {"type": "boolean"},
                            "matched_finding_indices": {
                                "type": "array", "items": {"type": "integer"},
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["id", "hit", "matched_finding_indices", "reason"],
                    },
                },
                "unmatched_findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "finding_index": {"type": "integer"},
                            "classification": {
                                "type": "string",
                                "enum": ["false_positive", "noise"],
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["finding_index", "classification", "reason"],
                    },
                },
            },
            "required": ["bugs", "unmatched_findings"],
        },
    },
}


def validate_verdict(verdict: dict, bugs: list, findings: list) -> list[str]:
    """Structural checks on a judge verdict. Returns a list of problems
    (empty list = valid). Pure function so it is unit-testable offline."""
    problems = []
    expected_ids = [b["id"] for b in bugs]
    got = verdict.get("bugs")
    if not isinstance(got, list):
        return ["'bugs' missing or not a list"]
    got_ids = [b.get("id") for b in got]
    if sorted(got_ids) != sorted(expected_ids):
        problems.append(f"bug ids mismatch: expected {sorted(expected_ids)}, got {sorted(got_ids)}")

    n = len(findings)
    matched = set()
    for b in got:
        idxs = b.get("matched_finding_indices")
        if not isinstance(idxs, list):
            problems.append(f"bug {b.get('id')}: matched_finding_indices not a list")
            continue
        if b.get("hit") and not idxs:
            problems.append(f"bug {b.get('id')}: hit=true but no matched findings")
        if not b.get("hit") and idxs:
            problems.append(f"bug {b.get('id')}: hit=false but has matched findings")
        for i in idxs:
            if not isinstance(i, int) or not (0 <= i < n):
                problems.append(f"bug {b.get('id')}: finding index {i} out of range 0..{n - 1}")
            else:
                matched.add(i)  # a finding may legitimately hit several bugs

    extra = verdict.get("unmatched_findings")
    if not isinstance(extra, list):
        return problems + ["'unmatched_findings' missing or not a list"]
    seen_extra = set()
    for e in extra:
        i = e.get("finding_index")
        if not isinstance(i, int) or not (0 <= i < n):
            problems.append(f"unmatched finding index {i} out of range 0..{n - 1}")
            continue
        if i in matched:
            problems.append(f"finding {i} is both matched to a bug and listed as unmatched")
        if i in seen_extra:
            problems.append(f"finding {i} listed twice in unmatched_findings")
        seen_extra.add(i)
        if e.get("classification") not in ("false_positive", "noise"):
            problems.append(f"finding {i}: bad classification {e.get('classification')!r}")
    uncovered = set(range(n)) - matched - seen_extra
    if uncovered:
        problems.append(f"findings never mentioned: {sorted(uncovered)}")
    return problems


def compute_metrics(verdicts: dict) -> dict:
    """Aggregate per-diff verdicts into hit/fp/noise counts and totals.
    verdicts: {diff_name: {"verdict": ..., "n_findings": int}}"""
    per_diff, tot = {}, {"bugs": 0, "hits": 0, "findings": 0, "matched": 0,
                         "false_positives": 0, "noise": 0}
    for name, entry in sorted(verdicts.items()):
        v, n = entry["verdict"], entry["n_findings"]
        hits = sum(1 for b in v["bugs"] if b["hit"])
        # unique finding indices, so a finding hitting two bugs counts once
        matched = len({i for b in v["bugs"] for i in b["matched_finding_indices"]})
        fp = sum(1 for e in v["unmatched_findings"] if e["classification"] == "false_positive")
        noise = sum(1 for e in v["unmatched_findings"] if e["classification"] == "noise")
        per_diff[name] = {"bugs": len(v["bugs"]), "hits": hits, "findings": n,
                          "matched_findings": matched, "false_positives": fp, "noise": noise}
        tot["bugs"] += len(v["bugs"]); tot["hits"] += hits
        tot["findings"] += n; tot["matched"] += matched
        tot["false_positives"] += fp; tot["noise"] += noise
    tot["recall"] = round(tot["hits"] / tot["bugs"], 3) if tot["bugs"] else None
    tot["precision"] = round(tot["matched"] / tot["findings"], 3) if tot["findings"] else None
    return {"per_diff": per_diff, "total": tot}


def judge_one(client, model: str, name: str, bugs: list, findings: list) -> dict:
    """Ask the judge model for a verdict on one diff; validate; retry once."""
    payload = json.dumps({"planted_bugs": bugs, "agent_findings": findings},
                         ensure_ascii=False, indent=2)
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": f"Diff: {name}\n\n{payload}"},
    ]
    last_problems = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = client.chat.completions.create(
            model=model, max_tokens=4000, temperature=0.0,
            tools=[SCORE_TOOL], tool_choice="auto", messages=messages,
        )
        msg = response.choices[0].message
        call = next((tc for tc in (msg.tool_calls or [])
                     if tc.function.name == "submit_scores"), None)
        if call is None:
            last_problems = [f"judge answered in text instead of calling submit_scores: {msg.content!r:.200}"]
        else:
            try:
                verdict = json.loads(call.function.arguments)
            except json.JSONDecodeError as e:
                last_problems = [f"malformed JSON in tool arguments: {e}"]
            else:
                last_problems = validate_verdict(verdict, bugs, findings)
                if not last_problems:
                    return verdict
        print(f"  [attempt {attempt}] invalid verdict: {last_problems}", file=sys.stderr)
        # feed the errors back and retry once
        messages.append({"role": "assistant", "content": msg.content or "",
                         **({"tool_calls": [{"id": call.id, "type": "function",
                                             "function": {"name": "submit_scores",
                                                          "arguments": call.function.arguments}}]}
                            if call else {})})
        if call:
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": "Verdict rejected: " + "; ".join(last_problems)})
        else:
            messages.append({"role": "user",
                             "content": "You must call submit_scores. Problems: "
                                        + "; ".join(last_problems)})
    raise RuntimeError(f"{name}: judge failed after {MAX_ATTEMPTS} attempts: {last_problems}")


def main():
    parser = argparse.ArgumentParser(description="LLM-judge auto-scorer")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS),
                        help="Directory with run_eval.py outputs to score")
    parser.add_argument("--truth", default=str(TRUTH_PATH),
                        help="Ground-truth JSON (e.g. eval/holdout/truth.json)")
    args = parser.parse_args()
    results_dir = Path(args.results_dir)
    scores_path = results_dir / "scores.json"

    truth = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    client, model = make_client()

    verdicts = {}
    for name, bugs in sorted(truth.items()):
        result_path = results_dir / f"{name}.json"
        if not result_path.is_file():
            print(f"SKIP {name}: no result file (run run_eval.py first)", file=sys.stderr)
            continue
        findings = json.loads(result_path.read_text(encoding="utf-8"))["findings"]
        indexed = [{"index": i, **f} for i, f in enumerate(findings)]
        print(f"judging {name} ({len(bugs)} bugs vs {len(findings)} findings)...",
              file=sys.stderr)
        verdict = judge_one(client, model, name, bugs, indexed)
        verdicts[name] = {"verdict": verdict, "n_findings": len(findings)}

    if not verdicts:
        sys.exit("nothing judged")

    metrics = compute_metrics(verdicts)
    scores_path.write_text(
        json.dumps({"metrics": metrics, "verdicts": verdicts},
                   indent=2, ensure_ascii=False), encoding="utf-8")

    t = metrics["total"]
    print(f"\n{'diff':<12} {'hits':>6} {'findings':>9} {'FP':>4} {'noise':>6}")
    for name, d in metrics["per_diff"].items():
        print(f"{name:<12} {d['hits']:>3}/{d['bugs']:<3} {d['findings']:>7} "
              f"{d['false_positives']:>4} {d['noise']:>6}")
    print(f"\nrecall    = {t['hits']}/{t['bugs']} = {t['recall']}")
    print(f"precision = {t['matched']}/{t['findings']} = {t['precision']}  "
          f"(matched findings / all findings)")
    print(f"false positives = {t['false_positives']}, noise = {t['noise']}")
    print(f"\nfull verdicts -> {scores_path}")


if __name__ == "__main__":
    main()
