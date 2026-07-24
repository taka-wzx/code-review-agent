"""Minimal code-review agent loop (W0), provider-agnostic.

Feed a unified diff -> the model reviews it, reading repo files for context
via a read_file tool when it wants to -> prints a structured JSON review.

Works on any OpenAI-compatible API (provider selection lives in llm.py).

Usage (after `pip install -e .`):
    crag sample.diff [--repo path/to/repo]
    python -m code_review_agent sample.diff
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from openai import OpenAI
import openai

from code_review_agent.agentloop import run_submit_loop
from code_review_agent.context import CONTEXT_TOKEN_CAP, build_context_for_mode, parse_diff
from code_review_agent.context_memory import (
    ContextMode,
    OrganizationPolicyStore,
    RepositoryMemoryStore,
    RunContext,
    repository_source_sha,
)
from code_review_agent.findings import dedup_union, split_by_scope
from code_review_agent.llm import make_client
from code_review_agent.orchestration import (
    DEFAULT_REVIEW_TIMEOUT_SECONDS,
    Deadline,
    run_parallel_pair,
)
from code_review_agent.tools import READ_FILE_TOOL, RUN_LINTER_TOOL, SEARCH_REPO_TOOL, ToolSession
from code_review_agent.tracelog import Trace, force_utf8, tev, tspan
from code_review_agent.verifier import _verify_findings

force_utf8()

MAX_STEPS = 10           # hard cap on loop iterations
MAX_SUBMIT_ATTEMPTS = 2  # invalid submit_review payloads before giving up
FINDER2_TEMPERATURE = 0.7  # second finder run samples; run 1 stays at 0.0
REVIEW_TIMEOUT_SECONDS = DEFAULT_REVIEW_TIMEOUT_SECONDS
_run_pair = run_parallel_pair

SYSTEM = """You are a code reviewer. You are given a unified diff.
Use the read_file tool when you need context beyond the diff (the full
function, callers, related tests). When you are done, you MUST report your
findings by calling the submit_review tool exactly once. Do not answer in
plain text.

Report every issue you find, including ones you are uncertain about or
consider low-severity. Do not filter for importance at this stage -- your
goal is coverage. For each finding include severity so a downstream filter
can rank them. Only omit pure style/naming nits.

Actively check these commonly missed defect classes. Treat them as
hypotheses to verify against the actual code; report one only when you
can name the concrete code path and failure mechanism:
- Dead paths: when a branch is guarded by flags or config values,
  substitute their actual values (often already in the provided
  context) into the condition and check the guarded code can ever
  run; a value combination that makes it unreachable is a reportable
  bug, and stronger evidence than a function merely having no callers.
- Documented-but-unhandled inputs: when a docstring or comment states an
  input condition (duplicates, gaps, missing/empty entries), check the
  code actually handles it -- documenting a condition is not handling it.
  This includes conditions documented at the call site: substitute the
  caller-documented operating regime (typical lengths, ranges, rates)
  into the function's guards and early returns -- a guard that swallows
  the documented common case is a functional dead zone, not correct
  None-handling.
- Numeric/statistical defects: division by a difference that can be
  zero; repeated updates accumulating floating-point error in long-lived
  state (e.g. a symmetry or conservation invariant silently lost); an
  estimator or fit missing a term or constrained without justification.

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
EXPLORE_TOOLS = [READ_FILE_TOOL, SEARCH_REPO_TOOL, RUN_LINTER_TOOL]

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


def _finder_pass(client, model: str, user: str, repo_root: Path, *,
                 trace, component: str, temperature: float, deadline=None,
                 run_context: RunContext | None = None):
    """One finder conversation: fresh messages and a fresh ToolSession per
    pass (run_submit_loop mutates messages in place, and the repeat-call
    cache / miss-streak state are per-conversation by design)."""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ]

    def parse_submit(raw: str):
        try:
            review = json.loads(raw)
        except json.JSONDecodeError as e:
            return None, [f"malformed JSON in submit_review arguments: {e}"]
        return review, validate_review(review)

    return run_submit_loop(
        client, model, messages,
        explore_tools=EXPLORE_TOOLS, submit_tool=SUBMIT_TOOL,
        parse=parse_submit,
        session=ToolSession(repo_root, trace=trace, component=component),
        max_steps=MAX_STEPS, max_submit_attempts=MAX_SUBMIT_ATTEMPTS,
        max_tokens=8000, temperature=temperature,
        budget_msg="Step budget exhausted. Call submit_review NOW with the "
                   "findings you have established so far.",
        reject_msg=lambda problems: (
            "Review rejected -- fix these problems and call "
            "submit_review again: " + "; ".join(problems)),
        trace=trace, component=component,
        label="" if component == "finder" else component,
        on_text_answer="raise",
        deadline=deadline,
        run_context=run_context,
    )


