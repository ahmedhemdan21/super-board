#!/usr/bin/env bash
# super-board-qa-target.sh — what the Tester tests against, and the login it uses.
#
# The QA lane runs every step in a fresh shell, so nothing it exports survives to
# the next Bash call. This script is the one place that knows the targets: each
# call prints `export` lines the lane evals, and a local stack it started is
# remembered in a state file until `stop`.
#
# Usage:
#   super-board-qa-target.sh check --config <cfg>
#       Health-check the remote targets (config.target.url + config.target.urls).
#   super-board-qa-target.sh start --config <cfg> --issue <N> --worktree <dir>
#       config.qa.local set: lease a port block, start the app from <dir>, wait for
#       every qa.local.health URL. Absent: prints nothing, exits 0 (use `check`).
#   super-board-qa-target.sh stop  --config <cfg> --issue <N>
#       Stop what `start` started (qa.local.stop, then the whole process tree) and
#       free the port block. Idempotent: always safe, always exits 0.
#   super-board-qa-target.sh env   --config <cfg> [--issue <N>]
#       Print the targets as export lines: the running local stack for <N>, else
#       the remote targets. Use as:  eval "$(bash .claude/bin/super-board-qa-target.sh env ...)"
#   super-board-qa-target.sh auth  --config <cfg> [--issue <N>]
#       Run config.qa.auth_command with the targets in its env. Prints only where
#       the login landed (SB_AUTH_KIND / SB_AUTH_FILE / SB_AUTH_KEYS), never a value.
#
# Env the start command, the stop command, the auth command and the lane see:
#   SB_PORT_BASE   first port of this lane's block (qa.local only). The block is
#                  SB_PORT_BASE .. SB_PORT_BASE+port_stride-1, unique per running
#                  lane: parallel QA lanes never share a port.
#   SB_QA_ISSUE    the card number.
#   SB_TARGET_URL  the default target (target.url, else the first named one).
#   SB_TARGET_<NAME>  one per named target (target.urls key or qa.local.health
#                  name, upper-cased, non-alphanumerics → _).
#   BASE_URL       = SB_TARGET_URL, which super-qa reads.
# Placeholders in qa.local start/stop/health: {port} = SB_PORT_BASE, {port+K} = SB_PORT_BASE+K.
#
# Exits: 0 ok · 3 a remote target is unhealthy · 4 local stack never became
#        healthy (already stopped) · 5 no free port block · 6 auth_command failed
#        or printed neither JSON nor a file path · 64 bad args · 66 bad config
set -uo pipefail
# jq on Windows ends lines with CRLF; a stray \r in a URL fails every health check.
jqx() { jq "$@" | tr -d '\r'; }

CMD="${1:-}"; [ $# -gt 0 ] && shift
CONFIG=""; ISSUE=""; WORKTREE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --config)   CONFIG="$2"; shift 2 ;;
    --issue)    ISSUE="$2"; shift 2 ;;
    --worktree) WORKTREE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done
case "$CMD" in check|start|stop|env|auth) ;; *) echo "usage: $0 check|start|stop|env|auth --config <cfg> [--issue N] [--worktree DIR]" >&2; exit 64 ;; esac
[ -n "$CONFIG" ] && [ -r "$CONFIG" ] || { echo "config not found: ${CONFIG:-<unset>}" >&2; exit 66; }
CFG=$(cat "$CONFIG")
echo "$CFG" | jqx -e 'type == "object"' >/dev/null 2>&1 || { echo "config is not a JSON object: $CONFIG" >&2; exit 66; }
case "$CMD" in start|stop) [ -n "$ISSUE" ] || { echo "$CMD needs --issue" >&2; exit 64; } ;; esac
if [ -n "$ISSUE" ]; then case "$ISSUE" in *[!0-9]*) echo "--issue must be a number" >&2; exit 64 ;; esac; fi

# Port leases must be shared by every lane on this machine, so state lives in
# the git common dir (<common>/super-board/qa-local): every linked worktree sees
# the same directory, yet it is inside .git, never a checkout. Writing beside
# the main clone would drop files into the user's working tree when a lane runs
# from a linked worktree. Per-card pids, env files and logs sit with the leases.
state_root() {
  if [ -n "${SB_QA_STATE_DIR:-}" ]; then echo "$SB_QA_STATE_DIR"; return; fi
  local common
  common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) && [ -n "$common" ]     && { echo "$common/super-board/qa-local"; return; }
  echo "$PWD/.claude/super-board/qa-local"
}
STATE=$(state_root)
ensure_state() {
  mkdir -p "$STATE/ports" || exit 66
  [ -e "$STATE/.gitignore" ] || printf '*\n' > "$STATE/.gitignore"
}

