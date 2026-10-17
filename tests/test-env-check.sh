#!/usr/bin/env bash
# Tests super-board-env-check.sh — key names in, present/empty/missing out, and
# never a byte of any value on stdout or stderr.
set -euo pipefail
cd "$(dirname "$0")"
EC="$PWD/../scripts/super-board-env-check.sh"
fail() { echo "FAIL: $1" >&2; exit 1; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
SECRET="sk_$(printf 'live')_$(printf 'abc123')"
D=.env
printf 'A=%s\nexport B="%s"\nC=\n# DCOMMENT=%s\n  E = x\n' "$SECRET" "$SECRET" "$SECRET" > "$T/$D"

# 1 — states per key, exit 1 because one is not present.
RC=0; OUT=$(cd "$T" && "$EC" A B C DCOMMENT E Z 2>&1) || RC=$?
[ "$RC" -eq 1 ] || fail "a missing key should exit 1, got $RC"
echo "$OUT" | grep -qx "A present"   || fail "A present: $OUT"
echo "$OUT" | grep -qx "B present"   || fail "export B present: $OUT"
echo "$OUT" | grep -qx "C empty"     || fail "C empty: $OUT"
echo "$OUT" | grep -qx "DCOMMENT missing" || fail "a commented key is missing: $OUT"
echo "$OUT" | grep -qx "Z missing"   || fail "Z missing: $OUT"

# 2 — THE POINT: the value never appears.
echo "$OUT" | grep -q "abc123" && fail "a value leaked: $OUT"

# 3 — all present exits 0; --file picks another file; .env.local wins by default.
(cd "$T" && "$EC" A B >/dev/null) || fail "all present should exit 0"
printf 'ONLYLOCAL=1\n' > "$T/${D}.local"
(cd "$T" && "$EC" ONLYLOCAL | grep -qx "ONLYLOCAL present") || fail ".env.local should be the default when present"
(cd "$T" && "$EC" --file "$D" A | grep -qx "A present") || fail "--file should select the file"

# 4 — no file at all: every key is missing, no crash.
E=$(mktemp -d); OUT=$(cd "$E" && "$EC" A || true); rm -rf "$E"
[ "$OUT" = "A missing" ] || fail "no dotenv should read missing, got: $OUT"

# 5 — usage: no keys, or a key name that could smuggle a pattern.
RC=0; "$EC" >/dev/null 2>&1 || RC=$?; [ "$RC" -eq 64 ] || fail "no keys should exit 64, got $RC"
RC=0; (cd "$T" && "$EC" 'A.*' >/dev/null 2>&1) || RC=$?; [ "$RC" -eq 64 ] || fail "a bad key name should exit 64, got $RC"

# 6 — several files, in subdirectories, and appsettings JSON paths (.NET style).
mkdir -p "$T/backend/src/Host" "$T/frontend/apps/dashboard" "$T/.claude"
printf 'ConnectionStrings__Default=%s\n' "$SECRET" > "$T/backend/.env"
printf 'VITE_API_URL=%s\n' "$SECRET" > "$T/frontend/apps/dashboard/.env.local"
printf '{"ConnectionStrings":{"Default":"","Redis":"%s"},"Jwt":{"Issuer":"%s","Keys":[]}}' "$SECRET" "$SECRET" \
  > "$T/backend/src/Host/appsettings.Development.json"
J=backend/src/Host/appsettings.Development.json
RC=0; OUT=$(cd "$T" && "$EC" --file "$J" ConnectionStrings:Redis Jwt__Issuer ConnectionStrings:Default Jwt:Keys Jwt:Nope Nope:Deeper:X 2>&1) || RC=$?
[ "$RC" -eq 1 ] || fail "a missing JSON key should exit 1, got $RC"
echo "$OUT" | grep -qx "ConnectionStrings:Redis present" || fail "Section:Key in JSON: $OUT"
echo "$OUT" | grep -qx "Jwt__Issuer present" || fail "Section__Key in JSON: $OUT"
echo "$OUT" | grep -qx "ConnectionStrings:Default empty" || fail "empty JSON string: $OUT"
echo "$OUT" | grep -qx "Jwt:Keys empty" || fail "empty JSON array: $OUT"
echo "$OUT" | grep -qx "Jwt:Nope missing" || fail "missing JSON key: $OUT"
echo "$OUT" | grep -qx "Nope:Deeper:X missing" || fail "missing JSON section: $OUT"
echo "$OUT" | grep -q "abc123" && fail "a JSON value leaked: $OUT"
# Present in a later file beats empty in an earlier one; Section:Key reads Section__Key in dotenv.
OUT=$(cd "$T" && "$EC" --file "$J" --file backend/.env ConnectionStrings:Default) || fail "present in any file should exit 0: $OUT"
[ "$OUT" = "ConnectionStrings:Default present" ] || fail "later file should win over empty: $OUT"
# --config reads env_check.files.
printf '{"env_check":{"files":["backend/.env","frontend/apps/dashboard/.env.local","%s"]}}' "$J" > "$T/.claude/c.json"
OUT=$(cd "$T" && "$EC" --config .claude/c.json VITE_API_URL ConnectionStrings__Default Jwt:Issuer 2>&1) || fail "config files should find every key: $OUT"
echo "$OUT" | grep -q "abc123" && fail "a value leaked via --config: $OUT"
[ "$(echo "$OUT" | grep -c ' present$')" -eq 3 ] || fail "three keys present via config: $OUT"
RC=0; (cd "$T" && "$EC" 'A:' >/dev/null 2>&1) || RC=$?; [ "$RC" -eq 64 ] || fail "a trailing colon should exit 64, got $RC"

# 7 — ASP.NET appsettings are JSONC: comments and trailing commas parse; a broken file
#     is "unreadable <file>", never "missing"; a "//" inside a string is a value, not a comment.
cat > "$T/backend/src/Host/appsettings.json" <<JSON
{
  // connection strings
  "ConnectionStrings": { "Default": "Host=db;Password=$SECRET", /* inline */ },
  "Serilog": { "Url": "http://seq//x" },
  /* block
     comment */
  "Jwt": { "Issuer": "", },
}
JSON
J2=backend/src/Host/appsettings.json
RC=0; OUT=$(cd "$T" && "$EC" --file "$J2" ConnectionStrings:Default Serilog:Url Jwt:Issuer Jwt:Nope 2>&1) || RC=$?
echo "$OUT" | grep -qx "ConnectionStrings:Default present" || fail "JSONC comments: $OUT"
echo "$OUT" | grep -qx "Serilog:Url present" || fail "// inside a string: $OUT"
echo "$OUT" | grep -qx "Jwt:Issuer empty" || fail "trailing comma: $OUT"
echo "$OUT" | grep -qx "Jwt:Nope missing" || fail "missing key in JSONC: $OUT"
echo "$OUT" | grep -q "$SECRET" && fail "a JSONC value leaked: $OUT"
printf '{ "Jwt": { "Issuer": "x" ' > "$T/backend/src/Host/appsettings.Broken.json"
RC=0; OUT=$(cd "$T" && "$EC" --file backend/src/Host/appsettings.Broken.json Jwt:Issuer 2>&1) || RC=$?
[ "$RC" -eq 1 ] || fail "an unreadable file should exit 1, got $RC"
[ "$OUT" = "Jwt:Issuer unreadable backend/src/Host/appsettings.Broken.json" ] || fail "unreadable, not missing: $OUT"

echo "PASS: test-env-check.sh (7 scenarios)"
