#!/usr/bin/env bash
# super-collect-file.sh — file one collected item onto the super-board project,
# into the holding column only. Dry-run unless --yes.
#
# This is a router, not a third filer. Bugs, features and recurring-problem fixes go
# through super-qa-file-bug.sh; refactors through super-review-file-refactor.sh.
# What it adds on top of them:
#   1. a repo-wide fingerprint dedupe (theirs only see their own source label),
#   2. holding-column resolution BEFORE dispatch — both filers fall back toward
#      Ready when the requested column is missing, and super-collect must never
#      put a card in Ready. No holding column → refuse (exit 65).
#   3. `--adopt N` for a user-filed issue that is not on the board yet: place the
#      existing issue instead of filing a copy of it.
#   4. one fingerprint shape per source, so the same problem keys the same way on
#      every run: sentry `err|sentry|<issue-id>`, posthog `posthog|<signal>|<key>`
#      (tracking gaps: `gap|<workflow>`),
#      github `github|<issue#>`, prs `prs|<boundary>|<cause>`,
#      architecture `arch|<module>|<problem>`, loki `loki|<group>|<signature>`,
#      prometheus `prom|alert|<name>|<labels>` or `prom|target|<job>|<instance>`,
#      slack `slack|<channel id>|<message ts>` (the whole key is checked, not just the prefix).
#   5. one card per real problem ACROSS sources: `--signature <crossSignature>` is stamped as
#      `<!-- sb-signature: <hash> -->`. When no issue holds this fingerprint but an OPEN one
#      holds the signature (Sentry, Loki and PostHog saw the same crash), it appends ONE
#      `[collect] [evidence]` comment there instead of filing (at most one per source per
#      issue per 24h, marker `<!-- sb-evidence: <source>:<fingerprint> -->`). A CLOSED one
#      with the signature: file new, "seen before in #n".
#   6. `--trace-id <32 hex>` adds a Trace line to Evidence (collect_tempo.py evidence:
#      summary + Grafana Explore link when collect.tempo is configured, else the id).
#   7. triage: the composed body goes through super-board-card-ready.py; a body that is
#      not ready is filed with the `needs-triage` label (references/triage.md).
#   8. hub boards (a config with `members`, hub.md → Intake): `--target hub|<member key>`
#      picks the repo. Unset, the source decides: top-level `collect.sources` → hub,
#      a member's own `members.<key>.collect.sources` → that member; a source listed in
#      none or several needs --target. Hub filings land in Backlog with `needs-split`
#      (triage's split maps them to members). Dedupe (fingerprint and signature) reads
#      the hub's and every member's issue history, so a parent already split or
#      transferred to a member is found there. The receipt lock is the hub repo's.
#      Issue ids print as `owner/repo#N` in this mode.
#
# Usage:
#   super-collect-file.sh --config <cfg.json> --type bug|feature|refactor|fix \
#     --source sentry|posthog|loki|prometheus|slack|github|prs|architecture|custom --title "<one line>" --body-file <md> \
#     --fingerprint "<stable key>" [--priority high|medium|low] [--area <a>] \
#     [--label <l>]... [--yes]          # e.g. --label needs-triage --label ux
#     [--signature <12 hex>] [--trace-id <32 hex>]
#     [--count N] [--first-seen T] [--last-seen T] [--link URL] [--sample "<one line>"]
#                                       # the evidence comment on a signature match
#     [--target hub|<member key>]       # hub configs only (§ 8)
#   super-collect-file.sh --config <cfg.json> --adopt <issue#> --type bug|feature|refactor \
#     [--target hub|<key> | --source <s>] [--yes]
#
# Stdout: dry-run → one `would-file|duplicate|evidence|recurrence|would-adopt` plan line
#         (hub configs: issues as owner/repo#N, and the target repo appended);
#         --yes   → the issue number (new, existing on a dedupe hit, or adopted);
#         hub configs print `owner/repo#N`.
# Exits:  0 ok · 64 bad args · 65 no holding column · 66 unreadable/weak body
#         70 gh failure · 71 filed but not placed (number still on stdout)
#         79 repository paused (existing issue number still on stdout after discovery)
set -uo pipefail

