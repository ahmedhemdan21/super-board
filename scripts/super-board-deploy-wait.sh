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
#   deploy.regression_watch  optional {enabled, window_s, promql_error_rate,
#                      promql_request_rate, min_ratio, min_abs, loki_error_ratio,
#                      revert_card}: error RATE after vs before the deploy
#                      (collect_prometheus.py check) → regression; waits until
#                      deploy + window_s has passed, bounded by timeout_s
#
# Any post-deploy regression (telemetry_regression or regression) files the bug
# with label `regression` and fingerprint `regression|pr-<N>`: one open
# regression bug per merged PR. revert_card: true also files a Backlog card
# "Revert #<pr>" that names the bug. Nothing here reverts or pushes.
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
# Every gh call names its repo: the caller's cwd may be another member's checkout.
REPO=$(echo "$CFG" | jqx -r '.repo.remote // ""' | tr -d '\r' | sed -E 's#^(https?://github\.com/|git@github\.com:)##; s#\.git$##')
[ -z "$REPO" ] || export SUPER_QA_REPO="${SUPER_QA_REPO:-$REPO}"  # the bug filer's repo, not the cwd's
[ -n "$ISSUE" ] || ISSUE=""

# gh quota discipline (rate-limit-etiquette.md): the guard before the burst.
# shellcheck source=/dev/null
. "$HERE/super-board-gh-guard.sh"
guard() { sb_gh_guard_check 200 || exit $?; }

python3 "$GITHUB_READ" --check || exit $?
guard

# The merge commit, and whether this PR merged into the deploy branch at all.
PRJSON=$(python3 "$GITHUB_READ" --kind merge-state -- pr view "$PR" ${REPO:+--repo "$REPO"} --json state,headRefOid,mergeCommit,baseRefName,url) || exit $?
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
DEPLOY_TS=""; SMOKE_OK=""
if [ -f "$SFILE" ]; then STARTED=$(jqx -r '.started' "$SFILE"); RUN_ID=$(jqx -r '.run_id // empty' "$SFILE")
  DEPLOY_TS=$(jqx -r '.deploy_ts // empty' "$SFILE"); SMOKE_OK=$(jqx -r '.smoke_ok // empty' "$SFILE")
