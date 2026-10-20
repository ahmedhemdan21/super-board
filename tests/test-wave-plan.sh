#!/usr/bin/env bash
# Tests super-board-wave-plan.sh against fixtures. No gh calls, no network —
# the dependency graph is injected with --deps.
#
# The contract these tests pin down changed on 2026-08-20. A wave used to be
# "one card per column, then fill to max_workers". It is now "every Ready card
# the dependency graph says is free, plus everything already in flight", with
# max_workers demoted to an optional throttle. Several scenarios below exist
# because the old contract shipped a board where five cards sat in Blocked long
# after their blockers had closed.
set -euo pipefail
cd "$(dirname "$0")"
PLAN="../scripts/super-board-wave-plan.sh"
CFG=fixtures/wave-config.json
ITEMS=fixtures/wave-items.json
DEPS=fixtures/wave-deps.json

fail() { echo "FAIL: $1" >&2; exit 1; }
plan() { "$PLAN" --config "$1" --items "$ITEMS" --deps "$DEPS"; }

# Uncapped is the default shape, so every scenario runs without a cap unless it
# is specifically testing the throttle.
NOCAP=$(jq 'del(.max_workers)' "$CFG")
OUT=$(plan <(echo "$NOCAP"))

# 1 — width follows the graph, not a knob. Free Ready cards are #12 alone:
#     #11 is assigned, #14 waits on open #12, #15 is unreadable, the draft is not
#     an issue. In flight: Review #10 and QA #13. Total 3.
echo "$OUT" | jq -e '.cards | length == 3' >/dev/null || fail "expected 3 cards, got: $OUT"
echo "$OUT" | jq -e '[.cards[].number] | sort == [10,12,13]' >/dev/null || fail "wrong cards: $OUT"

# 2 — downstream-first: in-flight cards lead, so work already begun finishes
#     before new work starts.
echo "$OUT" | jq -e '[.cards[].status] | index("Ready") > index("Review")' >/dev/null \
  || fail "in-flight cards must precede Ready cards: $OUT"

# 3 — a Ready card with an OPEN blocker is not dispatched. This is the whole
#     point: #14 would previously have been picked as backlog fill, hit its own
#     preflight, and parked in Blocked.
echo "$OUT" | jq -e '[.cards[].number] | index(14) == null' >/dev/null \
  || fail "#14 waits on open #12 and must not be dispatched: $OUT"

# 4 — an unreadable dependency line is fail-safe: never dispatched, always flagged.
echo "$OUT" | jq -e '[.cards[].number] | index(15) == null' >/dev/null \
  || fail "#15 has an unreadable Blocked-by line and must not be dispatched"
echo "$OUT" | jq -e '[.flag[].number] == [15]' >/dev/null \
  || fail "#15 must be flagged so a human can fix the line, got: $(echo "$OUT" | jq -c .flag)"
echo "$OUT" | jq -e '.flag[0].why | test("None")' >/dev/null \
  || fail "the flag must carry the reason, not just the number"

# 5 — the sweep: a Blocked card whose blockers have all closed comes back.
#     #16 waits on #9, which is closed. #17 waits on #12, which is open.
echo "$OUT" | jq -e '[.sweep[].number] == [16]' >/dev/null \
  || fail "expected only #16 swept, got: $(echo "$OUT" | jq -c .sweep)"
echo "$OUT" | jq -e '.sweep[0].clearedBy == [9]' >/dev/null \
  || fail "the sweep must name what cleared the card, for the comment it will post"

# 6 — a swept card is NOT also dispatched in the same plan. The orchestrator moves
#     it to Ready first; dispatching it straight from Blocked would skip that move
#     and leave the board lying about where the card is.
echo "$OUT" | jq -e '[.cards[].number] | index(16) == null' >/dev/null \
  || fail "#16 must be swept, not dispatched from Blocked"

# 7 — max_workers still throttles when set. It is a safety valve now, not the
#     wave-sizing mechanism.
OUT7=$(plan <(jq '.max_workers = 1' "$CFG"))
echo "$OUT7" | jq -e '(.cards | length) == 1 and .cards[0].number == 10' >/dev/null \
  || fail "max_workers=1 should keep only the leading in-flight card, got: $OUT7"

