#!/usr/bin/env bash
# super-board-card.sh — read and move project cards for a few GraphQL points.
#
# Why this exists: on 2026-10-04 a 3-card wave spent ~2,850 of the 5,000/hr
# GraphQL points (~950 a card) and the board stalled an hour after every wave.
# Almost all of it was board lookups, not work. Measured with
# `rateLimit(dryRun:true){cost}` against gh 2.101:
#   gh project item-list          101 points per 100-item page (fieldValues(100)
#                                 x labels/users/reviewers/pullRequests(10) each),
#                                 so a 131-card board costs ~203 per call
#   gh project field-list         101 — the same query with items(first:100),
#                                 fetched only to be thrown away
#   gh project view               2   (owner lookup + project)
#   gh issue/pr view, pr list     1 each
# Lanes moved a card as item-list + field-list + item-edit: ~305 points a move.
# This script moves one for 1 point (cached ids) or 2 (first sight of an issue),
# and lists the whole board for ~1 point per 100 cards.
#
# Usage:
#   super-board-card.sh [--config <cfg.json>] [--owner O --number N] [--repo owner/name] <verb>
#     move <issue> <Status>   set the card's Status column (Ready, QA, Review, Blocked, Done, …)
#     status <issue>          print the card's current Status name (1 point, never cached)
#     item-id <issue>         print the card's project item id (cached)
#     ids                     print {projectId, fieldId, options:{name:id}} (cached)
#     items                   the whole board in `gh project item-list --format json` shape
#                             ({items:[{id,status,labels,title,content:{type,number,title,url,repository,body}}],totalCount})
#                             body: the issue body, for super-board-card-ready.py (no extra points)
#     link <issue> --to <owner/name> --project <owner/number> --new blocks|waits
#          --title <title> [--body-file <md>] [--why <text>]
#                             create the counterpart of <issue> on a linked repo's board
#                             (cross-board flow, SKILL.md → /super-board link)
#
# LINK. A feature split over two repos needs a card on each board, and one waits
# on the other. `link` creates the counterpart issue in <owner/name>, adds it to
# that repo's board in Backlog, and writes the refs both ways:
#   --new blocks   the NEW issue blocks <issue>: <issue>'s `## Blocked by` gets
#                  `- owner/name#M`
#   --new waits    the NEW issue waits on <issue>: its `## Blocked by` gets
#                  `- this/repo#N`
# and both bodies get a `- **Linked:** <other ref>` bullet in `## Context`
# (writing-standard.md § 3). The other board's config usually is not on this
# machine, so its project is passed explicitly (--project owner/number) and never
# guessed. <owner/name> must be in this config's `linked_repos`: a ref to a repo
# that is not linked makes super-board-deps.sh fail the card safe, so link
# refuses before writing anything. For --new waits the OTHER board's config needs
# this repo in its linked_repos; link reminds, it cannot check.
#
# Owner/number/repo come from --config (project.owner, project.number,
# repo.remote), else $BUILD_LOOP_OWNER / $BUILD_LOOP_PROJECT and the origin remote.
#
# Cache: <main checkout>/.claude/super-board/cache/board-<owner>-<number>.json,
# shared by every worktree. Option ids are reminted whenever someone edits the
# column list, so a failed move drops the cache, re-reads it once, and retries
# once — a stale id costs one extra point, never a wrong move.
#
# Exit: 0 ok · 64 usage · 66 config · 69 GitHub read/mutation failed (link: the
#       message lists what was already written and what is left) ·
#       79 reads halted (super-board-github-read.py) · 3 issue not on this board /
#       Status option missing.
set -euo pipefail
# Fixed read commands: a malformed payload is GitHub's, so it halts (#32).
export SB_GITHUB_READ_STRICT=1

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONFIG="${SB_CONFIG:-}"; OWNER="${BUILD_LOOP_OWNER:-}"; NUMBER="${BUILD_LOOP_PROJECT:-}"; REPO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --owner)  OWNER="$2"; shift 2 ;;
    --number) NUMBER="$2"; shift 2 ;;
    --repo)   REPO="$2"; shift 2 ;;
    -h|--help) sed -n '2,61p' "$0"; exit 0 ;;
    --) shift; break ;;
    -*) echo "unknown flag: $1" >&2; exit 64 ;;
    *) break ;;
  esac
