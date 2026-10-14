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
from contextlib import suppress
from urllib.parse import urlsplit

import requests

from . import config as C
from . import core as K
from . import doctor as D
from . import structure as S

if hasattr(sys.stdout, "reconfigure"):
    with suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")


def log(msg: str) -> None:
    """Print a timestamped progress line."""
    print("[{}] {}".format(time.strftime("%H:%M:%S"), msg), flush=True)


def _workdir() -> str:
    """Working directory under ~/.dashrip/work (created)."""
    d = os.path.join(os.path.expanduser("~"), ".dashrip", "work")
    os.makedirs(d, exist_ok=True)
    return d


def _catalog_path() -> str:
    """Path of the cached CMS title index."""
    return os.path.join(os.path.expanduser("~"), ".dashrip", "catalog.json")


# ------------------------- text helpers -------------------------

def norm(s: str) -> str:
    """Locale-tolerant normalisation for title matching."""
    s = (s or "").lower()
    for a, b in (("\u00e7", "c"), ("\u011f", "g"), ("\u0131", "i"),
                 ("\u00f6", "o"), ("\u015f", "s"), ("\u00fc", "u")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", s)


def _query_text(q: str) -> str:
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


def load_catalog(cfg: dict) -> dict:
    """Title -> show id index, built once from the CMS and cached."""
    p = _catalog_path()
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d:
                return d
        except Exception as e:
            log(f"catalog cache unreadable ({e}), rebuilding")
    print("Building the CMS index (a few seconds)...")
    names = {}
    hdrs = K._cms_headers(cfg)
    for rt in ["home"] + ["genre/" + g for g in GENRES]:
        try:
            r = requests.get(K.CMS_BASE + "/cms/routes/" + rt +
                             "?include=default&decorators=badges",
                             headers=hdrs, timeout=60)
            if r.status_code == 200:
                for x in r.json().get("included", []):
                    if x.get("type") == "show":
                        a = x.get("attributes") or {}
                        nm = a.get("name") or a.get("originalName")
                        if nm:
                            names[nm] = x["id"]
        except Exception as e:
            log(f"route {rt} skipped ({e})")
        time.sleep(random.uniform(1, 3))
    if names:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(names, f, indent=1)
        print(f"  index: {len(names)} shows")
    return names


def find_show(cfg: dict, query: str) -> list:
    """Return [(name, show_id)] candidates for a URL / UUID / title."""
    q = (query or "").strip()
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                  q, re.IGNORECASE)
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

def ask_yn(q: str, default_yes: bool = True) -> bool:
    """Yes/no prompt; the default applies to a blank answer."""
    d = "Y/n" if default_yes else "y/N"
    raw = input(f"{q} [{d}]: ").strip().lower()
    if not raw:
        return default_yes
    return raw in ("y", "yes")


def get_items() -> list:
    """Read titles from stdin, or from a pasted file path."""
    print()
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
            with open(line, encoding="utf-8") as fh:
                for fl in fh:
                    fl = fl.strip()
                    if fl and not fl.startswith("#"):
                        lines.append(fl)
            print(f"  loaded {len(lines)} titles from file")
            return lines
        lines.append(line)
    return lines


# ------------------------- episode selection -------------------------

def parse_ep_spec(spec: str, videos: list) -> list:
    """Expand an episode spec like 1-6 or 1 3 5."""
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


