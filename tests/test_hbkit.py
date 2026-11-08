"""Tests for hbkit.

Tests that need a real archive are skipped unless HBK_TEST_ARCHIVE points at one, so CI
still exercises everything that does not require backup data. Correctness of extraction is
checked against the archive's OWN MD5/CRC32 and against file-format markers, so a pass
means the bytes are genuinely right rather than merely the right length.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

import hbkit
from hbkit import archive as hbk

ARCHIVE = os.environ.get("HBK_TEST_ARCHIVE", "")
if not ARCHIVE:
    _skip = "HBK_TEST_ARCHIVE is not set - point it at a real .hbk archive"
elif not os.path.exists(ARCHIVE):
    _skip = f"HBK_TEST_ARCHIVE={ARCHIVE!r} does not exist (drive not mounted?)"
else:
    _skip = ""
needs_archive = pytest.mark.skipif(bool(_skip), reason=_skip or "ok")


@pytest.fixture(scope="module")
def arc():
    a = hbk.Archive(ARCHIVE)
    yield a
    a.close()


# ---------------------------------------------------------------- no archive needed

def test_version():
    assert hbkit.__version__


def test_modules_import():
    from hbkit import cli, doctor, index, runner, tui  # noqa: F401


def test_cli_runs_without_args():
    r = subprocess.run([sys.executable, "-m", "hbkit.cli"], capture_output=True, text=True)
    assert "Synology Hyper Backup" in r.stdout


def test_missing_archive_is_a_clean_error():
    with pytest.raises(FileNotFoundError):
        hbk.Archive("/definitely/not/an/archive")


def test_doctor_reports_blocker_for_missing_archive():
    from hbkit import doctor
    r = doctor.diagnose("/definitely/not/an/archive")
    assert r.blockers and not r.ok
    assert "cannot be recovered" in doctor.render(r)


def test_unsupported_archive_is_its_own_exception():
    assert issubclass(hbk.UnsupportedArchive, Exception)


def test_resolve_prefers_highest_generation(tmp_path):
    for name in ("thing", "thing.1", "thing.5", "thing.2", "._thing.9"):
        (tmp_path / name).write_text("x")
    assert os.path.basename(hbk.resolve(str(tmp_path), "thing")) == "thing.5"


def test_resolve_ignores_appledouble(tmp_path):
    (tmp_path / "._thing.9").write_text("x")
    (tmp_path / "thing").write_text("x")
    assert os.path.basename(hbk.resolve(str(tmp_path), "thing")) == "thing"


def test_zero_byte_file_extracts_to_nothing():
    a = hbk.Archive.__new__(hbk.Archive)          # no archive needed for the early return
    assert a.extract(0, 0) == b""


# ------------------------------------------------------------------- needs archive

@needs_archive
def test_archive_discovery_and_metadata(arc):
    assert arc.root.endswith(".hbk")
    assert arc.task_config().get("name")
    assert arc.shares()
    assert os.path.exists(arc.share_db(arc.shares()[0]))


@needs_archive
def test_layouts_are_known(arc):
    assert arc.vf.record_size == 56
    assert arc.ci.record_size in (16, 29)


@needs_archive
def test_bucket_chunks_verify(arc):
    """Every chunk must satisfy the archive's own MD5 and the record's CRC32."""
    import hashlib
    import struct
    import zlib
    idx, _ = arc._bucket(0)
    n = arc._brec[0]
    for i in range(8):
        rec = idx[64 + n * i: 64 + n * (i + 1)]
        clen, off, ulen = struct.unpack(">III", rec[:12])
        if n == 32:
            assert (zlib.crc32(rec[:28]) & 0xFFFFFFFF) == struct.unpack(">I", rec[28:32])[0]
        data = arc.read_chunk(0, 64 + n * i)
        assert len(data) == ulen
        assert hashlib.md5(data).digest() == rec[12:28]


@needs_archive
def test_index_builds_and_reconciles(arc):
    import sqlite3
    from hbkit import index as hbk_index
    path = hbk_index.open_or_build(arc)
    assert hbk_index.is_current(arc)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    roots = db.execute("SELECT id,size FROM node WHERE parent IS NULL").fetchall()
    assert roots
    for rid, size in roots:                      # directory sizes are subtree totals
        kid = db.execute("SELECT SUM(size) FROM node WHERE parent=?", (rid,)).fetchone()[0]
        if kid is not None:
            assert kid == size
    assert db.execute("SELECT COUNT(*) FROM node WHERE name='@eaDir'").fetchone()[0] == 0


@needs_archive
def test_extract_is_byte_exact_and_resumable(arc):
    import sqlite3
    from hbkit import index as hbk_index
    from hbkit import runner as hbk_run
    db = sqlite3.connect(f"file:{hbk_index.open_or_build(arc)}?mode=ro", uri=True)
    rows = db.execute("""
        WITH RECURSIVE t(id,path,isdir) AS (
            SELECT id,'/'||name,isdir FROM node WHERE parent IS NULL
            UNION ALL SELECT n.id,t.path||'/'||n.name,n.isdir FROM node n JOIN t ON n.parent=t.id)
        SELECT t.path,n.ovf,n.size,n.mtime FROM t JOIN node n ON n.id=t.id
        WHERE t.isdir=0 AND n.ovf>=0 AND lower(t.path) LIKE '%.jpg'
          AND n.size BETWEEN 400000 AND 4000000 LIMIT 8""").fetchall()
    if not rows:
        pytest.skip("no suitable sample files in this archive")

    data = arc.extract(rows[0][1], rows[0][2])
    assert len(data) == rows[0][2]
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9", "not a complete JPEG"

    tmp = tempfile.mkdtemp(prefix="hbkit-test-")
    try:
        def drain(r):
            r.start()
            while not r.finished:
                r.poll()
                time.sleep(0.05)
            r.poll()
            r.cleanup()

        r = hbk_run.Runner(arc.root, tmp, rows, n_workers=4)
        drain(r)
        assert r.failed == 0, r.errors[:3]
        assert r.ok == len(rows)
        written = [os.path.join(dp, f) for dp, _, fs in os.walk(tmp) for f in fs]
        assert written and not any(p.endswith(".part") for p in written)

        r2 = hbk_run.Runner(arc.root, tmp, rows, n_workers=4)
        drain(r2)
        assert r2.skipped == len(rows), "resume did not skip completed files"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_archive
def test_doctor_passes_on_a_real_archive():
    from hbkit import doctor
    r = doctor.diagnose(ARCHIVE, sample=3)
    assert not r.blockers, r.blockers
    assert r.ok, [c for c in r.checks if not c[1]]


# ------------------------------------------------------- encrypted archive tests

ENC = os.environ.get("HBK_TEST_ENC_ARCHIVE", "")
ENC_PW = os.environ.get("HBK_TEST_ENC_PASSWORD", "")
needs_enc = pytest.mark.skipif(
    not (ENC and os.path.exists(ENC) and ENC_PW),
    reason="set HBK_TEST_ENC_ARCHIVE and HBK_TEST_ENC_PASSWORD")


@needs_enc
def test_encrypted_requires_password():
    with pytest.raises(hbk.NeedPassword):
        hbk.Archive(ENC)


@needs_enc
def test_wrong_password_is_rejected_before_reading_data():
    from hbkit.crypto import WrongPassword
    with pytest.raises(WrongPassword):
        hbk.Archive(ENC, password=ENC_PW + "x")


@needs_enc
def test_encrypted_names_and_content():
    """Names and bytes must both come back right, checked against chunk MD5s."""
    import sqlite3
    from hbkit import index as hbk_index
    arc = hbk.Archive(ENC, password=ENC_PW)
    assert arc.crypto is not None
    db = sqlite3.connect(f"file:{hbk_index.open_or_build(arc)}?mode=ro", uri=True)
    rows = db.execute("""
        WITH RECURSIVE t(id,path,isdir) AS (
            SELECT id,'/'||name,isdir FROM node WHERE parent IS NULL
            UNION ALL SELECT n.id,t.path||'/'||n.name,n.isdir FROM node n JOIN t ON n.parent=t.id)
        SELECT t.path,n.ovf,n.size FROM t JOIN node n ON n.id=t.id
        WHERE t.isdir=0 AND n.ovf>=0 AND n.size>0 LIMIT 20""").fetchall()
    assert rows, "no files in encrypted archive index"
    # decrypted names must be printable text, not base64 ciphertext
    assert all("<undecryptable" not in p for p, _, _ in rows)
    for path, ovf, size in rows[:5]:
        data = arc.extract(ovf, size, verify=True)   # verify=True checks every chunk MD5
        assert len(data) == size, path
    arc.close()


# ------------------------------------- one data key per backup version (no archive needed)

def _multi_key_archive():
    """An Archive holding just enough state for read_chunk: bucket N contains one chunk
    encrypted with backup version N+1's data key, the way vkey.db lays them out."""
    import ctypes
    import hashlib
    import io
    import struct
    import zlib

    nb = pytest.importorskip("nacl.bindings")
    try:
        lib = hbk.lz4()
    except RuntimeError:
        pytest.skip("liblz4 not available")
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from hbkit.crypto import ARGON2_ALG, ARGON2_MEMLIMIT, ARGON2_OPSLIMIT, ArchiveCrypto

    pw, unikey = "pw", "unikey"
    seed = nb.crypto_pwhash_alg(32, pw.encode(), hashlib.md5(unikey.encode()).digest(),
                                ARGON2_OPSLIMIT, ARGON2_MEMLIMIT, ARGON2_ALG)
    pub, _ = nb.crypto_box_seed_keypair(seed)
    keys = [(os.urandom(32), os.urandom(16)) for _ in range(3)]

    arc = object.__new__(hbk.Archive)
    arc.crypto = ArchiveCrypto(pw, unikey, pub, [(nb.crypto_box_seal(k, pub),
                                                  nb.crypto_box_seal(iv, pub)) for k, iv in keys])
    arc._brec, arc._bkey, buckets, plain = {}, {}, {}, []
    lib.LZ4_compress_default.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                         ctypes.c_int, ctypes.c_int]
    for bid, (key, iv) in enumerate([*keys, (os.urandom(32), os.urandom(16))]):
        data = f"chunk written by version {bid + 1}. ".encode() * 40
        dst = ctypes.create_string_buffer(len(data) + 64)
        n = lib.LZ4_compress_default(data, dst, len(data), len(dst))
        lz = dst.raw[:n]
        pad = padding.PKCS7(128).padder()
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        ct = enc.update(pad.update(lz) + pad.finalize()) + enc.finalize()
        rec = struct.pack(">III", len(ct), 0, len(data)) + hashlib.md5(data).digest()
        buckets[bid] = (rec + struct.pack(">I", zlib.crc32(rec)), io.BytesIO(ct))
        arc._brec[bid] = 32
        plain.append(data)
    arc._bucket = buckets.__getitem__
    return arc, plain


