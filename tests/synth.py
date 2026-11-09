"""Build small but complete multi-version Hyper Backup archives on disk, for tests.

The layout follows FORMAT.md: one bucket per backup version, a chunk_index of 29-byte
v3 records, one file_chunk family, 56-byte virtual_file records, and per-share
`<version>.db` file trees. A chunk whose bytes an earlier version already stored is not
written again: the newer version's chunk list points back at the old record, so a file
can be rebuilt from chunks written by several versions - and, on an encrypted archive,
under several versions' data keys.

`version_info.db` uses the exact schema seen on a real Hyper Backup 4.2.2 archive.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import sqlite3
import struct
import zlib
from dataclasses import dataclass, field

from hbkit import archive as hbk

VERSION_INFO_SQL = (
    "CREATE TABLE version_info (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, "
    "name TEXT, source TEXT, share TEXT, status TEXT, statistics TEXT, diff_size INTEGER, "
    "locked INTEGER NOT NULL default 0, share_info BLOB, missing_src TEXT, tag_db_magic BLOB, "
    "tag_db_file_size_thr INTEGER, has_suspend_dup INTEGER DEFAULT 0, bad_share TEXT DEFAULT '', "
    "internal_share TEXT DEFAULT '',suspend_history TEXT DEFAULT '', enc_cksum BLOB DEFAULT NULL, "
    "depose_time INTEGER DEFAULT -1)")

PASSWORD = "synthetic-pw"
UNIKEY = "0011223344AA_2_1700000000"


@dataclass
class V:
    """One backup version: {path below the share: [chunk bytes, ...]}."""
    files: dict[str, list[bytes]]
    timestamp: int
    status: str = "Complete"
    shares: list[str] = field(default_factory=lambda: ["db_staging"])


def _hdr(kind: int, recsize: int, total: int) -> bytes:
    return struct.pack(">16I", hbk.MAGIC, kind, 0, 0, recsize, total >> 32,
                       total & 0xFFFFFFFF, *([0] * 9))


def _lz4(data: bytes) -> bytes:
    lib = hbk.lz4()
    lib.LZ4_compress_default.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                         ctypes.c_int, ctypes.c_int]
    dst = ctypes.create_string_buffer(len(data) + 64 + len(data) // 255)
    n = lib.LZ4_compress_default(data, dst, len(data), len(dst))
    assert n > 0
    return dst.raw[:n]


class _Crypto:
    def __init__(self, n_versions: int):
        import nacl.bindings as nb

        from hbkit.crypto import ARGON2_ALG, ARGON2_MEMLIMIT, ARGON2_OPSLIMIT, FILENAME_IV_SALT
        seed = nb.crypto_pwhash_alg(32, PASSWORD.encode(), hashlib.md5(UNIKEY.encode()).digest(),
                                    ARGON2_OPSLIMIT, ARGON2_MEMLIMIT, ARGON2_ALG)
        self.pub, priv = nb.crypto_box_seed_keypair(seed)
        self.keys = [(os.urandom(32), os.urandom(16)) for _ in range(n_versions)]
        self.sealed = [(nb.crypto_box_seal(k, self.pub), nb.crypto_box_seal(iv, self.pub))
                       for k, iv in self.keys]
        self.fn_key = hashlib.sha256(priv + UNIKEY.encode()).digest()
        self.fn_iv = hashlib.md5(UNIKEY.encode() + FILENAME_IV_SALT).digest()

    @staticmethod
    def _aes(data: bytes, key: bytes, iv: bytes) -> bytes:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        p = padding.PKCS7(128).padder()
        e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return e.update(p.update(data) + p.finalize()) + e.finalize()

    def chunk(self, data: bytes, version_index: int) -> bytes:
        return self._aes(data, *self.keys[version_index])

    def name(self, plain: str) -> str:
        ct = self._aes(plain.encode(), self.fn_key, self.fn_iv)
        return base64.b64encode(ct).decode().replace("/", "_")


def build(root: str, versions: list[V], encrypted: bool = False,
          with_version_info: bool = True) -> str:
    """Write `<root>/synth.hbk` holding `versions` (version ids 1..n). Returns its path."""
    arc = os.path.join(root, "synth.hbk")
    cfg, pool = f"{arc}/Config", f"{arc}/Pool"
    for d in (f"{cfg}/virtual_file.index", f"{cfg}/file_chunk1.index",
              f"{pool}/chunk_index", f"{pool}/0/0"):
        os.makedirs(d)
    crypto = _Crypto(len(versions)) if encrypted else None

    chunks: dict[bytes, int] = {}          # chunk bytes -> chunk_index key
    ci = bytearray()                       # chunk_index records, after the header
    fc = bytearray()                       # file_chunk stream, after the header
    vf = bytearray()                       # virtual_file records, after the header
    trees: dict[tuple[str, int], list[tuple]] = {}

    for vi, ver in enumerate(versions):
        bid = vi                           # one bucket per version, as on real archives
        bidx, bdat = bytearray(), bytearray()
        for share in ver.shares:
            rows, dirs = [], {}
            for rel, parts in sorted(ver.files.items()):
                keys = []
                for c in parts:
                    if c not in chunks:
                        comp = _lz4(c)
                        if crypto:
                            comp = crypto.chunk(comp, vi)
                        rec = struct.pack(">III", len(comp), len(bdat), len(c)) + \
                            hashlib.md5(c).digest()
                        boff = 64 + len(bidx)
                        bidx += rec + struct.pack(">I", zlib.crc32(rec) & 0xFFFFFFFF)
                        bdat += comp
                        chunks[c] = 64 + len(ci)
                        ci += b"\x00" + struct.pack(">ii", bid, boff) + bytes(20)
                    keys.append(chunks[c])
                fc += bytes(12)                                  # per-file header
                ptr = (1 << 48) | (64 + len(fc))
                fc += b"".join(struct.pack(">q", k) for k in keys)
                ovf = 64 + len(vf)
                vf += struct.pack(">q", ptr) + bytes(48)

                comps = rel.split("/")
                for depth in range(1, len(comps)):
                    dpath = "/".join(comps[:depth])
                    if dpath not in dirs:
                        dirs[dpath] = hashlib.sha1(f"{share}/{dpath}".encode()).digest()
                        parent = dirs.get("/".join(comps[:depth - 1]), dirs[dpath])
                        rows.append((dirs[dpath], parent, comps[depth - 1], 0, 0o40755,
                                     ver.timestamp, None))
                nid = hashlib.sha1(f"{share}/{rel}".encode()).digest()
                rows.append((nid, dirs["/".join(comps[:-1])], comps[-1],
                             sum(map(len, parts)), 0o100644, ver.timestamp, ovf))
            trees[(share, vi + 1)] = rows
        with open(f"{pool}/0/0/{bid}.index", "wb") as fh:
            fh.write(_hdr(2, 32, 64 + len(bidx)) + bidx)
        with open(f"{pool}/0/0/{bid}.bucket", "wb") as fh:
            fh.write(bdat)

    for path, kind, rs, body in ((f"{cfg}/virtual_file.index/0.idx", 0, 56, vf),
                                 (f"{cfg}/file_chunk1.index/0.idx", 1, 0, fc),
                                 (f"{pool}/chunk_index/0.idx", 1, 29, ci)):
        with open(path, "wb") as fh:
            fh.write(_hdr(kind, rs, 64 + len(body)) + body)

    for (share, vid), rows in trees.items():
        d = f"{cfg}/@Share/{share}"
        os.makedirs(d, exist_ok=True)
        db = sqlite3.connect(f"{d}/{vid}.db")
        db.execute("CREATE TABLE version_list (name_id_v2 BLOB PRIMARY KEY, pname_id_v2 BLOB, "
                   "off_virtual_file INTEGER, file_name TEXT, size INTEGER, mode INTEGER, "
                   "mtime_sec INTEGER, ctime_sec INTEGER, inode INTEGER)")
        for nid, pid, name, size, mode, mt, ovf in rows:
            db.execute("INSERT INTO version_list VALUES (?,?,?,?,?,?,?,?,?)",
                       (nid, pid, ovf, crypto.name(name) if crypto else name,
                        size, mode, mt, mt, 0))
        db.commit()
        db.close()
        cl = sqlite3.connect(f"{d}/complete_list.db")
        cl.execute("CREATE TABLE IF NOT EXISTS complete_list (version_id INTEGER PRIMARY KEY)")
        if versions[vid - 1].status == "Complete":
            cl.execute("INSERT INTO complete_list VALUES (?)", (vid,))
        cl.commit()
        cl.close()

    if with_version_info:
        db = sqlite3.connect(f"{cfg}/version_info.db")
        db.execute(VERSION_INFO_SQL)
        for vid, ver in enumerate(versions, 1):
            db.execute("INSERT INTO version_info (id, timestamp, name, share, status) "
                       "VALUES (?,?,?,?,?)",
                       (vid, ver.timestamp, "my_ver", "".join(s + "," for s in ver.shares),
                        ver.status))
        db.commit()
        db.close()

    conf = ['[task_config]', 'name="synth"', 'host_name="synth-host"',
            'data_compress_type=1', f'enable_data_encrypt={"true" if crypto else "false"}']
    if crypto:
        conf.append(f'unikey="{UNIKEY}"')
        with open(f"{cfg}/public.pem", "wb") as fh:
            fh.write(crypto.pub)
        db = sqlite3.connect(f"{pool}/vkey.db")
        db.execute("CREATE TABLE vkey (version_id INTEGER PRIMARY KEY, rsa_vkey BLOB NOT NULL, "
                   "rsa_vkey_iv BLOB, checksum BLOB, ref_count INTEGER NOT NULL default 0)")
        for vid, (k, iv) in enumerate(crypto.sealed, 1):
            db.execute("INSERT INTO vkey VALUES (?,?,?,?,1)", (vid, k, iv, b""))
        db.commit()
        db.close()
    with open(f"{arc}/_Syno_TaskConfig", "w") as fh:
        fh.write("\n".join(conf) + "\n")
    return arc
