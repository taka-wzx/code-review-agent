# Issue 37: Image signing and provenance verification

Status: implementation complete; pending Draft PR review

## Goal

Add a tag-release supply-chain workflow that pushes a service image, keylessly
signs its immutable digest with Cosign, and publishes GitHub provenance. Add a
local deploy-time verifier policy that rejects mutable, unsigned, tampered, or
provenance-mismatched images through injected command fakes.

## Base

- Base branch: `master`
- Base commit: `6ced1c4ebde377bfc386c4173efb700acdb27416`
- Task branch: `codex/issue-37-image-provenance`
- Integration branch: `integration/issue37-image-provenance`

## Frozen interfaces

- Existing `.github/workflows/ci.yml`, Dockerfiles, public Python APIs,
  dependencies, packaging, deployment manifests, and existing scripts remain
  unchanged.
- The new workflow uses only full-SHA-pinned official actions and is limited to
  `v*` tag pushes and manual dispatch. It performs signing only when GitHub
  executes that explicitly configured release workflow.
- The verifier accepts only immutable `@sha256:` image references, the GitHub
  OIDC issuer, the repository's supply-chain workflow identity, and SLSA v1
  provenance bound to the same digest. Tests inject command results and do not
  execute Cosign or contact a registry.

## File ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue37-image-provenance.md`; `.github/workflows/supply-chain.yml`; `supply_chain/image-verification-policy.json`; `scripts/verify_image_provenance.py`; `tests/test_issue37_image_provenance.py` | `.github/workflows/ci.yml`; `Dockerfile.service`; `Dockerfile`; `tests/test_ci_workflow_contract.py`; all existing source, deployment, and test files |

No other agent has write ownership for this task.

## Prohibited changes

- No direct commit, merge, rebase, or push to `master`.
- No real registry push, image build, signing, provenance upload, Cosign call,
  token access, release creation, or cloud deployment from the local task.
- No changes to existing workflows, Dockerfiles, dependencies, CI contexts,
  runtime configuration, public APIs, or data schema.
- No access to, execution of, or changes under `eval/` or `eval/holdout/`.
- No credentials, keys, identity tokens, certificate material, mutable image
  tags, or host paths in committed artifacts or test output.

## Codex assignment

- Objective: add only the Issue #37 release workflow, verifier policy, offline
  verifier, and focused tests.
- Required tests:

  ```powershell
  .venv\Scripts\python.exe -m unittest -v tests.test_issue37_image_provenance
  .venv\Scripts\python.exe scripts\verify_image_provenance.py --help
  .venv\Scripts\python.exe -m unittest discover -s tests
  .venv\Scripts\python.exe -m ruff check .
  .venv\Scripts\python.exe -m mypy src/code_review_agent
  .venv\Scripts\python.exe scripts\verify.py
  git diff --check
  ```

## Acceptance criteria

- The tag-release workflow builds and pushes `Dockerfile.service`, signs the
  returned immutable digest keylessly, and uploads a registry provenance
  attestation with least-privilege GitHub permissions.
- Every workflow action uses an official full commit SHA and the workflow is
  never a pull-request or branch-push credential path.
- The deploy-time verifier rejects mutable references, signature digest
  mismatch, bad issuer or workflow identity, missing/tampered provenance, and
  provenance subject mismatch without emitting raw command output.
- Offline tests exercise valid and invalid signature/provenance fixtures plus
  workflow structure and policy binding.
- The complete offline repository validation passes; no real signing or image
  publish is claimed from this local task.

## Delivery report

- Summary: implemented the tag-only image build, keyless signing, provenance
  attestation, and offline deploy-time verification policy.
- Changed files: the five Codex-owned files listed in the File ownership table.
- Commit: recorded in the task-branch history.
- Commands run and results: focused Issue 37 tests, verifier CLI help, ruff,
  mypy, and `git diff --cached --check` passed. The repository-wide test suite
  and `scripts/verify.py` both ran 1106 tests but were blocked by 9 failures
  and 8 errors in existing Phase 9B/9D service tests; no source or test file
  involved in those failures is changed by this task.
- Known risks or assumptions: full GitHub OIDC signing and registry attestation
  are configured for a future trusted tag workflow run. The repository-wide
  green gate remains blocked until the unrelated Phase 9B/9D failures are
  resolved and the suite is rerun in CI.
