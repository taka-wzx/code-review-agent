# Resume README v1

## Goal

Make the repository homepage useful to an interviewer: explain the product in one
screen, lead with verified engineering evidence, provide a compact system view, and
state the non-production boundary without hiding it.

## Owned Files

- `README.md`
- `docs/plans/resume-readme-v1.md`

All other files are read-only. Do not read, run, or modify `eval/**` or
`eval/holdout/**`.

## Content Boundaries

- Preserve the existing detailed technical documentation after the new homepage
  overview.
- Use only repository evidence already verified by Phase 11D receipts and GitHub CI.
- Keep `production_ready=false`, `business_claim_allowed=false`, and
  `quality_claim_allowed=false` explicit.
- Do not expose local paths, participant identities, credentials, raw model content,
  or private authorization artifacts.

## Acceptance Criteria

1. The first screen explains the product, core architecture, and strongest engineering
   evidence without requiring readers to understand phase history.
2. Phase 11D results accurately report 20/20 Reviews, 61 Findings, one Repair, one
   human-approved Draft PR, 118 seconds of active review, and CNY 1.226588 Review cost.
3. The README links to the public Draft Repair PR and CI workflow while preserving the
   Draft/no-merge boundary.
4. Markdown headings, table layout, links, and Mermaid syntax remain valid.

## Validation

- Read the complete README diff.
- Run `git diff --check`.
- Verify all repository-relative links added by this task resolve locally.
- Do not run code, evaluation, holdout, or Provider commands for this docs-only task.
