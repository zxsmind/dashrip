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


def log(msg: str) -> None:
    """Print a message with the dashrip prefix."""
    print(f"[dashrip] {msg}", flush=True)


# ------------------------- small helpers -------------------------

def safe_dir(name: str) -> str:
    """Make a string safe for use as a directory/file name component.

    Filesystem-invalid characters (e.g. ':') become a space and runs of
    whitespace are collapsed, so 'Rick and Morty: The Anime' -> 'Rick and
    Morty The Anime' (no stray underscores).
    """
    s = re.sub(r'[\\/:*?"<>|]', ' ', name or '')
    s = re.sub(r'\s+', ' ', s).strip()
    return s or 'untitled'


def year_of(meta: dict | None) -> str:
    """Extract the 4-digit year from a CMS `premiereDate` (or similar)."""
    d = (meta or {}).get('premiereDate') or ''
    m = re.match(r'(\d{4})', d)
    return m.group(1) if m else ''


def slug(title: str | None) -> str:
    """Dotted slug for file names: 'The Matrix' -> 'The.Matrix'."""
    return re.sub(r'[^\w.\-]+', '.', (title or '')).strip('.')


def work_name(title: str, v: dict) -> str:
    """Working file name (no quality suffix): 'Title' or 'Title.S01E02'."""
    t = slug(title)
    if v.get('season') is not None:
        t += f".S{v['season'] or 0:02d}E{v['episode'] or 0:02d}"
    return t


# ------------------------- directory layout -------------------------

def title_dir(outdir: str, title: str, meta: dict | None, is_series: bool) -> str:
    """Top-level content folder (Movies/<Title> (Year) or Series/<Show>)."""
    if is_series:
        return os.path.join(outdir, 'Series', safe_dir(title))
    yr = year_of(meta)
    return os.path.join(outdir, 'Movies',
                        safe_dir(title) + ((f' ({yr})') if yr else ''))


def ep_dir(outdir: str, title: str, v: dict, single_season: bool) -> str:
    """Per-episode folder under Series/<Show>/Season NN/Episode NN - <Name>."""
    seas = 1 if single_season else (v['season'] or 1)
    epn = v['episode'] or 0
    epname = safe_dir(v.get('name') or '') or f'Episode {epn:02d}'
    return os.path.join(outdir, 'Series', safe_dir(title),
                        f'Season {seas:02d}', f'Episode {epn:02d} - {epname}')


def final_name(title: str, v: dict, single_season: bool) -> str:
    """Final MKV file name for a movie or an episode."""
    if v.get('season') is None:
        return slug(title) + '.720p.mkv'
    seas = 1 if single_season else (v['season'] or 1)
    epn = v['episode'] or 0
    epname = safe_dir(v.get('name') or '') or f'Episode {epn:02d}'
    return f'S{seas:02d}E{epn:02d} - {epname}.720p.mkv'


def final_path(outdir: str, title: str, v: dict, meta: dict | None, single_season: bool) -> str:
    """Full destination path for the finished MKV (dir + name)."""
    if v.get('season') is None:
        d = title_dir(outdir, title, meta, False)
    else:
        d = ep_dir(outdir, title, v, single_season)
    return os.path.join(d, final_name(title, v, single_season))


def poster_dirs(outdir: str, title: str, v: dict, meta: dict | None,
                single_season: bool) -> tuple[str, str | None]:
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

def sha256_of(path: str) -> str:
    """Hex SHA-256 of a file, read in 1 MiB chunks."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def manifest_path(outdir: str) -> str:
    """Path of the manifest file inside a library folder."""
    return os.path.join(outdir, 'dashrip_manifest.json')


def manifest_append(outdir: str, entry: dict) -> None:
    """Add (or replace-by-file) an entry in the library manifest."""
    os.makedirs(outdir, exist_ok=True)
    mf = manifest_path(outdir)
    data = []
    if os.path.exists(mf):
        try:
            with open(mf, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log(f"manifest unreadable ({e}), starting from an empty list")
            data = []
    data = [e for e in data if e.get('file') != entry.get('file')]
    data.append(entry)
    with open(mf, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1, ensure_ascii=False)


def manifest_load(outdir: str) -> list:
    """Manifest entries of a folder; [] when absent or unreadable."""
    mf = manifest_path(outdir)
    if not os.path.exists(mf):
        return []
    try:
        with open(mf, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"manifest unreadable ({e}), treating it as empty")
        return []


# ------------------------- cleanup -------------------------

def cleanup(dirp: str) -> None:
    """Remove a working directory (transient, already-verified). Never
    touches the final library output."""
    shutil.rmtree(dirp, ignore_errors=True)


def make_entry(outdir: str, title: str, v: dict, final: str, parsed: dict,
               info: dict, aud: list, subfiles: list, sidecars: list,
               single_season: bool = False) -> dict:
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
