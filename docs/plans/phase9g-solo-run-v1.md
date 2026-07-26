# Phase 9G-Solo-Run v1: Single-Participant Real Exploratory Run

Status: **active and frozen**

Frozen date: 2026-07-26

Baseline: `origin/master` at `a79b77e9e7e3792dd46cea4d6415c18ddcc54bb4`

Task branch: `codex/phase9g-solo-run-v1`

## Goal

Materialize and, only if every runtime gate becomes true, execute one bounded real
single-participant exploratory Review run over five deterministically selected,
authorized PRs. This task is the real-run continuation of
`docs/plans/phase9g-solo-exploratory-v1.md`; it does not reopen or weaken the original
3--5-person Business Pilot or three-human Formal Quality contract.

Permanent claim values are:

```text
evidence_type=single_participant_exploratory
business_claim_allowed=false
quality_claim_allowed=false
formal_quality_status=incomplete
```

There is exactly one human participant bound by the private authorization. The
participant may also be the repository owner, identity custodian, and authorization
approver under that same stable pseudonym. One human may not be represented as
multiple people, and no model may provide human feedback or annotation.

## Verified baseline

- Solo preparation PR #12 merged through the protected PR path.
- Solo contract merge commit and selection source commit:
  `a79b77e9e7e3792dd46cea4d6415c18ddcc54bb4`.
- The merge-commit master CI run `30191865985` completed successfully in all nine
  jobs.
- Local `master` was fast-forwarded and verified equal to `origin/master` before this
  worktree was created.
- Frozen selection seed:
  `4520d525e3673de85dda5c12144ed6cd32bf26da34e3892e518e7ed089e7f52f`.

## User authorization already received

The user explicitly attested:

- the privately authorized participant ID belongs to one real human;
- the participant owns or controls the privately authorized opaque repository
  mapping;
- the frozen window contains at least five eligible merged PRs;
- shadow mode, five PRs, raw-diff reads for the selected PRs, real paid GLM calls,
  CNY 20 maximum cost, 30/30/7-day data/feedback/raw-trace retention, and task-branch
  commit/push/PR/Ready operations are approved;
- staging deployment, real GitHub API use for evidence collection, comments, Checks,
  publication, and agent merge of `master` remain forbidden;
- the originally disclosed credential was revoked and a replacement is stored only
  locally. The revoked credential and its derived `auth-001` are permanently invalid.

The hash-valid replacement authorization `phase9g-solo-run-v1-auth-002` froze:

```text
provider=glm
model=glm-5.2
temperature=0
max_logical_calls=30
max_http_attempts=45
max_input_tokens=2000000
max_output_tokens=200000
max_cost_microcny=20000000
paid_calls=true
read_selected_raw_diff=true
approved_at=2026-07-26T07:29:48Z
expires_at=2026-08-25T07:29:48Z
runtime_config_sha256=cee0b676c3c00f2570aab960ca506473fb9412d7ba2809e917083d57554660da
authorization_sha256=365ba325a31645f40610c8bf9cf32b21e071fb7109163955384468084d2bcc89
```

`auth-002` is sufficient to materialize identity/repository artifacts, enumerate
local Git metadata, select the cohort, and read/hash only selected diffs. It is not
sufficient for a paid call because the current product runtime is not identical to
its frozen runtime configuration.

## Paid-call gate currently closed

The repository's current Review runtime has:

- Finder anchor temperature `0.0`;
- Finder sampler temperature `0.7`;
- Verifier A/B temperature `0.0`;
- OpenAI-compatible SDK `max_retries=2` in the CLI client;
- up to 10 loop steps in each of two Finder conversations and up to 6 in each of two
  Verifier conversations, or 32 logical calls per PR in the theoretical worst case.

Therefore the user-approved single temperature `0`, 30 logical calls, and 45 HTTP
attempts cannot truthfully describe or hard-bound an unmodified current-product run.
No paid call may occur until one of these paths is explicitly approved and hash-bound:

1. a new `auth-003` binds the product temperature profile, disables SDK retries in a
   dedicated Solo executor, freezes a sufficient call/HTTP ceiling, freezes the exact
   endpoint and CNY token/cache tariff, and passes all validators; or
2. a Solo-only deterministic runtime is explicitly authorized to use temperature zero
   in every stage, zero SDK retries, and the original 30/45 ceilings, with the risk of
   an incomplete five-PR run stated in advance.

