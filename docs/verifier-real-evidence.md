# Phase 8D Real Verifier Evidence Preparation

Phase 8D prepares the real Finder and human-label workflow without exercising
external authority. The active machine config intentionally has zero provider
budget, no raw-diff read permission, no assigned humans, no trace-retention
period, no model-training authority, and no commit authority.

The controlling contract is
[`docs/plans/week8d-real-verifier-evidence.md`](plans/week8d-real-verifier-evidence.md).
The offline gate is `verifier_training/phase8d-config.json`.

## What is implemented now

`verifier_phase8d.py` provides:

- deterministic envelopes for all 29 frozen Finder queue entries;
- strict future Finder receipt validation, including honest
  `completed_zero_candidates` receipts;
- candidate/receipt reconciliation and per-PR limits;
- blinded independent packet export with seeded ordering;
- complete response import with tool-computed annotation hashes;
- adjudication packet export only for disagreement or an `uncertain` vote;
- adjudication import bound to both exact source annotation hashes;
- deterministic annotation merge/readiness reporting;
- a Finder-bound real freeze wrapper;
- a real-model readiness gate that remains blocked until the corpus and later
  authorization both close.

It contains no provider client, network request, raw-diff reader, credential
loader, model trainer, or production integration.

## Current blocked Finder preparation

The following command validates the 29 queue/source bindings and emits only
non-executable envelopes:

```powershell
.venv\Scripts\python.exe verifier_phase8d.py prepare-finder `
  --config verifier_training\phase8d-config.json `
  --plan verifier_training\corpus-plan.json `
  --pr-sources verifier_training\corpus-snapshot\pr-sources.jsonl `
  --queue verifier_training\corpus-snapshot\finder-queue.jsonl `
  --out traces\week8d\finder-envelopes.jsonl
```

Every envelope reports these blockers:

- `provider_identity_missing`;
- `provider_budget_zero`;
- `raw_diff_read_unauthorized`.

The command does not open the `diff_object_key`. An envelope is provenance for
future work, not evidence that Finder ran.

## Finder receipt contract

A later executor must write one receipt per queue entry. The status rules are:

| Status | Candidate count | Error |
| --- | ---: | --- |
| `completed` | 1--16 | none |
| `completed_zero_candidates` | 0 | none |
| `failed` | 0 | required bounded category |

Candidate IDs must be sorted, unique across runs, and exactly match candidate
source rows bound to that PR. Tokens, cost, provider/model, prompt, trace,
queue, PR source, and diff hashes are part of each immutable receipt.

Zero-candidate completion closes only the execution-completeness gate. It does
not create a fabricated drop example. A failed run remains an incomplete gate.

The active validator rejects every non-synthetic receipt because real provider
authority is still absent. A later contract amendment must change that gate
before a provider adapter is added.

## Independent annotation packets

After real candidate import, generate two packets with different stable
reviewer IDs and preferably different order seeds:

```powershell
.venv\Scripts\python.exe verifier_phase8d.py export-independent `
  --plan verifier_training\corpus-plan.json `
  --pr-sources verifier_training\corpus-snapshot\pr-sources.jsonl `
  --candidate-sources <candidate-sources.jsonl> `
  --reviewer-id <annotator-a-id> `
  --rubric-sha256 <rubric-sha256> `
  --order-seed <seed-a> `
  --created-at <UTC timestamp> `
  --out <packet-a.json>
```

Repeat for annotator B. Real packet export currently fails closed; the example
shape is exercised only by synthetic in-memory tests until human identities are
authorized.

Packets include candidate text, evidence, bounded tool summaries, repository,
source ID, and merge revision. They omit explicit split names, peer decisions,
model scores, predictions, thresholds, and test results. Repository identity is
necessarily visible to inspect code, so organizational procedures must also
tell reviewers not to infer or discuss split roles.

Humans return JSONL rows containing only:

```json
{
  "candidate_id": "...",
  "label": "keep",
  "rationale": "Concrete evidence-based reason.",
  "created_at": "2026-07-22T09:00:00Z"
}
```

Import computes all provenance hashes:

```powershell
.venv\Scripts\python.exe verifier_phase8d.py import-responses `
  --plan verifier_training\corpus-plan.json `
  --pr-sources verifier_training\corpus-snapshot\pr-sources.jsonl `
  --candidate-sources <candidate-sources.jsonl> `
  --packet <packet-a.json> `
  --responses <responses-a.jsonl> `
  --out <annotations-a.jsonl>
```

Missing, duplicate, or extra candidate IDs fail closed.

## Adjudication

Merge the two independent files only after both reviewers have submitted. The
adjudication packet contains exactly the candidates where labels disagree or
either label is `uncertain`; it includes both immutable source labels and
hashes. The adjudicator ID must differ from both annotators.

```powershell
.venv\Scripts\python.exe verifier_phase8d.py export-adjudication `
  --plan verifier_training\corpus-plan.json `
  --pr-sources verifier_training\corpus-snapshot\pr-sources.jsonl `
  --candidate-sources <candidate-sources.jsonl> `
  --annotations <independent-annotations.jsonl> `
  --reviewer-id <adjudicator-id> `
  --rubric-sha256 <rubric-sha256> `
  --order-seed <adjudication-seed> `
  --created-at <UTC timestamp> `
  --out <adjudication-packet.json>
```

Import adjudicator responses with `import-responses`, then combine all files:

```powershell
.venv\Scripts\python.exe verifier_phase8d.py merge-annotations `
  --plan verifier_training\corpus-plan.json `
  --pr-sources verifier_training\corpus-snapshot\pr-sources.jsonl `
  --candidate-sources <candidate-sources.jsonl> `
  --inputs <annotations-a.jsonl> <annotations-b.jsonl> <adjudications.jsonl> `
  --out <annotations-final.jsonl>
```

`ready_to_freeze=true` means every candidate has a resolvable label. It does not
by itself mean the Finder receipts or repository-support gates are complete.

## Real freeze and model readiness

`freeze-real` binds all Finder receipt hashes to the existing corpus manifest.
Completed-zero sources do not trigger `selected_pr_without_candidates`; failed
runs, unresolved annotations, synthetic provenance, or a repository with no
resolved candidate still keep `trainable=false`.

The active config then deliberately produces these model blockers:

- `real_freeze_not_trainable` until real data closes;
- `real_model_training_unauthorized`;
- `real_model_plan_unfrozen`;
- `real_model_seeds_missing`.

There is intentionally no Phase 8D model-run command yet. Adding one before the
real freeze, seeds, test-label custodian, compute limits, and quality claim rule
are frozen would turn an offline preparation task into an unauthorized
experiment.

## Decisions required for the next amendment

| Decision | Current value |
| --- | --- |
| Finder provider | not assigned |
| Exact model/snapshot ID | not assigned |
| Temperature and prompt SHA-256 | not assigned |
| Maximum calls/input tokens/output tokens/CNY | all zero |
| Read the 29 retained raw diffs | forbidden |
| Raw trace retention | not assigned |
| Annotator A/B IDs | not assigned |
| Distinct adjudicator ID | not assigned |
| Stable local Phase 8 commit | forbidden |
| Real model seeds/resources/run | forbidden |

Actual names need not enter the repository. Stable pseudonymous IDs may be used
if an external custodian keeps the identity mapping and confirms that all three
people are distinct.
