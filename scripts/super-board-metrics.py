#!/usr/bin/env python3
"""super-board-metrics.py — the board's scorecard. Read-only, stdlib only.

WHY: the board writes everything it does onto GitHub already: Status moves on the
project, one lane comment per lane exit (`[role] [label] status`, writing-standard
§ 4), labels on the cards it files, `gh-quota-on-exit:` lines on handoffs. This
script reads that back for one config and a window and answers "is the board getting
better": how long cards sit in each lane, how often QA bounces work, how much merges
first time, how much needs a human, what got reverted.

Usage:
  super-board-metrics.py [--config <path|slug>] [--since 14d|YYYY-MM-DD] [--json]
                         [--html <file>] [--ttl 900] [--refresh] [--runs-dir <dir>]

  --config   a config path, or a slug under .claude/super-board/configs/ (default: the
             active pointer, else the only config)
  --since    window start: Nd / Nh / Nw or a date (default 14d)
  --json     print the JSON below instead of the text table
  --html     also write a self-contained HTML scorecard (no external assets)
  --ttl      reuse the cached GitHub payload younger than this many seconds (default 900)
  --refresh  ignore the cache
  --runs-dir scheduler run records for the "Recent runs" section (default: the
             super-board-scheduler state dir, when it exists)

GitHub: ONE paginated GraphQL search per repo (`repo:<o>/<r> updated:>=<since>`,
issues and PRs, with labels, up to 100 comments, ProjectV2 Status-change timeline
events and project membership), through super-board-github-read.py. The raw pages
are cached under <git common dir>/../.claude/super-board/metrics/. The read uses its
own halt file in that folder: this report never stops a board run, and a failed read
exits 79 without leaving a board halt behind.

Where each number comes from (never guessed: a metric without its evidence is null
with a reason):
  cycle time    ProjectV2ItemStatusChangedEvent timeline items: time between a card
                entering a Status and leaving it, for Ready, Building, QA, Review, and
                Ready→Done in total, counted when the exit falls in the window. Lane
                comments are written on exit only, so they cannot place an entry.
  bounce rate   QA→Building moves / all moves out of QA (status events); without
                events, ❌ `[qa] [report]` comments / all `[qa] [report]` comments.
  first pass    board cards closed as completed in the window that never moved back to
                Building from QA or Review and carry no ❌/↩️ qa or reviewer comment.
  human share   of those merged cards: label needs-you / needs-human, a 🙋 lane
                comment, or a `human-gate:` line (merge gate exit 7).
  reverts       PRs merged in the window whose title starts with "Revert" (or
                "⏪ [revert]" / "revert(…)"), or whose body says "Reverts …#<n>".
  flaky / regression  issues opened in the window with a label containing "flaky" /
                the label `regression`.
  collect       issues opened in the window per `collect:<source>` label (fallback:
                the `<!-- super-collect-source: x -->` marker).
  quota         `gh-quota-on-exit: graphql=<n>/<max> rest=<n>/<max>` lines in comments
                written in the window.

JSON (schema 1, stable; add fields, never rename):
  {"schema":1, "repo":"o/r", "project":{"owner","number"}, "since":ISO, "generated":ISO,
   "fetched":{"at":ISO, "cached":bool, "pages":n, "truncated":[…]},
   "cards":{"seen":n, "merged":n},
   "cycle_time_hours":{"Ready"|"Building"|"QA"|"Review"|"total":
       {"median":h|null, "p90":h|null, "n":n, "reason":str|null}},
   "bounce_rate":{"value":0..1|null, "bounces":n, "exits":n, "basis":"status_events"|"lane_comments"|null, "reason"},
   "first_pass_merge_rate":{"value", "first_pass":n, "merged":n, "reason"},
   "human_merge_share":{"value", "human":n, "merged":n, "issues":[…], "reason"},
   "reverts":{"count":n, "prs":[…]}, "flaky_opened":{"count", "issues"},
   "regression_bugs_opened":{"count", "issues"}, "collect_filed":{"<source>":n},
   "github_quota":{"samples":n, "graphql_min", "graphql_median", "rest_min", "last", "reason"},
   "recent_runs":[run.json …] (only with scheduler records)}

Exits: 0 ok · 64 bad args · 66 config not found · 79 GitHub read failed (see stderr)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GITHUB_READ = HERE / "super-board-github-read.py"
UTC = dt.timezone.utc
LANES = ("Ready", "Building", "QA", "Review")
HEADER = re.compile(r"^\[(builder|qa|reviewer|collect|orchestrator|scheduler)\] \[([a-z]+)\] (\S+)")
QUOTA = re.compile(r"gh-quota-on-exit:\s*graphql=(\d+)/(\d+)\s+rest=(\d+)/(\d+)")
REVERTS = re.compile(r"(?i)\breverts?\b[^\n]*?#(\d+)")
REVERT_TITLE = re.compile(r"(?i)^(?:⏪\s*)?\[?revert\b")  # "Revert …", "⏪ [revert] …", "revert(x): …"
SOURCE_MARK = re.compile(r"<!--\s*super-collect-source:\s*([A-Za-z0-9_.-]+)\s*-->")
HUMAN_LABELS = {"needs-you", "needs-human"}

QUERY = """query($q:String!,$endCursor:String){
 search(query:$q,type:ISSUE,first:25,after:$endCursor){
  issueCount pageInfo{hasNextPage endCursor}
  nodes{
   __typename
   ... on Issue{number title state stateReason createdAt closedAt
    labels(first:30){nodes{name}}
    comments(first:100){totalCount nodes{createdAt body}}
    timelineItems(first:100,itemTypes:[PROJECT_V2_ITEM_STATUS_CHANGED_EVENT]){totalCount nodes{
     ... on ProjectV2ItemStatusChangedEvent{createdAt previousStatus status project{number}}}}
    projectItems(first:10){nodes{project{number}}}}
   ... on PullRequest{number title state createdAt mergedAt body
    labels(first:30){nodes{name}}
    comments(first:100){totalCount nodes{createdAt body}}}
  }}}"""


# ── time helpers ────────────────────────────────────────────────────────────

def parse_since(arg, now):
    m = re.fullmatch(r"(\d+)([dhw])", (arg or "14d").strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now - {"d": dt.timedelta(days=n), "h": dt.timedelta(hours=n), "w": dt.timedelta(weeks=n)}[unit]
    d = dt.datetime.fromisoformat(arg.strip().replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def iso(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def pct(values, q):
    """Nearest-rank percentile; values non-empty."""
    v = sorted(values)
    k = max(0, min(len(v) - 1, int(round(q * len(v) + 0.5)) - 1))
    return v[k]


# ── config + repo ───────────────────────────────────────────────────────────

def resolve_config(arg):
    if arg and (arg.endswith(".json") or os.sep in arg or "/" in arg):
        p = Path(arg)
    else:
        base = Path(".claude/super-board")
        slug = arg
        if not slug and (base / "active").is_file():
            slug = (base / "active").read_text(encoding="utf-8").strip()
        if not slug:
            cfgs = sorted((base / "configs").glob("*.json"))
            slug = cfgs[0].stem if len(cfgs) == 1 else None
        if not slug or not re.fullmatch(r"[A-Za-z0-9._-]+", slug) or slug in (".", ".."):
            print("pass --config <path|slug> (no active pointer and not exactly one config)", file=sys.stderr)
            raise SystemExit(64)
        p = base / "configs" / f"{slug}.json"
    if not p.is_file():
        print(f"config not found: {p}", file=sys.stderr)
        raise SystemExit(66)
    return json.loads(p.read_text(encoding="utf-8")), p.resolve()


def repo_root(cfg_path):
    """The repo the config lives in (<root>/.claude/super-board/configs/x.json)."""
    p = cfg_path.parent
    if p.name == "configs" and p.parent.name == "super-board" and p.parent.parent.name == ".claude":
        return p.parent.parent.parent
    return Path.cwd()


def repo_name(cfg, root):
    remote = ((cfg.get("repo") or {}).get("remote") or "").strip()
    if not remote:
        r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(root), capture_output=True, text=True)
        remote = r.stdout.strip() if r.returncode == 0 else ""
    m = re.search(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", remote)
    if not m:
        print("cannot tell the GitHub repo: set repo.remote in the config", file=sys.stderr)
        raise SystemExit(66)
    return f"{m.group(1)}/{m.group(2)}"


def state_dir(root):
    # Linked worktrees share the main checkout's folder (common dir <main>/.git). A
    # submodule's common dir is <super>/.git/modules/<name>: keep it in its own checkout.
    r = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=str(root), capture_output=True, text=True)
    common = (root / r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None
    base = common.parent if common is not None and common.name == ".git" else root
    d = base / ".claude" / "super-board" / "metrics"
    d.mkdir(parents=True, exist_ok=True)
    if not (d / ".gitignore").exists():
        (d / ".gitignore").write_text("*\n", encoding="utf-8")
    return d


# ── fetch ───────────────────────────────────────────────────────────────────

def fetch(repo, since, root, cache_dir, ttl, refresh, now):
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", repo) + "-" + since.strftime("%Y%m%d")
    cache = cache_dir / f"{key}.json"
    if cache.is_file() and not refresh:
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if now.timestamp() - saved["fetched"] < ttl:
                return saved["pages"], dt.datetime.fromtimestamp(saved["fetched"], UTC), True
        except (ValueError, KeyError):
            pass
    halt = cache_dir / "github-halt.json"
    if halt.exists():
        halt.unlink()  # a report has no run to stop; each run reads fresh
    env = dict(os.environ, SB_GITHUB_HALT_FILE=str(halt), SB_REPO_PATH=str(root))
    q = f"repo:{repo} updated:>={since.strftime('%Y-%m-%d')}"
    argv = [sys.executable, str(GITHUB_READ), "--kind", "json", "--", "api", "graphql", "--paginate", "--slurp",
            "-f", "query=" + QUERY, "-f", "q=" + q]
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        print("metrics: GitHub read failed; nothing computed", file=sys.stderr)
        raise SystemExit(79)
    pages = json.loads(r.stdout)
    pages = pages if isinstance(pages, list) else [pages]
    cache.write_text(json.dumps({"fetched": int(now.timestamp()), "pages": pages}), encoding="utf-8")
    return pages, now, False


# ── compute (pure) ──────────────────────────────────────────────────────────

def nodes_of(pages):
    out, truncated = [], []
    for page in pages:
        s = ((page.get("data") or {}).get("search")) or {}
        if (s.get("issueCount") or 0) > 1000:
            truncated.append(f"search matched {s['issueCount']} items; GitHub search returns at most 1000")
        out.extend(n for n in (s.get("nodes") or []) if n)
    return out, truncated


def labels(n):
    return [l["name"] for l in ((n.get("labels") or {}).get("nodes") or []) if l and l.get("name")]


def comments(n):
    out = []
    for c in ((n.get("comments") or {}).get("nodes") or []):
        if not c:
            continue
        body = c.get("body") or ""
        first = body.lstrip().splitlines()[0] if body.strip() else ""
        m = HEADER.match(first)
        out.append({"at": ts(c.get("createdAt")), "body": body,
                    "role": m.group(1) if m else None, "label": m.group(2) if m else None,
                    "emoji": m.group(3) if m else None})
    return out


def on_board(n, number):
    items = [((i or {}).get("project") or {}).get("number") for i in ((n.get("projectItems") or {}).get("nodes") or [])]
    evs = [((e or {}).get("project") or {}).get("number") for e in ((n.get("timelineItems") or {}).get("nodes") or [])]
    return number in items or number in evs


def status_events(n, number):
    evs = []
    for e in ((n.get("timelineItems") or {}).get("nodes") or []):
        if not e or not e.get("createdAt"):
            continue
        p = (e.get("project") or {}).get("number")
        if p is not None and p != number:
            continue
        evs.append((ts(e["createdAt"]), e.get("previousStatus") or "", e.get("status") or ""))
    return sorted(evs)


def bounced(evs, cs):
    if any(prev in ("QA", "Review") and new == "Building" for _, prev, new in evs):
        return True
    return any(c["role"] in ("qa", "reviewer") and c["emoji"] and c["emoji"][0] in "❌↩" for c in cs)


def human_gated(n, cs):
    if HUMAN_LABELS & set(labels(n)):
        return True
    return any((c["emoji"] or "").startswith("🙋") or re.search(r"^human-gate:", c["body"], re.M) for c in cs)


def stat(samples, reason_if_empty):
    if not samples:
        return {"median": None, "p90": None, "n": 0, "reason": reason_if_empty}
    hours = [s / 3600 for s in samples]
    return {"median": round(statistics.median(hours), 2), "p90": round(pct(hours, 0.9), 2), "n": len(hours), "reason": None}


def ratio(num, den, reason):
    return round(num / den, 3) if den else None, (None if den else reason)


def compute(pages, cfg, since, now, fetched_at=None, cached=False):
    number = int((cfg.get("project") or {}).get("number") or 0)
    nodes, truncated = nodes_of(pages)
    issues = [n for n in nodes if n.get("__typename") == "Issue"]
    prs = [n for n in nodes if n.get("__typename") == "PullRequest"]
    for n in nodes:
        if ((n.get("comments") or {}).get("totalCount") or 0) > 100:
            truncated.append(f"#{n.get('number')}: only the first 100 comments read")
        if ((n.get("timelineItems") or {}).get("totalCount") or 0) > 100:
            truncated.append(f"#{n.get('number')}: only the first 100 status events read")
    board = [n for n in issues if on_board(n, number)]
    in_window = lambda d: d is not None and d >= since  # noqa: E731

    # cycle time
    dwell = {lane: [] for lane in LANES}
    total, any_events = [], False
    for n in board:
        evs = status_events(n, number)
        any_events = any_events or bool(evs)
        for (t0, _, s0), (t1, _, _) in zip(evs, evs[1:]):
            if s0 in dwell and in_window(t1):
                dwell[s0].append((t1 - t0).total_seconds())
        ready = next((t for t, _, s in evs if s == "Ready"), None)
        done = next((t for t, _, s in evs if s == "Done" and ready and t >= ready), None)
        if ready and done and in_window(done):
            total.append((done - ready).total_seconds())
    if any_events:
        cycle = {lane: stat(dwell[lane], f"no card left {lane} in the window") for lane in LANES}
        cycle["total"] = stat(total, "no card went Ready→Done in the window")
    else:
        why = ("no ProjectV2 Status-change events for this project in the window; lane comments are "
               "written on exit only and cannot place a lane entry")
        cycle = {lane: stat([], why) for lane in (*LANES, "total")}

    # bounce rate
    if any_events:
        exits = bounces = 0
        for n in board:
            for t, prev, new in status_events(n, number):
                if prev == "QA" and new != "QA" and in_window(t):
                    exits += 1
                    bounces += new == "Building"
        value, reason = ratio(bounces, exits, "no card left QA in the window")
        bounce = {"value": value, "bounces": bounces, "exits": exits, "basis": "status_events", "reason": reason}
    else:
        qa = [c for n in board for c in comments(n) if c["role"] == "qa" and c["label"] == "report" and in_window(c["at"])]
        bounces = sum(1 for c in qa if (c["emoji"] or "").startswith("❌"))
        value, reason = ratio(bounces, len(qa), "no status events and no [qa] [report] comment in the window")
        bounce = {"value": value, "bounces": bounces, "exits": len(qa), "basis": "lane_comments" if qa else None,
                  "reason": reason}

    # merged cards: first pass and human share
    merged = [n for n in board if n.get("state") == "CLOSED" and n.get("stateReason") == "COMPLETED"
              and in_window(ts(n.get("closedAt")))]
    first = [n for n in merged if not bounced(status_events(n, number), comments(n))]
    human = [n for n in merged if human_gated(n, comments(n))]
    fp_v, fp_r = ratio(len(first), len(merged), "no board card was closed as completed in the window")
    hu_v, hu_r = ratio(len(human), len(merged), "no board card was closed as completed in the window")

    # reverts
    merged_prs = [p for p in prs if in_window(ts(p.get("mergedAt")))]
    reverts = sorted(p["number"] for p in merged_prs
                     if REVERT_TITLE.match(p.get("title") or "") or REVERTS.search(p.get("body") or ""))

    opened = [n for n in issues if in_window(ts(n.get("createdAt")))]
    flaky = sorted(n["number"] for n in opened if any("flaky" in l.lower() for l in labels(n)))
    regress = sorted(n["number"] for n in opened if "regression" in labels(n))
    collect = {}
    for n in opened:
        srcs = [l.split(":", 1)[1] for l in labels(n) if l.startswith("collect:")]
        if not srcs:
            m = SOURCE_MARK.search(n.get("body") or "")
            srcs = [m.group(1)] if m else []
        for s in srcs:
            collect[s] = collect.get(s, 0) + 1

    # quota
    samples = []
    for n in nodes:
        for c in comments(n):
            m = QUOTA.search(c["body"]) if in_window(c["at"]) else None
            if m:
                samples.append((c["at"], int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))))
    samples.sort()
    if samples:
        g = [s[1] for s in samples]
        quota = {"samples": len(samples), "graphql_min": min(g), "graphql_median": statistics.median(g),
                 "rest_min": min(s[3] for s in samples),
                 "last": {"at": iso(samples[-1][0]), "graphql": samples[-1][1], "rest": samples[-1][3]}, "reason": None}
    else:
        quota = {"samples": 0, "graphql_min": None, "graphql_median": None, "rest_min": None, "last": None,
                 "reason": "no gh-quota-on-exit line in a comment written in the window"}

    return {"schema": 1, "repo": None, "project": {"owner": (cfg.get("project") or {}).get("owner"), "number": number},
            "since": iso(since), "generated": iso(now),
            "fetched": {"at": iso(fetched_at), "cached": cached, "pages": len(pages), "truncated": truncated},
            "cards": {"seen": len(board), "merged": len(merged)},
            "cycle_time_hours": cycle, "bounce_rate": bounce,
            "first_pass_merge_rate": {"value": fp_v, "first_pass": len(first), "merged": len(merged), "reason": fp_r},
            "human_merge_share": {"value": hu_v, "human": len(human), "merged": len(merged),
                                  "issues": sorted(n["number"] for n in human), "reason": hu_r},
            "reverts": {"count": len(reverts), "prs": reverts},
            "flaky_opened": {"count": len(flaky), "issues": flaky},
            "regression_bugs_opened": {"count": len(regress), "issues": regress},
            "collect_filed": dict(sorted(collect.items())), "github_quota": quota}


# ── scheduler run records ───────────────────────────────────────────────────

def recent_runs(runs_dir, root, limit=10):
    if runs_dir is None:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("sbsched", HERE / "super-board-scheduler.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            runs_dir = mod.default_state_dir() / "runs"
        except Exception:
            return []
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    want = os.path.normcase(str(root.resolve()))
    out = []
    for f in runs_dir.glob("*/*/run.json"):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("repo") and os.path.normcase(str(Path(r["repo"]).resolve())) != want:
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("started") or "", reverse=True)
    return out[:limit]


# ── render ──────────────────────────────────────────────────────────────────

def fmt_rate(m):
    return "null" if m["value"] is None else f"{m['value'] * 100:.0f}%"


def text_table(r):
    lines = [f"super-board scorecard · {r['repo']} · project #{r['project']['number']} · since {r['since'][:10]}",
             f"cards seen {r['cards']['seen']} · merged {r['cards']['merged']}"
             + (" · cached" if r["fetched"]["cached"] else ""), "",
             f"{'lane':<10} {'median h':>9} {'p90 h':>8} {'n':>4}"]
    for lane in (*LANES, "total"):
        c = r["cycle_time_hours"][lane]
        med = "null" if c["median"] is None else f"{c['median']:.1f}"
        p90 = "null" if c["p90"] is None else f"{c['p90']:.1f}"
        lines.append(f"{lane:<10} {med:>9} {p90:>8} {c['n']:>4}" + (f"  ({c['reason']})" if c["reason"] else ""))
    b, f, h = r["bounce_rate"], r["first_pass_merge_rate"], r["human_merge_share"]
    q = r["github_quota"]
    lines += ["",
              f"QA→Build bounce   {fmt_rate(b)} ({b['bounces']}/{b['exits']})" + (f"  ({b['reason']})" if b["reason"] else ""),
              f"first-pass merge  {fmt_rate(f)} ({f['first_pass']}/{f['merged']})" + (f"  ({f['reason']})" if f["reason"] else ""),
              f"human merge share {fmt_rate(h)} ({h['human']}/{h['merged']})" + (f"  ({h['reason']})" if h["reason"] else ""),
              f"reverts           {r['reverts']['count']} {r['reverts']['prs'] or ''}".rstrip(),
              f"flaky opened      {r['flaky_opened']['count']}",
              f"regression bugs   {r['regression_bugs_opened']['count']}",
              "collect filed     " + (", ".join(f"{k} {v}" for k, v in r["collect_filed"].items()) or "0"),
              "gh quota          " + (f"graphql min {q['graphql_min']} · median {q['graphql_median']} · rest min "
                                      f"{q['rest_min']} ({q['samples']} handoffs)" if q["samples"] else f"null ({q['reason']})")]
    for t in r["fetched"]["truncated"]:
        lines.append(f"note: {t}")
    return "\n".join(lines)


CSS = """
:root{--bg:#fbfaf8;--fg:#1d1d1b;--muted:#6b6a66;--card:#fff;--line:#e5e2dc;--bar:#3d6fb6;--bar2:#9bb5dc;
--ok:#2f7d4f;--bad:#b3412e;--warn:#9a6a00}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--muted:#a3a19b;--card:#1f1f1d;--line:#34332f;
--bar:#7aa3e0;--bar2:#3f5f8f;--ok:#6cc08b;--bad:#ef8a76;--warn:#e3b341}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.muted{color:var(--muted)}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.tile b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.tile span{color:var(--muted);font-size:13px}
.null{color:var(--muted);font-size:13px}.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td.num{text-align:right;font-variant-numeric:tabular-nums}svg{display:block;max-width:100%}
.chip{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid currentColor}
.ok{color:var(--ok)}.failed,.timeout{color:var(--bad)}.skipped,.missed{color:var(--warn)}
"""


def bars_svg(rows):
    """rows: [(label, median, p90)] → inline SVG horizontal bars (median solid, p90 light)."""
    vals = [v for _, m, p in rows for v in (m, p) if v is not None]
    top = max(vals) if vals else 1
    h, out = 28, []
    for i, (label, m, p) in enumerate(rows):
        y = i * h
        out.append(f'<text x="0" y="{y + 18}" font-size="13" fill="currentColor">{html.escape(label)}</text>')
        if m is None:
            out.append(f'<text x="90" y="{y + 18}" font-size="12" fill="var(--muted)">null</text>')
            continue
        wp = 0 if p is None else 260 * p / top
        wm = 260 * m / top
        out.append(f'<rect x="90" y="{y + 6}" width="{wp:.1f}" height="16" rx="3" fill="var(--bar2)"/>')
        out.append(f'<rect x="90" y="{y + 6}" width="{wm:.1f}" height="16" rx="3" fill="var(--bar)"/>')
        out.append(f'<text x="{96 + max(wm, wp):.1f}" y="{y + 18}" font-size="12" fill="currentColor">'
                   f'{m:.1f}h · p90 {p:.1f}h</text>')
    return (f'<svg viewBox="0 0 460 {h * len(rows)}" width="100%" role="img" '
            f'aria-label="cycle time per lane">{"".join(out)}</svg>')


def tile(label, value, sub=""):
    v = f'<b>{html.escape(value)}</b>' if value != "null" else '<b class="null">null</b>'
    return f'<div class="tile">{v}<span>{html.escape(label)}</span>{"<br><span>" + html.escape(sub) + "</span>" if sub else ""}</div>'


def render_html(r):
    e = html.escape
    cyc = r["cycle_time_hours"]
    rows = [(lane, cyc[lane]["median"], cyc[lane]["p90"]) for lane in (*LANES, "total")]
    reasons = "".join(f"<li>{e(k)}: {e(v['reason'])}</li>" for k, v in cyc.items() if v["reason"])
    b, f, h, q = r["bounce_rate"], r["first_pass_merge_rate"], r["human_merge_share"], r["github_quota"]
    tiles = "".join([
        tile("QA→Build bounce", fmt_rate(b), b["reason"] or f"{b['bounces']}/{b['exits']} · {b['basis']}"),
        tile("first-pass merge", fmt_rate(f), f["reason"] or f"{f['first_pass']}/{f['merged']}"),
        tile("human merge share", fmt_rate(h), h["reason"] or f"{h['human']}/{h['merged']}"),
        tile("reverts", str(r["reverts"]["count"]), " ".join(f"#{n}" for n in r["reverts"]["prs"])),
        tile("flaky cards opened", str(r["flaky_opened"]["count"])),
        tile("regression bugs", str(r["regression_bugs_opened"]["count"])),
        tile("gh graphql min", "null" if q["graphql_min"] is None else str(q["graphql_min"]),
             q["reason"] or f"{q['samples']} handoffs"),
    ])
    coll = "".join(f"<tr><td>{e(k)}</td><td class='num'>{v}</td></tr>" for k, v in r["collect_filed"].items()) \
        or "<tr><td colspan=2 class='muted'>no collect cards opened in the window</td></tr>"
    runs = r.get("recent_runs") or []
    runs_html = ""
    if runs:
        trs = "".join(
            f"<tr><td>{e(x.get('started') or '')[:16]}</td><td>{e(x.get('job') or '')}</td>"
            f"<td>{e(x.get('trigger') or '')}</td><td><span class='chip {e(x.get('status') or '')}'>{e(x.get('status') or '')}</span></td>"
            f"<td class='num'>{'' if x.get('duration_s') is None else int(x['duration_s'])}s</td></tr>" for x in runs)
        runs_html = (f"<h2>Recent runs</h2><div class='wrap'><table><tr><th>started</th><th>job</th><th>trigger</th>"
                     f"<th>status</th><th>duration</th></tr>{trs}</table></div>")
    notes = "".join(f"<li>{e(t)}</li>" for t in r["fetched"]["truncated"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>super-board scorecard</title>
<style>{CSS}</style></head><body><main>
<h1>super-board scorecard</h1>
<p class="muted">{e(r['repo'] or '')} · project #{r['project']['number']} · since {e(r['since'][:10])} ·
{r['cards']['seen']} cards seen · {r['cards']['merged']} merged · generated {e(r['generated'])}</p>
<h2>Cycle time per lane</h2>{bars_svg(rows)}
<p class="muted">Median (dark) and p90 (light), hours in each Status before the card left it.</p>
{"<ul class='muted'>" + reasons + "</ul>" if reasons else ""}
<h2>Outcomes</h2><div class="tiles">{tiles}</div>
<h2>Cards filed per collect source</h2><div class="wrap"><table>{coll}</table></div>
{runs_html}
{"<h2>Notes</h2><ul class='muted'>" + notes + "</ul>" if notes else ""}
</main></body></html>
"""


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config")
    p.add_argument("--since", default="14d")
    p.add_argument("--json", action="store_true")
    p.add_argument("--html")
    p.add_argument("--ttl", type=int, default=900)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--runs-dir")
    p.add_argument("--payload", help=argparse.SUPPRESS)  # tests: a saved pages file instead of GitHub
    a = p.parse_args(argv)
    now = dt.datetime.now(UTC)
    try:
        since = parse_since(a.since, now)
    except ValueError:
        print("--since must be Nd / Nh / Nw or a date", file=sys.stderr)
        return 64
    cfg, cfg_path = resolve_config(a.config)
    root = repo_root(cfg_path)
    repo = repo_name(cfg, root)
    if a.payload:
        pages, fetched_at, cached = json.loads(Path(a.payload).read_text(encoding="utf-8")), now, False
    else:
        pages, fetched_at, cached = fetch(repo, since, root, state_dir(root), a.ttl, a.refresh, now)
    r = compute(pages, cfg, since, now, fetched_at, cached)
    r["repo"] = repo
    runs = recent_runs(a.runs_dir, root)
    if runs:
        r["recent_runs"] = runs
    if a.html:
        Path(a.html).write_text(render_html(r), encoding="utf-8")
    print(json.dumps(r, indent=1, ensure_ascii=False) if a.json else text_table(r))
    if a.html:
        print(f"html: {a.html}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
