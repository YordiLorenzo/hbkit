#!/usr/bin/env python3
"""Build the browsable pre-index for a Hyper Backup archive.

Produces a SQLite tree:
    node(id, parent, name, isdir, size, nfiles, ovf, mtime, share)
where a directory's `size`/`nfiles` are SUBTREE totals, so a UI can show folder weights
without walking children. Synology sidecar metadata (@eaDir, @SynoEAStream,
@SynoResource) is pruned - it is not user data.

Indexes are cached per archive under ~/.cache/hbkit/ and rebuilt automatically
when the archive's share databases change.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import sys
import time

from . import archive as hbk

def _default_cache() -> str:
    """~/.cache/hbkit, falling back to the pre-0.4.1 name if that is what exists locally
    so an upgrade does not silently orphan an index someone waited minutes to build."""
    new = os.path.expanduser("~/.cache/hbkit")
    old = os.path.expanduser("~/.cache/hbk-recovery")
    if not os.path.isdir(new) and os.path.isdir(old):
        return old
    return new


CACHE = os.path.expanduser(os.environ.get("HBK_CACHE", "")) or _default_cache()
SCHEMA = 2


def is_sidecar(name: str) -> bool:
    """Synology metadata: thumbnail dirs, xattr and resource-fork streams."""
    return name == "@eaDir" or name.endswith(("@SynoEAStream", "@SynoResource"))


def fingerprint(arc: hbk.Archive) -> str:
    """Changes whenever any share database changes, so a stale index rebuilds itself."""
    h = hashlib.sha1(f"v{SCHEMA}|{arc.root}|enc={arc.crypto is not None}".encode())
    for share in arc.shares():
        try:
            p = arc.share_db(share)
            st = os.stat(p)
            h.update(f"|{share}:{st.st_size}:{int(st.st_mtime)}".encode())
        except FileNotFoundError:
            h.update(f"|{share}:missing".encode())
    return h.hexdigest()[:16]


def cache_path(arc: hbk.Archive) -> str:
    safe = os.path.basename(arc.root.rstrip("/")) or "archive"
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in safe)
    return os.path.join(CACHE, f"{safe}-{fingerprint(arc)}.db")


def is_current(arc: hbk.Archive) -> bool:
    p = cache_path(arc)
    if not os.path.exists(p):
        return False
    try:
        db = sqlite3.connect(p)
        ok = db.execute("SELECT value FROM meta WHERE key='complete'").fetchone()
        db.close()
        return bool(ok and ok[0] == "1")
    except sqlite3.Error:
        return False


def _stage_local(src: str, cache_dir: str, progress=None, label: str = "") -> tuple[str, bool]:
    """Copy a share database to local disk before opening it with SQLite.

    SQLite issues thousands of small random page reads. On a network mount (rclone/S3/R2)
    each one becomes its own range request, which is pathologically slow - far slower than
    streaming the whole file down once. Measured: ~8.5 MB/s sequential on an R2 mount, so a
    356 MB share db streams in ~40s, versus many minutes read page-by-page in place.
    """
    size = os.path.getsize(src)
    if size < 32 << 20:                     # small enough that it doesn't matter
        return src, False
    os.makedirs(cache_dir, exist_ok=True)
    dst = os.path.join(cache_dir, f"stage-{os.getpid()}-{os.path.basename(src)}")
    done = 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            b = fi.read(8 << 20)
            if not b:
                break
            fo.write(b)
            done += len(b)
            if progress:
                progress(f"{label} {done // (1 << 20)}/{size // (1 << 20)} MB", done, size)
    return dst, True


def build(arc: hbk.Archive, out: str | None = None, progress=None) -> str:
    """Build (or rebuild) the index. `progress(stage, done, total)` is called as it goes."""
    out = out or cache_path(arc)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".building"
    for p in (tmp, out):
        if os.path.exists(p):
            os.remove(p)

    db = sqlite3.connect(tmp)
    db.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE node(
            id INTEGER PRIMARY KEY, parent INTEGER, name TEXT, isdir INTEGER,
            size INTEGER, nfiles INTEGER, ovf INTEGER, mtime INTEGER, share TEXT);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    """)

    shares = arc.shares()
    nid = 0
    rows: list[tuple] = []
    for si, share in enumerate(shares):
        if progress:
            progress(f"reading {share}", si, len(shares))
        try:
            path = arc.share_db(share)
        except FileNotFoundError:
            continue
        staged, is_copy = _stage_local(path, os.path.dirname(out), progress,
                                       f"downloading {share}")
        s = sqlite3.connect(f"file:{staged}?immutable=1", uri=True)
        s.text_factory = bytes
        try:
            if progress:
                progress(f"reading {share}", si, len(shares))
            raw = s.execute("SELECT name_id_v2,pname_id_v2,file_name,size,mode,mtime_sec,"
                            "off_virtual_file FROM version_list").fetchall()
        except sqlite3.Error:
            s.close()
            if is_copy:
                os.unlink(staged)
            continue
        s.close()
        if is_copy:
            os.unlink(staged)

        idmap: dict[bytes, int] = {}
        meta: dict[int, tuple] = {}
        # In encrypted archives every path component is ciphertext; decrypt here so the
        # rest of the tool (tree, search, globs, extraction paths) sees plain names.
        decrypt = arc.crypto.decrypt_name if arc.crypto else None
        for b, pb, name, size, mode, mt, ovf in raw:
            nid += 1
            idmap[b] = nid
            fname = name.decode("utf-8", "replace")
            if decrypt:
                try:
                    fname = decrypt(fname)
                except Exception:                      # keep the row; flag the name
                    fname = f"<undecryptable:{fname[:16]}>"
            meta[nid] = (pb, fname, size or 0,
                         stat.S_ISDIR(mode or 0), mt or 0, ovf)

        nid += 1
        root_id = nid
        kids: dict[int, list[int]] = {}
        for i, (pb, *_rest) in meta.items():
            par = idmap.get(pb, root_id)
            if par == i:
                par = root_id                       # self-parented archive root
            kids.setdefault(par, []).append(i)

        # walk top-down, pruning sidecar subtrees, then aggregate bottom-up
        order, stack = [], [root_id]
        while stack:
            n = stack.pop()
            if n != root_id and is_sidecar(meta[n][1]):
                continue
            order.append(n)
            stack.extend(kids.get(n, ()))
        keep = set(order)

        agg_sz: dict[int, int] = {}
        agg_nf: dict[int, int] = {}
        for n in reversed(order):
            children = [c for c in kids.get(n, ()) if c in keep]
            if n == root_id or meta[n][3]:
                agg_sz[n] = sum(agg_sz.get(c, 0) for c in children)
                agg_nf[n] = sum(agg_nf.get(c, 0) for c in children)
            else:
                agg_sz[n], agg_nf[n] = meta[n][2], 1

        rows.append((root_id, None, share, 1, agg_sz[root_id], agg_nf[root_id], None, 0, share))
        for n in order:
            if n == root_id:
                continue
            pb, name, size, isdir, mt, ovf = meta[n]
            par = idmap.get(pb, root_id)
            if par == n or par not in keep:
                par = root_id
            rows.append((n, par, name, 1 if isdir else 0,
                         agg_sz[n], agg_nf[n], ovf, mt, share))
        if progress:
            progress(f"indexed {share} ({len(raw):,} rows)", si + 1, len(shares))

    db.executemany("INSERT INTO node VALUES (?,?,?,?,?,?,?,?,?)", rows)
    if progress:
        progress("building indexes", len(shares), len(shares))
    db.executescript("""
        CREATE INDEX node_parent ON node(parent, isdir DESC, name COLLATE NOCASE);
        CREATE INDEX node_name   ON node(name COLLATE NOCASE);
    """)
    db.executemany("INSERT INTO meta VALUES (?,?)", [
        ("root", arc.root), ("schema", str(SCHEMA)),
        ("fingerprint", fingerprint(arc)), ("built", str(int(time.time()))),
        ("complete", "1"),
    ])
    db.commit()
    db.close()
    os.replace(tmp, out)
    return out


