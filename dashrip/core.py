"""dashrip.core -- DRM (Widevine) streaming pipeline.

HBO Max is fully automatic: show id -> playbackInfo -> MPD -> license ->
content keys -> DASH download -> decrypt -> MKV. Amazon Prime is
capture-assisted (see cli.prime).

All per-user values (device file, cookie, playback template) come from
dashrip.config. Nothing user-specific is hard-coded here.
"""

import base64
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

import requests

import dashrip.config as cfg

CMS_BASE = "https://default.any-emea.prd.api.hbomax.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
ORIGIN = "https://play.hbomax.com"
WV_SYSTEM_ID = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
M = '{urn:mpeg:dash:schema:mpd:2011}'


def log(msg):
    print("[dashrip] " + msg, flush=True)


# --------------------------------------------------------------------------
# CMS (catalog)
# --------------------------------------------------------------------------

def _cms_headers(cfg_obj):
    """Headers for CMS requests: the cookie plus the capture headers that
    the user's browser sent (x-disco-*, x-device-info, ...). The cookie is
    the only mandatory part; capture headers are optional metadata."""
    h = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
    }
    cookie = cfg.get(cfg_obj, "cookie")
    if cookie:
        h["Cookie"] = cookie
    for k, v in (cfg.get(cfg_obj, "cms_headers") or {}).items():
        h[k] = v
    return h


def resolve(cfg_obj, show_id):
    """Fetch a show/film page. Returns (show_meta, videos).

    show_meta: the item dict matching show_id (may be None).
    videos:    list of dicts {editId, vid, name, season, episode,
              duration, airDate, desc}; trailers/previews are skipped.
    """
    url = "%s/cms/routes/show/%s?include=default&decorators=badges" % (
        CMS_BASE, show_id)
    r = requests.get(url, headers=_cms_headers(cfg_obj), timeout=30)
    r.raise_for_status()
    data = r.json()

    show_meta = None
    videos = []
    for item in data.get("included", []):
        t = item.get("type")
        if t == "show" and item.get("id") == show_id:
            show_meta = item
        elif t == "video":
            rel = item.get("relationships", {})
            if "edit" not in rel:
                continue
            edit_id = rel["edit"]["data"]["id"]
            name = (item.get("attributes", {}) or {}).get("title", "")
            if name.startswith(("trailer:", "short-preview:")):
                continue
            attr = item.get("attributes", {}) or {}
            videos.append({
                "editId": edit_id,
                "vid": item.get("id"),
                "name": name,
                "season": attr.get("seasonNumber"),
                "episode": attr.get("episodeNumber"),
                "duration": attr.get("duration", 0),
                "airDate": attr.get("premiereDate") or attr.get("releaseDate") or "",
                "desc": attr.get("synopsis", ""),
            })
    videos.sort(key=lambda v: (v.get("season") or 0, v.get("episode") or 0))
    return show_meta, videos


def show_images(cfg_obj, show_id):
    """Poster URLs from the CMS.

    Returns (show_poster_url, {video_item_id: episode_poster_url}).
    Show art prefers portrait cards with title; episode art prefers the
    tall centered card, then generic artwork.
    """
    url = "%s/cms/routes/show/%s?include=default" % (CMS_BASE, show_id)
    r = requests.get(url, headers=_cms_headers(cfg_obj), timeout=30)
    r.raise_for_status()
    data = r.json()

    def pick_kinds(urls, prefs):
        for kind in prefs:
            for u in urls:
                if u.get("kind") == kind:
                    return u.get("src")
        return None

    by_id = {i.get("id"): i for i in data.get("included", [])}
    show_poster = None
    ep_posters = {}
    for item in data.get("included", []):
        rel = item.get("relationships", {})
        imgs = [by_id[x["id"]] for x in (rel.get("images", {}).get("data") or [])
                if x["id"] in by_id]
        urls = [i.get("attributes", {}) or {} for i in imgs]
        if item.get("type") == "show" and item.get("id") == show_id:
            show_poster = pick_kinds(urls, ("poster-with-logo",
                                            "centered-background-small",
                                            "default"))
        elif item.get("type") == "video":
            u = pick_kinds(urls, ("centered-background-small", "default",
                                  "cover-artwork"))
            if u:
                ep_posters[item.get("id")] = u
    return show_poster, ep_posters