def test_chunks_decode_under_the_key_of_the_version_that_wrote_them():
    """Issue #4: vkey.db has a row per backup version, each a different AES key. Only the
    first was ever loaded, so every chunk a later version wrote failed to decompress."""
    arc, plain = _multi_key_archive()
    for bid in range(3):
        assert arc.read_chunk(bid, 0, verify=False) == plain[bid]
    assert arc._bkey == {0: 0, 1: 1, 2: 2}


def test_a_chunk_no_key_decodes_is_an_error_even_without_verify():
    arc, _ = _multi_key_archive()
    with pytest.raises(ValueError, match="no data key decodes b3@0"):
        arc.read_chunk(3, 0, verify=False)


def test_verify_still_rejects_options_it_does_not_take():
    """`verify '*' --verison 3` must fail loudly, not verify the default version."""
    r = subprocess.run([sys.executable, "-m", "hbkit.cli", "/no/such/archive",
                        "verify", "*", "--verison", "3"], capture_output=True, text=True)
    assert r.returncode == 2
    assert "unknown option --verison" in r.stderr


def test_encrypted_archive_with_null_stdin_fails_instead_of_prompting(tmp_path):
    """On Windows `< NUL` is a character device, so isatty() says True and getpass used
    to wait on the console forever. Only enough of an archive to reach NeedPassword."""
    import struct
    header = struct.pack(">16I", hbk.MAGIC, 0, 0, 0, 56, *([0] * 11))
    root = tmp_path / "enc.hbk"
    for d in ("Config/virtual_file.index", "Pool/chunk_index",
              "Config/file_chunk1.index", "Pool/0"):
        (root / d).mkdir(parents=True)
    (root / "Config/virtual_file.index/0.idx").write_bytes(header)
    (root / "Pool/chunk_index/0.idx").write_bytes(header)
    (root / "_Syno_TaskConfig").write_text('enable_data_encrypt="true"\n')
    env = {k: v for k, v in os.environ.items() if k != "HBK_PASSWORD"}
    r = subprocess.run([sys.executable, "-m", "hbkit.cli", str(root), "list"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       env=env, timeout=30, check=False)
    assert r.returncode != 0, r.stderr
    assert "pass --password or set HBK_PASSWORD" in r.stderr


# ------------------------------------------------- shard maths (no archive needed)

def _make_shards(tmp_path, sizes, shard_size, monkeypatch):
    """Write synthetic <N>.idx.2 shards. Shard 0 carries a valid 64-byte header, as real
    archives do; the header is part of the logical stream so offsets stay comparable."""
    import struct

    from hbkit import archive as A
    monkeypatch.setattr(A, "SHARD_SIZE", shard_size)
    total = sum(sizes)
    header = struct.pack(">16I", A.MAGIC, 0, 0, 0, 32, total >> 32, total & 0xFFFFFFFF,
                         *([0] * 9))
    blob = b""
    for i, n in enumerate(sizes):
        if i == 0:
            part = header + bytes((64 + j) % 251 for j in range(n - 64))
        else:
            part = bytes((len(blob) + j) % 251 for j in range(n))
        (tmp_path / f"{i}.idx.2").write_bytes(part)
        blob += part
    return blob


def test_cat_computes_offsets_without_stating_every_shard(tmp_path, monkeypatch):
    """Sizes/offsets must be derived from the fixed shard size + one final stat."""
    from hbkit import archive as A
    blob = _make_shards(tmp_path, [128, 128, 10], 128, monkeypatch)
    c = A.Cat(str(tmp_path))
    assert len(c.files) == 3
    assert c.total == len(blob) == 266
    assert c.starts == [0, 128, 256]
    assert c.sizes == [128, 128, 10]
    assert c.record_size == 32          # read from the header, not guessed


def test_cat_reads_across_shard_boundaries(tmp_path, monkeypatch):
    """The multi-shard read path: every offset/length combination must match one blob."""
    from hbkit import archive as A
    blob = _make_shards(tmp_path, [128, 128, 10], 128, monkeypatch)
    c = A.Cat(str(tmp_path))
    for off in range(0, len(blob), 7):
        for n in (1, 5, 127, 128, 129, 260):
            assert c.read(off, n) == blob[off:off + n], f"off={off} n={n}"
    assert c.read(0, len(blob)) == blob          # whole stream in one read
    assert c.read(len(blob), 10) == b""          # past the end
    c.close()


def test_cat_single_shard_still_works(tmp_path, monkeypatch):
    from hbkit import archive as A
    blob = _make_shards(tmp_path, [100], 128, monkeypatch)
    c = A.Cat(str(tmp_path))
    assert c.total == 100 and c.read(70, 20) == blob[70:90]
    c.close()


def test_cat_preserves_sparse_shard_offsets(tmp_path, monkeypatch):
    from hbkit import archive as A
    blob = _make_shards(tmp_path, [128, 10], 128, monkeypatch)
    (tmp_path / "1.idx.2").rename(tmp_path / "2.idx.2")
    c = A.Cat(str(tmp_path))
    assert c.starts == [0, 256]
    assert c.total == 266
    assert c.read(128, 1) == b""
    assert c.read(256, 10) == blob[128:]
    c.close()


# --------------------------------------------------- rclone mount helper (no network)

def test_mount_profiles_pick_the_right_cache_mode(monkeypatch):
    from hbkit import mount as m
    monkeypatch.setattr(m, "require_rclone", lambda: "/usr/bin/rclone")
    browse = m.build_command("r2:b", "/tmp/mp", "browse")
    restore = m.build_command("r2:b", "/tmp/mp", "restore")
    assert "off" == browse[browse.index("--vfs-cache-mode") + 1]
    assert "full" == restore[restore.index("--vfs-cache-mode") + 1]
    # bulk restores cache whole ~50MB buckets, so the cache MUST be capped
    assert "--vfs-cache-max-size" in restore
    assert "--vfs-cache-max-size" not in browse


def test_mount_command_is_always_read_only_and_skips_modtimes(monkeypatch):
    """--no-modtime avoids a HEAD per object: 2021-entry listing 546.8s -> 4.07s."""
    from hbkit import mount as m
    monkeypatch.setattr(m, "require_rclone", lambda: "/usr/bin/rclone")
    for profile in ("browse", "restore"):
        cmd = m.build_command("r2:b", "/tmp/mp", profile)
        assert "--read-only" in cmd, profile
        assert "--no-modtime" in cmd, profile
        assert "--dir-cache-time" in cmd, profile


def test_mount_rejects_unknown_profile(monkeypatch):
    from hbkit import mount as m
    monkeypatch.setattr(m, "require_rclone", lambda: "/usr/bin/rclone")
    with pytest.raises(ValueError):
        m.build_command("r2:b", "/tmp/mp", "nonsense")


def test_mount_cache_size_override(monkeypatch):
    from hbkit import mount as m
    monkeypatch.setattr(m, "require_rclone", lambda: "/usr/bin/rclone")
    cmd = m.build_command("r2:b", "/tmp/mp", "restore", cache_size="10G")
    assert cmd[cmd.index("--vfs-cache-max-size") + 1] == "10G"


def test_missing_rclone_is_a_clear_error(monkeypatch):
    from hbkit import mount as m
    monkeypatch.setattr(m.shutil, "which", lambda _: None)
    with pytest.raises(m.RcloneMissing) as e:
        m.require_rclone()
    assert "rclone config" in str(e.value)


def test_warm_ignores_a_directory_with_no_archives(tmp_path):
    from hbkit import mount as m
    (tmp_path / "not-an-archive").mkdir()
    assert m.warm(str(tmp_path), quiet=True) == 0


def test_rate_never_reports_zero_for_a_live_transfer():
    """Network transfers are often < 1 MB/s; '0 MB/s' would be a lie."""
    from hbkit.tui import rate
    assert rate(167_000) == "167 KB/s"      # the R2 case that exposed this
    assert rate(900_000).endswith("KB/s")
    assert rate(1_200_000) == "1.2 MB/s"
    assert rate(106_000_000) == "106 MB/s"
    assert rate(0) == "0 B/s"
    for v in (1, 999, 1_000, 999_999, 1_000_000, 10_000_000):
        assert "0 MB/s" != rate(v), v


# ------------------------------------------------------- restore manifest / resume state

def test_manifest_roundtrip_and_torn_tail(tmp_path):
    from hbkit import manifest as mf
    d = str(tmp_path)
    mf.append(d, "a/b.png", 100, "aa" * 16)
    mf.append(d, "c.png", 5, "bb" * 16)
    with open(mf.path_for(d), "a") as fh:      # simulate a run killed mid-write
        fh.write('{"p": "torn", "s": 1')
    got = mf.load(d)
    assert set(got) == {"a/b.png", "c.png"}, "a torn final line must be skipped, not fatal"
    assert got["a/b.png"]["s"] == 100


def test_manifest_detects_right_size_wrong_bytes(tmp_path):
    """The case plain size comparison cannot see - the reason this file exists."""
    from hbkit import manifest as mf
    d = str(tmp_path)
    good = b"hello world" * 10
    (tmp_path / "f.bin").write_bytes(good)
    mf.append(d, "f.bin", len(good), mf.file_md5(str(tmp_path / "f.bin")))
    known = mf.load(d)
    assert mf.is_complete(d, "f.bin", len(good), known, verify=True)
    (tmp_path / "f.bin").write_bytes(b"\xff" * len(good))          # same size, wrong bytes
    assert mf.is_complete(d, "f.bin", len(good), known, verify=False), "size-only still passes"
    assert not mf.is_complete(d, "f.bin", len(good), known, verify=True), "strict must catch it"


def test_manifest_strict_refuses_files_it_has_no_hash_for(tmp_path):
    from hbkit import manifest as mf
    d = str(tmp_path)
    (tmp_path / "x.bin").write_bytes(b"1234")
    assert mf.is_complete(d, "x.bin", 4, {}, verify=False)
    assert not mf.is_complete(d, "x.bin", 4, {}, verify=True), "no recorded hash => not vouched"


def test_sweep_parts_removes_only_part_files(tmp_path):
    from hbkit import manifest as mf
    (tmp_path / "sub").mkdir()
    for n in ("a.part", "sub/b.part", "keep.png", "sub/keep2.jpg"):
        (tmp_path / n).write_bytes(b"x")
    assert mf.sweep_parts(str(tmp_path)) == 2
    assert (tmp_path / "keep.png").exists() and (tmp_path / "sub/keep2.jpg").exists()
    assert not (tmp_path / "a.part").exists()


def test_missing_file_is_never_complete(tmp_path):
    from hbkit import manifest as mf
    assert not mf.is_complete(str(tmp_path), "nope.bin", 10, {})


def test_cache_dir_prefers_new_name_but_honours_the_old_one(tmp_path, monkeypatch):
    """Renaming the cache must not orphan an index someone waited minutes to build."""
    from hbkit import index as hbki
    home = tmp_path
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(home)) if p.startswith("~") else p)
    assert hbki._default_cache().endswith("/.cache/hbkit")          # nothing exists yet
    (home / ".cache" / "hbk-recovery").mkdir(parents=True)
    assert hbki._default_cache().endswith("/.cache/hbk-recovery")   # legacy honoured
    (home / ".cache" / "hbkit").mkdir(parents=True)
    assert hbki._default_cache().endswith("/.cache/hbkit")          # new wins once present


