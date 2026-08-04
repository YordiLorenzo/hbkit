#!/usr/bin/env python3
"""Probe an unknown Hyper Backup archive and report whether it can be recovered.

This exists because the tool has only ever been proven against a handful of archives.
Rather than claim generality, `doctor` inspects what is actually in front of it, then
*proves* readability by reconstructing a random sample of real files and checking every
chunk against the archive's own MD5 and CRC32. A PASS is evidence, not an assertion.
"""
from __future__ import annotations

import os
import random
import sqlite3
import stat
import sys
import time


from . import archive as hbk

CODECS = {"0": "none", "1": "lz4", "2": "lz4-hc", "4": "zlib"}


class Report:
    def __init__(self):
        self.facts: list[tuple[str, str]] = []
        self.checks: list[tuple[str, bool, str]] = []
        self.blockers: list[str] = []
        self.warnings: list[str] = []

    def fact(self, k, v):
        self.facts.append((k, str(v)))

    def check(self, name, ok, detail=""):
        self.checks.append((name, bool(ok), detail))
        return ok

    @property
    def ok(self):
        return not self.blockers and all(c[1] for c in self.checks)


def human(n):
    n = float(n or 0)
    for u in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or u == "T":
            return f"{n:.0f}B" if u == "B" else f"{n:,.1f}{u}"
        n /= 1024
    return f"{n:,.1f}T"


