# Phase 11D Human Review-to-Repair Pilot v1

Status: **Gate A offline implementation only**. Gate B real Pilot execution remains
closed until a future owner approval exactly matches a frozen authorization bundle.

## Scope

This Gate A package adds a standard-library-only offline tool:

```powershell
python phase11d_human_pilot.py generate-gate-a --output phase11d_human_pilot/examples/gate_a
python phase11d_human_pilot.py validate-bundle --bundle phase11d_human_pilot/examples/gate_a
python phase11d_human_pilot.py generate-gate-b-template --output phase11d_human_pilot/templates/gate_b_authorization.template.json
```

The tool generates and validates synthetic receipts, reports, and a canonical manifest.
It does not call providers, read credentials, open network transports, enroll real
participants, push Pilot-generated branches, create Pilot-generated Draft Repair PRs,
comment, label, check, review, merge, or mutate protected/default branches.

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
