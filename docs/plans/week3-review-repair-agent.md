# Week 3: Review + Repair Agent

## Goal

Upgrade the existing read-only Review Agent with an opt-in repair workflow that
can plan, patch, test, reflect, recover after interruption, and create a local
task-branch commit only after explicit human approval. Every repair task uses a
fresh Git worktree, and every subprocess command runs through Docker or a
restricted sandbox. Existing review-only behavior remains compatible.

The core workflow is:

```text
DISCOVER -> PLAN -> PATCH -> TEST -> REFLECT
                         ^          |
                         |-- failure|

REFLECT -> WAIT_APPROVAL -> SUBMIT
```

`PLAN -> PATCH` is conditional on a write approval. A failed `REFLECT -> PATCH`
retry must emit a revised plan and obtain a new write approval. `WAIT_APPROVAL
-> SUBMIT` is conditional on a separate commit approval.

## Base and delivery

- Base branch: `master`
- Base commit: `d6ef2ef5e934d143e44d8b9e0e171912e484c055`
- Codex branch: `codex/week3-review-repair-agent`
- Planned integration branch: `integration/week3-review-repair-agent`
- Phase 0 scope: this task contract only; no runtime, test, CI, packaging, or
  user-documentation changes.

No task step authorizes a merge, rebase, or push to `master`. No step
authorizes publishing a branch, pull request, review, or issue comment.

## Required state model

The durable states are:

- `DISCOVER`: validate repository identity and base commit; create or verify the
  task worktree; collect the issue, repository policy, status, diff, and test
  commands; initialize budgets and the checkpoint journal.
- `PLAN`: produce a human-readable and machine-readable plan containing the
  intended files, changes, tests, risk, rollback boundary, and estimated
  budget. The state pauses here until a human grants a write approval bound to
  this exact plan and repository snapshot.
- `PATCH`: apply only the approved patch to approved paths. Save checkpoints
  immediately before and after every mutation.
- `TEST`: run only contract-approved test commands through the sandbox runner;
  persist command, exit code, duration, and bounded output.
- `REFLECT`: classify the test result. Success proceeds to `WAIT_APPROVAL`.
  A recoverable failure may produce a revised plan and return to `PATCH` only
  after a fresh write approval and while the repair-attempt budget remains.
- `WAIT_APPROVAL`: show the final status, complete diff, test evidence, budget
  ledger, and remaining risks. Pause until a human grants commit approval bound
  to the current diff hash and checkpoint.
- `SUBMIT`: revalidate status, diff hash, test evidence, budgets, and approval;
  then create one local commit on the task branch. Push and PR creation are out
  of scope.

Operational terminal states `FAILED` and `CANCELLED` may be added without
changing the core workflow. An unsafe condition, exhausted budget, invalid
checkpoint, rejected approval, or exhausted repair attempts must fail closed.
Failure may roll back the task worktree or quarantine it for inspection, but
must never mutate the original checkout.

Every transition is checked in code. Illegal transitions raise a typed error
and emit an audit event rather than being coerced to the nearest valid state.

## Human approval contract

There are two independent approval kinds:

1. **Write approval** is required before the initial patch and before every
   self-repair patch. It is bound to run ID, checkpoint ID, base SHA, current
   diff hash, plan hash, writable paths, and one patch attempt.
2. **Commit approval** is required immediately before `SUBMIT`. It is bound to
   run ID, checkpoint ID, base SHA, final diff hash, test-result hash, and the
   proposed commit message.

Approvals are one-use, expiring records delivered through a control path the
model cannot invoke. The local CLI may accept approval only from a real TTY or
an injected human-approval provider. There will be no `--yes`, `--force`,
environment-variable bypass, implicit approval, or model-callable approval
tool. A stale, replayed, mismatched, or already consumed approval is invalid.

Before requesting write approval, the exact candidate must pass parsing, path
scope validation, and a read-only sandboxed `git apply --check` against the
bound repository snapshot. A rejected candidate consumes one of at most two
candidate retries in the current PATCH cycle and must never prompt the human.
Candidate-format or preflight retries are distinct from the two self-repair
attempts reserved for failures observed after TEST. Both remain subject to the
same token, cost, time, and tool-call ceilings. After approval, the same snapshot
and preflight are revalidated immediately before mutation to close the
confirmation-time race.

