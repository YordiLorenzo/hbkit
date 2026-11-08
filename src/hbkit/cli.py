#!/usr/bin/env python3
"""Recover files from a Synology Hyper Backup (.hbk) archive. No Synology software needed.

  hbk <archive> doctor                               check whether it can be recovered
  hbk <archive> info                                 show task, shares, codec, versions
  hbk <archive> versions [--porcelain]               list backup versions with dates
  hbk <archive> list  [pattern|glob]                 search the file index
  hbk <archive> get   <glob> <dest> [-j N] [--strict] extract (keeps tree + mtimes)
  hbk <archive> verify <glob>      [-j N]            integrity-check, write nothing
  hbk <archive> tui                                  browse in the full-screen UI

  hbk mount   <remote:bucket> <dir> [--for browse|restore] [--no-warm]
  hbk warm    <dir>                                  pre-cache directory listings
  hbk unmount <dir>                                  unmount it
  hbk remotes                                        list configured rclone remotes
  hbk --version                                      print the installed version

Encrypted archives: add -p/--password, or set HBK_PASSWORD, or you'll be prompted.

Backup versions: list, get, verify, info, doctor and tui read the newest COMPLETE version
unless told otherwise:
  --version N                 exactly version N (see `versions`)
  --version @YYYY-MM-DD[ HH:MM[:SS]]
                              the newest complete version that started no later than
                              that moment, in this machine's local time; a bare date
                              means the end of that day
  --before 'YYYY-MM-DD HH:MM' the same as --version @...
  --allow-incomplete          also accept a version whose backup did not finish

Exit codes: 0 ok, 1 files failed to extract or verify (checksum or read error),
2 usage or unreadable/unsupported archive, 3 password missing or wrong,
4 no such version, 5 nothing matched, 6 not enough disk space, 130 interrupted.

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
from datetime import datetime

from . import archive as hbk
from . import index as hbk_index
from . import runner as hbk_run

CODECS = {"0": "none", "1": "lz4", "2": "lz4-hc", "4": "zlib"}

# Exit codes. Scripts depend on these; they are documented in the README.
EXIT_OK = 0
EXIT_FAILED = 1        # one or more files failed to extract/verify (checksum, read error)
EXIT_USAGE = 2         # bad arguments, archive missing/unreadable/unsupported
EXIT_PASSWORD = 3      # encrypted archive: no password given, or the wrong one
EXIT_VERSION = 4       # the requested backup version does not exist/is not complete
EXIT_NO_MATCH = 5      # the glob or pattern matched no file
EXIT_NO_SPACE = 6      # the destination does not have room for the selection
EXIT_INTERRUPTED = 130 # Ctrl-C / SIGINT: whatever was running stopped part-way
EXIT_PIPE = 141        # stdout closed early under get/verify: the outcome went unreported

_command = ""          # the running command, for main_entry's broken-pipe exit code

# Most positional arguments each archive command takes, counting <archive> and the
# command (too few is reported by the command itself, with the usage text).
MAX_ARGS = {"list": 3, "get": 4, "verify": 3, "info": 2, "versions": 2, "tui": 2}


class UsageError(Exception):
    pass


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


def stdin_is_interactive():
    """True when a password prompt can be answered: stdin is a real terminal/console."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    if os.name != "nt":
        return True
    # `< NUL` is a character device, so isatty() is True; only a real console has a mode
    import ctypes
    import msvcrt
    try:
        h = msvcrt.get_osfhandle(sys.stdin.fileno())
    except OSError:
        return False
    mode = ctypes.c_uint32()
    return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(h), ctypes.byref(mode)))


def open_archive(archive, password=None, version=None, allow_incomplete=False):
    """Open, prompting for a password only if the archive turns out to be encrypted.

    Errors propagate (WrongPassword, NeedPassword, VersionNotFound...) so `main_entry`
    can turn each into its documented exit code.
    """
    kw = {"version": version, "allow_incomplete": allow_incomplete}
    try:
        return hbk.Archive(archive, password=password, **kw)
    except hbk.NeedPassword:
        pw = resolve_password(password)
        if not pw:
            if not stdin_is_interactive():
                raise hbk.NeedPassword(
                    "archive is encrypted - pass --password or set HBK_PASSWORD") from None
            pw = getpass.getpass("archive password: ")
        return hbk.Archive(archive, password=pw, **kw)


def arc_password(arc, password):
    """The password actually used to unlock `arc` (may have come from a prompt)."""
    return getattr(arc, "_password", None)


