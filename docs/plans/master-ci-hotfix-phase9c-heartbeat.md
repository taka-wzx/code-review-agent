# Master CI hotfix: Phase 9C heartbeat lifecycle

Status: active

Date: 2026-07-27

Base: `origin/master` at `3b62a040e2268a48c9a572bede7aaef6303db492`

Branch: `codex/master-ci-hotfix-phase9c-heartbeat`

## Purpose and evidence boundary

GitHub Actions run `30235540089` failed only on Windows Python 3.11 in
`Phase9CDurableServiceTests.test_worker_heartbeats_are_not_delayed_by_poll_interval`.
The job remained `running` after its fake runner was released, and teardown could not
remove the SQLite database because a worker-owned thread still had it open. This
hotfix is limited to making worker lease-loss/shutdown ownership deterministic and
making the regression assert the scheduler contract without a one-second CI timing
assumption.

The exact trigger for the delayed heartbeat in that single CI execution is not
proven and remains unknown. The fix must not be represented as a production capacity,
availability, Business Pilot, model-quality, or online Review-to-Repair result.

## Frozen behavior

- The worker keeps an execution thread counted against local concurrency after its
  durable lease is lost, until that thread reports completion.
- A lease-lost execution is not heartbeated again and cannot commit with its stale
  fencing token.
- Shutdown continues to wait, within the existing grace deadline, for that execution
  thread to finish.
- Heartbeat cadence remains independent of the queue poll interval.
- No public API, state-machine state, retry policy, database schema, dependency,
  workflow, or deployment behavior is added.

## Single Writer files

Codex has write ownership only for:

- `docs/plans/master-ci-hotfix-phase9c-heartbeat.md`;
- `src/code_review_agent/worker.py`;
- `tests/test_phase9c_durable_service.py`.

Every other path is read-only. In particular, `eval/**` and `eval/holdout/**` must
not be read, enumerated, modified, or executed.

## Offline acceptance

- A deterministic regression proves a lease-lost execution remains tracked until
  its thread completes, then is reaped.
- A deterministic scheduler regression proves the next wait is bounded by the
  heartbeat deadline rather than a deliberately longer poll interval, without
  depending on lease expiry.
- The targeted Phase 9C module passes repeatedly on Windows.
- The Phase 9C contract suite and repository-wide offline verification pass.
- Ruff, mypy, pip check, and `git diff --check` pass.
- No real model/API call, paid call, GitHub product write, deployment, or protected
  branch mutation occurs.

Commands:

```powershell
python -m unittest -v tests.test_phase9c_durable_service
python -m unittest discover -s tests
python -m ruff check .
python -m mypy src/code_review_agent
python scripts/verify.py
python -m pip check
git diff --check
```

The general discovery command is permitted only because repository tests are fake
and the project verifier excludes frozen evaluation assets. No eval-specific command
may be added or run.

## Delivery control

The hotfix may be committed and pushed only on its task branch and delivered through
a pull request. It must never be pushed or merged directly to `master`; the repository
owner performs the merge. Phase 10 Prep may begin only from the subsequently synced
`master` after its CI is green.