POLL="${SB_QA_LOCAL_POLL:-3}"
LOCAL=$(echo "$CFG" | jqx -c '.qa.local // empty')
PORT_BASE=$(echo "$CFG" | jqx -r '.qa.local.port_base // 20000')
STRIDE=$(echo "$CFG" | jqx -r '.qa.local.port_stride // 10')
SLOTS=$(echo "$CFG" | jqx -r '.qa.local.port_slots // 50')
TIMEOUT=$(echo "$CFG" | jqx -r '.qa.local.timeout_s // 300')

envname() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | sed -E 's/[^A-Z0-9]+/_/g; s/^_+|_+$//g'; }
squote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
# subst <text> <base>: {port} and {port+K}.
subst() {
  jqx -rn --arg s "$1" --argjson b "$2" \
    '$s | gsub("\\{port\\+(?<k>[0-9]+)\\}"; ((.k | tonumber) + $b | tostring)) | gsub("\\{port\\}"; ($b | tostring))'
}
healthy() { curl -fsS -o /dev/null --max-time 5 "$1" >/dev/null 2>&1; }
http_code() { local c; c=$(curl -s -o /dev/null --max-time 5 -w '%{http_code}' "$1" 2>/dev/null); echo "${c:-000}"; }
alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
is_windows() { case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; esac; return 1; }

