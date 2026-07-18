# Week 5: Trusted SWE-bench Repair Evaluation

## Goal

Build an offline-first, auditable evaluation framework for the Repair Agent
against a preregistered 20--50 task subset of SWE-bench Verified. The framework
must prove that each task/configuration attempt uses a fresh Git worktree and
isolated Docker execution, preserve the Week 3 approval and fail-closed safety
model, and report pass@1, resource use, reliability, policy violations, and
deterministic Bootstrap 95% confidence intervals.

Week 5 delivers the evaluation contract, candidate-selection plan, configuration
and resource preregistration, local manifest/run-plan validators, synthetic
fixtures, statistics, tests, and operator documentation. It does not claim that
SWE-bench data has already been downloaded, that a cohort has been materialized,
or that any model/Docker benchmark result exists.

## Base and delivery

- Required base policy: latest `master` after Week 4 integration and CI success.
- Exact base commit:
  `afb6e1fa85701d7b6af5b16198c9dd992740a03d`
- Codex branch:
  `codex/week5-swebench-repair-evaluation`
- Codex worktree:
  `E:\shiyan\code_review_agent\traces\worktrees\codex-week5`
- Planned Claude branch:
  `claude/week5-swebench-repair-evaluation-review`
- Planned integration branch:
  `integration/week5-swebench-repair-evaluation`

No task step authorizes direct mutation of `master`. Codex must stop on a
validated integration branch and request user approval before merging into
`master`. A final push happens only after the approved master integration and
must be followed through GitHub CI to a terminal state.

## Authorization boundary

Authorized local work:

- create and validate this contract, candidate plan, configuration plan,
  schemas, synthetic examples, offline statistics, run-plan generator, tests,
  and documentation;
- inspect existing Week 3 Repair and Week 4 evaluation code as read-only input;
- run focused and full offline validation without `--eval-assets`;
- create local Codex, Claude-review, and integration branches/worktrees and
  local handoff/integration commits;
- provide a complete manual Claude Code review handoff.

Not authorized without a later, explicit user approval:

- network access, downloading SWE-bench, cloning task repositories, pulling
  task images, or installing benchmark dependencies;
- external model calls, paid evaluation, or model-based judging;
- starting a real task Docker container or a large-scale Docker evaluation;
- opening materialized development, tuning, or reporting task contents;
- reading, listing, searching, hashing, or validating the existing `eval/` or
  `eval/holdout/` assets;
- changing prompts, sentinels, Review/Repair runtime behavior, public APIs,
  dependencies, lockfiles, packaging, CI, or external repositories;
- pushing a task/integration branch or merging/pushing `master` before the
  required approval point.

The future acquisition, smoke, development, tuning, sealed reporting, and
publication phases are separate authorization gates. Approval of one phase
does not authorize the next.

## File ownership

Codex may create or modify only:

- `docs/plans/week5-swebench-repair-evaluation.md`
- `docs/swebench-repair-evaluation.md`
- `swebench_repair_eval.py`
- `swebench_repair_runner.py`
- `swebench_repair/cohort-plan.json`
- `swebench_repair/config-plan.json`
- `swebench_repair/schemas/cohort.schema.json`
- `swebench_repair/schemas/run-plan.schema.json`
- `swebench_repair/schemas/runs.schema.json`
- `swebench_repair/examples/synthetic-cohort.json`
- `swebench_repair/examples/synthetic-run-plan.json`
- `swebench_repair/examples/synthetic-runs.jsonl`
- `tests/test_swebench_repair_eval.py`
- `tests/test_swebench_repair_runner.py`
- `README.md`
- `AGENDA.md`

During manual Claude review, Claude owns only:

- `docs/reviews/week5-claude.md`

Claude treats all implementation, plan, test, schema, and documentation paths
as read-only and reports proposed fixes. Codex applies accepted fixes during
integration only within the Codex-owned list. All other paths remain read-only.
Ownership expansion requires a contract amendment and explicit user approval
before the first out-of-list edit.

## Candidate cohort preregistration

### Source and current materialization state

The sole allowed source is the official SWE-bench Verified dataset identified
at acquisition time by:

- canonical dataset name `princeton-nlp/SWE-bench_Verified`;
- immutable dataset revision/commit;
- exact raw-manifest SHA-256;
- acquisition command/version and UTC timestamp;
- official harness revision and image digests.

The current plan is deliberately `materialized: false` and contains no task
IDs, repository snapshots, issue text, gold patches, or test specifications.
The absence is required by the user's no-download boundary. Task IDs must not
be invented from memory. A later acquisition commit must replace the placeholder
revision with an immutable revision and bind the byte hash of the complete
candidate-selection log before any model run. It must also freeze the raw
manifest task count. The exact-byte selection log must contain exactly that
many rows, so changing its hash cannot conceal a deleted candidate row.

### Target size and split

The target is 30 Verified tasks, within the required 20--50 range:

| Role | Target | Allowed use |
| --- | ---: | --- |
| development | 5 | harness smoke, schema debugging, prompt/code development |
| tuning | 5 | one preregistered configuration decision checkpoint |
| reporting | 20 | sealed headline and ablation reporting only |

Repository identity is the split unit. A repository may occur in exactly one
role. The 20-task reporting set must contain at least four repositories and at
least three tasks per reporting repository. Development, tuning, and reporting
repositories are pairwise disjoint.

Repositories or tasks previously used in this project are ineligible,
including the Week 3 pilot and Week 4 preregistration repositories:

- `pallets/click`
- `pallets/flask`
- `psf/requests`
- `encode/httpx`

Any additional task manually inspected, used in a prompt/example/test, or
opened before split freeze is ineligible for reporting and must be recorded in
the exclusion log.

### Eligibility

Eligibility is decided without Agent output and before split assignment. A task
must:

- be present in the frozen SWE-bench Verified manifest;
- expose immutable repository/base/patch/test identities and official
  `FAIL_TO_PASS` plus `PASS_TO_PASS` specifications;
- have an available content-addressed task image or a reproducible image build;
- run without network, host credentials, GPU, privileged mode, or host Docker
  socket;
- complete baseline and gold-patch harness checks within the preregistered CPU,
  memory, disk, process, and time ceilings;
- reproduce the required red baseline and gold green result;
- contain no submodule/LFS dependency that cannot be frozen offline;
- permit a worktree-based checkout and a repository-relative writable scope;
- avoid security embargoes, secrets, non-redistributable artifacts, flaky
  official tests, or infrastructure failures during two acquisition-only
  reproducibility checks.

The two reproducibility checks are not Agent runs and require separate approval
because they start Docker. They may determine objective eligibility but must
not expose gold patches or reporting task content to the implementer.

### Outcome-blind deterministic selection

The cohort seed is:

`SHA256(b"swebench-repair-cohort-v1" + b"\x00" + ASCII(base_commit))`

For this base it is:

`39a89ee8c3368d08f2444ce84c5c86294bef36b2164397f514b50b01be963ce0`

The acquisition controller computes `patch_changed_lines` from the frozen
official gold patch as the number of added plus deleted content lines, excluding
diff headers. The immutable method ID is `gold_patch_changed_lines_v1`:

- `small`: 1--5 changed lines;
- `medium`: 6--20 changed lines;
- `large`: 21 or more changed lines.

The controller may derive this sealed metadata, but neither the developer nor
the Agent receives the gold patch. The validator recomputes every `size_band`
from `patch_changed_lines`; a free-form band cannot influence repository
allocation.

After objective eligibility screening:

1. canonicalize every instance ID and repository identity;
2. group by repository;
3. rank repositories by
   `SHA256(seed + "\nrepo\n" + canonical_repository)`;
4. assign whole repositories to reporting, tuning, then development until all
   role constraints can be satisfied;
5. rank tasks within a repository by
   `SHA256(seed + "\ntask\n" + canonical_instance_id)`;
6. take tasks in rank order while satisfying only preregistered repository and
   fixed size-band quotas;
7. record every manifest task, eligibility decision, exclusion reason,
   changed-line count, derived band, rank, selected flag, and selected role in
   an exact-byte JSONL selection log.

Only selected rows carry a role. Every unselected row, including ineligible
rows inside an assigned repository and rank-six-or-later eligible rows, carries
`role: null`. The log row count must equal the frozen raw-manifest task count.

No selection or exclusion may depend on Agent success, test outcome after an
Agent patch, token/cost use, model familiarity, issue difficulty inferred from
model output, or desired metric values. If the constraints cannot be met, the
materialization fails closed and requires a public contract amendment before
any model output is viewed.

## Leakage and tuning-pollution controls

- Development, tuning, and reporting are repository-disjoint.
- Reporting task IDs, issue text, source snapshots, gold patches, test specs,
  logs, and per-task outcomes remain sealed until the primary configuration,
  all ablations, exact model IDs, pricing, seeds, and resource limits are
  committed in a pre-run freeze attestation.
- Gold patches are never supplied to the Agent or context retriever. Official
  evaluation runs in a separate judge container after the Agent patch freezes.
- Development outputs may change code/prompts only on a development branch.
- Tuning data may be opened once at a preregistered checkpoint and may select
  only among frozen alternatives. Any subsequent change invalidates tuning
  results and requires a new version before reporting is opened.
- Reporting results may not select a prompt, threshold, model, context policy,
  retry rule, sentinel, resource limit, or configuration. All preregistered
  configurations are reported, including failures.
- No failed, timed-out, expensive, or policy-violating headline run is silently
  retried or replaced. Infrastructure reruns require a versioned protocol
  amendment made before results are inspected.
- The existing `eval/` and `eval/holdout/` are forbidden inputs and comparison
  sources for Week 5.
- Task source commit, dataset revision/hash, image digest, harness revision,
  configuration hash, Agent source commit, and all input/result byte hashes are
  carried into every report.

## Primary configuration and ablations

The reporting matrix uses one frozen primary configuration and five
single-factor ablations:

| ID | Finder | Context | Verifier | Repair Reflection | Model |
| --- | --- | --- | --- | --- | --- |
| `primary` | dual | on | on | on | model A |
| `single_finder` | single | on | on | on | model A |
| `no_context` | dual | off | on | on | model A |
| `no_verifier` | dual | on | off | on | model A |
| `no_reflection` | dual | on | on | off | model A |
| `model_b` | dual | on | on | on | model B |

This one-factor-at-a-time design avoids an unbudgeted 32-cell full factorial.
Model A/B are placeholders until a separately approved paid phase binds exact
provider snapshot IDs and pricing revisions. Results support mechanism
comparison only; reporting outcomes cannot be used to choose a winner.

Each of the six configurations runs exactly once on each of the 20 sealed
reporting tasks: 120 task/config attempts. `primary` supplies headline pass@1.
Ablation deltas are paired by task and include paired Bootstrap confidence
intervals. Development/tuning executions require their own run matrix and do
not enter reporting metrics.

## Resource budget

### Per task/config launch and reservation limits

| Resource | Limit |
| --- | ---: |
| Total elapsed wall time | 3,600 seconds |
| Combined LLM tokens | 120,000 |
| Estimated LLM cost | USD 0.50 |
| Tool calls | 150 |
| Repair attempts after the initial patch | 2 (`0` for `no_reflection`) |
| Test command invocations | 10 |
| One command | 600 seconds |
| One command output | 1 MiB |
| Docker CPUs | 2 |
| Docker memory | 4 GiB |
| Docker pids | 256 |
| Writable task volume | 20 GiB |
| Network | none |

The current Week 3 runtime ceiling of USD 1 per task and USD 10 per cohort is
not relaxed. Reporting uses one durable USD 10 cohort ledger per configuration;
with 20 tasks at USD 0.50 each the six-configuration reporting ceiling is
USD 60. Development and tuning together receive at most USD 20, so the full
Week 5 paid ceiling is USD 80. This is a hard maximum, not an estimate or an
authorization to spend.

