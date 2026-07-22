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
- Stable Phase 8 commit: `c96ef4e8364f4f5c9ce03a04d5d98761db2957f9`
- Stable GLM amendment commit: `b10a280def80e5c4713918e0706063840a6aa9f5`

The Phase 8A--8D offline preparation is committed at the stable commit above.
This authorized provider amendment remains on the same isolated task branch.

## Authorization freeze (2026-07-22, GLM amendment)

The user authorized the following exact Finder execution boundary:

- provider `glm`, OpenAI-compatible base URL
  `https://open.bigmodel.cn/api/paas/v4`, API model ID `glm-5.2`;
- anchor temperature `0.20`, sampling temperature `0.70`, thinking disabled,
  reasoning effort `none`, non-streaming responses, and automatic tool choice;
- at most 580 logical calls, 1,740 theoretical HTTP attempts including the
  client's two retries, 20,000,000 input tokens, 2,000,000 output tokens, and
  CNY 250 total cost;
- read access only to the 29 hash-bound unified-diff objects selected in Phase
  8B; raw diff and provider trace retention is 30 days;
- stable IDs `human-reviewer-a-v1`, `human-reviewer-b-v1`, and
  `human-adjudicator-c-v1`, which must map to three distinct real people;
- local commits, task-branch push, pull request, merge, and `master` changes.

The API model ID is a service alias, not an immutable weight snapshot. Each
response must therefore preserve the returned model ID, request ID, UTC time,
system fingerprint when supplied, and the frozen documentation/input hashes.
The executor must not run without an explicit environment credential and must
fail closed before exceeding any ceiling. No credential may enter a trace.

This amendment does **not** authorize real model training, opening protected
test labels, fabricating human decisions, or claiming model quality. A merge to
`master` is permitted by the user but remains inappropriate until the three
real-human decisions and real quality gates are complete.

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
- `verifier_phase8d_glm.py`
- `tests/test_verifier_phase8d_glm.py`

All other provider adapters, production package files, dependencies, CI, prompts,
sentinels, protected evaluation assets, raw diff objects, and prior result
artifacts are read-only.

## Validation

```powershell
.venv\Scripts\python.exe -m unittest tests.test_verifier_phase8d tests.test_verifier_corpus -v
.venv\Scripts\python.exe -m unittest tests.test_verifier_phase8d_glm -v
.venv\Scripts\python.exe -m ruff check verifier_phase8d.py verifier_corpus.py `
  verifier_phase8d_glm.py tests\test_verifier_phase8d.py `
  tests\test_verifier_phase8d_glm.py tests\test_verifier_corpus.py
.venv\Scripts\python.exe verifier_phase8d.py validate-config `
  --config verifier_training\phase8d-config.json
.venv\Scripts\python.exe scripts\verify.py
git diff --check
```

The protected evaluation consistency command is deliberately omitted. No
command in this phase may read `eval/` or `eval/holdout/`.

## Acceptance criteria

- The amended config accepts exactly the authorized GLM identity, generation
  settings, budgets, retained diff boundary, human IDs, and local commit
  authority, and rejects any expansion or weakening.
- The provider executor is offline-testable with an injected fake client,
  verifies every raw object's path, size, and SHA-256 before reading it into a
  request, and emits no replacement or fabricated run.
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

## Remaining external gates

- A `GLM_API_KEY` or `ZHIPUAI_API_KEY` must be supplied by the operator without
  committing or logging it.
- The two independent reviewers and the adjudicator must be three real people;
  their labels cannot be generated by an agent.
- Real-model training resources/seeds require a separate authorization after a
  trainable, real-only corpus freeze exists.

## Delivery report (2026-07-22)

### GLM amendment

- Froze the exact GLM-5.2 identity, two temperatures, reasoning/thinking
  controls, logical/HTTP attempt ceilings, token and CNY budgets, 30-day raw
  retention, three stable human IDs, and local-commit authority.
- Added an offline-testable provider executor with conservative pre-request
  budget reservation, exact-path/size/SHA diff attestation, immutable-output
  refusal, honest diff-only tool responses, deterministic candidate IDs, and
  response model/request/fingerprint evidence.
- Attested all 29 retained diff objects (145,838 bytes) with aggregate
  attestation SHA-256
  `6d6ca6470d7b345866eb92ec96bdf7af0ffccf65cabaed1664369a98f675fb04`.
- Focused validation passed 20 tests. Full `scripts/verify.py` passed 635 tests
  with 3 skips, 86% coverage, Ruff, mypy, CLI smokes, and the 48-case security
  suite at zero attack success, false block, secret disclosure, or
  unauthorized execution.
- The shared venv initially lacked locked FastAPI/MCP dependencies and pointed
  its editable install at an older worktree. Restoring the locked packages and
  reinstalling this worktree editable fixed the environment; no dependency or
  lock file changed.
- No provider call occurred because neither accepted GLM key environment
  variable is present. No human label, adjudication, model training, protected
  evaluation read, quality claim, merge, or `master` change occurred. The task
  branch was pushed and draft PR #4 was opened for review.

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
