"""Read a Synology Hyper Backup (.hbk) archive directly. No Synology software required.

Format (reverse engineered; all integers BIG-ENDIAN):
  version_list.off_virtual_file -> virtual_file.index record (56B)
    i64 @0 = (file_chunk_index_id << 48) | byte_offset
  -> Config/file_chunk<id>.index @byte_offset : flat array of BE i64 keys, 8B each
  -> each key = byte offset into Pool/chunk_index (29B records)
       u8 mode @0 ; if mode&1 -> i64 cite_offset @1 (indirect, recurse)
                    else      -> i32 bucket_id @1, i32 bucket_index_offset @5
  -> Pool/<pool>/<bucket_id>>11>/<bucket_id & 0x7FF>.index @bucket_index_offset  (32B record)
       u32 comp_len, u32 offset, u32 uncomp_len, md5[16] of DECOMPRESSED chunk,
       u32 crc32 of record[0:28]
  -> same-named .bucket @offset, comp_len bytes: raw LZ4 BLOCK (zlib is the fallback codec;
     SYNO::Backup::decompress dispatches type 1/2/4 = lz4 / lz4-hc / zlib)

Index families are <N>.idx shards concatenated in numeric order; records start at stream
offset 64; only shard 0 carries the 64-byte header (magic 0x7053A86E).

Synology writes a generation suffix on most files (`0.idx.2`, `1.db.2`, `index_ver.json.1`).
Everything here resolves `name`, `name.1`, `name.2`... transparently, preferring the highest
generation, and ignores macOS AppleDouble junk (`._*`).
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import struct
import zlib
from collections import OrderedDict

class NeedPassword(Exception):
    """The archive is encrypted and no password was supplied."""


class UnsupportedArchive(Exception):
    """The archive uses a layout this reader does not implement. Never guess - a wrong
    guess here means silently wrong bytes, which is the one outcome a recovery tool
    must never produce."""


MAX_OPEN_BUCKETS = int(os.environ.get("HBK_MAX_OPEN", "64"))
BUCKET_BUF = int(os.environ.get("HBK_BUCKET_BUF", 1 << 22))   # read-ahead; platter drives care
INDEX_BUF = int(os.environ.get("HBK_INDEX_BUF", 1 << 16))     # chunk keys are mostly sequential;
                                                             # unbuffered = one syscall per 29 B
MAGIC = 0x7053A86E
SHARD_SIZE = 8 << 20        # every index shard is exactly 8 MiB except the last

_LZ4_CANDIDATES = [
    os.environ.get("HBK_LZ4"),
    "/opt/homebrew/lib/liblz4.dylib",
    "/usr/local/lib/liblz4.dylib",
    "/usr/lib/x86_64-linux-gnu/liblz4.so.1",
    "/usr/lib/aarch64-linux-gnu/liblz4.so.1",
    "liblz4.so.1", "liblz4.so", "liblz4.dylib",
    "liblz4.dll", "lz4.dll",                       # Windows (untested)
]
_lz4 = None


def lz4():
    """Lazily bind liblz4. Note the x86_64 dylib inside HyperBackupExplorer.app will
    not load on Apple Silicon - use a native one (brew install lz4)."""
    global _lz4
    if _lz4 is None:
        for c in _LZ4_CANDIDATES:
            if not c:
                continue
            try:
                lib = ctypes.CDLL(c)
            except OSError:
                continue
            lib.LZ4_decompress_safe.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                                ctypes.c_int, ctypes.c_int]
            lib.LZ4_decompress_safe.restype = ctypes.c_int
            _lz4 = lib
            break
        else:
            raise RuntimeError("liblz4 not found - set HBK_LZ4 to its path (brew install lz4)")
    return _lz4


# ------------------------------------------------------------ path resolution

_SHARD = re.compile(r"^(\d+)\.idx(?:\.(\d+))?$")


def resolve(d: str, base: str) -> str:
    """Find `base` / `base.<gen>` inside directory `d`, preferring the highest generation."""
    best = None
    try:
        names = os.listdir(d)
    except OSError as e:
        raise FileNotFoundError(f"{d}: {e}") from e
    for f in names:
        if f.startswith("._"):
            continue
        if f == base:
            gen = 0
        elif f.startswith(base + "."):
            suf = f[len(base) + 1:]
            if not suf.isdigit():
                continue
            gen = int(suf)
        else:
            continue
        if best is None or gen > best[0]:
            best = (gen, f)
    if best is None:
        raise FileNotFoundError(f"no {base}[.gen] in {d}")
    return os.path.join(d, best[1])


class Cat:
    """Numbered <N>.idx[.gen] shards presented as a single logical byte stream.

    Shards are a fixed SHARD_SIZE except the last, so offsets are computed rather than
    measured. That matters enormously on network mounts: stat-ing every shard cost ~2,600
    round-trips on a 3 TB archive (minutes before reading a byte). We now do one directory
    listing plus a single stat of the final shard. If the assumption were ever wrong, a
    short read surfaces as a chunk MD5 failure - loudly - never as silent corruption.
    """

    def __init__(self, d: str):
        self.d = d
        found: dict[int, tuple[int, str]] = {}
        for f in os.listdir(d):
            if f.startswith("._"):
                continue
            m = _SHARD.match(f)
            if not m:
                continue
            n, gen = int(m.group(1)), int(m.group(2) or 0)
            if n not in found or gen > found[n][0]:
                found[n] = (gen, f)
        if not found:
            raise FileNotFoundError(f"no .idx shards in {d}")
        self.files = [found[k][1] for k in sorted(found)]
        n = len(self.files)
        last = os.path.getsize(os.path.join(d, self.files[-1]))
        self.sizes = [SHARD_SIZE] * (n - 1) + [last]
        self.starts = [i * SHARD_SIZE for i in range(n)]
        self.total = (n - 1) * SHARD_SIZE + last
        self._fh: dict[int, object] = {}
        # Shard 0 carries a 64-byte header that declares the record size, so readers do
        # not have to hardcode per-DSM-version layouts.
        with open(os.path.join(d, self.files[0]), "rb") as fh:
            head = fh.read(64)
        if len(head) < 64:
            raise UnsupportedArchive(f"{d}: shard 0 is shorter than its header")
        w = struct.unpack(">16I", head)
        if w[0] != MAGIC:
            raise UnsupportedArchive(f"{d}: bad magic {w[0]:#010x}, expected {MAGIC:#010x}")
        self.kind, self.variant = w[1], w[2]
        self.record_size = w[4]
        self.declared_total = (w[5] << 32) | w[6]

    def read(self, off: int, n: int) -> bytes:
        """Read n bytes at logical offset off, spanning shards as needed."""
        out = b""
        i = off // SHARD_SIZE
        while n > 0 and i < len(self.files):
            st = self.starts[i]
            take = min(n, st + self.sizes[i] - off)
            if take <= 0:
                break
            fh = self._fh.get(i)
            if fh is None:
                fh = self._fh[i] = open(os.path.join(self.d, self.files[i]), "rb",
                                        buffering=INDEX_BUF)
            fh.seek(off - st)
            got = fh.read(take)
            out += got
            if len(got) < take:            # short shard: stop rather than mis-align
                break
            off += take
            n -= take
            i += 1
        return out

    def close(self):
        for fh in self._fh.values():
            fh.close()
        self._fh.clear()


class _LazyCats(dict):
    """file_chunk<N>.index families, opened only when a file actually references one."""

    def __init__(self, dirs: dict[int, str]):
        super().__init__()
        self._dirs = dirs

    def get(self, k, default=None):
        if k in self:
            return self[k]
        d = self._dirs.get(k)
        if d is None:
            return default
        self[k] = c = Cat(d)
        return c

    def __missing__(self, k):
        c = self.get(k)
        if c is None:
            raise KeyError(k)
        return c

    def values(self):
        return [v for v in dict.values(self)]

    @property
    def known(self) -> list[int]:
        """Every shard id the archive has, opened or not.

        Iterating the dict yields only what something has already touched, so a
        caller that just wants to *describe* the archive - `doctor` - has to ask
        for this instead, or it reports an archive with shards as having none.
        """
        return sorted(self._dirs)


def is_archive(d: str) -> bool:
    return os.path.isdir(os.path.join(d, "Pool")) and os.path.isdir(os.path.join(d, "Config"))


def find_archive_root(path: str) -> str:
    """Accept the .hbk directory itself, a parent containing one, or the .bkpi file."""
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(path):
        path = os.path.dirname(path)
    if is_archive(path):
        return path
    if os.path.isdir(path):
        for e in sorted(os.listdir(path)):
            c = os.path.join(path, e)
            if not e.startswith("._") and os.path.isdir(c) and is_archive(c):
                return c
    raise FileNotFoundError(f"no Hyper Backup archive at {path}")


# --------------------------------------------------------------------- archive

class Archive:
    """One .hbk archive. Cheap to construct; caches open handles and bucket indexes."""

    def __init__(self, root: str, password: str | None = None):
        self.root = find_archive_root(root)
        self.crypto = None
        c, p = f"{self.root}/Config", f"{self.root}/Pool"
        self.vf = Cat(f"{c}/virtual_file.index")
        self.ci = Cat(f"{p}/chunk_index")
        self._fc_dirs: dict[int, str] = {}
        for e in os.listdir(c):
            m = re.match(r"^file_chunk(\d+)\.index$", e)
            if m and os.path.isdir(os.path.join(c, e)):
                self._fc_dirs[int(m.group(1))] = os.path.join(c, e)
        if not self._fc_dirs:
            raise FileNotFoundError(f"no file_chunk<N>.index directories in {c}")
        self.fc: dict[int, Cat] = _LazyCats(self._fc_dirs)
        pools = [e for e in os.listdir(p) if e.isdigit() and os.path.isdir(os.path.join(p, e))]
        if not pools:
            raise FileNotFoundError(f"no numbered pool directory under {p}")
        self.pools = sorted(pools, key=int)
        self.pool = os.path.join(p, self.pools[0])
        self._bidx: OrderedDict[int, bytes] = OrderedDict()
        self._bdat: OrderedDict[int, object] = OrderedDict()
        self._brec: dict[int, int] = {}
        self._password = None
        if self.is_encrypted():
            if password is None:
                raise NeedPassword(f"{self.root} is encrypted - a password is required")
            self.crypto = self._open_crypto(password)
            self._password = password        # kept in memory so workers can be given it

        if self.vf.record_size != 56:
            raise UnsupportedArchive(
                f"virtual_file record size {self.vf.record_size}, only 56 is implemented")
        if self.ci.record_size not in (16, 29):
            raise UnsupportedArchive(
                f"chunk_index record size {self.ci.record_size}, "
                "only 16 (v1/v2) and 29 (v3) are implemented")

    # -- metadata ---------------------------------------------------------

    def task_config(self) -> dict:
        out: dict[str, str] = {}
        try:
            p = resolve(self.root, "_Syno_TaskConfig")
        except FileNotFoundError:
            return out
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "=" in line and not line.startswith("["):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"')
        return out

    def _open_crypto(self, password: str):
        """Unwrap the version key using the password. Raises WrongPassword on mismatch."""
        import sqlite3

        from .crypto import ArchiveCrypto

        cfg = self.task_config()
        unikey = cfg.get("unikey")
        if not unikey:
            raise UnsupportedArchive("encrypted archive has no unikey in _Syno_TaskConfig")
        with open(resolve(f"{self.root}/Config", "public.pem"), "rb") as fh:
            pub = fh.read()
        db = sqlite3.connect(f"file:{resolve(f'{self.root}/Pool', 'vkey.db')}?immutable=1", uri=True)
        row = db.execute("SELECT rsa_vkey, rsa_vkey_iv FROM vkey ORDER BY version_id").fetchone()
        db.close()
        if not row:
            raise UnsupportedArchive("encrypted archive has no rows in vkey db")
        return ArchiveCrypto(password, unikey, pub, bytes(row[0]), bytes(row[1]))

    def decrypt_name(self, stored: str) -> str:
        """Decrypt a stored filename, or return it unchanged for plaintext archives."""
        return self.crypto.decrypt_name(stored) if self.crypto else stored

    def is_encrypted(self) -> bool:
        return self.task_config().get("enable_data_encrypt", "false").lower() == "true"

    def shares(self) -> list[str]:
        d = f"{self.root}/Config/@Share"
        if not os.path.isdir(d):
            return []
        return sorted(e for e in os.listdir(d)
                      if not e.startswith("._") and os.path.isdir(os.path.join(d, e)))

    def share_versions(self, share: str) -> list[int]:
        d = f"{self.root}/Config/@Share/{share}"
        vs = set()
        for f in os.listdir(d):
            if f.startswith("._") or f.startswith("complete_list"):
                continue
            m = re.match(r"^(\d+)\.db(?:\.\d+)?$", f)
            if m:
                vs.add(int(m.group(1)))
        return sorted(vs)

    def share_db(self, share: str, version: int | None = None) -> str:
        d = f"{self.root}/Config/@Share/{share}"
        if version is None:
            vs = self.share_versions(share)
            if not vs:
                raise FileNotFoundError(f"no version db in {d}")
            version = max(vs)
        return resolve(d, f"{version}.db")

    # -- chunk plumbing ---------------------------------------------------

    def _bucket(self, bid: int):
        idx = self._bidx.get(bid)
        if idx is None:
            sub = os.path.join(self.pool, str(bid >> 11))
            f = bid & 0x7FF
            with open(resolve(sub, f"{f}.index"), "rb") as fh:
                idx = fh.read()
            w = struct.unpack(">16I", idx[:64])
            if w[0] != MAGIC:
                raise UnsupportedArchive(f"bucket {bid}: bad index magic {w[0]:#010x}")
            if w[4] not in (28, 32):
                raise UnsupportedArchive(
                    f"bucket {bid}: index record size {w[4]}, only 28 and 32 are implemented")
            self._brec[bid] = w[4]
            self._bidx[bid] = idx
            self._bdat[bid] = open(resolve(sub, f"{f}.bucket"), "rb", buffering=BUCKET_BUF)
            while len(self._bidx) > MAX_OPEN_BUCKETS:
                k, _ = self._bidx.popitem(last=False)
                self._bdat.pop(k).close()
        else:
            self._bidx.move_to_end(bid)
            self._bdat.move_to_end(bid)
        return idx, self._bdat[bid]

    def chunk_ref(self, key: int, depth: int = 0):
        """Resolve a chunk_index key to (bucket_id, bucket_index_offset)."""
        if depth > 16:
            raise ValueError("chunk_index cite loop")
        n = self.ci.record_size
        r = self.ci.read(key, n)
        if len(r) < min(n, 9):
            raise ValueError(f"short chunk_index record at {key}")
        if n == 16:                       # v1/v2: bucket_id and offset sit at the front
            return struct.unpack(">ii", r[0:8])
        if r[0] & 1:                      # v3 bit0 = intra-cite indirection (dedup)
            return self.chunk_ref(struct.unpack(">q", r[1:9])[0], depth + 1)
        return struct.unpack(">ii", r[1:9])

    def read_chunk(self, bid: int, boff: int, verify: bool = True) -> bytes:
        idx, fh = self._bucket(bid)
        n = self._brec[bid]
        rec = idx[boff:boff + n]
        if len(rec) < n:
            raise ValueError(f"bucket {bid} index truncated at {boff}")
        clen, off, ulen = struct.unpack(">III", rec[:12])
        # 32-byte records carry a trailing CRC32 over the record; 28-byte legacy ones do not
        if verify and n == 32 and (zlib.crc32(rec[:28]) & 0xFFFFFFFF) != struct.unpack(">I", rec[28:32])[0]:
            raise ValueError(f"bucket index CRC32 mismatch b{bid}@{boff}")
        fh.seek(off)
        raw = fh.read(clen)
        if self.crypto is not None:
            raw = self.crypto.decrypt_chunk(raw)
            clen = len(raw)
        dst = ctypes.create_string_buffer(ulen)
        n = lz4().LZ4_decompress_safe(raw, dst, clen, ulen)
        data = dst.raw[:n] if n > 0 else b""
        if n != ulen:
            try:
                data = zlib.decompress(raw)                 # compress type 4
            except Exception:
                raise ValueError(f"decompress failed b{bid}@{boff} lz4={n} want={ulen}") from None
        if verify and hashlib.md5(data).digest() != rec[12:28]:
            raise ValueError(f"MD5 mismatch b{bid}@{boff}")
        return data

    def extract(self, ovf: int, size: int, verify: bool = True, out=None, on_bytes=None):
        """Rebuild one file. Writes to `out` if given (returns byte count), else returns bytes.

        `on_bytes(n)` is called periodically with bytes produced since the last call, so a
        caller can report progress *within* a large file rather than only when it finishes.
        """
        if not size:
            return 0 if out is not None else b""
        if ovf is None or ovf < 0:
            # sentinel: the whole file is deduped into Pool/file_pool rather than chunked.
            # Exactly 1 of 501,278 files in the reference archive. Not decoded.
            raise ValueError("whole-file dedup entry (Pool/file_pool) - not supported")
        v = struct.unpack(">q", self.vf.read(ovf, 8))[0]
        fc = self.fc.get(v >> 48)
        if fc is None:
            raise ValueError(f"no file_chunk{v >> 48}.index in archive")
        off = v & 0xFFFFFFFFFFFF
        parts, got, i, BATCH = [], 0, 0, 4096
        pending = 0
        while got < size:
            blob = fc.read(off + i * 8, 8 * BATCH)
            if not blob:
                raise ValueError(f"chunk list exhausted at {got}/{size} bytes")
            for j in range(0, len(blob) - 7, 8):
                if got >= size:
                    break
                bid, boff = self.chunk_ref(struct.unpack(">q", blob[j:j + 8])[0])
                d = self.read_chunk(bid, boff, verify=verify)
                got += len(d)
                if out is not None:
                    out.write(d)
                else:
                    parts.append(d)
                i += 1
                if on_bytes is not None:
                    pending += len(d)
                    if pending >= 1 << 22:          # report about every 4 MB
                        on_bytes(pending)
                        pending = 0
        if on_bytes is not None and pending:
            on_bytes(pending)
        if got != size:
            raise ValueError(f"size mismatch: rebuilt {got}, expected {size}")
        return got if out is not None else b"".join(parts)

    def close(self):
        self.vf.close()
        self.ci.close()
        for c in self.fc.values():
            c.close()
        for fh in self._bdat.values():
            fh.close()
        self._bdat.clear()
        self._bidx.clear()


# ------------------------------------------------- module-level convenience API
# One cached Archive per root, so worker processes build it once and reuse it.

_cache: dict[str, Archive] = {}


def open_archive(root: str | None = None, password: str | None = None) -> Archive:
    root = root or os.environ.get("HBK_ROOT") or ""
    if password is None:
        password = os.environ.get("HBK_PASSWORD") or None
    key = os.path.abspath(os.path.expanduser(root))
    a = _cache.get(key)
    if a is None:
        a = _cache[key] = Archive(root, password=password)
    return a


def extract(ovf: int, size: int, verify: bool = True, out=None,
            root: str | None = None, password: str | None = None):
    return open_archive(root, password).extract(ovf, size, verify=verify, out=out)
