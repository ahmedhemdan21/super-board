# super-board link — cross-board flow

One product, two repos, two boards: a backend board on `owner/api` and a frontend board on
`owner/web`, each with its own config and its own orchestrator session. A feature usually
needs a backend card done before the frontend card can start. `link` creates the other half
of such a pair and writes the refs both ways; the wave planner then holds the waiting card
until the blocker is done on the other board, and sweeps it from Blocked to Ready when it is.

## Setup, once per board

Each config lists the repos whose cards may block it:

```json
"linked_repos": ["owner/api"]          // in owner/web's config
"linked_repos": ["owner/web"]          // in owner/api's config, if anything there waits on web
```

A blocker in a repo that is NOT listed is never trusted: `super-board-deps.sh` marks the card
unreadable, the planner flags it, and it is not built. A typo'd owner is a flag, not a card
that waits forever or one that starts early.

## Usage

```
bash .claude/bin/super-board-card.sh --config <this board's config> link <issue> \
  --to <owner/name> --project <owner/number> --new blocks|waits \
  --title "<ticket title>" [--body-file <ticket.md>] [--why "<one line>"]
```

| Flag | Meaning |
|---|---|
| `<issue>` | the existing card on THIS board |
| `--to` | the linked repo; must be in this config's `linked_repos` |
| `--project` | that repo's board, `owner/number`. Its config is not read from here, so it is never guessed: copy `project.owner`/`project.number` from that repo's config. Missing → exit 64 |
| `--new blocks` | the new card blocks `<issue>` (create the backend half of a frontend card) |
| `--new waits` | the new card waits on `<issue>` (create the frontend half of a backend card) |
| `--body-file` | the new ticket, in the ticket format (writing-standard.md § 3). Without it the body is the title plus `## Blocked by` |
| `--why` | the reason written on the `## Blocked by` bullet |

What it writes:

1. The counterpart issue in `--to`, with `- **Linked:** <this repo>#<issue> — <role>` in Context
   (and, for `--new waits`, `- <this repo>#<issue> — <why>` under `## Blocked by`).
2. That issue on the `--project` board, Status **Backlog** — the other board's human or lint
   promotes it to Ready as usual.
3. On `<issue>`: `- **Linked:** <owner/name>#<M> — <role>` in Context, and for `--new blocks`
   `- <owner/name>#<M> — <why>` under `## Blocked by` (a lone `- None.` gives way).

Stdout is `{created, url, linkedTo, blocker, dependent}`. Rerunning after a partial failure does
not double a bullet; the error names what was already written and what is left. If `<issue>`
has a `blocked-by:` line in a Block comment, that line wins over the body — the command says so;
post a new Block comment whose `blocked-by:` line includes the ref.

For `--new waits`, the OTHER board's config must list this repo in its `linked_repos`;
`link` reminds you, it cannot check a config that is not on this machine.

## How the planner reads a foreign blocker

`super-board-wave-plan.sh` passes `linked_repos` to `super-board-deps.sh`, which reads every
foreign blocker's state in ONE batched GraphQL query through `super-board-github-read.py`:

| Foreign state | Verdict |
|---|---|
| Issue closed `COMPLETED`, or PR merged | done — the card is free once its other blockers are |
| Issue open, PR open, unreadable (not found, no access) | open — the card waits |
| Issue closed `NOT_PLANNED`, or PR closed unmerged | flagged — a person decides whether the card still needs it and edits `## Blocked by` |

`blockers`, `openBlockers` and the sweep's `clearedBy` carry foreign refs as `owner/repo#N`
strings; same-repo blockers stay numbers. A Blocked card whose foreign blockers are all done is
swept to Ready in the next wave like any other.

Linking lends state, not cards. If a linked repo's issue sits on this board's Project too, the
planner reports it in `foreign` and never plans it: a card is `owner/repo#N`, and only
`repo.remote`'s cards are this board's (hub.md → Card identity).
