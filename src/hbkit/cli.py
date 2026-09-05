#!/usr/bin/env python3
"""Recover files from a Synology Hyper Backup (.hbk) archive. No Synology software needed.

    hbk <archive> doctor [-s N]                        check whether it can be recovered
  hbk <archive> info                                 show task, shares, codec
  hbk <archive> list  [pattern]                      search the file index
  hbk <archive> get   <glob> <dest> [-j N] [--strict] extract (keeps tree + mtimes)
  hbk <archive> verify <glob>      [-j N]            integrity-check, write nothing
  hbk <archive> tui                                  browse in the full-screen UI

  hbk mount   <remote:bucket> <dir> [--for browse|restore] [--no-warm]
  hbk warm    <dir>                                  pre-cache directory listings
  hbk unmount <dir>                                  unmount it
  hbk remotes                                        list configured rclone remotes
  hbk --version                                      print the installed version

Encrypted archives: add -p/--password, or set HBK_PASSWORD, or you'll be prompted.

<archive> is a .hbk directory, or any drive/folder containing one.
Globs match the full archive path, which starts with the share name.
Extraction is resumable: correctly-sized files are skipped. Each restored file's MD5 is
recorded in <dest>/.hbkit-restore.jsonl; --strict re-checks those hashes when resuming,
catching a file that is the right size but the wrong content (costs a local re-read).

  hbk mount r2:mybucket ~/mnt/backup --for browse
  hbk ~/mnt/backup/target.hbk doctor
  hbk /Volumes/Backup doctor
  hbk /Volumes/Backup list holiday
  hbk /Volumes/Backup get "/Photos/2019/*" ~/restore
"""
from __future__ import annotations

import fnmatch
import getpass
import os
import shutil
import sqlite3
import sys
import time


from . import archive as hbk
from . import index as hbk_index
from . import runner as hbk_run

CODECS = {"0": "none", "1": "lz4", "2": "lz4-hc", "4": "zlib"}


def human(n) -> str:
    n = float(n or 0)
    for u in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or u == "T":
            return f"{n:.0f}B" if u == "B" else f"{n:,.1f}{u}"
        n /= 1024
    return f"{n:,.1f}T"


def rate(bps) -> str:
    """Bytes/sec as a human string. Network transfers are often < 1 MB/s, where a
    plain '{:.0f} MB/s' renders as a misleading '0 MB/s'."""
    bps = float(bps or 0)
    if bps >= 1e6:
        return f"{bps/1e6:,.0f} MB/s" if bps >= 1e7 else f"{bps/1e6:,.1f} MB/s"
    if bps >= 1e3:
        return f"{bps/1e3:,.0f} KB/s"
    return f"{bps:,.0f} B/s"


def hms(s) -> str:
    s = max(0, int(s or 0))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def all_files(db):
    """(path, ovf, size, mtime) for every file; path begins with the share name."""
    return db.execute("""
        WITH RECURSIVE t(id,path,isdir) AS (
            SELECT id, '/'||name, isdir FROM node WHERE parent IS NULL
            UNION ALL
            SELECT n.id, t.path||'/'||n.name, n.isdir
              FROM node n JOIN t ON n.parent=t.id
        )
        SELECT t.path, n.ovf, n.size, n.mtime
          FROM t JOIN node n ON n.id=t.id
         WHERE t.isdir=0 AND n.ovf IS NOT NULL
    """)


def resolve_password(password):
    """-p wins, then HBK_PASSWORD. None means "prompt if the archive turns out to need it"."""
    return password or os.environ.get("HBK_PASSWORD")


def open_archive(archive, password=None):
    """Open, prompting for a password only if the archive turns out to be encrypted."""
    try:
        return hbk.Archive(archive, password=password)
    except hbk.NeedPassword:
        pw = resolve_password(password)
        if not pw:
            if not sys.stdin.isatty():
                sys.exit("archive is encrypted - pass --password or set HBK_PASSWORD")
            pw = getpass.getpass("archive password: ")
        try:
            return hbk.Archive(archive, password=pw)
        except Exception as e:
            sys.exit(f"{type(e).__name__}: {e}")


