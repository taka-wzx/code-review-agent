# Phase 8D Real Candidate Annotation Rubric v1

## Purpose and independence

Decide whether each Finder candidate describes a real defect in the selected
public pull request. Reviewer A and reviewer B must be two different real
people. Work independently: do not exchange labels, rationales, candidate
ordering, or aggregate impressions before both complete responses are frozen.
Do not use model scores, predictions, split membership, or another reviewer's
work. The packet order is intentionally different for each reviewer.

The packet is the immutable assignment. Public source material for the bound PR
and merge revision may be consulted when the packet evidence is insufficient,
but do not inspect any peer response, protected evaluation label, later model
result, or adjudication decision. Never copy credentials or private data into a
rationale.

## Labels

Choose exactly one label for every candidate:

- `keep`: The candidate identifies a concrete functional, correctness,
  security, reliability, compatibility, or material performance defect that is
  introduced or exposed by the bound change. The rationale must state the
  affected code path, triggering condition, and observable consequence, and
  cite the decisive packet or public-source evidence.
- `drop`: The claimed defect is not present, is contradicted by the bound code,
  is outside the selected change, is only style/naming/preference, lacks a
  concrete failure mechanism after inspection, or duplicates another claim in
  a way that should not become a separate training example. The rationale must
  state the concrete reason for rejection; absence of confidence alone is not
  enough.
- `uncertain`: Available authorized evidence cannot resolve a material factual
  question needed to choose `keep` or `drop`. State exactly what is missing or
  ambiguous and what evidence would resolve it. Do not use `uncertain` merely
  because the issue is low severity or because investigation takes effort.

Severity supplied by Finder is context only and must not determine the label.
Judge the candidate's substance, not its wording quality or proposed fix. A
correct issue with an imperfect suggestion can still be `keep`; explain the
distinction in the rationale.

## Evidence discipline

- Bind the decision to the packet's candidate ID, source ID, merge revision,
  candidate-source hash, and evidence hash.
- Treat the Phase 8D tool summary honestly: the Finder saw a frozen unified
  diff, not a complete repository checkout. Do not infer repository-wide
  absence from a missing tool result.
- When consulting public source, use the selected PR and bound merge revision;
  later repository state is not decisive evidence.
- Do not repair, rewrite, merge, or delete candidates. Record only the label and
  rationale. The import tool computes annotation IDs and hashes.

## Response format and completion

Return JSONL with exactly one object per packet item and no extra candidates:

```json
{"candidate_id":"...","label":"keep|drop|uncertain","rationale":"Concrete evidence-based reason.","created_at":"YYYY-MM-DDTHH:MM:SSZ"}
```

Use a real second-precision UTC timestamp. Rationales must be non-empty and at
most 4,000 UTF-8 bytes. Every one of the 137 packet items must be decided. The
import will fail on missing, duplicate, foreign, stale, malformed, or extra
rows. Disagreement between reviewers, or either reviewer choosing `uncertain`,
is routed to the distinct real adjudicator `human-adjudicator-c-v1`.
