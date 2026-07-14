"""Shared repo tools for the finder and verifier (W6).

Two read-only tools -- read_file and search_repo -- plus a ToolSession that
executes tool calls with two guardrails from the failure-mode playbook:

  * recoverable errors: a failed call returns what went wrong AND what to
    try next (candidate paths, "symbol does not exist in this repo"), so
    the model can act on the failure instead of guessing;
  * repeat-call short-circuit: an identical call in the same session gets
    a stub answer instead of re-burning tokens (loop guard).
"""
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path

from code_review_agent.tracelog import tev

SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".cfg", ".ini", ".json", ".yaml", ".yml"}
# Secrets-shaped files the model must never read: their contents would land
# in the conversation and any --trace file on disk. Matched against the
# lowercased basename (search/listings already exclude them via TEXT_SUFFIXES,
# so this guard only needs to cover direct read_file/run_linter paths).
SENSITIVE_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", ".netrc",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "credentials*",
)
READ_CAP = 50_000        # chars per read_file result
SEARCH_MAX_HITS = 40     # lines returned per search
SEARCH_LINE_CAP = 200    # chars per returned hit line
MAX_CANDIDATES = 10      # "did you mean" suggestions on a missing path

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a file from the repository under review. Call this when "
            "the diff alone is not enough context -- e.g. to see the full "
            "function, its callers, or related code. Large files are "
            "truncated; continue with start_line."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path"},
                "start_line": {
                    "type": "integer",
                    "description": "1-based line to start from (default 1); "
                                   "use it to continue past a truncated result",
                },
            },
            "required": ["path"],
        },
    },
}

RUN_LINTER_TOOL = {
    "type": "function",
    "function": {
        "name": "run_linter",
        "description": (
            "Run a static linter (pyflakes) on one Python file in the "
            "repository. Catches undefined names, unused imports, "
            "redefinitions, and syntax errors without executing any code. "
            "Use it to confirm suspicions like 'is this name actually "
            "imported/defined in this file?'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Repo-relative path of a .py file"},
            },
            "required": ["path"],
        },
    },
}

SEARCH_REPO_TOOL = {
    "type": "function",
    "function": {
        "name": "search_repo",
        "description": (
            "Search all Python/text files in the repository for a literal "
            "string (not a regex). Returns matching lines as path:line: text. "
            "Use it to find where a symbol is defined or used, to chase an "
            "import, or to check whether something exists in the repo at all."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string",
                            "description": "Literal text to search for, e.g. 'def normalize_ts' or 'FRAME_DT_S'"},
            },
            "required": ["pattern"],
        },
    },
}


def _iter_text_files(root: Path):
    # os.walk with in-place pruning: SKIP_DIRS are never descended into
    # (rglob("*") walked the whole .venv/.git tree before filtering).
    # Collected then sorted so the ordering matches the old sorted(rglob).
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in TEXT_SUFFIXES and p.is_file():
                found.append(p)
    yield from sorted(found)


def _refuse_read(root: Path, target: Path, rel_path: str) -> str | None:
    """Guard for tools that read one file directly: refuse non-project
    directories and secrets-shaped files. Returns an Error string, or None
    when the read is allowed."""
    if any(part in SKIP_DIRS for part in target.relative_to(root).parts):
        return (f"Error: {rel_path} is inside a non-project directory "
                "(vcs/venv/cache) -- these are not part of the code under "
                "review.")
    name = target.name.lower()
    if any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_FILE_PATTERNS):
        return (f"Error: refusing to read {rel_path}: it matches a secrets "
                "file pattern (.env, keys, credentials). Its contents are "
                "never needed for a code review.")
    return None


def _missing_file_msg(root: Path, rel_path: str) -> str:
    """Recoverable error text for a path that is not a file in the repo."""
    name = Path(rel_path).name
    candidates = [p.relative_to(root).as_posix() for p in _iter_text_files(root)
                  if p.name == name][:MAX_CANDIDATES]
    if candidates:
        return (f"Error: file not found: {rel_path}. Files with the same "
                f"name do exist -- did you mean: {', '.join(candidates)}?")
    return (f"Error: file not found: {rel_path}, and no file named "
            f"{name!r} exists anywhere in this repo. If the diff imports "
            "or references it, that unresolved reference may itself be a "
            "defect worth reporting. Use search_repo to look for the "
            "symbols you expected it to contain.")


def read_file(repo_root: Path, rel_path: str, start_line: int = 1) -> str:
    root = Path(repo_root).resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        return f"Error: path escapes repo root: {rel_path}. Use repo-relative paths only."
    refusal = _refuse_read(root, target, rel_path)
    if refusal:
        return refusal
    if target.is_dir():
        listing = [p.relative_to(root).as_posix() for p in _iter_text_files(root)
                   if p.is_relative_to(target)][:50]
        return (f"Error: {rel_path or '.'} is a directory, not a file. "
                "Files under it: " + (", ".join(listing) or "(none)"))
    if not target.is_file():
        return _missing_file_msg(root, rel_path)
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, int(start_line))
    if start > len(lines):
        return f"Error: start_line={start} is past the end of {rel_path} ({len(lines)} lines)."
    out, used = [], 0
    for n in range(start - 1, len(lines)):
        ln = lines[n]
        if used + len(ln) > READ_CAP:
            out.append(f"...[truncated; continue with read_file(path={rel_path!r}, "
                       f"start_line={n + 1})]")
            break
        out.append(ln)
        used += len(ln) + 1
    prefix = f"[{rel_path} from line {start}]\n" if start > 1 else ""
    return prefix + "\n".join(out)