The agent may implement and test both gate mechanics with fakes, but may not choose a
path, increase a ceiling, infer a tariff, or make a paid request on the user's behalf.

## Selection contract

- Repository: the authorized local checkout bound to the private repository manifest.
- Window: `2026-01-01T00:00:00Z <= merged_at < 2026-07-26T00:00:00Z`.
- Candidate source: local first-parent `origin/master` commit metadata only; evidence
  collection must not call the GitHub API.
- Candidate eligibility and extraction rules are frozen before reading a candidate
  diff. Every in-window PR-shaped first-parent commit is represented exactly once.
- PR numbers/titles and the locator mapping stay in a controlled external evidence
  root. Committed artifacts use opaque IDs derived with a run-specific private salt.
- Rank is exactly `SHA256(seed + "\n" + opaque_pr_id)`.
- The five eligible rows with the lowest ranks are selected.
- Only after selection may the executor read each selected first-parent diff and bind
  its SHA-256 plus a deterministic snapshot SHA-256.
- Unselected candidate diffs are never opened.

No model output, Finding, feedback, or prior run outcome may influence selection.

## Credential and provider boundary

- Runtime model code is exactly `glm-5.2`; the repository default `glm-4.6` must never
  be used implicitly.
- The replacement credential is read only from `GLM_API_KEY_FILE` or
  `ZHIPUAI_API_KEY_FILE` outside the repository.
- Preflight checks only presence/readability and never prints the secret, secret ID,
  prefix, suffix, length, path, or exception content.
- Credential validation must not make an unreceipted smoke request. The first network
  request, if authorized, belongs to selected PR attempt 1 and is a headline request.
- The endpoint must be frozen as either standard BigModel API or Coding Plan before
  `auth-003`; the agent may not infer account plan from secret content.

## Evidence storage and retention

The controlled full evidence root is outside this Git worktree and is never printed or
committed. It contains the authorization, stable-ID mapping, complete candidate source
metadata, selected raw diffs, human feedback, review time, raw traces, and the locally
validated full bundle.

The Git branch may contain only deliberately sanitized material:

- frozen contracts/runbook and standard-library offline executor/validators;
- templates and synthetic fixtures;
- authorization/runtime/cohort/receipt-set hashes without credentials, locators,
  source text, Prompt text, raw diffs, identity mapping, host paths, or raw traces;
- aggregate Solo report with no participant ID;
- offline validation receipts.

Raw traces expire after 7 days; data and feedback after 30 days. Purge receipts may
retain only stable hashes, counts, UTC timestamps, and the retention rule. Immutable
Git history must not contain an artifact that is supposed to be deleted.

## Executor and receipt discipline

- The real executor is sequential across selected PRs. Stage-internal concurrency may
  occur only if it matches the final runtime hash and budget reservation remains atomic.
- A standard-library budget controller reserves logical calls, HTTP attempts, maximum
  input/output tokens, and worst-case micro-CNY before every provider request.
- SDK retries are zero in the recommended dedicated executor; any later diagnostic is
  an explicit attempt with its own receipt.
- Every selected PR registers attempt 1 before execution. Attempt 1 is the sole
  headline and can never be superseded.
- Process interruption, credential failure, pre-model budget refusal, timeout,
  cancellation, malformed output, and local persistence failure all become explicit
  stable failure receipts. A zero-call failure is not dropped.
- All failed and diagnostic attempts remain in cumulative budgets and denominators.
- Receipt telemetry is derived from actual provider usage plus the frozen tariff.
  Estimated floating prices are not billing evidence. Unknown cost fails the report.
- Output and trace serialization pass the repository redaction layer before storage.

## Human feedback and report boundary

After all headline attempts, the authorized human participant alone may import Finding decisions:
`accepted`, `rejected`, `uncertain`, `fixed`, or `duplicate`. Rejected, uncertain, and
duplicate decisions require a human rationale; fixed decisions require `fixed_at`.
Missing feedback remains in the full eligible-Finding denominator.

Every selected PR requires one consolidated active/paused time record completed by the
human. Feedback is a within-person workflow observation and is never gold or model
quality evidence.

The report must say `single-participant exploratory observation` and
`model quality not measured`. It must not contain Precision/Recall/F1, Bootstrap,
time-saved, productivity, multi-user adoption, Business Pilot success, formal-quality,
or generalization claims.

## Single Writer paths

Codex owns only:

- `docs/plans/phase9g-solo-run-v1.md`;
- `docs/phase9g-solo-run-v1.md`;
- `phase9g_solo_run.py`;
- `phase9g_solo_run/**`;
- `tests/test_phase9g_solo_run.py`;
- `README.md` (Solo-Run status and links only).

All existing Phase 9G files, production package code, prompts, sentinels, dependencies,
locks, migrations, CI, other tests, and all evaluation assets are read-only. No command
may read, enumerate, execute, copy, or modify `eval/**` or `eval/holdout/**`.

The enclosing historical worktree's pre-existing `%SystemDrive%/` path and the linked
worktree container itself are not task artifacts and must never be staged or committed.

## Offline acceptance before any paid call

```powershell
python -m unittest -v tests.test_phase9g_solo_run
python phase9g_solo_run.py validate-synthetic
python phase9g_solo.py validate-bundle --bundle phase9g_solo/examples/synthetic
python -m ruff check .
python -m mypy src/code_review_agent phase9g_pilot.py phase9g_solo.py phase9g_solo_run.py
python scripts/verify.py
python -m pip check
git diff --check
```

Tests must cover exact authorization/runtime binding; expired/revoked credentials;
credential presence without disclosure; standard-vs-Coding endpoint freeze; complete
candidate ledger and merge-commit anchoring; deterministic opaque IDs/ranks; selected-
only diff access; call/HTTP/token/micro-CNY atomic reservation; zero SDK retries;
parallel reservation safety; immutable headline failures; crash recovery; cumulative
diagnostic budgets; retention; redaction; synthetic propagation; exact report
recomputation; and permanent claim denial.

No test imports a live provider credential or contacts an external service.

## Delivery control

The user authorizes local commits, task-branch push, Draft PR creation, CI observation,
and Ready transition for `codex/phase9g-solo-run-v1`. The agent must not merge, rebase,
or push `master`; the human repository owner alone merges through the protected PR.

Real evidence may be committed only after the full controlled bundle validates and the
per-file disclosure audit passes. Failed or incomplete preregistered targets must be
reported as failed or incomplete, never as success.

## Change control

This contract is frozen after creation. Any paid call, endpoint choice, tariff, changed
temperature/call/HTTP ceiling, production code change, new dependency, new writable
path, GitHub evidence API use, publication, deployment, or broader claim requires
explicit user approval and a hash-bound contract/authorization revision before action.

## Amendment 1: auth-003 paid Solo runtime

Status: **user-approved; paid calls remain gated until implementation evidence passes**

Approved date: 2026-07-26

The user explicitly approved the following replacement paid-runtime authority. It
supersedes auth-002 only for paid model execution; auth-002 remains the immutable
selection/materialization authority and the selected five-PR denominator is unchanged.

```text
authorization_id=phase9g-solo-run-v1-auth-003
endpoint_kind=standard
base_url=https://open.bigmodel.cn/api/paas/v4
provider=glm
exact_model_snapshot=glm-5.2
temperature_profile=0.01/0.70/0.01/0.01
sdk_max_retries=0
max_logical_calls=96
max_http_attempts=96
max_input_tokens=1750000
max_output_tokens=200000
max_cost_microcny=20000000
input_microcny_per_million_tokens=8000000
output_microcny_per_million_tokens=28000000
cached_input_microcny_per_million_tokens=2000000
selected_diff_policy=block_headline_zero_call
blocked_selected_prs=2
max_runnable_prs=3
```

The profile fields, in order, are Finder anchor, Finder sampler, Verifier A, and
Verifier B. Product zero-temperature requests are mapped to `0.01`; the sampler stays
at `0.70`. Any other requested temperature fails before a provider side effect.

The two selected secret-scan hits become immutable attempt-1 headline failures with
zero calls, zero HTTP attempts, zero Tokens, and zero cost. They remain in the five-PR
denominator and cannot be replaced, cleared, or rerun under auth-003. Only the three
remaining selected PRs may make paid requests.

The dedicated Solo executor additionally narrows authority as follows:

- no repository context pack and no readable repository snapshot; the model receives
  only the authorized selected diff, while read tools are rooted at an empty directory;
- no tiebreak pass, deployment, GitHub API, comment, Check, or publication;
- at most 2,048 output Tokens per logical request, counted within the aggregate output
  ceiling;
- every request reserves one logical call, one HTTP attempt, a conservative input-Token
  upper bound, the per-call output maximum, and worst-case non-cached micro-CNY before
  network I/O;
