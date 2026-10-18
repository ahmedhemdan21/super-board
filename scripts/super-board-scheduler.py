#!/usr/bin/env python3
"""super-board-scheduler.py — run super-board jobs on a schedule, with history.

WHY: the board and the collector are Claude skills; something has to start them at
02:00 or after each dev deploy, and someone has to see what happened afterwards. A
plain OS scheduler keeps no history and no summary; WSL brings a second toolchain.
This is one stdlib-Python process (3.9+, Windows / Linux / macOS) that fires jobs,
runs `claude -p` for each step, keeps a record per run, and serves the records on a
loopback web page. Later the same file runs unchanged on a Linux server.

Usage:
  super-board-scheduler.py serve [--config <file>] [--bind 127.0.0.1] [--allow-remote]
  super-board-scheduler.py status | show <job>/<stamp> | run-now <job> | ui-url
  super-board-scheduler.py install-service | uninstall-service [--dry-run] [--out <dir>]
  super-board-scheduler.py check-config

Config (default %APPDATA%\\super-board\\scheduler.json on Windows,
$XDG_CONFIG_HOME or ~/.config/super-board/scheduler.json elsewhere; --config or
SB_SCHEDULER_CONFIG override):
  {"port": 8765, "timezone": null, "state_dir": null, "mention_on_failure": null,
   "jobs": [{"name": "backend-nightly", "repo": "D:/Work/app/backend", "config": "app-backend",
             "steps": ["collect", "run"], "cron": "0 2 * * *",
             "after_workflow": {"repo": "owner/app", "workflow": "deploy-dev.yml", "branch": "dev"},
             "collect_sources": ["sentry", "posthog"], "timeout_s": 14400,
             "permission_mode": null, "max_budget_usd": null, "runs_issue": true}]}
  steps is an enum (collect, run): the scheduler runs nothing else, ever. Unknown job
  keys are refused.

Steps (each `claude -p "<prompt>" --output-format stream-json --verbose` in the job repo):
  collect  `/super-collect --yes` (or `/super-collect <sources…> --yes`)
  run      `/super-board run <config>`, with `--allowedTools Workflow`. The default
           worker backend runs waves through the Workflow tool, which `claude -p`
           offers but denies without that flag ("Review dynamic workflow before
           running"). A run step whose session reports no Workflow tool is stopped and
           failed with that reason; a config with worker_backend "claude-p" is refused.
           Nothing ever falls back to the legacy backend.
  Anything else a lane needs must be in the project's permissions.allow (onboard →
  Permissions): headless sessions have nobody to answer a prompt, so it is denied.

Triggers:
  cron            5 fields (minute hour day-of-month month day-of-week; `*`, lists,
                  ranges, steps; day-of-month and day-of-week both restricted = either
                  matches, like cron). Local wall time, or config "timezone" (IANA).
                  DST: a wall time skipped by spring-forward fires at the first minute
                  after the jump (02:30 → 03:30); a wall time repeated by fall-back
                  fires once (the first time). A slot found more than 5 minutes late
                  (machine off or asleep) is recorded `missed`, never run late.
  after_workflow  every poll_s (default 120, min 60): `gh run list --workflow --branch
                  --status success --limit 1`; fires once per new run id (the first
                  sighting is the baseline). Skipped while GraphQL or REST remaining < 500.

A run is `skipped: board busy` while the repo has a board running (inflight/
workflow-wave.lock, codex-wave.pid, a legacy inflight/<N> lane lock, or another
scheduler run on that repo) and `skipped: github halted` while the repo's GitHub halt
file exists. Lock files are trusted as present: a stale one keeps skipping, visibly.

Records: <state>/runs/<job>/<UTC stamp>/ run.json · log.txt (stream-json flattened,
secrets redacted) · stream.jsonl · summary.json (board snapshots before/after through
super-board-status.py --json, PRs opened/merged, collect receipts, quota). The last 200
runs per job are kept. State dir: %LOCALAPPDATA%\\super-board\\scheduler, ~/Library/
Application Support/super-board/scheduler, or $XDG_STATE_HOME (~/.local/state)/
super-board/scheduler; 0700 where the OS has modes.

Each finished run (not `missed`) appends one comment to the repo's open issue titled
"super-board runs" (label super-board-runs), created when absent, in the
writing-standard comment format with role `scheduler`.

Web UI: 127.0.0.1:<port> only. A non-loopback --bind needs --allow-remote. Every page
needs the token (<state>/token, 0600; `ui-url` prints the link) as ?token= once, then a
cookie. "Run now" is a POST with the token and a same-origin check.

Never runs as root / Administrator. Exits: 0 ok · 64 usage/config error · 69 busy
(another serve holds the state dir) · 77 refused (root, non-loopback bind).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import html
import http.server
import importlib.util
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = 1
STEPS = ("collect", "run")
JOB_KEYS = {"name", "repo", "config", "steps", "cron", "after_workflow", "collect_sources", "timeout_s",
            "permission_mode", "max_budget_usd", "runs_issue", "poll_s", "mention_on_failure"}
TOP_KEYS = {"port", "bind", "timezone", "state_dir", "mention_on_failure", "jobs"}
PERMISSION_MODES = ("default", "acceptEdits", "auto", "dontAsk", "plan")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SLUG = re.compile(r"[A-Za-z0-9._-]{1,64}")
SOURCE = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")
GH_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
KEEP_RUNS = 200
MISSED_GRACE_S = 300
TICK_S = 15
RUNS_TITLE = "super-board runs"
RUNS_LABEL = "super-board-runs"
QUOTA_FLOOR = 500

# Secret shapes, copied from hooks/guard-key-literals.py (the hooks ship separately and
# install.sh --no-hooks skips them); tests/test_scheduler.py checks the two agree.
KEY_PATTERNS = [
    r"sk-ant-[A-Za-z0-9_-]{20,}", r"sk-proj-[A-Za-z0-9_-]{20,}", r"sk-[A-Za-z0-9]{24,}",
    r"AKIA[0-9A-Z]{16}", r"ASIA[0-9A-Z]{16}", r"gh[pousr]_[A-Za-z0-9]{30,}", r"github_pat_[A-Za-z0-9_]{30,}",
    r"xox[baprs]-[A-Za-z0-9-]{12,}", r"AIza[A-Za-z0-9_-]{35}", r"sbp_[a-f0-9]{40}", r"r8_[A-Za-z0-9]{35,}",
    r"hf_[A-Za-z0-9]{30,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]
REDACT = re.compile("|".join(KEY_PATTERNS + [
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{12,}",
    r"(?<=[?&]token=)[A-Za-z0-9_-]{8,}",
]), re.IGNORECASE)
ASSIGN = re.compile(r"(?i)\b([A-Z0-9_]*(?:api[_-]?key|secret|token|password|passwd)[A-Z0-9_]*)(\s*[:=]\s*)([\"']?)[^\s\"',;]{8,}")


def redact(text):
    text = REDACT.sub("[redacted]", text or "")
    return ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[redacted]", text)


# ── paths ───────────────────────────────────────────────────────────────────

def default_config_path():
    if os.environ.get("SB_SCHEDULER_CONFIG"):
        return Path(os.environ["SB_SCHEDULER_CONFIG"])
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "super-board" / "scheduler.json"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "super-board" / "scheduler.json"


def default_state_dir():
    if os.environ.get("SB_SCHEDULER_STATE"):
        return Path(os.environ["SB_SCHEDULER_STATE"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "super-board" / "scheduler"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "super-board" / "scheduler"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "super-board" / "scheduler"


def private_dir(p):
    p.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(p, 0o700)
    return p


def write_private(path, text):
    """Create/replace a file readable by the owner only (0600 where modes exist)."""
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json(path, data):
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def is_admin():
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ── config ──────────────────────────────────────────────────────────────────

class ConfigError(ValueError):
    pass


def load_config(path):
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"no scheduler config at {path} (see references/scheduling.md)") from None
    except ValueError as e:
        raise ConfigError(f"{path} is not JSON: {e}") from None
    return validate_config(cfg)


def validate_config(cfg):
    if not isinstance(cfg, dict):
        raise ConfigError("config must be an object")
    extra = set(cfg) - TOP_KEYS
    if extra:
        raise ConfigError(f"unknown config keys: {sorted(extra)}")
    port = cfg.get("port", 8765)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ConfigError("port must be 1024-65535")
    if cfg.get("timezone"):
        tz(cfg["timezone"])
    if cfg.get("mention_on_failure") and not LOGIN.fullmatch(str(cfg["mention_on_failure"])):
        raise ConfigError("mention_on_failure must be a GitHub login")
    jobs, names = [], set()
    for j in cfg.get("jobs") or []:
        if not isinstance(j, dict):
            raise ConfigError("each job must be an object")
        extra = set(j) - JOB_KEYS
        if extra:
            raise ConfigError(f"job {j.get('name')}: unknown keys {sorted(extra)} (steps is the only way to say what runs)")
        name = str(j.get("name") or "")
        if not NAME.fullmatch(name) or name in names:
            raise ConfigError(f"job name {name!r} must be unique, letters/digits/._-")
        names.add(name)
        if not j.get("repo") or not Path(j["repo"]).is_dir():
            raise ConfigError(f"job {name}: repo {j.get('repo')!r} is not a folder")
        if not SLUG.fullmatch(str(j.get("config") or "")):
            raise ConfigError(f"job {name}: config must be a super-board config slug")
        steps = j.get("steps")
        if not isinstance(steps, list) or not steps or any(s not in STEPS for s in steps):
            raise ConfigError(f"job {name}: steps must be a non-empty list of {STEPS}")
        if j.get("cron"):
            Cron(j["cron"])
        aw = j.get("after_workflow")
        if aw is not None:
            if not (isinstance(aw, dict) and GH_REPO.fullmatch(str(aw.get("repo") or ""))
                    and re.fullmatch(r"[A-Za-z0-9._-]+\.ya?ml", str(aw.get("workflow") or ""))
                    and re.fullmatch(r"[A-Za-z0-9._/-]+", str(aw.get("branch") or ""))):
                raise ConfigError(f"job {name}: after_workflow needs repo owner/name, workflow <file>.yml, branch")
        for s in j.get("collect_sources") or []:
            if not SOURCE.fullmatch(str(s)):
                raise ConfigError(f"job {name}: collect source {s!r} is not a source name")
        if j.get("permission_mode") not in (None,) + PERMISSION_MODES:
            raise ConfigError(f"job {name}: permission_mode must be one of {PERMISSION_MODES} (never bypassPermissions)")
        t = j.get("timeout_s", 14400)
        if not isinstance(t, int) or not 60 <= t <= 86400:
            raise ConfigError(f"job {name}: timeout_s must be 60-86400")
        if j.get("mention_on_failure") and not LOGIN.fullmatch(str(j["mention_on_failure"])):
            raise ConfigError(f"job {name}: mention_on_failure must be a GitHub login")
        jobs.append(dict(j, timeout_s=t, poll_s=max(60, int(j.get("poll_s") or 120)),
                         runs_issue=j.get("runs_issue", True)))
    return dict(cfg, port=port, jobs=jobs)


def tz(name):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as e:  # missing tzdata on Windows, bad name
        raise ConfigError(f"timezone {name!r} is not available: {e}") from None


# ── cron ────────────────────────────────────────────────────────────────────

class Cron:
    RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))

    def __init__(self, expr):
        parts = str(expr).split()
        if len(parts) != 5:
            raise ConfigError(f"cron {expr!r}: need 5 fields (minute hour day month weekday)")
        self.expr = expr
        self.sets = [self._field(p, lo, hi, i == 4) for i, (p, (lo, hi)) in enumerate(zip(parts, self.RANGES))]
        self.dom_any, self.dow_any = parts[2] == "*", parts[4] == "*"

    @staticmethod
    def _field(text, lo, hi, dow):
        out = set()
        for item in text.split(","):
            m = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", item)
            if not m:
                raise ConfigError(f"cron field {text!r} is not valid")
            rng, step = m.group(1), int(m.group(2) or 1)
            if rng == "*":
                a, b = lo, hi
            elif "-" in rng:
                a, b = (int(x) for x in rng.split("-"))
            else:
                a = b = int(rng)
                if m.group(2):
                    b = hi
            top = 7 if dow else hi  # weekday 7 is Sunday, like 0
            if step < 1 or a < lo or b > top or a > b:
                raise ConfigError(f"cron field {text!r} is out of range {lo}-{hi}")
            out.update(x % 7 if dow else x for x in range(a, b + 1, step))
        return out

    def day_ok(self, t):
        _, _, dom, _, dow = self.sets
        d_ok, w_ok = t.day in dom, (t.isoweekday() % 7) in dow
        # Both restricted: either one matches (cron). One is `*`: the other decides.
        return (d_ok and w_ok) if (self.dom_any or self.dow_any) else (d_ok or w_ok)

    def matches(self, w):
        mi, ho, _, mon, _ = self.sets
        return w.minute in mi and w.hour in ho and w.month in mon and self.day_ok(w)

    def next_after(self, w):
        """The first matching wall minute strictly after naive wall time w."""
        t = w.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        limit = t + dt.timedelta(days=366 * 5)
        mi, ho, _, mon, _ = self.sets
        while t < limit:
            if t.month not in mon:
                t = (t.replace(day=1, hour=0, minute=0) + dt.timedelta(days=32)).replace(day=1)
            elif not self.day_ok(t):
                t = t.replace(hour=0, minute=0) + dt.timedelta(days=1)
            elif t.hour not in ho:
                t = t.replace(minute=0) + dt.timedelta(hours=1)
            elif t.minute not in mi:
                t += dt.timedelta(minutes=1)
            else:
                return t
        raise ConfigError(f"cron {self.expr!r} never fires")


def wall_to_epoch(w, zone=None):
    """Naive wall time → epoch. A time skipped by spring-forward lands after the jump."""
    if zone is None:
        return time.mktime(w.timetuple()[:8] + (-1,))
    return w.replace(tzinfo=zone, fold=0).timestamp()


def epoch_to_wall(e, zone=None):
    if zone is None:
        return dt.datetime.fromtimestamp(e).replace(tzinfo=None)
    return dt.datetime.fromtimestamp(e, zone).replace(tzinfo=None)


def due_slots(cron, last_wall, now_epoch, zone=None, scan=20000):
    """Slots in (last_wall, now]: (fire, missed, newest). fire = (wall, epoch) of the
    newest slot when it is at most MISSED_GRACE_S old, else None; missed = the older
    (wall, epoch) slots; newest = the wall time to store as last_slot. Naive wall times."""
    out, slot = [], last_wall
    while len(out) < scan:
        nxt = cron.next_after(slot)
        e = wall_to_epoch(nxt, zone)
        if e > now_epoch:
            break
        out.append((nxt, e))
        slot = nxt
    if not out:
        return None, [], last_wall
    fire = out[-1] if now_epoch - out[-1][1] <= MISSED_GRACE_S else None
    return fire, (out[:-1] if fire else out), out[-1][0]


# ── github (reads through super-board-github-read.py) ───────────────────────

def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_GH = None


def ghread_mod():
    global _GH
    if _GH is None:
        _GH = _load("sb_github_read", "super-board-github-read.py")
    return _GH


class GitHub:
    """Required reads go through super-board-github-read.py with a scheduler-private
    halt file: a polling outage must never halt a board run. Writes check the repo's
    own halt first (a halted board gets no comment)."""

    def __init__(self, state):
        self.halt = state / "github-halt.json"

    def read(self, kind, args, repo_path=None):
        if self.halt.exists():
            self.halt.unlink()
        env = dict(os.environ, SB_GITHUB_HALT_FILE=str(self.halt), SB_GITHUB_READ_STRICT="1")
        if repo_path:
            env["SB_REPO_PATH"] = str(repo_path)
        r = subprocess.run([sys.executable, str(HERE / "super-board-github-read.py"), "--kind", kind, "--", *args],
                           capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"GitHub read failed ({' '.join(args[:2])}): {redact(r.stderr.strip())[-300:]}")
        return json.loads(r.stdout)

    def write(self, args, repo_path):
        chk = subprocess.run([sys.executable, str(HERE / "super-board-github-read.py"), "--check"],
                             capture_output=True, text=True, env=dict(os.environ, SB_REPO_PATH=str(repo_path)))
        if chk.returncode != 0:
            raise RuntimeError("the repo's GitHub halt is set; not writing")
        r = subprocess.run(ghread_mod().command("gh", args), capture_output=True, text=True, encoding="utf-8",
                           timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args[:2])} failed: {redact(r.stderr.strip())[-300:]}")
        return r.stdout

    def quota(self):
        q = self.read("quota", ["api", "rate_limit"])["resources"]
        return {"graphql": q["graphql"]["remaining"], "core": q["core"]["remaining"]}


# ── board state ─────────────────────────────────────────────────────────────

def board_root(repo):
    """The checkout whose .claude/super-board holds the board's markers (the main
    checkout for a linked worktree; the repo itself for a submodule or plain clone)."""
    r = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=str(repo), capture_output=True, text=True)
    common = (Path(repo) / r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None
    return common.parent if common is not None and common.name == ".git" else Path(repo).resolve()


def board_busy(repo):
    """A reason string when a board is running in (or halted for) this repo, else None."""
    sb = board_root(repo) / ".claude" / "super-board"
    if (sb / "github-halt.json").exists():
        return "github halted (.claude/super-board/github-halt.json)"
    if (sb / "inflight" / "workflow-wave.lock").exists():
        return "board busy (inflight/workflow-wave.lock)"
    if (sb / "codex-wave.pid").exists():
        return "board busy (codex-wave.pid)"
    inflight = sb / "inflight"
    if inflight.is_dir() and any(p.name.isdigit() for p in inflight.iterdir()):
        return "board busy (legacy lane lock in inflight/)"
    if os.name != "nt" and shutil.which("pgrep"):
        r = subprocess.run(["pgrep", "-f", "super-board-run.sh"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return "board busy (super-board-run.sh is running)"
    return None


class RepoLock:
    """One scheduler run per repo, across processes (serve and run-now)."""

    def __init__(self, state, repo):
        key = hashlib.sha256(os.path.normcase(str(Path(repo).resolve())).encode()).hexdigest()[:16]
        self.path = private_dir(state / "locks") / f"{key}.lock"

    def acquire(self):
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    pid = self.path.read_text().strip()
                except OSError:
                    pid = ""
                if pid_alive(pid):
                    return False
                try:
                    self.path.unlink()  # its process is gone
                except OSError:
                    return False
        return False

    def release(self):
        try:
            self.path.unlink()
        except OSError:
            pass


def board_config(repo, slug):
    p = Path(repo) / ".claude" / "super-board" / "configs" / f"{slug}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def board_repo_name(cfg):
    m = re.search(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
                  ((cfg or {}).get("repo") or {}).get("remote") or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


# ── steps ───────────────────────────────────────────────────────────────────

def step_prompt(job, step):
    if step == "collect":
        srcs = " ".join(job.get("collect_sources") or [])
        return f"/super-collect {srcs} --yes".replace("  ", " ")
    return f"/super-board run {job['config']}"


def claude_argv(job, step, claude=None):
    """argv for one step, never a shell string. `claude` (tests only, never config) is an
    argv prefix such as [python, stub.py]. Otherwise `claude` on PATH: claude.exe, an npm
    .cmd shim (run through cmd.exe /c), or a script (super-board-github-read.command)."""
    args = ["-p", step_prompt(job, step), "--output-format", "stream-json", "--verbose"]
    if step == "run":
        args += ["--allowedTools", "Workflow"]
    if job.get("permission_mode"):
        args += ["--permission-mode", job["permission_mode"]]
    if job.get("max_budget_usd"):
        args += ["--max-budget-usd", str(float(job["max_budget_usd"]))]
    if claude:
        return [*claude, *args]
    argv = ghread_mod().command("claude", args)
    if os.name == "nt" and argv[0].lower().endswith((".cmd", ".bat")):
        argv = ["cmd.exe", "/d", "/c", *argv]
    return argv


def kill_tree(proc):
    """Stop a step and everything it started (lane agents, gh, node)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def flatten(event):
    """One stream-json event → readable log lines (unredacted; the caller redacts)."""
    t = event.get("type")
    if t == "system" and event.get("subtype") == "init":
        tools = event.get("tools") or []
        return [f"[init] model={event.get('model')} permissionMode={event.get('permissionMode')} "
                f"tools={len(tools)} workflow={'yes' if 'Workflow' in tools else 'no'}"]
    if t in ("assistant", "user"):
        out = []
        content = (event.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append(c["text"].strip())
            elif c.get("type") == "tool_use":
                out.append(f"→ {c.get('name')} {json.dumps(c.get('input'), ensure_ascii=False)[:300]}")
            elif c.get("type") == "tool_result":
                body = c.get("content")
                if isinstance(body, list):
                    body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                out.append(f"← {str(body)[:500]}")
        return out
    if t == "result":
        return [f"[result] {event.get('subtype')} is_error={event.get('is_error')} "
                f"duration_ms={event.get('duration_ms')} cost_usd={event.get('total_cost_usd')}",
                str(event.get("result") or "")[-2000:]]
    return []


def run_step(job, step, run_dir, deadline, stop, claude=None):
    """Run one step; returns its record. Never leaves the child running."""
    rec = {"step": step, "prompt": step_prompt(job, step), "started": now_iso(), "exit": None,
           "status": "failed", "error": None}
    argv = claude_argv(job, step, claude)
    kw = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    log = open(run_dir / "log.txt", "a", encoding="utf-8")
    raw = open(run_dir / "stream.jsonl", "a", encoding="utf-8")
    log.write(f"=== step {step}: {rec['prompt']}\n")
    try:
        proc = subprocess.Popen(argv, cwd=job["repo"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", **kw)
    except OSError as e:
        rec.update(error=f"cannot start claude: {e}", ended=now_iso())
        log.write(rec["error"] + "\n")
        log.close(); raw.close()
        return rec
    seen = {"result": None, "init": None, "denied_workflow": False}

    def pump():
        for line in proc.stdout:
            raw.write(redact(line))
            try:
                ev = json.loads(line)
            except ValueError:
                log.write(redact(line))
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init" and seen["init"] is None:
                seen["init"] = ev
            if ev.get("type") == "result":
                seen["result"] = ev
                if any((d or {}).get("tool_name") == "Workflow" for d in ev.get("permission_denials") or []):
                    seen["denied_workflow"] = True
            for l in flatten(ev):
                log.write(redact(l) + "\n")
            log.flush()
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    reason = None
    while proc.poll() is None:
        if step == "run" and seen["init"] is not None and "Workflow" not in (seen["init"].get("tools") or []):
            reason = ("the Workflow tool is not available to claude -p here (dynamic workflows off?); "
                      "refusing: the run step never falls back to the legacy claude-p backend")
            break
        if stop.is_set():
            reason = "scheduler stopped mid-run"
            break
        if time.time() >= deadline:
            reason = "timeout"
            break
        time.sleep(0.5)
    if reason:
        kill_tree(proc)
    else:
        proc.wait()
    reader.join(15)
    try:
        proc.stdout.close()
    except OSError:
        pass
    rec["exit"] = proc.returncode
    res = seen["result"] or {}
    if reason == "timeout":
        rec.update(status="timeout", error=f"step ran past the job's timeout_s ({job['timeout_s']}s); process tree killed")
    elif reason:
        rec.update(status="failed", error=reason)
    elif proc.returncode != 0:
        rec["error"] = f"claude exited {proc.returncode}"
    elif seen["denied_workflow"]:
        rec["error"] = "the Workflow tool was denied (needs --allowedTools Workflow)"
    elif not res or res.get("is_error") or res.get("subtype") != "success":
        rec["error"] = f"no successful result event ({res.get('subtype') or 'none'})"
    else:
        rec["status"] = "ok"
    rec["ended"] = now_iso()
    log.write(f"=== step {step}: {rec['status']}{(' — ' + rec['error']) if rec['error'] else ''}\n")
    log.close(); raw.close()
    return rec


# ── summary ─────────────────────────────────────────────────────────────────

def status_snapshot(repo, slug):
    script = Path(repo) / ".claude" / "bin" / "super-board-status.py"
    script = script if script.is_file() else HERE / "super-board-status.py"
    r = subprocess.run([sys.executable, str(script), slug, "--json"], cwd=str(repo), capture_output=True,
                       text=True, encoding="utf-8", timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"status snapshot failed (exit {r.returncode}): {redact(r.stderr.strip())[-200:]}")
    return json.loads(r.stdout)


def collect_receipts(repo, start, end):
    """{source: n} from .claude/super-board/collect/*.json receipts written in [start, end]."""
    out = {}
    d = board_root(repo) / ".claude" / "super-board" / "collect"
    for f in d.glob("*.json") if d.is_dir() else []:
        try:
            receipts = json.loads(f.read_text(encoding="utf-8")).get("receipts") or {}
        except (OSError, ValueError):
            continue
        for fp, r in receipts.items():
            if isinstance(r, dict) and start <= (r.get("at") or 0) <= end:
                src = fp.split("|", 1)[0]
                out[src] = out.get(src, 0) + 1
    return dict(sorted(out.items()))


def summarize(before, after, prs, receipts, quota, errors):
    """summary.json from board snapshots (never from Claude's chat text). Pure."""
    s = {"moved": [], "moved_per_lane": {}, "new_cards": [], "prs_opened": None, "prs_merged": None,
         "bugs_filed_per_source": receipts, "flaky_opened": None, "regression_opened": None,
         "quota": quota, "errors": list(errors), "counts_before": None, "counts_after": None}
    if before and after:
        b = {i["number"]: i for i in before.get("items") or []}
        a = {i["number"]: i for i in after.get("items") or []}
        for n, it in sorted(a.items()):
            if n in b and b[n]["status"] != it["status"]:
                s["moved"].append({"number": n, "from": b[n]["status"], "to": it["status"]})
                k = f"{b[n]['status']}→{it['status']}"
                s["moved_per_lane"][k] = s["moved_per_lane"].get(k, 0) + 1
        new = [a[n] for n in sorted(set(a) - set(b))]
        s["new_cards"] = [{"number": i["number"], "status": i["status"], "labels": i["labels"]} for i in new]
        s["flaky_opened"] = sum(1 for i in new if any("flaky" in l for l in i["labels"]))
        s["regression_opened"] = sum(1 for i in new if "regression" in i["labels"])
        s["counts_before"], s["counts_after"] = before.get("counts"), after.get("counts")
    if prs is not None:
        s["prs_opened"], s["prs_merged"] = prs
    return s


def prs_between(gh, repo_name, start_iso, end_iso, repo_path):
    rows = gh.read("array", ["pr", "list", "-R", repo_name, "--state", "all", "--search", f"updated:>={start_iso[:10]}",
                             "--json", "number,createdAt,mergedAt", "--limit", "200"], repo_path)
    opened = sum(1 for r in rows if start_iso <= (r.get("createdAt") or "") <= end_iso)
    merged = sum(1 for r in rows if start_iso <= (r.get("mergedAt") or "") <= end_iso)
    return opened, merged


# ── runs issue ──────────────────────────────────────────────────────────────

STATUS_EMOJI = {"ok": "✅", "failed": "❌", "timeout": "❌", "skipped": "⚠️", "missed": "⚠️"}


def human_duration(s):
    s = int(s or 0)
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def runs_comment(run, summary, next_run, mention=None):
    """The writing-standard § 4 comment for one finished run (≤ 8 prose lines)."""
    st = run["status"]
    lines = [f"[scheduler] [report] {STATUS_EMOJI.get(st, '⚠️')} {st} · {run['job']}",
             f"Did: job {run['job']} · trigger {run['trigger']} · {human_duration(run.get('duration_s'))}"]
    sm = summary or {}
    moved = len(sm.get("moved") or [])
    bugs = sm.get("bugs_filed_per_source") or {}
    if summary is None:
        done = ["nothing ran"]
    elif sm.get("counts_after") is None:
        done = ["card moves unknown (board snapshot failed)"]
    else:
        done = [f"moved {moved} card{'s' * (moved != 1)}"]
    if sm.get("prs_merged") is not None:
        done.append(f"{sm['prs_opened']} PRs opened, {sm['prs_merged']} merged")
    if summary is not None:
        done.append(f"{sum(bugs.values())} filed" + (f" ({', '.join(f'{k} {v}' for k, v in bugs.items())})" if bugs else ""))
    lines.append("✅ Done: " + " · ".join(done))
    errs = [e for e in [run.get("error")] + [s.get("error") for s in run.get("steps") or []] + (sm.get("errors") or []) if e]
    if errs:
        lines.append("❌ Not done: " + "; ".join(dict.fromkeys(errs))[:400])
    nxt = f"next run {next_run}" if next_run else "no scheduled run"
    who = f"@{mention} · " if mention and st in ("failed", "timeout") else ""
    lines.append(f"Next: {who}{nxt}")
    q = sm.get("quota") or {}
    if q.get("graphql") is not None:
        lines.append(f"gh-quota-on-exit: graphql={q['graphql']}/5000 rest={q['core']}/5000")
    return "\n".join(lines)


def post_runs_comment(gh, repo_name, repo_path, body):
    rows = gh.read("array", ["issue", "list", "-R", repo_name, "--label", RUNS_LABEL, "--state", "open",
                             "--json", "number,title", "--limit", "50"], repo_path)
    hit = next((r["number"] for r in rows if r.get("title") == RUNS_TITLE), None)
    if hit is None:
        try:
            gh.write(["label", "create", RUNS_LABEL, "-R", repo_name, "--color", "BFD4F2", "--force"], repo_path)
        except RuntimeError:
            pass
        url = gh.write(["issue", "create", "-R", repo_name, "--title", RUNS_TITLE, "--label", RUNS_LABEL, "--body",
                        "One comment per super-board scheduler run (local scheduler and cloud routine). "
                        "Not a board card: nothing here is work to pick up."], repo_path).strip()
        hit = int(url.rstrip("/").rsplit("/", 1)[-1])
    gh.write(["issue", "comment", str(hit), "-R", repo_name, "--body", body], repo_path)
    return hit


# ── the scheduler ───────────────────────────────────────────────────────────

def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Scheduler:
    def __init__(self, cfg, state=None, claude=None, github=None):
        self.cfg = cfg
        self.state_dir = private_dir(Path(state or cfg.get("state_dir") or default_state_dir()))
        self.zone = tz(cfg.get("timezone"))
        self.claude = claude  # tests only: an argv prefix standing in for `claude`
        self.gh = github or GitHub(self.state_dir)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.queue = queue.Queue()
        self.running = {}
        self.state_file = self.state_dir / "state.json"
        try:
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {"jobs": {}}
        self._quota = (0.0, None)

    def job(self, name):
        return next((j for j in self.cfg["jobs"] if j["name"] == name), None)

    def jstate(self, name):
        return self.state["jobs"].setdefault(name, {})

    def save(self):
        with self.lock:
            write_json(self.state_file, self.state)

    # triggers
    def check_cron(self, now_epoch=None):
        now_epoch = now_epoch or time.time()
        fired = []
        for j in self.cfg["jobs"]:
            if not j.get("cron"):
                continue
            js = self.jstate(j["name"])
            now_wall = epoch_to_wall(now_epoch, self.zone).replace(second=0, microsecond=0)
            if "last_slot" not in js:
                js["last_slot"] = now_wall.strftime("%Y-%m-%dT%H:%M")  # no backfill on first start
                self.save()
                continue
            last = dt.datetime.strptime(js["last_slot"], "%Y-%m-%dT%H:%M")
            fire, missed, newest = due_slots(Cron(j["cron"]), last, now_epoch, self.zone)
            if missed:  # one record per gap, not one per slot: a week off is one line
                first, final = missed[0][0], missed[-1][0]
                span = f"{first:%Y-%m-%d %H:%M}" + (f" to {final:%Y-%m-%d %H:%M}" if len(missed) > 1 else "")
                self.record_simple(j, "cron", "missed", f"{len(missed)} slot(s) missed ({span}): machine off or "
                                   "asleep; not run late", missed[-1][1])
            if newest != last:
                js["last_slot"] = newest.strftime("%Y-%m-%dT%H:%M")
                self.save()
            if fire:
                self.queue.put((j["name"], "cron"))
                fired.append(j["name"])
        return fired

    def quota_ok(self):
        at, q = self._quota
        if time.time() - at > 110:
            try:
                q = self.gh.quota()
            except Exception:
                q = None
            self._quota = (time.time(), q)
        return bool(q) and q["graphql"] >= QUOTA_FLOOR and q["core"] >= QUOTA_FLOOR

    def check_workflows(self, now_epoch=None):
        now_epoch = now_epoch or time.time()
        fired = []
        for j in self.cfg["jobs"]:
            aw = j.get("after_workflow")
            if not aw:
                continue
            js = self.jstate(j["name"])
            if now_epoch - js.get("last_poll", 0) < j["poll_s"]:
                continue
            js["last_poll"] = now_epoch
            if not self.quota_ok():
                js["poll_note"] = "skipped: GitHub quota under 500 or unreadable"
                continue
            try:
                rows = self.gh.read("array", ["run", "list", "-R", aw["repo"], "--workflow", aw["workflow"],
                                              "--branch", aw["branch"], "--status", "success", "--limit", "1",
                                              "--json", "databaseId,headSha,updatedAt"])
            except Exception as e:
                js["poll_note"] = f"poll failed: {e}"[:300]
                continue
            js["poll_note"] = None
            if not rows:
                continue
            rid = rows[0].get("databaseId")
            if js.get("last_run_id") is None:
                js["last_run_id"] = rid  # baseline: never fire for a run that predates the scheduler
            elif rid != js["last_run_id"]:
                js["last_run_id"] = rid
                self.queue.put((j["name"], f"after_workflow:{rid}"))
                fired.append(j["name"])
        self.save()
        return fired

    # records
    def new_run_dir(self, job, when=None):
        stamp = dt.datetime.fromtimestamp(when or time.time(), dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = private_dir(self.state_dir / "runs" / job)
        d, i = base / stamp, 1
        while d.exists():
            i += 1
            d = base / f"{stamp}-{i}"
        d.mkdir()
        return d

    def prune(self, job):
        base = self.state_dir / "runs" / job
        dirs = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name) if base.is_dir() else []
        for p in dirs[:-KEEP_RUNS]:
            shutil.rmtree(p, ignore_errors=True)

    def record_simple(self, job, trigger, status, error, when=None):
        d = self.new_run_dir(job["name"], when)
        stamp = dt.datetime.fromtimestamp(when or time.time(), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run = {"job": job["name"], "repo": str(job["repo"]), "config": job["config"], "trigger": trigger,
               "started": stamp, "ended": stamp, "duration_s": 0, "exit": None, "status": status, "steps": [],
               "error": error, "version": VERSION}
        write_json(d / "run.json", run)
        write_json(d / "summary.json", summarize(None, None, None, {}, None, [error]))
        (d / "log.txt").write_text(f"{status}: {error}\n", encoding="utf-8")
        self.prune(job["name"])
        return d, run

    def next_run(self, job):
        if job.get("cron"):
            w = Cron(job["cron"]).next_after(epoch_to_wall(time.time(), self.zone))
            return w.strftime("%Y-%m-%d %H:%M") + (f" {self.cfg['timezone']}" if self.cfg.get("timezone") else " local")
        if job.get("after_workflow"):
            aw = job["after_workflow"]
            return f"after the next green {aw['workflow']} on {aw['branch']}"
        return None

    # execution
    def execute(self, name, trigger):
        job = self.job(name)
        if job is None:
            raise ValueError(f"no job named {name}")
        bc = board_config(job["repo"], job["config"])
        busy = board_busy(job["repo"])
        if busy:
            d, run = self.record_simple(job, trigger, "skipped", busy)
            self.finish(job, run, None)
            return d, run
        if bc is None:
            d, run = self.record_simple(job, trigger, "failed", f"no board config .claude/super-board/configs/{job['config']}.json")
            self.finish(job, run, None)
            return d, run
        if "run" in job["steps"] and (bc.get("worker_backend") or "workflow") != "workflow":
            d, run = self.record_simple(job, trigger, "failed", "worker_backend is not \"workflow\": the scheduler "
                                        "runs boards through the Workflow tool only, never the legacy claude-p runner")
            self.finish(job, run, None)
            return d, run
        lock = RepoLock(self.state_dir, job["repo"])
        if not lock.acquire():
            d, run = self.record_simple(job, trigger, "skipped", "board busy (another scheduler run on this repo)")
            self.finish(job, run, None)
            return d, run
        try:
            return self._execute(job, trigger, bc)
        finally:
            lock.release()

    def _execute(self, job, trigger, bc):
        start = time.time()
        d = self.new_run_dir(job["name"], start)
        run = {"job": job["name"], "repo": str(job["repo"]), "config": job["config"], "trigger": trigger,
               "started": now_iso(), "ended": None, "duration_s": None, "exit": None, "status": "running",
               "steps": [], "error": None, "version": VERSION}
        write_json(d / "run.json", run)
        with self.lock:
            self.running[job["name"]] = d
        errors = []
        before = after = None
        try:
            before = status_snapshot(job["repo"], job["config"])
        except Exception as e:
            errors.append(f"snapshot before: {e}")
        deadline = start + job["timeout_s"]
        for step in job["steps"]:
            rec = run_step(job, step, d, deadline, self.stop, self.claude)
            run["steps"].append(rec)
            write_json(d / "run.json", run)
            if rec["status"] != "ok":
                break
        end = time.time()
        statuses = [s["status"] for s in run["steps"]]
        run["status"] = "timeout" if "timeout" in statuses else ("ok" if statuses and all(s == "ok" for s in statuses) else "failed")
        run["exit"] = next((s["exit"] for s in run["steps"] if s["status"] != "ok"), 0)
        run["ended"], run["duration_s"] = now_iso(), round(end - start, 1)
        try:
            after = status_snapshot(job["repo"], job["config"])
        except Exception as e:
            errors.append(f"snapshot after: {e}")
        prs = quota = None
        name = board_repo_name(bc)
        if name:
            try:
                prs = prs_between(self.gh, name, run["started"], run["ended"], job["repo"])
            except Exception as e:
                errors.append(str(e))
            try:
                quota = self.gh.quota()
            except Exception as e:
                errors.append(str(e))
        summary = summarize(before, after, prs, collect_receipts(job["repo"], start, end), quota, errors)
        write_json(d / "summary.json", summary)
        write_json(d / "run.json", run)
        with self.lock:
            self.running.pop(job["name"], None)
        self.finish(job, run, summary)
        self.prune(job["name"])
        return d, run

    def finish(self, job, run, summary):
        if run["status"] == "missed" or not job.get("runs_issue", True):
            return
        bc = board_config(job["repo"], job["config"])
        name = board_repo_name(bc)
        if not name:
            return
        mention = job.get("mention_on_failure") or self.cfg.get("mention_on_failure")
        try:
            post_runs_comment(self.gh, name, job["repo"], runs_comment(run, summary, self.next_run(job), mention))
        except Exception as e:
            print(f"runs issue: {redact(str(e))}", file=sys.stderr)

    # serve loop
    def worker(self):
        while not self.stop.is_set():
            try:
                name, trigger = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            threading.Thread(target=self._safe_execute, args=(name, trigger), daemon=True).start()

    def _safe_execute(self, name, trigger):
        try:
            self.execute(name, trigger)
        except Exception as e:
            print(f"job {name}: {redact(str(e))}", file=sys.stderr)

    def loop(self):
        while not self.stop.is_set():
            try:
                self.check_cron()
                self.check_workflows()
            except Exception as e:
                print(f"tick: {redact(str(e))}", file=sys.stderr)
            self.stop.wait(TICK_S)

    def runs(self, job=None, limit=50):
        out = []
        base = self.state_dir / "runs"
        for f in base.glob(f"{job or '*'}/*/run.json") if base.is_dir() else []:
            try:
                r = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            r["id"] = f"{f.parent.parent.name}/{f.parent.name}"
            out.append(r)
        out.sort(key=lambda r: (r.get("started") or "", r["id"]), reverse=True)
        return out[:limit]

    def run_path(self, run_id):
        m = re.fullmatch(r"([A-Za-z0-9._-]+)/([0-9TZ-]+)", run_id or "")
        if not m:
            return None
        p = self.state_dir / "runs" / m.group(1) / m.group(2)
        return p if (p / "run.json").is_file() else None


# ── web UI ──────────────────────────────────────────────────────────────────

LOOPBACK = {"127.0.0.1", "::1", "localhost"}

PAGE_CSS = """:root{--bg:#fbfaf8;--fg:#1d1d1b;--muted:#6b6a66;--line:#e5e2dc;--card:#fff;--ok:#2f7d4f;--bad:#b3412e;--warn:#9a6a00;--acc:#3d6fb6}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--muted:#a3a19b;--line:#34332f;--card:#1f1f1d;--ok:#6cc08b;--bad:#ef8a76;--warn:#e3b341;--acc:#7aa3e0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px}h1{font-size:20px;margin:0 0 12px}h2{font-size:16px;margin:24px 0 8px}
a{color:var(--acc)}.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
.chip{display:inline-block;padding:0 8px;border-radius:99px;font-size:12px;border:1px solid currentColor}
.ok{color:var(--ok)}.failed,.timeout{color:var(--bad)}.skipped,.missed,.running{color:var(--warn)}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;overflow-x:auto;font-size:12px;white-space:pre-wrap;word-break:break-word}
button{font:inherit;padding:2px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.muted{color:var(--muted)}"""


def page(title, body):
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
            f"<style>{PAGE_CSS}</style></head><body><main>{body}</main></body></html>").encode("utf-8")


def make_handler(sched, token):
    e = html.escape
    csrf = hmac.new(token.encode(), b"run-now", hashlib.sha256).hexdigest()  # never the token itself in a page

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "super-board-scheduler"

        def log_message(self, fmt, *args):  # no tokens in logs: paths only, query dropped
            sys.stderr.write("ui: %s %s\n" % (self.command, self.path.split("?", 1)[0]))

        def authed(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            cookie = dict(c.strip().split("=", 1) for c in (self.headers.get("Cookie") or "").split(";") if "=" in c)
            for cand in (q.get("token", [""])[0], cookie.get("sbtoken", "")):
                if cand and hmac.compare_digest(cand, token):
                    return "query" if cand == q.get("token", [""])[0] else "cookie"
            return None

        def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def deny(self):
            self.send(401, page("token required", "<h1>Token required</h1><p>Open the link "
                                "<code>super-board-scheduler.py ui-url</code> prints.</p>"))

        def do_GET(self):
            how = self.authed()
            if not how:
                return self.deny()
            path = urllib.parse.urlsplit(self.path).path
            if how == "query":  # move the token into a cookie and drop it from the URL
                return self.send(303, b"", extra={"Location": path,
                                                  "Set-Cookie": f"sbtoken={token}; HttpOnly; SameSite=Strict; Path=/"})
            if path == "/":
                return self.send(200, self.index())
            if path == "/api/runs":
                return self.send(200, json.dumps(sched.runs()).encode(), "application/json")
            m = re.fullmatch(r"/run/([A-Za-z0-9._-]+/[0-9TZ-]+)", path)
            if m and sched.run_path(m.group(1)):
                return self.send(200, self.detail(m.group(1)))
            self.send(404, page("not found", "<h1>Not found</h1>"))

        def do_POST(self):
            if not self.authed():
                return self.deny()
            host = self.headers.get("Host") or ""
            origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
            if urllib.parse.urlsplit(origin).netloc != host:
                return self.send(403, page("forbidden", "<h1>Cross-origin request refused</h1>"))
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(min(n, 4096)).decode("utf-8", "replace"))
            job = form.get("job", [""])[0]
            if urllib.parse.urlsplit(self.path).path != "/run-now" or not sched.job(job) \
                    or not hmac.compare_digest(form.get("csrf", [""])[0], csrf):
                return self.send(400, page("bad request", "<h1>Bad request</h1>"))
            sched.queue.put((job, "manual"))
            self.send(303, b"", extra={"Location": "/"})

        def index(self):
            rows = "".join(
                f"<tr><td><a href='/run/{e(r['id'])}'>{e((r.get('started') or '')[:16].replace('T', ' '))}</a></td>"
                f"<td>{e(r['job'])}</td><td>{e(r.get('trigger') or '')}</td>"
                f"<td><span class='chip {e(r.get('status') or '')}'>{e(r.get('status') or '')}</span></td>"
                f"<td>{'' if r.get('duration_s') is None else e(human_duration(r['duration_s']))}</td></tr>"
                for r in sched.runs())
            jobs = "".join(
                f"<tr><td>{e(j['name'])}</td><td>{e(', '.join(j['steps']))}</td><td>{e(sched.next_run(j) or 'manual only')}</td>"
                f"<td><form method='post' action='/run-now'><input type='hidden' name='job' value='{e(j['name'])}'>"
                f"<input type='hidden' name='csrf' value='{csrf}'><button>Run now</button></form></td></tr>"
                for j in sched.cfg["jobs"])
            return page("super-board scheduler", f"<h1>super-board scheduler</h1>"
                        f"<h2>Next runs</h2><div class='wrap'><table><tr><th>job</th><th>steps</th><th>next</th><th></th></tr>{jobs}</table></div>"
                        f"<h2>Runs</h2><div class='wrap'><table><tr><th>started (UTC)</th><th>job</th><th>trigger</th><th>status</th><th>duration</th></tr>"
                        f"{rows or '<tr><td colspan=5 class=muted>no runs yet</td></tr>'}</table></div>")

        def detail(self, run_id):
            p = sched.run_path(run_id)
            run = json.loads((p / "run.json").read_text(encoding="utf-8"))
            try:
                summary = json.loads((p / "summary.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                summary = {}
            try:
                log = (p / "log.txt").read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                log = []
            full = "full=1" in (urllib.parse.urlsplit(self.path).query or "")
            tail = log if full else log[-400:]
            steps = "".join(f"<tr><td>{e(s['step'])}</td><td><span class='chip {e(s['status'])}'>{e(s['status'])}</span></td>"
                            f"<td>{e(str(s.get('exit')))}</td><td>{e(s.get('error') or '')}</td></tr>" for s in run.get("steps") or [])
            return page(f"run {run_id}", f"<p><a href='/'>← runs</a></p><h1>{e(run['job'])} · "
                        f"<span class='chip {e(run['status'])}'>{e(run['status'])}</span></h1>"
                        f"<p class='muted'>{e(run.get('trigger') or '')} · started {e(run.get('started') or '')} · "
                        f"{e(human_duration(run.get('duration_s')))}{(' · ' + e(run['error'])) if run.get('error') else ''}</p>"
                        f"<h2>Steps</h2><div class='wrap'><table><tr><th>step</th><th>status</th><th>exit</th><th>error</th></tr>{steps}</table></div>"
                        f"<h2>Summary</h2><pre>{e(json.dumps(summary, indent=1, ensure_ascii=False))}</pre>"
                        f"<h2>Log{'' if full else ' (last 400 lines)'}</h2>"
                        f"{'' if full else f'<p><a href=?full=1>full log</a></p>'}<pre>{e(chr(10).join(tail))}</pre>")

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def check_bind(bind, allow_remote):
    if bind in LOOPBACK:
        return
    if not allow_remote:
        raise SystemExit(f"refusing to bind {bind}: the UI is loopback-only unless --allow-remote is given")


def load_token(state):
    p = state / "token"
    if p.is_file():
        t = p.read_text(encoding="utf-8").strip()
        if len(t) >= 32:
            return t
    t = secrets.token_urlsafe(32)
    write_private(p, t)
    return t


# ── service files ───────────────────────────────────────────────────────────

TASK = "super-board-scheduler"


def service_plan(osname, python, script, config, home):
    """(files {path: text}, install commands, uninstall commands, notes). Pure."""
    if osname == "windows":
        pyw = Path(python).with_name("pythonw.exe")  # no console window at logon
        exe = str(pyw) if pyw.exists() else str(python)
        tr = f'"{exe}" "{script}" serve --config "{config}"'
        create = ["schtasks", "/Create", "/TN", TASK, "/SC", "ONLOGON", "/RL", "LIMITED", "/IT", "/F", "/TR", tr]
        return ({}, [create, ["schtasks", "/Run", "/TN", TASK]], [["schtasks", "/End", "/TN", TASK],
                                                                   ["schtasks", "/Delete", "/TN", TASK, "/F"]],
                ["Runs at your logon as you (limited rights, no admin).",
                 "If schtasks says access denied, put a shortcut to the same command in shell:startup instead."])
    if osname == "linux":
        unit = Path(home) / ".config" / "systemd" / "user" / f"{TASK}.service"
        text = (f"[Unit]\nDescription=super-board scheduler\nAfter=network-online.target\n\n[Service]\n"
                f"ExecStart={python} {script} serve --config {config}\nRestart=on-failure\nRestartSec=30\n"
                f"KillMode=control-group\nTimeoutStopSec=60\n\n[Install]\nWantedBy=default.target\n")
        return ({str(unit): text}, [["systemctl", "--user", "daemon-reload"],
                                    ["systemctl", "--user", "enable", "--now", f"{TASK}.service"]],
                [["systemctl", "--user", "disable", "--now", f"{TASK}.service"]],
                ["On a server, keep it running after logout: loginctl enable-linger $USER"])
    plist = Path(home) / "Library" / "LaunchAgents" / "dev.super-board.scheduler.plist"
    args = "".join(f"<string>{html.escape(str(a))}</string>" for a in (python, script, "serve", "--config", config))
    text = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
            "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\"><dict>"
            "<key>Label</key><string>dev.super-board.scheduler</string>"
            f"<key>ProgramArguments</key><array>{args}</array>"
            "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>\n")
    return ({str(plist): text}, [["launchctl", "load", "-w", str(plist)]], [["launchctl", "unload", "-w", str(plist)]],
            ["Runs at login as you."])


def os_name():
    return "windows" if os.name == "nt" else ("macos" if sys.platform == "darwin" else "linux")


def cmd_service(a, install):
    if is_admin():
        print("refusing: run as your normal user, not root / Administrator", file=sys.stderr)
        return 77
    home = Path(a.out) if a.out else Path.home()
    files, inst, uninst, notes = service_plan(os_name(), sys.executable, str(Path(__file__).resolve()),
                                              str(Path(a.config or default_config_path()).resolve()), home)
    cmds = inst if install else uninst
    for path, text in files.items():
        if install:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text, encoding="utf-8")
            print(f"wrote {path}")
        elif Path(path).exists() and not a.dry_run:
            Path(path).unlink()
            print(f"removed {path}")
    for c in cmds:
        print("$ " + subprocess.list2cmdline(c))
        if not a.dry_run and not a.out:
            r = subprocess.run(c, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  failed: {(r.stderr or r.stdout).strip()[:300]}", file=sys.stderr)
    for n in notes:
        print(n)
    print("undo: super-board-scheduler.py " + ("uninstall-service" if install else "install-service"))
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def make(a):
    cfg = load_config(a.config or default_config_path())
    return Scheduler(cfg, state=os.environ.get("SB_SCHEDULER_STATE"))


def cmd_serve(a):
    if is_admin():
        print("refusing: the scheduler never runs as root / Administrator", file=sys.stderr)
        return 77
    s = make(a)
    if sys.stdout is None or sys.stderr is None:  # pythonw (the Windows logon task) has no console
        out = open(s.state_dir / "serve.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or out
        sys.stderr = sys.stderr or out
    bind = a.bind or s.cfg.get("bind") or "127.0.0.1"
    try:
        check_bind(bind, a.allow_remote)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 77
    lockf = s.state_dir / "serve.pid"
    if lockf.exists() and pid_alive(lockf.read_text().strip() or 0) and int(lockf.read_text().strip()) != os.getpid():
        print(f"another scheduler is serving this state dir (pid {lockf.read_text().strip()})", file=sys.stderr)
        return 69
    lockf.write_text(str(os.getpid()))
    token = load_token(s.state_dir)
    httpd = Server((bind, a.port or s.cfg["port"]), make_handler(s, token))

    def on_signal(signum, frame):
        s.stop.set()
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), on_signal)
    threads = [threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True),
               threading.Thread(target=s.worker, daemon=True), threading.Thread(target=s.loop, daemon=True)]
    for t in threads:
        t.start()
    print(f"super-board scheduler: {len(s.cfg['jobs'])} job(s), state {s.state_dir}")
    print(f"UI: http://{bind}:{httpd.server_address[1]}/  (link with token: super-board-scheduler.py ui-url)")
    sys.stdout.flush()
    try:
        while not s.stop.is_set():
            s.stop.wait(0.5)
    except KeyboardInterrupt:
        s.stop.set()
    print("stopping: ending running steps and the web UI")
    deadline = time.time() + 30
    while s.running and time.time() < deadline:  # run_step sees stop and kills its tree
        time.sleep(0.5)
    httpd.shutdown()
    httpd.server_close()
    try:
        lockf.unlink()
    except OSError:
        pass
    return 0


def cmd_status(a):
    s = make(a)
    print("next runs:")
    for j in s.cfg["jobs"]:
        note = s.jstate(j["name"]).get("poll_note")
        print(f"  {j['name']:<24} {s.next_run(j) or 'manual only'}" + (f"  ({note})" if note else ""))
    print("last 10 runs:")
    for r in s.runs(limit=10):
        print(f"  {r['id']:<40} {r.get('status'):<8} {r.get('trigger'):<24} {human_duration(r.get('duration_s'))}"
              + (f"  {r['error']}" if r.get("error") else ""))
    return 0


def cmd_show(a):
    s = make(a)
    p = s.run_path(a.run)
    if not p:
        print(f"no run {a.run} (use <job>/<stamp> from status)", file=sys.stderr)
        return 64
    print((p / "run.json").read_text(encoding="utf-8"))
    if (p / "summary.json").is_file():
        print((p / "summary.json").read_text(encoding="utf-8"))
    log = (p / "log.txt").read_text(encoding="utf-8", errors="replace").splitlines() if (p / "log.txt").is_file() else []
    print("\n".join(log[-60:]))
    return 0


def cmd_run_now(a):
    if is_admin():
        print("refusing: the scheduler never runs as root / Administrator", file=sys.stderr)
        return 77
    s = make(a)
    if not s.job(a.job):
        print(f"no job named {a.job}", file=sys.stderr)
        return 64
    stop_on = lambda *_: s.stop.set()  # noqa: E731
    for sig in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, sig), stop_on)
    d, run = s.execute(a.job, "manual")
    print(f"{run['status']}: {d}")
    return 0 if run["status"] in ("ok", "skipped") else 1


