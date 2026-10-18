#!/usr/bin/env python3
"""super-collect enricher `tempo`: summarise one trace from Grafana Tempo, read-only.

An enricher, not a source: it files nothing and `/super-collect` never lists it in
`sources`. Loki and Sentry candidates carry a `traceId`; the filing step
(super-collect-file.sh --trace-id) calls `evidence` to put the trace in the card.

Stdlib only. Config `collect.tempo`:
  url                     Tempo base URL, e.g. http://tempo:3200 (required for a fetch)
  transport               {"kind":"http"} or {"kind":"ssh","host":…,"docker_network":…}
                          (collect_common.get_json). Absent: collect.loki's transport, since
                          Tempo usually lives on the same host and Docker network.
  grafana_url             optional, e.g. http://grafana:3000: adds an Explore link
  grafana_datasource_uid  the Tempo datasource uid in that Grafana (needed for the link)
  grafana_org_id          default 1

    collect_tempo.py trace <trace_id> [--url http://tempo:3200] [--config cfg.json]
    collect_tempo.py evidence <trace_id> [--url …]      # ≤ 10 markdown lines for a card
    collect_tempo.py ping [--url …]

`trace` GETs /api/traces/<id> (OTLP JSON, `batches` or `resourceSpans`, optionally under
`trace`) and prints {traceId, services (in order of first span), root {service, name,
durationMs}, spanCount, errorCount, errors [{service, name, durationMs, exception, count}]
(one row per distinct service + span + exception), spans (≤ 25, by start time)}.
An error span is status code 2 / STATUS_CODE_ERROR, attribute otel.status_code=ERROR, or
one with an `exception` event. Unreachable → {"status":"unavailable"} and exit 2.
`evidence` never fails the filing: with no collect.tempo.url it prints just the trace id;
when Tempo is unreachable it prints the id and why, exit 0.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_common as cc  # noqa: E402

MAX_SPANS = 25
MAX_EVIDENCE_LINES = 10
_TRACE_ID = re.compile(r"[0-9a-f]{32}")
_UID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class TempoError(RuntimeError):
    pass


def trace_id(raw):
    t = str(raw or "").strip().lower()
    if not _TRACE_ID.fullmatch(t):
        raise TempoError("trace id must be 32 hex characters")
    return t


def settings(cfg, url=None):
    s = cc.collect_block(cfg, "tempo")
    loki = cc.collect_block(cfg, "loki")
    return {"url": (url or s.get("url") or "").strip(),
            "transport": s.get("transport") or loki.get("transport") or {"kind": "http"},
            "grafana_url": (s.get("grafana_url") or "").strip().rstrip("/"),
            "datasource_uid": (s.get("grafana_datasource_uid") or "").strip(),
            "org_id": int(s.get("grafana_org_id") or 1)}


def fetch(st, tid):
    if not st["url"]:
        raise TempoError("collect.tempo.url is not set")
    try:
        return cc.get_json(st["url"], f"/api/traces/{tid}", None, st["transport"], label="Tempo")
    except cc.FetchError as e:
        raise TempoError(str(e)) from None


# ── OTLP JSON ──────────────────────────────────────────────────────────────

def attr_value(v):
    if not isinstance(v, dict):
        return v
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return v[k]
    return None


def attrs(lst):
    return {a.get("key"): attr_value(a.get("value")) for a in lst or [] if isinstance(a, dict)}


def span_hex(raw):
    """OTLP JSON ids are hex (Tempo) or base64 (protobuf JSON mapping)."""
    s = str(raw or "")
    if not s or re.fullmatch(r"[0-9a-fA-F]+", s):
        return s.lower()
    try:
        return binascii.hexlify(base64.b64decode(s)).decode()
    except (binascii.Error, ValueError):
        return s


def iter_spans(doc):
    """(service, span) for every span, whichever OTLP JSON shape Tempo sent."""
    doc = doc.get("trace") if isinstance(doc, dict) and isinstance(doc.get("trace"), dict) else doc
    if not isinstance(doc, dict):
        raise TempoError("Tempo sent no trace object")
    batches = doc.get("batches") or doc.get("resourceSpans") or []
    for b in batches:
        service = attrs((b.get("resource") or {}).get("attributes")).get("service.name") or "unknown"
        for scope in (b.get("scopeSpans") or b.get("instrumentationLibrarySpans") or []):
            for sp in scope.get("spans") or []:
                yield service, sp


def is_error(sp, a):
    code = (sp.get("status") or {}).get("code")
    if code in (2, "2", "STATUS_CODE_ERROR", "ERROR"):
        return True
    if str(a.get("otel.status_code") or "").upper() == "ERROR":
        return True
    return any((e.get("name") or "") == "exception" for e in sp.get("events") or [])


def exception_text(sp, a):
    for e in sp.get("events") or []:
        if (e.get("name") or "") == "exception":
            ea = attrs(e.get("attributes"))
            t, m = ea.get("exception.type") or "", ea.get("exception.message") or ""
            if t or m:
                return (f"{t}: {m}" if t and m else t or m)[:200]
    m = (sp.get("status") or {}).get("message") or a.get("otel.status_description") or a.get("error.message") or ""
    return str(m)[:200]


def summarize(doc, tid=""):
    rows = []
    for service, sp in iter_spans(doc):
        a = attrs(sp.get("attributes"))
        start, end = int(sp.get("startTimeUnixNano") or 0), int(sp.get("endTimeUnixNano") or 0)
        rows.append({"service": service, "name": sp.get("name") or "?",
                     "spanId": span_hex(sp.get("spanId")), "parent": span_hex(sp.get("parentSpanId")),
                     "start": start, "durationMs": round(max(0, end - start) / 1e6, 1),
                     "error": is_error(sp, a), "exception": exception_text(sp, a) if is_error(sp, a) else ""})
    if not rows:
        raise TempoError(f"trace {tid} has no spans")
    rows.sort(key=lambda r: r["start"])
    ids = {r["spanId"] for r in rows}
    roots = [r for r in rows if not r["parent"] or r["parent"] not in ids]
    root = roots[0]
    services = []
    for r in rows:
        if r["service"] not in services:
            services.append(r["service"])
    # One row per distinct (service, span name, exception): a retried handler is one problem.
    errors, by_key = [], {}
    for r in rows:
        if not r["error"]:
            continue
        key = (r["service"], r["name"], r["exception"])
        if key in by_key:
            by_key[key]["count"] += 1
            continue
        by_key[key] = {k: r[k] for k in ("service", "name", "durationMs", "exception")}
        by_key[key]["count"] = 1
        errors.append(by_key[key])
    return {"traceId": tid, "services": services,
            "root": {"service": root["service"], "name": root["name"], "durationMs": root["durationMs"]},
            "spanCount": len(rows), "errorCount": sum(r["error"] for r in rows), "errors": errors[:MAX_SPANS],
            "start": rows[0]["start"], "end": max(r["start"] + int(r["durationMs"] * 1e6) for r in rows),
            "spans": [{k: r[k] for k in ("service", "name", "durationMs", "error")} for r in rows[:MAX_SPANS]]}


# ── Grafana Explore link ───────────────────────────────────────────────────

def explore_link(st, tid, start_ns=0, end_ns=0):
    """Grafana's documented Explore URL (`/explore?panes=…&schemaVersion=1&orgId=…`) with one
    Tempo TraceQL query for the trace id. None without grafana_url + datasource uid."""
    if not (st["grafana_url"] and st["datasource_uid"]):
        return None
    if not _UID.fullmatch(st["datasource_uid"]):
        raise TempoError("collect.tempo.grafana_datasource_uid has unsafe characters")
    if start_ns and end_ns:
        rng = {"from": str(start_ns // 1_000_000 - 3_600_000), "to": str(end_ns // 1_000_000 + 3_600_000)}
    else:
        rng = {"from": "now-24h", "to": "now"}
    ds = {"type": "tempo", "uid": st["datasource_uid"]}
    panes = {"sb": {"datasource": st["datasource_uid"], "range": rng,
                    "queries": [{"refId": "A", "datasource": ds, "queryType": "traceql", "query": tid}]}}
    q = urllib.parse.urlencode({"panes": json.dumps(panes, separators=(",", ":")), "schemaVersion": "1",
                                "orgId": str(st["org_id"])}, quote_via=urllib.parse.quote)
    return f"{st['grafana_url']}/explore?{q}"


def evidence(st, tid):
    """≤ 10 markdown lines: the Trace line for a card's Evidence. Never raises."""
    head = f"- **Trace:** `{tid}`"
    if not st["url"]:
        return head
    try:
        s = summarize(fetch(st, tid), tid)
    except TempoError as e:
        return f"{head} · Tempo unavailable: {str(e)[:120]}"
    lines = [f"{head} · root `{s['root']['service']}` {s['root']['name']} · {s['root']['durationMs']} ms · "
             f"{s['spanCount']} spans · {s['errorCount']} errors",
             "  - services: " + ", ".join(f"`{x}`" for x in s["services"][:8])]
    for e in s["errors"]:
        if len(lines) >= MAX_EVIDENCE_LINES - 1:
            break
        lines.append(f"  - ❌ `{e['service']}` {e['name']} · {e['durationMs']} ms"
                     + (f" · {e['exception']}" if e["exception"] else "")
                     + (f" · ×{e['count']}" if e["count"] > 1 else ""))
    try:
        link = explore_link(st, tid, s["start"], s["end"])
    except TempoError:
        link = None
    if link:
        lines.append(f"  - [Open in Grafana Explore]({link})")
    return "\n".join(lines[:MAX_EVIDENCE_LINES])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config")
    p.add_argument("--url", help="Tempo base URL; overrides collect.tempo.url for this call")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("trace", "evidence"):
        sub.add_parser(name).add_argument("trace_id")
    sub.add_parser("ping")
    a = p.parse_args(argv)
    cfg, _ = cc.active_config(a.config)
    st = settings(cfg, a.url)
    try:
        if a.cmd == "evidence":
            print(evidence(st, trace_id(a.trace_id)))
            return 0
        if a.cmd == "trace":
            tid = trace_id(a.trace_id)
            out = summarize(fetch(st, tid), tid)
            out["explore"] = explore_link(st, tid, out["start"], out["end"])
        else:
            if not st["url"]:
                raise TempoError("collect.tempo.url is not set")
            try:
                info = cc.get_json(st["url"], "/api/status/buildinfo", None, st["transport"], label="Tempo")
            except cc.FetchError as e:
                raise TempoError(str(e)) from None
            out = {"ok": True, "url": st["url"], "version": (info or {}).get("version")}
    except TempoError as e:
        print(json.dumps({"source": "tempo", "status": "unavailable", "error": str(e)}))
        return 2
    print(json.dumps(out, indent=1, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # Windows cp1252 consoles cannot print the docstring
    sys.exit(main())