def when(ts) -> str:
    """Unix seconds as local 'YYYY-MM-DD HH:MM:SS', or '?' when unknown."""
    return "?" if ts is None else datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def describe_version(arc) -> str:
    """One line naming the version being read, for stderr."""
    vid = arc.selected_version()
    try:
        vs = {v.id: v for v in arc.versions()}
    except hbk.UnsupportedArchive:
        return ("version list unreadable: reading the newest file list of each share; "
                "completion NOT checked (--allow-incomplete)")
    if vid is None:
        return "reading the newest version of each share (the archive does not record versions)"
    v = vs.get(vid)
    what = ("version" if arc.version is not None else
            "newest version" if arc.allow_incomplete else "newest complete version")
    if v is None:
        return f"reading {what} {vid}"
    return f"reading {what} {vid} ({when(v.timestamp)}, {v.status or 'status unknown'})"


def open_index(archive, password=None, version=None, allow_incomplete=False):
    arc = open_archive(archive, password, version, allow_incomplete)
    try:
        several = len(arc.versions()) > 1
    except hbk.UnsupportedArchive:      # only reachable with --allow-incomplete
        several = True
    if several or arc.version is not None:
        print(describe_version(arc), file=sys.stderr)
    path = hbk_index.cache_path(arc)
    if not hbk_index.is_current(arc):
        print("indexing archive (first use only)...", file=sys.stderr)
        # open what was built: recomputing the path re-stats share databases, which a
        # backup still running (--allow-incomplete) may have changed in between
        path = hbk_index.build(
            arc, progress=lambda s, d, n: print(f"  {s} [{d}/{n}]", file=sys.stderr))
    return arc, sqlite3.connect(f"file:{path}?mode=ro", uri=True)


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
        r.cancelled = True
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
    if getattr(r, "cancelled", False):
        return EXIT_INTERRUPTED            # a partial restore must never look like success
    return EXIT_FAILED if r.failed else EXIT_OK


def pop_option(a: list[str], *names: str) -> str | None:
    """Remove `NAME VALUE` or `NAME=VALUE` from `a` and return VALUE (None if absent).
    Giving the option twice is an error: silently using one of them would read the
    wrong thing."""
    v = _pop_one(a, names)
    if v is not None and _pop_one(a, names) is not None:
        raise UsageError(f"{names[-1]} given more than once")
    return v


def _pop_one(a: list[str], names: tuple[str, ...]) -> str | None:
    for i, x in enumerate(a):
        for n in names:
            if x == n:
                if i + 1 >= len(a):
                    raise UsageError(f"{n} needs a value")
                v = a[i + 1]
                del a[i:i + 2]
                return v
            if x.startswith(n + "="):
                del a[i]
                return x[len(n) + 1:]
    return None


def pop_flag(a: list[str], name: str) -> bool:
    if name in a:
        while name in a:
            a.remove(name)
        return True
    return False


def cmd_versions(archive, porcelain: bool) -> int:
    """List backup versions. Needs no password: version_info.db is stored in the clear."""
    arc = hbk.Archive(archive, unlock=False)
    vs = arc.versions()
    if not vs:
        print("no backup versions found", file=sys.stderr)
        return EXIT_VERSION
    try:
        default = arc.resolve_version(None)
    except hbk.VersionNotFound:
        default = None
    if porcelain:
        # id <TAB> local ISO time with offset <TAB> unix seconds <TAB> status
        for v in vs:
            iso = ("-" if v.timestamp is None else
                   datetime.fromtimestamp(v.timestamp).astimezone().isoformat())
            print(f"{v.id}\t{iso}\t{'-' if v.timestamp is None else v.timestamp}"
                  f"\t{v.status or '-'}")
        return EXIT_OK
    shares = {s: set(arc.share_versions(s)) for s in arc.shares()}
    print(f"  {'version':>7}  {'started (local time)':<20}  {'status':<10}  shares")
    for v in vs:
        mark = "  <- default" if v.id == default else ""
        here = ",".join(s for s, have in shares.items() if v.id in have) or "-"
        print(f"  {v.id:>7}  {when(v.timestamp):<20}  {(v.status or '?'):<10}  {here}{mark}")
    return EXIT_OK


