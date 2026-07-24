# Phase 9E: Hierarchical Context and Trusted Repository Memory

Status: **active and frozen**

Frozen date: 2026-07-24

Baseline: `origin/master` at `f2ffaefa399d174980c3f9fe62a17c8b46ab2df1`

Task branch: `codex/phase9e-context-memory`

## Goal

Add three explicit context layers for code review without introducing chat
history, user profiles, general-domain knowledge, embeddings, or a vector
database:

1. `RunContext` is short-lived state for one review attempt. It contains the
   diff/source revision, current plan, tool summaries, Finder/Verifier state,
   and token budget. It is never promoted to durable memory automatically.
2. `RepositoryMemory` stores versioned repository conventions, commands,
   language/framework metadata, code owners, risk paths, and human-confirmed
   Finding feedback fingerprints. Every entry has organization/repository
   lineage, source SHA, creator, confirmation identity, reason, and validity.
3. `OrganizationPolicy` stores severity rules, forbidden operations, allowed
   tools, approval thresholds, retention, and cost budget. It is scoped to one
   organization and is never used across tenants.

## Write and trust rules

Model output is never a trusted write source. Durable memory accepts only an
explicit human confirmation, administrator configuration, or a verifiable
repository file. A rejected Finding is stored only as a repository-scoped
suppression candidate; it cannot become a global rule. Every durable write
requires a reason, source/provenance, and validity/expiry information. The
organization and repository predicates are mandatory for every read, write,
delete, and purge operation.

## Retrieval contract

Retrieval is deterministic and bounded:

```text
organization/repository/base SHA
  -> path/language/symbol filters
  -> PostgreSQL FTS (SQLite LIKE compatibility) + path/symbol graph
  -> deterministic score/rerank
  -> token/character cap
  -> provenance for every returned entry
```

The old conventions, changed files, imports, and callers remain in the static
context pack. Hierarchical mode prepends the bounded repository/policy results
and retains that pack. A source SHA mismatch is excluded by default; old
memory never silently contaminates a new revision.

## Ablation interface

The review API exposes three deterministic modes: `off`, `current_static`, and
`hierarchical`. `current_static` is the compatibility default. `off` omits
both proactive context layers while preserving the existing tool loop.

## Retention and deletion

Expired or invalidated entries are not retrievable. Retention purge removes
expired repository memory and policy versions. Repository removal cascades or
hard-deletes all memory, graph, and policy lineage so later retrieval returns
no records. Explicit policy invalidation is recorded and immediately excludes
the old version.

## Owned paths

Codex owns only these paths for this task:

- `docs/plans/phase9e-context-memory.md`;
- `migrations/versions/0005_phase9e_context_memory.py`;
- `src/code_review_agent/context_memory.py` (new);
- `src/code_review_agent/context.py`;
- `src/code_review_agent/agent.py`;
- `src/code_review_agent/agentloop.py`;
- `src/code_review_agent/database.py`;
- `src/code_review_agent/identity.py`;
- `src/code_review_agent/service_core.py`;
- `src/code_review_agent/service.py`;
- `src/code_review_agent/service_queue.py`;
- `src/code_review_agent/worker.py`;
- `tests/test_phase9e_context_memory.py` (new).

All other paths are read-only. In particular, `eval/**` and
`eval/holdout/**` are prohibited, and the pre-existing untracked
`%SystemDrive%/` path is not owned and must not be deleted, staged, committed,
or included in the diff.

## Validation

All checks are offline and use SQLite/fakes. No real model, GitHub, Postgres
service, or evaluation asset is accessed:

```powershell
$repoRoot = git rev-parse --show-toplevel
$python = Join-Path $repoRoot ".venv\\Scripts\\python.exe"
$env:PYTHONPATH = Join-Path $repoRoot "src"

& $python -m unittest -v tests.test_phase9e_context_memory
& $python -m ruff check .
& $python -m mypy src/code_review_agent
& $python scripts\\verify.py
& $python -m pip check
git diff --check
git diff --name-only origin/master...HEAD
git status --short --branch
```

Required tests cover deterministic retrieval and reranking, token caps,
tenant isolation, SHA invalidation, model-write rejection, repository removal,
retention/policy invalidation, accepted/rejected feedback memory hit and
expiry, and all three ablations without a model call.

## Delivery control

The user authorizes a local task-branch commit, push of only
`codex/phase9e-context-memory`, Draft PR creation, CI observation, Ready state,
and merge through that PR after all checks pass. Direct push/merge/rebase of
`master` is prohibited. After merge, verify the merge SHA and master CI.

## Change control

This contract is frozen after creation. Any new dependency, public interface,
external call, durable table outside the owned migration, or writable path
requires explicit user approval and a contract revision before implementation.