def test_runner_hands_its_import_path_to_workers(tmp_path, monkeypatch):
    """Workers are spawned as `sys.executable -m hbkit.runner`, and sys.executable is the
    *unwrapped* interpreter. Where hbkit reaches sys.path through a wrapper script instead
    of site-packages - a Nix build, a PYTHONPATH-based distro package - that interpreter
    cannot import hbkit and every worker dies before extracting a byte. Caught by packaging
    for Nix, invisible to a pip install."""
    import io

    from hbkit import runner as hbk_runner

    seen = {}

    class FakePopen:
        def __init__(self, cmd, env=None, **kw):
            seen["cmd"], seen["env"] = cmd, env
            self.stdout, self.stderr = io.StringIO(""), io.StringIO("")

        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(hbk_runner.subprocess, "Popen", FakePopen)
    r = hbk_runner.Runner("/archive", str(tmp_path), [("/a", 0, 1, 0)], n_workers=1)
    r.start()
    r.cleanup()

    assert seen["cmd"][1:3] == ["-m", "hbkit.runner"]
    paths = seen["env"]["PYTHONPATH"].split(os.pathsep)
    assert any(os.path.isdir(os.path.join(p, "hbkit")) for p in paths), \
        f"no path in PYTHONPATH provides hbkit: {paths}"


@pytest.mark.parametrize(("flag", "env", "want"), [
    (None, "env", "env"),          # the bug: doctor ignored HBK_PASSWORD entirely
    ("flag", "env", "flag"),       # an explicit -p still wins
    (None, None, None),            # nothing set: leave it to the prompt
])
def test_doctor_honours_the_password_environment_variable(monkeypatch, flag, env, want):
    """`-p` and HBK_PASSWORD are documented as equivalent, but doctor forwarded only `-p`,
    so `HBK_PASSWORD=... hbk <archive> doctor` called a readable encrypted archive
    unrecoverable."""
    from hbkit import cli, doctor

    seen = {}

    class Result:
        ok, blockers = True, []

    monkeypatch.setattr(doctor, "diagnose",
                        lambda a, password=None: (seen.update(pw=password), Result())[1])
    monkeypatch.setattr(doctor, "render", lambda r: "")
    monkeypatch.delenv("HBK_PASSWORD", raising=False)
    if env:
        monkeypatch.setenv("HBK_PASSWORD", env)

    argv = ["hbk", "/some/archive", "doctor"] + (["-p", flag] if flag else [])
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 0
    assert seen["pw"] == want


@pytest.mark.parametrize("flag", ["-s", "-n", "--sample"])
def test_doctor_forwards_sample_count(monkeypatch, flag):
    from hbkit import cli, doctor

    seen = {}

    class Result:
        ok, blockers = True, []

    monkeypatch.setattr(doctor, "diagnose",
                        lambda a, sample=10, password=None:
                        (seen.update(sample=sample), Result())[1])
    monkeypatch.setattr(doctor, "render", lambda r: "")
    monkeypatch.setattr(sys, "argv", ["hbk", "/some/archive", "doctor", flag, "2"])
    assert cli.main() == 0
    assert seen["sample"] == 2


def test_lazycats_lists_shards_before_any_are_opened():
    """`doctor` describes an archive without extracting from it, so nothing has opened a
    file_chunk shard by the time it prints them. Iterating the dict reported an empty list
    and made a healthy archive look malformed."""
    cats = hbk._LazyCats({4: "/nonexistent/file_chunk4.index",
                          0: "/nonexistent/file_chunk0.index"})
    assert cats.known == [0, 4]
    assert len(cats) == 0, "listing shards must not open them"