def main() -> int:
    global _command
    _command = ""
    a = sys.argv[1:]
    if a in (["-h"], ["--help"]):
        print(__doc__)
        return EXIT_OK
    # alone, --version is hbkit's own version; with arguments it selects a backup version
    if a in (["--version"], ["-V"]):
        from . import __version__
        print(f"hbkit {__version__}")
        return 0
    # `--` ends the options: what follows is positional even if it starts with "-"
    literal: list[str] = []
    if "--" in a:
        i = a.index("--")
        a, literal = a[:i], a[i + 1:]
    try:
        jobs = 8
        j = pop_option(a, "-j")
        jobs_given = j is not None
        if j is not None:
            try:
                jobs = int(j)
            except ValueError:
                raise UsageError("-j needs a number") from None
        strict = pop_flag(a, "--strict")
        password = pop_option(a, "-p", "--password")
        version = pop_option(a, "--version")
        before = pop_option(a, "--before")
        if version is not None and before is not None:
            raise UsageError("use either --version or --before, not both")
        if before is not None:
            version = "@" + before.removeprefix("@")
        allow_incomplete = pop_flag(a, "--allow-incomplete")
        porcelain = pop_flag(a, "--porcelain")
    except UsageError as e:
        print(e, file=sys.stderr)
        return EXIT_USAGE
    n_flagged = len(a)                  # positions where a "-" word is an option
    a += literal
    if len(a) < 2 and (not a or a[0] != "remotes"):
        print(__doc__)
        return EXIT_USAGE
    cmd = a[1] if len(a) > 1 else ""
    if porcelain and cmd != "versions":
        print("--porcelain applies only to `versions`", file=sys.stderr)
        return EXIT_USAGE
    if a[0] in ("mount", "unmount", "remotes", "warm") and (version or allow_incomplete):
        print(f"{a[0]} takes no --version/--before/--allow-incomplete", file=sys.stderr)
        return EXIT_USAGE
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
    _command = cmd

    # Refuse what a command would silently ignore: a stray word (an unquoted path with a
    # space in it) or an option that has no effect there.
    if cmd in MAX_ARGS:
        # globs and patterns are archive paths or words, so a leading "-" is a stray option
        hi = MAX_ARGS[cmd]
        stray = [x for x in a[2:n_flagged] if x.startswith("-")]
        if stray:
            print(f"unknown option {stray[0]} (put `--` before a pattern starting with -)",
                  file=sys.stderr)
            return EXIT_USAGE
        if len(a) > hi:
            print(f"{cmd}: unexpected argument {a[hi]!r} (quote paths with spaces)",
                  file=sys.stderr)
            return EXIT_USAGE
    for given, name, cmds in ((strict, "--strict", ("get",)),
                              (jobs_given, "-j", ("get", "verify", "tui")),
                              (allow_incomplete, "--allow-incomplete",
                               ("list", "get", "verify", "info", "doctor", "tui"))):
        if given and cmd not in cmds:
            print(f"{name} does not apply to `{cmd}`", file=sys.stderr)
            return EXIT_USAGE

    if cmd == "versions":
        if version is not None:
            print("versions lists every version; it takes no --version/--before",
                  file=sys.stderr)
            return EXIT_USAGE
        return cmd_versions(archive, porcelain)

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
        if version is not None or allow_incomplete:
            # a typo or a missing version is not an unrecoverable archive: say so plainly
            if version is not None:
                hbk.Archive(archive, unlock=False).resolve_version(version, allow_incomplete)
            kw["version"], kw["allow_incomplete"] = version, allow_incomplete
        r = doctor.diagnose(archive, **kw)
        print(doctor.render(r))
        return 0 if (r.ok and not r.blockers) else 1

    if cmd == "tui":
        from .tui import HBKApp
        # fail here, on the terminal, rather than inside the full-screen UI
        if version is not None:
            hbk.Archive(archive, unlock=False).resolve_version(version, allow_incomplete)
        elif allow_incomplete:          # same fallback as `get` when the list is unusable
            hbk.Archive(archive, unlock=False, allow_incomplete=True).selected_version()
        HBKApp(jobs, archive, version=version, allow_incomplete=allow_incomplete).run()
        return 0

    if cmd == "info":
        arc = open_archive(archive, password, version, allow_incomplete)
        cfg = arc.task_config()
        print(f"archive   : {arc.root}")
        print(f"task      : {cfg.get('name','?')}   host {cfg.get('host_name','?')}")
        print(f"codec     : {CODECS.get(cfg.get('data_compress_type',''), '?')}")
        print(f"encrypted : {'yes (unlocked)' if arc.crypto else arc.is_encrypted()}")
        print(f"sources   : {cfg.get('backup_folders','?')}")
        problem = None
        try:
            arc.selected_version()
        except (hbk.VersionNotFound, hbk.UnsupportedArchive) as e:
            problem = e                  # still show what there is, to choose from
        for s in arc.shares():
            reading = ""
            if problem is None:
                try:
                    reading = f"   reading={arc.share_version(s)}"
                except FileNotFoundError:
                    reading = "   (not in the selected version)"
            print(f"  share {s}   versions={arc.share_versions(s)}{reading}")
        try:
            vs = arc.versions()
        except hbk.UnsupportedArchive as e:
            print(f"versions  : unknown ({e})")
            if problem:                  # carries the --allow-incomplete hint
                print(f"unsupported archive: {problem}", file=sys.stderr)
                return EXIT_USAGE
            return EXIT_OK
        print(f"versions  : {len(vs)}"
              + ("" if problem else f"   {describe_version(arc)}"))
        for v in vs:
            print(f"  {v.id:>5}  {when(v.timestamp)}  {v.status or '?'}")
        if problem:
            print(problem, file=sys.stderr)
            return EXIT_VERSION
        return EXIT_OK

    if cmd not in ("list", "get", "verify"):
        print(__doc__)
        return EXIT_USAGE

    arc, db = open_index(archive, password, version, allow_incomplete)

    if cmd == "list":
        pat = a[2] if len(a) > 2 else ""
        if any(c in pat for c in "*?["):        # a glob, matched exactly as `get` does
            match = lambda p: fnmatch.fnmatch(p, pat)          # noqa: E731
        else:                                   # plain text: case-insensitive substring
            match = lambda p: pat.lower() in p.lower()         # noqa: E731
        n = tot = 0
        for path, ovf, size, mt in all_files(db):
            if match(path):
                n += 1
                tot += size or 0
                if n <= 200:
                    print(f"{size or 0:>13,}  {path}")
        print(f"\n{n:,} files, {human(tot)}" + (" (first 200 shown)" if n > 200 else ""))
        return EXIT_OK if n else EXIT_NO_MATCH

    if cmd in ("get", "verify"):
        if len(a) < 3 or (cmd == "get" and len(a) < 4):
            print(__doc__)
            return EXIT_USAGE
        glob = a[2]
        dest = os.path.abspath(os.path.expanduser(a[3])) if cmd == "get" else ""
        sel = [row for row in all_files(db) if fnmatch.fnmatch(row[0], glob)]
        if not sel:
            print("no files matched that glob")
            return EXIT_NO_MATCH
        if cmd == "get":
            need = sum(s[2] for s in sel)
            probe = dest
            while probe and not os.path.exists(probe):
                probe = os.path.dirname(probe)
            free = shutil.disk_usage(probe or "/").free
            if need > free:
                print(f"not enough space: need {human(need)}, have {human(free)} free")
                return EXIT_NO_SPACE
        return run(arc, sel, dest, jobs, cmd == "verify",
                   password=arc_password(arc, password), strict=strict)

    print(__doc__)
    return EXIT_USAGE


