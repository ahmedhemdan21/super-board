#!/usr/bin/env python3
"""super-board-split.py — split a hub issue into one issue per member repo (hub.md → Split).

WHY. On a hub board a report is filed once, in the hub repo, and the code lives in member
repos. Triage decides which sides a report touches; this script carries out that decision:
it moves a one-sided report to its member repo, or files one sub-issue per side under the hub
parent. It holds no judgement: the decision file says which sides, and why.

Verbs:
  plan          --config C --issue <hub#N>
                Read-only. Prints the hub issue (title, body, labels, Project column, existing
                sub-issues by member), the members and the split settings, for the triage agent.
  apply         --config C --issue <hub#N> --decision D [--approved] [--dry-run]
                D = {"sides": ["backend", "frontend"], "why": "…",
                     "children": {"backend": {"title": "…", "body_file": "…"}, …}}
                (children only for two or more sides; body_file relative to D's folder).
                One side: `gh issue transfer` to that member, labels re-applied (only those that
                exist there; the rest reported), the Project item checked (added back when
                missing), and a `[triage] … split → <member>` comment.
                Several sides: one issue per side in its member repo, in split.default_order;
                each later side's `## Blocked by` names the side before it. Labels copied from
                the parent, each child on the Project (the parent's column, Backlog when
                split.confirm is true) and attached with addSubIssue. The parent gets the label
                `parent`, goes to Backlog, and one comment lists the children.
                split.confirm true: without --approved (or a `done <digest8>` reply to the
                proposal from a human with write on the hub and every member written to),
                apply only posts the proposal as a 🙋 comment: awaiting-approval.

Trust. Markers (sb-split, sb-split-proposal, sb-parent-needs-human) count only when this run's
own identity (`gh api user`) wrote them. A child recorded in a marker is used only when it is
in the side's member repo, carries `<!-- sb-split-child: <hub ref> <side> -->` and was opened by
the same identity. Labels and column come from the live parent, except a one-sided resume after
the transfer, which can only read the labels the run's own marker saved before it.
  close-parents --config C [--dry-run]
                Every open hub issue with sub-issues: all children closed COMPLETED → close it
                as completed with a comment; any child NOT_PLANNED → comment once that it
                needs a human, leave it open.

Idempotent. Children are found by repository in the parent's `subIssues.nodes` (never
`subIssuesSummary`, which lags), else in the progress marker `<!-- sb-split: {…} -->` on the
parent's split comment, written right after each child is created. A rerun after a partial
failure continues from there and creates nothing twice.

Reads go through super-board-github-read.py (exit 79 = halted). Writes are checked against the
halt file and super-board-gh-guard.sh's sb_gh_guard_check first, and never retried: a failed
write stops with what was written and what is left.

Exit: 0 ok (stdout JSON has `status`) · 3 refused (decision or child body not usable; nothing
written) · 64 usage · 65 config invalid (super-board-config.py validate) · 66 config
unreadable or not a hub board · 69 a GitHub write failed · 79 GitHub reads halted.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


github_read = _load("github_read", "super-board-github-read.py")
card_ready = _load("card_ready", "super-board-card-ready.py")
resolver = _load("sb_config", "super-board-config.py")  # P2: members / hub / split

MARKER = re.compile(r"<!--\s*sb-split:\s*(\{.*?\})\s*-->", re.S)
PROPOSAL = re.compile(r"<!--\s*sb-split-proposal:\s*([0-9a-f]{64})\s*-->")
NEEDS_HUMAN = "<!-- sb-parent-needs-human -->"
# Approval reply: `done <first 8 hex of the proposal digest>`; a bare `done` never approves.
APPROVE = re.compile(r"\s*done\s+([0-9a-f]{8})[.!]?\s*\Z", re.I)
CHILD_LINK = "<!-- sb-split-child: {hub} {side} -->"  # backlink written into every child body
TRUSTED = ("write", "maintain", "admin")
NOT_COPIED = {"parent", "needs-triage"}  # labels a child never inherits


class Refused(Exception):
    """The decision or a child body is not usable; nothing was written."""


class WriteFailed(Exception):
    pass


# ── config ──────────────────────────────────────────────────────────────

def hub_config(path):
    """(config, {hub, members: {key: {key, repo, path, base_branch}}, order, confirm, owner,
    number}) read through the P2 resolver (super-board-config.py), which validates the hub
    shape; only what the resolver leaves open (a hub at all, the Project) is checked here."""
    cfg = resolver.checked(path)
    if "members" not in cfg:
        raise ValueError("not a hub board: the config has no `members` (hub.md → Config)")
    members = {m["key"]: {**m, "base_branch": cfg["members"][m["key"]].get("base_branch")}
               for m in resolver.members_of(cfg, path)}
    split = cfg.get("split") or {}
    order = list(split.get("default_order") or [])
    order += [k for k in members if k not in order]
    project = cfg.get("project") or {}
    if not project.get("owner") or not project.get("number"):
        raise ValueError("config has no project.owner / project.number")
    return cfg, {"hub": resolver.repo_name(cfg["hub"]["remote"]), "members": members, "order": order,
                 "confirm": split.get("confirm", True) is not False,
                 "owner": str(project["owner"]), "number": int(project["number"])}


def parse_ref(text, default_repo):
    m = re.fullmatch(r"(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#?([0-9]+)", str(text).strip())
    if not m:
        raise ValueError(f"issue must be N, #N or owner/repo#N: {text}")
    return (m.group(1) or default_repo), int(m.group(2))


def ref(repo, n):
    return f"{repo}#{n}"


def same(a, b):
    return (a or "").lower() == (b or "").lower()


# ── GitHub ──────────────────────────────────────────────────────────────

ISSUE_QUERY = """query SplitIssue($owner:String!,$name:String!,$n:Int!){
  repository(owner:$owner,name:$name){ issue(number:$n){
    id number title body state stateReason url repository{ nameWithOwner } author{ login }
    labels(first:50){ nodes{ name } }
    subIssues(first:100){ nodes{ id number title state stateReason url repository{ nameWithOwner } } }
    projectItems(first:20){ nodes{ id project{ number owner{ ... on User{ login } ... on Organization{ login } } }
      fieldValueByName(name:"Status"){ ... on ProjectV2ItemFieldSingleSelectValue{ name } } } }
    comments(last:100){ nodes{ databaseId body createdAt author{ login } } } } } }"""

PARENTS_QUERY = """query SplitParents($owner:String!,$name:String!,$endCursor:String){
  repository(owner:$owner,name:$name){ issues(states:OPEN,first:50,after:$endCursor){
    pageInfo{ hasNextPage endCursor }
    nodes{ number title
      subIssues(first:100){ nodes{ number state stateReason repository{ nameWithOwner } } }
      comments(last:100){ nodes{ body author{ login } } } } } } }"""

ADD_SUB = ("mutation($p:ID!,$c:ID!){ addSubIssue(input:{issueId:$p, subIssueId:$c})"
           "{ subIssue{ number repository{ nameWithOwner } } } }")


class GitHub:
    def __init__(self, hc, config_path, dry):
        self.hc, self.config_path, self.dry = hc, config_path, dry
        self.writes, self.done = [], []
        self.guarded = False
        self.labels = {}
        self.perms = {}
        self.tmp = []
        self.dry_n = 0
        self._me = None

    # reads
    def read(self, args, kind="object"):
        return github_read.read(list(args), kind)[1]

    def me(self):
        """The login this run writes as. Only its own comments count as markers."""
        if self._me is None:
            value = self.read(["api", "user"], "object")
            login = value.get("login") if isinstance(value, dict) else None
            if not isinstance(login, str) or not login:
                raise WriteFailed("cannot tell who is running: `gh api user` has no login")
            self._me = login
        return self._me

    def mine(self, comment):
        return same((comment.get("author") or {}).get("login"), self.me())

    def issue(self, repo, n):
        owner, name = repo.split("/")
        value = self.read(["api", "graphql", "-f", "query=" + ISSUE_QUERY,
                           "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"n={n}"])
        if value.get("errors"):
            raise WriteFailed(f"reading {ref(repo, n)} returned errors: {json.dumps(value['errors'])[:300]}")
        node = ((value.get("data") or {}).get("repository") or {}).get("issue")
        if not node:
            raise Refused(f"{ref(repo, n)} not found")
        return node

    def repo_labels(self, repo):
        if repo.lower() not in self.labels:
            pages = self.read(["api", "--paginate", "--slurp", f"repos/{repo}/labels?per_page=100"], "array")
            self.labels[repo.lower()] = {l["name"].lower(): l["name"] for page in pages for l in page
                                         if isinstance(l, dict) and l.get("name")}
        return self.labels[repo.lower()]

    def permission(self, repo, login):
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\[bot\])?", login or ""):
            return None
        key = (repo.lower(), login.lower())
        if key not in self.perms:
            self.perms[key] = self.read(["api", f"repos/{repo}/collaborators/{login}/permission"],
                                        "permission").get("permission")
        return self.perms[key]

    # writes
    def guard(self):
        if self.guarded or self.dry:
            return
        script = HERE / "super-board-gh-guard.sh"
        if script.exists():
            r = subprocess.run(github_read.command("bash", ["-c", '. "$1" && sb_gh_guard_check 200', "guard",
                                                            script.as_posix()]))
            if r.returncode == github_read.HALTED:
                raise github_read.ReadHalted("gh guard stopped the run")
            if r.returncode:
                raise WriteFailed(f"sb_gh_guard_check failed (exit {r.returncode})")
        self.guarded = True

    def file(self, text):
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        self.tmp.append(path)
        return path

    def write(self, what, args, dry_out="", body=None, program="gh"):
        entry = {"what": what, "command": " ".join([program] + [shlex.quote(a) for a in args])}
        if body is not None:
            entry["body"] = body
        self.writes.append(entry)
        if self.dry:
            return dry_out
        self.guard()
        github_read.check_halt()
        argv = github_read.command("bash", [(HERE / "super-board-card.sh").as_posix(), *args]) if program == "card" \
            else github_read.command("gh", args)
        try:
            r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            raise WriteFailed(f"{what}: {e}")
        if r.returncode:
            raise WriteFailed(f"{what}: {(r.stderr or r.stdout).strip()[:400]}")
        self.done.append(what)
        return r.stdout

    def comment(self, repo, n, body):
        out = self.write(f"comment on {ref(repo, n)}",
                         ["issue", "comment", str(n), "--repo", repo, "--body-file", self.file(body)],
                         dry_out=f"https://github.com/{repo}/issues/{n}#issuecomment-0", body=body)
        m = re.search(r"#issuecomment-([0-9]+)", out or "")
        return int(m.group(1)) if m else None

    def edit_comment(self, repo, comment_id, body, n):
        self.write(f"update the split comment on {ref(repo, n)}",
                   ["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}", "-f", f"body={body}"],
                   body=body)

    def create(self, repo, title, body, labels):
        args = ["issue", "create", "--repo", repo, "--title", title, "--body-file", self.file(body)]
        for label in labels:
            args += ["--label", label]
        self.dry_n += 1
        out = self.write(f"create the {repo} issue", args, dry_out=f"https://github.com/{repo}/issues/0", body=body)
        m = re.search(r"/issues/([0-9]+)\s*$", (out or "").strip())
        if not m:
            raise WriteFailed(f"create the {repo} issue: no issue URL in {out.strip()[:200]!r} — check {repo} before rerunning")
        return int(m.group(1))

    def add_labels(self, repo, n, labels):
        if labels:
            self.write(f"label {ref(repo, n)}: {', '.join(labels)}",
                       ["issue", "edit", str(n), "--repo", repo, "--add-label", ",".join(labels)])

    def add_to_project(self, repo, n):
        out = self.write(f"add {ref(repo, n)} to project {self.hc['owner']}/{self.hc['number']}",
                         ["project", "item-add", str(self.hc["number"]), "--owner", self.hc["owner"],
                          "--url", f"https://github.com/{repo}/issues/{n}", "--format", "json"], dry_out="{}")
        return out

    def move(self, repo, n, column):
        self.write(f"move {ref(repo, n)} to {column}",
                   ["--config", self.config_path, "move", ref(repo, n), column], program="card")

    def add_sub_issue(self, parent_id, child_id, label):
        out = self.write(f"attach {label} to the parent",
                         ["api", "graphql", "-f", "query=" + ADD_SUB, "-f", f"p={parent_id}", "-f", f"c={child_id}"],
                         dry_out="{}")
        if not self.dry:
            try:
                value = json.loads(out)
            except ValueError:
                value = {"errors": [out[:200]]}
            if value.get("errors"):
                raise WriteFailed(f"attach {label} to the parent: {json.dumps(value['errors'])[:300]}")

    def cleanup(self):
        for p in self.tmp:
            try:
                os.unlink(p)
            except OSError:
                pass


# ── issue helpers ───────────────────────────────────────────────────────

def labels_of(node):
    return [l["name"] for l in ((node.get("labels") or {}).get("nodes") or []) if l.get("name")]


def project_item(node, hc):
    for it in (node.get("projectItems") or {}).get("nodes") or []:
        p = it.get("project") or {}
        if p.get("number") == hc["number"] and same((p.get("owner") or {}).get("login"), hc["owner"]):
            return {"id": it.get("id"), "status": (it.get("fieldValueByName") or {}).get("name") or ""}
    return None


def comments_of(node):
    return [c for c in ((node.get("comments") or {}).get("nodes") or []) if isinstance(c, dict)]


def marker_of(node, gh):
    """(state, comment databaseId) of the newest sb-split marker this run's identity wrote, or
    (None, None). A marker anyone else wrote is ignored and reported."""
    for c in reversed(comments_of(node)):
        m = MARKER.search(c.get("body") or "")
        if m and not gh.mine(c):
            print(f"split: ignored an sb-split marker written by {(c.get('author') or {}).get('login')!r}",
                  file=sys.stderr)
            continue
        if m:
            try:
                state = json.loads(m.group(1))
            except ValueError:
                continue
            if isinstance(state, dict):
                return state, c.get("databaseId")
    return None, None


def subs_by_member(node, hc):
    """{member key: sub-issue node}, matched by repository (first per repo)."""
    out = {}
    for s in (node.get("subIssues") or {}).get("nodes") or []:
        repo = (s.get("repository") or {}).get("nameWithOwner")
        for key, m in hc["members"].items():
            if same(repo, m["repo"]) and key not in out:
                out[key] = s
    return out


# ── decision ────────────────────────────────────────────────────────────

def load_decision(path, hc):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Refused(f"decision unreadable: {e}")
    if not isinstance(d, dict):
        raise Refused("decision must be an object")
    sides = d.get("sides")
    if not isinstance(sides, list) or not sides or len(set(sides)) != len(sides):
        raise Refused("decision.sides must be a non-empty list of distinct member keys")
    unknown = [s for s in sides if s not in hc["members"]]
    if unknown:
        raise Refused(f"unknown member(s) {unknown}; members: {list(hc['members'])}")
    if not isinstance(d.get("why"), str) or not d["why"].strip():
        raise Refused("decision.why is required: one line on why these sides")
    sides = [k for k in hc["order"] if k in sides]
    children = {}
    if len(sides) > 1:
        raw = d.get("children") or {}
        for side in sides:
            c = raw.get(side) if isinstance(raw, dict) else None
            if not isinstance(c, dict) or not str(c.get("title") or "").strip() or not c.get("body_file"):
                raise Refused(f"decision.children.{side} needs a title and a body_file")
            bf = Path(c["body_file"])
            if not bf.is_absolute():
                bf = Path(path).resolve().parent / bf
            try:
                body = bf.read_text(encoding="utf-8").replace("\r\n", "\n")
            except OSError as e:
                raise Refused(f"decision.children.{side}.body_file unreadable: {e}")
            children[side] = {"title": str(c["title"]).strip(), "body": body}
    canon = {"sides": sides, "why": d["why"].strip(),
             "children": {k: {"title": v["title"], "body": v["body"]} for k, v in children.items()}}
    digest = hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"sides": sides, "why": d["why"].strip(), "children": children, "hash": digest}


def add_blocker(body, blocker, why):
    """`- <blocker> — why` into the last `## Blocked by` (a lone `- None.` gives way), or a new
    section at the end. Idempotent. Same placement as super-board-card.sh link."""
    lines = body.replace("\r\n", "\n").split("\n")
    heads = [i for i, l in enumerate(lines) if re.match(r"^##[ \t]+Blocked by[ \t]*:?[ \t]*$", l, re.I)]
    bullet = f"- {blocker} — {why}"
    if not heads:
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines + ["", "## Blocked by", "", bullet, ""])
    start = heads[-1]
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^##[ \t]", lines[i])), len(lines))
    section = lines[start + 1:end]
    if any(re.match(r"^\s*-\s*" + re.escape(blocker) + r"(?![0-9])", l) for l in section):
        return body
    section = [l for l in section if not re.match(r"^\s*-\s*None\.?\s*$", l, re.I)]
    bullets = [i for i, l in enumerate(section) if re.match(r"^\s*-\s", l)]
    if bullets:
        section.insert(bullets[-1] + 1, bullet)
    else:
        if not section or section[0].strip():
            section.insert(0, "")
        section.insert(1, bullet)
    lines[start + 1:end] = section
    return "\n".join(lines)


def child_body(decision, side, prev, hub_ref):
    """The body filed for `side`; prev = (member key, ref) of the side before it, or None.
    Ends with the backlink that later proves this child belongs to this split."""
    body = decision["children"][side]["body"]
    if prev:
        body = add_blocker(body, prev[1], f"the {prev[0]} side lands first")
    return body.rstrip("\n") + "\n\n" + CHILD_LINK.format(hub=hub_ref.lower(), side=side) + "\n"


def verify_child(gh, hc, hub_ref, side, recorded):
    """A child named in the marker is a hint: it counts only when it is in the side's member
    repo, carries this split's backlink and was opened by this run's identity."""
    repo = hc["members"][side]["repo"]
    try:
        crepo, num = parse_ref(recorded, repo)
    except ValueError:
        raise Refused(f"recorded {side} child {recorded!r} is not an issue ref")
    if not same(crepo, repo):
        raise Refused(f"recorded {side} child {recorded} is not in {repo}")
    node = gh.issue(repo, num)
    link = CHILD_LINK.format(hub=hub_ref.lower(), side=side)
    author = (node.get("author") or {}).get("login")
    has_link = link in (node.get("body") or "")
    if not has_link or not same(author, gh.me()):
        raise Refused(f"recorded {side} child {ref(repo, num)} was not filed by this split "
                      f"(author {author!r}, backlink {'present' if has_link else 'missing'})")
    return num


def validate_children(decision, hc, labels, cfg, skip=()):
    """card-ready's ticket check on every body that will be filed. Refused lists what is missing."""
    bad = []
    ac_min = int(((cfg.get("triage") or {}).get("checkable_ac_min")) or 1)
    sides = decision["sides"]
    for i, side in enumerate(sides):
        if side in skip:
            continue
        repo = hc["members"][side]["repo"]
        prev = (sides[i - 1], ref(hc["members"][sides[i - 1]]["repo"], 1)) if i else None
        issue = {"number": 1, "title": decision["children"][side]["title"],
                 "body": child_body(decision, side, prev, ref(hc["hub"], 1)),
                 "labels": sorted({l.lower() for l in labels}), "state": "OPEN", "comments": None, "repository": repo}
        others = [m["repo"] for k, m in hc["members"].items() if k != side] + [hc["hub"]]
        verdict = None
        if "qa" not in issue["labels"]:
            graph = card_ready.deps_run([issue], ["--repo", repo, "--linked", ",".join(others)])
            entry = graph.get("1")
            verdict = (bool(entry.get("parseable")), entry.get("why") or "") if isinstance(entry, dict) else None
        missing, _ = card_ready.judge(issue, verdict, max(1, ac_min))
        if missing:
            bad.append(f"{side}: missing {', '.join(missing)}")
    if bad:
        raise Refused("child body not in the ticket format (writing-standard.md § 3) — " + "; ".join(bad))


