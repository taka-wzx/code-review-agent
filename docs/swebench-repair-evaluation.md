# Trusted SWE-bench Repair evaluation

This is the operator protocol for the Week 5 Repair Agent benchmark. The
normative requirements live in
`docs/plans/week5-swebench-repair-evaluation.md`; the Python validators are the
normative executable interpretation of the JSON artifacts.

## Current status

The committed cohort is deliberately unmaterialized:

- source: `princeton-nlp/SWE-bench_Verified`;
- target: 30 tasks (5 development, 5 tuning, 20 reporting);
- reporting matrix: 20 tasks × 6 frozen configurations = 120 attempts;
- task IDs/data: absent;
- exact model IDs/pricing: absent;
- downloaded repositories/images: absent;
- Docker/model/evaluator runs: none.

The local implementation is therefore an evaluation instrument, not a
SWE-bench result. The synthetic files demonstrate JSON shape only. They are not
real tasks, do not pass the final 120-row cross-file validator, and must never
be quoted as benchmark evidence.

## Authorization gates

Each gate needs a separate user approval:

1. **Acquisition**: download one immutable Verified manifest/harness revision
   and content-addressed task images or build inputs.
2. **Eligibility smoke**: run objective base/gold reproducibility checks in
   Docker, without an Agent or paid model.
3. **Development**: open only development repositories/tasks and run the
   approved development matrix.
4. **Tuning**: open only tuning tasks at the one frozen tuning checkpoint.
5. **Reporting**: freeze source/config/model/pricing/resource hashes, then run
   the exact 120 sealed task/config rows once.
6. **Publication**: inspect and publish aggregate/per-task results and retained
   audit evidence.

Approval does not flow forward automatically. The current repository state
authorizes none of these external phases.

## Offline plan validation now

From the Week 5 worktree:

```powershell
E:\shiyan\code_review_agent\.venv\Scripts\python.exe -B `
  swebench_repair_runner.py validate-plans `
  --cohort swebench_repair\cohort-plan.json `
  --config swebench_repair\config-plan.json
```

Expected facts:

```json
{
  "materialized": false,
  "selected_tasks": 0,
  "configurations": 6,
  "planned_reporting_attempts": 120,
  "models_frozen": false,
  "valid": true
}
```

This command performs no network, Git, Docker, subprocess, SDK, model, or old
evaluation-asset operation.

## Acquisition artifact

An authorized acquisition phase produces an exact-byte JSONL selection log.
Each row has:

- `instance_id`
- canonical lower-case `repository`
- objective `eligible` boolean
- preregistered `exclusion_reason` or null
- `selected` boolean
- assigned `role` or null
- coarse `size_band`
- recomputed `repository_rank_sha256`
- recomputed `task_rank_sha256`

Allowed exclusion reasons are fixed in the Python validator. A forbidden
repository must be logged with `forbidden_repository`; silently dropping it is
invalid.

The materialized cohort adds only selected task metadata:

- immutable base commit/tree and source snapshot hashes;
- content-addressed task image;
- official harness-task hash;
- `FAIL_TO_PASS` and `PASS_TO_PASS` counts;
- role, size band, repository rank, and task rank.

Issue text, source trees, test bodies, gold patches, logs, and model results do
not belong in the repository manifest.

## Deterministic 30-task selection

The seed is derived from the Week 5 base, not chosen by an operator:

```text
SHA256("swebench-repair-cohort-v1" || NUL || ASCII(base_commit))
```

For `afb6e1fa85701d7b6af5b16198c9dd992740a03d` the seed is:

```text
39a89ee8c3368d08f2444ce84c5c86294bef36b2164397f514b50b01be963ce0
```

The executable allocation is intentionally more specific than the prose
minimum:

1. recompute each repository and task rank;
2. an allocatable repository needs at least five eligible tasks, and its first
   five task ranks must cover at least two size bands;
3. sort allocatable repositories by repository rank;
4. assign the first four repositories to reporting and select their first five
   tasks (20 total);
5. assign the next repository to tuning and select five;
6. assign the next repository to development and select five;
7. leave later repositories unassigned and unselected.

Thus repositories cannot cross roles and a human cannot choose convenient task
IDs. If six qualifying repositories do not exist, selection fails instead of
loosening the rule after outcomes are known.

After authorized materialization:

```powershell
python -B swebench_repair_runner.py verify-selection `
  --cohort <materialized-cohort.json> `
  --selection-log <candidate-selection.jsonl>