done
VERB="${1:-}"; [ -n "$VERB" ] || { echo "usage: super-board-card.sh <move|status|item-id|ids|items|link> …" >&2; exit 64; }
shift

if [ -n "$CONFIG" ]; then
  [ -r "$CONFIG" ] || { echo "config not found: $CONFIG" >&2; exit 66; }
  CJ=$(cat "$CONFIG")
  [ -n "$OWNER" ]  || OWNER=$(echo "$CJ" | jq -r '.project.owner // ""')
  [ -n "$NUMBER" ] || NUMBER=$(echo "$CJ" | jq -r '.project.number // ""')
  [ -n "$REPO" ]   || REPO=$(echo "$CJ" | jq -r '.repo.remote // ""')
fi
[ -n "$REPO" ] || REPO=$(git remote get-url origin 2>/dev/null || true)
REPO=$(echo "$REPO" | sed -E 's#^(https?://github\.com/|git@github\.com:)##; s#\.git$##')
[ -n "$OWNER" ] && [ -n "$NUMBER" ] || { echo "project owner/number unknown: pass --config or --owner/--number" >&2; exit 66; }

# Reads go through the kit's bounded retry helper when it is installed, so a
# board read shares the run's halt file; plain gh otherwise.
gh_read() {
  if [ -f "$HERE/super-board-github-read.py" ]; then
    python3 "$HERE/super-board-github-read.py" --kind json -- "$@"
  else
    gh "$@"
  fi
}
no_errors() { jq -e 'if type == "array" then all(.[]; (.errors // null) == null) else (.errors // null) == null end' >/dev/null; }

# ── cache ───────────────────────────────────────────────────────────────
cache_dir() {
  if [ -n "${SB_CARD_CACHE_DIR:-}" ]; then echo "$SB_CARD_CACHE_DIR"; return; fi
  local common
  common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  if [ -n "$common" ]; then echo "$(dirname "$common")/.claude/super-board/cache"
  else echo "$PWD/.claude/super-board/cache"; fi
}
CACHE_DIR=$(cache_dir)
CACHE="$CACHE_DIR/board-${OWNER}-${NUMBER}.json"
cache_get() { [ -s "$CACHE" ] && jq -r "($1) // empty" "$CACHE" 2>/dev/null || true; }
cache_put() {  # $1 = jq filter applied to the current cache, with --arg/--argjson in "$@"
  local filter="$1"; shift
  mkdir -p "$CACHE_DIR"
  [ -f "$CACHE_DIR/.gitignore" ] || echo '*' > "$CACHE_DIR/.gitignore"
  local cur tmp
  cur=$(cat "$CACHE" 2>/dev/null || echo '{}'); echo "$cur" | jq -e . >/dev/null 2>&1 || cur='{}'
  tmp=$(mktemp "$CACHE_DIR/.board.XXXXXX")
  echo "$cur" | jq "$@" "$filter" > "$tmp" && mv "$tmp" "$CACHE" || rm -f "$tmp"
}
cache_drop() { rm -f "$CACHE"; }

# ── reads ───────────────────────────────────────────────────────────────
load_ids() {  # fills PROJECT_ID FIELD_ID OPTIONS_JSON — 0 points when cached, else 1
  PROJECT_ID=$(cache_get .projectId); FIELD_ID=$(cache_get .fieldId)
  OPTIONS_JSON=$(cache_get '.options // empty | tojson')
  [ -n "$PROJECT_ID" ] && [ -n "$FIELD_ID" ] && [ -n "$OPTIONS_JSON" ] && return 0
  local out
  out=$(gh_read api graphql -f query='query($owner:String!,$number:Int!){
      repositoryOwner(login:$owner){ ... on ProjectV2Owner { projectV2(number:$number){ id
        field(name:"Status"){ ... on ProjectV2SingleSelectField { id options { id name } } } } } } }' \
      -F owner="$OWNER" -F number="$NUMBER") || return $?
  echo "$out" | no_errors || { echo "Status field read returned errors" >&2; return 69; }
  PROJECT_ID=$(echo "$out" | jq -r '.data.repositoryOwner.projectV2.id // empty')
  FIELD_ID=$(echo "$out" | jq -r '.data.repositoryOwner.projectV2.field.id // empty')
  OPTIONS_JSON=$(echo "$out" | jq -c '[.data.repositoryOwner.projectV2.field.options[]? | {(.name): .id}] | add // {}')
  [ -n "$PROJECT_ID" ] && [ -n "$FIELD_ID" ] || { echo "project $OWNER/$NUMBER or its Status field not found" >&2; return 69; }
  cache_put '.projectId = $p | .fieldId = $f | .options = $o' --arg p "$PROJECT_ID" --arg f "$FIELD_ID" --argjson o "$OPTIONS_JSON"
}