If a commit command fails, the approval is considered consumed, a bounded error
record is persisted, and the workflow returns to `WAIT_APPROVAL`; retrying
requires a new commit approval. The sandboxed commit command uses the fixed,
non-secret local identity `code-review-agent <code-review-agent@localhost>` and
never inherits host Git identity or credentials.

## Worktree isolation

- Each repair run creates a unique `repair/<issue>-<run-id>` branch and sibling
  worktree from an exact base commit.
- Mutable commands use the task worktree as their only working directory. The
  original checkout is never the cwd of a mutable command.
- Before task creation, record the original checkout branch, HEAD, staged,
  tracked, and untracked status. Recheck and compare them at every terminal
  state.
- Resolve every requested path and require it to remain under the task
  worktree. Reject `.git`, symlink escapes, device paths, and paths outside the
  plan's writable set.
- A failed task defaults to rollback of run-owned mutations while retaining
  its external checkpoint/audit record. If rollback cannot be proved safe, the
  worktree is quarantined instead of cleaned with a broad Git command.
- Do not use unscoped `git clean`, `git reset --hard`, or deletion based on
  model-generated path strings.

## Sandbox command contract

All subprocess creation in the repair workflow goes through one
`SandboxRunner` interface. Direct `subprocess.run`, shell strings, and fallback
to unrestricted host execution are prohibited.

The production backend is Docker. Another backend is acceptable only when it
can prove equivalent worktree confinement, environment filtering, process
termination, time and output limits, and network policy. If no valid backend
is available, repair mode refuses to start.

The runner must enforce:

- argv arrays with `shell=False`; no command concatenation, redirection,
  command substitution, or arbitrary interpreter snippets;
- an exact executable/argument-template allowlist defined by the task policy;
- non-root execution, a scrubbed environment, and no automatic propagation of
  API keys, GitHub tokens, SSH configuration, or host credentials;
- `--network none` by default. Repository acquisition or dependency setup
  needs a separately approved, network-enabled sandbox phase;
- only the task worktree mounted writable; all other mounts absent or
  read-only;
- per-command timeout, process-tree termination, stdout/stderr caps, and a
  structured audit result;
- fail-closed handling for unavailable Docker, invalid mounts, ambiguous path
  translation, or unsupported platforms.

Checkpoint persistence and other control-plane file operations are not shell
commands, but they must still obey path and secret-handling rules.

## Repair tool contract

The repair model receives only the following new tools in addition to safe
read/search capabilities:

- `git_status`: read-only structured porcelain status for the task worktree.
- `git_diff`: read-only diff for the approved base, index, working tree, or
  approved paths, with a bounded response.
- Oversized approved source files are represented by a line-aligned tail window
  capped at 32 KiB per file and 64 KiB total. The window records the omitted
  line range and next real line number; binary, command-truncated, or
  single-line-unbounded input still fails closed.
- `preflight_patch`: validate unified-diff paths and the current snapshot, run
  sandboxed `git apply --check`, and prove the command left the worktree
  unchanged before any write approval is requested.
- `apply_patch`: revalidate paths, snapshot, and preflight, then apply the patch
  only with valid write approval.
- `run_command`: run one allowlisted argv command in the sandbox. It cannot
  dynamically expand its allowlist.
- `run_tests`: run the task contract's declared test commands and persist a
  structured result for each command.
- `rollback`: restore only paths created or changed by the current run, based
  on the checkpoint manifest. It is a gated write tool and never performs a
  repository-wide destructive reset.

Every invocation consumes one tool-call budget unit. Each underlying command
also receives a unique operation ID. On recovery, a possibly interrupted write
is reconciled using status, diff hash, and the operation manifest; it is never
blindly replayed.

## Checkpoint and recovery contract

Checkpoint data lives outside the target repository under a configurable state
root such as `%LOCALAPPDATA%\code-review-agent\runs\<run-id>` on Windows. It
must not create untracked state in the original checkout or task worktree.
The state root is a trusted control-plane boundary: startup must prove that it
is disjoint from the original checkout and every repair worktree, reject
symlink, junction, or reparse-point aliases, and never mount it into the task
sandbox. Deployments must restrict it to the control-plane account; if those
ownership or access-control assumptions cannot be established, repair mode
fails closed. The ledger checksum detects accidental corruption, not a writer
who already controls this trusted directory.

