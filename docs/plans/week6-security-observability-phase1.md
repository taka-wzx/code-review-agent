# Week 6 Phase 1: Frozen Security and Observability Contract

## Status

This is the normative Phase 1 annex to
`docs/plans/week6-security-observability.md`.

- Phase 0 input commit:
  `74a53dfaf84582a2c2d63bcb94e8aee8e559e4db`
- Phase 1 freeze time: `2026-07-18T08:31:44Z`
- Contract profile: `crag.observability/v1alpha1`
- Runtime implementation: not authorized
- Synthetic case materialization or execution: not authorized
- Docker: not authorized
- External models or agents: not authorized
- Paid evaluation: not authorized
- Existing `eval/`, `eval/holdout/`, SWE-bench material, and sealed Week 4/5
  reporting assets: prohibited and unread

The user authorized read-only Internet lookup of OWASP and OpenTelemetry
official documentation only. No external document was saved into the
repository, no dataset or package was downloaded, and no external service was
called.

## Normative source freeze

### OWASP

The Week 6 risk taxonomy is frozen to:

- title: *OWASP Top 10 for Agentic Applications for 2026*;
- document version: `2026`;
- publication date shown by the document: December 2025;
- official landing page:
  `https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/`;
- official document URL:
  `https://genai.owasp.org/download/52117/?tmstv=1765059207`;
- access date: `2026-07-18`.

The exact risk identifiers and titles used by this contract are:

| ID | Frozen title |
| --- | --- |
| `ASI01` | Agent Goal Hijack |
| `ASI02` | Tool Misuse and Exploitation |
| `ASI03` | Identity and Privilege Abuse |
| `ASI04` | Agentic Supply Chain Vulnerabilities |
| `ASI05` | Unexpected Code Execution (RCE) |
| `ASI06` | Memory & Context Poisoning |
| `ASI07` | Insecure Inter-Agent Communication |
| `ASI08` | Cascading Failures |
| `ASI09` | Human-Agent Trust Exploitation |
| `ASI10` | Rogue Agents |

The older OWASP *Agentic AI Threats and Mitigations* resource may be used as
informative background only. It does not override the frozen 2026 taxonomy.
OWASP describes agent goal hijack as arising when an agent cannot reliably
distinguish instructions from untrusted content, including deceptive tool
output and malicious artifacts. It describes tool misuse as legitimate tool
use steered into harmful outcomes. Those are the primary mappings for the
README/comment and tool-output red-team families.

### OpenTelemetry

The Week 6 semantic mapping is frozen to both:

- OpenTelemetry core Semantic Conventions `1.43.0`, official page
  `https://opentelemetry.io/docs/specs/semconv/`;
- OpenTelemetry GenAI semantic-conventions repository commit
  `63f8200eee093730ce845d26ce2aafb621b0807e`, official revision
  `https://github.com/open-telemetry/semantic-conventions-genai/commit/63f8200eee093730ce845d26ce2aafb621b0807e`.

At freeze time the official GenAI repository:

- declares the GenAI conventions and agent/framework span conventions to be
  **Development**;
- pins core semantic conventions `v1.43.0`;
- has no published release tag;
- has no published schema URL.

Therefore:

1. no Week 6 document or output may call GenAI fields Stable;
2. the immutable GenAI commit, not a floating branch, is authoritative;
3. mappings live behind `crag.observability/v1alpha1`;
4. an upstream rename cannot silently change stored evidence;
5. no OpenTelemetry SDK, exporter, collector, or dependency is required;
6. remote GenAI export is disabled by default and remains a separate approval
   gate.

Stable core fields used by this profile retain their upstream stability
designation. In particular, `error.type` is Stable at the frozen core
revision. A record mixing Stable and Development fields must preserve the
per-field stability metadata in the profile; the record as a whole is not
promoted to Stable.

## Frozen OWASP mapping

The machine-readable mapping is per case in
`security_redteam/case-plan.json`. This table explains the mapping rule:

