#!/usr/bin/env bash
# super-board-env-check.sh — say which env keys exist, never what they hold.
#
# Onboarding needs to know whether SENTRY_AUTH_TOKEN, POSTHOG_PERSONAL_API_KEY
# or a migration DB URL is set. The guard-secrets hook (rightly) denies `grep`,
# `cat` or `source` on a dotenv file, so the old `grep -c '^NAME=' .env` check
# was blocked on every install that had the guard. This script reads the file
# itself and prints one word per key — present, empty or missing — so no value
# ever reaches stdout, stderr or the transcript. guard-secrets allows a plain
# call to it (see hooks/guard-secrets.py → ENV_CHECK).
#
# Usage:
#   super-board-env-check.sh [--file <path>]... [--config <config.json>] KEY [KEY ...]
#
# --file may repeat; each is a dotenv file or an appsettings*.json (any *.json).
# --config adds the config's env_check.files (relative to the current dir).
# With neither, the first that exists of .env.local, .env (in the CWD).
# A key is checked in every file in order: present anywhere wins, then empty.
# Dotenv: accepts `KEY=…` and `export KEY=…`; a commented line does not count.
# JSON: a key names a nested path, .NET style — ConnectionStrings:Default or
# ConnectionStrings__Default. In a dotenv file Section:Key is read as Section__Key.
#
# Stdout, one line per key:   KEY present | KEY empty | KEY missing
# Exit: 0 every key present · 1 at least one empty or missing · 64 usage
set -euo pipefail

FILES=()
KEYS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --file) FILES+=("${2:-}"); shift 2 ;;
    --config)
      [ -f "${2:-}" ] || { echo "config not found: ${2:-}" >&2; exit 64; }
      while IFS= read -r f; do f=${f%$'\r'}; [ -n "$f" ] && FILES+=("$f"); done \
        < <(python3 -c 'import json,sys
for f in (json.load(open(sys.argv[1], encoding="utf-8")).get("env_check") or {}).get("files") or []: print(f)' "$2")
      shift 2 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 64 ;;
    *) KEYS+=("$1"); shift ;;
  esac
done
[ "${#KEYS[@]}" -gt 0 ] || { echo "usage: $0 [--file <path>]... [--config <cfg>] KEY [KEY ...]" >&2; exit 64; }

if [ "${#FILES[@]}" -eq 0 ]; then
  for f in .env.local .env; do [ -f "$f" ] && { FILES+=("$f"); break; }; done
fi

# state_in <file> <key> — one word, nothing from the value itself.
state_in() {
  local file=$1 k=$2
  [ -f "$file" ] || { echo missing; return; }
  case "$file" in
    *.json)
      python3 - "$file" "$k" <<'PY'
import json, re, sys
try:
    node = json.load(open(sys.argv[1], encoding="utf-8-sig"))
except Exception:
    print("missing"); sys.exit(0)
for part in re.split(r":|__", sys.argv[2]):
    if not isinstance(node, dict):
        print("missing"); sys.exit(0)
    hit = next((v for n, v in node.items() if n.lower() == part.lower()), KeyError)
    if hit is KeyError:
        print("missing"); sys.exit(0)
    node = hit
print("empty" if node is None or (isinstance(node, str) and not node.strip()) or node in ({}, []) else "present")
PY
      ;;
    *)
      awk -v k="${k//:/__}" '
        { sub(/^[ \t]+/, ""); sub(/^export[ \t]+/, "") }
        index($0, k "=") == 1 {
          v = substr($0, length(k) + 2); gsub(/^["'\'' \t]+|["'\'' \t\r]+$/, "", v)
          s = (v == "") ? "empty" : "present"
        }
        END { print (s == "" ? "missing" : s) }' "$file" ;;
  esac
}

rc=0
for k in "${KEYS[@]}"; do
  case "$k" in
    *[!A-Za-z0-9_:]*|""|:*|*:) echo "invalid key name: $k" >&2; exit 64 ;;
  esac
  state=missing
  for f in ${FILES[@]+"${FILES[@]}"}; do
    s=$(state_in "$f" "$k"); s=${s%$'\r'}
    if [ "$s" = present ]; then state=present; break; fi
    [ "$s" = empty ] && state=empty
  done
  echo "$k $state"
  [ "$state" = present ] || rc=1
done
exit "$rc"