def build_review_input(diff_text: str, repo_root: Path,
                       use_context: bool = True,
                       log=lambda msg: None,
                       *,
                       context_mode: ContextMode | str | None = None,
                       run_context: RunContext | None = None,
                       memory_store: RepositoryMemoryStore | None = None,
                       policy_store: OrganizationPolicyStore | None = None,
                       organization_id: str = "",
                       repository_id: str = "",
                       base_sha: str | None = None,
                       context_token_cap: int = CONTEXT_TOKEN_CAP) -> str:
    """The exact user message the finder receives (and the verifier, which
    shares the finder's view). Shared with replay_verifier.py so replays
    reconstruct inputs with production code instead of a copy."""
    user = f"Review this diff:\n\n```diff\n{diff_text}\n```"
    mode = context_mode or (
        ContextMode.CURRENT_STATIC if use_context else ContextMode.OFF
    )
    if ContextMode.parse(mode) is not ContextMode.OFF:
        pack = build_context_for_mode(
            diff_text,
            Path(repo_root),
            mode=mode,
            run_context=run_context,
            memory_store=memory_store,
            policy_store=policy_store,
            organization_id=organization_id,
            repository_id=repository_id,
            base_sha=base_sha,
            token_cap=context_token_cap,
            log=log,
        )
        if pack:
            if ContextMode.parse(mode) is ContextMode.CURRENT_STATIC:
                user += ("\n\nRepository context retrieved automatically (conventions, "
                         "changed files in full, callers). You can still use read_file "
                         "for anything not covered:\n\n" + pack)
            else:
                user += ("\n\nRepository context retrieved automatically (trusted memory, "
                         "policy, conventions, changed files, imports, and callers as enabled). "
                         "You can still use read_file for anything not covered:\n\n" + pack)
    return user