# ── comments ────────────────────────────────────────────────────────────

def marker_line(state):
    return f"<!-- sb-split: {json.dumps(state, sort_keys=True, ensure_ascii=False)} -->"


def split_comment(state, hc, why, final):
    sides = state["sides"]
    kids = state.get("children") or {}
    if len(sides) == 1:
        side = sides[0]
        if not final:
            head = f"[triage] [report] ⏳ splitting → {side}"
            lines = [head, f"Did: {why}", f"Next: triage — moving this issue to {hc['members'][side]['repo']}"]
        else:
            head = f"[triage] [report] ✅ split → {side}"
            lines = [head, f"Did: {why}",
                     f"✅ Done: moved from {state['from']} to {hc['members'][side]['repo']}"
                     + (f"; labels re-applied: {', '.join(state['applied'])}" if state.get("applied") else "")]
            if state.get("skipped"):
                lines.append(f"❌ Not done: labels missing in {hc['members'][side]['repo']}: {', '.join(state['skipped'])}")
            lines.append("Next: none")
    else:
        listed = " · ".join(f"{k} {kids[k]}" for k in sides if k in kids)
        if final:
            lines = [f"[triage] [report] ✅ split → {', '.join(sides)}", f"Did: {why}",
                     f"✅ Done: {listed}; each later side waits on the one before it",
                     "Next: Eric — move the children to Ready" if hc["confirm"] else "Next: none"]
        else:
            left = [k for k in sides if k not in kids]
            lines = [f"[triage] [report] ⏳ splitting → {', '.join(sides)}", f"Did: {why}",
                     f"✅ Done: {listed}" if listed else "✅ Done: nothing filed yet",
                     f"❌ Not done: {', '.join(left)}" if left else "❌ Not done: attaching and board placement",
                     "Next: triage — rerun apply to finish"]
    return "\n".join(lines + [marker_line(state)])


