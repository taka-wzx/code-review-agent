# Codex project instructions

Before modifying code, read:

- `docs/agent-contract.md`
- the active task contract under `docs/plans/`
- `README.md` and `pyproject.toml` for project-specific commands

Follow the Single Writer rule: only edit files assigned to Codex in the active
task contract. Other agents' files are read-only until their commit is handed
off. Work on an isolated branch or worktree, never merge directly into
`master`, and do not widen the task without user approval.

Before delivery, inspect `git diff`, run the validation commands required by
the active task contract, and report changed files, command results, and
remaining risks.