No paid call starts unless the user approves exact models, pricing revisions,
run counts, and aggregate ceiling. The ledger reserves worst-case cost before
each request. Unknown pricing, an unbound model alias, a missing usage record,
or an exhausted per-task/config/global ledger fails closed.

The limits prohibit starting new work; they are not instructions to clamp
observed telemetry. A `budget_exhausted` record may truthfully carry up to 5%
post-response settlement tail for tokens/cost and one already-in-flight
tool/test operation. A `timeout` or `budget_exhausted` record may carry up to
5 seconds of process-termination latency and up to 1 second of command-kill
latency. These bounded tails authorize no additional request or operation.
Every exceeded dimension is emitted per task. Command output is truncated at
the frozen byte cap and repair-attempt count never receives a grace allowance;
values beyond those caps remain invalid evidence.

At most two task/config runs may execute concurrently. The full reporting
matrix has a 120 container-hour hard ceiling and must stop on any cohort-wide
integrity failure. Docker image acquisition/build time is accounted separately
and cannot be hidden inside Agent latency.

## Per-attempt isolation and evidence

Every `(instance_id, configuration_id)` attempt receives:

- a unique `run_id`, `repair/<slug-at-most-32>-<identity-prefix-12>` branch,
  and fresh worktree from the exact task base SHA;
- a unique container namespace and content-addressed image digest;
- task worktree as the only writable bind mount;
- read-only container root, non-root UID/GID, all capabilities dropped,
  `no-new-privileges`, pids/CPU/memory limits, scrubbed environment, and
  `--network none`;
- no host Git credentials, SSH files, API keys, Docker socket, control-plane
  state, other worktrees, or gold/evaluator artifacts mounted into the Agent
  container;
- a disjoint state/checkpoint directory outside every repository/worktree;
- pre/post original-checkout snapshots and cleanup/quarantine evidence.

Worktree paths are not committed in portable artifacts. Reports carry a
domain-separated deterministic path token derived without a host path, plus
task branch, base SHA, worktree creation evidence, and container/image
identities. The token proves the planned identity, not the observed absolute
path. Duplicate worktree IDs, branch names, container names, run IDs,
task/config pairs, or evidence hashes fail closed.

The official evaluator runs after the Agent patch is frozen, in a different
judge container with the gold/test specifications mounted read-only. The Agent
container never sees gold patches or hidden tests. The evaluator records
`FAIL_TO_PASS` and `PASS_TO_PASS` details, exit code, bounded logs, and exact
input/output hashes.

## Run lifecycle and pass@1

For each task/config:

1. validate the frozen cohort/configuration and pre-run attestation;
2. create the unique worktree and verify the base tree hash;
3. start the Repair workflow under the task Docker policy;
4. route writes through the Week 3 preflight and approval boundary;
5. freeze the first submitted patch (or explicit no-patch terminal result);
6. run the official evaluator exactly once in the isolated judge container;
7. collect trace/checkpoint/budget/policy/isolation evidence, including actual
   repair-attempt and per-command maxima plus Agent/judge container intervals;
8. prove original checkout unchanged and container cleanup or quarantine;
9. atomically append one immutable run record.

`pass@1` is:

`official_resolved_tasks / all_attempted_reporting_tasks`

for the primary configuration. A task is resolved only when the official
SWE-bench evaluator marks both `FAIL_TO_PASS` and `PASS_TO_PASS` requirements
successful for that first frozen patch. No-patch, model error, approval
rejection, timeout, budget exhaustion, sandbox failure, cleanup uncertainty,
test failure, malformed evidence, or unauthorized operation remains in the
denominator and counts unresolved.

The benchmark control plane may provide approvals only through a separately
reviewed, non-model-callable provider bound to the exact preflight candidate.
It cannot auto-approve path-scope violations or bypass Week 3 bindings.
Reporting never creates or pushes task commits to external repositories.

