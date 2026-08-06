# Phase 11D: Human Review-to-Repair Pilot v1

Status: **Gate A complete - default-closed Gate B executor implementation; real Pilot is closed pending exact approval**

## Scope revision: hash-only Gate B preparation (2026-08-05)

The repository owner directed Codex to prepare the remaining Gate B authorization
inputs. This revision authorizes only offline generation and validation of a
credential descriptor containing stable identifiers and SHA-256 fingerprints, plus
an external restricted-directory Gate B draft. It does not authorize reading raw
credentials, minting tokens, opening provider or GitHub transports, selecting real
PRs, pushing a branch, creating a Pilot-generated Draft PR, or enabling Gate B.

The exact real executor, frozen runtime/deployment digests, canonical authorization
digest, and owner exact-approval text remain required before any real operation.

## Scope revision: offline Gate B runtime preflight (2026-08-06)

The repository owner directed Codex to continue Phase 11D after Gate A CI passed.
This revision authorizes a standard-library-only, default-closed preflight that
freezes and validates source, executable, runtime, deployment, and runtime-identity
SHA-256 descriptors. The preflight has no credential input, no transport code, and
always records `execution_capability=preflight_only` with every real-operation flag
false. It may not be presented as a Gate B executor or used to enable a real Pilot.

## Scope revision: default-closed Gate B executor implementation (2026-08-06)

The repository owner explicitly requested continuation after Gate A delivery. This
revision authorizes Codex to implement and test a real Gate B executor behind a
separate, default-closed runtime gate. It does **not** by itself authorize a real
Provider call, access to a raw credential, a real repository read, a branch push, or
Draft Repair PR creation. Those effects remain conditional on the complete
authorization bundle and the exact owner approval described below.

The implementation may:

- add `phase11d_gate_b_executor.py` and extend
  `tests/test_phase11d_human_pilot.py` with standard-library fakes and negative cases;
- read only explicitly supplied hash-only manifests and the explicitly supplied
  credential file at execution time; raw credential bytes must remain in memory only,
  never be printed, persisted, logged, or included in receipts;
- use strict HTTPS transports limited to the frozen Zhipu chat endpoint and the
  allowlisted GitHub App/repository read and Git object/Draft-PR endpoints;
- enforce the frozen participant, repository, PR-selection, budget, retention,
  kill-switch, and branch protections before any credential access or network call;
- create at most one Pilot repair branch, one commit, one push, and one Draft Repair
  PR, with comments/checks/labels/reviews, Ready, merge, and protected/default branch
  mutation permanently disabled;
- emit only sanitized, hash-bound receipts outside the repository's committed source
  tree.

The executor must remain `execution_capability=closed` unless all of these checks
pass at the same invocation: the authorization bundle's canonical SHA matches the
frozen inputs, the exact approval text and its SHA match the bundle, the UTC window is
active, the credential fingerprints match, the repository installation identity and
allowlist match, and the runtime kill switch is inactive. A generic request to
continue is not a substitute for the exact approval text; the current external draft
therefore remains closed until it is finalized and approved.

The default-closed implementation includes a one-job human Finding/WRITE/DRAFT_PR
state machine, an offline/non-root sandbox-evidence binding, a bounded GLM review
budget, a GitHub Git-data/Draft-PR publisher with an external sanitized journal, and
read-back restart reconciliation. All top-level credential/network execution paths,
and the publisher before every publish or reconciliation attempt, re-hash the actual
executing source/runtime before opening a transport. Lower-level dependency-injected
clients have no user-facing command and are invoked only after their caller completes
that gate. These implementation components do not create a real Pilot record,
substitute for human decisions, or authorize a real operation by themselves.

Task branch: `codex/phase11d-human-pilot-v1`

Fetched baseline: `origin/master` at
`4af4b2756e8d2de6764d08e17a6e12040e24975e`.

Local `master` at task start was
`21344a2b72be8cb83361875b5cc8f2952e99ffbf`, behind `origin/master`; Codex did not
fast-forward, merge, rebase, push, or otherwise mutate `master`. This task branch was
created directly from the fetched `origin/master` baseline above.

## Claim and authorization boundary

Phase 11D Gate A prepares a real Human Review-to-Repair Pilot protocol, but executes
only offline fakes and validators. It does not enroll participants, read real Pilot
repositories or PR diffs, call a provider, spend money, push Pilot-generated branches,
create Pilot-generated Draft Repair PRs, comment, label, check, review, deploy, merge,
or mutate a protected/default branch.