def search_repo(repo_root: Path, pattern: str) -> str:
    root = Path(repo_root).resolve()
    if not pattern or not pattern.strip():
        return "Error: empty search pattern."
    hits, n_files = [], 0
    for p in _iter_text_files(root):
        n_files += 1
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        for i, ln in enumerate(lines, 1):
            if pattern in ln:
                hits.append(f"{rel}:{i}: {ln.strip()[:SEARCH_LINE_CAP]}")
                if len(hits) >= SEARCH_MAX_HITS:
                    hits.append(f"...[stopped at {SEARCH_MAX_HITS} hits; narrow the pattern]")
                    return "\n".join(hits)
    if not hits:
        msg = (f"No matches for {pattern!r} anywhere in the repo "
               f"(searched {n_files} files).")
        # A regex-looking pattern that missed is probably a usage error, not
        # proof of absence -- say so instead of misleading the model.
        if any(ch in pattern for ch in "\\^$*+?[]|"):
            return (msg + " NOTE: search is literal, not regex -- your pattern "
                    "contains regex-like characters that were matched as raw "
                    "text. Retry with plain text before concluding anything.")
        return msg + " This text/symbol does not exist in this repository."
    return "\n".join(hits)


LINT_TIMEOUT_S = 30
LINT_OUTPUT_CAP = 4_000


def run_linter(repo_root: Path, rel_path: str) -> str:
    """Static lint (pyflakes) of one repo .py file; never executes it."""
    root = Path(repo_root).resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        return f"Error: path escapes repo root: {rel_path}. Use repo-relative paths only."
    refusal = _refuse_read(root, target, rel_path)
    if refusal:
        return refusal
    if not target.is_file():
        return _missing_file_msg(root, rel_path)
    if target.suffix.lower() != ".py":
        return (f"Error: run_linter only lints Python files; {rel_path} "
                "is not a .py file.")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pyflakes", str(target)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=LINT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"Error: linter timed out after {LINT_TIMEOUT_S}s on {rel_path}."
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if "No module named pyflakes" in out:
        return "Error: pyflakes is not installed in this environment; lint unavailable."
    if not out:
        return f"No lint findings in {rel_path}."
    out = out.replace(str(target), rel_path)
    if len(out) > LINT_OUTPUT_CAP:
        out = out[:LINT_OUTPUT_CAP] + "\n...[truncated]"
    return out


class ToolSession:
    """Executes read_file/search_repo/run_linter calls for one agent conversation."""

    MISS_STREAK_NUDGE_AT = 3  # consecutive search misses before nudging

    def __init__(self, repo_root: Path, trace=None, component: str = ""):
        self.repo_root = Path(repo_root)
        self.trace = trace
        self.component = component
        self._seen: set[tuple] = set()
        self._miss_streak = 0

    def execute(self, name: str, arguments_json: str) -> str:
        try:
            args = json.loads(arguments_json or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            result = f"Error: malformed tool arguments: {e}"
            tev(self.trace, "tool", component=self.component, tool=name,
                args=arguments_json, error=True)
            return result

        key = (name, json.dumps(args, sort_keys=True))
        repeat = key in self._seen
        if repeat:
            result = (f"[repeat call] You already called {name} with these exact "
                      "arguments in this conversation -- the result is above. "
                      "Repeating identical calls adds no information; either use "
                      "different arguments or proceed to your conclusion.")
        else:
            self._seen.add(key)
            try:
                if name == "read_file":
                    result = read_file(self.repo_root, args.get("path", ""),
                                       int(args.get("start_line", 1) or 1))
                elif name == "search_repo":
                    result = search_repo(self.repo_root, args.get("pattern", ""))
                elif name == "run_linter":
                    result = run_linter(self.repo_root, args.get("path", ""))
                else:
                    result = f"Error: unknown tool {name!r}."
            except Exception as e:
                result = f"Error: {e}"
            result = self._apply_miss_streak(name, result)
        extra = {}
        if name == "search_repo" and not repeat:
            extra = {"miss": result.startswith("No matches for"),
                     "miss_streak": self._miss_streak}
        tev(self.trace, "tool", component=self.component, tool=name, args=args,
            repeat=repeat, result_chars=len(result),
            error=result.startswith("Error:"), **extra)
        return result

    def _apply_miss_streak(self, name: str, result: str) -> str:
        """Track consecutive search_repo misses; nudge instead of letting the
        model burn its step budget on spelling/syntax variants of one symbol.
        Absence stays a reportable conclusion (same stance as read_file's
        missing-file message) -- the nudge only discourages variant retries."""
        if name != "search_repo":
            if not result.startswith("Error:"):
                self._miss_streak = 0  # productive pivot to another tool
            return result
        # A regex-looking miss is a usage error the tool itself asks to retry
        # once with plain text; that retry is legitimate, so don't count it.
        if result.startswith("No matches for") and "search is literal" not in result:
            self._miss_streak += 1
            if self._miss_streak >= self.MISS_STREAK_NUDGE_AT:
                result += (
                    f"\nNOTE: {self._miss_streak} consecutive misses. One clean "
                    "miss already proves absence in this repository -- record "
                    "the conclusion (an unresolved reference may itself be a "
                    "reportable defect) and move on; do not retry spelling or "
                    "syntax variants of the same symbol.")
        elif not result.startswith("Error:"):
            self._miss_streak = 0
        return result
