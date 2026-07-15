# Multi-agent task contract

Copy this file to `docs/plans/<task-name>.md`, fill every required field, and
commit the contract before parallel implementation starts.

## Goal

<!-- One observable outcome. -->

## Base

- Base branch: `master`
- Base commit: `<full commit SHA>`
- Integration branch: `integration/<task-name>`

## Frozen interfaces

<!-- Public API, data shape, error behavior, and compatibility constraints. -->

## File ownership

Each path has exactly one writer during a parallel phase.

| Owner | Writable paths | Read-only dependencies |
| --- | --- | --- |
| Codex | `<paths>` | `<paths>` |
| Claude Code | `<paths>` | `<paths>` |
| Integrator | conflict resolution only | all paths |

If both agents must edit the same file, split the work into serial phases and
record the handoff commit here. Do not rely on textual merge success to prove
the combined behavior is correct.

## Prohibited changes

- No direct commits or merges to `master`.
- No unapproved dependency changes.
- No silent public API or persisted-data changes.
- No unrelated formatting or generated-file edits.
- No deleting or weakening tests to make validation pass.
- No `.env`, credentials, tokens, or local auth files in commits or handoffs.

## Agent assignments

### Codex

- Objective: `<bounded task>`
- Required tests: `<commands>`
- Delivery commit: `<filled after completion>`

### Claude Code

- Objective: `<bounded task>`
- Required tests: `<commands>`
- Delivery commit: `<filled after completion>`

## Validation

Run the smallest affected test set while developing, then run before
integration:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
.venv\Scripts\python.exe -m ruff check .
```

For evaluation-fixture changes, also run:

```powershell
.venv\Scripts\python.exe eval\check_consistency.py eval eval\holdout
```

Record any unavailable command and the reason; do not report an unrun check as
passing. Tests that call external LLM providers require explicit authorization
and must not expose `.env` values.

## Acceptance criteria

- `<observable behavior>`
- Existing supported behavior remains compatible.
- Assigned tests and repository checks pass.
- Final integration diff contains no unrelated changes.

## Handoff and integration

1. Each agent reviews and commits only its owned files.
2. The reviewer reads `git diff <base>...<delivery-commit>` without editing it.
3. The integrator merges the implementation commit first and runs focused tests.
4. The integrator merges the second commit, resolves semantic conflicts, and
   runs all required validation.
5. Only the user or designated integrator merges the validated integration
   branch into `master`.

## Delivery report

- Summary:
- Changed files:
- Commit:
- Commands run and results:
- Known risks or assumptions:
- Suggested review focus:
