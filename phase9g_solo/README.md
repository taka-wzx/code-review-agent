# Phase 9G-Solo offline artifacts

These forms belong only to `single_participant_exploratory` evidence. They do
not weaken or satisfy the 3--5-person Business Pilot or independent A/B/C
Formal Quality requirements.

`authorization.template.json` and `templates/` are deliberately incomplete.
Replacing placeholders and computing hashes does not grant authority. A real
Solo exercise needs a separately signed, unexpired authorization from the
named human approver. The tool itself is never a model or GitHub executor.

The synthetic descriptor expands to five selected fake PRs, one fake identity,
partial feedback, complete time/headline receipts, a retained headline failure,
and a successful diagnostic rerun. It must always return:

```json
{
  "business_claim_allowed": false,
  "evidence_type": "single_participant_exploratory",
  "exploratory_summary_allowed": false,
  "formal_quality_status": "incomplete",
  "quality_claim_allowed": false,
  "synthetic": true,
  "valid": true
}
```

Run the no-network protocol gate with:

```powershell
python phase9g_solo.py validate-bundle --bundle phase9g_solo/examples/synthetic
```

`hash-artifact` seals completed JSON/JSONL forms, and `materialize-cohort`
requires the sealed authorization plus the externally trusted Solo-contract
merge commit. A valid hash is integrity evidence, never permission.

The normative scope and operator sequence are in
`docs/plans/phase9g-solo-exploratory-v1.md` and
`docs/phase9g-solo-exploratory-v1.md`.
