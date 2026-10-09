"""Library layout, manifest bookkeeping, and cleanup.

This module owns *where* finished media lands on disk and the
human-readable manifest that records every deliverable. It is
dependency-free (standard library only) so it can be imported
without pulling in the network/DRM pipeline.

Layout (matches the "standard media library" convention):

    <out>/Movies/<Title> (<Year>)/<Title>.720p.mkv
    <out>/Series/<Show>/Season NN/Episode NN - <EpTitle>/S<NN>E<NN> - <EpTitle>.720p.mkv
    <out>/Series/<Show>/poster.jpg          (show poster)
    <out>/Series/<Show>/Season NN/Episode NN - <EpTitle>/poster.jpg  (episode art)

Every finished item is recorded in <out>/dashrip_manifest.json with its
path, size, SHA-256, duration, audio/subtitle languages, and date.
"""

import hashlib
import json
import os
import re
import shutil
import time


# ------------------------- small helpers -------------------------

def safe_dir(name):
    """Make a string safe for use as a directory/file name component.

    Filesystem-invalid characters (e.g. ':') become a space and runs of
    whitespace are collapsed, so 'Rick and Morty: The Anime' -> 'Rick and
    Morty The Anime' (no stray underscores).
    """
    s = re.sub(r'[\\/:*?"<>|]', ' ', name or '')
    s = re.sub(r'\s+', ' ', s).strip()
    return s or 'untitled'


def year_of(meta):
    """Extract the 4-digit year from a CMS `premiereDate` (or similar)."""
    d = (meta or {}).get('premiereDate') or ''
    m = re.match(r'(\d{4})', d)
    return m.group(1) if m else ''


def slug(title):
    """Dotted slug for file names: 'The Matrix' -> 'The.Matrix'."""
    return re.sub(r'[^\w.\-]+', '.', (title or '')).strip('.')


def work_name(title, v):
    """Working file name (no quality suffix): 'Title' or 'Title.S01E02'."""
    t = slug(title)
    if v.get('season') is not None:
        t += '.S%02dE%02d' % (v['season'] or 0, v['episode'] or 0)
    return t


# ------------------------- directory layout -------------------------

def title_dir(outdir, title, meta, is_series):
    """Top-level content folder (Movies/<Title> (Year) or Series/<Show>)."""
    if is_series:
        return os.path.join(outdir, 'Series', safe_dir(title))
    yr = year_of(meta)
    return os.path.join(outdir, 'Movies',
                        safe_dir(title) + ((' (%s)' % yr) if yr else ''))


def ep_dir(outdir, title, v, single_season):
    """Per-episode folder under Series/<Show>/Season NN/Episode NN - <Name>."""
    seas = 1 if single_season else (v['season'] or 1)
    epn = v['episode'] or 0
    epname = safe_dir(v.get('name') or '') or ('Episode %02d' % epn)
    return os.path.join(outdir, 'Series', safe_dir(title),
                        'Season %02d' % seas, 'Episode %02d - %s' % (epn, epname))


def final_name(title, v, single_season):
    """Final MKV file name for a movie or an episode."""
    if v.get('season') is None:
        return slug(title) + '.720p.mkv'
    seas = 1 if single_season else (v['season'] or 1)
    epn = v['episode'] or 0
    epname = safe_dir(v.get('name') or '') or ('Episode %02d' % epn)
    return 'S%02dE%02d - %s.720p.mkv' % (seas, epn, epname)


def final_path(outdir, title, v, meta, single_season):
    """Full destination path for the finished MKV (dir + name)."""
    if v.get('season') is None:
        d = title_dir(outdir, title, meta, False)
    else:
        d = ep_dir(outdir, title, v, single_season)
    return os.path.join(d, final_name(title, v, single_season))


def poster_dirs(outdir, title, v, meta, single_season):
    """Folders for artwork.

    Returns (content_dir, episode_dir_or_None).
      * movie  -> (Movies/<Title> (Year)/, None)
      * series -> (Series/<Show>/, Series/<Show>/Season NN/Episode NN - <Name>/)

    A show poster goes in content_dir; episode art goes in episode_dir.
    """
    if v.get('season') is None:
        return title_dir(outdir, title, meta, False), None
    return title_dir(outdir, title, None, True), ep_dir(outdir, title, v, single_season)


# ------------------------- hashing / manifest -------------------------

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def manifest_path(outdir):
    return os.path.join(outdir, 'dashrip_manifest.json')


def manifest_append(outdir, entry):
    """Add (or replace-by-file) an entry in the library manifest."""
    os.makedirs(outdir, exist_ok=True)
    mf = manifest_path(outdir)
    data = []
    if os.path.exists(mf):
        try:
            data = json.load(open(mf, encoding='utf-8'))
        except Exception:
            data = []
    data = [e for e in data if e.get('file') != entry.get('file')]
    data.append(entry)
    json.dump(data, open(mf, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)


def manifest_load(outdir):
    mf = manifest_path(outdir)
    if not os.path.exists(mf):
        return []
    try:
        return json.load(open(mf, encoding='utf-8'))
    except Exception:
        return []


# ------------------------- cleanup -------------------------

def cleanup(dirp):
    """Remove a working directory (transient, already-verified). Never
    touches the final library output."""
    try:
        shutil.rmtree(dirp, ignore_errors=True)
    except Exception:
        pass


def make_entry(outdir, title, v, final, parsed, info, aud, subfiles, sidecars,
               single_season=False):
    """Build a manifest entry for a finished item."""
    seas = None if v.get('season') is None else (1 if single_season else (v['season'] or 1))
    epn = v.get('episode')
    epname = v.get('name')
    return {
        'title': title,
        'season': seas,
        'episode': epn,
        'episode_name': epname,
        'air_date': v.get('airDate'),
        'file': final,
        'size': os.path.getsize(final),
        'sha256': sha256_of(final),
        'video': parsed.get('video_wh'),
        'duration_s': float(info.get('format', {}).get('duration') or 0),
        'audio': [a[0] for a in aud],
        'subs': [s[0] for s in subfiles] + [s[0] for s in sidecars],
        'sidecars': sidecars,
        'date': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
