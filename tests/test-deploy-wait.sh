#!/usr/bin/env bash
# Tests scripts/super-board-deploy-wait.sh — the post-merge dev check. Offline:
# `gh` is a stub on PATH, health URLs hit a loopback fixture server, and the bug
# filer is a stub that still runs the real ticket checks on the body it gets.
#
#   bash tests/test-deploy-wait.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/super-board-deploy-wait.sh"
WORK="$(mktemp -d)"
SERVER_PID=""
cleanup() { [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT
export SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$WORK/halt.json" \
  SB_DEPLOY_STATE_DIR="$WORK/state" SB_DEPLOY_POLL=1 SB_DEPLOY_POLL_MAX=1 SB_DEPLOY_HEALTH_GRACE=2

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  ✅ %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  ❌ %s\n     expected: %s\n     actual:   %s\n' "$1" "$2" "$3"; }
is()  { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }
has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "contains: $3" "$2" ;; esac; }
hasnt() { case "$2" in *"$3"*) bad "$1" "no '$3'" "found it" ;; *) ok "$1" ;; esac; }

SHA=abcdef1234567890abcdef1234567890abcdef12
HEAD=1111111111111111111111111111111111111111

# ── loopback health server.
HEALTH_VERSION="build-${SHA:0:7}" python3 "$REPO_ROOT/tests/fixtures/health_server.py" 0 "$WORK/port" &
SERVER_PID=$!
for _ in $(seq 1 100); do [ -s "$WORK/port" ] && break; sleep 0.1; done
PORT=$(cat "$WORK/port" 2>/dev/null)
[ -n "$PORT" ] || { echo "health server did not start"; exit 1; }
H="http://127.0.0.1:$PORT"

# ── gh stub.
#   PR_BASE      — baseRefName of the PR (default dev)
#   RUN_LIST     — JSON `run list` returns (default one run, id 42)
#   RUN_SEQ      — space-separated status:conclusion per `run view` call; the last repeats
cat > "$WORK/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$1 ${2:-}" in
  "api rate_limit") echo '{"resources":{"graphql":{"limit":5000,"remaining":4000,"reset":1},"core":{"limit":5000,"remaining":4000,"reset":1}}}' ;;
  "api graphql") echo '{"data":{"rateLimit":{"limit":5000,"remaining":4000,"used":1000,"resetAt":"2030-01-01T00:00:00Z"}}}' ;;
  "pr view") printf '{"state":"MERGED","headRefOid":"%s","mergeCommit":{"oid":"%s"},"baseRefName":"%s","url":"https://github.com/acme/app/pull/%s"}\n' \
      "$STUB_HEAD" "$STUB_SHA" "${PR_BASE:-dev}" "$3" ;;
  "run list")
    DEFAULT='[{"databaseId":42,"status":"in_progress","conclusion":""}]'
    echo "${RUN_LIST:-$DEFAULT}" ;;
  "run view")
    n=$(cat "$RUN_COUNT" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$RUN_COUNT"
    set -- ${RUN_SEQ:-completed:success}; i=$n; [ "$i" -le "$#" ] || i=$#
    eval "entry=\${$i}"
    printf '{"status":"%s","conclusion":"%s","url":"https://github.com/acme/app/actions/runs/42"}\n' "${entry%%:*}" "${entry#*:}" ;;
  *) exit 0 ;;
esac
STUB
chmod +x "$WORK/gh"

