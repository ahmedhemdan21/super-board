# Hub boards — one board, several repos

A hub board covers several member repos (for example a backend and a frontend that are
submodules of one parent). Reports are filed once as parent issues in a hub repo, triage splits
them into per-repo sub-issues, and one run builds both. Code, PRs, merge gates and deploys stay
in each member repo. Single-repo configs (no `members`) are unchanged.

## What GitHub does (checked 2026-10-19)

Checked on throwaway private repos and a user Project; queries below are the ones used.

| Question | Answer | What super-board does about it |
|---|---|---|
| Sub-issues across repos? | Yes. `addSubIssue(input:{issueId, subIssueId})` attaches an issue from another repo of the same owner; `parent`, `subIssues`, `subIssuesSummary` read back on both sides. | The split attaches each member issue to the hub parent. |
| Is `subIssuesSummary` current right after attaching? | No. It read `total: 1` for two children seconds after attaching; later `2`. | Count children from `subIssues.nodes`, never from the summary. |
| Does the parent close when every child closes? | No. Parent stays `OPEN` at 100 %. | super-board closes the parent itself when every child closed as completed. |
| One Project with items from several repos? | Yes. Three `#1` issues from three repos are three separate items. Built-in fields: **Repository**, **Parent issue**, **Sub-issues progress**. | Cards are `owner/repo#N`; views filter on Repository. |
| Moving a one-sided report to a member repo? | `gh issue transfer` keeps comments and the Project item (it now points at the new issue); the old URL still resolves. Labels are **dropped**, even when the target has the same label. | Re-apply labels after the transfer. |
| Cost of one board read? | 100 items with repo, labels, parent and sub-issues: 2 points (1 without the parent fields). | One hub read costs what two single-repo reads cost today. |

```graphql
mutation($p:ID!,$c:ID!){ addSubIssue(input:{issueId:$p, subIssueId:$c}){ subIssue{ number repository{ nameWithOwner } } } }

query($o:String!,$n:Int!){ rateLimit{ cost }
  user(login:$o){ projectV2(number:$n){ items(first:100){ nodes{ id
    fieldValueByName(name:"Status"){ ...on ProjectV2ItemFieldSingleSelectValue{ name } }
    content{ ...on Issue{ number title state labels(first:20){ nodes{ name } }
      repository{ nameWithOwner } parent{ number repository{ nameWithOwner } }
      subIssues(first:20){ nodes{ number state repository{ nameWithOwner } } } } } } } } } }
```

(An organization Project reads the same way under `organization(login:)`.)

## Card identity

A card is `owner/repo#N`; two repos' `#7` are two cards. Keys compare lower-cased and print as
GitHub spells them.

- **Home repo** = config `repo.remote`. A home card prints and is passed as plain `#N`, as
  before: worktrees `.claude/worktrees/issue-<N>-build|qa|review/`, branch `issue-<N>-<slug>`.
- `super-board-card.sh` takes `N`, `#N` or `owner/repo#N` (plain `N` = home). Its cache keys
  `.items` by card key; an older cache keyed by bare number is dropped and re-read.
- The planner plans home cards only. Another repo's card on the Project goes in `foreign`, even
  when that repo is in `linked_repos` (linking lends blocker state, not cards). Graph and triage
  verdicts are matched by card key, never by number alone.
- `super-board-status.py` and `super-board-card-ready.py` carry `repository` on every card and
  read each issue with `--repo <its repo>`.
- A submodule keeps its own `.claude/super-board/` (halt file, card cache) in its own checkout.

Later: member cards (P4) get worktree and branch names with a member prefix, so `issue-7` of
two members never share a folder.

## Config

P2: a hub config plans and resolves; running a members board arrives in P4. Until then
`/super-board run` refuses one (`super-board-config.py run-gate`, exit 78), as do the
legacy runner and the Codex wave.

```json
{ "project": { "owner": "acme", "title": "Acme", "number": 3 },
  "hub": { "remote": "https://github.com/acme/docs", "path": "." },
  "members": {
    "backend":  { "repo": { "path": "backend",  "remote": "https://github.com/acme/api" },
                  "base_branch": "dev", "verify_commands": ["dotnet test"],
                  "collect": { "sources": ["loki"] } },
    "frontend": { "repo": { "path": "frontend", "remote": "https://github.com/acme/web" },
                  "base_branch": "dev" } },
  "split": { "default_order": ["backend", "frontend"], "confirm": true },
  "collect": { "sources": ["slack", "github"] },
  "verify_commands": ["git diff --check"], "max_workers": 6 }
```

