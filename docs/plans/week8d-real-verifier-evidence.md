# Phase 8D: Real Verifier Evidence Preparation

## Goal

Prepare the real-data Finder, independent annotation, adjudication, freeze, and
model-run interfaces without making an external model call or claiming that any
human work has occurred. This phase closes offline protocol gaps exposed after
the Phase 8C synthetic smoke experiment.

The deliverable is a fail-closed operator workflow. It is not a real corpus,
human annotation, trained-model result, API comparison, or production rollout.

## Base and current branch

- Phase 8 base commit: `ae4a7ffd307072fa1ddadff4c82a96f9c170d847`
- Current task branch: `codex/week8-verifier-training`
- Worktree: the existing isolated Week 8 worktree
- Stable Phase 8 commit: not authorized or created

The Phase 8A--8C changes remain uncommitted. This continuation therefore stays
on their current task branch rather than pretending an uncommitted state is a
stable Phase 8D base.

## Authorization freeze (2026-07-22)

The user's instruction authorizes only repository-local Phase 8D contracts,
schemas, examples, validators, deterministic packet generation, and tests.

Until a later explicit amendment, all of the following remain unauthorized:

- provider/API calls and network access;
- reading the retained 29 raw unified diffs;
- nonzero model-call, token, or monetary budgets;
- real annotator or adjudicator identity assignment;
- a raw-trace retention period;
- real model training or protected test-label opening;
- a local commit, push, pull request, merge, upload, or production integration.

The machine-readable config must encode those missing decisions as `null`,
`false`, or zero. Offline tools must reject any expansion.

## Offline workflow

1. Validate the frozen authorization config.
2. Validate the 29 immutable Finder queue records and generate deterministic
   execution envelopes without reading their diff objects.
3. Import Finder run receipts and candidate records produced by a future,
   separately authorized executor. A successful run may legitimately contain
   zero candidates; it receives a completed-zero receipt and must not be turned
   into a fabricated negative candidate.
4. Export two blinded independent annotation packets. Packets omit split names,
   peer labels, model scores, and model predictions.
5. Import each reviewer's complete response set and compute canonical annotation
   hashes in tooling rather than asking humans to calculate them.
6. Export an adjudication packet only for disagreement or any `uncertain` vote;
   import decisions from a third distinct identity and bind both source hashes.
7. Merge annotations and build a real freeze wrapper that binds Finder receipts
   to the existing corpus compiler.
8. Validate a real-model run plan. Actual training remains disabled until the
   real freeze is trainable and model-run seeds/resources are separately frozen.

## Frozen invariants

- One Finder receipt binds each of the 29 queue records exactly once.
- `completed` requires one or more candidate IDs; `completed_zero_candidates`
  requires none; `failed` requires a bounded error category.
- Candidates bind the queue source, PR source hash, merge SHA, and diff hash;
  candidate counts agree with their receipt and remain within 16 per PR / 480
  total.
- A completed-zero receipt closes execution completeness without creating a
  training row.
- Independent packets use distinct reviewer IDs and never include peer labels.
- Response import requires exactly one decision for every packet item and no
  extra candidate IDs.
- Agreement on a non-uncertain label forbids adjudication. Disagreement or any
  uncertain vote requires exactly one distinct adjudicator.
- Real records use `synthetic=false`; synthetic fixtures cannot open the real
  trainable gate.
- Whole-repository train/validation/test isolation remains unchanged. Test
  labels remain sealed from model selection, hyperparameter changes, threshold
  selection, and error-driven iteration.

## Single Writer ownership

Codex may create or modify only:

- `docs/plans/week8d-real-verifier-evidence.md`
- `docs/verifier-real-evidence.md`
- `verifier_phase8d.py`
- `tests/test_verifier_phase8d.py`
- `tests/test_verifier_training.py` only to extend the exact schema inventory
  with the four declared Phase 8D schemas