def proposal_comment(decision, hc, digest):
    sides = decision["sides"]
    repos = ", ".join([hc["hub"]] + [hc["members"][s]["repo"] for s in sides])
    lines = [f"[triage] [question] 🙋 approve split → {', '.join(sides)}", f"Did: {decision['why']}",
             f"Next: Eric — reply exactly `done {digest[:8]}` to split as below (needs write on {repos})"]
    if len(sides) == 1:
        lines.append(f"- move this issue to {hc['members'][sides[0]]['repo']}")
    for i, side in enumerate(sides if len(sides) > 1 else []):
        wait = f" (waits on {sides[i - 1]})" if i else ""
        lines.append(f"- {side} · {hc['members'][side]['repo']}: {decision['children'][side]['title']}{wait}")
    return "\n".join(lines + [f"<!-- sb-split-proposal: {digest} -->"])


def approved_reply(gh, node, decision, hc):
    """(approver, proposal posted?). Approved = after this run's own proposal for THIS decision,
    a human replied `done <digest[:8]>` and has write+ on the hub AND every member repo the
    decision writes to. Proposals written by anyone else are ignored."""
    digest = decision["hash"]
    comments = comments_of(node)
    at = None
    for i, c in enumerate(comments):
        m = PROPOSAL.search(c.get("body") or "")
        if m and gh.mine(c):
            at = i if m.group(1) == digest else None
    if at is None:
        return None, False
    repos = [hc["hub"]] + [hc["members"][s]["repo"] for s in decision["sides"]]
    for c in comments[at + 1:]:
        m = APPROVE.match(c.get("body") or "")
        login = (c.get("author") or {}).get("login")
        if not m or m.group(1).lower() != digest[:8]:
            continue
        if all(gh.permission(r, login) in TRUSTED for r in repos):
            return login, True
        print(f"split: approval by {login!r} ignored: needs write on {', '.join(repos)}", file=sys.stderr)
    return None, True


# ── verbs ───────────────────────────────────────────────────────────────

def plan(gh, hc, repo, n):
    node = gh.issue(repo, n)
    item = project_item(node, hc)
    state, _ = marker_of(node, gh)
    subs = subs_by_member(node, hc)
    return {"issue": {"ref": ref((node.get("repository") or {}).get("nameWithOwner") or repo, n),
                      "title": node.get("title"), "body": node.get("body"), "state": node.get("state"),
                      "labels": labels_of(node), "column": item["status"] if item else None,
                      "subIssues": [{"ref": ref((s.get("repository") or {}).get("nameWithOwner"), s["number"]),
                                     "title": s.get("title"), "state": s.get("state")}
                                    for s in (node.get("subIssues") or {}).get("nodes") or []],
                      "byMember": {k: ref((s.get("repository") or {}).get("nameWithOwner"), s["number"])
                                   for k, s in subs.items()},
                      "split": state},
            "hub": hc["hub"], "members": list(hc["members"].values()),
            "split": {"default_order": hc["order"], "confirm": hc["confirm"]}}


def apply(gh, hc, cfg, repo, n, decision, approved):
    node = gh.issue(repo, n)
    here = (node.get("repository") or {}).get("nameWithOwner") or repo
    state, comment_id = marker_of(node, gh)
    sides = decision["sides"]
    if state and state.get("sides") != sides:
        raise Refused(f"a split with sides {state.get('sides')} is already recorded on {ref(here, n)}; "
                      "finish it or remove its sb-split comment")
    if len(sides) == 1:
        target = hc["members"][sides[0]]["repo"]
        if not same(here, hc["hub"]) and not same(here, target):
            raise Refused(f"{ref(here, n)} is neither in the hub {hc['hub']} nor in {target}")
        if same(here, target) and not state:
            raise Refused(f"{ref(here, n)} already lives in {target} and carries no split record")
    elif not same(here, hc["hub"]):
        raise Refused(f"{ref(here, n)} is not a hub issue ({hc['hub']})")

    # Recorded children are hints: verified (repo, backlink, author) or refused. Children not
    # yet filed must pass the ticket check. Both before anything is written.
    existing = subs_by_member(node, hc)
    verified = {}
    if len(sides) > 1:
        for side, recorded in ((state or {}).get("children") or {}).items():
            if side in sides and side not in existing:
                verified[side] = verify_child(gh, hc, ref(here, n), side, recorded)
        validate_children(decision, hc, labels_of(node), cfg, skip=set(existing) | set(verified))

    if state and state.get("done"):
        if len(sides) == 1 or all(s in existing for s in sides):
            return {"status": "already-split", "issue": ref(here, n), "sides": sides,
                    "children": {k: ref((existing[k].get("repository") or {}).get("nameWithOwner"), existing[k]["number"])
                                 for k in existing} if len(sides) > 1 else {}}

    # The confirm gate. A marker passes it only when it recorded an approval (and markers are
    # already limited to this run's own identity).
    if not hc["confirm"]:
        approved_by = "confirm-off"
    elif approved:
        approved_by = "--approved"
    elif state and state.get("approved_by") not in (None, "", "confirm-off"):
        approved_by = state["approved_by"]
    else:
        approved_by, posted = approved_reply(gh, node, decision, hc)
        if not approved_by:
            if not posted:
                gh.comment(here, n, proposal_comment(decision, hc, decision["hash"]))
            return {"status": "awaiting-approval", "issue": ref(here, n), "sides": sides,
                    "proposal": "posted" if not posted else "already posted"}

    if len(sides) == 1:
        return apply_one(gh, hc, node, here, n, decision, state, comment_id, approved_by)
    return apply_many(gh, hc, node, here, n, decision, comment_id, existing, verified, approved_by)


