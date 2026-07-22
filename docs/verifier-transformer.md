# Phase 8C Transformer Smoke Experiments

Phase 8C proves that the frozen Verifier protocol can drive four local,
hash-bound Transformer experiment paths under one test manifest. The run is a
synthetic pipeline smoke test. It does not establish model quality, training
benefit, or cross-repository generalization.

The controlling contract is
[`docs/plans/week8-verifier-training.md`](plans/week8-verifier-training.md).
The frozen machine-readable inputs are `verifier_training/phase8c-config.json`,
`verifier_training/phase8c-runtime.lock`, and
`verifier_training/model-snapshot.json`.

## Frozen model and runtime

- Model: `google/bert_uncased_L-2_H-128_A-2`, Apache-2.0.
- Revision: `30b0a37ccaaa32f332884b96992754e246e48c5f`.
- Format: safetensors only; `model.safetensors` is 17,739,144 bytes with
  SHA-256 `7fb69ad9f6866d8983183c930e33828f326470bf6ad8bbb2ad4ed957a92e9414`.
- Runtime: CPython 3.13.0, PyTorch 2.13.0 CPU, Transformers 5.13.0, and
  PEFT 0.19.1. The complete resolved environment is pinned in the runtime lock.
- Bounds: four CPU threads, two CPU-hours, 2 GiB runtime storage, 64 MiB model
  download, zero accelerator-hours, and CNY 0.

The environment and model live under ignored `traces/week8c-runtime/`. The
runner verifies the declared model-file hashes, refuses unsafe weight formats,
forces local loading, and imports the training stack lazily so the installable
review-agent package gains no dependency.

The pretrained BERT snapshot has no task-specific sequence-classification
head. Creating that head locally produces the expected missing-head warning;
its deterministic initialized state is included in each experiment's model
state hash.

## Frozen experiments

All paths use seed `20260719`, maximum sequence length 256, the same synthetic
train/validation/test records, validation-only threshold selection, and the
same six scored candidates. The complete config hash is
`1f59207e9f90b66e5cac9d121a15b82c08e33057b5afabadf7554aeaff55bc0a`;
the literal input-template hash is
`6b9a230f7fc52580101fb28d0385420cffed081f50908a97fa4ba6c7a69e6742`:

| Path | Training | Trainable parameters |
| --- | --- | ---: |
| Base | no optimizer step | 4,386,178 |
| Full SFT | 30 epochs, learning rate 0.0002 | 4,386,178 |
| LoRA SFT | 50 epochs, rank 4, alpha 8, learning rate 0.0005 | 4,354 |
| LoRA pairwise | 80 epochs, pairwise ranking loss, same LoRA shape | 4,354 |

The Base path is an initialized classification probe, not a pretrained
Verifier. The pairwise path uses a ranking loss over the protocol's paired
keep/drop records; it is not DPO over generated language.

## Recorded smoke result

The committed result family binds dataset SHA-256
`4a7f1f4a9c0e6ff52b781ca5a31d30c6af7a484a984c4e695d5962e20344e8e8`.
Its comparison manifest is
`aa7fa3b40394d8783cc40fb8c716f2aee1d9ad098db373305533f5362eedc6bc`.

| Path | Frozen threshold | Test TP/TN/FP/FN | Precision | Recall | F1 | Wall time |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| Base | 0.51771283 | 1/1/0/0 | 1.0 | 1.0 | 1.0 | 0.092 s |
| Full SFT | 0.81725836 | 0/1/0/1 | undefined | 0.0 | undefined | 1.356 s |
| LoRA SFT | 0.56504530 | 0/1/0/1 | undefined | 0.0 | undefined | 1.091 s |
| LoRA pairwise | 0.66155344 | 0/1/0/1 | undefined | 0.0 | undefined | 1.739 s |

There are only two binary test examples. Base happened to classify both
constructed records correctly while all trained paths missed the one positive.
This is tiny-sample variance, not evidence that Base is better or post-training
is harmful. Every report therefore freezes `synthetic_only=true` and
`quality_claim_allowed=false`. Full PR/calibration metrics, predictions,
latencies, resource records, state hashes, and qualitative error slices remain
in `verifier_training/examples/phase8c/`.

The ignored runtime occupied 857,461,423 bytes after the run, below the 2 GiB
ceiling. Each report also records wall time and a conservative four-thread
CPU-seconds upper bound. No accelerator, provider API, dataset hub, upload,
protected evaluation asset, or product integration was used.

## Reproduction and validation

Contract validation uses only the normal project environment and does not load
PyTorch:

```powershell
.venv\Scripts\python.exe verifier_transformer.py validate `
  --config verifier_training\phase8c-config.json `
  --model-snapshot verifier_training\model-snapshot.json
```

After independently recreating the exact ignored runtime and verified model
snapshot, run offline into an ignored output directory first:

```powershell
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"
$env:HF_DATASETS_OFFLINE = "1"
$env:TOKENIZERS_PARALLELISM = "false"
traces\week8c-runtime\.venv\Scripts\python.exe verifier_transformer.py run `
  --config verifier_training\phase8c-config.json `
  --output-dir traces\week8c-runtime\results-recheck `
  --run-at 2026-07-19T12:00:00Z
```

Wall time, RSS, and per-example latency naturally vary. Dataset, model snapshot,
configuration, predictions, model-state hashes, and semantic metrics are the
reproducibility evidence; a newly generated manifest also changes when measured
resource values change.

## Remaining real-data gates

The real 29-PR source snapshot now has a deterministic pending Finder queue in
`verifier_training/corpus-snapshot/finder-queue.jsonl`. Week 8 cannot truthfully
complete the model-quality objective until an authorized Finder produces
bounded candidates for those sources, two distinct humans independently label
each candidate, and an independent adjudicator resolves every disagreement or
`uncertain` label. Only then may a new immutable corpus freeze replace the
synthetic manifest and enable a separately reviewed real-data run.

An API Verifier comparison, trained-model production integration, upload,
accelerator experiment, and deployment claim remain explicitly out of scope.
