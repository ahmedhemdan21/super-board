#!/usr/bin/env bash
# super-board installer.
# Copies skills/, scripts/, workflows/ and the guard hooks into the target
# project's .claude/ tree, and merges the hook settings into
# .claude/settings.json (backed up first, never duplicated).
#
# Usage:
#   ./install.sh [--no-hooks] [--protect-main] [target-project-dir]
#   ./install.sh --hub [--no-hooks] [--protect-main] <hub checkout>
#   ./install.sh --member [--protect-main] [--hub-dir <hub>] [--remove-pack [--apply]] <member checkout>
# Defaults to the current working directory. --no-hooks skips the guard hooks
# and leaves settings.json untouched. --protect-main also wires the opt-in
# guard that blocks direct and force pushes to main/master/base_branch; with
# --no-hooks it installs that one guard alone. The installer asks nothing:
# super-board onboard asks the questions (protect main is one of them).
# Hub boards (hub.md → Cut-over): --hub installs the full pack into the hub checkout
# and validates its hub configs (exit 65 when one is invalid). --member installs only
# the guard hooks, their settings.json wiring and the AGENTS.md block that points at
# the hub. --remove-pack lists the pack files a previous full install left in the
# member (skills, .claude/bin scripts, workflows the pack ships); --apply deletes them.
# Configs under .claude/super-board/, hooks and anything else are never touched.
#
# Output is grouped with emojis, one line per group of files (failures still name
# the file). Environment: SUPER_BOARD_QUIET_NEXT=1 prints only the "🔧 installing"
# group — get.sh sets it and prints its own header and summary around it.
#
# An older super-board already in the target (a different skills/super-board/VERSION,
# or one with no VERSION) is recorded in .claude/super-board/upgrade.json
# {from, to, added_skills}; onboard's 🔍 Checks finishes the upgrade and lists it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS=1
PROTECT=0
MODE=full          # full | hub | member
REMOVE_PACK=0
APPLY=0
HUB_DIR=""
TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-hooks) HOOKS=0 ;;
    --protect-main) PROTECT=1 ;;
    --hub) [ "$MODE" = full ] || { echo "--hub and --member exclude each other" >&2; exit 64; }; MODE=hub ;;
    --member) [ "$MODE" = full ] || { echo "--hub and --member exclude each other" >&2; exit 64; }; MODE=member ;;
    --remove-pack) REMOVE_PACK=1 ;;
    --apply) APPLY=1 ;;
    --hub-dir) [ $# -ge 2 ] || { echo "--hub-dir needs a folder" >&2; exit 64; }; HUB_DIR="$2"; shift ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 64 ;;
    *) TARGET="$1" ;;
  esac
  shift
done
TARGET="${TARGET:-$PWD}"
if [ "$MODE" != member ] && { [ "$REMOVE_PACK" -eq 1 ] || [ "$APPLY" -eq 1 ] || [ -n "$HUB_DIR" ]; }; then
  echo "--remove-pack, --apply and --hub-dir go with --member" >&2; exit 64
fi
[ "$APPLY" -eq 0 ] || [ "$REMOVE_PACK" -eq 1 ] || { echo "--apply goes with --remove-pack" >&2; exit 64; }
if [ "$MODE" = member ] && [ "$HOOKS" -eq 0 ]; then
  echo "--member installs the guard hooks; --no-hooks would leave nothing to install" >&2; exit 64
fi

# Primary: the board and its lanes. Secondary: standalone helpers the lanes
# can call (visual, git-sync, ui-refine-loop). Worktree cleanup (cleanup-wt) is a
# hook, not a skill: it ships with the guard hooks below.
PRIMARY_SKILLS="super-board super-build super-qa super-review super-collect"
SECONDARY_SKILLS="visual git-sync ui-refine-loop"
# Every file the pack copies into .claude/bin and .claude/workflows. --remove-pack
# deletes exactly these (and the skills above) from a member, nothing else.
BIN_SCRIPTS="super-board-project-verifier.py super-board-skills-ready.py super-board-calibration.py super-board-verification-ready.py super-board-github-read.py super-board-collect-state.py super-board-run.sh super-board-gh-guard.sh super-board-card.sh super-board-status.py super-board-wave-plan.sh super-board-codex-lane.sh super-board-codex-wave.sh super-board-deps.sh super-board-preflight.sh super-board-merge-gate.sh super-board-merge-policy.py super-board-approval.py super-board-env-check.sh super-board-agents-md.py super-board-settings.py super-board-setup.py super-board-usage.sh super-board-throttle.sh super-board-host.sh super-board-pr-body.sh super-board-commit-header.py super-review-file-refactor.sh super-qa-file-bug.sh super-board-stop.sh super-board-qa-target.sh super-board-deploy-wait.sh super-board-metrics.py super-board-scheduler.py super-board-card-ready.py super-board-config.py super-board-split.py"
WORKFLOWS="super-board-wave.js ui-refine-loop.js"