def apply_one(gh, hc, node, here, n, decision, state, comment_id, approved_by):
    side = decision["sides"][0]
    target = hc["members"][side]["repo"]
    item = project_item(node, hc)
    # Labels and column come from the live issue. The transfer drops the labels, so after it
    # the only record left is this run's own marker, read back only when resuming.
    if not state:
        state = {"v": 1, "sides": [side], "from": ref(here, n), "labels": labels_of(node),
                 "column": item["status"] if item else "", "done": False, "approved_by": approved_by}
        comment_id = gh.comment(here, n, split_comment(state, hc, decision["why"], False))
    state["approved_by"] = approved_by
    moved_n = n
    if same(here, hc["hub"]):
        out = gh.write(f"transfer {ref(here, n)} to {target}", ["issue", "transfer", str(n), target, "--repo", here],
                       dry_out=f"https://github.com/{target}/issues/0")
        m = re.search(r"/issues/([0-9]+)\s*$", (out or "").strip())
        if not m:
            raise WriteFailed(f"transfer {ref(here, n)}: no issue URL in {out.strip()[:200]!r} — find it in {target}")
        moved_n = int(m.group(1))
        if not gh.dry:
            print(f"transferred {ref(here, n)} → {ref(target, moved_n)}", file=sys.stderr)
    moved = gh.issue(target, moved_n) if not gh.dry else {"labels": {"nodes": []}, "projectItems": {"nodes": []},
                                                           "comments": {"nodes": []}}
    if not gh.dry:
        found, cid = marker_of(moved, gh)
        comment_id = cid if cid is not None else comment_id
    have = {l.lower() for l in labels_of(moved)}
    want = [l for l in state.get("labels") or [] if l.lower() not in have]
    exist = gh.repo_labels(target)
    applied = [exist[l.lower()] for l in want if l.lower() in exist]
    skipped = [l for l in want if l.lower() not in exist]
    gh.add_labels(target, moved_n, applied)
    if not project_item(moved, hc):
        gh.add_to_project(target, moved_n)
        gh.move(target, moved_n, state.get("column") or "Backlog")
    state.update({"done": True, "to": ref(target, moved_n), "applied": applied, "skipped": skipped})
    body = split_comment(state, hc, decision["why"], True)
    if comment_id is not None:
        gh.edit_comment(target, comment_id, body, moved_n)
    else:
        gh.comment(target, moved_n, body)
    return {"status": "dry-run" if gh.dry else "split", "issue": ref(target, moved_n), "from": state["from"],
            "sides": [side], "labelsApplied": applied, "labelsSkipped": skipped}


