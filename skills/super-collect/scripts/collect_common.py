"""Shared helpers for the super-collect fetchers. Stdlib only.

- active_config(): the project's super-board config (`.claude/super-board/active`
  -> `configs/<slug>.json`), or the file named by --config.
- secret(name): an env var, else the nearest `.env.local` / `.env` walking up
  from cwd (`.env.local` wins). Only the named key is read; values are never printed.
- secret_file(repo_root): where collector keys must be written (`.env.local` when
  `.env` is tracked by git).
- since(arg, default_days): `--since 30d` / `--since 2026-09-01` -> aware UTC datetime.
- fingerprint(source, *parts): the stable dedupe key super-collect-file.sh stamps.
- cross_signature(type, message, top_frame): the key shared across sources for one
  exception (`<!-- sb-signature: … -->`); stack_top_frame / frames_top_frame find the frame.
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
        # No run in progress (the `active` marker exists only during a run): a project with
        # exactly one config uses it, as super-board-status.py does. Several → ambiguous, none.
        cdir = _find_up(os.path.join(".claude", "super-board", "configs"))
        if cdir and os.path.isdir(cdir):
            only = sorted(f for f in os.listdir(cdir) if f.endswith(".json"))
            if len(only) == 1:
                path = os.path.join(cdir, only[0])
    if not path:
        return {}, None
    with open(path, encoding="utf-8") as f:
        return json.load(f), path


def collect_block(cfg, source=None):
    block = (cfg or {}).get("collect") or {}
    return block.get(source) or {} if source else block


# Collector token names. scripts/super-board-setup.py keeps a copy (it ships to .claude/bin
# and cannot import this file); tests/test_collect_telemetry.py checks the two agree.
SECRET_KEYS = ("SENTRY_AUTH_TOKEN", "POSTHOG_PERSONAL_API_KEY", "SLACK_BOT_TOKEN")
SECRET_PATTERN = re.compile(r"(?:LOKI|PROMETHEUS)_\w*(?:TOKEN|KEY|PASSWORD|SECRET)\w*")


def secret_file(repo_root):
    """The env file collector keys belong in. A tracked `.env` is rewritten by any branch
    switch (and pushed with the next commit), so then it is always `.env.local`; also
    `.env.local` when `.env` is absent and `.env.local` exists. Else `.env`."""
    import subprocess
    env, local = os.path.join(repo_root, ".env"), os.path.join(repo_root, ".env.local")
    try:
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"], cwd=repo_root,
                                 capture_output=True, text=True).returncode == 0
    except OSError:
        tracked = False
    if tracked or (not os.path.exists(env) and os.path.exists(local)):
        return local
    return env


def secret_hint():
    """The env file name to tell the user to add a key to (`.env` or `.env.local`)."""
    return os.path.basename(secret_file(os.getcwd()))


def secret(name):
    """An env var, else the nearest env file walking up; `.env.local` beats `.env`."""
    if os.environ.get(name):
        return os.environ[name]
    d = os.getcwd()
    while True:
        for fname in (".env.local", ".env"):
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
    # The URL never goes on the remote command line: curl reads it from stdin (`-K -`, see
    # curl_config). Quoting it for the remote shell is not portable: Python on Windows starting
    # Git's MSYS ssh.exe has its single quotes stripped, the remote shell then cuts the URL at
    # the first `&`, and Loki silently answers the default last hour (seen 2026-10-18).
    return ["ssh", "-o", "BatchMode=yes", host, "docker", "run", "-i", "--rm", "--network", net,
            image, "-fsS", "--max-time", "20", "-K", "-"]


def curl_config(url):
    """The curl config ssh_argv's remote curl reads from stdin: one quoted `url` line."""
    if not _SAFE_URL.fullmatch(url):
        raise FetchError("refusing URL with unsafe characters")
    return 'url = "' + url.replace("\\", "\\\\").replace('"', '\\"') + '"\n'


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
        p = subprocess.run(ssh_argv(transport, url), input=curl_config(url), capture_output=True,
                           text=True, timeout=timeout + 20)
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


# ── Cross-source signature: one crash seen by Sentry, Loki and PostHog keys the same.
#
# Each source keeps its own fingerprint (`err|sentry|<id>`, `loki|<group>|<sig>`, …) for its
# own dedupe; cross_signature is the shared key super-collect-file.sh stamps as
# `<!-- sb-signature: <hash> -->` so a second source appends evidence instead of a new card.
# Inputs, normalized so every source agrees:
#   type       last dotted segment, lower-case (Sentry .NET says `InvalidOperationException`,
#              OTel says `System.InvalidOperationException`)
#   message    through signature() (ids, numbers, timestamps collapsed)
#   top frame  the innermost frame NOT in a framework/vendor namespace (VENDOR_FRAMES).
#              .NET: `Namespace.Class.Method` (compiler async/lambda names folded back);
#              JS: `function@file-basename` (line/column dropped: they move every deploy).
# Empty when there is neither a type nor a frame: bare messages never cross-match.

# The one list of framework/vendor frames. A frame is skipped when its symbol, module or
# file STARTS WITH a prefix, or its file CONTAINS a path marker.
VENDOR_PREFIXES = (
    # .NET runtime and common framework/library namespaces
    "System.", "Microsoft.", "Npgsql.", "Grpc.", "Polly.", "Newtonsoft.", "Serilog.",
    "OpenTelemetry.", "Swashbuckle.", "MediatR.", "FluentValidation.", "Hangfire.", "Wolverine.",
    "JasperFx.", "Marten.", "Yarp.", "lambda_method",
    # JS runtimes
    "node:", "internal/", "node_modules/", "webpack/", "<anonymous>", "[native code]", "native ",
)
VENDOR_PATHS = ("/node_modules/", "/react-dom", "/react/", "/scheduler/", "/next/dist/", "next/dist/",
                "react-dom.", "/webpack/")


