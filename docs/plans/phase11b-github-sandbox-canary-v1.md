# Phase 11B-Prep: GitHub Sandbox Publisher Canary v1

Status: **canary-executor preparation and delivery authorized; real writes remain
closed until the complete second gate, six human decisions, and short-lived
credential gate are satisfied**

Phase 11A is merged on `master` at
`010537202ca653d40dc6da2464482cf191aba401`; the verified green master CI run is
`30419658651`. The Phase 11B implementation was rebased onto that merge before
validation. The old stacked development baseline
`567bd3cf9fe97774ce2177275d325c7d30ff1631` is historical only and must not be used
for authorization or evidence.

Task branch: `codex/phase11b-github-sandbox-canary-v1`

## Claim and authorization boundary

This phase implements and verifies a fail-closed GitHub sandbox Draft PR publisher
using deterministic fakes. It does not execute a real GitHub request, read a real
repository, inject a GitHub App token or private key, call a model, incur cost, push
the task branch, create a delivery PR, or modify `master`.

Every receipt and report keeps these fields:

```text
environment=github_sandbox_canary
synthetic_input_only=true
real_github_sandbox_writes=false
real_model_calls=false
real_business_repository_writes=false
business_claim_allowed=false
quality_claim_allowed=false
production_ready=false
```

The permanent auth-004 evidence remains `selected=5`, `headline=5`, `completed=0`,
`failed=5`, `provider_or_pipeline_RuntimeError=5`, and `root_cause=unknown`. Phase
11B does not rerun, replace, backfill, or change the denominator of auth-004 and is
not a Business Pilot, model-quality result, production writer, or readiness proof.

## Frozen publisher backend

The only real-write backend implemented by this task is the **GitHub REST Git
Database API** over an injected HTTPS transport. GitHub Contents API, shelling out
to `git` or `gh`, libgit transports, PAT fallback, global Git credentials, and every
other write backend are prohibited. The publisher creates pre-bound Git objects,
then an exact branch ref, then a Draft PR. It has no merge, Ready, comment, review,
label, Check, status, release, deployment, branch-delete, PR-close, force-push, or
protected/default-branch mutation method.

The real HTTPS transport may send only the following method/endpoint enums to
`https://api.github.com`; path parameters are separately normalized and bound to the
authorized owner/repository, exact object SHA, exact branch, or exact PR number:

| Endpoint enum | Method | Path template | Purpose |
| --- | --- | --- | --- |
| `repository_read` | `GET` | `/repos/{owner}/{repo}` | Verify immutable repository ID, owner/name, and default branch |
| `ref_read` | `GET` | `/repos/{owner}/{repo}/git/ref/heads/{branch}` | Verify frozen base or reconcile exact head ref |
| `blob_read` | `GET` | `/repos/{owner}/{repo}/git/blobs/{blob_sha}` | Reconcile an unresolved content-addressed blob request by exact SHA |
| `tree_read` | `GET` | `/repos/{owner}/{repo}/git/trees/{tree_sha}` | Reconcile an unresolved content-addressed tree request by exact SHA |
| `commit_read` | `GET` | `/repos/{owner}/{repo}/git/commits/{commit_sha}` | Verify/reconcile the pre-bound commit |
| `blob_create` | `POST` | `/repos/{owner}/{repo}/git/blobs` | Create one pre-hashed synthetic blob |
| `tree_create` | `POST` | `/repos/{owner}/{repo}/git/trees` | Create the exact pre-hashed synthetic tree |
| `commit_create` | `POST` | `/repos/{owner}/{repo}/git/commits` | Create the exact pre-hashed commit |
| `ref_create` | `POST` | `/repos/{owner}/{repo}/git/refs` | Create the exact repair head branch once |
| `draft_pr_list` | `GET` | `/repos/{owner}/{repo}/pulls` | Read back candidates by exact head/base |
| `draft_pr_create` | `POST` | `/repos/{owner}/{repo}/pulls` | Create one Draft PR |
| `draft_pr_read` | `GET` | `/repos/{owner}/{repo}/pulls/{number}` | Reconcile the exact Draft PR receipt |

Unknown methods/endpoints fail with `endpoint_denied` before transport I/O. HTTPS,
normal certificate verification, `api.github.com` host pinning, disabled automatic
redirect following, bounded timeouts, response-size limits, and fixed user-agent/API
version headers are mandatory. A redirect is never followed; any redirect response is
`redirect_denied`. The transport obtains one short-lived installation token only from
an injected token provider after every offline gate passes. It never reads environment
PAT variables, `gh` state, Git configuration, credential helpers, or user-global files.
Blob/tree read-back responses are used only to compare the top-level SHA; any returned
content or tree entries are discarded before control returns from the transport and
must never enter an exception, log, trace, metric, ledger, or receipt.