- `verifier_training/phase8d-config.json`
- `verifier_training/schemas/finder-run.schema.json`
- `verifier_training/schemas/annotation-packet.schema.json`
- `verifier_training/schemas/annotation-response.schema.json`
- `verifier_training/schemas/real-freeze-manifest.schema.json`
- `verifier_training/examples/phase8d/README.md`
- `verifier_corpus.py` only for the backward-compatible completed-source input
  used by the Phase 8D real freeze wrapper
- `tests/test_verifier_corpus.py` only for that compatibility gate
- `README.md`
- `AGENDA.md`

All provider adapters, production package files, dependencies, CI, prompts,
sentinels, protected evaluation assets, raw diff objects, and prior result
artifacts are read-only.

## Validation

```powershell
.venv\Scripts\python.exe -m unittest tests.test_verifier_phase8d tests.test_verifier_corpus -v
.venv\Scripts\python.exe -m ruff check verifier_phase8d.py verifier_corpus.py `
  tests\test_verifier_phase8d.py tests\test_verifier_corpus.py
.venv\Scripts\python.exe verifier_phase8d.py validate-config `
  --config verifier_training\phase8d-config.json
.venv\Scripts\python.exe scripts\verify.py
git diff --check
```

The protected evaluation consistency command is deliberately omitted. No
command in this phase may read `eval/` or `eval/holdout/`.

## Acceptance criteria

- The offline config rejects nonzero provider authority, raw-diff authority,
  assigned humans, retention, or commit authority.
- Finder envelopes and receipts are deterministic, exact-key, hash-bound, and
  support honest zero-candidate completion.
- Receipt/candidate reconciliation rejects missing, duplicate, foreign,
  over-limit, failed-as-complete, or count-mismatched records.
- Independent packet export is blind and deterministic under a seed.
- Annotation and adjudication imports enforce exact coverage, distinct human
  identities, immutable candidate/evidence hashes, and source-hash freshness.
- Real freeze output binds Finder receipts and cannot report trainable while a
  run failed, annotations are unresolved, synthetic records remain, or a
  represented repository is missing.
- Real model-plan validation refuses to run without a trainable real freeze,
  sealed test declaration, frozen seeds, and a later model-run authorization.
- Existing Phase 8A--8C and project validation remain green.

## Remaining decisions for the next amendment

The user must explicitly provide the Finder provider and exact model, maximum
calls/input tokens/output tokens/cost, raw-diff read authority, raw-trace
retention, two annotator IDs and a distinct adjudicator ID, and local commit
authority. No default is inferred for any of them.

## Delivery report (2026-07-22)

- Implemented the zero-authority config, 29 deterministic non-executable Finder
  envelopes, completed/zero/failed receipt validation, blind independent packet
  export, complete response import, adjudication-only packet export, annotation
  merge, Finder-bound real freeze wrapper, and blocked real-model readiness gate.
- Added an optional completed-source input to the existing corpus compiler.
  Its default behavior is unchanged; a Phase 8D completed-zero receipt may now
  close execution completeness without fabricating a candidate.
- Focused Phase 8A--8D validation passed 39 tests. Targeted Ruff, config CLI,
  and a real 29-envelope preparation smoke passed with `executable=0`.
- Full `scripts/verify.py` passed 632 tests with 3 skips, 86% coverage, Ruff,
  mypy for 26 package files, both CLI smokes, and all 48 security cases at zero
  attack success, false block, secret disclosure, or unauthorized execution.
- `git diff --check`, JSON parsing, trailing-whitespace, absolute-host-path, and
  credential-pattern audits passed. The full suite's generated `%SystemDrive%`
  cache directory was inspected and removed from the worktree.
- The first combined focused run exposed only an intentionally exact schema
  inventory that lacked the four new filenames. Ownership was explicitly
  expanded, the inventory was updated, and the authoritative rerun passed.
- No provider/network call, raw diff read, human decision, model training,
  protected evaluation access, commit, push, merge, or upload occurred.
