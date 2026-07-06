"""Minimal code-review agent loop (W0), provider-agnostic.

Feed a unified diff -> the model reviews it, reading repo files for context
via a read_file tool when it wants to -> prints a structured JSON review.

Works on any OpenAI-compatible API. Pick the provider with LLM_PROVIDER:
    deepseek (default)  needs DEEPSEEK_API_KEY
    glm                 needs GLM_API_KEY (or ZHIPUAI_API_KEY)

Usage:
    python agent.py sample.diff [--repo path/to/repo]
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Windows redirects default to GBK; model output may contain any unicode
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from openai import OpenAI
import openai

from context import build_context
from tools import READ_FILE_TOOL, SEARCH_REPO_TOOL, ToolSession
from tracelog import Trace, tev
from verifier import verify_findings

# --- provider config ---------------------------------------------------------
# Both DeepSeek and Zhipu/GLM expose OpenAI-compatible endpoints, so one client
# works for both -- only the base_url, model id, and key env var change.
PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-pro",
        "key_envs": ("DEEPSEEK_API_KEY",),
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.6",
        "key_envs": ("GLM_API_KEY", "ZHIPUAI_API_KEY"),
    },
}
MAX_STEPS = 10           # hard cap on loop iterations
MAX_SUBMIT_ATTEMPTS = 2  # invalid submit_review payloads before giving up
REQUEST_TIMEOUT = 120.0  # seconds per API call

SYSTEM = """You are a code reviewer. You are given a unified diff.
Use the read_file tool when you need context beyond the diff (the full
function, callers, related tests). When you are done, you MUST report your
findings by calling the submit_review tool exactly once. Do not answer in
plain text.

Report every issue you find, including ones you are uncertain about or
consider low-severity. Do not filter for importance at this stage -- your
goal is coverage. For each finding include severity so a downstream filter
can rank them. Only omit pure style/naming nits.

