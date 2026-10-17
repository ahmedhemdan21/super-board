#!/usr/bin/env bash
# super-board-deploy-wait.sh — the post-merge dev check (QA pass two).
#
# Pre-merge QA proves the PR on a local stack. This proves the merge on the dev
# environment: after the gate squash-merges a card's PR into config.deploy.branch,
# wait for the config.deploy.workflow GitHub Actions run of that merge commit,
# check every deploy.health URL (and the deployed version, when asked), run the
# optional smoke command, then the post-deploy checks hook.
#
# A failure never reverts or blocks the merged PR: the card stays Done, and the
# failure is filed as a new bug card (super-qa-file-bug.sh) that names the
# original card and PR.
#
# Usage:
#   super-board-deploy-wait.sh --config <cfg> --pr <N> [--issue <N>] [--sha <merge sha>]
#                              [--budget-s <n>] [--no-file-bug]
#
#   --budget-s  stop after <n> seconds of waiting with exit 75 and call again;
#               the overall deploy.timeout_s clock is kept in a state file, so a
#               caller whose shell call is capped (a lane's Bash tool: 10 min)
#               loops on 75 without restarting the timeout.
#
# Config (config-schema.json → deploy):
#   deploy.branch      the branch whose merges deploy (absent → base_branch)
#   deploy.workflow    workflow file name, e.g. deploy-dev.yml
#   deploy.timeout_s   whole wait, run + health + smoke (default 1800)
#   deploy.health      [{name, url, expect_version?}]; expect_version is a string
#                      the body must contain, {sha} / {short_sha} substituted, or
#                      true = must contain {short_sha}
#   deploy.smoke       optional command; env SB_DEPLOY_SHA, SB_DEPLOY_RUN_ID,
#                      SB_TARGET_<NAME> per health entry, SB_TARGET_URL
#   deploy.telemetry   optional {source:"loki", window_s, max_new_error_groups,
#                      selector_extra, release_label}: new Loki error groups after
#                      the deploy → telemetry_regression (collect.loki = where)
#
# Stdout, last line: `deploy: healthy|skipped|failed …` (+ `bug: #<n>` on a filed bug).
# Exits: 0 healthy or skipped (not configured, PR not into deploy.branch)
#        10 deploy check failed, bug filed · 11 failed, bug NOT filed (see stderr)
#        75 budget spent, still waiting — call again · 64 bad args · 66 bad config
#        79 GitHub read halt (super-board-github-read.py) — stop the run
set -uo pipefail
jqx() { jq "$@" | tr -d '\r'; }

CONFIG=""; PR=""; ISSUE=""; SHA=""; BUDGET=0; FILE_BUG=1
while [ $# -gt 0 ]; do
  case "$1" in
    --config)      CONFIG="$2"; shift 2 ;;
    --pr)          PR="$2"; shift 2 ;;
    --issue)       ISSUE="$2"; shift 2 ;;
    --sha)         SHA="$2"; shift 2 ;;
    --budget-s)    BUDGET="$2"; shift 2 ;;
    --no-file-bug) FILE_BUG=0; shift ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done
[ -n "$CONFIG" ] && [ -r "$CONFIG" ] || { echo "config not found: ${CONFIG:-<unset>}" >&2; exit 66; }
case "$PR" in ''|*[!0-9]*) echo "--pr <number> is required" >&2; exit 64 ;; esac
case "$BUDGET" in *[!0-9]*) echo "--budget-s must be a number" >&2; exit 64 ;; esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GITHUB_READ="$HERE/super-board-github-read.py"
FILE_BUG_SH="${SB_FILE_BUG:-$HERE/super-qa-file-bug.sh}"
CFG=$(cat "$CONFIG")
DEPLOY=$(echo "$CFG" | jqx -c '.deploy // empty')
if [ -z "$DEPLOY" ] || [ "$DEPLOY" = "null" ]; then
  echo "deploy: skipped (no deploy block in config)"; exit 0