def apply_many(gh, hc, node, here, n, decision, comment_id, existing, verified, approved_by):
    sides = decision["sides"]
    item = project_item(node, hc)  # the live parent: labels and column never come from a marker
    column = "Backlog" if hc["confirm"] or not item or not item["status"] else item["status"]
    state = {"v": 1, "sides": sides, "done": False, "approved_by": approved_by,
             "children": {k: ref(hc["members"][k]["repo"], v) for k, v in verified.items()}}
    parent_labels = [l for l in labels_of(node) if l.lower() not in NOT_COPIED]
    subs = {s.get("id") for s in (node.get("subIssues") or {}).get("nodes") or []}
    out = {}
    prev = None
    for side in sides:
        repo = hc["members"][side]["repo"]
        if side in existing:
            num = existing[side]["number"]
        elif side in verified:
            num = verified[side]
        else:
            exist = gh.repo_labels(repo)
            labels = [exist[l.lower()] for l in parent_labels if l.lower() in exist]
            num = gh.create(repo, decision["children"][side]["title"], child_body(decision, side, prev, ref(here, n)), labels)
            state["children"][side] = ref(repo, num)
            body = split_comment(state, hc, decision["why"], False)
            if comment_id is not None:
                gh.edit_comment(here, comment_id, body, n)
            else:
                comment_id = gh.comment(here, n, body)
        state["children"][side] = ref(repo, num)
        out[side] = ref(repo, num)
        prev = (side, ref(repo, num))
        if gh.dry and side not in existing and num == 0:
            gh.add_to_project(repo, num)
            gh.move(repo, num, column)
            gh.add_sub_issue(node.get("id"), f"<id of {ref(repo, num)}>", ref(repo, num))
            continue
        child = gh.issue(repo, num)
        citem = project_item(child, hc)
        if not citem:
            gh.add_to_project(repo, num)
        if not citem or not citem["status"]:
            gh.move(repo, num, column)
        if child.get("id") not in subs:
            gh.add_sub_issue(node.get("id"), child.get("id"), ref(repo, num))
    if "parent" not in {l.lower() for l in labels_of(node)}:
        if "parent" not in gh.repo_labels(here):
            gh.write(f"create the parent label in {here}",
                     ["label", "create", "parent", "--repo", here, "--color", "BFD4F2",
                      "--description", "Hub issue split into sub-issues; never built itself"])
        gh.add_labels(here, n, ["parent"])
    if item and item["status"] != "Backlog":
        gh.move(here, n, "Backlog")
    state["done"] = True
    body = split_comment(state, hc, decision["why"], True)
    if comment_id is not None:
        gh.edit_comment(here, comment_id, body, n)
    else:
        gh.comment(here, n, body)
    return {"status": "dry-run" if gh.dry else "split", "issue": ref(here, n), "sides": sides,
            "children": out, "column": column}