The permanent Phase 11C and auth-004 facts are inherited:

- Phase 11C DIAGNOSTIC completed with receipt SHA-256
  `97a887015e95e02e94460979dd170b36d01558ce71b882df272b1d2e8aa0a41c`.
- Phase 11C HEADLINE_COHORT ended `inconclusive`; target 1 returned
  `text_only_response`.
- Phase 11C HEADLINE cohort receipt SHA-256 is
  `107f664a6fb1f11caeb85682b648472e351f61b34b4db987ff7b32f3d0e1f146`.
- Phase 11C HEADLINE ledger SHA-256 is
  `680f3cc1938856cfcc00b1f9a9c1aa3dc233c97d6bd794f8409d039817760419`.
- Phase 11C final evidence archive SHA-256 is
  `e269f4394a25a812b4a2ac08e3c7b1dbc396e9356b5f522286372bae65abb9f2`.
- auth-004 remains `5/5` headline failed and `0` completed. It must not be rerun,
  replaced, backfilled, or revised.

Phase 11D must always treat provider text-only responses, schema/tool-call mismatch,
publisher ambiguity, usage ambiguity, receipt mismatch, stale approval, authorization
drift, and missing receipt as fail-closed terminal outcomes. Phase 11C does not prove
provider tool-call reliability.

Every Phase 11D Gate A artifact must keep:

```text
gate=gate_a
real_model_calls=false
real_github_writes=false
real_pilot_executed=false
business_claim_allowed=false
model_quality_status=not_measured
formal_quality_status=incomplete
production_ready=false
```

## Gate B remains closed

Gate B can start only after a future, separate owner approval exactly matches a frozen
authorization bundle and all required hashes. A generic request to continue, authorize,
or open permissions is insufficient.

The Gate B authorization bundle must fill and hash-bind all fields required by the
user brief: authorization identity, source tree, executable/source, runtime/image,
deployment, runtime identity, organization, 3-5 confirmed-real participants, per-person
roles and consent receipt hashes, repository allowlist, base SHA rules, UTC PR window,
deterministic selection rule and seed, 20-30 selected PRs, repair/job/branch/commit/
push/Draft-PR ceilings, GitHub App installation/scopes, provider/model/endpoint, call/
HTTP/token/cost/wall-clock budgets, data classification, provider-sendable code scope,
retention/deletion/incident/kill-switch owners, credential delivery/fingerprint/revoke
procedure, human approval SLA, business success thresholds, safety stops, and cost
stops.

Any missing field, mismatched SHA, expired/inactive window, permission denial, or exact
approval-text mismatch blocks Gate B before credential access, provider transport,
GitHub transport, or any Pilot-generated write.

## Single Writer declaration

Codex owns exactly these paths for Phase 11D Gate A:

- `docs/plans/phase11d-human-pilot-v1.md`;
- `docs/phase11d-human-pilot-v1.md`;
- `phase11d_human_pilot.py`;
- `phase11d_human_pilot/**`;
- `phase11d_gate_b_executor.py` (default-closed Gate B executor);
- `schemas/phase11d-authorization.schema.json`;
- `schemas/phase11d-cohort.schema.json`;
- `schemas/phase11d-review-receipt.schema.json`;
- `schemas/phase11d-repair-receipt.schema.json`;
- `schemas/phase11d-draft-pr-receipt.schema.json`;
- `schemas/phase11d-feedback.schema.json`;
- `schemas/phase11d-incident.schema.json`;
- `schemas/phase11d-business-report.schema.json`;
- `schemas/phase11d-claim-decision.schema.json`;
- `schemas/phase11d-acceptance.schema.json`;
- `schemas/phase11d-canonical-manifest.schema.json`;
- `tests/test_phase11d_human_pilot.py`.

All other paths are read-only unless this contract is revised before editing. In
particular, `eval/**` and `eval/holdout/**` must not be enumerated, read, run, copied,
or modified.

## Offline implementation requirements

- Use only Python standard-library code in the Phase 11D standalone tool.
- Reject duplicate JSON keys, unknown fields, missing required fields, bools where
  integers are required, floats for counters/currency, malformed hashes, relative UTC
  times, and raw-content-bearing fields.
