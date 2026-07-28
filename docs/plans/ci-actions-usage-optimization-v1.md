# CI Actions Usage Optimization v1

Status: **contract ready; implementation not started**

Date: 2026-07-28

## Task identity and new-dialog bootstrap

- Contract: `docs/plans/ci-actions-usage-optimization-v1.md`
- Task branch: `codex/ci-actions-usage-optimization-v1`
- Worktree name: `.codex-worktrees/ci-actions-usage-optimization-v1`
- Frozen stacked base: `567bd3cf9fe97774ce2177275d325c7d30ff1631`
- Base branch at freeze time: `codex/phase11a-synthetic-staging-v1`
- Base relationship: Phase 11A is not yet merged; this is not a `master` baseline.

This file is the durable source of truth for the task. A new conversation must not
create another contract. It must first:

1. open the CI optimization worktree, not the Phase 11A or Phase 11B worktree;
2. read `AGENTS.md`, `docs/agent-contract.md`, this contract, `README.md`, and
   `pyproject.toml` completely;
3. verify `git branch --show-current` equals the task branch above;
4. verify the base and current commit lineage with `git merge-base` and `git log`;
5. inspect `git status --short` and stop if another writer has uncommitted changes;
6. continue from committed task history rather than regenerating this file.

A new conversation still needs an explicit user instruction to begin implementation,
push, create or update a PR, or trigger GitHub Actions. Reading this committed contract
does not by itself grant external-write authority.

## Goal

Reduce GitHub-hosted Actions consumption without weakening the repository's required
quality, compatibility, packaging, container, Postgres, or Compose evidence. The
observable v1 outcome is:

- one full `scripts/verify.py` execution per workflow run instead of five;
- two compatibility jobs on pull requests and four on protected-branch pushes;
- one service-image build path instead of separate service smoke and Compose builds;
- stale runs are canceled only for the same pull request;
- every existing heavy gate still runs on `master`/`main` pushes;
- repository visibility, billing, and runner ownership remain unchanged.

This task is CI orchestration work. It is not Phase 11B publisher work, a model-quality
experiment, a production-readiness result, or evidence that any unexecuted GitHub run
would pass.

## Current measured structure

The frozen workflow starts up to nine GitHub-hosted jobs per run:

- five `test` matrix jobs: Linux Python 3.10, 3.11, 3.12, and 3.13 plus Windows 3.11;
- one lockfile installation job;
- one CLI and service-image smoke job;
- one real-Postgres integration/load job;
- one Compose fake-run job.

Each matrix entry currently calls `scripts/verify.py`, so Ruff, the full unit/coverage
suite, the coverage gate, mypy, and both package-entry smoke checks are repeated five
times. `container-smoke` builds the service image, while the Compose harness builds and
boots the service image again. These are the only redundancies v1 is authorized to
remove.

No percentage or minute saving is pre-claimed. GitHub rounds billable duration per job,
and final savings must be calculated from actual Actions Usage records after an
authorized run.

## Frozen validation semantics

The following behavior must not be weakened:

- `scripts/verify.py` remains the complete offline developer/quality gate and remains
  byte-for-byte read-only in v1.
- Branch coverage remains enabled with `fail_under = 85`.
- Ruff and mypy configuration and findings remain unchanged.
- All tests under `tests/` remain enabled; no test may be deleted, skipped, selected out,
  or have an assertion weakened to save minutes.
- Python 3.10 through 3.13 Linux compatibility remains covered on every protected-branch
  push. Python 3.13 is covered by the full-quality job.
- Windows Python 3.11 remains covered on pull requests and protected-branch pushes.
- Lockfile installation plus editable no-dependency import remains a required gate.
- CLI-image build/start smoke remains required.
- Service-image build/start, explicit migration, API, two-worker, Postgres, and Compose
  fake-run behavior remains required.
- The direct Postgres migration, 50-concurrent-submission/two-worker load gate, and
  `tests.test_phase9c_postgres` remain required.
- Full Git history remains available to every job that runs the repository-wide unit
  suite, because the security attestation checks require commit ancestry.
- All validation remains offline with fake clients. No LLM provider, real GitHub
  publisher, paid API, business repository, or `eval/**` asset is used.

Public Python APIs, CLI semantics, dependencies, lockfiles, migrations, database schema,
runtime code, Dockerfiles, Compose definitions, and persisted data are frozen.

## Authorized v1 workflow design