fi
WORKFLOW=$(echo "$DEPLOY" | jqx -r '.workflow // empty')
[ -n "$WORKFLOW" ] || { echo "deploy.workflow is required when deploy is set" >&2; exit 66; }
BRANCH=$(echo "$CFG" | jqx -r '.deploy.branch // .base_branch // "main"')
TIMEOUT=$(echo "$DEPLOY" | jqx -r '.timeout_s // 1800')
SMOKE=$(echo "$DEPLOY" | jqx -r '.smoke // empty')
POLL="${SB_DEPLOY_POLL:-10}"          # first poll interval, seconds
POLL_MAX="${SB_DEPLOY_POLL_MAX:-60}"  # backoff ceiling
export SB_REPO_PATH="${SB_REPO_PATH:-$(echo "$CFG" | jqx -r '.repo.path // "."')}"
[ -n "$ISSUE" ] || ISSUE=""

# gh quota discipline (rate-limit-etiquette.md): the guard before the burst.
# shellcheck source=/dev/null
. "$HERE/super-board-gh-guard.sh"
guard() { sb_gh_guard_check 200 || exit $?; }

python3 "$GITHUB_READ" --check || exit $?
guard

# The merge commit, and whether this PR merged into the deploy branch at all.
PRJSON=$(python3 "$GITHUB_READ" --kind merge-state -- pr view "$PR" --json state,headRefOid,mergeCommit,baseRefName,url) || exit $?
STATE_PR=$(echo "$PRJSON" | jqx -r '.state')
[ "$STATE_PR" = "MERGED" ] || { echo "deploy: skipped (PR #$PR is $STATE_PR, not merged)"; exit 0; }
BASE_REF=$(echo "$PRJSON" | jqx -r '.baseRefName // empty')
if [ -n "$BASE_REF" ] && [ "$BASE_REF" != "$BRANCH" ]; then
  echo "deploy: skipped (PR #$PR merged into $BASE_REF, deploy.branch is $BRANCH)"; exit 0
fi
[ -n "$SHA" ] || SHA=$(echo "$PRJSON" | jqx -r '.mergeCommit.oid')
PR_URL=$(echo "$PRJSON" | jqx -r '.url // empty')
SHORT=${SHA:0:7}

# Overall clock, kept across --budget-s calls.
# Board state lives under the configured repo.path, like merge-gate's
# REPO_PATH/.claude/super-board/inflight. Use SB_REPO_PATH itself, not the
# git common dir: hopping to the main clone wrote into the user's checkout
# when repo.path (or the caller) was a linked worktree.
state_dir() {
  if [ -n "${SB_DEPLOY_STATE_DIR:-}" ]; then echo "$SB_DEPLOY_STATE_DIR"; return; fi
  local top
  top=$(cd "$SB_REPO_PATH" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null) && [ -n "$top" ]     && { echo "$top/.claude/super-board/deploy"; return; }
  echo "$PWD/.claude/super-board/deploy"
}
SDIR=$(state_dir); mkdir -p "$SDIR"; [ -e "$SDIR/.gitignore" ] || printf '*\n' > "$SDIR/.gitignore"
SFILE="$SDIR/$SHA.json"
NOW=$(date +%s)
if [ -f "$SFILE" ]; then STARTED=$(jqx -r '.started' "$SFILE"); RUN_ID=$(jqx -r '.run_id // empty' "$SFILE")
else STARTED=$NOW; RUN_ID=""; fi
save_state() { jqx -n --argjson s "$STARTED" --arg r "$RUN_ID" '{started:$s, run_id:$r}' > "$SFILE"; }
save_state
DEADLINE=$(( STARTED + TIMEOUT ))
CALL_END=0; [ "$BUDGET" -gt 0 ] && CALL_END=$(( NOW + BUDGET ))

# nap: sleep the backoff, or end the call (75) / the wait (timeout) first.
DELAY="$POLL"
nap() {
  local now; now=$(date +%s)
  [ "$now" -lt "$DEADLINE" ] || return 1
  if [ "$CALL_END" -gt 0 ] && [ $(( now + DELAY )) -gt "$CALL_END" ]; then
    echo "deploy: waiting (budget spent; run $( [ -n "$RUN_ID" ] && echo "$RUN_ID" || echo 'not started' ), $(( DEADLINE - now ))s left) — call again"
    exit 75
  fi
  sleep "$DELAY"
  DELAY=$(awk -v d="$DELAY" -v m="$POLL_MAX" 'BEGIN { n = d * 1.5; if (n > m) n = m; printf "%d", (n < 1 ? 1 : n) }')
}