Use an append-only event journal plus an atomic current-state snapshot. Write a
temporary file, flush it, and atomically replace the snapshot; include a schema
version and checksum. Save before and after each state transition, model call,
tool call, mutation, test command, approval consumption, rollback, and commit.

The durable record includes at least:

- run ID, issue reference, repository identity, original checkout snapshot;
- base SHA, task branch and worktree, current durable state;
- plan, writable paths, plan hash, status summary, and diff hash;
- tool and command ledger with operation IDs and bounded results;
- test results and their aggregate hash;
- elapsed time, token, cost, tool-call, command, and repair-attempt counters;
- approvals, their binding fields, expiry, and consumption status;
- last completed transition and any in-progress operation intent;
- the exact bounded patch output for each completed patch-model call, plus a
  `pending_preflight` candidate binding (patch hash/text, model reservation,
  plan, attempt, and repository snapshot) saved before the first sandboxed
  preflight command.

Resume verifies repository identity, base SHA, worktree branch, filesystem
location, status, diff hash, checkpoint checksum, and budget ledger. A mismatch
must quarantine the run for human review. Persist elapsed wall time rather than
a process-local monotonic deadline so restart cannot reset the total budget.
An interruption after a paid patch response or during its first read-only
preflight resumes from that exact candidate without replaying the model call.
Recovery reruns the read-only preflight and requires a fresh approval checkpoint
and nonce before any mutation. Interrupted preflight commands verify that the
repository snapshot is unchanged even when termination is delivered as a
`KeyboardInterrupt`; unverifiable or changed state is quarantined.
Never store prompts containing secrets, credentials, raw environment dumps, or
unbounded command output.

## Default budgets

Defaults for the Week 3 small-issue cohort are hard per-task limits:

| Budget | Default |
| --- | ---: |
| Total elapsed time | 1,800 seconds |
| Combined LLM tokens | 80,000 |
| Estimated LLM cost | USD 1.00 |
| Total tool calls | 100 |
| Self-repair attempts | 2 |
| Candidate patch retries per PATCH cycle | 2 |
| One command | 300 seconds |
| One command's combined output | 1 MiB |

The ten-issue pilot has an aggregate cost ceiling of USD 10.00. A task may use
stricter limits. Raising any default or pilot aggregate limit requires explicit
human approval.

Before an LLM request starts, the budget manager atomically reserves its
maximum allowed tokens and estimated cost so parallel calls cannot overbook.
After the response it reconciles the reservation with actual usage. If pricing
is not configured well enough to enforce the cost ceiling, paid calls fail
closed. No new model or tool call begins when its relevant budget is exhausted.
The reviewed task contract binds the provider, exact model ID, and a non-empty
pricing revision ID. Input and output prices must be positive and finite. Each
request sets an output-token limit and reserves a conservative worst-case cost
from both the per-task ledger and a durable cross-process cohort ledger before
the provider is called. The ten-issue cohort ledger uses exact currency
arithmetic and a USD 10.00 ceiling; an actual cost above its reservation is
retained for audit and permanently freezes further paid calls.

Reflection decisions remain strict enum strings. A non-empty structured
`reason` returned by an OpenAI-compatible provider is normalized to
deterministic JSON text, so harmless provider formatting variance cannot strand
a run after its tests have already executed. A single JSON Markdown fence and
literal control characters inside JSON strings are normalized at the provider
boundary. Schema failures after a metered response reconcile actual usage and
are recorded as protocol errors rather than unresolved calls.
Before patch parsing, provider-only transport normalization adds a final newline,
prefixes otherwise invalid bare hunk lines as additions, and recomputes hunk
counts. The resulting candidate still passes the strict parser, path allowlist,
`git apply --check`, snapshot binding, and a fresh human WRITE approval before
any mutation.

## Compatibility and frozen interfaces

- Keep the public signatures and behavior of `run_review`, `verify_findings`,
  and existing read-only review tools.
