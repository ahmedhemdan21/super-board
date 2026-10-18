#!/usr/bin/env bash
# The paid collect evals grade what the real filer does against evals/_stubs/bin/gh. If the
# stub stops answering a filer read, every collect eval fails before the model is judged.
# This replays the filer, no model and no network, on two eval seeds: a duplicate gets a
# "Seen again" comment and no new issue; a new problem is created into Backlog, never Ready.
#
#   bash tests/test-eval-stubs.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
REAL_GITCONFIG="${HOME}/.gitconfig"

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  ✅ %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  ❌ %s\n     expected: %s\n     actual:   %s\n' "$1" "$2" "$3"; }
is()  { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }
has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "contains: $3" "$2" ;; esac; }
lacks() { case "$2" in *"$3"*) bad "$1" "no: $3" "found it" ;; *) ok "$1" ;; esac; }

cat > "$WORK/body.md" <<'MD'
## Problem
Checkout throws a TypeError when the last cart item is removed.

## Context
- **Where:** `/checkout`
  - a. Add one item
  - b. Go to `/checkout`
  - c. Remove the last item

## Evidence
<details><summary>12 rows</summary>

| Row | Value |
|---|---|
| Error + stack | TypeError · src/checkout/summary.ts:12 |
| Request / trace ID | n/a — none |
| Sentry | WEB-4411 |
| PostHog replay | n/a — none |
| Logs | n/a — client |
| Screenshots | n/a — none |
| HAR / API sample | n/a — none |
| Env + release | web@1.4.2 |
| First / last seen | Oct 5 · Oct 17 |
| Users affected | 14 |
| Steps | 1. add 2. checkout 3. remove |
| Expected / actual | empty cart / TypeError |

</details>

## Fix
Guard buildSummary against an empty cart.

## Acceptance Criteria
- [ ] Removing the last item shows the empty-cart state

## Risk
Low

## Blocked by
- None.
MD

# replay <case>: seed it like the eval (own HOME: the seed edits shell rc files), then file
# WEB-4411 with --yes. Sets WS and RC.
replay() {
  WS="$WORK/$1"; local home="$WORK/$1-home"
  mkdir -p "$WS" "$home"
  [ -f "$REAL_GITCONFIG" ] && cp "$REAL_GITCONFIG" "$home/"
  (
    export SB_EVAL_REPLAY=1 HOME="$home" PATH="$REPO_ROOT/evals/_stubs/bin:$PATH"
    export GIT_AUTHOR_NAME=eval GIT_AUTHOR_EMAIL=eval@example.com GIT_COMMITTER_NAME=eval GIT_COMMITTER_EMAIL=eval@example.com
    cd "$WS" && git init -q && bash "$REPO_ROOT/evals/$1/seed.sh" "$WS" >/dev/null 2>&1 || exit 90
    export PYTHONPATH="$REPO_ROOT/evals/_stubs/py" SC_FIXTURES="$WS/.git/collect-fixture" SB_GITHUB_RETRY_DELAY=0
    .claude/skills/super-collect/scripts/super-collect-file.sh --config .claude/super-board/configs/shop.json \
      --type bug --source sentry --title "Checkout crashes when the last item is removed" \
      --body-file "$WORK/body.md" --fingerprint 'err|sentry|4411' --priority high --area checkout --yes \
      >"$WS.out" 2>"$WS.err"
  )
  RC=$?
}

echo "── duplicate (collect-duplicate seed)"
replay collect-duplicate
is "filer exits 0" 0 "$RC"
CALLS=$(cat "$WS/.git/gh-calls.log" 2>/dev/null)
has "issue history read answered" "$CALLS" "api graphql --paginate --slurp"
has "Seen again comment on #301" "$CALLS" "issue comment 301"
lacks "no new issue" "$CALLS" "issue create"
has "comment body recorded" "$(cat "$WS/.git/gh-comments.md" 2>/dev/null)" "Seen again"

echo "── new problem (collect-sentry-verdicts seed)"
replay collect-sentry-verdicts
is "filer exits 0" 0 "$RC"
CALLS=$(cat "$WS/.git/gh-calls.log" 2>/dev/null)
has "issue created" "$CALLS" "issue create"
has "moved to Backlog" "$CALLS" "opt_backlog"
lacks "never Ready" "$CALLS" "opt_ready"
has "body recorded" "$(cat "$WS/.git/gh-created.md" 2>/dev/null)" "## Problem"

echo "── Windows guard (uname stubbed to MINGW)"
mkdir -p "$WORK/winbin" "$WORK/winws"
printf '#!/usr/bin/env bash\necho MINGW64_NT-10.0\n' > "$WORK/winbin/uname"; chmod +x "$WORK/winbin/uname"
for entry in "evals/run.sh" "evals/test-gap/seed.sh" "evals/collect-duplicate/seed.sh"; do
  MSG=$(cd "$WORK/winws" && env -u SB_EVAL_REPLAY PATH="$WORK/winbin:$PATH" bash "$REPO_ROOT/$entry" "$WORK/winws" 2>&1)
  is "$entry exits 75 on Windows" 75 "$?"
  has "$entry says why" "$MSG" "run them in WSL or CI"
done

echo "passed: $PASS  failed: $FAIL"
[ "$FAIL" -eq 0 ]