def run_review(client: OpenAI, diff_text: str, repo_root: Path, model: str,
               use_context: bool = True, use_verify: bool = True,
               trace: Trace | None = None, *,
               context_mode: ContextMode | str | None = None,
               run_context: RunContext | None = None,
               memory_store: RepositoryMemoryStore | None = None,
               policy_store: OrganizationPolicyStore | None = None,
               organization_id: str = "",
               repository_id: str = "",
               source_revision: str | None = None,
               context_token_cap: int = CONTEXT_TOKEN_CAP,
               run_token_budget: int = 200_000) -> dict:
    deadline = Deadline.after(REVIEW_TIMEOUT_SECONDS)
    mode = ContextMode.parse(context_mode or (
        ContextMode.CURRENT_STATIC if use_context else ContextMode.OFF
    ))
    revision = source_revision or repository_source_sha(Path(repo_root))
    if revision is None:
        revision = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    active_context = run_context or RunContext.create(
        diff_text, revision, run_token_budget
    )
    active_context.set_plan(("build_context", "finder", "verifier", "finalize"))
    with tspan(
        trace,
        "crag.stage context",
        operation="agent.stage",
        attributes={
            "crag.stage.name": "context",
            "crag.context.enabled": mode is not ContextMode.OFF,
            "crag.context.mode": mode.value,
            "crag.input.diff_bytes": len(diff_text.encode("utf-8")),
        },
    ):
        user = build_review_input(
            diff_text,
            repo_root,
            use_context,
            log=lambda m: print(f"[context] {m}", file=sys.stderr),
            context_mode=mode,
            run_context=active_context,
            memory_store=memory_store,
            policy_store=policy_store,
            organization_id=organization_id,
            repository_id=repository_id,
            base_sha=revision,
            context_token_cap=context_token_cap,
        )

    # The anchor and sampling conversations are independent and share only
    # their immutable input/client.  Join both before applying the historical
    # fatal-anchor / fail-open-sampler policy below.
    anchor_outcome, sample_outcome = _run_pair(
        lambda: _finder_pass(client, model, user, repo_root, trace=trace,
                             component="finder", temperature=0.0,
                             deadline=deadline, run_context=active_context),
        lambda: _finder_pass(client, model, user, repo_root, trace=trace,
                             component="finder2",
                             temperature=FINDER2_TEMPERATURE,
                             deadline=deadline, run_context=active_context),
        stage="finder", trace=trace,
    )
    if anchor_outcome.error is not None:
        raise anchor_outcome.error
    result = anchor_outcome.value
    if result.reason == "bad_submits":
        raise RuntimeError(
            "submit_review still invalid after "
            f"{MAX_SUBMIT_ATTEMPTS} attempts"
        )
    if result.reason == "deadline":
        raise RuntimeError("review deadline exhausted before the anchor finder "
                           "submitted a valid review")
    if result.reason == "token_budget":
        raise RuntimeError("review run context token budget exhausted")
    if result.reason != "ok":
        raise RuntimeError(f"agent did not finish within {MAX_STEPS} steps")

    review, u = result.payload, result.usage
    print(f"[done] steps={result.steps} tokens_in={u.prompt_tokens} "
          f"tokens_out={u.completion_tokens}", file=sys.stderr)

    # Sampling run (W12): temperature>0 buys recall on intermittently-found
    # bugs. Any failure degrades to the anchor run alone -- the second run
    # must never be able to break a review (mirrors the verifier fail-open).
    steps = result.steps
    extra = []
    if sample_outcome.error is None:
        result2 = sample_outcome.value
        reason2 = result2.reason
    elif isinstance(sample_outcome.error, RuntimeError):
        result2, reason2 = None, "text_answer"
    elif isinstance(sample_outcome.error,
                    (openai.AuthenticationError, openai.RateLimitError)):
        # Account-level failures poison every later call (the verifier
        # would fail open on the same cause) and main() has dedicated
        # actionable exits for exactly these two -- let them through.
        raise sample_outcome.error
    elif isinstance(sample_outcome.error, openai.OpenAIError):
        # Per-request failures (timeout, 5xx, connection) degrade exactly
        # like protocol failures -- otherwise an exception here discards
        # the completed anchor run.
        e = sample_outcome.error
        result2, reason2 = None, f"api_error:{type(e).__name__}"
    else:
        raise sample_outcome.error
    if result2 is not None and reason2 == "ok":
        u2 = result2.usage
        print(f"[finder2 done] steps={result2.steps} "
              f"tokens_in={u2.prompt_tokens} tokens_out={u2.completion_tokens}",
              file=sys.stderr)
        steps += result2.steps
        extra = result2.payload.get("findings", [])
    else:
        print(f"[finder2] FAILED ({reason2}) -- degrading to the anchor run "
              "alone", file=sys.stderr)
        tev(trace, "finder2_failed", reason=reason2)

    union, n_merged = dedup_union(review.get("findings", []), extra)
    active_context.record_stage(
        "finder", candidates=len(union), merged=n_merged, steps=steps
    )
    tev(trace, "finder_union", n_run1=len(review.get("findings", [])),
        n_run2=len(extra), n_merged=n_merged, n_union=len(union))

    # Scope (W12): findings outside the diff's changed files are demoted to
    # out_of_scope_findings, unverified -- decided in code, not by prompt.
    in_scope, out_of_scope = split_by_scope(union, parse_diff(diff_text)[0])
    if out_of_scope:
        print(f"[scope] {len(out_of_scope)} finding(s) outside the diff's "
              "changed files -> out_of_scope_findings (unverified)",
              file=sys.stderr)
    review["findings"] = in_scope
    review["out_of_scope_findings"] = out_of_scope

    if use_verify:
        # Persist the verifier's exact input (W13): replays reuse it
        # verbatim, keeping candidate order identical to the live run.
        review["candidate_findings"] = [dict(f) for f in in_scope]
        kept, dropped, vstatus = _verify_findings(
            client, model, user, in_scope, repo_root, trace=trace,
            deadline=deadline,
        )
        print(f"[verifier] kept {len(kept)}/{len(kept) + len(dropped)}",
              file=sys.stderr)
        review["findings"] = kept
        review["dropped_findings"] = dropped
        # "failed_open" means these findings are UNFILTERED (broken verifier
        # kept everything) -- consumers must see that, not infer it.
        review["verifier_status"] = vstatus
        active_context.record_stage(
            "verifier", status=vstatus, kept=len(kept), dropped=len(dropped)
        )
    tev(trace, "review", steps=steps, findings=len(review["findings"]),
        dropped=len(review.get("dropped_findings", [])),
        out_of_scope=len(out_of_scope))
    run_summary = active_context.close()
    del run_summary  # RunContext is intentionally not persisted as durable memory.
    return review