ORIGINAL_ARGS=("$@")
CONFIG=""; TYPE=""; SOURCE=""; TITLE=""; BODY_FILE=""; FP=""
PRIORITY="medium"; AREA=""; ADOPT=""; YES=0; EXTRA_LABELS=(); TARGET=""
SIG=""; TRACE_ID=""; EV_COUNT=""; EV_FIRST=""; EV_LAST=""; EV_LINK=""; EV_SAMPLE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --config)      CONFIG="$2"; shift 2 ;;
    --type)        TYPE="$2"; shift 2 ;;
    --source)      SOURCE="$2"; shift 2 ;;
    --title)       TITLE="$2"; shift 2 ;;
    --body-file)   BODY_FILE="$2"; shift 2 ;;
    --fingerprint) FP="$2"; shift 2 ;;
    --priority)    PRIORITY="$2"; shift 2 ;;
    --area)        AREA="$2"; shift 2 ;;
    --adopt)       ADOPT="$2"; shift 2 ;;
    --label)       EXTRA_LABELS+=("$2"); shift 2 ;;
    --signature)   SIG="$2"; shift 2 ;;
    --trace-id)    TRACE_ID="$2"; shift 2 ;;
    --count)       EV_COUNT="$2"; shift 2 ;;
    --first-seen)  EV_FIRST="$2"; shift 2 ;;
    --last-seen)   EV_LAST="$2"; shift 2 ;;
    --link)        EV_LINK="$2"; shift 2 ;;
    --sample)      EV_SAMPLE="$2"; shift 2 ;;
    --target)      TARGET="$2"; shift 2 ;;
    --yes)         YES=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done

die() { echo "$1" >&2; exit "$2"; }

[ -n "$CONFIG" ] && [ -r "$CONFIG" ] || die "config not found: ${CONFIG:-<unset>}" 66
case "$TYPE" in bug|feature|refactor|fix) ;; *) die "--type must be bug|feature|refactor|fix (got: ${TYPE:-<unset>})" 64 ;; esac
case "$PRIORITY" in high|medium|low) ;; *) die "--priority must be high|medium|low (got: $PRIORITY)" 64 ;; esac

if [ -z "$ADOPT" ]; then
  case "$SOURCE" in
    sentry) FP_PREFIX="err|sentry|" ;; posthog) FP_PREFIX="posthog|" ;; github) FP_PREFIX="github|" ;;
    prs) FP_PREFIX="prs|" ;; architecture) FP_PREFIX="arch|" ;;
    loki) FP_PREFIX="loki|" ;; prometheus) FP_PREFIX="prom|" ;;
    slack) FP_PREFIX="slack|" ;;     # collect_slack.py: slack|<channel id>|<message ts>
    custom) FP_PREFIX="custom|" ;;   # onboard's "➕ Add another source": custom|<name>|<key>
    *) die "--source must be sentry|posthog|loki|prometheus|slack|github|prs|architecture|custom (got: ${SOURCE:-<unset>})" 64 ;;
  esac
  [ -n "$TITLE" ] || die "--title is required" 64
  [ -n "$FP" ]    || die "--fingerprint is required (dedupe key)" 64
  case "$FP" in *[[:space:]]*) die "--fingerprint must be one token without whitespace" 64;; esac
  case "$FP" in *'"'*|*'\\'*) die "--fingerprint may not contain quotes or backslashes" 64 ;; esac
  case "$SOURCE|$FP" in "posthog|gap|"?*) FP_PREFIX="gap|" ;; esac
  case "$FP" in "$FP_PREFIX"?*) ;; *) die "--fingerprint for source ${SOURCE} must start with '${FP_PREFIX}' (got: $FP)" 64 ;; esac
  SLACK_FP='^slack\|[CG][A-Z0-9]+\|[0-9]+\.[0-9]+$'
  if [ "$SOURCE" = slack ] && ! [[ "$FP" =~ $SLACK_FP ]]; then
    die "--fingerprint for source slack must be slack|<channel id>|<message ts> (got: $FP)" 64
  fi
  [ -n "$BODY_FILE" ] && [ -r "$BODY_FILE" ] || die "--body-file missing or unreadable: ${BODY_FILE:-<unset>}" 66
  case "$SIG" in ""|[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
    *) die "--signature must be the 12-hex crossSignature (got: $SIG)" 64 ;; esac
  case "$TRACE_ID" in "") ;; *[!0-9a-f]*) die "--trace-id must be 32 lower-case hex" 64 ;;
    *) [ "${#TRACE_ID}" -eq 32 ] || die "--trace-id must be 32 lower-case hex" 64 ;; esac
  case "$EV_COUNT" in *[!0-9]*) die "--count takes a number" 64 ;; esac
else
  case "$ADOPT" in *[!0-9]*|"") die "--adopt takes an issue number (got: $ADOPT)" 64 ;; esac
  [ "$TYPE" != "fix" ] || die "--adopt takes bug|feature|refactor" 64
fi