@pytest.mark.parametrize("typed", ["~/hbkit-test-not-there/restore", "restore/here"])
def test_tui_free_space_follows_home_and_relative_destinations(tmp_path, monkeypatch, typed):
    """Typing ~/restore showed "invalid path" under the destination, though the restore
    itself expanded ~ fine. A relative path did the same."""
    import asyncio
    import sqlite3
    from types import SimpleNamespace

    from hbkit import tui

    db = tmp_path / "index.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE node(id INTEGER PRIMARY KEY, parent INTEGER, name TEXT, "
                "isdir INTEGER, size INTEGER, nfiles INTEGER, ovf INTEGER, mtime INTEGER, "
                "share TEXT)")
    con.execute("INSERT INTO node VALUES (1, NULL, 'share', 1, 10, 1, NULL, 0, 'share')")
    con.commit()
    con.close()
    monkeypatch.chdir(tmp_path)

    class App(tui.HBKApp):
        def on_mount(self, event):
            event.prevent_default()            # skip the archive picker
            self.push_screen(tui.BrowserScreen(SimpleNamespace(root="demo"), tui.Index(str(db))))

    async def space_line():
        app = App(1)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.screen.query_one("#dest", tui.Input).value = typed
            await pilot.pause()
            return str(app.screen.query_one("#space-info", tui.Static).render())

    line = asyncio.run(space_line())
    assert "free" in line and "invalid" not in line, line


# --------------------------------------- backup versions (synthetic archives, no data needed)

T0 = 1_760_000_000          # 2025-10-09 08:53:20 UTC; versions below are a day apart
DAY = 86_400


def _synth():
    """The synthetic-archive builder, or a skip: it compresses with liblz4."""
    import synth
    pytest.importorskip("nacl.bindings")
    try:
        hbk.lz4()
    except RuntimeError:
        pytest.skip("liblz4 not available")
    return synth


def _three_versions(tmp_path, encrypted=False, statuses=("Complete",) * 3, **kw):
    """The same file in three versions, with different bytes in each. Version 2 and 3
    reuse chunks written by earlier versions, as Hyper Backup's dedup does."""
    synth = _synth()
    head, mid = b"H" * 3000 + os.urandom(1000), os.urandom(5000)
    bodies = [[head, os.urandom(4096)],
              [head, mid, os.urandom(2048)],
              [head, mid, os.urandom(777)]]
    vs = [synth.V({"firebird/db one.gbk": parts,
                   "firebird/other.gbk": [f"other v{i + 1}".encode() * 50]},
                  T0 + i * DAY, status)
          for i, (parts, status) in enumerate(zip(bodies, statuses))]
    root = synth.build(str(tmp_path / "arc"), vs, encrypted=encrypted, **kw)
    return root, [b"".join(p) for p in bodies]


def test_versions_lists_id_start_time_and_completion(tmp_path):
    root, _ = _three_versions(tmp_path, statuses=("Complete", "Complete", "Backingup"))
    vs = hbk.Archive(root).versions()
    assert [v.id for v in vs] == [1, 2, 3]
    assert [v.timestamp for v in vs] == [T0, T0 + DAY, T0 + 2 * DAY]
    assert [v.status for v in vs] == ["Complete", "Complete", "Backingup"]
    assert [v.complete for v in vs] == [True, True, False]


def test_default_is_the_newest_complete_version(tmp_path):
    root, _ = _three_versions(tmp_path, statuses=("Complete", "Complete", "Backingup"))
    arc = hbk.Archive(root)
    assert arc.resolve_version(None) == 2
    assert os.path.basename(arc.share_db("db_staging")) == "2.db"


def test_an_explicit_version_number_is_used_exactly(tmp_path):
    root, _ = _three_versions(tmp_path)
    arc = hbk.Archive(root, version="1")
    assert arc.version == 1
    assert os.path.basename(arc.share_db("db_staging")) == "1.db"


def test_unfinished_version_is_refused_unless_allowed(tmp_path):
    root, _ = _three_versions(tmp_path, statuses=("Complete", "Complete", "Backingup"))
    with pytest.raises(hbk.VersionNotFound, match="not complete"):
        hbk.Archive(root, version="3")
    assert hbk.Archive(root, version="3", allow_incomplete=True).version == 3


@pytest.mark.parametrize(("spec", "error"), [
    ("9", hbk.VersionNotFound), ("0", hbk.VersionNotFound),
    ("-1", hbk.InvalidVersionSpec), ("abc", hbk.InvalidVersionSpec),
    ("@not-a-date", hbk.InvalidVersionSpec), ("@2025-02-30", hbk.InvalidVersionSpec),
])
def test_missing_or_malformed_version_is_refused(tmp_path, spec, error):
    root, _ = _three_versions(tmp_path)
    with pytest.raises(error):
        hbk.Archive(root, version=spec)


# Dates on the command line are local time. Tests that check dates pin it to UTC through
# TZ, which needs time.tzset - absent on Windows, where they are skipped.
needs_tz = pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset (POSIX)")


@pytest.fixture
def utc(monkeypatch):
    """Pin local time to UTC for the duration of a test."""
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset (POSIX)")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# v1 2025-10-09 08:53:20, v2 2025-10-10 08:53:20, v3 2025-10-11 08:53:20 (UTC)
@pytest.mark.parametrize(("spec", "want"), [
    ("@2025-10-09", 1),                 # a bare date means "by the end of that day"
    ("@2025-10-10", 2),
    ("@2025-10-10 08:53", 2),           # minute precision includes that whole minute
    ("@2025-10-10 08:52", 1),
    ("@2025-10-10T08:53:19", 1),        # second precision is exact
    ("@2025-10-10 08:53:20", 2),
    ("@2030-01-01", 2),                 # v3 is unfinished, so it is skipped
])
def test_version_by_date_is_newest_complete_not_later_than_it(tmp_path, utc, spec, want):
    root, _ = _three_versions(tmp_path, statuses=("Complete", "Complete", "Backingup"))
    assert hbk.Archive(root).resolve_version(spec) == want


def test_version_by_date_can_include_unfinished_versions_when_asked(tmp_path, utc):
    root, _ = _three_versions(tmp_path, statuses=("Complete", "Complete", "Backingup"))
    assert hbk.Archive(root).resolve_version("@2030-01-01", allow_incomplete=True) == 3


def test_version_by_date_before_the_first_backup_is_refused(tmp_path, utc):
    root, _ = _three_versions(tmp_path)
    with pytest.raises(hbk.VersionNotFound, match="no complete version at or before"):
        hbk.Archive(root, version="@2025-10-08")


def test_version_by_date_needs_recorded_dates(tmp_path, utc):
    root, _ = _three_versions(tmp_path, with_version_info=False)
    arc = hbk.Archive(root)
    assert [v.id for v in arc.versions()] == [1, 2, 3]       # numbers still work
    assert arc.resolve_version("2") == 2
    with pytest.raises(hbk.VersionNotFound, match="no version dates"):
        arc.resolve_version("@2025-10-10")


def test_each_version_gets_its_own_index(tmp_path, monkeypatch):
    import sqlite3

    from hbkit import index as hbk_index
    monkeypatch.setattr(hbk_index, "CACHE", str(tmp_path / "cache"))
    root, bodies = _three_versions(tmp_path)

    def size_of_db_one(arc):
        p = hbk_index.open_or_build(arc)
        db = sqlite3.connect(p)
        (n,) = db.execute("SELECT size FROM node WHERE name='db one.gbk'").fetchone()
        db.close()
        return p, n

    p1, n1 = size_of_db_one(hbk.Archive(root, version=1))
    p2, n2 = size_of_db_one(hbk.Archive(root, version=2))
    pd, nd = size_of_db_one(hbk.Archive(root))
    assert len({p1, p2, pd}) == 3, "versions must not share an index file"
    assert (n1, n2, nd) == tuple(len(b) for b in bodies)
    assert "-v1-" in os.path.basename(p1) and "-v2-" in os.path.basename(p2)
    # building v2 must not have invalidated v1's index
    assert hbk_index.is_current(hbk.Archive(root, version=1))


def _hbk(tmp_path, *args, password=None):
    env = {**os.environ, "HBK_CACHE": str(tmp_path / "cache"), "TZ": "UTC"}
    env.pop("HBK_PASSWORD", None)
    if password:
        env["HBK_PASSWORD"] = password
    return subprocess.run([sys.executable, "-m", "hbkit.cli", *map(str, args)],
                          capture_output=True, text=True, env=env, input="")


GLOB = "/db_staging/firebird/*.gbk"
DB_ONE = "db_staging/firebird/db one.gbk"


