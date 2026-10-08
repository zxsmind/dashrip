"""Pre-flight health check: a green/red checklist for every dependency.

Run with ``python -m dashrip doctor``.  Exits 0 when all required checks
pass, 1 otherwise.  Each check is independent, so a missing optional piece
never aborts the rest of the report.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from . import config as C


def _run(args, timeout=20) -> str:
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return (r.stdout or r.stderr or "").strip()


def _check(name: str, fn) -> bool:
    """Run ``fn() -> (status, detail)`` and print one checklist line."""
    try:
        status, detail = fn()
    except Exception as e:  # a broken check must not kill the report
        status, detail = "err", "%s: %s" % (type(e).__name__, e)
    mark = {"ok": "[ OK ]", "warn": "[ .. ]", "err": "[FAIL]"}.get(status, "[ ?  ]")
    print("  %s %-12s %s" % (mark, name, detail))
    return status == "ok"


def doctor(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else C.load()
    print("dashrip doctor  (config: %s)" % C.path())
    print()

    results = []

    def py():
        return "ok", "%d.%d.%d" % sys.version_info[:3]

    results.append(_check("python", py))

    def pw():
        import pywidevine
        return "ok", getattr(pywidevine, "__version__", "unknown")

    results.append(_check("pywidevine", pw))

    def dev():
        p = cfg.get("wvd")
        if not p:
            return "err", "not set (run `dashrip init`)"
        if not os.path.isfile(p):
            return "err", "file not found: %s" % p
        from pywidevine.device import Device
        d = Device.load(p)
        lvl = {1: "L1", 3: "L3"}.get(d.security_level, "L%d" % d.security_level)
        sid = str(d.system_id or 0).replace("-", "")
        return "ok", "%s, sys_id ...%s" % (lvl, sid[-4:] or "?")

    results.append(_check("device", dev))

    def tool(which: str, arg: str):
        exe = C.resolve_tool(cfg.get(which, ""))
        if not exe:
            return "err", "not found (set its path in the config or install it)"
        line = _run([exe, arg])
        first = (line.splitlines() or [""])[0].strip()
        return "ok", (first[:80] or exe)

    results.append(_check("ffmpeg", lambda: tool("ffmpeg", "-version")))
    results.append(_check("ffprobe", lambda: tool("ffprobe", "-version")))
    results.append(_check("shaka", lambda: tool("shaka", "--version")))

    def req():
        import requests
        return "ok", requests.__version__

    results.append(_check("requests", req))

    def out():
        od = cfg.get("outdir")
        if not od:
            return "warn", "not set (files would land in the current directory)"
        p = os.path.abspath(od)
        os.makedirs(p, exist_ok=True)
        t = os.path.join(p, ".dashrip_write_test")
        try:
            with open(t, "w") as fh:
                fh.write("ok")
            os.remove(t)
        except OSError as e:
            return "err", "not writable: %s" % e
        return "ok", p

    results.append(_check("output", out))

    def ck():
        tok = C.cookie_token(cfg.get("cookie", ""))
        if not tok:
            return "err", "not set"
        exp = C.cookie_expiry(tok)
        if exp is None:
            return "warn", "present but not a parseable st= JWT"
        if exp < time.time():
            return "err", "EXPIRED (capture a fresh one from the browser)"
        return "ok", "valid until %s" % time.strftime("%Y-%m-%d", time.gmtime(exp))

    results.append(_check("cookie", ck))

    def pb():
        has_url = bool(C.get(cfg, "playback.url"))
        has_body = bool(C.get(cfg, "playback.body"))
        if has_url and has_body:
            return "ok", "set"
        missing = "playback.url" if not has_url else "playback.body"
        return "err", "%s missing (run `dashrip init`)" % missing

    results.append(_check("playback", pb))

    def cms():
        # Authenticated probe: a guaranteed-missing content id.  A 404
        # proves the request passed authentication; 401/403 means the
        # cookie is missing or rejected.
        import requests
        from . import core as K
        tok = C.cookie_token(cfg.get("cookie", ""))
        hdrs = dict(C.get(cfg, "cms_headers", {}) or {})
        if tok:
            hdrs["Cookie"] = tok
        url = (K.CMS_BASE + "/cms/routes/show/"
               "00000000-0000-0000-0000-000000000000?include=default")
        r = requests.get(url, headers=hdrs, timeout=20)
        if r.status_code in (200, 404):
            return "ok", "reachable, cookie authenticated" if tok \
                else "reachable (no cookie sent)"
        return "err", "HTTP %d (cookie missing or rejected)" % r.status_code

    results.append(_check("cms", cms))

    failed = results.count(False)
    print()
    if failed == 0:
        print("  -> ALL CHECKS PASSED")
    else:
        print("  -> %d CHECK(S) NEED ATTENTION" % failed)
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if doctor() else 1)
