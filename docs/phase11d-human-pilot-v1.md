# Phase 11D Human Review-to-Repair Pilot v1

Status: **Gate A complete; the default-closed Gate B executor, Repair control flow,
and Draft PR publisher are implemented, but real Pilot execution remains closed** until
an owner approval exactly matches the final frozen authorization bundle.

## Scope

This Gate A package adds a standard-library-only offline tool:

```powershell
python phase11d_human_pilot.py generate-gate-a --output phase11d_human_pilot/examples/gate_a
python phase11d_human_pilot.py validate-bundle --bundle phase11d_human_pilot/examples/gate_a
python phase11d_human_pilot.py generate-gate-b-template --output phase11d_human_pilot/templates/gate_b_authorization.template.json
python phase11d_human_pilot.py generate-credential-descriptor --help
python phase11d_human_pilot.py freeze-gate-b-preflight --help
python phase11d_gate_b_executor.py freeze-runtime --help
python phase11d_gate_b_executor.py freeze-authorization --help
python phase11d_gate_b_executor.py approve-authorization --help
python phase11d_gate_b_executor.py validate-authorization --help
python phase11d_gate_b_executor.py select-pull-requests --help
python phase11d_gate_b_executor.py review-selected-pull-requests --help
```

The tool generates and validates synthetic receipts, reports, and a canonical manifest.
It does not call providers, read credentials, open network transports, enroll real
participants, push Pilot-generated branches, create Pilot-generated Draft Repair PRs,
comment, label, check, review, merge, or mutate protected/default branches.

`generate-credential-descriptor` accepts only SHA-256 fingerprints and stable
identifiers. It never accepts a private key, API key, token, or credential file;
the resulting descriptor is self-bound by `credential_descriptor_sha256` and is
safe to keep in the restricted Gate B authorization directory.

`freeze-gate-b-preflight` derives and self-binds the five runtime SHA-256 values
needed by a future Gate B authorization. It has no credential argument and always
records `execution_capability=preflight_only`; it cannot enable a real Pilot.

`phase11d_gate_b_executor.py` is a separate, standard-library control plane for the
future Gate B runtime. It freezes the actual executor source/runtime hashes, binds the
authorization draft to the participant, repository, and credential descriptors,
generates the canonical SHA-256 and exact owner approval text, and validates that text
before enabling a transport. Its GitHub App path uses only a short-lived installation
token minted from an explicitly named private-key file after the full local gate
passes. It does not use `gh` state, PATs, Git credential helpers, or a merge API.

Every top-level execution command that can reach a credential or network transport,
and the publisher before each publish or reconciliation attempt, re-hashes the actual
executing source, lock file, interpreter/runtime identity, and deployment descriptors
before it reads a credential. Lower-level dependency-injected clients have no
user-facing command and are invoked only after their caller completes that gate. A
stale runtime descriptor, source-root mismatch, expired authorization, wrong
installation identity, or inactive kill switch fails closed before a Provider or
GitHub transport opens.

The selection command is deterministic: it verifies the immutable GitHub repository
ID and normalized locator hash, considers only in-window open non-draft PRs on the
frozen base branch, ranks `pr-<number>` using the frozen seed plus a newline separator,
and refuses to proceed unless it can select exactly the authorized 20--30 PRs. The GLM
review client accepts only a structured `submit_review` tool call and records hashes,
counts, and stable terminal categories, never raw diffs, prompts, responses, or
credentials in a receipt.

`review-selected-pull-requests` consumes that immutable selection receipt and reads
back each selected PR before sending its bounded diff to the frozen GLM client. It
emits exactly one hash-only row per selected PR. A snapshot mismatch, GitHub read
failure, provider text-only response, tool/schema/usage ambiguity, redaction failure,
or budget failure stops new provider calls; unattempted denominator rows remain as
`cohort_stopped`. The command performs no GitHub write and cannot start Repair,
approve WRITE/DRAFT_PR, or publish a Draft PR.

The one-job `GateBRepairCoordinator` requires the exact sequence: a confirmed
maintainer/org-admin selects a completed-review Finding; an exact in-memory Repair Plan
is hash-bound; a single-use WRITE approval is consumed; an isolated offline/non-root
sandbox returns hash-bound patch, test, checkpoint, budget, reflection, tree, and
commit evidence; a second single-use DRAFT_PR approval binds that exact commit; then
the publisher may act. The coordinator does not invent selections, plans, approvals,
or sandbox evidence.

