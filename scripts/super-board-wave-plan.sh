#!/usr/bin/env bash
# super-board-wave-plan.sh — compute the next workflow-backend wave from board state.
# Read-only: no gh writes, no locks. The orchestrator owns every mutation; this
# script only says what the next wave should contain and what should be swept.
#
# WAVE SHAPE (changed 2026-08-20 — see "why" below)
#
# A wave is no longer a fixed number of slots. It is:
#
#   every Ready card whose `## Blocked by` list is fully closed,
#   plus every card already in flight in QA or Review,
#   minus anything already claimed by an assignee.
#
# Width therefore follows the dependency graph rather than a tuning knob. On a
# board with 19 free cards the wave is 19; on a board with 2 it is 2. The
# workflow runtime caps *concurrent* agents (min(16, cpus-2)) and queues the
# rest, so dispatching wide costs nothing and finishes sooner.
#
# WHY THE OLD FIXED CAP WAS WRONG, MEASURED. On a real board on 2026-08-20,
# `max_workers` was 7 and 23 of 32 Ready cards were waiting on an open blocker.
# A cap that does not look at dependencies spends its slots on cards that will
# hit their own preflight and park — so it throttled the free cards while doing
# nothing about the blocked ones. Filtering first and then not capping is
# strictly better: fewer wasted dispatches AND more real work per wave.
#
# `max_workers` is kept as a safety valve. Absent or 0 means unlimited, which is
# the default. Set it only to deliberately throttle a machine.
#
# THE BLOCKED SWEEP. `Blocked` used to be terminal: a card parked naming the
# issue it waited on, that issue later closed, and nothing ever read the note
# back. Cards sat until a human noticed — five of them did, on the board above.
# This script now reports, in `sweep`, every Blocked card whose blockers have all
# closed. The orchestrator moves them to Ready before launching, so they join the
# very next wave.
#
# FAIL SAFE ON AN UNREADABLE DEPENDENCY LINE. A card whose `## Blocked by`
# section cannot be parsed confidently is never treated as free. It is reported
# in `flag` so the orchestrator can comment on it, and it is left where it is.
# Guessing "runnable" is how a card gets built against a base branch that does
# not have what it needs. See super-board-deps.sh for what "unreadable" means and
# the three shapes that produce it.
#
# STRANDED BUILDING CARDS. No lane selects from Building: a Builder moves its card
# Ready → Building and on to QA in one lifecycle. A wave stopped (or crashed) in
# between leaves the card in Building, and nothing ever picks it up again. The
# planner runs only between waves, after the crash-recovery sweep has stripped
# leaked claims, so a Building card with no assignee has no live worker. It is
# reported in `stranded`; the orchestrator checks its branch and worktree and
# moves it back to Ready, keeping the branch.
#
# LABEL ROUTING (v3.0.0). One board shape for every project; a card's label picks
# its lane. Three labels: `qa`, `bug`, `feature`. A Ready card labelled `qa` skips
# the Builder and goes straight to the Tester (lane "qa"); every other Ready card,
# labelled or not, is built first (lane "build"). Each picked card carries `lane`
# and its `labels`. The old per-board `variant` is gone: a config still saying
# "qa-only" is refused (exit 65) until `super-board onboard` upgrades it, because
# reading it as "full" would start building on a board meant only for testing.
#
# Usage:
#   super-board-wave-plan.sh --config <config.json> [--items <project-items.json>]
#                            [--deps <deps.json>]
# A hub config (`members`) plans every member; see HUB BOARDS below.
# Without --items, fetches live board state via super-board-card.sh items — the
# item-list shape for ~1 GraphQL point per 100 cards, where `gh project item-list`
# spends 101 per 100 (fieldValues x four nested connections).
# Without --deps, derives the graph via super-board-deps.sh (passing
# config.linked_repos as --linked, so blockers on another repo's board count).
# Pass --deps in tests to keep the script offline. `clearedBy` holds numbers for
# this repo's blockers and "owner/repo#N" strings for a linked repo's.
#
# CARD IDENTITY. A Project may hold issues of several repos, each with its own #7.
# A card is `owner/repo#N` (compared lower-cased). Only home cards — the repo in
# config.repo.remote, or a card that names no repo — are planned, and every lookup
# (graph, triage) is by card key, so another repo's #7 never borrows this repo's
# verdict. Another repo's card in a working column is reported in `foreign` and
# nothing else, linked repo or not (linked_repos lends blocker state, not cards).
# Every reported card carries `repository` (nameWithOwner) when one is known.
#
# Stdout:
#   { "cards": [ {"number":10,"repository":"acme/api","status":"Review","title":"…","lane":"review","labels":[]} ],
#     "sweep": [ {"number":37,"repository":"acme/api","title":"…","clearedBy":[32]} ],
#     "resume": [ {"number":51,"repository":"acme/api","title":"…"} ],
#     "refreshApproval": [ {"number":52,"repository":"acme/api","title":"…","why":"PR head changed after review"} ],
#     "flag":  [ {"number":82,"repository":"acme/api","title":"…","why":"…"} ],
#     "foreign": [ {"number":7,"repository":"acme/web","status":"Ready","title":"…","why":"card from acme/web: not a member of this board"} ],
#     "stranded": [ {"number":44,"repository":"acme/api","title":"…"} ],
#     "triage": [ {"number":45,"repository":"acme/api","title":"…","missing":["Fix","checkable acceptance criterion"],"hints":["…"]} ] }
set -euo pipefail

