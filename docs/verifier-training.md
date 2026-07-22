# Verifier Training and Reranking

Week 8 separates the model-training question from the production review loop.
The repository now has an offline protocol for candidate data, repository-level
splits, lexical pipeline baselines, frozen predictions, and comparable metrics.
It now also contains a bounded synthetic Transformer smoke comparison. It does
**not** contain a real trainable corpus or evidence that post-training improves
Verifier quality.

The frozen task contract is
[`docs/plans/week8-verifier-training.md`](plans/week8-verifier-training.md).

## Evidence levels

| Level | What it proves | Current status |
| --- | --- | --- |
| Phase 8A synthetic protocol | Parsers, hashes, leakage gates, thresholding, metrics, and baseline artifacts are deterministic | Implemented offline |
| Phase 8B frozen corpus | Candidate/evidence/trace collection is authorized, licensed, secret-scanned, annotated, and repository-split | 9-repository/29-PR public source snapshot and pending Finder queue frozen; Finder and human annotation gates incomplete |
| Phase 8C model experiment | Base vs LoRA/SFT vs preference/ranking quality, calibration, cost, and latency on identical held-out repositories | Four-path CPU pipeline smoke completed on synthetic fixtures; quality claim prohibited |
| Product deployment | A trained artifact safely replaces or augments the API Verifier with rollback and fail-open guarantees | Out of scope |

Synthetic examples exist only to exercise the protocol. Their perfect sample
score is constructed and must never be quoted as model or benchmark quality.

## Artifact map

- `verifier_training.py`: standard-library validation, metrics, two lexical
  pipeline baselines, and CLI.
- `verifier_training/config.json`: frozen label, split, threshold, metric, and
  size conventions.
- `verifier_training/schemas/`: JSON Schemas for candidate, split, prediction,
  and future model-experiment manifests.
- `verifier_training/examples/`: nine synthetic records across three isolated
  repositories plus fixed test predictions.
- `tests/test_verifier_training.py`: leakage, integrity, metric, calibration,
  baseline-determinism, and CLI artifact regression tests.
- `verifier_corpus.py`: Phase 8B source, candidate, two-person annotation,
  adjudication, and deterministic freeze gates.
- `verifier_training/corpus-plan.json`: frozen public repositories, licenses,
  PR window, selection seed, split assignment, limits, retention, and authority.
- `docs/verifier-corpus.md`: Phase 8B operator protocol and current gate meaning.
- `verifier_transformer.py`: offline Base, full SFT, LoRA SFT, and LoRA
  pairwise runner with strict config/report validation and lazy heavy imports.
- `verifier_training/phase8c-config.json`, `phase8c-runtime.lock`, and
  `model-snapshot.json`: frozen experiment, dependency, and model identities.
- `verifier_training/examples/phase8c/`: hash-bound synthetic reports and
  comparison manifest.
- `docs/verifier-transformer.md`: reproduction commands, recorded results, and
  interpretation limits.

The tool is repository-only. Nothing imports it from `src/code_review_agent/`,
and it adds no runtime or training dependency to the installable package.

## Candidate records

Each JSONL row represents one Finder candidate and its final training label.
The protocol includes:

- stable candidate, repository, change, and source-revision identities;
- bounded candidate text;
- positive, negative, or explicitly missing evidence with repository-relative
  locations;
- bounded tool summaries, never raw tool stdout/stderr;
- `keep`, `drop`, or `uncertain`, label provenance, rationale, severity, and
  language;
- optional `pair_id` for ranking examples;
- a label-independent canonical-content hash and a full-record hash.

The validator recomputes both hashes. It also rejects unknown fields, unsafe
paths, NULs, oversized strings, and high-signal credential/host-path patterns.
This is defense in depth, not a replacement for a corpus-level secret scanner
and human privacy review before Phase 8B.