# 8 — the throttle does not suppress the sweep. A capped wave still frees cards,
#     because sweeping is a board correction and costs no worker.
echo "$OUT7" | jq -e '[.sweep[].number] == [16]' >/dev/null \
  || fail "a capped wave must still sweep, got: $(echo "$OUT7" | jq -c .sweep)"

# 9 — assignee is the cross-machine mutex and still wins over everything.
echo "$OUT" | jq -e '[.cards[].number] | index(11) == null' >/dev/null \
  || fail "#11 is assigned and must never be dispatched"

# 10 — reviews are no longer gated behind human_approves_merge. The merge race is
#      guarded where it happens (the merge lock plus the freshness gate inside the
#      wave workflow), so a second Review card is free to run here.
OUT10=$("$PLAN" --config <(echo "$NOCAP") --items fixtures/wave-items-review-heavy.json --deps "$DEPS")
echo "$OUT10" | jq -e '[.cards[] | select(.status == "Review")] | length >= 2' >/dev/null \
  || fail "reviews should run in parallel now, got: $OUT10"

# 11 — variant was removed in v3.0.0. A config still saying "qa-only" is refused
#      (exit 65) until onboard upgrades it: reading it as a build board would start
#      building on a board meant only for testing.
RC=0; "$PLAN" --config <(echo "$NOCAP" | jq '.variant = "qa-only"') --items "$ITEMS" --deps "$DEPS" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "a legacy qa-only config should exit 65, got $RC"

# 12 — an invalid variant fails loudly (exit 65); an absent one is the normal case.
RC=0; "$PLAN" --config <(jq '.variant = "fulll"' "$CFG") --items "$ITEMS" --deps "$DEPS" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "invalid variant should exit 65, got $RC"
"$PLAN" --config <(echo "$NOCAP" | jq 'del(.variant)') --items "$ITEMS" --deps "$DEPS" >/dev/null \
  || fail "a config with no variant must plan normally"

# 13 — empty board → empty everything (run-workflow.md's done condition depends
#      on this shape).
OUT13=$("$PLAN" --config <(echo "$NOCAP") --items <(echo '{"items":[]}') --deps "$DEPS")
echo "$OUT13" | jq -e '.cards == [] and .sweep == [] and .flag == [] and .stranded == []' >/dev/null \
  || fail "empty board should yield empty cards/sweep/flag/stranded, got: $OUT13"

# 14 — a card the graph has never heard of is not treated as free. An issue beyond
#      the fetch limit, or closed out from under the board, must not be dispatched
#      on the strength of a missing entry.
OUT14=$("$PLAN" --config <(echo "$NOCAP") --items "$ITEMS" --deps <(echo '{}'))
echo "$OUT14" | jq -e '[.cards[] | select(.status == "Ready")] | length == 0' >/dev/null \
  || fail "an empty graph must yield no Ready picks, got: $OUT14"

# 15 — a graph that cannot be derived is a hard failure, not an empty graph.
#      Without --deps and without config.repo.remote there is nothing to derive from.
RC=0; "$PLAN" --config <(jq 'del(.repo)' "$CFG") --items "$ITEMS" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 66 ] || fail "a missing repo remote should exit 66, got $RC"

# 16 — a Building card with no claim is stranded: a stopped wave left it there and
#      no lane selects from Building. A claimed one (#19) has a live worker.
echo "$OUT" | jq -e '[.stranded[].number] == [18]' >/dev/null \
  || fail "expected only #18 stranded, got: $(echo "$OUT" | jq -c .stranded)"
echo "$OUT" | jq -e '[.cards[].number] | index(18) == null' >/dev/null \
  || fail "#18 must be moved to Ready first, not dispatched from Building"

# 17 — label routing. A Ready card labelled `qa` skips the Builder (lane qa); `bug`,
#      `feature` and no label are built first (lane build). QA/Review cards keep their
#      column's lane. Labels come from the item (`labels`) or the content, any case.
LBL_ITEMS=$(jq -n '{items: [
  {status:"Ready", labels:["QA"], content:{type:"Issue", number:12, title:"qa card", assignees:[]}},
  {status:"Ready", labels:["bug"], content:{type:"Issue", number:10, title:"bug card", assignees:[]}},
  {status:"Ready", content:{type:"Issue", number:13, title:"no label", assignees:[], labels:[{name:"feature"}]}},
  {status:"Ready", content:{type:"Issue", number:9, title:"bare", assignees:[]}},
  {status:"QA", labels:["qa"], content:{type:"Issue", number:11, title:"in qa", assignees:[]}}]}')