A rejected policy operation increments `unauthorized_operations`, remains
unexecuted, and may coexist with the run's truthful later terminal status.
Such a run is always unresolved, even if the evaluator passes. `tool_calls`
counts accepted/executed tool operations and includes accepted test commands;
therefore `operations_total` is exactly
`tool_calls + unauthorized_operations`, not an adapter-chosen denominator.

## Metrics

### Headline and per-task outputs

Report:

- primary `pass@1` with integer numerator/denominator;
- resolved count for every ablation and paired delta from primary;
- per-task result for every configuration;
- per-task cost in integer micro-USD, plus total/mean/median/p95 and cost per
  resolved task;
- end-to-end Agent latency p50/p95/mean/max, excluding acquisition/image build
  but including worktree, model, tools, tests, and cleanup;
- mean/p50/p95 tool calls and optional tool-kind breakdown;
- observed repair-attempt total/mean/max and maximum single-command
  time/output;
- Agent test-command failure rate:
  failed/timed-out/truncated test invocations divided by all test invocations;
- task test-failure rate:
  attempts with at least one failed Agent test divided by attempted tasks;
- unauthorized-operation task rate:
  runs with at least one rejected policy operation divided by attempted tasks;
- unauthorized-operation event rate:
  rejected policy events divided by all attempted tool/command operations;
- terminal state/status counts, hard-failure, timeout, budget-exhaustion,
  cleanup/quarantine, approval-rejection, and scorable-evidence rates.

Every rate includes raw counts and an explicit denominator. Undefined
zero-denominator values are JSON `null`, never zero. Costs are integer
micro-USD. The validator rejects non-finite/negative values and its enumerated
cross-field contradictions: test failures exceeding tests, a `test_failure`
status without a failed test, denominator mismatch, unauthorized success,
budget-tail/status mismatch, isolation/timestamp mismatch, and official
evaluator contradictions. Other counters remain adapter-supplied evidence and
are not claimed to be independently trace-derived.

### Bootstrap 95% confidence intervals

Final reports use at least 10,000 deterministic percentile-bootstrap
replicates. The sampling unit is task, stratified by repository, preserving
within-task clustering across all telemetry and paired configurations. Method
`repository_stratified_task_sha256_v2` uses SHA-256 rejection sampling over
seed, replicate, repository, draw, and retry counter, so results do not depend
on Python's `random.choice` implementation.

Confidence intervals are reported for:

- pass@1 and each ablation's resolved rate;
- paired pass@1 delta versus primary;
- mean cost, p50/p95 latency, and mean tool calls;
- task test-failure and unauthorized-operation rates.

The recorded seed, replicate count, percentile implementation, defined
resample count, and omission reason are part of the report. Input ordering must
not affect output. Fewer than two contributing tasks or a metric undefined in
every resample yields `null` with a reason.

## Data artifacts and strict validation

The offline framework consumes:

1. a cohort plan/materialized manifest;
2. a configuration/resource plan;
3. a generated run plan with one row per task/config;
4. one immutable JSONL run record per attempted task/config.

All carry `schema_version`, canonical identifiers, UTC timestamps, SHA-256
bindings, and strict finite-number/type checks. The Python validator is
normative; JSON Schemas are interoperability references for row shape.

Validation fails closed on:

- unmaterialized data supplied to reporting;
- total selected tasks outside 20--50 or role counts other than 5/5/20;
- repository overlap across roles or fewer than four reporting repositories;
- duplicate task/run/worktree/container/task-config identities;
- selection seed/rank/log/hash mismatch;
- dataset, harness, image, source, config, or freeze hash mismatch;
- reporting run purpose other than `final_report` or `ablation_report`;
- missing primary/ablation run, more than one run, or silent replacement;
- reused worktree/container, writable extra mount, network enabled, root user,
  missing cleanup evidence, or original checkout changed;
- more than two overlapping task/config run intervals or Agent-plus-judge
  container time above the reporting ceiling;
