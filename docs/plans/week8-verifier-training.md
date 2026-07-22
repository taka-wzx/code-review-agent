# Week 8: Verifier Training Evidence

## Goal

Build a reproducible, leakage-resistant training and evaluation path for a
controllable finding Verifier/Reranker. The phase starts with an offline,
dependency-free data and metric foundation, then admits separately authorized
small-model Base, LoRA/SFT, and pairwise-preference experiments over a frozen
multi-repository corpus.

The deliverable is evidence about whether post-training improves keep/drop
ranking and calibration across repositories. It is not a replacement for the
production Verifier, a general code-model claim, or permission to train on the
protected evaluation fixtures.

## Base and delivery

- Base branch: `integration/week7-5-live-validation`
- Base commit: `ae4a7ffd307072fa1ddadff4c82a96f9c170d847`
- Codex branch: `codex/week8-verifier-training`
- Codex worktree: isolated local worktree (host path intentionally omitted)
- Integration branch: `integration/week8-verifier-training`

No commit, push, pull request, merge, paid model call, dataset download, or
accelerator use is authorized by this contract alone. Those actions require
the user's explicit approval. Direct changes to `master` remain prohibited.

## Phase boundaries

### Phase 8A: offline foundation

This task may implement and validate without external access:

- a strict JSONL candidate protocol containing candidate text, bounded evidence,
  bounded tool summaries, label provenance, repository identity, and immutable
  content hashes;
- deterministic repository-grouped train/validation/test manifests and leakage
  audits that reject repository, PR, finding, or content overlap;
- a dependency-free lexical baseline and pairwise ranker used only to prove the
  pipeline and metric implementation;
- Precision, Recall, F1, PR-curve, threshold, ECE/calibration, latency, cost,
  confusion, and error-slice reports;
- synthetic fixtures and offline unit tests.

Synthetic examples are contract tests, never benchmark evidence. Phase 8A may
report only pipeline-validation results.

### Phase 8B: frozen corpus

Before any model training, a separate recorded approval must freeze:

- repositories, licenses/permissions, source revisions, and collection window;
- annotation instructions and independent/adjudicated label provenance;
- content-retention and secret-scanning policy;
- repository-level split assignment and dataset hashes;
- maximum examples, tokens, storage, accelerator hours, and monetary cost.

Existing `trusted_review/` annotation records may inform the protocol but are
not silently converted into training text: they intentionally lack the full
candidate/evidence payload needed for this phase. `eval/` and `eval/holdout/`
are never training sources.

#### Phase 8B authorization amendment (2026-07-19)

The user's instruction to proceed with Phase 8B authorizes the following
bounded corpus work:

- freeze and validate the public-repository corpus plan below;
- implement dependency-free source, candidate, annotation, secret-scan, and
  freeze-manifest tooling with synthetic fixtures;
- download at most the selected public GitHub PR metadata and unified diffs,
  read-only, after the plan validates;
- retain raw objects only under the ignored task-local `traces/` area, capped at
  512 MiB and 30 days; commit only source identities, licenses, hashes,
  aggregate counts, and later sanitized candidate records;
- make no GitHub mutation and never print or persist authentication material.

This amendment does not authorize a paid Finder/API run, model or dataset hub
download, training dependency installation, accelerator use, model upload, or
Phase 8C experiment. Because Finder generation and two-person annotation are
not yet available, Phase 8B may stop after freezing public source snapshots and
must report the remaining candidate/annotation gates as incomplete.

The pilot corpus is preregistered as 29 merged, non-draft, code-changing PRs in
the fixed UTC window `2024-01-01T00:00:00Z` through
`2026-01-01T00:00:00Z`:

| Split | Repository | SPDX | Target PRs |
| --- | --- | --- | ---: |
| train | `pallets/click` | BSD-3-Clause | 4 |
| train | `Textualize/rich` | MIT | 4 |
| train | `pytest-dev/pytest` | MIT | 4 |
| train | `django/django` | BSD-3-Clause | 4 |
| validation | `pydantic/pydantic` | MIT | 2 |
| validation | `fastapi/fastapi` | MIT | 2 |
| test | `pallets/flask` | BSD-3-Clause | 3 |
| test | `psf/requests` | Apache-2.0 | 3 |
| test | `encode/httpx` | BSD-3-Clause | 3 |

The test repositories inherit the Week 4 reporting set and are forbidden from
candidate-generation tuning, annotation-rubric tuning, threshold selection,
or error-driven model changes. The pilot seed is
`f0140063e6bb4998f8fc8611c5762602ac50f346c94560798b5dd07f6c9ffe87`,
derived as SHA-256 of the UTF-8 bytes
`week8b-verifier-corpus-v1\n<base-commit>`.

