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

## Manual Claude Code handoff

When the user chooses to open Claude Code in VS Code and perform the Claude
phase manually, do not start Claude Code in the background. Complete and
validate the Codex-owned work first, then create a local task-branch commit so
Claude has a stable handoff base. This workflow authorizes the task branch and
local handoff commit, but it does not authorize pushing, merging into
`master`, or publishing changes.

Every manual Claude handoff must provide all of the following without making
the user ask again:

1. The Codex branch name and full handoff commit SHA.
2. A copy-pasteable PowerShell command that creates a new Claude worktree and
   `claude/<task>` branch from that exact commit.
3. The exact absolute worktree folder the user must open in a new VS Code
   window, plus commands to verify `git branch --show-current` and `git status`.
4. A complete, self-contained Claude Code task prompt containing the goal,
   handoff commit, writable paths, read-only or prohibited paths, frozen
   interfaces, acceptance criteria, validation commands, diff-review steps,
   commit requirement, and the rule not to merge or push `master`.
5. A return checklist asking for Claude's branch name, commit SHA, changed
   files, commands and results, review findings, and remaining risks.

Use a separate VS Code window opened on the Claude worktree; the Claude panel
uses the folder of its current VS Code window and does not need its own branch
selector. Do not recommend opening both agent worktrees as roots in one
multi-root workspace.

After the user returns Claude's handoff information, inspect the Claude commit
and its diff, integrate it into `integration/<task>`, and run the required
validation. Stop there by default. Merge into `master` or push only when the
user explicitly requests it.