@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "encrypted"])
def test_get_returns_exactly_the_requested_versions_bytes(tmp_path, encrypted):
    """Versions 2 and 3 rebuild the file from chunks several versions wrote - on the
    encrypted archive, under several versions' keys."""
    import synth
    root, bodies = _three_versions(tmp_path, encrypted=encrypted)
    pw = synth.PASSWORD if encrypted else None
    for n, want in enumerate(bodies, 1):
        out = tmp_path / f"out{n}"
        r = _hbk(tmp_path, root, "get", GLOB, out, "--version", n, "-j", "2", password=pw)
        assert r.returncode == 0, r.stdout + r.stderr
        assert (out / DB_ONE).read_bytes() == want, f"version {n}"
        assert (out / "db_staging/firebird/other.gbk").read_bytes() == \
            f"other v{n}".encode() * 50
    r = _hbk(tmp_path, root, "get", GLOB, tmp_path / "newest", password=pw)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "newest" / DB_ONE).read_bytes() == bodies[-1]


@needs_tz
@pytest.mark.parametrize("args", [["--version", "@2025-10-10"], ["--before", "2025-10-10 23:59"],
                                  ["--version=@2025-10-10"], ["--before=2025-10-10"]])
def test_get_by_date(tmp_path, args):
    root, bodies = _three_versions(tmp_path)
    out = tmp_path / "out"
    r = _hbk(tmp_path, root, "get", GLOB, out, *args)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (out / DB_ONE).read_bytes() == bodies[1]


def test_get_resumes_within_a_version(tmp_path):
    root, bodies = _three_versions(tmp_path)
    out = tmp_path / "out"
    assert _hbk(tmp_path, root, "get", GLOB, out, "--version", "1").returncode == 0
    r = _hbk(tmp_path, root, "get", GLOB, out, "--version", "1")
    assert r.returncode == 0 and "2 already present" in r.stdout, r.stdout
    assert (out / DB_ONE).read_bytes() == bodies[0]


def test_verify_and_list_follow_the_selected_version(tmp_path):
    root, bodies = _three_versions(tmp_path)
    r = _hbk(tmp_path, root, "verify", GLOB, "--version", "1")
    assert r.returncode == 0 and "2 verified" in r.stdout, r.stdout + r.stderr
    for n, body in enumerate(bodies, 1):
        r = _hbk(tmp_path, root, "list", "db one", "--version", n)
        assert r.returncode == 0, r.stderr
        assert f"{len(body):>13,}  /{DB_ONE}" in r.stdout, r.stdout


def test_list_accepts_the_same_glob_as_get(tmp_path):
    """`list "/db_staging/firebird/*"` searched for a literal '*' and found nothing."""
    root, _ = _three_versions(tmp_path)
    r = _hbk(tmp_path, root, "list", GLOB)
    assert r.returncode == 0 and "2 files" in r.stdout, r.stdout + r.stderr


def test_resume_never_keeps_a_same_sized_file_from_another_version(tmp_path):
    """Database dumps often keep their size between nights. Restoring version 2 into a
    directory holding version 1 must not skip a file just because its size matches."""
    synth = _synth()
    a, b = b"A" * 4096, b"B" * 4096
    root = synth.build(str(tmp_path / "arc"), [synth.V({"firebird/x.bak": [a]}, T0),
                                               synth.V({"firebird/x.bak": [b]}, T0 + DAY)])
    out = tmp_path / "out"
    glob = "/db_staging/firebird/*"
    assert _hbk(tmp_path, root, "get", glob, out, "--version", "1").returncode == 0
    assert (out / "db_staging/firebird/x.bak").read_bytes() == a
    r = _hbk(tmp_path, root, "get", glob, out, "--version", "2")
    assert r.returncode == 0 and "1 extracted" in r.stdout, r.stdout + r.stderr
    assert (out / "db_staging/firebird/x.bak").read_bytes() == b
    r = _hbk(tmp_path, root, "get", glob, out, "--version", "2")
    assert "1 already present" in r.stdout, r.stdout     # same version still resumes


@needs_tz
def test_versions_porcelain_is_one_tab_separated_line_per_version(tmp_path):
    root, _ = _three_versions(tmp_path, statuses=("Complete", "Complete", "Backingup"))
    r = _hbk(tmp_path, root, "versions", "--porcelain")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        f"1\t2025-10-09T08:53:20+00:00\t{T0}\tComplete",
        f"2\t2025-10-10T08:53:20+00:00\t{T0 + DAY}\tComplete",
        f"3\t2025-10-11T08:53:20+00:00\t{T0 + 2 * DAY}\tBackingup",
    ]


@needs_tz
def test_versions_marks_the_default_and_needs_no_password(tmp_path):
    """version_info.db is stored in the clear, so a script can pick a version before
    it has the password in hand."""
    root, _ = _three_versions(tmp_path, encrypted=True,
                              statuses=("Complete", "Complete", "Backingup"))
    r = _hbk(tmp_path, root, "versions")
    assert r.returncode == 0, r.stderr
    line2 = next(x for x in r.stdout.splitlines() if x.split()[:1] == ["2"])
    assert "2025-10-10 08:53:20" in line2 and "Complete" in line2 and "default" in line2
    assert "Backingup" in r.stdout


@pytest.mark.parametrize(("args", "code", "msg"), [
    (["get", GLOB, "OUT", "--version", "9"], 4, "version 9 does not exist"),
    (["list", "--version", "3"], 4, "not complete"),
    (["verify", GLOB, "--version", "@2020-01-01"], 4, "no complete version at or before"),
    (["get", GLOB, "OUT", "--version", "1", "--before", "2025-10-10"], 2, "not both"),
    (["get", "/nothing/*", "OUT"], 5, ""),
    (["list", "/nothing/*"], 5, ""),
    (["get", GLOB], 2, ""),
    (["versions", "--version", "1"], 2, "takes no --version"),
])
def test_exit_codes(tmp_path, args, code, msg):
    root, _ = _three_versions(tmp_path, statuses=("Complete", "Complete", "Backingup"))
    r = _hbk(tmp_path, root, *[str(tmp_path / "out") if x == "OUT" else x for x in args])
    assert r.returncode == code, r.stdout + r.stderr
    assert msg in r.stderr + r.stdout


def test_exit_codes_for_passwords(tmp_path):
    import synth
    root, _ = _three_versions(tmp_path, encrypted=True)
    assert _hbk(tmp_path, root, "list").returncode == 3                       # none given
    r = _hbk(tmp_path, root, "list", password=synth.PASSWORD + "x")
    assert r.returncode == 3 and "wrong password" in r.stderr
    assert _hbk(tmp_path, root, "list", password=synth.PASSWORD).returncode == 0