def is_vendor_frame(*parts):
    """True when any part (symbol, module, file) is framework/vendor code."""
    for p in parts:
        p = str(p or "").replace("\\", "/")
        if p and (p.startswith(VENDOR_PREFIXES) or any(v in p for v in VENDOR_PATHS)):
            return True
    return False


_ASYNC = re.compile(r"\.<([A-Za-z_]\w*)>[a-z]__[\w|]*")  # .<PlaceOrder>d__5 / .<Get>b__0 / g__Core|4_0
_DISPLAY = re.compile(r"\.<>c(__DisplayClass[\w]*)?")    # lambda closure classes


def dotnet_frame(symbol, module=None):
    """`Ns.Class.Method` for a .NET frame. `symbol` is a stack-trace symbol
    (`Ns.Class.<Method>d__5.MoveNext`) or a Sentry function (`Method`, `Class.Method`,
    `async Task<T> Class.Method(int id)`); `module` is Sentry's `Ns.Class`."""
    s = str(symbol or "").strip()
    s = re.sub(r"\(.*$", "", s)                        # arguments
    s = s.split()[-1] if s.split() else ""             # return type / async prefix
    s = re.sub(r"\[[^\]]*\]", "", s)                   # generic args Foo[T]
    s = _ASYNC.sub(r".\1", s)
    s = _DISPLAY.sub("", s)
    s = re.sub(r"\.MoveNext$", "", s)
    s = re.sub(r"`\d+", "", s)
    mod = re.sub(r"`\d+", "", str(module or "").strip())
    if mod and not s.startswith(mod + "."):
        method = s.rsplit(".", 1)[-1]
        s = f"{mod}.{method}" if method else mod
    return s.strip(".")


def js_frame(function, filename):
    """`function@basename` for a JS frame; `basename` alone when the function is anonymous."""
    fn = str(function or "").strip()
    if fn in ("?", "<anonymous>", "Object.<anonymous>"):
        fn = ""
    fn = re.sub(r"^(async |new )", "", fn)
    f = re.sub(r"[?#].*$", "", str(filename or "").strip())
    f = re.sub(r":\d+(:\d+)?$", "", f)
    base = f.replace("\\", "/").rsplit("/", 1)[-1]
    return f"{fn}@{base}" if fn and base else (fn or base)


_NET_AT = re.compile(r"^\s*at\s+([^\s(]+)\s*\(")
_JS_AT = re.compile(r"^\s*at\s+(?:(.+?)\s+\()?(\S+?):\d+(?::\d+)?\)?\s*$")


def stack_top_frame(stacktrace):
    """The innermost in-app frame of a rendered .NET or JS stack trace, canonical form.
    .NET inner-exception sections (` ---> … --- End of inner exception stack trace ---`)
    are skipped so the frame belongs to the outer exception, as Sentry and OTel report it."""
    inner = False
    for line in str(stacktrace or "").splitlines():
        if " ---> " in line or line.lstrip().startswith("--->"):
            inner = True
            continue
        if "--- End of inner exception stack trace ---" in line:
            inner = False
            continue
        if inner:
            continue
        m = _NET_AT.match(line)
        if m and "." in m.group(1) and "/" not in m.group(1):
            sym = m.group(1)
            if not is_vendor_frame(sym):
                return dotnet_frame(sym)
            continue
        m = _JS_AT.match(line)
        if m:
            fn, f = m.group(1) or "", m.group(2)
            if not is_vendor_frame(f, fn):
                return js_frame(fn, f)
    return ""


def frames_top_frame(frames, dotnet=False):
    """Innermost in-app frame of a Sentry / PostHog frame list. Both send frames outermost
    first (innermost last), so the list is read backwards."""
    for f in reversed(frames or []):
        if not isinstance(f, dict):
            continue
        fn = f.get("function") or f.get("resolved_name") or f.get("mangled_name") or ""
        if dotnet or str(f.get("lang") or "").lower() in ("csharp", "dotnet"):
            key = dotnet_frame(fn, f.get("module"))
            if key and not is_vendor_frame(key):
                return key
            continue
        fname = f.get("filename") or f.get("abs_path") or f.get("absPath") or f.get("source") or ""
        if is_vendor_frame(fname, fn, f.get("module")):
            continue
        key = js_frame(fn, fname)
        if key:
            return key
    return ""


def short_type(exc_type):
    t = str(exc_type or "").strip()
    t = re.sub(r"`\d+.*$", "", t)
    return t.rsplit(".", 1)[-1].lower()


def cross_signature(exc_type, message, top_frame):
    """12-hex key shared by every source for one exception; "" without type AND frame."""
    t, frame = short_type(exc_type), str(top_frame or "").strip()
    if not t and not frame:
        return ""
    raw = "|".join((t, signature(message or ""), frame))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


if __name__ == "__main__":
    # `python3 collect_common.py secret-file [repo_root]` prints the file to write keys to.
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "secret-file":
        print(secret_file(os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else os.getcwd())))
        sys.exit(0)
    print("usage: collect_common.py secret-file [repo_root]", file=sys.stderr)
    sys.exit(64)