REASON=""; DETAIL=""; POLLS=0
fail() { REASON="$1"; DETAIL="$2"; }

# 1. The workflow run for the merge commit. It can take a while to appear.
if [ -z "$RUN_ID" ]; then
  while :; do
    POLLS=$((POLLS + 1)); [ $((POLLS % 5)) -eq 0 ] && guard
    RUNS=$(python3 "$GITHUB_READ" --kind array -- run list --workflow "$WORKFLOW" --commit "$SHA" --json databaseId,status,conclusion --limit 5) || exit $?
    RUN_ID=$(echo "$RUNS" | jqx -r 'first | .databaseId // empty')
    [ -z "$RUN_ID" ] || { save_state; break; }
    nap || { fail timeout "no $WORKFLOW run for $SHORT appeared within ${TIMEOUT}s"; break; }
  done
fi

# 2. Wait for it to finish.
RUN_STATUS=""; RUN_CONCLUSION=""
if [ -z "$REASON" ]; then
  DELAY="$POLL"
  while :; do
    POLLS=$((POLLS + 1)); [ $((POLLS % 5)) -eq 0 ] && guard
    RUN=$(python3 "$GITHUB_READ" --kind object -- run view "$RUN_ID" --json status,conclusion,url) || exit $?
    RUN_STATUS=$(echo "$RUN" | jqx -r '.status // empty'); RUN_CONCLUSION=$(echo "$RUN" | jqx -r '.conclusion // empty')
    RUN_URL=$(echo "$RUN" | jqx -r '.url // empty')
    if [ "$RUN_STATUS" = "completed" ]; then
      DEPLOY_TS=$(date +%s)
      [ "$RUN_CONCLUSION" = "success" ] || fail deploy-failed "$WORKFLOW run $RUN_ID concluded ${RUN_CONCLUSION:-unknown}"
      break
    fi
    nap || { fail timeout "$WORKFLOW run $RUN_ID still ${RUN_STATUS:-unknown} after ${TIMEOUT}s"; break; }
  done
fi
RUN_URL="${RUN_URL:-}"

# 3. Health (and the deployed version). A fresh deploy may need a moment to roll.
HEALTH_LINES=$(echo "$DEPLOY" | jqx -r '(.health // [])[] | [(.name // "app"), .url, (if .expect_version == true then "{short_sha}" elif (.expect_version | type) == "string" then .expect_version else "" end)] | @tsv')
TARGET_ENV=()
envname() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | sed -E 's/[^A-Z0-9]+/_/g; s/^_+|_+$//g'; }
while IFS=$'\t' read -r name url _; do
  [ -n "$url" ] || continue
  TARGET_ENV+=("SB_TARGET_$(envname "$name")=$url")
  [ "${#TARGET_ENV[@]}" -gt 1 ] || TARGET_ENV+=("SB_TARGET_URL=$url")
done <<< "$HEALTH_LINES"

check_health() {  # prints the first problem; returns 1 on one
  local name url expect body code
  while IFS=$'\t' read -r name url expect; do
    [ -n "$url" ] || continue
    body=$(curl -sS --max-time 10 -w $'\n%{http_code}' "$url" 2>/dev/null) || { echo "$name $url unreachable"; return 1; }
    code=${body##*$'\n'}; body=${body%$'\n'*}
    case "$code" in 2??) ;; *) echo "$name $url returned HTTP $code"; return 1 ;; esac
    if [ -n "$expect" ]; then
      expect=${expect//\{sha\}/$SHA}; expect=${expect//\{short_sha\}/$SHORT}
      case "$body" in *"$expect"*) ;; *) echo "$name $url does not report version $expect"; return 1 ;; esac
    fi
  done <<< "$HEALTH_LINES"
}
if [ -z "$REASON" ] && [ -n "$HEALTH_LINES" ]; then
  DELAY="$POLL"; HEALTH_END=$(( $(date +%s) + ${SB_DEPLOY_HEALTH_GRACE:-120} ))
  while :; do
    PROBLEM=$(check_health) && break
    if [ "$(date +%s)" -ge "$HEALTH_END" ]; then fail unhealthy "$PROBLEM"; break; fi
    nap || { fail unhealthy "$PROBLEM"; break; }
  done
