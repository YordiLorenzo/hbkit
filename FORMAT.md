# The Synology Hyper Backup (`.hbk`) on-disk format

A reverse-engineered specification, written so that anyone can implement a reader in any
language. To our knowledge no public description of this format existed before this
document: searching the distinctive magic `70 53 A8 6E` returns nothing on the open web,
in file-signature databases, or in DFIR tooling.

**Status legend** — every claim below is tagged:

- **[V]** Verified. Proven by reconstructing real files byte-exactly and checking them
  against the archive's own MD5 and CRC32 values.
- **[D]** Disassembly. Read from exported C++ symbols in Synology's own
  `HyperBackupExplorer` binary, but not independently exercised by us.
- **[I]** Inferred. Consistent with observation, not proven. Treat with suspicion.
- **[?]** Unknown. Documented so the next person knows where the edges are.

Coverage caveat: this was derived from **one** archive — DSM 7, Hyper Backup 4.1.2-4039,
unencrypted, LZ4, single version, single pool. Variant handling marked [D] is implemented
from disassembly but has never met a real archive of that kind.

---

## 1. Conventions

- **All integers are big-endian.** No exceptions were found. [V]
- **Generation suffixes.** Most files carry a trailing `.<n>`: `0.idx.2`, `1.db.2`,
  `index_ver.json.1`. A reader should resolve `name`, `name.1`, `name.2`… and prefer the
  highest generation. [V]
- **macOS junk.** Archives stored on exFAT/HFS accumulate AppleDouble sidecars named
  `._<something>`. These are not part of the format and must be filtered, or they will be
  mistaken for real shards. [V]

## 2. Directory layout

```
<target>.hbk/
  _Syno_TaskConfig            INI-ish task settings (plain text)
  SynologyHyperBackup.bkpi    marker, may be zero bytes
  Config/
    index_ver.json[.n]        {"major":0,"minor":9,"sub_minor":1}
    target_ver.json[.n]
    version_info.db[.n]       SQLite: one row per backup version
    virtual_file.index/       shard dir  (56-byte records)
    file_chunk<N>.index/      shard dirs (flat i64 arrays), N = 1..4 observed
    @Share/<share>/
      <version>.db[.n]        SQLite: the file tree for that share+version
      complete_list.db[.n]    SQLite: which versions completed
  Pool/
    chunk_index/              shard dir (29-byte records, or 16 on older archives)
    bucketID.counter[.n]      u64 big-endian: number of buckets
    file_pool/                whole-file dedup store  [?]
    <pool>/<dir>/<n>.bucket[.n]   ~50 MB of concatenated compressed chunks
    <pool>/<dir>/<n>.index[.n]    the bucket's chunk directory
  Control/, Guard/            bookkeeping, not needed to read data
```

## 3. The shard container

Index families are directories of shards named `<N>.idx[.gen]`, N = 0,1,2,…
**Concatenate them in numeric order into one logical byte stream.** Shard size is 8 MiB;
records may straddle a shard boundary, so a reader must treat the family as one stream, not
as independent files. [V]

Only shard 0 carries a **64-byte header**, at stream offset 0. Records therefore begin at
**stream offset 64**. [V]

Header, as 16 big-endian `uint32` words:

| word | meaning |
|---|---|
| 0 | magic `0x7053A86E` [V] |
| 1 | kind — 0 virtual_file, 1 chunk_index/file_chunk, 2 bucket index [I] |
| 2 | variant [I] |
| 4 | **record size in bytes** [V] |
| 5:6 | `uint64` total stream length, as `(w5 << 32) | w6` [V] |

Word 4 is the important one: it lets a reader support layout variants without hardcoding
DSM versions. Observed: 56 (virtual_file), 29 (chunk_index), 32 (bucket index), 0 for
`file_chunk` — which is a flat `int64` array with no per-record framing. [V]

The declared length in words 5:6 may slightly exceed the bytes actually present; treat it
as advisory, not as a truncation check. [I]

## 4. The lookup chain

```
version_list.off_virtual_file        (SQLite, per share)
  -> virtual_file record (56 B)
  -> (shard, offset) into file_chunk<shard>.index
  -> flat array of int64 keys, 8 B each
  -> each key = byte offset of a chunk_index record
  -> chunk_index record -> (bucket_id, bucket_index_offset)
  -> bucket .index record -> (offset, compressed length, uncompressed length, MD5)
  -> bucket data file -> one raw LZ4 block
```

