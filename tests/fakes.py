"""Offline test doubles: a scripted OpenAI-compatible client and a trace
recorder.

FakeClient replays a fixed list of responses and records a deep copy of
every create() kwargs, so tests can assert the exact request sequence
(model, tools, messages) the agent code produced -- the golden contract
the behavior-preserving refactor must not change.
"""
import copy
import json
from types import SimpleNamespace


def tool_call(call_id: str, name: str, arguments) -> SimpleNamespace:
    """One tool call inside a scripted response. arguments may be a dict
    (JSON-encoded here) or a raw string (to test malformed payloads)."""
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return SimpleNamespace(
        id=call_id, type="function",
        function=SimpleNamespace(name=name, arguments=arguments))


def response(tool_calls=None, content=None, tokens_in=100, tokens_out=20):
    """A chat.completions.create() return value, shaped like the SDK's."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        usage=SimpleNamespace(prompt_tokens=tokens_in,
                              completion_tokens=tokens_out))


class FakeClient:
    """Pops scripted responses in order; raises if the code under test
    makes more requests than the script provides. A scripted entry that is
    an Exception instance is raised instead of returned (transport/API
    failure injection)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # messages is mutated in place by the agent loop between calls, so
        # snapshot now or every recorded request aliases the final state.
        self.requests.append(copy.deepcopy(kwargs))
        if not self._responses:
            raise AssertionError("FakeClient: script exhausted, unexpected "
                                 f"request #{len(self.requests)}")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class FakeTrace:
    """Records tev() events as dicts (what would land in the JSONL,
    minus the timestamp)."""

    def __init__(self):
        self.events: list[dict] = []

    def event(self, kind: str, **data) -> None:
        self.events.append({"kind": kind, **data})

    def close(self) -> None:
        pass
