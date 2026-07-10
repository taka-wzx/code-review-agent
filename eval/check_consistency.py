"""Consistency checker for eval assets.

Verifies three-way agreement between eval/diffs/*.diff, the fixture repo
(eval/repo/), and eval/truth.json:

  1. every path a diff touches exists in the fixture repo
  2. every hunk's post-image (its ' ' context and '+' added lines) appears
     as one contiguous block in the fixture file -- i.e. the fixture really
     is the post-change state of the diff
  3. truth.json keys == the set of diff names (no orphans either way)

Run it after adding or editing any eval asset:
    python eval/check_consistency.py [BASE_DIR ...]

BASE_DIR defaults to eval/; pass eval/holdout to check the held-out set,
or several dirs to check them all. Each must contain diffs/, repo/,
truth.json.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent          # eval/


def iter_hunks(diff_text):
    """Yield (b_path, [post_image_lines]) for every hunk in a diff."""
    path, hunk = None, None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@"):
            if hunk is not None:
                yield path, hunk
            hunk = []
        elif line.startswith("diff --git") or line.startswith("--- "):
            if hunk is not None:
                yield path, hunk
                hunk = None
        elif hunk is not None:
            if line[:1] == "+" or line[:1] == " " or line == "":
                # "" happens for context blank lines stripped of trailing ws
                hunk.append(line[1:] if line else "")
            # '-' lines belong to the pre-image; ignored
    if hunk is not None:
        yield path, hunk


def check(base: Path = HERE) -> int:
    DIFFS, REPO, TRUTH = base / "diffs", base / "repo", base / "truth.json"
    errors = []

    diff_files = sorted(DIFFS.glob("*.diff"))
    diff_names = {p.stem for p in diff_files}
    if not diff_files:
        errors.append(f"no diffs found in {DIFFS}")

    # 3. truth <-> diffs bijection
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    truth_names = set(truth)
    for name in sorted(diff_names - truth_names):
        errors.append(f"{name}: diff exists but no truth.json entry")
    for name in sorted(truth_names - diff_names):
        errors.append(f"{name}: truth.json entry but no diff file")

    for diff_path in diff_files:
        text = diff_path.read_text(encoding="utf-8", errors="replace")
        n_hunks = 0
        for b_path, hunk in iter_hunks(text):
            n_hunks += 1
            if b_path is None:
                continue  # file deletion; nothing to verify in fixture
            fixture = REPO / b_path
            # 1. path exists
            if not fixture.is_file():
                errors.append(f"{diff_path.stem}: {b_path} missing from fixture repo")
                continue
            # 2. hunk post-image is a contiguous block of the fixture file
            fixture_text = fixture.read_text(encoding="utf-8", errors="replace")
            block = "\n".join(hunk)
            if block and block not in fixture_text:
                # pinpoint the first divergent line for a usable error message
                flines = fixture_text.splitlines()
                first_bad = next((h for h in hunk if h and h not in flines), None)
                errors.append(
                    f"{diff_path.stem}: hunk not contiguous in {b_path}"
                    + (f"; first line missing from file: {first_bad!r}" if first_bad
                       else " (lines exist but not contiguous/in-order)"))
        if n_hunks == 0:
            errors.append(f"{diff_path.stem}: no hunks parsed")

    if errors:
        print(f"FAIL [{base}] ({len(errors)} problem(s)):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK [{base}]: {len(diff_files)} diffs consistent with fixture repo and "
          f"truth.json ({sum(len(v) for v in truth.values())} planted bugs total)")
    return 0


if __name__ == "__main__":
    bases = [Path(a) for a in sys.argv[1:]] or [HERE]
    sys.exit(max(check(b) for b in bases))