- Generate deliberately incomplete Gate B templates that validate as templates while
  keeping every real operation closed.
- Validate a complete offline bundle containing authorization, consent, repository
  allowlist, cohort, selection, immutable headline manifest, sanitized Review/Repair/
  Draft-PR/feedback/time/cost/incident receipts, business report, claim-decision
  report, final acceptance report, and canonical SHA-256 manifest.
- Keep synthetic rows and real rows separated; any synthetic row forces
  `business_claim_allowed=false`.
- Enforce 3-5 participants, 20-30 selected PRs for real Gate B bundles, one immutable
  headline per selected PR, no replacement after failure, and stable denominator
  treatment for missing, failed, refused, timed-out, or no-feedback records.
- Enforce that only `maintainer` and `org_admin` actors can start Repair, approve
  WRITE, or approve DRAFT_PR. `viewer`, `reviewer`, `webhook`, `github_webhook`,
  `model`, `finding`, `agent`, `system`, and unauthenticated actors cannot approve.
- Enforce exact Review-to-Repair ordering: human-selected Finding, exact Repair Plan,
  WRITE approval, isolated branch/worktree receipt, sandbox patch/test/reflect, DRAFT_PR
  approval view, exact approved commit, Draft PR publisher receipt, then human feedback.
- Enforce network default closed, provider/GitHub endpoint allowlists, finite retry and
  budget ceilings, kill switch stops, no protected/default branch mutation, no merge API,
  and no Pilot-generated PR Ready/merge.
- Receipts, reports, traces, and templates may contain only stable IDs, enums,
  booleans, non-negative integers, UTC timestamps, and SHA-256 values. They must not
  contain API keys, tokens, raw credentials, repository locators, raw diffs, prompts,
  provider responses, exception messages, stdout/stderr, host paths, comments, or
  free-form source content.

## Required offline fakes and negative cases

Tests must cover:

- full synthetic Gate A bundle generation and validation;
- incomplete Gate B authorization and any permission switch set to false;
- viewer/reviewer/webhook/model/Finding/agent/system start or approval attempts;
- double approval race/replay;
- stale approval, base/head drift, policy/checkpoint/patch/test/budget mismatch;
- test failure, approval decline, budget exhausted, kill switch, credential revoke;
- provider failure, provider text-only response, malformed tool response, usage unknown;
- publisher failure, ambiguous publisher result, crash after GitHub success before local
  receipt, restart reconciliation/quarantine;
- tenant isolation, repository allowlist mismatch, synthetic/real mixing, redaction scan;
- Draft PR accidentally Ready or merged;
- auth-004 and Phase 11C receipt boundaries remaining unchanged.

## Validation

The following delivery-validation commands are offline and must not access `eval/**`,
credentials, provider APIs, GitHub product writes, cloud control planes, or paid
services. The separately named Gate B executor commands remain default-closed and may
open their exact allowlisted transport only after final authorization validation.

```powershell
python -m unittest -v tests.test_phase11d_human_pilot
python phase11d_human_pilot.py validate-bundle --bundle phase11d_human_pilot/examples/gate_a
python phase11d_human_pilot.py generate-gate-b-template --output phase11d_human_pilot/templates/gate_b_authorization.template.json
python -m unittest discover -s tests
python -m ruff check .
python -m mypy src/code_review_agent phase11d_human_pilot.py
python -m mypy phase11d_gate_b_executor.py
python scripts/verify.py
python -m pip check
git diff --check
git diff --name-only 4af4b2756e8d2de6764d08e17a6e12040e24975e...HEAD
```

Docker/Postgres/container tests are required only when the implementation touches the
existing service/container path. This Gate A tool is standalone and uses no Docker or
Postgres runtime; existing repository validation still covers the pre-existing service
code.

## Delivery

After local validation and diff review, create one stable task-branch commit. The user
brief authorizes pushing only `codex/phase11d-human-pilot-v1` and creating or updating
one Phase 11D implementation Draft PR. It does not authorize pushing, merging, or
rebasing `master`, running Gate B, real provider calls, Pilot-generated GitHub writes,
comments/checks/labels/reviews, Ready/merge of Pilot-generated PRs, or automatic merge.

## Change control

This contract is frozen after creation. Any new dependency, package public interface
change, migration, workflow change, production service route, real data access, real
provider/GitHub operation, additional writable path, or broader claim requires an
explicit contract revision before implementation.