CONFIG=""; ITEMS_FILE=""; DEPS_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --items)  ITEMS_FILE="$2"; shift 2 ;;
    --deps)   DEPS_FILE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done
[ -n "$CONFIG" ] && [ -e "$CONFIG" ] || { echo "config not found: ${CONFIG:-<unset>}" >&2; exit 66; }

# Read the config ONCE — $CONFIG may be a process substitution (test mode),
# which is a FIFO and cannot be read twice.
CONFIG_JSON=$(cat "$CONFIG")
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# HUB BOARDS. A config with `members` plans each member as home in turn, through a
# resolved single-repo config (super-board-config.py member --path), over ONE board read.
# Every picked card carries `member`, `configPath` (that member's resolved config: lanes
# run with it), `checkout`, `baseBranch` and `deploy`; `foreign` keeps only repos that are
# no member, and says so for the hub repo's own issues (split, never built). Members trust
# each other's issues as blockers (the resolver adds the others to linked_repos), so a
# frontend card blocked by a backend issue is swept when that issue closes.
# In members mode --deps is {"<member>": <deps graph>, …}.
# PARENTS. A `parent`-labelled card (hub.md → Split) is never built, wherever it sits:
# it is dropped before any member plans and reported in `parents` while in a working column.
# SHARES. The top-level max_workers caps the merged wave; each member gets
# `members.<k>.max_workers`, else an even share (ceil(max_workers / members)), so one repo
# cannot starve the other. Slots a member without its own cap leaves unused go to the
# others' leftovers. In-flight cards first throughout.
if printf '%s' "$CONFIG_JSON" | jq -e 'has("members")' >/dev/null; then
  [ -f "$CONFIG" ] || { echo "a members config must be a file (paths resolve from its board root)" >&2; exit 64; }
  python3 "$HERE/super-board-config.py" validate --config "$CONFIG" || exit $?
  python3 "$HERE/super-board-github-read.py" --check --config "$CONFIG"
  MTMP=$(mktemp -d)
  trap 'rm -rf "$MTMP"' EXIT
  if [ -n "$ITEMS_FILE" ]; then cat "$ITEMS_FILE" > "$MTMP/items.json"
  else
    "$HERE/super-board-card.sh" --owner "$(printf '%s' "$CONFIG_JSON" | jq -r '.project.owner')"       --number "$(printf '%s' "$CONFIG_JSON" | jq -r '.project.number')" items > "$MTMP/items.json"
  fi
  [ -z "$DEPS_FILE" ] || cat "$DEPS_FILE" > "$MTMP/deps.json"
  # Same label shapes as labels_of below: on the item or the content, strings or {name}.
  LABELS_OF='[ ((.labels // []) + (.content.labels // []))[] | (if type == "object" then .name else . end) | ascii_downcase ]'
  jq "[ .items[] | select(.content.type == \"Issue\") | select($LABELS_OF | index(\"parent\"))
        | select(.status as \$s | [\"Ready\",\"Building\",\"QA\",\"Review\",\"Blocked\"] | index(\$s))
        | { number: .content.number, repository: (.content.repository // \"\"), status, title: .content.title,
            why: \"parent: its sub-issues are built, never the parent (close-parents closes it)\" } ]" \
    "$MTMP/items.json" > "$MTMP/parents.json"
  jq ".items |= map(select(($LABELS_OF | index(\"parent\")) | not))" "$MTMP/items.json" > "$MTMP/items.plan.json"
  MEMBERS=$(python3 "$HERE/super-board-config.py" members --config "$CONFIG") || exit $?
  printf '[]' > "$MTMP/plans.json"
  for KEY in $(printf '%s' "$MEMBERS" | jq -r '.[].key' | tr -d '\r'); do
    MCFG=$(python3 "$HERE/super-board-config.py" member "$KEY" --config "$CONFIG" --path | tr -d '\r') || exit $?
    jq '.max_workers = 0' "$MCFG" > "$MTMP/$KEY.json" || exit $?
    DEPS_ARGS=()
    if [ -n "$DEPS_FILE" ]; then
      jq --arg k "$KEY" '.[$k] // {}' "$MTMP/deps.json" > "$MTMP/$KEY.deps.json"
      DEPS_ARGS=(--deps "$MTMP/$KEY.deps.json")
    fi
    PLAN=$("${BASH_SOURCE[0]}" --config "$MTMP/$KEY.json" --items "$MTMP/items.plan.json" ${DEPS_ARGS[@]+"${DEPS_ARGS[@]}"}) || exit $?
    printf '%s' "$PLAN" > "$MTMP/$KEY.plan.json"
    jq --arg k "$KEY" --arg cfg "$MCFG" --slurpfile p "$MTMP/$KEY.plan.json" --slurpfile m "$MTMP/$KEY.json" \
       --argjson cap "$(printf '%s' "$CONFIG_JSON" | jq --arg k "$KEY" '.members[$k].max_workers // 0')" \
       '. + [{ key: $k, plan: $p[0], cap: $cap,
               lane: { member: $k, configPath: $cfg, checkout: $m[0].repo.path, baseBranch: $m[0].base_branch,
                       deploy: (($m[0].deploy | type) == "object"
                                and (($m[0].deploy.branch // $m[0].base_branch) == $m[0].base_branch)) } }]' \
       "$MTMP/plans.json" > "$MTMP/plans.next" && mv "$MTMP/plans.next" "$MTMP/plans.json"
  done
  HUB_REPO=$(printf '%s' "$CONFIG_JSON" | jq -r '.hub.remote // ""' | tr -d '\r' \
               | sed -E 's#^(https?://github\.com/|git@github\.com:)##; s#\.git$##')
  jq --argjson members "$MEMBERS" --argjson cap "$(printf '%s' "$CONFIG_JSON" | jq -r '.max_workers // 0')" \
     --arg hub "$HUB_REPO" --slurpfile parents "$MTMP/parents.json" '
    def tagged($f): [ .[] | .key as $k | .plan[$f][] | . + { member: $k } ];
    ([ $members[].repo | ascii_downcase ]) as $mine
    | def rank: { "Review": 0, "QA": 1 }[.status] // 2;
    # Each member'"'"'s share: its own cap (hard), else an even share of a positive board cap.
    length as $n
    | (if $cap > 0 then (($cap + $n - 1) / $n | floor) else 0 end) as $even
    | [ .[] | . + { share: (if .cap > 0 then .cap elif $even > 0 then $even else null end) } ] as $ps
    | ([ $ps[] as $p | ($p.plan.cards | if $p.share then .[:$p.share] else . end)[] | . + $p.lane ] | sort_by(rank)) as $first
    | ([ $ps[] | select(.cap == 0 and .share != null) as $p | $p.plan.cards[$p.share:][] | . + $p.lane ] | sort_by(rank)) as $rest
    | { cards: (if $cap <= 0 then $first
                elif ($first | length) >= $cap then $first[:$cap]
                else ($first + $rest[:($cap - ($first | length))]) | sort_by(rank) end),
        sweep: tagged("sweep"), resume: tagged("resume"), refreshApproval: tagged("refreshApproval"),
        flag: tagged("flag"),
        foreign: ([ .[].plan.foreign[] | select((.repository | ascii_downcase) as $r | $mine | index($r) | not) ]
                  | unique_by([(.repository | ascii_downcase), .number])
                  | map(.why = (if $hub != "" and (.repository | ascii_downcase) == ($hub | ascii_downcase)
                                then "hub issue: triage splits it into member sub-issues; never built"
                                else "card from \(.repository): not a member of this board" end))),
        parents: $parents[0],
        stranded: tagged("stranded"), triage: tagged("triage") }' "$MTMP/plans.json"
  exit 0
fi

printf '%s\n' "$CONFIG_JSON" | python3 "$HERE/super-board-verification-ready.py" --config -
export SB_REPO_PATH=$(echo "$CONFIG_JSON" | jq -r '.repo.path // "."')
python3 "$HERE/super-board-github-read.py" --check
VARIANT=$(echo "$CONFIG_JSON" | jq -r '.variant // ""')
# 0 or absent = unlimited. A wave is sized by the dependency graph, not a knob.
MAX_WORKERS=$(echo "$CONFIG_JSON" | jq -r '.max_workers // 0')
OWNER=$(echo "$CONFIG_JSON" | jq -r '.project.owner')
NUMBER=$(echo "$CONFIG_JSON" | jq -r '.project.number')
# The home repo (config repo.remote): its cards are this board's. Empty only in
# offline tests with no remote, and then every card counts as home.
HOME_REPO=$(echo "$CONFIG_JSON" | jq -r '.repo.remote // ""' | tr -d '\r' \
              | sed -E 's#^(https?://github\.com/|git@github\.com:)##; s#\.git$##')
LINKED_JSON=$(echo "$CONFIG_JSON" | jq -c '[(.linked_repos // [])[] | strings]')

if [ -n "$ITEMS_FILE" ]; then
  ITEMS=$(cat "$ITEMS_FILE")
else
  ITEMS=$("$HERE/super-board-card.sh" --owner "$OWNER" --number "$NUMBER" items)
fi

# `variant` was removed in v3.0.0 (labels route cards now). Absent or "full" is
# fine. Anything else — "qa-only", or a typo — is refused loudly rather than
# guessed: a guess either builds on a test-only board or strands the QA column.
case "$VARIANT" in
  ""|full) COLUMNS='["Review","QA","Ready"]' ;;
  qa-only) echo "config still says variant \"qa-only\" — removed in v3.0.0. Run /super-board onboard: it upgrades the config and labels those cards qa." >&2; exit 65 ;;
  *) echo "invalid variant in config: ${VARIANT} (variant was removed in v3.0.0 — delete the key)" >&2; exit 65 ;;
esac

# The dependency graph. Only Ready and Blocked cards are gated by it — a card in
# QA or Review already passed its own preflight and is mid-flight.
if [ -n "$DEPS_FILE" ]; then
  DEPS=$(cat "$DEPS_FILE")
else
  REPO="$HOME_REPO"
  [ -n "$REPO" ] || { echo "config.repo.remote is required to derive dependencies" >&2; exit 66; }
  HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  # A graph we cannot fetch is not a graph of zero blockers. Fail rather than
  # plan a wave that treats every card as free.
  # Cross-board flow: foreign blockers are trusted only for config.linked_repos.
  LINKED=$(echo "$CONFIG_JSON" | jq -r '(.linked_repos // []) | join(",")' | tr -d '')
  DEPS=$("$HERE/super-board-deps.sh" --repo "$REPO" --linked "$LINKED" --limit 300) || {
    rc=$?; echo "could not derive the dependency graph for ${REPO}" >&2; exit "$rc"; }
fi

# TRIAGE. A vague Ready card makes its lane stall or build the wrong thing, so every
# unassigned Ready issue goes through super-board-card-ready.py (the ticket format, a
# checkable criterion, deps.sh's own `## Blocked by` verdict from the graph above). A
# not-ready card is left out of `cards` and reported in `triage` with its `missing` list;
# the orchestrator labels it `needs-triage` (this script stays read-only). Bodies come
# from the board read itself (super-board-card.sh items carries `body`), no extra fetch.
# Fail safe: a checker crash or unreadable answer marks every candidate not ready.
# `triage.enabled: false` in the config skips the check.
TRIAGE='[]'
if [ "$(echo "$CONFIG_JSON" | jq -r 'if .triage.enabled == false then "off" else "on" end')" = on ]; then
  CANDIDATES=$(echo "$ITEMS" | jq -c --arg home "$HOME_REPO" '[ .items[]
      | select(.content.type == "Issue" and .status == "Ready")
      | select((.content.assignees // []) | length == 0)
      | (.content.repository // "") as $r
      | select($home == "" or $r == "" or ($r | ascii_downcase) == ($home | ascii_downcase))
      | { number: .content.number, title: .content.title, body: (.content.body // null),
          labels: ((.labels // []) + (.content.labels // [])) }
        + (if $r != "" then { repository: $r } elif $home != "" then { repository: $home } else {} end) ]')
  if [ "$CANDIDATES" != "[]" ]; then
    TMPD=$(mktemp -d)
    trap 'rm -rf "$TMPD"' EXIT
    printf '%s' "$DEPS" > "$TMPD/deps.json"
    printf '%s' "$CONFIG_JSON" > "$TMPD/config.json"
    RC=0
    TRIAGE=$(printf '%s' "$CANDIDATES" | python3 "$HERE/super-board-card-ready.py" \
               --deps "$TMPD/deps.json" --config "$TMPD/config.json" 2>"$TMPD/err") || RC=$?
    if { [ "$RC" -ne 0 ] && [ "$RC" -ne 3 ]; } || ! printf '%s' "$TRIAGE" | jq -e 'type == "array"' >/dev/null 2>&1; then
      echo "card-ready failed (exit $RC): $(head -c 300 "$TMPD/err") — no Ready card dispatched" >&2
      TRIAGE=$(echo "$CANDIDATES" | jq -c '[ .[] | { number } + (if .repository then { repository } else {} end)
                 + { ready: false, missing: ["card-ready failed"],
                     hints: ["super-board-card-ready.py crashed; rerun it on this card"] } ]')
    fi
  fi
fi

# Review extras used to be gated behind human_approves_merge, because concurrent
# auto-merges into one base branch race. That guard now lives where the race
# actually is — the merge step takes a lock inside the wave workflow and the
# freshness gate re-verifies under it — so reviews may run in parallel here.
echo "$ITEMS" | jq --argjson cols "$COLUMNS" --argjson cap "$MAX_WORKERS" --argjson deps "$DEPS" \
                   --argjson triage "$TRIAGE" --arg home "$HOME_REPO" --argjson linked "$LINKED_JSON" '
  # CARD KEYS. A Project can hold issues of several repos, and two repos both have
  # a #7. A card is `owner/repo#N`, compared lower-cased. Only home-repo cards
  # (repo.remote; a card naming no repo is home) are planned. Everything below is
  # keyed by card key, so another repo'"'"'s #7 never meets this repo'"'"'s graph entry.
  def ckey($repo; $n): (if $home == "" then "" else (($repo // $home) | ascii_downcase) end) + "#" + ($n | tostring);
  def is_home: (.content.repository // "") as $r
    | $home == "" or $r == "" or ($r | ascii_downcase) == ($home | ascii_downcase);
  def ident: { number } + (if .repository then { repository } else {} end);
  # deps.sh output is keyed by number for its own --repo; re-key it by card key.
  ([ $deps | to_entries[] | select(.key | test("^[0-9]+$")) | select(.value | type == "object")
     | { key: ckey(.value.repository; (.key | tonumber)), value: .value } ] | from_entries) as $dk
  | def dep($c): $dk[ckey($c.repository; $c.number)];
  ([ $triage[] | select(.ready | not) | { key: ckey(.repository; .number), value: . } ] | from_entries) as $notReady
  | def unready($c): $notReady[ckey($c.repository; $c.number)] != null;
  # gh project item-list puts label names on the item (`labels`); some shapes put
  # them on the content, as strings or {name}. Lower-cased, names only.
  def labels_of: [ ((.labels // []) + (.content.labels // []))[]
                   | (if type == "object" then .name else . end) | ascii_downcase ] | unique;
  def lane_of($status; $labels):
    if $status == "Review" then "review"
    elif $status == "QA" then "qa"
    elif ($labels | index("qa")) then "qa"
    else "build" end;
  # Unknown to the graph (closed, or beyond the fetch limit) is not "free".
  def free($c): (dep($c) | if . == null then false else .runnable end);

  [ .items[]
    | select(.content.type == "Issue")
    | select(is_home)
    | select((.content.assignees // []) | length == 0)
    | labels_of as $l
    | { number: .content.number }
      + (if (.content.repository // "") != "" then { repository: .content.repository }
         elif $home != "" then { repository: $home } else {} end)
      + { status: .status, title: .content.title, lane: lane_of(.status; $l), labels: $l } ] as $all

  # Cards of another repo on this Project: never planned, never matched to a
  # same-numbered home issue. Reported while they sit in a working column. A
  # linked repo'"'"'s card is still not this board'"'"'s (its own board plans it);
  # linked_repos only lends its issues'"'"' state to `## Blocked by` lines.
  | ($linked | map(ascii_downcase)) as $lk
  | [ .items[]
      | select(.content.type == "Issue")
      | select(is_home | not)
      | select(.status as $s | ["Ready","Building","QA","Review","Blocked"] | index($s))
      | .content.repository as $r
      | { number: .content.number, repository: $r, status: .status, title: .content.title,
          why: ("card from " + $r + ": not a member of this board"
                + (if ($lk | index($r | ascii_downcase)) then
                     " (a linked repo: its own board plans it; linked_repos only lends blocker state)"
                   else "" end)) } ] as $foreign

  # In flight: already past preflight, so the graph does not gate them. Iterated
  # in $cols order (Review, then QA) rather than board order, so a throttled wave
  # spends its slots on the work closest to Done.
  | [ $cols[] as $c | $all[] | select(.status == $c and $c != "Ready") ] as $flight

  # Ready: only the ones the graph says are actually free.
  # Ready: only the ones the graph says are actually free, and written well enough.
  | [ $all[] | select(.status == "Ready") | select(free(.)) | select(unready(.) | not) ] as $ready

  # Downstream-first, so work already begun finishes before new work starts.
  | ($flight + $ready) as $picked
  | { cards: (if $cap > 0 then ($picked | .[:$cap]) else $picked end),

      # Blocked cards whose blockers have all closed. The orchestrator moves
      # these to Ready before launching, so they join this very wave.
      sweep: [ $all[]
               | select(.status == "Blocked")
               | select(free(.))
               | ident + { title, clearedBy: (dep(.) | .blockers) } ],

      # Verified current-head human approval from the dependency helper. The
      # Reviewer independently rechecks it; labels are only UI state.
      resume: [ $all[]
                | select(.status == "Blocked")
                | select(dep(.) != null and (dep(.).needsYouDone // false))
                | ident + { title } ],

      # Changed code while Blocked needs a new review/request, never approval.
      refreshApproval: [ $all[]
                         | select(.status == "Blocked")
                         | select(dep(.).approvalRefresh // false)
                         | ident + { title, why: dep(.).approvalWhy } ],

      # Cards the graph could not read. Left where they are, reported so the
      # orchestrator can ask for the line to be fixed.
      flag:  [ $all[]
               | select(.status as $s | ["Ready","Blocked"] | index($s))
               | select(dep(.) != null and (dep(.).parseable | not))
               | ident + { title, why: (dep(.) | .why) } ],

      # Another repo'"'"'s cards on this Project (see $foreign above). Report only:
      # no comment, no move — the issue is not this repo'"'"'s.
      foreign: $foreign,

      # Building with no claim between waves = no live worker. Back to Ready.
      stranded: [ $all[] | select(.status == "Building") | ident + { title } ],

      # Ready cards card-ready says are too vague to dispatch. Left in Ready, labelled
      # needs-triage by the orchestrator. A card already in `flag` is reported there only.
      triage: [ $all[]
                | select(.status == "Ready" and unready(.))
                | select((dep(.) != null and (dep(.).parseable | not)) | not)
                | $notReady[ckey(.repository; .number)] as $t
                | ident + { title, missing: $t.missing, hints: $t.hints } ] }'
