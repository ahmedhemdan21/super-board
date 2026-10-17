#!/usr/bin/env python3
"""Guard against shell commands that reach a production environment.

Some projects keep dev and prod on one server: lane workers may run read-only
SSH/docker against dev but must never touch prod. This PreToolUse hook (Bash and
PowerShell) denies any command whose raw text names a prod target, including text
inside an `ssh host '...'` payload or a `docker exec` line -- matching is on the
whole command string, case-insensitive.

Rules come from `guard.prod` in .claude/super-board/configs/*.json (all configs
are merged):
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


def load_rules(project_dir):
    rules = {"patterns": [], "hosts": [], "ports": [], "message": []}
    for p in sorted(glob.glob(os.path.join(project_dir, ".claude", "super-board", "configs", "*.json"))):
        try:
            prod = (json.load(open(p, encoding="utf-8")).get("guard") or {}).get("prod") or {}
        except (OSError, ValueError, AttributeError):
            continue
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


def main():
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
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    except Exception:
        pass


if __name__ == "__main__":
    main()
