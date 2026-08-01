# Phase 11C Gate B Headline Cohort Executor v1

Status: **offline implementation and fake-only validation complete; no new provider
dispatch, credential read, image build, ECS mutation, push, or PR action is authorized
by this task contract**

## Identity and authority

- Task branch: `codex/phase11c-gateb-headline-cohort-executor-v1`
- Task worktree: `.codex-worktrees/phase11c-gateb-headline-cohort-executor-v1`
- Base executable commit: `470b499253a659b492c7ca68fa290d9b0ea3e049`
- Accountable owner and kill-switch operator: `taka-wzx`
- Aggregate hard ceiling: `15,000,000` micro-CNY
- Historical prior-image DIAGNOSTIC safe receipt SHA-256:
  `6ff641016ca966b933f7c12e8d968b09c3962dc0b959b79434be929530055959`
- Historical prior-image DIAGNOSTIC reservation: `19,584` micro-CNY; remaining aggregate ceiling:
  `14,980,416` micro-CNY

The original Phase 11C contract requires a second, independently bound
`HEADLINE_COHORT` decision after a successful deterministic synthetic DIAGNOSTIC.
The prior DIAGNOSTIC is valid historical provider-connectivity evidence, but it was
executed from a different immutable image. Because this task creates new executable
code and therefore a new image/configuration, that receipt and its approval are not
eligible to authorize the new image. The final executor must first implement and
pass a fresh, exact `DIAGNOSTIC` approval/run using the same frozen executable tree,
image, runtime, credential binding, and policy/tariff evidence that will later be
used by the headline cohort. Only its new sanitized receipt may enter the subsequent
`HEADLINE_COHORT` binding.

This task may only create and test the combined separate executor, schema,
deployment definition, and candidate binding mechanism. It must not read a
credential, open a provider socket, create an ECS resource, build an ECS image, push
a branch, or execute either stage until a later exact freeze and the applicable
one-use human approval.

## Implemented offline boundary

- `phase11c_gateb_headline_cohort_executor.py` provides separate `DIAGNOSTIC`
  and `HEADLINE_COHORT` commands in one final-image executable. Both use fixed
  direct TLS transport only in their live commands; tests inject fakes and never
  invoke the fixed credential reader or transport.
- The headline authorization explicitly binds the fresh diagnostic authorization
  SHA, diagnostic approval-binding SHA, diagnostic receipt SHA, three ordered
  target IDs/payload hashes, the selected cohort manifest, exact same-image
  freeze fields, tariff/policy/credential hashes, remaining-budget values, and
  a sealed stop-policy hash.
- The executor regenerates each public deterministic synthetic target by its
  fixed Gate A slot and rejects a hash/ID mismatch before it creates a request.
  Provider-supplied tool arguments are parsed, hashed, checked, and replaced by
  locally canonical arguments for the continuation; they are never retained.
- Every live attempt writes a sealed state transition before network I/O. A
  partial, prior, or corrupt state/target/ledger artifact makes the matching
  stage fail closed with a `*_quarantined` code and zero further provider calls.
  There is no automatic retry or recovery of an in-flight target.
- A headline credential failure still produces target 1's safe terminal receipt
  and two `not_run_gate_blocked` receipts. Therefore the three-target denominator
  is never dropped merely because a run cannot begin.
- The final cohort receipt and ledger are separately self-sealed. The ledger
  binds the cohort receipt, all three target-receipt hashes, authorization and
  diagnostic lineage, reservation accounting, and redaction state.

The fixed public endpoint, mount locations, deterministic synthetic wording, and
tool names are compiled protocol constants necessary to reproduce the canary. The
no-retention rule applies to provider-generated content, API keys, authorization
headers, host/runtime observations, tool call IDs/arguments, exception text, and all
other run-specific raw values; none may be written to receipts, state, ledger, logs,
Git evidence, or chat.

## Complete Single Writer declaration

Codex is the sole writer for exactly these new files:

- `docs/plans/phase11c-gateb-headline-cohort-executor-v1.md`;
- `phase11c_gateb_headline_cohort_executor.py`;
- `schemas/phase11c-gateb-protocol-diagnostic-authorization.schema.json`;
- `schemas/phase11c-gateb-protocol-diagnostic-receipt.schema.json`;
- `schemas/phase11c-gateb-headline-cohort-authorization.schema.json`;
- `schemas/phase11c-gateb-headline-cohort-receipt.schema.json`;
- `schemas/phase11c-gateb-headline-cohort-ledger.schema.json`;
- `Dockerfile.phase11c-gateb-headline`;
- `compose.phase11c-gateb-headline.yml`;
- `tests/test_phase11c_gateb_headline_cohort_executor.py`.

Every other path is read-only. In particular, Phase 11B artifacts, the prior Gate A
executor, the prior DIAGNOSTIC executor and receipt, `src/`, public interfaces,
dependencies, lockfiles, workflows, migrations, product Compose files, historical
auth-004 artifacts, and every `eval/**` asset are prohibited.

## Frozen intended headline boundary

- Source kind: `deterministic_synthetic`; no business data, repository data, PR,
  GitHub API, publisher, tool side effect, or product runtime credential.
- Exact headline denominator: three of the existing five Gate A deterministic target
  identifiers, selected in stable manifest order. The DIAGNOSTIC is not in the
  denominator.