def cmd_ui_url(a):
    s = make(a)
    print(f"http://{s.cfg.get('bind') or '127.0.0.1'}:{s.cfg['port']}/?token={load_token(s.state_dir)}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--bind")
    sv.add_argument("--port", type=int)
    sv.add_argument("--allow-remote", action="store_true")
    sub.add_parser("status")
    sh = sub.add_parser("show")
    sh.add_argument("run")
    rn = sub.add_parser("run-now")
    rn.add_argument("job")
    sub.add_parser("ui-url")
    sub.add_parser("check-config")
    for name in ("install-service", "uninstall-service"):
        sp = sub.add_parser(name)
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--out", help="write service files under this folder instead of $HOME (no registration)")
    a = p.parse_args(argv)
    try:
        if a.cmd == "serve":
            return cmd_serve(a)
        if a.cmd == "status":
            return cmd_status(a)
        if a.cmd == "show":
            return cmd_show(a)
        if a.cmd == "run-now":
            return cmd_run_now(a)
        if a.cmd == "ui-url":
            return cmd_ui_url(a)
        if a.cmd == "check-config":
            cfg = load_config(a.config or default_config_path())
            print(f"ok: {len(cfg['jobs'])} job(s)")
            return 0
        return cmd_service(a, a.cmd == "install-service")
    except ConfigError as e:
        print(f"config: {e}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