You have a limited step budget (~10 tool rounds). Verify what actually
bears on the diff, then submit; do not exhaustively explore the repo. Once
a fact is established (e.g. a file/symbol does not exist), record it as a
finding instead of re-checking it another way."""

# JSON schema for the final review, reused as the parameters of submit_review.
# Making "submit the review" a tool (instead of a provider-specific JSON-schema
# response mode) keeps structured output working on any OpenAI-compatible API.
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "issue": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["file", "line", "severity", "issue", "suggestion"],
            },
        },
    },
    "required": ["summary", "findings"],
}

SUBMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_review",
        "description": "Submit the final code review. Call this exactly once when done.",
        "parameters": REVIEW_SCHEMA,
    },
}
TOOLS = [READ_FILE_TOOL, SEARCH_REPO_TOOL, SUBMIT_TOOL]

SEVERITIES = ("high", "medium", "low")


def validate_review(review) -> list[str]:
    """Structural checks on a submit_review payload against REVIEW_SCHEMA.
    Pure function, unit-testable offline (same pattern as the verifier's
    validate_verdicts). Returns a list of problems; empty = valid."""
    if not isinstance(review, dict):
        return ["review is not a JSON object"]
    problems = []
    if not isinstance(review.get("summary"), str) or not review["summary"].strip():
        problems.append("'summary' missing or empty")
    findings = review.get("findings")
    if not isinstance(findings, list):
        return problems + ["'findings' missing or not a list"]
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            problems.append(f"finding {i}: not an object")
            continue
        for key in ("file", "issue", "suggestion"):
            if not isinstance(f.get(key), str) or not f[key].strip():
                problems.append(f"finding {i}: '{key}' missing or empty")
        if not isinstance(f.get("line"), int) or isinstance(f.get("line"), bool):
            problems.append(f"finding {i}: 'line' must be an integer, got {f.get('line')!r}")
        if f.get("severity") not in SEVERITIES:
            problems.append(f"finding {i}: 'severity' must be one of "
                            f"{list(SEVERITIES)}, got {f.get('severity')!r}")
    return problems


def run_review(client: OpenAI, diff_text: str, repo_root: Path, model: str,
               use_context: bool = True, use_verify: bool = True,
               trace: Trace | None = None) -> dict:
    user = f"Review this diff:\n\n```diff\n{diff_text}\n```"
    if use_context:
        pack = build_context(diff_text, Path(repo_root),
                             log=lambda m: print(f"[context] {m}", file=sys.stderr))
        if pack:
            user += ("\n\nRepository context retrieved automatically (conventions, "
                     "changed files in full, callers). You can still use read_file "
                     "for anything not covered:\n\n" + pack)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ]
    session = ToolSession(repo_root, trace=trace, component="finder")
    bad_submits = 0
    for step in range(1, MAX_STEPS + 1):
        # Graceful stop condition: on the last step, withdraw the explore
        # tools and demand the review, instead of crashing at the cap.
        final = step == MAX_STEPS
        if final:
            messages.append({"role": "user", "content":
                "Step budget exhausted. Call submit_review NOW with the "
                "findings you have established so far."})
        response = client.chat.completions.create(
            model=model,
            max_tokens=8000,
            temperature=0.0,
            tools=[SUBMIT_TOOL] if final else TOOLS,
            tool_choice="auto",
            messages=messages,
        )
        msg = response.choices[0].message
        tool_calls = msg.tool_calls or []
        u = response.usage
        tev(trace, "llm_response", component="finder", step=step,
            tool_calls=[tc.function.name for tc in tool_calls],
            tokens_in=u.prompt_tokens, tokens_out=u.completion_tokens)

        if not tool_calls:
            raise RuntimeError(
                "model stopped without calling submit_review; got:\n"
                f"{msg.content!r}"
            )

        # A submit_review call only ends the run if its payload validates;
        # otherwise the problems are fed back as the tool result and the
        # loop continues (same validate-and-retry pattern as the verifier).
        submit = next((tc for tc in tool_calls
                       if tc.function.name == "submit_review"), None)
        problems: list[str] = []
        if submit is not None:
            try:
                review = json.loads(submit.function.arguments)
            except json.JSONDecodeError as e:
                problems = [f"malformed JSON in submit_review arguments: {e}"]
            else:
                problems = validate_review(review)
            if not problems:
                print(f"[done] steps={step} tokens_in={u.prompt_tokens} "
                      f"tokens_out={u.completion_tokens}", file=sys.stderr)
                if use_verify:
                    kept, dropped = verify_findings(client, model, user,
                                                    review.get("findings", []),
                                                    repo_root, trace=trace)
                    print(f"[verifier] kept {len(kept)}/{len(kept) + len(dropped)}",
                          file=sys.stderr)
                    review["findings"] = kept
                    review["dropped_findings"] = dropped
                tev(trace, "review", steps=step, findings=len(review["findings"]),
                    dropped=len(review.get("dropped_findings", [])))
                return review
            bad_submits += 1
            print(f"[step {step}] submit_review rejected: {problems}", file=sys.stderr)
            tev(trace, "submit_rejected", component="finder", problems=problems)
            if bad_submits >= MAX_SUBMIT_ATTEMPTS:
                raise RuntimeError(
                    f"submit_review still invalid after {bad_submits} attempts: {problems}")

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
                content = ("Review rejected -- fix these problems and call "
                           "submit_review again: " + "; ".join(problems))
            else:
                print(f"[step {step}] {tc.function.name} "
                      f"{tc.function.arguments[:120]}", file=sys.stderr)
                content = session.execute(tc.function.name, tc.function.arguments)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": content,
            })

    raise RuntimeError(f"agent did not finish within {MAX_STEPS} steps")


def load_dotenv() -> None:
    """Load KEY=VALUE lines from .env next to this file (real env vars win)."""
    env_file = Path(__file__).parent / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def make_client() -> tuple[OpenAI, str]:
    """Build a provider-agnostic client + model id from LLM_PROVIDER env."""
    load_dotenv()
    provider = os.environ.get("LLM_PROVIDER", "deepseek").lower()
    if provider not in PROVIDERS:
        sys.exit(f"Unknown LLM_PROVIDER={provider!r}; choose one of {list(PROVIDERS)}")
    cfg = PROVIDERS[provider]
    api_key = next((os.environ[e] for e in cfg["key_envs"] if os.environ.get(e)), None)
    if not api_key:
        envs = " or ".join(cfg["key_envs"])
        sys.exit(f"No credentials for provider {provider!r}: set the {envs} environment variable\n"
                 f'  PowerShell:  $env:{cfg["key_envs"][0]} = "..."')
    client = OpenAI(api_key=api_key, base_url=cfg["base_url"],
                    timeout=REQUEST_TIMEOUT, max_retries=2)
    return client, cfg["model"]


def main():
    parser = argparse.ArgumentParser(description="Minimal code-review agent")
    parser.add_argument("diff", help="Path to a unified diff file")
    parser.add_argument("--repo", default=".", help="Repo root for read_file context")
    parser.add_argument("--no-context", action="store_true",
                        help="Skip proactive context retrieval (ablation)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip the verifier second pass (ablation)")
    parser.add_argument("--trace", metavar="PATH",
                        help="Write a JSONL run trace to this path")
    args = parser.parse_args()

    diff_text = Path(args.diff).read_text(encoding="utf-8", errors="replace")
    client, model = make_client()
    trace = Trace(args.trace) if args.trace else None
    try:
        review = run_review(client, diff_text, Path(args.repo), model,
                            use_context=not args.no_context,
                            use_verify=not args.no_verify,
                            trace=trace)
    except openai.AuthenticationError:
        sys.exit("Invalid or missing API key -- check your .env / environment variable")
    except openai.RateLimitError:
        sys.exit("Rate limited even after retries -- wait and rerun")
    except openai.APIStatusError as e:
        sys.exit(f"API error {e.status_code}: {e.message}")
    except openai.APIConnectionError:
        sys.exit("Network error -- check connection/proxy")
    finally:
        if trace:
            trace.close()

    print(json.dumps(review, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