### Trigger and cancellation policy

Keep the existing triggers exactly:

- `pull_request`;
- `push` to `master` and `main`.

Do not add `schedule`, `workflow_dispatch`, path-level workflow suppression, or new
external triggers in v1. Add workflow-level concurrency keyed by workflow plus pull
request number, with a unique run-ID fallback. `cancel-in-progress` is true only for
`pull_request`; protected-branch runs must never cancel one another.

### Full quality job

Replace the five copies of full validation with one `quality` job:

- runner: `ubuntu-latest`;
- Python: 3.13;
- checkout: `fetch-depth: 0`;
- install: `python -m pip install -e ".[dev]"`;
- command: `python scripts/verify.py` exactly once per workflow run;
- pip download cache keyed by both `requirements.lock` and `pyproject.toml`;
- timeout: 30 minutes.

This job owns Ruff, coverage, mypy, module-entry, console-entry, and one full copy of the
unit/golden suite.

### Compatibility job

Add a separate compatibility matrix that installs `.[dev]` and runs the repository-wide
unit suite without coverage, Ruff, mypy, or duplicate entry-point smoke:

```text
python -m unittest discover -s tests -v
```

Required pull-request entries:

- Ubuntu / Python 3.10;
- Windows / Python 3.11.

Required protected-branch push entries:

- Ubuntu / Python 3.10;
- Ubuntu / Python 3.11;
- Ubuntu / Python 3.12;
- Windows / Python 3.11.

Python 3.13 compatibility is supplied by the full quality job. Compatibility jobs keep
`fetch-depth: 0`, use the same pip cache inputs, and have a 30-minute timeout. Matrix
`fail-fast` is true for pull requests and false for protected-branch pushes so master
retains complete compatibility evidence.

The workflow must express the event-specific entries deterministically. Disabled
matrix entries must not allocate a runner, and their absence must be asserted by the
workflow contract test.

### Lockfile job

Preserve the lockfile job's interpreter, commands, and isolation semantics. It may add
pip download caching and a 20-minute timeout. It must not reuse the mutable environment
from the quality job or replace `requirements.lock` with an unconstrained install.

### Container and Compose job

Merge the current `container-smoke` steps into `compose-fake-run` so they share one
checkout, Python setup, install, and runner:

1. prepare the filtered image context;
2. build and start the CLI image with `--help`;
3. show the Compose runtime;
4. run `scripts/phase9c_container_test.py` for the service-image, migration, API,
   two-worker, Postgres, and fake-run acceptance.

Do not separately build `Dockerfile.service` before the Compose harness, because the
harness already builds and boots that service image. Preserve every harness assertion,
including secret redaction, capability/user checks, lock consistency, health/readiness,
worker scale, graceful shutdown, and cleanup. The combined job has a 45-minute timeout.

### Postgres integration job

Preserve the existing Postgres 16 service, explicit migration, 50-submission load gate,
two-worker behavior, and `tests.test_phase9c_postgres`. Add pip caching and a 30-minute
timeout only. It remains separate from Compose because it verifies the host-installed
package and database concurrency path rather than the container deployment path.

### Deferred optimizations

Path-based skipping, docs-only workflow suppression, scheduled matrices, artifact
sharing, custom images, larger runners, self-hosted runners, repository visibility
changes, and billing changes are out of scope for v1. They require separate evidence
because skipped required checks, cache/storage growth, or runner trust changes can cost
more than they save.

## Single Writer declaration

One active Codex writer owns exactly these paths for implementation:

- `docs/plans/ci-actions-usage-optimization-v1.md`;
- `.github/workflows/ci.yml`;
- `tests/test_ci_workflow_contract.py`;
- `README.md`.

Every other path is read-only. In particular:

- `scripts/verify.py`, `pyproject.toml`, dependency/lock files, runtime source,
  migrations, Dockerfiles, and Compose files are frozen;
- no command may enumerate, read, execute, copy, or modify `eval/**` or
  `eval/holdout/**`;
- the Phase 11A and Phase 11B task worktrees and uncommitted changes are read-only;
- no other agent may edit an owned file until the current writer commits and hands off.

If implementation needs another writable path, stop, amend this contract first, explain
the need to the user, and obtain approval before editing that path.

## Required workflow contract test

`tests/test_ci_workflow_contract.py` must use only the standard library and inspect the
workflow as data/text without executing GitHub Actions. It must fail if any of these
contracts drift:

