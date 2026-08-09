# Phase 11D Pilot v2 Candidate 18: PR URL Routing

## Scope

Cover explicit repository routing for GitHub PR URLs and malformed-host rejection.

## Owned Files

- `docs/plans/phase11d-pilot-v2-c18-pr-url-routing.md`
- `tests/test_phase11d_pilot_v2_c18_pr_url_routing.py`

All other paths, including `eval/**`, are read-only.

## Acceptance

- Numeric PRs retain repository placeholders.
- GitHub URLs pin the explicit owner and repository.
- Lookalike hosts and non-PR paths are rejected.