fi

# 4. Smoke.
if [ -z "$REASON" ] && [ -n "$SMOKE" ]; then
  SMOKE_LOG="$SDIR/$SHA-smoke.log"
  if ! ( cd "$SB_REPO_PATH" && env "${TARGET_ENV[@]}" SB_DEPLOY_SHA="$SHA" SB_DEPLOY_RUN_ID="$RUN_ID" bash -c "$SMOKE" ) >"$SMOKE_LOG" 2>&1; then
    fail smoke-failed "deploy.smoke exited non-zero; last lines: $(tail -n 5 "$SMOKE_LOG" | tr '\n' ' ' | cut -c1-300)"
  fi
fi

# 5. Post-deploy checks on the deployed build. Each check calls
#    `fail <reason> <detail>` and returns; a failure files the bug the same way
#    as the steps above.
#
#    telemetry (deploy.telemetry, source loki): error groups in Loki since the
#    deploy finished that were not there in the same-length window before it,
#    through `collect_loki.py check` (collect.loki holds url / selector /
#    transport). More than max_new_error_groups → telemetry_regression. Loki
#    unreachable or misconfigured → a warning line, never a failure. No log
#    line at all for the selector (status no_data) → "telemetry: unknown",
#    a WARNING, never "ok": zero errors out of zero logs proves nothing.
#    TELEMETRY (ok|unknown|unavailable|skipped|regression) goes on the summary line.
LOKI_PY="${SB_COLLECT_LOKI:-$HERE/../skills/super-collect/scripts/collect_loki.py}"
telemetry_check() {
  local tel src win maxn extra label out rc
  tel=$(echo "$DEPLOY" | jqx -c '.telemetry // empty')
  TELEMETRY=skipped
  if [ -z "$tel" ] || [ "$tel" = "null" ]; then echo "telemetry: skipped (no deploy.telemetry)"; return 0; fi
  src=$(echo "$tel" | jqx -r '.source // "loki"')
  [ "$src" = "loki" ] || { echo "telemetry: skipped (source $src is not supported; use loki)" >&2; return 0; }
  win=$(echo "$tel" | jqx -r '.window_s // 600'); maxn=$(echo "$tel" | jqx -r '.max_new_error_groups // 0')
  extra=$(echo "$tel" | jqx -r '.selector_extra // ""'); label=$(echo "$tel" | jqx -r '.release_label // empty')
  out=$(python3 "$LOKI_PY" --config "$CONFIG" check --since-epoch "${DEPLOY_TS:-$(date +%s)}" \
    --window-s "$win" --max-new "$maxn" --release "$SHA" ${label:+--release-label "$label"} --extra "$extra" 2>&1); rc=$?
  if [ "$rc" -eq 0 ] && [ "$(echo "$out" | jqx -r '.status // empty' 2>/dev/null)" = "no_data" ]; then
    TELEMETRY=unknown
    local sel; sel=$(echo "$out" | jqx -r '.selector // "the selector"')
    echo "telemetry: WARNING — no logs for $sel in the ${win}s window; health unknown, not failing the deploy" >&2
    echo "telemetry: unknown (no logs for $sel in window)"
    return 0
  fi
  if [ "$rc" -ne 0 ] || [ "$(echo "$out" | jqx -r '.status // empty' 2>/dev/null)" != "ok" ]; then
    TELEMETRY=unavailable
    echo "telemetry: warning — Loki check unavailable, not failing the deploy: $(echo "$out" | jqx -r '.error // empty' 2>/dev/null || echo "$out" | tail -n 1)" >&2
    return 0
  fi
  if [ "$(echo "$out" | jqx -r '.regression')" = "true" ]; then
    TELEMETRY=regression
    fail telemetry_regression "$(echo "$out" | jqx -r '"\(.new) new error group(s) in Loki within \(.windowS)s of the deploy (max \(.maxNew)): \(.summary)"' | cut -c1-600)"
    return 0
  fi
  TELEMETRY=ok
  echo "telemetry: ok ($(echo "$out" | jqx -r '"\(.after) error group(s) after, \(.new) new"'))"
}
post_deploy_checks() {
  telemetry_check
}
[ -n "$REASON" ] || post_deploy_checks

