#!/usr/bin/env python3
"""Guard against shell commands that reach a production environment.

Some projects keep dev and prod on one server: lane workers may run read-only
SSH/docker against dev but must never touch prod. This PreToolUse hook (Bash and
PowerShell) denies any command whose raw text names a prod target, including text
inside an `ssh host '...'` payload or a `docker exec` line -- matching is on the
whole command string, case-insensitive.

Rules come from `guard.prod` in .claude/super-board/configs/*.json (all configs
are merged; a hub config's `members.<k>.guard.prod` too, and in a member checkout the hub
config of an ancestor folder gives the board's and that member's rules):
  "guard": { "prod": {
    "patterns": ["primecore-prod-"],          # regexes
    "hosts":    ["prod.example.com"],          # matched as whole host tokens
    "ports":    [25432, 28200, 18080],         # :PORT, -p PORT, --port PORT, -pPORT
    "message":  "extra text for the agent"
  }}
No config or empty rules -> allow. Out of scope: base64, $(...) and quoting tricks.
Its own errors never block.
"""
import glob
import json
import os
import re
import sys

SHELL_TOOLS = ("Bash", "PowerShell")


def _configs(folder):
    for p in sorted(glob.glob(os.path.join(folder, ".claude", "super-board", "configs", "*.json"))):
        try:
            cfg = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(cfg, dict):
            yield cfg


def _same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def governing(project_dir, levels=4):
    """(config, member key or None) for every config that governs project_dir: its own
    configs, and a hub config in an ancestor naming it as a member checkout (hub.md:
    after the cut-over a member keeps no configs of its own)."""
    out = [(cfg, None) for cfg in _configs(project_dir)]
    here = os.path.dirname(os.path.realpath(project_dir))
    for _ in range(levels):
        for cfg in _configs(here):
            members = cfg.get("members")
            for key, m in (members.items() if isinstance(members, dict) else []):
                path = ((m or {}).get("repo") or {}).get("path") if isinstance(m, dict) else None
                if isinstance(path, str) and path and _same(os.path.join(here, os.path.expanduser(path)), project_dir):
                    out.append((cfg, key))
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return out


def prod_blocks(cfg, member):
    """guard.prod blocks a config applies: the board's, plus each member's (its own config
    names them all; a hub config found from a member checkout names that member's)."""
    blocks = [cfg.get("guard")]
    members = cfg.get("members") if isinstance(cfg.get("members"), dict) else {}
    for key, m in members.items():
        if isinstance(m, dict) and (member is None or key == member):
            blocks.append(m.get("guard"))
    return [(b.get("prod") or {}) for b in blocks if isinstance(b, dict)]


def load_rules(project_dir):
    rules = {"patterns": [], "hosts": [], "ports": [], "message": []}
    for prod in [p for cfg, member in governing(project_dir) for p in prod_blocks(cfg, member)]:
        if not isinstance(prod, dict):
            continue
        for key in ("patterns", "hosts", "ports"):
            vals = prod.get(key) or []
            if isinstance(vals, (list, tuple)):
                rules[key].extend(str(v) for v in vals if str(v).strip())
        if isinstance(prod.get("message"), str) and prod["message"].strip():
            rules["message"].append(prod["message"].strip())
    return rules


def regexes(rules):
    out = []
    for pat in rules["patterns"]:
        try:
            out.append((pat, re.compile(pat, re.IGNORECASE)))
        except re.error:
            out.append((pat, re.compile(re.escape(pat), re.IGNORECASE)))
    for host in rules["hosts"]:
        out.append((host, re.compile(r"(?<![\w.-])" + re.escape(host) + r"(?![\w.-])", re.IGNORECASE)))
    for port in rules["ports"]:
        port = port.lstrip(":")
        if port.isdigit():
            out.append((":" + port, re.compile(
                r"(?::|(?<![\w-])-p\s*|--port[=\s]+)" + port + r"(?!\d)", re.IGNORECASE)))
    return out


def check(command, rules):
    """None when allowed, else the matched rule."""
    for label, rx in regexes(rules):
        if rx.search(command):
            return label
    return None


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))


def main():
    rules = None
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if payload.get("hook_event_name", "PreToolUse") != "PreToolUse":
            return
        if payload.get("tool_name") not in SHELL_TOOLS:
            return
        command = (payload.get("tool_input") or {}).get("command") or ""
        if not command:
            return
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
        rules = load_rules(project_dir)
        hit = check(command, rules)
        if not hit:
            return
        reason = ("This command targets PRODUCTION (matched guard.prod rule %r). Do not run it, do not retry "
                  "it with a different spelling, and do not work around this guard. Stop and report to the "
                  "user what you were about to run and why; only a human acts on prod. Checked by guard-prod."
                  % hit)
        if rules["message"]:
            reason += " " + " ".join(rules["message"])
        deny(reason)
    except Exception:
        # No prod rules configured: stay a no-op like the sibling guards. With rules, a guard
        # that cannot decide must not wave a possible prod command through.
        if rules and any(rules[k] for k in ("patterns", "hosts", "ports")):
            deny("guard-prod could not check this command against the configured guard.prod rules, so it "
                 "is blocked. Stop and report to the user; do not retry it in another form.")


if __name__ == "__main__":
    main()
