"""Active context retrieval for the review agent (W3).

Given a diff and a repo root, builds a "context pack" the reviewer sees up
front, instead of having to discover everything through read_file calls:

  1. project conventions (CLAUDE.md / CONVENTIONS.md at repo root)
  2. full post-change content of every file the diff touches
  3. modules those changed files import (W8: flag/constant definitions the
     diff depends on -- the d7 dead-flag gap), plus an explicit note when
     an in-project import cannot resolve (feeds missing-dep detection)
  4. callers of every function the diff adds or modifies (incl. tests)

Retrieval is symbol-level string search -- no embeddings. Everything is
budgeted so the pack cannot blow up the prompt.
"""
import re
from pathlib import Path

from code_review_agent.tools import _iter_text_files

CONVENTION_FILES = ("CLAUDE.md", "CONVENTIONS.md", "CONTRIBUTING.md")
CONVENTIONS_CAP = 6_000      # chars per conventions file
CHANGED_FILE_CAP = 8_000     # chars per changed file
IMPORT_FILE_CAP = 5_000      # chars per imported-module file
MAX_IMPORT_FILES = 4         # imported modules prefetched per pack
SNIPPET_CTX_LINES = 3        # lines of context around a caller line
MAX_CALLER_FILES = 3         # caller files per symbol
MAX_HITS_PER_FILE = 3        # snippets per caller file
PACK_CAP = 28_000            # total chars for the whole pack

# Accepts both `+++ b/path` (default git) and `+++ path` (--no-prefix);
# stops at a tab so `+++ path<TAB>timestamp` (diff -u style) keeps only the
# path. A silent parse failure here is costly: empty changed_files makes
# split_by_scope fail open and scope filtering vanishes without a trace.
_DIFF_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?([^\t\r\n]+)", re.MULTILINE)
_ADDED_DEF_RE = re.compile(r"^\+\s*def\s+(\w+)", re.MULTILINE)
_HUNK_DEF_RE = re.compile(r"^@@[^@]*@@\s*def\s+(\w+)", re.MULTILINE)
_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
                        re.MULTILINE)


def parse_diff(diff_text: str) -> tuple[list[str], list[str]]:
    """Changed file paths + names of functions the diff adds or touches."""
    files = [f.rstrip() for f in _DIFF_FILE_RE.findall(diff_text)]
    files = [f for f in files if f != "/dev/null"]
    symbols = set(_ADDED_DEF_RE.findall(diff_text)) | set(_HUNK_DEF_RE.findall(diff_text))
    return files, sorted(symbols)


def _read_capped(path: Path, cap: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= cap else text[:cap] + "\n...[truncated]"


def find_callers(repo: Path, symbol: str, exclude: set[str]) -> list[tuple[str, str]]:
    """(rel_path, snippet) for files that call `symbol(` outside its def.

    `exclude`: repo-relative paths to skip (the changed files themselves --
    their full content is already in the pack).
    """
    out = []
    # Shared pruned walk (tools._iter_text_files): never descends into
    # vcs/venv/cache trees (rglob walked all of .venv before filtering, and
    # find_callers runs once per changed symbol), and matches the suffix
    # case-insensitively like every other repo tool.
    for py in _iter_text_files(repo):
        if py.suffix.lower() != ".py":
            continue
        rel = py.relative_to(repo).as_posix()
        if rel in exclude:
            continue
        try:
            lines = py.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        # Word-boundary call match: `run(` must not hit `overrun(`, and a
        # def line (sync or async) is the definition, not a caller. A dot
        # before the name stays a match -- `obj.run(` is a genuine call site.
        call_re = re.compile(r"(?<!\w)" + re.escape(symbol) + r"\s*\(")
        hits = [i for i, ln in enumerate(lines)
                if call_re.search(ln)
                and not re.match(r"\s*(?:async\s+)?def\s", ln)]
        if not hits:
            continue
        snippets = []
        for i in hits[:MAX_HITS_PER_FILE]:
            lo = max(0, i - SNIPPET_CTX_LINES)
            hi = min(len(lines), i + SNIPPET_CTX_LINES + 1)
            snippets.append("\n".join(f"{n + 1}: {lines[n]}" for n in range(lo, hi)))
        out.append((rel, "\n   ...\n".join(snippets)))
        if len(out) >= MAX_CALLER_FILES:
            break
    return out


def build_context(diff_text: str, repo: Path, log=lambda msg: None) -> str:
    """Assemble the context pack. Returns "" when nothing was retrievable.

    `log` gets one human-readable line per retrieval decision (observability).
    """
    repo = Path(repo)
    if not repo.is_dir():
        log(f"repo root not found: {repo}")
        return ""
    files, symbols = parse_diff(diff_text)
    log(f"diff touches files={files} symbols={symbols}")

    sections: list[str] = []

    for name in CONVENTION_FILES:
        p = repo / name
        if p.is_file():
            sections.append(f"## Project conventions ({name})\n\n"
                            + _read_capped(p, CONVENTIONS_CAP))
            log(f"conventions: {name} ({p.stat().st_size} bytes)")

    for rel in files:
        p = repo / rel
        if p.is_file():
            sections.append(f"## Changed file: {rel} (full post-change content)\n\n"
                            "```python\n" + _read_capped(p, CHANGED_FILE_CAP) + "\n```")
            log(f"changed file: {rel}")
        else:
            log(f"changed file MISSING in repo: {rel}")

    # Chase imports of the changed files (post-state): flag/constant
    # definitions the diff depends on become visible without a tool call.
    seen_mods: set[str] = set()
    n_imports = 0
    for rel in files:
        p = repo / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in _IMPORT_RE.finditer(text):
            mod = m.group(1) or m.group(2)
            if not mod or mod.startswith(".") or mod in seen_mods:
                continue
            seen_mods.add(mod)
            mod_rel = mod.replace(".", "/") + ".py"
            if mod_rel in files:
                continue   # full content already in the pack
            mp = repo / mod_rel
            if mp.is_file():
                if n_imports >= MAX_IMPORT_FILES:
                    log(f"import cap reached; skipping {mod_rel}")
                    continue
                sections.append(f"## Imported module: {mod_rel} (imported by {rel})\n\n"
                                "```python\n" + _read_capped(mp, IMPORT_FILE_CAP) + "\n```")
                n_imports += 1
                log(f"import: {mod_rel} (from {rel})")
            else:
                top = mod.split(".")[0]
                if (repo / top).is_dir() or (repo / (top + ".py")).is_file():
                    sections.append(f"## Import note\n\n`{mod}` is imported by {rel} "
                                    f"but `{mod_rel}` does not exist in this "
                                    "repository -- the import cannot resolve.")
                    log(f"import MISSING: {mod} (from {rel})")
                else:
                    log(f"import external/stdlib: {mod}")

    exclude = set(files)
    for sym in symbols:
        callers = find_callers(repo, sym, exclude)
        for rel, snippet in callers:
            sections.append(f"## Caller of {sym}() in {rel}\n\n"
                            "```python\n" + snippet + "\n```")
            log(f"callers of {sym}: {rel}")
        if not callers:
            log(f"callers of {sym}: none found")

    pack, used = [], 0
    for s in sections:
        if used + len(s) > PACK_CAP:
            pack.append("[context budget reached; remaining sections dropped]")
            log(f"budget cap hit at {used} chars")
            break
        pack.append(s)
        used += len(s)
    log(f"context pack: {len(pack)} sections, {used} chars")
    return "\n\n".join(pack)
