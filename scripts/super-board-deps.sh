#!/usr/bin/env bash
# super-board-deps.sh — work out, for every open issue, whether it can start now.
#
# Two sources, in precedence order:
#   1. the last `blocked-by:` line in the issue's Block comments (what a lane
#      wrote when it most recently parked the card)
#   2. the issue body's `## Blocked by` section (what the ticket was written with)
#
# This is the dependency primitive. The wave planner uses it to size a wave and to
# sweep the Blocked column; anything else that needs to know "what is waiting on
# what" should call this rather than re-parse issue bodies.
#
# WHY A SEPARATE SCRIPT, AND WHY IT IS FUSSY
#
# Parsing this section naively gets it wrong in three ways that were all observed
# on a real board on 2026-08-20:
#
#   1. The preflight banner at the top of every super-board ticket quotes the words
#      "## Blocked by" inside backticks. A parser that takes the FIRST match reads
#      the banner and finds no blockers. This takes the LAST heading-shaped match.
#
#   2. Tickets written by `to-tickets` follow its template, whose example line is
#      `**Blocked by:** … or "None — can start immediately"`. In practice that
#      produced lines like:
#
#          - None — but #26 must be merged first, since this edits the module it builds.
#
#      A human reads one blocker. A parser reads the word "None" and builds it
#      early. Any line that says None AND names an issue is UNPARSEABLE, never
#      "no blockers" — see the fail-safe rule below.
#
#   3. A missing section and an empty section both mean "nothing was written",
#      which is not the same as "nothing blocks this". Both are UNPARSEABLE.
#
# FAIL SAFE, NOT FAIL OPEN. An issue this script cannot read confidently is
# reported `parseable: false` and callers must treat it as blocked. Guessing
# "runnable" on an ambiguous line is how a card gets built against a base branch
# that does not have what it needs yet — one wave saved, one rewrite spent.
#
# BLOCKERS IN ANOTHER REPOSITORY (cross-board flow)
#
# A project split over two repos runs one board per repo, and a frontend card is
# often waiting on a backend card. A blocker may therefore be written as
#
#   - #32                                       this repo
#   - owner/repo#123                            another repo
#   - https://github.com/owner/repo/issues/123  (or /pull/123) — the same thing
#
# in `## Blocked by` bullets and in `blocked-by:` comment lines. A full ref to
# THIS repo (--repo) normalizes to the plain number. A foreign ref is only
# trusted for a repo the config lists in `linked_repos` (passed as --linked):
# anything else makes the card unparseable with a `why` naming the repo — a
# typo'd owner must not read as "a blocker that will never close" any more than
# as "no blocker".
#
# Foreign blockers' state comes from ONE batched GraphQL query (aliases per repo
# and per number) through super-board-github-read.py, never one call per ref. A
# foreign blocker is done only when:
#   - it is an Issue closed with stateReason COMPLETED, or
#   - it is a PullRequest that merged.
# An Issue closed NOT_PLANNED, or a PR closed without merging, will never become
# done, and dropping it silently would build the card without what it asked for.
# Whether the card still needs it is a person's call, so the card is reported
# `parseable: false` with a `why` — the planner flags it, the orchestrator asks
# for the `## Blocked by` line to be fixed. A ref GitHub cannot resolve, or a
# repo the token cannot read, is unreadable state: the blocker stays OPEN.
#
# Usage:
#   super-board-deps.sh --repo <owner/name> [--linked o/a,o/b] [--issues 12,34]
#                       [--from <payload.json>]
#
# --from payload, either shape (tests use it to stay offline):
#   [ <issue>, … ]                                  same-repo blockers only
#   { "issues": [ <issue>, … ],
#     "foreign": { "owner/repo#12": {"__typename":"Issue","state":"CLOSED","stateReason":"COMPLETED"},
#                  "owner/repo#13": {"__typename":"PullRequest","state":"MERGED","merged":true} } }
# <issue> = {number, title, state, body, labels?, comments?}. A foreign ref missing
# from "foreign" is unreadable, so it holds the card. Keys use the spelling from
# --linked (or as written for a repo that is not linked).
#
# Stdout, one object keyed by issue number:
#   { "40": { "number": 40, "title": "…", "state": "OPEN",
#             "blockers": [32, "owner/api#7"], "openBlockers": [32],
#             "parseable": true, "humanGated": false, "runnable": false, "why": "",
#             "needsYou": false, "needsYouDone": false } }
# Same-repo blockers stay numbers; foreign ones are "owner/repo#N" strings.
#
# `needsYou` — the newest Block comment carries the 🙋 reason tag, or the issue
# has the `needs-you` label: a human-only command is waiting (block-template.md).
# `needsYouDone` — verified by super-board-approval.py: a trusted human replied
# done after a pinned request for the current PR head. Labels are never authority.
# Saved --from payloads stay offline and cannot assert verified human approval.
# A 🙋 card stays humanGated either way — only the resume path moves it.
#
# `runnable` is true only when the line parses, no blocker is still open, AND the
# card is not human-gated. `humanGated` comes from `blocked-by: -`, which means
# "nothing on this board clears it" — credentials, permissions, a product ruling.
# The sweep must never free one of those; only a person can.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=""; LIMIT="200"; ONLY=""; FROM=""; LINKED=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)   REPO="$2"; shift 2 ;;
    --limit)  LIMIT="$2"; shift 2 ;;
    --issues) ONLY="$2"; shift 2 ;;
    --linked) LINKED="$2"; shift 2 ;;  # config.linked_repos, comma-separated
    --from)   FROM="$2"; shift 2 ;;   # a saved payload (shape above); tests use this
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done

