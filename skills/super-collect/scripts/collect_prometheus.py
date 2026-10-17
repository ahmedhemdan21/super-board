#!/usr/bin/env python3
"""super-collect source `prometheus`: firing alerts and down scrape targets, read-only.

Stdlib only. Config `collect.prometheus`:
  url         Prometheus base URL, e.g. http://prometheus:9090 (required)
  job_filter  optional regex; only alerts / targets whose `job` label matches
  transport   {"kind":"http"} (default) or {"kind":"ssh","host":…,"docker_network":…}

    collect_prometheus.py list [--since 14d] [--config cfg.json]
    collect_prometheus.py ping

`list` prints {"source":"prometheus","candidates":[…]}: one per firing alert
(`GET /api/v1/alerts`, fingerprint `prom|alert|<alertname>|<labels hash>`) and one per
down target (`GET /api/v1/targets?state=active`, `prom|target|<job>|<instance hash>`).
Both are current state: --since is only echoed. Pending alerts are left out.
Unreachable → {"status":"unavailable"} and exit 2.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_common as cc  # noqa: E402


class PromError(RuntimeError):
    pass


def settings(cfg):
    s = cc.collect_block(cfg, "prometheus")
    url = (s.get("url") or "").strip()
    if not url:
        raise PromError("collect.prometheus.url is required (e.g. http://prometheus:9090).")
    try:
        job = re.compile(s["job_filter"]) if s.get("job_filter") else None
    except re.error as e:
        raise PromError(f"collect.prometheus.job_filter is not a regex: {e}") from None
    return {"url": url, "job": job, "transport": s.get("transport") or {"kind": "http"}}


def get(st, path, params=None):
    try:
        out = cc.get_json(st["url"], path, params, st["transport"], label="Prometheus")
    except cc.FetchError as e:
        raise PromError(str(e)) from None
    if not isinstance(out, dict) or out.get("status") != "success":
        raise PromError(f"Prometheus API error on {path}: {str(out)[:200]}")
    return out.get("data") or {}


def keep(st, labels):
    return not st["job"] or bool(st["job"].search(labels.get("job", "")))


def label_key(labels, drop=("alertname",)):
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if k not in drop)


def list_candidates(st):
    out = []
    for a in get(st, "/api/v1/alerts").get("alerts") or []:
        labels = a.get("labels") or {}
        if a.get("state") != "firing" or not keep(st, labels):
            continue
        name = labels.get("alertname", "alert")
        ann = a.get("annotations") or {}
        out.append({"fingerprint": cc.fingerprint("prom", "alert", name, label_key(labels) or "-"),
                    "kind": "alert", "title": ann.get("summary") or name, "alertname": name,
                    "severity": labels.get("severity"), "labels": labels,
                    "description": ann.get("description"), "activeAt": a.get("activeAt"),
                    "value": a.get("value")})
    for t in get(st, "/api/v1/targets", {"state": "active"}).get("activeTargets") or []:
        labels = t.get("labels") or {}
        if t.get("health") != "down" or not keep(st, labels):
            continue
        job = labels.get("job", "job")
        out.append({"fingerprint": cc.fingerprint("prom", "target", job, labels.get("instance") or t.get("scrapeUrl") or "-"),
                    "kind": "target", "title": f"scrape target {job} is down", "job": job,
                    "instance": labels.get("instance"), "scrapeUrl": t.get("scrapeUrl"),
                    "lastError": t.get("lastError"), "lastScrape": t.get("lastScrape")})
    sev = {"critical": 0, "error": 1, "warning": 2}
    out.sort(key=lambda c: (c["kind"] != "alert", sev.get(c.get("severity") or "", 3)))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("list")
    lp.add_argument("--since")
    sub.add_parser("ping")
    a = p.parse_args(argv)
    cfg, _ = cc.active_config(a.config)
    try:
        st = settings(cfg)
        if a.cmd == "list":
            out = {"source": "prometheus", "since": a.since, "candidates": list_candidates(st)}
        else:
            info = get(st, "/api/v1/status/buildinfo")
            out = {"ok": True, "url": st["url"], "version": info.get("version")}
    except PromError as e:
        print(json.dumps({"source": "prometheus", "status": "unavailable", "error": str(e)}))
        return 2
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # Windows cp1252 consoles cannot print the docstring
    sys.exit(main())
