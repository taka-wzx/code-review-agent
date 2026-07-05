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

from openai import OpenAI
import openai

from context import build_context

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
MAX_STEPS = 8            # hard cap on loop iterations
REQUEST_TIMEOUT = 120.0  # seconds per API call

SYSTEM = """You are a code reviewer. You are given a unified diff.
Use the read_file tool when you need context beyond the diff (the full
function, callers, related tests). When you are done, you MUST report your
findings by calling the submit_review tool exactly once. Do not answer in
plain text.

Report every issue you find, including ones you are uncertain about or
consider low-severity. Do not filter for importance at this stage -- your
goal is coverage. For each finding include severity so a downstream filter
can rank them. Only omit pure style/naming nits."""

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

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file from the repository under review. Call this when "
                "the diff alone is not enough context -- e.g. to see the full "
                "function, its callers, or related code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative file path"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_review",
            "description": "Submit the final code review. Call this exactly once when done.",
            "parameters": REVIEW_SCHEMA,
        },
    },
]


def read_file(repo_root: Path, rel_path: str) -> str:
    target = (repo_root / rel_path).resolve()
    if not target.is_relative_to(repo_root.resolve()):
        raise ValueError(f"path escapes repo root: {rel_path}")
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > 50_000:
        text = text[:50_000] + "\n...[truncated]"
    return text


def run_review(client: OpenAI, diff_text: str, repo_root: Path, model: str,
               use_context: bool = True) -> dict:
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
    for step in range(1, MAX_STEPS + 1):
        response = client.chat.completions.create(
            model=model,
            max_tokens=8000,
            tools=TOOLS,
            tool_choice="auto",
            messages=messages,
        )
        msg = response.choices[0].message
        tool_calls = msg.tool_calls or []

        # The model finished by submitting the review -> parse and return.
        for tc in tool_calls:
            if tc.function.name == "submit_review":
                u = response.usage
                print(f"[done] steps={step} tokens_in={u.prompt_tokens} "
                      f"tokens_out={u.completion_tokens}", file=sys.stderr)
                return json.loads(tc.function.arguments)

        if not tool_calls:
            raise RuntimeError(
                "model stopped without calling submit_review; got:\n"
                f"{msg.content!r}"
            )

        # Otherwise it asked to read files -> execute each and feed results back.
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
            try:
                args = json.loads(tc.function.arguments)
                path = args.get("path", "")
                print(f"[step {step}] read_file({path})", file=sys.stderr)
                content = read_file(repo_root, path)
            except Exception as e:
                content = f"Error: {e}"
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
    args = parser.parse_args()

    diff_text = Path(args.diff).read_text(encoding="utf-8", errors="replace")
    client, model = make_client()
    try:
        review = run_review(client, diff_text, Path(args.repo), model,
                            use_context=not args.no_context)
    except openai.AuthenticationError:
        sys.exit("Invalid or missing API key -- check your .env / environment variable")
    except openai.RateLimitError:
        sys.exit("Rate limited even after retries -- wait and rerun")
    except openai.APIStatusError as e:
        sys.exit(f"API error {e.status_code}: {e.message}")
    except openai.APIConnectionError:
        sys.exit("Network error -- check connection/proxy")

    print(json.dumps(review, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