- **Members**: one or more (a board can be onboarded one repo at a time). Keys match
  `^[a-z][a-z0-9-]{0,30}$`. Each needs `repo.remote` (unique, not the hub's), `repo.path`
  and `base_branch`. Paths are relative to the board root (the folder holding
  `.claude/super-board/configs/`; members are usually its submodule folders) or absolute.
- **No top-level `repo`** in a members config: the hub repo is `hub`, code repos are members.
- **Per member** (member wins over a top-level default): `repo`, `base_branch`,
  `verify_commands`/`verify_retry`/`merge_gate`, `merge_policy`, `migrations`, `deploy`,
  `target`/`qa`, `env_check`, `runtime_verification`, `refine`, `writing`, `paths`, `guard`,
  `linked_repos`, `collect` (merged one level: a member's `collect.sources` replaces the board's).
- **Per board** (a member setting one is refused): `project`, `columns`, `labels`, `triage`,
  `max_workers`, `worker_backend`, `model_tier`, `no_progress_cycles`, `rebuild_cap`,
  `notifications`.

`scripts/super-board-config.py` is the one resolver:

| Command | Gives |
|---|---|
| `validate --config C` | exit 65 with every reason; `super-board-setup.py check` runs it per config |
| `member <key> --config C [--out F \| --path]` | a full single-repo config: `repo.path` absolute, `member`, `hub_config`, the other members added to `linked_repos`. `--path` caches it at `.claude/super-board/resolved/<slug>.<key>.json` (gitignored; rebuilt when the source is newer) and prints the path |
| `members --config C` | `[{key, repo, path}]`; a single-repo config is one member with key `""` |
| `for-repo <owner/name> --config C` | the member key; exit 3 when not a member |
| `run-gate --config C` | exit 78 for a members config |

Every single-repo script (merge gate, deploy wait, preflight, verify, card.sh) takes the
resolved config unchanged. `super-board-wave-plan.sh --config <hub config>` plans each member
as home over one board read: every card carries `member`, `foreign` lists only repos that
are no member (the hub's own parents too, until P3), `max_workers` caps the merged wave.
`super-board-github-read.py --check --config C` checks the hub's and every member's halt
file. Every `gh issue|pr|run` call names `--repo`.

## Split

`super-board-split.py` carries out triage's decision on a hub issue. It decides nothing itself;
triage (triage.md → Hub: split) decides the sides.

| Verb | What it does |
|---|---|
| `plan --config C --issue <hub#N>` | Read-only. Prints the issue (title, body, labels, column, sub-issues by member), the members and `split`. |
| `apply --config C --issue <hub#N> --decision D [--approved] [--dry-run]` | Carries out the decision below. |
| `close-parents --config C [--dry-run]` | Closes parents whose children are all done. |

Decision file (`body_file` relative to the decision file's folder):

```json
{ "sides": ["backend", "frontend"],
  "why": "needs a new endpoint and a button on the invoices page",
  "children": { "backend":  { "title": "✨ [feat] invoices: CSV endpoint", "body_file": "backend.md" },
                "frontend": { "title": "✨ [feat] invoices: export button", "body_file": "frontend.md" } } }
```

- **One side** (`"sides": ["backend"]`, no `children`): `gh issue transfer` to that member. The
  transfer drops labels, so the parent's labels are re-applied; a label the member repo lacks is
  reported, never created. The Project item survives a transfer; if it did not, the issue is
  added back in its old column. One `[triage] [report] ✅ split → backend` comment says why.
- **Several sides**: one issue per side in its member repo, in `split.default_order`. Each body
  must pass `super-board-card-ready.py`'s ticket check (writing-standard.md § 3), or apply exits 3
  before writing anything. Each later side's `## Blocked by` gets `- owner/repo#N — the <side>
  side lands first` naming the side before it (`- None.` gives way). Children get the parent's
  labels except `parent` and `needs-triage`, only those that exist in the member repo. Each child
  goes on the Project in the parent's column (Backlog when `split.confirm` is true) and is
  attached with `addSubIssue`. The parent gets the label `parent` and moves to Backlog; one
  comment lists the children.
- **Idempotent.** A child is found by repository in the parent's `subIssues.nodes`, else in the
  marker `<!-- sb-split: {…} -->` on the parent's split comment. The marker is written right
  after each child is created and the comment is edited in place, so a rerun after a failure
  continues and files nothing twice. A rerun on a finished split writes nothing
  (`already-split`). A one-sided split writes its marker before the transfer; the comment moves
  with the issue, so a rerun with either ref finishes the labels and the card.
- **Confirm.** With `split.confirm: true`, `apply` without `--approved` posts the proposal as one
  `[triage] [question] 🙋 approve split → …` comment and exits 0 with `awaiting-approval`. The
  proposal carries a hash of the decision; it is approved by a reply `done <first 8 hex>` from a
  human with write/maintain/admin on the hub AND every member repo the split writes to. A bare
  `done`, or a changed decision, needs a new approval. `--approved` is a human saying yes on the
  command line. The approval is recorded in the marker, so a resumed split does not ask again.
- **Trust.** Anyone can comment on a hub issue, so no comment steers the split. The `sb-split`,
  `sb-split-proposal` and `sb-parent-needs-human` markers count only when written by the run's
  own identity (`gh api user`); others are ignored and reported. A child named in a marker is
  used only when it sits in that side's member repo, carries the backlink
  `<!-- sb-split-child: <hub ref> <side> -->` and was opened by the same identity; otherwise
  apply exits 3 and writes nothing. Labels and column come from the live parent; only a
  one-sided resume after the transfer (which dropped the labels) reads them from its own marker.
- `--dry-run` reads, then prints every write it would make (`writes` in the JSON) and makes none.
- Writes check the halt file and `sb_gh_guard_check 200` first and are never retried: a failed
  write exits 69 and prints what was written. Rerun the same `apply`.

**Parents stay off the lanes.** A parent is labelled `parent` and sits in Backlog, which the
planner never plans. TODO at integration (P2/P4): `super-board-wave-plan.sh` must also skip a
`parent`-labelled card in Ready, and the status reader should show it as a parent.

**close-parents.** GitHub never closes a parent. For every open hub issue with sub-issues:
every child closed `COMPLETED` → comment listing them and close the parent as completed; any
child closed `NOT_PLANNED` → leave it open and comment once (`<!-- sb-parent-needs-human -->`)
that a human must decide. Run it once a wave, or after a merge.

## Intake (P5)

Reports are filed once. `/super-collect` runs from the hub checkout with the hub config
(super-collect SKILL.md → Hub boards):

- Top-level `collect.sources` (one Slack bug channel for every repo) file into the **hub** repo:
  Backlog, label `needs-split`. Triage's split (triage.md → Hub: split) then maps each to
  backend, frontend or both.
- A member's own `members.<key>.collect.sources` (loki for the backend, sentry for the
  frontend) file straight into that member, with that member's resolved settings.
- `super-collect-file.sh --target hub|<member key>` names the repo; unset, the source decides.
  A source listed in none or several places needs it (exit 64).
- Dedupe (fingerprint and `sb-signature`) reads the hub's and every member's issue history in
  one `super-board-github-read.py --issue-history hub,api,web` call, so a rerun never files
  twice: an open hit anywhere — the parent, or the parent after a one-sided transfer (the
  fingerprint stamp is in its body) — gets the comment. Receipts lock on the hub repo.
- The Slack thread reply links the hub issue (or the dedupe hit); a later transfer keeps the
  link working (GitHub redirects).
- Scheduled intake: a job whose `repo` is the hub checkout and `config` the hub config, steps
  `["collect"]`. "Board busy" and halt checks cover every member checkout; a `run` step on a hub
  config is refused until P4.
- Install: `install.sh <hub checkout>` installs the full pack into the hub's `.claude/`; member
  submodules keep their own `.claude/` untouched. Scripts resolve member paths through
  `super-board-config.py`, never cwd. Collector keys (`SLACK_BOT_TOKEN`, …) go in the hub's
  `.env.local`.

`super-board-config.py hub --config C [--out F]` gives the hub repo as a single-repo config
(`repo` = `hub.remote` at the hub path, every member in `linked_repos`). The member key `hub`
is reserved.

PrimeCore (`.claude/super-board/configs/primecore.json` in primecore-docs):

```json
{ "project": {"owner":"PrimeCoreDev","title":"PrimeCore","number":3},
  "hub": {"remote":"https://github.com/PrimeCoreDev/primecore-docs","path":"."},
  "members": {
    "backend": {"repo":{"path":"backend","remote":"https://github.com/PrimeCoreDev/primecore-platform"},"base_branch":"dev"},
    "frontend": {"repo":{"path":"frontend","remote":"https://github.com/PrimeCoreDev/primecore-frontend"},"base_branch":"dev"} },
  "split": {"default_order":["backend","frontend"],"confirm":true},
  "collect": {"sources":["slack"], "slack":{"channels":["#bugs-reporting"],"reaction":"bug"}} }
```
