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
# Stdout, one line per key:   KEY present | KEY empty | KEY missing | KEY unreadable <file>
# (appsettings JSONC comments and trailing commas are fine; a file that still will not
# parse is "unreadable", never "missing").
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

def strip_jsonc(text):
    """Drop // and /* */ comments outside strings, then trailing commas."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if ch == '"':
                in_str = False
            i += 1
        elif ch == '"':
            in_str = True; out.append(ch); i += 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch); i += 1
    s = "".join(out)
    # trailing commas: only outside strings, so walk again
    res, in_str, i = [], False, 0
    while i < len(s):
        ch = s[i]
        if in_str:
            res.append(ch)
            if ch == "\\" and i + 1 < len(s):
                res.append(s[i + 1]); i += 2; continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True; res.append(ch)
        elif ch == ",":
            j = i + 1
            while j < len(s) and s[j] in " \t\r\n":
                j += 1
            if j < len(s) and s[j] in "}]":
                i += 1; continue
            res.append(ch)
        else:
            res.append(ch)
        i += 1
    return "".join(res)

try:
    node = json.loads(strip_jsonc(open(sys.argv[1], encoding="utf-8-sig").read()))
except Exception:
    print("unreadable"); sys.exit(0)
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
    # JSONC that still will not parse: say so (file name only), never "missing"
    [ "$s" = unreadable ] && { [ "$state" = empty ] || state="unreadable $f"; }
    [ "$s" = empty ] && state=empty
  done
  echo "$k $state"
  [ "$state" = present ] || rc=1
done
exit "$rc"