def test_a_corrupt_chunk_fails_with_exit_code_1(tmp_path):
    root, _ = _three_versions(tmp_path)
    bucket = os.path.join(root, "Pool", "0", "0", "1.bucket")       # written by version 2
    with open(bucket, "r+b") as fh:
        b = bytearray(fh.read())
        b[len(b) // 2] ^= 0xFF
        fh.seek(0)
        fh.write(b)
    r = _hbk(tmp_path, root, "verify", GLOB, "--version", "2")
    assert r.returncode == 1 and "FAIL" in r.stdout, r.stdout + r.stderr
    assert _hbk(tmp_path, root, "verify", GLOB, "--version", "1").returncode == 0


@needs_tz
def test_info_lists_versions_with_dates(tmp_path):
    root, _ = _three_versions(tmp_path)
    r = _hbk(tmp_path, root, "info", "--version", "@2025-10-09")
    assert r.returncode == 0, r.stderr
    assert "reading=1" in r.stdout
    assert "2025-10-11 08:53:20  Complete" in r.stdout


def test_doctor_reads_the_selected_version(tmp_path):
    from hbkit import doctor
    root, _ = _three_versions(tmp_path, statuses=("Complete", "Complete", "Backingup"))
    r = doctor.diagnose(root, version="1")
    assert r.ok and not r.blockers, (r.blockers, r.checks)
    facts = dict(r.facts)
    assert facts["reading version"].startswith("1 ")
    assert not any("reads the newest" in w for w in r.warnings)
    r = doctor.diagnose(root, version="3")
    assert r.blockers and "not complete" in r.blockers[0]
    r = doctor.diagnose(root)
    assert dict(r.facts)["reading version"].startswith("2 ")


def test_doctor_cli_forwards_the_version(tmp_path):
    root, _ = _three_versions(tmp_path)
    r = _hbk(tmp_path, root, "doctor", "--version", "1")
    assert r.returncode == 0 and "reading version : 1 " in r.stdout, r.stdout


def test_tui_opens_the_selected_version(tmp_path, monkeypatch):
    import asyncio

    from hbkit import index as hbk_index
    from hbkit import tui
    monkeypatch.setattr(hbk_index, "CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(tui, "scan_for_archives", lambda: [])   # a real one would be probed too
    root, _ = _three_versions(tmp_path)

    class App(tui.HBKApp):
        def on_mount(self):
            self.push_screen(tui.ArchiveScreen(None, version=self.version))

    async def probe():
        app = App(1, version="1")
        async with app.run_test() as pilot:
            app.screen.probe(root)
            await pilot.pause()
            return app.screen._arc, str(app.screen.query_one("#probe", tui.Static).render())

    arc, text = asyncio.run(probe())
    assert arc is not None and arc.version == 1
    assert "version 1" in text


# ----------------------------------------- a real multi-version archive (opt-in, read-only)

MULTI = os.environ.get("HBK_TEST_MULTI_ARCHIVE", "")
MULTI_PW = os.environ.get("HBK_TEST_MULTI_PASSWORD", "") or None
# Narrow this to user data if the archive holds files hbkit cannot decode at all, such as
# whole-file-dedup entries (Hyper Backup's own @AppConfig/config.dss is one).
MULTI_GLOB = os.environ.get("HBK_TEST_MULTI_GLOB", "/*")
needs_multi = pytest.mark.skipif(not (MULTI and os.path.exists(MULTI)),
                                 reason="set HBK_TEST_MULTI_ARCHIVE (and "
                                        "HBK_TEST_MULTI_PASSWORD if it is encrypted)")


@needs_multi
def test_real_archive_lists_versions_without_a_password():
    arc = hbk.Archive(MULTI, unlock=False)
    vs = arc.versions()
    assert len(vs) >= 2, "point HBK_TEST_MULTI_ARCHIVE at an archive with several versions"
    assert all(v.timestamp for v in vs) and any(v.complete for v in vs)
    assert [v.id for v in vs] == sorted(v.id for v in vs)


@needs_multi
def test_real_archive_every_complete_version_verifies(tmp_path):
    """Rebuilds every file of the oldest and the newest complete version with full
    checksum checks. Reads only; writes nothing but the index cache in tmp_path."""
    arc = hbk.Archive(MULTI, unlock=False)
    done = [v.id for v in arc.versions() if v.complete]
    for vid in sorted({done[0], done[-1]}):
        r = _hbk(tmp_path, MULTI, "verify", MULTI_GLOB, "--version", vid, password=MULTI_PW)
        assert r.returncode == 0, f"version {vid}: {r.stdout[-2000:]}{r.stderr[-2000:]}"


def test_unknown_version_info_layout_is_refused_unless_incomplete_is_allowed(tmp_path):
    """A version_info.db hbkit does not understand cannot say which versions finished:
    refused by default, readable with --allow-incomplete; dates stay refused."""
    import sqlite3
    root, bodies = _three_versions(tmp_path)
    p = os.path.join(root, "Config", "version_info.db")
    os.remove(p)
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE version_info (vid INTEGER, started INTEGER)")
    db.commit()
    db.close()
    assert _hbk(tmp_path, root, "get", GLOB, tmp_path / "out").returncode == 2
    r = _hbk(tmp_path, root, "get", GLOB, tmp_path / "out", "--allow-incomplete")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "out" / DB_ONE).read_bytes() == bodies[-1]
    r = _hbk(tmp_path, root, "versions")
    assert r.returncode == 2 and "unknown layout" in r.stderr
    assert _hbk(tmp_path, root, "list", "--version", "@2025-10-10").returncode == 2


@needs_multi
def test_real_archive_default_restore_is_the_newest_complete_version(tmp_path):
    arc = hbk.Archive(MULTI, unlock=False)
    newest = [v.id for v in arc.versions() if v.complete][-1]
    a = _hbk(tmp_path, MULTI, "get", MULTI_GLOB, tmp_path / "a", password=MULTI_PW)
    b = _hbk(tmp_path, MULTI, "get", MULTI_GLOB, tmp_path / "b", "--version", newest,
             password=MULTI_PW)
    assert a.returncode == b.returncode == 0, a.stdout + a.stderr + b.stdout + b.stderr
    files = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*")
                   if p.is_file() and p.name != ".hbkit-restore.jsonl")
    assert files
    for f in files:
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes(), f


# ------------------------------------------------------------- review round 1

@pytest.fixture
def warsaw(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset (POSIX)")
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize(("day", "last"), [
    ("2026-03-29", "2026-03-29 23:59:59"),      # spring forward: a 23-hour day
    ("2026-10-25", "2026-10-25 23:59:59"),      # fall back: a 25-hour day
    ("2026-06-01", "2026-06-01 23:59:59"),
])
def test_a_bare_date_ends_at_local_midnight_even_on_dst_days(warsaw, day, last):
    import datetime as dt
    want = dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S").timestamp()
    assert hbk.parse_moment(day) == want


def test_a_corrupt_version_list_refuses_the_default_unless_allowed(tmp_path):
    """With the version list unreadable, hbkit cannot tell finished backups from one
    still being written (or torn mid-read), so the default refuses rather than guess;
    --allow-incomplete reads the newest file lists, with an accurate warning."""
    root, bodies = _three_versions(tmp_path, statuses=("Complete", "Complete", "Partial"))
    with open(os.path.join(root, "Config", "version_info.db"), "wb") as fh:
        fh.write(b"this is not a database" * 100)
    r = _hbk(tmp_path, root, "get", GLOB, tmp_path / "out")
    assert r.returncode == 2 and "--allow-incomplete" in r.stderr, r.stdout + r.stderr
    assert "Traceback" not in r.stderr
    r = _hbk(tmp_path, root, "get", GLOB, tmp_path / "out", "--allow-incomplete")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "completion" in r.stderr and "does not record" not in r.stderr, r.stderr
    assert (tmp_path / "out" / DB_ONE).read_bytes() == bodies[-1]
    r = _hbk(tmp_path, root, "versions")
    assert r.returncode == 2 and "Traceback" not in r.stderr, r.stderr
    r = _hbk(tmp_path, root, "info")             # still shows the shares, then the reason
    assert r.returncode == 2 and "share " in r.stdout and "versions  : unknown" in r.stdout
    assert "--allow-incomplete" in r.stderr, r.stderr


def test_allow_incomplete_alone_reads_the_newest_version_even_if_unfinished(tmp_path):
    root, bodies = _three_versions(tmp_path, statuses=("Complete", "Complete", "Backingup"))
    r = _hbk(tmp_path, root, "get", GLOB, tmp_path / "out", "--allow-incomplete")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "out" / DB_ONE).read_bytes() == bodies[2]


def test_no_complete_version_is_exit_4_not_an_empty_archive(tmp_path):
    from hbkit import doctor
    root, bodies = _three_versions(tmp_path, statuses=("Backingup",) * 3)
    for args in (["list"], ["get", GLOB, tmp_path / "out"], ["info"]):
        r = _hbk(tmp_path, root, *args)
        assert r.returncode == 4, (args, r.stdout + r.stderr)
        assert "--allow-incomplete" in r.stderr, r.stderr
    r = doctor.diagnose(root)
    assert r.blockers and not r.ok
    r = _hbk(tmp_path, root, "get", GLOB, tmp_path / "out", "--allow-incomplete")
    assert r.returncode == 0 and (tmp_path / "out" / DB_ONE).read_bytes() == bodies[2]


def test_default_reads_one_version_for_every_share(tmp_path):
    """A share dropped from the task must not be restored from an old version and
    presented as the newest: the default is one version, exactly like --version N."""
    synth = _synth()
    root = synth.build(str(tmp_path / "arc"), [
        synth.V({"d/a.txt": [b"a1" * 9]}, T0, shares=["A", "B"]),
        synth.V({"d/a.txt": [b"a2" * 9]}, T0 + DAY, shares=["A"]),
    ])
    default = _hbk(tmp_path, root, "list", "/")
    explicit = _hbk(tmp_path, root, "list", "/", "--version", "2")
    assert default.returncode == explicit.returncode == 0
    assert default.stdout == explicit.stdout
    assert "/B/" not in default.stdout
    assert "version 2" in default.stderr


def test_default_and_explicit_newest_share_one_index(tmp_path, monkeypatch):
    from hbkit import index as hbk_index
    monkeypatch.setattr(hbk_index, "CACHE", str(tmp_path / "cache"))
    root, _ = _three_versions(tmp_path)
    assert hbk_index.cache_path(hbk.Archive(root)) == \
        hbk_index.cache_path(hbk.Archive(root, version=3))


