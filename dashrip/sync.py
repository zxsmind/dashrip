"""Lossless, resumable folder transfer over scp.

Strategy
--------
* Every file is verified BYTE-FOR-BYTE: local SHA-256 -> remote check
  (size + hash) -> scp copy -> remote SHA-256 verification.
* Resume: re-run the same command. Files already verified are skipped
  (checkpoint), a partially copied or corrupted file is detected by the
  hash mismatch and re-sent.
* Sleep / network drop / corruption: just re-run the same command.
* Speed: the built-in OpenSSH ``scp`` client (much faster than an SFTP
  write loop; no extra software needed on either side).

Implementation notes (hard-won)
-------------------------------
* Windows OpenSSH scp does NOT strip single quotes from the remote path
  (they arrive literally), so the remote path is passed to scp UNQUOTED
  (spaces are fine). Quoting is only used inside remote bash commands.
* scp cannot create intermediate directories, so every unique parent
  directory is created on the remote first (``mkdir -p``).
* If a stale directory with the destination name exists remotely (a
  quirk of Windows scp), it is removed before the copy; the destination
  is ``touch``-ed first so scp overwrites a regular file.

Usage
-----
    python -m dashrip sync sync   --src C:\\... --target /home/u/Movie \
        --host IP --user u --key key.pem
    python -m dashrip sync status ...
    python -m dashrip sync verify ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

__all__ = ["main"]


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _run(cmd, timeout=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _ssh(base, remote_cmd, timeout=600):
    spec = "%s@%s" % (base["user"], base["host"])
    cmd = ["ssh", "-i", base["key"],
           "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=30",
           "-o", "BatchMode=yes",
           spec, remote_cmd]
    return _run(cmd, timeout=timeout)


def _sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _ckpt_path(src, target):
    """One checkpoint per (src, target) pair, kept outside the repo."""
    key = hashlib.sha1(("%s|%s" % (src, target)).encode("utf-8")).hexdigest()[:12]
    d = os.path.join(os.path.expanduser("~"), ".dashrip")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "sync_%s.json" % key)


def _load_ckpt(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"local_hashes": {}, "done": {}}


def _save_ckpt(path, ck):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ck, f)
    os.replace(tmp, path)


def _rq(p):
    """Quote a path for a REMOTE BASH command (the remote shell strips
    the quotes).  Never use this for scp arguments from Windows."""
    return "'" + p.replace("'", "'\\''") + "'"


def _list_local(root):
    out = []
    for dirpath, _, files in os.walk(root):
        for fn in files:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel.endswith((".tmp", ".part")) or os.path.basename(rel).startswith("."):
                continue
            out.append((rel, full, os.path.getsize(full)))
    out.sort(key=lambda x: x[2])  # small first: quick wins early
    return out


def _remote_state(base, target, rel):
    """Remote file state: None (missing) or (size, sha256hex)."""
    rp = os.path.join(target, rel).replace("\\", "/")
    q = _rq(rp)
    rc, out, _err = _ssh(
        base,
        "if [ -f %s ]; then echo -n SIZE $(stat -c %%s %s); echo; "
        "echo -n HASH $(sha256sum %s | cut -d' ' -f1); fi" % (q, q, q),
        timeout=900)
    out = out.strip()
    size = hsh = None
    for ln in out.splitlines():
        if ln.startswith("SIZE "):
            size = ln[5:].strip()
        elif ln.startswith("HASH "):
            hsh = ln[5:].strip()
    if size is None and hsh is None:
        return None
    return (size, hsh)


def _scp_transfer(base, local, target, rel):
    rp = os.path.join(target, rel).replace("\\", "/")  # UNQUOTED for scp
    q = _rq(rp)
    _ssh(base, "if [ -d %s ]; then rm -rf %s; fi" % (q, q), timeout=120)
    trc, tout, terr = _ssh(base, "touch " + q, timeout=120)
    if trc != 0:
        return 1, "touch failed rc=%s: %s %s" % (trc, tout, terr)
    remote_spec = "%s@%s:%s" % (base["user"], base["host"], rp)
    cmd = ["scp", "-i", base["key"],
           "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=30",
           local, remote_spec]
    return _run(cmd, timeout=None)


def _ensure_remote_dirs(base, target, files):
    parents = set()
    for rel, _full, _size in files:
        p = rel.rsplit("/", 1)[0]
        if p:
            parents.add(p)
    if not parents:
        return
    log("Creating %d parent directories remotely ..." % len(parents))
    for p in sorted(parents, key=lambda x: (x.count("/"), x)):
        rpp = os.path.join(target, p).replace("\\", "/")
        _ssh(base, "mkdir -p " + _rq(rpp), timeout=120)


# ------------------------- commands -------------------------

def _do_sync(a):
    base = {"host": a.host, "user": a.user, "key": a.key}
    cpath = _ckpt_path(a.src, a.target)
    ck = _load_ckpt(cpath)
    files = _list_local(a.src)
    total = sum(f[2] for f in files)
    log("Local: %d files, %.2f GB" % (len(files), total / 1e9))
    _ssh(base, "mkdir -p " + _rq(a.target), timeout=120)
    _ensure_remote_dirs(base, a.target, files)

    done_n = skip_n = fail_n = 0
    for i, (rel, full, size) in enumerate(files, 1):
        if rel in ck["done"]:
            skip_n += 1
            log("  SKIP (verified before) %s" % rel)
            continue
        lh = ck["local_hashes"].get(rel)
        if not lh:
            log("  hashing local %s ..." % rel)
            lh = _sha256_file(full)
            ck["local_hashes"][rel] = lh
            _save_ckpt(cpath, ck)
        rs = _remote_state(base, a.target, rel)
        if rs is not None:
            rsize, rh = rs
            if str(size) == str(rsize) and rh == lh:
                ck["done"][rel] = lh
                _save_ckpt(cpath, ck)
                done_n += 1
                log("  SKIP (already complete remotely) %s" % rel)
                continue
            log("  MISMATCH (remote %s) -> removing, re-sending" % (rs,))
            rp = os.path.join(a.target, rel).replace("\\", "/")
            _ssh(base, "rm -f " + _rq(rp), timeout=120)
        log("  scp %s ..." % rel)
        t0 = time.time()
        rc, err = _scp_transfer(base, full, a.target, rel)
        dt = time.time() - t0
        spd = (size / 1e6 / dt) if dt > 0 else 0
        if rc != 0:
            log("  scp FAILED rc=%d  %s" % (rc, (err or "").strip()[:200]))
            fail_n += 1
            continue
        rs = _remote_state(base, a.target, rel)
        if rs and rs[1] == lh and str(size) == str(rs[0]):
            ck["done"][rel] = lh
            _save_ckpt(cpath, ck)
            done_n += 1
            log("  OK (%.2f MB/s) %s" % (spd, rel))
        else:
            log("  VERIFICATION FAILED (next run retries) %s" % rel)
            fail_n += 1
    log("DONE: %d new, %d skipped, %d failed" % (done_n - skip_n, skip_n, fail_n))
    if fail_n:
        log("Re-run the same command to resume.")


def _do_status(a):
    base = {"host": a.host, "user": a.user, "key": a.key}
    cpath = _ckpt_path(a.src, a.target)
    ck = _load_ckpt(cpath)
    files = _list_local(a.src)
    ndone = sum(1 for f in files if f[0] in ck["done"])
    log("%d files total | verified: %d | remaining: %d"
        % (len(files), ndone, len(files) - ndone))
    for rel, _full, size in files:
        mark = "DONE" if rel in ck["done"] else "----"
        log("  [%s] %8.1f MB  %s" % (mark, size / 1e6, rel))


def _do_verify(a):
    base = {"host": a.host, "user": a.user, "key": a.key}
    cpath = _ckpt_path(a.src, a.target)
    ck = _load_ckpt(cpath)
    files = _list_local(a.src)
    bad = 0
    for _i, (rel, full, size) in enumerate(files, 1):
        lh = ck["local_hashes"].get(rel) or _sha256_file(full)
        rs = _remote_state(base, a.target, rel)
        ok = (rs is not None) and (rs[1] == lh) and (str(size) == str(rs[0]))
        if not ok:
            bad += 1
            log("  MISMATCH %s  (remote=%s)" % (rel, rs))
    log("verify: %d files, %d mismatched" % (len(files), bad))
    return 1 if bad else 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="dashrip sync",
        description="Lossless, resumable folder transfer over scp.")
    p.add_argument("cmd", nargs="?", default="sync",
                   choices=["sync", "status", "verify"])
    p.add_argument("--src", required=True, help="local source directory")
    p.add_argument("--host", required=True, help="ssh host")
    p.add_argument("--user", required=True, help="ssh user")
    p.add_argument("--key", required=True, help="ssh private key path")
    p.add_argument("--target", required=True, help="remote target directory")
    a = p.parse_args(argv)
    if a.cmd == "sync":
        _do_sync(a)
        return 0
    if a.cmd == "status":
        _do_status(a)
        return 0
    return _do_verify(a)