def fetch_image(cfg_obj, url, dest):
    """Download a poster; skip if already present and non-empty."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return False
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    with open(dest, "wb") as f:
        f.write(r.content)
    return True


# --------------------------------------------------------------------------
# Playback / manifest / keys
# --------------------------------------------------------------------------

def playback(cfg_obj, edit_id):
    """POST the configured playbackInfo template with this edit id.

    The template (url, headers, body) was captured from the user's browser
    during `dashrip init`; the edit id is swapped in place of the captured
    one. Returns (mpd_url, license_url, service_cert).
    """
    url = cfg.get(cfg_obj, "playback.url")
    body = cfg.get(cfg_obj, "playback.body")
    if not url or not body:
        raise RuntimeError("playback template missing -- run `dashrip init` "
                           "and capture a playbackInfo request")
    tmpl = cfg.get(cfg_obj, "playback.template_edit_id")
    if tmpl:
        body = body.replace(tmpl, edit_id)
    else:
        # fall back to replacing the last quoted 8-4-4-4-12 uuid in the body
        m = None
        for m in re.finditer(r'"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}'
                             r'-[0-9a-f]{4}-[0-9a-f]{12}"', body):
            pass
        if m:
            body = body[:m.start()] + '"%s"' % edit_id + body[m.end():]
        else:
            raise RuntimeError("cannot locate edit id in playback body")
    headers = {"User-Agent": UA, "Origin": ORIGIN,
               "Referer": ORIGIN + "/", "Content-Type": "application/json"}
    for k, v in (cfg.get(cfg_obj, "playback.headers") or {}).items():
        headers[k] = v
    cookie = cfg.get(cfg_obj, "cookie")
    if cookie:
        headers["Cookie"] = cookie
    r = requests.post(url, headers=headers, data=body.encode("utf-8"),
                      timeout=60)
    r.raise_for_status()
    d = r.json()
    manifest = d["manifest"]["url"]
    lic = d["drm"]["schemes"]["widevine"]["licenseUrl"]
    # service certificate is dynamic: it is part of every playbackInfo
    # response, so there is no static cert file to ship.
    cert = d["drm"]["schemes"]["widevine"].get("widevineServiceCertificate")
    return manifest, lic, cert


def fetch_mpd(cfg_obj, mpd_url):
    h = {"User-Agent": UA, "Origin": ORIGIN, "Referer": ORIGIN + "/"}
    cookie = cfg.get(cfg_obj, "cookie")
    if cookie:
        h["Cookie"] = cookie
    r = requests.get(mpd_url, headers=h, timeout=60)
    r.raise_for_status()
    return r.text


def cdn_base(mpd_url):
    """CDN base URL for relative segment paths: strip the trailing path
    element (and query) from the manifest URL."""
    u = mpd_url.split("?")[0]
    return u.rsplit("/", 1)[0] + "/"


def parse_mpd(mpd_xml):
    """Parse the DASH manifest.

    Content periods are those whose video representations carry relative
    BaseURLs (the platform serves one complete file per track; ad/bumper
    periods use explicit external base urls). Parsed with ElementTree using
    the real MPD element casing (<BaseURL>) and the cenc-namespaced <pssh>.

    Returns dict with video_base/video_id/video_wh, audio {lang: rel_path},
    subs [{lang, base, template, nums}], and the Widevine PSSH bytes.
    """
    root = ET.fromstring(mpd_xml)
    periods = root.findall(M + 'Period')
    content = []
    for p in periods:
        vrel = False
        has_audio = False
        for as_ in p.findall(M + 'AdaptationSet'):
            ct = as_.get('contentType')
            if ct == 'audio':
                has_audio = True
            if ct == 'video':
                for rep in as_.findall(M + 'Representation'):
                    bu = rep.find(M + 'BaseURL')
                    bu = (bu.text or '').strip() if bu is not None else ''
                    if bu and not bu.startswith('http'):
                        vrel = True
        if vrel and has_audio:
            content.append(p)
    if not content:
        raise RuntimeError('no content period found in MPD')

    best = None
    for p in content:
        for as_ in p.findall(M + 'AdaptationSet'):
            if as_.get('contentType') != 'video':
                continue
            for rep in as_.findall(M + 'Representation'):
                w = int(rep.get('width') or 0)
                h = int(rep.get('height') or 0)
                bu = rep.find(M + 'BaseURL')
                bu = (bu.text or '').strip() if bu is not None else ''
                if not bu or bu.startswith('http'):
                    continue
                if best is None or w * h > best[0]:
                    best = (w * h, w, h, bu, rep.get('id'))
    if not best:
        raise RuntimeError('no content video rep')
    v_area, v_w, v_h, v_bu, v_id = best

    audio = {}
    for p in content:
        for as_ in p.findall(M + 'AdaptationSet'):
            if as_.get('contentType') != 'audio':
                continue
            lg = as_.get('lang') or 'und'
            rep = as_.find(M + 'Representation')
            if rep is None:
                continue
            bu = rep.find(M + 'BaseURL')
            bu = (bu.text or '').strip() if bu is not None else ''
            if not bu or bu.startswith('http'):
                continue
            if lg not in audio:
                audio[lg] = bu

    subs = {}
    for p in content:
        for as_ in p.findall(M + 'AdaptationSet'):
            if as_.get('contentType') != 'text':
                continue
            lg = as_.get('lang') or 'und'
            rep = as_.find(M + 'Representation')
            if rep is None:
                continue
            st = rep.find(M + 'SegmentTemplate')
            if st is None:
                continue
            media = st.get('media') or ''
            base = re.sub(r'\$Number\$\.\w+$', '', media).rstrip('/')
            key = (lg, base)
            tr = subs.setdefault(
                key, {'lang': lg, 'base': base, 'template': media, 'nums': []})
            tr['nums'].append(int(st.get('startNumber') or 1))
    for tr in subs.values():
        tr['nums'] = sorted(set(tr['nums']))

    pssh_b64 = None
    for cp in root.iter():
        if (cp.get('schemeIdUri') or '').lower().endswith(WV_SYSTEM_ID):
            el = cp.find('{urn:mpeg:cenc:2013}pssh')
            if el is not None and el.text:
                pssh_b64 = el.text.strip()
                break
    if not pssh_b64:
        raise RuntimeError('Widevine PSSH not found')

    return {
        'video_base': v_bu,
        'video_id': v_id,
        'video_wh': '%dx%d' % (v_w, v_h),
        'audio': audio,
        'subs': list(subs.values()),
        'pssh': base64.b64decode(pssh_b64),
    }


def get_keys(cfg_obj, pssh, service_cert, lic_url):
    """Run the Widevine exchange with the configured device file.

    Loads the .wvd device, opens a session, sets the (dynamic) service
    certificate, generates the challenge, POSTs it to the license url and
    returns {kid_hex: key_hex}.
    """
    from pywidevine.device import Device
    from pywidevine.cdm import Cdm
    from pywidevine.pssh import PSSH

    dev = Device.load(cfg_obj["wvd"])
    cdm = Cdm.from_device(dev)
    sid = cdm.open()
    try:
        cert = service_cert
        if not cert:
            sc = cfg.get(cfg_obj, "service_cert")
            if sc and os.path.isfile(sc):
                with open(sc, "rb") as f:
                    cert = f.read()
        if cert:
            cdm.set_service_certificate(sid, cert)
        pssh_obj = PSSH(pssh)
        challenge = cdm.get_license_challenge(sid, pssh_obj)
        h = {"User-Agent": UA, "Origin": ORIGIN,
             "Referer": ORIGIN + "/", "Content-Type": "application/octet-stream"}
        cookie = cfg.get(cfg_obj, "cookie")
        if cookie:
            h["Cookie"] = cookie
        r = requests.post(lic_url, headers=h, data=challenge, timeout=60)
        if r.status_code != 200:
            raise RuntimeError("license request failed: HTTP %s" % r.status_code)
        # parse_license stores the keys in the session and returns None
        # (pywidevine >= 1.9); read them back with cdm.get_keys.
        cdm.parse_license(sid, r.content)
        keys = {}
        for k in cdm.get_keys(sid):
            keys[k.kid.hex] = k.key.hex() if k.key else "0" * 32
        return keys
    finally:
        cdm.close(sid)


def keys_arg(keys):
    """Build shaka-packager --keys argument.

    Widevine KID byte[1] is the key tier; the same 14-byte key body can
    appear under several tier prefixes (0100-0105). The license answers
    under one tier, but the file's tenc box may reference another, so each
    real 16-byte key is registered under all six tier variants.
    """
    expanded = {}
    for kid, k in keys.items():
        if not (k and len(k) == 32 and k != "0" * 32):
            continue
        expanded[kid] = k
        try:
            body = bytes.fromhex(kid)[2:]
        except ValueError:
            continue
        for tier in (0x00, 0x01, 0x02, 0x03, 0x04, 0x05):
            expanded.setdefault((b'\x01' + bytes([tier]) + body).hex(), k)
    parts = ["label=wv%d:key_id=%s:key=%s" % (i, kid, k)
             for i, (kid, k) in enumerate(expanded.items())]
    return ",".join(parts)


# --------------------------------------------------------------------------
# Download / decrypt / mux helpers
# --------------------------------------------------------------------------

def dl(cfg_obj, url, path, max_bps=0):
    """Download with resume (.part), Range support and throttling.

    Transient failures (CDN edge hiccups, dropped streams) are retried
    with a short backoff, resuming from the .part file. max_bps in
    bytes/sec, 0 = unlimited. Returns (new_bytes, total).
    """
    part = path + ".part"
    start_pos = os.path.getsize(part) if os.path.exists(part) else 0
    h = {"User-Agent": UA, "Origin": ORIGIN, "Referer": ORIGIN + "/"}
    cookie = cfg.get(cfg_obj, "cookie")
    if cookie:
        h["Cookie"] = cookie
    total = None
    for attempt in range(6):
        pos = os.path.getsize(part) if os.path.exists(part) else 0
        hh = dict(h)
        if pos:
            hh["Range"] = "bytes=%d-" % pos
        r = None
        try:
            r = requests.get(url, headers=hh, stream=True, timeout=60)
            if r.status_code == 416:
                # stale .part (pos >= file size): restart from zero
                r.close()
                os.remove(part)
                continue
            if r.status_code not in (200, 206):
                raise RuntimeError("HTTP %s" % r.status_code)
            if r.status_code == 200:
                pos = 0
            cr = r.headers.get("Content-Range")
            if cr and "/" in cr:
                total = int(cr.rsplit("/", 1)[1])
            elif r.status_code == 200:
                total = int(r.headers.get("Content-Length", 0)) or None
            mode = "ab" if pos else "wb"
            done = pos
            last_log = time.time()
            with open(part, mode) as f:
                for chunk in r.iter_content(1 << 20):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if max_bps:
                        time.sleep(len(chunk) / max_bps)
                    if time.time() - last_log > 5:
                        done_mb = done // (1 << 20)
                        tot = " / %dMB" % (total // (1 << 20)) if total else ""
                        log("  dl %s%s ... %d%%" % (
                            os.path.basename(path)[:40], tot,
                            done * 100 // total if total else -1))
                        last_log = time.time()
            r.close()
            break
        except Exception as ex:
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass
            if attempt == 5:
                raise RuntimeError(
                    "download failed after 6 attempts: %s" % ex)
            wait = 3 + attempt * 2
            log("  dl %s: %s -- retry %d/6 in %ds" % (
                os.path.basename(path)[:40], ex, attempt + 1, wait))
            time.sleep(wait)
    os.replace(part, path)
    return os.path.getsize(path) - start_pos, total


def shaka_decrypt(cfg_obj, inp, out, kind, keys):
    """Decrypt one DASH track with shaka-packager raw key decryption."""
    shaka = cfg.resolve_tool(cfg.get(cfg_obj, "shaka"))
    cmd = [shaka, "input=%s,stream=%s,out=%s" % (inp, kind, out),
           "--enable_raw_key_decryption", "--keys=" + keys_arg(keys)]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("shaka decrypt failed (rc=%d): %s" % (
            p.returncode, p.stderr.decode("utf-8", "replace")[-800:]))


def merge_subs(cfg_obj, segs):
    """Concatenate VTT segments.

    The platform emits segments with global 0-based cue timestamps, so no
    rebasing is needed; cues are renumbered sequentially. segs is
    [(path, out_name)]; returns {out_name: text}.
    """
    out = {}
    for path, name in segs:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
        blocks = [b for b in txt.split("WEBVTT\n\n") if b.strip()]
        cues = []
        for b in blocks:
            # a block may hold several cues, separated by blank lines
            for cue in b.strip("\n").split("\n\n"):
                if not cue.strip():
                    continue
                lines = cue.split("\n")
                # a cue may already start with its sequence number
                if lines and lines[0].isdigit():
                    lines = lines[1:]
                cues.append("\n".join(lines))
        out[name] = "WEBVTT\n\n" + "\n\n".join(
            "%d\n%s" % (i + 1, c) for i, c in enumerate(cues)) + "\n"
    return out


def probe(cfg_obj, path):
    """ffprobe summary: duration + codec list."""
    ffprobe = cfg.resolve_tool(cfg.get(cfg_obj, "ffprobe"))
    p = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name,width,height",
         "-of", "json", path], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("ffprobe failed: %s" % p.stderr.decode("utf-8", "replace"))
    return json.loads(p.stdout.decode("utf-8"))


def mux(cfg_obj, out, vdec, aud, subfiles):
    """Mux video + audio + subs into an MKV (stream copy, no re-encode).

    aud and subfiles are lists of (lang, path). Languages get proper
    MKV tags so players can list/select them.
    """
    ffmpeg = cfg.resolve_tool(cfg.get(cfg_obj, "ffmpeg"))
    cmd = [ffmpeg, "-y", "-i", vdec]
    for _lg, ap in aud:
        cmd += ["-i", ap]
    for _lg, vp in subfiles:
        cmd += ["-i", vp]
    cmd += ["-map", "0:v"]
    ai = 1
    for lg, _ap in aud:
        cmd += ["-map", "%d:a" % ai, "-metadata:s:a:%d" % (ai - 1),
                "language=" + (lg[:2] or "und")]
        ai += 1
    si = ai
    for lg, _vp in subfiles:
        cmd += ["-map", "%d:s" % si, "-metadata:s:s:%d" % (si - ai),
                "language=" + (lg[:2] or "und")]
        si += 1
    cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "webvtt", out]
    subprocess.run(cmd, check=True, capture_output=True)


def quick_decode(cfg_obj, path, seconds=15):
    """Decode a few seconds and report whether any error came out."""
    ffmpeg = cfg.resolve_tool(cfg.get(cfg_obj, "ffmpeg"))
    r = subprocess.run(
        [ffmpeg, "-v", "error", "-t", str(seconds), "-i", path, "-f", "null", "-"],
        capture_output=True, text=True)
    return not r.stderr.strip()
