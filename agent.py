"""Minimal code-review agent loop (W0).

Feed a unified diff -> Claude reviews it, reading repo files for context
via a read_file tool when it wants to -> prints a structured JSON review.

Usage:
    python agent.py sample.diff [--repo path/to/repo]
"""
import argparse
import json
import os
import sys
from pathlib import Path

import anthropic

MODEL = "claude-opus-4-8"
MAX_STEPS = 8            # hard cap on loop iterations
REQUEST_TIMEOUT = 120.0  # seconds per API call (SDK also retries 429/5xx twice)

SYSTEM = """You are a code reviewer. You are given a unified diff.
Use the read_file tool when you need context beyond the diff (the full
function, callers, related tests). Then report your findings.

Report every issue you find, including ones you are uncertain about or
consider low-severity. Do not filter for importance at this stage — your
goal is coverage. For each finding include severity so a downstream
filter can rank them. Only omit pure style/naming nits."""

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
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "findings"],
    "additionalProperties": False,
}

TOOLS = [{
    "name": "read_file",
    "description": (
        "Read a file from the repository under review. Call this when the "
        "diff alone is not enough context — e.g. to see the full function, "
        "its callers, or related code."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repo-relative file path"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "strict": True,
}]


def read_file(repo_root: Path, rel_path: str) -> str:
    target = (repo_root / rel_path).resolve()
    if not target.is_relative_to(repo_root.resolve()):
        raise ValueError(f"path escapes repo root: {rel_path}")
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > 50_000:
        text = text[:50_000] + "\n...[truncated]"
    return text


def run_review(client: anthropic.Anthropic, diff_text: str, repo_root: Path) -> dict:
    messages = [{
        "role": "user",
        "content": f"Review this diff:\n\n```diff\n{diff_text}\n```",
    }]
    for step in range(1, MAX_STEPS + 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=SYSTEM,
            tools=TOOLS,
            output_config={"format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
            messages=messages,
        )
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                path = block.input.get("path", "")
                print(f"[step {step}] read_file({path})", file=sys.stderr)
                try:
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": read_file(repo_root, path),
                    })
                except Exception as e:
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Error: {e}",
                        "is_error": True,
                    })
            messages.append({"role": "user", "content": results})
            continue
        if response.stop_reason != "end_turn":
            raise RuntimeError(f"unexpected stop_reason: {response.stop_reason}")
        u = response.usage
        print(f"[done] steps={step} tokens_in={u.input_tokens} tokens_out={u.output_tokens}",
              file=sys.stderr)
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)  # guaranteed valid JSON by output_config.format
    raise RuntimeError(f"agent did not finish within {MAX_STEPS} steps")


def main():
    parser = argparse.ArgumentParser(description="Minimal code-review agent")
    parser.add_argument("diff", help="Path to a unified diff file")
    parser.add_argument("--repo", default=".", help="Repo root for read_file context")
    args = parser.parse_args()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("No credentials: set the ANTHROPIC_API_KEY environment variable first\n"
                 '  PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."')

    diff_text = Path(args.diff).read_text(encoding="utf-8", errors="replace")
    client = anthropic.Anthropic(timeout=REQUEST_TIMEOUT)
    try:
        review = run_review(client, diff_text, Path(args.repo))
    except anthropic.AuthenticationError:
        sys.exit("Invalid or missing API key — set ANTHROPIC_API_KEY")
    except anthropic.RateLimitError:
        sys.exit("Rate limited even after SDK retries — wait and rerun")
    except anthropic.APIStatusError as e:
        sys.exit(f"API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("Network error — check connection/proxy")

    print(json.dumps(review, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
