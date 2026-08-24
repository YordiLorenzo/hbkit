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


def test_lazycats_lists_shards_before_any_are_opened():
    """`doctor` describes an archive without extracting from it, so nothing has opened a
    file_chunk shard by the time it prints them. Iterating the dict reported an empty list
    and made a healthy archive look malformed."""
    cats = hbk._LazyCats({4: "/nonexistent/file_chunk4.index",
                          0: "/nonexistent/file_chunk0.index"})
    assert cats.known == [0, 4]
    assert len(cats) == 0, "listing shards must not open them"