## Canonical authorization and exact publication binding

The versioned schema is `crag.github-sandbox-authorization/v1alpha1`. Unknown or
missing fields fail closed. Canonical bytes use deterministic JSON with sorted keys,
compact separators, ASCII escaping, no NaN, and UTF-8; their SHA-256 is the canonical
authorization digest. Timestamps are strict UTC `YYYY-MM-DDTHH:MM:SSZ`, ordered as
`issued_at <= not_before < expires_at`.

Authorization binds its unique ID, organization, GitHub owner/repository and immutable
repository ID, GitHub App/installation/account IDs, allowed base branch and frozen base
SHA, three exact canary cases and branches, maximum denominator of three, executable
code SHA, runtime-config SHA-256, request/mutation/read/branch/commit/Draft-PR budgets,
CNY-zero cost ceiling, time window, and named authorization/revocation/kill-switch
owners. It contains no token, private key, diff, commit message, PR body, credential,
identity mapping, or host path.

Configured publishing has a separate `real_github_writes_enabled` gate. Fake transports
must declare `real_github_writes=false` and can produce only offline receipts with
`real_github_sandbox_writes=false`. The gate can be true only with the pinned real HTTPS
transport, and that exact mode is persisted with the intent so a restart cannot swap a
fake and real transport. A transport/gate mismatch fails during construction.

Each publication additionally binds all of: organization; owner/repository; immutable
repository ID; App, installation, and installation-account IDs; allowed base branch;
frozen base SHA and exact base-tree SHA; exact head branch; exact diff, test evidence, durable budget, and
checkpoint SHA-256; exact commit SHA; commit-message and publisher-payload SHA-256;
WRITE and DRAFT_PR approval IDs and binding SHA-256 values; authorization ID and
canonical SHA-256; app idempotency key; canary case ID; executable code SHA; and
runtime-config SHA-256. Blob/tree object SHAs, stable title/body marker hashes, and
the exact synthetic file paths are also bound. Any drift invalidates the approvals and
authorization before external mutation; no replacement approval is generated.

The Phase 11A Repair repository and the GitHub sandbox are distinct Git namespaces.
Every case therefore carries separate `repair_base_sha` and `repair_diff_sha256`
lineage fields in addition to the frozen GitHub `base_sha` and synthetic
`diff_sha256`. The lineage gate compares only like-for-like fields; crossing either
pair is an authorization mismatch, not a value that an operator can make equal.

Because the Phase 11A DRAFT_PR approval is created before a GitHub commit exists, it
cannot bind the Phase 11B exact commit/App/installation/authorization fields. Phase
11B therefore adds two one-use canary approval envelopes, named `write` and `draft_pr`,
which both bind the complete exact publication and its Phase 11A Repair job/checkpoint
lineage before any GitHub mutation. The publisher verifies both are durable and
consumed with the exact binding hashes. Only a same-organization human
`maintainer`/`org_admin` Principal may consume them; viewer, reviewer, model, webhook,
Finding, anonymous, system, and agent actors cannot approve. Transactional
compare-and-set semantics make concurrent decisions produce one winner and a
conflict/replay result for every loser, with no external mutation during approval.

## Durable outbox and recovery

Migration `0008_phase11b_github_canary` is additive. Startup and request handlers never
run DDL. It adds a publication outbox and per-request ledger without changing or
rewriting Phase 11A tables. Durable publication states are exactly:

```text
publish_intent_recorded
branch_push_requested
branch_push_observed
draft_pr_requested
draft_pr_observed
receipt_reconciled
quarantined
```

The exact binding and intent are committed before transport use. Each mutation request
binding is transactionally inserted before sending. A completed request is immutable.
Unresolved object/ref/PR mutations are never blindly resent. Recovery reads the exact
base/ref/commit and Draft PR candidates; an exact existing ref/commit is observed,
another commit is `ref_collision`, multiple PR candidates or an unverifiable mutation
is quarantined, and an ambiguous Create PR is read-back only. Existing PR reconciliation
requires exact head, base, draft status, stable marker hashes, and publisher-payload
hash. Restart never creates a second branch, commit, ref, or Draft PR.