### 4.1 virtual_file record — 56 bytes [V for the pointer, D for the rest]

`off_virtual_file` from SQLite is a **byte offset into the virtual_file stream**.

| offset | size | field |
|---|---|---|
| 0 | 8 | **chunk-list pointer** — `(shard << 48) | (byte_offset & 0xFFFF_FFFF_FFFF)` [V] |
| 8 | 4 | ref_count [D] |
| 12 | 4 | uid [D] |
| 16 | 4 | gid [D] |
| 20 | 8 | atime seconds [D] |
| 28 | 4 | atime nanoseconds [D] |
| 32 | 8 | crtime seconds [D] |
| 40 | 4 | crtime nanoseconds [D] |
| 44 | 4 | mod_ver, or CRC depending on variant [D] |
| 48 | 8 | acl_offset, same packed encoding [D] |

The packing is exactly what the vendor binary does:
`FileChunkIndexIdParse(x) = x >> 48`, `FileChunkOffsetParse(x) = x & 0xFFFFFFFFFFFF`. [D]

The shard number is **read, not computed** — it varies per file (values 1–4 observed).

Note the record carries **no size, no name, no mode, no mtime**. All of that lives in
SQLite. The extractor needs the file size from `version_list` to know when to stop
consuming chunks. [V]

`off_virtual_file = -1` is a sentinel meaning the file is whole-file deduplicated into
`Pool/file_pool` rather than chunked. One occurrence in 501,278 files. The file_pool
format is **not decoded**. [?]

### 4.2 file_chunk arrays [V]

At the offset above, the `file_chunk<shard>.index` stream holds a **flat array of
big-endian `int64` values, 8 bytes each**, with no header and no per-record framing.
Each value is the **byte offset of a chunk_index record**.

Read entries in order, resolve each, and stop when the summed *uncompressed* chunk lengths
equal the file size from SQLite. There is no terminator and no stored chunk count.

Immediately before the array sits a **12-byte per-file header** (the `acl_offset` pointer
is exactly 12 bytes lower on every file sampled, 1547/1547). Its contents relate to
ACL/xattr storage and are **not decoded**. [?]

### 4.3 chunk_index record

Two layouts, distinguished by the record size in the shard header.

**29 bytes (v3)** [V]

| offset | size | field |
|---|---|---|
| 0 | 1 | mode; **bit 0 set = indirect** |
| 1 | 8 | if indirect: byte offset of another chunk_index record — follow it |
| 1 | 4 | if direct: `int32` bucket_id |
| 5 | 4 | if direct: `int32` byte offset into that bucket's `.index` |
| 25 | 4 | CRC [?] |

The indirect form is content deduplication: many files' chunks redirect to one canonical
chunk. Follow the chain until bit 0 is clear. Guard the recursion — a malformed archive
could otherwise loop. [V]

**16 bytes (v1/v2)** [D] — older archives:

| offset | size | field |
|---|---|---|
| 0 | 4 | `int32` bucket_id |
| 4 | 4 | `int32` bucket index offset |
| 8 | 4 | ref_count |
| 12 | 4 | mod_ver (v1) or CRC (v2) |

### 4.4 Bucket addressing [V]

```
dir  = bucket_id >> 11          (2048 buckets per directory)
file = bucket_id & 0x7FF
path = Pool/<pool>/<dir>/<file>.bucket[.gen]   and  .index[.gen]
```

The directory is part of the address: the same filename `<n>.bucket` exists in *every*
directory. Getting this wrong is the single easiest way to read the wrong data — and
because chunk MD5s are checked, it surfaces as a verification failure rather than silent
corruption.

### 4.5 Bucket index record

**32 bytes (LAYOUT_D)** [V]

| offset | size | field |
|---|---|---|
| 0 | 4 | `uint32` compressed length |
| 4 | 4 | `uint32` byte offset within the `.bucket` file |
| 8 | 4 | `uint32` uncompressed length |
| 12 | 16 | **MD5 of the decompressed chunk** |
| 28 | 4 | **CRC32 of bytes [0:28] of this record**, poly `0xEDB88320` |

**28 bytes (legacy)** — identical without the trailing CRC32. [D]

Both checks verified across all 82,313 chunks of a 674 MB file. Earlier public
descriptions call the 20-byte tail a SHA-1; it is not. It is MD5 + CRC32, confirmed both
empirically and from the vendor's `getChecksum`, which copies 16 bytes. [V]

