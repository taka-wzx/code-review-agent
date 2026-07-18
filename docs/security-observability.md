# Security and observability operations

## Current scope

Week 6 Phase 2 implements the offline observability core for Review, Verifier,
and Repair. It does not materialize or execute the Phase 3 red-team corpus and
does not authorize Docker, an external model, a remote collector, or an
evaluation asset.

The normative profile is `crag.observability/v1alpha1`, frozen in:

- `docs/plans/week6-security-observability-phase1.md`;
- `security_redteam/phase1-profile.json`;
- `security_redteam/schemas/phase1-profile.schema.json`.

The profile projects the frozen OpenTelemetry GenAI Development conventions
without adding an OpenTelemetry SDK dependency. Project-specific operations
remain under `crag.*`.

## Local traces

Review tracing remains opt-in:

```powershell
crag sample.diff --repo . --trace traces\review-001.jsonl
```

The path must be new. The writer creates the file with restrictive permissions
where the platform supports them and refuses to truncate or mix with an
existing audit file. Use a unique path for every invocation.

Repair creates a unique file automatically under:

```text
<state_root>/<run_id>/observability-<random-id>.jsonl
```

The mandatory Repair sink is initialized before the orchestrator can ask a
model, execute a tool, mutate the task worktree, request approval, or commit.
A local telemetry failure is a hard run failure; it never grants authority or
changes an approval decision.

Every file contains one root `agent.run` trace and bounded descendants:

```text
agent.run
|-- agent.stage
|   |-- llm.request
|   `-- tool.execute
|       `-- sandbox.command
|-- policy.decision
|-- checkpoint
`-- telemetry.export
```

Finder and Verifier worker contexts are explicitly propagated, so parallel
lanes are sibling descendants of the relevant stage rather than accidental
roots.

## Recorded metadata

When a provider supplies it, model spans record provider/model identity,
response identity/model, finish reasons, configured maximum output tokens and
temperature, input/output/cache/reasoning tokens, duration, and stable error
classification. Missing provider measurements are omitted; they are not
invented as zero.

Repair records integer `crag.cost.micro_usd`, settlement status, tool and
sandbox counts, bounded command categories, argv/input/output byte counts,
exit/timeout/truncation metadata, approval and state-transition decisions,
checkpoint operations, and terminal state. It never records command text,
patch text, stdout/stderr, approval challenge text, or exception messages in
the trace.

`aggregate_trace()` derives or cross-checks:

- input, output, cache, reasoning, and total tokens;
- integer micro-USD cost;
- LLM, tool, policy, sandbox, checkpoint, stage, retry, error, degraded, and
  fail-open counts;
- run, model, tool, policy, and sandbox durations;
- root status and telemetry mode.

## Redaction and configuration boundary

Redaction happens before any serializer or exporter receives a record.
`week6-redaction-v1`:

- hard-disables raw GenAI message, system-instruction, tool-argument, and
  tool-result fields;
- drops prompt, source, diff, environment, header, cookie, credential,
  stdout/stderr, exception-message, and similar keys;
- detects common credential/private-key/canary shapes, including adjacent
  split fragments;
- omits sensitive relative filenames and absolute host paths while allowing
  `.env.example`;
- normalizes ASCII and Unicode control characters;
- caps keys, strings, arrays, nesting, attributes, events, records, and files;
- omits unknown objects, bytes, sets, and non-finite numbers instead of using
  `default=str`.

The active redaction configuration is the versioned profile, not repository or
model input. Raw-content opt-in is unavailable in `v1alpha1`; adding a looser
mode or public CLI switch requires a separately reviewed profile amendment.
Operators may choose whether tracing is enabled for read-only Review, but
Repair cannot disable its mandatory local audit sink.

## Export and failure behavior

Remote export is disabled by default. Optional exporters are programmatic
operator integrations only. They receive deep-copied, already-sanitized
records. The first failure circuit-breaks that exporter for the remainder of
the trace, writes bounded local degraded evidence, and does not affect tool,
policy, approval, or sandbox authority.

The compatibility adapter still reads historical flat JSONL and projects
`legacy.*` events from canonical records for project `0.2.x`. New writes are
canonical only. Removing the compatibility view cannot occur before `0.3.0`
and requires a separate migration gate.

## Offline validation

Use the worktree source explicitly because the shared editable environment may
point at another worktree:

```powershell
$env:PYTHONPATH = "<week6-worktree>\src;<week6-worktree>\tests"
<python> -m unittest tests.test_observability tests.test_redaction -v
<python> scripts\verify.py
```

Do not add `--eval-assets`. Phase 2 validation does not read existing
`eval/`, `eval/holdout/`, SWE-bench, Week 4 reporting, or Week 5 reporting
assets.
