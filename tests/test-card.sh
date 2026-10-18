#!/usr/bin/env bash
# Tests super-board-card.sh against a fake gh. No network.
#
# The point of the helper is cost: a card move used to be item-list (~203
# points on a 131-card board) + field-list (101) + item-edit. These tests pin
# the call count — a warm move is ONE mutation, a cold one is two 1-point reads
# plus the mutation — and that a stale option id is recovered, not mis-moved.
set -euo pipefail
cd "$(dirname "$0")"
CARD="$PWD/../scripts/super-board-card.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $1" >&2; exit 1; }
pass=0; ok() { pass=$((pass + 1)); echo "  ✅ $1"; }

mkdir -p "$TMP/bin"
cat > "$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
# Fake gh: logs one line per call, answers by query shape.
q=""; for a in "$@"; do case "$a" in query=*) q="${a#query=}" ;; esac; done
kind=other
case "$q" in
  *mutation*) kind=mutation ;;
  *'field(name:"Status")'*) kind=ids ;;
  *issueOrPullRequest*) kind=card ;;
  *'items(first:100'*) kind=items ;;
esac
{ printf '%s ' "$kind" "$@" | tr '\n' ' '; echo; } >> "$FAKE_LOG"
case "$kind" in
  mutation)
    if [ -f "$FAKE_DIR/fail-mutation-once" ]; then rm -f "$FAKE_DIR/fail-mutation-once"
      echo 'GraphQL: The single select option Id does not belong to the field' >&2; exit 1; fi
    echo '{"data":{"updateProjectV2ItemFieldValue":{"projectV2Item":{"id":"PVTI_232"}}}}' ;;
  ids) echo '{"data":{"repositoryOwner":{"projectV2":{"id":"PVT_1","field":{"id":"PVTSSF_1","options":[{"id":"opt_ready","name":"Ready"},{"id":"opt_qa","name":"QA"}]}}}}}' ;;
  card)
    case "$*" in
      *issue=999*) echo '{"data":{"repository":{"issueOrPullRequest":{"projectItems":{"nodes":[{"id":"PVTI_other","project":{"number":7,"owner":{"login":"acme"}},"fieldValueByName":null}]}}}}}' ;;
      *) echo '{"data":{"repository":{"issueOrPullRequest":{"projectItems":{"nodes":[{"id":"PVTI_other","project":{"number":7,"owner":{"login":"acme"}},"fieldValueByName":null},{"id":"PVTI_232","project":{"number":15,"owner":{"login":"Acme"}},"fieldValueByName":{"name":"Building"}}]}}}}}' ;;
    esac ;;
  items) cat "$FAKE_DIR/items.json" ;;
  *) echo "unexpected gh call: $*" >&2; exit 1 ;;
esac
EOF
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH" FAKE_LOG="$TMP/log" FAKE_DIR="$TMP" SB_CARD_CACHE_DIR="$TMP/cache" \
       SB_GITHUB_HALT_FILE="$TMP/halt.json" SB_GITHUB_RETRY_DELAY=0
card() { "$CARD" --owner acme --number 15 --repo acme/app "$@"; }
calls() { grep -c "^$1 " "$FAKE_LOG" 2>/dev/null || true; }
reset_log() { : > "$FAKE_LOG"; }

# 1 — cold move: ids read + card read + one mutation, with the right option.
reset_log
OUT=$(card move 232 QA)
[ "$OUT" = "moved #232 → QA" ] || fail "cold move output: $OUT"
[ "$(wc -l < "$FAKE_LOG" | tr -d ' ')" = 3 ] || fail "cold move should be 3 calls: $(cat "$FAKE_LOG")"
grep '^mutation' "$FAKE_LOG" | grep -q 'o=opt_qa' || fail "mutation used wrong option: $(cat "$FAKE_LOG")"
grep '^mutation' "$FAKE_LOG" | grep -q 'i=PVTI_232' || fail "picked the other project's item: $(cat "$FAKE_LOG")"
ok "cold move = 2 reads + 1 mutation, this board's item"

# 2 — warm move: ONE call, the mutation.
reset_log
card move 232 Ready >/dev/null
[ "$(wc -l < "$FAKE_LOG" | tr -d ' ')" = 1 ] && [ "$(calls mutation)" = 1 ] || fail "warm move should be 1 mutation: $(cat "$FAKE_LOG")"
ok "warm move = 1 mutation"

# 3 — the cache is shared state, kept out of git.
[ -f "$TMP/cache/board-acme-15.json" ] || fail "cache file missing"
[ "$(cat "$TMP/cache/.gitignore")" = '*' ] || fail "cache dir not self-ignored"
jq -e '.items["232"] == "PVTI_232" and .options.QA == "opt_qa"' "$TMP/cache/board-acme-15.json" >/dev/null || fail "cache content wrong"
ok "cache holds ids, ignores itself"

