# Issue #25: OIDC token validation and JWK rotation

## Goal

Add a production-configurable OIDC bearer-token authentication backend that
validates JWT signatures, issuer, audience, expiry, and temporal claims using
a bounded JWKS cache. Unknown key IDs must cause one synchronous JWKS refresh
so normal signing-key rotation succeeds without restarting the service.

## Base

- Base branch: `master`
- Base commit: `345b3035eca2f7af65f650fdeaa7a1e5e7297194`
- Task branch: `codex/issue-25-oidc-jwk`

## Frozen interfaces

- Existing `AuthBackend`, `VerifiedOIDCJWTAuthBackend`, database bearer-token
  authentication, local-token mode, REST paths, and error response shape stay
  compatible.
- OIDC configuration is explicit and default-off. It accepts only a configured
  HTTPS issuer and JWKS endpoint, a non-empty audience, and approved asymmetric
  JWT algorithms.
- JWTs, Authorization headers, JWK HTTP bodies, and exception text must never
  be persisted, emitted in HTTP responses, or included in telemetry. Failure
  telemetry uses bounded reason codes only.
- OIDC claims map only to an existing active user membership matching both its
  configured organization claim and subject. A claim alone never creates a
  user or grants a role.

## File ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue25-oidc-jwk.md`, `src/code_review_agent/identity.py`, `src/code_review_agent/database.py`, `src/code_review_agent/service.py`, `tests/test_issue25_oidc_jwk.py` | all other paths |

## Prohibited changes

- No changes to `eval/**`, `eval/holdout/**`, prompts, sentinels, CI, packaging,
  dependencies, migrations, or public REST paths.
- No real OIDC issuer, JWKS, GitHub, provider, or database service calls in
  tests. Network transport is dependency-injected and tested with fakes.
- No direct merge or commit to `master`; no credential or local-auth file may
  enter the worktree, commit, or test fixture.

## Implementation

- Add a thread-safe, TTL-bounded JWKS cache and a configured OIDC JWT verifier
  using the already locked PyJWT runtime dependency.
- Require standard OIDC `iss`, `sub`, `aud`, `exp`, and `iat` claims; validate
  `nbf` when present; reject unsigned, symmetric, malformed, expired,
  future-not-valid, issuer-mismatched, audience-mismatched, and unmapped tokens.
- Add a database lookup for an active principal by organization and external
  subject, then wire the OIDC backend into the default service factory only
  when explicit OIDC mode is configured.
- Add deterministic tests for cache reuse, forced refresh on key rotation,
  cache expiry, invalid claims/signatures, unknown users, configuration errors,
  and existing authentication compatibility.

## Validation

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = 'E:\shiyan\code_review_agent\traces\worktrees\release-v0.1\.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $repoRoot 'src'
& $python -m unittest -v tests.test_issue25_oidc_jwk tests.test_phase9b_identity_rbac
& $python -m ruff check src/code_review_agent/identity.py src/code_review_agent/database.py src/code_review_agent/service.py tests/test_issue25_oidc_jwk.py
& $python -m mypy src/code_review_agent
git diff --check
git diff --name-only origin/master...HEAD
git status --short --branch
```

## Acceptance criteria

- Valid tokens signed by a cached JWK map to the intended existing principal.
- A new `kid` triggers one JWKS refresh and accepts a valid rotated signing key.
- Expired cache entries refresh before use; fetch or verification failures deny
  the request without leaking token or upstream details.
- Wrong issuer/audience, expired/not-yet-valid tokens, unsupported algorithms,
  missing required claims, and unmapped subjects are rejected.
- Existing local and database authentication behavior remains covered by
  regression tests, with no external calls in the suite.
