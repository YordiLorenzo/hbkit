"""Diagnose a file that will not rebuild.

Usage:  python3 hbk_probe.py <archive.hbk> <filename-substring> [-p PASSWORD]

Walks one file's chunk list the way extract() does. When an entry is not a usable
chunk_index key, it records it, skips it, and keeps going, then reports whether the
file lands on exactly its recorded size. Every chunk is MD5-checked against the
archive's own checksum, so "completes at the right size" also means "the bytes were
right". Reads only; prints sizes and record bytes, never file contents.
"""
import struct, sys

from hbkit import cli

path, needle = sys.argv[1], sys.argv[2].lower()
pw = sys.argv[sys.argv.index("-p") + 1] if "-p" in sys.argv else None

arc, db = cli.open_index(path, pw)
row = next((r for r in cli.all_files(db) if needle in r[0].lower()), None)
if row is None:
    sys.exit(f"no file matching {needle!r}")
_p, ovf, size, _ = row
REC, HDR = arc.ci.record_size, 64

v = struct.unpack(">q", arc.vf.read(ovf, 8))[0]
shard, off = v >> 48, v & 0xFFFFFFFFFFFF
fc = arc.fc.get(shard)
print(f"size {size:,}   ci.total {arc.ci.total:,}   rec {REC}   fc{shard} @ {off:,}")

def usable(k):
    return 0 <= k < arc.ci.total and (k - HDR) % REC == 0

got = i = skipped = 0
breaks = []
while got < size and i < 400_000:
    blob = fc.read(off + i * 8, 8 * 4096)
    if not blob:
        print(f"\nlist exhausted at index {i}, {got:,}/{size:,}")
        break
    j = 0
    while j <= len(blob) - 8 and got < size:
        k = struct.unpack(">q", blob[j:j + 8])[0]
        if not usable(k):
            # How long is this run of non-keys, and what do the raw bytes look like?
            run, jj = [], j
            while jj <= len(blob) - 8:
                kk = struct.unpack(">q", blob[jj:jj + 8])[0]
                if usable(kk):
                    break
                run.append(kk)
                jj += 8
            if len(breaks) < 4:
                print(f"\nBREAK at index {i}: {len(run)} non-key entr{'y' if len(run)==1 else 'ies'}"
                      f", {got:,}/{size:,} rebuilt")
                print("  raw:", blob[max(0, j - 16):jj + 16].hex(' '))
                for n, kk in enumerate(run[:6]):
                    print(f"    +{n}: {kk:#018x}")
            breaks.append((i, len(run)))
            skipped += len(run)
            i += len(run)
            j = jj
            continue
        bid, boff = arc.chunk_ref(k)
        got += len(arc.read_chunk(bid, boff, verify=True))   # MD5-checked
        i += 1
        j += 8

print(f"\n{'=' * 60}")
print(f"rebuilt      : {got:,} of {size:,}")
print(f"EXACT MATCH  : {got == size}")
print(f"chunks used  : {i - skipped:,}   entries skipped: {skipped:,}   breaks: {len(breaks)}")
if breaks:
    runs = {}
    for _ix, n in breaks:
        runs[n] = runs.get(n, 0) + 1
    print(f"run lengths  : {dict(sorted(runs.items()))}")
    print(f"break indices: {[b[0] for b in breaks[:12]]}{' ...' if len(breaks) > 12 else ''}")
