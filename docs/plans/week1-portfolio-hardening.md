# Week 1 portfolio hardening task contract

## Goal

Turn the current repository into a reproducible, clean-clone-friendly Week 1
portfolio baseline: fix the known `src/` import-prefetch defect, make dev
validation installable with one command, enforce coverage and type checks in
CI, add a container entry point, and prepare accurate public-facing release
documentation.

## Base

- Base branch: `integration/multi-agent-orchestration`
- Base commit: `82ddb9612b127f60a112bb19f181fe8a35e60147`
- Codex branch: `codex/week1-portfolio-hardening`
- Integration branch: `integration/week1-portfolio-hardening`

## Frozen interfaces

- Keep the existing `crag` CLI flags and output schemas compatible.
- Keep the default DeepSeek/GLM provider behavior compatible.
- Do not change finder/verifier prompts, sentinel rules, or eval assets.
- Do not run paid LLM evaluations during this task.

## File ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `src/code_review_agent/context.py`, runtime-only type fixes under `src/code_review_agent/`, `tests/`, `pyproject.toml`, `.github/workflows/ci.yml`, `.gitignore`, `.dockerignore`, `Dockerfile`, `scripts/` | `README.md`, `docs/interview-defense.md`, `eval/` |
| Claude Code | `README.md`, `docs/interview-defense.md`, `CHANGELOG.md` | Codex handoff commit and all implementation files |
| Integrator | conflict resolution only | all paths |

## Codex acceptance criteria

- Imports from both flat repositories and `src/`-layout packages are prefetched.
- Missing in-project imports remain distinguishable from external/stdlib imports.
- `pip install -e ".[dev]"` installs all local validation tooling.
- CI covers Python 3.10 through 3.13 on Linux plus Windows 3.11.
- Coverage has a failing minimum threshold of at least 85% for `src/`.
- Mypy checks the runtime package with a documented, non-empty configuration.
- A cross-platform Python validation entry point runs lint, tests with coverage,
  type checks, and package smoke tests without calling an LLM API.
- Docker build metadata never copies `.env`, VCS metadata, local traces, or eval
  results; the image starts the `crag` CLI.
- Existing tests and new regression tests pass without changing eval assets.
- Runtime-only type fixes preserve the CLI, provider, prompt, sentinel, and
  output behavior while making the configured mypy check pass.

## Claude Code acceptance criteria

- README clean-install instructions use the new dev extra and validation entry
  point and accurately state Python 3.10-3.13 support.
- The known `src/` import-prefetch limitation is removed only after reviewing
  the regression test and implementation.
- Test counts and coverage claims are regenerated from the handoff commit;
  stale numbers are not copied forward.
- `CHANGELOG.md` contains concise `v0.1.0` release notes and known limitations.
- Claude commits only its owned documentation files and does not merge or push.

## Validation

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src/code_review_agent
.venv\Scripts\python.exe scripts/verify.py
```

Docker build is required in CI because Docker is not installed on the current
Windows workstation. Paid eval scripts and provider-backed smoke tests are out
of scope.

## Delivery

- Codex provides its branch and full commit SHA.
- Codex provides the exact Claude worktree command and VS Code folder.
- Claude returns its branch, commit SHA, changed files, validation output,
  review findings, and remaining risks.
- The integrator reruns local validation and merges only into the integration
  branch. Publishing a public GitHub repository, pushing, tagging, and merging
  `master` require explicit user confirmation after the secret audit.