BIN="${SUPER_BOARD_BIN:-}"
if [ -z "$BIN" ]; then
  # Installed: .claude/skills/super-collect/scripts → .claude/bin. Pack: skills/… → scripts/.
  ROOT3=$(cd "$(dirname "$0")/../../.." 2>/dev/null && pwd)
  for d in ".claude/bin" "$ROOT3/bin" "$ROOT3/scripts"; do
    [ -x "$d/super-qa-file-bug.sh" ] && { BIN="$d"; break; }
  done
fi
[ -n "$BIN" ] || die "cannot find super-qa-file-bug.sh — set SUPER_BOARD_BIN or run install.sh" 70
GITHUB_READ="$BIN/super-board-github-read.py"
STATE="$BIN/super-board-collect-state.py"
RESOLVER="$BIN/super-board-config.py"
TMPS=()
trap 'rm -f ${TMPS[@]+"${TMPS[@]}"}' EXIT
repo_of() { echo "$1" | sed -E 's#(git@github\.com:|https://github\.com/)##; s#\.git$##; s#/+$##'; }

# --- hub boards: which repo files this (header § 8) -------------------------
HUB_MODE=0; HUB_CONFIG=""; HUB_REPO=""; BOARD_REPOS=""; CHECK=(--check)
if [ "$(jq -r 'has("members")' "$CONFIG")" = true ]; then
  HUB_MODE=1; HUB_CONFIG="$CONFIG"; CHECK=(--check --config "$HUB_CONFIG")
  python3 "$RESOLVER" validate --config "$HUB_CONFIG" >&2 || die "hub config is invalid: $HUB_CONFIG" 66
  if [ -z "$TARGET" ]; then
    WANT="$SOURCE"
    [ "$SOURCE" != custom ] || WANT=$(printf '%s' "$FP" | cut -d'|' -f2)
    [ -z "$WANT" ] || TARGET=$(jq -r --arg s "$WANT" '
      [(.members | to_entries[] | select((.value.collect.sources // []) | index($s)) | .key)]
      + (if ((.collect.sources // []) | index($s)) then ["hub"] else [] end)
      | if length == 1 then .[0] else "" end' "$HUB_CONFIG")
    [ -n "$TARGET" ] || die "hub config: source '${WANT:-<none>}' is in none or several of collect.sources / members.<key>.collect.sources — pass --target hub|<member key>" 64
  fi
  if [ "$TARGET" != hub ] && [ "$(jq -r --arg t "$TARGET" '.members | has($t)' "$HUB_CONFIG")" != true ]; then
    die "--target must be hub or a member key ($(jq -r '.members | keys | join(", ")' "$HUB_CONFIG")); got: $TARGET" 64
  fi
  HUB_REPO=$(repo_of "$(jq -r '.hub.remote' "$HUB_CONFIG")")
  MEMBER_REPOS=$(python3 "$RESOLVER" members --config "$HUB_CONFIG" | jq -r '[.[].repo] | join(",")') \
    || die "cannot list the hub's members" 66
  BOARD_REPOS="${HUB_REPO},${MEMBER_REPOS}"
  # A hub parent waits in Backlog for triage's split.
  [ "$TARGET" != hub ] || EXTRA_LABELS+=("needs-split")
elif [ -n "$TARGET" ]; then
  die "--target needs a hub config (one with members)" 64
fi

# Router-owned duplicate/adopt writes share the same halt as the child filers; a hub
# run also stops on any member's halt.
python3 "$GITHUB_READ" "${CHECK[@]}" || exit $?

load_target() { # CONFIG → OWNER NUMBER PTITLE REMOTE REPO_FLAG REPO_NAME
  OWNER=$(jq -r '.project.owner // empty' "$CONFIG")
  NUMBER=$(jq -r '.project.number // empty' "$CONFIG")
  PTITLE=$(jq -r '.project.title // empty' "$CONFIG")
  REMOTE=$(jq -r '.repo.remote // empty' "$CONFIG")
  [ -n "$OWNER" ] && [ -n "$NUMBER" ] || die "config lacks project.owner / project.number" 66
  REPO_FLAG=()
  [ -z "$REMOTE" ] || REPO_FLAG=(-R "$(repo_of "$REMOTE")")
  REPO_NAME="${REPO_FLAG[1]:-}"
  if [ -z "$REPO_NAME" ]; then
    REPO_NAME=$(python3 "$GITHUB_READ" --kind scalar -- repo view --json nameWithOwner --jq .nameWithOwner) || exit $?
  fi
  case "$REPO_NAME" in */*) ;; *) die "cannot resolve owner/name for collector" 70;; esac
}

# One lock per board: a hub's filings into the hub and every member serialize together.
if [ "$HUB_MODE" -eq 1 ]; then LOCK_REPO="$HUB_REPO"; else load_target; LOCK_REPO="$REPO_NAME"; fi
LOCK_KEY=$(printf '%s' "$LOCK_REPO" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.read().lower().encode()).hexdigest())')
if [ "${SB_COLLECT_LOCK_HELD:-}" != "$LOCK_KEY" ]; then
  exec python3 "$STATE" --repo "$LOCK_REPO" --lock -- "$0" "${ORIGINAL_ARGS[@]}"
fi
if [ "$HUB_MODE" -eq 1 ]; then
  # The target as one repo's config, from the resolver (paths never from cwd).
  RESOLVED=$(mktemp); TMPS+=("$RESOLVED")
  if [ "$TARGET" = hub ]; then python3 "$RESOLVER" hub --config "$HUB_CONFIG" --out "$RESOLVED" || exit 66
  else python3 "$RESOLVER" member "$TARGET" --config "$HUB_CONFIG" --out "$RESOLVED" || exit 66; fi
  CONFIG="$RESOLVED"
  load_target
fi
# ref N REPO: how an issue is named in plan lines and bodies; id_out: on stdout.
ref()    { if [ "$HUB_MODE" -eq 1 ]; then printf '%s#%s' "$2" "$1"; else printf '#%s' "$1"; fi; }
id_out() { if [ "$HUB_MODE" -eq 1 ]; then echo "$2#$1"; else echo "$1"; fi; }
TAIL=""; [ "$HUB_MODE" -eq 0 ] || TAIL="|${REPO_NAME}"
state() { python3 "$STATE" --repo "$LOCK_REPO" --action "$1" --fingerprint "${FP:-adopt|$ADOPT}" --digest "${DIGEST:-}" --issue "${2:-}"; }
DIGEST=""
if [ -n "$BODY_FILE" ]; then
  DIGEST=$(python3 -c 'import hashlib,sys; print(hashlib.sha256((sys.argv[1]+"\0").encode()+open(sys.argv[2],"rb").read()).hexdigest())' "$SOURCE" "$BODY_FILE") || exit 70
fi

# --- body sections ----------------------------------------------------------
# The ticket format is writing-standard.md § 3. super-qa-file-bug.sh enforces the
# full bug shape (lettered steps, the 12-row Evidence table). Features and
# recurring-problem fixes are not repro-shaped, so they get their own minimum here
# and the QA filer's repro check is bypassed for them. Refactors: the review filer.
need_sections() {
  local missing="" s
  for s in "$@"; do
    grep -qiE "^#{1,3}[[:space:]]+${s}[[:space:]]*$" "$BODY_FILE" || missing="${missing}${missing:+, }${s}"
  done
  [ -z "$missing" ] || die "body is missing required section(s): ${missing} (writing-standard.md § 3)" 66
}
need_checklist() {
  awk '/^#+[[:space:]]+/ { h = tolower($0); on = (h ~ /^#+[[:space:]]+acceptance criteria[[:space:]]*$/); next }
       on && /^[[:space:]]*- \[[ xX]\] / { found = 1 } END { exit !found }' "$BODY_FILE" \
    || die "Acceptance Criteria needs a checklist: one '- [ ] <checkable outcome>' per line" 66
}
if [ -z "$ADOPT" ]; then
  case "$TYPE" in
    bug) "$BIN/super-qa-file-bug.sh" --title "$TITLE" --body-file "$BODY_FILE" --check-body || exit $? ;;
    feature)  need_sections "Problem" "Context" "Fix" "Acceptance Criteria" "Risk"; need_checklist ;;
    fix)      need_sections "Problem" "Context" "Evidence" "Fix" "Acceptance Criteria" "Risk"; need_checklist ;;
    refactor) need_sections "Problem" "Context" "Fix" ;;
  esac
fi

# --- holding column ---------------------------------------------------------
FIELDS=$(python3 "$GITHUB_READ" --kind fields -- project field-list "$NUMBER" --owner "$OWNER" --format json) || exit $?
HOLD=""
HOLDS=(Backlog Todo "To do" Triage Inbox)
[ "$TARGET" != hub ] || HOLDS=(Backlog)   # hub parents wait for the split in Backlog
for c in "${HOLDS[@]}"; do
  hit=$(echo "$FIELDS" | jq -r --arg c "$c" '.fields[] | select(.name=="Status") | .options[]? | select(.name==$c) | .name' | head -1)
  if [ -n "$hit" ]; then HOLD="$hit"; break; fi
done
[ -n "$HOLD" ] || die "project ${OWNER}/${NUMBER} has no holding column (${HOLDS[*]}) — refusing: the filers would fall back to Ready" 65

place() { # $1 = issue url
  local item_id project_id field_id option_id
  python3 "$GITHUB_READ" "${CHECK[@]}" || return $?
  item_id=$(gh project item-add "$NUMBER" --owner "$OWNER" --url "$1" --format json --jq '.id') || return 1
  python3 "$GITHUB_READ" "${CHECK[@]}" || return $?
  project_id=$(gh project view "$NUMBER" --owner "$OWNER" --format json --jq '.id') || return 1
  field_id=$(echo "$FIELDS" | jq -r '.fields[] | select(.name=="Status") | .id')
  option_id=$(echo "$FIELDS" | jq -r --arg c "$HOLD" '.fields[] | select(.name=="Status") | .options[] | select(.name==$c) | .id')
  python3 "$GITHUB_READ" "${CHECK[@]}" || return $?
  gh project item-edit --id "$item_id" --project-id "$project_id" \
    --field-id "$field_id" --single-select-option-id "$option_id" >/dev/null
}

tag() { # $1 = issue number, rest = labels; best-effort
  local n="$1"; shift
  for l in "$@"; do
    python3 "$GITHUB_READ" "${CHECK[@]}" || return $?
    gh label create "$l" ${REPO_FLAG[@]+"${REPO_FLAG[@]}"} --color 5319E7 --force >/dev/null 2>&1 || true
  done
  for l in "$@"; do
    python3 "$GITHUB_READ" "${CHECK[@]}" || return $?
    gh issue edit "$n" ${REPO_FLAG[@]+"${REPO_FLAG[@]}"} --add-label "$l" >/dev/null 2>&1 || echo "warn: could not label #${n} ${l}" >&2
  done
  return 0
}

# --- adopt ------------------------------------------------------------------
if [ -n "$ADOPT" ]; then
  if [ "$YES" -ne 1 ]; then echo "would-adopt|$(ref "$ADOPT" "$REPO_NAME")|${TYPE}|${HOLD}"; exit 0; fi
  URL=$(python3 "$GITHUB_READ" --kind scalar -- issue view "$ADOPT" ${REPO_FLAG[@]+"${REPO_FLAG[@]}"} --json url --jq .url) || exit $?
  BOARD=$(python3 "$GITHUB_READ" --kind items -- project item-list "$NUMBER" --owner "$OWNER" --format json --limit 500) || exit $?
  # A matching issue number without its repository URL cannot prove whether
  # this is already our card. Preserve potentially live work until reconciled.
  AMBIGUOUS=$(printf '%s' "$BOARD" | jq -r --argjson n "$ADOPT" '[.items[] | select(.content.type == "Issue" and .content.number == $n and ((.content.url // "") == ""))] | length') || exit 70
  if [ "$AMBIGUOUS" != 0 ]; then
    python3 "$GITHUB_READ" --halt "Board identity is incomplete for issue #${ADOPT}; reconcile before adoption"
    exit 79
  fi
  # Never reset a card already on the board to Backlog, especially live work.
  ON_BOARD=$(printf '%s' "$BOARD" | jq -r --arg u "$URL" '[.items[] | select(.content.url == $u)] | length') || exit 70
  if [ "$ON_BOARD" != 0 ]; then id_out "$ADOPT" "$REPO_NAME"; exit 0; fi
  state begin "$ADOPT" || exit $?
  tag "$ADOPT" "$TYPE" "source:collect" ${EXTRA_LABELS[@]+"${EXTRA_LABELS[@]}"} || { RC=$?; id_out "$ADOPT" "$REPO_NAME"; exit "$RC"; }
  place "$URL" || { RC=$?; id_out "$ADOPT" "$REPO_NAME"; [ "$RC" -eq 79 ] && exit 79; echo "warn: #${ADOPT} not placed in '${HOLD}'" >&2; exit 71; }
  state success "$ADOPT" || exit $?
  id_out "$ADOPT" "$REPO_NAME"; exit 0
fi

# --- dedupe -----------------------------------------------------------------
# Repo-wide (a hub board: the hub and every member), any label, any state. An open hit
# is a duplicate; a closed-only hit is a recurrence — file again, but name the issue
# that claimed the fix.
# Exact match on a whitespace-delimited token: `err|sentry|441` must not hit `err|sentry|4411`.
ISSUES_JSON=$(python3 "$GITHUB_READ" --issue-history "${BOARD_REPOS:-$REPO_NAME}") || exit $?
# Lines `<number> <state> <repo>`, open first.
HITS=$(printf '%s' "$ISSUES_JSON" | jq -r --arg fp "$FP" --arg r "$REPO_NAME" '[.[] | select(any(.body | splits("\\s+"); . == $fp))] | sort_by(.state != "OPEN") | map("\(.number) \(.state) \(.repo // $r)") | .[]') || exit 70

OPEN_HIT=$(echo "$HITS" | awk '$2=="OPEN"{print $1; exit}')
OPEN_REPO=$(echo "$HITS" | awk '$2=="OPEN"{print $3; exit}')
CLOSED_HIT=$(echo "$HITS" | awk '$2!="OPEN" && $1!=""{print $1; exit}')
CLOSED_REPO=$(echo "$HITS" | awk '$2!="OPEN" && $1!=""{print $3; exit}')

# Cross-source signature (header § 5), from the same history read: no extra GitHub call.
SIG_OPEN=""; SIG_OPEN_REPO=""; SIG_CLOSED=""; SIG_CLOSED_REPO=""; SEEN_BEFORE=""; SEEN_REPO=""
if [ -n "$SIG" ]; then
  SIG_HITS=$(printf '%s' "$ISSUES_JSON" | jq -r --arg m "sb-signature: ${SIG} -->" --arg r "$REPO_NAME" '[.[] | select(.body | contains($m))] | sort_by(.state != "OPEN") | map("\(.number) \(.state) \(.repo // $r)") | .[]') || exit 70
  SIG_OPEN=$(echo "$SIG_HITS" | awk '$2=="OPEN"{print $1; exit}')
  SIG_OPEN_REPO=$(echo "$SIG_HITS" | awk '$2=="OPEN"{print $3; exit}')
  SIG_CLOSED=$(echo "$SIG_HITS" | awk '$2!="OPEN" && $1!=""{print $1; exit}')
  SIG_CLOSED_REPO=$(echo "$SIG_HITS" | awk '$2!="OPEN" && $1!=""{print $3; exit}')
fi

if [ -n "$OPEN_HIT" ]; then
  if [ "$YES" -ne 1 ]; then echo "duplicate|$(ref "$OPEN_HIT" "$OPEN_REPO")|${TYPE}|${TITLE}"; exit 0; fi
  python3 "$GITHUB_READ" "${CHECK[@]}" || { RC=$?; id_out "$OPEN_HIT" "$OPEN_REPO"; exit "$RC"; }
  SEEN=$(state seen "$OPEN_HIT") || exit $?
  if [ "$SEEN" = unchanged ]; then id_out "$OPEN_HIT" "$OPEN_REPO"; exit 0; fi
  state begin "$OPEN_HIT" || exit $?
  gh issue comment "$OPEN_HIT" -R "$OPEN_REPO" --body "Seen again by super-collect (${SOURCE}).

$(cat "$BODY_FILE")" >/dev/null 2>&1 || { python3 "$GITHUB_READ" --halt "Collector comment outcome unknown for ${OPEN_REPO}#${OPEN_HIT}; reconcile before retrying"; id_out "$OPEN_HIT" "$OPEN_REPO"; exit 79; }
  state success "$OPEN_HIT" || exit $?
  id_out "$OPEN_HIT" "$OPEN_REPO"; exit 0
fi

# Another source already filed this problem: evidence on that card, never a second card.
if [ -n "$SIG_OPEN" ]; then
  if [ "$YES" -ne 1 ]; then echo "evidence|$(ref "$SIG_OPEN" "$SIG_OPEN_REPO")|${SOURCE}|${TITLE}"; exit 0; fi
  python3 "$GITHUB_READ" "${CHECK[@]}" || { RC=$?; id_out "$SIG_OPEN" "$SIG_OPEN_REPO"; exit "$RC"; }
  COMMENTS=$(python3 "$GITHUB_READ" --kind comments -- api "repos/${SIG_OPEN_REPO}/issues/${SIG_OPEN}/comments" --paginate --slurp) \
    || { RC=$?; id_out "$SIG_OPEN" "$SIG_OPEN_REPO"; exit "$RC"; }
  # One evidence comment per source per issue per 24h. Unreadable dates count as recent:
  # a skipped comment costs nothing, a comment storm costs the issue's readability.
  RECENT=$(printf '%s' "$COMMENTS" | python3 -c '
import datetime as d, json, sys
mark = "<!-- sb-evidence: " + sys.argv[1] + ":"
now = d.datetime.now(d.timezone.utc)
cs = [c for page in json.loads(sys.stdin.buffer.read().decode("utf-8")) for c in page]
hit = any(mark in (c.get("body") or "") and
          (now - d.datetime.fromisoformat(str(c["created_at"]).replace("Z", "+00:00"))).total_seconds() < 86400
          for c in cs)
print("yes" if hit else "no")' "$SOURCE") || RECENT=yes
  if [ "$RECENT" != no ]; then id_out "$SIG_OPEN" "$SIG_OPEN_REPO"; exit 0; fi
  state begin "$SIG_OPEN" || exit $?
  EV_BODY="[collect] [evidence] 🔁 seen again · ${SOURCE}
Did: matched sb-signature \`${SIG}\` from ${SOURCE} (\`${FP}\`)
✅ Done: ${EV_COUNT:-?} events · first ${EV_FIRST:-?} · last ${EV_LAST:-?}${EV_LINK:+
Link: ${EV_LINK}}${EV_SAMPLE:+
Sample: \`$(printf '%s' "$EV_SAMPLE" | tr '\r\n`' '  '"'" | cut -c1-200)\`}${TRACE_ID:+
Trace: \`${TRACE_ID}\`}
Next: none
<!-- sb-evidence: ${SOURCE}:${FP} -->"
  gh issue comment "$SIG_OPEN" -R "$SIG_OPEN_REPO" --body "$EV_BODY" >/dev/null 2>&1 \
    || { python3 "$GITHUB_READ" --halt "Collector evidence comment outcome unknown for ${SIG_OPEN_REPO}#${SIG_OPEN}; reconcile before retrying"; id_out "$SIG_OPEN" "$SIG_OPEN_REPO"; exit 79; }
  state success "$SIG_OPEN" || exit $?
  id_out "$SIG_OPEN" "$SIG_OPEN_REPO"; exit 0
fi
# A closed issue with the same signature: file new, naming it (recurrence across sources).
[ -n "$CLOSED_HIT" ] || [ -z "$SIG_CLOSED" ] || { SEEN_BEFORE="$SIG_CLOSED"; SEEN_REPO="$SIG_CLOSED_REPO"; }

if [ "$YES" -ne 1 ]; then
  # A bug's full shape is the QA filer's check; run it now so the plan never says
  # would-file for a body --yes would refuse with 66.
  if [ "$TYPE" = bug ]; then "$BIN/super-qa-file-bug.sh" --title "$TITLE" --body-file "$BODY_FILE" --check-body || exit $?; fi
  if [ -n "$CLOSED_HIT" ]; then echo "recurrence|$(ref "$CLOSED_HIT" "$CLOSED_REPO")|${TYPE}|${TITLE}|${HOLD}${TAIL}"
  elif [ -n "${SEEN_BEFORE:-}" ]; then echo "recurrence|$(ref "$SEEN_BEFORE" "$SEEN_REPO")|${TYPE}|${TITLE}|${HOLD}${TAIL}"
  else echo "would-file|${TYPE}|${TITLE}|${HOLD}${TAIL}"; fi
  exit 0
fi

state begin || exit $?

# --- compose + dispatch -----------------------------------------------------
BODY_TMP=$(mktemp); TRACE_TMP=$(mktemp); CR_TMP=$(mktemp)
TMPS+=("$BODY_TMP" "$TRACE_TMP" "$CR_TMP")
HERE_SC=$(cd "$(dirname "$0")" && pwd)
SRC_BODY="$BODY_FILE"
if [ -n "$TRACE_ID" ]; then
  # The Trace line goes at the end of Evidence (header § 6). collect_tempo.py never fails
  # the filing: no collect.tempo → just the id; Tempo down → the id and why.
  python3 "$HERE_SC/collect_tempo.py" --config "$CONFIG" evidence "$TRACE_ID" > "$TRACE_TMP" 2>/dev/null \
    || printf -- '- **Trace:** `%s`\n' "$TRACE_ID" > "$TRACE_TMP"
  python3 -c '
import re, sys
body = open(sys.argv[1], encoding="utf-8").read().splitlines()
block = open(sys.argv[2], encoding="utf-8").read().splitlines()
heads = [i for i, l in enumerate(body) if re.match(r"^#{1,3}\s+Evidence\s*$", l, re.I)]
if heads:
    nxt = [i for i in range(heads[-1] + 1, len(body)) if re.match(r"^#{1,3}\s", body[i])]
    end = nxt[0] if nxt else len(body)
    body[end:end] = [""] + block + [""]
else:
    body += ["", "## Evidence"] + block
# Written as UTF-8 to a file, not stdout: a Windows console code page cannot encode ✅ / →.
open(sys.argv[3], "w", encoding="utf-8", newline=chr(10)).write(chr(10).join(body) + chr(10))' \
    "$BODY_FILE" "$TRACE_TMP" "$CR_TMP" || exit 70
  SRC_BODY="$CR_TMP"
fi
{
  cat "$SRC_BODY"
  if [ -n "$CLOSED_HIT" ]; then
    echo; echo "> Recurrence: this fingerprint was last filed as $(ref "$CLOSED_HIT" "$CLOSED_REPO"), which is closed."
  elif [ -n "$SEEN_BEFORE" ]; then
    echo; echo "> Seen before in $(ref "$SEEN_BEFORE" "$SEEN_REPO") (closed), reported by another source with the same signature."
  fi
  echo; echo "<!-- super-collect-fingerprint: ${FP} -->"
  echo "<!-- super-collect-source: ${SOURCE} -->"
  [ -z "$SIG" ] || echo "<!-- sb-signature: ${SIG} -->"
} > "$BODY_TMP"

# Triage (header § 7): a body card-ready rejects is filed with `needs-triage`. Both filers
# add `## Blocked by: - None.` when it is absent, so it is judged that way. A missing or
# crashing checker counts as not ready. `triage.enabled: false` skips it.
if [ "$(jq -r 'if .triage.enabled == false then "off" else "on" end' "$CONFIG")" = on ]; then
  case "$TYPE" in bug) TL=bug ;; feature) TL=feature ;; *) TL="" ;; esac
  CR_RC=0
  python3 -c '
import json, re, sys
body = open(sys.argv[1], encoding="utf-8").read()
if not re.search(r"^#{1,3}\s+Blocked by\s*$", body, re.I | re.M):
    body += chr(10) + "## Blocked by" + chr(10) + "- None." + chr(10)
print(json.dumps({"number": 0, "title": sys.argv[2], "body": body, "labels": [l for l in sys.argv[3:] if l]}))' \
    "$BODY_TMP" "$TITLE" "$TL" > "$CR_TMP" \
    && python3 "$BIN/super-board-card-ready.py" --from "$CR_TMP" --config "$CONFIG" >/dev/null 2>&1 || CR_RC=$?
  if [ "$CR_RC" -ne 0 ]; then
    case " ${EXTRA_LABELS[*]-} " in *" needs-triage "*) ;; *) EXTRA_LABELS+=("needs-triage") ;; esac
  fi
fi

ERR=$(mktemp)
case "$TYPE" in
  refactor)
    OUT=$("$BIN/super-review-file-refactor.sh" --config "$CONFIG" --title "$TITLE" \
      --body-file "$BODY_TMP" --fingerprint "$FP" --column "$HOLD" ${AREA:+--area "$AREA"} 2>"$ERR"); RC=$? ;;
  *)
    case "$TYPE" in bug) KIND=bug; WEAK=0 ;; feature) KIND=feature; WEAK=1 ;; fix) KIND=tech-debt; WEAK=1 ;; esac
    OUT=$(SUPER_QA_PROJECT_OWNER="$OWNER" SUPER_QA_PROJECT_TITLE="$PTITLE" \
      SUPER_QA_TARGET_OPTION_NAME="$HOLD" SUPER_QA_ALLOW_WEAK_BODY="$WEAK" SUPER_QA_REPO="$REPO_NAME" \
      "$BIN/super-qa-file-bug.sh" --title "$TITLE" --body-file "$BODY_TMP" --kind "$KIND" \
      --priority "$PRIORITY" --fingerprint "$FP" ${AREA:+--area "$AREA"} 2>"$ERR"); RC=$? ;;
esac
cat "$ERR" >&2; rm -f "$ERR"
N=$(echo "$OUT" | tail -1)
# A child may have created the issue before a required placement read failed.
# Preserve its identity without tagging or making further writes after the halt.
if [ "$RC" -eq 79 ]; then [ -z "$N" ] || id_out "$N" "$REPO_NAME"; exit 79; fi
if [ "$RC" -eq 64 ] || [ "$RC" -eq 66 ]; then state known-failure || true; fi
case "$N" in ''|*[!0-9]*) die "filer failed (exit ${RC})" "$([ "$RC" -ne 0 ] && echo "$RC" || echo 70)" ;; esac
if [ "$RC" -ne 0 ]; then
  python3 "$GITHUB_READ" --halt "Collector filing/placement incomplete for ${REPO_NAME}#${N}; reconcile before retrying"
  id_out "$N" "$REPO_NAME"; exit 79
fi
tag "$N" "source:collect" "collect:${SOURCE}" ${EXTRA_LABELS[@]+"${EXTRA_LABELS[@]}"} || { RC=$?; id_out "$N" "$REPO_NAME"; exit "$RC"; }
if [ "$RC" -eq 0 ]; then state success "$N" || exit $?; fi
id_out "$N" "$REPO_NAME"
exit "$RC"