LBL_DEPS=$(jq -n '[9,10,12,13] | map({(tostring): {runnable:true, parseable:true, blockers:[]}}) | add')
# Routing only: these bodiless cards would be held by triage (scenario 21).
OUT17=$("$PLAN" --config <(echo "$NOCAP" | jq '.triage.enabled = false') --items <(echo "$LBL_ITEMS") --deps <(echo "$LBL_DEPS"))
lane() { echo "$OUT17" | jq -r --argjson n "$1" '.cards[] | select(.number == $n) | .lane'; }
[ "$(lane 12)" = qa ]    || fail "a qa-labelled Ready card must skip Building (lane qa), got: $OUT17"
[ "$(lane 10)" = build ] || fail "a bug card is built first, got $(lane 10)"
[ "$(lane 13)" = build ] || fail "a feature card (label on content) is built first, got $(lane 13)"
[ "$(lane 9)" = build ]  || fail "no label = Building, got $(lane 9)"
[ "$(lane 11)" = qa ]    || fail "a QA-column card keeps lane qa, got $(lane 11)"
echo "$OUT17" | jq -e '.cards[] | select(.number == 12) | .labels == ["qa"]' >/dev/null \
  || fail "labels must be passed through lower-cased, got: $OUT17"

# 18 — resume: a 🙋 Blocked card whose human step is done (#20) is reported for
#      the move back to Review; one still waiting (#21) is not. Neither is swept
#      to Ready or dispatched from Blocked.
echo "$OUT" | jq -e '[.resume[].number] == [20]' >/dev/null \
  || fail "expected only #20 resumed, got: $(echo "$OUT" | jq -c .resume)"
echo "$OUT" | jq -e '[.sweep[].number, .cards[].number] | (index(20) == null and index(21) == null)' >/dev/null \
  || fail "🙋 cards must not be swept to Ready or dispatched"

# 19 — a moved head while Blocked goes to request refresh, never approved resume.
OUT19=$("$PLAN" --config <(echo "$NOCAP") --items "$ITEMS" --deps <(jq '.["21"].approvalRefresh=true | .["21"].approvalWhy="PR head changed"' "$DEPS"))
echo "$OUT19" | jq -e '[.refreshApproval[].number] == [21]' >/dev/null || fail "stale approval must get a fresh request"
echo "$OUT19" | jq -e '[.resume[].number, .cards[].number, .sweep[].number] | index(21) == null' >/dev/null || fail "refresh is not permission to merge or build"

# 20 — cross-board sweep: a Blocked card whose only blockers live on a linked
#      repo's board is swept once they are all done (closed COMPLETED / merged),
#      named in clearedBy as owner/repo#N. One still open stays; one closed not
#      planned is flagged for a person, never swept. The graph comes from
#      super-board-deps.sh over the cross-repo fixture, so both ends are real.
XDEPS=$(../scripts/super-board-deps.sh --from fixtures/deps-cross.json --repo acme/web --linked acme/api)
XITEMS=$(jq -n '{items: [
  {status:"Blocked", content:{type:"Issue", number:101, title:"foreign done", assignees:[]}},
  {status:"Blocked", content:{type:"Issue", number:102, title:"foreign open", assignees:[]}},
  {status:"Blocked", content:{type:"Issue", number:104, title:"foreign not planned", assignees:[]}},
  {status:"Blocked", content:{type:"Issue", number:113, title:"foreign done, other case", assignees:[]}}]}')
OUT20=$("$PLAN" --config <(echo "$NOCAP") --items <(echo "$XITEMS") --deps <(echo "$XDEPS"))
echo "$OUT20" | jq -e '[.sweep[].number] == [101,113]' >/dev/null   || fail "cards whose foreign blockers are done must be swept, got: $(echo "$OUT20" | jq -c .sweep)"
echo "$OUT20" | jq -e '.sweep[0].clearedBy == ["acme/api#5"]' >/dev/null   || fail "clearedBy must name the foreign blocker, got: $(echo "$OUT20" | jq -c .sweep)"
echo "$OUT20" | jq -e '[.flag[].number] == [104]' >/dev/null   || fail "a not-planned foreign blocker must be flagged, got: $(echo "$OUT20" | jq -c .flag)"

