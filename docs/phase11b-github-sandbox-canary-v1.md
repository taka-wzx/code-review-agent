# Phase 11B-Prep: GitHub Sandbox Publisher Canary v1

## Current status

Phase 11B publisher and canary executor are implemented and tested **offline only** on
top of the Phase 11A merge `010537202ca653d40dc6da2464482cf191aba401`.
The executor is delivered by the consolidated task-branch commit whose exact SHA is
reported externally; no real GitHub canary has run. The offline results are engineering
evidence, not a Business Pilot,
model-quality result, production writer, or readiness proof.

Phase 11A's verified green master CI run is `30419658651`. The rebased Phase 11B
task branch is `codex/phase11b-github-sandbox-canary-v1`; the earlier publisher-only
commit is `81c8f528a8fffd7d9120323bb97d0657dec95862`. The exact executor code SHA is frozen
only after the consolidated commit is created and therefore is intentionally not
self-recorded in this commit's contents.

## Preparation authorization table

This table is the locally completed **Phase 11B-Prep** record. It is not a
schema-valid real-canary authorization object and does not open the GitHub write
gate. Values marked `PENDING` cannot be safely inferred from the local repository
and must be supplied by the repository owner before any real mutation.

| Field | Value | Status |
| --- | --- | --- |
| Phase 11A merge SHA | `010537202ca653d40dc6da2464482cf191aba401` | Filled and verified |
| Phase 11A green master CI | `30419658651` | Filled and verified |
| Phase 11B branch | `codex/phase11b-github-sandbox-canary-v1` | Exact push/CI status is external delivery evidence, not self-recorded |
| Phase 11B executable code commit | `PENDING CONSOLIDATED COMMIT` | Must bind the executor, not the earlier publisher-only commit |
| Phase 11B green Draft PR CI | `PENDING` | Requires an explicitly authorized push and CI run |
| Runtime config SHA-256 | `PENDING` | No real-canary runtime config exists locally |
| Image digest | `PENDING` | No real-canary image was built or published |
| Phase 11B source baseline | `origin/master` / `010537202ca653d40dc6da2464482cf191aba401` | Phase 11A merge baseline; separate from the sandbox base below |
| Case denominator | `3` | Filled: `normal`, `crash_after_branch`, `crash_after_draft_pr` |
| Cost ceiling | `0 CNY` incremental | Filled |
| GitHub App / installation / account IDs | `4421400` / `149747930` / candidate `186135139` | App and installation confirmed by owner; account ID must be re-read at final freeze |
| Disposable sandbox repository owner/name/immutable ID | `taka-wzx/crag-phase11b-sandbox` / `1315679182` | Public synthetic-only repository; immutable ID must be re-read at final freeze |
| Candidate sandbox base | `main` / `a50e171d093219b084d26c754a5386500039095f` / tree `61ed082498193a9fc3363db50229f2b1c85c4738` | Discovery value only; all three values and the complete non-recursive root manifest must be re-read immediately before authorization |
| Exact base-tree, branch, object, marker, payload, test, budget, checkpoint and commit-message hashes | `PENDING` | Must be generated after the exact sandbox and payload are frozen |
| Authorization ID / canonical SHA-256 / time window | `PENDING` | Must be issued after all exact bindings are known |
| Token injection / expiry / revocation procedure | Explicit absolute Linux JSON file; regular, no symlink, owner root/current runtime UID, mode `0600`, exact IDs, revoked flag, <=1 hour | Private key stays outside CRAG; operator replaces or removes the token file to revoke local use and revokes the installation token/App grant at GitHub when required |
| Runtime host / egress / TLS policy | Existing Aliyun ECS in `cn-hangzhou`; runtime identity is SHA-256-redacted in config; only `api.github.com:443`; normal CA verification; redirects disabled | No new server, database, registry, public listener, DNS, or TLS endpoint |
| Authorization / revocation / kill-switch / incident / cleanup owners | `taka-wzx` | Cleanup remains separately authorized; no automatic branch/PR deletion exists |

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
- A strict runtime-config schema binds the exact local image ID, source/deployment
  digests, hashed runtime identity, repository/App/installation identity, complete root
  base-tree manifest, three cases, zero-retry budget, egress/TLS policy, and all derived
  Git object and publication hashes. Unknown/duplicate fields fail closed.
- The executor recomputes Git blob, tree, and commit objects with Git's native object
  encoding. Synthetic files are top-level only, so a complete non-recursive root-tree
  manifest is enough to reproduce the exact tree that GitHub's `base_tree` operation
  must return.
- Approval decisions authenticate an existing CRAG bearer credential against current
  database membership. No CLI role, user, organization, or auth-method argument exists.
  Each invocation decides only one of the six exact approval envelopes.
- `run` executes or reconciles one case only. The two crash cases return exit code 75 at
  their deterministic first stop; rerunning the identical command performs read-back
  reconciliation and cannot resend the recorded mutation.

Ordinary tests inject an in-memory recording transport and a synthetic token provider.
They do not open a socket, inspect Git credentials, call a model, or read a real
repository. Their receipts keep `real_github_sandbox_writes=false`; only the separately
authorized real-transport gate may set that field to `true`.

## Executor entry points