Selection first queries merged, non-draft, non-Dependabot PRs in the frozen
window, ordered by PR creation time ascending, and caps this query-qualified
pool at 64 PRs per repository. It ranks that bounded pool by
`SHA256(seed + "\n" + canonical owner/repo#number)`, then inspects entries in
rank order until the declared target count is admitted. Admission requires
exact base/head/merge SHAs, at least one non-generated source or test file, a
bounded unified diff, and no dependency-only, documentation-only, vendored,
security-embargoed, bot-authored, or secret-scan-positive content. The legacy
`eligible_pool_size` record field means this query-qualified pool size; each
selection log records deeper exclusions encountered during the ranked walk.

Resource ceilings are 29 PRs, 16 candidates per PR, 480 candidates overall,
64 MiB sanitized/frozen data, 512 MiB raw task-local snapshots, zero accelerator
hours, and zero paid-model cost. Exceeding a ceiling fails closed and requires a
new amendment before more data is viewed.

### Phase 8C: model experiments

After the Phase 8B corpus and runtime are approved, run the same frozen test set
for:

1. an untrained/base small model;
2. LoRA/SFT;
3. pairwise preference optimization or ranking loss;
4. the existing API Verifier as a reference, only if paid calls are separately
   authorized.

The exact model snapshot, tokenizer, framework versions, random seeds,
hyperparameters, prompt template, quantization, and hardware must be pinned in
the experiment manifest. No experiment may choose a threshold on the test set.

#### Phase 8C authorization amendment (2026-07-19)

The user's instruction to perform Phase 8C and finish the remaining Week 8 work
authorizes a bounded, local CPU experiment environment under ignored
`traces/week8c-runtime/` with these fail-closed ceilings:

- base model: `google/bert_uncased_L-2_H-128_A-2`, Apache-2.0, safetensors only,
  exact Hub revision and file SHA-256 recorded before training;
- top-level runtime: CPython 3.13, PyTorch 2.13.0, Transformers 5.13.0, and
  PEFT 0.19.1 in a task-local virtual environment; the resolved transitive lock
  is committed separately from package dependencies;
- at most 2 GiB environment/model/checkpoint storage, 2 CPU-hours, four CPU
  threads, 64 MiB model snapshot downloads, zero accelerator-hours, and CNY 0;
- no provider/API inference, dataset hub download, protected evaluation access,
  model/data upload, GitHub mutation, package-runtime dependency change, or
  production Verifier integration.

The authorized comparison is Base, full sequence-classification SFT,
LoRA/SFT, and LoRA pairwise ranking on one identical frozen test manifest.
Until the Phase 8B real candidate and two-human annotation gates close, these
runs use only explicitly synthetic protocol fixtures and are reported as
pipeline smoke evidence, never model-quality evidence. Synthetic labels cannot
be promoted to human provenance or make the real corpus trainable.

## Frozen data protocol

Each candidate row is versioned and immutable. Required semantic fields are:

- stable `candidate_id`, `repository_id`, `change_id`, and `source_revision`;
- `candidate_text` containing the proposed finding, with a byte limit;
- bounded positive/negative `evidence` items with repository-relative locations
  and no raw credentials or absolute host paths;
- bounded tool summaries that omit raw stdout/stderr and secret-bearing inputs;
- final label `keep`, `drop`, or `uncertain`, plus label provenance and rationale;
- optional pair/group identity for preference training;
- SHA-256 hashes over canonical content and the complete source record.

Rows are append-only after a dataset freeze. Corrected labels receive new row
identities and explicit lineage; frozen records are not edited in place.

The split manifest assigns whole repositories to exactly one of `train`,
`validation`, or `test`. Validation rejects overlap in repository, change,
candidate, pair/group, canonical content, or source-record hash. Duplicate
content across renamed repositories is therefore still rejected.

`uncertain` is retained for audit and optional three-way experiments. The
primary binary keep/drop metrics exclude it with an explicit count; it must not
be silently coerced into either class.

## Frozen evaluation protocol

- Positive class: `keep`.
- A score is a finite probability in `[0, 1]` where larger means more likely to
  keep.
- The operating threshold is selected on validation data, then frozen before
  test scoring.
- Primary report: micro Precision, Recall, F1, confusion counts, and test-set
  support at the frozen threshold.
- Curves: deterministic PR points for every distinct score and PR-AUC computed
  with an explicitly documented interpolation rule.
- Calibration: equal-width bins and Expected Calibration Error (ECE), including
  bin counts, mean confidence, and empirical keep rate.