# 21 — triage before Ready. A free Ready card whose body is too vague (no Fix, an AC
#      that says "works") is NOT dispatched and is reported in `triage` with what is
#      missing; a well-written one goes out. The graph's unreadable card (#15) stays in
#      `flag` only. A crashing checker holds every candidate (fail safe).
echo "$OUT" | jq -e '.triage == []' >/dev/null || fail "well-written fixture cards need no triage, got: $(echo "$OUT" | jq -c .triage)"
VAGUE=$(jq '(.items[] | select(.content.number == 12) | .content.body) |= (sub("## Fix\nApply the coupon before tax.\n\n"; "") | sub("- \\[ \\] Total[^\n]*"; "- [ ] works"))' "$ITEMS")
OUT21=$("$PLAN" --config <(echo "$NOCAP") --items <(echo "$VAGUE") --deps "$DEPS")
echo "$OUT21" | jq -e '[.cards[].number] | index(12) == null' >/dev/null || fail "a vague Ready card must not be dispatched: $OUT21"
echo "$OUT21" | jq -e '[.triage[].number] == [12]' >/dev/null || fail "the vague card must be reported for triage, got: $(echo "$OUT21" | jq -c .triage)"
echo "$OUT21" | jq -e '.triage[0].missing | (index("Fix") != null and index("checkable acceptance criterion") != null)' >/dev/null \
  || fail "triage must name what is missing, got: $(echo "$OUT21" | jq -c .triage)"
echo "$OUT21" | jq -e '[.flag[].number] == [15] and ([.triage[].number] | index(15) == null)' >/dev/null || fail "#15 belongs to flag only"
OUT21B=$("$PLAN" --config <(echo "$NOCAP" | jq '.triage.enabled = false') --items <(echo "$VAGUE") --deps "$DEPS")
echo "$OUT21B" | jq -e '[.cards[].number] | index(12) != null' >/dev/null || fail "triage.enabled=false must skip the check"
STUBBIN=$(mktemp -d); trap 'rm -rf "$STUBBIN"' EXIT
cp ../scripts/super-board-wave-plan.sh ../scripts/super-board-verification-ready.py ../scripts/super-board-github-read.py "$STUBBIN/"
printf 'import sys\nsys.exit(1)\n' > "$STUBBIN/super-board-card-ready.py"
OUT21C=$("$STUBBIN/super-board-wave-plan.sh" --config <(echo "$NOCAP") --items "$ITEMS" --deps "$DEPS" 2>/dev/null)
echo "$OUT21C" | jq -e '[.cards[] | select(.status == "Ready")] == [] and ([.triage[].number] | index(12) != null)' >/dev/null \
  || fail "a crashing card-ready must hold every Ready card, got: $OUT21C"

# 22 — card keys. The Project holds home acme/api#7 and acme/web#7 (plus web #8 in
#      QA and web #9 in Blocked). The graph is acme/api's. Home #7 is planned on its
#      own verdict; web cards are reported in `foreign`, never planned, swept,
#      stranded or matched to api's #7 — and the same when acme/web is linked.
#      A Done foreign card is not reported. Both ends are real: deps.sh builds the graph.
GOODBODY=$(jq -r '.items[] | select(.content.number == 12) | .content.body' "$ITEMS" | tr -d '\r')
KCFG=$(echo "$NOCAP" | jq '.repo.remote = "https://github.com/Acme/API.git"')
KITEMS=$(jq -n --arg b "$GOODBODY" '{items: [
  {status:"Ready",   content:{type:"Issue", number:7, title:"api seven", repository:"acme/api", assignees:[], body:$b}},
  {status:"Ready",   content:{type:"Issue", number:7, title:"web seven", repository:"acme/web", assignees:[], body:$b}},
  {status:"QA",      content:{type:"Issue", number:8, title:"web in qa", repository:"acme/web", assignees:[]}},
  {status:"Blocked", content:{type:"Issue", number:9, title:"web blocked", repository:"acme/web", assignees:[]}},
  {status:"Building",content:{type:"Issue", number:10, title:"web building", repository:"acme/web", assignees:[]}},
  {status:"Done",    content:{type:"Issue", number:11, title:"web done", repository:"acme/web", assignees:[]}}]}')