The low-cardinality failure enum is frozen to:

```text
auth_401 permission_403 missing_404 conflict_409 validation_422 rate_limited
server_5xx timeout base_drift ref_collision branch_protected token_revoked
token_expired ambiguous_result receipt_mismatch repository_mismatch
installation_mismatch authorization_expired authorization_mismatch endpoint_denied
redirect_denied budget_exhausted other
```

Rate-limit classification uses 403/429 together with bounded `Retry-After` and
`X-RateLimit-Remaining`/`X-RateLimit-Reset` fields. Logs, traces, metrics, and receipts
may contain only the enum, HTTP status, endpoint enum, bounded retry count, duration,
SHA-256 values, booleans, and non-negative counts. Exception messages, response bodies,
Authorization headers, tokens, keys, query strings, diffs/patches, PR bodies, commit
messages, identities, repository aliases, branch names, and host paths are prohibited.

## Single Writer declaration

Codex owns exactly these paths for Phase 11B-Prep:

- `docs/plans/phase11b-github-sandbox-canary-v1.md`;
- `docs/phase11b-github-sandbox-canary-v1.md`;
- `schemas/phase11b-github-sandbox-authorization.schema.json`;
- `migrations/versions/0008_phase11b_github_sandbox_canary.py`;
- `src/code_review_agent/repair_publish.py`;
- `src/code_review_agent/github_sandbox_publish.py`;
- `src/code_review_agent/github_canary_executor.py`;
- `scripts/phase11b_github_canary.py`;
- `schemas/phase11b-github-sandbox-runtime.schema.json`;
- `Dockerfile.service` (only the root-to-UID-1000 canary secret-file path handoff);
- `tests/test_phase11b_github_sandbox_canary.py`;
- `tests/test_phase11b_github_canary_executor.py`.

Every other path is read-only. In particular, no command may enumerate, read, execute,
copy, or modify `eval/**` or `eval/holdout/**`. Dependencies, lock files, package entry
points, workflows, existing service/worker routes, Phase 11A schema/data, prompts, and
sentinels remain frozen. The additions to the existing publisher public types are
expressly authorized by the Phase 11B request; existing fake/dry-run behavior and
Phase 10/11A callers remain backward compatible. Any further writable path or backend
requires a contract revision and explicit user approval before editing.

### Executor-preparation scope expansion

On 2026-07-29 the repository owner confirmed the Phase 11A merge and green master
CI and explicitly authorized the remaining Phase 11B executor work. The new paths
added above are required to provide a strict runtime-config schema, deterministic Git
object materialization, secure file-only credential providers, a database-authenticated
human approval command, and a one-case-at-a-time canary command. This revision does not
add a dependency, package entry point, service route, implicit migration, credential
minting path, or additional GitHub endpoint. `Dockerfile.service` is included only
because the existing root entrypoint already copies allowlisted root-owned host secret
files into a UID-1000 `0600` tmpfs handoff; extending that fixed variable list is needed
for the CRAG approver-token file and GitHub installation-token file while keeping secret
values out of image layers, environment values, argv, and logs.

The executor must:

- require an exact versioned runtime config with unknown fields rejected and canonical
  SHA-256 recomputed before every prepare/approve/run operation;
- bind the exact root base-tree manifest and independently reproduce the Git blob,
  resulting tree, and commit object IDs before an approval can be issued;
- accept only top-level synthetic canary files so the frozen non-recursive base-tree
  manifest is sufficient to reproduce the resulting root tree;
- authenticate an approving human by reading one explicitly named, secure CRAG bearer-
  token file and resolving the current Principal through the database; role and
  identity are never caller-supplied;
- issue and consume one WRITE plus one DRAFT_PR envelope per case, for six separate
  one-use human decisions total; neither Codex, an agent, nor the service may perform
  those decisions;
- read a GitHub installation token only from one explicitly named JSON file with an
  exact field set, bounded size, regular-file/no-symlink checks, mode `0600`, approved
  owner UID, exact App/installation/account identity, revocation state, and a lifetime
  no longer than one hour; no environment/PAT/`gh`/Git fallback exists;
- require exact schema head `0008_phase11b_github_canary` and never run a migration;
- run only one frozen case per explicit invocation, implement deterministic process-
  restart failpoints for the two crash cases, and block later cases after an earlier
  failure or quarantine;
- emit only canonical redacted worksheets/status/receipts and never print a token,
  private key, credential path, database URL, host path, repository alias, branch,
  synthetic body, or approving identity.

