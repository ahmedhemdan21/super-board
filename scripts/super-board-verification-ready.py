#!/usr/bin/env python3
"""Read-only verification configuration gate. Exit 9 means repair setup, not code."""
import argparse
import json
import sys


def readiness_error(config):
    """Return a setup error, or None when at least one project command is usable.
    A hub config is ready when every member is (its own keys over the board's)."""
    if not isinstance(config, dict):
        return "config must be a JSON object"
    if isinstance(config.get("members"), dict):
        for key, member in config["members"].items():
            merged = {**config, **(member if isinstance(member, dict) else {})}
            merged.pop("members")
            error = readiness_error(merged)
            if error:
                return f"members.{key}: {error}"
        return None
    commands = config.get("verify_commands")
    if not isinstance(commands, list):
        return "verify_commands must be an array of project command strings"
    # An entry is a command string, or {"cmd": "<command>", "retry": false} to
    # keep the merge gate from rerunning it (verify_retry).
    entries = commands
    commands = []
    for c in entries:
        if isinstance(c, dict):
            if not isinstance(c.get("cmd"), str) or not isinstance(c.get("retry", True), bool) \
                    or set(c) - {"cmd", "retry"}:
                return 'verify_commands objects must be {"cmd": "<command>", "retry": false}'
            c = c["cmd"]
        if not isinstance(c, str):
            return "verify_commands must be an array of project command strings"
        commands.append(c)
    retry = config.get("verify_retry", 1)
    if type(retry) is not int or retry < 0:
        return "verify_retry must be a whole number (0 = never rerun a failed test command)"
    if any("\x00" in c for c in commands):
        return "verify_commands must not contain NUL bytes (shells cannot preserve them)"
    if not any(c.strip() for c in commands):
        return "verify_commands has no usable project commands"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="config file, or - for stdin")
    args = parser.parse_args()
    try:
        if args.config == "-":
            config = json.load(sys.stdin)
        else:
            with open(args.config, encoding="utf-8") as source:
                config = json.load(source)
        error = readiness_error(config)
    except (OSError, ValueError) as exc:
        error = f"cannot read config: {exc}"
    if error:
        print(f"verification-readiness: {error}. Set verify_commands in "
              f"{args.config if args.config != '-' else 'the active config'} "
              "to the owning project's real checks, then restart the board. "
              "No merge or dispatch was authorized.", file=sys.stderr)
        return 9
    return 0


if __name__ == "__main__":
    # Windows pipes default to the ANSI code page; super-board text is UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
