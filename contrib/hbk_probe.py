"""Dump the chunk_index records behind one file, to diagnose a failing rebuild.

Usage:  python3 hbk_probe.py <archive.hbk> <filename-substring> [-p PASSWORD]

Reads only. Prints record bytes and sizes, never file contents.
"""
import struct, sys

from hbkit import cli
from hbkit import archive as hbk

path = sys.argv[1]
needle = sys.argv[2].lower()
pw = sys.argv[sys.argv.index("-p") + 1] if "-p" in sys.argv else None

arc, db = cli.open_index(path, pw)
row = next((r for r in cli.all_files(db) if needle in r[0].lower()), None)
if row is None:
    sys.exit(f"no file matching {needle!r}")
p, ovf, size, _ = row
REC, HDR = arc.ci.record_size, 64

print(f"size        : {size:,}")
print(f"ci.total    : {arc.ci.total:,}   record_size={REC}")

v = struct.unpack(">q", arc.vf.read(ovf, 8))[0]
shard, off = v >> 48, v & 0xFFFFFFFFFFFF
fc = arc.fc.get(shard)
print(f"vf record   : {v:#018x}  -> file_chunk{shard} @ {off:,}  (fc.total {fc.total:,})")

def sane(k):
    """A key must land inside chunk_index AND on a record boundary."""
    return 0 <= k < arc.ci.total and (k - HDR) % REC == 0

# Walk exactly like extract() does: stop as soon as the file is complete.
# The question this answers is whether the first unusable key arrives BEFORE or
# AFTER the file's bytes are all accounted for.
got, i, first_bad, rows = 0, 0, None, []
while got < size and i < 200_000:
    blob = fc.read(off + i * 8, 8 * 4096)
    if not blob:
        print(f"\nchunk list exhausted at key {i}, {got:,}/{size:,} bytes")
        break
    for j in range(0, len(blob) - 7, 8):
        if got >= size:
            break
        k = struct.unpack(">q", blob[j:j + 8])[0]
        if not sane(k):
            if first_bad is None:
                first_bad = (i, k)
            print(f"\nUNUSABLE KEY at index {i}: {k:#018x}")
            print(f"  bytes accumulated so far : {got:,} of {size:,}  ({100*got/size:.2f}%)")
            print(f"  short by                 : {size - got:,} bytes")
            print(f"  in_range={0 <= k < arc.ci.total}  boundary={(k - HDR) % REC == 0}")
            print(f"  previous 6 keys:")
            for pi, pk, pn in rows[-6:]:
                print(f"    [{pi:5}] {pk:#018x}  ulen={pn:,}")
            sys.exit(0)
        bid, boff = arc.chunk_ref(k)
        idx, _fh = arc._bucket(bid)
        n = arc._brec[bid]
        ulen = struct.unpack(">III", idx[boff:boff + n][:12])[2]
        got += ulen
        rows.append((i, k, ulen))
        i += 1

print(f"\nfile completes cleanly: {got:,}/{size:,} bytes over {i:,} chunks")
if rows:
    us = [r[2] for r in rows]
    print(f"chunk sizes: min {min(us):,}  max {max(us):,}  mean {sum(us)//len(us):,}")