- Generalization: per-repository metrics and macro averages on repositories not
  present in training.
- Resource evidence: example/token counts, wall time, peak memory when
  available, accelerator type, estimated or billed cost with provenance, and
  per-candidate inference latency.
- Error analysis: false-positive, false-negative, uncertain, evidence-missing,
  tool-failure, severity, language, and repository slices when those attributes
  are present.

Reports must distinguish `undefined` from zero when a denominator has no
support. Any comparison lacking identical frozen test records is labeled
non-comparable.

## Frozen runtime and product interfaces

- The existing `src/code_review_agent/verifier.py`, runtime prompts, sentinels,
  Review schema, CLI, HTTP/MCP surfaces, and fail-open behavior are unchanged.
- `pyproject.toml`, `requirements.lock`, `requirements.txt`, and CI are frozen.
- Phase 8 tooling is repository-only and dependency-free in Phase 8A.
- A future training environment must use a separate, pinned manifest; training
  libraries are not added to the installable review-agent package by default.
- Loading a trained artifact into the product is a separate deployment contract
  requiring compatibility, security, latency, fallback, and rollback gates.

## Single Writer ownership

Codex may create or modify only:

- `docs/plans/week8-verifier-training.md`
- `docs/verifier-training.md`
- `verifier_training.py`
- `verifier_training/config.json`
- `verifier_training/schemas/candidate.schema.json`
- `verifier_training/schemas/split-manifest.schema.json`
- `verifier_training/schemas/prediction.schema.json`
- `verifier_training/schemas/experiment.schema.json`
- `verifier_training/examples/candidates.jsonl`
- `verifier_training/examples/split-manifest.json`
- `verifier_training/examples/predictions.jsonl`
- `verifier_corpus.py`
- `verifier_training/corpus-plan.json`
- `verifier_training/schemas/pr-source.schema.json`
- `verifier_training/schemas/candidate-source.schema.json`
- `verifier_training/schemas/corpus-annotation.schema.json`
- `verifier_training/schemas/freeze-manifest.schema.json`
- `verifier_training/examples/corpus/pr-sources.jsonl`
- `verifier_training/examples/corpus/candidate-sources.jsonl`
- `verifier_training/examples/corpus/annotations.jsonl`
- `verifier_training/examples/corpus/freeze-manifest.json`
- `verifier_training/examples/corpus/frozen-candidates.jsonl`
- `verifier_training/examples/corpus/frozen-splits.json`
- `verifier_training/corpus-snapshot/pr-sources.jsonl`
- `verifier_training/corpus-snapshot/acquisition-manifest.json`
- `verifier_training/corpus-snapshot/finder-queue.jsonl`
- `verifier_transformer.py`
- `tests/test_verifier_transformer.py`
- `docs/verifier-transformer.md`
- `verifier_training/phase8c-config.json`
- `verifier_training/phase8c-runtime.lock`
- `verifier_training/model-snapshot.json`
- `verifier_training/examples/phase8c/base.json`
- `verifier_training/examples/phase8c/full-sft.json`
- `verifier_training/examples/phase8c/lora-sft.json`
- `verifier_training/examples/phase8c/lora-pairwise.json`
- `verifier_training/examples/phase8c/comparison.json`
- `tests/test_verifier_training.py`
- `tests/test_verifier_corpus.py`
- `docs/verifier-corpus.md`
- `README.md`
- `AGENDA.md`
- `docs/reviews/week8-claude.md` only when importing a future read-only review
  report

All package runtime, dependencies, CI, prior plans/reports, existing trusted
evaluation assets, security assets, prompts, sentinels, and release history are
read-only.

## Prohibited changes

- No reading, modifying, copying, or training on `eval/holdout`.
- No modifying any `eval/` fixture, truth, prompt, judge, or benchmark result.
- No external provider call, paid evaluation, training job, GPU/accelerator use,
  dataset download, model download, or model upload without explicit approval.
- No use of PR contents, traces, tool output, or annotations without documented
  authorization and secret/license review.
- No random row-level split across repositories or test-driven threshold tuning.
- No serialization of credentials, raw environment values, absolute host paths,
  private repository content, or unrestricted command output.
- No claim that synthetic examples demonstrate model quality.
- No replacement of the production Verifier or change to its prompt/sentinels in
  this phase.

## Validation

Focused Phase 8A gates:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_verifier_training -v
.venv\Scripts\python.exe -m ruff check verifier_training.py tests\test_verifier_training.py
.venv\Scripts\python.exe verifier_training.py validate `
  --data verifier_training\examples\candidates.jsonl `
  --splits verifier_training\examples\split-manifest.json
