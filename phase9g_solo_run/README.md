# Phase 9G-Solo-Run public artifacts

This directory may contain only sanitized, hash-bound public receipts for the
single-participant exploratory run. It must never contain stable participant or
repository IDs, repository locators, PR numbers or titles, raw diffs, source text,
prompts, credentials, token values, host paths, human feedback, or raw traces.

`selection-receipt.json`, when present, proves only that the frozen local metadata
cohort was materialized and that five selected diffs were hashed. It permanently
denies Business Pilot and Formal Quality claims and does not authorize a paid model
call. The current receipt records eight candidates, five selected PRs, and two
selected-diff secret-scan blocks without identifying any PR.

When present, `auth-003-attestation.json` and `offline-validation-auth003.json`
bind the approved standard endpoint, tariff, positive temperature profile,
zero-retry runtime, conservative budgets, executor source, and offline gates.
Neither file contains a credential or opens the dynamic paid-call gate by itself.
The auth-003 provider launch was rejected by tenant data-egress policy before the
executor started, so it produced zero requests, Tokens, cost, or run evidence and
remains `not_run_policy_blocked`.

`public-source-auth004.json`, when present, binds an anonymous credential-free
public Git clone, exact public commit, MIT license, complete candidate denominator,
deterministic five-PR selection, selected-diff hashes, and a declaration that no
private workspace diff or GitHub API was used. It contains no PR number, title,
locator, source text, or diff. `auth-004-attestation.json` and
`offline-validation-auth004.json`, when present, separately bind the post-source
human authorization, inherited model ceilings, committed executor, and offline
gates. None of these artifacts alone authorizes a provider call.

`run-receipt.json`, when present, contains only aggregate headline, usage, cost,
failure-denominator, and pending-human-feedback evidence. Model output and
feedback packets remain outside Git.
