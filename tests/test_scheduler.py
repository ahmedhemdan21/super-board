"""Tests for scripts/super-board-scheduler.py. Offline: `claude` is tests/fixtures/stub_claude.py
(passed as an argv prefix, so it runs on Windows too), GitHub is an in-memory fake, and the
board snapshot is a stub super-board-status.py inside a temp repo.

    python3 tests/test_scheduler.py
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("sbsched", ROOT / "scripts" / "super-board-scheduler.py")
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
STUB = [sys.executable, str(ROOT / "tests" / "fixtures" / "stub_claude.py")]
STANDARD = (ROOT / "skills" / "super-board" / "references" / "writing-standard.md").read_text(encoding="utf-8")


def zone(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


NY = zone("America/New_York")


class FakeGH:
    def __init__(self, runs=None, quota=(4000, 4000)):
        self.runs, self.q, self.writes, self.reads = list(runs or []), quota, [], []
        self.issues = []

    def read(self, kind, args, repo_path=None):
        self.reads.append(args)
        if args[:2] == ["run", "list"]:
            return [self.runs.pop(0)] if self.runs else []
        if args[:2] == ["pr", "list"]:
            return [{"number": 1, "createdAt": "2000-01-01T00:00:00Z", "mergedAt": None},
                    {"number": 2, "createdAt": s.now_iso(), "mergedAt": s.now_iso()}]
        if args[:2] == ["issue", "list"]:
            return self.issues
        raise AssertionError(args)

    def write(self, args, repo_path):
        self.writes.append(args)
        if args[:2] == ["issue", "create"]:
            self.issues = [{"number": 77, "title": s.RUNS_TITLE}]
            return "https://github.com/acme/app/issues/77\n"
        return ""

    def quota(self):
        return {"graphql": self.q[0], "core": self.q[1]}


STATUS_STUB = r'''
import json, os, sys
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "n")
n = int(open(p).read()) if os.path.exists(p) else 0
open(p, "w").write(str(n + 1))
items = [{"number": 1, "title": "a", "labels": [], "status": "Ready" if n == 0 else "Building"},
         {"number": 2, "title": "b", "labels": ["bug"], "status": "QA"}]
if n:
    items.append({"number": 3, "title": "c", "labels": ["bug", "regression"], "status": "Backlog"})
print(json.dumps({"config": sys.argv[1], "items": items, "counts": {}}))
'''


def make_repo(d, backend="workflow"):
    repo = Path(d) / "repo"
    (repo / ".claude" / "super-board" / "configs").mkdir(parents=True)
    (repo / ".claude" / "bin").mkdir(parents=True)
    (repo / ".claude" / "super-board" / "configs" / "app.json").write_text(json.dumps(
        {"project": {"owner": "acme", "number": 1}, "repo": {"remote": "https://github.com/acme/app"},
         "worker_backend": backend}), encoding="utf-8")
    (repo / ".claude" / "bin" / "super-board-status.py").write_text(STATUS_STUB, encoding="utf-8")
    return repo


def job(repo, **kw):
    j = {"name": "nightly", "repo": str(repo), "config": "app", "steps": ["collect", "run"], "cron": "0 2 * * *",
         "timeout_s": 120, "poll_s": 120, "runs_issue": True}
    j.update(kw)
    return j


class CronParse(unittest.TestCase):
    def test_fields(self):
        c = s.Cron("*/15 9-17 * 1,6 1-5")
        self.assertEqual(sorted(c.sets[0]), [0, 15, 30, 45])
        self.assertEqual(sorted(c.sets[1]), list(range(9, 18)))
        self.assertEqual(sorted(c.sets[3]), [1, 6])
        self.assertEqual(sorted(s.Cron("0 0 * * 7").sets[4]), [0], "7 is Sunday")
        self.assertEqual(sorted(s.Cron("0 0 * * 5-7").sets[4]), [0, 5, 6])
        self.assertEqual(sorted(s.Cron("5/20 * * * *").sets[0]), [5, 25, 45])
        for bad in ("* * * *", "60 * * * *", "* 24 * * *", "a * * * *", "*/0 * * * *", "5-1 * * * *"):
            with self.assertRaises(s.ConfigError, msg=bad):
                s.Cron(bad)

    def test_next_fire(self):
        w = dt.datetime
        self.assertEqual(s.Cron("0 2 * * *").next_after(w(2026, 10, 18, 2, 0)), w(2026, 10, 19, 2, 0))
        self.assertEqual(s.Cron("*/15 9-17 * * 1-5").next_after(w(2026, 10, 16, 17, 50)), w(2026, 10, 19, 9, 0))
        # day-of-month and weekday both restricted: either matches
        self.assertEqual(s.Cron("0 0 1,15 * 0").next_after(w(2026, 10, 1, 0, 0)), w(2026, 10, 4, 0, 0))
        self.assertEqual(s.Cron("0 0 29 2 *").next_after(w(2026, 3, 1)), w(2028, 2, 29, 0, 0))

    @unittest.skipIf(NY is None, "tzdata not installed")
    def test_dst_spring_forward_fires_after_the_jump(self):
        c = s.Cron("30 2 * * *")
        slot = c.next_after(dt.datetime(2026, 3, 8, 0, 0))
        self.assertEqual(slot, dt.datetime(2026, 3, 8, 2, 30))  # a wall time that does not exist
        e = s.wall_to_epoch(slot, NY)
        self.assertEqual(dt.datetime.fromtimestamp(e, NY).strftime("%H:%M %Z"), "03:30 EDT")
        fire, missed, newest = s.due_slots(c, dt.datetime(2026, 3, 8, 0, 0), e + 30, NY)
        self.assertEqual((fire[0], missed), (slot, []))

    @unittest.skipIf(NY is None, "tzdata not installed")
    def test_dst_fall_back_fires_once(self):
        c = s.Cron("30 1 * * *")
        first = s.wall_to_epoch(dt.datetime(2026, 11, 1, 1, 30), NY)
        fire, _, newest = s.due_slots(c, dt.datetime(2026, 11, 1, 0, 0), first + 30, NY)
        self.assertIsNotNone(fire)
        again = first + 3600  # the repeated 01:30 (EST)
        fire2, missed2, _ = s.due_slots(c, newest, again + 30, NY)
        self.assertEqual((fire2, missed2), (None, []), "the repeated wall time must not fire again")


class Missed(unittest.TestCase):
    def test_missed_slots_are_not_run_late(self):
        c = s.Cron("0 2 * * *")
        last = dt.datetime(2026, 10, 15, 2, 0)
        now = s.wall_to_epoch(dt.datetime(2026, 10, 17, 8, 0))
        fire, missed, newest = s.due_slots(c, last, now)
        self.assertIsNone(fire)
        self.assertEqual([m[0].day for m in missed], [16, 17])
        self.assertEqual(newest, dt.datetime(2026, 10, 17, 2, 0))
        fire, missed, _ = s.due_slots(c, last, s.wall_to_epoch(dt.datetime(2026, 10, 16, 2, 3)))
        self.assertEqual((fire[0].day, missed), (16, []), "3 minutes late is within grace")

    def test_scheduler_records_one_missed_run_per_gap(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            gh = FakeGH()
            sc = s.Scheduler({"jobs": [job(repo)]}, state=Path(d) / "state", claude=STUB, github=gh)
            now = s.wall_to_epoch(dt.datetime(2026, 10, 17, 8, 0))
            self.assertEqual(sc.check_cron(now - 86400 * 3), [], "first start is a baseline")
            self.assertEqual(sc.check_cron(now), [])
            runs = sc.runs()
            self.assertEqual([r["status"] for r in runs], ["missed"])
            self.assertIn("3 slot(s) missed", runs[0]["error"])
            self.assertEqual(gh.writes, [], "missed runs are not posted")
            self.assertTrue(sc.queue.empty())
            fired = sc.check_cron(s.wall_to_epoch(dt.datetime(2026, 10, 18, 2, 1)))
            self.assertEqual(fired, ["nightly"])


class AfterWorkflow(unittest.TestCase):
    def test_fires_once_per_new_run_id(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            aw = {"repo": "acme/app", "workflow": "deploy-dev.yml", "branch": "dev"}
            gh = FakeGH(runs=[{"databaseId": 10}, {"databaseId": 10}, {"databaseId": 11}, {"databaseId": 11}])
            sc = s.Scheduler({"jobs": [job(repo, cron=None, after_workflow=aw)]}, state=Path(d) / "st", claude=STUB, github=gh)
            t0 = 1_000_000
            self.assertEqual(sc.check_workflows(t0), [], "first sighting is the baseline")
            self.assertEqual(sc.check_workflows(t0 + 30), [], "poll_s not elapsed: no call")
            self.assertEqual(sc.check_workflows(t0 + 130), [], "same run id")
            self.assertEqual(sc.check_workflows(t0 + 260), ["nightly"], "new run id fires")
            self.assertEqual(sc.check_workflows(t0 + 390), [], "and only once")
            self.assertEqual(sc.queue.get_nowait(), ("nightly", "after_workflow:11"))
            listed = [a for a in gh.reads if a[:2] == ["run", "list"]]
            self.assertEqual(len(listed), 4)
            self.assertIn("--status", listed[0])
            sc2 = s.Scheduler({"jobs": [job(repo, cron=None, after_workflow=aw)]}, state=Path(d) / "st", claude=STUB,
                              github=FakeGH(runs=[{"databaseId": 11}]))
            self.assertEqual(sc2.check_workflows(t0 + 520), [], "state persisted across restarts")

    def test_low_quota_skips_polling(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(d)
            aw = {"repo": "acme/app", "workflow": "deploy-dev.yml", "branch": "dev"}
            gh = FakeGH(runs=[{"databaseId": 1}], quota=(499, 4000))
            sc = s.Scheduler({"jobs": [job(repo, cron=None, after_workflow=aw)]}, state=Path(d) / "st", claude=STUB, github=gh)
            sc.check_workflows(1_000_000)
            self.assertFalse(any(a[:2] == ["run", "list"] for a in gh.reads))
            self.assertIn("quota", sc.jstate("nightly")["poll_note"])


class Execute(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.repo = make_repo(self.d)
        self.gh = FakeGH()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def sched(self, **kw):
        return s.Scheduler({"jobs": [job(self.repo, **kw)]}, state=Path(self.d) / "state", claude=STUB, github=self.gh)

    def test_busy_board_is_skipped(self):
        lock = self.repo / ".claude" / "super-board" / "inflight"
        lock.mkdir(parents=True)
        (lock / "workflow-wave.lock").write_text("SLUG=app\n")
        d, run = self.sched().execute("nightly", "cron")
        self.assertEqual(run["status"], "skipped")
        self.assertIn("board busy", run["error"])
        self.assertFalse((d / "stream.jsonl").exists(), "no step ran")
        body = self.gh.writes[-1][-1]
        self.assertTrue(body.startswith("[scheduler] [report] ⚠️ skipped · nightly"), body)

    def test_halted_repo_and_legacy_backend_are_refused(self):
        (self.repo / ".claude" / "super-board" / "github-halt.json").write_text("{}")
        _, run = self.sched().execute("nightly", "manual")
        self.assertIn("github halted", run["error"])
        os.remove(self.repo / ".claude" / "super-board" / "github-halt.json")
        cfgp = self.repo / ".claude" / "super-board" / "configs" / "app.json"
        cfg = json.loads(cfgp.read_text())
        cfg["worker_backend"] = "claude-p"
        cfgp.write_text(json.dumps(cfg))
        _, run = self.sched().execute("nightly", "manual")
        self.assertEqual(run["status"], "failed")
        self.assertIn("never the legacy claude-p runner", run["error"])

    def test_run_record_and_summary_from_snapshots(self):
        # The stub's collect step writes the receipt mid-run (a pre-stamped `now + 1` raced
        # the run's [start, end] window on slow CI); the old prs receipt is outside it.
        os.environ["STUB_CLAUDE_RECEIPT"] = str(self.repo / ".claude" / "super-board" / "collect" / "x.json")
        try:
            d, run = self.sched(collect_sources=["sentry", "posthog"]).execute("nightly", "manual")
        finally:
            os.environ.pop("STUB_CLAUDE_RECEIPT")
        self.assertEqual(run["status"], "ok", run)
        self.assertEqual([x["step"] for x in run["steps"]], ["collect", "run"])
        self.assertEqual(run["steps"][0]["prompt"], "/super-collect sentry posthog --yes")
        self.assertEqual(run["steps"][1]["prompt"], "/super-board run app")
        for k in ("job", "trigger", "started", "ended", "duration_s", "exit", "status", "steps"):
            self.assertIn(k, run)
        log = (d / "log.txt").read_text(encoding="utf-8")
        self.assertIn("--allowedTools Workflow", log, "the run step allows the Workflow tool")
        self.assertIn("[init] model=stub", log)
        self.assertNotIn("ghp_" + "a" * 36, log, "secrets redacted")
        self.assertNotIn("ghp_" + "a" * 36, (d / "stream.jsonl").read_text(encoding="utf-8"))
        sm = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(sm["moved"], [{"number": 1, "from": "Ready", "to": "Building"}])
        self.assertEqual(sm["moved_per_lane"], {"Ready→Building": 1})
        self.assertEqual((sm["regression_opened"], sm["flaky_opened"]), (1, 0))
        self.assertEqual((sm["prs_opened"], sm["prs_merged"]), (1, 1))
        self.assertEqual(sm["bugs_filed_per_source"], {"err": 1})
        self.assertEqual(sm["quota"], {"graphql": 4000, "core": 4000})
        create = next(w for w in self.gh.writes if w[:2] == ["issue", "create"])
        self.assertIn(s.RUNS_TITLE, create)
        self.assertIn(s.RUNS_LABEL, create)
        comment = self.gh.writes[-1]
        self.assertEqual(comment[:3], ["issue", "comment", "77"])
        self.assertIn("✅ Done: moved 1 card · 1 PRs opened, 1 merged · 1 filed (err 1)", comment[-1])
        # second run reuses the issue
        self.gh.writes.clear()
        self.sched().execute("nightly", "manual")
        self.assertFalse(any(w[:2] == ["issue", "create"] for w in self.gh.writes))

    def test_timeout_kills_the_process_tree(self):
        pids = Path(self.d) / "pids"
        os.environ.update(STUB_CLAUDE_MODE="sleep", STUB_CLAUDE_PIDS=str(pids))
        try:
            sc = self.sched(steps=["collect"])
            sc.cfg["jobs"][0]["timeout_s"] = 3
            t0 = time.time()
            d, run = sc.execute("nightly", "manual")
        finally:
            os.environ.pop("STUB_CLAUDE_MODE"); os.environ.pop("STUB_CLAUDE_PIDS")
        self.assertEqual(run["status"], "timeout")
        self.assertLess(time.time() - t0, 60)
        time.sleep(1)
        for pid in pids.read_text().split():
            self.assertFalse(s.pid_alive(int(pid)), f"pid {pid} still running")

    def test_run_step_without_workflow_tool_refuses(self):
        os.environ["STUB_CLAUDE_MODE"] = "noworkflow"
        try:
            _, run = self.sched(steps=["run"]).execute("nightly", "manual")
        finally:
            os.environ.pop("STUB_CLAUDE_MODE")
        self.assertEqual(run["status"], "failed")
        self.assertIn("Workflow tool is not available", run["steps"][0]["error"])
        self.assertIn("never falls back", run["steps"][0]["error"])

    def test_failed_step_stops_the_run(self):
        os.environ["STUB_CLAUDE_MODE"] = "fail"
        try:
            _, run = self.sched().execute("nightly", "manual")
        finally:
            os.environ.pop("STUB_CLAUDE_MODE")
        self.assertEqual((run["status"], run["exit"], len(run["steps"])), ("failed", 3, 1))

    def test_retention_keeps_200(self):
        sc = self.sched()
        base = sc.state_dir / "runs" / "nightly"
        for i in range(205):
            (base / f"2026{i:05d}T000000Z").mkdir(parents=True)
        sc.prune("nightly")
        left = sorted(p.name for p in base.iterdir())
        self.assertEqual(len(left), 200)
        self.assertEqual(left[0], "202600005T000000Z")


class Comment(unittest.TestCase):
    HEADER = re.compile(r"^\[(builder|qa|reviewer|collect|orchestrator|scheduler)\] "
                        r"\[(blocker|issue|suggestion|nit|question|praise|report)\] \S")

    def test_format_matches_the_writing_standard(self):
        self.assertIn("`scheduler`", STANDARD.split("### Lane status and findings", 1)[1])
        run = {"job": "nightly", "trigger": "cron", "status": "failed", "duration_s": 3725, "error": None,
               "steps": [{"step": "run", "error": "claude exited 1"}]}
        sm = s.summarize({"items": [{"number": 1, "status": "Ready", "labels": []}], "counts": {}},
                         {"items": [{"number": 1, "status": "QA", "labels": []}], "counts": {}},
                         (2, 1), {"sentry": 2}, {"graphql": 4100, "core": 4900}, [])
        body = s.runs_comment(run, sm, "2026-10-19 02:00 local", mention="octocat")
        lines = body.splitlines()
        self.assertTrue(self.HEADER.match(lines[0]), lines[0])
        self.assertEqual(lines[0], "[scheduler] [report] ❌ failed · nightly")
        self.assertEqual(lines[1], "Did: job nightly · trigger cron · 1h 02m")
        self.assertTrue(lines[2].startswith("✅ Done: moved 1 card · 2 PRs opened, 1 merged · 2 filed (sentry 2)"))
        self.assertEqual(lines[3], "❌ Not done: claude exited 1")
        self.assertEqual(lines[4], "Next: @octocat · next run 2026-10-19 02:00 local")
        self.assertEqual(lines[-1], "gh-quota-on-exit: graphql=4100/5000 rest=4900/5000")
        prose = [l for l in lines if not l.startswith("gh-quota-on-exit:")]
        self.assertLessEqual(len(prose), 8)
        ok = s.runs_comment(dict(run, status="ok", steps=[]), sm, None, mention="octocat")
        self.assertNotIn("@octocat", ok, "mention only on failure")
        self.assertNotIn("Not done", ok)


class Web(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        repo = make_repo(self.d)
        self.sc = s.Scheduler({"jobs": [job(repo)], "port": 8765}, state=Path(self.d) / "st", claude=STUB, github=FakeGH())
        self.token = s.load_token(self.sc.state_dir)
        self.httpd = s.Server(("127.0.0.1", 0), s.make_handler(self.sc, self.token))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.d, ignore_errors=True)

    def get(self, path, headers=None, data=None):
        req = urllib.request.Request(self.base + path, headers=headers or {}, data=data)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def test_token_required(self):
        self.assertEqual(self.get("/")[0], 401)
        self.assertEqual(self.get("/?token=wrong")[0], 401)
        self.assertEqual(self.get("/api/runs")[0], 401)
        code, _, h = self.get(f"/?token={self.token}")
        self.assertEqual(code, 303, "the token moves into a cookie")
        self.assertIn("HttpOnly", h["Set-Cookie"])
        code, body, _ = self.get("/", {"Cookie": f"sbtoken={self.token}"})
        self.assertEqual(code, 200)
        self.assertIn("Run now", body)
        self.assertNotIn(self.token, body, "the page never embeds the token")
        if os.name != "nt":
            self.assertEqual(os.stat(self.sc.state_dir / "token").st_mode & 0o777, 0o600)

    def test_run_now_needs_same_origin_and_csrf(self):
        cookie = {"Cookie": f"sbtoken={self.token}"}
        _, body, _ = self.get("/", cookie)
        csrf = re.search(r"name='csrf' value='([0-9a-f]+)'", body).group(1)
        data = f"job=nightly&csrf={csrf}".encode()
        self.assertEqual(self.get("/run-now", dict(cookie, Origin="http://evil.example"), data)[0], 403)
        self.assertEqual(self.get("/run-now", {"Origin": self.base}, data)[0], 401, "no token, no run")
        self.assertEqual(self.get("/run-now", dict(cookie, Origin=self.base), b"job=nightly&csrf=x")[0], 400)
        self.assertTrue(self.sc.queue.empty())
        self.assertEqual(self.get("/run-now", dict(cookie, Origin=self.base), data)[0], 303)
        self.assertEqual(self.sc.queue.get_nowait(), ("nightly", "manual"))

    def test_loopback_only(self):
        s.check_bind("127.0.0.1", False)
        with self.assertRaises(SystemExit):
            s.check_bind("0.0.0.0", False)
        s.check_bind("0.0.0.0", True)


class Shutdown(unittest.TestCase):
    def test_serve_stops_cleanly_on_signal(self):
        import signal
        import socket
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            cfgp = Path(d) / "c.json"
            cfgp.write_text(json.dumps({"port": port, "jobs": []}))
            env = dict(os.environ, SB_SCHEDULER_STATE=str(Path(d) / "st"), PYTHONUNBUFFERED="1")
            kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
            p = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "super-board-scheduler.py"), "--config",
                                  str(cfgp), "serve"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                 text=True, **kw)
            try:
                first = p.stdout.readline()
                self.assertIn("super-board scheduler", first)
                self.assertTrue((Path(d) / "st" / "serve.pid").exists())
                p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
                out, _ = p.communicate(timeout=60)
            finally:
                if p.poll() is None:
                    p.kill()
            self.assertEqual(p.returncode, 0, out)
            self.assertIn("stopping", out)
            self.assertFalse((Path(d) / "st" / "serve.pid").exists(), "the pid file is removed")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Service(unittest.TestCase):
    def test_windows_task(self):
        files, inst, uninst, notes = s.service_plan("windows", r"C:\Py\python.exe", r"C:\x\sched.py", r"C:\c.json", "H")
        create = inst[0]
        self.assertEqual(create[:2], ["schtasks", "/Create"])
        for flag, val in (("/SC", "ONLOGON"), ("/RL", "LIMITED"), ("/TN", s.TASK)):
            self.assertEqual(create[create.index(flag) + 1], val)
        self.assertNotIn("/RU", create, "runs as the current user, never SYSTEM")
        self.assertIn("serve --config", create[create.index("/TR") + 1])
        self.assertEqual(uninst[-1][:2], ["schtasks", "/Delete"])

    def test_linux_and_macos_files(self):
        with tempfile.TemporaryDirectory() as home:
            files, inst, _, notes = s.service_plan("linux", "/usr/bin/python3", "/opt/s.py", "/etc/c.json", home)
            (path, text), = files.items()
            self.assertTrue(path.endswith(os.path.join(".config", "systemd", "user", "super-board-scheduler.service")))
            self.assertIn("ExecStart=/usr/bin/python3 /opt/s.py serve --config /etc/c.json", text)
            self.assertNotIn("User=root", text)
            self.assertIn("--user", inst[0])
            self.assertIn("enable-linger", notes[0])
            files, inst, _, _ = s.service_plan("macos", "/usr/bin/python3", "/opt/s.py", "/c.json", home)
            (path, text), = files.items()
            self.assertTrue(path.endswith("dev.super-board.scheduler.plist"))
            self.assertIn("<string>serve</string>", text)
            self.assertEqual(inst[0][:2], ["launchctl", "load"])

    def test_install_service_out_dir_registers_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "c.json"
            cfgp.write_text(json.dumps({"jobs": []}))
            import argparse
            import contextlib
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = s.cmd_service(argparse.Namespace(out=d, dry_run=False, config=str(cfgp)), True)
            self.assertEqual(rc, 0)
            self.assertIn("undo: super-board-scheduler.py uninstall-service", out.getvalue())
            self.assertIn("$ ", out.getvalue())


class Config(unittest.TestCase):
    def test_validation(self):
        with tempfile.TemporaryDirectory() as d:
            good = job(d)
            s.validate_config({"jobs": [good]})
            for bad in (dict(good, command="rm -rf /"), dict(good, steps=["deploy"]), dict(good, steps=[]),
                        dict(good, permission_mode="bypassPermissions"), dict(good, repo=os.path.join(d, "nope")),
                        dict(good, cron="* * *"), dict(good, collect_sources=["sentry; rm"]),
                        dict(good, after_workflow={"repo": "acme", "workflow": "x.yml", "branch": "dev"})):
                with self.assertRaises(s.ConfigError):
                    s.validate_config({"jobs": [bad]})
            with self.assertRaises(s.ConfigError):
                s.validate_config({"jobs": [good, good]})
            with self.assertRaises(s.ConfigError):
                s.validate_config({"jobs": [], "shell": "x"})

    def test_argv_is_a_list_with_the_workflow_flag_on_run_only(self):
        j = job("/r", collect_sources=["sentry"], permission_mode="acceptEdits")
        run = s.claude_argv(j, "run", STUB)
        col = s.claude_argv(j, "collect", STUB)
        self.assertEqual(run[run.index("-p") + 1], "/super-board run app")
        self.assertIn("Workflow", run)
        self.assertNotIn("--allowedTools", col)
        self.assertEqual(col[col.index("--permission-mode") + 1], "acceptEdits")


class Redaction(unittest.TestCase):
    def test_patterns_agree_with_the_guard_hook(self):
        gspec = importlib.util.spec_from_file_location("gkl", ROOT / "hooks" / "guard-key-literals.py")
        g = importlib.util.module_from_spec(gspec)
        gspec.loader.exec_module(g)
        for part in s.KEY_PATTERNS:
            self.assertIn(part, g.KEY_RE.pattern)
        for sample in ("ghp_" + "b" * 36, "sk-ant-" + "c" * 30, "AKIA" + "D" * 16, "-----BEGIN RSA PRIVATE KEY-----",
                       "Authorization: Bearer abcdefghijklmnop1234", "SENTRY_AUTH_TOKEN=sntrys_abcdefgh12345678"):
            out = s.redact(f"x {sample} y")
            self.assertIn("[redacted]", out, sample)
            self.assertNotIn(sample.split()[-1].split("=")[-1], out, sample)


if __name__ == "__main__":
    unittest.main(verbosity=1)