- The same final image exposes a minimal one-call `DIAGNOSTIC` stage and a separate
  headline stage. The diagnostic must complete before any headline binding exists.
- Each headline target sends at most two fixed non-streaming requests to the standard
  GLM endpoint: a fixed synthetic probe tool call followed by a local no-side-effect
  fake result and a fixed submit tool call. No redirect, proxy, SDK retry, transport
  retry, or concurrency greater than one is permitted.
- Each valid tool call must have the frozen target ID and frozen synthetic payload
  hash. Text, malformed, duplicate, unknown, ambiguous, or over-limit responses stop
  the remaining cohort and produce only safe enums, hashes, booleans, and counts.
- The response body, prompt, tool arguments, API key, authorization header, host
  path, exception message, and provider account data may exist only transiently in
  memory. They must never enter receipts, ledger, logs, traces, images, Git, or
  chat.
- Per request reservation is the existing conservative worst case of `19,584`
  micro-CNY (2,000 input tokens, 128 output tokens at the frozen tariff). The exact
  three-target headline reservation is `117,504` micro-CNY (six requests), and the
  fresh same-image diagnostic reservation is `19,584` micro-CNY. Any real execution
  must bind these lower caps and the remaining aggregate ceiling above.

## Required later freeze and approval

Before any diagnostic dispatch, an offline freeze must bind the current executable
and tree/image/deployment/runtime identities; provider policy and tariff evidence;
credential fingerprint; the diagnostic sub-cap; a future UTC window; and the final
sanitized preflight verdict. Its one-use approval is exactly
`APPROVE PHASE11C DIAGNOSTIC <binding_sha256>`.  The next stage may be prepared only
after that same-image diagnostic receipt completes successfully.

Before any headline dispatch, a new offline freeze must bind that new diagnostic
receipt SHA, the exact three-target cohort manifest and payload hashes, residual
aggregate budget, stop policy, and all final executable/image/deployment/runtime,
policy/tariff, credential, and window bindings. The executor must then print exactly:

```text
APPROVE PHASE11C HEADLINE_COHORT <approval_binding_sha256>
```

Only the human owner may provide that exact text. The approval is consumed before a
budget reservation, credential access, or any network I/O. A failed or incomplete
target is retained in the denominator; no target is retried, replaced, or silently
omitted.

The old one-shot image's approval, state volume, and credential are consumed evidence
only. They must not be reused. A later exact execution must use a new dedicated
credential path and a new state volume named
`phase11c-gateb-headline-cohort-state-v1`.

## Offline command interface

- `print-template` emits both safe authorization templates only; it neither reads a
  credential nor opens a socket.
- `seal-diagnostic-authorization` and `seal-headline-authorization` accept a
  canonical candidate from standard input. A seal requires a future UTC window of at
  most 30 minutes. Sealing a headline candidate alone cannot authorize execution;
  the later approval command independently verifies the completed same-image
  diagnostic lineage.
- `print-diagnostic-approval-binding` emits exactly one line:
  `APPROVE PHASE11C DIAGNOSTIC <binding_sha256>`.
- `print-headline-approval-binding` emits exactly one line:
  `APPROVE PHASE11C HEADLINE_COHORT <binding_sha256>`, but only after it validates
  the sealed fresh diagnostic authorization and completed receipt from the fixed
  state volume.
- `run-diagnostic` and `run-headline` are the sole live commands. A non-completed
  terminal receipt exits nonzero. The supplied Compose file contains no `build:`
  stanza and pins `pull_policy: never`; future deployment must use the exact local
  image ID and must not rebuild or pull during execution.

## Offline acceptance and validation

Tests use fakes only and may not read a credential or open a network socket. They
must cover canonical JSON/seals, both exact binding and window stages, target-manifest
selection, separate approvals, same-image diagnostic-to-headline linkage,
state/ledger monotonicity, two requests per target, stop-on-first-noncompleted
behavior, probe/submit validation, all safe terminal categories, budget reservation,
credential metadata/TOCTOU rejection, receipt redaction, and AST guards on live
transport/persistence primitives.

Required commands:

```powershell
$Python = (Resolve-Path '..\\..\\.venv\\Scripts\\python.exe').Path
$env:PYTHONPATH = (Resolve-Path 'src').Path
& $Python -m unittest -v tests.test_phase11c_gateb_headline_cohort_executor
& $Python phase11c_gateb_headline_cohort_executor.py print-template
& $Python -m unittest discover -s tests
& $Python -m ruff check .
& $Python -m mypy src/code_review_agent phase9g_pilot.py phase9g_solo.py phase9g_solo_run.py phase11c_gateb_headline_cohort_executor.py
& $Python scripts/verify.py
& $Python -m pip check
git diff --check
```

No validation command may read `eval/**`, a live credential, a provider API, a cloud
control plane, or a GitHub write API. Pushing, ECS mutation, image building, headline
approval, dispatch, credential revocation, final evidence commit, and final PR CI are
separate later actions.

## Delivery deviation record

During a final read-only file-list review, a path filter accidentally matched the
worktree's own name and enumerated `eval/**` file paths. No `eval/**` file was opened,
copied, executed, or modified, and no evaluation contents were emitted. This is a
process deviation from the no-enumeration rule and is recorded here so it cannot be
mistaken for permitted evaluation access.