def main_entry() -> int:
    """Console-script entry point; keeps `main()` usable as a plain function."""
    from .crypto import MissingCryptoDeps, WrongPassword
    try:
        rc = main()
        try:
            sys.stdout.flush()  # a closed pipe must fail here, not at interpreter exit (120)
        except BrokenPipeError:
            sys.stdout = open(os.devnull, "w")
            # `main()` finished, so its result is known: the reader only missed the tail
        return rc
    except WrongPassword:
        print("wrong password for this archive", file=sys.stderr)
        return EXIT_PASSWORD
    except hbk.VersionNotFound as e:
        print(f"{e}", file=sys.stderr)
        return EXIT_VERSION
    except hbk.InvalidVersionSpec as e:
        print(f"{e}", file=sys.stderr)
        return EXIT_USAGE
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
        return EXIT_PASSWORD
    except FileNotFoundError as e:
        print(f"{e}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")      # no second error at interpreter exit
        except OSError:
            pass
        # `hbk ... list | head`: the reader has what it wanted. `get`/`verify` may not
        # have run, or their result was lost with the report: never call that success.
        return EXIT_OK if _command in ("list", "versions", "info") else EXIT_PIPE
    except OSError as e:
        print(f"{e}", file=sys.stderr)
        return 2
    except Exception:                                        # noqa: BLE001
        # A bug or an unforeseen archive state. Exit 1 means "files failed their
        # checksums" to scripts, so an unexpected error must not look like that.
        import traceback
        traceback.print_exc()
        print("hbkit: unexpected error (exit 2) - please report it", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main_entry())