KDEPS=$(jq -n --arg b "$GOODBODY" '[{number:7, title:"api seven", state:"OPEN", body:$b},
          {number:9, title:"api nine", state:"OPEN", body:"## Blocked by\n- None."}]' > "$STUBBIN/kdeps.json"
        ../scripts/super-board-deps.sh --from "$STUBBIN/kdeps.json" --repo acme/api)
for LINK in '[]' '["acme/web"]'; do
  OUT22=$("$PLAN" --config <(echo "$KCFG" | jq --argjson l "$LINK" '.linked_repos = $l') --items <(echo "$KITEMS") --deps <(echo "$KDEPS"))
  echo "$OUT22" | jq -e '.cards == [{number:7, repository:"acme/api", status:"Ready", title:"api seven", lane:"build", labels:[]}]' >/dev/null \
    || fail "only home #7 is planned (linked=$LINK), got: $(echo "$OUT22" | jq -c .cards)"
  echo "$OUT22" | jq -e '[.foreign[] | "\(.repository)#\(.number):\(.status)"] == ["acme/web#7:Ready","acme/web#8:QA","acme/web#9:Blocked","acme/web#10:Building"]' >/dev/null \
    || fail "web cards must be reported as foreign (linked=$LINK), got: $(echo "$OUT22" | jq -c .foreign)"
  echo "$OUT22" | jq -e '.foreign | all(.why | startswith("card from acme/web: not a member of this board"))' >/dev/null \
    || fail "foreign cards need the reason (linked=$LINK): $(echo "$OUT22" | jq -c .foreign)"
  echo "$OUT22" | jq -e '.sweep == [] and .stranded == [] and .flag == [] and .triage == [] and .resume == []' >/dev/null \
    || fail "web #9 must not be swept on api #9's verdict, web #10 not stranded (linked=$LINK): $OUT22"
done
echo "$OUT22" | jq -e '.foreign[0].why | test("linked repo")' >/dev/null || fail "a linked repo's card says why it is still not planned"
# Without the home card on the board, web #7 never borrows api #7's free verdict.
OUT22B=$("$PLAN" --config <(echo "$KCFG") --items <(echo "$KITEMS" | jq '.items |= map(select(.content.repository != "acme/api"))') --deps <(echo "$KDEPS"))
echo "$OUT22B" | jq -e '.cards == [] and (.foreign | length) == 4' >/dev/null || fail "web #7 must not be planned on api #7's verdict: $OUT22B"

# 23 — hub board (hub.md → Config). A members config plans backend (acme/api) and
#      frontend (acme/web) each as home over one board read; every card carries
#      `member`. acme/other and the hub repo's own card are `foreign`. Frontend #7
#      waits on open acme/api#3 and stays; frontend #8 waits on closed acme/api#2 and
#      is swept: members trust each other without linked_repos. The graph is derived
#      for real (deps.sh --from per repo); a stub only records the --linked it got.
#      P4: each card carries its member's resolved config, checkout, base branch and
#      deploy flag; `parent` cards (hub or member) are never planned, only reported.
HUB="$STUBBIN/hub"; mkdir -p "$HUB/.claude/super-board/configs" "$HUB/backend" "$HUB/frontend"
cp ../scripts/super-board-config.py ../scripts/super-board-approval.py "$STUBBIN/"
cp ../scripts/super-board-deps.sh "$STUBBIN/super-board-deps-real.sh"
cat > "$STUBBIN/super-board-deps.sh" <<STUB
#!/usr/bin/env bash
r=""; a=("\$@"); while [ \$# -gt 0 ]; do [ "\$1" = --repo ] && r="\$2"; shift; done
printf '%s %s\n' "\$r" "\${a[*]}" >> "$STUBBIN/deps-calls"
exec "$STUBBIN/super-board-deps-real.sh" --from "$STUBBIN/deps-\${r//\//_}.json" "\${a[@]}"
STUB
chmod +x "$STUBBIN/super-board-deps.sh"
cp ../scripts/super-board-wave-plan.sh "$STUBBIN/super-board-wave-plan.sh"
cp ../scripts/super-board-card-ready.py "$STUBBIN/"
WEB7=$(printf '%s' "$GOODBODY" | sed 's/^- None\.$/- acme\/api#3 — the endpoint/')
WEB8=$(printf '%s' "$GOODBODY" | sed 's/^- None\.$/- https:\/\/github.com\/acme\/api\/issues\/2/')
jq -n --arg b "$GOODBODY" '[{number:7, title:"api seven", state:"OPEN", body:$b}]' > "$STUBBIN/deps-acme_api.json"
jq -n --arg w7 "$WEB7" --arg w8 "$WEB8" --arg b "$GOODBODY" '{issues: [
    {number:7, title:"web seven", state:"OPEN", body:$w7},
    {number:8, title:"web eight", state:"OPEN", body:$w8},
    {number:9, title:"web nine", state:"OPEN", body:$b}],
  foreign: {"acme/api#3": {__typename:"Issue", state:"OPEN"},
            "acme/api#2": {__typename:"Issue", state:"CLOSED", stateReason:"COMPLETED"}}}' > "$STUBBIN/deps-acme_web.json"
