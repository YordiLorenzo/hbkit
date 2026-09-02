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
if ovf is None or ovf < 0:
    sys.exit("this file is whole-file deduplicated into Pool/file_pool, which hbkit does "
             "not decode. It has no chunk list, so there is nothing here to probe.")
REC, HDR = arc.ci.record_size, 64

v = struct.unpack(">q", arc.vf.read(ovf, 8))[0]
shard, off = v >> 48, v & 0xFFFFFFFFFFFF
fc = arc.fc.get(shard)
if fc is None:
    sys.exit(f"virtual_file points at file_chunk{shard}.index, which this archive "
             f"does not have (present: {arc.fc.known})")
print(f"size {size:,}   ci.total {arc.ci.total:,}   rec {REC}   fc{shard} @ {off:,}")

def usable(k):
    """A key must point at a whole record that starts after the shard-0 header.

    The lower bound matters: with 16-byte records, 0/16/32/48 all satisfy the
    alignment test, so zero-filled padding would be handed to chunk_ref() as if it
    were a record and abort the probe instead of being skipped.
    """
    return HDR <= k and k + REC <= arc.ci.total and (k - HDR) % REC == 0

# The list cannot extend past the end of its own file_chunk family, so bound the walk
# on that rather than on a guessed entry count: a 3 GB file legitimately needs hundreds
# of thousands of chunks, and a fixed cap would report it as a rebuild failure.
limit, tail = divmod(fc.total - off, 8)
got = i = skipped = 0
breaks = []
stop = None
while got < size and i < limit:
    blob = fc.read(off + i * 8, 8 * 4096)
    if len(blob) < 8:
        # Cat computes shard offsets from a fixed shard size rather than measuring them,
        # so a short read here means that assumption does not hold for this archive.
        stop = f"unexpected short read at index {i}: shard sizes are not what Cat assumes"
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

if stop:
    print(f"\n{stop}")
elif got < size and i >= limit:
    print(f"\nreached the end of file_chunk{shard} after {i:,} entries with the file "
          f"still incomplete: the chunk list does not account for all of it")
    if tail:
        print(f"  the index also ends {tail} byte{'' if tail == 1 else 's'} into an "
              f"incomplete entry, so it is truncated")

print(f"\n{'=' * 60}")
print(f"rebuilt      : {got:,} of {size:,}")
print(f"LENGTH MATCH : {got == size}")
print("  note: each chunk was checked against its own stored MD5, which proves the chunk\n"
      "  is intact. It does NOT prove these are this file's chunks in this order, and the\n"
      "  archive stores no whole-file digest to check that against. A length match is\n"
      "  strong evidence the skipped entries were not chunk data, not proof of it.")
print(f"chunks used  : {i - skipped:,}   entries skipped: {skipped:,}   breaks: {len(breaks)}")
if breaks:
    runs = {}
    for _ix, n in breaks:
        runs[n] = runs.get(n, 0) + 1
    print(f"run lengths  : {dict(sorted(runs.items()))}")
    print(f"break indices: {[b[0] for b in breaks[:12]]}{' ...' if len(breaks) > 12 else ''}")
