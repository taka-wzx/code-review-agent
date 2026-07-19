# Week 7.5: Live Protocol Validation

## Goal

Produce bounded operational evidence for the Week 7 service by exercising one
real GitHub pull request through the official `gh` CLI, one paid model review,
and one temporary GitHub Webhook delivery plus redelivery. This phase validates
the deployed protocol chain; it does not add product features or claim remote
production readiness.

## Base and delivery

- Base branch: `master`
- Base commit: `dff20d3f49b9c293c0742b8cfcb50f4941b8230a`
- Task branch: `codex/week7-5-live-validation`
- Task worktree: isolated local worktree (host path intentionally omitted)
- Target repository: private `taka-wzx/code-review-agent`
- Target change: a draft PR from the task branch to `master`
- Provider/model: locally configured `deepseek` / `deepseek-v4-pro`

The task branch may be committed, pushed, and opened as a draft PR because
those actions are required to produce the live evidence. The PR must not be
merged and `master` must not be changed or pushed during this phase.

## Single Writer ownership

Codex may create or modify only:

- `docs/plans/week7-5-live-validation.md`
- `docs/week7-5-live-validation.md`
- `scripts/week7_5_live_validation.py` if a reusable redacted evidence helper
  is necessary
- `tests/test_week7_5_live_validation.py` if the helper is created
- `README.md`
- `AGENDA.md`

Claude is a read-only reviewer. Its only writable path in a separate review
worktree is `docs/reviews/week7-5-claude.md`. All `src/` code, dependencies,
CI, evaluation assets, holdout data, historical reports, and the target
repository contents are read-only unless a live probe proves a product defect
that requires a separately declared ownership expansion.

## Security, authority, and cost boundaries

- Provider and GitHub credentials are loaded only into process memory from
  existing local credential mechanisms. Their values must never be printed,
  copied, committed, placed in command output, or written to evidence files.
- The existing `.env` may be loaded by project code as designed, but Codex must
  not read or copy its contents.
- Generate fresh random service and webhook secrets in memory for this phase.
  Do not persist them.
- Use official portable `gh` and `cloudflared` binaries from their authoritative
  GitHub release assets. Store them only in a task-specific system temporary
  directory and remove that directory after the live probes.
- Create exactly one temporary GitHub repository webhook, pointed at a
  Cloudflare Quick Tunnel for the loopback service. Record its numeric identity
  and delivery metadata, then delete it after validation.
- Create a draft PR but do not post review comments, approve, merge, close,
  mutate `master`, or modify repository settings other than the temporary hook.
- Queue at most one unique paid review job. A GitHub redelivery must reuse the
  same delivery identity and must not cause a second model review.
- Phase budget is CNY 5.00. Historical evidence suggests approximately CNY
  0.11 per normal review and a prior large-file outlier of CNY 1.85. Stop before
  any second paid job if the first consumed model calls; infrastructure retries
  are allowed only when evidence proves no model call occurred.
- Do not run paid evaluation scripts, inspect `eval/holdout`, start Repair,
  execute reviewed code, or enable remote OAuth/A2A/approval endpoints.

## Execution sequence

1. Commit and push this frozen contract on the task branch.
2. Download and checksum the official portable `gh` and `cloudflared` release
   archives into a task-specific system temporary directory.
3. Authenticate `gh` from the existing Git credential in process memory and
   verify repository access without displaying credential material.
4. Start the Week 7 service on loopback with:
   - the target repository registered under its exact alias;
   - a fresh state directory outside the repository;
   - fresh in-memory bearer/webhook secrets;
   - the configured provider loaded through the existing project mechanism.
5. Start a temporary HTTPS tunnel, create the temporary pull-request webhook,
   and open the task branch as a draft PR.
6. Verify the real `opened` delivery reaches HTTP 202, the queued job becomes
   terminal, `gh pr diff` is used, and the review/trace remain sanitized.