- Keep all existing CLI flags and meanings. Repair and resume behavior must be
  opt-in and additive; review-only invocations retain their output and failure
  semantics.
- Keep existing review JSON keys, trace fields, finder/verifier prompts,
  schemas, sentinel rules, finding merge semantics, and 300-second review
  deadline behavior.
- Do not change project dependencies, entry points, package layout, lockfiles,
  requirements files, or evaluation assets.
- No external LLM calls, paid eval runs, prompt experiments, or holdout reads
  during implementation and ordinary validation.
- `.github/workflows/ci.yml` remains read-only unless the user separately
  authorizes the proposed Docker sandbox integration job.

## File ownership

Phase 0 has one Codex-owned path:

- `docs/plans/week3-review-repair-agent.md`

After the user approves this contract and starts implementation, the proposed
Codex ownership is:

- `docs/plans/week3-review-repair-agent.md`
- `src/code_review_agent/agent.py`
- new `src/code_review_agent/repair.py`
- new `src/code_review_agent/repair_state.py`
- new `src/code_review_agent/repair_budget.py`
- new `src/code_review_agent/repair_checkpoint.py`
- new `src/code_review_agent/repair_approval.py`
- new `src/code_review_agent/sandbox.py`
- new `src/code_review_agent/repair_tools.py`
- new `tests/test_week3_state.py`
- new `tests/test_week3_tools.py`
- new `tests/test_week3_recovery.py`
- new `tests/test_week3_repair.py`
- new `tests/test_week3_docker_e2e.py`
- new `Dockerfile.repair`
- new `AGENDA.md`

All other paths are read-only. Expanding this list, changing CI, or assigning a
second writer requires a contract update and human approval before edits.

## Implementation sequence

1. Implement typed states, transition validation, budget accounting, approval
   records, and atomic checkpoint persistence with pure unit tests.
2. Implement task worktree lifecycle and the fail-closed sandbox runner.
3. Implement `git_status`, `git_diff`, patch preflight/application, test
   execution, and manifest-scoped rollback.
4. Implement the orchestrator, revised-plan retry loop, crash reconciliation,
   and additive CLI start/resume paths.
5. Add fault-injection and end-to-end fake-model tests, then the Docker repair
   runner and sandbox integration checks.
6. Run the real-issue pilot only after offline and Docker validation pass and a
   human approves the issue list and any external repository actions.

## Validation

