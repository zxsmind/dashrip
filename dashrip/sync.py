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

Implementation notes
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
import time

__all__ = ["main"]


def log(msg: str) -> None:
    """Print a timestamped progress line."""
    print("[{}] {}".format(time.strftime("%H:%M:%S"), msg), flush=True)


def _run(cmd: list[str], timeout: int | None = None) -> tuple[int, str, str]:
    """Run a local command; returns (rc, stdout, stderr), rc 124 on timeout."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _ssh(base: dict, remote_cmd: str, timeout: int = 600) -> tuple[int, str, str]:
    """Run one command over ssh with the configured key."""
    spec = "{}@{}".format(base["user"], base["host"])
    cmd = ["ssh", "-i", base["key"],
           "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=30",
           "-o", "BatchMode=yes",
           spec, remote_cmd]
    return _run(cmd, timeout=timeout)


def _sha256_file(path: str, chunk: int = 1024 * 1024) -> str:
    """Hex SHA-256 of a file, read in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _ckpt_path(src: str, target: str) -> str:
    """One checkpoint per (src, target) pair, kept outside the repo."""
    key = hashlib.sha1((f"{src}|{target}").encode()).hexdigest()[:12]
    d = os.path.join(os.path.expanduser("~"), ".dashrip")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"sync_{key}.json")


def _load_ckpt(path: str) -> dict:
    """Checkpoint for a transfer pair; empty when absent or unreadable."""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"sync checkpoint unreadable ({e}), restarting")
    return {"local_hashes": {}, "done": {}}


def _save_ckpt(path: str, ck: dict) -> None:
    """Write the checkpoint atomically (tmp file + rename)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ck, f)
    os.replace(tmp, path)


def _rq(p: str) -> str:
    """Quote a path for a REMOTE BASH command (the remote shell strips
    the quotes).  Never use this for scp arguments from Windows."""
    return "'" + p.replace("'", "'\\''") + "'"


def _list_local(root: str) -> list[tuple[str, str, int]]:
    """Files under root as (rel, full, size), smallest first."""
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


def _remote_state(base: dict, target: str,
                  rel: str) -> tuple[str | None, str | None] | None:
    """Remote file state: None (missing) or (size, sha256hex)."""
    rp = os.path.join(target, rel).replace("\\", "/")
    q = _rq(rp)
    rc, out, _err = _ssh(
        base,
        f"if [ -f {q} ]; then echo -n SIZE $(stat -c %s {q}); echo; "
        f"echo -n HASH $(sha256sum {q} | cut -d' ' -f1); fi",
        timeout=900)
    if rc != 0:
        return None
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


def _scp_transfer(base: dict, local: str, target: str,
                  rel: str) -> tuple[int, str]:
    """Copy one file over scp after a remote touch. Returns (rc, error)."""
    rp = os.path.join(target, rel).replace("\\", "/")  # UNQUOTED for scp
    q = _rq(rp)
    _ssh(base, f"if [ -d {q} ]; then rm -rf {q}; fi", timeout=120)
    trc, tout, terr = _ssh(base, "touch " + q, timeout=120)
    if trc != 0:
        return 1, f"touch failed rc={trc}: {tout} {terr}"
    remote_spec = "{}@{}:{}".format(base["user"], base["host"], rp)
    cmd = ["scp", "-i", base["key"],
           "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=30",
           local, remote_spec]
    rc, _out, err = _run(cmd, timeout=None)
    return rc, err


def _ensure_remote_dirs(base: dict, target: str, files: list) -> None:
    """Create every parent directory of the transfer set on the remote."""
    parents = set()
    for rel, _full, _size in files:
        p = rel.rsplit("/", 1)[0]
        if p:
            parents.add(p)
    if not parents:
        return
    log(f"Creating {len(parents)} parent directories remotely ...")
    for p in sorted(parents, key=lambda x: (x.count("/"), x)):
        rpp = os.path.join(target, p).replace("\\", "/")
        _ssh(base, "mkdir -p " + _rq(rpp), timeout=120)


# ------------------------- commands -------------------------

def _do_sync(a: argparse.Namespace) -> None:
    """Transfer the missing or mismatched files, checkpointing as it goes."""
    base = {"host": a.host, "user": a.user, "key": a.key}
    cpath = _ckpt_path(a.src, a.target)
    ck = _load_ckpt(cpath)
    files = _list_local(a.src)
    total = sum(f[2] for f in files)
    log(f"Local: {len(files)} files, {total / 1e9:.2f} GB")
    _ssh(base, "mkdir -p " + _rq(a.target), timeout=120)
    _ensure_remote_dirs(base, a.target, files)

    done_n = skip_n = fail_n = 0
    for i, (rel, full, size) in enumerate(files, 1):
        if rel in ck["done"]:
            skip_n += 1
            log(f"  SKIP (verified before) {rel}")
            continue
        lh = ck["local_hashes"].get(rel)
        if not lh:
            log(f"  hashing local {rel} ...")
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
                log(f"  SKIP (already complete remotely) {rel}")
                continue
            log(f"  MISMATCH (remote {rs}) -> removing, re-sending")
            rp = os.path.join(a.target, rel).replace("\\", "/")
            _ssh(base, "rm -f " + _rq(rp), timeout=120)
        log(f"  scp {rel} ...")
        t0 = time.time()
        rc, err = _scp_transfer(base, full, a.target, rel)
        dt = time.time() - t0
        spd = (size / 1e6 / dt) if dt > 0 else 0
        if rc != 0:
            log(f"  scp FAILED rc={rc}  {(err or '').strip()[:200]}")
            fail_n += 1
            continue
        rs = _remote_state(base, a.target, rel)
        if rs and rs[1] == lh and str(size) == str(rs[0]):
            ck["done"][rel] = lh
            _save_ckpt(cpath, ck)
            done_n += 1
            log(f"  OK ({spd:.2f} MB/s) {rel}")
        else:
            log(f"  VERIFICATION FAILED (next run retries) {rel}")
            fail_n += 1
    log(f"DONE: {done_n - skip_n} new, {skip_n} skipped, {fail_n} failed")
    if fail_n:
        log("Re-run the same command to resume.")


def _do_status(a: argparse.Namespace) -> None:
    """Print the local checkpoint against the file list."""
    cpath = _ckpt_path(a.src, a.target)
    ck = _load_ckpt(cpath)
    files = _list_local(a.src)
    ndone = sum(1 for f in files if f[0] in ck["done"])
    log(f"{len(files)} files total | verified: {ndone} | "
        f"remaining: {len(files) - ndone}")
    for rel, _full, size in files:
        mark = "DONE" if rel in ck["done"] else "----"
        log(f"  [{mark}] {size / 1e6:8.1f} MB  {rel}")


def _do_verify(a: argparse.Namespace) -> int:
    """Re-hash every file against the remote; 1 when anything mismatches."""
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
            log(f"  MISMATCH {rel}  (remote={rs})")
    log(f"verify: {len(files)} files, {bad} mismatched")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``dashrip sync`` (sync | status | verify)."""
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