The existing `trusted_review/` records cannot be silently repurposed here. They
have auditable labels and hashes but intentionally omit full candidate text,
evidence, and tool summaries. Reconstructing those inputs requires a separately
authorized, reproducible collection phase.

## Leakage rules

The split unit is the complete repository. Every repository occurs in exactly
one of `train`, `validation`, or `test`; every dataset repository must appear in
the manifest. Validation also checks cross-split collisions for:

- candidate identity;
- change identity;
- pair/group identity;
- canonical candidate/evidence/tool content hash;
- complete record hash.

The content-hash check catches renamed or copied examples even when their
metadata differs. Row-level random splitting is prohibited.

`uncertain` rows remain in the dataset and report support. Primary binary
metrics exclude them rather than coercing them into keep or drop. A future
three-way experiment must be declared as a distinct protocol.

## Metrics and threshold discipline

Scores are finite probabilities in `[0, 1]`; larger means more likely to keep.
The validation-set threshold selector maximizes F1 and resolves ties by recall,
then precision, then the higher threshold. A test report uses the threshold
already frozen in the split manifest or an explicit CLI threshold whose source
is recorded. It never searches test labels for the best threshold.

Reports contain:

- micro confusion counts, Precision, Recall, and F1;
- per-repository metrics and macro means over defined repository values;
- tied-score PR points and average precision using step interpolation;
- equal-width calibration bins and ECE;
- total, binary, and excluded-uncertain support;
- mean/max recorded inference latency;
- compact false-positive/false-negative slices for severity, language, missing
  evidence, and tool errors.

Undefined denominators serialize as `null`, not `0`. This matters for a
repository containing no positive labels or no predicted positives.

## Offline commands

Validate the synthetic protocol fixture:

```powershell
.venv\Scripts\python.exe verifier_training.py validate `
  --data verifier_training\examples\candidates.jsonl `
  --splits verifier_training\examples\split-manifest.json
```

Evaluate the fixed synthetic predictions with the manifest's validation-frozen
threshold:

```powershell
.venv\Scripts\python.exe verifier_training.py evaluate `
  --data verifier_training\examples\candidates.jsonl `
  --splits verifier_training\examples\split-manifest.json `
  --predictions verifier_training\examples\predictions.jsonl `
  --split test
```

Exercise the dependency-free pointwise baseline and write auditable artifacts:

```powershell
.venv\Scripts\python.exe verifier_training.py train `
  --data verifier_training\examples\candidates.jsonl `
  --splits verifier_training\examples\split-manifest.json `
  --algorithm logreg `
  --experiment-id synthetic-logreg `
  --model-out tmp\synthetic-logreg.json `
  --predictions-out tmp\synthetic-logreg-predictions.jsonl
```

Use `--algorithm pairwise` for the pairwise lexical ranker. Both algorithms use
stable SHA-256 feature hashing and seed-controlled example order. They are
pipeline probes, not substitutes for the Base/LoRA/SFT/preference experiments.

## Before real training

The Phase 8C amendment allowed one exact local model snapshot and an isolated
CPU runtime for the synthetic smoke experiment. It did not authorize a dataset
download, API Verifier call, accelerator, upload, product integration, or
real-corpus training. Before a real experiment, a follow-up freeze must record:

1. repository permissions, licenses, revisions, retention, and secret scan;
2. independent annotation/adjudication protocol and quality thresholds;
3. repository split and immutable dataset hashes;
4. exact base model/tokenizer revisions and framework lock;
5. seeds, prompt template hash, hyperparameters, and quantization;
6. maximum storage, tokens, accelerator hours, monetary cost, and stop rules;
7. identical held-out records for Base, LoRA/SFT, preference/ranking, and any
   paid API reference;
8. report template for calibration, threshold sensitivity, cross-repository
   generalization, cost/latency, ablation, and qualitative errors.

`eval/` and `eval/holdout/` are never training sources. Loading a trained model
into the review product is a later deployment phase with separate compatibility,
security, latency, fallback, and rollback acceptance gates.
