# Issue #31: Cross-host encrypted artifact-store adapter

## Goal

Provide an isolated encrypted artifact-store adapter that can use a portable
byte-oriented backend.  Each stored object must use a fresh data-encryption
key, authenticated encryption, canonical integrity metadata, and a local
filesystem implementation suitable for offline tests.

## Base

- Base branch: `master`
- Base commit: `6ced1c4ebde377bfc386c4173efb700acdb27416`
- Task branch: `codex/issue-31-encrypted-artifact-store`
- GitHub Issue: https://github.com/taka-wzx/code-review-agent/issues/31

## Frozen Interfaces

- Existing `JobStore`, service routes, database schema, artifact cleanup, and
  public CLI behavior remain unchanged.
- No real object storage, KMS, cloud, network, credential file, or deployment
  integration is introduced.  Tests use only temporary local directories and
  in-memory fakes.
- No dependency, lockfile, migration, workflow, evaluator, prompt, or
  `eval/**` path is changed or read.
- The adapter accepts caller-supplied bytes only.  It must not log, serialize,
  or expose a raw master key, per-object data key, plaintext, filesystem path,
  or encryption exception detail.
- The project already locks `cryptography==49.0.0`; this task uses its
  AES-256-GCM primitive without changing dependency declarations.

## File Ownership

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `docs/plans/issue31-encrypted-artifact-store.md`, `src/code_review_agent/artifact_store.py`, `tests/test_issue31_encrypted_artifact_store.py` | all other paths |

## Implementation

- Add a byte-oriented backend protocol so future cross-host stores can provide
  object reads and create-if-absent writes without exposing filesystem paths.
- Add a portable local backend with strict object identifiers, bounded envelope
  sizes, atomic create-if-absent writes, and no overwrite fallback.
- Add an AES-256-GCM key wrapper with a caller-provided 32-byte local key and
  a stable non-secret key identifier.
- Encrypt every object with a newly generated 32-byte data key.  Wrap that key
  separately, bind both operations to canonical metadata, and persist only a
  strict JSON envelope containing ciphertext, nonces, wrapped key material,
  content SHA-256, size, object identifier, and key identifier.
- On read, validate the envelope shape and expected object identifier, unwrap
  the data key, authenticate/decrypt, then verify plaintext size and SHA-256.
  Any malformed, tampered, wrong-key, unavailable, or integrity-failed object
  must fail closed through bounded adapter exceptions.
- Add tests for encrypted round trips, distinct per-object ciphertext/data-key
  envelopes, tamper detection, wrong-key rejection, content-hash validation,
  object-key validation, duplicate-write rejection, and a non-filesystem fake
  backend proving the adapter portability boundary.

## Validation

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = 'E:\shiyan\code_review_agent\traces\worktrees\release-v0.1\.venv\Scripts\python.exe'
$env:PYTHONPATH = Join-Path $repoRoot 'src'
& $python -m unittest -v tests.test_issue31_encrypted_artifact_store
& $python -m ruff check src/code_review_agent/artifact_store.py tests/test_issue31_encrypted_artifact_store.py
& $python -m mypy src/code_review_agent/artifact_store.py
& $python scripts/verify.py
& $python -m pip check
git diff --check
```

## Acceptance Criteria

- Encrypted data round-trips through the local backend, while stored bytes do
  not reveal the original plaintext or any raw encryption key.
- Each object uses independent ciphertext and wrapped data-key material.
- Ciphertext, metadata, wrapped-key, and content-hash tampering are rejected.
- A different key identifier or different key material cannot decrypt an
  existing object.
- A test-only in-memory backend can satisfy the same byte-store protocol,
  demonstrating that the adapter has no local-host path dependency.
- Existing offline validation remains passing; no real external service is
  contacted.
