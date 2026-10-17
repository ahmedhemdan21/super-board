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
        self.assertEqual(argv[-1], "'http://primecore-loki:3100/x?a=%7B'")
        for bad in ({"kind": "ssh", "host": "h;rm -rf /", "docker_network": "n"},
                    {"kind": "ssh", "host": "h", "docker_network": "$(x)"}, {"kind": "ssh"}):
            with self.assertRaises(cc.FetchError):
                cc.ssh_argv(bad, "http://x/")

    def test_ssh_transport_runs_remote_curl(self):
        seen = {}

        def run(argv, **kw):
            seen["argv"] = argv
            return mock.Mock(returncode=0, stdout='{"status":"success","data":{"alerts":[]}}', stderr="")
        with mock.patch("subprocess.run", run):
            out = cc.get_json("http://p:9090", "/api/v1/alerts", None, SSH)
        self.assertEqual(out["data"], {"alerts": []})
        self.assertIn("curlimages/curl", seen["argv"])

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
                f.write(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{log}'\necho '{{\"status\":\"success\",\"data\":{{}}}}'\n")
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
            with mock.patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ["PATH"]}):
                cc.get_json("http://p:9090", "/api/v1/alerts", None, SSH)
            with open(log) as f:
                args = f.read().split("\n")
        self.assertIn("docker", args)
        self.assertIn("'http://p:9090/api/v1/alerts'", args)

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


if __name__ == "__main__":
    unittest.main(verbosity=1)