def series_videos(videos: list) -> list:
    """Drop unnumbered video items (trailers, extras) from a series list.

    Movies consist entirely of unnumbered items, so this filter is only
    applied when the resolved show is a series.
    """
    return [v for v in videos
            if v["season"] is not None or v["episode"] is not None]


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_airdate(s: str) -> str:
    """'2024-08-15T00:01:00Z' -> '15 Aug 2024'; '' when unparseable."""
    try:
        y, m, d = (int(x) for x in s[:10].split("-"))
        if not (1990 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
            return ""
        return f"{d} {_MONTHS[m - 1]} {y}"
    except Exception:
        return ""


def choose_episodes(videos: list, spec: str = "") -> list:
    """Pick the episodes to rip: from a spec or interactively."""
    seasons = sorted({v["season"] for v in videos if v["season"] is not None})
    print(f"Total {len(videos)} episodes.")
    if spec:
        sel = parse_ep_spec(spec, videos)
    elif len(seasons) <= 1:
        for v in videos[:30]:
            d = (v["duration"] or 0) / 60000
            if d > 0:
                extra = f"({d:.0f} min)"
            else:
                extra = "({})".format(fmt_airdate(v.get("airDate") or ""))
                if extra == "()":
                    extra = "(--)"
            print(f"  E{v['episode'] or 0:<3} {(v['name'] or '')[:52]}  {extra}")
        if len(videos) > 30:
            print(f"  ... {len(videos) - 30} more")
        spec = input("All, or a selection? (blank = all; e.g. 1-6, 1 3 5) ").strip()
        sel = parse_ep_spec(spec, videos)
    else:
        print("Seasons: {}".format(", ".join(str(s) for s in seasons)))
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
    print(f"Selected: {len(sel)} {unit}"
          + (f" (~{total_min:.0f} min total)" if total_min > 0 else ""))
    return sel


# ------------------------- dynamic track detection -------------------------

def probe_tracks(cfg: dict, edit_id: str) -> dict:
    """Track summary (video, audio, subs) for one edit id."""
    mpd_url, _lic, _cert = K.playback(cfg, edit_id)
    mpd = K.fetch_mpd(cfg, mpd_url)
    parsed = K.parse_mpd(mpd)
    return {"audio": list(parsed["audio"].keys()),
            "subs": sorted({t["lang"] for t in parsed["subs"]}),
            "video_wh": parsed["video_wh"]}


def _resolve_audio_policy(policy: str | list | None, available: list,
                          original_hint: str = "") -> list | None:
    """Resolve a configured audio policy to concrete available codes.

    The policy may be a list (e.g. ["orig", "de"]), a comma string,
    "all"/"a", or "none"/"n".  The 'orig' pseudo-code resolves to the
    content's original language.  Returns a list of available codes,
    None for "all", or [] for "none".
    """
    if policy is None or policy == "":
        return None
    if isinstance(policy, str):
        policy = [x.strip() for x in policy.replace(" ", ",").split(",")
                  if x.strip()]
    if not policy:
        return None
    if len(policy) == 1 and policy[0].lower() in ("all", "a"):
        return None
    if len(policy) == 1 and policy[0].lower() in ("none", "n"):
        return []
    by_lc = {a.lower(): a for a in available}
    out = []
    for p in policy:
        if p.lower() == "orig":
            o = K.resolve_original_lang(available, original_hint)
            if o:
                out.append(o)
        elif p.lower() in by_lc:
            out.append(by_lc[p.lower()])
    seen, res = set(), []
    for x in out:
        if x.lower() not in seen:
            seen.add(x.lower())
            res.append(x)
    return res


def _pick_langs(label: str, available: list, allow_none: bool = False,
                preset: str = "", blank_none: bool = False,
                default_policy: list | None = None) -> list:
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
            log("  ignoring unknown {}: {}".format(label, ", ".join(missing)))
        return ok
    if not available:
        print(f"{label}: none available.")
        return []
    # the blank default: a configured policy (filtered to what is
    # available), otherwise all / none
    policy: list | None = None
    if default_policy is not None:
        by_avail = {x.lower(): x for x in available}
        policy = [by_avail[p.lower()] for p in default_policy
                  if p.lower() in by_avail]
    use_policy = policy is not None
    blank_label = (", ".join(policy) if policy else "none") if use_policy \
        else ("none" if blank_none else "all")
    print("{} available: {}".format(label, ", ".join(available)))
    hint = (f"blank = {blank_label}") \
        + ("; n = none" if allow_none else "")
    raw = input(f"  which to include? ({hint}; or comma list) ").strip().lower()
    if raw in ("", "all", "a"):
        if raw == "":
            if use_policy:
                return policy or []
            return [] if blank_none else list(available)
        return list(available)
    if allow_none and raw in ("n", "none", "0"):
        return []
    ok, missing = _match(raw)
    if missing:
        print("  (ignoring unknown: {})".format(", ".join(missing)))
    return ok


def choose_tracks(probe: dict, audio_preset: str = "", subs_preset: str = "",
                  embed_preset: str = "", audio_default: list | None = None,
                  original_hint: str = "") -> tuple[list, list, str]:
    audios = _pick_langs("Audio (dubs)", probe["audio"], False, audio_preset,
                         default_policy=_resolve_audio_policy(
                             audio_default, probe["audio"], original_hint))
    if not audios:
        audios = [probe["audio"][0]]
        print(f"  no audio selected; defaulting to first: {audios[0]}")
    subs = _pick_langs("Subtitles", probe["subs"], True, subs_preset)
    if embed_preset:
        mode = "separate" if embed_preset in ("2", "sidecar", "s") else "merge"
    else:
        m = input("Subtitles: embed in MKV (1) / separate sidecar files (2) [1]: "
                  ).strip() or "1"
        mode = "separate" if m == "2" else "merge"
    return audios, subs, mode


# ------------------------- item pipeline -------------------------

def _download_audio(cfg: dict, base: str, avail: dict, audios: list,
                    dirp: str, max_bps: int, keys: dict) -> list:
    """Download and decrypt the requested audio tracks. Falls back to the
    first available language when none of the requests exist. Returns
    [(lang, decrypted path)]."""
    aud = []
    for lg in audios:
        if lg not in avail:
            continue
        ap = os.path.join(dirp, f"audio_{lg}.mp4")
        K.dl(cfg, base + avail[lg], ap, max_bps)
        K.shaka_decrypt(cfg, ap, ap + ".dec.mp4", "audio", keys)
        aud.append((lg, ap + ".dec.mp4"))
    if not aud:
        lg0 = min(avail.keys())
        log(f"  requested audio not found, taking first: {lg0}")
        ap = os.path.join(dirp, f"audio_{lg0}.mp4")
        K.dl(cfg, base + avail[lg0], ap, max_bps)
        K.shaka_decrypt(cfg, ap, ap + ".dec.mp4", "audio", keys)
        aud.append((lg0, ap + ".dec.mp4"))
    return aud


def _download_subs(cfg: dict, base: str, tracks: list, subs: list,
                   mode: str, dirp: str, max_bps: int) -> tuple[list, list]:
    """Download the selected subtitle tracks and merge their segments.
    Returns (merged files, sidecar files) depending on the embed mode."""
    subfiles: list = []
    sidecars: list = []
    for tr in tracks:
        if tr["lang"] not in subs:
            continue
        segs = []
        for i, num in enumerate(tr["nums"]):
            url = tr["template"].replace("$Number$", str(num))
            sp = os.path.join(dirp, f"sub_{tr['lang']}_{i}.vtt")
            K.dl(cfg, base + url, sp, max_bps)
            segs.append(sp)
        merged = K.merge_subs(cfg, [(sp, "ep") for sp in segs])["ep"]
        vp = os.path.join(dirp, "subs_{}.vtt".format(tr["lang"]))
        with open(vp, "w", encoding="utf-8") as f:
            f.write(merged)
        (subfiles if mode == "merge" else sidecars).append((tr["lang"], vp))
    return subfiles, sidecars


def rip_item(cfg: dict, title: str, v: dict, audios: list, subs: list,
             mode: str, outdir: str, meta: dict, single_season: bool = False,
             ep_poster_url: str | None = None) -> tuple[str, bool]:
    """Rip one episode or movie: download, decrypt, mux, deliver."""
    work = S.work_name(title, v)
    dirp = os.path.join(_workdir(), "." + work)
    os.makedirs(dirp, exist_ok=True)
    log(f"--- {work} ---")

    mpd_url, lic_url, cert = K.playback(cfg, v["editId"])
    mpd = K.fetch_mpd(cfg, mpd_url)
    parsed = K.parse_mpd(mpd)
    keys = K.get_keys(cfg, parsed["pssh"], cert, lic_url)
    nreal = sum(1 for k in keys.values() if k and k != "0" * 32)
    log(f"quality: {parsed['video_wh']} | keys: {nreal} real")
    if parsed.get("duration"):
        log("duration: %.1f min" % (parsed["duration"] / 60.0))

    max_bps = int((cfg.get("bwlimit_kbps") or 0) * 1000 // 8)

    base = K.cdn_base(mpd_url)
    vpath = os.path.join(dirp, "video.mp4")
    K.dl(cfg, base + parsed["video_base"], vpath, max_bps)
    K.shaka_decrypt(cfg, vpath, vpath + ".dec.mp4", "video", keys)

    aud = _download_audio(cfg, base, parsed["audio"], audios, dirp,
                          max_bps, keys)
    subfiles, sidecars = _download_subs(
        cfg, base, parsed["subs"], subs, mode, dirp, max_bps)

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
            log(f"  ep poster: skipped ({type(ex).__name__}: {ex})")
    if not os.path.exists(final):
        shutil.copy2(out, final)
    side_final = []
    for lg, vp in sidecars:
        sp2 = os.path.join(tdir, work + f".subs.{lg[:2]}.vtt")
        shutil.copy2(vp, sp2)
        side_final.append(sp2)

    S.manifest_append(outdir, S.make_entry(
        outdir, title, v, final, parsed, info, aud, subfiles, side_final,
        single_season))
    S.cleanup(dirp)
    log("temp cleaned: " + dirp)
    return final, ok


# ------------------------- command: max -------------------------

def _pick_show(cfg: dict, q: str, interactive: bool, results: list) -> tuple | None:
    """Resolve a query to a (name, show id) pair. Returns None when the
    title is not found, ambiguous, or skipped; that outcome is recorded
    in results by this function."""
    cands = find_show(cfg, q)
    if not cands:
        log("  not found in index; skipped")
        results.append((q, "not found"))
        return None
    nq = norm(_query_text(q))
    exact = [c for c in cands if norm(c[0]) == nq]
    name, sid = None, None
    if len(exact) == 1:
        name, sid = exact[0]
        log(f"  auto-picked exact match: {name}")
    elif len(cands) == 1:
        name, sid = cands[0]
    elif interactive:
        while name is None:
            print("Several candidates:")
            for i, (nm, s) in enumerate(cands, 1):
                print(f"  {i}. {nm}")
            sel_i = input("Pick (number, 0 = skip this title): ").strip()
            if sel_i == "0":
                log("  skipped by user")
                results.append((q, "skipped"))
                return None
            try:
                name, sid = cands[int(sel_i) - 1]
            except (ValueError, IndexError):
                print("  invalid number; try again")
    else:
        log("  ambiguous without interactive pick; skipped")
        results.append((q, "ambiguous"))
        return None
    return name, sid


def _resolve_item(cfg: dict, sid: str, args: argparse.Namespace) -> tuple | None:
    """Resolve the show id into delivery metadata plus the selected
    episodes. Returns a 7-tuple, or None when nothing is selected."""
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
        return None
    return (title, meta, videos, sel, show_poster, ep_posters, is_series)


def _plan_item(cfg: dict, args: argparse.Namespace, interactive: bool,
               sel: list, videos: list, is_series: bool, title: str,
               meta: dict, show_poster: str | None,
               outdir: str) -> tuple[list, list, str, bool, bool]:
    """Probe the first selected episode, choose the tracks, and fetch the
    show poster when requested. Returns (audios, subs, mode, want episode
    posters, single season)."""
    probe = probe_tracks(cfg, sel[0]["editId"])
    log("  video: {} | audio: {} | subs: {}".format(
        probe["video_wh"], ", ".join(probe["audio"]) or "none",
        ", ".join(probe["subs"]) or "none"))
    if interactive:
        audio_p, subs_p, embed_p = args.audio, args.subs, args.embed
    else:
        audio_p = args.audio or "all"
        subs_p = args.subs or "all"
        embed_p = args.embed or "1"
    audios, subs, mode = choose_tracks(
        probe, audio_p, subs_p, embed_p,
        audio_default=(cfg.get("defaults") or {}).get("audio", ["orig"]),
        original_hint="")
    if interactive:
        want_show_p = ask_yn("Download show poster?", True)
        want_ep_p = ask_yn("Download episode posters?", True) \
            if is_series else False
    else:
        want_show_p = args.poster in ("", "yes", "y", "show", "all")
        want_ep_p = want_show_p and is_series \
            and args.poster in ("", "yes", "y", "all")
    seasons_all = sorted({v["season"] for v in videos
                             if v["season"] is not None})
    single_season = len(seasons_all) <= 1
    if want_show_p and show_poster:
        tdir0 = S.title_dir(outdir, title, meta, is_series)
        os.makedirs(tdir0, exist_ok=True)
        try:
            K.fetch_image(cfg, show_poster,
                          os.path.join(tdir0, "poster.jpg"))
        except Exception as ex:
            log(f"  poster: skipped ({type(ex).__name__}: {ex})")
    it = "item" if len(sel) == 1 else "items"
    log(f"  plan: {len(sel)} {it} | audio: {audios} | "
        f"subs: {subs or 'none'} | mode: {mode}")
    return audios, subs, mode, want_ep_p, single_season


def cmd_max(args: argparse.Namespace) -> int:
    """Rip one or more Max queries end to end."""
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

    results: list = []
    for idx, q in enumerate(items, 1):
        log(f"=== [{idx}/{len(items)}] {q} ===")
        try:
            cand = _pick_show(cfg, q, interactive, results)
            if cand is None:
                continue
            name, sid = cand
            log(f"  show: {name}")

            picked = _resolve_item(cfg, sid, args)
            if picked is None:
                results.append((q, "no episodes selected"))
                continue
            title, meta, videos, sel, show_poster, ep_posters, \
                is_series = picked

            audios, subs, mode, want_ep_p, single_season = _plan_item(
                cfg, args, interactive, sel, videos, is_series, title, meta,
                show_poster, outdir)
            for i, v in enumerate(sel, 1):
                ep_p = ep_posters.get(v.get("vid")) if want_ep_p else None
                try:
                    final, ok = rip_item(cfg, title, v, audios, subs, mode,
                                         outdir, meta, single_season,
                                         ep_poster_url=ep_p)
                    st = "OK" if ok else "OK (decode warning!)"
                    label = ("{} - {}".format(title, v.get("name") or ""))[:56]
                    results.append((label, st))
                    log(f"  [{i}/{len(sel)}] done: {final}")
                except Exception as ex:
                    if getattr(args, "debug", False):
                        import traceback
                        traceback.print_exc()
                    log(f"  ERROR: {type(ex).__name__}: {ex}")
                    label = ("{} - {}".format(title, v.get("name") or ""))[:56]
                    results.append((label, f"ERROR: {ex}"))
                if i < len(sel):
                    time.sleep(random.uniform(5, 15))
        except Exception as ex:
            if getattr(args, "debug", False):
                import traceback
                traceback.print_exc()
            results.append((q, f"ERROR: {ex}"))
        if idx < len(items):
            gap = random.uniform(90, 420)
            log(f"  human gap: {gap:.0f} s before next title...")
            time.sleep(gap)

    print()
    print("=== Summary ===")
    for nm, st in results:
        print(f"  {str(nm)[:56]:<56} {st}")
    print("Manifest: {}".format(os.path.join(outdir, "dashrip_manifest.json")))
    return 0


# ------------------------- command: init -------------------------

def _extract_har(har_path: str) -> tuple[str, dict, str, str, dict]:
    """Pull the playbackInfo template and CMS headers from a HAR file."""
    with open(har_path, encoding="utf-8") as f:
        data = json.load(f)
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


def _pip_install(pkg: str) -> None:
    """Install a package into the current interpreter with pip."""
    print(f"Installing {pkg} ...")
    subprocess.run([sys.executable, "-m", "pip", "install", pkg], check=False)


def _tool_runs(exe: str, version_arg: str) -> bool:
    """True if the tool starts and exits cleanly for its version argument."""
    try:
        p = subprocess.run([exe, version_arg], capture_output=True, timeout=20)
    except Exception:
        return False
    return p.returncode == 0


def _ask_tool(cfg: dict, label: str, key: str, version_arg: str) -> bool:
    """Prompt for an external tool, verify it runs, and store its path."""
    cur = C.get(cfg, key, "")
    raw = input(f"{label} [{cur or key}]: ").strip()
    val = raw or cur
    exe = C.resolve_tool(val) if val else None
    if exe and _tool_runs(exe, version_arg):
        C.set(cfg, key, val)
        print(f"  {key}: {exe}")
        return True
    print(f"  not found or not runnable; set it later in {C.path()}")
    return False


cfg_global: dict = {}


def _ask_device(cfg: dict) -> None:
    """Prompt until a loadable .wvd device file is configured."""
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
            print(f"  could not load the device: {e!r}")
            continue
        lvl = {1: "L1", 3: "L3"}.get(d.security_level, f"L{d.security_level}")
        print(f"  device OK: {lvl}, sys_id ...{str(d.system_id)[-4:]}")
        if d.security_level == 1:
            print("  note: L1 devices unlock the highest tiers (1080p+)")
        C.set(cfg, "wvd", os.path.abspath(wvd))
        break


def _ask_cookie(cfg: dict) -> None:
    """Prompt until a fresh, parseable session cookie is configured."""
    while True:
        raw = input("Session cookie (st=eyJ... , from F12 -> Network): ").strip()
        tok = C.cookie_token(raw)
        if tok:
            exp = C.cookie_expiry(tok)
            if exp and exp < time.time():
                print("  this cookie is expired; capture a fresh one")
                continue
            C.set(cfg, "cookie", raw)
            print("  cookie OK" + (" (expires {})".format(time.strftime(
                "%Y-%m-%d", time.gmtime(exp))) if exp else ""))
            break
        print("  expected an st= JWT token; try again")


def _ask_playback(cfg: dict) -> None:
    """Prompt until a HAR with a playback template is configured."""
    while True:
        harp = input("HAR file from the browser (F12 -> Network -> Save as HAR): "
                     ).strip()
        if not harp or not os.path.isfile(harp):
            print("  file not found; try again")
            continue
        try:
            url, hdrs, body, tid, chdrs = _extract_har(harp)
        except Exception as e:
            print(f"  {e}")
            continue
        C.set(cfg, "playback.url", url)
        C.set(cfg, "playback.headers", hdrs)
        C.set(cfg, "playback.body", body)
        C.set(cfg, "playback.template_edit_id", tid)
        C.set(cfg, "cms_headers", chdrs)
        print(f"  playback template captured ({len(hdrs)} headers)")
        break


def cmd_init(args: argparse.Namespace) -> int:
    """Guided first-time setup, finished with a doctor run."""
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
            if ask_yn(f"  {pkg} is missing. Install it now? (pip)", True):
                _pip_install(pkg)

    _ask_tool(cfg_global, "ffmpeg path", "ffmpeg", "-version")
    _ask_tool(cfg_global, "ffprobe path", "ffprobe", "-version")
    _ask_tool(cfg_global, "shaka-packager path", "shaka", "--version")

    _ask_device(cfg_global)
    _ask_cookie(cfg_global)
    _ask_playback(cfg_global)

    outdir = input("Output directory [default]: ").strip() \
        or cfg_global.get("outdir") or ""
    C.set(cfg_global, "outdir", outdir)
    try:
        bl = input("Download speed cap in kbps (0 = unlimited) [0]: ").strip()
        C.set(cfg_global, "bwlimit_kbps", int(bl or 0))
    except ValueError:
        C.set(cfg_global, "bwlimit_kbps", 0)

    p = C.save(cfg_global)
    print(f"\nConfig written to {p}")
    print()
    ok = D.doctor(cfg_global)
    print()
    print("init complete." if ok else "init finished -- fix the FAIL lines above.")
    return 0 if ok else 1


# ------------------------- command: prime -------------------------

PRIME_STEPS = """
Amazon Prime is capture-assisted: the browser performs one short playback,
and dashrip downloads, decrypts and files the content.

HAR flow (no pasting)
  1. Browser: WidevineProxy extension enabled, loaded with your .wvd
     device in WVD mode. Pick the content and play it for ~10 s.
  2. F12 -> Network -> right click -> "Save all as HAR with content".
  3. python -m dashrip prime <file.har>
     The MPD url, the keys, the subtitle tracks and the
     title/year/poster all come from the HAR.

Paste-a-command flow
  Steps 1-2 as above, then open the extension's History and copy the
  Command of the captured entry. Paste it when asked. Offer the HAR as
  well to keep subtitles and the automatic title/year.

The HAR yields the keys only when the proxy ran in WVD mode with the
device from your config; otherwise the keys come from the pasted command.
MPD urls expire after ~30 minutes, so recapture if a download fails.
"""


def _parse_n3u_cmd(line: str) -> dict:
    """Parse an N_m3u8DL-RE command line into its parts."""
    import shlex
    toks = shlex.split(line, posix=True)
    out: dict = {"exe": toks[0], "url": None, "headers": [], "keys": {}, "extra": []}
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
        elif ((t == "-M" or t in ("-sv", "-sa", "-se")) and
              i + 1 < len(toks)):
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


def _probe_prime_mpd(cfg: dict, url: str, headers: list) -> tuple[list, list]:
    """Probe the MPD for available qualities and audio codes."""
    import xml.etree.ElementTree as ET

    import requests
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
        ct = as_.get("contentType") or as_.get("mimeType") or ""
        lg = as_.get("lang") or ""
        for rep in as_.findall(M + "Representation"):
            if ct == "video" or "video" in ct:
                w, h = int(rep.get("width") or 0), int(rep.get("height") or 0)
                vids.append((w * h, f"{w}x{h}"))
            elif "audio" in (ct or ""):
                auds.append(lg or "und")
    return sorted(set(vids)), sorted(set(auds))


def _add_prime_dub(cfg: dict, mpd_url: str, headers: list, keys: dict,
                   lang: str, mkv: str, work: str, audio_idx: int) -> str | None:
    """Download an extra audio track from the Prime MPD, decrypt it, and
    remux it into the existing MKV as an extra audio track (no
    re-encode).  ``lang`` is the target language code (ISO 639-1 or
    BCP-47); ``audio_idx`` is the 0-based audio-stream index the new
    track will have in the resulting MKV.  Returns the new MKV path,
    or None if it could not be added."""
    import requests
    hdrs = {"User-Agent": K.UA,
            "Origin": "https://www.primevideo.com",
            "Referer": "https://www.primevideo.com/"}
    for h in headers:
        if ":" in h:
            k, v = h.split(":", 1)
            hdrs[k.strip()] = v.strip()
    try:
        r = requests.get(mpd_url, headers=hdrs, timeout=30)
        r.raise_for_status()
        parsed = K.parse_mpd(r.text)
    except Exception as e:
        log(f"  extra audio {lang}: cannot fetch/parse MPD ({e}); skipped")
        return None
    track = ""
    rel = ""
    want = str(lang).lower().split("-")[0]
    for lg, p in parsed["audio"].items():
        if lg.lower().split("-")[0] == want:
            track, rel = lg, p
            break
    if not track:
        log(f"  extra audio {lang}: not in the MPD; skipped")
        return None
    base = K.cdn_base(mpd_url)
    max_bps = int((cfg.get("bwlimit_kbps") or 0) * 1000 // 8)
    tag = track.split("-")[0][:3] or "und"
    src = os.path.join(work, f"dub_{tag}.mp4")
    dec = os.path.join(work, f"dub_{tag}.dec")
    try:
        K.dl(cfg, base + rel, src, max_bps)
        K.shaka_decrypt(cfg, src, dec, "audio", keys)
    except Exception as e:
        log(f"  extra audio {lang}: download/decrypt failed ({e}); skipped")
        return None
    out = os.path.join(work, "with_dub.mkv")
    ffmpeg = C.resolve_tool(cfg.get("ffmpeg") or "ffmpeg") or "ffmpeg"
    r2 = subprocess.run([ffmpeg, "-y", "-i", mkv, "-i", dec,
                         "-map", "0", "-map", "1:0", "-c", "copy",
                         f"-metadata:s:a:{audio_idx}",
                         f"language={tag}", out],
                        cwd=work, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
    if r2.returncode == 0 and os.path.exists(out):
        return out
    log(f"  extra audio {lang}: remux failed (ffmpeg exit {r2.returncode}); "
        f"skipped")
    return None


def _prime_har_context(cfg: dict, har_path: str,
                       wvd: str) -> tuple[str, list, list, dict, dict, str]:
    """Resolve MPD url, headers, subtitle tracks, title metadata and keys
    from a HAR capture. Every value falls back to empty when the capture
    is missing or unusable."""
    mpd_url = ""
    headers: list = []
    har_subs: list = []
    prime_meta: dict = {}
    keys: dict = {}
    original_hint = ""
    if har_path and not os.path.exists(har_path):
        log(f"HAR not found: {har_path}")
        har_path = ""
    if har_path:
        h = K.parse_prime_har(har_path)
        if h["error"]:
            log("  HAR: {}".format(h["error"]))
        har_subs = h["subs"]
        mpd_url = h["mpd_url"]
        headers = h["mpd_headers"]
        original_hint = h.get("original_lang", "")
        if har_subs:
            log(f"  HAR: {len(har_subs)} subtitle tracks available")
        if h.get("title_id"):
            prime_meta = K.fetch_prime_meta(h["title_id"])
            if prime_meta.get("title"):
                yr = prime_meta.get("year", "")
                log("  title: {}{}".format(prime_meta["title"],
                     f" ({yr})" if yr else ""))
            else:
                log("  could not resolve the title from Prime; will ask")
        if wvd:
            got, kerr = K.prime_keys_from_har(har_path, wvd)
            if kerr:
                log(f"  keys from HAR: {kerr}")
            else:
                keys = got or {}
                log(f"  keys: {len(keys)} extracted from the HAR")
        else:
            log("  no device file configured (config key: wvd); the keys "
                "come from the command")
    return mpd_url, headers, har_subs, prime_meta, keys, original_hint


def _prime_select_args(cfg: dict, parsed: dict | None, vids: list,
                       auds: list, q: str, a: str,
                       original_hint: str) -> tuple[list, str | None, list]:
    """Build the N_m3u8DL-RE selection arguments from the answered prompts:
    video quality, main audio track, and the extra audio tracks to add."""
    extra = []
    if parsed is not None:
        i, n = 0, len(parsed["extra"])
        while i < n:
            if parsed["extra"][i] == "-M":
                i += 2
                continue
            extra.append(parsed["extra"][i])
            i += 1
    if q:
        extra += ["-sv", 'res="{}*":for=best'.format(q.split("x")[0])]
    elif any(v[1].startswith("1280") for v in vids):
        extra += ["-sv", 'res="1280*":for=best']
    chosen_audio: str | None = a
    if not chosen_audio and auds:
        chosen_audio = K.resolve_original_lang(auds, original_hint)
    if chosen_audio:
        extra += ["-sa", f"lang={chosen_audio}:for=best"]
    extra_audio = []
    extra_cfg = cfg.get("extra_audio") or []
    if isinstance(extra_cfg, str):
        extra_cfg = [x.strip() for x in extra_cfg.split(",") if x.strip()]
    _seen = set()
    if auds and chosen_audio:
        for code in extra_cfg:
            want = str(code).strip().lower().split("-")[0]
            if not want or want in _seen:
                continue
            for x in auds:
                if x.lower().split("-")[0] == want \
                        and x.lower() != chosen_audio.lower():
                    _seen.add(want)
                    extra_audio.append(x)
                    break
    return extra, chosen_audio, extra_audio


def _prime_fetch(exe: str, mpd_url: str, headers: list, keys: dict,
                 extra: list, work: str) -> tuple[int, str | None]:
    """Run N_m3u8DL-RE for the capture. Returns (returncode, mkv path);
    the path is None when the run failed or produced no .mkv."""
    cmd = [exe, mpd_url]
    for h in headers:
        cmd += ["-H", h]
    for k, v in keys.items():
        cmd += ["--key", f"{k}:{v}"]
    cmd += [*extra, "-M", "format=mkv", "--save-dir", work]
    log(f"running: N_m3u8DL-RE + {len(headers)} headers + {len(keys)} keys")
    r = subprocess.run(cmd, cwd=work)
    if r.returncode != 0:
        return r.returncode, None
    for f in os.listdir(work):
        if f.endswith(".mkv"):
            return 0, os.path.join(work, f)
    return 0, None


def _embed_prime_subs(cfg: dict, work: str, mkv: str, sel_subs: list) -> tuple[str, list]:
    """Convert the selected TTML tracks to SRT and mux them into the rip.
    Returns (mkv path, embedded language codes); the original path and an
    empty list come back when there is nothing to embed or the mux fails."""
    if not sel_subs:
        return mkv, []
    ffmpeg = C.resolve_tool(cfg.get("ffmpeg") or "ffmpeg") or "ffmpeg"
    srt_inputs = []
    for s in sel_subs:
        txt = K.fetch_ttml(s["url"])
        srt = K.ttml_to_srt(txt)
        if not srt:
            log("  subtitle {}: nothing converted; skipped".format(s["code"]))
            continue
        sp = os.path.join(work, "sub_{}.srt".format(s["code"]))
        with open(sp, "w", encoding="utf-8") as f:
            f.write(srt)
        srt_inputs.append((sp, s["code"]))
    if not srt_inputs:
        return mkv, []
    out_mkv = os.path.join(work, "with_subs.mkv")
    cmd2 = [ffmpeg, "-y", "-i", mkv]
    for sp, code in srt_inputs:
        cmd2 += ["-i", sp]
    cmd2 += ["-map", "0"]
    for i in range(len(srt_inputs)):
        cmd2 += ["-map", f"{i + 1}:0"]
    cmd2 += ["-c", "copy", "-c:s", "srt"]
    for i in range(len(srt_inputs)):
        lang = srt_inputs[i][1].split("-")[0][:2] or "und"
        cmd2 += [f"-metadata:s:s:{i}", f"language={lang}"]
    cmd2 += [out_mkv]
    r2 = subprocess.run(cmd2, cwd=work)
    if r2.returncode == 0 and os.path.exists(out_mkv):
        codes = [c for _, c in srt_inputs]
        log("  subtitles embedded: {}".format(", ".join(codes)))
        return out_mkv, codes
    log(f"  subtitle remux failed (ffmpeg exit {r2.returncode}); "
        f"delivering without subtitles")
    return mkv, []


def _prompt_prime_delivery(prime_meta: dict) -> tuple[str, dict, dict]:
    """Ask for the delivery metadata: title, then either the movie year
    or the season/episode numbers. Returns (title, episode dict, meta)."""
    def_title = prime_meta.get("title", "")
    def_year = prime_meta.get("year", "")
    title = input(f"Title [{def_title or 'unknown'}]: ").strip() \
        or def_title or "unknown"
    ep_hint = prime_meta.get("episode") or {}
    is_series = input("Series? (y/N, only if this is an episode): ").strip().lower() \
        in ("y", "yes")
    meta = {}
    if not is_series:
        yr = input(f"Year [{def_year or 'blank'}]: ").strip() or def_year
        meta = {"premiereDate": yr + "-01-01"} if yr else {}
    v: dict = {}
    if is_series:
        v["season"] = int(input("Season [{}]: ".format(ep_hint.get("season", 1))).strip()
                           or ep_hint.get("season", 1))
        v["episode"] = int(input("Episode [{}]: ".format(ep_hint.get("episode", 1))).strip()
                            or ep_hint.get("episode", 1))
        v["name"] = input("Episode title [{}]: ".format(
            ep_hint.get("title") or "blank")).strip() \
            or ep_hint.get("title", "")
    return title, v, meta


def cmd_prime(args: argparse.Namespace) -> int:
    """Capture-assisted Prime rip of one title."""
    cfg = C.load()
    exe = C.resolve_tool(cfg.get("nm3u8dlre") or "N_m3u8DL-RE")
    if not exe:
        print("N_m3u8DL-RE not found. Install it and set its path in the")
        print("config (key: nm3u8dlre), or install it via the extension.")
        return 1
    print(PRIME_STEPS)
    wvd = (cfg.get("wvd") or "").strip()

    har_path = (args.har or "").strip().strip('"')
    if not har_path:
        har_path = input("HAR file (full automation; blank = paste a "
                         "command): ").strip().strip('"')

    (mpd_url, headers, har_subs, prime_meta, keys,
     original_hint) = _prime_har_context(cfg, har_path, wvd)
    parsed = None

    if not mpd_url or not keys:
        line = input("Paste the N_m3u8DL-RE command: ").strip()
        if not line:
            print("Nothing to do.")
            return 1
        parsed = _parse_n3u_cmd(line)
        if not parsed["url"]:
            print("Could not find the MPD URL in that command.")
            return 1
        if not mpd_url:
            mpd_url = parsed["url"]
        if not headers:
            headers = parsed["headers"]
        if not keys:
            keys = parsed["keys"]
    log(f"MPD: {mpd_url[:100]}...")

    try:
        vids, auds = _probe_prime_mpd(cfg, mpd_url, headers)
    except Exception as e:
        log(f"could not probe the MPD ({e!r}); will ask N_m3u8DL-RE to pick")
        vids, auds = [], []
    if vids:
        print("  qualities: {}".format(", ".join(v[1] for v in vids)))
        q = input("  quality (blank = highest 720p-or-below): ").strip()
    else:
        q = ""
    if auds:
        orig = K.resolve_original_lang(auds, original_hint)
        print("  audio: {}".format(", ".join(auds)))
        a = input(f"  audio (blank = original: {orig}; or a code) ").strip()
    else:
        a = ""

    extra, chosen_audio, extra_audio = _prime_select_args(
        cfg, parsed, vids, auds, q, a, original_hint)

    sel_subs = []
    if har_subs:
        codes = [s["code"] for s in har_subs]
        picked = _pick_langs("Subtitles", codes, True, "",
                             default_policy=codes)
        sel_subs = [s for s in har_subs if s["code"] in picked]

    want_poster = False
    if prime_meta.get("poster"):
        want_poster = input("Download poster? [Y/n]: ").strip().lower() \
            in ("", "y", "yes")

    run_dir = os.path.join(_workdir(), "prime_{}".format(time.strftime("%Y%m%d_%H%M%S")))
    os.makedirs(run_dir, exist_ok=True)
    work = run_dir
    rc, mkv = _prime_fetch(exe, mpd_url, headers, keys, extra, work)
    if rc != 0:
        print(f"N_m3u8DL-RE exited with {rc}")
        if parsed is None:
            log("if this HAR is older than ~30 minutes, the MPD url has "
                "expired; make a fresh capture")
        return 1
    if not mkv:
        print("No .mkv found in the work dir; check the output above.")
        return 1

    mkv, embedded_subs = _embed_prime_subs(cfg, work, mkv, sel_subs)

    extra_added = []
    for i, code in enumerate(extra_audio):
        dub_mkv = _add_prime_dub(cfg, mpd_url, headers, keys, code, mkv,
                                 run_dir, 1 + i)
        if dub_mkv:
            mkv = dub_mkv
            extra_added.append(code)
            log(f"  extra audio {code} embedded")

    info = K.probe(cfg, mkv)
    title, v, meta = _prompt_prime_delivery(prime_meta)
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
            log(f"  poster: skipped ({type(ex).__name__}: {ex})")
    audio_list = []
    if chosen_audio:
        audio_list.append((chosen_audio, ""))
    for code in extra_added:
        audio_list.append((code, ""))
    shutil.move(mkv, final)
    S.manifest_append(outdir, S.make_entry(
        outdir, title, v, final, {"video_wh": ""}, info,
        audio_list,
        [(c, "") for c in embedded_subs], [], False))
    S.cleanup(run_dir)
    print(f"Delivered: {final}")
    return 0

# ------------------------- command: sync -------------------------

def cmd_sync(args: argparse.Namespace) -> int:
    """Dispatch the sync subcommand; returns its exit code."""
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
    return sync.main([args.cmd or "sync", *argv])


# ------------------------- main -------------------------

def build_parser() -> argparse.ArgumentParser:
    """Argument parser for the dashrip command line."""
    p = argparse.ArgumentParser(
        prog="dashrip",
        description="Rip DRM-protected streaming content (Widevine) to MKV.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("init", help="first-time guided setup")
    sub.add_parser("doctor", help="health check")
    pm = sub.add_parser("max", help="rip HBO Max content")
    pm.add_argument("queries", nargs="*",
                    help="URLs, titles or UUIDs (blank = interactive)")
    pm.add_argument("--audio", default="", help="audio langs, e.g. en,de")
    pm.add_argument("--subs", default="", help="subtitle langs, e.g. en,de")
    pm.add_argument("--embed", default="", help="1 = embed, 2 = sidecar files")
    pm.add_argument("--poster", default="",
                    help="poster policy: yes/no (blank = ask)")
    pm.add_argument("--ep", default="", help="episode spec, e.g. 1-6 or S01E02")
    pm.add_argument("--outdir", default="", help="output directory")
    pm.add_argument("--debug", action="store_true",
                     help="print full tracebacks on errors")
    pp = sub.add_parser("prime", help="rip an Amazon Prime capture")
    pp.add_argument("har", nargs="?", default="",
                    help="HAR capture (full automation, no pasting)")
    ps = sub.add_parser("sync", help="lossless resumable folder transfer")
    ps.add_argument("--src", help="local source dir")
    ps.add_argument("--host", help="ssh host")
    ps.add_argument("--user", help="ssh user")
    ps.add_argument("--key", help="ssh private key path")
    ps.add_argument("--target", help="remote target dir")
    ps.add_argument("--cmd", help="sync|status|verify (default sync)")
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse, dispatch, return the exit code."""
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
