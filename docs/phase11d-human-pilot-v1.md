# Phase 11D Human Review-to-Repair Pilot v1

Status: **Gate A complete; the default-closed Gate B Review-to-Repair operator,
Repair control flow, Draft PR publisher, and closeout tooling are implemented. Real
Pilot execution remains closed** until a newly frozen runtime, denominator, and exact
owner approval authorize the full write-enabled flow.

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
python phase11d_gate_b_executor.py run-review-repair-session --help
python phase11d_gate_b_executor.py build-timeout-recovery-checkpoint --help
python phase11d_gate_b_executor.py resume-write-approved-repair-session --help
python phase11d_gate_b_executor.py prepare-pilot-closeout --help
python phase11d_gate_b_executor.py approve-pilot-closeout --help
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

`resume-selected-pull-requests` is the only supported recovery path after a Review
cohort stops on `http_transport_failure` before the affected PR reaches the Provider.
It requires the prior authorization, selection receipt, and hash-only Review receipt,
plus a newly frozen active runtime and selection receipt for the identical PR rows.
The resulting v1alpha2 receipt binds the immediate prior receipt SHA-256, carries all
completed rows and cumulative budget usage forward, and starts at the first zero-call
failure. It rejects Provider failures, changed PR snapshots or selection order,
nonzero failed-row usage, and completed cohorts. Review-only authorization may keep
both Repair write switches false; the Repair coordinator and Publisher still reject
that permission state.

The one-job `GateBRepairCoordinator` requires the exact sequence: a confirmed
maintainer/org-admin selects a completed-review Finding; an exact in-memory Repair Plan
is hash-bound; a single-use WRITE approval is consumed; an isolated offline/non-root
sandbox returns hash-bound patch, test, checkpoint, budget, reflection, tree, and
commit evidence; a second single-use DRAFT_PR approval binds that exact commit; then
the publisher may act. The coordinator does not invent selections, plans, approvals,
or sandbox evidence.

`run-review-repair-session` runs one newly authorized immutable Review cohort and
passes each structured Provider response to two views in the same process: the
existing hash-only `ReviewOutcome` and an ephemeral human-readable Finding view. It
then starts an authenticated HTTP operator on `127.0.0.1` only. The bearer token is
printed once to the controlling terminal. The server rejects non-loopback and
unauthenticated requests, suppresses request logging, sets `Cache-Control: no-store`,
and terminates after publish, decline, human stop, or timeout.

The operator sequence is fixed:

1. `GET /v1/status` displays in-memory Findings only while selection is pending.
2. `POST /v1/select-and-plan` binds one human-selected Finding and exact Repair Plan,
   clears all unselected Findings, and returns the WRITE binding SHA-256.
3. `POST /v1/write-approval` consumes one maintainer/org-admin approval.
4. `POST /v1/sandbox` accepts only isolated sandbox evidence bound to the exact patch,
   tests, reflection, checkpoint, budget, tree, and commit, then returns the DRAFT_PR
   binding SHA-256. Patch bytes remain in process memory.
5. `POST /v1/draft-pr-approval` consumes the second approval for that exact commit.
6. `POST /v1/publish` may create one dedicated branch and one Draft PR. It writes only
   sanitized Review, Repair, Draft PR, operator-session, and publication-journal
   receipts, clears remaining raw material, and stops the server.

The operator does not generate or attest sandbox evidence. The PATCH/TEST/REFLECT
runner must remain in its separately authorized Docker/worktree boundary with network
disabled and a non-root identity; submitted evidence is rejected unless all existing
`SandboxResult` bindings and pass gates match.

If the operator expires after Finding selection and WRITE approval but before sandbox
evidence is accepted, `build-timeout-recovery-checkpoint` creates the only supported
WRITE-stage recovery checkpoint. It accepts only a completed 20--30 PR Review receipt
with `stop_category=none` and an `expired/timeout` operator receipt. The self-hashed
checkpoint binds the source authorization/runtime, selection and Review receipts,
selected Finding and Review response, prior selection/Plan/WRITE lineage, recovery
actor, and new recovery selection, Repair job, and WRITE approval identifiers. It is
offline and has no Provider or GitHub transport.

Recovery requires a newly frozen runtime and an independently exact-approved Gate B
authorization whose `deterministic_selection_seed_sha256` equals the checkpoint
SHA-256. `resume-write-approved-repair-session` reorders and rebinds the same immutable
20--30 PR rows under that authorization, carries the completed Review rows and budget
usage into a resumed hash-only receipt, and starts directly at
`awaiting_write_approval`. It has no Provider client and cannot replay any Review call.
The recovery actor must be a confirmed maintainer/org-admin and a maintainer/org-admin
must consume the new single-use WRITE binding before sandbox evidence is accepted.

The recovered flow keeps the normal publication boundary unchanged: passing isolated
sandbox evidence must bind the exact commit and produce a new DRAFT_PR binding; a
separate exact DRAFT_PR approval must then be consumed before the publisher factory is
created or any GitHub credential is read. Until that approval, no Repair branch may be
pushed and no Draft Repair PR may be created. The publisher can still create only one
Draft PR and cannot mark it Ready or merge it.

`GitHubDraftPublisher` is limited to repository verification, ref read-back, Git data
objects, one dedicated `crag/phase11d/` branch, and one Draft PR. It has no PATCH, PUT,
Ready, merge, comment, check, label, or review route. It writes a sanitized journal
outside the source tree, verifies the exact tree/commit/ref and Draft state, and
reconciles an ambiguous post-write restart by read-back; unresolved ambiguity is
quarantined. The implementation tests use fakes only and have made no real Provider
call, GitHub write, Pilot branch, or Pilot Draft PR.

The exact Repair commit is parented by the frozen selected PR head, not by the PR's
historical base SHA. Before any Git object write, the publisher reads that source-head
commit and requires its tree to equal the sandbox `base_tree_sha`. The target base
branch may remain unchanged or advance while implementation and authorization work is
merged. An advanced base is accepted only when GitHub Compare reports `ahead`, zero
behind commits, and both the base commit and merge base equal the frozen base SHA.
Diverged, behind, missing, malformed, or ambiguous base/source-head evidence is
quarantined before branch creation. Publication journals carrying the source-head
binding use v1alpha2; v1alpha1 journals remain hash-valid but require a newly approved
publication session rather than silent upgrade.

After the Draft PR is reviewed by a participant, `prepare-pilot-closeout` hashes the
human rationale and writes feedback, time/cost, business, and claim-decision reports,
plus an exact final sign-off text. `approve-pilot-closeout` accepts only a human
`org_admin` approval of that exact text and then writes `final-acceptance-report.json`
and `canonical-manifest.json`. Completion means only that this Phase 11D Pilot's
evidence chain is complete. The reports permanently keep `business_claim_allowed=false`,
`quality_claim_allowed=false`, `production_ready=false`,
`model_quality_status=not_measured`, `formal_quality_status=incomplete`, and
`final_project_complete=false`.

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
