#!/usr/bin/env bash
# super-board-stop.sh on a hub board (hub.md → Cut-over): in-flight locks are read in
# every member checkout, each card is wrapped up with --repo <its member repo> and its
# member-prefixed branch, and control markers stay. A single-repo stop is unchanged.
# Offline: gh and pgrep are stubs; worker PIDs are dead numbers.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
fail() { cat "$TMP"/stop*.log 2>/dev/null | head -30 >&2; echo "FAIL: $1" >&2; exit 1; }
PASS=0
ok() { PASS=$((PASS + 1)); }

mkdir -p "$TMP/bin"
export STOP_TRACE="$TMP/trace"
cat > "$TMP/bin/gh" <<'SH'
#!/usr/bin/env bash
printf 'gh %s\n' "$*" >> "$STOP_TRACE"
case "$*" in "pr list --repo acme/api "*) echo 41 ;; esac
exit 0
SH
printf '#!/usr/bin/env bash\nexit 1\n' > "$TMP/bin/pgrep"
chmod +x "$TMP/bin/"*

# 1 — hub: backend #7 (build) and frontend #7 (qa) in flight, in their own checkouts.
H="$TMP/hub"
mkdir -p "$H/.claude/super-board/configs"
cat > "$H/.claude/super-board/configs/pc.json" <<'JSON'
{"project":{"owner":"acme","number":3},"hub":{"remote":"https://github.com/acme/docs","path":"."},
 "notifications":{"bot_identity":"bot"},
 "members":{"backend":{"repo":{"path":"backend","remote":"https://github.com/acme/api"},"base_branch":"dev"},
            "frontend":{"repo":{"path":"frontend","remote":"https://github.com/acme/web"},"base_branch":"dev"}}}
JSON
for m in backend frontend; do
  mkdir -p "$H/$m/.claude/super-board/inflight"
  git -C "$H/$m" init -q -b dev
  git -C "$H/$m" -c user.name=t -c user.email=t@t commit -q --allow-empty -m "init $m"
done
git -C "$H/backend" -c user.name=t -c user.email=t@t checkout -q -b backend-issue-7-csv
git -C "$H/backend" -c user.name=t -c user.email=t@t commit -q --allow-empty -m "backend csv endpoint"
git -C "$H/backend" checkout -q dev
printf 'PID=999999991\nLANE=build\nSTARTED=now\n' > "$H/backend/.claude/super-board/inflight/7"
printf 'PID=999999992\nLANE=qa\nSTARTED=now\n' > "$H/frontend/.claude/super-board/inflight/7"
mkdir -p "$H/backend/.claude/super-board/inflight/merge.lock"
printf 'SLUG=pc\nHUB=acme/docs\n' > "$H/backend/.claude/super-board/inflight/workflow-wave.lock"

(cd "$H" && PATH="$TMP/bin:$PATH" bash "$ROOT/scripts/super-board-stop.sh" pc > "$TMP/stop.log" 2>&1) \
  || { cat "$TMP/stop.log"; fail "hub stop failed"; }
grep -q '^gh issue comment 7 --repo acme/api --body' "$STOP_TRACE" || fail "backend #7 comment must name --repo acme/api"; ok
grep -q '^gh issue comment 7 --repo acme/web --body' "$STOP_TRACE" || fail "frontend #7 comment must name --repo acme/web"; ok
grep -q '^gh pr comment 41 --repo acme/api --body' "$STOP_TRACE" || fail "backend PR comment must name its repo"; ok
grep -q 'pr list --repo acme/api .*backend-issue-7-' "$STOP_TRACE" || fail "backend PR lookup must match backend-issue-7-*"; ok
grep -q 'pr list --repo acme/web .*frontend-issue-7-' "$STOP_TRACE" || fail "frontend PR lookup must match frontend-issue-7-*"; ok
grep -q 'backend csv endpoint' "$STOP_TRACE" || fail "the backend comment names the last commit on backend-issue-7-*"; ok
grep -q '^gh issue edit 7 --repo acme/api --remove-assignee bot' "$STOP_TRACE" || fail "claim released in the member repo"; ok
grep -q '^gh issue edit 7 --repo acme/web --remove-label loop:in-qa' "$STOP_TRACE" || fail "lane labels removed in the member repo"; ok
if grep -q '^gh issue \(comment\|edit\) 7 --body\|^gh issue \(comment\|edit\) 7 --remove' "$STOP_TRACE"; then
  fail "a hub card was wrapped up without --repo"
fi; ok
[ ! -e "$H/backend/.claude/super-board/inflight/7" ] && [ ! -e "$H/frontend/.claude/super-board/inflight/7" ] \
  || fail "worker locks cleared in each member checkout"; ok
[ -d "$H/backend/.claude/super-board/inflight/merge.lock" ] && [ -f "$H/backend/.claude/super-board/inflight/workflow-wave.lock" ] \
  || fail "control markers are not worker locks"; ok
grep -q 'stopped: 2 worker(s)' "$TMP/stop.log" || fail "summary counts both members' workers: $(cat "$TMP/stop.log")"; ok

# 2 — single-repo stop is unchanged: no --repo flag (GH_REPO carries it), plain issue-<N>- branch.
S="$TMP/single"
mkdir -p "$S/.claude/super-board/configs" "$S/.claude/super-board/inflight"
printf '{"bot_identity":"bot","repo":{"remote":"https://github.com/acme/app"}}\n' > "$S/.claude/super-board/configs/app.json"
printf 'PID=999999993\nLANE=review\n' > "$S/.claude/super-board/inflight/12"
: > "$STOP_TRACE"
(cd "$S" && PATH="$TMP/bin:$PATH" bash "$ROOT/scripts/super-board-stop.sh" app > "$TMP/stop1.log" 2>&1) \
  || { cat "$TMP/stop1.log"; fail "single-repo stop failed"; }
grep -q '^gh issue comment 12 --body' "$STOP_TRACE" || fail "single-repo comment keeps its old shape"; ok
grep -q 'startswith(\\\?"issue-12-' "$STOP_TRACE" || grep -q 'startswith("issue-12-' "$STOP_TRACE" || fail "single-repo PR lookup keeps issue-<N>-"; ok
[ ! -e "$S/.claude/super-board/inflight/12" ] || fail "single-repo lock cleared"; ok

echo "PASS: test-stop-hub.sh ($PASS checks)"
