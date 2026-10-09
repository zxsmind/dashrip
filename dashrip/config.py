"""Configuration handling for dashrip.

The user's setup is stored in ``<repo>/config/dashrip.json`` (plain JSON, so
the standard library is enough to read and write it).  The file is git-
ignored, is created by ``dashrip init``, and can be edited by hand
afterwards.

Layout
------
wvd                 str  path to a Widevine device file (``.wvd``)
cookie              str  platform session cookie: a raw cookie line
                         (``k1=v1; k2=v2``), a URL-encoded ``st=...`` pair,
                         or the bare ``st=`` JWT itself
ffmpeg              str  path to the ffmpeg executable (or a bare command name)
ffprobe             str  path to the ffprobe executable (or a bare command name)
shaka               str  path to the shaka-packager executable (or a bare name)
outdir              str  default output directory; the standard ``Movies/``
                         and ``Series/`` trees are created underneath it
                         (empty = current directory)
bwlimit_kbps        int  download speed cap in kbit/s (0 = unlimited)
playback            dict template of the platform *playback-info* request,
                     captured once from the user's browser (HAR export):
      url                playback endpoint URL
      headers            request headers WITHOUT the Cookie header
      body               JSON request body as a string
      template_edit_id   the editId value found in the body; it is replaced
                         with the per-item editId on every request
cms_headers         dict headers for CMS/catalog requests WITHOUT the Cookie
                     header (the cookie is added automatically)
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil

__all__ = [
    "CONFIG_NAME",
    "load",
    "save",
    "path",
    "repo_root",
    "get",
    "set",
    "validate",
    "cookie_token",
    "cookie_expiry",
    "resolve_tool",
]

CONFIG_NAME = "dashrip.json"


def repo_root() -> str:
    """Absolute path of the repository root (the parent of this package)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def path() -> str:
    """Absolute path of the user config file."""
    return os.path.join(repo_root(), "config", CONFIG_NAME)


def _defaults() -> dict:
    return {
        "wvd": "",
        "cookie": "",
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "shaka": "shaka-packager",
        "outdir": "",
        "bwlimit_kbps": 0,
        "playback": {
            "url": "",
            "headers": {},
            "body": "",
            "template_edit_id": "",
        },
        "cms_headers": {},
        "defaults": {"audio": ["orig"], "subs": "all"},
    }


def load(path_: str | None = None) -> dict:
    """Load the config merged over defaults.  A missing file yields defaults."""
    cfg = _defaults()
    p = path_ or path()
    if os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for key, val in data.items():
            if key in ("playback", "defaults") and isinstance(val, dict):
                for k2, v2 in val.items():
                    cfg[key][k2] = v2
            else:
                cfg[key] = val
    return cfg


def save(cfg: dict, path_: str | None = None) -> str:
    """Write the config file (pretty-printed).  Returns the file path."""
    p = path_ or path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return p


def get(cfg: dict, dotted: str, default=None):
    """Read a dotted key, e.g. ``get(cfg, "playback.url")``."""
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set(cfg: dict, dotted: str, value) -> None:
    """Set a dotted key, e.g. ``set(cfg, "playback.url", "...")``."""
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def cookie_token(cookie: str) -> str:
    """Normalise a session cookie to the bare ``st=`` token when present.

    Accepts a full cookie line (``k1=v1; k2=v2``), a URL-encoded form, or the
    bare JWT.  Returns the token with its ``st=`` prefix, or ``""``.
    """
    if not cookie:
        return ""
    raw = cookie.strip().replace("%3D", "=").replace("%3B", ";")
    m = re.search(r"st=([A-Za-z0-9\-_\.]+)", raw)
    if m:
        return "st=" + m.group(1)
    if raw.count(".") == 2 and raw.startswith("eyJ"):
        return "st=" + raw
    return ""


def cookie_expiry(token: str) -> int | None:
    """Return the ``exp`` (epoch seconds) of the ``st=`` JWT, or ``None``."""
    m = re.match(r"st=([A-Za-z0-9\-_\.]+)$", token or "")
    if not m:
        return None
    parts = m.group(1).split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
    exp = data.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def resolve_tool(name: str) -> str | None:
    """Resolve an executable: an existing path, or a PATH lookup if bare."""
    if not name:
        return None
    if os.path.isfile(name):
        return os.path.abspath(name)
    return shutil.which(name)


def validate(cfg: dict) -> list[str]:
    """Return a list of human-readable problems (empty list = all good)."""
    problems: list[str] = []
    if not cfg.get("wvd"):
        problems.append("wvd: not set")
    elif not os.path.isfile(cfg["wvd"]):
        problems.append("wvd: file not found: %s" % cfg["wvd"])
    if not cfg.get("cookie"):
        problems.append("cookie: not set")
    if not get(cfg, "playback.url"):
        problems.append("playback.url: not set (run `dashrip init`)")
    if not get(cfg, "playback.body"):
        problems.append("playback.body: not set (run `dashrip init`)")
    for tool in ("ffmpeg", "ffprobe", "shaka"):
        if not resolve_tool(cfg.get(tool, "")):
            problems.append("%s: not found (set the path or install it)" % tool)
    return problems