.venv\Scripts\python.exe verifier_training.py evaluate `
  --data verifier_training\examples\candidates.jsonl `
  --splits verifier_training\examples\split-manifest.json `
  --predictions verifier_training\examples\predictions.jsonl `
  --split test
```

Focused Phase 8B gates:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_verifier_corpus -v
.venv\Scripts\python.exe -m ruff check verifier_corpus.py tests\test_verifier_corpus.py
.venv\Scripts\python.exe verifier_corpus.py validate `
  --plan verifier_training\corpus-plan.json `
  --pr-sources verifier_training\examples\corpus\pr-sources.jsonl `
  --candidate-sources verifier_training\examples\corpus\candidate-sources.jsonl `
  --annotations verifier_training\examples\corpus\annotations.jsonl
.venv\Scripts\python.exe verifier_corpus.py freeze `
  --plan verifier_training\corpus-plan.json `
  --pr-sources verifier_training\examples\corpus\pr-sources.jsonl `
  --candidate-sources verifier_training\examples\corpus\candidate-sources.jsonl `
  --annotations verifier_training\examples\corpus\annotations.jsonl `
  --frozen-at 2026-07-19T02:00:00Z `
  --candidates-out verifier_training\examples\corpus\frozen-candidates.jsonl `
  --splits-out verifier_training\examples\corpus\frozen-splits.json `
  --manifest-out verifier_training\examples\corpus\freeze-manifest.json
```

Focused Phase 8C gates:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_verifier_transformer -v
.venv\Scripts\python.exe -m ruff check verifier_transformer.py tests\test_verifier_transformer.py
.venv\Scripts\python.exe verifier_transformer.py validate `
  --config verifier_training\phase8c-config.json `
  --model-snapshot verifier_training\model-snapshot.json
