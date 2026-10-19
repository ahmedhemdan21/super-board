#!/usr/bin/env bash
# Tests super-board-split.py (hub boards: hub.md → Split) against a stateful fake gh
# (tests/fixtures/split-gh-stub.py). No network.
#
# The rules it pins: a one-sided report moves with its labels and its board card; a two-sided
# one becomes two attached sub-issues where the later side waits on the earlier; nothing is
# ever filed twice (rerun, resume after a failure); split.confirm only proposes until a
# trusted human says done; --dry-run writes nothing; a parent closes only when every child
# was completed.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$PWD"
SPLIT="$HERE/../scripts/super-board-split.py"
DEPS="$HERE/../scripts/super-board-deps.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $1" >&2; exit 1; }
pass=0; ok() { pass=$((pass + 1)); echo "  ✅ $1"; }

mkdir -p "$TMP/bin" "$TMP/d"
cp "$HERE/fixtures/split-gh-stub.py" "$TMP/bin/gh"; chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH" FAKE_DIR="$TMP" FAKE_LOG="$TMP/log" SB_CARD_CACHE_DIR="$TMP/cache" \
       SB_GITHUB_HALT_FILE="$TMP/halt.json" SB_GITHUB_RETRY_DELAY=0 SB_GH_GUARD_STATE_FILE="$TMP/budget"

cfg() {  # $1 = file, $2 = confirm
  jq -n --argjson c "$2" '{project:{owner:"o",number:3},
    hub:{remote:"https://github.com/o/hub",path:"."},
    members:{backend:{repo:{path:"backend",remote:"https://github.com/o/api"},base_branch:"dev"},
             frontend:{repo:{path:"frontend",remote:"https://github.com/o/web.git"},base_branch:"dev"}},
    split:{default_order:["backend","frontend"],confirm:$c}}' > "$1"
}
cfg "$TMP/cfg.json" false
cfg "$TMP/cfg-confirm.json" true

BODY='## Problem
Invoices cannot be exported.

## Context
- **Where:** `/invoices`
  - a. Open the invoices page
- **Who:** owner

## Fix
Add a CSV export.

## Acceptance Criteria
- [ ] GET /invoices.csv returns every invoice row

## Risk
🟢 **Low** · a new endpoint only.

## Blocked by
- None.
'
printf '%s' "$BODY" > "$TMP/d/backend.md"
printf '%s' "$BODY" > "$TMP/d/frontend.md"
sed '/^## Risk/,/^## Blocked by/{/^## Blocked by/!d}' "$TMP/d/backend.md" > "$TMP/d/norisk.md"
grep -q '^## Risk' "$TMP/d/norisk.md" && fail "fixture: norisk.md still has Risk"
jq -n '{sides:["frontend","backend"], why:"needs an endpoint and a button",
        children:{backend:{title:"✨ [feat] invoices: CSV endpoint", body_file:"backend.md"},
                  frontend:{title:"✨ [feat] invoices: export button", body_file:"frontend.md"}}}' > "$TMP/d/two.json"
jq '.children.frontend.body_file = "norisk.md"' "$TMP/d/two.json" > "$TMP/d/bad.json"
jq -n '{sides:["backend"], why:"server-side 500 only"}' > "$TMP/d/one.json"

hub_issue() {  # $1 = number, $2 = status or "", $3 = labels json
  jq --argjson n "$1" --arg s "$2" --argjson l "$3" '.issues["o/hub#\($n)"] = {repo:"o/hub", number:$n, id:"I_hub_\($n)",
    title:"Invoice export", body:"Shoppers want invoices as CSV.", state:"OPEN", labels:$l, comments:[], subIssues:[],
    item:(if $s == "" then null else {id:"PVTI_hub_\($n)", status:$s} end)}' "$TMP/state.json" > "$TMP/s" && mv "$TMP/s" "$TMP/state.json"
}
jq -n '{issues:{}, next:{}, seq:0, fail:{}, perms:{eric:"write", rando:"read", halfway:{"o/hub":"admin","o/api":"write","o/web":"read"}},
        project:{owner:"o", number:3}, options:{Backlog:"opt_b", Ready:"opt_r", Building:"opt_bd"},
        labels:{"o/hub":["feature","bug","needs-triage","area-billing"], "o/api":["feature","bug","needs-triage"], "o/web":["feature"]}}' > "$TMP/state.json"

