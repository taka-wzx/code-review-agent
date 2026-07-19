# Week 7.5 Live Protocol Validation

Date: 2026-07-19

## Outcome

The bounded live chain passed its core safety and functionality checks. A
private draft pull request triggered exactly one persisted review job through a
temporary GitHub Webhook. The job used `deepseek/deepseek-v4-pro`, reached
`succeeded`, and produced a canonical sanitized trace. Official GitHub
redelivery and a signed replay of the same real payload reused the original
review identity without adding a job or provider request. Invalid HMAC traffic
was rejected with HTTP 401.

This is operational evidence for one Windows/Python 3.13 run. It is not a
production-readiness, Linux, container, remote OAuth, or remote MCP claim.

## Frozen scope and authority

- Base: `master` at `dff20d3f49b9c293c0742b8cfcb50f4941b8230a`.
- Live source commit: `1482b385fc8c7128b25b6e813e9d6aa863203d55`.
- Target: private repository `taka-wzx/code-review-agent`, draft PR
  [#3](https://github.com/taka-wzx/code-review-agent/pull/3), task branch to
  `master`.
- No review comment, approval, merge, close, `master` mutation, OAuth, A2A,
  Repair, paid evaluation, or protected evaluation-asset access occurred.
- Existing credentials were loaded only through the project's normal process
  mechanisms. Fresh service/Webhook secrets existed only in process memory.

## Official tool evidence

| Tool | Version | Verified artifact SHA-256 |
| --- | --- | --- |
| GitHub CLI | 2.96.0 (2026-07-02) | release ZIP `c2d6acc935cd2f00e2144d7e036d5cd82e6b6bd5594e8c75aa75ef2a4ed6aac3`; extracted executable `cd79f16203f1fbe56937c4c96e2b6eadd10549418dcb241d91576ac77af0ac8b` |
| cloudflared | 2026.7.2 (2026-07-15) | `cdb5d4432f6ae1595654a692a51308b69d2bf7af961f5578d9391837cf072df9` |

The checksums matched the corresponding authoritative GitHub Release metadata.
The authenticated CLI confirmed administrative access to the private target.
`gh pr diff 3` returned a non-empty 7,239-byte, 141-line diff; its normalized
capture SHA-256 was
`7035bbfa4ac45e30dee103a5b8ee9ff3671ab4a6c7d80824293f9f0b0ed7de80`.
No diff content is reproduced here.

## Webhook and idempotency evidence

Temporary hook `654333233` subscribed only to `pull_request` and was deleted
after the probes.

| Evidence | Result |
| --- | --- |
| Hook ping delivery `3832062618120486912` | HTTP 200 in 0.94 s |
| Initial `opened` delivery `3832062645580595200` | GitHub timed out after 10 s and recorded 500; the service subsequently logged 202 and persisted the job |
| Delivery GUID | `b24ad0c0-833c-11f1-9bbc-99132fab3584` |
| Official redelivery `3832063602550898688` | Same GUID, `redelivery=true`, HTTP 202 in 5.07 s, same review identity and `succeeded` state |
| Signed replay of the same real payload | HTTP 202, same review identity, `state=succeeded`, `duplicate=true` |
| Invalid-signature public probe | HTTP 401 |
| Counts after all replays | 1 job, 1 persisted delivery identity, 28 model client spans |

The official redelivery response was 30 KB at the service. GitHub retained only
3,990 bytes, cutting the JSON inside the completed review and before the final
`duplicate` field. The supplemental signed replay was therefore used to read
the complete response and prove `duplicate=true`; it did not queue work.

The initial delivery exposes a real operational limitation: PR diff acquisition
is still on the Webhook response path, so a slow `gh` call can exceed GitHub's
delivery timeout even though the job is safely accepted and redelivery is
idempotent. This should be remediated before calling the endpoint production
ready.

## Model, cost, and result evidence

- Review ID: `99e93a3997b7411da456f45370ef47dc`.
- State transition: `queued -> running -> succeeded`.
- Runtime: 2026-07-19 06:40:25.741 UTC to 06:44:39.780 UTC, approximately
  254.0 seconds.
- Provider/model: `deepseek` / `deepseek-v4-pro`.
- Model client spans: 28.
- Input: 545,200 tokens, comprising 395,008 cache-hit and 150,192 cache-miss
  tokens.
- Output: 28,969 tokens, including 19,474 reasoning tokens.
- Findings: 17 candidates, 13 kept, 4 dropped; kept severities were 1 high,
  7 medium, and 5 low. Finding text is intentionally omitted.

DeepSeek did not return a monetary-cost field. Using the provider's prices on
2026-07-19 for V4-Pro (CNY 0.025/M cache-hit input, CNY 3/M cache-miss input,
and CNY 6/M output), the trace-derived estimate is:

```text
395,008 * 0.025 / 1,000,000 = CNY 0.0098752
150,192 * 3     / 1,000,000 = CNY 0.4505760
 28,969 * 6     / 1,000,000 = CNY 0.1738140
total                          CNY 0.6342652
```

This is an estimate rather than provider billing evidence, and it is below the
CNY 5.00 phase cap. Prices are from the
[official DeepSeek model-pricing page](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/).

## Trace and sanitation evidence

- 85 canonical span records, all schema `crag.observability/v1alpha1`, runtime
  `0.1.0`, and source commit `1482b385fc8c7128b25b6e813e9d6aa863203d55`.
- Provider/model, token counts, terminal status, stages, tools, and durations
  were present. The trace did not contain raw diff markers, authorization
  headers, bearer tokens, API-key assignments, or host absolute paths.
- Redaction telemetry recorded 47 redacted/omitted values.
- The review JSON contained two source-derived references to the absolute path
  that the frozen contract itself originally placed in the reviewed diff. The
  trace remained clean, no credential material was involved, and the path was
  removed from the final contract copy. This demonstrates that model output can
  repeat sensitive text supplied as review input; callers must classify input
  content accordingly.

## Cleanup and residual risk

The temporary Webhook was deleted and confirmed absent. The service and tunnel
process trees were stopped, and the task-specific temporary directory and its
downloaded binaries, state, raw response, trace, and logs were removed after
the aggregate evidence above was extracted. The draft PR remains open and
unmerged as the authorized durable evidence.

Remaining work is deliberately outside Week 7.5:

- move slow PR-diff acquisition out of the Webhook response deadline;
- return a compact Webhook acknowledgement instead of embedding a completed
  review on duplicate delivery;
- build `Dockerfile.service` and verify the locked install on Linux;
- exercise MCP Streamable HTTP end to end;
- design and test remote OAuth/identity, approval binding, and later A2A only in
  separately frozen phases;
- obtain broader multi-run, multi-model, and production-network evidence before
  making reliability or security generalizations.

## Offline validation

All required pre-handoff gates passed:

- Week 7 focused `unittest`: 33/33 passed.
- Targeted Ruff: clean.
- mypy: no issues in 26 source files.
- `scripts/verify.py`: 593 tests passed, 3 skipped, total coverage 86% against
  the 85% gate; its Ruff, mypy, module CLI, console CLI, and synthetic security
  checks also passed.
- `code_review_agent.service --help` exited 0 even with
  `CRAG_SERVICE_PORT=abc`; `code_review_agent.mcp_server --help` exited 0.
- `git diff --check`: clean.

The run used only offline validation fixtures. No paid evaluation command or
protected evaluation asset was used.