def _git_diff_text(args) -> str:
    """Resolve the diff to review from git / gh instead of a file.

    The repo working tree is what read_file and the context pack see, so
    it must be checked out at the post-change state of whatever diff is
    reviewed (HEAD for --commit HEAD, the PR branch for --pr, the current
    tree for --uncommitted).
    """
    # Values are passed to git/gh as separate argv entries (no shell), but a
    # leading "-" would still be parsed as an option by the tool itself
    # (argument injection, e.g. --commit=--output=x). Reject those outright.
    if args.pr:
        pr = str(args.pr).strip()
        if not (pr.isdigit() or pr.startswith("https://")):
            sys.exit(f"--pr must be a PR number or https URL, got {args.pr!r}")
        cmd = ["gh", "pr", "diff", pr]
    elif args.uncommitted:
        cmd = ["git", "diff", "HEAD", "--no-color", "--unified=3"]
    else:
        if args.commit.startswith("-"):
            sys.exit(f"--commit must be a revision, not an option: {args.commit!r}")
        cmd = ["git", "show", args.commit, "--format=", "--no-color", "--unified=3"]
        if args.commit != "HEAD":
            print(
                "[warn] reviewing a non-HEAD commit but read_file sees the current "
                "working tree -- check out that commit for consistent context",
                file=sys.stderr,
            )
    try:
        proc = subprocess.run(cmd, cwd=args.repo, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        sys.exit(f"{cmd[0]!r} not found on PATH")
    if proc.returncode != 0:
        sys.exit(f"{cmd[0]} failed: {_safe_command_error(proc.stderr)}")
    if not proc.stdout.strip():
        sys.exit(f"{cmd[0]} produced an empty diff -- nothing to review")
    return proc.stdout


def _safe_command_error(stderr: object) -> str:
    """Map external stderr to a bounded category without echoing raw output."""

    normalized = str(stderr).casefold()
    for marker, category in (
        ("bad revision", "bad revision"),
        ("permission denied", "permission denied"),
        ("authentication", "authentication failed"),
        ("rate limit", "rate limited"),
        ("not found", "not found"),
    ):
        if marker in normalized:
            return category
    return "external command error"


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "repair":
        from code_review_agent.repair import repair_cli_main
        return repair_cli_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description="Minimal code-review agent")
    parser.add_argument("diff", nargs="?",
                        help="Path to a unified diff file (or use --commit/--uncommitted/--pr)")
    parser.add_argument("--repo", default=".", help="Repo root for read_file context")
    parser.add_argument("--commit", metavar="SHA", nargs="?", const="HEAD",
                        help="Review a git commit in --repo (default HEAD)")
    parser.add_argument("--uncommitted", action="store_true",
                        help="Review uncommitted changes in --repo (git diff HEAD)")
    parser.add_argument("--pr", metavar="N",
                        help="Review GitHub PR #N in --repo (needs gh; check out the PR branch first)")
    parser.add_argument("--no-context", action="store_true",
                        help="Skip proactive context retrieval (ablation)")
    parser.add_argument(
        "--context-mode",
        choices=[mode.value for mode in ContextMode],
        default=ContextMode.CURRENT_STATIC.value,
        help="Context ablation: off, current_static, or hierarchical",
    )
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip the verifier second pass (ablation)")
    parser.add_argument("--trace", metavar="PATH",
                        help="Write a JSONL run trace to this path")
    parser.add_argument("--format", choices=["json", "md"], default="json",
                        help="Output format: json (default) or md (PR-comment markdown)")
    parser.add_argument("--out", metavar="PATH",
                        help="Also write the formatted review to this file")
    parser.add_argument("--post", action="store_true",
                        help="Post the review to the PR as inline comments "
                             "via gh api (requires --pr)")
    parser.add_argument("--post-dry-run", action="store_true",
                        help="Print the gh command and review payload "
                             "without posting (requires --pr)")
    args = parser.parse_args()

    if args.no_context and args.context_mode != ContextMode.CURRENT_STATIC.value:
        parser.error("--no-context cannot be combined with an explicit --context-mode")

    sources = [bool(args.diff), bool(args.commit), args.uncommitted, bool(args.pr)]
    if sum(sources) != 1:
        parser.error("give exactly one diff source: a diff file, --commit, "
                     "--uncommitted, or --pr")
    if args.post or args.post_dry_run:
        if not args.pr:
            parser.error("--post/--post-dry-run need --pr (inline comments "
                         "attach to a pull request)")
        # Validate the API path now: an unpostable --pr form must fail
        # before the review runs, not after the tokens are spent.
        from code_review_agent.github_review import pr_api_path
        try:
            pr_api_path(args.pr)
        except ValueError as e:
            parser.error(str(e))
    if args.diff:
        diff_text = Path(args.diff).read_text(encoding="utf-8", errors="replace")
    else:
        diff_text = _git_diff_text(args)
    client, model = make_client()
    trace = Trace(args.trace) if args.trace else None
    # Reproducibility: record what actually served this run -- provider
    # model ids are aliases the vendor can repoint, so cross-run comparisons
    # need the id (and date) on the trace itself.
    tev(trace, "meta", provider=os.environ.get("LLM_PROVIDER", "deepseek"),
        model=model)
    trace_error: tuple[str, str] | None = None
    try:
        review = run_review(client, diff_text, Path(args.repo), model,
                            use_context=not args.no_context,
                            use_verify=not args.no_verify,
                            trace=trace,
                            context_mode=(ContextMode.OFF.value if args.no_context
                                          else args.context_mode))
    except openai.AuthenticationError as exc:
        trace_error = (type(exc).__name__, "auth")
        sys.exit("Invalid or missing API key -- check your .env / environment variable")
    except openai.RateLimitError as exc:
        trace_error = (type(exc).__name__, "rate_limit")
        sys.exit("Rate limited even after retries -- wait and rerun")
    except openai.APIStatusError as e:
        trace_error = (type(e).__name__, "provider")
        sys.exit(f"API error {e.status_code}")
    except openai.APIConnectionError as exc:
        trace_error = (type(exc).__name__, "connection")
        sys.exit("Network error -- check connection/proxy")
    except BaseException as exc:
        trace_error = (type(exc).__name__, "internal")
        raise
    finally:
        if trace:
            if trace_error is None:
                trace.close()
            else:
                trace.close(
                    error_type=trace_error[0],
                    error_category=trace_error[1],
                )

    if args.format == "md":
        from code_review_agent.render import render_markdown
        src = args.diff or (f"PR #{args.pr}" if args.pr else
                            "uncommitted changes" if args.uncommitted else args.commit)
        output = render_markdown(review, title=f"Code review: {src}")
    else:
        output = json.dumps(review, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"[out] review written to {args.out}", file=sys.stderr)
        if args.pr and args.format == "md":
            print(f"[hint] post it with: gh pr comment {args.pr} "
                  f"--body-file {args.out}", file=sys.stderr)
    print(output)

    if args.post or args.post_dry_run:
        from code_review_agent.github_review import (build_review_payload,
                                                     format_dry_run,
                                                     gh_post_command)
        payload = build_review_payload(review, diff_text,
                                       title=f"Code review: PR #{args.pr}")
        if args.post_dry_run:
            print(format_dry_run(args.pr, payload))
        else:
            proc = subprocess.run(gh_post_command(args.pr, payload),
                                  cwd=args.repo,
                                  input=json.dumps(payload,
                                                   ensure_ascii=False),
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if proc.returncode != 0:
                sys.exit(f"gh api post failed: {_safe_command_error(proc.stderr)}")
            print(f"[post] review posted to PR #{args.pr} "
                  f"({len(payload['comments'])} inline comment(s))",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