else STARTED=$NOW; RUN_ID=""; fi
# deploy_ts: when the run finished, kept so a --budget-s re-call measures the
# regression-watch window from the deploy, not from the re-call. smoke_ok: the
# smoke already passed for this sha; a re-call while the watch waits skips it.
save_state() { jqx -n --argjson s "$STARTED" --arg r "$RUN_ID" --arg d "$DEPLOY_TS" --arg k "$SMOKE_OK" '{started:$s, run_id:$r, deploy_ts:$d, smoke_ok:$k}' > "$SFILE"; }
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
    RUNS=$(python3 "$GITHUB_READ" --kind array -- run list ${REPO:+--repo "$REPO"} --workflow "$WORKFLOW" --commit "$SHA" --json databaseId,status,conclusion --limit 5) || exit $?
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
    RUN=$(python3 "$GITHUB_READ" --kind object -- run view "$RUN_ID" ${REPO:+--repo "$REPO"} --json status,conclusion,url) || exit $?
    RUN_STATUS=$(echo "$RUN" | jqx -r '.status // empty'); RUN_CONCLUSION=$(echo "$RUN" | jqx -r '.conclusion // empty')
    RUN_URL=$(echo "$RUN" | jqx -r '.url // empty')
    if [ "$RUN_STATUS" = "completed" ]; then
      [ -n "$DEPLOY_TS" ] || { DEPLOY_TS=$(date +%s); save_state; }
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
if [ -z "$REASON" ] && [ -n "$SMOKE" ] && [ -z "$SMOKE_OK" ]; then
  SMOKE_LOG="$SDIR/$SHA-smoke.log"
  if ! ( cd "$SB_REPO_PATH" && env "${TARGET_ENV[@]}" SB_DEPLOY_SHA="$SHA" SB_DEPLOY_RUN_ID="$RUN_ID" bash -c "$SMOKE" ) >"$SMOKE_LOG" 2>&1; then
    fail smoke-failed "deploy.smoke exited non-zero; last lines: $(tail -n 5 "$SMOKE_LOG" | tr '\n' ' ' | cut -c1-300)"
  else
    SMOKE_OK=1; save_state
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
  TEL_OUT="$out"
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
#    regression watch (deploy.regression_watch, enabled: true): the error RATE
#    in [deploy, deploy+window_s] against [deploy-window_s, deploy], from
#    Prometheus (and Loki error-line counts when collect.loki is set), through
#    `collect_prometheus.py check`. It first waits until deploy+window_s has
#    passed (nap: a --budget-s call exits 75 and the next call resumes; the
#    deploy time is in the state file), bounded by timeout_s — at the deadline
#    it checks the window it has (the output says clipped). Regression only
#    when the ratio AND the absolute floor are both exceeded. No data →
#    "watch: inconclusive" (a warning, never ok). Unreachable → a warning.
#    WATCH (ok|inconclusive|unavailable|skipped|regression) goes on the summary line.
PROM_PY="${SB_COLLECT_PROM:-$HERE/../skills/super-collect/scripts/collect_prometheus.py}"
WATCH=skipped; WATCH_OUT=""
watch_enabled() { [ "$(echo "$DEPLOY" | jqx -r '.regression_watch.enabled // false')" = "true" ]; }
regression_watch_wait() {
  watch_enabled || return 0
  local win target now
  win=$(echo "$DEPLOY" | jqx -r '.regression_watch.window_s // 1800')
  target=$(( ${DEPLOY_TS:-$(date +%s)} + win ))
  DELAY="$POLL"
  while now=$(date +%s); [ "$now" -lt "$target" ]; do
    # Sleep at most up to the target; nap ends the call (75) or the wait (deadline).
    [ $(( target - now )) -lt "$DELAY" ] && DELAY=$(( target - now ))
    nap || { echo "watch: timeout_s reached before the ${win}s window closed; checking the window so far" >&2; break; }
  done
}
regression_watch_check() {
  watch_enabled || { echo "watch: skipped (deploy.regression_watch not enabled)"; return 0; }
  local out rc st
  out=$(python3 "$PROM_PY" --config "$CONFIG" check --since-epoch "${DEPLOY_TS:-$(date +%s)}" 2>&1); rc=$?
  WATCH_OUT="$out"
  st=$(echo "$out" | jqx -r '.status // empty' 2>/dev/null)
  if [ "$rc" -ne 0 ] || [ -z "$st" ] || [ "$st" = "unavailable" ]; then
    WATCH=unavailable
    echo "watch: warning — regression watch unavailable, not failing the deploy: $(echo "$out" | jqx -r '.error // .summary // empty' 2>/dev/null || echo "$out" | tail -n 1)" >&2
    return 0
  fi
  if [ "$(echo "$out" | jqx -r '.regression')" = "true" ]; then
    WATCH=regression
    local why; why=$(echo "$out" | jqx -r '"error rate up within \(.windowS)s of the deploy: \(.summary)"' | cut -c1-600)
    if [ "$REASON" = telemetry_regression ]; then DETAIL="$why; $DETAIL"; REASON=regression
    else fail regression "$why"; fi
    return 0
  fi
  if [ "$st" = "inconclusive" ]; then
    WATCH=inconclusive
    echo "watch: WARNING — no data to compare: $(echo "$out" | jqx -r '.summary') ; health unknown, not failing the deploy" >&2
    echo "watch: inconclusive (no data)"
    return 0
  fi
  WATCH=ok
  echo "watch: ok ($(echo "$out" | jqx -r '.summary'))"
}
post_deploy_checks() {
  regression_watch_wait
  telemetry_check
  regression_watch_check
}
[ -n "$REASON" ] || post_deploy_checks