- actual provider usage and cache-hit Tokens are recorded separately from conservative
  reservations; missing usage keeps cost/reporting incomplete;
- five attempt-1 headlines are registered before the first paid request. Process
  interruption finalizes missing headlines as failures and never resumes or replaces
  them.

The inherited authorization expiry remains `2026-08-25T07:29:48Z`. Before the first
paid request, auth-003 must be canonical-hash sealed outside Git, the sanitized public
attestation must validate, the executor source must be committed and hash-bound, every
offline gate in this contract must pass, and the repository-external credential file
must pass a fresh content-free preflight. These are conjunctive gates; user approval by
itself does not open network execution.

Permanent claims are unchanged:

```text
business_claim_allowed=false
quality_claim_allowed=false
formal_quality_status=incomplete
model_quality_status=not_measured
```

## Amendment 2: auth-004 anonymous-public-source alternative

Status: **public source approved; exact paid authorization remains closed pending a
post-materialization human hash approval**

Approved scope date: 2026-07-26

The auth-003 executor passed its credential preflight, but the Codex tenant policy
rejected the network launch before the shell or executor started because auth-003
would disclose selected private-workspace diffs to an external GLM/BigModel endpoint.
The rejection happened before any provider request, run directory, receipt, Token, or
cost existed. Auth-003 remains immutable with status `not_run_policy_blocked`; it must
not be retried, relabelled, or represented as a model failure.

The user then explicitly approved creation of `phase9g-solo-run-v1-auth-004` with
candidate input restricted to anonymously verifiable public PR data. This approval
permits the offline/public-read implementation and materialization below. It does not
yet constitute the final post-materialization approval of an exact authorization
SHA-256, so no auth-004 paid call may occur from this amendment alone.

The single frozen public source is:

```text
source_kind=anonymous_public_git_exact_commit
repository=psf/black
license=MIT
branch=main
source_commit=db2e3e7b317b40685ba4618235a8388c7c6ea5e2
window=[2026-01-01T00:00:00Z, 2026-07-26T00:00:00Z)
selection_seed=5e190bc14d84c2439e43e0560db7d250c4cd702cd42cf32c9746425078c8ad38
candidate_prs=180
selected_prs=5
selected_diff_secret_scan_blocked=0
replacement_after_scan=false
```

`psf/black` is not one of the repositories registered by this repository's Formal
Quality or Verifier evidence contracts. The probe used an anonymous credential-free
partial clone, verified the exact commit and MIT license, and opened only the five
lowest-ranked selected diffs. It did not use GitHub's API, cookies, a GitHub token, the
private Solo repository, any unselected diff content, or a model. The official
materializer must reproduce all denominators and hashes exactly or fail closed; probe
results are not run evidence.

Auth-004 inherits, without increasing, the auth-003 model authority:

```text
provider=glm
exact_model_snapshot=glm-5.2
endpoint_kind=standard
temperature_profile=0.01/0.70/0.01/0.01
sdk_max_retries=0
max_logical_calls=96
max_http_attempts=96
max_input_tokens=1750000
max_output_tokens=200000
max_cost_microcny=20000000
input/output/cached_input micro-CNY per million=8000000/28000000/2000000
```

The executor remains sequential, uses the product Finder/Verifier stage pairs with
context disabled and an empty read-tool root, retains no Prompt or response content in
traces, and has no publish/deploy/GitHub API authority. Five public selected diffs are
runnable subject to the unchanged cumulative ceilings; budget exhaustion or any new
secret-scan mismatch becomes an immutable headline failure rather than a replacement.

Before auth-004 can make a provider request, all of the following are conjunctive:

1. committed executor source and full offline acceptance;
2. exact anonymous public materialization receipt and private hash cross-checks;
3. a separately created auth-004 authorization/runtime/tariff bundle and sanitized
   attestation;
4. explicit human approval after seeing the exact source, runtime, ceilings, expiry,
   and canonical authorization SHA-256;
5. a fresh repository-external Key preflight;
6. a fresh tenant data-egress approval. Tenant policy may still refuse the external
   call; such refusal remains `not_run_policy_blocked` and is never worked around.

Auth-004 public-PR observations cannot be backfilled into the private-repository
auth-003 denominator and do not establish a Business Pilot, product benefit, model
quality, Precision/Recall/F1, Formal Quality, or generalization claim. Permanent claim
values remain false/incomplete/not measured.
