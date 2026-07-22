# Week 7.5 Live Protocol Validation — Independent Claude Review

- Reviewer model: `fable5` (Claude Code, read-only reviewer per
  `docs/plans/week7-5-live-validation.md`)
- Review branch: `claude/week7-5-live-validation-review`
- Base commit: `dff20d3f49b9c293c0742b8cfcb50f4941b8230a`
- Handoff commit: `7f03c317c5979aabaa614d352defaf3942d3d51d`
- Review date: 2026-07-19
- Scope inspected: every line of `git diff dff20d3..7f03c31` (4 files,
  +310/−3, documentation only), the intermediate freeze commit `1482b38`,
  and read-only consistency inspection of the Week 7 implementation
  (`service.py`, `service_core.py`, `mcp_server.py`, `observability.py`,
  `tracelog.py`) against every checkable claim in the two new documents.
  All mandated offline verification commands were rerun on this worktree.

## Verdict: CONDITIONAL PASS

No P1 finding. The numerical and procedural evidence is accurate everywhere I
could independently reproduce it — cost arithmetic, provider unit prices, the
`gh pr diff` byte count and SHA-256 (reproduced exactly from git history), the
frozen-interface surface, and all offline gates. The failure of the initial
Webhook delivery is disclosed, not hidden, and the live conclusions are
correctly bounded to one Windows/Python 3.13 run. One P2 must be corrected:
all three narrative documents attribute the initial delivery's GitHub 500 to
PR-diff acquisition sitting on the Webhook response path, and that causal
mechanism contradicts the Week 7 implementation as committed. Two P3s are
documentation-completeness gaps.

## Focus-item conclusions

### 1. Full diff `dff20d3..7f03c31`

Four files: `AGENDA.md` (+9), `README.md` (+9/−3), new
`docs/plans/week7-5-live-validation.md` (146 lines), new
`docs/week7-5-live-validation.md` (149 lines). No change to `src/`, `tests/`,
`pyproject.toml`, CI, or `eval/`. The intermediate commit `1482b38` froze the
plan; `7f03c31` filled in the delivery report and removed the task-worktree
host path from the plan (see W75C-P3-01). Ownership matches the plan's
Single Writer list. `git diff --check` is clean.

### 2. Cost calculation

Verified digit by digit:

- 395,008 × 0.025 / 1,000,000 = 0.0098752
- 150,192 × 3 / 1,000,000 = 0.4505760
- 28,969 × 6 / 1,000,000 = 0.1738140
- Sum = CNY 0.6342652 exactly as reported
  ([docs/week7-5-live-validation.md:88-93](../week7-5-live-validation.md)).

Input decomposition is consistent: 395,008 + 150,192 = 545,200, matching the
stated input total. I fetched the cited official DeepSeek pricing page on
2026-07-19; it lists V4-Pro at CNY 0.025/M cache-hit input, CNY 3/M cache-miss
input, CNY 6/M output — identical to the rates used. The report correctly
frames the figure as a trace-derived estimate, not provider billing evidence,
and it is below the CNY 5.00 phase cap.

### 3. "One job, 28 model client spans, no new calls on redelivery"