The runtime config contains no secret, private key, token, raw host IP, database URL,
or credential path. It binds the exact code commit and local image ID, tracked-source
archive and rendered deployment-config digests, repository/App/installation identity,
base ref and root-tree manifest, three cases, request/time/retry budgets, pinned API
host, TLS/redirect policy, token-file injection mode, synthetic-only claim boundary,
and a SHA-256-redacted Aliyun runtime identity.

## Offline acceptance

All ordinary tests inject a recording fake transport and synthetic token provider;
the HTTPS transport is validated with fake openers and never opens a socket. Coverage
includes default disabled and missing-config gates, schema/unknown-field rejection,
time/code/config/repository/installation/base/branch/hash/approval drift, exact human
approval lineage, one-winner concurrency compatibility, protected/default branch and
prefix denial, endpoint/redirect/budget denial, the full HTTP taxonomy, token expiry or
revocation before/after a write, crash after object/ref/PR success but before receipt,
read-back-only recovery, ref collision, multiple PR quarantine, receipt mismatch,
no duplicate mutations after restart, no false `draft_published`, fixed redaction, and
absence of every prohibited GitHub capability.

Required validation is offline:

```powershell
python -m unittest -v tests.test_phase11b_github_sandbox_canary
python -m unittest -v tests.test_phase11b_github_canary_executor
python -m unittest -v tests.test_phase11a_synthetic_staging
python -m unittest discover -s tests
python -m ruff check .
python -m mypy src/code_review_agent
python scripts/verify.py
python -m pip check
git diff --check
git diff --name-only 010537202ca653d40dc6da2464482cf191aba401...HEAD
```

No eval-assets mode, real model, real GitHub, credential, or paid test is allowed.
Docker/Postgres integration is required only if the executable is locally available;
an environment skip is reported and is not canary evidence.

## Delivery and second authorization gate

This executor round ends with one stable task-branch commit after full diff review,
Single Writer verification, and sensitive-data scanning. The owner's 2026-07-29
instruction authorizes exactly one non-force push of the consolidated branch and one
Phase 11B Draft PR (or update of the single existing Phase 11B PR) after every local
gate passes. It does not authorize a second delivery PR, an empty trial commit, a
product-side GitHub write, or a claim that Phase 11B is complete.

After Phase 11A owner merge, record the actual merge SHA and green master CI run,
verify `master == origin/master`, rebase the unpushed Phase 11B commit onto that merge,
review the complete diff, rerun Phase 10/11A/11B offline validation, and refreeze the
executable code SHA. Any Phase 11A executable change requires interface reintegration.

A real canary remains fail-closed until the repository owner separately supplies every
field in the Phase 11B request's second authorization gate, including green Phase 11A
and Phase 11B CI, image/config/code digests, exact repository/App/installation/base/
branch/marker identities, three-case denominator, all request/mutation/read/time/retry
budgets, CNY-zero cost, short-lived credential injection/revocation, runtime egress/TLS
policy, incident owner, and cleanup/retention owner. That later authorization is not
implied by this contract.

## GitHub Actions quota discipline

The following owner rule is mandatory for this task: diagnose first, batch the complete
fix, and push once; with no code/config/workflow change, rerun only failed jobs.

- A CI result is evidence only for its exact commit SHA. A new commit receives at most
  one complete PR workflow run; already-passing jobs are not separately rerun.
- After a first failure, inspect only the failed job steps/logs and record the complete
  root-cause set before changing code.
- An infrastructure-only intermittent failure with no code, lockfile, or workflow
  change may use GitHub's **Re-run failed jobs** exactly; **Re-run all jobs** is
  prohibited.
- If code must change, resolve all variants locally, run every affected local gate,
  create one normal consolidated commit, and make one non-force push. Empty commits,
  repeated small-fix pushes, duplicate PRs, superseded-run reruns, passed-run reruns,
  and validation-only `workflow_dispatch` runs are prohibited.
- After the PR passes, merge happens once through the protected owner path and only the
  merge commit's one main CI run is monitored. Before any tag/release, verify the tag
  ref cannot match `push.branches` and cause an unintended extra run.
- Every necessary extra run is recorded with run ID, commit SHA, trigger, whether code
  changed, and why failed-job-only rerun was insufficient.
- Network, Docker, Compose, Gitleaks, pre-commit, or GitHub API unavailability is
  recorded once and is not retried merely to consume another Actions run.