def arc_password(arc, password):
    """The password actually used to unlock `arc` (may have come from a prompt)."""
    return getattr(arc, "_password", None)


def open_index(archive, password=None):
    arc = open_archive(archive, password)
    if not hbk_index.is_current(arc):
        print("indexing archive (first use only)...", file=sys.stderr)
        hbk_index.build(arc, progress=lambda s, d, n: print(f"  {s} [{d}/{n}]", file=sys.stderr))
    return arc, sqlite3.connect(f"file:{hbk_index.cache_path(arc)}?mode=ro", uri=True)


def run(arc, sel, dest, jobs, check, password=None, strict=False):
    total = sum(s[2] for s in sel)
    print(f"{len(sel):,} files, {human(total)}, {jobs} workers"
          + (" - verify only, nothing written" if check else f" -> {dest}")
          + (" [strict resume]" if strict and not check else ""))
    if not check:
        os.makedirs(dest, exist_ok=True)
    r = hbk_run.Runner(arc.root, dest, sel, jobs, verify=True, check=check,
                       password=password, strict=strict)
    tty = sys.stderr.isatty()
    t0 = time.time()
    r.start()
    try:
        while not r.finished:
            r.poll()
            el = max(time.time() - t0, 1e-6)
            pct = 100 * r.bytes_done / total if total else 100
            rt = r.bytes_done / el
            eta = (total - r.bytes_done) / rt if rt > 0 else 0
            if tty:      # only paint a live line on a terminal; piped output stays clean
                sys.stderr.write(f"\r  {pct:5.1f}%  {r.done_files:,}/{r.total_files:,}  "
                                 f"{human(r.bytes_done)}  {rate(rt)}  eta {hms(eta)}   ")
                sys.stderr.flush()
            time.sleep(0.25)
        r.poll()
    except KeyboardInterrupt:
        r.cancel()
        print("\ncancelled", file=sys.stderr)
    finally:
        r.cleanup()
    el = max(time.time() - t0, 1e-6)
    if tty:
        sys.stderr.write("\r" + " " * 78 + "\r")
    if r.swept:
        print(f"cleaned up {r.swept} leftover .part file(s) from an interrupted run")
    verb = "verified" if check else "extracted"
    print(f"{r.ok:,} {verb}, {r.skipped:,} already present, {r.failed:,} failed "
          f"in {hms(el)} ({rate(r.bytes_done/el)})")
    for p, m in r.errors[:20]:
        print(f"  FAIL {p}: {m}")
    if len(r.errors) > 20:
        print(f"  ... and {len(r.errors)-20} more")
    return 1 if r.failed else 0