if [ -z "$REASON" ]; then
  rm -f "$SFILE"
  echo "deploy: healthy sha=$SHORT run=$RUN_ID branch=$BRANCH telemetry=${TELEMETRY:-skipped} watch=$WATCH${RUN_URL:+ $RUN_URL}"
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

# A post-deploy regression (rate watch or new Loki groups) carries the numbers
# of both windows and the top new Loki error groups, and dedupes per merged PR.
IS_REGRESSION=0; WATCH_MD=""; FP="deploy|$WORKFLOW|pr-$PR|$REASON"; EXTRA_LABELS=()
case "$REASON" in regression|telemetry_regression) IS_REGRESSION=1; FP="regression|pr-$PR"; EXTRA_LABELS=(--label regression) ;; esac
clean() { tr -d '<>`'; }  # markdown tables stay; markup the ticket checks read as placeholders goes
if [ "$IS_REGRESSION" -eq 1 ]; then
  if [ -n "$WATCH_OUT" ] && echo "$WATCH_OUT" | jq -e . >/dev/null 2>&1; then
    WATCH_MD=$(echo "$WATCH_OUT" | jqx -r '
      def n: if . == null then "n/a" else tostring end;
      "| Window | Prometheus errors/s | Prometheus requests/s | Loki error lines |",
      "|---|---|---|---|",
      "| before (\(.before.start) to \(.before.end)) | \(.prometheus.before.errorRate|n) | \(.prometheus.before.requestRate|n) | \(.loki.errorsBefore|n) |",
      "| after (\(.after.start) to \(.after.end)) | \(.prometheus.after.errorRate|n) | \(.prometheus.after.requestRate|n) | \(.loki.errorsAfter|n) |",
      "",
      "Thresholds: ratio ≥ \(.minRatio) (Loki ≥ \(.lokiErrorRatio)) and ≥ \(.minAbs) errors/s after. Window \(.windowS)s\(if .clipped then " (clipped by deploy.timeout_s)" else "" end).",
      "Result: \(.summary)"' | clean)
  fi
  # Top new Loki error groups: the telemetry check's output, else one Loki check now.
  if [ -z "${TEL_OUT:-}" ] && [ -n "$(echo "$CFG" | jqx -r '.collect.loki.url // empty')" ]; then
    TEL_OUT=$(python3 "$LOKI_PY" --config "$CONFIG" check --since-epoch "${DEPLOY_TS:-$(date +%s)}" \
      --window-s "$(echo "$DEPLOY" | jqx -r '.regression_watch.window_s // 1800')" --release "$SHA" 2>/dev/null) || TEL_OUT=""
  fi
  GROUPS_MD=$(echo "${TEL_OUT:-}" | jqx -r '(.newGroups // [])[:5][] | "- \(.group): \(.signature[:120]) (\(.sampleCount)x)"' 2>/dev/null | clean)
  WATCH_MD="${WATCH_MD:-No rate numbers: the regression came from the Loki group check.}

Top new Loki error groups after the deploy:
${GROUPS_MD:-- none found (Loki not configured, unreachable, or no new groups)}"
fi
WATCH_SECTION=""
[ "$IS_REGRESSION" -eq 0 ] || WATCH_SECTION="## Regression watch
$WATCH_MD
"
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

$WATCH_SECTION
## Fix
Find why the merged change broke the dev deploy (workflow run $RUN_REF), fix it forward on a new branch, and keep PR #$PR merged.

## Acceptance Criteria
- [ ] The \`$WORKFLOW\` run for the fix commit succeeds
- [ ] Every deploy health URL answers 2xx with the fix commit's version
- [ ] The deploy smoke command passes$( [ "$REASON" = telemetry_regression ] && printf '\n- [ ] No new error groups in Loki in the %s window after the fix deploys' "deploy.telemetry" )$( [ "$REASON" = regression ] && printf '\n- [ ] The %s error rate after the fix deploys stays under the thresholds' "deploy.regression_watch" )

## Risk
🟡 **Medium** · dev is broken or stale until this lands; production is untouched.
MD
BUG=$(bash "$FILE_BUG_SH" --title "dev deploy check failed after PR #$PR: $REASON" --body-file "$BODY" \
  --kind bug --priority high --category functional --area deploy \
  --fingerprint "$FP" --suggested-skill super-build ${EXTRA_LABELS[@]+"${EXTRA_LABELS[@]}"})
RC=$?
if [ "$RC" -eq 79 ]; then exit 79; fi
if [ -n "$BUG" ] && { [ "$RC" -eq 0 ] || [ "$RC" -eq 71 ]; }; then
  echo "deploy: failed reason=$REASON sha=$SHORT bug: #$BUG"
  # Optional revert card: a Backlog ticket a human may pick. Never a revert.
  if [ "$IS_REGRESSION" -eq 1 ] && [ "$(echo "$DEPLOY" | jqx -r '.regression_watch.revert_card // false')" = "true" ]; then
    REVERT_BODY=$(mktemp); trap 'rm -f "$BODY" "$REVERT_BODY"' EXIT
    cat > "$REVERT_BODY" <<MD
## Problem
PR #$PR is the suspected cause of regression bug #$BUG on dev. Reverting it is one way out; fixing forward (#$BUG) is the other.

## Context
- **Where:** \`$BRANCH\` at commit \`$SHORT\`, deployed by run $RUN_REF
  - a. PR #$PR merged as \`$SHA\`
  - b. The post-deploy regression watch filed bug #$BUG
  - c. This card waits in Backlog until a human decides to revert
- **Original card:** $REF_CARD · **PR:** ${PR_URL:-#$PR} · **Bug:** #$BUG

## Evidence
<details><summary>revert candidate · PR #$PR · bug #$BUG</summary>

| Row | Value |
|---|---|
| Error + stack | n/a — see bug #$BUG |
| Request / trace ID | n/a — see bug #$BUG |
| Sentry | n/a — see bug #$BUG |
| PostHog replay | n/a — no user session involved |
| Logs | see bug #$BUG |
| Screenshots | n/a — no UI step |
| HAR / API sample | n/a — rate check only |
| Env + release | dev · \`$SHA\` |
| First / last seen | $(date -u +%Y-%m-%dT%H:%MZ) |
| Users affected | n/a — dev environment |
| Steps | merge PR #$PR, deploy, watch the error rate |
| Expected / actual | error rate unchanged / $REASON |

</details>

## Fix
If a human chooses to revert: open a revert PR of PR #$PR on a new branch. Nothing is reverted automatically.

## Acceptance Criteria
- [ ] A human decided between this revert and the fix in #$BUG
- [ ] If reverted, the error rate after the revert deploys is back under the thresholds

## Risk
🟡 **Medium** · a revert drops the feature from PR #$PR; decide against bug #$BUG.
MD
    REVERT=$(SUPER_QA_TARGET_OPTION_NAME=Backlog bash "$FILE_BUG_SH" --title "Revert #$PR (regression #$BUG)" \
      --body-file "$REVERT_BODY" --kind bug --priority medium --area deploy \
      --fingerprint "revert|pr-$PR" --label regression --label revert); RRC=$?
    if [ "$RRC" -eq 79 ]; then exit 79; fi
    [ -n "$REVERT" ] && echo "deploy: revert card #$REVERT (Backlog) for PR #$PR" \
      || echo "deploy: warning — revert card not filed (exit $RRC)" >&2
  fi
  exit 10
fi
echo "deploy: failed reason=$REASON sha=$SHORT (bug filing failed, exit $RC)"
exit 11