HCFG="$HUB/.claude/super-board/configs/hub.json"
jq -n '{project:{owner:"acme", number:3}, hub:{remote:"https://github.com/acme/docs"},
  members:{backend:{repo:{path:"backend", remote:"https://github.com/acme/api"}, base_branch:"dev"},
           frontend:{repo:{path:"frontend", remote:"https://github.com/acme/web"}, base_branch:"dev"}},
  verify_commands:["git diff --check"]}' > "$HCFG"
HITEMS=$(jq -n --arg b "$GOODBODY" --arg w7 "$WEB7" '{items: [
  {status:"Ready",   content:{type:"Issue", number:7, title:"api seven", repository:"acme/api", assignees:[], body:$b}},
  {status:"Ready",   content:{type:"Issue", number:7, title:"web seven", repository:"acme/web", assignees:[], body:$w7}},
  {status:"Blocked", content:{type:"Issue", number:8, title:"web eight", repository:"acme/web", assignees:[]}},
  {status:"QA",      content:{type:"Issue", number:9, title:"web nine", repository:"acme/web", assignees:[]}},
  {status:"Ready",   content:{type:"Issue", number:1, title:"other one", repository:"acme/other", assignees:[]}},
  {status:"Ready",   content:{type:"Issue", number:4, title:"hub report", repository:"acme/docs", assignees:[]}},
  {status:"Ready",   labels:["parent"], content:{type:"Issue", number:5, title:"hub parent", repository:"acme/docs", assignees:[]}},
  {status:"Ready",   content:{type:"Issue", number:12, title:"api parent", repository:"acme/api", assignees:[], body:$b,
                              labels:[{name:"Parent"}]}}]}')
echo "$HITEMS" > "$STUBBIN/hub-items.json"
OUT23=$("$STUBBIN/super-board-wave-plan.sh" --config "$HCFG" --items "$STUBBIN/hub-items.json") || fail "hub plan failed"
echo "$OUT23" | jq -e '[.cards[] | "\(.member):\(.repository)#\(.number):\(.lane)"] == ["frontend:acme/web#9:qa","backend:acme/api#7:build"]' >/dev/null \
  || fail "both members' cards are planned with member, in flight first, got: $(echo "$OUT23" | jq -c .cards)"
echo "$OUT23" | jq -e '[.foreign[] | "\(.repository)#\(.number)"] == ["acme/docs#4","acme/other#1"]' >/dev/null \
  || fail "only non-member repos are foreign, got: $(echo "$OUT23" | jq -c .foreign)"
echo "$OUT23" | jq -e '[.foreign[].why] == ["hub issue: triage splits it into member sub-issues; never built",
                                            "card from acme/other: not a member of this board"]' >/dev/null \
  || fail "foreign why names the repo, and a hub issue says it is split: $(echo "$OUT23" | jq -c .foreign)"
echo "$OUT23" | jq -e '[.parents[] | "\(.repository)#\(.number):\(.status)"] == ["acme/docs#5:Ready","acme/api#12:Ready"]' >/dev/null \
  || fail "parent cards are reported, hub or member, got: $(echo "$OUT23" | jq -c .parents)"
echo "$OUT23" | jq -e '[.cards[], .triage[], .flag[], .foreign[] | select(.number == 5 or .number == 12)] == []' >/dev/null \
  || fail "a parent card is never planned, triaged or foreign: $OUT23"
# P6: packDir is the hub's absolute .claude, so lanes in a slim member worktree find the pack.
echo "$OUT23" | jq -e '.packDir | test("/hub/\\.claude$") and (test("\\\\") | not)' >/dev/null \
  || fail "a hub plan carries packDir = <hub>/.claude, got: $(echo "$OUT23" | jq -c .packDir)"