# remote_targets: name<TAB>url lines; the default first as URL.
remote_targets() {
  echo "$CFG" | jqx -r '
    (.target.url // null) as $u
    | ((.target.urls // {}) | to_entries | map(select(.value != null and .value != ""))) as $named
    | (if $u then [{key:"URL", value:$u}] elif ($named | length) > 0 then [{key:"URL", value:$named[0].value}] else [] end)
      + $named | .[] | "\(.key)\t\(.value)"'
}

print_exports() {  # stdin: name<TAB>url
  local name url key first="" has_default=0
  while IFS=$'\t' read -r name url; do
    [ -n "$url" ] || continue
    key="SB_TARGET_$(envname "$name")"
    echo "export ${key}=$(squote "$url")"
    [ "$key" = "SB_TARGET_URL" ] && has_default=1
    [ -n "$first" ] || first="$url"
  done
  if [ -n "$first" ]; then
    [ "$has_default" -eq 1 ] || echo "export SB_TARGET_URL=$(squote "$first")"
    echo "export BASE_URL=$(squote "$first")"
  fi
}

state_file() { echo "$STATE/issue-$1.json"; }

kill_tree() {  # <pid> <winpid>
  local pid="$1" winpid="${2:-}" i
  if is_windows; then
    # Native children (dotnet, node) are not in an msys process group: kill the tree by Windows PID.
    [ -n "$pid" ] && [ -r "/proc/$pid/winpid" ] && winpid="$winpid $(cat "/proc/$pid/winpid")"
    for w in $winpid; do taskkill //F //T //PID "$w" >/dev/null 2>&1 || true; done
  fi
  alive "$pid" || return 0
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  for i in $(seq 1 20); do alive "$pid" || return 0; sleep 0.5; done
  kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
  return 0
}

do_stop() {  # <issue>
  local f pid winpid base slot dir stop_cmd
  f=$(state_file "$1")
  [ -f "$f" ] || { echo "qa-local: issue #$1 has no running stack" >&2; return 0; }
  pid=$(jqx -r '.pid // empty' "$f"); winpid=$(jqx -r '.winpid // empty' "$f")
  base=$(jqx -r '.port_base' "$f"); slot=$(jqx -r '.slot' "$f"); dir=$(jqx -r '.worktree' "$f")
  stop_cmd=$(echo "$CFG" | jqx -r '.qa.local.stop // empty')
  if [ -n "$stop_cmd" ]; then
    stop_cmd=$(subst "$stop_cmd" "$base")
    ( cd "$dir" 2>/dev/null || cd "$PWD"; SB_PORT_BASE="$base" SB_QA_ISSUE="$1" bash -c "$stop_cmd" ) \
      >>"$STATE/issue-$1.log" 2>&1 || echo "qa-local: stop command failed for #$1; killing the process tree" >&2
  fi
  kill_tree "$pid" "$winpid"
  rm -rf "$STATE/ports/$slot" "$f" "$STATE/issue-$1.env"
  echo "qa-local: stopped #$1 (ports ${base}+${STRIDE})" >&2
}

# lease_slot <issue>: prints "slot base"; atomic mkdir per slot, stale leases reclaimed.
lease_slot() {
  local n="$1" i slot base owner opid
  for i in $(seq 0 $((SLOTS - 1))); do
    slot=$(( (n + i) % SLOTS )); base=$(( PORT_BASE + slot * STRIDE ))
    if ! mkdir "$STATE/ports/$slot" 2>/dev/null; then
      owner=$(cat "$STATE/ports/$slot/issue" 2>/dev/null || echo "")
      leaser=$(cat "$STATE/ports/$slot/leaser" 2>/dev/null || echo "")
      opid=$(jqx -r '.pid // empty' "$(state_file "${owner:-x}")" 2>/dev/null || echo "")
      # Live while the start that leased it is still running (health wait) or the
      # stack it started is; the instant between mkdir and the owner write is
      # covered by the age check. Anything else is a crashed lane's lease.
      if alive "$leaser" || alive "$opid"; then continue; fi
      if [ -z "$owner" ] && [ -n "$(find "$STATE/ports/$slot" -maxdepth 0 -mmin -1 2>/dev/null)" ]; then continue; fi
      rm -rf "$STATE/ports/$slot"; mkdir "$STATE/ports/$slot" 2>/dev/null || continue
    fi
    echo "$$" > "$STATE/ports/$slot/leaser"
    echo "$n" > "$STATE/ports/$slot/issue"
    # Something outside the board already answering on the block's first port.
    if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:${base}/" 2>/dev/null; then
      rm -rf "$STATE/ports/$slot"; continue
    fi
    echo "$slot $base"; return 0
  done
  return 1
}

local_exports() {  # <issue>: from the env file a start wrote
  local f="$STATE/issue-$1.env"
  [ -f "$f" ] && cat "$f"
}

case "$CMD" in
  check)
    TARGETS=$(remote_targets)
    [ -n "$TARGETS" ] || { echo "qa-target: no target.url / target.urls configured — nothing to check" >&2; exit 0; }
    BAD=0
    while IFS=$'\t' read -r name url; do
      [ -n "$url" ] || continue
      if healthy "$url"; then echo "ok ${name} ${url}"
      else echo "unhealthy ${name} ${url} (HTTP $(http_code "$url"))"; BAD=1; fi
    done <<< "$TARGETS"
    [ "$BAD" -eq 0 ] || exit 3
    exit 0 ;;

  env)
    if [ -n "$ISSUE" ] && [ -f "$STATE/issue-$ISSUE.env" ]; then local_exports "$ISSUE"; exit 0; fi
    remote_targets | print_exports
    exit 0 ;;

  stop)
    [ -d "$STATE" ] || exit 0
    do_stop "$ISSUE"; exit 0 ;;

  start)
    [ -n "$LOCAL" ] || { echo "qa-target: qa.local not configured — test target.url / target.urls (check)" >&2; exit 0; }
    START_CMD=$(echo "$CFG" | jqx -r '.qa.local.start // empty')
    [ -n "$START_CMD" ] || { echo "qa.local.start is required when qa.local is set" >&2; exit 66; }
    echo "$CFG" | jqx -e '(.qa.local.health // []) | type == "array" and length > 0 and all(.[]; (.url // "") != "")' >/dev/null \
      || { echo "qa.local.health needs at least one {name, url}" >&2; exit 66; }
    [ -n "$WORKTREE" ] && [ -d "$WORKTREE" ] || { echo "start needs --worktree <existing dir>" >&2; exit 64; }
    WORKTREE=$(cd "$WORKTREE" && pwd)
    ensure_state
    # A stack left by a crashed lane for this card is stopped before a new one starts.
    [ -f "$(state_file "$ISSUE")" ] && do_stop "$ISSUE"
    LEASE=$(lease_slot "$ISSUE") || { echo "qa-local: no free port block (${SLOTS} x ${STRIDE} from ${PORT_BASE})" >&2; exit 5; }
    SLOT=${LEASE% *}; BASE=${LEASE#* }
    CMDLINE=$(subst "$START_CMD" "$BASE")
    LOG="$STATE/issue-$ISSUE.log"; : > "$LOG"
    # Own process group (setsid, else job control) so stop can take the whole tree.
    if command -v setsid >/dev/null 2>&1 && ! is_windows; then
      ( cd "$WORKTREE" && SB_PORT_BASE="$BASE" SB_QA_ISSUE="$ISSUE" setsid bash -c "$CMDLINE" ) >>"$LOG" 2>&1 </dev/null &
      PID=$!
    else
      set -m
      ( cd "$WORKTREE" && SB_PORT_BASE="$BASE" SB_QA_ISSUE="$ISSUE" exec bash -c "$CMDLINE" ) >>"$LOG" 2>&1 </dev/null &
      PID=$!
      set +m
    fi
    WINPID=""; [ -r "/proc/$PID/winpid" ] && WINPID=$(cat "/proc/$PID/winpid" 2>/dev/null)
    jqx -n --argjson pid "$PID" --arg winpid "$WINPID" --argjson base "$BASE" --argjson slot "$SLOT" \
      --arg worktree "$WORKTREE" --argjson issue "$ISSUE" --arg started "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      '{issue:$issue, pid:$pid, winpid:$winpid, port_base:$base, slot:$slot, worktree:$worktree, started:$started}' \
      > "$(state_file "$ISSUE")"
    HEALTH=$(echo "$CFG" | jqx -r '.qa.local.health[] | "\(.name // "app")\t\(.url)"')
    RESOLVED=""
    while IFS=$'\t' read -r name url; do
      RESOLVED="${RESOLVED}${name}"$'\t'"$(subst "$url" "$BASE")"$'\n'
    done <<< "$HEALTH"
    DEADLINE=$(( $(date +%s) + TIMEOUT ))
    while :; do
      PENDING=""
      while IFS=$'\t' read -r name url; do
        [ -n "$url" ] || continue
        healthy "$url" || PENDING="${PENDING}${PENDING:+, }${name} ${url}"
      done <<< "$RESOLVED"
      [ -n "$PENDING" ] || break
      if ! alive "$PID"; then
        echo "qa-local: start command exited before ${PENDING} became healthy; log: $LOG" >&2
        do_stop "$ISSUE"; exit 4
      fi
      if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        echo "qa-local: not healthy after ${TIMEOUT}s: ${PENDING}; log: $LOG" >&2
        do_stop "$ISSUE"; exit 4
      fi
      sleep "$POLL"
    done
    {
      echo "export SB_QA_LOCAL=1"
      echo "export SB_QA_ISSUE=$ISSUE"
      echo "export SB_PORT_BASE=$BASE"
      printf '%s' "$RESOLVED" | print_exports
    } > "$STATE/issue-$ISSUE.env"
    echo "qa-local: #$ISSUE healthy on ports ${BASE}..$((BASE + STRIDE - 1)) (pid $PID, log $LOG)" >&2
    local_exports "$ISSUE"
    exit 0 ;;

  auth)
    AUTH_CMD=$(echo "$CFG" | jqx -r '.qa.auth_command // empty')
    [ -n "$AUTH_CMD" ] || { echo "qa-target: qa.auth_command not configured — use super-qa's default login" >&2; exit 0; }
    umask 077
    OUT=$(mktemp -d "${TMPDIR:-/tmp}/super-board-auth-${ISSUE:-x}-XXXXXX") || exit 66
    EXPORTS=$( if [ -n "$ISSUE" ] && [ -f "$STATE/issue-$ISSUE.env" ]; then local_exports "$ISSUE"; else remote_targets | print_exports; fi )
    # Output is captured to files and never echoed: it holds tokens.
    ( eval "$EXPORTS"; SB_QA_ISSUE="${ISSUE:-}" bash -c "$AUTH_CMD" ) >"$OUT/auth.out" 2>"$OUT/auth.err"
    RC=$?
    if [ "$RC" -ne 0 ]; then
      echo "qa-auth: auth_command failed (exit $RC); its stderr is in $OUT/auth.err (not printed: it may hold secrets)" >&2
      exit 6
    fi
    if jqx -e 'type == "object"' "$OUT/auth.out" >/dev/null 2>&1; then
      mv "$OUT/auth.out" "$OUT/auth.json"
      KEYS=$(jqx -r 'keys | join(",")' "$OUT/auth.json")
      if jqx -e 'has("cookies") and has("origins")' "$OUT/auth.json" >/dev/null; then KIND=storage-state; else KIND=json; fi
      FILE="$OUT/auth.json"
    else
      LINE=$(grep -v '^[[:space:]]*$' "$OUT/auth.out" | head -n 1 | tr -d '\r' | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')
      if [ -n "$LINE" ] && [ -f "$LINE" ]; then KIND=storage-state; FILE="$LINE"; KEYS=""
      else
        echo "qa-auth: auth_command printed neither a JSON object nor an existing file path (output not shown)" >&2
        exit 6
      fi
    fi
    echo "qa-auth: ${KIND} login ready in ${FILE}${KEYS:+ (keys: ${KEYS})}" >&2
    echo "export SB_AUTH_KIND=$(squote "$KIND")"
    echo "export SB_AUTH_FILE=$(squote "$FILE")"
    echo "export SB_AUTH_KEYS=$(squote "$KEYS")"
    exit 0 ;;
esac
