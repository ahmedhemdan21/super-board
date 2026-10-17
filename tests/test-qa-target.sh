#!/usr/bin/env bash
# Tests scripts/super-board-qa-target.sh — QA targets, the pre-merge local stack
# and the generic QA login. Offline: real loopback servers, no gh.
#
#   bash tests/test-qa-target.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/super-board-qa-target.sh"
SERVER="$REPO_ROOT/tests/fixtures/health_server.py"
WORK="$(mktemp -d)"
EXT_PID=""
cleanup() {
  for n in 7 8 9 11 12; do SB_QA_STATE_DIR="$WORK/state" bash "$SCRIPT" stop --config "$WORK/local.json" --issue "$n" 2>/dev/null; done
  [ -n "$EXT_PID" ] && kill "$EXT_PID" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT
export SB_QA_STATE_DIR="$WORK/state" SB_QA_LOCAL_POLL=0.2
mkdir -p "$WORK/wt"

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  ✅ %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  ❌ %s\n     expected: %s\n     actual:   %s\n' "$1" "$2" "$3"; }
is()  { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }
has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "contains: $3" "$2" ;; esac; }
hasnt() { case "$2" in *"$3"*) bad "$1" "no '$3'" "found it" ;; *) ok "$1" ;; esac; }
up() { curl -fsS -o /dev/null --max-time 2 "$1" 2>/dev/null; }
q() { SB_QA_STATE_DIR="$WORK/state" bash "$SCRIPT" "$@"; }

# A free-standing server for the remote-target checks.
python3 "$SERVER" 0 "$WORK/ext-port" &
EXT_PID=$!
for _ in $(seq 1 100); do [ -s "$WORK/ext-port" ] && break; sleep 0.1; done
EXT="http://127.0.0.1:$(cat "$WORK/ext-port")"

echo "== remote targets: target.url + target.urls"
jq -n --arg u "$EXT/health" --arg a "$EXT/down" '{target:{url:$u, urls:{"admin api":$a, web:$u}}}' > "$WORK/remote.json"
ENV=$(q env --config "$WORK/remote.json")
has "default as SB_TARGET_URL" "$ENV" "export SB_TARGET_URL='$EXT/health'"
has "named target, normalized" "$ENV" "export SB_TARGET_ADMIN_API='$EXT/down'"
has "BASE_URL for super-qa" "$ENV" "export BASE_URL='$EXT/health'"
is "SB_TARGET_URL once" 1 "$(echo "$ENV" | grep -c '^export SB_TARGET_URL=')"
OUT=$(q check --config "$WORK/remote.json"); RC=$?
is "one unhealthy target → exit 3" 3 "$RC"
has "names the unhealthy one" "$OUT" "unhealthy admin api $EXT/down (HTTP 503)"
has "reports the healthy one" "$OUT" "ok web $EXT/health"
jq -n --arg u "$EXT/health" '{target:{url:null, urls:{web:$u}}}' > "$WORK/named.json"
has "urls only: first is the default" "$(q env --config "$WORK/named.json")" "export SB_TARGET_URL='$EXT/health'"
q check --config "$WORK/named.json" >/dev/null; is "all healthy → exit 0" 0 $?
echo '{"target":{"url":null}}' > "$WORK/none.json"
q check --config "$WORK/none.json" 2>/dev/null; is "no targets → exit 0" 0 $?

echo "== qa.local absent → start is a no-op"
OUT=$(q start --config "$WORK/remote.json" --issue 7 --worktree "$WORK/wt" 2>/dev/null); RC=$?
is "exit 0" 0 "$RC"; is "prints nothing" "" "$OUT"

echo "== two lanes in parallel get different port blocks"
jq -n --arg s "python3 \"$SERVER\" {port}" --arg stop "echo stopped \$SB_PORT_BASE >> \"$WORK/stop.log\"" \
  '{qa:{local:{start:$s, stop:$stop, timeout_s:30, port_base:23000, port_slots:10,
               health:[{name:"api", url:"http://127.0.0.1:{port}/health"}, {name:"web app", url:"http://127.0.0.1:{port}/version?p={port+1}"}]}}}' > "$WORK/local.json"