echo "$OUT23" | jq -e --arg hub "$HUB" '.cards | map(select(.member == "backend"))[0]
    | (.configPath | test("resolved[\\\\/]hub\\.backend\\.json$")) and (.checkout | test("[\\\\/]backend$"))
      and .baseBranch == "dev" and .deploy == false' >/dev/null \
  || fail "a member card carries its resolved config, checkout, base and deploy: $(echo "$OUT23" | jq -c .cards)"
BCFG=$(echo "$OUT23" | jq -r '.cards | map(select(.member == "backend"))[0].configPath')
jq -e '.member == "backend" and .repo.remote == "https://github.com/acme/api"' "$BCFG" >/dev/null \
  || fail "configPath is the backend's resolved config: $BCFG"
echo "$OUT23" | jq -e '.sweep == [{number:8, repository:"acme/web", title:"web eight", clearedBy:["acme/api#2"], member:"frontend"}]' >/dev/null \
  || fail "a cross-member blocker that closed sweeps the card, got: $(echo "$OUT23" | jq -c .sweep)"
echo "$OUT23" | jq -e '.flag == [] and .triage == [] and .stranded == []' >/dev/null \
  || fail "a cross-member ref is trusted, never flagged: $OUT23"
grep -q '^acme/web .*--linked acme/api ' "$STUBBIN/deps-calls" && grep -q '^acme/api .*--linked acme/web ' "$STUBBIN/deps-calls" \
  || fail "each member's graph links the other members, got: $(cat "$STUBBIN/deps-calls")"
# max_workers caps the merged wave, in-flight first; --deps per member stays offline.
jq '.max_workers = 1' "$HCFG" > "$HCFG.tmp" && mv "$HCFG.tmp" "$HCFG"
HDEPS=$(jq -n --argjson a "$(../scripts/super-board-deps.sh --from "$STUBBIN/deps-acme_api.json" --repo acme/api)" \
              --argjson w "$(../scripts/super-board-deps.sh --from "$STUBBIN/deps-acme_web.json" --repo acme/web --linked acme/api)" \
              '{backend:$a, frontend:$w}')
OUT23B=$("$STUBBIN/super-board-wave-plan.sh" --config "$HCFG" --items "$STUBBIN/hub-items.json" --deps <(echo "$HDEPS")) || fail "hub plan with --deps failed"
echo "$OUT23B" | jq -e '[.cards[] | "\(.member):\(.number)"] == ["frontend:9"]' >/dev/null \
  || fail "max_workers caps the merged wave, got: $(echo "$OUT23B" | jq -c .cards)"
# A halt in one member stops the hub plan (board-wide --check), naming the member.
mkdir -p "$HUB/frontend/.claude/super-board" && echo '{}' > "$HUB/frontend/.claude/super-board/github-halt.json"
RC=0; ERR=$("$STUBBIN/super-board-wave-plan.sh" --config "$HCFG" --items "$STUBBIN/hub-items.json" 2>&1 >/dev/null) || RC=$?
[ "$RC" -eq 79 ] && echo "$ERR" | grep -q 'frontend' || fail "a member halt must stop the hub plan (79, naming it), got $RC: $ERR"
# An invalid hub config is refused before anything is read.
jq '.repo = {path:".", remote:"https://github.com/acme/x"}' "$HCFG" > "$HCFG.tmp" && mv "$HCFG.tmp" "$HCFG"
RC=0; "$STUBBIN/super-board-wave-plan.sh" --config "$HCFG" --items "$STUBBIN/hub-items.json" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "a members config with a top-level repo must exit 65, got $RC"

