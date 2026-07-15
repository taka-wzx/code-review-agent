"""Run the repository's complete offline developer validation.

Install once with ``python -m pip install -e ".[dev]"``, then run this file
with that same interpreter. No step contacts an LLM provider.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def console_script(name: str) -> str | None:
    """Find an entry point installed for this interpreter before PATH.

    A globally installed command with the same name must not make validation
    pass when the current virtual environment is missing its console script.
    """
    scripts_dir = Path(sysconfig.get_path("scripts"))
    for candidate_name in (f"{name}.exe", name):
        candidate = scripts_dir / candidate_name
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def run(label: str, command: list[str]) -> None:
    print(f"\n== {label} ==", flush=True)
    print("+ " + subprocess.list2cmdline(command), flush=True)
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode:
        raise SystemExit(f"{label} failed with exit code {completed.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run offline repository validation")
    parser.add_argument(
        "--eval-assets",
        action="store_true",
        help="also validate the frozen eval and holdout asset relationships",
    )
    args = parser.parse_args()

    python = sys.executable
    run("Ruff", [python, "-m", "ruff", "check", "."])
    run(
        "Unit and golden tests with coverage",
        [
            python,
            "-m",
            "coverage",
            "run",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ],
    )
    run("Coverage threshold", [python, "-m", "coverage", "report"])
    run("Mypy", [python, "-m", "mypy", "src/code_review_agent"])
    run("Module entry point", [python, "-m", "code_review_agent", "--help"])

    crag = console_script("crag")
    if crag is None:
        raise SystemExit(
            "console entry point 'crag' was not found; install with "
            "`python -m pip install -e \".[dev]\"`"
        )
    run("Console entry point", [crag, "--help"])

    if args.eval_assets:
        run(
            "Eval asset consistency",
            [python, "eval/check_consistency.py", "eval", "eval/holdout"],
        )

    print("\nAll offline validation passed.", flush=True)


if __name__ == "__main__":
    main()