if [ ! -d "$TARGET" ]; then
  echo "target directory not found: $TARGET" >&2
  exit 64
fi
TARGET="$(cd "$TARGET" && pwd)"

QUIET="${SUPER_BOARD_QUIET_NEXT:-}"
PACK_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION" 2>/dev/null || echo '?')"

# Shared by every mode.
# File destinations can be links into the pack too. -ef follows symlinks and
# also recognizes hard links, so cp never tries to overwrite its own input.
# Byte-code is never shipped, whichever copy path asks.
copy_file() {
  case "$1" in *.pyc|*.pyo|*/__pycache__/*) return 0 ;; esac
  if [ ! "$1" -ef "$2" ]; then
    cp "$1" "$2"
  fi
}

SETTINGS_PY="$REPO_ROOT/scripts/super-board-settings.py"
# Stdlib-only merge (scripts/super-board-settings.py): keeps every existing key
# and hook, adds each guard command once per event + matcher, backs the file up
# before writing, and leaves an invalid settings.json untouched (exit 2).
# Prints one summary word for the settings line; the helper's own lines (an
# invalid-JSON refusal) go to stderr untouched.
merge_settings() {
  local had=0 res rc=0
  [ -f "$TARGET/.claude/settings.json" ] && had=1
  res=$(python3 "$SETTINGS_PY" hooks "$TARGET/.claude/settings.json" "$@" 2>&1) || rc=$?
  if [ "$rc" -eq 2 ]; then printf '%s\n' "$res" >&2; echo "not valid JSON — left untouched"
  elif echo "$res" | grep -q "already present"; then echo "already present"
  elif [ "$had" -eq 1 ]; then echo "backup kept"
  else echo "new file"; fi
}

install_hooks() {
  if [ "$HOOKS" -eq 1 ]; then
    mkdir -p "$TARGET/.claude/hooks"
    # hooks/*.py only: hooks/dev/ holds gates for repos that author skills, never installed.
    local n=0 h snippets
    for h in "$REPO_ROOT"/hooks/*.py; do
      [ -f "$h" ] || continue
      copy_file "$h" "$TARGET/.claude/hooks/$(basename "$h")"
      chmod +x "$TARGET/.claude/hooks/$(basename "$h")"
      n=$((n + 1))
    done
    snippets="$REPO_ROOT/hooks/settings-snippet.json"
    if [ "$PROTECT" -eq 1 ]; then snippets="$snippets $REPO_ROOT/hooks/settings-protect-main.json"; fi
    # shellcheck disable=SC2086
    echo "   🛡️  $n guard hooks → .claude/settings.json ($(merge_settings $snippets))"
  elif [ "$PROTECT" -eq 1 ]; then
    # --no-hooks --protect-main: the one guard asked for, nothing else. This is the
    # command onboard names when its protect-main question finds no guard script.
    mkdir -p "$TARGET/.claude/hooks"
    copy_file "$REPO_ROOT/hooks/guard-protected-push.py" "$TARGET/.claude/hooks/guard-protected-push.py"
    chmod +x "$TARGET/.claude/hooks/guard-protected-push.py"
    echo "   🛡️  --no-hooks: only the push guard → .claude/settings.json ($(merge_settings "$REPO_ROOT/hooks/settings-protect-main.json"))"
  else
    echo "   ⏭️  guard hooks skipped (--no-hooks)"
  fi
}

# The pack-owned paths present in the target: the skills, .claude/bin scripts and
# workflows this installer copies. One per line, relative to the target.
pack_paths() {
  local s
  for s in $PRIMARY_SKILLS $SECONDARY_SKILLS; do
    { [ -e "$TARGET/.claude/skills/$s" ] || [ -L "$TARGET/.claude/skills/$s" ]; } && echo ".claude/skills/$s"
  done
  for s in $BIN_SCRIPTS; do
    { [ -e "$TARGET/.claude/bin/$s" ] || [ -L "$TARGET/.claude/bin/$s" ]; } && echo ".claude/bin/$s"
  done
  for s in $WORKFLOWS; do
    { [ -e "$TARGET/.claude/workflows/$s" ] || [ -L "$TARGET/.claude/workflows/$s" ]; } && echo ".claude/workflows/$s"
  done
  return 0
}

# ───────────────────────── member mode (hub.md → Cut-over) ─────────────────────────
# A member checkout of a hub board keeps the guard hooks and the AGENTS.md block;
# the pack lives in the hub. Nothing here copies skills, scripts or workflows.
if [ "$MODE" = member ]; then
  if [ "$TARGET" -ef "$REPO_ROOT" ]; then
    echo "refusing --member on the pack's own folder: $TARGET" >&2; exit 64
  fi
  if python3 - "$TARGET" <<'PY'
import glob, json, os, sys
for p in glob.glob(os.path.join(sys.argv[1], ".claude", "super-board", "configs", "*.json")):
    try:
        if "members" in json.load(open(p, encoding="utf-8")):
            sys.exit(0)
    except Exception:
        pass
sys.exit(1)
PY
  then
    echo "refusing --member: $TARGET holds a hub config (members) — it is the hub; use --hub" >&2; exit 64
  fi
  HUB_JSON=$(python3 "$REPO_ROOT/scripts/super-board-config.py" hub-of "$TARGET" ${HUB_DIR:+--hub-dir "$HUB_DIR"} 2>/dev/null) || HUB_JSON=""
  [ -n "$QUIET" ] || echo "🧩 super-board $PACK_VERSION → $TARGET (hub member)"
  echo "🔧 installing"
  install_hooks
  # AGENTS.md: the member block names the hub, its slug and how to run the board.
  VARS=$(python3 - "$TARGET" "$HUB_JSON" <<'PY'
import json, os, sys
target, raw = sys.argv[1], sys.argv[2]
hub = json.loads(raw) if raw.strip() else {}
rel = os.path.relpath(hub["hub_root"], target).replace(os.sep, "/") if hub else ""
print("\n".join([
    "member=" + (hub.get("member") or "<member>"),
    "slug=" + (hub.get("slug") or "<hub slug>"),
    "hub_repo=" + (hub.get("hub_repo") or "<hub repo>"),
    "hub_path=" + (rel or "<hub checkout>"),
]))
PY
)
  VAR_ARGS=()
  HUB_SLUG=""; HUB_MEMBER=""; HUB_REPO=""
  VARS=$(printf '%s' "$VARS" | tr -d '\r')
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    VAR_ARGS+=(--var "$line")
    case "$line" in slug=*) HUB_SLUG="${line#slug=}" ;; member=*) HUB_MEMBER="${line#member=}" ;; hub_repo=*) HUB_REPO="${line#hub_repo=}" ;; esac
  done <<< "$VARS"
  if python3 "$REPO_ROOT/scripts/super-board-agents-md.py" block --root "$TARGET" --create \
       --template "$REPO_ROOT/skills/super-board/references/agents-md-member-block.md" \
       --version "$PACK_VERSION" "${VAR_ARGS[@]}" >/dev/null 2>&1; then
    echo "   📜 AGENTS.md: hub member block written"
  else
    echo "   ! AGENTS.md: couldn't write the hub member block — check its markers"
  fi
  if [ -n "$HUB_JSON" ]; then
    echo "   🏠 member $HUB_MEMBER of hub board $HUB_SLUG ($HUB_REPO)"
  else
    echo "   ! no hub config names this checkout as a member — the block has placeholders; pass --hub-dir <hub> or add the member to the hub config"
  fi
  STALE=$(pack_paths)
  if [ "$REMOVE_PACK" -eq 1 ]; then
    if [ -z "$STALE" ]; then
      echo "   ✓ no pack files left from a full install"
    elif [ "$APPLY" -eq 0 ]; then
      echo "   🔎 dry run — these pack files would be removed (add --apply to delete them):"
      printf '%s\n' "$STALE" | sed 's/^/      - /'
    else
      echo "   🧹 removing the pack files of a previous full install:"
      removed=0
      while IFS= read -r rel; do
        [ -n "$rel" ] || continue
        p="$TARGET/$rel"
        name=$(basename "$rel")
        case "$rel" in
          .claude/skills/*) src="$REPO_ROOT/skills/$name" ;;
          .claude/bin/*) src="$REPO_ROOT/scripts/$name" ;;
          .claude/workflows/*) src="$REPO_ROOT/workflows/$name" ;;
          *) echo "      ✗ refusing $rel: not a pack path" >&2; continue ;;
        esac
        if [ -L "$p" ]; then
          rm -f "$p"   # the link only, never what it points at
        elif [ "$p" -ef "$src" ]; then
          echo "      ✗ kept $rel: it is the pack's own source" >&2; continue
        elif [ -d "$p" ]; then
          rm -rf "${p:?}"
        else
          rm -f "$p"
        fi
        echo "      - $rel"
        removed=$((removed + 1))
      done <<< "$STALE"
      for d in bin workflows skills; do rmdir "$TARGET/.claude/$d" 2>/dev/null || true; done
      echo "   ✓ removed $removed pack path(s); .claude/super-board/ (configs) and hooks kept"
    fi
  elif [ -n "$STALE" ]; then
    echo "   ! $(printf '%s\n' "$STALE" | wc -l | tr -d ' ') pack files from a full install are still here — list them: install.sh --member --remove-pack $TARGET"
  fi
  if [ -z "$QUIET" ]; then
    echo "🎉 $TARGET is a hub member: guard hooks only"
    if [ -n "$HUB_JSON" ]; then
      echo "👉 next: run the board from the hub checkout: /super-board run $HUB_SLUG"
    else
      echo "👉 next: run the board from the hub checkout"
    fi
  fi
  exit 0
fi

# Record an upgrade BEFORE the copy overwrites the old VERSION.
PREV_VERSION=""
if [ -d "$TARGET/.claude/skills/super-board" ]; then
  PREV_VERSION="$(tr -d '[:space:]' < "$TARGET/.claude/skills/super-board/VERSION" 2>/dev/null || echo 'older')"
  [ -n "$PREV_VERSION" ] || PREV_VERSION="older"
fi
ADDED_SKILLS=""
if [ -n "$PREV_VERSION" ]; then
  for skill in $PRIMARY_SKILLS $SECONDARY_SKILLS; do
    [ -e "$TARGET/.claude/skills/$skill" ] || ADDED_SKILLS="$ADDED_SKILLS $skill"
  done
fi

[ -n "$QUIET" ] || echo "🧩 super-board $PACK_VERSION → $TARGET$([ "$MODE" = hub ] && echo " (hub)")"
echo "🔧 installing"

# Resolve all skill paths before replacing anything. A destination can contain
# the pack (or a linked source skill); clearing it would destroy our input.
# A destination inside its source would instead recursively copy into itself.
python3 - "$REPO_ROOT" "$TARGET" $PRIMARY_SKILLS $SECONDARY_SKILLS <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2])
for skill in sys.argv[3:]:
    src = (root / "skills" / skill).resolve()
    dest = (target / ".claude" / "skills" / skill).resolve()
    if src == dest:
        continue
    if dest == root or dest in root.parents or dest in src.parents or src in dest.parents:
        sys.exit(f"    ✗ refusing to install {skill}: source and destination overlap ({dest})")
PY

mkdir -p "$TARGET/.claude/skills" "$TARGET/.claude/bin"

# A project may keep the real skill directories elsewhere and symlink them into
# .claude/skills — a layout some repos use to share one tree between .claude and
# another agent host. `cp -R src dest/` fails outright on such a target ("Not a
# directory"), so resolve the link and write to where it actually points. That
# keeps the project's chosen layout instead of replacing it with a plain copy.
for skill in $PRIMARY_SKILLS $SECONDARY_SKILLS; do
  if [ ! -d "$REPO_ROOT/skills/$skill" ]; then
    echo "    ✗ missing $skill in repo — skipping" >&2
    continue
  fi
  dest="$TARGET/.claude/skills/$skill"
  note=""
  if [ -L "$dest" ]; then
    # `cd -P` walks the link to its physical target in one step. Do NOT rebuild
    # the path from `readlink` output: `cd ""` SUCCEEDS in bash, so an empty
    # readlink silently leaves you in the parent, and the rm below then empties
    # .claude/skills instead of one skill. That is not hypothetical — it deleted
    # 190 entries on a real repo on 2026-08-20 before this comment existed.
    resolved=$(cd -P "$dest" 2>/dev/null && pwd) || resolved=""
    if [ -z "$resolved" ]; then
      echo "    ✗ $skill is a symlink pointing nowhere — skipping rather than replacing it" >&2
      continue
    fi
    dest="$resolved"
    note=" (via symlink → ${resolved#$TARGET/})"
  fi

  # Belt and braces: whatever the resolution produced, it must be a path ENDING
  # in this skill's own name. Anything else is a bug in the line above, and the
  # blast radius of being wrong here is the user's entire skills tree.
  case "$dest" in
    */"$skill") : ;;
    *) echo "    ✗ refusing to write $skill: resolved to '$dest', which is not a $skill directory" >&2
       continue ;;
  esac

  # A self-vendored project can expose this very source through one or more
  # links. There is nothing to copy in that case; clearing dest erases src too.
  if [ "$REPO_ROOT/skills/$skill" -ef "$dest" ]; then
    echo "   ✓ $skill already uses the source directory"
    continue
  fi

  # Replace the contents, not the directory itself, so a symlink stays a symlink.
  mkdir -p "$dest"
  rm -rf "${dest:?}/"* 2>/dev/null || true
  cp -R "$REPO_ROOT/skills/$skill/." "$dest/"
  # Python byte-code from running the pack's own tests is machine-local junk: drop it
  # from the copy (only under $dest, which was just written from the pack).
  find "$dest" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$dest" -name '*.py[co]' -type f -exec rm -f {} + 2>/dev/null || true
  [ -z "$note" ] || echo "   ✓ $skill$note"
