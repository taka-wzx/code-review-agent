# Phase 9G offline artifacts

`authorization.template.json` and `templates/` are deliberately incomplete
operator forms. Filling a placeholder or computing a hash does not grant
authority; the completed artifact must pass `phase9g_pilot.py` and be signed
by the named human custodian.

`examples/synthetic/bundle.json` is a compact, hash-bound descriptor. The
validator expands it into a deterministic full bundle in memory: 20 synthetic
PRs, three synthetic participant identities, feedback/time/run receipts,
independent A/B annotation, a C conflict resolution, gold freeze, run manifest
and business report. It must always return:

```json
{
  "business_claim_allowed": false,
  "quality_claim_allowed": false,
  "synthetic": true,
  "valid": true
}
```

Run the offline fixture gate with:

```powershell
python phase9g_pilot.py validate-bundle --bundle phase9g/examples/synthetic
```

The normative field rules and operator sequence are documented in
`docs/plans/phase9g-real-pilot.md` and `docs/phase9g-real-pilot.md`.