def diagnose(path: str, sample: int = 10, seed: int = 0, password: str | None = None) -> Report:
    r = Report()

    try:
        arc = hbk.Archive(path, password=password)
    except hbk.UnsupportedArchive as e:
        r.blockers.append(f"unsupported layout: {e}")
        return r
    except hbk.NeedPassword:
        r.blockers.append("archive is ENCRYPTED - re-run with --password")
        return r
    except Exception as e:                                   # noqa: BLE001
        r.blockers.append(f"cannot open archive: {type(e).__name__}: {e}")
        return r

    cfg = arc.task_config()
    r.fact("archive", arc.root)
    r.fact("task", cfg.get("name", "?"))
    r.fact("source host", cfg.get("host_name", "?"))
    r.fact("source model", cfg.get("source_model", "?"))
    r.fact("created by", cfg.get("compatible_info", "?")[:80])
    r.fact("backed-up paths", cfg.get("backup_folders", "?"))
    codec = CODECS.get(cfg.get("data_compress_type", ""), f"unknown({cfg.get('data_compress_type')})")
    r.fact("chunk codec", codec)
    r.fact("encrypted", "yes (unlocked)" if arc.crypto else "no")
    r.fact("dedup across files", cfg.get("support_cross_file_dedup", "?"))

    # --- blockers -------------------------------------------------------
    if arc.is_encrypted():
        r.check("password unlocks the archive", arc.crypto is not None,
                "derived public key matches Config/public.pem")
    if codec.startswith("unknown"):
        r.warnings.append(f"unrecognised data_compress_type {cfg.get('data_compress_type')!r}; "
                          "lz4 and zlib are both attempted per chunk, so it may still work")

    # --- structural layout ----------------------------------------------
    r.fact("virtual_file record", f"{arc.vf.record_size} B")
    r.fact("chunk_index record", f"{arc.ci.record_size} B "
                                 f"({'v3' if arc.ci.record_size == 29 else 'v1/v2'})")
    r.fact("file_chunk shards", ", ".join(str(k) for k in sorted(arc.fc)))
    r.check("virtual_file layout known", arc.vf.record_size == 56, f"{arc.vf.record_size} B")
    r.check("chunk_index layout known", arc.ci.record_size in (16, 29), f"{arc.ci.record_size} B")

    if len(arc.pools) > 1:
        r.warnings.append(f"{len(arc.pools)} pool directories present; only pool "
                          f"{os.path.basename(arc.pool)} is read")
    r.fact("pool dirs", len(arc.pools))

    # bucket index variant, sampled from the first bucket we can find
    try:
        first = None
        for d in sorted(os.listdir(arc.pool), key=lambda x: int(x) if x.isdigit() else 1 << 30):
            if d.isdigit():
                first = int(d) << 11
                break
        if first is not None:
            arc._bucket(first)
            n = arc._brec[first]
            r.fact("bucket index record", f"{n} B ({'md5+crc32' if n == 32 else 'md5 only, legacy'})")
            r.check("bucket layout known", n in (28, 32), f"{n} B")
    except hbk.UnsupportedArchive as e:
        r.blockers.append(str(e))
    except Exception as e:                                   # noqa: BLE001
        r.warnings.append(f"could not probe a bucket: {type(e).__name__}: {e}")

    # --- shares and versions --------------------------------------------
    shares = arc.shares()
    r.fact("shares", ", ".join(shares) or "(none)")
    if not shares:
        r.blockers.append("no shares found under Config/@Share")
    multi = []
    for s in shares:
        vs = arc.share_versions(s)
        if len(vs) > 1:
            multi.append(f"{s}={vs}")
    if multi:
        r.warnings.append("multiple backup versions present; the newest is used by default "
                          "(--version selects another): " + "; ".join(multi))

    # --- prove it by actually rebuilding files ---------------------------
    if not r.blockers:
        picked = []
        for s in shares:
            try:
                db = sqlite3.connect(f"file:{arc.share_db(s)}?immutable=1", uri=True)
                rows = db.execute(
                    "SELECT file_name,size,off_virtual_file,mode FROM version_list "
                    "WHERE size>0 AND off_virtual_file>=0").fetchall()
                db.close()
            except Exception as e:                           # noqa: BLE001
                r.warnings.append(f"share {s}: cannot read file list ({e})")
                continue
            rows = [x for x in rows if stat.S_ISREG(x[3] or 0)]
            if rows:
                rnd = random.Random(seed or 1234)
                picked += [(s, *rnd.choice(rows)[:3]) for _ in range(min(sample, len(rows)))]

        if not picked:
            r.warnings.append("no regular files found to sample")
        else:
            ok = bad = 0
            nbytes = 0
            errs = []
            t0 = time.time()
            for share, name, size, ovf in picked:
                try:
                    d = arc.extract(ovf, size, verify=True)
                    if len(d) == size:
                        ok += 1
                        nbytes += size
                    else:
                        bad += 1
                        errs.append(f"{name}: got {len(d)} want {size}")
                except Exception as e:                       # noqa: BLE001
                    bad += 1
                    errs.append(f"{name}: {type(e).__name__}: {e}")
            el = max(time.time() - t0, 1e-6)
            r.check(f"rebuilt {len(picked)} sampled files, all chunks verified",
                    bad == 0, f"{ok} ok, {bad} failed" + (f" - {errs[0]}" if errs else ""))
            r.fact("sample throughput", f"{human(nbytes)} in {el:.1f}s "
                                        f"({nbytes/el/1e6:,.0f} MB/s, single-threaded)")
    arc.close()
    return r


def render(r: Report) -> str:
    out = []
    w = max((len(k) for k, _ in r.facts), default=0)
    for k, v in r.facts:
        out.append(f"  {k.rjust(w)} : {v}")
    if r.checks:
        out.append("")
        for name, ok, detail in r.checks:
            out.append(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    for wmsg in r.warnings:
        out.append(f"\n  WARNING: {wmsg}")
    for b in r.blockers:
        out.append(f"\n  BLOCKED: {b}")
    out.append("")
    if r.blockers:
        out.append("  VERDICT: this archive cannot be recovered by this tool.")
    elif r.ok:
        out.append("  VERDICT: recoverable. Sampled files rebuilt byte-exact and checksum-verified.")
    else:
        out.append("  VERDICT: layout recognised but sample extraction FAILED - do not trust output.")
    return "\n".join(out)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("archive")
    ap.add_argument("-p", "--password", help="for encrypted archives")
    ap.add_argument("-n", "--sample", type=int, default=10,
                    help="files to rebuild per share as proof (default 10)")
    a = ap.parse_args()
    r = diagnose(a.archive, a.sample, password=a.password)
    print(render(r))
    return 0 if r.ok and not r.blockers else 1


if __name__ == "__main__":
    sys.exit(main())