- task start before the external freeze attestation;
- official evaluator evidence missing/inconsistent with `resolved`;
- negative/non-finite/inconsistent cost, timing, tool, test, or policy counts.

Trace, checkpoint, evaluator-input, and evaluator-output artifacts must use a
canonical envelope containing the exact `run_id` before hashing. Global
evidence-hash uniqueness then detects reuse without rejecting two legitimate
runs whose inner patch or evaluator payload happens to be byte-identical.

## Offline implementation requirements

`swebench_repair_runner.py` is a standard-library, repository-only planner. It
must:

- strictly validate the unmaterialized/materialized cohort and config plan;
- recompute cohort seed, deterministic selection ranks, and selected sets;
- generate a deterministic 120-row reporting run plan;
- derive collision-resistant run/worktree/container IDs without exposing host
  absolute paths;
- emit task-isolation requirements and pre-run hashes;
- make no network, Docker, Git, subprocess, model, dataset, or eval-asset call.

`swebench_repair_eval.py` is a standard-library, repository-only validator and
reporter. It must:

- strictly validate cohort/config/run-plan/run JSONL and cross-file hashes;
- verify exact one-run coverage and isolation/evaluator evidence;
- compute pass@1, ablation paired deltas, per-task and aggregate resource,
  test-failure, unauthorized-operation, and terminal-state statistics;
- compute deterministic repository-stratified task Bootstrap intervals;
- emit report input hashes, metric version, seeds, and explicit denominators;
- make no network, Docker, Git, subprocess, model, dataset, or eval-asset call.

No implementation code starts a real benchmark. A later authorized operator
must translate each frozen run-plan row into the existing reviewed Repair
control plane and official SWE-bench harness.

## Validation

Focused development:

```powershell
$env:PYTHONPATH = "<week5-worktree>\src"
<python> -m unittest tests.test_swebench_repair_runner -v
<python> -m unittest tests.test_swebench_repair_eval -v
<python> -m ruff check swebench_repair_runner.py swebench_repair_eval.py `
  tests\test_swebench_repair_runner.py tests\test_swebench_repair_eval.py