The runtime config and canonical authorization are non-secret files. The database URL
and password continue to use the existing `CRAG_DATABASE_URL` /
`CRAG_DATABASE_URL_FILE` and `CRAG_DATABASE_PASSWORD_FILE` deployment boundary. The
executor verifies exact Alembic revision `0008_phase11b_github_canary`; it never runs a
migration.

On the Aliyun service image, invoke the installed module as
`python -m code_review_agent.github_canary_executor` with the same arguments shown
below. The root entrypoint accepts only the two additional **path** variables
`CRAG_CANARY_APPROVER_TOKEN_FILE` and `CRAG_CANARY_GITHUB_TOKEN_FILE`, copies their
root-owned `0600` source files into tmpfs, changes only the copies to UID 1000, and then
drops privileges. The explicit CLI paths inside the container are respectively
`/tmp/crag-secrets/CRAG_CANARY_APPROVER_TOKEN` and
`/tmp/crag-secrets/CRAG_CANARY_GITHUB_TOKEN`; neither environment variable carries a
token value.

Global options precede the subcommand:

```powershell
python scripts/phase11b_github_canary.py `
  --runtime-config <runtime-config.json> `
  --authorization <authorization.json> validate

python scripts/phase11b_github_canary.py `
  --runtime-config <runtime-config.json> `
  --authorization <authorization.json> prepare
```

`prepare` returns a redacted worksheet containing the three case IDs, six approval IDs,
their exact binding hashes, and the worksheet hash. It does not expose repository,
branch, path, content, credential, or human identity. For each worksheet item, the human
operator—not Codex or the service—runs one decision command:

```powershell
python scripts/phase11b_github_canary.py `
  --runtime-config <runtime-config.json> `
  --authorization <authorization.json> approve `
  --case-id <case-id> `
  --kind <write-or-draft_pr> `
  --approval-id <exact-approval-id> `
  --decision approve `
  --crag-token-file <absolute-secure-file>
```

Only after all six decisions are consumed and the operator has created a short-lived
installation-token JSON file may one case be invoked:

```powershell
python scripts/phase11b_github_canary.py `
  --runtime-config <runtime-config.json> `
  --authorization <authorization.json> run `
  --case-id <exact-case-id> `
  --github-token-file <absolute-secure-json-file>
```

The token JSON fields are exactly `token`, `github_app_id`, `installation_id`,
`installation_account_id`, `expires_at`, and `revoked`. The executor never mints an
installation token and never reads a GitHub App private key, PAT, environment token,
`gh` credential, or Git credential helper.

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
- allowed base branch/frozen GitHub base SHA/base-tree SHA; exact three head branches; exact
  synthetic diff, blob/tree/commit, test, budget, checkpoint, commit-message, title/body marker,
  and publisher-payload hashes;
- separate `repair_base_sha` and `repair_diff_sha256` values from the Phase 11A Repair lineage;
  these must never be substituted with the GitHub sandbox base SHA or synthetic Git diff binding;
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

The required branch integration is complete. `origin/master` points to
`010537202ca653d40dc6da2464482cf191aba401`, the Phase 11A master CI run is
`30419658651`, and the Phase 11B branch contains that merge as its direct baseline.
The separate release worktree's local `master` ref is still stale at `21344a2...`; this
task does not mutate or fast-forward `master`, and real-canary evidence must use the
verified `origin/master` merge SHA. The earlier executable publisher commit is
`81c8f528a8fffd7d9120323bb97d0657dec95862`; it is superseded for runtime purposes by
the pending consolidated executor commit.

Offline validation completed on the rebased tree:

| Command | Result |
| --- | --- |
| `python -m unittest -v tests.test_phase11b_github_sandbox_canary tests.test_phase11b_github_canary_executor` | Passed; publisher and executor offline suites both green |
| `python -m unittest -v tests.test_phase11b_github_canary_executor` | 11 tests passed |
| `python -m unittest -v tests.test_phase11a_synthetic_staging` | 20 tests passed; 1 Postgres environment skip |
| `python -m unittest discover -s tests` (through `scripts/verify.py`) | 934 tests passed; 18 environment skips |
| `python -m ruff check .` | Passed |
| `python -m mypy src/code_review_agent` | Passed; 37 source files |
| `python scripts/verify.py` | Passed; total branch coverage 85%, offline verification valid |
| `python -m pip check` | Passed; no broken requirements |
| `git diff --check` | Passed |

Local Docker integration could not start because access to the Docker Desktop Linux
engine named pipe was denied; this fact was recorded once and the command was not
retried. The Phase 11A real-Postgres test also skipped because
`CRAG_PHASE11A_POSTGRES_URL` was not supplied. Neither skip is canary evidence. The
installed Gitleaks executable was also denied by the local OS before it could start;
the fixed changed-file scan for private-key markers, live-token prefixes, host IP, and
absolute user paths returned no matches and Gitleaks was not retried. No local
`pre-commit` executable is installed, and the frozen CI workflow declares neither a
Gitleaks nor pre-commit job. The
owner now authorizes one consolidated non-force push and one Phase 11B Draft PR only
after every final local gate passes,
under the Actions quota discipline in the task contract. They do not authorize a real
GitHub canary. Any Phase 11A executable change requires a fresh integration review.
