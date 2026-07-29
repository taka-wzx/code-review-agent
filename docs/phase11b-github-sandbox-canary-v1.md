# Phase 11B-Prep: GitHub Sandbox Publisher Canary v1

## Current status

Phase 11B is implemented and tested **offline only** on top of the Phase 11A merge
`010537202ca653d40dc6da2464482cf191aba401`. Phase 11B has not been pushed, and no
real GitHub canary has run. The offline results are
engineering evidence, not a Business Pilot, model-quality result, production writer,
or readiness proof.

Phase 11A's verified green master CI run is `30419658651`. The rebased Phase 11B
task branch is `codex/phase11b-github-sandbox-canary-v1`; its executable publisher
commit is `81c8f528a8fffd7d9120323bb97d0657dec95862`, and its current local tip
before this report update is `c1f14781676aac9522542e1d12fc5a6c38ac5c9d`.

## Preparation authorization table

This table is the locally completed **Phase 11B-Prep** record. It is not a
schema-valid real-canary authorization object and does not open the GitHub write
gate. Values marked `PENDING` cannot be safely inferred from the local repository
and must be supplied by the repository owner before any real mutation.

| Field | Value | Status |
| --- | --- | --- |
| Phase 11A merge SHA | `010537202ca653d40dc6da2464482cf191aba401` | Filled and verified |
| Phase 11A green master CI | `30419658651` | Filled and verified |
| Phase 11B branch | `codex/phase11b-github-sandbox-canary-v1` | Filled locally; not pushed |
| Phase 11B executable code commit | `81c8f528a8fffd7d9120323bb97d0657dec95862` | Filled locally |
| Phase 11B green Draft PR CI | `PENDING` | Requires an explicitly authorized push and CI run |
| Runtime config SHA-256 | `PENDING` | No real-canary runtime config exists locally |
| Image digest | `PENDING` | No real-canary image was built or published |
| Base branch / base SHA | `master` / `010537202ca653d40dc6da2464482cf191aba401` | Branch filled; exact canary base must be frozen later |
| Case denominator | `3` | Filled: `normal`, `crash_after_branch`, `crash_after_draft_pr` |
| Cost ceiling | `0 CNY` incremental | Filled |
| GitHub App / installation / account IDs | `PENDING` | Must be supplied from GitHub configuration |
| Disposable sandbox repository owner/name/immutable ID | `PENDING` | Must be created and verified by owner |
| Exact base-tree, branch, object, marker, payload, test, budget, checkpoint and commit-message hashes | `PENDING` | Must be generated after the exact sandbox and payload are frozen |
| Authorization ID / canonical SHA-256 / time window | `PENDING` | Must be issued after all exact bindings are known |
| Token injection / expiry / revocation procedure | `PENDING` | Must be approved operationally; no token was accessed |
| Runtime host / egress / TLS policy | `PENDING` | Must be approved for the real transport |
| Authorization / revocation / kill-switch / incident / cleanup owners | `PENDING` | Named owners are not present in repository evidence |

The publisher therefore remains fail-closed. No GitHub App credential, private key,
installation token, repository alias, or real endpoint was accessed during this prep.

The auth-004 evidence remains permanently unchanged: five selected/headline attempts,
zero completed, five failed, stable category
`provider_or_pipeline_RuntimeError × 5`, and root cause `unknown`.

## What the offline implementation adds

- `GitHubDraftPrPublisher` remains disabled when constructed without a complete gate.
  The Phase 10 fake/dry-run behavior is unchanged.
- The only configured write backend is the GitHub REST Git Database API. There is no
  Contents API backend, Git/gh subprocess, PAT fallback, credential-helper lookup, or
  merge/Ready/comment/review/label/Check/status/cleanup method.
- A strict canonical authorization binds one immutable sandbox repository, GitHub App
  and installation identity, frozen base, exactly three canary cases/branches, code and
  runtime-config digests, UTC window, owners, and request/mutation/read/cost budgets.
- Two one-use canary approval envelopes bind the exact publication after its commit SHA
  is known. Only a same-organization human maintainer or org-admin can consume them.
- Additive migration `0008_phase11b_github_canary` adds hash-only approval, publication,
  request-ledger, and receipt state. API/worker startup still never runs DDL.
- Every mutation is durably reserved before transport use. Existing unresolved
  mutation records are read-back only; they are never resent. Exact blob/tree/commit,
  ref, and Draft PR recovery is exercised with deterministic fakes.
- The HTTPS transport pins `https://api.github.com`, normal CA verification, a frozen
  endpoint/method allowlist, disabled redirect following, bounded timeout/response
  size, and response projection that discards blob/tree contents.

Ordinary tests inject an in-memory recording transport and a synthetic token provider.
They do not open a socket, inspect Git credentials, call a model, or read a real
repository. Their receipts keep `real_github_sandbox_writes=false`; only the separately
authorized real-transport gate may set that field to `true`.

## Durable sequence

The publication outbox state is monotonic:

```text
publish_intent_recorded
  -> branch_push_requested
  -> branch_push_observed
  -> draft_pr_requested
  -> draft_pr_observed
  -> receipt_reconciled
```

Any unresolvable result becomes `quarantined`. A quarantined intent cannot be replaced
or automatically retried.

Before the first transport call the publisher verifies:

1. feature gate enabled and every constructor dependency supplied;
2. canonical authorization SHA, code SHA, runtime-config SHA, exact base-tree SHA, and UTC window;
3. exact owner/name/immutable repository ID in the in-process allowlist;
4. exact case ID and case branch with the `crag-canary/` prefix;
5. Repair job organization/repository/base/checkpoint/diff/test/budget lineage;
6. consumed, hash-matching WRITE and DRAFT_PR approval envelopes;
7. short-lived token App/installation/account identity, expiry, and revocation;
8. durable request, mutation, read, branch, commit, and Draft PR budgets.

It then reads repository metadata and the exact base ref. A base drift, repository
mismatch, protected/default head, installation mismatch, expired/revoked credential,
or any binding drift stops before mutation.

Git object creation is ordered blob -> tree -> commit -> exact ref. A request record is
committed before each operation. If a process dies after GitHub accepted a content-
addressed object but before the local receipt, restart reads the exact object SHA. If it
dies after ref creation, restart reads the exact ref and compares the exact commit. An
existing ref at another commit is `ref_collision`; a missing result after an unresolved
ref mutation is `ambiguous_result` and quarantines.

Draft PR creation always sends `draft=true` and `maintainer_can_modify=false`. An
unresolved result is reconciled by an exact head/base query only. One candidate must
match exact head ref/SHA, base, Draft state, title hash, body hash, and stable marker.
Zero candidates is ambiguous; multiple candidates or mismatched content quarantines.
The create request is never resent.

## Receipt and disclosure boundary

A successful future real sandbox receipt must contain:

```text
environment=github_sandbox_canary
synthetic_input_only=true
real_github_sandbox_writes=true
real_model_calls=false
real_business_repository_writes=false
business_claim_allowed=false
quality_claim_allowed=false
production_ready=false
```

The durable ledger and receipt store only stable enums, counts, HTTP status, endpoint
enum, object SHA, and SHA-256 values. They exclude token/private key, Authorization
header, exception message, response body, diff/patch, PR body/title, commit message,
identity, owner/repository alias, branch/path, sensitive query, and host path. The raw
synthetic blob/title/body/commit message exist only in the in-memory exact request long
enough to build the authorized transport payload.

## Second authorization gate for a real canary

No real operation may begin until the repository owner supplies a new, complete,
time-bounded authorization record containing all of the following:

- actual Phase 11A merge SHA and green master CI run;
- rebased Phase 11B executable code SHA, green Draft PR CI, image digest, and runtime
  config SHA-256;
- authorization ID, canonical SHA-256, issued/not-before/expires times, authorization
  owner, revocation owner, and kill-switch owner;
- GitHub App ID, installation ID, installation account ID, and a unique disposable
  repository owner/name plus immutable repository ID and data classification;
- allowed base branch/frozen base SHA/base-tree SHA; exact three head branches; exact synthetic diff,
  blob/tree/commit, test, budget, checkpoint, commit-message, title/body marker, and
  publisher-payload hashes;
- branch/commit/Draft-PR maximums no greater than three, plus exact mutation/read/total
  request caps, per-request timeout, total window, and retry/backoff caps;
- incremental cost ceiling CNY 0;
- short-lived installation-token injection, expiry, and revocation procedures;
- runtime host, egress allowlist, TLS/CA/redirect policy, incident owner, and
  cleanup/retention owner.

Any blank, mismatched, expired, unverified, or changed field keeps the publisher closed.
The repository delivery credential and product-side GitHub App credential must be
different and isolated. No private key is placed in runtime without separate approval.

## Frozen canary cases

If the second gate is later approved, the denominator is frozen before execution:

```text
planned=3
denominator=3
```

The cases run at most once each and use different idempotency keys and branches:

1. `normal`: exact objects/ref -> Draft PR -> read-back -> redacted receipt.
2. `crash_after_branch`: deterministic stop after GitHub accepts the ref and before the
   local ref receipt; restart may only read and reconcile.
3. `crash_after_draft_pr`: deterministic stop after GitHub accepts the Draft PR and
   before the local PR receipt; restart may only list/read and reconcile.

Each final case status is one of `succeeded`, `failed`, `quarantined`, or
`not_run_gate_blocked`. Failure or quarantine remains in the denominator and is never
replaced. Any unexpected repo, installation, permission, base, branch, credential,
budget, window, or network result stops remaining mutation.

No cleanup is implied. Branches and Draft PRs remain until a separate owner-approved
cleanup window; the service has no delete/close method.

## Phase 11A merge integration

The required integration is complete. Local `master` and `origin/master` both point to
`010537202ca653d40dc6da2464482cf191aba401`, the Phase 11A master CI run is
`30419658651`, and the Phase 11B branch was rebased onto that merge before validation.
The changed-file set remains exactly the seven paths declared by the Single Writer
section. The executable publisher commit is
`81c8f528a8fffd7d9120323bb97d0657dec95862`.

Offline validation completed on the rebased tree:

| Command | Result |
| --- | --- |
| `python -m unittest -v tests.test_phase11b_github_sandbox_canary` | 20 tests passed; 1 skipped |
| `python -m unittest -v tests.test_phase11a_synthetic_staging` | Passed |
| `python -m unittest discover -s tests` | 923 tests passed; 18 skipped |
| `python -m ruff check .` | Passed |
| `python -m mypy src/code_review_agent` | Passed; 36 source files |
| `python scripts/verify.py` | Passed; offline verification valid |
| `python -m pip check` | Passed; no broken requirements |
| `git diff --check` | Passed |

Docker/Postgres integration was not used for this offline prep and is not canary
evidence. The results above authorize only local Phase 11B-Prep delivery; they do not
authorize a task-branch push, Draft PR creation, or real GitHub canary. Any Phase 11A
executable change requires a fresh integration review.
