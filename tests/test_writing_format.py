"""Format tests for the writing standard (skills/super-board/references/writing-standard.md).

Pins the shapes scripts and agents depend on: the commit subject, the ticket's required
sections, the comment header, the PR body markers. Each test reads the shipped docs and
scripts, so a template that drifts from the standard fails here.

Run directly:  python3 tests/test_writing_format.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# PATH's bash: on Windows a bare 'bash' finds System32's WSL bash before Git Bash.
BASH = shutil.which('bash') or 'bash'
REF = ROOT / "skills" / "super-board" / "references"
STANDARD = (REF / "writing-standard.md").read_text(encoding="utf-8")

EMOJI = {
    "feat": "✨", "fix": "🐛", "chore": "🔧", "refactor": "♻️", "test": "🧪", "perf": "⚡",
    "docs": "📝", "ci": "👷", "ui": "💄", "security": "🔒", "revert": "⏪", "wip": "🚧",
}
SUBJECT = re.compile(
    r"^(?P<emoji>✨|🐛|🔧|♻️|🧪|⚡|📝|👷|💄|🔒|⏪|🚧) \[(?P<type>feat|fix|chore|refactor|test|perf|docs|ci|ui|security|revert|wip)\] "
    r"[a-z0-9][a-z0-9._/-]*: \S"
)
COMMENT_HEADER = re.compile(
    r"^\[(builder|qa|reviewer|collect|orchestrator|scheduler)\] \[(blocker|issue|suggestion|nit|question|praise|report)\] \S"
)
TICKET_SECTIONS = ["Problem", "Context", "Fix", "Acceptance Criteria", "Risk", "Blocked by"]
EVIDENCE_ROWS = [
    "Error + stack", "Request / trace ID", "Sentry", "PostHog replay", "Logs", "Screenshots",
    "HAR / API sample", "Env + release", "First / last seen", "Users affected", "Steps",
    "Expected / actual",
]
PR_BLOCKS = ["status", "problem", "solution", "ac", "history", "visual", "risk"]


def subject_ok(s: str) -> bool:
    m = SUBJECT.match(s)
    return bool(m) and EMOJI[m["type"]] == m["emoji"] and len(s) <= 72


def fenced(text: str) -> list[str]:
    return re.findall(r"```[a-z]*\n(.*?)```", text, flags=re.S)


# ── commit ───────────────────────────────────────────────────────────────────

def test_subject_regex():
    good = [
        "🐛 [fix] receipts: show a size error over 10 MB",
        "🔧 [chore] loop: close #42 — stream replies",
        "🚧 [wip] loop: #42 partial — schema only",
        "🧪 [test] super-qa: iter 3 (2 bugs, 9 items, 1 PRs opened)",
        "💄 [ui] reports: before/after screenshots",
    ]
    bad = [
        "fix(receipts): show a size error",          # conventional commits
        "🐛 fix receipts: show a size error",          # no brackets
        "✨ [fix] receipts: show a size error",        # emoji does not match type
        "🐛 [bugfix] receipts: show a size error",     # unknown type
        "🐛 [fix] Receipts: show a size error",        # scope not lowercase
        "🐛 [fix] receipts: " + "x" * 80,              # too long
    ]
    for s in good:
        assert subject_ok(s), f"should pass: {s}"
    for s in bad:
        assert not subject_ok(s), f"should fail: {s}"


def test_emoji_map_matches_standard():
    rows = dict((t, e) for e, t in re.findall(r"^\| (\S+) \| `([a-z]+)` \|", STANDARD, flags=re.M))
    assert rows == EMOJI, f"writing-standard.md emoji table drifted: {rows}"
    claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for t, e in EMOJI.items():
        assert f"{e} {t}" in claude_md, f"CLAUDE.md writing table lacks '{e} {t}'"


def test_standard_examples_are_valid_subjects():
    seen = 0
    for block in fenced(STANDARD):
        first = block.splitlines()[0] if block.strip() else ""
        if re.match(r"^[^\s\[<]\S* \[[a-z]+\] ", first):
            if re.match(r"^\S+ \[(bug|feat|ui|test|docs|refactor|security)\] ", first) and "## Problem" in block:
                continue  # a ticket title, checked below
            assert subject_ok(first), f"example subject is off-standard: {first}"
            seen += 1
    assert seen >= 1, "expected a commit example in writing-standard.md"


def test_dispatcher_greps_the_standard_loop_subjects():
    src = (ROOT / "skills" / "super-build" / "scripts" / "super-build-dispatch.sh").read_text(encoding="utf-8")
    pats = re.findall(r'grep -E "\^\[0-9a-f\]\+ (.*?)" \| head', src)
    assert len(pats) == 2, f"expected the close and wip greps, found {pats}"
    close_pat, wip_pat = (p.replace("$N", "42").replace("\\\\", "\\") for p in pats)

    def grep(pat: str, subject: str) -> bool:
        r = subprocess.run(["grep", "-E", "^[0-9a-f]+ " + pat], input=f"abc123 {subject}\n",
                           capture_output=True, text=True, encoding="utf-8")
        return r.returncode == 0

    assert grep(close_pat, "🔧 [chore] loop: close #42 — add streaming")
    assert not grep(close_pat, "🔧 [chore] loop: close #421 — other card")
    assert grep(wip_pat, "🚧 [wip] loop: #42 partial — schema only")
    assert grep(close_pat, "chore(loop): close #42 — legacy branch")


def test_no_old_commit_shapes_in_templates():
    # chore(loop) is not here: it is the conventional-format Builder subject (§ 1).
    old = re.compile(r"wip\(loop\)|fix\(super-qa\)|feat\(super-qa\)|refine\(\$?\{?[a-z<]")
    allowed = {"super-build-dispatch.sh"}  # documents and still accepts the legacy shapes
    for p in list((ROOT / "skills").rglob("*.md")) + list((ROOT / "skills").rglob("*.sh")) + list((ROOT / "workflows").glob("*.js")):
        if p.name in allowed or "visual" in p.parts:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            assert not old.search(line), f"old commit shape at {p.relative_to(ROOT)}:{n}: {line.strip()}"


# ── conventional format (writing.commit_format) ──────────────────────────────

HEADER = ROOT / "scripts" / "super-board-commit-header.py"


def header(*args: str, cfg: dict | None = None) -> tuple[int, str]:
    extra: list[str] = []
    if cfg is not None:
        import json, tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(cfg, f); f.close()
        extra = ["--config", f.name]
    r = subprocess.run([sys.executable, str(HEADER), args[0], *extra, *args[1:]],
                       capture_output=True, text=True, encoding="utf-8")
    return r.returncode, (r.stdout + r.stderr).strip()


CONV = {"writing": {"commit_format": "conventional", "header_max": 110}}


def test_conventional_header_validation():
    good = ["fix(receipts): show a size error over 10 MB", "feat(chat)!: stream replies",
            "chore(loop): close #42 — stream replies", "test(super-qa): iter 3 (2 bugs, 9 items, 1 PRs opened)",
            "docs: explain the board", "docs(api): API usage examples"]
    bad = ["🐛 [fix] receipts: show a size error",   # emoji shape
           "wip(loop): #42 partial — schema",         # wip is not conventional
           "ui(reports): new chart",                   # not a config-conventional type
           "fix(receipts): Show a size error",         # subject upper-case start
           "fix(receipts): show a size error.",        # trailing period
           "fix(Receipts): show a size error"]         # scope not lower-case
    for s in good:
        assert header("check", s, cfg=CONV)[0] == 0, f"should pass: {s}"
    for s in bad:
        assert header("check", s, cfg=CONV)[0] == 65, f"should fail: {s}"


def test_header_length_counts_the_pr_suffix():
    h = "feat(chat): " + "x" * (110 - len("feat(chat): ") - len(" (#1234)"))
    assert header("check", "--pr", "1234", h, cfg=CONV)[0] == 0
    assert header("check", "--pr", "12345", h, cfg=CONV)[0] == 65, "the (#n) suffix must count"
    assert header("check", "--pr", "1234", h + " (#1234)", cfg=CONV)[0] == 0, "an added suffix counts once"
    assert header("check", h + "x", cfg=CONV)[0] == 65, "no --pr reserves 8 characters"
    rc, out = header("check", "fix(a): " + "x" * 95, cfg={"writing": {"commit_format": "conventional"}})
    assert rc == 65 and '"max": 100' in out, f"conventional default is 100: {out}"


def test_mapping_to_conventional():
    cases = {("feat", "chat", ""): "feat(chat): stream replies", ("ui", "chat", ""): "feat(chat): stream replies",
             ("ui", "chat", "--repairs-bug"): "fix(chat): stream replies",
             ("security", "auth", ""): "fix(auth): stream replies", ("security", "", ""): "fix(security): stream replies",
             ("perf", "chat", ""): "perf(chat): stream replies", ("revert", "chat", ""): "revert(chat): stream replies"}
    for (t, scope, fl), want in cases.items():
        args = ["format", "--type", t, "--scope", scope, "--subject", "Stream replies."] + ([fl] if fl else [])
        rc, out = header(*args, cfg=CONV)
        assert rc == 0 and out == want, f"{t}/{scope}{fl}: {out}"
    rc, out = header("format", "--type", "docs", "--scope", "api", "--subject", "API usage examples", cfg=CONV)
    assert rc == 0 and out == "docs(api): API usage examples", f"acronym lower-cased: {out}"
    for t in ("feat", "fix", "chore", "refactor", "test", "perf", "docs", "ci", "ui", "security", "revert"):
        assert f"`{t}`" in STANDARD.split("### Conventional format", 1)[1].split("## 2 · PR")[0], f"mapping lacks {t}"


def test_wip_is_refused_in_conventional():
    rc, out = header("format", "--type", "wip", "--scope", "loop", "--subject", "partial", cfg=CONV)
    assert rc == 65 and "wip" in out
    assert header("format", "--type", "wip", "--scope", "loop", "--subject", "x")[0] == 0, "emoji keeps wip"


def test_emoji_default_is_unchanged():
    rc, out = header("format", "--type", "fix", "--scope", "receipts", "--subject", "show a size error")
    assert rc == 0 and out == "🐛 [fix] receipts: show a size error"
    assert header("check", "🐛 [fix] receipts: show a size error")[0] == 0
    assert header("check", "🐛 [fix] receipts: " + "x" * 80)[0] == 65
    assert header("check", "fix(receipts): show a size error")[0] == 65


def test_conventional_machine_subjects_match_the_dispatcher():
    src = (ROOT / "skills" / "super-build" / "scripts" / "super-build-dispatch.sh").read_text(encoding="utf-8")
    close_pat, wip_pat = (p.replace("$N", "42").replace("\\\\", "\\")
                          for p in re.findall(r'grep -E "\^\[0-9a-f\]\+ (.*?)" \| head', src))
    for pat, subject in ((close_pat, "chore(loop): close #42 — stream replies"),
                         (wip_pat, "chore(loop): #42 partial — schema only")):
        r = subprocess.run(["grep", "-E", "^[0-9a-f]+ " + pat], input=f"abc123 {subject}\n",
                           capture_output=True, text=True)
        assert r.returncode == 0, f"dispatcher misses {subject}"
        assert f"`{subject.split(' — ')[0].replace('42', '<N>')} — " in STANDARD, f"standard lacks {subject}"
        assert header("check", subject, cfg=CONV)[0] == 0


# ── ticket ───────────────────────────────────────────────────────────────────

def headings(text: str) -> list[str]:
    return re.findall(r"^## (.+?)\s*$", text, flags=re.M)


def test_ticket_template_has_required_sections_in_order():
    for name, text in [("ticket-format.md", (REF / "ticket-format.md").read_text(encoding="utf-8")),
                       ("writing-standard.md example", next(b for b in fenced(STANDARD) if "## Problem" in b and "## Blocked by" in b))]:
        h = [x for x in headings(text) if x in TICKET_SECTIONS]
        assert h == TICKET_SECTIONS, f"{name}: sections {h} != {TICKET_SECTIONS}"
        assert re.search(r"^- \[ \] ", text, flags=re.M), f"{name}: Acceptance Criteria must be a checklist"
        assert re.search(r"^- \*\*Where:\*\*.*\n(  - [a-z]\. .+\n)+", text, flags=re.M), f"{name}: Context needs lettered steps under Where"


def test_bug_filer_matches_the_standard():
    src = (ROOT / "scripts" / "super-qa-file-bug.sh").read_text(encoding="utf-8")
    for s in ["Problem", "Context", "Evidence", "Fix", "Acceptance Criteria", "Risk"]:
        assert f'"{s}"' in src, f"super-qa-file-bug.sh does not require {s}"
    rows = re.search(r'EVIDENCE_ROWS="([^"]+)"', src)[1].split("|")
    assert rows == [r.lower() for r in EVIDENCE_ROWS], f"filer evidence rows drifted: {rows}"
    table = re.findall(r"^\| ([A-Za-z][^|]+?) \| ", STANDARD.split("**Bug Evidence**", 1)[1], flags=re.M)
    table = [t for t in table if t != "Row"][:12]
    assert table == EVIDENCE_ROWS, f"writing-standard.md evidence rows drifted: {table}"


def test_ticket_title_shape():
    title = re.compile(r"^(🐛 \[bug\]|✨ \[feat\]|💄 \[ui\]|🧪 \[test\]|📝 \[docs\]|♻️ \[refactor\]|🔒 \[security\]) [a-z0-9][a-z0-9._/-]*: \S")
    assert title.match("🐛 [bug] receipts: spinner never stops over 10 MB")
    assert not title.match("🐛 Bug /imports — CSV upload silently drops rows")
    src = (ROOT / "scripts" / "super-qa-file-bug.sh").read_text(encoding="utf-8")
    assert 'FULL_TITLE="${BADGE} ${SCOPE}: ${TITLE}"' in src


# ── comment ──────────────────────────────────────────────────────────────────

def test_comment_header_regex():
    assert COMMENT_HEADER.match("[qa] [report] ❌ failing · round 1")
    assert COMMENT_HEADER.match("[reviewer] [blocker] 🛑 blocked · 🔐 missing key")
    assert not COMMENT_HEADER.match("🔍 super-board · QA fail · v1")
    assert not COMMENT_HEADER.match("[QA] [report] ❌ failing")
    assert not COMMENT_HEADER.match("[qa] [fyi] ❌ failing")
    assert COMMENT_HEADER.match("[scheduler] [report] ✅ ok · backend-nightly")


def test_scheduler_comment_matches_the_standard():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sbsched", ROOT / "scripts" / "super-board-scheduler.py")
    sched = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sched)
    run = {"job": "backend-nightly", "trigger": "cron", "status": "ok", "duration_s": 3725, "steps": []}
    sm = sched.summarize(None, None, (3, 2), {"sentry": 3, "prs": 1}, {"graphql": 4120, "core": 4870}, [])
    sm["counts_after"] = {}
    body = sched.runs_comment(run, sm, "2026-10-19 02:00 local")
    assert COMMENT_HEADER.match(body), body
    example = next(b for b in fenced(STANDARD) if b.startswith("[scheduler]"))
    assert body.splitlines()[0] == example.splitlines()[0], (body, example)
    labels = lambda text: [l.split(":")[0] for l in text.strip().splitlines()]  # noqa: E731
    assert labels(body) == labels(example), body


def test_comment_examples_follow_the_header():
    """Lane comments have headers; author notes use three labels without a routing prefix."""
    machine = re.compile(r"^(Local tests|gh-quota-on-exit|blocked-by|root-cause-hash):|^\||^$|^<!--")
    run_md = (REF / "run.md").read_text(encoding="utf-8")
    checked = 0
    author_notes = 0
    for text in (STANDARD, run_md):
        for block in fenced(text):
            lines = block.splitlines()
            if lines and lines[0].startswith("<!-- super-board:author-note "):
                assert lines[0].endswith(" -->"), "author-note marker must stay hidden"
                labels = re.findall(r"^\*\*(.+?):\*\* \S", block, flags=re.M)
                assert labels == ["Purpose", "What changed", "Why it matters"], labels
                assert len(lines) == 4, "author-note example must stay short"
                assert not any(COMMENT_HEADER.match(line) for line in lines), "author notes must not route as findings"
                author_notes += 1
                continue
            lines = [l for l in lines if not l.startswith("<!-- super-review:report")]
            if not lines or not re.match(r"^\[[a-z<]", lines[0]) or lines[0].startswith("[<"):
                continue
            assert COMMENT_HEADER.match(lines[0]), f"bad comment header: {lines[0]}"
            prose = [l for l in lines if not machine.match(l)]
            assert len(prose) <= 8, f"comment over 8 lines: {lines[0]}"
            checked += 1
    assert checked >= 6, f"expected comment samples, checked {checked}"
    assert author_notes == 1, "expected one shared author-note example"


def test_block_template_uses_the_header():
    text = (REF / "block-template.md").read_text(encoding="utf-8")
    heads = [b.splitlines()[0] for b in fenced(text) if b.startswith("[")]
    assert heads, "block-template.md has no header line"
    for h in heads:
        assert re.match(r"^\[(<role>|builder|qa|reviewer|collect|orchestrator)\] \[blocker\] \S", h), h
    assert "Reason tag:" in text, "super-board-deps.sh parses 'Reason tag:'"


def test_needs_you_comment_is_checklist_first():
    """The 🙋 comment shows what to do first; why and evidence fold away; the planner's lines stay."""
    text = (REF / "block-template.md").read_text(encoding="utf-8")
    block = next(b for b in fenced(text) if "🙋 Your turn on" in b)
    lines = block.splitlines()
    assert re.match(r"^\[reviewer\] \[blocker\] 🙋 Your turn on #<N> — ", lines[0]), lines[0]
    assert lines[1].startswith("- [ ] "), "the first line after the header is a checklist item"
    assert any(l == "- [ ] Comment `done` here" for l in lines), "the checklist ends with: comment done"
    fold = block.index("<details><summary>Why, and what I checked</summary>")
    assert block.index("- [ ] ") < fold, "checklist before the fold"
    assert re.search(r"Reason tag:[^\n]*🙋", block[fold:]), "deps.sh reads 'Reason tag: 🙋'"
    assert re.search(r"^blocked-by: -$", block[fold:], flags=re.M), "deps.sh reads 'blocked-by: -'"



# ── PR body ──────────────────────────────────────────────────────────────────

def test_pr_body_markers():
    out = subprocess.run([BASH, str(ROOT / "scripts" / "super-board-pr-body.sh"), "--skeleton"],
                         capture_output=True, text=True, check=True).stdout
    for b in PR_BLOCKS:
        assert f"<!-- sb:{b} -->" in out and f"<!-- /sb:{b} -->" in out, f"skeleton lacks {b}"
    run_md = (REF / "run.md").read_text(encoding="utf-8")
    for b in ["status", "problem", "solution", "ac", "history", "risk"]:
        assert f"<!-- sb:{b} -->" in run_md and f"<!-- /sb:{b} -->" in run_md, f"run.md PR template lacks {b}"
    assert "## Not verified" not in run_md and "**Status:**" not in run_md


def test_status_alerts():
    for alert in ("> [!NOTE]", "> [!TIP]", "> [!WARNING]"):
        assert alert in STANDARD, f"writing-standard.md lacks {alert}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to the ANSI code page
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"  ✅ {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {t.__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed.")
    sys.exit(1 if failed else 0)
