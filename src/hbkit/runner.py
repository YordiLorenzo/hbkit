#!/usr/bin/env python3
"""Parallel extraction runner.

Extraction is CPU-bound in Python (per-chunk struct/dict work holds the GIL), so threads
do not scale - measured flat at ~32 MB/s for 1..12 threads versus ~106 MB/s across 8
processes. We therefore fan out to real processes.

We launch them with subprocess rather than multiprocessing: multiprocessing's spawn
passes inherited fds, which breaks inside a Textual app whose stdio is redirected
("bad value(s) in fds_to_keep"). A plain subprocess with a JSON job file and JSON-lines
progress on stdout has no such coupling, and is identical for the CLI and the TUI.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque



# ------------------------------------------------------------------- worker side

def worker_main(job_path: str) -> int:
    import hashlib

    from . import archive as hbk
    from . import manifest as mf

    with open(job_path) as fh:
        job = json.load(fh)
    out_w = sys.stdout

    def emit(**kw):
        out_w.write(json.dumps(kw) + "\n")
        out_w.flush()

    try:
        arc = hbk.Archive(job["root"], password=os.environ.get("HBK_PASSWORD") or None)
    except Exception as e:                                   # noqa: BLE001
        emit(k="E", p="<archive>", m=f"{type(e).__name__}: {e}")
        return 0   # already reported; reader emits the done event

    dest, verify, check = job["dest"], job.get("verify", True), job.get("check", False)
    strict = job.get("strict", False)
    known = mf.load(dest) if not check else {}
    for path, ovf, size, mtime in job["items"]:
        rel = path.lstrip("/")
        target = os.path.join(dest, rel)
        try:
            if check:                      # integrity pass: rebuild, verify, write nothing
                arc.extract(ovf, size, verify=True, on_bytes=lambda n: emit(k="P", b=n))
                emit(k="O", p=rel)
                continue
            if mf.is_complete(dest, rel, size, known, verify=strict):
                emit(k="S", b=size, p=rel)
                continue
            d = os.path.dirname(target)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = target + ".part"
            h = hashlib.md5()

            class _Tee:
                __slots__ = ("f",)

                def __init__(self, f):
                    self.f = f

                def write(self, b):
                    h.update(b)
                    return self.f.write(b)

            with open(tmp, "wb") as fh:
                arc.extract(ovf, size, verify=verify, out=_Tee(fh),
                            on_bytes=lambda n: emit(k="P", b=n))
            os.replace(tmp, target)
            if mtime:
                os.utime(target, (mtime, mtime))
            mf.append(dest, rel, size, h.hexdigest())
            emit(k="O", p=rel)
        except Exception as e:                               # noqa: BLE001 - report, continue
            emit(k="E", p=rel, m=f"{type(e).__name__}: {e}")
    return 0


# ------------------------------------------------------------------- parent side

class Runner:
    """Fan `items` out over `n_workers` subprocesses; drain progress with poll()."""

    def __init__(self, root, dest, items, n_workers=8, verify=True, check=False,
                 python=None, password=None, strict=False):
        self.root, self.dest, self.items = root, dest, items
        self.check = check
        self.password = password
        self.strict = strict           # verify recorded MD5s when deciding to skip
        self.swept = 0
        self.n_workers = max(1, min(n_workers, len(items) or 1))
        self.verify = verify
        self.python = python or sys.executable
        self.total_files = len(items)
        self.total_bytes = sum(i[2] for i in items)
        self.ok = self.skipped = self.failed = 0
        self.bytes_done = 0
        self.errors: list[tuple[str, str]] = []
        # bounded: rendering the tail costs the same for 9 files or 500,000
        self.recent: deque[tuple[str, bool]] = deque(maxlen=15)
        self._q: queue.Queue = queue.Queue()
        self._procs: list[subprocess.Popen] = []
        self._threads: list[threading.Thread] = []
        self._tmp = None
        self._live = 0
        self.cancelled = False

    def start(self):
        if not self.check and os.path.isdir(self.dest):
            from . import manifest as mf
            self.swept = mf.sweep_parts(self.dest)   # tidy up an interrupted run
        self._tmp = tempfile.mkdtemp(prefix="hbk-job-")
        # Order by virtual-file offset and hand each worker a CONTIGUOUS block.
        # Hyper Backup appends as it backs up, so ovf order approximates bucket order:
        # each worker then sweeps the pool in one direction instead of eight heads
        # chasing eight regions of a spinning disk. Costs nothing - no extra I/O.
        items = self.items
        if os.environ.get("HBK_ORDER", "locality") == "locality":
            items = sorted(items, key=lambda x: x[1])
        per = -(-len(items) // self.n_workers)
        for i in range(self.n_workers):
            slice_ = items[i * per:(i + 1) * per]
            if not slice_:
                continue
            jp = os.path.join(self._tmp, f"job{i}.json")
            with open(jp, "w") as fh:
                json.dump({"root": self.root, "dest": self.dest, "verify": self.verify,
                           "check": self.check, "strict": self.strict, "items": slice_}, fh)
            env = dict(os.environ)
            # sys.executable is the *unwrapped* interpreter. Where hbkit reaches
            # sys.path through a wrapper script rather than site-packages - a Nix
            # build, a PYTHONPATH-based distro package - that interpreter cannot
            # import hbkit, and every worker dies before extracting a byte. Hand
            # the parent's own path down so the child resolves what the parent did.
            env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
            if self.password is not None:      # env, not the job file - no secret on disk
                env["HBK_PASSWORD"] = self.password
            p = subprocess.Popen(
                [self.python, "-m", "hbkit.runner", "--worker", jp],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, close_fds=True, env=env)
            self._procs.append(p)
            t = threading.Thread(target=self._reader, args=(p,), daemon=True)
            t.start()
            self._threads.append(t)
        self._live = len(self._procs)

    def _reader(self, p: subprocess.Popen):
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._q.put(json.loads(line))
            except ValueError:
                pass
        err = (p.stderr.read() or "").strip()
        rc = p.wait()
        if rc != 0 and not self.cancelled:
            self._q.put({"k": "E", "p": "<worker>", "m": err.splitlines()[-1] if err else f"exit {rc}"})
        self._q.put({"k": "D"})

    def poll(self, limit=8000) -> int:
        """Drain pending events into the counters. Returns how many were processed."""
        n = 0
        while n < limit:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                break
            n += 1
            k = ev.get("k")
            if k == "P":                      # bytes produced inside a file, live
                self.bytes_done += ev.get("b", 0)
            elif k == "O":
                self.ok += 1
                if ev.get("p"):
                    self.recent.append((ev["p"], False))
            elif k == "S":
                self.skipped += 1
                self.bytes_done += ev.get("b", 0)
                if ev.get("p"):
                    self.recent.append((ev["p"], True))
            elif k == "E":
                self.failed += 1
                self.errors.append((ev.get("p", "?"), ev.get("m", "")))
            elif k == "D":
                self._live -= 1
        return n

    @property
    def done_files(self) -> int:
        return self.ok + self.skipped

    @property
    def finished(self) -> bool:
        return self._live <= 0

    def cancel(self):
        self.cancelled = True
        for p in self._procs:
            if p.poll() is None:
                p.terminate()

    def cleanup(self):
        for p in self._procs:
            if p.poll() is None:
                p.kill()
        if self._tmp and os.path.isdir(self._tmp):
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        return worker_main(sys.argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
