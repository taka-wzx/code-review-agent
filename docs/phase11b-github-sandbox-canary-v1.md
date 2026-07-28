# Phase 11B-Prep: GitHub Sandbox Publisher Canary v1

## Current status

Phase 11B is implemented and tested **offline only** on the temporary stacked baseline
`567bd3cf9fe97774ce2177275d325c7d30ff1631`. Phase 11A is not yet merged, Phase 11B
has not been pushed, and no real GitHub canary has run. The offline results are
engineering evidence, not a Business Pilot, model-quality result, production writer,
or readiness proof.

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

Before a Phase 11B push or real canary:

1. fetch origin and record the actual Phase 11A merge SHA and green master CI URL/ID;
2. verify local `master == origin/master ==` that merge SHA;
3. rebase or cherry-pick the unpushed Phase 11B local commit onto that exact SHA;
4. inspect every changed file and re-check the frozen interfaces;
5. rerun the Phase 10, Phase 11A, and Phase 11B offline suites, Ruff, mypy,
   `scripts/verify.py`, `pip check`, and diff/leak checks;
6. freeze a new Phase 11B executable code SHA and discard all pre-rebase code evidence.

Run the following from the clean Phase 11B worktree only after the owner confirms the
Phase 11A merge and its green master CI result. Replace both placeholders before
running; this block intentionally performs no push, PR creation, merge, or canary:

```powershell
$Phase11AMerge = "<actual 40-character Phase 11A merge SHA>"
$Phase11ACiRun = "<green Phase 11A master CI URL or run ID>"
$StackedBase = "567bd3cf9fe97774ce2177275d325c7d30ff1631"
$TaskBranch = "codex/phase11b-github-sandbox-canary-v1"

if ($Phase11AMerge -notmatch '^[0-9a-f]{40}$') {
    throw "Fill the exact Phase 11A merge SHA first"
}
if ([string]::IsNullOrWhiteSpace($Phase11ACiRun) -or $Phase11ACiRun.StartsWith("<")) {
    throw "Record the green Phase 11A master CI URL or run ID first"
}
if ((git branch --show-current).Trim() -ne $TaskBranch) {
    throw "Open the Phase 11B task worktree before integration"
}
if (@(git status --porcelain).Count -ne 0) {
    throw "Phase 11B worktree must be clean"
}

$OfflineTip = (git rev-parse HEAD).Trim()
$OfflineCommitCount = [int](git rev-list --count "$StackedBase..$OfflineTip")
git fetch --prune origin
if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }

$LocalMaster = (git rev-parse master).Trim()
$OriginMaster = (git rev-parse origin/master).Trim()
if ($LocalMaster -ne $Phase11AMerge -or $OriginMaster -ne $Phase11AMerge) {
    throw "master/origin/master does not equal the approved Phase 11A merge SHA"
}
if ((git merge-base $StackedBase $OfflineTip).Trim() -ne $StackedBase) {
    throw "Offline Phase 11B tip is not based on the frozen stacked baseline"
}

git rebase --onto $Phase11AMerge $StackedBase $TaskBranch
if ($LASTEXITCODE -ne 0) { throw "Resolve or abort the rebase; do not continue validation" }

$RebasedTip = (git rev-parse HEAD).Trim()
if ((git merge-base $Phase11AMerge $RebasedTip).Trim() -ne $Phase11AMerge) {
    throw "Rebased Phase 11B is not based on the approved Phase 11A merge SHA"
}
if ([int](git rev-list --count "$Phase11AMerge..$RebasedTip") -ne $OfflineCommitCount) {
    throw "The rebase changed the number of Phase 11B commits"
}

$ExpectedFiles = @(
    "docs/phase11b-github-sandbox-canary-v1.md"
    "docs/plans/phase11b-github-sandbox-canary-v1.md"
    "migrations/versions/0008_phase11b_github_sandbox_canary.py"
    "schemas/phase11b-github-sandbox-authorization.schema.json"
    "src/code_review_agent/github_sandbox_publish.py"
    "src/code_review_agent/repair_publish.py"
    "tests/test_phase11b_github_sandbox_canary.py"
)
$ChangedFiles = @(git diff --name-only "$Phase11AMerge...$RebasedTip")
$UnexpectedFiles = @(Compare-Object $ExpectedFiles $ChangedFiles)
if ($UnexpectedFiles.Count -ne 0) {
    $UnexpectedFiles | Format-Table | Out-String | Write-Error
    throw "Rebased changed-file set differs from the Single Writer contract"
}

$Python = (Resolve-Path '..\..\.venv\Scripts\python.exe').Path
$env:PYTHONPATH = (Resolve-Path 'src').Path
& $Python -m unittest -v tests.test_phase10_repair_service
if ($LASTEXITCODE -ne 0) { throw "Phase 10 regression failed" }
& $Python -m unittest -v tests.test_phase11a_synthetic_staging
if ($LASTEXITCODE -ne 0) { throw "Phase 11A regression failed" }
& $Python -m unittest -v tests.test_phase11b_github_sandbox_canary
if ($LASTEXITCODE -ne 0) { throw "Phase 11B regression failed" }
& $Python -m ruff check .
if ($LASTEXITCODE -ne 0) { throw "Ruff failed" }
& $Python -m mypy src/code_review_agent
if ($LASTEXITCODE -ne 0) { throw "mypy failed" }
& $Python scripts/verify.py
if ($LASTEXITCODE -ne 0) { throw "full offline verification failed" }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw "pip check failed" }
git diff --check "$Phase11AMerge...$RebasedTip"
if ($LASTEXITCODE -ne 0) { throw "rebased diff check failed" }

$LeakPattern = 'github_pat_|gh[pousr]_[A-Za-z0-9]{20,}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|[A-Za-z]:\\Users\\'
$LeakMatches = @(
    git diff --no-color "$Phase11AMerge...$RebasedTip" |
        Where-Object { $_ -notmatch '^\+\$LeakPattern = ' } |
        Select-String -Pattern $LeakPattern
)
if ($LeakMatches.Count -ne 0) {
    throw "Review potential credential or host-path leakage before continuing"
}

Write-Output "Phase 11A merge: $Phase11AMerge"
Write-Output "Phase 11A green master CI: $Phase11ACiRun"
Write-Output "Pre-rebase Phase 11B tip (evidence now stale): $OfflineTip"
Write-Output "Refrozen Phase 11B executable code SHA: $RebasedTip"
```

Any Phase 11A executable change before merge requires a fresh integration review. A
successful offline rebase still does not authorize GitHub product writes, a task-branch
push, a Phase 11B PR, or a real canary.
