#!/usr/bin/env bash
# Tests skills/super-collect/scripts/super-collect-file.sh. Deterministic, no
# network: `gh` is a stub on PATH, and the real super-qa / super-review filers
# run underneath it. Covers dry-run default, per-source fingerprint shapes,
# repo-wide dedupe, recurrence, holding-column resolution (never Ready), routing,
# extra labels (needs-triage), and --adopt.
#
#   bash tests/test-collect-file.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/skills/super-collect/scripts/super-collect-file.sh"
WORK="$(mktemp -d)"
export SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$WORK/halt.json"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  ✅ %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  ❌ %s\n     expected: %s\n     actual:   %s\n' "$1" "$2" "$3"; }
is()  { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }
has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "contains: $3" "$2" ;; esac; }
lacks() { case "$2" in *"$3"*) bad "$1" "no: $3" "found it" ;; *) ok "$1" ;; esac; }

cat > "$WORK/config.json" <<'JSON'
{"version":1,"project":{"owner":"acme","title":"Board","number":7},
 "variant":"full","repo":{"path":".","remote":"https://github.com/acme/app.git"}}
JSON

cat > "$WORK/bug.md" <<'MD'
## Problem
Checkout 500s on an empty cart.
## Context
- **Where:** Checkout, `/checkout`
  - a. Empty the cart
  - b. Open `/checkout`
- **Who:** any shopper
## Evidence
<details><summary>12 rows · Sentry 4411 · 37 events</summary>

| Row | Value |
|---|---|
| Error + stack | `TypeError` · `checkout.ts:12` |
| Request / trace ID | `req_9` |
| Sentry | 4411 |
| PostHog replay | n/a — no replay linked |
| Logs | `500 cart=empty` |
| Screenshots | n/a — server error, no UI state |
| HAR / API sample | `GET /checkout → 500` |
| Env + release | prod · `abc1234` |
| First / last seen | Sep 1 · Sep 14 |
| Users affected | 37 events in 14 days |
| Steps | 1. empty cart 2. open checkout |
| Expected / actual | empty-cart message / HTTP 500 |

</details>
## Fix
Guard the empty cart in CheckoutService.
## Acceptance Criteria
- [ ] Empty cart renders the message, no 500.
## Risk
🟢 **Low** · one guard clause.
MD

cat > "$WORK/fix.md" <<'MD'
## Problem
Merge gate keeps bouncing cards on stale lockfiles.
## Context
- **Where:** merge gate, `verify_commands`
## Evidence
Runs 2026-09-01, 09-08, 09-15: #12, #19, #27 bounced on lockfile drift.
## Fix
verify_commands run without npm ci; install before typecheck.
## Acceptance Criteria
- [ ] verify_commands install before typecheck.
## Risk
🟡 **Medium** · every card's verify step changes.
MD

# gh stub. Env switches:
#   STUB_HITS        — JSON array the issue-list dedupe query sees ([{number,state,body}])
#   STATUS_OPTS — Status option names on the board
cat > "$WORK/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
if [ "${STUB_PAUSE_AFTER:-}" = "$1 ${2:-}" ]; then
  printf '{"reason":"another worker paused during discovery"}\n' > "$SB_GITHUB_HALT_FILE"