def test_a_rebuilt_index_replaces_the_stale_one_for_the_same_version(tmp_path, monkeypatch):
    import sqlite3

    from hbkit import index as hbk_index
    cache = tmp_path / "cache"
    monkeypatch.setattr(hbk_index, "CACHE", str(cache))
    root, _ = _three_versions(tmp_path)
    keep = hbk_index.build(hbk.Archive(root, version=1))
    old = hbk_index.build(hbk.Archive(root, version=2))
    # a 0.4.7-era index of the same archive (no version in meta) and a foreign one
    legacy = cache / "synth.hbk-0123456789abcdef.db"
    foreign = cache / "synth.hbk-v2-fedcba9876543210.db"
    for p, r in ((legacy, root), (foreign, "/some/other/synth.hbk")):
        db = sqlite3.connect(p)
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('root', ?)", (r,))
        db.commit()
        db.close()
    os.utime(os.path.join(root, "Config", "@Share", "db_staging", "2.db"), (1, 1))
    new = hbk_index.build(hbk.Archive(root, version=2))
    assert new != old and os.path.exists(new)
    assert not os.path.exists(old), "stale index for the same version left behind"
    assert not legacy.exists(), "pre-version index of this archive left behind"
    assert os.path.exists(keep), "another version's index must survive"
    assert foreign.exists(), "another archive's index must survive"


@pytest.mark.parametrize("spec", ["@2026-13-01", "abc", "@yesterday", "1.5"])
def test_a_malformed_selector_is_a_usage_error(tmp_path, spec):
    root, _ = _three_versions(tmp_path)
    r = _hbk(tmp_path, root, "list", "--version", spec)
    assert r.returncode == 2, r.stdout + r.stderr
    assert spec in r.stderr


@pytest.mark.parametrize("args", [
    ["list", "--porcelain"],
    ["get", GLOB, "OUT", "--porcelain"],
    ["list", "--version", "1", "--version", "2"],
    ["list", "--before", "2025-10-10", "--before", "2025-10-11"],
])
def test_options_that_would_be_ignored_are_refused(tmp_path, args):
    root, _ = _three_versions(tmp_path)
    r = _hbk(tmp_path, root, *[str(tmp_path / "out") if x == "OUT" else x for x in args])
    assert r.returncode == 2, r.stdout + r.stderr


def test_version_options_are_refused_by_mount_commands(tmp_path):
    r = _hbk(tmp_path, "warm", tmp_path, "--version", "2")
    assert r.returncode == 2 and "--version" in r.stderr


def test_an_archive_opened_without_its_password_refuses_to_extract(tmp_path):
    root, _ = _three_versions(tmp_path, encrypted=True)
    arc = hbk.Archive(root, unlock=False)
    with pytest.raises(hbk.NeedPassword):
        arc.extract(64, 10)


def test_tui_refuses_an_archive_with_no_complete_version(tmp_path, monkeypatch):
    import asyncio

    from hbkit import index as hbk_index
    from hbkit import tui
    monkeypatch.setattr(hbk_index, "CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(tui, "scan_for_archives", lambda: [])   # a real one would be probed too
    root, _ = _three_versions(tmp_path, statuses=("Backingup",) * 3)

    class App(tui.HBKApp):
        def on_mount(self):
            self.push_screen(tui.ArchiveScreen(None))

    async def probe():
        app = App(1)
        async with app.run_test() as pilot:
            app.screen.probe(root)
            await pilot.pause()
            return app.screen._arc, str(app.screen.query_one("#probe", tui.Static).render())

    arc, text = asyncio.run(probe())
    assert arc is None and "no complete backup version" in text


# ------------------------------------------------------------- review round 2

def test_the_default_version_is_fixed_for_the_life_of_an_archive_object(tmp_path, monkeypatch):
    """A backup finishing while hbkit runs must not switch versions half-way through
    building an index (one share from the old version, one from the new)."""
    import sqlite3

    from hbkit import index as hbk_index
    monkeypatch.setattr(hbk_index, "CACHE", str(tmp_path / "cache"))
    synth = _synth()
    root = synth.build(str(tmp_path / "arc"), [
        synth.V({"d/a.txt": [b"v1" * 9]}, T0, shares=["A", "B"]),
        synth.V({"d/a.txt": [b"v2" * 9]}, T0 + DAY, "Backingup", shares=["A", "B"]),
    ])
    arc = hbk.Archive(root)
    assert arc.selected_version() == 1
    db = sqlite3.connect(os.path.join(root, "Config", "version_info.db"))
    db.execute("UPDATE version_info SET status='Complete' WHERE id=2")
    db.commit()
    db.close()
    assert arc.selected_version() == 1
    p = hbk_index.build(arc)
    assert "-v1-" in os.path.basename(p) and p == hbk_index.cache_path(arc)
    assert hbk.Archive(root).selected_version() == 2       # a new run sees the new one


def test_a_leading_version_option_selects_a_backup_not_the_hbkit_version(tmp_path):
    root, bodies = _three_versions(tmp_path)
    r = _hbk(tmp_path, "--version", "1", root, "get", GLOB, tmp_path / "out")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "out" / DB_ONE).read_bytes() == bodies[0]
    for flag in ("--version", "-V"):
        r = _hbk(tmp_path, flag)
        assert r.returncode == 0 and r.stdout.startswith("hbkit ")


def test_an_interrupted_restore_exits_130(tmp_path, monkeypatch):
    from hbkit import cli
    from hbkit import runner as hbk_runner

    class Interrupted(hbk_runner.Runner):
        def start(self):
            self._live = 1

        def poll(self, limit=8000):
            raise KeyboardInterrupt

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(hbk_runner, "Runner", Interrupted)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    rows = [("/s/a", 64, 10, 0)]
    arc = type("A", (), {"root": str(tmp_path)})()
    assert cli.run(arc, rows, str(tmp_path / "out"), 1, False) == 130


def test_exporting_an_index_elsewhere_keeps_the_cached_one(tmp_path, monkeypatch):
    from hbkit import index as hbk_index
    monkeypatch.setattr(hbk_index, "CACHE", str(tmp_path / "cache"))
    root, _ = _three_versions(tmp_path)
    cached = hbk_index.build(hbk.Archive(root, version=2))
    hbk_index.build(hbk.Archive(root, version=2), out=str(tmp_path / "export.db"))
    assert os.path.exists(cached)


def test_indexes_of_versions_no_longer_in_the_archive_are_pruned(tmp_path, monkeypatch):
    """Retention deletes old versions; their indexes must not pile up forever."""
    import sqlite3

    from hbkit import index as hbk_index
    monkeypatch.setattr(hbk_index, "CACHE", str(tmp_path / "cache"))
    root, _ = _three_versions(tmp_path)
    gone = hbk_index.build(hbk.Archive(root, version=1))
    kept = hbk_index.build(hbk.Archive(root, version=2))
    os.remove(os.path.join(root, "Config", "@Share", "db_staging", "1.db"))
    db = sqlite3.connect(os.path.join(root, "Config", "version_info.db"))
    db.execute("DELETE FROM version_info WHERE id=1")
    db.commit()
    db.close()
    hbk_index.build(hbk.Archive(root, version=3))
    assert not os.path.exists(gone) and os.path.exists(kept)


def test_a_version_without_file_lists_is_refused_and_skipped_by_dates(tmp_path, utc):
    root, _ = _three_versions(tmp_path)
    os.remove(os.path.join(root, "Config", "@Share", "db_staging", "2.db"))
    arc = hbk.Archive(root)
    with pytest.raises(hbk.VersionNotFound, match="no file lists"):
        arc.resolve_version("2")
    assert arc.resolve_version("@2025-10-10") == 1


@pytest.mark.parametrize("args", [
    ["get", GLOB, "OUT", "extra"],
    ["verify", GLOB, "extra"],
    ["list", "a", "b"],
    ["info", "x"],
    ["versions", "--allow-incomplete"],
    ["list", "--strict"],
    ["list", "-j", "4"],
])
def test_arguments_a_command_would_ignore_are_refused(tmp_path, args):
    root, _ = _three_versions(tmp_path)
    r = _hbk(tmp_path, root, *[str(tmp_path / "out") if x == "OUT" else x for x in args])
    assert r.returncode == 2, r.stdout + r.stderr


def test_info_lists_versions_even_when_none_is_complete(tmp_path):
    root, _ = _three_versions(tmp_path, statuses=("Backingup",) * 3)
    r = _hbk(tmp_path, root, "info")
    assert r.returncode == 4
    assert r.stdout.count("Backingup") == 3, r.stdout


