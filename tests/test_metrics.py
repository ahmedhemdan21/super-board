"""Tests for scripts/super-board-metrics.py — the scorecard. Offline: a fixture payload
shaped like the GraphQL search pages stands in for GitHub.

    python3 tests/test_metrics.py
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "super-board-metrics.py"
spec = importlib.util.spec_from_file_location("sbmetrics", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
SINCE = NOW - dt.timedelta(days=14)
CFG = {"project": {"owner": "acme", "number": 1}, "repo": {"remote": "https://github.com/acme/app"}}


def t(hours_ago):
    return (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(hours_ago, prev, new, project=1):
    return {"createdAt": t(hours_ago), "previousStatus": prev, "status": new, "project": {"number": project}}


def issue(number, events=(), comments=(), labels=(), state="CLOSED", reason="COMPLETED", closed=1, created=100,
          body="", board=True):
    return {"__typename": "Issue", "number": number, "title": f"card {number}", "state": state,
            "stateReason": reason if state == "CLOSED" else None, "createdAt": t(created),
            "closedAt": t(closed) if state == "CLOSED" else None, "body": body,
            "labels": {"nodes": [{"name": l} for l in labels]},
            "comments": {"totalCount": len(comments), "nodes": [{"createdAt": t(h), "body": b} for h, b in comments]},
            "timelineItems": {"totalCount": len(events), "nodes": list(events)},
            "projectItems": {"nodes": [{"project": {"number": 1}}] if board else []}}


def pr(number, title, merged=2, body=""):
    return {"__typename": "PullRequest", "number": number, "title": title, "state": "MERGED",
            "createdAt": t(merged + 5), "mergedAt": t(merged) if merged is not None else None, "body": body,
            "labels": {"nodes": []}, "comments": {"totalCount": 0, "nodes": []}}


def pages(*nodes):
    return [{"data": {"search": {"issueCount": len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None},
                                 "nodes": list(nodes)}}}]


# Card 10: clean pass Ready(10h) → Building(8h) → QA(6h) → Review(5h) → Done(2h).
CLEAN = issue(10, [ev(10, "", "Ready"), ev(8, "Ready", "Building"), ev(6, "Building", "QA"),
                   ev(5, "QA", "Review"), ev(2, "Review", "Done")],
              [(5.5, "[qa] [report] ✅ 3/3 AC · PR #20\nDid: x\ngh-quota-on-exit: graphql=4100/5000 rest=4900/5000"),
               (2.1, "[reviewer] [report] ✅ merged · round 1\ngh-quota-on-exit: graphql=3900/5000 rest=4800/5000")],
              closed=2)
# Card 11: QA bounces once, then merges via a human gate.
BOUNCED = issue(11, [ev(20, "", "Ready"), ev(18, "Ready", "Building"), ev(16, "Building", "QA"),
                     ev(15, "QA", "Building"), ev(12, "Building", "QA"), ev(11, "QA", "Review"),
                     ev(3, "Review", "Done")],
                [(15.1, "[qa] [report] ❌ failing · round 1"),
                 (9, "[reviewer] [blocker] 🙋 Your turn on #11 — merge it\nhuman-gate: money — prices.ts")],
                labels=("needs-you",), closed=3)
# Card 12: open, still in Review; filed by collect from sentry.
OPEN = issue(12, [ev(30, "", "Backlog"), ev(7, "Backlog", "Ready"), ev(4, "Ready", "Building")],
             state="OPEN", created=30, labels=("bug", "source:collect", "collect:sentry"))
# Not on this board: other project, must be ignored.
OTHER = issue(13, [ev(5, "", "Ready", project=2), ev(4, "Ready", "Done", project=2)], board=False)
FLAKY = issue(14, state="OPEN", created=10, labels=("flaky", "qa"))
REGRESSION = issue(15, state="OPEN", created=6, labels=("bug", "regression", "source:qa"))
MARKED = issue(16, state="OPEN", created=6, body="x\n<!-- super-collect-source: posthog -->", labels=("source:collect",))
REVERT = pr(30, "⏪ [revert] cart: undo the price change")
REVERT_BODY = pr(31, "fix things", body="Reverts acme/app#20")
NORMAL = pr(20, "✨ [feat] cart: price change")
OLD = pr(32, "Revert something old", merged=24 * 30)


class Compute(unittest.TestCase):
    def setUp(self):
        self.r = m.compute(pages(CLEAN, BOUNCED, OPEN, OTHER, FLAKY, REGRESSION, MARKED, REVERT, REVERT_BODY,
                                 NORMAL, OLD), CFG, SINCE, NOW)

    def test_cards_seen_are_board_cards_only(self):
        self.assertEqual(self.r["cards"], {"seen": 6, "merged": 2})

    def test_cycle_time_from_status_events(self):
        c = self.r["cycle_time_hours"]
        self.assertEqual(c["Ready"]["n"], 3)          # 10, 11 and 12 left Ready
        self.assertEqual(c["Ready"]["median"], 2.0)   # 2, 2, 3
        self.assertEqual(c["QA"]["n"], 3)             # clean 1h, bounced 1h + 1h
        self.assertEqual(c["Review"]["median"], 5.5)  # 3h and 8h
        self.assertEqual(c["total"]["n"], 2)
        self.assertEqual(c["total"]["median"], 12.5)  # 8h and 17h
        self.assertIsNone(c["Ready"]["reason"])

    def test_bounce_rate_from_status_events(self):
        b = self.r["bounce_rate"]
        self.assertEqual((b["bounces"], b["exits"], b["basis"]), (1, 3, "status_events"))
        self.assertAlmostEqual(b["value"], 0.333)

    def test_first_pass_and_human_share(self):
        f, h = self.r["first_pass_merge_rate"], self.r["human_merge_share"]
        self.assertEqual((f["first_pass"], f["merged"], f["value"]), (1, 2, 0.5))
        self.assertEqual((h["human"], h["issues"], h["value"]), (1, [11], 0.5))

    def test_reverts_by_title_and_body_in_window(self):
        self.assertEqual(self.r["reverts"], {"count": 2, "prs": [30, 31]})

    def test_labels_and_sources(self):
        self.assertEqual(self.r["flaky_opened"], {"count": 1, "issues": [14]})
        self.assertEqual(self.r["regression_bugs_opened"], {"count": 1, "issues": [15]})
        self.assertEqual(self.r["collect_filed"], {"posthog": 1, "sentry": 1})

    def test_quota_from_handoff_lines(self):
        q = self.r["github_quota"]
        self.assertEqual((q["samples"], q["graphql_min"], q["rest_min"]), (2, 3900, 4800))
        self.assertEqual(q["last"]["graphql"], 3900)


class MissingData(unittest.TestCase):
    def test_no_status_events_gives_null_cycle_times_with_reason(self):
        a = issue(40, comments=[(5, "[qa] [report] ❌ failing · round 1"), (3, "[qa] [report] ✅ pass · round 2")])
        r = m.compute(pages(a), CFG, SINCE, NOW)
        for lane in ("Ready", "Building", "QA", "Review", "total"):
            self.assertIsNone(r["cycle_time_hours"][lane]["median"])
            self.assertIn("Status-change events", r["cycle_time_hours"][lane]["reason"])
        b = r["bounce_rate"]
        self.assertEqual((b["value"], b["basis"]), (0.5, "lane_comments"), "falls back to qa comments")

    def test_nothing_at_all_is_null_never_zero(self):
        r = m.compute(pages(), CFG, SINCE, NOW)
        self.assertIsNone(r["bounce_rate"]["value"])
        self.assertIsNotNone(r["bounce_rate"]["reason"])
        self.assertIsNone(r["first_pass_merge_rate"]["value"])
        self.assertIn("closed as completed", r["first_pass_merge_rate"]["reason"])
        self.assertIsNone(r["human_merge_share"]["value"])
        self.assertIsNone(r["github_quota"]["graphql_min"])
        self.assertIn("gh-quota-on-exit", r["github_quota"]["reason"])

    def test_truncation_is_reported(self):
        a = issue(41)
        a["comments"]["totalCount"] = 150
        p = pages(a)
        p[0]["data"]["search"]["issueCount"] = 1500
        notes = m.compute(p, CFG, SINCE, NOW)["fetched"]["truncated"]
        self.assertTrue(any("1000" in n for n in notes) and any("first 100 comments" in n for n in notes), notes)


class Render(unittest.TestCase):
    def setUp(self):
        self.r = m.compute(pages(CLEAN, BOUNCED, REVERT), CFG, SINCE, NOW)
        self.r["repo"] = "acme/app"

    def test_text_table(self):
        out = m.text_table(self.r)
        self.assertIn("QA→Build bounce   33% (1/3)", out)
        self.assertIn("reverts           1 [30]", out)

    def test_html_is_self_contained_and_themed(self):
        self.r["recent_runs"] = [{"started": "2026-10-18T02:00:00Z", "job": "nightly", "trigger": "cron",
                                  "status": "ok", "duration_s": 3600}]
        page = m.render_html(self.r)
        self.assertIn("prefers-color-scheme:dark", page)
        self.assertIn('name="viewport"', page)
        self.assertIn("<svg", page)
        self.assertIn("Recent runs", page)
        self.assertNotRegex(page, r"<script|<link|src=\"http|url\(http")


class Cli(unittest.TestCase):
    def test_json_from_a_saved_payload(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "cfg.json"
            cfg.write_text(json.dumps(CFG), encoding="utf-8")
            payload = Path(d) / "pages.json"
            payload.write_text(json.dumps(pages(CLEAN)), encoding="utf-8")
            out = Path(d) / "card.html"
            r = subprocess.run([sys.executable, str(SCRIPT), "--config", str(cfg), "--payload", str(payload),
                                "--json", "--since", "30d", "--html", str(out), "--runs-dir", str(Path(d) / "none")],
                               capture_output=True, text=True, encoding="utf-8", cwd=d)
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads(r.stdout)
            self.assertEqual((data["schema"], data["repo"]), (1, "acme/app"))
            for key in ("cycle_time_hours", "bounce_rate", "first_pass_merge_rate", "human_merge_share", "reverts",
                        "flaky_opened", "regression_bugs_opened", "collect_filed", "github_quota"):
                self.assertIn(key, data)
            self.assertTrue(out.read_text(encoding="utf-8").startswith("<!doctype html>"))

    def test_recent_runs_filtered_by_repo(self):
        with tempfile.TemporaryDirectory() as d:
            runs = Path(d) / "runs"
            for job, repo in (("a", d), ("b", os.path.join(d, "elsewhere"))):
                rd = runs / job / "20261018T020000Z"
                rd.mkdir(parents=True)
                (rd / "run.json").write_text(json.dumps({"job": job, "repo": repo, "started": "2026-10-18T02:00:00Z"}),
                                             encoding="utf-8")
            got = m.recent_runs(runs, Path(d))
            self.assertEqual([r["job"] for r in got], ["a"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
