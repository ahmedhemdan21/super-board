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

# ── telemetry: collect_loki.py stubbed; prints $TEL_OUT, exits $TEL_RC, logs its args.
cat > "$WORK/loki_stub.py" <<'STUB'
import os, sys
with open(os.environ["TEL_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
print(os.environ.get("TEL_OUT", ""))
sys.exit(int(os.environ.get("TEL_RC", "0")))
STUB
export SB_COLLECT_LOKI="$WORK/loki_stub.py" TEL_LOG="$WORK/tel.log"
telcfg() {
  config "$GOOD"
  jq '.deploy.telemetry = {source:"loki", window_s:600, max_new_error_groups:0, selector_extra:"| service_name=\"api\""}
      | .collect.loki = {url:"http://loki:3100", selector:"{deployment_environment=\"dev\"}"}' "$WORK/cfg.json" > "$WORK/cfg2.json"
  mv "$WORK/cfg2.json" "$WORK/cfg.json"
}

echo "== telemetry: new error groups after deploy → bug"
telcfg; rm -f "$TEL_LOG"
TEL_OUT='{"status":"ok","regression":true,"new":2,"after":3,"maxNew":0,"windowS":120,"summary":"api: timeout calling ledger (4x); api: nullreferenceexception (1x)"}' run
is "exit 10" 10 "$RC"
has "telemetry reason" "$OUT" "reason=telemetry_regression"
hasnt "telemetry body passes the ticket checks" "$(cat "$WORK/bug.log")" "BODY REJECTED"
has "bug body names the new groups" "$(cat "$WORK/bug-body.md")" "timeout calling ledger"
TL=$(cat "$TEL_LOG")
has "check called with the deploy sha" "$TL" "check --since-epoch"
has "passes release" "$TL" "--release $SHA"
has "passes window and threshold" "$TL" "--window-s 600 --max-new 0"

echo "== telemetry: no new groups → healthy"
telcfg
TEL_OUT='{"status":"ok","regression":false,"new":0,"after":1,"maxNew":0,"windowS":120,"summary":""}' run
is "exit 0" 0 "$RC"
has "reports telemetry ok" "$OUT" "telemetry: ok"
has "still healthy" "$OUT" "deploy: healthy"
has "summary says telemetry=ok" "$OUT" "telemetry=ok"

echo "== telemetry: no logs at all → unknown warning, not ok"
telcfg
TEL_OUT='{"source":"loki","status":"no_data","lines":0,"regression":false,"windowS":120,"selector":"{deployment_environment=\"dev\"}"}' run
is "exit 0" 0 "$RC"
has "reports unknown" "$OUT" 'telemetry: unknown (no logs for {deployment_environment="dev"} in window)'
hasnt "never says ok" "$OUT" "telemetry: ok"
has "WARNING on stderr" "$(cat "$WORK/err")" "telemetry: WARNING"
has "summary carries it" "$OUT" "telemetry=unknown"
[ -e "$WORK/bug.log" ] && bad "no bug filed" "no bug.log" "filed" || ok "no bug filed"

echo "== telemetry: absent config → skipped"
config "$GOOD"; rm -f "$TEL_LOG"
run
is "exit 0" 0 "$RC"
has "says skipped" "$OUT" "telemetry: skipped"
[ -e "$TEL_LOG" ] && bad "collector not called" "no tel.log" "called" || ok "collector not called"

echo "== telemetry: Loki unreachable → warning, deploy healthy"
telcfg
TEL_RC=2 TEL_OUT='{"source":"loki","status":"unavailable","error":"Loki unreachable over ssh"}' run
is "exit 0" 0 "$RC"
has "deploy healthy" "$OUT" "deploy: healthy"
has "warning on stderr" "$(cat "$WORK/err")" "telemetry: warning"
[ -e "$WORK/bug.log" ] && bad "no bug filed" "no bug.log" "filed" || ok "no bug filed"

# ── regression watch: collect_prometheus.py stubbed; prints $PROM_OUT, exits $PROM_RC.
cat > "$WORK/prom_stub.py" <<'STUB'
import os, sys, time
with open(os.environ["PROM_LOG"], "a") as f:
    f.write("%d %s\n" % (int(time.time()), " ".join(sys.argv[1:])))
print(os.environ.get("PROM_OUT", ""))
sys.exit(int(os.environ.get("PROM_RC", "0")))
STUB
export SB_COLLECT_PROM="$WORK/prom_stub.py" PROM_LOG="$WORK/prom.log"
watchcfg() {  # <window_s> [revert_card]
  config "$GOOD"
  jq --argjson w "$1" --argjson rc "${2:-false}" '.deploy.regression_watch = {enabled:true, window_s:$w, promql_error_rate:"sum(rate(x[5m]))", revert_card:$rc}
      | .collect.loki = {url:"http://loki:3100", selector:"{deployment_environment=\"dev\"}"}' "$WORK/cfg.json" > "$WORK/cfg2.json"
  mv "$WORK/cfg2.json" "$WORK/cfg.json"
}
REG_OUT='{"source":"prometheus","status":"ok","regression":true,"windowS":2,"clipped":false,"minRatio":2.0,"minAbs":0.05,"lokiErrorRatio":2.0,
 "before":{"start":"2026-10-18T10:00:00+00:00","end":"2026-10-18T10:30:00+00:00"},"after":{"start":"2026-10-18T10:30:00+00:00","end":"2026-10-18T11:00:00+00:00"},
 "prometheus":{"status":"ok","before":{"errorRate":0.02,"requestRate":null},"after":{"errorRate":0.41,"requestRate":null}},
 "loki":{"status":"ok","errorsBefore":12,"errorsAfter":930},"summary":"prometheus error_rate 0.02 -> 0.41 (x20.50, errors/s after 0.41); loki errors 12 -> 930 (x77.50)"}'
LOKI_GROUPS='{"status":"ok","regression":true,"new":1,"newGroups":[{"group":"api","signature":"timeout calling ledger","sampleCount":44}]}'

echo "== regression watch: waits for the window, then files a regression bug"
watchcfg 2; rm -f "$PROM_LOG" "$TEL_LOG"
T0=$(date +%s)
PROM_OUT="$REG_OUT" TEL_OUT="$LOKI_GROUPS" run
is "exit 10" 10 "$RC"
has "regression reason" "$OUT" "reason=regression"
WAITED=$(( $(awk '{print $1}' "$PROM_LOG" | head -n 1) - T0 ))
[ "$WAITED" -ge 2 ] && ok "check ran after deploy + window_s (${WAITED}s)" || bad "check waited for the window" ">= 2s" "${WAITED}s"
has "check passes the deploy time" "$(cat "$PROM_LOG")" "check --since-epoch"
BUGLOG=$(cat "$WORK/bug.log")
hasnt "regression body passes the ticket checks" "$BUGLOG" "BODY REJECTED"
has "labelled regression" "$BUGLOG" "--label regression"
has "one open regression bug per PR" "$BUGLOG" "--fingerprint regression|pr-12"
BODYMD=$(cat "$WORK/bug-body.md")
has "both windows' numbers" "$BODYMD" "| 0.41 |"
has "before window" "$BODYMD" "before (2026-10-18T10:00:00+00:00"
has "top new Loki group" "$BODYMD" "api: timeout calling ledger (44x)"
has "links the deploy run" "$BODYMD" "actions/runs/42"
has "rate AC" "$BODYMD" "deploy.regression_watch error rate"
is "no revert card by default" 1 "$(grep -c -- '--body-file' "$WORK/bug.log")"
hasnt "never reverts" "$(cat "$GH_LOG")" "revert"

echo "== regression watch: revert_card true → a second Backlog card naming the bug"
watchcfg 1 true
cat > "$WORK/file-bug-env.sh" <<STUB
#!/usr/bin/env bash
printf 'target=%s %s\n' "\${SUPER_QA_TARGET_OPTION_NAME:-}" "\$*" >> "$WORK/bug.log"
while [ \$# -gt 0 ]; do [ "\$1" = "--body-file" ] && cp "\$2" "$WORK/bug-body-\$(date +%s%N).md" && bash "$REPO_ROOT/scripts/super-qa-file-bug.sh" --title t --body-file "\$2" --check-body || { [ "\$1" = "--body-file" ] && echo "BODY REJECTED" >> "$WORK/bug.log"; }; shift; done
echo 88
STUB
chmod +x "$WORK/file-bug-env.sh"
SB_FILE_BUG="$WORK/file-bug-env.sh" PROM_OUT="$REG_OUT" TEL_OUT="$LOKI_GROUPS" run
is "exit 10" 10 "$RC"
BUGLOG=$(cat "$WORK/bug.log")
is "two cards filed" 2 "$(grep -c -- '--body-file' "$WORK/bug.log")"
has "revert card goes to Backlog" "$BUGLOG" "target=Backlog --title Revert #12 (regression #88)"
has "revert card dedupes per PR" "$BUGLOG" "--fingerprint revert|pr-12"
hasnt "both bodies pass the ticket checks" "$BUGLOG" "BODY REJECTED"
has "names the revert card" "$OUT" "revert card #88 (Backlog)"
hasnt "never reverts or pushes" "$(cat "$GH_LOG")" "revert"

echo "== regression watch: no data → inconclusive warning, never ok"
watchcfg 1
PROM_OUT='{"source":"prometheus","status":"inconclusive","regression":false,"summary":"prometheus inconclusive: no error-rate series in either window"}' run
is "exit 0" 0 "$RC"
has "says inconclusive" "$OUT" "watch: inconclusive"
hasnt "never ok" "$OUT" "watch: ok"
has "summary carries it" "$OUT" "watch=inconclusive"
has "WARNING on stderr" "$(cat "$WORK/err")" "watch: WARNING"
[ -e "$WORK/bug.log" ] && bad "no bug filed" "no bug.log" "filed" || ok "no bug filed"

echo "== regression watch: ratio under the floor → ok"
watchcfg 1
PROM_OUT='{"source":"prometheus","status":"ok","regression":false,"summary":"prometheus error_rate 0.001 -> 0.01 (x10.00, errors/s after 0.01)"}' run
is "exit 0" 0 "$RC"
has "watch ok" "$OUT" "watch: ok (prometheus error_rate 0.001 -> 0.01"
has "summary" "$OUT" "watch=ok"

echo "== regression watch: Prometheus unreachable → warning, deploy healthy"
watchcfg 1
PROM_RC=2 PROM_OUT='{"source":"prometheus","status":"unavailable","error":"Prometheus unreachable"}' run
is "exit 0" 0 "$RC"
has "summary" "$OUT" "watch=unavailable"
has "warning" "$(cat "$WORK/err")" "watch: warning"

echo "== regression watch: disabled → skipped, collector not called"
config "$GOOD"; jq '.deploy.regression_watch = {enabled:false, promql_error_rate:"x"}' "$WORK/cfg.json" > "$WORK/cfg2.json"; mv "$WORK/cfg2.json" "$WORK/cfg.json"
rm -f "$PROM_LOG"
run
is "exit 0" 0 "$RC"
has "says skipped" "$OUT" "watch: skipped"
[ -e "$PROM_LOG" ] && bad "collector not called" "no prom.log" "called" || ok "collector not called"

echo "== regression watch: --budget-s exits 75 while waiting; the re-call keeps the deploy time"
watchcfg 30; rm -f "$PROM_LOG"
PROM_OUT="$REG_OUT" run --budget-s 1
is "exit 75" 75 "$RC"
DTS=$(jq -r '.deploy_ts' "$WORK/state/$SHA.json" 2>/dev/null)
[ -n "$DTS" ] && [ "$DTS" != "null" ] && ok "deploy time saved ($DTS)" || bad "deploy time saved" "a number" "$DTS"
[ -e "$PROM_LOG" ] && bad "no check before the window closes" "no prom.log" "called" || ok "no check before the window closes"
PAST=$(( $(date +%s) - 31 ))
jq --arg d "$PAST" '.deploy_ts = $d' "$WORK/state/$SHA.json" > "$WORK/s2" && mv "$WORK/s2" "$WORK/state/$SHA.json"
: > "$GH_LOG"; rm -f "$RUN_COUNT"
OUT=$(PROM_OUT='{"status":"ok","regression":false,"summary":"fine"}' "$SCRIPT" --config "$WORK/cfg.json" --pr 12 --issue 5 --budget-s 5 2>"$WORK/err"); RC=$?
is "re-call finishes" 0 "$RC"
has "watch ran on the re-call" "$OUT" "watch: ok (fine)"
has "with the saved deploy time" "$(cat "$PROM_LOG")" "--since-epoch $PAST"

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