S() { jq -r "$1" "$TMP/state.json" | tr -d '\r'; }  # jq.exe prints CRLF on Windows
reply() {  # $1 = hub issue, $2 = author, $3 = body
  jq --arg k "o/hub#$1" --arg a "$2" --arg b "$3" '.seq += 1 | .issues[$k].comments += [{id: (500 + .seq), body: $b, author: $a}]' \
    "$TMP/state.json" > "$TMP/s" && mv "$TMP/s" "$TMP/state.json"
}
digest8() { S ".issues[\"o/hub#$1\"].comments | map(.body) | map(select(test(\"sb-split-proposal\"))) | last" \
  | sed -n 's/.*sb-split-proposal: \([0-9a-f]\{8\}\).*/\1/p'; }
writes() { grep -c '^W ' "$TMP/log" 2>/dev/null || true; }
reset_log() { : > "$TMP/log"; }
split() { python3 "$SPLIT" "$@"; }

# 1 — plan is read-only and hands triage the issue and the members.
hub_issue 4 Ready '["feature"]'
reset_log
OUT=$(split plan --config "$TMP/cfg.json" --issue 4)
echo "$OUT" | jq -e '.issue.ref == "o/hub#4" and .issue.column == "Ready" and (.members | map(.repo)) == ["o/api","o/web"]
  and .split.default_order == ["backend","frontend"] and .split.confirm == false' >/dev/null || fail "plan output: $OUT"
[ "$(writes)" = 0 ] || fail "plan wrote: $(cat "$TMP/log")"
ok "plan: read-only, issue + members"

