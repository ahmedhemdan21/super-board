#!/usr/bin/env python3
"""super-collect source `prometheus`: firing alerts and down scrape targets, read-only;
plus the post-deploy rate check (`check`, config deploy.regression_watch).

Stdlib only. Config `collect.prometheus`:
  url         Prometheus base URL, e.g. http://prometheus:9090 (required)
  job_filter  optional regex; only alerts / targets whose `job` label matches
  transport   {"kind":"http"} (default) or {"kind":"ssh","host":…,"docker_network":…}

    collect_prometheus.py list [--since 14d] [--config cfg.json]
    collect_prometheus.py check --since-epoch <deploy unix ts> [--window-s 1800] [--config cfg.json]
    collect_prometheus.py ping

`list` prints {"source":"prometheus","candidates":[…]}: one per firing alert
(`GET /api/v1/alerts`, fingerprint `prom|alert|<alertname>|<labels hash>`) and one per
down target (`GET /api/v1/targets?state=active`, `prom|target|<job>|<instance hash>`).
Both are current state: --since is only echoed. Pending alerts are left out.
Unreachable → {"status":"unavailable"} and exit 2.

`check` is the regression watch (it mirrors `collect_loki.py check`). Config
`deploy.regression_watch`: promql_error_rate (required), promql_request_rate (optional),
window_s (default 1800), min_ratio (default 2.0), min_abs (default 0.05), loki_error_ratio
(default 2.0). It averages `/api/v1/query_range` over [deploy-window, deploy] ("before") and
[deploy, deploy+window] ("after", clipped to now: the caller waits, see
super-board-deploy-wait.sh). The compared value is the error rate, or error/request when
promql_request_rate is set. Prometheus regression = after/before >= min_ratio AND the
after-window error rate >= min_abs errors/s (the low-traffic noise guard: 0.05/s is 90
errors in a 30-minute window). A zero before-value with errors after is an infinite ratio.
No series in both windows → "inconclusive": never ok, never regression. When collect.loki
is set, the same two windows are compared on Loki error lines (count_over_time of
selector + error_filter): regression = after/before rate >= loki_error_ratio AND after
errors/s >= min_abs; no log line at all after the deploy → inconclusive.
Overall: regression when either side is; status "ok" when at least one side was
conclusive, "inconclusive" when none was, "unavailable" (exit 2) when neither side answered.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time

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


# ── check: the post-deploy regression watch.

WATCH_DEFAULTS = {"window_s": 1800, "min_ratio": 2.0, "min_abs": 0.05, "loki_error_ratio": 2.0}
_UNSAFE_QUERY = re.compile(r"[\n\r`]")


def watch_settings(cfg):
    w = dict(WATCH_DEFAULTS)
    block = ((cfg or {}).get("deploy") or {}).get("regression_watch") or {}
    w.update({k: v for k, v in block.items() if v is not None})
    if not str(w.get("promql_error_rate") or "").strip():
        raise PromError("deploy.regression_watch.promql_error_rate is required when the watch is enabled")
    for k in ("promql_error_rate", "promql_request_rate"):
        if w.get(k) and _UNSAFE_QUERY.search(str(w[k])):
            raise PromError(f"deploy.regression_watch.{k} has unsafe characters")
    return w


def range_avg(st, query, start, end):
    """Mean of every sample of every series in [start, end]; None when there is no sample."""
    span = max(1, int(end - start))
    step = max(15, span // 120)
    data = get(st, "/api/v1/query_range",
               {"query": query, "start": str(int(start)), "end": str(int(end)), "step": str(step)})
    vals = []
    for row in data.get("result") or []:
        for v in row.get("values") or []:
            try:
                x = float(v[1])
            except (TypeError, ValueError, IndexError):
                continue
            if not (math.isnan(x) or math.isinf(x)):
                vals.append(x)
    return sum(vals) / len(vals) if vals else None


def _ratio(after, before):
    if before:
        return after / before
    return math.inf if after else 0.0


def _num(x):
    """JSON-safe number: inf (zero before-value) is written as null next to beforeZero."""
    return None if x is None or math.isinf(x) else round(x, 6)


def prom_side(st, w, before_win, after_win):
    q_err, q_req = w["promql_error_rate"], w.get("promql_request_rate")
    err_b, err_a = range_avg(st, q_err, *before_win), range_avg(st, q_err, *after_win)
    req_b = req_a = None
    if q_req:
        req_b, req_a = range_avg(st, q_req, *before_win), range_avg(st, q_req, *after_win)
    out = {"before": {"errorRate": _num(err_b), "requestRate": _num(req_b)},
           "after": {"errorRate": _num(err_a), "requestRate": _num(req_a)}}
    if q_req and (not req_b or not req_a):
        # The ratio needs traffic in both windows; zero or missing traffic proves nothing.
        return dict(out, status="inconclusive", regression=False, reason="no request traffic in one or both windows")
    if err_b is None and err_a is None and not q_req:
        return dict(out, status="inconclusive", regression=False, reason="no error-rate series in either window")
    # One window without an error series (counter filtered to 5xx, none happened) = 0.
    eb, ea = err_b or 0.0, err_a or 0.0
    if q_req:
        vb, va, metric = eb / req_b, ea / req_a, "error_ratio"
    else:
        vb, va, metric = eb, ea, "error_rate"
    ratio = _ratio(va, vb)
    over_ratio, over_abs = ratio >= float(w["min_ratio"]), ea >= float(w["min_abs"])
    return dict(out, status="ok", metric=metric, valueBefore=_num(vb), valueAfter=_num(va),
                ratio=_num(ratio), beforeZero=(vb == 0), overRatio=over_ratio, overAbs=over_abs,
                regression=over_ratio and over_abs)


def loki_side(cfg, w, before_win, after_win):
    """Loki error lines over the same two windows. Skipped without collect.loki."""
    if not cc.collect_block(cfg, "loki"):
        return {"status": "skipped", "regression": False, "reason": "collect.loki not configured"}
    import collect_loki as lk
    try:
        st = lk.settings(cfg)
        lines_after = lk.line_count(st, *after_win)
        if lines_after == 0:
            return {"status": "inconclusive", "regression": False, "reason": "no log line after the deploy"}
        q = lk.base_query(st)
        counts = []
        for start, end in (before_win, after_win):
            rng = max(1, int(end - start))
            res = lk.get(st, "/loki/api/v1/query",
                         {"query": f"sum(count_over_time({q} [{rng}s]))", "time": str(int(end))})
            rows = (res.get("data") or {}).get("result") or []
            counts.append(sum(int(float((r.get("value") or [0, "0"])[-1])) for r in rows))
    except lk.LokiError as e:
        return {"status": "unavailable", "regression": False, "reason": str(e)[:200]}
    cb, ca = counts
    rate_b = cb / max(1, int(before_win[1] - before_win[0]))
    rate_a = ca / max(1, int(after_win[1] - after_win[0]))
    ratio = _ratio(rate_a, rate_b)
    over_ratio, over_abs = ratio >= float(w["loki_error_ratio"]), rate_a >= float(w["min_abs"])
    return {"status": "ok", "errorsBefore": cb, "errorsAfter": ca, "linesAfter": lines_after,
            "ratio": _num(ratio), "beforeZero": cb == 0, "overRatio": over_ratio, "overAbs": over_abs,
            "regression": over_ratio and over_abs}


def _ratio_text(side):
    return "inf" if side.get("ratio") is None else f"{side['ratio']:.2f}"


def check(cfg, deploy_ts, window_s=None, now=None):
    w = watch_settings(cfg)
    win = int(window_s or w["window_s"])
    now = now or time.time()
    end = max(deploy_ts + 1, min(now, deploy_ts + win))
    before_win, after_win = (deploy_ts - win, deploy_ts), (deploy_ts, end)
    try:
        prom = prom_side(settings(cfg), w, before_win, after_win)
    except PromError as e:
        prom = {"status": "unavailable", "regression": False, "reason": str(e)[:200]}
    loki = loki_side(cfg, w, before_win, after_win)
    regression = bool(prom.get("regression") or loki.get("regression"))
    if regression or "ok" in (prom["status"], loki["status"]):
        status = "ok"
    elif "inconclusive" in (prom["status"], loki["status"]):
        status = "inconclusive"
    else:
        status = "unavailable"
    parts = []
    if prom["status"] == "ok":
        parts.append(f"prometheus {prom['metric']} {prom['valueBefore']} to {prom['valueAfter']} "
                     f"(x{_ratio_text(prom)}, errors/s after {prom['after']['errorRate'] or 0})")
    else:
        parts.append(f"prometheus {prom['status']}: {prom.get('reason', '')}")
    if loki["status"] == "ok":
        parts.append(f"loki errors {loki['errorsBefore']} to {loki['errorsAfter']} (x{_ratio_text(loki)})")
    elif loki["status"] != "skipped":
        parts.append(f"loki {loki['status']}: {loki.get('reason', '')}")

    def iso(t):
        return dt.datetime.fromtimestamp(t, cc.UTC).isoformat()
    return {"source": "prometheus", "status": status, "deploy": iso(deploy_ts),
            "windowS": int(end - deploy_ts), "configuredWindowS": win, "clipped": int(end - deploy_ts) < win,
            "before": {"start": iso(before_win[0]), "end": iso(before_win[1])},
            "after": {"start": iso(after_win[0]), "end": iso(after_win[1])},
            "minRatio": float(w["min_ratio"]), "minAbs": float(w["min_abs"]),
            "lokiErrorRatio": float(w["loki_error_ratio"]),
            "prometheus": prom, "loki": loki, "regression": regression, "summary": "; ".join(parts)}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("list")
    lp.add_argument("--since")
    ck = sub.add_parser("check")
    ck.add_argument("--since-epoch", type=int, required=True)
    ck.add_argument("--window-s", type=int)
    sub.add_parser("ping")
    a = p.parse_args(argv)
    cfg, _ = cc.active_config(a.config)
    if a.cmd == "check":
        try:
            out = check(cfg, a.since_epoch, a.window_s)
        except PromError as e:
            print(json.dumps({"source": "prometheus", "status": "unavailable", "error": str(e)}))
            return 2
        print(json.dumps(out, indent=1, default=str))
        return 2 if out["status"] == "unavailable" else 0
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