fi
case "$1 ${2:-}" in
  "repo view")     case "$*" in *nameWithOwner*) echo "acme/app";; *) echo "acme";; esac ;;
  "issue list")
    if [ "${STUB_ROUTER_OUTAGE:-}" = 1 ]; then case "$*" in *"--state all"*) echo "HTTP 503" >&2; exit 1;; esac; fi
    jq_expr=""; prev=""
    for a in "$@"; do [ "$prev" = "--jq" ] && jq_expr="$a"; prev="$a"; done
    hits="${STUB_HITS:-[]}"
    case "$*" in *"--state open"*) hits=$(echo "$hits" | jq '[.[] | select(.state == "OPEN")]');; esac
    if [ -n "$jq_expr" ]; then echo "$hits" | jq -r "$jq_expr"; else echo "$hits"; fi ;;
  "api graphql")
    if [ "${STUB_ROUTER_OUTAGE:-}" = 1 ]; then echo "HTTP 503" >&2; exit 1; fi
    if [ "${STUB_PAUSE_AFTER:-}" = "issue list" ]; then printf '{"reason":"paused during history"}\n' > "$SB_GITHUB_HALT_FILE"; fi
    hits="${STUB_HITS:-[]}"
    echo "$hits" | jq '[{data:{repository:{issues:{totalCount:length,pageInfo:{hasNextPage:false,endCursor:null},nodes:.}}}}]' ;;
  "issue view")    echo "https://github.com/acme/app/issues/55" ;;
  "api repos/"*)   echo "${STUB_COMMENTS:-[[]]}" ;;
  "issue create")
    prev=""
    for a in "$@"; do [ "$prev" = "--body-file" ] && [ -r "$a" ] && cat "$a" > "$BODY_LOG"; prev="$a"; done
    echo "https://github.com/acme/app/issues/412" ;;
  "project item-list")
    if [ -n "${STUB_BOARD:-}" ]; then echo "$STUB_BOARD"; else echo '{"totalCount":0,"items":[]}'; fi ;;
  "project list")  echo '{"projects":[{"number":7,"title":"Board"}]}' ;;
  "project item-add") echo "PVTI_stub" ;;
  "project view")  echo "PVT_stub" ;;
  "project field-list")
    OPTS=""
    for encoded in ${STATUS_OPTS:-Backlog Ready Done}; do
      name=$(echo "$encoded" | tr '_' ' ')
      OPTS="${OPTS}{\"id\":\"opt_${encoded}\",\"name\":\"${name}\"},"
    done
    echo "{\"fields\":[{\"id\":\"FLD_status\",\"name\":\"Status\",\"options\":[${OPTS%,}]}]}" ;;
  *) exit 0 ;;
esac
STUB
chmod +x "$WORK/gh"
export PATH="$WORK:$PATH" GH_LOG="$WORK/gh.log" BODY_LOG="$WORK/body.filed"
export SB_COLLECT_STATE_DIR="$WORK/collect-state"
export SUPER_BOARD_BIN="$REPO_ROOT/scripts"

run() { [ "${KEEP_COLLECT_STATE:-0}" = 1 ] || rm -rf "$SB_COLLECT_STATE_DIR"; : > "$GH_LOG"; : > "$BODY_LOG"; "$SCRIPT" --config "$WORK/config.json" "$@" 2>"$WORK/err"; }
BUG=(--type bug --source sentry --title "Checkout 500 on empty cart" --body-file "$WORK/bug.md" --fingerprint "err|sentry|4411")
FIX=(--type fix --source prs --title "Merge gate bounces on lockfile drift" --body-file "$WORK/fix.md" --fingerprint "prs|merge-gate|lockfile-drift")