# 4 — a reminted option id: first mutation fails, ids re-read, retried once.
jq '.options.QA = "opt_stale"' "$TMP/cache/board-acme-15.json" > "$TMP/c" && mv "$TMP/c" "$TMP/cache/board-acme-15.json"
touch "$TMP/fail-mutation-once"; reset_log
OUT=$(card move 232 QA 2>/dev/null) || fail "stale-id move should recover"
[ "$(calls mutation)" = 2 ] && [ "$(calls ids)" = 1 ] || fail "stale recovery calls: $(cat "$FAKE_LOG")"
tail -1 "$FAKE_LOG" | grep -q 'o=opt_qa' || fail "retry still used the stale id"
ok "stale option id → one re-read, one retry"

# 5 — a column the board does not have: re-read once, then exit 3, no mutation.
reset_log; RC=0; card move 232 Shipping >/dev/null 2>&1 || RC=$?
[ "$RC" = 3 ] && [ "$(calls mutation)" = 0 ] || fail "unknown Status should exit 3 without mutating (rc=$RC)"
ok "unknown Status refused"

# 6 — status reads live (never cached) and prints the name.
reset_log
[ "$(card status 232)" = Building ] && [ "$(calls card)" = 1 ] || fail "status read wrong"
ok "status = 1 live read"

# 7 — an issue on another board only: exit 3.
RC=0; card item-id 999 >/dev/null 2>&1 || RC=$?
[ "$RC" = 3 ] || fail "issue off this board should exit 3 (rc=$RC)"
ok "issue not on this board → 3"

# 8 — items: gh item-list shape, the fields the wave planner reads.
cat > "$TMP/items.json" <<'EOF'
[{"data":{"repositoryOwner":{"projectV2":{"items":{"totalCount":3,"pageInfo":{"hasNextPage":true,"endCursor":"c1"},"nodes":[
  {"id":"I1","fieldValueByName":{"name":"Ready"},"content":{"__typename":"Issue","number":12,"title":"Twelve","url":"u12","body":"Problem: x","repository":{"nameWithOwner":"acme/app"},"labels":{"nodes":[{"name":"qa"}]}}},
  {"id":"I2","fieldValueByName":null,"content":{"__typename":"DraftIssue","title":"Draft"}}]}}}}},
 {"data":{"repositoryOwner":{"projectV2":{"items":{"totalCount":3,"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[
  {"id":"I3","fieldValueByName":{"name":"Review"},"content":{"__typename":"PullRequest","number":40,"title":"Forty","url":"u40","repository":{"nameWithOwner":"acme/app"},"labels":{"nodes":[]}}}]}}}}}]
EOF
OUT=$(card items)
echo "$OUT" | jq -e '.totalCount == 3 and (.items | length) == 3' >/dev/null || fail "items count: $OUT"
echo "$OUT" | jq -e '.items[0] == {id:"I1",title:"Twelve",status:"Ready",labels:["qa"],content:{type:"Issue",title:"Twelve",number:12,url:"u12",repository:"acme/app",body:"Problem: x"}}' >/dev/null || fail "issue item shape: $OUT"
echo "$OUT" | jq -e '.items[1] | has("status") | not' >/dev/null || fail "no-status item must omit status: $OUT"
echo "$OUT" | jq -e '.items[2].content.type == "PullRequest" and (.items[2] | has("labels") | not)' >/dev/null || fail "PR item shape: $OUT"
ok "items in gh item-list shape"

# 9 — a short read is refused, never planned on.
jq '.[1].data.repositoryOwner.projectV2.items.nodes = []' "$TMP/items.json" > "$TMP/i2" && mv "$TMP/i2" "$TMP/items.json"
RC=0; card items >/dev/null 2>&1 || RC=$?
[ "$RC" != 0 ] || fail "incomplete board read must fail"
ok "incomplete board read refused"

# ── link: the cross-board pair (acme/web card → acme/api counterpart) ─────
cat > "$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
# Fake gh for `link`: one log line per call, a copy of every body written.
q=""; for a in "$@"; do case "$a" in query=*) q="${a#query=}" ;; esac; done
printf '%s\n' "$*" | tr '\n' ' ' >> "$FAKE_LOG"; echo >> "$FAKE_LOG"
bodyfile() { while [ $# -gt 0 ]; do [ "$1" = --body-file ] && { echo "$2"; return; }; shift; done; }
case "$1 $2" in
  "issue view") cat "$FAKE_DIR/cur.json" ;;
  "issue create") cp "$(bodyfile "$@")" "$FAKE_DIR/created.md"; echo "https://github.com/acme/api/issues/77" ;;
  "issue edit") cp "$(bodyfile "$@")" "$FAKE_DIR/edited.md" ;;
  "project item-add") [ -f "$FAKE_DIR/fail-item-add" ] && exit 1; echo '{"id":"PVTI_new"}' ;;
  "api graphql")
    case "$q" in
      *mutation*) echo '{"data":{"updateProjectV2ItemFieldValue":{"projectV2Item":{"id":"PVTI_new"}}}}' ;;
      *'field(name:"Status")'*) echo '{"data":{"repositoryOwner":{"projectV2":{"id":"PVT_api","field":{"id":"F_api","options":[{"id":"opt_backlog","name":"Backlog"},{"id":"opt_ready","name":"Ready"}]}}}}}' ;;
      *) exit 1 ;;
    esac ;;
  *) echo "unexpected gh call: $*" >&2; exit 1 ;;