# One targeted query (1 point): this issue's item on this board, and its Status.
read_card() {  # $1 = issue; sets ITEM_ID STATUS_NAME
  [ -n "$REPO" ] || { echo "repo unknown: pass --repo or a config with repo.remote" >&2; return 66; }
  local out
  out=$(gh_read api graphql -f query='query($owner:String!,$name:String!,$issue:Int!){
      repository(owner:$owner,name:$name){ issueOrPullRequest(number:$issue){
        ... on Issue { projectItems(first:20){ nodes{ id project{ number owner{ ... on User{login} ... on Organization{login} } }
          fieldValueByName(name:"Status"){ ... on ProjectV2ItemFieldSingleSelectValue{ name } } } } }
        ... on PullRequest { projectItems(first:20){ nodes{ id project{ number owner{ ... on User{login} ... on Organization{login} } }
          fieldValueByName(name:"Status"){ ... on ProjectV2ItemFieldSingleSelectValue{ name } } } } } } } }' \
      -F owner="${REPO%%/*}" -F name="${REPO##*/}" -F issue="$1") || return $?
  echo "$out" | no_errors || { echo "card read for #$1 returned errors" >&2; return 69; }
  local node
  node=$(echo "$out" | jq -c --argjson n "$NUMBER" --arg o "$OWNER" '
    [.data.repository.issueOrPullRequest.projectItems.nodes[]?
     | select(.project.number == $n and ((.project.owner.login // "") | ascii_downcase) == ($o | ascii_downcase))][0] // empty')
  [ -n "$node" ] || { echo "#$1 is not on project $OWNER/$NUMBER" >&2; return 3; }
  ITEM_ID=$(echo "$node" | jq -r .id)
  STATUS_NAME=$(echo "$node" | jq -r '.fieldValueByName.name // ""')
  cache_put '.items[$k] = $v' --arg k "$1" --arg v "$ITEM_ID"
}

item_id() {  # $1 = issue
  ITEM_ID=$(cache_get ".items[\"$1\"]")
  [ -n "$ITEM_ID" ] || read_card "$1"
}

set_status() {  # $1 = item id, $2 = option id — one mutation
  local out
  out=$(gh api graphql -f query='mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){
      updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){ projectV2Item{ id } } }' \
      -f p="$PROJECT_ID" -f i="$1" -f f="$FIELD_ID" -f o="$2" 2>&1) || { echo "$out" >&2; return 69; }
  echo "$out" | no_errors || { echo "$out" >&2; return 69; }
}

move() {  # $1 = issue, $2 = Status name
  local issue="$1" name="$2" opt attempt
  for attempt in 1 2; do
    load_ids || return $?
    opt=$(echo "$OPTIONS_JSON" | jq -r --arg n "$name" '.[$n] // empty')
    if [ -z "$opt" ]; then
      [ "$attempt" = 1 ] && { cache_drop; continue; }
      echo "Status option '$name' is not on project $OWNER/$NUMBER" >&2; return 3
    fi
    item_id "$issue" || return $?
    if set_status "$ITEM_ID" "$opt"; then echo "moved #$issue → $name"; return 0; fi
    [ "$attempt" = 1 ] && { echo "move failed; re-reading board ids once" >&2; cache_drop; }
  done
  return 69
}

items() {  # whole board, gh item-list shape, ~1 point per 100 cards
  local pages
  pages=$(gh_read api graphql --paginate --slurp -f query='query($owner:String!,$number:Int!,$endCursor:String){
      repositoryOwner(login:$owner){ ... on ProjectV2Owner { projectV2(number:$number){
        items(first:100, after:$endCursor){ totalCount pageInfo{ hasNextPage endCursor }
          nodes{ id fieldValueByName(name:"Status"){ ... on ProjectV2ItemFieldSingleSelectValue{ name } }
            content{ __typename
              ... on Issue { number title url body repository{ nameWithOwner } labels(first:30){ nodes{ name } } }
              ... on PullRequest { number title url repository{ nameWithOwner } labels(first:30){ nodes{ name } } }
              ... on DraftIssue { title } } } } } } } }' \
      -F owner="$OWNER" -F number="$NUMBER") || return $?
  echo "$pages" | no_errors || { echo "board read returned errors" >&2; return 69; }
  echo "$pages" | jq -e '
    [ .[].data.repositoryOwner.projectV2.items ] as $p
    | ($p[0].totalCount) as $total
    | [ $p[].nodes[] | select(.content != null)
        | { id,
            title: .content.title,
            content: ({ type: .content.__typename, title: .content.title }
                      + (if .content.__typename == "DraftIssue" then {} else
                          { number: .content.number, url: .content.url,
                            repository: .content.repository.nameWithOwner }
                          + (if .content.body != null then { body: .content.body } else {} end) end)) }
          + (if .fieldValueByName.name then { status: .fieldValueByName.name } else {} end)
          + (if ((.content.labels.nodes // []) | length) > 0
             then { labels: [ .content.labels.nodes[].name ] } else {} end) ] as $items
    | if ($items | length) == ([ $p[].nodes[] ] | length) and ([ $p[].nodes[] ] | length) == $total
      then { items: $items, totalCount: $total }
      else error("board read incomplete: \([ $p[].nodes[] ] | length) of \($total) items") end' \
    || { echo "board read incomplete" >&2; return 69; }
}

# ── link (cross-board flow) ─────────────────────────────────────────────
# edit_body <linked|blocker> <ref> <why> < body > body: one bullet added where the
# ticket format puts it (writing-standard.md § 3). Idempotent: a rerun after a
# partial failure never doubles a bullet. A lone `- None.` gives way to a blocker.
edit_body() {
  python3 -c '
import re, sys
mode, ref, why = sys.argv[1:4]
lines = sys.stdin.buffer.read().decode("utf-8").replace("\r\n", "\n").split("\n")
def bounds(name):
    heads = [i for i, l in enumerate(lines) if re.match(r"^##[ \t]+" + re.escape(name) + r"[ \t]*$", l)]
    if not heads:
        return None
    start = heads[-1]
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^##[ \t]", lines[i])), len(lines))
    return start, end
def has(pattern, b):
    return b is not None and any(re.search(pattern, l, re.I) for l in lines[b[0] + 1:b[1]])
tail = (" — " + why) if why else ""
exact = re.escape(ref) + r"(?![0-9])"
if mode == "linked":
    b = bounds("Context")
    if not has(r"^\s*-\s*\*\*Linked:\*\*.*" + exact, b):
        line = "- **Linked:** " + ref + tail
        if b:
            at = b[1]
            while at > b[0] + 1 and not lines[at - 1].strip():
                at -= 1
            lines.insert(at, line)
        else:
            nxt = next((i for i, l in enumerate(lines)
                        if re.match(r"^##[ \t]+(Fix|Evidence|Acceptance Criteria|Risk|Blocked by)[ \t]*$", l)), None)
            block = ["## Context", line, ""]
            if nxt is None:
                lines += [""] + block
            else:
                lines[nxt:nxt] = block
else:
    b = bounds("Blocked by")
    if not has(r"^\s*-\s*" + exact, b):
        bullet = "- " + ref + tail
        if b is None:
            while lines and not lines[-1].strip():
                lines.pop()
            lines += ["", "## Blocked by", "", bullet, ""]
        else:
            body = [l for l in lines[b[0] + 1:b[1]] if not re.match(r"^\s*-\s*None\.?\s*$", l, re.I)]
            bullets = [i for i, l in enumerate(body) if re.match(r"^\s*-\s", l)]
            if bullets:
                body.insert(bullets[-1] + 1, bullet)
            else:
                if not body or body[0].strip():
                    body.insert(0, "")
                body.insert(1, bullet)
            lines[b[0] + 1:b[1]] = body
sys.stdout.buffer.write("\n".join(lines).encode("utf-8"))
' "$@"
}

LINK_TMP=""
apply_edit() {  # edit $LINK_TMP in place
  edit_body "$@" < "$LINK_TMP" > "$LINK_TMP.n" && mv "$LINK_TMP.n" "$LINK_TMP" \
    || { echo "link: could not edit the issue body; nothing was written for this step" >&2; return 70; }
}
trap 'rm -f "${LINK_TMP:-/nonexistent}" "${LINK_TMP:-/nonexistent}.n"' EXIT
link() {  # $1 = issue in this repo; flags as in the header
  local issue="$1"; shift
  local to="" project="" new="" title="" body_file="" why=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --to)        to="$2"; shift 2 ;;
      --project)   project="$2"; shift 2 ;;
      --new)       new="$2"; shift 2 ;;
      --title)     title="$2"; shift 2 ;;
      --body-file) body_file="$2"; shift 2 ;;
      --why)       why="$2"; shift 2 ;;
      *) echo "link: unknown flag: $1" >&2; return 64 ;;
    esac
  done
  case "$issue" in ''|*[!0-9]*) echo "link: <issue> must be an issue number in $REPO" >&2; return 64 ;; esac
  [[ "$to" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "link: --to <owner/name> is required (the linked repo)" >&2; return 64; }
  [[ "$project" =~ ^[A-Za-z0-9_.-]+/[0-9]+$ ]] || {
    echo "link: --project <owner/number> is required — the board of $to. Its config is not read from here; copy project.owner/project.number from that repo's .claude/super-board/configs/<slug>.json" >&2; return 64; }
  case "$new" in blocks|waits) ;; *) echo "link: --new blocks|waits is required (does the new card block #$issue, or wait on it?)" >&2; return 64 ;; esac
  [ -n "$title" ] || { echo "link: --title is required" >&2; return 64; }
  [ -z "$body_file" ] || [ -r "$body_file" ] || { echo "link: --body-file not readable: $body_file" >&2; return 66; }
  [ -n "$REPO" ] || { echo "link: repo unknown: pass --repo or a config with repo.remote" >&2; return 66; }
  [ "$(echo "$to" | tr '[:upper:]' '[:lower:]')" != "$(echo "$REPO" | tr '[:upper:]' '[:lower:]')" ] \
    || { echo "link: --to is this repo; a counterpart lives on another repo's board" >&2; return 64; }
  # The ref must survive super-board-deps.sh, which trusts only linked_repos.
  [ -n "${CJ:-}" ] || { echo "link: needs --config, to check linked_repos before writing anything" >&2; return 66; }
  echo "$CJ" | jq -e --arg r "$to" '(.linked_repos // []) | map(ascii_downcase) | index($r | ascii_downcase) != null' >/dev/null \
    || { echo "link: $to is not in linked_repos of $CONFIG — add it first, or the new Blocked-by ref makes the card unreadable to the planner" >&2; return 64; }

  local here_ref="$REPO#$issue" cur cur_body new_url new_n there_ref item done_steps role_here role_there
  if [ "$new" = blocks ]; then role_there="waits on this card"; role_here="blocks this card"
  else role_there="blocks this card"; role_here="waits on this card"; fi
  cur=$(gh_read issue view "$issue" --repo "$REPO" --json number,title,body,comments) || return $?
  cur_body=$(echo "$cur" | jq -r '.body // ""')
  if echo "$cur" | jq -e '[.comments[]?.body // "" | split("\n")[] | select(test("^[ \t]*blocked-by:"; "i"))] | length > 0' >/dev/null; then
    echo "note: #$issue has a blocked-by: line in a Block comment, which wins over the body — update that line too" >&2
  fi
  LINK_TMP=$(mktemp)
  { if [ -n "$body_file" ]; then cat "$body_file"; else printf '## Problem\n%s\n\n## Blocked by\n\n- None.\n' "$title"; fi; } > "$LINK_TMP"
  apply_edit linked "$here_ref" "$role_there" || return $?
  if [ "$new" = waits ]; then
    apply_edit blocker "$here_ref" "${why:-linked counterpart}" || return $?
  fi

  # 1. the counterpart issue
  python3 "$HERE/super-board-github-read.py" --check || return $?
  new_url=$(gh issue create --repo "$to" --title "$title" --body-file "$LINK_TMP" | tr -d '\r' | tail -1) \
    || { echo "link: creating the issue in $to failed — check $to for it before rerunning; nothing else was written" >&2; return 69; }
  new_n=${new_url##*/}
  case "$new_n" in ''|*[!0-9]*) echo "link: could not read the new issue number from: $new_url — check $to before rerunning" >&2; return 69 ;; esac
  there_ref="$to#$new_n"; done_steps="created $there_ref"

  # 2. onto the other board, in Backlog
  OWNER="${project%%/*}"; NUMBER="${project##*/}"; CACHE="$CACHE_DIR/board-${OWNER}-${NUMBER}.json"
  python3 "$HERE/super-board-github-read.py" --check || return $?
  item=$(gh project item-add "$NUMBER" --owner "$OWNER" --url "$new_url" --format json | jq -r '.id // empty') || item=""
  [ -n "$item" ] || { echo "link: $done_steps, but adding it to project $project failed. Left: add $new_url to that board in Backlog; add the refs to #$issue" >&2; return 69; }
  local opt="" attempt
  for attempt in 1 2; do
    load_ids || return $?
    opt=$(echo "$OPTIONS_JSON" | jq -r '.Backlog // empty')
    [ -n "$opt" ] && break
    cache_drop
  done
  [ -n "$opt" ] || { echo "link: $done_steps and added it to $project, which has no Backlog column. Left: set its Status; add the refs to #$issue" >&2; return 3; }
  python3 "$HERE/super-board-github-read.py" --check || return $?
  set_status "$item" "$opt" || { echo "link: $done_steps and added it to $project, but moving it to Backlog failed. Left: set its Status to Backlog; add the refs to #$issue" >&2; return 69; }
  done_steps="$done_steps, on $project in Backlog"

  # 3. the refs on this repo's card
  printf '%s' "$cur_body" > "$LINK_TMP"
  apply_edit linked "$there_ref" "$role_here" || return $?
  if [ "$new" = blocks ]; then
    apply_edit blocker "$there_ref" "${why:-linked counterpart}" || return $?
  fi
  python3 "$HERE/super-board-github-read.py" --check || return $?
  gh issue edit "$issue" --repo "$REPO" --body-file "$LINK_TMP" >/dev/null \
    || { echo "link: $done_steps, but editing #$issue failed. Left: add '- **Linked:** $there_ref' to its Context$( [ "$new" = blocks ] && echo " and '- $there_ref' to its Blocked by")" >&2; return 69; }

  [ "$new" = blocks ] || echo "note: $to's board must list $REPO in its linked_repos, or $here_ref reads as unreadable there" >&2
  jq -n --arg created "$there_ref" --arg url "$new_url" --arg here "$here_ref" --arg new "$new" \
    '{created: $created, url: $url, linkedTo: $here,
      blocker: (if $new == "blocks" then $created else $here end),
      dependent: (if $new == "blocks" then $here else $created end)}'
}

case "$VERB" in
  link)    [ $# -ge 1 ] || { echo "usage: link <issue> --to <owner/name> --project <owner/number> --new blocks|waits --title <t> [--body-file <md>] [--why <text>]" >&2; exit 64; }
           link "$@" ;;
  move)    [ $# -eq 2 ] || { echo "usage: move <issue> <Status>" >&2; exit 64; }; move "$1" "$2" ;;
  status)  [ $# -eq 1 ] || { echo "usage: status <issue>" >&2; exit 64; }; read_card "$1"; echo "$STATUS_NAME" ;;
  item-id) [ $# -eq 1 ] || { echo "usage: item-id <issue>" >&2; exit 64; }; item_id "$1"; echo "$ITEM_ID" ;;
  ids)     load_ids; jq -n --arg p "$PROJECT_ID" --arg f "$FIELD_ID" --argjson o "$OPTIONS_JSON" '{projectId:$p, fieldId:$f, options:$o}' ;;
  items)   items ;;
  *) echo "unknown verb: $VERB" >&2; exit 64 ;;
esac