| Local family | Primary OWASP risks | Rationale |
| --- | --- | --- |
| prompt injection | `ASI01`; case-specific `ASI02`, `ASI03`, `ASI08`, `ASI09` | untrusted repository text attempts to redirect goals, tools, authority, budgets, or human trust |
| tool-output injection | `ASI01`, `ASI02`; case-specific `ASI03`, `ASI09` | tool results are untrusted content and cannot grant a second tool call or approval |
| secret access | `ASI02`, `ASI03`; case-specific `ASI05`, `ASI06` | sensitive reads misuse capability or privilege; aliases and exception/context leaks add execution or poisoning paths |
| path aliasing | `ASI02`, `ASI03`, `ASI05` | traversal and aliases turn an allowed filesystem tool into access or execution outside its authority |
| command execution | `ASI02`, `ASI05`; case-specific `ASI08` | argument, shell, interpreter, test, and network behavior are unexpected code execution and can cascade |
| resource exhaustion | `ASI02`, `ASI08`; case-specific `ASI01` | legitimate calls are amplified into cost, time, output, or availability failures |
| authorization | `ASI03`, `ASI10`; case-specific `ASI06`, `ASI07`, `ASI09` | stale/replayed authority and corrupted state can create a rogue, scope-divergent run |
| trace/export | `ASI01`, `ASI06`, `ASI08`, `ASI09`; case-specific `ASI02` | forged or poisoned evidence weakens detection and human verification; exporter failure must not cascade into policy change |

`ASI04` remains in the frozen taxonomy but has no mandatory Phase 3 case. A
package, MCP descriptor, model, image, or external corpus supply-chain probe
would require a new contract and separate acquisition approval. This omission
is explicit and must not be hidden as Top-10-complete coverage.

`ASI07` is exercised only by approval replay in `W6-AU-03`; Week 6 does not
claim comprehensive multi-agent communication coverage.

## Frozen case-plan contract

`security_redteam/case-plan.json` freezes:

- 36 adversarial identities;
- 12 matched benign-control identities;
- titles, family, OWASP mapping, expected outcome, platform class, matching
  relationships, and observable forbidden-effect identifiers;
- zero host credential reads, zero network attempts, and zero host process
  starts; command controls use a bounded recording fake executor;
- `materialized:false` and every later-phase authorization flag as `false`.

It contains no attack payload, real repository content, external task,
credential value, executable command, or stochastic result.

### Per-case hash algorithm

For each case:

1. remove the `case_spec_sha256` member;
2. serialize the remaining case object as UTF-8 JSON with keys sorted
   lexicographically, no insignificant whitespace, non-ASCII characters
   preserved, and JSON separators `,` and `:`;
3. calculate lowercase SHA-256 over those bytes.

The current case objects use only strings and arrays of strings, so the
algorithm has no floating-point or Unicode-normalization ambiguity. A later
schema change adding another JSON type requires a new hash-algorithm
identifier.

The runner must recompute every hash before materialization and reject a
mismatch, duplicate case ID, unknown risk ID, missing referenced control or
attack, count mismatch, forbidden real target, or non-false authorization
flag. It must not silently replace or rewrite a frozen case after observing a
result.

## Frozen telemetry hierarchy

One logical invocation has one root trace:

```text
invoke_agent code-review-agent                  INTERNAL
|-- crag.stage {stage}                          INTERNAL
|   |-- chat {gen_ai.request.model}             CLIENT
|   `-- execute_tool {gen_ai.tool.name}         INTERNAL
|       `-- crag.sandbox {command-category}     INTERNAL
|-- crag.policy {operation}                     INTERNAL
|-- crag.checkpoint {save|restore}              INTERNAL
`-- crag.telemetry.export                       INTERNAL
```

Finder and Verifier work that overlaps in time is represented as sibling
spans. A logical model span covers all transport retries for that request, as
required by the frozen inference-span guidance. Individual retries remain
bounded span events with `crag.retry.*` attributes.

Project stage, policy, sandbox, checkpoint, approval, cost, and telemetry
operations do not pretend to be standard GenAI operations. They use the
`crag.*` extension namespace.

### Required canonical envelope

Every canonical span record must have:

- `schema_version` equal to `crag.observability/v1alpha1`;
- one nonzero lowercase 32-hex trace ID;
- one nonzero lowercase 16-hex span ID;
- a valid parent span ID except on the root;
- a bounded stable run ID;
- start/end UTC timestamps and monotonic duration;
- span kind, name, status, source commit, runtime version, and redaction policy;
- bounded attributes and events;
- deterministic UTF-8 JSON serialization with non-finite values rejected.

The validator rejects duplicate IDs, unknown parents, parent cycles, negative
or non-finite durations, an end before start, mutation after end, invalid
status/error combinations, oversize data, forbidden content fields, absolute
host paths, and synthetic canaries.

### OpenTelemetry fields

The exact field list and requirement are machine-readable in
`security_redteam/phase1-profile.json`.

The Development GenAI projection includes:

- `gen_ai.operation.name`;
- `gen_ai.provider.name`;
- `gen_ai.agent.id`, `.name`, and `.version`;
- `gen_ai.request.model`;
- `gen_ai.response.id`, `.model`, and `.finish_reasons`;
- input, output, cache-read, cache-creation, and reasoning token usage;
- `gen_ai.tool.name`, `.call.id`, and `.type`.

`error.type` is the Stable error field. Week 6 additionally uses the bounded
`crag.error.category` enum:

- `auth`;
- `rate_limit`;
- `timeout`;
- `connection`;
- `provider`;
- `invalid_response`;
- `budget_exhausted`;
- `policy_denied`;
- `approval_rejected`;
- `sandbox_violation`;
- `telemetry_write`;
- `telemetry_export`;
- `redaction_failure`;
- `internal`.

Unknown provider token counts remain absent/unknown. They are never invented
as zero. The input-token total follows the provider's billed count and may
include cache-read tokens; cache-read and cache-creation values are recorded
separately only when reported.

`gen_ai.provider.name=deepseek` is the standard provider value for DeepSeek.
The existing project provider identifier `glm` is retained as a documented,
lowercase custom value because the frozen semantic convention permits custom
providers and does not define a GLM well-known value.

### Content fields

The following upstream opt-in content fields are hard-disabled for
`v1alpha1`, even if a caller requests verbose telemetry:

- `gen_ai.input.messages`;
- `gen_ai.output.messages`;
- `gen_ai.system_instructions`;
- `gen_ai.tool.call.arguments`;
- `gen_ai.tool.call.result`.

Raw prompt/source/diff content, tool arguments/results, stdout/stderr,
exception messages, authorization headers, environment data, and absolute
host paths are also forbidden in local JSONL, console output, reports, and
remote export.

Only bounded metadata may be recorded: byte counts, redaction counts, safe
enumerations, repository-relative validated identifiers, exit metadata, and
SHA-256 of an explicitly approved non-secret artifact. A plain digest of a
secret is forbidden. Keyed sensitive correlation is disabled unless an
operator supplies an ephemeral key outside repository/model control; Week 6
does not persist that key.

## Frozen limits and storage policy

| Limit | Frozen value |
| --- | ---: |
| one serialized record | 65,536 bytes |
| attributes per span | 64 |
| attribute key | 128 bytes |
| attribute string | 1,024 characters |
| array items | 32 |
| nested depth | 8 |
| events per span | 128 |
| links per span | 32 |
| export queue | 1,024 records |
| one local audit file | 67,108,864 bytes |
| rotated files | at most 5 when explicitly enabled |
| remote export retry | at most 2 attempts / 2,000 ms total |

Local security, policy, approval, budget, cleanup, and error records are never
sampled. Remote export is disabled. Repository/model content cannot set
endpoints, credentials, TLS, sampling, retention, or redaction.

There is no automatic deletion default. Audit output remains operator-owned.
Rotation is disabled until an operator supplies an explicit path and policy.
This avoids silently deleting evidence. Crossing the file cap is a local audit
failure, not permission to overwrite or discard security records.

Repair requires a writable local audit sink before any protected mutation.
Local audit failure stops before the next mutation and triggers quarantine
when state may be uncertain. A read-only Review may return only with an
explicit `telemetry_degraded` terminal state. Optional remote failure creates
bounded local degraded evidence and cannot relax policy.

## Compatibility freeze

- Existing `Trace.event(kind, **data)` and `tev(...)` callers are bridged
  through a bounded adapter.
- Existing flat JSONL remains readable through project `0.2.x`.
- Earliest removal is `0.3.0`, after a separately approved migration gate.
- New writes default to canonical records; dual emission is off by default.
- Compatibility never preserves raw prompt/tool/exception payloads.
- Review CLI (`python -m code_review_agent` and `crag`), Repair checkpoints,
  approval bindings, sandbox decisions, and Week 4/5 evidence formats do not
  change silently.
- No new dependency, SDK, OTLP protocol, network exporter, collector, or
  public CLI flag is authorized in Phase 1.

## Phase 2--3 exact proposed Single Writer ownership

This list is frozen as the maximum proposed Codex write scope for Phase 2--3.
It is **not active authorization**. The user must authorize Phase 2 before any
path below is modified or created.

Implementation:

- `src/code_review_agent/observability.py`
- `src/code_review_agent/redaction.py`
- `src/code_review_agent/tracelog.py`
- `src/code_review_agent/agent.py`
- `src/code_review_agent/agentloop.py`
- `src/code_review_agent/orchestration.py`
- `src/code_review_agent/tools.py`
- `src/code_review_agent/verifier.py`
- `src/code_review_agent/repair.py`
- `src/code_review_agent/repair_tools.py`
- `src/code_review_agent/sandbox.py`
- `src/code_review_agent/repair_approval.py`
- `src/code_review_agent/repair_budget.py`
- `src/code_review_agent/repair_checkpoint.py`

Validation and synthetic inputs:

- `scripts/verify_security.py`
- `tests/test_observability.py`
- `tests/test_redaction.py`
- `tests/test_security_redteam.py`
- `tests/test_week2_orchestration.py`
- `tests/test_week3_repair.py`
- `tests/test_week3_tools.py`
- `security_redteam/README.md`
- `security_redteam/cases.jsonl`
- `security_redteam/schemas/case.schema.json`
- `security_redteam/schemas/report.schema.json`

Documentation and frozen-input maintenance:

- `docs/security-observability.md`
- `docs/plans/week6-security-observability.md`
- `docs/plans/week6-security-observability-phase1.md`
- `README.md`
- `AGENDA.md`
- `security_redteam/phase1-profile.json`
- `security_redteam/case-plan.json`
- `security_redteam/schemas/phase1-profile.schema.json`
- `security_redteam/schemas/case-plan.schema.json`

Any necessary path outside that list requires a contract amendment before the
edit. `pyproject.toml`, lockfiles, CI/workflow files, Dockerfiles, existing
evaluation assets, and sealed results remain read-only. Claude's later Single
Writer scope is only `docs/reviews/week6-claude.md`.

## Implementation sequencing after approval

Phase 2 must remain one serial Codex writer:

1. implement canonical IDs, clocks, lifecycle, serializer, validation, and
   redaction without instrumenting business logic;
2. bridge legacy JSONL and prove compatibility;
3. instrument one Review vertical path and validate trace relationships;
4. instrument Verifier and remaining Review tools;
5. instrument Repair model, tool, sandbox, budget, approval, checkpoint,
   cleanup, and terminal paths;
6. cross-check token, cost, latency, retry, tool, policy, and failure
   aggregates from spans;
7. stop for Phase 2 validation before materializing Phase 3 cases.

Phase 3 then materializes the frozen identities without changing their titles,
mapping, expected outcomes, forbidden effects, or hashes. If a case is
impossible to measure safely, it fails the gate; it is not dropped.

## Frozen artifact hashes

The SHA-256 values below bind each machine-readable Phase 1 input after
decoding it as UTF-8 and normalizing `CRLF` or bare `CR` line endings to `LF`,
with no other content normalization. This makes the binding independent of
Git's platform checkout line-ending conversion. The annex itself is bound by
the Phase 1 Git commit.

| Path | SHA-256 |
| --- | --- |
| `security_redteam/phase1-profile.json` | `5ece56db02b69a276216aeead208203e5143e3585393fc807ba4c5a5aeb2bc79` |
| `security_redteam/case-plan.json` | `b5bc761dd0494ac5f11e427eb1ff015b7bb0cf8d1f79b5e2a78280900162f4b7` |
| `security_redteam/schemas/phase1-profile.schema.json` | `f7db2a4e68151fe36160261b016d68fef85ea8bbdcf32c4f576aa6af5919fb28` |
| `security_redteam/schemas/case-plan.schema.json` | `a294dc48dfa1325d7556a1f39638ddf1eeda3636be5b9fc63c4d773fc56f097a` |

Hashes are calculated over the canonical UTF-8/LF bytes after validation and
before the Phase 1 commit. Changing any bound file content requires updating
this annex in a new contract-amendment commit.

## Phase 1 validation gate

Phase 1 is complete only if:

- all four JSON files parse with duplicate-key rejection;
- schema identities, frozen source revisions, counts, IDs, risk mappings,
  matching references, authorization flags, and per-case hashes pass an
  independent standard-library validator;
- all 48 case IDs are unique and exactly 36 adversarial plus 12 controls;
- all official risk IDs are from the frozen ASI01--ASI10 taxonomy;
- file hashes match this annex;
- `git diff --check` passes;
- changed paths are exactly the six Phase 1-owned paths;
- the normal offline repository verifier passes without `--eval-assets`;
- no data, dependency, Docker, external model, paid evaluation, or prohibited
  evaluation asset is accessed.

Phase 1 completion does not authorize Phase 2.
