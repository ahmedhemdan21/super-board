#!/usr/bin/env python3
"""super-board-commit-header.py — check or build a commit / PR-title header in the
project's format (writing-standard.md § 1, `writing.commit_format`).

    super-board-commit-header.py check  [--config C | --format F] [--max N] [--pr N] "<header>"
    super-board-commit-header.py format [--config C | --format F] --type T --scope S --subject "<s>"
                                        [--repairs-bug] [--breaking]

Formats: "emoji" (default: `✨ [feat] scope: subject`, ≤ 72 chars) and "conventional"
(`feat(scope)!: subject`, @commitlint/config-conventional). In conventional mode the length
counts GitHub's squash suffix ` (#<PR>)`; without --pr it reserves 8 characters.
`--max` overrides `writing.header_max` (conventional default 100).

`check` prints one JSON object. Exit: 0 ok · 65 header breaks the format, or `wip` asked
of conventional · 64 usage · 66 config unreadable. Stdlib only.
"""
from __future__ import annotations

import json
import re
import sys

EMOJI = {
    "feat": "✨", "fix": "🐛", "chore": "🔧", "refactor": "♻️", "test": "🧪", "perf": "⚡",
    "docs": "📝", "ci": "👷", "ui": "💄", "security": "🔒", "revert": "⏪", "wip": "🚧",
}
EMOJI_RE = re.compile(
    r"^(?P<emoji>✨|🐛|🔧|♻️|🧪|⚡|📝|👷|💄|🔒|⏪|🚧) \[(?P<type>feat|fix|chore|refactor|test|perf|docs|ci|ui|security|revert|wip)\] "
    r"[a-z0-9][a-z0-9._/-]*: \S"
)
CONVENTIONAL_TYPES = ["build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test"]
CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z0-9][a-z0-9._/-]*)\))?(?P<bang>!)?: (?P<subject>\S.*)$"
)
# super-board type → conventional type. ui and security are handled in to_conventional.
SAME = {"feat", "fix", "chore", "refactor", "test", "perf", "docs", "ci", "revert"}
DEFAULT_MAX = {"emoji": 72, "conventional": 100}
SUFFIX_RESERVE = 8  # " (#nnnn)"


class Refused(Exception):
    pass


def settings(config=None, fmt=None, max_len=None):
    w = (config or {}).get("writing") or {}
    fmt = fmt or w.get("commit_format") or "emoji"
    if fmt not in DEFAULT_MAX:
        raise ValueError(f"unknown commit_format: {fmt}")
    hm = max_len if max_len is not None else w.get("header_max")
    return fmt, int(hm) if hm is not None else DEFAULT_MAX[fmt]


def to_conventional(sb_type, scope, subject, repairs_bug=False, breaking=False):
    if sb_type == "wip":
        raise Refused("wip is not a conventional type: use a partial shape (writing-standard.md § 1)")
    if sb_type in SAME:
        t = sb_type
    elif sb_type == "ui":
        t = "fix" if repairs_bug else "feat"
    elif sb_type == "security":
        t, scope = "fix", scope or "security"
    else:
        raise Refused(f"unknown super-board type: {sb_type}")
    subject = subject.strip().rstrip(".")
    if not subject[1:2].isupper():  # keep acronyms: "API docs", not "aPI docs"
        subject = subject[:1].lower() + subject[1:]
    return f"{t}{f'({scope})' if scope else ''}{'!' if breaking else ''}: {subject}"


def format_header(fmt, sb_type, scope, subject, repairs_bug=False, breaking=False):
    if fmt == "conventional":
        return to_conventional(sb_type, scope, subject, repairs_bug, breaking)
    if sb_type not in EMOJI:
        raise Refused(f"unknown super-board type: {sb_type}")
    return f"{EMOJI[sb_type]} [{sb_type}] {scope}: {subject.strip()}"


def check(header, fmt, max_len, pr=None):
    """Return (ok, reason, counted length)."""
    if fmt == "emoji":
        m = EMOJI_RE.match(header)
        n = len(header)
        if not m:
            return False, "not `<emoji> [<type>] <scope>: <subject>`", n
        if EMOJI[m["type"]] != m["emoji"]:
            return False, f"emoji does not match [{m['type']}]", n
        return (n <= max_len, None if n <= max_len else f"{n} chars > {max_len}", n)
    m = CONVENTIONAL_RE.match(header)
    suffix = len(f" (#{pr})") if pr else SUFFIX_RESERVE
    if header.endswith(f" (#{pr})") and pr:
        suffix = 0  # the caller already added GitHub's suffix
    n = len(header) + suffix
    if not m:
        return False, "not `<type>(<scope>)!: <subject>`", n
    if m["type"] not in CONVENTIONAL_TYPES:
        why = "wip is not a conventional type" if m["type"] == "wip" else f"type {m['type']} not in config-conventional"
        return False, why, n
    if m["subject"][:1].isupper() and not m["subject"][1:2].isupper():  # sentence-case; acronyms pass
        return False, "subject must start lower-case", n
    if m["subject"].endswith("."):
        return False, "subject must not end with a period", n
    if n > max_len:
        return False, f"{n} chars (with the (#PR) suffix) > {max_len}", n
    return True, None, n


def flag(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 64
    cmd, rest = argv[0], argv[1:]
    cfg = None
    if flag(rest, "--config"):
        try:
            with open(flag(rest, "--config"), encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError) as e:
            print(f"config unreadable: {e}", file=sys.stderr)
            return 66
    try:
        mx = flag(rest, "--max")
        fmt, max_len = settings(cfg, flag(rest, "--format"), int(mx) if mx else None)
        if cmd == "check":
            vals = {"--config", "--format", "--max", "--pr"}
            pos = [a for i, a in enumerate(rest) if not a.startswith("--") and (i == 0 or rest[i - 1] not in vals)]
            if len(pos) != 1:
                return 64
            ok, why, n = check(pos[0], fmt, max_len, flag(rest, "--pr"))
            print(json.dumps({"ok": ok, "format": fmt, "max": max_len, "length": n, "reason": why}, ensure_ascii=False))
            return 0 if ok else 65
        if cmd == "format":
            t, subject = flag(rest, "--type"), flag(rest, "--subject")
            if not (t and subject):
                return 64
            print(format_header(fmt, t, flag(rest, "--scope", ""), subject,
                                "--repairs-bug" in rest, "--breaking" in rest))
            return 0
    except Refused as e:
        print(str(e), file=sys.stderr)
        return 65
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 64
    print(__doc__, file=sys.stderr)
    return 64


if __name__ == "__main__":
    # Windows pipes default to the ANSI code page; super-board text is UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main(sys.argv[1:]))