<python> -m mypy swebench_repair_runner.py swebench_repair_eval.py
```

Full offline validation:

```powershell
$env:PYTHONPATH = "<week5-worktree>\src"
<python> scripts\verify.py
```

The implementation must not run:

```text
scripts\verify.py --eval-assets
eval\check_consistency.py
run_eval.py
judge.py
repeat_eval.py
replay_verifier.py
bench_verifier.py
docker build
docker run
git clone
git fetch
```

Before every handoff or commit:

```powershell
git status --short
git diff --check
git diff --stat <base>...HEAD
git diff <base>...HEAD
```

The changed path set must be a subset of the ownership list and contain no
credentials, raw SWE-bench task data, gold patches, generated reports, host
absolute paths, or unrelated formatting.

## Automated acceptance criteria

- The committed plan is unmaterialized, contains no real task contents, and
  validates without network/Docker/model access.
- A synthetic 5-development/5-tuning/20-reporting cohort with repository
  isolation produces exactly 120 reporting run-plan rows.
- Any role repository overlap, forbidden repository, duplicate task, wrong
  role count, or reporting set below four repositories is rejected.
- Selection-log byte hash, seed, repository/task ranks, and selected sets are
  recomputed and fail closed on mismatch.
- Selection-log row count equals the frozen manifest count; changed-line size
  bands and selected-only roles are recomputed.
- Every task/config receives a unique run, worktree, branch, container, state,
  and judge identity with network-none/read-only/non-root evidence.
- Missing/reused isolation evidence or changed original checkout fails closed.
- Official evaluator fields and resolved status cannot contradict.
- Primary pass@1 counts all 20 attempts; failures are never silently dropped.
- Cost, latency, tool, test-failure, unauthorized-operation, state, and failure
  statistics have exact deterministic tests including zero denominators.
- Bounded termination/settlement overruns remain truthful, explicitly marked
  evidence; repair and command caps plus parallel/container ceilings are
  cross-checked.
- Bootstrap is repository-stratified, paired for ablation deltas,
  order-independent, and stable for a fixed seed.
- Reporting refuses development/tuning purposes, duplicate/replacement runs,
  post-result configuration changes, and unbound model/pricing/image/harness
  identities.
- Full offline validation passes without old eval assets or external services.

## Codex, Claude, integration, master, and push workflow

1. Codex writes and self-reviews this contract plus unmaterialized candidate
   and resource plans before implementation.
2. Codex implements only owned paths, runs focused/full offline validation,
   reads the complete diff, and creates a local handoff commit.
3. The user creates a separate Claude worktree from that exact commit. Claude
   performs an independent code/protocol review and writes only
   `docs/reviews/week5-claude.md`, then returns one local Claude commit.
4. Codex creates `integration/week5-swebench-repair-evaluation`, integrates the
   Claude report, applies accepted fixes within Codex ownership, records every
   finding disposition, and reruns all required offline validation.
5. Codex stops on the validated integration branch and asks the user whether
   to merge into local `master`.
6. After explicit approval, fast-forward or otherwise safely integrate into
   local `master`, revalidate, push `master` normally (never force), verify the
   exact remote SHA, and track GitHub CI to a terminal state.

No acquisition, Docker/model run, or result claim is part of this local
implementation workflow.

## Claude review integration dispositions

Claude's independent review is preserved unchanged in
`docs/reviews/week5-claude.md` at commit
`0451d5b93dc08cfe7d84ef01b6891f6966fb2244`. Integration dispositions:

- **F-1 (P1), accepted and fixed:** roles now exist only on selected rows;
  ineligible/unselected rows are null-role audit rows. The selection log must
  exactly match the frozen manifest row count.
- **F-2 (P2), accepted and fixed:** `operations_total` is exactly executed
  `tool_calls` plus rejected `unauthorized_operations`; tests are a subset of
  tool calls.
- **F-3 (P2), accepted and fixed:** rejected operations may coexist with the
  truthful terminal status, but always force unresolved.
- **F-4 (P2), accepted and fixed:** bounded termination/in-flight/settlement
  tails are accepted only with matching terminal states and are reported per
  task instead of clamped.
- **F-5 (P2), accepted and fixed:** run evidence and metrics now include repair
  attempts and maximum command time/output; `no_reflection` is enforceably zero.
- **F-6 (P2), accepted and fixed:** `gold_patch_changed_lines_v1` and frozen
  changed-line inputs make size bands machine-recomputable.
- **F-7 (P3), accepted and fixed:** Agent/judge intervals, run-overlap scanning,
  and aggregate container-time validation make both ceilings auditable.
- **F-8 (P3), accepted as an adapter precondition:** canonical evidence
  envelopes must embed `run_id`, so uniqueness detects reuse without
  forbidding identical inner payloads.
- **F-9 (P3), accepted and narrowed:** test/status, policy/resolution,
  operations, budget, isolation, and evaluator cross-field invariants are
  enforced; documentation no longer claims independent trace derivation for
  every adapter-supplied counter.
- **F-10 (P3), accepted and fixed:** Python/schema repository and task-branch
  patterns match; branch and domain-separated path-token wording match the
  implementation.
- **F-11 (P3), accepted and fixed:** bootstrap draws use explicit SHA-256
  rejection sampling rather than Python's `random.choice`.
- **F-12 (P3), accepted and fixed:** conservative Windows trailing-dot/space
  and short-alias guards plus explicit degenerate-bootstrap failure handling
  are covered by tests.
- **F-13 (P3), accepted:** the listed high-risk paths received focused
  regressions, including materialized selection variants, CLI output guards,
  evidence reuse, setup failure, and exact budget boundaries.

These changes amend the implementation contract before any acquisition,
attestation freeze, execution-adapter work, or real sealed run.

## Codex delivery record

The contract was frozen before implementation in commit
`d12169797eef8327d25f1ed0f5b27bc7a8f21727`. The reviewed Codex implementation
is commit `a72ad9fc79b535276775d0ceeca5fc9b88bc012f`.

Offline validation performed from the Week 5 worktree with
`PYTHONPATH=E:\shiyan\code_review_agent\traces\worktrees\codex-week5\src`:

- `python -m unittest tests.test_swebench_repair_runner
  tests.test_swebench_repair_eval -v`: 67 tests passed;
- Ruff on both Week 5 tools and their tests: passed;
- mypy on both Week 5 tools: passed;
- `python scripts\verify.py` without `--eval-assets`: exit 0, 470 tests passed,
  3 environment skips, 85% total coverage, Ruff passed, 21 source files passed
  mypy, and both CLI smoke checks passed;
- committed unmaterialized plan validation: `valid: true`, zero selected real
  tasks, six configurations, and 120 planned reporting attempts;
- complete synthetic 20-task by 6-configuration report probe: 120 attempts and
  10,000 deterministic Bootstrap replicates completed, with the primary
  denominator remaining 20.

No network request, data download, dependency installation, external-model
call, paid evaluation, Docker task run, or existing `eval/` / `eval/holdout/`
asset access occurred. The new CLIs also reject any input or output path whose
resolved components include `eval` or `holdout`.

Remaining risks for independent review:

- the cohort and exact models are intentionally unmaterialized; there are no
  real SWE-bench outcomes;
- the authorized implementation is a planner, evidence validator, and
  reporter, not the future adapter that maps all six ablations into the Week 3
  Repair runtime and official harness;
- run counters are supplied by that future adapter and hash-bound here, but
  this reporter does not independently replay a trace to derive every counter;
- freeze/run Git chronology needs the preregistered independent external
  auditor because timestamps and hashes alone cannot prove commit ordering;
- the 20-task/four-reporting-repository design is intentionally budget-bounded
  and may produce wide confidence intervals;
- the shared virtual environment uses an editable installation, so validation
  explicitly fixed `PYTHONPATH` to this worktree; the installed `crag` smoke
  entry point remains an environment-level check.

## Integration validation record

The Claude report was integrated as local commit
`90b41eadce0b1e657e4528a453c9313b972c4b80`; the accepted finding fixes and
contract amendments were committed as
`6cde33279489d19b29c3700e9061f4da4ccc8ed0`.

Final offline validation ran from
`E:\shiyan\code_review_agent\traces\worktrees\integration-week5` with
`PYTHONPATH` explicitly fixed to that worktree's `src`:

- focused Week 5 unittest suite: 80 tests passed;
- focused Ruff and mypy: passed;
- committed unmaterialized `validate-plans`: `valid: true`,
  `materialized: false`, zero selected real tasks, six configurations, and 120
  planned reporting attempts;
- synthetic complete-matrix report probe: 120 attempts, primary denominator
  20, 10,000 Bootstrap replicates using
  `repository_stratified_task_sha256_v2`;
- `python scripts\verify.py` without `--eval-assets`: exit 0, 483 tests passed,
  3 environment skips, 85% total coverage, Ruff passed, 21 source files passed
  mypy, and both CLI smoke checks passed;
- `git diff --check`: clean.

No acquisition, real instance, Docker task, external model, paid evaluation,
dependency install, network operation, or existing `eval/` / `eval/holdout/`
asset access occurred during review integration.

Remaining execution-time trust boundaries are explicit:

- the future adapter/data controller still supplies eligibility decisions,
  changed-line counts, telemetry counters, timestamps, and security booleans;
  this validator proves internal and frozen-plan consistency, not physical
  truth, so canonical evidence and independent audit remain mandatory;
- the pre-run freeze commit chronology and absence of matrix-level run-shopping
  need an external auditor;
- no real task was downloaded or materialized, so no pass@1, cost, latency, or
  ablation performance claim exists;
- four reporting repositories with five tasks each imply wide intervals,
  especially for p95 metrics.