echo "── arg validation"
is "type is an enum"        64 "$(run --type chore --source sentry --title T --body-file "$WORK/bug.md" --fingerprint f >/dev/null; echo $?)"
is "source is an enum"      64 "$(run --type bug --source vibes --title T --body-file "$WORK/bug.md" --fingerprint f >/dev/null; echo $?)"
is "old intake source gone" 64 "$(run --type bug --source intake --title T --body-file "$WORK/bug.md" --fingerprint "err|sentry|1" >/dev/null; echo $?)"
is "fp must match source"   64 "$(run --type bug --source posthog --title T --body-file "$WORK/bug.md" --fingerprint "err|sentry|1" >/dev/null; echo $?)"
is "gap only for posthog"  64 "$(run --type feature --source sentry --title T --body-file "$WORK/fix.md" --fingerprint "gap|checkout" >/dev/null; echo $?)"
is "custom fp must start custom|" 64 "$(run --type bug --source custom --title T --body-file "$WORK/bug.md" --fingerprint "linear|ENG-1" >/dev/null; echo $?)"
is "fp prefix needs a key"  64 "$(run --type bug --source sentry --title T --body-file "$WORK/bug.md" --fingerprint "err|sentry|" >/dev/null; echo $?)"
for pair in "sentry err|sentry|9" "posthog posthog|exception|abc123" "posthog gap|checkout" "github github|55" "prs prs|merge-gate|drift" "architecture arch|order-intake|shallow" "custom custom|linear|ENG-42"; do
  src=${pair%% *}; fp=${pair#* }
  OUT=$(run --type bug --source "$src" --title T --body-file "$WORK/bug.md" --fingerprint "$fp")
  has "fingerprint shape accepted for $src" "$OUT" "would-file|bug|T|Backlog"
done
is "fingerprint required"   64 "$(run --type bug --source sentry --title T --body-file "$WORK/bug.md" >/dev/null; echo $?)"
sed '/## Evidence/,/## Fix/d' "$WORK/fix.md" > "$WORK/fix-no-evidence.md"
is "fix needs Evidence"     66 "$(run --type fix --source prs --title T --body-file "$WORK/fix-no-evidence.md" --fingerprint "prs|x|y" >/dev/null; echo $?)"
sed 's/^- \[ \] /- /' "$WORK/fix.md" > "$WORK/fix-no-checklist.md"
is "fix needs an AC checklist" 66 "$(run --type fix --source prs --title T --body-file "$WORK/fix-no-checklist.md" --fingerprint "prs|x|y" >/dev/null; echo $?)"

echo "── dry-run is the default"
OUT=$(run "${BUG[@]}")
is    "plans, does not file"       "would-file|bug|Checkout 500 on empty cart|Backlog" "$OUT"
lacks "no issue create on dry-run" "$(cat "$GH_LOG")" "issue create"

echo "── holding column, never Ready"
OUT=$(STATUS_OPTS="Todo Ready Done" run "${BUG[@]}")
has "falls back to another holding column" "$OUT" "|Todo"
is  "no holding column refuses" 65 "$(STATUS_OPTS="Ready QA Done" run "${BUG[@]}" >/dev/null; echo $?)"

echo "── dedupe"
OUT=$(STUB_HITS='[{"number":301,"state":"OPEN","body":"x err|sentry|4411 y"}]' run "${BUG[@]}")
is  "open hit is a duplicate" "duplicate|#301|bug|Checkout 500 on empty cart" "$OUT"
OUT=$(STUB_HITS='[{"number":301,"state":"OPEN","body":"err|sentry|4411"}]' run "${BUG[@]}" --yes)
is  "--yes on duplicate returns existing" "301" "$OUT"
has "comments instead of filing" "$(cat "$GH_LOG")" "issue comment 301"
lacks "no duplicate card" "$(cat "$GH_LOG")" "issue create"
OUT=$(STUB_HITS='[{"number":88,"state":"CLOSED","body":"prs|merge-gate|lockfile-drift"}]' run "${FIX[@]}")
has "closed-only hit is a recurrence" "$OUT" "recurrence|#88"

echo "── filing a bug through super-qa-file-bug.sh"
OUT=$(run "${BUG[@]}" --yes)
is  "returns the new issue" "412" "$OUT"
has "routes via the QA filer (source:qa label)" "$(cat "$GH_LOG")" "source:qa"
has "lands in Backlog"        "$(cat "$GH_LOG")" "opt_Backlog"
lacks "never Ready"           "$(cat "$GH_LOG")" "opt_Ready"
has "stamps the fingerprint"  "$(cat "$BODY_LOG")" "super-collect-fingerprint: err|sentry|4411"
has "labels source:collect"   "$(cat "$GH_LOG")" "source:collect"

echo "── filing a prs fix (weak-body bypass, own section check)"
OUT=$(STUB_HITS='[{"number":88,"state":"CLOSED","body":"prs|merge-gate|lockfile-drift"}]' run "${FIX[@]}" --yes)
is  "returns the new issue" "412" "$OUT"
has "files as tech-debt"      "$(cat "$GH_LOG")" "tech-debt"
has "names the recurrence"    "$(cat "$BODY_LOG")" "#88"
lacks "never Ready"           "$(cat "$GH_LOG")" "opt_Ready"

echo "── refactor with criteria still lands in Backlog (filer would pick Ready)"
OUT=$(run --type refactor --source architecture --title "Split OrderIntake" --body-file "$WORK/fix.md" --fingerprint "arch|order-intake|shallow" --yes)
has "routes via the review filer" "$(cat "$GH_LOG")" "source:review"
has "lands in Backlog"            "$(cat "$GH_LOG")" "opt_Backlog"
lacks "never Ready"               "$(cat "$GH_LOG")" "opt_Ready"

has "labels collect:architecture" "$(cat "$GH_LOG")" "collect:architecture"

echo "── posthog bug with extra labels (needs-triage, ux)"
OUT=$(run --type bug --source posthog --title "Dead clicks on /checkout" --body-file "$WORK/bug.md" --fingerprint "posthog|dead_click|1a2b3c" --label needs-triage --label ux --yes)
is  "returns the new issue"   "412" "$OUT"
has "stamps posthog fingerprint" "$(cat "$BODY_LOG")" "super-collect-fingerprint: posthog|dead_click|1a2b3c"
has "labels needs-triage"     "$(cat "$GH_LOG")" "--add-label needs-triage"
has "labels ux"               "$(cat "$GH_LOG")" "--add-label ux"
has "labels collect:posthog"  "$(cat "$GH_LOG")" "collect:posthog"
lacks "never Ready"           "$(cat "$GH_LOG")" "opt_Ready"

echo "── one card per problem across sources (sb-signature)"
SIG=abcdef012345
SIGBODY="x
<!-- sb-signature: ${SIG} -->"
HIT_OPEN=$(jq -cn --arg b "$SIGBODY" '[{number:301,state:"OPEN",body:$b}]')
is "--signature must be 12 hex" 64 "$(run "${BUG[@]}" --signature nothex >/dev/null; echo $?)"
OUT=$(STUB_HITS="$HIT_OPEN" run --type bug --source loki --title "Checkout 500" --body-file "$WORK/bug.md" --fingerprint "loki|gw|aa11bb22cc33" --signature "$SIG")
is "open signature hit plans evidence, not a card" "evidence|#301|loki|Checkout 500" "$OUT"
OUT=$(STUB_HITS="$HIT_OPEN" run --type bug --source loki --title "Checkout 500" --body-file "$WORK/bug.md" --fingerprint "loki|gw|aa11bb22cc33" --signature "$SIG" --count 7 --first-seen 2026-10-17 --last-seen 2026-10-18 --yes)
is    "evidence returns the existing issue" "301" "$OUT"
has   "evidence comment on the open issue" "$(cat "$GH_LOG")" "issue comment 301"
has   "evidence comment header"           "$(cat "$GH_LOG")" "[collect] [evidence]"
has   "evidence comment marker"           "$(cat "$GH_LOG")" "<!-- sb-evidence: loki:loki|gw|aa11bb22cc33 -->"
has   "evidence comment counts"           "$(cat "$GH_LOG")" "7 events · first 2026-10-17 · last 2026-10-18"
lacks "no second card"                    "$(cat "$GH_LOG")" "issue create"
NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# comment(<body>, <created_at>) in the REST shape the comments read validates.
comment() { jq -cn --arg b "$1" --arg t "$2" '{id:1,body:$b,user:{login:"bot"},created_at:$t,updated_at:$t}'; }
RECENT="[[$(comment '<!-- sb-evidence: loki:loki|gw|other -->' "$NOW_ISO")]]"
RC=0; OUT=$(STUB_COMMENTS="$RECENT" STUB_HITS="$HIT_OPEN" run --type bug --source loki --title "Checkout 500" --body-file "$WORK/bug.md" --fingerprint "loki|gw|aa11bb22cc33" --signature "$SIG" --yes) || RC=$?
is    "recent same-source evidence: exit 0" 0 "$RC"
is    "recent same-source evidence: issue returned" "301" "$OUT"
has   "the comments were read" "$(cat "$GH_LOG")" "api repos/acme/app/issues/301/comments"
lacks "at most one evidence comment per source per 24h" "$(cat "$GH_LOG")" "issue comment"
OLD="[[$(comment '<!-- sb-evidence: loki:loki|gw|other -->' 2026-01-01T00:00:00Z),$(comment '<!-- sb-evidence: sentry:err|sentry|1 -->' "$NOW_ISO")]]"
OUT=$(STUB_COMMENTS="$OLD" STUB_HITS="$HIT_OPEN" run --type bug --source loki --title "Checkout 500" --body-file "$WORK/bug.md" --fingerprint "loki|gw|aa11bb22cc33" --signature "$SIG" --yes)
has   "older or other-source evidence does not block" "$(cat "$GH_LOG")" "issue comment 301"
HIT_CLOSED=$(jq -cn --arg b "$SIGBODY" '[{number:88,state:"CLOSED",body:$b}]')
OUT=$(STUB_HITS="$HIT_CLOSED" run --type bug --source loki --title "Checkout 500" --body-file "$WORK/bug.md" --fingerprint "loki|gw|aa11bb22cc33" --signature "$SIG")
is  "closed signature hit is a recurrence" "recurrence|#88|bug|Checkout 500|Backlog" "$OUT"
OUT=$(STUB_HITS="$HIT_CLOSED" run --type bug --source loki --title "Checkout 500" --body-file "$WORK/bug.md" --fingerprint "loki|gw|aa11bb22cc33" --signature "$SIG" --yes)
is  "closed signature hit files new" "412" "$OUT"
has "names the closed issue"     "$(cat "$BODY_LOG")" "Seen before in #88"
has "stamps the signature"       "$(cat "$BODY_LOG")" "<!-- sb-signature: ${SIG} -->"
has "keeps the fingerprint stamp" "$(cat "$BODY_LOG")" "super-collect-fingerprint: loki|gw|aa11bb22cc33"

echo "── trace evidence"
TID=0b7b05232a90b467cc6111b73ccb480a
is "--trace-id must be 32 hex" 64 "$(run "${BUG[@]}" --trace-id 123 >/dev/null; echo $?)"
OUT=$(run "${BUG[@]}" --trace-id "$TID" --yes)
is  "files with a trace id" "412" "$OUT"
has "no collect.tempo: just the trace id in Evidence" "$(cat "$BODY_LOG")" "- **Trace:** \`${TID}\`"
EV_BLOCK=$(awk '/^## Evidence/{on=1;next} /^## /{on=0} on' "$BODY_LOG")
has "the Trace line sits inside Evidence" "$EV_BLOCK" "**Trace:**"

echo "── triage: thin collector bodies get needs-triage"
sed 's/^- \[ \] verify_commands install before typecheck\./- [ ] works/' "$WORK/fix.md" > "$WORK/fix-vague.md"
OUT=$(run --type fix --source prs --title "Vague" --body-file "$WORK/fix-vague.md" --fingerprint "prs|x|vague" --yes)
is  "vague body still files" "412" "$OUT"
has "vague body labelled needs-triage" "$(cat "$GH_LOG")" "--add-label needs-triage"
OUT=$(run "${BUG[@]}" --yes)
lacks "a ready body gets no needs-triage" "$(cat "$GH_LOG")" "needs-triage"
jq '.triage.enabled = false' "$WORK/config.json" > "$WORK/config-notriage.json"
OUT=$("$SCRIPT" --config "$WORK/config-notriage.json" --type fix --source prs --title "Vague" --body-file "$WORK/fix-vague.md" --fingerprint "prs|x|vague2" --yes 2>/dev/null)
lacks "triage.enabled=false skips the label" "$(cat "$GH_LOG" | tail -20)" "needs-triage"

echo "── adopt a user-filed issue"
is  "adopt dry-run" "would-adopt|#55|feature|Backlog" "$(run --adopt 55 --type feature)"
OUT=$(run --adopt 55 --type feature --yes)
is  "adopt returns the issue" "55" "$OUT"
lacks "adopt files nothing new" "$(cat "$GH_LOG")" "issue create"
has "adopt places in Backlog"  "$(cat "$GH_LOG")" "opt_Backlog"

echo "── duplicate and adopt stop when the repository is paused"
for path in duplicate adopt; do
  printf '{"reason":"another worker paused GitHub writes"}\n' > "$SB_GITHUB_HALT_FILE"
  RC=0
  if [ "$path" = duplicate ]; then
    STUB_HITS='[{"number":301,"state":"OPEN","body":"err|sentry|4411"}]' run "${BUG[@]}" --yes >/dev/null || RC=$?
  else
    run --adopt 55 --type feature --yes >/dev/null || RC=$?
  fi
  is "$path reports the paused run" 79 "$RC"
  WRITES=$(awk '/^(issue (create|comment|edit)|label create|project (item-add|item-edit)) /' "$GH_LOG")
  is "$path makes no GitHub mutations while paused" "" "$WRITES"
  rm -f "$SB_GITHUB_HALT_FILE"
done

echo "── a pause discovered during a read stops the next write"
for path in duplicate adopt; do
  RC=0
  if [ "$path" = duplicate ]; then
    OUT=$(STUB_PAUSE_AFTER="issue list" STUB_HITS='[{"number":301,"state":"OPEN","body":"err|sentry|4411"}]' run "${BUG[@]}" --yes) || RC=$?
    EXPECTED=""
  else
    OUT=$(STUB_PAUSE_AFTER="issue view" run --adopt 55 --type feature --yes) || RC=$?
    EXPECTED=""
  fi
  is "$path propagates a pause after its read" 79 "$RC"
  is "$path preserves the existing issue identity after a pause" "$EXPECTED" "$OUT"
  WRITES=$(awk '/^(issue (create|comment|edit)|label create|project (item-add|item-edit)) /' "$GH_LOG")
  is "$path makes no mutations after the discovery pause" "" "$WRITES"
  rm -f "$SB_GITHUB_HALT_FILE"
done

echo "── pauses during adoption stop labeling and placement"
RC=0
OUT=$(STUB_PAUSE_AFTER="label create" run --adopt 55 --type feature --yes) || RC=$?
is "adoption pauses between label writes" 79 "$RC"
is "mid-label pause preserves adopted issue" 55 "$OUT"
COUNT=$(awk '/^(issue (create|comment|edit)|label create|project (item-add|item-edit)) / {n++} END {print n+0}' "$GH_LOG")
is "only the first pre-pause label mutation happened" 1 "$COUNT"
rm -f "$SB_GITHUB_HALT_FILE"
RC=0
OUT=$(STUB_PAUSE_AFTER="project view" run --adopt 55 --type feature --yes) || RC=$?
is "placement pause stays exit 79" 79 "$RC"
is "placement pause preserves adopted issue" 55 "$OUT"
lacks "placement pause prevents a column write" "$(cat "$GH_LOG")" "project item-edit"
rm -f "$SB_GITHUB_HALT_FILE"

echo
echo "── required collector duplicate read cannot fail open"
RC=0
OUT=$(STUB_ROUTER_OUTAGE=1 run "${BUG[@]}" --yes) || RC=$?
is "all-state duplicate outage halts collector" 79 "$RC"
WRITES=$(awk '/^(issue (create|comment|edit)|label create|project (item-add|item-edit)) /' "$GH_LOG")
is "duplicate outage makes no mutations" "" "$WRITES"
rm -f "$SB_GITHUB_HALT_FILE"

echo "passed: $PASS  failed: $FAIL"
[ "$FAIL" -eq 0 ]
