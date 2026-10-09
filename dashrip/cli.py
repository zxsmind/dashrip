"""dashrip command-line interface.

Commands
--------
    init                  first-time guided setup (dependencies, device,
                          cookie, playback template, output dir)
    doctor                green/red health check of the whole stack
    max [query ...]       rip HBO Max content (movie or series)
    prime                 rip an Amazon Prime capture (assisted workflow)
    sync                  lossless resumable folder transfer helper

Run ``python -m dashrip`` with no arguments for the usage text.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from urllib.parse import urlsplit

from . import config as C
from . import core as K
from . import doctor as D
from . import structure as S

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _workdir():
    d = os.path.join(os.path.expanduser("~"), ".dashrip", "work")
    os.makedirs(d, exist_ok=True)
    return d


def _catalog_path():
    return os.path.join(os.path.expanduser("~"), ".dashrip", "catalog.json")


# ------------------------- text helpers -------------------------

def norm(s):
    """Locale-tolerant normalisation for title matching."""
    s = (s or "").lower()
    for a, b in (("\u00e7", "c"), ("\u011f", "g"), ("\u0131", "i"),
                 ("\u00f6", "o"), ("\u015f", "s"), ("\u00fc", "u")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", s)


def _query_text(q):
    """Extract the meaningful text of a query (URL -> last path segment)."""
    q = (q or "").strip()
    if "://" in q or re.match(r"^www\.", q):
        p = urlsplit(q if "://" in q else "https://" + q)
        segs = [s for s in p.path.split("/") if s]
        if segs:
            return segs[-1]
    return q


# ------------------------- catalog / resolve -------------------------

GENRES = ["action", "adult-animation", "comedy", "crime", "docs-news",
          "drama", "fantasy-sci-fi", "horror", "kids-family"]


def load_catalog(cfg):
    """Title -> show id index, built once from the CMS and cached."""
    p = _catalog_path()
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            if isinstance(d, dict) and d:
                return d
        except Exception:
            pass
    print("Building the CMS index (a few seconds)...")
    names = {}
    hdrs = K._cms_headers(cfg)
    for rt in ["home"] + ["genre/" + g for g in GENRES]:
        try:
            r = requests_get(cfg, K.CMS_BASE + "/cms/routes/" + rt +
                             "?include=default&decorators=badges",
                             headers=hdrs, timeout=60)
            if r.status_code == 200:
                for x in r.json().get("included", []):
                    if x.get("type") == "show":
                        a = x.get("attributes") or {}
                        nm = a.get("name") or a.get("originalName")
                        if nm:
                            names[nm] = x["id"]
        except Exception:
            pass
        time.sleep(random.uniform(1, 3))
    if names:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        json.dump(names, open(p, "w", encoding="utf-8"), indent=1)
        print("  index: %d shows" % len(names))
    return names


def requests_get(cfg, url, **kw):
    import requests
    return requests.get(url, **kw)


def find_show(cfg, query):
    """Return [(name, show_id)] candidates for a URL / UUID / title."""
    q = (query or "").strip()
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                  q, re.I)
    if m:
        return [(m.group(0), m.group(0))]
    text = _query_text(q)
    cat = load_catalog(cfg)
    nq = norm(text)
    if not nq:
        return []
    scored = []
    for name, sid in cat.items():
        nn = norm(name)
        if nn == nq:
            scored.append((0, name, sid))
        elif nn.startswith(nq) or nq.startswith(nn):
            scored.append((1, name, sid))
        elif nq in nn or nn in nq:
            scored.append((2, name, sid))
    scored.sort(key=lambda t: (t[0], len(t[1])))
    seen, out = set(), []
    for _, name, sid in scored:
        if sid in seen:
            continue
        seen.add(sid)
        out.append((name, sid))
        if len(out) >= 5:
            break
    return out


# ------------------------- interactive input -------------------------

def ask_yn(q, default_yes=True):
    d = "Y/n" if default_yes else "y/N"
    raw = input("%s [%s]: " % (q, d)).strip().lower()
    if not raw:
        return default_yes
    return raw in ("y", "yes")


def get_items():
    print("")
    print("Enter one or more titles. Each line = a URL, a name, or a UUID.")
    print("You may also paste a path to a text file (one title per line).")
    print('End the list with a line containing only "."')
    lines = []
    while True:
        line = input("  > ").strip()
        if line == ".":
            break
        if not line:
            continue
        if os.path.exists(line) and os.path.isfile(line):
            for fl in open(line, encoding="utf-8"):
                fl = fl.strip()
                if fl and not fl.startswith("#"):
                    lines.append(fl)
            print("  loaded %d titles from file" % len(lines))
            return lines
        lines.append(line)
    return lines


# ------------------------- episode selection -------------------------

def parse_ep_spec(spec, videos):
    spec = (spec or "").strip().lower()
    if spec in ("", "all", "a"):
        return list(videos)
    sel = []
    for t in re.findall(r"s\d+e\d+|\d+(?:-\d+)?", spec):
        m = re.match(r"s(\d+)e(\d+)", t)
        if m:
            sel += [v for v in videos
                    if v["season"] == int(m.group(1)) and v["episode"] == int(m.group(2))]
            continue
        m = re.match(r"(\d+)-(\d+)", t)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            sel += [v for v in videos
                    if v["episode"] is not None and a <= v["episode"] <= b]
            continue
        n = int(t)
        sel += [v for v in videos if v["episode"] == n]
    seen, out = set(), []
    for v in sel:
        k = (v["season"], v["episode"], v["editId"])
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out


def series_videos(videos):
    """Drop unnumbered video items (trailers, extras) from a series list.

    Movies consist entirely of unnumbered items, so this filter is only
    applied when the resolved show is a series.
    """
    return [v for v in videos
            if v["season"] is not None or v["episode"] is not None]


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_airdate(s):
    """'2024-08-15T00:01:00Z' -> '15 Aug 2024'; '' when unparseable."""
    try:
        y, m, d = s[:10].split("-")
        y, m, d = int(y), int(m), int(d)
        if not (1990 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
            return ""
        return "%d %s %d" % (d, _MONTHS[m - 1], y)
    except Exception:
        return ""


def choose_episodes(videos, spec=""):
    seasons = sorted(set(v["season"] for v in videos if v["season"] is not None))
    print("Total %d episodes." % len(videos))
    if spec:
        sel = parse_ep_spec(spec, videos)
    elif len(seasons) <= 1:
        for v in videos[:30]:
            d = (v["duration"] or 0) / 60000
            if d > 0:
                extra = "(%.0f min)" % d
            else:
                extra = "(%s)" % fmt_airdate(v.get("airDate") or "")
                if extra == "()":
                    extra = "(--)"
            print("  E%-3d %s  %s" % (v["episode"] or 0,
                                      (v["name"] or "")[:52], extra))
        if len(videos) > 30:
            print("  ... %d more" % (len(videos) - 30))
        spec = input("All, or a selection? (blank = all; e.g. 1-6, 1 3 5) ").strip()
        sel = parse_ep_spec(spec, videos)
    else:
        print("Seasons: %s" % ", ".join(str(s) for s in seasons))
        opt = input("a = all episodes, s = pick seasons, e = episode list [a]: "
                    ).strip().lower() or "a"
        if opt == "a":
            sel = list(videos)
        elif opt == "s":
            sq = input("Which seasons? (e.g. 1  or  1 2 3) ").strip()
            nums = [int(x) for x in re.findall(r"\d+", sq)]
            sel = [v for v in videos if v["season"] in nums]
        else:
            spec = input("Episodes (e.g. S01E01 S02E03, 1-6): ").strip()
            sel = parse_ep_spec(spec, videos)
    if not sel:
        return []
    total_min = sum(v["duration"] or 0 for v in sel) / 60000
    unit = "episode" if len(sel) == 1 else "episodes"
    print("Selected: %d %s%s"
          % (len(sel), unit, " (~%.0f min total)" % total_min if total_min > 0
             else ""))
    return sel


# ------------------------- dynamic track detection -------------------------

def probe_tracks(cfg, edit_id):
    mpd_url, _lic, _cert = K.playback(cfg, edit_id)
    mpd = K.fetch_mpd(cfg, mpd_url)
    parsed = K.parse_mpd(mpd)
    return {"audio": list(parsed["audio"].keys()),
            "subs": sorted(set(t["lang"] for t in parsed["subs"])),
            "video_wh": parsed["video_wh"]}


def _pick_langs(label, available, allow_none=False, preset="", blank_none=False):
    # Case-insensitive match: language codes carry an uppercase region
    # (en-US, de-DE) but typed input is lower-cased. Return the original
    # casing exactly as it appears in `available`.
    by_lc = {a.lower(): a for a in available}

    def _match(words):
        words = [x.strip() for x in words.replace(" ", ",").split(",")
                 if x.strip()]
        ok = [by_lc[w.lower()] for w in words if w.lower() in by_lc]
        missing = [w for w in words if w.lower() not in by_lc]
        return ok, missing

    if preset:
        if preset.strip().lower() in ("all", "a"):
            return list(available)
        if allow_none and preset.strip().lower() in ("none", "n", "0"):
            return []
        ok, missing = _match(preset)
        if missing:
            log("  ignoring unknown %s: %s" % (label, ", ".join(missing)))
        return ok
    if not available:
        print("%s: none available." % label)
        return []
    print("%s available: %s" % (label, ", ".join(available)))
    hint = ("blank = none" if blank_none else "blank = all") \
        + ("; n = none" if allow_none else "")
    raw = input("  which to include? (%s; or comma list) " % hint).strip().lower()
    if raw in ("", "all", "a"):
        if blank_none and raw == "":
            return []
        return list(available)
    if allow_none and raw in ("n", "none", "0"):
        return []
    ok, missing = _match(raw)
    if missing:
        print("  (ignoring unknown: %s)" % ", ".join(missing))
    return ok


def choose_tracks(probe, audio_preset="", subs_preset="", embed_preset=""):
    audios = _pick_langs("Audio (dubs)", probe["audio"], False, audio_preset)
    if not audios:
        audios = [probe["audio"][0]]
        print("  no audio selected; defaulting to first: %s" % audios[0])
    subs = _pick_langs("Subtitles", probe["subs"], True, subs_preset)
    if embed_preset:
        mode = "separate" if embed_preset in ("2", "sidecar", "s") else "merge"
    else:
        m = input("Subtitles: embed in MKV (1) / separate sidecar files (2) [1]: "
                  ).strip() or "1"
        mode = "separate" if m == "2" else "merge"
    return audios, subs, mode


# ------------------------- item pipeline -------------------------

def rip_item(cfg, title, v, audios, subs, mode, outdir, meta,
             single_season=False, ep_poster_url=None):
    work = S.work_name(title, v)
    dirp = os.path.join(_workdir(), "." + work)
    os.makedirs(dirp, exist_ok=True)
    log("--- %s ---" % work)

    mpd_url, lic_url, cert = K.playback(cfg, v["editId"])
    mpd = K.fetch_mpd(cfg, mpd_url)
    parsed = K.parse_mpd(mpd)
    keys = K.get_keys(cfg, parsed["pssh"], cert, lic_url)
    nreal = sum(1 for k in keys.values() if k and k != "0" * 32)
    log("quality: %s | keys: %d real" % (parsed["video_wh"], nreal))
    if parsed.get("duration"):
        log("duration: %.1f min" % (parsed["duration"] / 60.0))

    max_bps = int((cfg.get("bwlimit_kbps") or 0) * 1000 // 8)

    base = K.cdn_base(mpd_url)
    vpath = os.path.join(dirp, "video.mp4")
    K.dl(cfg, base + parsed["video_base"], vpath, max_bps)
    K.shaka_decrypt(cfg, vpath, vpath + ".dec.mp4", "video", keys)

    aud = []
    avail = parsed["audio"]
    for lg in audios:
        if lg not in avail:
            continue
        ap = os.path.join(dirp, "audio_%s.mp4" % lg)
        K.dl(cfg, base + avail[lg], ap, max_bps)
        K.shaka_decrypt(cfg, ap, ap + ".dec.mp4", "audio", keys)
        aud.append((lg, ap + ".dec.mp4"))
    if not aud:
        lg0 = sorted(avail.keys())[0]
        log("  requested audio not found, taking first: %s" % lg0)
        ap = os.path.join(dirp, "audio_%s.mp4" % lg0)
        K.dl(cfg, base + avail[lg0], ap, max_bps)
        K.shaka_decrypt(cfg, ap, ap + ".dec.mp4", "audio", keys)
        aud.append((lg0, ap + ".dec.mp4"))

    subfiles, sidecars = [], []
    for tr in parsed["subs"]:
        if tr["lang"] not in subs:
            continue
        segs = []
        for i, num in enumerate(tr["nums"]):
            url = tr["template"].replace("$Number$", str(num))
            sp = os.path.join(dirp, "sub_%s_%d.vtt" % (tr["lang"], i))
            K.dl(cfg, base + url, sp, max_bps)
            segs.append(sp)
        merged = K.merge_subs(cfg, [(sp, "ep") for sp in segs])["ep"]
        vp = os.path.join(dirp, "subs_%s.vtt" % tr["lang"])
        open(vp, "w", encoding="utf-8").write(merged)
        (subfiles if mode == "merge" else sidecars).append((tr["lang"], vp))

    out = os.path.join(dirp, work + ".mkv")
    K.mux(cfg, out, vpath + ".dec.mp4", aud, subfiles)
    info = K.probe(cfg, out)
    ok = K.quick_decode(cfg, out)

    final = S.final_path(outdir, title, v, meta, single_season)
    tdir = os.path.dirname(final)
    os.makedirs(tdir, exist_ok=True)
    if ep_poster_url:
        try:
            K.fetch_image(cfg, ep_poster_url, os.path.join(tdir, "poster.jpg"))
        except Exception as ex:
            log("  ep poster: skipped (%s: %s)" % (type(ex).__name__, ex))
    if not os.path.exists(final):
        shutil.copy2(out, final)
    side_final = []
    for lg, vp in sidecars:
        sp2 = os.path.join(tdir, work + ".subs.%s.vtt" % lg[:2])
        shutil.copy2(vp, sp2)
        side_final.append(sp2)

    S.manifest_append(outdir, S.make_entry(
        outdir, title, v, final, parsed, info, aud, subfiles, side_final,
        single_season))
    S.cleanup(dirp)
    log("temp cleaned: " + dirp)
    return final, ok


# ------------------------- command: max -------------------------

def cmd_max(args):
    cfg = C.load()
    problems = C.validate(cfg)
    if problems:
        print("Config is not complete:")
        for p in problems:
            print("  - " + p)
        print("Run `dashrip init` (or `dashrip doctor`) first.")
        return 1

    # A content query never controls interactivity; only the per-run
    # option flags do. A title on the command line is used as-is; we
    # prompt for titles only when none was passed.
    interactive = not (args.audio or args.subs or args.poster or args.outdir)
    items = list(args.queries)
    if not items:
        items = get_items()
    if not items:
        print("Nothing to do.")
        return 0

    outdir = args.outdir or (cfg.get("outdir") or ".").strip() or "."
    os.makedirs(outdir, exist_ok=True)

    results = []
    for idx, q in enumerate(items, 1):
        log("=== [%d/%d] %s ===" % (idx, len(items), q))
        try:
            cands = find_show(cfg, q)
            if not cands:
                log("  not found in index; skipped")
                results.append((q, "not found"))
                continue
            nq = norm(_query_text(q))
            exact = [c for c in cands if norm(c[0]) == nq]
            name, sid = None, None
            if len(exact) == 1:
                name, sid = exact[0]
                log("  auto-picked exact match: %s" % name)
            elif len(cands) == 1:
                name, sid = cands[0]
            elif interactive:
                while name is None:
                    print("Several candidates:")
                    for i, (nm, s) in enumerate(cands, 1):
                        print("  %d. %s" % (i, nm))
                    sel_i = input("Pick (number, 0 = skip this title): ").strip()
                    if sel_i == "0":
                        log("  skipped by user")
                        results.append((q, "skipped"))
                        break
                    try:
                        name, sid = cands[int(sel_i) - 1]
                    except (ValueError, IndexError):
                        print("  invalid number; try again")
            else:
                log("  ambiguous without interactive pick; skipped")
                results.append((q, "ambiguous"))
                continue

            log("  show: %s" % name)
            show_meta, videos = K.resolve(cfg, sid)
            meta = (show_meta or {}).get("attributes") or {}
            title = meta.get("name") or meta.get("originalName") or sid
            show_poster, ep_posters = K.show_images(cfg, sid)
            is_series = any(v["season"] is not None for v in videos)
            if is_series:
                videos = series_videos(videos)
                sel = choose_episodes(videos, spec=args.ep)
            else:
                sel = [max(videos, key=lambda v: v["duration"] or 0)]
            if not sel:
                results.append((q, "no episodes selected"))
                continue

            probe = probe_tracks(cfg, sel[0]["editId"])
            log("  video: %s | audio: %s | subs: %s"
                % (probe["video_wh"], ", ".join(probe["audio"]) or "none",
                   ", ".join(probe["subs"]) or "none"))
            if interactive:
                audio_p, subs_p, embed_p = args.audio, args.subs, args.embed
            else:
                audio_p = args.audio or "all"
                subs_p = args.subs or "all"
                embed_p = args.embed or "1"
            audios, subs, mode = choose_tracks(probe, audio_p, subs_p, embed_p)
            if interactive:
                want_show_p = ask_yn("Download show poster?", True)
                want_ep_p = ask_yn("Download episode posters?", True) \
                    if is_series else False
            else:
                want_show_p = args.poster in ("", "yes", "y", "show", "all")
                want_ep_p = want_show_p and is_series \
                    and args.poster in ("", "yes", "y", "all")
            seasons_all = sorted(set(v["season"] for v in videos
                                     if v["season"] is not None))
            single_season = len(seasons_all) <= 1
            if want_show_p and show_poster:
                tdir0 = S.title_dir(outdir, title, meta, is_series)
                os.makedirs(tdir0, exist_ok=True)
                try:
                    K.fetch_image(cfg, show_poster,
                                  os.path.join(tdir0, "poster.jpg"))
                except Exception as ex:
                    log("  poster: skipped (%s: %s)"
                        % (type(ex).__name__, ex))
            it = "item" if len(sel) == 1 else "items"
            log("  plan: %d %s | audio: %s | subs: %s | mode: %s"
                % (len(sel), it, audios, subs or "none", mode))
            for i, v in enumerate(sel, 1):
                ep_p = ep_posters.get(v.get("vid")) if want_ep_p else None
                try:
                    final, ok = rip_item(cfg, title, v, audios, subs, mode,
                                         outdir, meta, single_season,
                                         ep_poster_url=ep_p)
                    st = "OK" if ok else "OK (decode warning!)"
                    label = ("%s - %s" % (title, v.get("name") or ""))[:56]
                    results.append((label, st))
                    log("  [%d/%d] done: %s" % (i, len(sel), final))
                except Exception as ex:
                    if getattr(args, "debug", False):
                        import traceback
                        traceback.print_exc()
                    log("  ERROR: %s: %s" % (type(ex).__name__, ex))
                    label = ("%s - %s" % (title, v.get("name") or ""))[:56]
                    results.append((label, "ERROR: %s" % ex))
                if i < len(sel):
                    time.sleep(random.uniform(5, 15))
        except Exception as ex:
            if getattr(args, "debug", False):
                import traceback
                traceback.print_exc()
            results.append((q, "ERROR: %s" % ex))
        if idx < len(items):
            gap = random.uniform(90, 420)
            log("  human gap: %.0f s before next title..." % gap)
            time.sleep(gap)

    print("")
    print("=== Summary ===")
    for nm, st in results:
        print("  %-56s %s" % (str(nm)[:56], st))
    print("Manifest: %s" % os.path.join(outdir, "dashrip_manifest.json"))
    return 0


# ------------------------- command: init -------------------------

def _extract_har(har_path):
    """Pull the playbackInfo template and CMS headers from a HAR file."""
    data = json.load(open(har_path, encoding="utf-8"))
    entries = data["log"]["entries"]
    pb = None
    for e in entries:
        if "playbackInfo" in e["request"]["url"]:
            req = e["request"]
            hdrs = {h["name"]: h["value"] for h in req["headers"]
                    if not h["name"].startswith(":")
                    and h["name"].lower()
                    not in ("content-length", "accept-encoding", "priority",
                            "cookie")}
            body = req.get("postData", {}).get("text", "")
            if body:
                pb = (req["url"], hdrs, body)
            break
    if not pb:
        raise RuntimeError("no playbackInfo request found in the HAR "
                           "(start playback in the browser before saving)")
    chdrs = {}
    for e in entries:
        if "/cms/routes/video/watch/" in e["request"]["url"]:
            for h in e["request"]["headers"]:
                if h["name"].lower().startswith(
                        ("x-disco", "x-device", "x-wbd", "x-fingerprint",
                         "traceparent", "tracestate")):
                    chdrs[h["name"].lower()] = h["value"]
            break

    def find_edit(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "editId" and isinstance(v, str):
                    return v
                r = find_edit(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = find_edit(v)
                if r:
                    return r
        return None

    url, hdrs, body = pb
    tid = find_edit(json.loads(body))
    if not tid:
        raise RuntimeError("could not locate the editId in the playback body")
    return url, hdrs, body, tid, chdrs


def _pip_install(pkg):
    print("Installing %s ..." % pkg)
    subprocess.run([sys.executable, "-m", "pip", "install", pkg], check=False)


def _ask_tool(label, key, arg_check):
    cur = C.get(C.load(), key, "")
    prompt = "%s [%s]: " % (label, cur or "ffmpeg")
    raw = input(prompt).strip()
    val = raw or cur
    if val and C.resolve_tool(val):
        C.set(cfg_global, key, val)
        print("  %s: %s" % (key, C.resolve_tool(val)))
        return True
    print("  not found; you can set it later in %s" % C.path())
    return False


cfg_global = {}


def cmd_init(args):
    global cfg_global
    print("=== dashrip init ===")
    print()
    print("dashrip rips DRM-protected streaming content (Widevine) to MKV.")
    print("Only use it with content you are entitled to watch, and be aware")
    print("that this may violate your service's terms of service.")
    print()
    cfg_global = C.load()

    for pkg in ("requests", "pywidevine"):
        try:
            __import__(pkg)
        except ImportError:
            if ask_yn("  %s is missing. Install it now? (pip)" % pkg, True):
                _pip_install(pkg)

    _ask_tool("ffmpeg path", "ffmpeg", "-version")
    _ask_tool("ffprobe path", "ffprobe", "-version")
    _ask_tool("shaka-packager path", "shaka", "--version")

    while True:
        wvd = input("Widevine device file (.wvd) path: ").strip()
        if not wvd:
            print("  a .wvd file is required; get one for your own device")
            continue
        if not os.path.isfile(wvd):
            print("  file not found; try again")
            continue
        try:
            from pywidevine.device import Device
            d = Device.load(wvd)
        except Exception as e:
            print("  could not load the device: %r" % e)
            continue
        lvl = {1: "L1", 3: "L3"}.get(d.security_level, "L%d" % d.security_level)
        print("  device OK: %s, sys_id ...%s"
              % (lvl, str(d.system_id)[-4:]))
        if d.security_level == 1:
            print("  note: L1 devices unlock the highest tiers (1080p+)")
        C.set(cfg_global, "wvd", os.path.abspath(wvd))
        break

    while True:
        raw = input("Session cookie (st=eyJ... , from F12 -> Network): ").strip()
        tok = C.cookie_token(raw)
        if tok:
            exp = C.cookie_expiry(tok)
            if exp and exp < time.time():
                print("  this cookie is expired; capture a fresh one")
                continue
            C.set(cfg_global, "cookie", raw)
            print("  cookie OK" + (" (expires %s)" % time.strftime(
                "%Y-%m-%d", time.gmtime(exp)) if exp else ""))
            break
        print("  expected an st= JWT token; try again")

    while True:
        harp = input("HAR file from the browser (F12 -> Network -> Save as HAR): "
                     ).strip()
        if not harp or not os.path.isfile(harp):
            print("  file not found; try again")
            continue
        try:
            url, hdrs, body, tid, chdrs = _extract_har(harp)
        except Exception as e:
            print("  %s" % e)
            continue
        C.set(cfg_global, "playback.url", url)
        C.set(cfg_global, "playback.headers", hdrs)
        C.set(cfg_global, "playback.body", body)
        C.set(cfg_global, "playback.template_edit_id", tid)
        C.set(cfg_global, "cms_headers", chdrs)
        print("  playback template captured (%d headers)" % len(hdrs))
        break

    outdir = input("Output directory [default]: ").strip() \
        or cfg_global.get("outdir") or ""
    C.set(cfg_global, "outdir", outdir)
    try:
        bl = input("Download speed cap in kbps (0 = unlimited) [0]: ").strip()
        C.set(cfg_global, "bwlimit_kbps", int(bl or 0))
    except ValueError:
        C.set(cfg_global, "bwlimit_kbps", 0)

    p = C.save(cfg_global)
    print("\nConfig written to %s" % p)
    print()
    ok = D.doctor(cfg_global)
    print()
    print("init complete." if ok else "init finished -- fix the FAIL lines above.")
    return 0 if ok else 1


# ------------------------- command: prime -------------------------

PRIME_STEPS = """
Amazon Prime is capture-assisted: the browser (with the WidevineProxy
extension + a .wvd device) performs one short playback, and you paste the
ready-made command from the extension's History. dashrip then downloads,
decrypts, renames and files everything automatically.

 1. In the browser: install the WidevineProxy extension, enable it,
    load your .wvd device, pick the content, and play it for ~10 s.
 2. Open the extension's History, open the captured entry, and press
    "copy" on the Command line (an N_m3u8DL-RE command).
 3. Paste that command below.

 Optional: with F12 open (Network tab, "Preserve log"), save the playback
 as "HAR with content". If you offer the file when asked, dashrip lists the
 subtitle tracks it captured (30+ languages), converts the ones you pick,
 and embeds them in the MKV. The capture also carries the content id, so
 the title, year, and poster are resolved automatically from Prime's
 public detail page; you are not asked to type them.