if [ -z "$REASON" ]; then
  rm -f "$SFILE"
  echo "deploy: healthy sha=$SHORT run=$RUN_ID branch=$BRANCH telemetry=${TELEMETRY:-skipped}${RUN_URL:+ $RUN_URL}"
  exit 0
fi
rm -f "$SFILE"
echo "deploy: failed reason=$REASON sha=$SHORT ${DETAIL}" >&2
[ "$FILE_BUG" -eq 1 ] || { echo "deploy: failed reason=$REASON sha=$SHORT (bug filing disabled)"; exit 11; }

# File the bug. The merged PR stays merged and its card stays Done.
# Tool output can hold markup the ticket checks read as placeholders.
DETAIL=$(printf '%s' "$DETAIL" | tr -d '<>`|' | sed 's/→/->/g')
BODY=$(mktemp); trap 'rm -f "$BODY"' EXIT
REF_CARD="${ISSUE:+#$ISSUE}"; REF_CARD="${REF_CARD:-n/a — no card number given}"
RUN_REF="${RUN_URL:-${RUN_ID:-n/a — no run started}}"
cat > "$BODY" <<MD
## Problem
After PR #$PR merged into \`$BRANCH\`, the dev deploy check failed: $REASON. $DETAIL

## Context
- **Where:** dev deploy of \`$BRANCH\` at commit \`$SHORT\`, workflow \`$WORKFLOW\`
  - a. PR #$PR (card $REF_CARD) merged as \`$SHA\`
  - b. The board waited for the \`$WORKFLOW\` run of that commit
  - c. It then checked the deploy health URLs and ran the smoke command
- **Original card:** $REF_CARD · **PR:** ${PR_URL:-#$PR} — merged and left in place; this card tracks the fix.

## Evidence
<details><summary>deploy check · $REASON · $SHORT</summary>

| Row | Value |
|---|---|
| Error + stack | n/a — the check reads deploy output, not app stacks |
| Request / trace ID | n/a — no request traced |
| Sentry | n/a — not read by the deploy check |
| PostHog replay | n/a — no user session involved |
| Logs | \`$REASON\`: $DETAIL |
| Screenshots | n/a — no UI step |
| HAR / API sample | n/a — health checks only |
| Env + release | dev · \`$SHA\` |
| First / last seen | $(date -u +%Y-%m-%dT%H:%MZ) |
| Users affected | n/a — dev environment |
| Steps | merge PR #$PR, wait for run $RUN_REF, check health, run smoke |
| Expected / actual | a green deploy that passes health and smoke / $REASON |

</details>

## Fix
Find why the merged change broke the dev deploy (workflow run $RUN_REF), fix it forward on a new branch, and keep PR #$PR merged.

## Acceptance Criteria
- [ ] The \`$WORKFLOW\` run for the fix commit succeeds
- [ ] Every deploy health URL answers 2xx with the fix commit's version
- [ ] The deploy smoke command passes$( [ "$REASON" = telemetry_regression ] && printf '\n- [ ] No new error groups in Loki in the %s window after the fix deploys' "deploy.telemetry" )

## Risk
🟡 **Medium** · dev is broken or stale until this lands; production is untouched.
MD
BUG=$(bash "$FILE_BUG_SH" --title "dev deploy check failed after PR #$PR: $REASON" --body-file "$BODY" \
  --kind bug --priority high --category functional --area deploy \
  --fingerprint "deploy|$WORKFLOW|pr-$PR|$REASON" --suggested-skill super-build)
RC=$?
if [ "$RC" -eq 79 ]; then exit 79; fi
if [ -n "$BUG" ] && { [ "$RC" -eq 0 ] || [ "$RC" -eq 71 ]; }; then
  echo "deploy: failed reason=$REASON sha=$SHORT bug: #$BUG"
  exit 10
fi
echo "deploy: failed reason=$REASON sha=$SHORT (bug filing failed, exit $RC)"
exit 11
