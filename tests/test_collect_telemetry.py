"""Unit tests for super-collect's self-hosted telemetry sources: collect_loki.py,
collect_prometheus.py and the shared transport in collect_common.py.

Offline: urlopen answers from tests/fixtures/collect (fixture_http.Urlopen) or a local
router; the ssh transport is checked by argv and, on POSIX, through a stub `ssh` on PATH.

    python3 tests/test_collect_telemetry.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "collect")
sys.path.insert(0, os.path.join(HERE, "..", "skills", "super-collect", "scripts"))
sys.path.insert(0, FIX)
import collect_common as cc  # noqa: E402
import collect_loki as lk  # noqa: E402
import collect_prometheus as pm  # noqa: E402
import fixture_http  # noqa: E402

SEL = '{deployment_environment="dev"}'
LOKI_CFG = {"collect": {"loki": {"url": "http://primecore-loki:3100", "selector": SEL}}}
PROM_CFG = {"collect": {"prometheus": {"url": "http://primecore-prometheus:9090"}}}
SSH = {"kind": "ssh", "host": "primecore-dev", "docker_network": "primecore-network"}


def queries(fx):
    out = []
    for r in fx.calls:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query)
        out.append(q.get("query", [""])[0])
    return out


class Transport(unittest.TestCase):
    def test_url_params_are_encoded_and_checked(self):
        url = cc.build_url("http://loki:3100", "/q", {"query": SEL + ' |= "a b"'})
        self.assertNotIn('"', url)
        self.assertNotIn(" ", url)
        with self.assertRaises(cc.FetchError):
            cc.build_url("http://loki:3100/$(id)", "/q")

    def test_ssh_argv_quotes_url_and_validates_names(self):
        argv = cc.ssh_argv(SSH, "http://primecore-loki:3100/x?a=%7B")
        self.assertEqual(argv[:4], ["ssh", "-o", "BatchMode=yes", "primecore-dev"])
        self.assertIn("primecore-network", argv)
        # The URL is sent on stdin (curl -K -), never on the remote command line: MSYS ssh
        # started from Windows Python strips quotes and the remote shell cuts it at `&`.
        self.assertEqual(argv[-2:], ["-K", "-"])
        self.assertIn("-i", argv)
        self.assertFalse(any("primecore-loki" in a for a in argv))
        cfg = cc.curl_config("http://primecore-loki:3100/x?a=%7B&start=1&end=2")
        self.assertEqual(cfg, 'url = "http://primecore-loki:3100/x?a=%7B&start=1&end=2"\n')
        for bad in ({"kind": "ssh", "host": "h;rm -rf /", "docker_network": "n"},
                    {"kind": "ssh", "host": "h", "docker_network": "$(x)"}, {"kind": "ssh"}):
            with self.assertRaises(cc.FetchError):
                cc.ssh_argv(bad, "http://x/")

    def test_ssh_transport_runs_remote_curl(self):
        seen = {}

        def run(argv, **kw):
            seen["argv"], seen["input"] = argv, kw.get("input")
            return mock.Mock(returncode=0, stdout='{"status":"success","data":{"alerts":[]}}', stderr="")
        with mock.patch("subprocess.run", run):
            out = cc.get_json("http://p:9090", "/api/v1/alerts", None, SSH)
        self.assertEqual(out["data"], {"alerts": []})
        self.assertIn("curlimages/curl", seen["argv"])
        self.assertEqual(seen["input"], 'url = "http://p:9090/api/v1/alerts"\n')

    def test_ssh_failure_is_fetch_error(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=255, stdout="", stderr="ssh: no route")):
            with self.assertRaisesRegex(cc.FetchError, "no route"):
                cc.get_json("http://p:9090", "/x", None, SSH)

    @unittest.skipIf(os.name == "nt", "Windows cannot exec a script stub named ssh")
    def test_stub_ssh_on_path(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "log")
            stub = os.path.join(d, "ssh")
            with open(stub, "w") as f:
                f.write(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{log}'\ncat > '{log}.stdin'\n"
                        "echo '{\"status\":\"success\",\"data\":{}}'\n")
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
            with mock.patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ["PATH"]}):
                cc.get_json("http://p:9090", "/api/v1/alerts", {"a": "1", "b": "2"}, SSH)
            with open(log) as f:
                args = f.read().split("\n")
            with open(log + ".stdin") as f:
                stdin = f.read()
        self.assertIn("docker", args)
        self.assertFalse(any("p:9090" in a for a in args))
        self.assertEqual(stdin, 'url = "http://p:9090/api/v1/alerts?a=1&b=2"\n')

    def test_signature_collapses_noise(self):
        a = cc.signature('{"message":"Timeout 10.0.0.12 after 3000ms req 8f14e45f-ceea-467f-a9b3-1c2d3e4f5a6b"}')
        b = cc.signature('{"message":"Timeout 10.0.0.13 after 2999ms req 1a2b3c4d-1111-2222-3333-444455556666"}')
        self.assertEqual(a, b)
        self.assertEqual(cc.signature("x at 2026-10-17T10:00:01Z"), cc.signature("x at 2026-10-18T11:12:13Z"))


class Loki(unittest.TestCase):
    def test_selector_is_required(self):
        for blk in ({"url": "http://l:3100"}, {"url": "http://l:3100", "selector": "dev"}, {"selector": SEL}):
            with self.assertRaises(lk.LokiError):
                lk.settings({"collect": {"loki": blk}})

    def test_list_groups_signatures_and_fingerprints(self):
        fx = fixture_http.Urlopen(FIX)
        with mock.patch("urllib.request.urlopen", fx):
            rows = lk.list_groups(lk.settings(LOKI_CFG), cc.since("1d"))
        self.assertEqual(len(rows), 2)  # two signatures in one service; the zero-count service dropped
        self.assertEqual(rows[0]["sampleCount"], 2)
        self.assertEqual(rows[0]["groupCount"], 7)
        self.assertRegex(rows[0]["fingerprint"], r"^loki\|primecore-gateway\|[0-9a-f]{12}$")
        qs = queries(fx)
        self.assertTrue(all(SEL in q for q in qs), qs)
        self.assertIn('sum by (service_name) (count_over_time(' + SEL + ' | severity_text=~', qs[0])
        self.assertIn('| service_name="primecore-gateway"', qs[1])

    def test_check_flags_new_groups_after_deploy(self):
        deploy = 1_760_700_000
        base = fixture_http.Urlopen(FIX)

        def fx(req, timeout=None):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
            before = int(q.get("time", q.get("end", ["0"]))[0][:10]) <= deploy
            if before and "/query_range" not in req.full_url:
                return fixture_http._Resp(b'{"status":"success","data":{"result":[]}}')
            return base(req)
        with mock.patch("urllib.request.urlopen", fx):
            out = lk.check(lk.settings(LOKI_CFG), deploy, 600, 0, now=deploy + 600)
        self.assertTrue(out["regression"])
        self.assertEqual((out["before"], out["new"]), (0, 2))
        self.assertIn("primecore-gateway", out["summary"])

    def test_check_same_groups_is_no_regression(self):
        with mock.patch("urllib.request.urlopen", fixture_http.Urlopen(FIX)):
            out = lk.check(lk.settings(LOKI_CFG), 1_760_700_000, 600, 0, now=1_760_700_600)
        self.assertFalse(out["regression"])
        self.assertEqual(out["new"], 0)

    def test_check_no_logs_is_no_data_not_ok(self):
        empty = fixture_http._Resp(b'{"status":"success","data":{"result":[]}}')
        with mock.patch("urllib.request.urlopen", lambda req, timeout=None: empty):
            out = lk.check(lk.settings(LOKI_CFG), 1_760_700_000, 600, 0, now=1_760_700_600)
        self.assertEqual(out["status"], "no_data")
        self.assertFalse(out["regression"])
        self.assertIn(SEL, out["selector"])

    def test_check_logs_without_errors_is_ok(self):
        def fx(req, timeout=None):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query).get("query", [""])[0]
            if "severity_text" in q:
                return fixture_http._Resp(b'{"status":"success","data":{"result":[]}}')
            return fixture_http._Resp(b'{"status":"success","data":{"result":[{"metric":{},"value":[1,"42"]}]}}')
        with mock.patch("urllib.request.urlopen", fx):
            out = lk.check(lk.settings(LOKI_CFG), 1_760_700_000, 600, 0, now=1_760_700_600)
        self.assertEqual((out["status"], out["lines"], out["after"], out["regression"]), ("ok", 42, 0, False))

    def test_help_prints_under_cp1252(self):
        for script in ("collect_loki.py", "collect_prometheus.py"):
            path = os.path.join(HERE, "..", "skills", "super-collect", "scripts", script)
            env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
            r = subprocess.run([sys.executable, path, "--help"], capture_output=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))

    def test_release_narrowing_validated(self):
        with self.assertRaises(lk.LokiError):
            lk.check(lk.settings(LOKI_CFG), 1, release="x;y", release_label="service_version")

    def test_unreachable_is_unavailable(self):
        cfg = {"collect": {"loki": {"url": "http://127.0.0.1:9", "selector": SEL}}}
        with mock.patch.object(lk.cc, "active_config", return_value=(cfg, None)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = lk.main(["ping"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "unavailable")


class SecretFile(unittest.TestCase):
    """Collector keys never go in a git-tracked .env: a branch switch rewrites it."""

    def repo(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        subprocess.run(["git", "init", "-q", d], check=True)
        return d

    def write(self, d, name, text):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_untracked_env_is_used(self):
        d = self.repo()
        self.write(d, ".env", "A=1\n")
        self.assertEqual(cc.secret_file(d), os.path.join(d, ".env"))

    def test_tracked_env_means_env_local(self):
        d = self.repo()
        self.write(d, ".env", "VITE_X=1\n")
        subprocess.run(["git", "-C", d, "add", ".env"], check=True)
        self.assertEqual(cc.secret_file(d), os.path.join(d, ".env.local"))

    def test_only_env_local_present(self):
        d = self.repo()
        self.write(d, ".env.local", "A=1\n")
        self.assertEqual(cc.secret_file(d), os.path.join(d, ".env.local"))

    def test_env_local_wins_on_conflict(self):
        d = self.repo()
        self.write(d, ".env", "SENTRY_AUTH_TOKEN=from_env\n")
        self.write(d, ".env.local", "SENTRY_AUTH_TOKEN=from_local\n")
        old = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, old)
        with mock.patch.dict(os.environ, {"SENTRY_AUTH_TOKEN": ""}):
            self.assertEqual(cc.secret("SENTRY_AUTH_TOKEN"), "from_local")
            self.write(d, ".env.local", "SENTRY_AUTH_TOKEN=\n")
            self.assertEqual(cc.secret("SENTRY_AUTH_TOKEN"), "from_env")  # empty never shadows

    def test_setup_copy_of_key_names_agrees(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sb_setup", os.path.join(HERE, "..", "scripts", "super-board-setup.py"))
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        self.assertEqual(setup.SECRET_KEYS, cc.SECRET_KEYS)
        self.assertEqual(setup.SECRET_PATTERN.pattern, cc.SECRET_PATTERN.pattern)


class LokiStructuredMetadata(unittest.TestCase):
    """Serilog -> OTLP -> Loki: the line is often empty and the error lives in structured
    metadata, merged into `stream` or sent as a third value element (categorize-labels)."""
    METRIC = {"status": "success", "data": {"result": [
        {"metric": {"service_name": "api"}, "value": [0, "2"]}]}}
    EXC = "Order 1234 not found for tenant 8f14e45f-ceea-467f-a9b3-1c2d3e4f5a6b"
    STACK = "\n".join(f"   at Frame{i}()" for i in range(12))

    def run_groups(self, streams):
        replies = iter([self.METRIC, {"status": "success", "data": {"result": streams}}])
        with mock.patch.object(lk, "get", lambda *a, **k: next(replies)):
            return lk.flatten(lk.groups(lk.settings(LOKI_CFG), 0, 600))

    def test_metadata_merged_into_stream_labels(self):
        rows = self.run_groups([{"stream": {"service_name": "api", "exception_message": self.EXC,
                                            "exception_stacktrace": self.STACK},
                                 "values": [["1000000000000", ""], ["2000000000000", ""]]}])
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["signature"], "unsampled")
        self.assertTrue(rows[0]["sample"].startswith(self.EXC))
        self.assertIn("Frame7", rows[0]["sample"])
        self.assertNotIn("Frame8", rows[0]["sample"])  # first 8 stack lines only

    def test_categorize_labels_third_element(self):
        other = "Order 99 not found for tenant 1a2b3c4d-1111-2222-3333-444455556666"
        rows = self.run_groups([{"stream": {"service_name": "api"}, "values": [
            ["1000000000000", "", {"structuredMetadata": {"exception_message": self.EXC}}],
            ["2000000000000", "  ", {"structuredMetadata": {"exception_message": other}, "parsed": {}}]]}])
        self.assertEqual(len(rows), 1)  # numbers / GUIDs collapsed: one stable signature
        self.assertEqual(rows[0]["sampleCount"], 2)
        self.assertEqual(rows[0]["signature"], cc.signature(self.EXC))

    def test_empty_line_falls_back_to_message_keys(self):
        rows = self.run_groups([{"stream": {"service_name": "api"}, "values": [
            ["1000000000000", "", {"structuredMetadata": {"body": "db pool exhausted"}}]]}])
        self.assertEqual(rows[0]["sample"], "db pool exhausted")

    def test_exception_message_keys_over_rendered_line(self):
        # Rendered lines carry per-request values; the same exception must group once.
        rows = self.run_groups([{"stream": {"service_name": "api", "exception_message": self.EXC},
                                 "values": [["1000000000000", "Validation failure on: tenant acme"],
                                            ["2000000000000", "Validation failure on: tenant vanti"]]}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signature"], cc.signature(self.EXC))
        line = '{"message":"Timeout after 3000ms"}'
        plain = self.run_groups([{"stream": {"service_name": "api"}, "values": [["1000000000000", line]]}])
        self.assertEqual(plain[0]["signature"], cc.signature(line))

    def test_all_empty_is_unsampled(self):
        rows = self.run_groups([{"stream": {"service_name": "api"}, "values": [
            ["1000000000000", "", {"structuredMetadata": {}, "parsed": {}}]]}])
        self.assertEqual(rows[0]["signature"], "unsampled")
        self.assertEqual(rows[0]["sample"], "")


class Prometheus(unittest.TestCase):
    def test_list_firing_alerts_and_down_targets(self):
        with mock.patch("urllib.request.urlopen", fixture_http.Urlopen(FIX)):
            rows = pm.list_candidates(pm.settings(PROM_CFG))
        self.assertEqual([r["kind"] for r in rows], ["alert", "alert", "target"])
        self.assertEqual(rows[0]["alertname"], "HighErrorRate")  # critical first, pending dropped
        self.assertTrue(rows[0]["fingerprint"].startswith("prom|alert|HighErrorRate|"))
        self.assertEqual(rows[2]["fingerprint"], "prom|target|worker|primecore-worker:8080")

    def test_job_filter(self):
        cfg = {"collect": {"prometheus": {"url": "http://p:9090", "job_filter": "^(gateway|worker)$"}}}
        with mock.patch("urllib.request.urlopen", fixture_http.Urlopen(FIX)):
            rows = pm.list_candidates(pm.settings(cfg))
        self.assertEqual([r.get("alertname") or r["job"] for r in rows], ["HighErrorRate", "worker"])

    def test_ping(self):
        with mock.patch("urllib.request.urlopen", fixture_http.Urlopen(FIX)), \
                mock.patch.object(pm.cc, "active_config", return_value=(PROM_CFG, None)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = pm.main(["ping"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])


DEPLOY = 1_760_700_000
ERR_Q = 'sum(rate(http_server_request_duration_seconds_count{http_response_status_code=~"5.."}[5m]))'
REQ_Q = "sum(rate(http_server_request_duration_seconds_count[5m]))"


def watch_cfg(**watch):
    w = {"enabled": True, "window_s": 1800, "promql_error_rate": ERR_Q}
    w.update(watch)
    return {"collect": {"prometheus": {"url": "http://p:9090"}}, "deploy": {"regression_watch": w}}


def prom_router(series, loki=None):
    """urlopen stand-in. series[(query, 'before'|'after')] = list of sample values (None → no
    series). loki[('lines'|'errors', 'before'|'after')] = a count (None → empty result)."""
    calls = []

    def fx(req, timeout=None):
        u = urllib.parse.urlsplit(req.full_url)
        q = urllib.parse.parse_qs(u.query)
        calls.append((u.path, q))
        query = q.get("query", [""])[0]
        if u.path == "/api/v1/query_range":
            side = "before" if int(q["end"][0]) <= DEPLOY else "after"
            vals = series.get((query, side))
            result = [] if vals is None else [{"metric": {}, "values": [[DEPLOY, str(v)] for v in vals]}]
            return fixture_http._Resp(json.dumps({"status": "success", "data": {"result": result}}).encode())
        if u.path == "/loki/api/v1/query":
            side = "before" if int(q["time"][0]) <= DEPLOY else "after"
            kind = "errors" if "severity_text" in query else "lines"
            n = (loki or {}).get((kind, side))
            result = [] if n is None else [{"metric": {}, "value": [DEPLOY, str(n)]}]
            return fixture_http._Resp(json.dumps({"status": "success", "data": {"result": result}}).encode())
        raise AssertionError(f"unexpected URL {req.full_url}")
    fx.calls = calls
    return fx


class RegressionWatch(unittest.TestCase):
    def run_check(self, cfg, series, loki=None):
        fx = prom_router(series, loki)
        with mock.patch("urllib.request.urlopen", fx):
            out = pm.check(cfg, DEPLOY, now=DEPLOY + 1800)
        return out, fx.calls

    def test_regression_when_ratio_and_floor_exceeded(self):
        out, calls = self.run_check(watch_cfg(), {(ERR_Q, "before"): [0.02, 0.04], (ERR_Q, "after"): [0.3, 0.5]})
        self.assertEqual((out["status"], out["regression"]), ("ok", True))
        self.assertAlmostEqual(out["prometheus"]["ratio"], 0.4 / 0.03, places=3)
        self.assertEqual(out["windowS"], 1800)
        self.assertFalse(out["clipped"])
        ranges = [q for p, q in calls if p == "/api/v1/query_range"]
        self.assertEqual({(q["start"][0], q["end"][0]) for q in ranges},
                         {(str(DEPLOY - 1800), str(DEPLOY)), (str(DEPLOY), str(DEPLOY + 1800))})
        self.assertIn("x13.33", out["summary"])

    def test_below_floor_is_no_regression(self):
        # 10x the errors, but 0.01 errors/s after: low-traffic noise.
        out, _ = self.run_check(watch_cfg(), {(ERR_Q, "before"): [0.001], (ERR_Q, "after"): [0.01]})
        p = out["prometheus"]
        self.assertEqual((out["status"], out["regression"], p["overRatio"], p["overAbs"]), ("ok", False, True, False))

    def test_ratio_only_and_abs_only_are_no_regression(self):
        out, _ = self.run_check(watch_cfg(min_abs=1.0), {(ERR_Q, "before"): [0.1], (ERR_Q, "after"): [0.5]})
        self.assertFalse(out["regression"], "ratio over, floor under")
        out, _ = self.run_check(watch_cfg(), {(ERR_Q, "before"): [0.4], (ERR_Q, "after"): [0.6]})
        self.assertFalse(out["regression"], "floor over, ratio 1.5 under 2.0")
        self.assertEqual(out["status"], "ok")

    def test_zero_before_counts_as_infinite_ratio(self):
        out, _ = self.run_check(watch_cfg(), {(ERR_Q, "after"): [0.2]})
        self.assertTrue(out["regression"])
        self.assertTrue(out["prometheus"]["beforeZero"])
        self.assertIsNone(out["prometheus"]["ratio"])
        self.assertIn("xinf", out["summary"])

    def test_no_data_in_both_windows_is_inconclusive(self):
        out, _ = self.run_check(watch_cfg(), {})
        self.assertEqual((out["status"], out["regression"]), ("inconclusive", False))
        self.assertIn("no error-rate series", out["prometheus"]["reason"])

    def test_request_rate_makes_a_ratio(self):
        series = {(ERR_Q, "before"): [0.1], (ERR_Q, "after"): [0.4], (REQ_Q, "before"): [10], (REQ_Q, "after"): [40]}
        out, _ = self.run_check(watch_cfg(promql_request_rate=REQ_Q), series)
        self.assertEqual(out["prometheus"]["metric"], "error_ratio")
        self.assertFalse(out["regression"], "4x traffic, same error share")
        out, _ = self.run_check(watch_cfg(promql_request_rate=REQ_Q), {(ERR_Q, "after"): [0.3]})
        self.assertEqual(out["status"], "inconclusive", "no traffic series → inconclusive")
        series = {(REQ_Q, "before"): [10], (REQ_Q, "after"): [10]}
        out, _ = self.run_check(watch_cfg(promql_request_rate=REQ_Q), series)
        self.assertEqual((out["status"], out["regression"]), ("ok", False), "traffic, no 5xx → ok")

    def test_loki_error_ratio_over_the_same_windows(self):
        cfg = watch_cfg()
        cfg["collect"]["loki"] = {"url": "http://l:3100", "selector": SEL}
        loki = {("lines", "after"): 5000, ("errors", "before"): 20, ("errors", "after"): 400}
        out, calls = self.run_check(cfg, {}, loki)
        self.assertEqual((out["status"], out["regression"]), ("ok", True), "prom inconclusive, loki regressed")
        self.assertEqual((out["loki"]["errorsBefore"], out["loki"]["errorsAfter"]), (20, 400))
        lq = [q["query"][0] for p, q in calls if p == "/loki/api/v1/query"]
        self.assertTrue(all(SEL in q for q in lq), lq)
        self.assertTrue(any("[1800s]" in q and "severity_text" in q for q in lq), lq)
        loki = {("lines", "after"): 5000, ("errors", "before"): 1, ("errors", "after"): 20}
        out, _ = self.run_check(cfg, {}, loki)
        self.assertFalse(out["regression"], "20 errors in 30 min is under the 0.05/s floor")
        out, _ = self.run_check(cfg, {}, {})
        self.assertEqual(out["status"], "inconclusive", "no logs and no series → inconclusive")
        self.assertEqual(out["loki"]["status"], "inconclusive")

    def test_window_clips_to_now(self):
        fx = prom_router({(ERR_Q, "before"): [0.1], (ERR_Q, "after"): [0.1]})
        with mock.patch("urllib.request.urlopen", fx):
            out = pm.check(watch_cfg(), DEPLOY, now=DEPLOY + 600)
        self.assertEqual((out["windowS"], out["clipped"]), (600, True))

    def test_missing_query_and_unreachable(self):
        cfg = watch_cfg()
        del cfg["deploy"]["regression_watch"]["promql_error_rate"]
        with mock.patch.object(pm.cc, "active_config", return_value=(cfg, None)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = pm.main(["check", "--since-epoch", str(DEPLOY)])
        self.assertEqual(rc, 2)
        self.assertIn("promql_error_rate is required", out.getvalue())
        cfg = watch_cfg()
        cfg["collect"]["prometheus"]["url"] = "http://127.0.0.1:9"
        with mock.patch.object(pm.cc, "active_config", return_value=(cfg, None)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = pm.main(["check", "--since-epoch", str(DEPLOY)])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out.getvalue())["status"], "unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=1)
