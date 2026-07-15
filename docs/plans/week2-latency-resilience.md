# Week 2: latency resilience and parallel review lanes

## Goal

Run the two finder lanes and the two verifier lanes concurrently while one
review-wide soft deadline prevents new LLM requests from starting after the
latency budget is exhausted. Preserve the existing review, degradation, and
fail-open semantics.

## Base

- Base branch: `master`
- Base commit: `b092184847ceaaf5cefcea761f33411a2238b76d`
- Integration branch: `integration/week2-latency-resilience`

## Frozen interfaces

- Keep the public signatures of `run_review`, `verify_findings`, and `main`.
- Keep all CLI flags and their meanings unchanged.
- Keep the existing review JSON keys and failure/degradation status values.
- Keep finder/verifier prompts, schemas, sentinel rules, and finding merge
  semantics unchanged.
- Keep all existing trace event fields; new timing/deadline event kinds may be
  added without changing existing records.
- The deadline is intentionally soft: no new LLM request starts after expiry,
  and each request is capped by the smaller of the remaining budget and the
  existing provider request timeout. A request already in flight is not
  force-killed by another thread.

## File ownership

Each path has exactly one writer during the manual two-agent phase.

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/week2-latency-resilience.md`; `src/code_review_agent/agent.py`; `src/code_review_agent/agentloop.py`; `src/code_review_agent/orchestration.py`; `src/code_review_agent/tracelog.py`; `src/code_review_agent/verifier.py`; `tests/test_golden.py`; `tests/test_week2_orchestration.py` | all other repository paths |
| Claude Code | `README.md`; `CHANGELOG.md`; `docs/interview-defense.md` | Codex delivery commit and all runtime/tests |
| Integrator | conflict resolution only | all paths |

## Prohibited changes

- No direct commits or merges to `master`.
- No dependency, lockfile, packaging, or CI changes.
- No CLI or existing review-JSON contract changes.
- No prompt, sentinel, or evaluation-fixture changes.
- No external LLM calls or paid evaluation runs.
- No unrelated formatting or generated-file edits.
- No deleting or weakening tests to make validation pass.
- No `.env`, credentials, tokens, or local auth files in commits or handoffs.

## Agent assignments

### Codex

- Objective: implement concurrent finder/verifier lane orchestration, a shared
  review deadline, request-timeout capping, thread-safe JSONL tracing, and
  deterministic offline regression tests.
- Required tests: focused Week 2 tests, full unittest discovery, Ruff, mypy,
  both CLI smoke tests, and eval-asset consistency through
  `scripts/verify.py --eval-assets` without any LLM calls.
- Delivery commit: filled after completion.

### Claude Code

- Objective: review the Codex diff for concurrency, failure-semantics, and
  deadline defects; then document the implemented behavior, measured offline
  evidence, limitations, and interview defense without changing runtime code.
- Required tests: documentation diff self-review; run the full offline verify
  entry point to confirm documentation integration did not disturb the build.
- Delivery commit: filled after completion.

## Validation

Run the focused tests while developing:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_week2_orchestration
.venv\Scripts\python.exe -m unittest tests.test_golden
```

Before the Codex handoff and final integration, run:

```powershell
.venv\Scripts\python.exe scripts\verify.py --eval-assets
```

The verification entry point performs Ruff, coverage-gated unit tests, mypy,
both CLI smoke tests, and frozen eval/holdout consistency. It must make zero
external LLM calls.

## Acceptance criteria

- Finder anchor and sampling lanes overlap in time under the production pair
  runner; verifier A and B lanes overlap likewise.
- The finder anchor remains fatal on failure; finder2 still degrades to anchor
  only; one verifier failure still degrades; two verifier failures still fail
  open; authentication and rate-limit failures remain visible.
- One 300-second deadline begins before context construction and is shared by
  every finder/verifier loop in `run_review`.
- No loop starts another LLM request after deadline expiry. A started request
  receives a timeout no greater than the remaining review budget or the
  existing per-request cap.
- Concurrent trace writes cannot corrupt or interleave JSONL records, and
  stage timing/deadline outcomes are auditable.
- Existing supported behavior, request protocol, and output shape remain
  compatible.
- All assigned tests and repository checks pass with zero LLM API calls.
- Final integration diff contains no unrelated changes.

## Handoff and integration

1. Codex reviews and commits only its owned files.
2. Claude reads `git diff b092184847ceaaf5cefcea761f33411a2238b76d...<codex-delivery>`.
3. Claude edits only its three documentation paths and commits its result.
4. The integrator merges Codex first, runs focused tests, merges Claude, then
   runs the full offline validation entry point.
5. Only the user-authorized integrator may merge the validated integration
   branch into `master` or push it.

## Delivery report

- Summary: pending
- Changed files: pending
- Commit: pending
- Commands run and results: pending
- Known risks or assumptions: pending
- Suggested review focus: deadline boundaries, shared-client thread safety,
  trace serialization, and preservation of degradation semantics
