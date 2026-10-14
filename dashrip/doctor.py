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
from collections.abc import Callable

from . import config as C

Check = tuple[str, Callable[[], tuple[str, str]]]


def _run(args: list[str], timeout: int = 20) -> str:
    """Run a local command and return its output."""
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return (r.stdout or r.stderr or "").strip()


def _check(name: str, fn: Callable[[], tuple[str, str]]) -> bool:
    """Run ``fn() -> (status, detail)`` and print one checklist line."""
    try:
        status, detail = fn()
    except Exception as e:  # a broken check must not kill the report
        status, detail = "err", f"{type(e).__name__}: {e}"
    mark = {"ok": "[ OK ]", "warn": "[ .. ]", "err": "[FAIL]"}.get(status, "[ ?  ]")
    print(f"  {mark} {name:<12} {detail}")
    return status == "ok"


# ------------------------- individual checks -------------------------

def _ck_python() -> tuple[str, str]:
    """Interpreter version."""
    return "ok", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _ck_pywidevine() -> tuple[str, str]:
    """pywidevine package version."""
    import pywidevine
    return "ok", getattr(pywidevine, "__version__", "unknown")


def _ck_device(cfg: dict) -> tuple[str, str]:
    """Configured .wvd device file: level and system id."""
    p = cfg.get("wvd")
    if not p:
        return "err", "not set (run `dashrip init`)"
    if not os.path.isfile(p):
        return "err", f"file not found: {p}"
    from pywidevine.device import Device
    d = Device.load(p)
    lvl = {1: "L1", 3: "L3"}.get(d.security_level, f"L{d.security_level}")
    sid = str(d.system_id or 0).replace("-", "")
    return "ok", "{}, sys_id ...{}".format(lvl, sid[-4:] or "?")


def _tool(cfg: dict, which: str, arg: str) -> tuple[str, str]:
    """First line of a tool version banner."""
    exe = C.resolve_tool(cfg.get(which, ""))
    if not exe:
        return "err", "not found (set its path in the config or install it)"
    line = _run([exe, arg])
    first = (line.splitlines() or [""])[0].strip()
    return "ok", (first[:80] or exe)


def _ck_requests() -> tuple[str, str]:
    """requests package version."""
    import requests
    return "ok", requests.__version__


def _ck_output(cfg: dict) -> tuple[str, str]:
    """Output directory exists and accepts writes."""
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
        return "err", f"not writable: {e}"
    return "ok", p


def _ck_cookie(cfg: dict) -> tuple[str, str]:
    """Session cookie presence, format and expiry."""
    tok = C.cookie_token(cfg.get("cookie", ""))
    if not tok:
        return "err", "not set"
    exp = C.cookie_expiry(tok)
    if exp is None:
        return "warn", "present but not a parseable st= JWT"
    if exp < time.time():
        return "err", "EXPIRED (capture a fresh one from the browser)"
    return "ok", "valid until {}".format(time.strftime("%Y-%m-%d", time.gmtime(exp)))


def _ck_playback(cfg: dict) -> tuple[str, str]:
    """Playback template (url and body) present in the config."""
    has_url = bool(C.get(cfg, "playback.url"))
    has_body = bool(C.get(cfg, "playback.body"))
    if has_url and has_body:
        return "ok", "set"
    missing = "playback.url" if not has_url else "playback.body"
    return "err", f"{missing} missing (run `dashrip init`)"


def _ck_cms(cfg: dict) -> tuple[str, str]:
    """Authenticated reachability probe against the CMS."""
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
    return "err", f"HTTP {r.status_code} (cookie missing or rejected)"


# ------------------------- the report -------------------------

def doctor(cfg: dict | None = None) -> bool:
    """Run every check in order and print the checklist. Returns True
    when all of them pass."""
    cfg = cfg if cfg is not None else C.load()
    print(f"dashrip doctor  (config: {C.path()})")
    print()

    checks: list[Check] = [
        ("python", _ck_python),
        ("pywidevine", _ck_pywidevine),
        ("device", lambda: _ck_device(cfg)),
        ("ffmpeg", lambda: _tool(cfg, "ffmpeg", "-version")),
        ("ffprobe", lambda: _tool(cfg, "ffprobe", "-version")),
        ("shaka", lambda: _tool(cfg, "shaka", "--version")),
        ("requests", _ck_requests),
        ("output", lambda: _ck_output(cfg)),
        ("cookie", lambda: _ck_cookie(cfg)),
        ("playback", lambda: _ck_playback(cfg)),
        ("cms", lambda: _ck_cms(cfg)),
    ]
    results = [_check(name, fn) for name, fn in checks]

    failed = results.count(False)
    print()
    if failed == 0:
        print("  -> ALL CHECKS PASSED")
    else:
        print(f"  -> {failed} CHECK(S) NEED ATTENTION")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if doctor() else 1)