def test_doctor_points_at_allow_incomplete(tmp_path):
    from hbkit import doctor
    root, _ = _three_versions(tmp_path, statuses=("Backingup",) * 3)
    out = doctor.render(doctor.diagnose(root))
    assert "--allow-incomplete" in out
    root2, _ = _three_versions(tmp_path / "b", statuses=("Complete", "Complete", "Backingup"))
    r = doctor.diagnose(root2, allow_incomplete=True)
    assert not any("newest complete" in w for w in r.warnings), r.warnings


def test_a_bad_selector_is_reported_before_asking_for_the_password(tmp_path):
    root, _ = _three_versions(tmp_path, encrypted=True)
    assert _hbk(tmp_path, root, "list", "--version", "abc").returncode == 2
    assert _hbk(tmp_path, root, "list", "--version", "9").returncode == 4


def test_unusual_digits_are_a_usage_error(tmp_path):
    root, _ = _three_versions(tmp_path)
    r = _hbk(tmp_path, root, "list", "--version", "²")
    assert r.returncode == 2 and "Traceback" not in r.stderr, r.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="needs bash; see the in-process pipe test")
def test_a_closed_pipe_is_not_an_internal_error(tmp_path):
    root, _ = _three_versions(tmp_path)
    env = {**os.environ, "HBK_CACHE": str(tmp_path / "cache")}
    p = subprocess.run(f'"{sys.executable}" -m hbkit.cli "{root}" list / | head -c 1 >/dev/null; '
                       'exit ${PIPESTATUS[0]}', shell=True, executable="/bin/bash",
                       capture_output=True, text=True, env=env)
    assert "Traceback" not in p.stderr and "please report" not in p.stderr, p.stderr
    assert p.returncode == 0, p.stderr                       # list: the reader had enough


# ------------------------------------------------------------- review round 3

def test_a_file_restored_by_an_older_hbkit_is_extracted_again(tmp_path):
    """A pre-version manifest record has an MD5 but no source offset: it cannot say which
    version the file came from, so neither plain nor --strict resume may keep it."""
    import json
    synth = _synth()
    a, b = b"A" * 4096, b"B" * 4096
    root = synth.build(str(tmp_path / "arc"), [synth.V({"firebird/x.bak": [a]}, T0),
                                               synth.V({"firebird/x.bak": [b]}, T0 + DAY)])
    out = tmp_path / "out"
    glob = "/db_staging/firebird/*"
    assert _hbk(tmp_path, root, "get", glob, out, "--version", "2").returncode == 0
    man = out / ".hbkit-restore.jsonl"
    recs = [json.loads(x) for x in man.read_text().splitlines()]
    man.write_text("".join(json.dumps({k: v for k, v in r.items() if k != "o"}) + "\n"
                           for r in recs))                   # as 0.4.7 wrote it
    for extra in ([], ["--strict"]):
        r = _hbk(tmp_path, root, "get", glob, out, "--version", "1", *extra)
        assert r.returncode == 0 and "1 extracted" in r.stdout, (extra, r.stdout)
        assert (out / "db_staging/firebird/x.bak").read_bytes() == a
        man.write_text("".join(json.dumps({k: v for k, v in r.items() if k != "o"}) + "\n"
                               for r in recs))


def test_replacing_a_file_from_another_version_marks_it_first(tmp_path):
    """Between the rename and the new record, the old record (another version's source)
    must already be void, so a run killed in that window cannot vouch for new bytes."""
    from hbkit import manifest as mf
    d = str(tmp_path)
    mf.append(d, "x", 4, "aa" * 16, ovf=64)
    mf.void(d, "x")
    (tmp_path / "x").write_bytes(b"BBBB")
    known = mf.load(d)
    assert not mf.is_complete(d, "x", 4, known, ovf=64)
    assert not mf.is_complete(d, "x", 4, known, ovf=128)


def test_double_dash_ends_options(tmp_path):
    synth = _synth()
    root = synth.build(str(tmp_path / "arc"), [synth.V({"d/-odd name.txt": [b"x" * 9]}, T0)])
    r = _hbk(tmp_path, root, "list", "--", "-odd")
    assert r.returncode == 0 and "-odd name.txt" in r.stdout, r.stdout + r.stderr
    r = _hbk(tmp_path, root, "list", "-odd")
    assert r.returncode == 2


@pytest.mark.parametrize(("spec", "code"), [("1x", 2), ("9", 4)])
def test_doctor_reports_a_bad_selector_as_such(tmp_path, spec, code):
    root, _ = _three_versions(tmp_path)
    r = _hbk(tmp_path, root, "doctor", "--version", spec)
    assert r.returncode == code, r.stdout + r.stderr
    assert "cannot be recovered" not in r.stdout


def test_before_accepts_a_leading_at(tmp_path):
    root, bodies = _three_versions(tmp_path)
    r = _hbk(tmp_path, root, "get", GLOB, tmp_path / "out", "--before", "@2025-10-10")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "out" / DB_ONE).read_bytes() == bodies[1]


def test_help_is_not_an_error(tmp_path):
    for flag in ("-h", "--help"):
        r = _hbk(tmp_path, flag)
        assert r.returncode == 0 and "Synology Hyper Backup" in r.stdout


def test_the_version_number_signals_the_new_behaviour():
    major, minor = (int(x) for x in hbkit.__version__.split(".")[:2])
    assert (major, minor) >= (0, 5)


# ------------------------------------------------------------- review round 4

def test_an_empty_version_list_refuses_the_default_unless_allowed(tmp_path):
    """A version_info.db with the right columns but no rows (a first backup that never
    finished) says nothing about which file lists are finished: same as unreadable."""
    import sqlite3
    root, bodies = _three_versions(tmp_path)
    db = sqlite3.connect(os.path.join(root, "Config", "version_info.db"))
    db.execute("DELETE FROM version_info")
    db.commit()
    db.close()
    r = _hbk(tmp_path, root, "get", GLOB, tmp_path / "out")
    assert r.returncode == 2 and "--allow-incomplete" in r.stderr, r.stdout + r.stderr
    assert not (tmp_path / "out" / DB_ONE).exists()
    r = _hbk(tmp_path, root, "get", GLOB, tmp_path / "out", "--allow-incomplete")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "out" / DB_ONE).read_bytes() == bodies[-1]


def test_tui_allow_incomplete_gets_the_same_fallback_as_get(tmp_path, monkeypatch):
    from hbkit import cli, tui
    root, _ = _three_versions(tmp_path)
    with open(os.path.join(root, "Config", "version_info.db"), "wb") as fh:
        fh.write(b"this is not a database" * 100)
    started = []

    class App:
        def __init__(self, *a, **kw):
            started.append(kw)

        def run(self):
            pass

    monkeypatch.setattr(tui, "HBKApp", App)
    monkeypatch.setattr(sys, "argv", ["hbk", root, "tui", "--allow-incomplete"])
    assert cli.main() == 0 and started


@pytest.mark.parametrize(("cmd", "mid_run", "after"), [
    (["list", "/"], 0, 0), (["versions"], 0, 0), (["info"], 0, 0),
    (["list", "zzz-no-such"], 0, 5),
    (["verify", GLOB], 141, 0), (["get", GLOB, "OUT"], 141, 0),
    (["verify", "/nothing/*"], 141, 5),
])
@pytest.mark.parametrize("on", ["write", "flush"])     # flush: output smaller than the buffer
def test_a_closed_pipe_is_success_only_for_output_only_commands(tmp_path, monkeypatch,
                                                                 cmd, mid_run, after, on):
    """`verify | head -n 1` must not turn a lost (possibly failing) result into exit 0.
    A pipe closing mid-run loses the outcome; one closing at the final flush, after the
    command finished, must keep the code it returned."""
    from hbkit import cli, index
    monkeypatch.setattr(index, "CACHE", str(tmp_path / "cache"))
    root, _ = _three_versions(tmp_path)

    class Closed:
        def write(self, s):
            if on == "write":
                raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    argv = [str(tmp_path / "out") if x == "OUT" else x for x in cmd]
    monkeypatch.setattr(sys, "argv", ["hbk", root, *argv])
    monkeypatch.setattr(sys, "stdout", Closed())
    assert cli.main_entry() == (mid_run if on == "write" else after)


def test_a_failed_version_without_file_lists_names_its_status(tmp_path):
    """Hyper Backup deletes a failed run's file lists after the next successful one: the
    refusal should say the version failed, not blame retention."""
    root, _ = _three_versions(tmp_path, statuses=("Complete", "Failure", "Complete"))
    os.remove(os.path.join(root, "Config", "@Share", "db_staging", "2.db"))
    with pytest.raises(hbk.VersionNotFound, match="status 'Failure'"):
        hbk.Archive(root).resolve_version("2", allow_incomplete=True)
