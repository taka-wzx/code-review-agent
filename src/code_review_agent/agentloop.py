"""Shared submit-tool loop engine for the finder and verifier.

agent.py's run_review and verifier.py's _verify_pass drive the same
protocol: call the model with explore tools + a submit tool; execute tool
calls and feed the results back; a submit call only ends the loop when its
payload validates, otherwise the problems are returned as the tool result
and the loop continues; on the last step the explore tools are withdrawn
and the submit is demanded. This module is that protocol, written once.

The callers keep everything semantic: prompts, schemas, parsing and
validation (including malformed-JSON wording), and what success or failure
means. Behavior contract: the request sequence (messages, tools, sampling
params) and the trace events are identical to the pre-refactor
per-component loops -- pinned by tests/test_golden.py. stderr wording is
not part of the contract.
"""
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from code_review_agent.tracelog import tev


@dataclass
class LoopResult:
    payload: Any = None    # validated submit payload; None = loop failed
    steps: int = 0         # LLM rounds consumed
    usage: Any = None      # usage of the last LLM response
    problems: list = field(default_factory=list)  # last validation problems
    reason: str = ""       # "ok" | "bad_submits" | "text_answer" | "step_cap"


def run_submit_loop(client, model: str, messages: list, *,
                    explore_tools: list, submit_tool: dict,
                    parse: Callable[[str], tuple[Any, list]],
                    session,
                    max_steps: int, max_submit_attempts: int,
                    max_tokens: int, temperature: float = 0.0,
                    budget_msg: str,
                    reject_msg: Callable[[list], str],
                    trace=None, component: str = "",
                    label: str = "",
                    on_text_answer: str = "raise",
                    text_answer_problem: str = "",
                    text_answer_nudge: str = "") -> LoopResult:
    """Run the loop until a validated submit, a failure, or the step cap.

    parse: raw submit-arguments JSON -> (payload, problems); the payload
    counts only when problems is empty. budget_msg is injected as a user
    message on the final step. reject_msg(problems) becomes the tool
    result of a rejected submit. label prefixes stderr step logs (e.g.
    "verifierA"). on_text_answer: "raise" (finder) raises RuntimeError
    when the model answers in plain text; "count" (verifier) counts it as
    a bad submit, feeds text_answer_nudge back, and keeps going.
    """
    submit_name = submit_tool["function"]["name"]
    step_label = f"{label} step" if label else "step"
    bad_submits = 0
    empty_retried = False
    last_problems: list = []
    for step in range(1, max_steps + 1):
        # Graceful stop condition: on the last step, withdraw the explore
        # tools and demand the submit, instead of failing at the cap.
        final = step == max_steps
        if final:
            messages.append({"role": "user", "content": budget_msg})
        response = client.chat.completions.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            tools=[submit_tool] if final else explore_tools + [submit_tool],
            tool_choice="auto", messages=messages,
        )
        msg = response.choices[0].message
        tool_calls = msg.tool_calls or []
        u = response.usage
        # Provider cache accounting (DeepSeek splits prompt_tokens into
        # cache hit/miss). Keys are included only when the SDK object has
        # them, so traces from other providers are unchanged.
        cache = {k: v for k, v in (
            ("cache_hit", getattr(u, "prompt_cache_hit_tokens", None)),
            ("cache_miss", getattr(u, "prompt_cache_miss_tokens", None)),
        ) if v is not None}
        tev(trace, "llm_response", component=component, step=step,
            tool_calls=[tc.function.name for tc in tool_calls],
            tokens_in=u.prompt_tokens, tokens_out=u.completion_tokens,
            **cache)

        # A submit call only ends the run if its payload validates;
        # otherwise the problems are fed back as the tool result and the
        # loop continues.
        submit = next((tc for tc in tool_calls
                       if tc.function.name == submit_name), None)
        problems: list = []
        if submit is not None:
            payload, problems = parse(submit.function.arguments)
            if not problems:
                return LoopResult(payload=payload, steps=step, usage=u,
                                  reason="ok")
            bad_submits += 1
            last_problems = problems
            print(f"[{step_label} {step}] {submit_name} rejected: {problems}",
                  file=sys.stderr)
            tev(trace, "submit_rejected", component=component,
                problems=problems)
            if bad_submits >= max_submit_attempts:
                return LoopResult(steps=step, usage=u, problems=problems,
                                  reason="bad_submits")

        if not tool_calls:
            # One free retry on a completely empty response (no text, no
            # tool calls): a W16 real-PR run hit this provider glitch mid-
            # exploration and the fatal anchor run died on it. Nothing is
            # appended, so the identical request is re-sent; a second empty
            # response falls through to the normal text-answer handling.
            # The retry consumes a step, keeping the loop bounded.
            if not (msg.content or "").strip() and not empty_retried:
                empty_retried = True
                print(f"[{step_label} {step}] empty response; retrying once",
                      file=sys.stderr)
                tev(trace, "empty_response_retry", component=component,
                    step=step)
                continue
            if on_text_answer == "raise":
                raise RuntimeError(
                    f"model stopped without calling {submit_name}; got:\n"
                    f"{msg.content!r}")
            # "count": a text answer is a failed attempt, then a nudge.
            bad_submits += 1
            last_problems = [text_answer_problem]
            print(f"[{step_label} {step}] {text_answer_problem}",
                  file=sys.stderr)
            if bad_submits >= max_submit_attempts:
                return LoopResult(steps=step, usage=u,
                                  problems=last_problems,
                                  reason="text_answer")
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user", "content": text_answer_nudge})
            continue

        # Execute this round's tool calls; a rejected submit gets its
        # problem list back as the tool result.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name,
                             "arguments": tc.function.arguments},
            } for tc in tool_calls],
        })
        for tc in tool_calls:
            if tc is submit:
                content = reject_msg(problems)
            else:
                print(f"[{step_label} {step}] {tc.function.name} "
                      f"{tc.function.arguments[:120]}", file=sys.stderr)
                content = session.execute(tc.function.name,
                                          tc.function.arguments)
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": content})

    return LoopResult(steps=max_steps, problems=last_problems,
                      reason="step_cap")
