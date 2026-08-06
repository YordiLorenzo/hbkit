"""Convenience wrapper around `rclone mount` for archives kept in object storage.

This stores no credentials and talks to no cloud API - `rclone config` still owns all of
that. All it does is pick the flags, because the flags are easy to get badly wrong:
rclone's `--vfs-cache-mode full` fetches *whole files*, and hbkit reads a 32-byte index
record and a ~5 KB chunk at a time, so a 10 KB extraction can pull ~82 MB. Meanwhile
`off` issues range requests, which is wrong for the one-time index build (a single
sequential read of a share database that can be hundreds of MB).

So the mode depends on the job, and that is what `--for` selects:

    browse   -> off,  range requests; cheap metadata, good for cherry-picking files
    restore  -> full, whole-file fetch acts as read-ahead when pulling a whole folder,
                capped so it cannot fill the disk
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

PROFILES = {
    # name: (vfs-cache-mode, extra flags, one-line rationale)
    "browse": ("off", [],
               "range requests - best for opening an archive and pulling a few files"),
    "restore": ("full", ["--vfs-cache-max-size", "50G"],
                "whole-file fetch as read-ahead for bulk restores, cache capped at 50G"),
}

# --no-modtime is the single biggest win. Without it rclone issues a HEAD per object just
# to fill in modification times for a directory listing, which is ~0.27s per entry: the
# 2,021-shard chunk_index directory took 546.8s cold. With it, 4.07s - a 134x speedup.
# hbkit never uses the modtimes of an archive's internal files (restored file mtimes come
# from the archive's own SQLite metadata), so dropping them costs nothing.
COMMON = ["--read-only", "--no-modtime", "--dir-cache-time", "72h", "--daemon"]


class RcloneMissing(Exception):
    pass


def require_rclone() -> str:
    p = shutil.which("rclone")
    if not p:
        raise RcloneMissing(
            "rclone is not installed. Install it (brew install rclone / "
            "apt install rclone), then run `rclone config` to add your S3/R2 remote.")
    return p


def remotes() -> list[str]:
    """Configured rclone remotes, e.g. ['r2:', 's3:']."""
    try:
        out = subprocess.run([require_rclone(), "listremotes"],
                             capture_output=True, text=True, timeout=15)
        return [x for x in out.stdout.split() if x]
    except (RcloneMissing, subprocess.SubprocessError):
        return []


def is_mounted(path: str) -> bool:
    path = os.path.abspath(os.path.expanduser(path))
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
    except subprocess.SubprocessError:
        return False
    return any(f" {path} " in ln or ln.endswith(f" {path}") for ln in out.splitlines())


def build_command(remote: str, mountpoint: str, profile: str = "browse",
                  cache_size: str | None = None) -> list[str]:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; choose from {', '.join(PROFILES)}")
    mode, extra, _ = PROFILES[profile]
    extra = list(extra)
    if cache_size and mode == "full":
        extra = ["--vfs-cache-max-size", cache_size]
    return ([require_rclone(), "mount", remote, os.path.abspath(os.path.expanduser(mountpoint)),
             "--vfs-cache-mode", mode] + extra + COMMON)


def mount(remote: str, mountpoint: str, profile: str = "browse",
          cache_size: str | None = None, dry_run: bool = False) -> int:
    mp = os.path.abspath(os.path.expanduser(mountpoint))
    if is_mounted(mp):
        print(f"already mounted: {mp}")
        return 0
    cmd = build_command(remote, mp, profile, cache_size)
    mode, _, why = PROFILES[profile]
    print(f"profile {profile}: --vfs-cache-mode {mode}  ({why})")
    print("  " + " ".join(cmd))
    if dry_run:
        return 0
    os.makedirs(mp, exist_ok=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print((r.stderr or r.stdout).strip(), file=sys.stderr)
        return r.returncode
    # --daemon returns immediately; wait for the mountpoint to become live
    import time
    for _ in range(60):
        if is_mounted(mp) and os.listdir(mp):
            break
        time.sleep(0.5)
    if not is_mounted(mp):
        print(f"rclone returned success but {mp} is not mounted", file=sys.stderr)
        return 1
    print(f"mounted {remote} at {mp}")
    print("  tip: over a network mount use more workers than the local default "
          "(-j 16); throughput is latency-bound, not CPU-bound")
    for e in sorted(os.listdir(mp))[:10]:
        print(f"    {e}")
    return 0


# Directories hbkit lists when opening an archive. rclone caches a listing for
# --dir-cache-time, so walking these once up front turns a ~9 minute "did it hang?" on
# the user's first command into a visible, attributable wait here. Measured on a 3 TB
# archive over R2: 9m21s cold, then 1.6s, then 0.07s.
def warm(mountpoint: str, quiet: bool = False) -> int:
    """Populate rclone's directory cache for every .hbk archive under `mountpoint`."""
    import time
    mp = os.path.abspath(os.path.expanduser(mountpoint))
    archives = []
    for e in sorted(os.listdir(mp)):
        d = os.path.join(mp, e)
        if os.path.isdir(d) and os.path.isdir(os.path.join(d, "Config")):
            archives.append(d)
    if not archives:
        return 0
    n = 0
    t0 = time.time()
    for arc in archives:
        targets = [arc, os.path.join(arc, "Config"), os.path.join(arc, "Pool")]
        for sub in ("Config/virtual_file.index", "Pool/chunk_index", "Config/@Share"):
            targets.append(os.path.join(arc, sub))
        cfg = os.path.join(arc, "Config")
        try:
            targets += [os.path.join(cfg, x) for x in os.listdir(cfg)
                        if x.startswith("file_chunk") and os.path.isdir(os.path.join(cfg, x))]
        except OSError:
            pass
        share = os.path.join(arc, "Config", "@Share")
        if os.path.isdir(share):
            targets += [os.path.join(share, x) for x in os.listdir(share)]
        for t in targets:
            if not os.path.isdir(t):
                continue
            if not quiet:
                print(f"  warming {os.path.relpath(t, mp)} ...", end="", flush=True)
            s0 = time.time()
            try:
                c = len(os.listdir(t))
            except OSError as e:
                c = -1
            if not quiet:
                print(f" {c} entries, {time.time()-s0:.1f}s")
            n += 1
    if not quiet:
        print(f"warmed {n} directories in {time.time()-t0:.0f}s "
              f"(cached for --dir-cache-time; later opens are instant)")
    return 0


def unmount(mountpoint: str) -> int:
    mp = os.path.abspath(os.path.expanduser(mountpoint))
    if not is_mounted(mp):
        print(f"not mounted: {mp}")
        return 0
    for cmd in (["umount", mp], ["fusermount", "-u", mp], ["diskutil", "unmount", "force", mp]):
        if shutil.which(cmd[0]) and subprocess.run(cmd, capture_output=True).returncode == 0:
            print(f"unmounted {mp}")
            return 0
    print(f"could not unmount {mp}", file=sys.stderr)
    return 1