def close_parents(gh, hc):
    owner, name = hc["hub"].split("/")
    pages = gh.read(["api", "graphql", "--paginate", "--slurp", "-f", "query=" + PARENTS_QUERY,
                     "-F", f"owner={owner}", "-F", f"name={name}"], "array")
    closed, flagged, waiting = [], [], []
    for page in pages:
        if not isinstance(page, dict) or page.get("errors"):
            raise WriteFailed("open hub issues read returned errors")
        for issue in page["data"]["repository"]["issues"]["nodes"]:
            kids = (issue.get("subIssues") or {}).get("nodes") or []
            if not kids:
                continue
            n = issue["number"]
            refs = [ref((k.get("repository") or {}).get("nameWithOwner"), k["number"]) for k in kids]
            done = [r for r, k in zip(refs, kids) if k.get("state") == "CLOSED" and k.get("stateReason") == "COMPLETED"]
            dropped = [r for r, k in zip(refs, kids) if k.get("state") == "CLOSED" and k.get("stateReason") == "NOT_PLANNED"]
            if len(done) == len(kids):
                gh.comment(hc["hub"], n, "\n".join([
                    "[orchestrator] [report] ✅ done · every sub-issue completed",
                    "Did: closed this parent; GitHub never closes it on its own",
                    f"✅ Done: {' · '.join(refs)}", "Next: none"]))
                gh.write(f"close {ref(hc['hub'], n)} as completed",
                         ["issue", "close", str(n), "--repo", hc["hub"], "--reason", "completed"])
                closed.append(ref(hc["hub"], n))
            elif dropped:
                if not any(NEEDS_HUMAN in (c.get("body") or "") and gh.mine(c) for c in comments_of(issue)):
                    gh.comment(hc["hub"], n, "\n".join([
                        f"[orchestrator] [question] 🙋 {len(dropped)} sub-issue(s) closed as not planned",
                        f"Did: left this parent open; not planned: {' · '.join(dropped)}",
                        f"✅ Done: {' · '.join(done)}" if done else "✅ Done: none yet",
                        "Next: Eric — close this parent, or file the missing side again",
                        NEEDS_HUMAN]))
                flagged.append(ref(hc["hub"], n))
            else:
                waiting.append(ref(hc["hub"], n))
    return {"status": "dry-run" if gh.dry else "ok", "closed": closed, "needsHuman": flagged, "open": waiting}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("verb", choices=("plan", "apply", "close-parents"))
    p.add_argument("--config", required=True)
    p.add_argument("--issue")
    p.add_argument("--decision")
    p.add_argument("--approved", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    try:
        cfg, hc = hub_config(a.config)
    except resolver.ConfigError as e:
        print(f"split: config invalid: {e}", file=sys.stderr)
        return resolver.INVALID
    except (OSError, ValueError, SystemExit) as e:
        print(f"split: config: {e}", file=sys.stderr)
        return 66
    if a.verb != "close-parents" and not a.issue:
        print("split: --issue <hub#N> is required", file=sys.stderr)
        return 64
    if a.verb == "apply" and not a.decision:
        print("split: apply needs --decision <file.json>", file=sys.stderr)
        return 64
    gh = GitHub(hc, a.config, a.dry_run)
    try:
        if a.verb == "close-parents":
            result = close_parents(gh, hc)
        else:
            repo, n = parse_ref(a.issue, hc["hub"])
            if a.verb == "plan":
                result = plan(gh, hc, repo, n)
            else:
                result = apply(gh, hc, cfg, repo, n, load_decision(a.decision, hc), a.approved)
        if a.dry_run:
            result["writes"] = gh.writes
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Refused as e:
        print(f"split: refused: {e}", file=sys.stderr)
        return 3
    except github_read.ReadHalted as e:
        print(f"split: HALT: {e}", file=sys.stderr)
        return github_read.HALTED
    except WriteFailed as e:
        print(f"split: {e}", file=sys.stderr)
        print(f"split: written: {'; '.join(gh.done) or 'nothing'}", file=sys.stderr)
        print("split: left: rerun the same apply; it continues from the sb-split marker", file=sys.stderr)
        return 69
    except ValueError as e:
        print(f"split: {e}", file=sys.stderr)
        return 64
    finally:
        gh.cleanup()


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