( q start --config "$WORK/local.json" --issue 7 --worktree "$WORK/wt" > "$WORK/s7" 2>"$WORK/e7"; echo $? > "$WORK/rc7" ) &
P7=$!
( q start --config "$WORK/local.json" --issue 17 --worktree "$WORK/wt" > "$WORK/s17" 2>"$WORK/e17"; echo $? > "$WORK/rc17" ) &
P17=$!
wait "$P7" "$P17"   # not a bare wait: the fixture server is a child too
is "lane #7 healthy" 0 "$(cat "$WORK/rc7")"
is "lane #17 healthy" 0 "$(cat "$WORK/rc17")"
B7=$(sed -n 's/^export SB_PORT_BASE=//p' "$WORK/s7"); B17=$(sed -n 's/^export SB_PORT_BASE=//p' "$WORK/s17")
[ -n "$B7" ] && [ -n "$B17" ] && [ "$B7" != "$B17" ] && ok "distinct SB_PORT_BASE ($B7, $B17)" || bad "distinct SB_PORT_BASE" "two different" "$B7 / $B17"
has "health name → SB_TARGET_WEB_APP with {port+1}" "$(cat "$WORK/s7")" "export SB_TARGET_WEB_APP='http://127.0.0.1:$B7/version?p=$((B7 + 1))'"
has "first health URL is BASE_URL" "$(cat "$WORK/s7")" "export BASE_URL='http://127.0.0.1:$B7/health'"
up "http://127.0.0.1:$B7/health" && ok "#7 serving on its block" || bad "#7 serving" "up" "down"
up "http://127.0.0.1:$B17/health" && ok "#17 serving on its block" || bad "#17 serving" "up" "down"
has "env --issue returns the local stack" "$(q env --config "$WORK/local.json" --issue 7)" "SB_PORT_BASE=$B7"

echo "== stop: stop command, then the tree; lease freed; idempotent"
q stop --config "$WORK/local.json" --issue 7 2>/dev/null; is "stop exits 0" 0 $?
sleep 1
up "http://127.0.0.1:$B7/health" && bad "#7 stopped" "down" "still up" || ok "#7 stopped"
up "http://127.0.0.1:$B17/health" && ok "#17 untouched" || bad "#17 untouched" "up" "down"
has "stop command got SB_PORT_BASE" "$(cat "$WORK/stop.log" 2>/dev/null)" "stopped $B7"
[ -e "$WORK/state/issue-7.json" ] && bad "state removed" "gone" "present" || ok "state removed"
q stop --config "$WORK/local.json" --issue 7 2>/dev/null; is "second stop still exits 0" 0 $?
q stop --config "$WORK/local.json" --issue 17 2>/dev/null
ls "$WORK/state/ports" | grep -q . && bad "all leases freed" "empty" "$(ls "$WORK/state/ports")" || ok "all leases freed"

echo "== a port block already in use is skipped"
SLOT=$((9 % 10)); BUSY=$((23000 + SLOT * 10))
python3 "$SERVER" "$BUSY" &
BUSY_PID=$!
for _ in $(seq 1 100); do up "http://127.0.0.1:$BUSY/health" && break; sleep 0.1; done
OUT=$(q start --config "$WORK/local.json" --issue 9 --worktree "$WORK/wt" 2>/dev/null); RC=$?
is "start still succeeds" 0 "$RC"
hasnt "does not take the busy block" "$OUT" "SB_PORT_BASE=$BUSY"
q stop --config "$WORK/local.json" --issue 9 2>/dev/null
kill "$BUSY_PID" 2>/dev/null

echo "== never healthy → exit 4, stopped, lease freed"
jq '.qa.local.health = [{name:"api", url:"http://127.0.0.1:{port}/down"}] | .qa.local.timeout_s = 2' "$WORK/local.json" > "$WORK/sick.json"
q start --config "$WORK/sick.json" --issue 11 --worktree "$WORK/wt" >/dev/null 2>"$WORK/e11"; is "exit 4" 4 $?
has "names the pending URL" "$(cat "$WORK/e11")" "/down"
[ -e "$WORK/state/issue-11.json" ] && bad "stopped after timeout" "no state" "state left" || ok "stopped after timeout"
SICK=$((23000 + 11 % 10 * 10))
sleep 1; up "http://127.0.0.1:$SICK/health" && bad "server killed" "down" "up" || ok "server killed"

