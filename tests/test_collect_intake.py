"""Cross-source signatures (collect_common.cross_signature) and the Tempo enricher.

One .NET exception and one JS exception, each seen by Sentry, Loki and PostHog, must give
one signature per exception; a near miss (another top frame) must not. Offline: payloads
are inline fixtures, urlopen / the transport are patched.

    python3 tests/test_collect_intake.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.parse
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "super-collect", "scripts"))
import collect_common as cc  # noqa: E402
import collect_loki as lk  # noqa: E402
import collect_posthog as ph  # noqa: E402
import collect_sentry as sn  # noqa: E402
import collect_tempo as tp  # noqa: E402

TRACE = "0b7b05232a90b467cc6111b73ccb480a"

# ── one .NET exception, three sources ────────────────────────────────────────
NET_MSG = "The plan limit 'limit.storage-gb' (0) has been reached."
NET_STACK = (
    "PrimeCore.Billing.EntitlementException: The plan limit 'limit.storage-gb' (0) has been reached.\n"
    "   at PrimeCore.FileStorage.StorageSoftCap.EnsureCanStore(EntitlementSnapshot s, Int64 used) in /src/StorageSoftCap.cs:line 42\n"
    "   at PrimeCore.FileStorage.Import.StoredImageWriter.<PersistAsync>d__4.MoveNext() in /src/StoredImageWriter.cs:line 88\n"
    "   at Wolverine.Runtime.Handlers.Executor.ExecuteAsync(MessageContext context)\n")
NET_SENTRY_EVENT = {
    "platform": "csharp", "contexts": {"trace": {"trace_id": TRACE}},
    "entries": [{"type": "exception", "data": {"values": [{
        "type": "PrimeCore.Billing.EntitlementException", "value": "The plan limit 'limit.storage-gb' (5) has been reached.",
        "stacktrace": {"frames": [
            {"module": "Wolverine.Runtime.Handlers.Executor", "function": "ExecuteAsync", "inApp": False},
            {"module": "PrimeCore.FileStorage.Import.StoredImageWriter", "function": "PersistAsync", "inApp": True},
            {"module": "PrimeCore.FileStorage.StorageSoftCap",
             "function": "void StorageSoftCap.EnsureCanStore(EntitlementSnapshot s, long used)", "inApp": True}]}}]}}]}
NET_LOKI_META = {"structuredMetadata": {"exception_type": "PrimeCore.Billing.EntitlementException",
                                        "exception_message": NET_MSG, "exception_stacktrace": NET_STACK,
                                        "trace_id": TRACE, "span_id": "b9c7c989f97918e1"}}
NET_POSTHOG_LIST = [{"type": "EntitlementException", "value": NET_MSG, "stacktrace": {"frames": [
    {"module": "PrimeCore.FileStorage.Import.StoredImageWriter", "function": "PersistAsync", "lang": "csharp"},
    {"module": "PrimeCore.FileStorage.StorageSoftCap", "function": "EnsureCanStore", "lang": "csharp"}]}}]

# ── one JS exception, three sources ──────────────────────────────────────────
JS_MSG = "Cannot read properties of undefined (reading 'items')"
JS_SENTRY_EVENT = {
    "platform": "javascript",
    "entries": [{"type": "exception", "data": {"values": [{"type": "TypeError", "value": JS_MSG,
        "stacktrace": {"frames": [
            {"filename": "./node_modules/react-dom/cjs/react-dom.development.js", "function": "commitRoot"},
            {"filename": "./src/checkout/CheckoutPage.tsx", "function": "CheckoutPage"},
            {"filename": "./src/checkout/summary.ts", "function": "buildSummary"}]}}]}}]}
JS_LOKI_META = {"structuredMetadata": {
    "exception_type": "TypeError", "exception_message": JS_MSG,
    "exception_stacktrace": ("TypeError: Cannot read properties of undefined (reading 'items')\n"
                             "    at buildSummary (webpack-internal:///./src/checkout/summary.ts:12:18)\n"
                             "    at CheckoutPage (webpack-internal:///./src/checkout/CheckoutPage.tsx:31:5)\n"
                             "    at commitRoot (webpack-internal:///./node_modules/react-dom/cjs/react-dom.development.js:1201:3)")}}
JS_POSTHOG_LIST = [{"type": "TypeError", "value": JS_MSG, "stacktrace": {"frames": [
    {"filename": "https://shop.example.com/_next/static/chunks/node_modules/react-dom.js", "function": "commitRoot", "in_app": False},
    {"filename": "https://shop.example.com/src/checkout/CheckoutPage.tsx", "function": "CheckoutPage", "in_app": True},
    {"filename": "https://shop.example.com/src/checkout/summary.ts?v=3", "function": "buildSummary", "in_app": True}]}}]


def loki_cross(meta):
    return lk.entry_links({}, meta)[0]


class CrossSignature(unittest.TestCase):
    def test_dotnet_exception_matches_across_sources(self):
        s = sn.event_links(NET_SENTRY_EVENT)[0]
        lo = loki_cross(NET_LOKI_META)
        p = ph.exception_cross(json.dumps(NET_POSTHOG_LIST))
        self.assertRegex(s, r"^[0-9a-f]{12}$")
        self.assertEqual(s, lo)
        self.assertEqual(s, p)

    def test_js_exception_matches_across_sources(self):
        s = sn.event_links(JS_SENTRY_EVENT)[0]
        lo = loki_cross(JS_LOKI_META)
        p = ph.exception_cross(json.dumps(JS_POSTHOG_LIST))
        self.assertRegex(s, r"^[0-9a-f]{12}$")
        self.assertEqual(s, lo)
        self.assertEqual(s, p)
        self.assertNotEqual(s, sn.event_links(NET_SENTRY_EVENT)[0])

    def test_near_miss_top_frame_differs(self):
        meta = json.loads(json.dumps(NET_LOKI_META))
        md = meta["structuredMetadata"]
        md["exception_stacktrace"] = md["exception_stacktrace"].replace("StorageSoftCap.EnsureCanStore", "StorageSoftCap.EnsureCanRead")
        self.assertNotEqual(loki_cross(meta), loki_cross(NET_LOKI_META))
        js = json.loads(json.dumps(JS_POSTHOG_LIST))
        js[0]["stacktrace"]["frames"][-1]["function"] = "buildTotals"
        self.assertNotEqual(ph.exception_cross(js), ph.exception_cross(JS_POSTHOG_LIST))

    def test_bare_message_never_cross_matches(self):
        self.assertEqual(cc.cross_signature("", "Connection refused", ""), "")
        self.assertEqual(loki_cross({"structuredMetadata": {"exception_message": "boom"}}), "")
        self.assertEqual(ph.exception_cross("not json"), "")

    def test_vendor_frames_are_skipped(self):
        only_vendor = "System.Exception: x\n   at System.Net.Http.HttpClient.Send()\n   at Microsoft.AspNetCore.X.Y()\n"
        self.assertEqual(cc.stack_top_frame(only_vendor), "")
        self.assertTrue(cc.is_vendor_frame("node_modules/react/index.js"))
        self.assertTrue(cc.is_vendor_frame("webpack-internal:///./node_modules/next/dist/client/x.js"))
        self.assertFalse(cc.is_vendor_frame("PrimeCore.Api.OrdersController"))

    def test_inner_exception_frames_are_skipped(self):
        stack = ("System.Net.Http.HttpRequestException: refused\n"
                 " ---> System.Net.Sockets.SocketException (111): refused\n"
                 "   at PrimeCore.Inner.Socketish.Connect()\n"
                 "   --- End of inner exception stack trace ---\n"
                 "   at PrimeCore.Gateway.Proxy.<SendAsync>g__Core|4_0(HttpRequestMessage r)\n")
        self.assertEqual(cc.stack_top_frame(stack), "PrimeCore.Gateway.Proxy.SendAsync")

    def test_dotnet_frame_forms_agree(self):
        want = "Ns.OrderService.PlaceOrder"
        self.assertEqual(cc.dotnet_frame("Ns.OrderService.<PlaceOrder>d__5.MoveNext"), want)
        self.assertEqual(cc.dotnet_frame("PlaceOrder", "Ns.OrderService"), want)
        self.assertEqual(cc.dotnet_frame("async Task<Order> OrderService.PlaceOrder(int id)", "Ns.OrderService"), want)


class SourcesCarryLinks(unittest.TestCase):
    def test_loki_candidates_carry_cross_signature_and_trace(self):
        streams = {"status": "success", "data": {"result": [{"stream": {"service_name": "primecore-admin-bff"},
                   "values": [["1760700100000000000", "", NET_LOKI_META]]}]}}
        metric = {"status": "success", "data": {"result": [{"metric": {"service_name": "primecore-admin-bff"}, "value": [1, "3"]}]}}

        def get_json(base, path, params=None, transport=None, timeout=25, label=""):
            return streams if path.endswith("query_range") else metric
        st = lk.settings({"collect": {"loki": {"url": "http://l:3100", "selector": '{env="dev"}'}}})
        with mock.patch.object(cc, "get_json", get_json):
            rows = lk.list_groups(st, cc.since("1d"))
        self.assertEqual(rows[0]["crossSignature"], sn.event_links(NET_SENTRY_EVENT)[0])
        self.assertEqual((rows[0]["traceId"], rows[0]["spanId"]), (TRACE, "b9c7c989f97918e1"))

    def test_sentry_trace_id_from_contexts(self):
        self.assertEqual(sn.event_links(NET_SENTRY_EVENT)[1], TRACE)
        self.assertEqual(sn.event_links(JS_SENTRY_EVENT)[1], "")

    def test_posthog_exception_rows_carry_cross_signature(self):
        row = ["issue-1", "TypeError", 9, 6, "2026-10-01", "2026-10-02", "sess", json.dumps(JS_POSTHOG_LIST)]
        st = {"host": "https://us.posthog.com", "pid": "1", "min_users": None}
        with mock.patch.object(ph, "hogql", return_value=[row]) as q:
            out, _ = ph.run_signal(st, "exception", cc.since("1d"))
        self.assertIn("$exception_list", q.call_args[0][1])
        self.assertEqual(out[0]["crossSignature"], sn.event_links(JS_SENTRY_EVENT)[0])


# ── Tempo ────────────────────────────────────────────────────────────────────
def kv(k, v):
    return {"key": k, "value": {"stringValue": v}}


SPANS_GW = [{"spanId": "aaaaaaaaaaaaaaaa", "name": "POST /api/tenants", "startTimeUnixNano": "1000000000",
             "endTimeUnixNano": "1176000000", "status": {}}]
SPANS_BFF = [
    {"spanId": "bbbbbbbbbbbbbbbb", "parentSpanId": "aaaaaaaaaaaaaaaa", "name": "ImportExternalImage",
     "startTimeUnixNano": "1010000000", "endTimeUnixNano": "1090000000", "status": {"code": 2},
     "events": [{"name": "exception", "attributes": [kv("exception.type", "EntitlementException"),
                                                     kv("exception.message", NET_MSG)]}]},
    {"spanId": "cccccccccccccccc", "parentSpanId": "aaaaaaaaaaaaaaaa", "name": "SELECT tenants",
     "startTimeUnixNano": "1005000000", "endTimeUnixNano": "1006000000",
     "attributes": [kv("otel.status_code", "ERROR")], "status": {"message": "timeout"}}]
BATCHES = {"batches": [
    {"resource": {"attributes": [kv("service.name", "primecore-gateway")]}, "scopeSpans": [{"spans": SPANS_GW}]},
    {"resource": {"attributes": [kv("service.name", "primecore-admin-bff")]},
     "instrumentationLibrarySpans": [{"spans": SPANS_BFF}]}]}
RESOURCE_SPANS = {"trace": {"resourceSpans": BATCHES["batches"]}}
TEMPO_CFG = {"collect": {"tempo": {"url": "http://primecore-tempo:3200", "grafana_url": "http://grafana:3000",
                                   "grafana_datasource_uid": "tempo-dev"},
                         "loki": {"transport": {"kind": "ssh", "host": "dev", "docker_network": "net"}}}}


class Tempo(unittest.TestCase):
    def test_both_otlp_shapes_summarise_the_same(self):
        a, b = tp.summarize(BATCHES, TRACE), tp.summarize(RESOURCE_SPANS, TRACE)
        self.assertEqual(a, b)
        self.assertEqual(a["services"], ["primecore-gateway", "primecore-admin-bff"])
        self.assertEqual(a["root"], {"service": "primecore-gateway", "name": "POST /api/tenants", "durationMs": 176.0})
        self.assertEqual(a["spanCount"], 3)

    def test_error_spans_extracted(self):
        errs = tp.summarize(BATCHES, TRACE)["errors"]
        self.assertEqual([e["name"] for e in errs], ["SELECT tenants", "ImportExternalImage"])
        self.assertEqual(errs[1]["exception"], "EntitlementException: " + NET_MSG)
        self.assertEqual(errs[0]["exception"], "timeout")

    def test_span_list_is_capped(self):
        many = {"batches": [{"resource": {"attributes": [kv("service.name", "s")]}, "scopeSpans": [{"spans": [
            {"spanId": f"{i:016x}", "name": f"op{i}", "startTimeUnixNano": str(i), "endTimeUnixNano": str(i + 1)}
            for i in range(1, 60)]}]}]}
        out = tp.summarize(many, TRACE)
        self.assertEqual((out["spanCount"], len(out["spans"])), (59, tp.MAX_SPANS))

    def test_explore_link(self):
        st = tp.settings(TEMPO_CFG)
        link = tp.explore_link(st, TRACE, 1_000_000_000, 2_000_000_000)
        self.assertTrue(link.startswith("http://grafana:3000/explore?"))
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
        panes = json.loads(q["panes"][0])
        query = list(panes.values())[0]["queries"][0]
        self.assertEqual(query["datasource"], {"type": "tempo", "uid": "tempo-dev"})
        self.assertEqual(query["query"], TRACE)
        self.assertEqual(q["schemaVersion"], ["1"])
        self.assertIsNone(tp.explore_link(tp.settings({"collect": {"tempo": {"url": "http://t"}}}), TRACE))

    def test_transport_defaults_to_loki_and_evidence_is_capped(self):
        seen = {}

        def get_json(base, path, params=None, transport=None, timeout=25, label=""):
            seen.update(base=base, path=path, transport=transport)
            return BATCHES
        with mock.patch.object(cc, "get_json", get_json):
            ev = tp.evidence(tp.settings(TEMPO_CFG), TRACE)
        self.assertEqual(seen["path"], f"/api/traces/{TRACE}")
        self.assertEqual(seen["transport"]["kind"], "ssh")
        lines = ev.splitlines()
        self.assertLessEqual(len(lines), tp.MAX_EVIDENCE_LINES)
        self.assertIn(TRACE, lines[0])
        self.assertIn("Grafana Explore", lines[-1])

    def test_no_config_is_just_the_id(self):
        with mock.patch.object(cc, "get_json", side_effect=AssertionError("no fetch without config")):
            self.assertEqual(tp.evidence(tp.settings({}), TRACE), f"- **Trace:** `{TRACE}`")

    def test_unreachable_evidence_keeps_the_id(self):
        with mock.patch.object(cc, "get_json", side_effect=cc.FetchError("Tempo unreachable over ssh")):
            ev = tp.evidence(tp.settings(TEMPO_CFG), TRACE)
        self.assertIn(TRACE, ev)
        self.assertIn("unavailable", ev)

    def test_trace_id_validated_and_base64_ids(self):
        with self.assertRaises(tp.TempoError):
            tp.trace_id("abc;rm")
        self.assertEqual(tp.span_hex("qqqqqqqqqqo="), "aaaaaaaaaaaaaaaa")


if __name__ == "__main__":
    unittest.main()
