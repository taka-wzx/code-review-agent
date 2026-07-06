"""JSONL run traces (W6): one event per line, append-as-you-go.

A trace answers "what did the agent actually do this run" -- every LLM
response (tool calls chosen, token usage), every tool execution, every
rejected submit, the final outcome. That is what makes run-to-run variance
debuggable and step-efficiency metrics (wasted/repeat calls) computable.

Named tracelog (not trace) to avoid shadowing the stdlib trace module.
"""
import json
import time
from pathlib import Path


class Trace:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("w", encoding="utf-8")

    def event(self, kind: str, **data) -> None:
        rec = {"t": round(time.time(), 3), "kind": kind, **data}
        self._f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def tev(trace, kind: str, **data) -> None:
    """Emit an event if tracing is on; no-op when trace is None."""
    if trace is not None:
        trace.event(kind, **data)