echo "== start command dies → exit 4 without waiting for timeout_s"
jq '.qa.local.start = "exit 1" | .qa.local.timeout_s = 60' "$WORK/local.json" > "$WORK/dead.json"
T0=$(date +%s)
q start --config "$WORK/dead.json" --issue 12 --worktree "$WORK/wt" >/dev/null 2>&1; is "exit 4" 4 $?
[ $(( $(date +%s) - T0 )) -lt 30 ] && ok "fails fast" || bad "fails fast" "<30s" "$(( $(date +%s) - T0 ))s"

echo "== state from a linked worktree lives in the git common dir, not a checkout"
git init -q "$WORK/main" && git -C "$WORK/main" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$WORK/main" worktree add -q "$WORK/lane" 2>/dev/null
COMMON=$(git -C "$WORK/main" rev-parse --path-format=absolute --git-common-dir)
( cd "$WORK/lane" && env -u SB_QA_STATE_DIR bash "$SCRIPT" start --config "$WORK/dead.json" --issue 12 --worktree "$WORK/lane" >/dev/null 2>&1 )
[ -d "$COMMON/super-board/qa-local/ports" ] && ok "leases under <common>/super-board" || bad "leases under <common>/super-board" "$COMMON/super-board/qa-local" "missing"
[ -e "$WORK/main/.claude" ] && bad "main checkout untouched" "no main/.claude" "created" || ok "main checkout untouched"
[ -e "$WORK/lane/.claude" ] && bad "lane checkout untouched" "no lane/.claude" "created" || ok "lane checkout untouched"

echo "== qa.auth_command: JSON login, never printed"
jq -n --arg u "$EXT/health" '{target:{url:$u}, qa:{auth_command:"printf %s \"{\\\"token\\\":\\\"s3cr3t-tok\\\",\\\"user\\\":\\\"$BASE_URL\\\"}\""}}' > "$WORK/auth.json"
OUT=$(q auth --config "$WORK/auth.json" 2>"$WORK/ea"); RC=$?
is "exit 0" 0 "$RC"
has "kind json" "$OUT" "export SB_AUTH_KIND='json'"
has "keys listed" "$OUT" "export SB_AUTH_KEYS='token,user'"
hasnt "token not on stdout" "$OUT" "s3cr3t-tok"
hasnt "token not on stderr" "$(cat "$WORK/ea")" "s3cr3t-tok"
FILE=$(echo "$OUT" | sed -n "s/^export SB_AUTH_FILE='\(.*\)'$/\1/p")
is "file holds the login, with the targets in the command's env" "s3cr3t-tok $EXT/health" "$(jq -r '"\(.token) \(.user)"' "$FILE" 2>/dev/null)"
echo '{"cookies":[],"origins":[]}' > "$WORK/state.json"
jq --arg p "$WORK/state.json" '.qa.auth_command = "echo \($p)"' "$WORK/auth.json" > "$WORK/auth2.json"
OUT=$(q auth --config "$WORK/auth2.json" 2>/dev/null)
has "a printed path is a storageState file" "$OUT" "export SB_AUTH_KIND='storage-state'"
has "path passed through" "$OUT" "state.json'"
jq '.qa.auth_command = "echo leaked-s3cr3t >&2; exit 2"' "$WORK/auth.json" > "$WORK/auth3.json"
OUT=$(q auth --config "$WORK/auth3.json" 2>"$WORK/ea"); RC=$?
is "failed command → exit 6" 6 "$RC"
hasnt "its stderr is not echoed" "$(cat "$WORK/ea")$OUT" "leaked-s3cr3t"
jq '.qa.auth_command = "echo not-json-or-path"' "$WORK/auth.json" > "$WORK/auth4.json"
q auth --config "$WORK/auth4.json" >/dev/null 2>&1; is "neither JSON nor a path → exit 6" 6 $?
OUT=$(q auth --config "$WORK/remote.json" 2>/dev/null); RC=$?
is "no auth_command → exit 0" 0 "$RC"; is "and no exports" "" "$OUT"

echo
echo "PASS: $PASS  FAIL: $FAIL"
[ "$FAIL" -eq 0 ]