```

Full pre-handoff gates:

```powershell
.venv\Scripts\python.exe scripts\verify.py
git diff --check
```

The evaluation consistency command is intentionally omitted because this task
must not read or change protected evaluation assets. The synthetic model run is
performed only in the separately pinned ignored runtime described by the Phase
8C amendment; the normal validation command does not import that runtime.

## Acceptance criteria

### Phase 8A

- Candidate and prediction inputs reject unknown fields, invalid identifiers,
  non-finite scores, oversized text, unsafe paths, malformed hashes, and
  inconsistent canonical hashes.
- Split validation proves whole-repository isolation and rejects overlap by all
  frozen leakage keys, including duplicate canonical content.
- Threshold selection uses validation labels only; test evaluation consumes an
  already frozen threshold or reports a clearly marked diagnostic default.
- Metrics, PR curve/AUC, ECE, per-repository aggregation, and error slices have
  deterministic unit tests covering empty/undefined and tie cases.
- The lexical baseline and pairwise ranker are deterministic under a seed and
  are labeled pipeline baselines rather than small-model evidence.
- Synthetic CLI validation/evaluation succeeds with no network, model, package
  runtime, or protected-asset access.
- Existing offline verification remains green and the diff is ownership-clean.

### Phase 8B/8C

- Corpus provenance, permissions, secret scan, repository split, hashes, and
  annotation quality are frozen before training.
- The corpus plan rejects unknown repositories, split overlap, unsupported
  licenses, mutable source identities, selection-rank mismatches, missing raw
  object hashes, failed secret scans, or any configured resource overrun.
- Every candidate binds an admitted PR source and canonical content hash; no PR
  exceeds 16 candidates and the complete corpus does not exceed 480.
- Two distinct independent human annotations are required per candidate.
  Agreement with no `uncertain` becomes final; disagreement or either
  `uncertain` requires a third adjudication that cryptographically cites both
  source annotations. Synthetic fixtures are the sole exception and remain
  marked synthetic.
- Freeze compilation is deterministic and emits final candidate rows, a
  repository split manifest, annotation-agreement counts, provenance hashes,
  and explicit incomplete gates. A real corpus cannot report `trainable=true`
  until every selected source has candidates, every candidate has a final
  label, and license/secret/retention checks pass.
- Base, LoRA/SFT, preference/ranking, and API reference results use identical
  test records and report all frozen metrics and resources.
- At least three repositories contribute to the held-out test split, unless the
  report explicitly downgrades the result to a pilot.
- The final report includes Base/SFT/preference ablation, calibration, threshold
  sensitivity, cross-repository analysis, cost/latency, and qualitative errors.
- A future Claude review has no unresolved P0/P1/P2 before integration.

## Handoff and integration

1. Complete Phase 8A only on the Codex task branch and inspect every changed
   line against this contract.
2. If the user chooses manual Claude review, create the authorized local handoff
   commit and provide the exact worktree command and self-contained review
   prompt required by `AGENTS.md`.
3. Do not begin Phase 8B/8C until corpus, dependencies, hardware, budget, and
   external-access authority are explicitly frozen.
4. Integrate a future review into `integration/week8-verifier-training`, rerun
   all applicable gates, and stop there by default.
5. Only the user may authorize push or merge to `master`.

## Delivery report

- Summary: Phase 8A implements the strict candidate, split, metric, and lexical
  baseline protocol. Phase 8B freezes 29 public PR sources plus a pending Finder
  queue and keeps the real corpus non-trainable. Phase 8C pins and runs four
  synthetic CPU Transformer paths with hash-bound, non-claiming reports.
- Changed files: only the paths declared in Single Writer ownership: the Week 8
  contract/docs, `README.md`, `AGENDA.md`, three repository-only tools and test
  modules, and the declared `verifier_training/` configs, schemas, snapshots,
  queues, fixtures, runtime lock, and experiment reports.
- Commit: not authorized or created.
- Commands and results: 32 focused Phase 8 tests passed; all focused CLI and
  Ruff gates passed; the offline Transformer rerun reproduced dataset, state,
  score, threshold, and semantic-metric values. The authoritative
  `scripts/verify.py` run passed 625 tests with 3 skips, 86% coverage, Ruff,
  mypy for 26 source files, both CLI smokes, and 48 security cases at zero
  attack success, false block, secret disclosure, or unauthorized execution.
  `git diff --check`, JSON parsing, and whitespace/host-path/credential audits
  passed. An earlier full run used the root `.venv`, which lacks Week 7 `mcp`
  and `fastapi`, and therefore stopped at two import errors; the authoritative
  rerun used the existing locked Week 7 environment with this worktree's `src`
  first on `PYTHONPATH`.
- Known risks or assumptions: Phase 8A and 8C validate pipelines only. No real
  trained-model quality evidence exists until Finder candidates, two-person
  annotations, required adjudications, and a real immutable freeze complete.
  The high-signal scanner is defense in depth, not a substitute for human
  privacy review. Phase 8C used the one explicitly authorized model download;
  it used no dataset download, accelerator, provider/API call, paid evaluation,
  upload, product integration, or protected evaluation asset.

### Phase 8B progress (2026-07-19)

- The bounded public source acquisition completed all nine repository targets:
  29 selected PRs, 2,021,424 raw bytes, and zero high-signal secret findings on
  selected diffs. Raw metadata, selection logs, scan reports, and diff objects
  remain ignored under `traces/week8b-corpus/`.
- The committable `verifier_training/corpus-snapshot/` contains only public PR
  identities, immutable revisions, licenses, object/scan/log hashes, aggregate
  counts, and incomplete gates. Its source-set SHA-256 is
  `4ce49ebfe68cb2d0ebe125f2cbd68f77ea3222a64449fcabedd3d39d2ec56209`.
- `trainable=false` remains mandatory because Finder candidates and two-person
  independent human annotations/adjudication have not been produced. No paid
  provider, model/dataset download, accelerator, GitHub mutation, or protected
  evaluation asset was used.

### Phase 8C progress (2026-07-19)

- The exact Apache-2.0 model revision
  `30b0a37ccaaa32f332884b96992754e246e48c5f` was fetched as safetensors only;
  the 17,739,144-byte weight file and all supporting files are SHA-256 bound in
  `model-snapshot.json`.
- An isolated CPython 3.13 CPU runtime pins PyTorch 2.13.0, Transformers 5.13.0,
  PEFT 0.19.1, and every resolved dependency. The completed runtime occupied
  857,461,423 bytes; all four experiment paths completed in under two seconds
  apiece using four CPU threads, zero accelerator-hours, and CNY 0.
- Base, full SFT, LoRA SFT, and LoRA pairwise used the same synthetic manifest,
  validation-selected thresholds, dataset hash, metric implementation, and
  report schema. Base happened to score 2/2 on the tiny binary test while all
  trained paths missed its one positive; this is pipeline smoke variance, not
  evidence of quality or post-training benefit. Every artifact freezes
  `synthetic_only=true` and `quality_claim_allowed=false`.
- All 29 real PR sources now have an immutable `pending` Finder queue record.
  Real candidates, two distinct human labels per candidate, and adjudication
  where required remain external gates, so the real corpus remains
  `trainable=false`. No API Verifier call, dataset download, accelerator,
  upload, GitHub mutation, product integration, or protected evaluation access
  was used.