def open_or_build(arc: hbk.Archive, progress=None) -> str:
    return cache_path(arc) if is_current(arc) else build(arc, progress=progress)


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("archive", help="path to the .hbk directory (or its parent)")
    ap.add_argument("-o", "--out", help="output index path")
    ap.add_argument("-f", "--force", action="store_true", help="rebuild even if current")
    a = ap.parse_args()
    arc = hbk.Archive(a.archive)
    print(f"archive : {arc.root}")
    print(f"shares  : {', '.join(arc.shares()) or '(none)'}")
    if arc.is_encrypted():
        print("WARNING : this archive is ENCRYPTED - extraction is not supported", file=sys.stderr)
    if not a.force and not a.out and is_current(arc):
        print(f"index   : {cache_path(arc)} (already current)")
        return 0
    t = time.time()
    p = build(arc, a.out, progress=lambda s, d, n: print(f"  {s} [{d}/{n}]", flush=True))
    db = sqlite3.connect(p)
    n, sz = db.execute("SELECT COUNT(*), SUM(CASE WHEN isdir=0 THEN size ELSE 0 END) "
                       "FROM node").fetchone()
    db.close()
    print(f"\nindex   : {p}")
    print(f"          {n:,} nodes, {(sz or 0)/1e12:.2f} TB, "
          f"{os.path.getsize(p)/1e6:.0f} MB, {time.time()-t:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