def main() -> int:
    a = sys.argv[1:]
    if a and a[0] in ("--version", "-V"):
        from . import __version__
        print(f"hbkit {__version__}")
        return 0
    jobs = 8
    password = None
    if "-j" in a:
        i = a.index("-j")
        jobs = int(a[i + 1])
        del a[i:i + 2]
    strict = "--strict" in a
    if strict:
        a.remove("--strict")
    for flag in ("-p", "--password"):
        if flag in a:
            i = a.index(flag)
            password = a[i + 1]
            del a[i:i + 2]
    if len(a) < 2 and (not a or a[0] != "remotes"):
        print(__doc__)
        return 1
    if a[0] in ("mount", "unmount", "remotes", "warm"):
        from . import mount as m
        try:
            if a[0] == "remotes":
                rs = m.remotes()
                print("\n".join(f"  {r}" for r in rs) if rs
                      else "  no rclone remotes configured - run: rclone config")
                return 0
            if a[0] == "warm":
                if len(a) < 2:
                    print("usage: hbk warm <dir>")
                    return 1
                return m.warm(a[1])
            if a[0] == "unmount":
                if len(a) < 2:
                    print("usage: hbk unmount <dir>")
                    return 1
                return m.unmount(a[1])
            if len(a) < 3:
                print("usage: hbk mount <remote:bucket> <dir> [--for browse|restore]")
                return 1
            prof, rest = "browse", a[3:]
            if "--for" in rest:
                i = rest.index("--for")
                prof = rest[i + 1]
            cache = None
            if "--cache-size" in rest:
                cache = rest[rest.index("--cache-size") + 1]
            rc = m.mount(a[1], a[2], prof, cache, dry_run="--dry-run" in rest)
            if rc == 0 and "--dry-run" not in rest and "--no-warm" not in rest:
                print("\npre-warming directory cache (first open over a network mount "
                      "is slow; doing it here so later commands are instant)")
                m.warm(a[2])
            return rc
        except (m.RcloneMissing, ValueError) as e:
            print(f"{e}", file=sys.stderr)
            return 2

    archive, cmd = a[0], a[1]

    if cmd == "doctor":
        from . import doctor
        sample = None
        for flag in ("-s", "-n", "--sample"):
            if flag in a[2:]:
                i = a.index(flag, 2)
                try:
                    sample = int(a[i + 1])
                except (IndexError, ValueError):
                    print(f"{flag} requires a positive integer", file=sys.stderr)
                    return 2
                if sample < 1:
                    print(f"{flag} requires a positive integer", file=sys.stderr)
                    return 2
                break
        kw = {"password": resolve_password(password)}
        if sample is not None:
            kw["sample"] = sample
        r = doctor.diagnose(archive, **kw)
        print(doctor.render(r))
        return 0 if (r.ok and not r.blockers) else 1

    if cmd == "tui":
        from .tui import HBKApp
        HBKApp(jobs, archive).run()
        return 0

    if cmd == "info":
        arc = open_archive(archive, password)
        cfg = arc.task_config()
        print(f"archive   : {arc.root}")
        print(f"task      : {cfg.get('name','?')}   host {cfg.get('host_name','?')}")
        print(f"codec     : {CODECS.get(cfg.get('data_compress_type',''), '?')}")
        print(f"encrypted : {'yes (unlocked)' if arc.crypto else arc.is_encrypted()}")
        print(f"sources   : {cfg.get('backup_folders','?')}")
        for s in arc.shares():
            print(f"  share {s}   versions={arc.share_versions(s)}")
        return 0

    arc, db = open_index(archive, password)

    if cmd == "list":
        pat = a[2].lower() if len(a) > 2 else ""
        n = tot = 0
        for path, ovf, size, mt in all_files(db):
            if pat in path.lower():
                n += 1
                tot += size or 0
                if n <= 200:
                    print(f"{size or 0:>13,}  {path}")
        print(f"\n{n:,} files, {human(tot)}" + (" (first 200 shown)" if n > 200 else ""))
        return 0

    if cmd in ("get", "verify"):
        if len(a) < 3 or (cmd == "get" and len(a) < 4):
            print(__doc__)
            return 1
        glob = a[2]
        dest = os.path.abspath(os.path.expanduser(a[3])) if cmd == "get" else ""
        sel = [row for row in all_files(db) if fnmatch.fnmatch(row[0], glob)]
        if not sel:
            print("no files matched that glob")
            return 1
        if cmd == "get":
            need = sum(s[2] for s in sel)
            probe = dest
            while probe and not os.path.exists(probe):
                probe = os.path.dirname(probe)
            free = shutil.disk_usage(probe or "/").free
            if need > free:
                print(f"not enough space: need {human(need)}, have {human(free)} free")
                return 1
        return run(arc, sel, dest, jobs, cmd == "verify",
                   password=arc_password(arc, password), strict=strict)

    print(__doc__)
    return 1


def main_entry() -> int:
    """Console-script entry point; keeps `main()` usable as a plain function."""
    from .crypto import MissingCryptoDeps, WrongPassword
    try:
        return main()
    except WrongPassword:
        print("wrong password for this archive", file=sys.stderr)
        return 2
    except MissingCryptoDeps as e:
        print(f"{e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except hbk.UnsupportedArchive as e:
        print(f"unsupported archive: {e}", file=sys.stderr)
        return 2
    except hbk.NeedPassword as e:
        print(f"{e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main_entry())
