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
    assert hbk.resolve(str(tmp_path), "thing").endswith("thing.5")


def test_resolve_ignores_appledouble(tmp_path):
    (tmp_path / "._thing.9").write_text("x")
    (tmp_path / "thing").write_text("x")
    assert hbk.resolve(str(tmp_path), "thing").endswith("/thing")


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