done

for script in $BIN_SCRIPTS; do
  if [ -f "$REPO_ROOT/scripts/$script" ]; then
    copy_file "$REPO_ROOT/scripts/$script" "$TARGET/.claude/bin/$script"
    chmod +x "$TARGET/.claude/bin/$script"
  fi
done

mkdir -p "$TARGET/.claude/workflows"
for wf in $WORKFLOWS; do
  if [ -f "$REPO_ROOT/workflows/$wf" ]; then
    copy_file "$REPO_ROOT/workflows/$wf" "$TARGET/.claude/workflows/$wf"
  fi
done
echo "   ✓ skills, scripts and workflows → .claude/"

install_hooks

# AGENTS.md: a re-install refreshes the managed super-board block and touches
# nothing outside its markers. No block yet → onboard writes it (with consent).
# A hub's block also lists its hub boards (slug, members), one per valid hub config.
HUB_CONFIGS=()
for c in "$TARGET"/.claude/super-board/configs/*.json; do
  [ -f "$c" ] || continue
  if python3 -c 'import json,sys; sys.exit(0 if "members" in json.load(open(sys.argv[1], encoding="utf-8")) else 1)' "$c" 2>/dev/null; then
    HUB_CONFIGS+=("$c")
  fi
done
if [ -f "$TARGET/AGENTS.md" ] && grep -q '^<!-- super-board:begin' "$TARGET/AGENTS.md"; then
  HUB_ARGS=()
  for c in ${HUB_CONFIGS[@]+"${HUB_CONFIGS[@]}"}; do
    python3 "$REPO_ROOT/scripts/super-board-config.py" validate --config "$c" >/dev/null 2>&1 && HUB_ARGS+=(--hub-config "$c")
  done
  python3 "$REPO_ROOT/scripts/super-board-agents-md.py" block --root "$TARGET" \
    --template "$REPO_ROOT/skills/super-board/references/agents-md-block.md" \
    --version "$PACK_VERSION" ${HUB_ARGS[@]+"${HUB_ARGS[@]}"} >/dev/null 2>&1 \
    && echo "   📜 AGENTS.md: super-board block refreshed" \
    || echo "   ! AGENTS.md: couldn't refresh the super-board block — onboard repairs it"
fi
if [ -f "$TARGET/CLAUDE.md" ] && ! grep -m1 -v '^[[:space:]]*$' "$TARGET/CLAUDE.md" | grep -qx '@AGENTS.md'; then
  echo "   ! CLAUDE.md holds its own rules — onboard offers a merge"
fi

# Upgrade record for onboard's 🔍 Checks (it migrates config, labels, columns).
if [ -n "$PREV_VERSION" ] && [ "$PREV_VERSION" != "$PACK_VERSION" ]; then
  mkdir -p "$TARGET/.claude/super-board"
  python3 - "$TARGET/.claude/super-board/upgrade.json" "$PREV_VERSION" "$PACK_VERSION" $ADDED_SKILLS <<'PY'
import json, sys, time
path, frm, to, added = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
try:
    old = json.load(open(path))
except Exception:
    old = {}
rec = {"from": old.get("from", frm), "to": to, "added_skills": sorted(set(old.get("added_skills", []) + added)),
       "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
json.dump(rec, open(path, "w"), indent=2)
PY
  echo "   ⬆️  upgrade from $PREV_VERSION — onboard finishes it (config, labels, columns)"
fi

# --hub: validate every hub config here and report each member checkout: exists, its
# guard hooks, and any full pack left from before the cut-over (hub.md → Cut-over).
HUB_RC=0
if [ "$MODE" = hub ]; then
  if [ "${#HUB_CONFIGS[@]}" -eq 0 ]; then
    echo "   ! no hub config yet (.claude/super-board/configs/<slug>.json with members) — onboard writes one, then re-run --hub"
  fi
  for c in ${HUB_CONFIGS[@]+"${HUB_CONFIGS[@]}"}; do
    python3 - "$TARGET/.claude/bin/super-board-config.py" "$c" "$REPO_ROOT/install.sh" "$BIN_SCRIPTS" <<'PY' || HUB_RC=65
import json, os, subprocess, sys
resolver, config, installer, bins = sys.argv[1:5]
sys.stdout.reconfigure(encoding="utf-8")
slug = os.path.splitext(os.path.basename(config))[0]
check = subprocess.run([sys.executable, resolver, "validate", "--config", config], capture_output=True, text=True)
if check.returncode:
    print(f"   ✗ hub config {slug} is invalid:")
    for line in check.stderr.strip().splitlines():
        print(f"      {line}")
    sys.exit(65)
members = json.loads(subprocess.run([sys.executable, resolver, "members", "--config", config],
                                    capture_output=True, text=True, check=True).stdout)
print(f"   🏠 hub config {slug}: valid · {len(members)} member(s) · run: /super-board run {slug}")
todo = []
for m in members:
    path = m["path"]
    if not os.path.isdir(path):
        print(f"      ✗ {m['key']}: checkout {path} not found (git submodule update --init?)")
        continue
    hooks = os.path.isfile(os.path.join(path, ".claude", "hooks", "guard-worktree-path.py"))
    stale = sum(os.path.exists(os.path.join(path, ".claude", "bin", b)) for b in bins.split())
    stale += os.path.isdir(os.path.join(path, ".claude", "skills", "super-board"))
    bits = ["hooks ✓" if hooks else "hooks missing"]
    if stale:
        bits.append(f"old full pack ({stale} files)")
    print(f"      {'✓' if hooks and not stale else '!'} {m['key']} ({m['repo']}): " + " · ".join(bits))
    if not hooks or stale:
        todo.append(f"{installer} --member{' --remove-pack' if stale else ''} {path}")
for cmd in todo:
    print(f"      👉 {cmd}")
PY
  done
fi

if [ -z "$QUIET" ]; then
  echo "🎉 super-board $PACK_VERSION is installed"
  echo "👉 next: open Claude Code here and run /super-board onboard"
fi
exit "$HUB_RC"