Internally consistent and code-consistent. The deliveries table keys on the
GitHub delivery GUID ([service_core.py:350-355](../../src/code_review_agent/service_core.py#L350-L355));
a duplicate short-circuits before queueing
([service_core.py:386-392](../../src/code_review_agent/service_core.py#L386-L392),
[service_core.py:609-610](../../src/code_review_agent/service_core.py#L609-L610)),
so the official redelivery and the signed replay reusing the same GUID cannot
start a second job or model call. "1 job, 1 persisted delivery identity,
28 model client spans" after all replays is exactly what that code path
produces. Arithmetic cross-checks all pass: 13 kept = 1 high + 7 medium +
5 low; 17 − 13 = 4 dropped; 06:40:25.741 → 06:44:39.780 UTC = 254.039 s ≈
"approximately 254.0 seconds"; 28 client spans ⊂ 85 total spans; the trace
schema/runtime/source-commit fields match `observability.py` constants and the
PR head commit. The span/token numbers themselves are historical (trace
deleted at cleanup) and cannot be re-derived; they are accepted as internally
consistent testimony, and the report classifies them as evidence, not as a
reproducible artifact.

### 4. Honesty of the 500 → 202 sequence

Honest. The evidence table states plainly that GitHub timed out after 10 s and
recorded 500 for the initial `opened` delivery
([docs/week7-5-live-validation.md:52](../week7-5-live-validation.md)); the
plan's delivery report and AGENDA both repeat the failure. Nothing claims all
acceptance criteria passed unconditionally — the Outcome section is hedged to
"core safety and functionality checks". Two qualifications: the report never
gives a per-criterion disposition, and the plan's criterion "GitHub records an
`opened` pull-request delivery with a 2xx response"
([docs/plans/week7-5-live-validation.md:96-97](../plans/week7-5-live-validation.md))
was satisfied only by the official redelivery row, not the initial attempt
(W75C-P3-02). More importantly, the *causal explanation* attached to the 500
is wrong per the committed code (W75C-P2-01).

### 5. Truncation and signed-replay evidence boundary

Properly bounded, and the truncation story is independently corroborated by
the response serialization order: the Webhook duplicate response is
`{**job, "duplicate": duplicate}`
([service.py:271](../../src/code_review_agent/service.py#L271)), where the job
dict carries the ~30 KB `review_json` before the final `duplicate` key —
so a 3,990-byte retention by GitHub necessarily cuts "inside the completed
review and before the final `duplicate` field", exactly as described. The
report does not claim GitHub's stored record proves `duplicate=true`; it
explicitly says the supplemental signed replay was used to read the complete
response, and the replay's no-op nature is guaranteed by the duplicate
short-circuit above and reflected in the unchanged job/span counts.

### 6. Leak scan of committed documents

Clean. I grepped both new documents for drive-letter and POSIX absolute
paths, key/token/secret material, bearer values, raw diff markers
(`diff --git`), prompt or review text, and command output. None present.
Repository alias, PR number/URL, hook and delivery numeric IDs, delivery
GUID, timestamps, checksums, and aggregate token/cost values are all within
the plan's declared evidence policy. Residual: the task-worktree host path
exists in git history and in PR #3's diff (W75C-P3-01).

### 7. Scoping of the live conclusion

Correct. The report states up front that this is "operational evidence for
one Windows/Python 3.13 run" and "not a production-readiness, Linux,
container, remote OAuth, or remote MCP claim"
([docs/week7-5-live-validation.md:14-15](../week7-5-live-validation.md)).
README explicitly retains "因此不声称生产可用"
([README.md:313-314](../../README.md)).

### 8. Unverified surfaces still listed

Correct. Docker/Linux, MCP Streamable HTTP end-to-end, remote OAuth/identity,
approval binding, and A2A are enumerated as deliberately outside Week 7.5 in
the report ([docs/week7-5-live-validation.md:127-131](../week7-5-live-validation.md)),
the plan's remaining-risks bullet, README, and AGENDA. The frozen Week 7
surface is untouched by the diff and re-verified present in source: three MCP
tools, both resources, the `review_change` prompt
([mcp_server.py:32-67](../../src/code_review_agent/mcp_server.py#L32-L67)),
all REST/Webhook endpoints, and no `approve_patch` anywhere.

### 9. Remaining risks and remediation adequacy

Mostly adequate: response latency, duplicate-response truncation, and the
unverified surfaces are all listed. Two gaps: the first remediation item is
aimed at a mechanism the code does not have (W75C-P2-01), and the
absolute-path residual in history is not listed (W75C-P3-01).

## Independent reproduction of the `gh pr diff` evidence

PR #3 is the frozen-contract branch head `1482b38` against base `dff20d3`,
both present in this repository, so this historical claim is reproducible:
`git diff dff20d3..1482b38` yields 7,099 bytes with 140 newline-terminated
lines; after CRLF normalization it is exactly **7,239 bytes** with SHA-256
**`7035bbfa4ac45e30dee103a5b8ee9ff3671ab4a6c7d80824293f9f0b0ed7de80`** —
byte-identical to the reported "normalized capture". The reported "141-line"
figure is a split-convention count of the same content (140 terminated lines);
the byte count and hash uniquely pin the reviewed diff to the freeze commit.

## Verification command results (rerun on this worktree)

All commands used the designated interpreter with `PYTHONPATH=src` and an
isolated `COVERAGE_FILE` (removed afterwards).

| Command | Result |
| --- | --- |
| `git diff --check` | clean |
| `git diff --stat dff20d3..7f03c31` | 4 files, +310/−3 |
| `unittest tests.test_week7_service_core tests.test_week7_service tests.test_week7_mcp` | Ran 33 tests, OK — matches "33/33" |
| Targeted Ruff (3 src + 3 test files) | All checks passed |
| `mypy src/code_review_agent` | no issues in 26 source files — matches report |
| `scripts/verify.py` | "All offline validation passed"; Ran 593 tests, OK (skipped=3); TOTAL coverage 86% — matches "593 tests, 3 skips, 86%" |
| `code_review_agent.service --help` with `CRAG_SERVICE_PORT=abc` | exit 0 |
| `code_review_agent.mcp_server --help` | exit 0 |

No paid evaluation, model call, webhook, tunnel, or protected asset was
touched by this review.

## P1 findings

None.

## P2 findings

### W75C-P2-01 — Initial-delivery root cause is asserted as a synchronous diff wait, which the committed implementation does not have

- Files:
  [docs/week7-5-live-validation.md:64-68](../week7-5-live-validation.md)
  ("PR diff acquisition is still on the Webhook response path, so a slow `gh`
  call can exceed GitHub's delivery timeout"), the derived remediation bullet
  at [docs/week7-5-live-validation.md:124](../week7-5-live-validation.md),
  [AGENDA.md:199-200](../../AGENDA.md) ("初次 Webhook 因同步等待 PR diff 超过
  GitHub 10 秒期限"), and [README.md:312-313](../../README.md) ("`gh pr diff`
  可能超过 GitHub 10 秒响应期限的运行风险").
- Evidence: the Webhook handler validates the HMAC, parses JSON, and calls
  `submit_pr` on a worker thread, returning 202 immediately afterwards
  ([service.py:238-271](../../src/code_review_agent/service.py#L238-L271)).
  `submit_pr` performs only alias resolution, a SQLite insert, and
  `ThreadPoolExecutor.submit`
  ([service_core.py:586-611](../../src/code_review_agent/service_core.py#L586-L611),
  [service_core.py:551-556](../../src/code_review_agent/service_core.py#L551-L556)).
  `gh pr diff` runs exclusively inside `DefaultReviewRunner.__call__` on the
  executor thread, after the job row is committed and off the response path
  ([service_core.py:205-234](../../src/code_review_agent/service_core.py#L205-L234),
  [service_core.py:248](../../src/code_review_agent/service_core.py#L248)).
  The live run used this exact code (the branch differs from base only in
  docs). The report's own sentence concedes "the job is safely accepted",
  which is only possible because acceptance precedes, and does not wait for,
  diff acquisition.
- Impact: the observed facts (GitHub recorded 500 at its 10 s deadline; the
  service completed 202 later and persisted the job) are honest, but the
  stated mechanism is an unverified inference presented as fact and is
  contradicted by the code. A maintainer implementing the listed remediation
  ("move slow PR-diff acquisition out of the Webhook response deadline")
  would refactor an already-asynchronous path and likely not fix the actual
  latency, whose cause is not established by any committed evidence.
  Candidate causes the evidence cannot distinguish: tunnel/edge latency (the
  official redelivery of a trivial dedup lookup still took 5.07 s), slow
  request-body streaming through the tunnel, host-level contention around the
  first `gh` process spawn, or an event-loop stall. I cannot rule out that
  activity correlated with the first `gh` invocation contributed empirically,
  but that is not "diff acquisition on the response path", and "同步等待" is
  not what the handler does.
- Remediation (documentation-only, by the doc owner): reword the three
  passages to state the observation and mark the cause as not established
  from committed evidence; re-scope the remaining-work bullet to "instrument
  Webhook response-path timing and reproduce the latency before designing a
  fix". If instrumentation later shows queue-side work delaying responses,
  the current wording can be restored with evidence.

## P3 findings

### W75C-P3-01 — Absolute host path persists in branch history and PR #3; not listed as a residual

- Files: [docs/week7-5-live-validation.md:107-112](../week7-5-live-validation.md)
  and the residual list at
  [docs/week7-5-live-validation.md:114-131](../week7-5-live-validation.md);
  history commit `1482b385fc8c7128b25b6e813e9d6aa863203d55`
  (`docs/plans/week7-5-live-validation.md`, "Task worktree" line).
- Evidence: `git diff 1482b38..7f03c31 -- docs/plans/week7-5-live-validation.md`
  shows the task-worktree host path being replaced with "host path
  intentionally omitted". The tip is clean (verified by grep), but the path
  remains reachable in the freeze commit on this branch and in the diff of
  the deliberately kept-open draft PR #3 — the same text the model echoed
  back, per the report's own sanitation note.
- Impact: low — a local directory path with no credential material, in a
  private repository. But "the path was removed from the final contract copy"
  can be read as complete remediation, and the residual-risk list omits the
  history/PR residual, slightly understating the report's otherwise careful
  evidence accounting.
- Remediation: add one residual-risk line acknowledging the path remains in
  branch history and the open PR #3 diff. No history rewrite is recommended;
  whether the residual matters for a private repository is a human call.

### W75C-P3-02 — No per-acceptance-criterion disposition; the "2xx recorded" criterion was met only via redelivery

- Files: [docs/plans/week7-5-live-validation.md:96-97](../plans/week7-5-live-validation.md)
  (criterion) versus the delivery report at
  [docs/plans/week7-5-live-validation.md:122-146](../plans/week7-5-live-validation.md)
  and [docs/week7-5-live-validation.md:49-57](../week7-5-live-validation.md).
- Evidence: GitHub's record for the initial `opened` attempt is 500; the 202
  exists only on the official redelivery row of the same delivery GUID. Both
  facts are reported honestly, and no unconditional-pass claim is made, but
  neither document states per criterion whether the frozen acceptance list is
  considered satisfied, so a reader must reconcile the 2xx criterion with the
  500 themselves.
- Impact: ambiguity only; the substance is disclosed.
- Remediation: add a one-line disposition per acceptance criterion, marking
  this one "met via official redelivery of the same delivery identity; the
  initial attempt was recorded 500 by GitHub".

## Evidence-tier classification

- **Verified by this review (reproducible now):** diff scope and ownership;
  frozen Week 7 interface untouched and present in source; cost arithmetic;
  DeepSeek V4-Pro unit prices (official pricing page fetched 2026-07-19);
  `gh pr diff` byte count and SHA-256 reproduced from git history; all
  offline gates in the table above; the duplicate short-circuit and response
  serialization order backing the idempotency and truncation narratives.
- **Historical, not reproducible (internally consistent testimony):** GitHub
  hook/delivery rows, latencies, and the 3,990-byte retention; trace contents
  (85 spans, 28 model client spans, token totals, 47 redactions); model
  finding counts; tunnel/webhook/cleanup lifecycle. Every arithmetic and
  ordering cross-check on these passed.
- **Inference (reasonable, labeled as such here):** "no additional provider
  calls on redelivery/replay" rests on unchanged job/span counts plus the
  code guarantee that duplicates never queue.
- **Unverified risk:** the actual cause of the initial-delivery latency
  (W75C-P2-01); Docker/Linux, MCP-over-HTTP, remote OAuth/approval/A2A;
  multi-run and non-Windows reliability; provider billing ground truth for
  the cost estimate.

## Conclusion

The Week 7.5 delivery is honest about its one live failure, bounded in its
claims, numerically accurate, and leak-clean at the tip. Actionable findings:
one P2 (misattributed root cause for the initial Webhook 500, in the report,
README, and AGENDA) and two P3s (history-path residual not listed; no
per-criterion acceptance disposition). All are documentation-only corrections
for the document owner; no product code change is required or permitted in
this phase.