7. Request a GitHub redelivery and prove it returns the original review identity
   with `duplicate: true` and no second model job.
8. Delete the temporary webhook, stop the tunnel/service, and remove temporary
   binaries/state after extracting redacted aggregate evidence.
9. Document results, run offline regression gates, prepare the manual Claude
   review handoff, and stop on the task branch. Do not merge `master`.

## Acceptance criteria

- Official `gh` authenticates to the private repository and returns a non-empty,
  bounded diff for the exact draft PR without shell interpolation.
- GitHub records an `opened` pull-request delivery with a 2xx response from the
  real temporary HTTPS endpoint.
- Exactly one persisted job transitions monotonically to `succeeded` or a
  truthful sanitized `failed` state. Success is required to claim the model
  path passed; failure remains evidence and must not be hidden by replacement.
- The trace records provider/model, terminal status, request counts, token/cost
  telemetry when supplied, and no raw diff, credential, command output, or host
  absolute path.
- Redelivery keeps the same GitHub delivery identity, returns the same review
  identity with `duplicate: true`, and does not add provider calls.
- Invalid-signature traffic through the public tunnel is rejected before job or
  model work.
- The temporary webhook is deleted and the tunnel/service processes and
  temporary task directory are removed. The draft PR remains open as evidence.
- Week 7 focused tests, Ruff, mypy, full offline verification, CLI smokes, and
  `git diff --check` pass. No paid evaluation or protected assets are used.

## Evidence policy

The committed report may include repository alias, PR number/URL, commit SHAs,
GitHub hook/delivery numeric IDs, timestamps, HTTP status, job identity, state,
finding counts, provider/model aliases, aggregate token/cost/latency values,
trace schema and event counts, executable versions and SHA-256 checksums. It
must not include secrets, authorization headers, raw request bodies, raw diff,
prompt/tool content, provider responses, command stdout/stderr, or local
absolute paths.

## Delivery report

- Summary: the bounded GitHub, Webhook, `gh`, and paid-model paths ran on
  2026-07-19; full evidence is in `docs/week7-5-live-validation.md`.
- Draft PR: private draft PR #3 remains open and unmerged.
- Live `gh` result: authenticated repository access and a non-empty PR diff
  succeeded with official `gh` 2.96.0.
- Live model result and cost: one unique job succeeded with
  `deepseek/deepseek-v4-pro`; the trace-derived price estimate was CNY
  0.6342652, below the CNY 5.00 cap.
- Webhook delivery/redelivery result: the first delivery was accepted by the
  service but exceeded GitHub's 10-second response deadline; official
  redelivery returned 202 with the original review identity. A signed replay
  of the same real payload proved `duplicate: true`, while job and provider
  counts stayed unchanged. Invalid HMAC returned 401.
- Cleanup result: the temporary hook was deleted; service/tunnel processes and
  the task-specific temporary directory were removed.
- Acceptance disposition: recorded criterion by criterion in
  `docs/week7-5-live-validation.md`; the 2xx delivery criterion was met only by
  official redelivery of the same delivery identity, while the initial attempt
  was recorded as 500 by GitHub.
- Offline commands and results: pre-handoff and post-review integration runs
  both passed 33/33 focused tests, targeted Ruff, mypy for 26 source files,
  full `scripts/verify.py` (593 tests, 3 skips, 86% coverage), both service/MCP
  help smokes, and `git diff --check`.
- Claude review: commit `2e762ee6fca196ed79b6668b2c1bb8d0e5af4bca`
  returned CONDITIONAL PASS (1 P2, 2 P3, no P1). All three findings were
  accepted and addressed as documentation-only corrections in integration.
- Remaining risks: initial Webhook response latency with no established root
  cause, the host path still reachable in the frozen commit/PR history,
  GitHub response-body truncation for completed duplicate jobs, and the
  unverified Docker/Linux, MCP-over-HTTP, live OAuth, A2A, and remote approval
  paths.