esac
EOF
chmod +x "$TMP/bin/gh"
jq -n '{number: 12, title: "Show invoices", comments: [],
        body: "## Problem\nNo invoice list.\n\n## Context\n- **Where:** `/invoices`\n- **Who:** owner\n\n## Fix\nList them.\n\n## Acceptance Criteria\n- [ ] list renders\n\n## Risk\n🟢 Low\n\n## Blocked by\n- None.\n"}' > "$TMP/cur.json"
printf '{"project":{"owner":"acme","number":15},"repo":{"remote":"https://github.com/acme/web"},"linked_repos":["acme/api"]}' > "$TMP/cfg.json"
lnk() { "$CARD" --config "$TMP/cfg.json" link 12 "$@"; }

# 10 — --new blocks: the counterpart is created in acme/api, put on ITS board in
#      Backlog, and the web card gets the Blocked-by ref plus a Linked bullet.
reset_log
OUT=$(lnk --to acme/api --project acme/9 --new blocks --title "✨ [feat] invoices: list endpoint" --why "the endpoint")
echo "$OUT" | jq -e '.created == "acme/api#77" and .blocker == "acme/api#77" and .dependent == "acme/web#12"' >/dev/null || fail "link output: $OUT"
grep -q '^project item-add 9 --owner acme --url https://github.com/acme/api/issues/77' "$FAKE_LOG" || fail "counterpart not added to the other board: $(cat "$FAKE_LOG")"
grep -q 'o=opt_backlog' "$FAKE_LOG" || fail "counterpart not moved to Backlog: $(cat "$FAKE_LOG")"
grep -q -- '- \*\*Linked:\*\* acme/web#12 — waits on this card' "$TMP/created.md" || fail "new body lacks Linked: $(cat "$TMP/created.md")"
python3 - "$TMP/edited.md" <<'PY' || fail "web card body wrong: $(cat "$TMP/edited.md")"
import sys, re
b = open(sys.argv[1], encoding="utf-8").read()
blocked = b.split("## Blocked by")[-1]
assert "- acme/api#77 — the endpoint" in blocked, blocked
assert "None" not in blocked, "a lone - None. must give way to the blocker"
context = b.split("## Context")[1].split("## Fix")[0]
assert "- **Linked:** acme/api#77 — blocks this card" in context, context
assert "\r" not in b
PY
ok "link --new blocks: created, Backlog on the other board, refs both ways"

# 11 — the written ref parses: deps.sh frees the web card once acme/api#77 is done.
jq -n --rawfile b "$TMP/edited.md" '{issues: [{number: 12, title: "web", state: "OPEN", body: $b}],
  foreign: {"acme/api#77": {__typename: "Issue", state: "CLOSED", stateReason: "COMPLETED"}}}' > "$TMP/deps.json"
"$PWD/../scripts/super-board-deps.sh" --from "$TMP/deps.json" --repo acme/web --linked acme/api \
  | jq -e '.["12"].blockers == ["acme/api#77"] and .["12"].runnable == true' >/dev/null || fail "deps.sh cannot read what link wrote"
ok "link output round-trips through deps.sh"

# 12 — --new waits: the NEW card carries the Blocked-by ref; the web card only Linked.
reset_log
lnk --to acme/api --project acme/9 --new waits --title "🧪 [test] invoices: contract test" >/dev/null 2>&1 || fail "link --new waits failed"
grep -q -- '^- acme/web#12' "$TMP/created.md" || fail "the waiting card needs the blocker ref: $(cat "$TMP/created.md")"
grep -q -- '- acme/api#77' "$TMP/edited.md" && fail "the blocker card must not wait on its dependent"
grep -q -- '- \*\*Linked:\*\* acme/api#77 — waits on this card' "$TMP/edited.md" || fail "web card lacks Linked"
ok "link --new waits: ref on the new card"

# 13 — refusals before any write: repo not in linked_repos, missing --project.
reset_log; RC=0; lnk --to evil/other --project acme/9 --new blocks --title t >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && ! grep -qE '^(issue create|issue edit|project item-add)' "$FAKE_LOG" || fail "unlinked repo must refuse before writing (rc=$RC)"
RC=0; ERR=$(lnk --to acme/api --new blocks --title t 2>&1 >/dev/null) || RC=$?
[ "$RC" = 64 ] && echo "$ERR" | grep -q -- '--project' || fail "missing --project must say so (rc=$RC): $ERR"
ok "link refuses unlinked repos and a missing --project"

# 14 — a failed board add after the issue exists says what is left; the web card is untouched.
reset_log; rm -f "$TMP/edited.md"; touch "$TMP/fail-item-add"; RC=0
ERR=$(lnk --to acme/api --project acme/9 --new blocks --title t 2>&1 >/dev/null) || RC=$?
rm -f "$TMP/fail-item-add"
[ "$RC" = 69 ] && echo "$ERR" | grep -q 'created acme/api#77' && [ ! -f "$TMP/edited.md" ] || fail "partial link must report and stop (rc=$RC): $ERR"
ok "partial link reports what was written"

echo "PASS: $pass card-helper checks"
