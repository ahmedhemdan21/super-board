"""Shared helpers for the super-collect fetchers. Stdlib only.

- active_config(): the project's super-board config (`.claude/super-board/active`
  -> `configs/<slug>.json`), or the file named by --config.
- secret(name): an env var, else the nearest `.env` / `.env.local` walking up
  from cwd. Only the named key is read; values are never printed.
- since(arg, default_days): `--since 30d` / `--since 2026-09-01` -> aware UTC datetime.
- fingerprint(source, *parts): the stable dedupe key super-collect-file.sh stamps.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re

UTC = dt.timezone.utc


def _find_up(rel):
    d = os.getcwd()
    while True:
        p = os.path.join(d, rel)
        if os.path.exists(p):
            return p
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def active_config(path=None):
    """Return (config dict, path). Empty dict when no config is found."""
    if not path:
        ptr = _find_up(os.path.join(".claude", "super-board", "active"))
        if ptr:
            with open(ptr, encoding="utf-8") as f:
                slug = f.read().strip()
            cand = os.path.join(os.path.dirname(ptr), "configs", f"{slug}.json")
            path = cand if os.path.exists(cand) else None
    if not path:
        return {}, None
    with open(path, encoding="utf-8") as f:
        return json.load(f), path


def collect_block(cfg, source=None):
    block = (cfg or {}).get("collect") or {}
    return block.get(source) or {} if source else block


def secret(name):
    if os.environ.get(name):
        return os.environ[name]
    d = os.getcwd()
    while True:
        for fname in (".env", ".env.local"):
            p = os.path.join(d, fname)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        if k.strip().removeprefix("export ").strip() == name:
                            v = v.strip().strip('"').strip("'")
                            if v:
                                return v
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent


def since(arg, default_days=14, now=None):
    now = now or dt.datetime.now(UTC)
    if not arg:
        return now - dt.timedelta(days=int(default_days))
    m = re.fullmatch(r"(\d+)([dhw])", arg.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now - {"d": dt.timedelta(days=n), "h": dt.timedelta(hours=n), "w": dt.timedelta(weeks=n)}[unit]
    d = dt.datetime.fromisoformat(arg.strip().replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def window_days(cfg):
    return int(collect_block(cfg).get("window_days") or 14)


_NOISE = re.compile(r"0x[0-9a-f]+|[0-9a-f]{8,}|\d+")


def normalize(text):
    """Collapse ids, numbers and whitespace so one cause keeps one key across runs."""
    return re.sub(r"\s+", " ", _NOISE.sub("N", (text or "").lower())).strip()[:300]


def fingerprint(source, *parts):
    """`posthog|exception|<hash>` etc. Free text is hashed so the key never holds quotes."""
    safe = []
    for p in parts:
        p = str(p)
        safe.append(p if re.fullmatch(r"[A-Za-z0-9_.:/-]{1,60}", p) else hashlib.sha1(normalize(p).encode()).hexdigest()[:12])
    return "|".join([source, *safe])


# ── Transport for self-hosted telemetry (Loki, Prometheus). GET only.
#
# {"kind": "http"} (default): urllib straight to the URL.
# {"kind": "ssh", "host": "dev-box", "docker_network": "app-net"}: the stack is only
# reachable inside a Docker network on a remote host, so the GET runs there as
#   ssh -o BatchMode=yes <host> docker run --rm --network <net> curlimages/curl -fsS --max-time 20 '<url>'
# Every query parameter is URL-encoded locally, the URL is checked against a strict
# character set, host / network against a name pattern, and the URL is single-quoted
# for the remote shell, so nothing from config reaches a shell unquoted.

class FetchError(RuntimeError):
    pass


_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,120}")
_SAFE_URL = re.compile(r"https?://[A-Za-z0-9._~:/?&=%+-]+")
_SAFE_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,200}")


def build_url(base, path, params=None):
    import urllib.parse
    url = (base or "").rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    if not _SAFE_URL.fullmatch(url):
        raise FetchError(f"refusing URL with unsafe characters: {url[:80]}")
    return url


def ssh_argv(transport, url):
    host, net = transport.get("host") or "", transport.get("docker_network") or ""
    if not (_SAFE_NAME.fullmatch(host) and _SAFE_NAME.fullmatch(net)):
        raise FetchError("transport ssh needs host and docker_network (letters, digits, . _ @ -)")
    image = transport.get("image") or "curlimages/curl"
    if not _SAFE_IMAGE.fullmatch(image):
        raise FetchError("transport.image has unsafe characters")
    if not _SAFE_URL.fullmatch(url):
        raise FetchError("refusing URL with unsafe characters")
    # ssh joins the remote args with spaces for the remote shell: quote the URL there.
    return ["ssh", "-o", "BatchMode=yes", host, "docker", "run", "--rm", "--network", net,
            image, "-fsS", "--max-time", "20", "'" + url + "'"]


def get_json(base, path, params=None, transport=None, timeout=25, label="source"):
    """GET base+path?params and parse JSON, over the configured transport."""
    import subprocess
    import urllib.error
    import urllib.request
    url = build_url(base, path, params)
    kind = (transport or {}).get("kind") or "http"
    if kind == "http":
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise FetchError(f"{label} HTTP {e.code} on {path}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise FetchError(f"{label} unreachable on {path}: {getattr(e, 'reason', e)}") from None
    if kind != "ssh":
        raise FetchError(f"transport.kind must be http or ssh (got {kind})")
    try:
        p = subprocess.run(ssh_argv(transport, url), capture_output=True, text=True, timeout=timeout + 20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FetchError(f"{label} unreachable over ssh: {e}") from None
    if p.returncode != 0:
        last = (p.stderr or "").strip().splitlines()[-1:] or [f"exit {p.returncode}"]
        raise FetchError(f"{label} unreachable over ssh on {path}: {last[0][:200]}")
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise FetchError(f"{label} sent non-JSON over ssh on {path}") from None


_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_TS = re.compile(r"\d{4}-\d\d-\d\d[t ]\d\d:\d\d:\d\d([.,]\d+)?(z|[+-]\d\d:?\d\d)?", re.I)


def signature(line):
    """One error line → a stable signature (GUIDs, timestamps, numbers collapsed)."""
    text = line or ""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            for k in ("exception.type", "exception_type", "message", "msg", "body", "Body", "error"):
                if obj.get(k):
                    text = str(obj[k])
                    break
    except ValueError:
        pass
    return normalize(_TS.sub("T", _GUID.sub("G", text)))[:160]