# ── bug filer stub: runs the real body checks, records the call, prints a number.
cat > "$WORK/file-bug.sh" <<STUB
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$WORK/bug.log"
while [ \$# -gt 0 ]; do [ "\$1" = "--body-file" ] && cp "\$2" "$WORK/bug-body.md"; shift; done
bash "$REPO_ROOT/scripts/super-qa-file-bug.sh" --title t --body-file "$WORK/bug-body.md" --check-body \
  || { echo "BODY REJECTED" >> "$WORK/bug.log"; exit 66; }
echo 88
STUB
chmod +x "$WORK/file-bug.sh"
export PATH="$WORK:$PATH" GH_LOG="$WORK/gh.log" RUN_COUNT="$WORK/run-count" \
  SB_FILE_BUG="$WORK/file-bug.sh" STUB_SHA="$SHA" STUB_HEAD="$HEAD"

config() {  # <health json> [smoke] [timeout] [extra jq]
  jq -n --argjson health "$1" --arg smoke "${2:-}" --argjson t "${3:-60}" \
    '{base_branch:"dev", repo:{path:"."}, deploy:{branch:"dev", workflow:"deploy-dev.yml", timeout_s:$t, health:$health}}
     | if $smoke != "" then .deploy.smoke = $smoke else . end' > "$WORK/cfg.json"
}
run() {
  : > "$GH_LOG"; rm -f "$RUN_COUNT" "$WORK/bug.log" "$WORK/bug-body.md"
  OUT=$("$SCRIPT" --config "$WORK/cfg.json" --pr 12 --issue 5 "$@" 2>"$WORK/err"); RC=$?
}
GOOD="[{\"name\":\"api\",\"url\":\"$H/health\"},{\"name\":\"web\",\"url\":\"$H/version\",\"expect_version\":\"{short_sha}\"}]"

echo "== healthy deploy: run waits, health + version + smoke pass"
config "$GOOD" "printf '%s %s %s' \"\$SB_DEPLOY_SHA\" \"\$SB_TARGET_API\" \"\$SB_DEPLOY_RUN_ID\" > '$WORK/smoke.out'"
RUN_SEQ="in_progress: completed:success" run
is "exit 0" 0 "$RC"
has "reports healthy" "$OUT" "deploy: healthy sha=${SHA:0:7} run=42"
is "run polled until completed" 2 "$(cat "$RUN_COUNT")"
has "run looked up by workflow and merge commit" "$(cat "$GH_LOG")" "run list --workflow deploy-dev.yml --commit $SHA"
is "smoke got sha, target env and run id" "$SHA $H/health 42" "$(cat "$WORK/smoke.out" 2>/dev/null)"
[ -e "$WORK/bug.log" ] && bad "no bug filed" "no bug.log" "bug filed" || ok "no bug filed"
has "quota guard ran" "$(cat "$GH_LOG")" "api rate_limit"
hasnt "never reverts or edits the PR" "$(cat "$GH_LOG")" "pr edit"

echo "== deploy workflow fails → bug filed, PR left alone"
config "$GOOD"
RUN_SEQ="completed:failure" run
is "exit 10" 10 "$RC"
has "names the reason and bug" "$OUT" "reason=deploy-failed"
has "bug number on stdout" "$OUT" "bug: #88"
BUGLOG=$(cat "$WORK/bug.log" 2>/dev/null)
hasnt "bug body passes the ticket checks" "$BUGLOG" "BODY REJECTED"
has "bug title names the PR" "$BUGLOG" "after PR #12: deploy-failed"
has "bug is a high-priority deploy bug" "$BUGLOG" "--kind bug --priority high"
has "bug body links the original card" "$(cat "$WORK/bug-body.md")" "card #5"
has "bug body links the PR" "$(cat "$WORK/bug-body.md")" "https://github.com/acme/app/pull/12"
hasnt "never reverts or closes anything" "$(cat "$GH_LOG")" "revert"

echo "== no run for the commit → timeout bug"
config "$GOOD" "" 2
RUN_LIST='[]' run
is "exit 10" 10 "$RC"
has "timeout reason" "$OUT" "reason=timeout"
hasnt "timeout body passes the ticket checks" "$(cat "$WORK/bug.log")" "BODY REJECTED"

echo "== run stays in progress past timeout_s → timeout"
config "$GOOD" "" 2
RUN_SEQ="in_progress:" run
is "exit 10" 10 "$RC"
has "timeout reason" "$OUT" "reason=timeout"

echo "== unhealthy URL → bug"
config "[{\"name\":\"api\",\"url\":\"$H/down\"}]"
run
is "exit 10" 10 "$RC"
has "unhealthy reason" "$OUT" "reason=unhealthy"
has "names the failing URL" "$(cat "$WORK/err")" "$H/down returned HTTP 503"

echo "== wrong version deployed → unhealthy"
config "[{\"name\":\"web\",\"url\":\"$H/version\",\"expect_version\":\"build-0000000\"}]"
run
is "exit 10" 10 "$RC"
has "version named" "$(cat "$WORK/err")" "does not report version build-0000000"

echo "== smoke fails → bug"
config "$GOOD" "echo '<html>broken</html>'; exit 3"
run
is "exit 10" 10 "$RC"
has "smoke reason" "$OUT" "reason=smoke-failed"
hasnt "smoke output does not break the ticket checks" "$(cat "$WORK/bug.log")" "BODY REJECTED"

echo "== --no-file-bug → exit 11, nothing filed"
config "$GOOD"
RUN_SEQ="completed:failure" run --no-file-bug
is "exit 11" 11 "$RC"
[ -e "$WORK/bug.log" ] && bad "nothing filed" "no bug.log" "filed" || ok "nothing filed"

echo "== skips"
echo '{"base_branch":"dev","repo":{"path":"."}}' > "$WORK/cfg.json"
run
is "no deploy block → exit 0" 0 "$RC"
has "says skipped" "$OUT" "deploy: skipped"
is "no gh calls" "" "$(cat "$GH_LOG")"
config "$GOOD"
PR_BASE=main run
is "PR into another branch → exit 0" 0 "$RC"
has "names the branch" "$OUT" "merged into main"

echo "== --budget-s: exit 75, the next call keeps the run and the clock"
config "$GOOD" "" 60
RUN_SEQ="in_progress:" run --budget-s 1
is "exit 75" 75 "$RC"
has "says call again" "$OUT" "call again"
[ -f "$WORK/state/$SHA.json" ] && ok "state kept" || bad "state kept" "state file" "missing"
: > "$GH_LOG"; rm -f "$RUN_COUNT"
OUT=$(RUN_SEQ="completed:success" "$SCRIPT" --config "$WORK/cfg.json" --pr 12 --issue 5 2>"$WORK/err"); RC=$?
is "second call finishes" 0 "$RC"
hasnt "second call reuses the run id" "$(cat "$GH_LOG")" "run list"
[ -f "$WORK/state/$SHA.json" ] && bad "state cleared" "no file" "still there" || ok "state cleared"

echo "== state from a linked worktree stays in that repo.path, not the main clone"
git init -q "$WORK/main" && git -C "$WORK/main" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$WORK/main" worktree add -q "$WORK/lane" 2>/dev/null
config "$GOOD" "" 60
: > "$GH_LOG"; rm -f "$RUN_COUNT"
OUT=$(env -u SB_DEPLOY_STATE_DIR SB_REPO_PATH="$WORK/lane" RUN_SEQ="in_progress:" "$SCRIPT" --config "$WORK/cfg.json" --pr 12 --issue 5 --budget-s 1 2>"$WORK/err"); RC=$?
is "exit 75" 75 "$RC"
[ -f "$WORK/lane/.claude/super-board/deploy/$SHA.json" ] && ok "state in the lane's repo.path" || bad "state in the lane's repo.path" "lane state file" "$(find "$WORK" -name "$SHA.json")"
[ -e "$WORK/main/.claude" ] && bad "main clone untouched" "no main/.claude" "created" || ok "main clone untouched"

echo
echo "PASS: $PASS  FAIL: $FAIL"
[ "$FAIL" -eq 0 ]