# 2 — one side: transfer, labels re-applied (missing ones reported), card kept, comment.
hub_issue 5 Ready '["bug","needs-triage","area-billing"]'
reset_log
OUT=$(split apply --config "$TMP/cfg.json" --issue o/hub#5 --decision "$TMP/d/one.json")
NEW=$(echo "$OUT" | jq -r .issue)
echo "$OUT" | jq -e '.status == "split" and .from == "o/hub#5" and .labelsApplied == ["bug","needs-triage"] and .labelsSkipped == ["area-billing"]' >/dev/null || fail "one-side output: $OUT"
grep -q '^W issue transfer 5 o/api --repo o/hub' "$TMP/log" || fail "no transfer: $(cat "$TMP/log")"
K=$(echo "$NEW" | tr '[:upper:]' '[:lower:]')
[ "$(S ".issues[\"$K\"].labels | join(\",\")")" = "bug,needs-triage" ] || fail "labels not re-applied: $(S ".issues[\"$K\"].labels")"
[ "$(S ".issues[\"$K\"].item.status")" = Ready ] || fail "card lost its column"
grep -q '^W project item-add' "$TMP/log" && fail "item still existed; must not be re-added"
C=$(S ".issues[\"$K\"].comments[-1].body")
echo "$C" | head -1 | grep -qE '^\[triage\] \[report\] ✅ split → backend$' || fail "comment header: $C"
echo "$C" | grep -q 'labels missing in o/api: area-billing' && echo "$C" | grep -q 'server-side 500 only' || fail "comment body: $C"
[ "$(S ".issues[\"$K\"].comments | length")" = 1 ] || fail "one comment expected, edited in place"
ok "one side: transferred, labels re-applied, card kept, comment"

# 2b — a transfer that loses the card: added back, in its old column.
hub_issue 6 Building '["bug"]'
jq '.issues["o/hub#6"].drop_item = true' "$TMP/state.json" > "$TMP/s" && mv "$TMP/s" "$TMP/state.json"
reset_log
NEW=$(split apply --config "$TMP/cfg.json" --issue 6 --decision "$TMP/d/one.json" | jq -r .issue | tr '[:upper:]' '[:lower:]')
grep -q '^W project item-add 3 --owner o' "$TMP/log" && [ "$(S ".issues[\"$NEW\"].item.status")" = Building ] || fail "lost card not restored: $(cat "$TMP/log")"
reset_log
split apply --config "$TMP/cfg.json" --issue 6 --decision "$TMP/d/one.json" | jq -e '.status == "already-split"' >/dev/null || fail "one-side rerun"
[ "$(writes)" = 0 ] || fail "one-side rerun wrote: $(cat "$TMP/log")"
ok "one side: lost card re-added; rerun writes nothing"

# 3 — two sides: two children in default order, attached, Blocked-by chain, parent labelled.
hub_issue 10 Ready '["feature","needs-triage"]'
reset_log
OUT=$(split apply --config "$TMP/cfg.json" --issue 10 --decision "$TMP/d/two.json")
BE=$(echo "$OUT" | jq -r .children.backend); FE=$(echo "$OUT" | jq -r .children.frontend)
[ "$(grep -c '^W issue create' "$TMP/log")" = 2 ] || fail "two creates expected: $(cat "$TMP/log")"
grep '^W issue create' "$TMP/log" | head -1 | grep -q -- '--repo o/api' || fail "backend must be filed first (default_order)"
[ "$(grep -c '^W api graphql.*addSubIssue' "$TMP/log")" = 2 ] || fail "addSubIssue x2 expected"
[ "$(S '.issues["o/hub#10"].subIssues | length')" = 2 ] || fail "children not attached"
FK=$(echo "$FE" | tr '[:upper:]' '[:lower:]'); BK=$(echo "$BE" | tr '[:upper:]' '[:lower:]')
FB=$(S ".issues[\"$FK\"].body"); BB=$(S ".issues[\"$BK\"].body")
echo "$FB" | sed -n '/^## Blocked by/,$p' | grep -qx -- "- $BE — the backend side lands first" || fail "frontend must wait on backend: $FB"
echo "$FB" | sed -n '/^## Blocked by/,$p' | grep -q 'None' && fail "- None. must give way to the blocker"
echo "$BB" | sed -n '/^## Blocked by/,$p' | grep -qx -- '- None.' || fail "backend blocks on nothing: $BB"
[ "$(S ".issues[\"$BK\"].labels | join(\",\")")" = feature ] || fail "labels copied minus needs-triage: $(S ".issues[\"$BK\"].labels")"
[ "$(S ".issues[\"$BK\"].item.status")" = Ready ] && [ "$(S ".issues[\"$FK\"].item.status")" = Ready ] || fail "children not in the parent's column"
S '.issues["o/hub#10"].labels | index("parent") != null' | grep -q true || fail "parent label missing"
[ "$(S '.issues["o/hub#10"].item.status')" = Backlog ] || fail "parent must leave the lanes (Backlog)"
C=$(S '.issues["o/hub#10"].comments[-1].body')
echo "$C" | head -1 | grep -q '^\[triage\] \[report\] ✅ split → backend, frontend' && echo "$C" | grep -q "$BE" && echo "$C" | grep -q "$FE" \
  && echo "$C" | grep -q '"done": true' || fail "parent comment: $C"
jq -n --arg b "$FB" '{issues:[{number:1,title:"fe",state:"OPEN",body:$b}], foreign:{}}' > "$TMP/deps.json"
"$DEPS" --from "$TMP/deps.json" --repo o/web --linked o/api | jq -e --arg be "$BE" '.["1"].parseable and .["1"].blockers == [$be] and (.["1"].runnable | not)' >/dev/null \
  || fail "deps.sh cannot read the child's Blocked by"
ok "two sides: created in order, attached x2, frontend waits on backend, parent labelled"

# 4 — idempotent: rerun creates and writes nothing.
reset_log
split apply --config "$TMP/cfg.json" --issue 10 --decision "$TMP/d/two.json" | jq -e '.status == "already-split"' >/dev/null || fail "rerun status"
[ "$(writes)" = 0 ] || fail "rerun wrote: $(cat "$TMP/log")"
ok "rerun on a split parent writes nothing"

# 5 — resume: the frontend create fails once; the rerun files only the frontend.
hub_issue 11 Ready '["feature"]'
jq '.fail["create:o/web"] = 1' "$TMP/state.json" > "$TMP/s" && mv "$TMP/s" "$TMP/state.json"
reset_log; RC=0
ERR=$(split apply --config "$TMP/cfg.json" --issue 11 --decision "$TMP/d/two.json" 2>&1 >/dev/null) || RC=$?
[ "$RC" = 69 ] && echo "$ERR" | grep -q 'written: create the o/api issue' || fail "partial failure must exit 69 and say what was written (rc=$RC): $ERR"
S '.issues["o/hub#11"].comments[-1].body' | grep -q 'sb-split:' || fail "progress marker missing"
reset_log
split apply --config "$TMP/cfg.json" --issue 11 --decision "$TMP/d/two.json" | jq -e '.status == "split"' >/dev/null || fail "resume failed"
[ "$(grep -c '^W issue create' "$TMP/log")" = 1 ] && grep '^W issue create' "$TMP/log" | grep -q -- '--repo o/web' || fail "resume must file only the frontend: $(cat "$TMP/log")"
[ "$(S '[.issues[] | select(.repo == "o/api" and .title == "✨ [feat] invoices: CSV endpoint")] | length')" = 2 ] || fail "backend filed twice overall"
[ "$(S '.issues["o/hub#11"].subIssues | length')" = 2 ] && [ "$(S '.issues["o/hub#11"].comments | length')" = 1 ] || fail "resume: attach / single comment"
ok "partial failure resumes from the marker; nothing filed twice"

# 6 — confirm: a 🙋 proposal only; an untrusted done is ignored; --approved performs.
hub_issue 12 Ready '["feature"]'
reset_log
split apply --config "$TMP/cfg-confirm.json" --issue 12 --decision "$TMP/d/two.json" | jq -e '.status == "awaiting-approval"' >/dev/null || fail "confirm status"
[ "$(writes)" = 1 ] && grep -q '^W issue comment 12' "$TMP/log" || fail "confirm must only comment: $(cat "$TMP/log")"
P=$(S '.issues["o/hub#12"].comments[-1].body')
echo "$P" | head -1 | grep -q '^\[triage\] \[question\] 🙋 approve split → backend, frontend' && echo "$P" | grep -q 'export button (waits on backend)' || fail "proposal: $P"
reset_log
split apply --config "$TMP/cfg-confirm.json" --issue 12 --decision "$TMP/d/two.json" | jq -e '.proposal == "already posted"' >/dev/null || fail "proposal re-posted"
[ "$(writes)" = 0 ] || fail "second awaiting run wrote"
D8=$(echo "$P" | sed -n 's/.*sb-split-proposal: \([0-9a-f]\{8\}\).*/\1/p')
echo "$P" | grep -q "reply exactly \`done $D8\`" || fail "proposal must say what to reply: $P"
reply 12 rando "done $D8"
split apply --config "$TMP/cfg-confirm.json" --issue 12 --decision "$TMP/d/two.json" | jq -e '.status == "awaiting-approval"' >/dev/null || fail "read-only user approved"
reset_log
split apply --config "$TMP/cfg-confirm.json" --issue 12 --decision "$TMP/d/two.json" --approved | jq -e '.status == "split" and .column == "Backlog"' >/dev/null || fail "--approved must split"
[ "$(grep -c '^W issue create' "$TMP/log")" = 2 ] || fail "--approved creates"
S '.issues["o/hub#12"].comments[] | select(.body | test("sb-split:")) | .body' | grep -q '"approved_by": "--approved"' || fail "approval not recorded in the marker"
ok "confirm: proposal once, untrusted done ignored, --approved splits into Backlog"

# 6b — `done <digest8>` from a trusted human approves; a bare `done` does not; a changed
#      decision needs a new proposal.
hub_issue 13 Ready '["feature"]'
split apply --config "$TMP/cfg-confirm.json" --issue 13 --decision "$TMP/d/two.json" >/dev/null
reply 13 eric "Done."
jq '.why = "changed my mind"' "$TMP/d/two.json" > "$TMP/d/two-b.json"
reset_log
split apply --config "$TMP/cfg-confirm.json" --issue 13 --decision "$TMP/d/two-b.json" | jq -e '.status == "awaiting-approval" and .proposal == "posted"' >/dev/null || fail "a changed decision must be proposed again"
reply 13 eric "done"
split apply --config "$TMP/cfg-confirm.json" --issue 13 --decision "$TMP/d/two-b.json" | jq -e '.status == "awaiting-approval"' >/dev/null || fail "a bare done must not approve"
reply 13 eric "done $(digest8 13)"
split apply --config "$TMP/cfg-confirm.json" --issue 13 --decision "$TMP/d/two-b.json" | jq -e '.status == "split"' >/dev/null || fail "trusted done <digest8> must approve"
ok "done <digest8> approves the current proposal only; bare done does not"

# 6c — `done <digest8>` from someone with write on the hub but not on a member repo: refused.
hub_issue 18 Ready '["feature"]'
split apply --config "$TMP/cfg-confirm.json" --issue 18 --decision "$TMP/d/two.json" >/dev/null
reply 18 halfway "done $(digest8 18)"
reset_log; ERR=$(split apply --config "$TMP/cfg-confirm.json" --issue 18 --decision "$TMP/d/two.json" 2>&1 >/dev/null) || true
split apply --config "$TMP/cfg-confirm.json" --issue 18 --decision "$TMP/d/two.json" 2>/dev/null | jq -e '.status == "awaiting-approval"' >/dev/null || fail "member-repo permission must be required"
echo "$ERR" | grep -q "approval by 'halfway' ignored" && [ "$(writes)" = 0 ] || fail "halfway approval: $ERR"
ok "approver needs write on the hub and every member written to"

# 6d — markers by anyone else are ignored: a forged finished split, a forged approval record.
hub_issue 30 Ready '["feature"]'
FORGED='[triage] [report] ✅ split
<!-- sb-split: {"v":1,"sides":["backend","frontend"],"children":{"backend":"o/api#1"},"done":true,"approved_by":"eric"} -->'
reply 30 mallory "$FORGED"
reset_log; ERR=$(split apply --config "$TMP/cfg-confirm.json" --issue 30 --decision "$TMP/d/two.json" 2>&1 >"$TMP/out") || fail "forged marker run failed: $ERR"
jq -e '.status == "awaiting-approval"' "$TMP/out" >/dev/null || fail "a forged marker must not pass the gate: $(cat "$TMP/out")"
echo "$ERR" | grep -q "ignored an sb-split marker written by 'mallory'" || fail "forged marker not reported: $ERR"
grep '^W ' "$TMP/log" | grep -qv '^W issue comment 30' && fail "forged marker caused writes: $(cat "$TMP/log")"
ok "forged marker by another user ignored"

# 6e — a bot marker whose children entry names an issue this split did not file: refused.
hub_issue 31 Ready '["feature"]'
jq '.issues["o/api#50"] = {repo:"o/api", number:50, id:"I_o_api_50", title:"unrelated", body:"## Problem\nx", state:"OPEN",
     labels:[], comments:[], subIssues:[], item:null, author:"mallory"}' "$TMP/state.json" > "$TMP/s" && mv "$TMP/s" "$TMP/state.json"
reply 31 bot '[triage] [report] ⏳ splitting
<!-- sb-split: {"v":1,"sides":["backend","frontend"],"children":{"backend":"o/api#50"},"done":false,"approved_by":"confirm-off"} -->'
reset_log; RC=0
ERR=$(split apply --config "$TMP/cfg.json" --issue 31 --decision "$TMP/d/two.json" 2>&1 >/dev/null) || RC=$?
[ "$RC" = 3 ] && echo "$ERR" | grep -q 'o/api#50 was not filed by this split' && [ "$(writes)" = 0 ] || fail "forged child must be refused (rc=$RC): $ERR"
ok "recorded child without backlink / bot author refused, nothing written"

# 7 — dry-run: prints every write, writes nothing.
hub_issue 14 Ready '["feature"]'
reset_log
OUT=$(split apply --config "$TMP/cfg.json" --issue 14 --decision "$TMP/d/two.json" --dry-run)
[ "$(writes)" = 0 ] || fail "dry-run wrote: $(cat "$TMP/log")"
echo "$OUT" | jq -e '.status == "dry-run" and ([.writes[].command | select(startswith("gh issue create"))] | length) == 2
  and ([.writes[].what | select(test("attach"))] | length) == 2 and ([.writes[].what | select(test("label o/hub#14: parent"))] | length) == 1' >/dev/null \
  || fail "dry-run plan: $OUT"
[ "$(S '.issues["o/hub#14"].comments | length')" = 0 ] || fail "dry-run commented"
reset_log
hub_issue 15 Ready '["bug"]'
split apply --config "$TMP/cfg.json" --issue 15 --decision "$TMP/d/one.json" --dry-run | jq -e '[.writes[].what | select(test("transfer"))] | length == 1' >/dev/null || fail "one-side dry-run"
[ "$(writes)" = 0 ] || fail "one-side dry-run wrote"
ok "dry-run lists the writes and makes none"

# 8 — a child body missing a ticket section is refused before any write.
hub_issue 16 Ready '["feature"]'
reset_log; RC=0
ERR=$(split apply --config "$TMP/cfg.json" --issue 16 --decision "$TMP/d/bad.json" 2>&1 >/dev/null) || RC=$?
[ "$RC" = 3 ] && echo "$ERR" | grep -q 'frontend: missing Risk' && [ "$(writes)" = 0 ] || fail "bad body must be refused (rc=$RC): $ERR"
jq '.sides = ["mobile"]' "$TMP/d/one.json" > "$TMP/d/unknown.json"; RC=0
split apply --config "$TMP/cfg.json" --issue 16 --decision "$TMP/d/unknown.json" >/dev/null 2>&1 || RC=$?
[ "$RC" = 3 ] && [ "$(writes)" = 0 ] || fail "unknown member must be refused (rc=$RC)"
ok "bad child body / unknown member refused, nothing written"

# 9 — close-parents: closes only when every child completed; NOT_PLANNED asks once.
jq 'reduce .issues["o/hub#10"].subIssues[] as $k (.; .issues[$k] |= (.state = "CLOSED" | .stateReason = "COMPLETED"))' "$TMP/state.json" > "$TMP/s" && mv "$TMP/s" "$TMP/state.json"
jq '.issues[.issues["o/hub#11"].subIssues[0]] |= (.state = "CLOSED" | .stateReason = "COMPLETED")
  | .issues[.issues["o/hub#11"].subIssues[1]] |= (.state = "CLOSED" | .stateReason = "NOT_PLANNED")' "$TMP/state.json" > "$TMP/s" && mv "$TMP/s" "$TMP/state.json"
jq '.issues[.issues["o/hub#12"].subIssues[0]] |= (.state = "CLOSED" | .stateReason = "COMPLETED")' "$TMP/state.json" > "$TMP/s" && mv "$TMP/s" "$TMP/state.json"
# hub#32: a dropped child, and someone else's needs-human marker that must not silence the ask.
hub_issue 32 Ready '["feature"]'
jq '.issues["o/web#60"] = {repo:"o/web", number:60, id:"I_o_web_60", title:"fe", body:"x", state:"CLOSED", stateReason:"NOT_PLANNED",
     labels:[], comments:[], subIssues:[], item:null} | .issues["o/hub#32"].subIssues = ["o/web#60"]' "$TMP/state.json" > "$TMP/s" && mv "$TMP/s" "$TMP/state.json"
reply 32 mallory "nothing to see <!-- sb-parent-needs-human -->"
reset_log
split close-parents --config "$TMP/cfg.json" --dry-run | jq -e '.closed == ["o/hub#10"]' >/dev/null || fail "close-parents dry-run"
[ "$(writes)" = 0 ] || fail "close-parents dry-run wrote"
OUT=$(split close-parents --config "$TMP/cfg.json")
echo "$OUT" | jq -e '.closed == ["o/hub#10"] and .needsHuman == ["o/hub#11","o/hub#32"] and (.open | index("o/hub#12")) != null' >/dev/null || fail "close-parents: $OUT"
[ "$(S '.issues["o/hub#10"].state')/$(S '.issues["o/hub#10"].stateReason')" = CLOSED/COMPLETED ] || fail "parent 10 not closed completed"
[ "$(S '.issues["o/hub#11"].state')" = OPEN ] && [ "$(S '.issues["o/hub#12"].state')" = OPEN ] || fail "only all-completed parents close"
S '.issues["o/hub#10"].comments[-1].body' | head -1 | grep -q '^\[orchestrator\] \[report\] ✅' || fail "close comment"
S '.issues["o/hub#11"].comments[-1].body' | grep -q 'sb-parent-needs-human' || fail "needs-human comment"
S '.issues["o/hub#32"].comments[-1] | "\(.author)|\(.body)"' | grep -q '^bot|\[orchestrator\] \[question\]' || fail "a non-bot needs-human marker must not suppress the ask"
reset_log
split close-parents --config "$TMP/cfg.json" | jq -e '.needsHuman == ["o/hub#11","o/hub#32"] and .closed == []' >/dev/null || fail "close-parents rerun"
[ "$(writes)" = 0 ] || fail "needs-human must be said once: $(cat "$TMP/log")"
ok "close-parents: all COMPLETED closes; NOT_PLANNED asks once; only its own marker silences it"

# 10 — writes pass the guard: a halted run writes nothing.
hub_issue 17 Ready '["feature"]'
echo '{"reason":"test"}' > "$TMP/halt.json"; reset_log; RC=0
split apply --config "$TMP/cfg.json" --issue 17 --decision "$TMP/d/two.json" >/dev/null 2>&1 || RC=$?
rm -f "$TMP/halt.json"
[ "$RC" = 79 ] && [ "$(writes)" = 0 ] || fail "halted run must exit 79 without writing (rc=$RC)"
ok "halt file stops it before any write"

echo "PASS: $pass split checks"