python -B swebench_repair_runner.py validate-plans `
  --cohort <materialized-cohort.json> `
  --config <frozen-config.json> `
  --selection-log <candidate-selection.jsonl>
```

## Split discipline

Repository identity, not task identity, is the split unit:

- development tasks may support code/prompt work;
- tuning tasks may select only among alternatives frozen before the tuning
  checkpoint;
- reporting tasks cannot influence any configuration and are opened only after
  the pre-run freeze.

The Week 3 pilot repository and Week 4 preregistration repositories are
forbidden. Any additionally inspected task/repository must enter the exclusion
log before materialization.

Reporting task IDs and contents remain sealed from the implementer. A data
controller may generate hashes and the run plan without giving the Agent or
developer access to gold patches or hidden tests.

## Frozen configurations

`swebench_repair/config-plan.json` contains:

- `primary`: dual Finder, context on, Verifier on, Reflection on, model A;
- `single_finder`;
- `no_context`;
- `no_verifier`;
- `no_reflection`;
- `model_b`.

Only one factor changes from primary in each ablation. The benchmark is not a
32-cell factorial. Model A and B must bind distinct exact provider/model
identities and non-empty pricing revisions before a run plan can be generated.
Server aliases that can drift are insufficient unless the provider supplies no
snapshot identifier and that limitation is explicitly retained.

## Resource ceilings

Per task/config:

| Resource | Ceiling |
| --- | ---: |
| wall time | 3,600 seconds |
| tokens | 120,000 |
| cost | 500,000 micro-USD (USD 0.50) |
| tool calls | 150 |
| post-initial repair attempts | 2 |
| Agent test invocations | 10 |
| one command | 600 seconds |
| one command output | 1 MiB |
| Docker | 2 CPU, 4 GiB, 256 pids, 20 GiB writable |

`no_reflection` receives zero post-initial repair attempts. Reporting is capped
at USD 60 and 120 container-hours; all Week 5 paid phases are capped at USD 80.
No more than two task/config attempts run concurrently. These are hard limits,
not spending approval.

## Freeze and run-plan generation

Before any reporting model call, commit an attestation outside normal
development paths that binds:

- materialized cohort canonical hash and selection-log byte hash;
- raw Verified manifest hash and immutable dataset revision;
- official harness revision and every task image digest;
- exact Agent source commit;
- exact primary/ablation configuration hash;
- exact model and pricing identities;
- all budgets, bootstrap seed, and replicate count.

An independent auditor confirms the freeze commit precedes every run. The
offline tool validates identities and timestamps but cannot prove Git ordering
without this external check.

Generate the matrix only after that freeze:

```powershell
python -B swebench_repair_runner.py generate-run-plan `
  --cohort <materialized-cohort.json> `
  --config <frozen-config.json> `
  --selection-log <candidate-selection.jsonl> `
  --agent-source-commit <40-hex> `
  --gold-freeze-commit <40-hex> `
  --freeze-attestation-sha256 <64-hex> `
  --created-at <YYYY-MM-DDTHH:MM:SSZ> `
  --out <run-plan.json>
```

The output has exactly 120 rows. Every row derives unique run, branch,
worktree, container, judge-container, state, and path-token identities. It
contains no host absolute path and starts no process.

## Per-attempt isolation

Every run-plan row maps to a fresh task worktree from the exact base SHA.
Task and judge containers are distinct. Required controls:

- `--network none`;
- read-only container root;
- non-root execution;
- all Linux capabilities dropped;
- `no-new-privileges`;
- fixed pids/CPU/memory limits;
- exactly one writable mount: the task worktree;
- no credentials, API keys, SSH config, Docker socket, other worktrees,
  checkpoint state, gold patches, or evaluator inputs exposed to the Agent.

The control-plane checkpoint root is disjoint from repositories/worktrees.
After the attempt, the original checkout must match its pre-run HEAD/status and
the task worktree/container must be removed or explicitly quarantined.