- triggers differ from pull requests plus `master`/`main` pushes;
- same-PR cancellation or protected-branch non-cancellation is absent;
- `scripts/verify.py` appears other than exactly once;
- the PR or protected-branch compatibility entries differ from the frozen sets;
- Windows 3.11 or Linux 3.10-to-3.13 protected-branch coverage disappears;
- compatibility invokes Ruff, mypy, coverage, or entry-point smoke;
- lockfile installation/import commands disappear;
- CLI filtered-context build/start smoke disappears;
- a standalone service-image build remains in addition to the Compose harness;
- Postgres migration/load/integration commands disappear;
- Compose fake-run behavior disappears;
- any unit-suite job loses full history;
- pip cache inputs or job timeouts are missing;
- `--eval-assets`, real provider/publisher commands, repository secrets, or a
  self-hosted runner is introduced.

Do not add PyYAML, actionlint, Node packages, or another dependency solely to test the
workflow. If reliable standard-library inspection becomes impossible, stop and request
permission rather than silently weakening the test.

## Offline validation

During implementation run the focused contract test first, then all required offline
validation with the project virtual environment:

```powershell
$Python = (Resolve-Path '..\..\.venv\Scripts\python.exe').Path
$env:PYTHONPATH = (Resolve-Path 'src').Path
& $Python -m unittest -v tests.test_ci_workflow_contract
& $Python -m unittest discover -s tests
& $Python -m ruff check .
& $Python -m mypy src/code_review_agent
& $Python scripts/verify.py
& $Python -m pip check
git diff --check
git diff --name-only 567bd3cf9fe97774ce2177275d325c7d30ff1631...HEAD
```

The full discovery and verifier are permitted because they use repository fakes and do
not enable `--eval-assets`. No eval-specific, real-model, real-GitHub, paid, deployment,
or canary command may be run.

For the contract-only commit, validation is limited to complete diff review,
Single Writer verification, Markdown/whitespace checks, and leak scanning because no
executable behavior has changed.

## Acceptance criteria

- The implementation diff is limited to the four Single Writer paths.
- One workflow run contains exactly one complete quality gate.
- Pull requests allocate two compatibility runners; protected-branch pushes allocate
  four, with Python 3.13 covered by `quality`.
- All five previously supported OS/Python combinations remain represented on protected
  branches.
- A force-push to a PR cancels only the older run for that PR.
- Master/main runs are never canceled by concurrency policy.
- The service image is built through one path; CLI image smoke remains present.
- Lockfile, Postgres, load, container, and Compose evidence remains mandatory.
- No job uses `self-hosted`, a larger runner, external credentials, or real services.
- Focused and repository-wide offline validation passes.
- Actual GitHub Actions results are reported honestly, including every skipped, canceled,
  failed, and successful job; local inspection is not called CI success.
- Savings are reported only from pre/post billable job-duration evidence.

## Rollout, rollback, and evidence

Implementation is local-only until separately authorized. Before any push:

1. review the full diff from the frozen baseline;
2. record the implementation commit SHA and clean worktree status;
3. if Phase 11A has merged, rebase onto its actual merge SHA and rerun all validation;
4. if Phase 11A has not merged, the owner decides whether to cherry-pick the commit into
   PR #17 or wait for post-merge integration;
5. never push or rewrite the Phase 11A branch without explicit authorization.

An authorized CI trial must first run on the task PR. Record workflow run URL/ID, event,
commit SHA, job list, conclusion, wall duration, and billable duration for every job.
Only after the optimized task PR is green may the owner decide whether to integrate it.
The owner alone merges to `master`; auto-merge and direct agent pushes/merges to
`master` are prohibited.

Rollback is the single workflow implementation commit (plus its README/test changes),
not selective deletion of failed gates. A rollback must restore the previous workflow
without altering application code or historical CI evidence.

## Delivery report

Every handoff or final response must include:

- branch name and full commit SHA;
- changed files with one-line purpose;
- commands run and exact results;
- focused review of trigger, concurrency, matrix, cache, timeout, history, container,
  Postgres, and lockfile behavior;
- actual versus projected Actions usage, clearly distinguished;
- skipped/unavailable checks and reasons;
- remaining risks and the Phase 11A integration state;
- explicit confirmation that no eval asset, real model, real GitHub publisher, paid API,
  repository visibility, self-hosted runner, push, PR, or master mutation occurred.