`GitHubDraftPublisher` is limited to repository verification, ref read-back, Git data
objects, one dedicated `crag/phase11d/` branch, and one Draft PR. It has no PATCH, PUT,
Ready, merge, comment, check, label, or review route. It writes a sanitized journal
outside the source tree, verifies the exact tree/commit/ref and Draft state, and
reconciles an ambiguous post-write restart by read-back; unresolved ambiguity is
quarantined. The implementation tests use fakes only and have made no real Provider
call, GitHub write, Pilot branch, or Pilot Draft PR.

## Preserved Boundaries

- Phase 11C `DIAGNOSTIC` completed with receipt SHA-256
  `97a887015e95e02e94460979dd170b36d01558ce71b882df272b1d2e8aa0a41c`.
- Phase 11C `HEADLINE_COHORT` ended `inconclusive`; target 1 returned
  `text_only_response`.
- Phase 11C headline cohort receipt SHA-256:
  `107f664a6fb1f11caeb85682b648472e351f61b34b4db987ff7b32f3d0e1f146`.
- Phase 11C headline ledger SHA-256:
  `680f3cc1938856cfcc00b1f9a9c1aa3dc233c97d6bd794f8409d039817760419`.
- Phase 11C final evidence archive SHA-256:
  `e269f4394a25a812b4a2ac08e3c7b1dbc396e9356b5f522286372bae65abb9f2`.
- `auth-004` remains `5/5` headline failed and `0` completed; it is not rerun,
  replaced, backfilled, or revised.

Phase 11C does not prove provider tool-call reliability. Provider text-only output,
tool-call mismatch, schema mismatch, usage ambiguity, publisher ambiguity, and receipt
mismatch remain fail-closed terminal categories.

## Archived Gate A Artifacts

`phase11d_human_pilot/examples/gate_a/` contains:

- `authorization.json`
- `consent-receipts.json`
- `repository-allowlist.json`
- `cohort.json`
- `selection-receipt.json`
- `headline-manifest.json`
- `review-receipts.jsonl`
- `repair-receipts.jsonl`
- `draft-pr-receipts.jsonl`
- `feedback-receipts.jsonl`
- `time-cost-latency-receipts.jsonl`
- `incident-stop-receipts.jsonl`
- `business-report.json`
- `claim-decision-report.json`
- `final-acceptance-report.json`
- `canonical-manifest.json`

The generated canonical manifest SHA-256 is
`d7e450a010bcbee2270376bc0f9fe23456c0ca3caea2a7b572ed841d910dbdd1`.

`phase11d_human_pilot/templates/gate_b_authorization.template.json` is deliberately
incomplete. It keeps real provider calls, real GitHub repair branch pushes, and real
Draft Repair PR creation disabled until all required fields are filled, hash-bound, and
approved by the owner with exact approval text.

## Claim Boundary

All Gate A artifacts keep:

```text
real_model_calls=false
real_github_writes=false
real_pilot_executed=false
business_claim_allowed=false
model_quality_status=not_measured
formal_quality_status=incomplete
production_ready=false
```

Pilot completion is not business success, model quality success, formal quality success,
or production readiness. Any future business claim requires pre-registered thresholds
and independent owner sign-off for the exact Gate B denominator.

## Validation

Focused Gate A validation:

```powershell
python -B -m unittest -v tests.test_phase11d_human_pilot
python -B phase11d_human_pilot.py validate-bundle --bundle phase11d_human_pilot/examples/gate_a
python -B phase11d_human_pilot.py generate-gate-b-template --output phase11d_human_pilot/templates/gate_b_authorization.template.json
```

Repository validation required before delivery:

```powershell
python -m unittest discover -s tests
python -m ruff check .
python -m mypy src/code_review_agent phase11d_human_pilot.py
python scripts/verify.py
python -m pip check
git diff --check
git diff --name-only 4af4b2756e8d2de6764d08e17a6e12040e24975e...HEAD
```

If this environment cannot write bytecode to the pre-existing root `__pycache__`, use
`python -B` for equivalent no-bytecode offline validation and report that deviation.
