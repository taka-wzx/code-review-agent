# Phase 8D Real Verifier Evidence Preparation

Phase 8D prepares and executes the bounded real Finder and then hands its
candidate set to three real people. The active config authorizes GLM-5.2,
reading exactly 29 retained hash-bound diffs, CNY 250, 30-day raw retention,
and a stable local commit. It does not authorize model training or permit an
agent to stand in for any human reviewer.

The controlling contract is
[`docs/plans/week8d-real-verifier-evidence.md`](plans/week8d-real-verifier-evidence.md).
The machine gate is `verifier_training/phase8d-config.json`.

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

`verifier_phase8d_glm.py` adds the separately bounded provider adapter. It
attests every object before use, injects no credential into artifacts, forces
the frozen GLM request settings, records response identity and pass degradation,
and refuses replacement artifacts or a budget/path/hash expansion.

## Finder input attestation and execution

The following commands validate the executable envelopes and then read/hash all
29 authorized objects without contacting the provider:

```powershell
.venv\Scripts\python.exe verifier_phase8d.py prepare-finder `
  --config verifier_training\phase8d-config.json `
  --plan verifier_training\corpus-plan.json `
  --pr-sources verifier_training\corpus-snapshot\pr-sources.jsonl `
  --queue verifier_training\corpus-snapshot\finder-queue.jsonl `
  --out traces\week8d\finder-envelopes.jsonl

.venv\Scripts\python.exe verifier_phase8d_glm.py attest-inputs `
  --config verifier_training\phase8d-config.json `
  --plan verifier_training\corpus-plan.json `
  --pr-sources verifier_training\corpus-snapshot\pr-sources.jsonl `
  --queue verifier_training\corpus-snapshot\finder-queue.jsonl `
  --raw-root traces\week8b-corpus
```

`prepare-finder` still does not open a diff. `attest-inputs` opens only the
content-addressed objects and reports counts/hashes, never their contents.

Set one credential in the current PowerShell process, then invoke the explicit
runner. Do not put the key on the command line or commit it:

```powershell
$env:GLM_API_KEY = "<operator-supplied secret>"
.venv\Scripts\python.exe verifier_phase8d_glm.py run `
  --config verifier_training\phase8d-config.json `
  --plan verifier_training\corpus-plan.json `
  --pr-sources verifier_training\corpus-snapshot\pr-sources.jsonl `
  --queue verifier_training\corpus-snapshot\finder-queue.jsonl `
  --raw-root traces\week8b-corpus `
  --trace-dir traces\week8d\glm-traces `
  --receipts-out traces\week8d\finder-runs.jsonl `
  --candidates-out traces\week8d\candidate-sources.jsonl
```

The model ID is a mutable service alias. Raw traces therefore bind each API
response's model, request ID, UTC receipt time, optional system fingerprint,
input hashes, documentation hash, and anchor/sampling status. Delete raw diffs
and raw traces when their recorded 30-day retention expires.

## Real GLM-5.2 run result

The first authorized run completed all 29 queue positions on 2026-07-22. Its
validated sanitized artifacts contain 24 completed PRs, 3 honest zero-candidate
PRs, 2 failed PRs, and 116 candidates. Returned usage was 603,883 input tokens
and 119,027 output tokens; the conservative uncached-price estimate is CNY
8.16382. All 175 successful responses identified themselves as `glm-5.2` and
all receipt-to-trace SHA-256 bindings matched.

The failures are `Textualize/rich#3468` (`finder_protocol_error`) and
`psf/requests#6655` (`provider_error`). The sampling pass for
`pytest-dev/pytest#11574` degraded, so its anchor result is the recorded result.
No failed item was retried. These failures keep the real freeze closed, and
blind packets are intentionally deferred so a later authorized recovery cannot
silently invalidate completed human work.

Phase 8D-R1 subsequently retried exactly those two failures once. Both recovery
runs completed and added 21 candidates. The immutable effective view therefore
contains 26 completed sources, 3 zero-candidate sources, zero failures, and 137
candidates. R1 used 13 logical calls, 32,779 input tokens, 8,825 output tokens,
and an estimated CNY 0.509332; combined v1+R1 estimated cost is CNY 8.673152.
The supersession audit binds both old failure hashes to the new receipts rather
than rewriting history. Finder is now complete.

Two 137-item blind packets and deliberately blank response templates are now
frozen under `verifier_training/real/`. Packet A belongs only to
`human-reviewer-a-v1`; packet B belongs only to `human-reviewer-b-v1`. Give the
files to two distinct real people separately. Neither reviewer may see the
other packet or response. Each person fills every template row with one of
`keep`, `drop`, or `uncertain`, an evidence-based rationale, and a real UTC
timestamp. Work on copies outside the committed repository; do not edit the
frozen templates in place. Return the two completed JSONL files for strict
import. No adjudication packet can be generated until both complete response
sets exist.

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

The active validator accepts real receipts only when provider, requested model,
prompt hash, aggregate tokens, and aggregate cost remain inside the amendment.

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

Repeat with `human-reviewer-b-v1`; annotator A is `human-reviewer-a-v1`.
An external custodian must confirm those IDs map to two distinct real people.

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

## Frozen authorization and remaining gate

| Decision | Current value |
| --- | --- |
| Finder provider | `glm` |
| Exact API model ID | `glm-5.2` (mutable service alias) |
| Temperatures | anchor 0.20 / sampling 0.70 |
| Maximum calls/input/output/CNY | 580 / 20M / 2M / 250 |
| Theoretical HTTP attempts | 1,740 including two SDK retries |
| Read the 29 retained raw diffs | authorized, hash-bound only |
| Raw diff/trace retention | 30 days |
| Annotator A/B IDs | `human-reviewer-a-v1` / `human-reviewer-b-v1` |
| Distinct adjudicator ID | `human-adjudicator-c-v1` |
| Stable local Phase 8 commit | authorized and created |
| Real model seeds/resources/run | forbidden |

Actual names need not enter the repository. Stable pseudonymous IDs may be used
if an external custodian keeps the identity mapping and confirms that all three
people are distinct.