### 4.6 Chunk payload [V]

At `offset` in the `.bucket` file, read `compressed length` bytes and decompress to exactly
`uncompressed length`.

The payload is a **raw LZ4 block** — *not* an LZ4 frame. There is no frame header and no
magic; call the block API directly (`LZ4_decompress_safe`) with the known output size.

The codec comes from `data_compress_type` in `_Syno_TaskConfig`. The vendor dispatcher
`SYNO::Backup::decompress(type, …)` branches on **1 = lz4, 2 = lz4-hc, 4 = zlib**; lz4 and
lz4-hc share a decompressor. A robust reader attempts LZ4 and falls back to zlib. [D for
the mapping, V for LZ4]

Existing open-source tools implement only LZ4 and report corruption on some chunks; the
zlib path is the likely explanation.

## 5. SQLite metadata [V]

`Config/@Share/<share>/<version>.db` holds the file tree:

```sql
CREATE TABLE version_list (
  name_id_v2 BLOB PRIMARY KEY,   -- 20-byte node id
  pname_id_v2 BLOB,              -- parent's name_id_v2; join on this to build paths
  off_virtual_file INTEGER,      -- byte offset into the virtual_file stream, or -1
  file_name TEXT, size INTEGER, mode INTEGER,
  mtime_sec INTEGER, ctime_sec INTEGER, inode INTEGER, ...
);
```

Reconstruct paths by joining `pname_id_v2 -> name_id_v2`. Roots are rows whose parent is
absent from the table or which are self-parented. `mode` is a POSIX mode word — use
`S_ISDIR` to separate directories from files.

`Config/version_info.db` lists backup versions with timestamps and completion status.

Entries named `@eaDir`, or ending `@SynoEAStream` / `@SynoResource`, are Synology
metadata — thumbnails, extended attributes, resource forks — not user data. In the
reference archive they were 553,899 of 1,135,405 rows but only ~40 GB of 4.88 TB.

## 6. Verification

Every chunk is self-checking: MD5 over the decompressed bytes, plus CRC32 over the index
record. **A reader that verifies both cannot silently return wrong data** — the worst case
is a loud failure. This matters more than throughput in a recovery tool, and it is what
makes it safe to attempt undecoded variants: a wrong guess fails rather than corrupts.

## 7. Encryption

`enable_data_encrypt` in `_Syno_TaskConfig` indicates client-side encryption. This
document covers **unencrypted archives only**. The encrypted variant is not implemented
here; a 2016 Python 2 script by "mrsandman" and an accompanying synology-forum.de thread
document the key derivation, and remain the only public reference.

## 8. Not decoded

- `Pool/file_pool` whole-file dedup store, and the `off_virtual_file = -1` reference.
- The 12-byte per-file header preceding each chunk array (ACL/xattr related).
- The trailing 4 bytes of a v3 chunk_index record.
- How Synology *chooses* which `file_chunk` shard a file lands in (readers only need to
  read the value, never predict it).
- Encrypted archives.

## 9. How this was derived

Two independent routes, cross-checked against each other:

1. **Empirical.** Anchor on a file whose content could be recognised — a 10,244-byte
   `.DS_Store` that turned out to be exactly two chunks (9232 + 1012) — then generalise and
   confirm by rebuilding progressively larger files until an 674 MB video reproduced
   byte-exactly across 82,313 chunks.
2. **Disassembly.** Synology's macOS `HyperBackupExplorer` ships **full C++ symbols**
   (~24k mangled names, plus original source paths). `nm -a` and `objdump -d` on named
   functions such as `ChunkIndexAdapter::getChunkIndexInfo`,
   `VirtualFileAdapter::getVirtualFileInfo` and `VirtualFile::FileChunkOffsetParse` give
   exact field offsets and endianness. This turns guesswork into reading the vendor's own
   field arithmetic.

Where the two disagreed, the empirical result won and the disassembly note was corrected —
that is how the "SHA-1" error was caught.

No vendor code is reproduced here. Field offsets, record sizes and wire layouts are facts
about a data format, not authorship. The independent Rust implementation
[TeamDman/teamy-hyper-backup-explorer](https://github.com/TeamDman/teamy-hyper-backup-explorer)
(MPL-2.0) was consulted; its constants agree with what we derived separately.
