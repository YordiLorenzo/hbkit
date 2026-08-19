"""Dump the chunk_index records behind one file, to diagnose a failing rebuild.

Usage:  python3 hbk_probe.py <archive.hbk> <filename-substring> [-p PASSWORD]

Reads only. Prints raw record bytes; no file contents are shown.
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
print(f"file        : {p}")
print(f"size        : {size:,}")
print(f"ovf         : {ovf}")
print(f"ci.total    : {arc.ci.total:,}   record_size={arc.ci.record_size}")
print(f"vf.total    : {arc.vf.total:,}")

v = struct.unpack(">q", arc.vf.read(ovf, 8))[0]
shard, off = v >> 48, v & 0xFFFFFFFFFFFF
print(f"vf record   : {v:#018x}  -> file_chunk{shard} @ {off:,}")
fc = arc.fc.get(shard)
if fc is None:
    sys.exit(f"no file_chunk{shard}.index")
print(f"fc{shard}.total  : {fc.total:,}")

# how many chunk keys does this file need, roughly
blob = fc.read(off, 8 * 4096)
keys = [struct.unpack(">q", blob[j:j + 8])[0] for j in range(0, len(blob) - 7, 8)]

def in_range(k):
    return 0 <= k < arc.ci.total

bad = [(i, k) for i, k in enumerate(keys) if not in_range(k)]
print(f"\nkeys read   : {len(keys)}   out-of-range: {len(bad)}")
print("\nfirst 8 keys:")
for i, k in enumerate(keys[:8]):
    print(f"  [{i:4}] {k:#018x}  {'OK ' if in_range(k) else 'BAD'}  low56={k & ((1<<56)-1):,}")

if bad:
    i, k = bad[0]
    lo56 = k & ((1 << 56) - 1)
    lo48 = k & ((1 << 48) - 1)
    print(f"\nfirst BAD key at index {i}: {k:#018x}")
    print(f"  top byte    : {k >> 56:#04x}      low56 = {lo56:,}  in range: {in_range(lo56)}")
    print(f"  top 16 bits : {k >> 48:#06x}    low48 = {lo48:,}  in range: {in_range(lo48)}")
    for label, cand in (("low56", lo56), ("low48", lo48)):
        if in_range(cand):
            rec = arc.ci.read(cand, arc.ci.record_size)
            print(f"  record at {label} ({cand:,}): {rec.hex(' ')}")
            if len(rec) >= 9:
                print(f"      flags=0x{rec[0]:02x}  as>ii={struct.unpack('>ii', rec[1:9])}")

print("\nfor comparison, a GOOD key's record:")
g = next((k for k in keys if in_range(k)), None)
if g is not None:
    rec = arc.ci.read(g, arc.ci.record_size)
    print(f"  key {g:#018x} @ {g:,}: {rec.hex(' ')}")
    print(f"      flags=0x{rec[0]:02x}  as>ii={struct.unpack('>ii', rec[1:9])}")