Focused development commands, using the task worktree's own environment:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_week3_state
.venv\Scripts\python.exe -m unittest tests.test_week3_tools
.venv\Scripts\python.exe -m unittest tests.test_week3_recovery
.venv\Scripts\python.exe -m unittest tests.test_week3_repair
```

Before Codex delivery:

```powershell
.venv\Scripts\python.exe scripts\verify.py
```

This implementation does not modify prompts or eval assets, so Codex does not
run or inspect `eval/holdout`. Docker validation must additionally prove image
build, non-root execution, network denial, mount confinement, command timeout,
and a complete fake-model repair run. If Docker remains unavailable locally,
report that limitation and run the Docker checks only in an explicitly
authorized environment; unit-test mocks cannot satisfy the final sandbox
acceptance criterion by themselves.

Before every handoff or commit, inspect `git status`, `git diff --check`, and
the complete diff against the base. The changed path set must be a subset of
the active ownership list and contain no secrets, generated state, local paths,
or unrelated formatting.

## Automated acceptance criteria

- Every durable state and legal transition is covered, and illegal transitions
  fail without mutation.
- A patch cannot be applied without a matching unconsumed write approval; each
  retry requires another approval.
- A local commit cannot be created without matching unconsumed commit approval.
- No CLI option, environment variable, checkpoint edit, model tool, or resume
  path bypasses either approval gate.
- Each task uses a unique branch/worktree from the recorded base SHA.
- Success, test failure, timeout, budget exhaustion, cancellation, rollback,
  and forced process termination leave the original checkout unchanged.
- Patch paths, rollback paths, cwd, command argv, environment, network, time,
  output, and writable mounts are constrained and auditable.
- Test failure allows no more than two self-repair attempts and preserves the
  failure evidence for each attempt.
- Forced interruption before a patch, after a patch, during a test, and before
  commit resumes without duplicate mutation, reset budget, or reused approval.
- Existing review behavior and the complete offline validation remain green
  with zero external LLM calls.

## Ten-issue pilot acceptance

Select ten genuine, small GitHub issues only after human review. Each issue
must have a stable URL, a reproducible offline test, an expected patch of about
one to three files and no more than roughly 200 lines, and no secrets,
dependency upgrade, generated assets, migration, or broad refactor.

A pilot task counts as completed when it has:

1. issue URL, repository identity, exact base SHA, run ID, unique worktree and
   branch;
2. pre-change plan and bound write-approval evidence;
3. patch, test, reflection, retry, budget, and checkpoint audit records;
4. passing issue-specific tests and repository-required offline tests;
5. final status/diff/test hashes and bound commit-approval evidence;
6. one human-approved local commit SHA;
7. proof that the original checkout is byte-for-byte equivalent in Git status
   and HEAD before and after the run;
8. recorded review findings and remaining risks.

At least two of the ten tasks must include deliberate process termination at
different mutation/test boundaries and successful resume. Separate negative
acceptance runs must demonstrate persistent-test-failure rollback, budget
exhaustion, and refusal to commit without approval; negative runs do not count
toward the ten successful issues.

The pilot does not authorize cloning with host credentials, pushing branches,
opening PRs, posting comments, or closing issues. Those external writes require
separate per-repository human authorization. A local approved commit linked to
the real issue is sufficient for this contract's completion count.

## Delivery and handoff

### Manual Claude Code Phase 1 review

After Codex commits implementation sequence step 1, ownership transfers for a
time-bounded manual Claude Code review. Codex makes no further edits until the
Claude commit is returned. During this review Claude Code may edit only:

- `src/code_review_agent/repair_state.py`
- `src/code_review_agent/repair_budget.py`
- `src/code_review_agent/repair_approval.py`
- `src/code_review_agent/repair_checkpoint.py`
- `tests/test_week3_state.py`
- `tests/test_week3_recovery.py`
- new `docs/reviews/week3-phase1-claude.md`

All other paths are read-only. Claude reviews state-transition correctness,
concurrent and restart-safe budget accounting, approval replay/scope/expiry
guards, checkpoint atomicity and corruption handling, and tests for bypasses or
unsafe recovery. It may fix confirmed defects only within the writable paths;
it must not add runtime integration, CLI behavior, sandbox commands, worktree
management, dependencies, prompts, eval changes, or unrelated refactors.

Claude runs both focused Week 3 suites and `scripts/verify.py` without
`--eval-assets`, reviews its complete diff, writes the review report, and
creates one local commit on `claude/week3-phase1-review`. It must not merge,
push, or modify `master`. The returned handoff includes branch, full commit
SHA, changed files, commands/results, findings fixed or rejected, and remaining
risks.

Codex reports changed files, focused and full validation results, Docker test
results or the exact unavailable-environment limitation, branch and commit,
known risks, and any ownership deviation. If the user elects a manual Claude
Code review phase, follow `AGENTS.md`: create a stable local Codex handoff
commit, provide the exact worktree command and complete prompt, then integrate
Claude's returned commit on `integration/week3-review-repair-agent`. Stop after
validated integration unless the user explicitly requests merge or push.

## Pilot execution record (2026-07-16)

The implementation was frozen before the real-issue cohort with combined
SHA-256 `ba4a7b3ff3db19e9544402b2ede60be5f46b59c5b0a20861afa6b12851fc0d2e`.
The cohort used `glm-5.2` with thinking disabled, the reviewed conservative
pricing revision, `code-review-agent-pilot-click:py312`, and a USD 10 aggregate
ceiling. Raw checkpoints, journals, contracts, and TTY transcripts remain in
the local control-plane trace root and are intentionally not committed because
TTY transcripts contain expiring approval strings.

Ten selected real-issue runs reached `SUBMIT`. Every selected run has two
consumed approvals, an exit-zero contract test, a clean task worktree, a commit
whose sole parent equals the approved base SHA, and an unchanged original
checkout.

| Issue | Run ID | Local commit | Independent Docker recheck |
| --- | --- | --- | --- |
| click#1925 | `formal-click-1925-20260716-01` | `2659352601c06e9452a9674a95658482ad136ff1` | 84 passed |
| click#1138 | `formal-click-1138-20260716-01` | `d2d182803bb159c9758f058ebd6c5d99869bbd11` | 61 passed, 21 skipped |
| click#3298 | `formal-click-3298-20260716-01` | `1bb5cb51f183762eb828faa9961dceb0632c433c` | 552 passed |
| click#2971 | `replacement-click-2971-20260716-02` | `2a8c03509040f2fdf5df7e555cd226de71a5b1ee` | 145 passed |
| click#3277 | `replacement-click-3277-20260716-02` | `13c1ea8542e76ece1092f795d53340ac62ca1757` | 59 passed |
| click#2703 | `replacement-click-2703-20260716-02` | `85478472e314a4144e1dad1622ca1f2bb8472723` | 48 passed |
| click#3487 | `harness-click-3487-20260716-01` | `9a2f4fd3e3dc78e134d81e7af2afbcc24176fdb2` | red baseline; 1 passed after repair |
| click#1929 | `harness2-click-1929-20260716-01` | `d3a0667da0b48ec2cfddfed068dea86817732ae7` | red baseline; 1 passed after repair |
| click#2332 | `interrupt1-click-2332-20260716-01` | `c133b44b7abc258ff88e3a9db6600cc1586182a6` | red baseline; 6 passed after repair |
| click#1969 | `interrupt2-click-1969-20260716-01` | `9252eb648b3e3057e9e30d503e18feef2dd1be02` | red baseline; 1 passed after repair |

The selected runs used 295,550 model tokens and USD 0.472049. Their elapsed
time was p50 160.5 seconds and p95 216.1 seconds, with 9.7 tool calls per run on
average. The complete exploratory cohort, including failed candidates,
methodology corrections, recovery drills, and negative acceptance, cost USD
2.063600. The durable cohort ledger ended with zero outstanding reservations,
well below its USD 10 ceiling.

Two selected tasks include deliberate process termination and successful
resume at different boundaries:

- click#1969 was terminated while waiting for the first write approval. The
  persisted paid patch candidate resumed without a duplicate patch-model call,
  required a fresh write approval, tested once, and submitted successfully.
- click#2332 was terminated while waiting for commit approval. Resume retained
  the same diff and test evidence, did not replay the model or patch, required a
  fresh commit approval, and submitted successfully.

Separate negative runs demonstrated the required fail-closed behavior:

- `formal-click-2790-20260716-01`: persistent test failure reached `FAILED`,
  rolled the task worktree back to its base, and left the original clean.
- `harness-click-3015-20260716-01`: resume after the durable total-time budget
  expired returned `budget_exceeded`, reset no budget, rolled back to base, and
  left the original clean.
- `negative-no-commit-click-2332-20260716-01`: the write was approved and tests
  passed, but the human rejected commit approval. The run reached `CANCELLED`,
  created no commit, kept the original checkout unchanged, and retained the
  isolated task-worktree diff for inspection.

Final validation rebuilt `Dockerfile.repair` successfully and ran two opt-in
real-Docker tests in 53.213 seconds. The live boundary probe confirmed UID
65532, no outbound GitHub access under `--network none`, writes confined to the
task bind mount by a read-only container root, and removal of the named
container after a forced command timeout. The complete fake-model E2E then
reconfirmed worktree isolation, both approval gates, an exit-zero containerized
test, a single local commit, durable checkpoint state, and idempotent completed
resume. Offline validation passed 348 tests with 3 environment skips, the 85%
branch-coverage gate, Ruff, mypy across 21 source files, and both CLI smoke
tests.

Methodology limitation: the first six selected runs validate real GitHub issue
plans, scoped production patches, and relevant repository suites, but their
pre-repair suites were green and therefore are workflow-completion evidence,
not strict issue-resolution proofs. The final four selected runs use independent
test-only harness commits that are demonstrably red on the historical buggy
base and green on the agent commit. Week 4 should treat the latter red-to-green
method as mandatory and should not report the 10-run pilot as pass@1.