# 24 — member shares (hub.md → Run). Backend has three free Ready cards, frontend one
#      in QA and one free Ready. A board max_workers gives each member an even share;
#      a member that has fewer cards leaves its slots to the other's leftovers; a
#      member's own max_workers is a hard cap. A deploy block on the base branch sets
#      `deploy` for that member's cards only.
HUB2="$STUBBIN/hub2"; mkdir -p "$HUB2/.claude/super-board/configs" "$HUB2/backend" "$HUB2/frontend"
H2CFG="$HUB2/.claude/super-board/configs/pair.json"
hub2() {  # <jq filter on the base config>
  jq -n '{project:{owner:"acme", number:3}, hub:{remote:"https://github.com/acme/docs"},
    members:{backend:{repo:{path:"backend", remote:"https://github.com/acme/api"}, base_branch:"dev",
                      deploy:{workflow:"deploy.yml"}},
             frontend:{repo:{path:"frontend", remote:"https://github.com/acme/web"}, base_branch:"main",
                       deploy:{workflow:"deploy.yml", branch:"prod"}}},
    verify_commands:["git diff --check"]}' | jq "$1" > "$H2CFG"
}
SITEMS=$(jq -n --arg b "$GOODBODY" '{items: [
  {status:"Ready", content:{type:"Issue", number:21, title:"api 21", repository:"acme/api", assignees:[], body:$b}},
  {status:"Ready", content:{type:"Issue", number:22, title:"api 22", repository:"acme/api", assignees:[], body:$b}},
  {status:"Ready", content:{type:"Issue", number:23, title:"api 23", repository:"acme/api", assignees:[], body:$b}},
  {status:"QA",    content:{type:"Issue", number:31, title:"web 31", repository:"acme/web", assignees:[]}},
  {status:"Ready", content:{type:"Issue", number:32, title:"web 32", repository:"acme/web", assignees:[], body:$b}}]}')
SDEPS=$(jq -n --arg b "$GOODBODY" '[21,22,23] | map({number: ., title: "api \(.)", state: "OPEN", body: $b})' > "$STUBBIN/s-api.json"
        jq -n --arg b "$GOODBODY" '[{number:32, title:"web 32", state:"OPEN", body:$b}]' > "$STUBBIN/s-web.json"
        jq -n --argjson a "$(../scripts/super-board-deps.sh --from "$STUBBIN/s-api.json" --repo acme/api)" \
              --argjson w "$(../scripts/super-board-deps.sh --from "$STUBBIN/s-web.json" --repo acme/web)" '{backend:$a, frontend:$w}')
share() {  # <config filter> <items filter> -> member:number list
  hub2 "$1"
  "$STUBBIN/super-board-wave-plan.sh" --config "$H2CFG" --items <(echo "$SITEMS" | jq "$2") --deps <(echo "$SDEPS") \
    | jq -c '[.cards[] | "\(.member):\(.number)"]'
}
GOT=$(share '.max_workers = 0' '.')
[ "$GOT" = '["frontend:31","backend:21","backend:22","backend:23","frontend:32"]' ] || fail "no cap plans every free card, got $GOT"
GOT=$(share '.max_workers = 2' '.')
[ "$GOT" = '["frontend:31","backend:21"]' ] || fail "max_workers 2 = one slot per member, got $GOT"
GOT=$(share '.max_workers = 4' '.')
[ "$GOT" = '["frontend:31","backend:21","backend:22","frontend:32"]' ] || fail "max_workers 4 = two per member, got $GOT"
GOT=$(share '.max_workers = 4' '.items |= map(select(.content.repository == "acme/api"))')
[ "$GOT" = '["backend:21","backend:22","backend:23"]' ] || fail "an idle member's slots go to the other, got $GOT"
GOT=$(share '.members.backend.max_workers = 1' '.')
[ "$GOT" = '["frontend:31","backend:21","frontend:32"]' ] || fail "a member's own max_workers caps it with no board cap, got $GOT"
GOT=$(share '.max_workers = 4 | .members.backend.max_workers = 1' '.items |= map(select(.content.number != 32))')
[ "$GOT" = '["frontend:31","backend:21"]' ] || fail "a member's own cap is hard: no backfill past it, got $GOT"
hub2 '.'
OUT24=$("$STUBBIN/super-board-wave-plan.sh" --config "$H2CFG" --items <(echo "$SITEMS") --deps <(echo "$SDEPS"))
echo "$OUT24" | jq -e '[.cards[] | "\(.member):\(.baseBranch):\(.deploy)"] | unique == ["backend:dev:true","frontend:main:false"]' >/dev/null \
  || fail "deploy follows each member's deploy.branch vs its base, got: $(echo "$OUT24" | jq -c .cards)"
# Single-repo plans are untouched: no member fields, no parents key.
echo "$OUT" | jq -e '(has("parents") | not) and ([.cards[] | has("member") or has("configPath")] | any | not)' >/dev/null \
  || fail "a single-repo plan must not change shape"

echo "PASS: test-wave-plan.sh (24 scenarios)"