"""


def _parse_n3u_cmd(line):
    """Parse an N_m3u8DL-RE command line into its parts."""
    import shlex
    toks = shlex.split(line, posix=True)
    out = {"exe": toks[0], "url": None, "headers": [], "keys": {}, "extra": []}
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == "-H" and i + 1 < len(toks):
            out["headers"].append(toks[i + 1])
            i += 2
        elif t == "--key" and i + 1 < len(toks):
            k, v = toks[i + 1].split(":", 1)
            out["keys"][k.lower()] = v
            i += 2
        elif t == "-M" and i + 1 < len(toks):
            out["extra"] += [t, toks[i + 1]]
            i += 2
        elif t in ("-sv", "-sa", "-se") and i + 1 < len(toks):
            out["extra"] += [t, toks[i + 1]]
            i += 2
        elif t.startswith("-"):
            out["extra"].append(t)
            i += 1
        elif out["url"] is None:
            out["url"] = t
            i += 1
        else:
            out["extra"].append(t)
            i += 1
    return out


def _probe_prime_mpd(cfg, url, headers):
    import requests
    import xml.etree.ElementTree as ET
    hdrs = {"User-Agent": K.UA, "Origin": "https://www.primevideo.com",
            "Referer": "https://www.primevideo.com/"}
    for h in headers:
        if ":" in h:
            k, v = h.split(":", 1)
            hdrs[k.strip()] = v.strip()
    r = requests.get(url, headers=hdrs, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    M = "{urn:mpeg:dash:schema:mpd:2011}"
    vids, auds = [], []
    for as_ in root.iter(M + "AdaptationSet"):
        ct = as_.get("contentType") or as_.get("mimeType", "")
        lg = as_.get("lang") or ""
        for rep in as_.findall(M + "Representation"):
            if ct == "video" or "video" in ct:
                w, h = int(rep.get("width") or 0), int(rep.get("height") or 0)
                vids.append((w * h, "%dx%d" % (w, h)))
            elif "audio" in (ct or ""):
                auds.append(lg or "und")
    return sorted(set(vids)), sorted(set(auds))


def cmd_prime(args):
    cfg = C.load()
    exe = C.resolve_tool(cfg.get("nm3u8dlre") or "N_m3u8DL-RE")
    if not exe:
        print("N_m3u8DL-RE not found. Install it and set its path in the")
        print("config (key: nm3u8dlre), or install it via the extension.")
        return 1
    print(PRIME_STEPS)
    line = input("Paste the N_m3u8DL-RE command: ").strip()
    if not line:
        print("Nothing to do.")
        return 1
    parsed = _parse_n3u_cmd(line)
    if not parsed["url"]:
        print("Could not find the MPD URL in that command.")
        return 1
    log("MPD: %s" % parsed["url"][:100] + "...")
    log("keys captured: %d" % len(parsed["keys"]))
    har_path = input("HAR file (optional, subtitles + title) [blank]: ") \
        .strip().strip('"')
    har_subs = []
    prime_meta = {}
    if har_path:
        if not os.path.exists(har_path):
            log("  HAR not found: %s (continuing without subtitles)" % har_path)
        else:
            h = K.parse_prime_har(har_path)
            if h["error"]:
                log("  HAR: %s (continuing without subtitles)" % h["error"])
            else:
                har_subs = h["subs"]
                log("  HAR: %d subtitle tracks available" % len(har_subs))
                if h.get("title_id"):
                    prime_meta = K.fetch_prime_meta(h["title_id"])
                    if prime_meta.get("title"):
                        yr = prime_meta.get("year", "")
                        log("  title: %s%s" % (prime_meta["title"],
                             " (%s)" % yr if yr else ""))
                    else:
                        log("  could not resolve the title from Prime; "
                            "will ask")
    try:
        vids, auds = _probe_prime_mpd(cfg, parsed["url"], parsed["headers"])
    except Exception as e:
        log("  could not probe the MPD (%r); will ask N_m3u8DL-RE to pick" % e)
        vids, auds = [], []
    if vids:
        print("  qualities: %s" % ", ".join(v[1] for v in vids))
        q = input("  quality (blank = highest 720p-or-below): ").strip()
    else:
        q = ""
    if auds:
        print("  audio: %s" % ", ".join(auds))
        a = input("  audio (blank = first): ").strip()
    else:
        a = ""

    extra = list(parsed["extra"])
    if q:
        extra += ["-sv", 'res="%s*":for=best' % q.split("x")[0]]
    elif any(v[1].startswith("1280") for v in vids):
        extra += ["-sv", 'res="1280*":for=best']
    chosen_audio = a
    if not chosen_audio and auds:
        chosen_audio = "tr" if "tr" in auds else auds[0]
    if chosen_audio:
        extra += ["-sa", "lang=%s:for=best" % chosen_audio]

    sel_subs = []
    if har_subs:
        picked = _pick_langs("Subtitles",
                             [s["code"] for s in har_subs], True, "",
                             blank_none=True)
        sel_subs = [s for s in har_subs if s["code"] in picked]

    want_poster = False
    if prime_meta.get("poster"):
        want_poster = input("Download poster? [Y/n]: ").strip().lower() \
            in ("", "y", "yes")

    run_dir = os.path.join(_workdir(), "prime_%s" % time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(run_dir, exist_ok=True)
    work = run_dir
    # drop any -M pair coming from the pasted command (added once, below)
    kept, i = [], 0
    while i < len(extra):
        if extra[i] == "-M":
            i += 2
            continue
        kept.append(extra[i])
        i += 1
    cmd = [exe, parsed["url"]]
    for h in parsed["headers"]:
        cmd += ["-H", h]
    for k, v in parsed["keys"].items():
        cmd += ["--key", "%s:%s" % (k, v)]
    cmd += kept + ["-M", "format=mkv", "--save-dir", work]
    log("running: N_m3u8DL-RE + %d header + %d keys (720p/en) ..."
        % (len(parsed["headers"]), len(parsed["keys"])))
    r = subprocess.run(cmd, cwd=work)
    if r.returncode != 0:
        print("N_m3u8DL-RE exited with %d" % r.returncode)
        return 1
    mkv = None
    for f in os.listdir(work):
        if f.endswith(".mkv"):
            mkv = os.path.join(work, f)
            break
    if not mkv:
        print("No .mkv found in the work dir; check the output above.")
        return 1

    embedded_subs = []
    if sel_subs:
        ffmpeg = C.resolve_tool(cfg.get("ffmpeg") or "ffmpeg") or "ffmpeg"
        srt_inputs = []
        for s in sel_subs:
            txt = K.fetch_ttml(s["url"])
            srt = K.ttml_to_srt(txt)
            if not srt:
                log("  subtitle %s: nothing converted; skipped" % s["code"])
                continue
            sp = os.path.join(work, "sub_%s.srt" % s["code"])
            with open(sp, "w", encoding="utf-8") as f:
                f.write(srt)
            srt_inputs.append((sp, s["code"]))
        if srt_inputs:
            out_mkv = os.path.join(work, "with_subs.mkv")
            cmd2 = [ffmpeg, "-y", "-i", mkv]
            for sp, code in srt_inputs:
                cmd2 += ["-i", sp]
            cmd2 += ["-map", "0"]
            for i in range(len(srt_inputs)):
                cmd2 += ["-map", "%d:0" % (i + 1)]
            cmd2 += ["-c", "copy", "-c:s", "srt"]
            for i in range(len(srt_inputs)):
                lang = srt_inputs[i][1].split("-")[0][:2] or "und"
                cmd2 += ["-metadata:s:s:%d" % i, "language=%s" % lang]
            cmd2 += [out_mkv]
            r2 = subprocess.run(cmd2, cwd=work)
            if r2.returncode == 0 and os.path.exists(out_mkv):
                mkv = out_mkv
                embedded_subs = [c for _, c in srt_inputs]
                log("  subtitles embedded: %s" % ", ".join(embedded_subs))
            else:
                log("  subtitle remux failed (ffmpeg exit %d); "
                    "delivering without subtitles" % r2.returncode)

    info = K.probe(cfg, mkv)
    dur = float(info.get("format", {}).get("duration") or 0)
    def_title = prime_meta.get("title", "")
    def_year = prime_meta.get("year", "")
    title = input("Title [%s]: " % (def_title or "unknown")).strip() \
        or def_title or "unknown"
    ep_hint = prime_meta.get("episode") or {}
    is_series = input("Series? (y/N, only if this is an episode): ").strip().lower() \
        in ("y", "yes")
    meta = {}
    if not is_series:
        yr = input("Year [%s]: " % (def_year or "blank")).strip() or def_year
        meta = {"premiereDate": yr + "-01-01"} if yr else {}
    v = {}
    if is_series:
        v["season"] = int(input("Season [%s]: " % ep_hint.get("season", 1)).strip()
                           or ep_hint.get("season", 1))
        v["episode"] = int(input("Episode [%s]: " % ep_hint.get("episode", 1)).strip()
                            or ep_hint.get("episode", 1))
        v["name"] = input("Episode title [%s]: "
                          % (ep_hint.get("title") or "blank")).strip() \
            or ep_hint.get("title", "")
    outdir = (cfg.get("outdir") or ".").strip() or "."
    os.makedirs(outdir, exist_ok=True)
    final = S.final_path(outdir, title, v, meta, False)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    if want_poster and prime_meta.get("poster"):
        try:
            K.fetch_image(cfg, prime_meta["poster"],
                          os.path.join(os.path.dirname(final), "poster.jpg"))
            log("  poster: downloaded")
        except Exception as ex:
            log("  poster: skipped (%s: %s)" % (type(ex).__name__, ex))
    shutil.move(mkv, final)
    S.manifest_append(outdir, S.make_entry(
        outdir, title, v, final, {"video_wh": ""}, info,
        [(chosen_audio, "")] if chosen_audio else [],
        [(c, "") for c in embedded_subs], [], False))
    S.cleanup(run_dir)
    print("Delivered: %s" % final)
    return 0


# ------------------------- command: sync -------------------------

def cmd_sync(args):
    try:
        from . import sync
    except ImportError:
        print("The sync module is not available in this build.")
        return 1
    argv = []
    for flag, val in (("--src", args.src), ("--host", args.host),
                      ("--user", args.user), ("--key", args.key),
                      ("--target", args.target)):
        if val:
            argv += [flag, val]
    return sync.main([args.cmd or "sync"] + argv)


# ------------------------- main -------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="dashrip",
        description="Rip DRM-protected streaming content (Widevine) to MKV.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("init", help="first-time guided setup")
    sub.add_parser("doctor", help="health check")
    pm = sub.add_parser("max", help="rip HBO Max content")
    pm.add_argument("queries", nargs="*",
                    help="URLs, titles or UUIDs (blank = interactive)")
    pm.add_argument("--audio", default="", help="audio langs, e.g. tr,en")
    pm.add_argument("--subs", default="", help="subtitle langs, e.g. tr,en")
    pm.add_argument("--embed", default="", help="1 = embed, 2 = sidecar files")
    pm.add_argument("--poster", default="",
                    help="poster policy: yes/no (blank = ask)")
    pm.add_argument("--ep", default="", help="episode spec, e.g. 1-6 or S01E02")
    pm.add_argument("--outdir", default="", help="output directory")
    pm.add_argument("--debug", action="store_true",
                     help="print full tracebacks on errors")
    pp = sub.add_parser("prime", help="rip an Amazon Prime capture")
    ps = sub.add_parser("sync", help="lossless resumable folder transfer")
    ps.add_argument("--src", help="local source dir")
    ps.add_argument("--host", help="ssh host")
    ps.add_argument("--user", help="ssh user")
    ps.add_argument("--key", help="ssh private key path")
    ps.add_argument("--target", help="remote target dir")
    ps.add_argument("--cmd", help="sync|status|verify (default sync)")
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    try:
        if args.cmd == "init":
            return cmd_init(args)
        if args.cmd == "doctor":
            return 0 if D.doctor() else 1
        if args.cmd == "max":
            return cmd_max(args)
        if args.cmd == "prime":
            return cmd_prime(args)
        if args.cmd == "sync":
            return cmd_sync(args)
    except KeyboardInterrupt:
        print("\nInterrupted (Ctrl+C). Partial files kept; re-run to resume.")
        return 130
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