# Open issues carry the graph; closed ones are only needed to answer "is this
# blocker still open?", which `openBlockers` derives from the open set itself.
FOREIGN_FIXTURE='{}'
if [ -n "$FROM" ]; then
  RAW=$(cat "$FROM")
  ISSUES=$(printf '%s' "$RAW" | jq 'if type == "array" then . else (.issues // []) end')
  FOREIGN_FIXTURE=$(printf '%s' "$RAW" | jq -c 'if type == "object" then (.foreign // {}) else {} end')
else
  [ -n "$REPO" ] || { echo "--repo <owner/name> is required" >&2; exit 64; }
  # GraphQL rather than `gh issue list`, because the lanes' `blocked-by:` line
  # lives in a COMMENT and the REST list cannot return comments. One query gets
  # bodies and comments together; `gh issue view` per issue would be one REST
  # call each, and a 35-card board burns that budget for no reason.
  PAGES=$(python3 "$HERE/super-board-github-read.py" --kind issues -- api graphql --paginate --slurp -f query='
    query($owner:String!,$repo:String!,$endCursor:String){
      repository(owner:$owner,name:$repo){
        issues(states:OPEN,first:100,after:$endCursor){
          pageInfo{ hasNextPage endCursor }
          nodes{ number title body state labels(first:30){ nodes{ name } } comments(last:8){ nodes{ body } } }
        }
      }
    }' -F owner="${REPO%%/*}" -F repo="${REPO##*/}") || exit $?
  ISSUES=$(printf '%s' "$PAGES" | jq '[ .[].data.repository.issues.nodes[] ]')
fi

# linked_repos as a lower-cased lookup → the spelling the config uses.
LINKED_MAP=$(jq -nc --arg s "$(printf '%s' "$LINKED" | tr -d '\r')" '
  $s | split(",") | map(gsub("^[ \t]+|[ \t]+$"; "")) | map(select(test("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")))
  | map({ key: ascii_downcase, value: . }) | from_entries')

# ---- pass 1: parse every card ------------------------------------------------
PARSED=$(echo "$ISSUES" | jq --arg repo "$REPO" --argjson lmap "$LINKED_MAP" '
  # ---- the parser ---------------------------------------------------------
  # Everything after the LAST "## Blocked by" heading, stopping at the next
  # "## " heading so a following section is never swallowed.
  def section:
    ( . / "\n" ) as $lines
    | [ $lines | to_entries[] | select(.value | test("^##[ \t]+Blocked by[ \t]*$")) | .key ] as $heads
    | if ($heads | length) == 0 then null
      else ( $heads | last ) as $at
        # Take the lines after the heading, then cut at the first line that is
        # itself a heading. Done by index rather than by substituting a sentinel
        # string: a sentinel has to be a value no real line can equal, and the
        # obvious choices are control characters — which is how a NUL byte ended
        # up in this script and made git treat it as a binary file.
        | [ $lines[($at + 1):][] ] as $tail
        | ( [ $tail | to_entries[] | select(.value | test("^##[ \t]")) | .key ] | first ) as $end
        | if $end == null then $tail else $tail[:$end] end
      end;

  # One reference, in any of the three spellings. Owner and name are limited to
  # GitHub'"'"'s own character set: they are later spliced into a GraphQL query.
  def REF: "#[0-9]+|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+|https?://github\\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[0-9]+";

  # "#12" | "o/r#12" | URL  ->  12 (this repo) or "o/r#12" (another repo)
  def norm_ref:
    ( if test("^#[0-9]+$") then { r: null, n: ltrimstr("#") }
      elif test("^[0-9]+$") then { r: null, n: . }
      elif test("^https?://") then capture("github\\.com/(?<r>[^/]+/[^/]+)/(?:issues|pull)/(?<n>[0-9]+)")
      else capture("^(?<r>[^#]+)#(?<n>[0-9]+)$") end ) as $x
    | ($x.n | tonumber) as $n
    | if $x.r == null then $n
      elif $repo != "" and ($x.r | ascii_downcase) == ($repo | ascii_downcase) then $n
      else (($lmap[$x.r | ascii_downcase]) // $x.r) + "#" + ($n | tostring) end;

  # First-seen order, duplicates dropped (#110 and its URL are one blocker).
  def dedupe: reduce .[] as $x ([]; if index([$x]) != null then . else . + [$x] end);

  # Blockers on bullets: "- #32 — The price feed" -> 32, "- o/r#7" -> "o/r#7"
  def refs: [ .[] | select(test("^[ \t]*-[ \t]*(" + REF + ")"))
                  | capture("^[ \t]*-[ \t]*(?<t>" + REF + ")").t | norm_ref ] | dedupe;

  # A BULLET that claims None while naming an issue is the landmine of 2026-08-20.
  # Restricted to bullets on purpose: a blockquote underneath may legitimately
  # explain why a past blocker no longer applies, and naming it there is history,
  # not a dependency. Only the bullet is the machine-read line.
  def none_but_names: any(.[];
        (test("^[ \t]*-[ \t]*[Nn]one")) and (test("#[0-9]+|github\\.com/[^ \t]+/(issues|pull)/[0-9]+")));

  def blank: (map(select(test("[^ \t]"))) | length) == 0;

  # The lanes write `blocked-by: 32, 91` as the last line of a Block comment. The
  # most recent one wins over the issue body: a lane that just parked the card
  # knows why, and the `## Blocked by` in the body may be weeks older.
  #
  # `blocked-by: -` is NOT "no blockers". It means nothing on this board clears
  # it — a credentials, permissions or product-decision block — so the card is
  # parseable AND deliberately not runnable. Only a person moves it.
  def comment_line:
    [ (.comments.nodes // .comments // [])[]
      | (.body // "") | split("\n")[]
      | select(test("^[ \t]*blocked-by:"; "i")) ]
    | last // null;

  # A comment token: a bare number, or a full ref to a (linked) repo.
  def comment_token: test("^([0-9]+|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+|https?://github\\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(issues|pull)/[0-9]+)$");

  def bodies: [ (.comments.nodes // .comments // [])[] | (.body // "") ];
  def label_names: [ (.labels.nodes // .labels // [])[] | (.name // "") ];
  # Index of the newest 🙋 Block comment, or null.
  def needs_at: ( bodies | to_entries
                  | map(select(.value | test("Reason tag:[^\n]*🙋"))) | last | .key ) // null;
  def needs_you: (label_names | index("needs-you") != null) or (needs_at != null);

  [ .[]
    | . as $i
    | ($i.body | section) as $sec
    | ($i | comment_line) as $cl
    | ( if $cl != null then
          ( $cl | capture("blocked-by:[ \t]*(?<v>.*)$"; "i").v | gsub("[ \t]"; "") ) as $v
          | if $v == "-" or $v == "" then
              { parseable: true, blockers: [], human_gated: true, why: "" }
            elif ($v / "," | all(.[]; comment_token)) then
              { parseable: true, blockers: ($v / "," | map(norm_ref) | dedupe), human_gated: false, why: "" }
            else
              { parseable: false, blockers: [], human_gated: false,
                why: ("the `blocked-by:` line reads \"" + $v + "\" — it must be issue numbers alone (owner/repo#N for a linked repo), or `-`") }
            end
        elif $sec == null then
          { parseable: false, blockers: [], human_gated: false,
            why: "no `## Blocked by` section — silence is not the same as None" }
        elif ($sec | blank) then
          { parseable: false, blockers: [], human_gated: false,
            why: "`## Blocked by` section is empty — write `- None.` if nothing blocks it" }
        elif ($sec | none_but_names) then
          { parseable: false, blockers: ($sec | refs), human_gated: false,
            why: "the section says None and then names an issue — a planner reads no blocker, a human reads one" }
        elif (($sec | refs) | length) > 0 then
          { parseable: true, blockers: ($sec | refs), human_gated: false, why: "" }
        elif ($sec | any(.[]; test("^[ \t]*-?[ \t]*[Nn]one"))) then
          { parseable: true, blockers: [], human_gated: false, why: "" }
        else
          { parseable: false, blockers: [], human_gated: false,
            why: "`## Blocked by` section has no `- #N` bullets and no `- None.`" }
        end ) as $p0
    # A foreign blocker in a repo nobody linked is a typo or a missing config
    # line until proven otherwise. Fail safe, and name the repo so it can be fixed.
    | ( [ $p0.blockers[] | strings | select(($lmap[(split("#")[0] | ascii_downcase)]) == null) ] ) as $unlinked
    | ( if $p0.parseable and ($unlinked | length) > 0 then
          $p0 + { parseable: false,
                  why: ("blocker " + ($unlinked | join(", ")) + " is in "
                        + ($unlinked | map(split("#")[0]) | unique | join(", "))
                        + ", which is not in linked_repos — add the repo to the config or fix the ref") }
        else $p0 end ) as $p
    | { number: $i.number, title: $i.title, state: $i.state, p: $p, needsYou: ($i | needs_you) } ]')

# ---- foreign state: one batched query ---------------------------------------
# Only refs on parseable cards matter: an unparseable card is held regardless.
WANT=$(echo "$PARSED" | jq -c '[ .[] | select(.p.parseable) | .p.blockers[] | strings ] | unique')
FOREIGN='{}'
if [ "$WANT" != "[]" ]; then
  if [ -n "$FROM" ]; then
    FOREIGN="$FOREIGN_FIXTURE"
  else
    # r<k>: repository(owner,name){ i<n>: issueOrPullRequest(number:n){…} } — every
    # foreign ref in one query. Owner/name passed REF'"'"'s character-set check above.
    PLAN=$(echo "$WANT" | jq -c '
      map(capture("^(?<repo>[^/]+/[^#]+)#(?<n>[0-9]+)$") | .n |= tonumber)
      | group_by(.repo | ascii_downcase)
      | to_entries | map({ alias: ("r" + (.key | tostring)), repo: .value[0].repo, ns: [ .value[].n ] })')
    QUERY=$(echo "$PLAN" | jq -r '
      "query{ " + (map(.alias + ": repository(owner:\"" + (.repo | split("/")[0]) + "\",name:\""
        + (.repo | split("/")[1]) + "\"){ "
        + (.ns | map("i" + tostring + ": issueOrPullRequest(number:" + tostring
                     + "){ __typename ... on Issue{ state stateReason } ... on PullRequest{ state merged } }") | join(" "))
        + " }") | join(" ")) + " }"')
    RESP=$(python3 "$HERE/super-board-github-read.py" --kind foreign-state -- api graphql -f query="$QUERY") || exit $?
    FOREIGN=$(printf '%s' "$RESP" | jq -c --argjson plan "$PLAN" '
      .data as $d
      | [ $plan[] | . as $r | .ns[]
          | { key: ($r.repo + "#" + tostring), value: ($d[$r.alias][("i" + tostring)] // null) } ]
      | from_entries')
  fi
fi

# ---- pass 2: verdicts ---------------------------------------------------------
APPROVAL_REPO="$REPO"
[ -z "$FROM" ] || APPROVAL_REPO=""
echo "$PARSED" | jq --arg only "$ONLY" --argjson foreign "$FOREIGN" '
  # done | open | abandoned. Anything unexpected — including no answer — is open.
  def foreign_state:
    if . == null then "open"
    elif .__typename == "PullRequest" then
      (if .merged == true then "done" elif .state == "CLOSED" then "abandoned" else "open" end)
    elif .__typename == "Issue" then
      (if .state == "CLOSED" and .stateReason == "COMPLETED" then "done"
       elif .state == "CLOSED" and .stateReason == "NOT_PLANNED" then "abandoned"
       else "open" end)
    else "open" end;

  ( [ .[] | .number ] ) as $open
  | ( if $only == "" then null else ($only / "," | map(tonumber)) end ) as $filter
  | [ .[]
      | . as $c
      | ( [ $c.p.blockers[] | strings | select(($foreign[.] | foreign_state) == "abandoned") ] ) as $gone
      | ( if $c.p.parseable and ($gone | length) > 0 then
            $c.p + { parseable: false,
                     why: ($gone | join(", ")) + " was closed without being done (not planned, or a PR closed unmerged) — a person decides whether this card still needs it; fix `## Blocked by`" }
          else $c.p end ) as $p
      | ( [ $p.blockers[]
            | select(if type == "number" then (. as $b | $open | index($b)) != null
                     else ($foreign[.] | foreign_state) != "done" end) ] ) as $stillOpen
      | { number: $c.number, title: $c.title, state: $c.state,
          blockers: $p.blockers, openBlockers: $stillOpen,
          parseable: $p.parseable,
          humanGated: $p.human_gated,
          runnable: ($p.parseable and ($p.human_gated | not) and (($stillOpen | length) == 0)),
          why: $p.why,
          needsYou: $c.needsYou,
          needsYouDone: false }
    ]
  | ( if $filter == null then . else map(select(.number as $n | $filter | index($n))) end )
  | INDEX(.number | tostring)' | python3 "$(dirname "${BASH_SOURCE[0]}")/super-board-approval.py" --deps --repo "$APPROVAL_REPO"
