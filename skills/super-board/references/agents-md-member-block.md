## Super Board (hub member)

This repo is member `{{member}}` of the hub board `{{slug}}`. The board, its skills and its scripts live in the hub repo `{{hub_repo}}` (checkout `{{hub_path}}` from here). This repo keeps only the guard hooks.

| When | Do | NEVER |
|---|---|---|
| Drain the board | from the hub checkout: `/super-board run {{slug}}` | NEVER start a board from this repo |
| Board state / halt | from the hub checkout: `/super-board status {{slug}}` · `/super-board stop {{slug}}` | |
| Report a bug or feature | `/super-collect` in the hub checkout, or an issue here | DON'T file the same report in both |
| Set up / repair | `install.sh --hub <hub>` · `install.sh --member <this repo>` | DON'T copy the pack into this repo |

| Rule | Value |
|---|---|
| Worktrees | `.claude/worktrees/{{member}}-issue-<N>-<lane>/` · branch `{{member}}-issue-<N>-<slug>` |
| Scripts | the hub's `.claude/bin/` (absolute path); this repo has no `.claude/bin` |
| Merge | the hub's `super-board-merge-gate.sh` only · NEVER `gh pr merge` direct |
| Writing | commits, PRs, tickets, comments: the hub's `.claude/skills/super-board/references/writing-standard.md` |
| Secrets | NEVER read `.env` |
