#!/usr/bin/env python3
"""super-board-card-ready.py — is this card written well enough to build or test?

WHY. A vague card makes a lane agent stall or build the wrong thing, and collector-filed
cards are often thin. The wave planner runs this on every Ready candidate and keeps a
not-ready card out of the wave (label `needs-triage`, see references/triage.md);
super-collect runs it on the body it is about to file. Read-only, stdlib, no GitHub calls:
the caller passes the issue payload it already has.

Rules (writing-standard.md § 3, the ticket format):
  build cards  Problem · Context · Fix · Acceptance Criteria · Risk · Blocked by present and
               non-empty; + Evidence on a `bug` card, holding a log / stack / link / repro.
  `qa` cards   lighter: Problem · Acceptance Criteria · a target (Where line, URL or route).
  every card   >= triage.checkable_ac_min (default 1) checkable `- [ ]` criteria
               (VAGUE_AC below), and a parseable `## Blocked by`, judged by
               super-board-deps.sh itself (its graph via --deps, else deps.sh --from on
               these issues with --repo / --linked from the config, so a foreign
               `owner/repo#N` blocker obeys `linked_repos`), never by a second parser.

Usage:
  super-board-card-ready.py [--from <issue.json>] [--config <cfg.json>] [--deps <deps.json>]
  Input (stdin or --from): one issue {number, title, body, labels, state?, comments?} or a
  list of them. labels: ["bug"] / [{"name":"bug"}] / {"nodes":[…]}.

Stdout: {number, ready, missing: [...], hints: [...]} (a list for list input).
Exit:   0 every card ready · 3 any card not ready · 64 usage / unreadable input.
Fail safe: anything this cannot judge (deps.sh crashed, no body) is not ready.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The one list of non-checkable acceptance criteria. A criterion is vague when, lower-cased
# and stripped of punctuation, it is one of these, or it has fewer than MIN_AC_WORDS words.
VAGUE_AC = (
    "works", "it works", "works correctly", "works properly", "works fine", "works well",
    "works as expected", "it works as expected", "is fixed", "it is fixed", "fixed", "bug is fixed",
    "the bug is fixed", "issue is fixed", "the issue is fixed", "looks good", "looks fine", "looks right",
    "looks ok", "done", "no bugs", "as expected", "behaves correctly", "behaves as expected",
    "tbd", "todo", "n/a", "tests pass", "all tests pass", "is fast", "is faster", "is better",
)
MIN_AC_WORDS = 3
# What makes a bug's Evidence evidence: a stack frame, file:line, a link, an exception or
# error name, a log/trace id, or numbered repro steps.
EVIDENCE_TOKEN = re.compile(
    r"https?://|\w\.\w+:\d+|:line \d+|\bat \S+\(|\w+(Exception|Error)\b|\btrace\b|\breq_\w+"
    r"|^\s*(\d+\.|- [a-z]\.)\s+\S|\b(HTTP|status) ?[45]\d\d\b|\b[45]\d\d\b.*\b(GET|POST|PUT|PATCH|DELETE)\b"
    r"|\b(GET|POST|PUT|PATCH|DELETE) /",
    re.I | re.M)
TARGET = re.compile(r"\*\*Where:\*\*\s*\S|https?://\S+|`/[^`\s]*`|(^|\s)/[a-z0-9][\w/-]*", re.I | re.M)
EMPTY_TEXT = re.compile(r"^(tbd|todo|n/?a|none yet|\(none yet\)|\.\.\.|…|-)$", re.I)

BUILD_SECTIONS = ("Problem", "Context", "Fix", "Acceptance Criteria", "Risk", "Blocked by")
QA_SECTIONS = ("Problem", "Acceptance Criteria")
HINTS = {
    "Problem": "Add `## Problem`: 1–3 sentences on what is wrong or missing, from the user's side.",
    "Context": "Add `## Context`: `- **Where:**` with lettered steps, and `- **Who:**`.",
    "Fix": "Add `## Fix`: the intended change in 1–3 lines.",
    "Acceptance Criteria": "Add `## Acceptance Criteria`: 2–5 `- [ ] <checkable outcome>` lines.",
    "Risk": "Add `## Risk`: 🟢/🟡/🔴 level and one line.",
    "Blocked by": "Add `## Blocked by` with `- #N — why` bullets, or exactly `- None.`.",
    "Evidence": "Add `## Evidence` (bug): the folded 12-row table with the error, stack or log line.",
}


def sections(body):
    """{lower-case heading: text} for every `#`..`###` heading; the LAST one of a name wins
    (a preflight banner may quote a heading in backticks above the real one)."""
    out, name, buf = {}, None, []
    for line in (body or "").splitlines():
        m = re.match(r"^#{1,3}[ \t]+(.+?)[ \t]*:?[ \t]*$", line)
        if m:
            if name is not None:
                out[name] = "\n".join(buf)
            name, buf = m.group(1).strip().lower(), []
        elif name is not None:
            buf.append(line)
    if name is not None:
        out[name] = "\n".join(buf)
    return out


def meaningful(text):
    """Text minus HTML comments and blank lines; '' when only placeholders remain."""
    t = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    lines = [l for l in lines if not EMPTY_TEXT.match(l.lstrip("-* ").strip())]
    return "\n".join(lines)


def criteria(ac_text):
    return [m.group(1).strip() for m in re.finditer(r"^[ \t]*[-*] \[[ xX]\][ \t]+(.+)$", ac_text or "", re.M)]


def checkable(item):
    t = re.sub(r"[`*_]", "", item).strip().lower()
    t = re.sub(r"[.!;:,]+$", "", t).strip()
    return t not in VAGUE_AC and len(t.split()) >= MIN_AC_WORDS


def label_names(labels):
    if isinstance(labels, dict):
        labels = labels.get("nodes") or []
    return sorted({(l.get("name") if isinstance(l, dict) else str(l)).strip().lower()
                   for l in labels or [] if (l.get("name") if isinstance(l, dict) else l)})


# ── Blocked by: super-board-deps.sh's verdict ──────────────────────────────

def bash():
    """Git Bash on Windows (never System32's WSL bash), plain bash elsewhere."""
    if os.name != "nt":
        return "bash"
    helper = HERE / "super-board-github-read.py"
    if helper.exists():
        spec = importlib.util.spec_from_file_location("github_read", helper)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.git_bash()
    return "bash"


def deps_scope(cfg):
    """deps.sh's --repo / --linked from the config, so a foreign `owner/repo#N` blocker is
    judged as the planner judges it (only `linked_repos` are trusted)."""
    remote = str(((cfg or {}).get("repo") or {}).get("remote") or "")
    repo = re.sub(r"^(https?://github\.com/|git@github\.com:)", "", remote)
    repo = re.sub(r"\.git$", "", repo)
    args = ["--repo", repo] if re.fullmatch(r"[\w.-]+/[\w.-]+", repo) else []
    linked = [str(x) for x in (cfg or {}).get("linked_repos") or [] if x]
    return args + (["--linked", ",".join(linked)] if linked else [])


def deps_verdicts(issues, deps_file=None, cfg=None):
    """{number: (parseable, why)}. From the planner's graph when given; else one
    `super-board-deps.sh --from` run over these issues. A failure leaves numbers out."""
    if deps_file:
        with open(deps_file, encoding="utf-8") as f:
            graph = json.load(f)
        return {int(k): (bool(v.get("parseable")), v.get("why") or "") for k, v in graph.items()
                if isinstance(v, dict) and str(k).isdigit()}
    payload = [{"number": i["number"], "title": i.get("title") or "", "body": i.get("body") or "",
                "state": i.get("state") or "OPEN", "labels": {"nodes": [{"name": n} for n in i["labels"]]},
                "comments": i.get("comments") or {"nodes": []}} for i in issues]
    script = HERE / "super-board-deps.sh"
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        r = subprocess.run([bash(), str(script), "--from", path, *deps_scope(cfg)], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        if r.returncode != 0:
            return {}
        graph = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return {int(k): (bool(v.get("parseable")), v.get("why") or "") for k, v in graph.items()}


# ── the check ──────────────────────────────────────────────────────────────

def judge(issue, verdict, ac_min=1):
    missing, hints = [], []

    def need(name, hint=None):
        missing.append(name)
        hints.append(hint or HINTS.get(name, ""))

    body = issue.get("body")
    if not isinstance(body, str) or not body.strip():
        need("body", "The card has no body: write it in the ticket format (writing-standard.md § 3).")
        return missing, hints
    secs = sections(body)
    labels = issue["labels"]
    qa = "qa" in labels
    for name in (QA_SECTIONS if qa else BUILD_SECTIONS):
        if name == "Blocked by":
            continue  # deps.sh's verdict below; it also reads a lane's `blocked-by:` comment
        if not meaningful(secs.get(name.lower(), "")):
            need(name)
    ac = criteria(secs.get("acceptance criteria", ""))
    if "Acceptance Criteria" not in missing:
        good = [c for c in ac if checkable(c)]
        if len(good) < ac_min:
            need("checkable acceptance criterion",
                 f"Write at least {ac_min} `- [ ]` criterion a test can assert (an observable outcome, "
                 f"not \"works\" / \"is fixed\" / \"looks good\")" + (f"; vague now: {ac[0][:60]!r}" if ac else "") + ".")
    if qa:
        target_text = "\n".join(secs.get(k, "") for k in ("context", "problem", "target", "acceptance criteria"))
        if not TARGET.search(target_text):
            need("target", "Name what to test: a `- **Where:**` line, the URL, or the route (`/checkout`).")
    else:
        if "bug" in labels:
            ev = meaningful(secs.get("evidence", ""))
            if not ev:
                need("Evidence")
            elif not EVIDENCE_TOKEN.search(ev):
                need("Evidence", "Evidence names no log line, stack frame, link or repro step: add one.")
        if verdict is None:
            need("Blocked by", "Could not check `## Blocked by` (super-board-deps.sh failed): treated as missing.")
        elif not verdict[0]:
            need("Blocked by", verdict[1] or HINTS["Blocked by"])
    return missing, hints


def normalize(raw):
    if not isinstance(raw, dict):
        raise ValueError("each issue must be an object")
    content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    number = raw.get("number", content.get("number"))
    if not isinstance(number, int):
        raise ValueError("each issue needs an integer number")
    labels = label_names(raw.get("labels") or content.get("labels") or [])
    body = raw["body"] if "body" in raw else content.get("body")
    return {"number": number, "title": raw.get("title") or content.get("title") or "", "body": body,
            "labels": labels, "state": raw.get("state") or "OPEN", "comments": raw.get("comments")}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="src")
    p.add_argument("--config")
    p.add_argument("--deps")
    a = p.parse_args(argv)
    try:
        text = open(a.src, encoding="utf-8").read() if a.src else sys.stdin.read()
        raw = json.loads(text)
        single = isinstance(raw, dict)
        issues = [normalize(x) for x in ([raw] if single else raw)]
        cfg = {}
        if a.config:
            with open(a.config, encoding="utf-8") as f:
                cfg = json.load(f)
        ac_min = int(((cfg.get("triage") or {}).get("checkable_ac_min")) or 1)
    except (OSError, ValueError, TypeError) as e:
        print(f"card-ready: {e}", file=sys.stderr)
        return 64
    build = [i for i in issues if "qa" not in i["labels"]]
    verdicts = deps_verdicts(build, a.deps, cfg) if build else {}
    out = []
    for i in issues:
        missing, hints = judge(i, verdicts.get(i["number"]), max(1, ac_min))
        out.append({"number": i["number"], "ready": not missing, "missing": missing, "hints": hints})
    print(json.dumps(out[0] if single else out, ensure_ascii=False))
    return 0 if all(o["ready"] for o in out) else 3


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