An infrastructure failure before container creation still occupies its
task/config denominator. It records the unique planned isolation identities,
`worktree_created=false`, `cleanup_status=not_created`, and no evaluator
evidence. It cannot be replaced silently.

## Repair and evaluator boundary

The later execution adapter must preserve Week 3:

- bounded plan, patch, test, reflection, checkpoint, and recovery;
- patch preflight before a write approval;
- approvals delivered through a non-model-callable control provider;
- approved path scope and no approval replay;
- no host credential inheritance;
- budget reservation before every paid call.

The Agent's first frozen patch is the only pass@1 candidate. The official
SWE-bench evaluator runs afterward in a separate judge container. It records
the exact evaluator input/output hashes, bounded log hash, exit code, and
`FAIL_TO_PASS`/`PASS_TO_PASS` counts. The Agent never receives gold or hidden
test artifacts.

## Run JSONL

One immutable JSON object is appended for every run-plan row. It binds:

- task/config/run-plan/purpose/model identities;
- canonical start/completion/record timestamps;
- terminal status/reason and frozen patch hash;
- integer micro-USD, combined tokens, latency milliseconds, tools, tests, and
  policy events;
- planned versus observed worktree/container isolation;
- official evaluator counts and outcome;
- trace, checkpoint, run-plan, evaluator-input, and evaluator-output hashes.

The validator rejects unknown keys, duplicate run/task-config identities,
non-finite numbers, negative counters, reused worktrees/containers/evidence,
and any cross-file mismatch.

## pass@1 and failures

Primary pass@1 is:

```text
officially resolved primary tasks / 20 attempted primary tasks
```

Resolved requires all of:

- terminal `completed`;
- official evaluator attempted and exit zero;
- every `FAIL_TO_PASS` and `PASS_TO_PASS` test passed;
- Agent and judge containers started under the frozen isolation policy;
- original checkout unchanged;
- cleanup proved removed;
- no unauthorized operation.

Model errors, no patch, rejection, timeout, budget exhaustion, test failure,
sandbox failure, policy violation, quarantine, malformed evidence, and
unattempted evaluator all count unresolved. Invalid or contradictory artifacts
fail before metrics rather than disappearing from denominators.

## Metrics and confidence intervals

The report contains, per configuration and for primary headline:

- resolved numerator, attempted denominator, and pass@1;
- per-task and aggregate integer micro-USD cost;
- per-task tokens plus aggregate token total/mean/p50/p95;
- latency mean/p50/p95/max;
- tool total/mean/p50/p95;
- failed Agent test invocations / all test invocations;
- tasks with a failed Agent test / attempted tasks;
- rejected policy events / all attempted operations;
- tasks with any unauthorized event / attempted tasks;
- terminal-status counts;
- paired pass@1 delta for every ablation.

At least 10,000 deterministic percentile-bootstrap replicates sample tasks with
replacement inside each reporting repository. The same sampled task IDs are
used across configurations, so ablation deltas are paired. Intervals cover
pass@1, paired deltas, mean cost, p50/p95 latency, mean tools, task test-failure
rate, and unauthorized-operation task rate.

Generate a final report only from a complete sealed matrix:

```powershell
python -B swebench_repair_eval.py validate-runs `
  --cohort <materialized-cohort.json> `
  --config <frozen-config.json> `
  --selection-log <candidate-selection.jsonl> `
  --run-plan <run-plan.json> `
  --runs <runs.jsonl>

python -B swebench_repair_eval.py report `
  --cohort <materialized-cohort.json> `
  --config <frozen-config.json> `
  --selection-log <candidate-selection.jsonl> `
  --run-plan <run-plan.json> `
  --runs <runs.jsonl> `
  --created-at <YYYY-MM-DDTHH:MM:SSZ> `
  --out <aggregate-report.json>
```

The report binds canonical cohort/config/run-plan hashes and exact selection
and run JSONL byte hashes.

## What this implementation does not do

It does not:

- download SWE-bench;
- know any real instance ID;
- clone or fetch a task repository;
- pull/build or start Docker;
- call a model or judge;
- modify the Repair Agent runtime;
- open old `eval/` or `eval/holdout/`;
- claim a pass@1, cost, latency, or ablation result.

Those omissions are intentional authorization and trust boundaries, not
successful benchmark outcomes.
