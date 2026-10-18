#!/usr/bin/env python3
"""super-collect source `loki`: error-level log groups from Grafana Loki, read-only.

Stdlib only. Config `collect.loki`:
  url           Loki base URL, e.g. http://loki:3100 (required)
  selector      LogQL stream selector, e.g. {deployment_environment="dev"} (REQUIRED, no
                default: one Loki can hold prod too, so the collector refuses to run without it)
  error_filter  pipeline stage(s) that keep error lines;
                default | severity_text=~"(?i)error|fatal|critical"
  group_by      labels one group is keyed on; default ["service_name"]
  transport     {"kind":"http"} (default) or {"kind":"ssh","host":…,"docker_network":…}
                (collect_common.get_json)

    collect_loki.py list  [--since 14d|YYYY-MM-DD] [--samples 200] [--config cfg.json]
    collect_loki.py check --since-epoch <deploy unix ts> [--window-s 600] [--max-new 0]
                          [--release <sha> --release-label service_version] [--extra '<stages>']
    collect_loki.py ping

`list` prints {"source":"loki","since":…,"candidates":[…]}: one candidate per group and
error signature (sampled lines, numbers / GUIDs / timestamps collapsed; an empty line falls
back to structured metadata exception_message, then message / msg / body), fingerprint
`loki|<group>|<signature hash>`. `check` is the post-deploy telemetry check: error groups
in [deploy, deploy+window] that were not seen in the same-length window before the deploy;
`regression` is true when there are more than --max-new of them. When the selector
matches no log line at all after the deploy, `check` returns status "no_data" (not
"ok"): zero error groups out of zero logs says nothing about health.
Unreachable → {"status":"unavailable"} and exit 2.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_common as cc  # noqa: E402

DEFAULT_FILTER = '| severity_text=~"(?i)error|fatal|critical"'
_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BAD = re.compile(r"[\n\r`]")


class LokiError(RuntimeError):
    pass


def settings(cfg):
    s = cc.collect_block(cfg, "loki")
    url, sel = (s.get("url") or "").strip(), (s.get("selector") or "").strip()
    if not url:
        raise LokiError("collect.loki.url is required (e.g. http://loki:3100).")
    if not (sel.startswith("{") and sel.endswith("}") and len(sel) > 2) or _BAD.search(sel):
        raise LokiError('collect.loki.selector is required, e.g. {deployment_environment="dev"}: '
                        "Loki may hold prod logs too, so there is no default.")
    flt = s.get("error_filter") or DEFAULT_FILTER
    group_by = s.get("group_by") or ["service_name"]
    if _BAD.search(flt) or not all(_LABEL.fullmatch(g or "") for g in group_by):
        raise LokiError("collect.loki.error_filter / group_by has unsafe characters")
    return {"url": url, "selector": sel, "filter": flt, "group_by": list(group_by),
            "transport": s.get("transport") or {"kind": "http"}}


def get(st, path, params):
    try:
        out = cc.get_json(st["url"], path, params, st["transport"], label="Loki")
    except cc.FetchError as e:
        raise LokiError(str(e)) from None
    if not isinstance(out, dict) or out.get("status") not in (None, "success"):
        raise LokiError(f"Loki query failed on {path}: {str(out)[:200]}")
    return out


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def base_query(st, extra=""):
    if extra and _BAD.search(extra):
        raise LokiError("extra pipeline stage has unsafe characters")
    return " ".join(x for x in (st["selector"], st["filter"], extra) if x)


MESSAGE_KEYS = ("message", "msg", "body", "Body")
STACK_LINES = 8


def entry_fields(stream_labels, meta):
    """Labels + structured metadata for one entry. Loki 3.x either merges structured
    metadata into the stream labels, or (X-Loki-Response-Encoding-Flags:
    categorize-labels) sends it as a third value element {"structuredMetadata", "parsed"}."""
    out = dict(stream_labels or {})
    if isinstance(meta, dict):
        for k in ("structuredMetadata", "parsed"):
            if isinstance(meta.get(k), dict):
                out.update(meta[k])
    return out


def entry_message(line, stream_labels=None, meta=None):
    """(message, stack) for one log entry: exception_message (+ the first lines of
    exception_stacktrace), else the line itself, else message / msg / body. ("", "") only
    when every source is empty."""
    f = entry_fields(stream_labels, meta)
    exc = str(f.get("exception_message") or "").strip()
    if exc:
        stack = "\n".join(str(f.get("exception_stacktrace") or "").strip().splitlines()[:STACK_LINES])
        return exc, stack
    if (line or "").strip():
        return line.strip(), ""
    for k in MESSAGE_KEYS:
        v = str(f.get(k) or "").strip()
        if v:
            return v, ""
    return "", ""


def groups(st, start, end, extra="", samples=200):
    """{key: {labels, count, sigs: {sig: {count, sample, first, last}}}} for [start, end] (unix s)."""
    rng = max(1, int(end - start))
    q = base_query(st, extra)
    by = ", ".join(st["group_by"])
    metric = get(st, "/loki/api/v1/query",
                 {"query": f"sum by ({by}) (count_over_time({q} [{rng}s]))", "time": str(int(end))})
    out = {}
    for row in (metric.get("data") or {}).get("result") or []:
        labels = {g: (row.get("metric") or {}).get(g, "") for g in st["group_by"]}
        value = row.get("value") or [0, "0"]
        count = int(float(value[1])) if len(value) > 1 else 0
        if count <= 0:
            continue
        key = "/".join(labels[g] or "-" for g in st["group_by"])
        narrow = " ".join(f'| {g}="{esc(v)}"' for g, v in labels.items() if v)
        logs = get(st, "/loki/api/v1/query_range",
                   {"query": f"{q} {narrow}".strip(), "start": f"{int(start)}000000000",
                    "end": f"{int(end)}000000000", "limit": str(samples), "direction": "backward"})
        sigs = {}
        for stream in (logs.get("data") or {}).get("result") or []:
            labels_s = stream.get("stream") or {}
            for value in stream.get("values") or []:
                if not isinstance(value, (list, tuple)) or len(value) < 2:
                    continue
                ts, line = value[0], value[1] or ""
                meta = value[2] if len(value) > 2 else None
                msg, stack = entry_message(line, labels_s, meta)
                # A non-empty line keeps keying the group as before (no dedupe-key churn);
                # only lines that were empty fall back to the metadata message.
                sig = (cc.signature(line) if line.strip() else cc.signature(msg)) or "unsampled"
                t = int(ts) // 1_000_000_000
                sample = (msg[:300] + ("\n" + stack if stack else ""))[:1200]
                e = sigs.setdefault(sig, {"count": 0, "sample": sample, "first": t, "last": t})
                e["count"] += 1
                e["first"], e["last"] = min(e["first"], t), max(e["last"], t)
        out[key] = {"labels": labels, "count": count, "sigs": sigs or {"unsampled": {"count": count, "sample": "", "first": None, "last": None}}}
    return out


def line_count(st, start, end, extra=""):
    """Log lines (any level) the selector matches in [start, end]; 0 means no data."""
    if extra and _BAD.search(extra):
        raise LokiError("extra pipeline stage has unsafe characters")
    rng = max(1, int(end - start))
    q = " ".join(x for x in (st["selector"], extra) if x)
    out = get(st, "/loki/api/v1/query", {"query": f"sum(count_over_time({q} [{rng}s]))", "time": str(int(end))})
    total = 0
    for row in (out.get("data") or {}).get("result") or []:
        value = row.get("value") or [0, "0"]
        total += int(float(value[1])) if len(value) > 1 else 0
    return total


def iso(t):
    return dt.datetime.fromtimestamp(t, cc.UTC).isoformat() if t else None


def flatten(gs):
    rows = []
    for key, g in gs.items():
        for sig, e in g["sigs"].items():
            rows.append({"fingerprint": cc.fingerprint("loki", key, sig), "group": key, "labels": g["labels"],
                         "signature": sig, "sample": e["sample"], "sampleCount": e["count"],
                         "groupCount": g["count"], "firstSeen": iso(e["first"]), "lastSeen": iso(e["last"])})
    rows.sort(key=lambda r: (-r["groupCount"], -r["sampleCount"]))
    return rows


def list_groups(st, since, now=None, samples=200):
    now = now or time.time()
    return flatten(groups(st, since.timestamp(), now, samples=samples))


def check(st, deploy_ts, window_s=600, max_new=0, release=None, release_label=None, extra="", now=None):
    now = now or time.time()
    end = min(now, deploy_ts + window_s)
    after_extra = extra
    if release and release_label:
        if not (re.fullmatch(r"[0-9a-f]{7,40}", release) and _LABEL.fullmatch(release_label)):
            raise LokiError("--release must be a hex sha and --release-label a label name")
        after_extra = f'{extra} | {release_label}=~"{release[:7]}.*"'.strip()
    after_end = max(end, deploy_ts + 1)
    lines = line_count(st, deploy_ts, after_end, extra)
    if lines == 0:
        return {"source": "loki", "status": "no_data", "deploy": iso(deploy_ts), "windowS": int(end - deploy_ts),
                "release": release, "selector": " ".join(x for x in (st["selector"], extra) if x), "lines": 0,
                "regression": False}
    before = flatten(groups(st, deploy_ts - window_s, deploy_ts, extra))
    after = flatten(groups(st, deploy_ts, after_end, after_extra))
    seen = {r["fingerprint"] for r in before}
    new = [r for r in after if r["fingerprint"] not in seen]
    return {"source": "loki", "status": "ok", "deploy": iso(deploy_ts), "windowS": int(end - deploy_ts),
            "release": release, "lines": lines, "before": len(before), "after": len(after), "new": len(new),
            "maxNew": max_new, "regression": len(new) > max_new,
            "summary": "; ".join(f"{r['group']}: {r['signature'][:80]} ({r['sampleCount']}x)" for r in new[:5]),
            "newGroups": new}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("list")
    lp.add_argument("--since")
    lp.add_argument("--samples", type=int, default=200)
    ck = sub.add_parser("check")
    ck.add_argument("--since-epoch", type=int, required=True)
    ck.add_argument("--window-s", type=int, default=600)
    ck.add_argument("--max-new", type=int, default=0)
    ck.add_argument("--release")
    ck.add_argument("--release-label")
    ck.add_argument("--extra", default="")
    sub.add_parser("ping")
    a = p.parse_args(argv)
    cfg, _ = cc.active_config(a.config)
    try:
        st = settings(cfg)
        if a.cmd == "list":
            since = cc.since(a.since, cc.window_days(cfg))
            out = {"source": "loki", "since": since.isoformat(), "selector": st["selector"],
                   "candidates": list_groups(st, since, samples=a.samples)}
        elif a.cmd == "check":
            out = check(st, a.since_epoch, a.window_s, a.max_new, a.release, a.release_label, a.extra)
        else:
            info = get(st, "/loki/api/v1/status/buildinfo", None)
            out = {"ok": True, "url": st["url"], "selector": st["selector"], "version": info.get("version")}
    except LokiError as e:
        print(json.dumps({"source": "loki", "status": "unavailable", "error": str(e)}))
        return 2
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # Windows cp1252 consoles cannot print the docstring
    sys.exit(main())
